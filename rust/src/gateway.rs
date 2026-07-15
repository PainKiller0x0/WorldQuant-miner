use crate::domain::AlphaMetrics;
use anyhow::{anyhow, Context, Result};
use async_trait::async_trait;
use reqwest::{
    header::{LOCATION, RETRY_AFTER},
    Client, StatusCode, Url,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SimulationResult {
    pub alpha_id: Option<String>,
    pub status: String,
    pub data: Value,
}

#[async_trait]
pub trait WorldQuantGateway: Send + Sync {
    async fn operators(&self) -> Result<Vec<String>>;
    async fn simulate(&self, expression: &str, settings: Value) -> Result<SimulationResult>;
    async fn find_unsubmitted(&self, metrics: &AlphaMetrics) -> Result<Vec<Value>>;
    async fn alpha(&self, alpha_id: &str) -> Result<Value>;
    async fn check_submission(&self, alpha_id: &str) -> Result<Value>;
    async fn submit(&self, alpha_id: &str) -> Result<Value>;
}

#[derive(Clone)]
pub struct LiveWorldQuant {
    client: Client,
    base_url: String,
    user_id: String,
    api_key: String,
    auth_lock: Arc<Mutex<bool>>,
    request_lock: Arc<Mutex<()>>,
    limiter: Arc<Mutex<AdaptiveRateController>>,
    limiter_state_path: PathBuf,
    limiter_state_lock: Arc<Mutex<()>>,
}

#[derive(Debug)]
struct AdaptiveRateController {
    timestamps: Vec<Instant>,
    current_tpm: usize,
    min_tpm: usize,
    max_tpm: usize,
    cooldown_until: Option<Instant>,
    request_latency_ewma_ms: Option<f64>,
    simulation_duration_ewma_sec: Option<f64>,
    successful_requests: usize,
    last_adjustment: Instant,
    last_adjustment_reason: &'static str,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct LimiterSnapshot {
    mode: String,
    current_tpm_limit: usize,
    min_tpm_limit: usize,
    max_tpm_limit: usize,
    request_latency_ewma_ms: Option<f64>,
    simulation_duration_ewma_sec: Option<f64>,
    cooldown_until_epoch: Option<i64>,
    last_adjustment_reason: String,
    updated_at_epoch: i64,
}

impl AdaptiveRateController {
    const MIN_TPM: usize = 6;
    const MAX_TPM: usize = 60;

    fn new(initial_tpm: usize, now: Instant) -> Self {
        Self {
            timestamps: Vec::new(),
            current_tpm: initial_tpm.clamp(Self::MIN_TPM, Self::MAX_TPM),
            min_tpm: Self::MIN_TPM,
            max_tpm: Self::MAX_TPM,
            cooldown_until: None,
            request_latency_ewma_ms: None,
            simulation_duration_ewma_sec: None,
            successful_requests: 0,
            last_adjustment: now,
            last_adjustment_reason: "startup",
        }
    }

    fn current_tpm(&self) -> usize {
        self.current_tpm
    }

    fn throttle_wait(&mut self, now: Instant) -> Duration {
        self.timestamps
            .retain(|timestamp| now.duration_since(*timestamp) < Duration::from_secs(60));
        if let Some(until) = self.cooldown_until {
            if until > now {
                return until.duration_since(now);
            }
            self.cooldown_until = None;
        }
        if self.timestamps.len() >= self.current_tpm {
            return Duration::from_secs(60).saturating_sub(now.duration_since(self.timestamps[0]))
                + Duration::from_millis(100);
        }
        self.timestamps.push(now);
        Duration::ZERO
    }

    fn observe_request_success(&mut self, latency: Duration, now: Instant) {
        self.request_latency_ewma_ms = Some(ewma(
            self.request_latency_ewma_ms,
            latency.as_secs_f64() * 1000.0,
            0.2,
        ));
        self.successful_requests += 1;
        let healthy = self.request_latency_ewma_ms.unwrap_or(f64::MAX) <= 1_500.0;
        if healthy
            && self.successful_requests >= 10
            && now.duration_since(self.last_adjustment) >= Duration::from_secs(30)
        {
            self.increase((self.current_tpm / 10).max(1), now, "healthy_requests");
        }
    }

    fn observe_rate_limit(&mut self, retry_after: Duration, now: Instant) {
        self.current_tpm = ((self.current_tpm * 2) / 3).max(self.min_tpm);
        self.cooldown_until = Some(now + retry_after.max(Duration::from_secs(1)));
        self.successful_requests = 0;
        self.last_adjustment = now;
        self.last_adjustment_reason = "http_429";
    }

    fn observe_simulation(&mut self, duration: Duration, status: &str, now: Instant) {
        let seconds = duration.as_secs_f64();
        if status == "TIMEOUT" {
            self.decrease((self.current_tpm / 4).max(3), now, "simulation_failed");
            return;
        }
        if status != "COMPLETE" {
            self.last_adjustment_reason = "simulation_rejected";
            return;
        }
        self.simulation_duration_ewma_sec =
            Some(ewma(self.simulation_duration_ewma_sec, seconds, 0.3));
        if seconds <= 120.0 {
            self.increase((self.current_tpm / 8).max(2), now, "fast_simulation");
        } else if seconds <= 240.0 {
            self.increase(1, now, "healthy_simulation");
        } else if seconds > 600.0 {
            self.decrease((self.current_tpm / 10).max(2), now, "slow_simulation");
        }
    }

    fn increase(&mut self, amount: usize, now: Instant, reason: &'static str) {
        self.current_tpm = (self.current_tpm + amount).min(self.max_tpm);
        self.successful_requests = 0;
        self.last_adjustment = now;
        self.last_adjustment_reason = reason;
    }

    fn decrease(&mut self, amount: usize, now: Instant, reason: &'static str) {
        self.current_tpm = self.current_tpm.saturating_sub(amount).max(self.min_tpm);
        self.successful_requests = 0;
        self.last_adjustment = now;
        self.last_adjustment_reason = reason;
    }

    fn cooldown_remaining(&self, now: Instant) -> Duration {
        self.cooldown_until
            .and_then(|until| (until > now).then(|| until.duration_since(now)))
            .unwrap_or_default()
    }

    fn snapshot(&self, now: Instant) -> LimiterSnapshot {
        let now_epoch = chrono::Utc::now().timestamp();
        let cooldown = self.cooldown_remaining(now);
        LimiterSnapshot {
            mode: "adaptive".into(),
            current_tpm_limit: self.current_tpm,
            min_tpm_limit: self.min_tpm,
            max_tpm_limit: self.max_tpm,
            request_latency_ewma_ms: self.request_latency_ewma_ms.map(round_one),
            simulation_duration_ewma_sec: self.simulation_duration_ewma_sec.map(round_one),
            cooldown_until_epoch: (!cooldown.is_zero())
                .then_some(now_epoch + cooldown.as_secs() as i64),
            last_adjustment_reason: self.last_adjustment_reason.into(),
            updated_at_epoch: now_epoch,
        }
    }
}

fn ewma(previous: Option<f64>, sample: f64, alpha: f64) -> f64 {
    previous
        .map(|value| value * (1.0 - alpha) + sample * alpha)
        .unwrap_or(sample)
}

fn round_one(value: f64) -> f64 {
    (value * 10.0).round() / 10.0
}

fn initial_tpm_from_state(path: &Path) -> usize {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str::<LimiterSnapshot>(&raw).ok())
        .map(|snapshot| snapshot.current_tpm_limit)
        .unwrap_or(15)
        .clamp(
            AdaptiveRateController::MIN_TPM,
            AdaptiveRateController::MAX_TPM,
        )
}

impl LiveWorldQuant {
    pub fn new(
        user_id: String,
        api_key: String,
        limiter_state_path: impl Into<PathBuf>,
    ) -> Result<Self> {
        let limiter_state_path = limiter_state_path.into();
        let now = Instant::now();
        let limiter = AdaptiveRateController::new(initial_tpm_from_state(&limiter_state_path), now);
        let instance = Self {
            client: Client::builder()
                .cookie_store(true)
                .connect_timeout(Duration::from_secs(20))
                .timeout(Duration::from_secs(120))
                .build()?,
            base_url: "https://api.worldquantbrain.com".into(),
            user_id,
            api_key,
            auth_lock: Arc::new(Mutex::new(false)),
            request_lock: Arc::new(Mutex::new(())),
            limiter: Arc::new(Mutex::new(limiter)),
            limiter_state_path,
            limiter_state_lock: Arc::new(Mutex::new(())),
        };
        instance.persist_limiter_state_sync();
        Ok(instance)
    }

    async fn authenticate(&self) -> Result<()> {
        let mut state = self.auth_lock.lock().await;
        if *state {
            return Ok(());
        }
        let response = self
            .client
            .post(format!("{}/authentication", self.base_url))
            .basic_auth(&self.user_id, Some(&self.api_key))
            .send()
            .await
            .context("WorldQuant authentication request")?;
        if !response.status().is_success() {
            return Err(anyhow!(
                "WorldQuant authentication failed: {}",
                response.status()
            ));
        }
        *state = true;
        Ok(())
    }

    async fn throttle(&self) {
        loop {
            let wait = {
                let mut bucket = self.limiter.lock().await;
                bucket.throttle_wait(Instant::now())
            };
            if wait.is_zero() {
                return;
            }
            tokio::time::sleep(wait).await;
        }
    }

    async fn request_unchecked(
        &self,
        method: reqwest::Method,
        url: String,
        body: Option<Value>,
    ) -> Result<reqwest::Response> {
        let _serial = self.request_lock.lock().await;
        self.authenticate().await?;
        self.throttle().await;
        let mut request = self.client.request(method, url);
        if let Some(body) = body {
            request = request.json(&body);
        }
        let started = Instant::now();
        let response = request.send().await.context("WorldQuant request")?;
        let latency = started.elapsed();
        if response.status() == StatusCode::UNAUTHORIZED {
            *self.auth_lock.lock().await = false;
        }
        let status = response.status();
        let retry_after =
            (status == StatusCode::TOO_MANY_REQUESTS).then(|| retry_after(&response, 60));
        let (before, after, reason, persist) = {
            let mut limiter = self.limiter.lock().await;
            let before = limiter.current_tpm();
            if let Some(retry_after) = retry_after {
                limiter.observe_rate_limit(retry_after, Instant::now());
            } else if status.is_success() {
                limiter.observe_request_success(latency, Instant::now());
            }
            let after = limiter.current_tpm();
            let persist = before != after
                || status == StatusCode::TOO_MANY_REQUESTS
                || (status.is_success() && limiter.successful_requests % 5 == 0);
            (before, after, limiter.last_adjustment_reason, persist)
        };
        if before != after || status == StatusCode::TOO_MANY_REQUESTS {
            tracing::info!(
                before_tpm = before,
                current_tpm = after,
                reason,
                http_status = %status,
                latency_ms = round_one(latency.as_secs_f64() * 1000.0),
                "adaptive WQ TPM adjusted"
            );
        }
        if persist {
            self.persist_limiter_state().await;
        }
        Ok(response)
    }

    fn persist_limiter_state_sync(&self) {
        let Ok(limiter) = self.limiter.try_lock() else {
            return;
        };
        let snapshot = limiter.snapshot(Instant::now());
        if let Some(parent) = self.limiter_state_path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Ok(value) = serde_json::to_vec_pretty(&snapshot) {
            let _ = std::fs::write(&self.limiter_state_path, value);
        }
    }

    async fn persist_limiter_state(&self) {
        let _write = self.limiter_state_lock.lock().await;
        let snapshot = self.limiter.lock().await.snapshot(Instant::now());
        if let Some(parent) = self.limiter_state_path.parent() {
            if tokio::fs::create_dir_all(parent).await.is_err() {
                return;
            }
        }
        let temporary = self
            .limiter_state_path
            .with_extension(format!("json.{}.tmp", std::process::id()));
        let Ok(value) = serde_json::to_vec_pretty(&snapshot) else {
            return;
        };
        if tokio::fs::write(&temporary, value).await.is_ok() {
            let _ = tokio::fs::rename(temporary, &self.limiter_state_path).await;
        }
    }

    async fn request(
        &self,
        method: reqwest::Method,
        url: String,
        body: Option<Value>,
    ) -> Result<reqwest::Response> {
        Ok(self
            .request_unchecked(method, url, body)
            .await?
            .error_for_status()?)
    }
}

#[async_trait]
impl WorldQuantGateway for LiveWorldQuant {
    async fn operators(&self) -> Result<Vec<String>> {
        let response = self
            .request(
                reqwest::Method::GET,
                format!("{}/operators", self.base_url),
                None,
            )
            .await?;
        let value: Value = response.json().await.context("parse operators")?;
        let values = value
            .get("results")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_else(|| value.as_array().cloned().unwrap_or_default());
        Ok(values
            .into_iter()
            .filter_map(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .or_else(|| v.get("name").and_then(Value::as_str).map(str::to_owned))
                    .or_else(|| v.get("operator").and_then(Value::as_str).map(str::to_owned))
            })
            .collect())
    }

    async fn simulate(&self, expression: &str, settings: Value) -> Result<SimulationResult> {
        let simulation_started = Instant::now();
        let body = json!({"type":"REGULAR", "regular": expression, "settings": settings});
        let response = self
            .request(
                reqwest::Method::POST,
                format!("{}/simulations", self.base_url),
                Some(body),
            )
            .await?;
        let location = response
            .headers()
            .get(LOCATION)
            .and_then(|v| v.to_str().ok())
            .ok_or_else(|| anyhow!("simulation response has no location"))?
            .to_owned();
        let poll_url = if location.starts_with("http") {
            location
        } else {
            format!("{}{}", self.base_url, location)
        };
        let result = loop {
            if simulation_started.elapsed() > Duration::from_secs(1800) {
                break SimulationResult {
                    alpha_id: None,
                    status: "TIMEOUT".into(),
                    data: Value::Null,
                };
            }
            let response = self
                .request(reqwest::Method::GET, poll_url.clone(), None)
                .await?;
            let value: Value = response.json().await.context("parse simulation status")?;
            let status = value
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("UNKNOWN")
                .to_owned();
            match status.as_str() {
                "COMPLETE" => {
                    let alpha_id = value
                        .get("alpha")
                        .and_then(Value::as_str)
                        .map(str::to_owned);
                    let data = if let Some(id) = &alpha_id {
                        self.request(
                            reqwest::Method::GET,
                            format!("{}/alphas/{}", self.base_url, id),
                            None,
                        )
                        .await?
                        .json()
                        .await?
                    } else {
                        value.clone()
                    };
                    break SimulationResult {
                        alpha_id,
                        status,
                        data,
                    };
                }
                "ERROR" => {
                    break SimulationResult {
                        alpha_id: None,
                        status,
                        data: value,
                    }
                }
                _ => tokio::time::sleep(Duration::from_secs(10)).await,
            }
        };
        let duration = simulation_started.elapsed();
        let (before, after, reason) = {
            let mut limiter = self.limiter.lock().await;
            let before = limiter.current_tpm();
            limiter.observe_simulation(duration, &result.status, Instant::now());
            (
                before,
                limiter.current_tpm(),
                limiter.last_adjustment_reason,
            )
        };
        tracing::info!(
            duration_secs = round_one(duration.as_secs_f64()),
            status = %result.status,
            before_tpm = before,
            current_tpm = after,
            reason,
            "WorldQuant simulation observed by adaptive limiter"
        );
        self.persist_limiter_state().await;
        Ok(result)
    }

    async fn find_unsubmitted(&self, metrics: &AlphaMetrics) -> Result<Vec<Value>> {
        let mut url = Url::parse(&format!("{}/users/self/alphas", self.base_url))?;
        url.query_pairs_mut()
            .append_pair("limit", "20")
            .append_pair("offset", "0")
            .append_pair("status", "UNSUBMITTED")
            .append_pair("order", "-dateCreated")
            .append_pair("is.sharpe", &metrics.sharpe.to_string())
            .append_pair("is.returns", &metrics.returns.to_string())
            .append_pair("is.turnover", &metrics.turnover.to_string());
        let value: Value = self
            .request(reqwest::Method::GET, url.to_string(), None)
            .await?
            .json()
            .await
            .context("parse unsubmitted alpha lookup")?;
        Ok(value
            .get("results")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
    }

    async fn alpha(&self, alpha_id: &str) -> Result<Value> {
        self.request(
            reqwest::Method::GET,
            format!("{}/alphas/{}", self.base_url, alpha_id),
            None,
        )
        .await?
        .json()
        .await
        .context("parse alpha detail")
    }

    async fn check_submission(&self, alpha_id: &str) -> Result<Value> {
        let url = format!("{}/alphas/{}/check", self.base_url, alpha_id);
        let response = self
            .request_unchecked(reqwest::Method::GET, url, None)
            .await?;
        if response.status() == StatusCode::TOO_MANY_REQUESTS {
            let wait = retry_after(&response, 60);
            return Err(anyhow!(
                "submission check rate limited for {alpha_id}; retry after {}s",
                wait.as_secs()
            ));
        }
        let response = response.error_for_status()?;
        let bytes = response.bytes().await?;
        let check_response = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).context("parse submission check response")?
        };
        let detail = self.alpha(alpha_id).await?;
        Ok(if check_response.get("is").is_some() {
            merge_check_detail(detail, &check_response)
        } else if detail.is_null() {
            check_response
        } else {
            detail
        })
    }

    async fn submit(&self, alpha_id: &str) -> Result<Value> {
        let url = format!("{}/alphas/{}/submit", self.base_url, alpha_id);
        let response = self
            .request_unchecked(reqwest::Method::POST, url.clone(), None)
            .await?;
        if response.status() == StatusCode::CONFLICT {
            return Ok(json!({"status":"already_submitted","alpha_id":alpha_id}));
        }
        if !response.status().is_success() {
            return submit_rejection_response(response).await;
        }

        let started = Instant::now();
        loop {
            if started.elapsed() > Duration::from_secs(1200) {
                return Err(anyhow!("submission timed out for {alpha_id}"));
            }
            let response = self
                .request_unchecked(reqwest::Method::GET, url.clone(), None)
                .await?;
            if response.status() == StatusCode::NOT_FOUND {
                return Ok(json!({"status":"submitted","alpha_id":alpha_id}));
            }
            if response.status() == StatusCode::TOO_MANY_REQUESTS {
                tokio::time::sleep(retry_after(&response, 60)).await;
                continue;
            }
            if !response.status().is_success() {
                return submit_rejection_response(response).await;
            }
            let bytes = response.bytes().await?;
            if bytes.is_empty() {
                tokio::time::sleep(Duration::from_secs(10)).await;
                continue;
            }
            let value: Value =
                serde_json::from_slice(&bytes).context("parse submission response")?;
            return Ok(value);
        }
    }
}

async fn submit_rejection_response(response: reqwest::Response) -> Result<Value> {
    let http_status = response.status();
    let bytes = response.bytes().await?;
    let payload = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or_else(
            |_| json!({"message": String::from_utf8_lossy(&bytes).trim().to_owned()}),
        )
    };
    let message = payload
        .get("message")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| format!("alpha submit returned {http_status}"));
    Ok(json!({
        "status": "rejected",
        "http_status": http_status.as_u16(),
        "message": message,
        "response": payload
    }))
}

fn retry_after(response: &reqwest::Response, default_seconds: u64) -> Duration {
    response
        .headers()
        .get(RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<f64>().ok())
        .map(Duration::from_secs_f64)
        .unwrap_or_else(|| Duration::from_secs(default_seconds))
        .max(Duration::from_secs(1))
}

#[cfg(test)]
fn has_pending_checks(detail: &Value) -> bool {
    detail
        .get("is")
        .and_then(|value| value.get("checks"))
        .or_else(|| detail.get("checks"))
        .and_then(Value::as_array)
        .map(|checks| {
            checks
                .iter()
                .any(|check| check.get("result").and_then(Value::as_str) == Some("PENDING"))
        })
        .unwrap_or(true)
}

#[cfg(test)]
fn has_completed_checks(detail: &Value) -> bool {
    detail
        .get("is")
        .and_then(|value| value.get("checks"))
        .or_else(|| detail.get("checks"))
        .and_then(Value::as_array)
        .map(|checks| {
            !checks.is_empty()
                && checks.iter().all(|check| {
                    matches!(
                        check.get("result").and_then(Value::as_str),
                        Some("PASS" | "FAIL")
                    )
                })
        })
        .unwrap_or(false)
}

fn merge_check_detail(mut detail: Value, check_response: &Value) -> Value {
    let Some(check_is) = check_response.get("is").and_then(Value::as_object) else {
        return detail;
    };
    let Some(detail_object) = detail.as_object_mut() else {
        return check_response.clone();
    };
    let detail_is = detail_object.entry("is").or_insert_with(|| json!({}));
    let Some(detail_is_object) = detail_is.as_object_mut() else {
        *detail_is = check_response.get("is").cloned().unwrap_or(Value::Null);
        return detail;
    };
    for (key, value) in check_is {
        detail_is_object.insert(key.clone(), value.clone());
    }
    detail
}

#[derive(Default)]
pub struct FakeWorldQuant;

#[async_trait]
impl WorldQuantGateway for FakeWorldQuant {
    async fn operators(&self) -> Result<Vec<String>> {
        Ok(vec!["rank".into(), "ts_mean".into()])
    }
    async fn simulate(&self, expression: &str, _settings: Value) -> Result<SimulationResult> {
        Ok(SimulationResult {
            alpha_id: Some("fake-alpha".into()),
            status: "COMPLETE".into(),
            data: json!({"regular":{"code":expression},"fake":true}),
        })
    }
    async fn find_unsubmitted(&self, _metrics: &AlphaMetrics) -> Result<Vec<Value>> {
        Ok(Vec::new())
    }
    async fn alpha(&self, alpha_id: &str) -> Result<Value> {
        Ok(json!({"id":alpha_id,"status":"UNSUBMITTED"}))
    }
    async fn check_submission(&self, alpha_id: &str) -> Result<Value> {
        Ok(json!({"status":"checked","alpha_id":alpha_id}))
    }
    async fn submit(&self, alpha_id: &str) -> Result<Value> {
        Ok(json!({"status":"submitted","alpha_id":alpha_id}))
    }
}

#[cfg(test)]
mod tests {
    use super::{
        has_completed_checks, has_pending_checks, merge_check_detail, AdaptiveRateController,
    };
    use serde_json::json;
    use std::time::{Duration, Instant};

    #[test]
    fn adaptive_rate_increases_for_fast_healthy_worldquant_responses() {
        let started = Instant::now();
        let mut limiter = AdaptiveRateController::new(15, started);

        for offset in 0..12 {
            limiter.observe_request_success(
                Duration::from_millis(250),
                started + Duration::from_secs(31 + offset),
            );
        }

        assert!(limiter.current_tpm() > 15);
        let after_requests = limiter.current_tpm();
        limiter.observe_simulation(
            Duration::from_secs(75),
            "COMPLETE",
            started + Duration::from_secs(90),
        );
        assert!(limiter.current_tpm() > after_requests);
    }

    #[test]
    fn adaptive_rate_uses_multiplicative_decrease_and_server_cooldown_on_429() {
        let started = Instant::now();
        let mut limiter = AdaptiveRateController::new(30, started);

        limiter.observe_rate_limit(Duration::from_secs(45), started + Duration::from_secs(5));

        assert_eq!(limiter.current_tpm(), 20);
        assert_eq!(
            limiter.cooldown_remaining(started + Duration::from_secs(10)),
            Duration::from_secs(40)
        );
    }

    #[test]
    fn adaptive_rate_reduces_pressure_after_a_slow_or_timed_out_simulation() {
        let started = Instant::now();
        let mut limiter = AdaptiveRateController::new(30, started);

        limiter.observe_simulation(
            Duration::from_secs(11 * 60),
            "COMPLETE",
            started + Duration::from_secs(11 * 60),
        );
        let after_slow = limiter.current_tpm();
        limiter.observe_simulation(
            Duration::from_secs(30 * 60),
            "TIMEOUT",
            started + Duration::from_secs(31 * 60),
        );

        assert!(after_slow < 30);
        assert!(limiter.current_tpm() < after_slow);
        assert!(limiter.current_tpm() >= 6);
    }

    #[test]
    fn expression_rejection_does_not_reduce_worldquant_request_rate() {
        let started = Instant::now();
        let mut limiter = AdaptiveRateController::new(24, started);

        limiter.observe_simulation(
            Duration::from_secs(20),
            "ERROR",
            started + Duration::from_secs(20),
        );

        assert_eq!(limiter.current_tpm(), 24);
    }

    #[test]
    fn pending_check_detection_reads_is_checks() {
        assert!(has_pending_checks(&json!({
            "is":{"checks":[{"name":"SELF_CORRELATION","result":"PENDING"}]}
        })));
        assert!(!has_pending_checks(&json!({
            "is":{"checks":[{"name":"SELF_CORRELATION","result":"PASS"}]}
        })));
    }

    #[test]
    fn completed_check_response_overrides_stale_detail_and_keeps_metrics() {
        let detail = json!({"is":{
            "fitness":1.38,
            "sharpe":2.1,
            "checks":[{"name":"SELF_CORRELATION","result":"PENDING"}]
        }});
        let check_response = json!({"is":{
            "checks":[{"name":"SELF_CORRELATION","result":"PASS","value":0.7}],
            "selfCorrelated":{"max":0.7}
        }});

        let merged = merge_check_detail(detail, &check_response);

        assert_eq!(merged["is"]["fitness"], 1.38);
        assert_eq!(merged["is"]["sharpe"], 2.1);
        assert_eq!(merged["is"]["checks"][0]["result"], "PASS");
        assert_eq!(merged["is"]["selfCorrelated"]["max"], 0.7);
        assert!(has_completed_checks(&check_response));
        assert!(!has_pending_checks(&merged));
    }

    #[test]
    fn check_error_is_not_a_completed_submission_check() {
        let check_response = json!({"is":{"checks":[
            {"name":"LOW_SHARPE","result":"PASS"},
            {"name":"SELF_CORRELATION","result":"ERROR"}
        ]}});

        assert!(!has_completed_checks(&check_response));
    }
}

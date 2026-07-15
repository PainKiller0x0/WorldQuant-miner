use crate::domain::AlphaMetrics;
use anyhow::{anyhow, Context, Result};
use async_trait::async_trait;
use reqwest::{
    header::{LOCATION, RETRY_AFTER},
    Client, StatusCode, Url,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
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
    limiter: Arc<Mutex<TokenBucket>>,
}

#[derive(Debug)]
struct TokenBucket {
    timestamps: Vec<Instant>,
    tpm: usize,
    last_429: Option<Instant>,
}

impl LiveWorldQuant {
    pub fn new(user_id: String, api_key: String) -> Result<Self> {
        let tpm = std::env::var("WQ_TPM")
            .ok()
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(15)
            .clamp(1, 60);
        Ok(Self {
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
            limiter: Arc::new(Mutex::new(TokenBucket {
                timestamps: Vec::new(),
                tpm,
                last_429: None,
            })),
        })
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
                let now = Instant::now();
                bucket
                    .timestamps
                    .retain(|t| now.duration_since(*t) < Duration::from_secs(60));
                let cooldown = bucket
                    .last_429
                    .map(|t| Duration::from_secs(60).saturating_sub(now.duration_since(t)))
                    .unwrap_or_default();
                if cooldown > Duration::ZERO {
                    cooldown
                } else if bucket.timestamps.len() >= bucket.tpm {
                    Duration::from_secs(60).saturating_sub(now.duration_since(bucket.timestamps[0]))
                        + Duration::from_millis(100)
                } else {
                    bucket.timestamps.push(now);
                    Duration::ZERO
                }
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
        let response = request.send().await.context("WorldQuant request")?;
        if response.status() == StatusCode::UNAUTHORIZED {
            *self.auth_lock.lock().await = false;
        }
        if response.status() == StatusCode::TOO_MANY_REQUESTS {
            self.limiter.lock().await.last_429 = Some(Instant::now());
        }
        Ok(response)
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
        let started = Instant::now();
        loop {
            if started.elapsed() > Duration::from_secs(1800) {
                return Ok(SimulationResult {
                    alpha_id: None,
                    status: "TIMEOUT".into(),
                    data: Value::Null,
                });
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
                    return Ok(SimulationResult {
                        alpha_id,
                        status,
                        data,
                    });
                }
                "ERROR" => {
                    return Ok(SimulationResult {
                        alpha_id: None,
                        status,
                        data: value,
                    })
                }
                _ => tokio::time::sleep(Duration::from_secs(10)).await,
            }
        }
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
        let started = Instant::now();
        loop {
            if started.elapsed() > Duration::from_secs(120) {
                return Err(anyhow!("submission check timed out for {alpha_id}"));
            }
            let response = self
                .request_unchecked(reqwest::Method::GET, url.clone(), None)
                .await?;
            if response.status() == StatusCode::TOO_MANY_REQUESTS {
                tokio::time::sleep(retry_after(&response, 60)).await;
                continue;
            }
            let response = response.error_for_status()?;
            let retry = response
                .headers()
                .get(RETRY_AFTER)
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.parse::<f64>().ok())
                .map(Duration::from_secs_f64);
            let bytes = response.bytes().await?;
            let value = if bytes.is_empty() {
                Value::Null
            } else {
                serde_json::from_slice(&bytes).context("parse submission check response")?
            };
            if has_completed_checks(&value) {
                let detail = self.alpha(alpha_id).await?;
                return Ok(merge_check_detail(detail, &value));
            }
            if let Some(wait) = retry {
                tokio::time::sleep(wait.max(Duration::from_secs(1))).await;
                continue;
            }
            let detail = self.alpha(alpha_id).await?;
            if has_pending_checks(&detail) {
                tokio::time::sleep(Duration::from_secs(10)).await;
                continue;
            }
            return Ok(if detail.is_null() { value } else { detail });
        }
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
            return Err(anyhow!("alpha submit returned {}", response.status()));
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
            let response = response.error_for_status()?;
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

fn has_completed_checks(detail: &Value) -> bool {
    detail
        .get("is")
        .and_then(|value| value.get("checks"))
        .or_else(|| detail.get("checks"))
        .and_then(Value::as_array)
        .map(|checks| {
            !checks.is_empty()
                && checks
                    .iter()
                    .all(|check| check.get("result").and_then(Value::as_str) != Some("PENDING"))
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
    use super::{has_completed_checks, has_pending_checks, merge_check_detail};
    use serde_json::json;

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
}

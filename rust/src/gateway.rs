use anyhow::{anyhow, Context, Result};
use async_trait::async_trait;
use reqwest::{header::LOCATION, Client, StatusCode};
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
                tpm: 60,
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

    async fn request(
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
        Ok(response.error_for_status()?)
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

    async fn submit(&self, alpha_id: &str) -> Result<Value> {
        let value = self
            .request(
                reqwest::Method::POST,
                format!("{}/alphas/{}/submit", self.base_url, alpha_id),
                Some(json!({})),
            )
            .await?
            .json()
            .await?;
        Ok(value)
    }
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
    async fn submit(&self, alpha_id: &str) -> Result<Value> {
        Ok(json!({"status":"submitted","alpha_id":alpha_id}))
    }
}

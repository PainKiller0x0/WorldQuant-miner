use crate::config::{AppConfig, ModelConfig};
use crate::domain::{ExpressionPolicy, Role};
use anyhow::{anyhow, Context, Result};
use async_trait::async_trait;
use reqwest::Client;
use serde_json::{json, Value};
use std::time::Duration;

#[async_trait]
pub trait ModelGateway: Send + Sync {
    async fn generate(&self, role: Role, prompt: &str) -> Result<String>;
}

#[derive(Clone)]
pub struct LiveModelGateway {
    client: Client,
    miner: Vec<ModelConfig>,
    evolver: Vec<ModelConfig>,
}

impl LiveModelGateway {
    pub fn new(config: &AppConfig) -> Result<Self> {
        Ok(Self {
            client: Client::builder()
                .connect_timeout(Duration::from_secs(15))
                .timeout(Duration::from_secs(180))
                .build()?,
            miner: config.miner_models.clone(),
            evolver: config.evolver_models.clone(),
        })
    }

    async fn call(&self, role: Role, model: &ModelConfig, prompt: &str) -> Result<String> {
        let url = completion_url(&model.base_url);
        tracing::info!(?role, model=%model.model_name, endpoint=%url, "calling model");
        let temperature = match role {
            Role::Miner => 0.8,
            Role::Evolver => 0.55,
        };
        let mut body = json!({
            "model": model.model_name,
            "messages": [
                {"role":"system","content":system_prompt(role)},
                {"role":"user","content":prompt}
            ],
            "temperature": temperature,
            "max_tokens": 1200
        });
        if model.model_name.to_ascii_lowercase().contains("glm") {
            body["thinking"] = json!({"type":"disabled"});
        }
        let response = self
            .client
            .post(url)
            .bearer_auth(&model.api_key)
            .json(&body)
            .send()
            .await
            .context("LLM request")?;
        let status = response.status();
        let value: Value = response.json().await.context("parse LLM response")?;
        if !status.is_success() {
            return Err(anyhow!("LLM returned {}", status));
        }
        tracing::info!(?role, model=%model.model_name, "model response received");
        response_text(&value).ok_or_else(|| anyhow!("LLM response has no message content"))
    }
}

fn system_prompt(role: Role) -> &'static str {
    match role {
        Role::Miner => "You are the Miner in a WorldQuant Brain research pipeline. Generate diverse, syntactically valid FASTEXPR candidates that obey the user's exact field, operator, and output constraints. Return expressions only; never invent identifiers or add prose.",
        Role::Evolver => "You are the Evolver in a WorldQuant Brain research pipeline. Create controlled structural mutations of the supplied high-quality parent while obeying the user's exact field, operator, and output constraints. Return expressions only; never add prose or cosmetic-only variants.",
    }
}

#[async_trait]
impl ModelGateway for LiveModelGateway {
    async fn generate(&self, role: Role, prompt: &str) -> Result<String> {
        let models = match role {
            Role::Miner => &self.miner,
            Role::Evolver => &self.evolver,
        };
        if models.is_empty() {
            return Err(anyhow!("no models configured for {:?}", role));
        }
        let mut errors = Vec::new();
        for model in models {
            match self.call(role, model, prompt).await {
                Ok(value) => return Ok(value),
                Err(error) => {
                    tracing::warn!(
                        ?role,
                        model=%model.model_name,
                        error=%error,
                        "model call failed"
                    );
                    errors.push(format!("{}: {}", model.model_name, error));
                }
            }
        }
        Err(anyhow!(
            "all {:?} models failed: {}",
            role,
            errors.join("; ")
        ))
    }
}

fn completion_url(base: &str) -> String {
    let base = base.trim_end_matches('/');
    if base.ends_with("/chat/completions") {
        base.to_owned()
    } else if base.ends_with("/v1")
        || base.ends_with("/v3")
        || base.ends_with("/v4")
        || base.ends_with("/api/paas/v4")
        || base.ends_with("/api/v3")
    {
        format!("{base}/chat/completions")
    } else {
        format!("{base}/v1/chat/completions")
    }
}

fn content_text(value: &Value) -> Option<String> {
    if let Some(text) = value.as_str() {
        let text = text.trim();
        return (!text.is_empty()).then(|| text.to_owned());
    }
    value.as_array().and_then(|parts| {
        let text = parts
            .iter()
            .filter_map(|part| part.get("text").and_then(Value::as_str))
            .collect::<Vec<_>>()
            .join("");
        (!text.trim().is_empty()).then_some(text)
    })
}

fn response_text(value: &Value) -> Option<String> {
    let choice = value.get("choices")?.as_array()?.first()?;
    let message = choice.get("message");
    ["content", "reasoning_content"]
        .iter()
        .filter_map(|key| message.and_then(|message| message.get(*key)))
        .find_map(content_text)
        .or_else(|| choice.get("text").and_then(content_text))
}

pub fn extract_expressions(text: &str, policy: &ExpressionPolicy) -> Vec<String> {
    let mut expressions = Vec::new();
    let cleaned = text.replace("<think>", "").replace("</think>", "");
    for line in cleaned.lines() {
        let candidate = line
            .trim()
            .trim_start_matches(|c: char| c == '-' || c == '*' || c == ' ' || c as u32 == 96)
            .trim();
        if candidate.is_empty() || candidate.starts_with('#') || candidate.len() > 500 {
            continue;
        }
        if let Ok(expression) = policy.validate(candidate) {
            if !expressions.contains(&expression) {
                expressions.push(expression);
            }
        }
    }
    let mut starts = Vec::new();
    for operator in &policy.operators {
        let needle = format!("{operator}(");
        let mut offset = 0;
        while let Some(found) = cleaned[offset..].find(&needle) {
            let start = offset + found;
            if start == 0 || !cleaned.as_bytes()[start - 1].is_ascii_alphanumeric() {
                starts.push(start);
            }
            offset = start + needle.len();
            if offset >= cleaned.len() {
                break;
            }
        }
    }
    starts.sort_unstable();
    starts.dedup();
    for start in starts {
        let Some(end) = balanced_end(&cleaned[start..]) else {
            continue;
        };
        if let Ok(expression) = policy.validate(&cleaned[start..start + end]) {
            if !expressions.contains(&expression) {
                expressions.push(expression);
            }
        }
    }
    expressions
}

fn balanced_end(value: &str) -> Option<usize> {
    let mut depth = 0;
    for (index, character) in value.char_indices() {
        match character {
            '(' => depth += 1,
            ')' => {
                depth -= 1;
                if depth == 0 {
                    return Some(index + character.len_utf8());
                }
            }
            _ => {}
        }
    }
    None
}

pub fn default_policy() -> ExpressionPolicy {
    ExpressionPolicy {
        fields: [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "vwap",
            "cap",
            "returns",
            "beta",
            "adv20",
            "indneutral_beta",
        ]
        .into_iter()
        .map(String::from)
        .collect(),
        operators: [
            "rank",
            "ts_mean",
            "ts_std_dev",
            "ts_delta",
            "ts_corr",
            "ts_rank",
            "ts_zscore",
            "decay_linear",
            "winsorize",
            "zscore",
            "group_neutralize",
            "group_rank",
            "signed_power",
            "abs",
            "log",
            "sqrt",
            "sign",
            "max",
            "min",
            "sum",
            "product",
            "if_else",
            "multiply",
            "divide",
            "add",
            "subtract",
            "correlation",
            "std_dev",
            "mean",
            "power",
            "sigmoid",
            "tanh",
            "inverse",
            "clamp",
            "filter",
            "trade_when",
            "group_mean",
            "group_std_dev",
            "industry_neutralize",
            "sector_neutralize",
        ]
        .into_iter()
        .map(String::from)
        .collect(),
        forbidden: ["buy_turnover", "sell_turnover", "momentum"]
            .into_iter()
            .map(String::from)
            .collect(),
    }
}

#[cfg(test)]
mod tests {
    use super::{default_policy, extract_expressions, response_text, system_prompt};
    use crate::domain::Role;
    use serde_json::json;

    #[test]
    fn extracts_common_ts_corr_expression_from_markdown_response() {
        let response = "```fastexpr\nrank(ts_corr(low, volume, 10));\n```";
        let expressions = extract_expressions(response, &default_policy());

        assert!(expressions.contains(&"rank(ts_corr(low, volume, 10));".to_owned()));
    }

    #[test]
    fn falls_back_to_reasoning_content_when_message_content_is_empty() {
        let response = json!({
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning_content": "rank(close);"
                }
            }]
        });

        assert_eq!(response_text(&response).as_deref(), Some("rank(close);"));
    }

    #[test]
    fn miner_and_evolver_have_distinct_system_instructions() {
        let miner = system_prompt(Role::Miner);
        let evolver = system_prompt(Role::Evolver);
        assert!(miner.contains("diverse"));
        assert!(evolver.contains("structural mutations"));
        assert_ne!(miner, evolver);
    }
}

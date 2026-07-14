use crate::domain::{AlphaCandidate, AlphaMetrics, AlphaRecord, Role};
use crate::gateway::{SimulationResult, WorldQuantGateway};
use crate::llm::{default_policy, extract_expressions, ModelGateway};
use crate::store::AlphaStore;
use anyhow::{Context, Result};
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Duration;
use tracing::{info, warn};

pub async fn run_loop(
    role: Role,
    store: Arc<AlphaStore>,
    models: Arc<dyn ModelGateway>,
    worldquant: Arc<dyn WorldQuantGateway>,
    interval: Duration,
) -> Result<()> {
    loop {
        if let Err(error) = run_once(role, store.clone(), models.clone(), worldquant.clone()).await
        {
            warn!(?error, ?role, "pipeline iteration failed");
        }
        tokio::time::sleep(interval).await;
    }
}

pub async fn run_once(
    role: Role,
    store: Arc<AlphaStore>,
    models: Arc<dyn ModelGateway>,
    worldquant: Arc<dyn WorldQuantGateway>,
) -> Result<usize> {
    let policy = default_policy();
    let recent = store.list_recent(12).await.unwrap_or_default();
    let prompt = build_prompt(role, &recent);
    let answer = models
        .generate(role, &prompt)
        .await
        .context("generate alpha candidates")?;
    let expressions = extract_expressions(&answer, &policy);
    if expressions.is_empty() {
        warn!(?role, "model returned no valid expressions");
        return Ok(0);
    }
    let settings = default_settings();
    let mut accepted = 0;
    for expression in expressions.into_iter().take(8) {
        let candidate = AlphaCandidate {
            expression: expression.clone(),
            settings: settings.clone(),
            parent_id: recent.first().map(|a| a.id.clone()),
        };
        if !store.insert_candidate(candidate).await? {
            continue;
        }
        let local_id = crate::store::stable_id(&expression);
        match worldquant.simulate(&expression, settings.clone()).await {
            Ok(result) => {
                let failed = result.status == "ERROR" || result.alpha_id.is_none();
                let (metrics, raw, reason) = simulation_data(&result);
                store
                    .update_result(local_id, metrics, raw, failed, reason)
                    .await?;
                if !failed {
                    accepted += 1;
                }
            }
            Err(error) => warn!(?error, expression, "simulation failed"),
        }
    }
    info!(?role, accepted, "pipeline iteration complete");
    Ok(accepted)
}

pub async fn submit_pending(
    store: Arc<AlphaStore>,
    worldquant: Arc<dyn WorldQuantGateway>,
    limit: i64,
) -> Result<usize> {
    let candidates = store.get_unsubmitted(limit).await?;
    let mut submitted = 0;
    for candidate in candidates {
        let Some(alpha_id) = candidate
            .raw_data
            .get("wq_alpha_id")
            .and_then(Value::as_str)
        else {
            warn!(id=%candidate.id, "candidate has no WorldQuant alpha id; skipping submit");
            continue;
        };
        match worldquant.submit(alpha_id).await {
            Ok(_) => {
                store.mark_submitted(candidate.expression.clone()).await?;
                submitted += 1;
                info!(id=%candidate.id, alpha_id, "alpha submitted");
            }
            Err(error) => warn!(?error, alpha_id, "alpha submission failed"),
        }
    }
    Ok(submitted)
}

fn build_prompt(role: Role, recent: &[AlphaRecord]) -> String {
    let examples = recent
        .iter()
        .take(8)
        .map(|a| {
            format!(
                "{} fitness={:.3} sharpe={:.3}",
                a.expression, a.metrics.fitness, a.metrics.sharpe
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    match role {
        Role::Miner => format!("Discover 4 novel US equity FASTEXPR alpha expressions. Use only known price/volume fields and standard operators. Avoid buy_turnover, sell_turnover, momentum and invented identifiers. Every expression must be one line and end with a semicolon. Existing candidates:\n{examples}"),
        Role::Evolver => format!("Improve the strongest existing alpha while changing its structure enough to avoid duplication. Return 4 FASTEXPR expressions, one per line, ending with semicolons. Existing candidates:\n{examples}"),
    }
}

fn default_settings() -> Value {
    json!({
        "instrumentType": "EQUITY", "universe": "TOP3000", "region": "USA",
        "delay": 1, "decay": 4, "neutralization": "SUBINDUSTRY",
        "truncation": 0.1, "pasteurization": "ON", "unitHandling": "VERIFY",
        "nanHandling": "ON", "language": "FASTEXPR", "visualization": false
    })
}

fn simulation_data(result: &SimulationResult) -> (AlphaMetrics, Value, Option<String>) {
    let mut raw = result.data.clone();
    if let Value::Object(map) = &mut raw {
        if let Some(id) = &result.alpha_id {
            map.insert("wq_alpha_id".into(), json!(id));
        }
    }
    let is = result.data.get("is").unwrap_or(&result.data);
    let metrics = AlphaMetrics {
        fitness: number(is, "fitness"),
        sharpe: number(is, "sharpe"),
        returns: number(is, "returns"),
        turnover: number(is, "turnover"),
        pass_count: result
            .data
            .get("checks")
            .and_then(Value::as_array)
            .map(|v| {
                v.iter()
                    .filter(|x| x.get("result").and_then(Value::as_str) == Some("PASS"))
                    .count() as i64
            })
            .unwrap_or_default(),
        fail_count: result
            .data
            .get("checks")
            .and_then(Value::as_array)
            .map(|v| {
                v.iter()
                    .filter(|x| x.get("result").and_then(Value::as_str) == Some("FAIL"))
                    .count() as i64
            })
            .unwrap_or_default(),
        checks_summary: result
            .data
            .get("checks")
            .map(Value::to_string)
            .unwrap_or_default(),
    };
    let reason = if result.status == "ERROR" {
        Some(result.data.to_string())
    } else if result.alpha_id.is_none() {
        Some(result.status.clone())
    } else {
        None
    };
    (metrics, raw, reason)
}

fn number(value: &Value, key: &str) -> f64 {
    value
        .get(key)
        .and_then(Value::as_f64)
        .or_else(|| {
            value
                .get(key)
                .and_then(Value::as_str)
                .and_then(|v| v.parse().ok())
        })
        .unwrap_or_default()
}

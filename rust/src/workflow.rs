use crate::domain::{AlphaCandidate, AlphaMetrics, AlphaRecord, ExpressionPolicy, Role};
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
    if role == Role::Evolver {
        retry_pending(store.clone(), worldquant.clone()).await?;
    }
    let recent = store.list_recent(12).await.unwrap_or_default();
    let prompt = build_prompt(role, &recent, &policy);
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
        let inserted = store.insert_candidate(candidate).await?;
        if !inserted && !store.needs_simulation(expression.clone()).await? {
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

async fn retry_pending(
    store: Arc<AlphaStore>,
    worldquant: Arc<dyn WorldQuantGateway>,
) -> Result<()> {
    for candidate in store.rust_pending(8).await? {
        let settings = candidate
            .raw_data
            .get("settings")
            .cloned()
            .unwrap_or_else(default_settings);
        match worldquant.simulate(&candidate.expression, settings).await {
            Ok(result) => {
                let failed = result.status == "ERROR" || result.alpha_id.is_none();
                let (metrics, raw, reason) = simulation_data(&result);
                store
                    .update_result(candidate.id, metrics, raw, failed, reason)
                    .await?;
            }
            Err(error) => {
                warn!(?error, expression=%candidate.expression, "pending simulation retry failed")
            }
        }
    }
    Ok(())
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
        if has_failed_check(&candidate.raw_data) {
            info!(id=%candidate.id, alpha_id, "alpha has failed checks; skipping submit");
            continue;
        }
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

fn has_failed_check(raw_data: &Value) -> bool {
    raw_data
        .get("checks")
        .and_then(Value::as_array)
        .map(|checks| {
            checks
                .iter()
                .any(|check| check.get("result").and_then(Value::as_str) == Some("FAIL"))
        })
        .unwrap_or(true)
}

fn build_prompt(role: Role, recent: &[AlphaRecord], policy: &ExpressionPolicy) -> String {
    let fields = policy.fields.join(", ");
    let operators = policy.operators.join(", ");
    let forbidden = policy.forbidden.join(", ");
    let signature_guide = "rank(x); zscore(x); abs(x); log(x); sqrt(x); sign(x); \
ts_mean(x, d); ts_std_dev(x, d); ts_delta(x, d); ts_rank(x, d); ts_zscore(x, d); \
decay_linear(x, d); ts_corr(x, y, d); correlation(x, y, d); \
max(x, y); min(x, y); signed_power(x, p); power(x, p); \
multiply(x, y); divide(x, y); add(x, y); subtract(x, y)";
    let output_contract = format!(
        "STRICT OUTPUT CONTRACT:\n\
- Return exactly 4 lines.\n\
- Each line must contain one complete FASTEXPR expression and end with ';'.\n\
- Return no numbering, bullets, Markdown fences, JSON, variable assignments, comments, or explanations.\n\
- Parentheses must be balanced. Every d must be an integer lookback from 2 to 252; every p must be a numeric exponent.\n\
- Use only the signatures in PREFERRED OPERATOR SIGNATURES. Every argument is required: for example, ts_delta(x) is invalid and ts_delta(x, 5) is valid.\n\
- Before answering, silently check all function argument counts and remove any invalid line.\n\
- Identifiers must come only from the allowed lists below. Arithmetic symbols +, -, *, and / are allowed.\n\
ALLOWED DATA FIELDS: {fields}\n\
ALLOWED OPERATORS: {operators}\n\
PREFERRED OPERATOR SIGNATURES: {signature_guide}\n\
FORBIDDEN IDENTIFIERS: {forbidden}"
    );

    match role {
        Role::Miner => {
            let recent_examples = format_examples(recent.iter().take(8));
            format!(
                "ROLE: MINER\n\
TASK: Discover four diverse, economically plausible WorldQuant Brain alpha candidates for USA TOP3000 equities with delay 1.\n\
RESEARCH RULES:\n\
- Each candidate must express a meaningfully different hypothesis; do not create four parameter tweaks of one formula.\n\
- Prefer compact structures with 2-6 operators and at least two market inputs where sensible.\n\
- Use interpretable price, volume, return, volatility, correlation, or liquidity relationships. Avoid random operator stacking.\n\
- Do not copy any recent candidate verbatim or return one of its nested subexpressions.\n\
{output_contract}\n\
RECENT CANDIDATES TO AVOID COPYING:\n{recent_examples}"
            )
        }
        Role::Evolver => {
            let mut ranked = recent.iter().collect::<Vec<_>>();
            ranked.sort_by(|left, right| {
                right
                    .metrics
                    .fitness
                    .total_cmp(&left.metrics.fitness)
                    .then_with(|| right.metrics.sharpe.total_cmp(&left.metrics.sharpe))
            });
            let parent = ranked
                .first()
                .map(|alpha| format_alpha(alpha))
                .unwrap_or_else(|| {
                    "No parent is available; create conservative seed candidates.".to_owned()
                });
            let comparison = format_examples(ranked.into_iter().skip(1).take(5));
            format!(
                "ROLE: EVOLVER\n\
TASK: Produce four structurally distinct children of the target parent while preserving its plausible economic intuition.\n\
MUTATION RULES:\n\
- Every child must make at least one structural change, not merely alter a lookback number.\n\
- Across the four children, cover at least three mutation dimensions: data-field relationship, time-series transformation, normalization/ranking, and signal interaction.\n\
- Keep useful parts of the parent, but do not copy it verbatim or reproduce another child with different constants.\n\
- Prefer controlled changes over unnecessary complexity; every added operator must have a clear purpose.\n\
{output_contract}\n\
TARGET PARENT (highest fitness, then Sharpe):\n{parent}\n\
OTHER STRONG/RECENT CANDIDATES FOR CONTEXT ONLY:\n{comparison}"
            )
        }
    }
}

fn format_examples<'a>(records: impl Iterator<Item = &'a AlphaRecord>) -> String {
    let values = records.map(format_alpha).collect::<Vec<_>>();
    if values.is_empty() {
        "(none)".to_owned()
    } else {
        values.join("\n")
    }
}

fn format_alpha(alpha: &AlphaRecord) -> String {
    format!(
        "{} | fitness={:.3} sharpe={:.3} turnover={:.3}",
        alpha.expression, alpha.metrics.fitness, alpha.metrics.sharpe, alpha.metrics.turnover
    )
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

#[cfg(test)]
mod tests {
    use super::{build_prompt, default_policy};
    use crate::domain::{AlphaMetrics, AlphaRecord, Role};
    use serde_json::json;

    fn alpha(expression: &str, fitness: f64, sharpe: f64) -> AlphaRecord {
        AlphaRecord {
            id: expression.to_owned(),
            expression: expression.to_owned(),
            metrics: AlphaMetrics {
                fitness,
                sharpe,
                turnover: 0.12,
                ..AlphaMetrics::default()
            },
            is_submitted: false,
            is_failed_on_wq: false,
            failure_reason: None,
            raw_data: json!({}),
        }
    }

    #[test]
    fn miner_prompt_is_strict_and_matches_expression_policy() {
        let prompt = build_prompt(Role::Miner, &[], &default_policy());
        assert!(prompt.contains("Return exactly 4 lines"));
        assert!(prompt.contains("ALLOWED DATA FIELDS: open"));
        assert!(prompt.contains("ts_corr"));
        assert!(prompt.contains("FORBIDDEN IDENTIFIERS: buy_turnover"));
        assert!(prompt.contains("no numbering, bullets, Markdown fences, JSON"));
        assert!(prompt.contains("ts_delta(x, d)"));
        assert!(prompt.contains("ts_delta(x) is invalid"));
        assert!(prompt.contains("silently check all function argument counts"));
    }

    #[test]
    fn evolver_prompt_selects_best_parent_and_requires_structural_mutation() {
        let weak = alpha("rank(close);", 0.2, 0.4);
        let strong = alpha("rank(ts_delta(close, 5));", 1.4, 1.1);
        let prompt = build_prompt(Role::Evolver, &[weak, strong], &default_policy());
        let parent_section = prompt.split("TARGET PARENT").nth(1).unwrap();
        assert!(parent_section
            .starts_with(" (highest fitness, then Sharpe):\nrank(ts_delta(close, 5));"));
        assert!(prompt.contains("at least one structural change"));
        assert!(prompt.contains("not merely alter a lookback number"));
    }
}

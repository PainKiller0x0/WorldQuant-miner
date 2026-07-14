use crate::domain::{AlphaCandidate, AlphaMetrics, AlphaRecord, ExpressionPolicy, Role};
use crate::gateway::{SimulationResult, WorldQuantGateway};
use crate::llm::{default_policy, extract_expressions, ModelGateway};
use crate::store::AlphaStore;
use anyhow::{Context, Result};
use chrono::Utc;
use serde::Serialize;
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

#[derive(Debug, Default, Serialize)]
pub struct SubmissionBatchResult {
    pub configured_daily_limit: i64,
    pub effective_daily_limit: i64,
    pub submitted_last_24h: i64,
    pub processed: usize,
    pub matched: usize,
    pub stale: usize,
    pub resimulated: usize,
    pub checked: usize,
    pub ready: usize,
    pub submitted: usize,
    pub rejected: usize,
    pub skipped: usize,
}

#[derive(Debug, PartialEq, Eq)]
enum SubmissionState {
    Stale,
    Failed(Vec<String>),
    CheckPending,
    Ready,
    Waiting,
}

pub async fn process_submissions(
    store: Arc<AlphaStore>,
    worldquant: Arc<dyn WorldQuantGateway>,
    limit: i64,
    dry_run: bool,
    auto_submit: bool,
    ramp_submit: bool,
    daily_limit: i64,
) -> Result<SubmissionBatchResult> {
    let include_ready = dry_run || auto_submit;
    let mut candidates = store
        .get_submission_candidates(1, include_ready, Some(true))
        .await?;
    let remaining = limit.saturating_sub(candidates.len() as i64);
    if remaining > 0 {
        candidates.extend(
            store
                .get_submission_candidates(remaining, include_ready, Some(false))
                .await?,
        );
    }
    let already_submitted = store.submitted_today().await?;
    let effective_daily_limit = if ramp_submit {
        daily_limit.min(submit_ramp_limit(
            store.auto_submit_started_at().await?,
            Utc::now().timestamp(),
        ))
    } else {
        daily_limit
    };
    let mut submit_slots = effective_daily_limit
        .saturating_sub(already_submitted)
        .max(0);
    let mut result = SubmissionBatchResult {
        configured_daily_limit: daily_limit,
        effective_daily_limit,
        submitted_last_24h: already_submitted,
        ..SubmissionBatchResult::default()
    };

    for candidate in candidates {
        result.processed += 1;
        let linked_id = candidate
            .raw_data
            .get("wq_alpha_id")
            .and_then(Value::as_str)
            .map(str::to_owned);

        let (mut alpha_id, mut detail) = if let Some(alpha_id) = linked_id {
            let detail = worldquant.alpha(&alpha_id).await?;
            (alpha_id, detail)
        } else {
            let matches = worldquant.find_unsubmitted(&candidate.metrics).await?;
            let exact = select_exact_alpha(&candidate.expression, &matches);
            let Some(exact) = exact else {
                result.skipped += 1;
                warn!(id=%candidate.id, "no exact unsubmitted WorldQuant alpha match");
                continue;
            };
            let Some(alpha_id) = exact.get("id").and_then(Value::as_str).map(str::to_owned) else {
                result.skipped += 1;
                warn!(id=%candidate.id, "matched WorldQuant alpha has no id");
                continue;
            };
            result.matched += 1;
            let detail = worldquant.alpha(&alpha_id).await?;
            (alpha_id, detail)
        };

        let mut state = classify_submission(&detail);
        if state == SubmissionState::Stale {
            result.stale += 1;
            if dry_run {
                info!(id=%candidate.id, alpha_id, "dry-run: stale simulation requires rerun");
                continue;
            }
            let settings = detail
                .get("settings")
                .cloned()
                .unwrap_or_else(default_settings);
            let simulation = worldquant
                .simulate(&candidate.expression, settings)
                .await
                .context("resimulate stale alpha")?;
            let Some(new_alpha_id) = simulation.alpha_id.clone() else {
                result.skipped += 1;
                let (metrics, raw, reason) = simulation_data(&simulation);
                store
                    .update_result(candidate.id.clone(), metrics, raw, true, reason)
                    .await?;
                continue;
            };
            alpha_id = new_alpha_id;
            detail = simulation.data;
            state = classify_submission(&detail);
            persist_remote_state(&store, &candidate, &alpha_id, &detail, &state).await?;
            result.resimulated += 1;
            info!(id=%candidate.id, alpha_id, ?state, "stale alpha resimulated");
        }

        if matches!(state, SubmissionState::CheckPending) {
            if dry_run {
                info!(id=%candidate.id, alpha_id, "dry-run: submission check required");
                continue;
            }
            let checked_detail = match worldquant.check_submission(&alpha_id).await {
                Ok(detail) => detail,
                Err(error) => {
                    result.skipped += 1;
                    warn!(?error, id=%candidate.id, alpha_id, "submission check remains pending");
                    continue;
                }
            };
            detail = if checked_detail.get("is").is_some() {
                checked_detail
            } else {
                worldquant.alpha(&alpha_id).await?
            };
            state = classify_submission(&detail);
            persist_remote_state(&store, &candidate, &alpha_id, &detail, &state).await?;
            result.checked += 1;
            info!(id=%candidate.id, alpha_id, ?state, "submission check complete");
        }

        match state {
            SubmissionState::Ready => {
                result.ready += 1;
                if !dry_run {
                    persist_remote_state(&store, &candidate, &alpha_id, &detail, &state).await?;
                }
                if !auto_submit || dry_run || submit_slots == 0 {
                    info!(
                        id=%candidate.id,
                        alpha_id,
                        auto_submit,
                        submit_slots,
                        "alpha ready for submission"
                    );
                    continue;
                }
                worldquant.submit(&alpha_id).await?;
                store.mark_submitted(candidate.expression.clone()).await?;
                if ramp_submit {
                    store.mark_auto_submit_started().await?;
                }
                result.submitted += 1;
                submit_slots -= 1;
                info!(id=%candidate.id, alpha_id, "alpha submitted");
            }
            SubmissionState::Failed(ref reasons) => {
                result.rejected += 1;
                if !dry_run {
                    persist_remote_state(&store, &candidate, &alpha_id, &detail, &state).await?;
                }
                info!(id=%candidate.id, alpha_id, ?reasons, "alpha rejected by submission checks");
            }
            SubmissionState::Stale => {
                result.skipped += 1;
                warn!(id=%candidate.id, alpha_id, "fresh simulation is still marked old");
            }
            SubmissionState::CheckPending | SubmissionState::Waiting => {
                result.skipped += 1;
                if !dry_run {
                    persist_remote_state(&store, &candidate, &alpha_id, &detail, &state).await?;
                }
                info!(id=%candidate.id, alpha_id, ?state, "alpha is not ready for submission");
            }
        }
    }
    Ok(result)
}

fn submit_ramp_limit(started_at: Option<i64>, now: i64) -> i64 {
    let Some(started_at) = started_at else {
        return 1;
    };
    let completed_days = now.saturating_sub(started_at) / 86_400;
    if completed_days == 0 {
        1
    } else {
        (3 + completed_days).min(10)
    }
}

async fn persist_remote_state(
    store: &AlphaStore,
    candidate: &AlphaRecord,
    alpha_id: &str,
    detail: &Value,
    state: &SubmissionState,
) -> Result<()> {
    let simulation = SimulationResult {
        alpha_id: Some(alpha_id.to_owned()),
        status: "COMPLETE".into(),
        data: detail.clone(),
    };
    let (metrics, raw, base_reason) = simulation_data(&simulation);
    let failure_reason = match state {
        SubmissionState::Failed(reasons) => Some(reasons.join(",")),
        _ => base_reason,
    };
    store
        .update_result(
            candidate.id.clone(),
            metrics,
            raw,
            matches!(state, SubmissionState::Failed(_)),
            failure_reason,
        )
        .await
}

fn select_exact_alpha(expression: &str, matches: &[Value]) -> Option<Value> {
    let expected = comparable_expression(expression);
    matches
        .iter()
        .find(|alpha| {
            alpha
                .get("regular")
                .and_then(|regular| regular.get("code"))
                .and_then(Value::as_str)
                .map(comparable_expression)
                .as_deref()
                == Some(expected.as_str())
        })
        .cloned()
}

fn comparable_expression(expression: &str) -> String {
    expression
        .chars()
        .filter(|character| !character.is_whitespace())
        .collect::<String>()
        .trim_end_matches(';')
        .to_owned()
}

fn classify_submission(detail: &Value) -> SubmissionState {
    let Some(checks) = detail
        .get("is")
        .and_then(|value| value.get("checks"))
        .or_else(|| detail.get("checks"))
        .and_then(Value::as_array)
    else {
        return SubmissionState::Waiting;
    };
    let failed = checks
        .iter()
        .filter(|check| check.get("result").and_then(Value::as_str) == Some("FAIL"))
        .filter_map(|check| check.get("name").and_then(Value::as_str))
        .map(str::to_owned)
        .collect::<Vec<_>>();
    if failed.iter().any(|name| name == "OLD_SIMULATION") {
        return SubmissionState::Stale;
    }
    if !failed.is_empty() {
        return SubmissionState::Failed(failed);
    }
    let passed = checks
        .iter()
        .filter(|check| check.get("result").and_then(Value::as_str) == Some("PASS"))
        .count();
    let pending = checks
        .iter()
        .filter(|check| check.get("result").and_then(Value::as_str) == Some("PENDING"))
        .count();
    if passed >= 8 && pending == 0 {
        SubmissionState::Ready
    } else if passed >= 7 && pending == 1 {
        SubmissionState::CheckPending
    } else {
        SubmissionState::Waiting
    }
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
    let checks = is.get("checks").or_else(|| result.data.get("checks"));
    let metrics = AlphaMetrics {
        fitness: number(is, "fitness"),
        sharpe: number(is, "sharpe"),
        returns: number(is, "returns"),
        turnover: number(is, "turnover"),
        pass_count: checks
            .and_then(Value::as_array)
            .map(|v| {
                v.iter()
                    .filter(|x| x.get("result").and_then(Value::as_str) == Some("PASS"))
                    .count() as i64
            })
            .unwrap_or_default(),
        fail_count: checks
            .and_then(Value::as_array)
            .map(|v| {
                v.iter()
                    .filter(|x| x.get("result").and_then(Value::as_str) == Some("FAIL"))
                    .count() as i64
            })
            .unwrap_or_default(),
        checks_summary: checks.map(Value::to_string).unwrap_or_default(),
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
    use super::{
        build_prompt, classify_submission, default_policy, select_exact_alpha, simulation_data,
        submit_ramp_limit, SubmissionState,
    };
    use crate::domain::{AlphaMetrics, AlphaRecord, Role};
    use crate::gateway::SimulationResult;
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

    #[test]
    fn matches_remote_alpha_by_expression_not_metrics_alone() {
        let matches = vec![
            json!({"id":"wrong","regular":{"code":"rank(open);"}}),
            json!({"id":"right","regular":{"code":" rank(close) ; "}}),
        ];

        let selected = select_exact_alpha("rank(close);", &matches).unwrap();

        assert_eq!(
            selected.get("id").and_then(|value| value.as_str()),
            Some("right")
        );
    }

    #[test]
    fn old_simulation_failure_requires_resimulation_before_check() {
        let detail = json!({"is":{"checks":[
            {"name":"LOW_SHARPE","result":"PASS"},
            {"name":"OLD_SIMULATION","result":"FAIL"},
            {"name":"SELF_CORRELATION","result":"PENDING"}
        ]}});

        assert_eq!(classify_submission(&detail), SubmissionState::Stale);
    }

    #[test]
    fn seven_pass_one_pending_requires_check_and_eight_pass_is_ready() {
        let pending_checks = (0..7)
            .map(|index| json!({"name":format!("PASS_{index}"),"result":"PASS"}))
            .chain([json!({"name":"SELF_CORRELATION","result":"PENDING"})])
            .collect::<Vec<_>>();
        let ready_checks = (0..8)
            .map(|index| json!({"name":format!("PASS_{index}"),"result":"PASS"}))
            .collect::<Vec<_>>();

        assert_eq!(
            classify_submission(&json!({"is":{"checks":pending_checks}})),
            SubmissionState::CheckPending
        );
        assert_eq!(
            classify_submission(&json!({"is":{"checks":ready_checks}})),
            SubmissionState::Ready
        );
    }

    #[test]
    fn simulation_metrics_read_checks_from_is_payload() {
        let result = SimulationResult {
            alpha_id: Some("alpha-1".into()),
            status: "COMPLETE".into(),
            data: json!({
                "is": {
                    "fitness": 1.2,
                    "sharpe": 1.5,
                    "returns": 0.1,
                    "turnover": 0.2,
                    "checks": [
                        {"name":"A","result":"PASS"},
                        {"name":"B","result":"FAIL"}
                    ]
                }
            }),
        };

        let (metrics, raw, _) = simulation_data(&result);

        assert_eq!(metrics.pass_count, 1);
        assert_eq!(metrics.fail_count, 1);
        assert_eq!(
            raw.get("wq_alpha_id").and_then(|value| value.as_str()),
            Some("alpha-1")
        );
    }

    #[test]
    fn submit_limit_ramps_from_one_to_four_then_one_per_day() {
        let start = 1_000_000;
        assert_eq!(submit_ramp_limit(None, start), 1);
        assert_eq!(submit_ramp_limit(Some(start), start), 1);
        assert_eq!(submit_ramp_limit(Some(start), start + 86_399), 1);
        assert_eq!(submit_ramp_limit(Some(start), start + 86_400), 4);
        assert_eq!(submit_ramp_limit(Some(start), start + 2 * 86_400), 5);
        assert_eq!(submit_ramp_limit(Some(start), start + 7 * 86_400), 10);
        assert_eq!(submit_ramp_limit(Some(start), start + 30 * 86_400), 10);
    }
}

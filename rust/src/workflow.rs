use crate::domain::{
    AlphaCandidate, AlphaMetrics, AlphaPools, AlphaRecord, ExpressionPolicy, Role,
};
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
        let delay = match run_once(role, store.clone(), models.clone(), worldquant.clone()).await {
            Ok(_) => interval,
            Err(error) => {
                let delay = retry_delay(&error, interval);
                warn!(
                    ?error,
                    ?role,
                    retry_after_secs = delay.as_secs(),
                    "pipeline iteration failed"
                );
                delay
            }
        };
        tokio::time::sleep(delay).await;
    }
}

fn retry_delay(error: &anyhow::Error, interval: Duration) -> Duration {
    if error
        .chain()
        .any(|cause| cause.to_string().contains("429 Too Many Requests"))
    {
        interval.max(Duration::from_secs(15 * 60))
    } else {
        interval
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
    let pools = store
        .generation_pools(12)
        .await
        .context("load generation pools")?;
    let plan = build_generation_plan(role, &pools, &policy);
    let answer = models
        .generate(role, &plan.prompt)
        .await
        .context("generate alpha candidates")?;
    let extracted = extract_expressions(&answer, &policy);
    let extracted_count = extracted.len();
    let expressions = extracted
        .into_iter()
        .filter(|expression| is_research_worthy(expression, &policy))
        .collect::<Vec<_>>();
    if expressions.is_empty() {
        warn!(
            ?role,
            extracted_count, "model returned no research-worthy expressions"
        );
        return Ok(0);
    }
    let settings = default_settings();
    let mut accepted = 0;
    for expression in expressions.into_iter().take(8) {
        let candidate = AlphaCandidate {
            expression: expression.clone(),
            settings: settings.clone(),
            parent_id: plan.parent_id.clone(),
            role,
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
    info!(
        ?role,
        accepted,
        parent_id = plan.parent_id.as_deref().unwrap_or("none"),
        prompt_version = "quality-v2",
        "pipeline iteration complete"
    );
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
    pub submission_limit: &'static str,
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
    CheckError(Vec<String>),
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
) -> Result<SubmissionBatchResult> {
    let include_ready = dry_run || auto_submit;
    let already_submitted = store.submitted_today().await?;
    let mut candidates = if include_ready {
        store.get_ready_submission_candidates(limit).await?
    } else {
        Vec::new()
    };
    let active_candidate = if dry_run {
        store
            .get_submission_candidates(1, false, None)
            .await?
            .into_iter()
            .next()
    } else {
        store.claim_submission_candidate().await?
    };
    let active_candidate_id = active_candidate
        .as_ref()
        .map(|candidate| candidate.id.clone());
    if let Some(candidate) = active_candidate {
        if !candidates.iter().any(|ready| ready.id == candidate.id) {
            candidates.push(candidate);
        }
    }
    let mut result = SubmissionBatchResult {
        submission_limit: "unlimited",
        submitted_last_24h: already_submitted,
        ..SubmissionBatchResult::default()
    };

    for candidate in candidates {
        result.processed += 1;
        let is_active_check = active_candidate_id.as_deref() == Some(candidate.id.as_str());
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
                if !dry_run && is_active_check {
                    store
                        .record_submission_queue_error(
                            &candidate.id,
                            "match_missing",
                            "no exact unsubmitted WorldQuant alpha match",
                        )
                        .await?;
                    store.release_submission_candidate(&candidate.id).await?;
                }
                warn!(id=%candidate.id, "no exact unsubmitted WorldQuant alpha match");
                continue;
            };
            let Some(alpha_id) = exact.get("id").and_then(Value::as_str).map(str::to_owned) else {
                result.skipped += 1;
                if !dry_run && is_active_check {
                    store
                        .record_submission_queue_error(
                            &candidate.id,
                            "match_missing",
                            "matched WorldQuant alpha has no id",
                        )
                        .await?;
                    store.release_submission_candidate(&candidate.id).await?;
                }
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
            store
                .record_submission_stage(&candidate.id, "resimulating", Some(&alpha_id))
                .await?;
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
                if is_active_check {
                    store.release_submission_candidate(&candidate.id).await?;
                }
                continue;
            };
            alpha_id = new_alpha_id;
            detail = simulation.data;
            state = classify_submission(&detail);
            persist_remote_state(&store, &candidate, &alpha_id, &detail, &state, false).await?;
            result.resimulated += 1;
            info!(id=%candidate.id, alpha_id, ?state, "stale alpha resimulated");
        }

        if matches!(state, SubmissionState::CheckPending) && is_active_check {
            if dry_run {
                info!(id=%candidate.id, alpha_id, "dry-run: submission check required");
                continue;
            }
            persist_remote_state(&store, &candidate, &alpha_id, &detail, &state, true).await?;
            let checked_detail = match worldquant.check_submission(&alpha_id).await {
                Ok(detail) => detail,
                Err(error) => {
                    result.skipped += 1;
                    let retry_count = store
                        .record_submission_queue_error(
                            &candidate.id,
                            "check_error",
                            &error.to_string(),
                        )
                        .await?;
                    if retry_count >= 3 {
                        store.release_submission_candidate(&candidate.id).await?;
                    }
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
            persist_remote_state(&store, &candidate, &alpha_id, &detail, &state, true).await?;
            result.checked += 1;
            info!(id=%candidate.id, alpha_id, ?state, "submission check complete");
        }

        let self_correlation_override = is_self_correlation_submit_override(&state);
        if auto_submit
            && !dry_run
            && (matches!(state, SubmissionState::Ready) || self_correlation_override)
        {
            if matches!(state, SubmissionState::Ready) {
                result.ready += 1;
                persist_remote_state(&store, &candidate, &alpha_id, &detail, &state, false).await?;
            } else {
                warn!(
                    id=%candidate.id,
                    alpha_id,
                    "SELF_CORRELATION check errored; attempting submit and trusting the submit response"
                );
            }
            store
                .record_submission_stage(&candidate.id, "submitting", Some(&alpha_id))
                .await?;
            let submit_response = match worldquant.submit(&alpha_id).await {
                Ok(response) => response,
                Err(error) => {
                    result.skipped += 1;
                    let retry_count = store
                        .record_submission_queue_error(
                            &candidate.id,
                            "submit_error",
                            &error.to_string(),
                        )
                        .await?;
                    if is_active_check && retry_count >= 3 {
                        store.release_submission_candidate(&candidate.id).await?;
                    }
                    warn!(?error, id=%candidate.id, alpha_id, "submission transport failed; will retry");
                    continue;
                }
            };
            if let Some(reason) = submission_rejection_reason(&submit_response) {
                persist_submit_outcome(
                    &store,
                    &candidate,
                    &alpha_id,
                    &detail,
                    &submit_response,
                    Some(reason.clone()),
                    self_correlation_override,
                )
                .await?;
                if is_active_check {
                    store.release_submission_candidate(&candidate.id).await?;
                }
                result.rejected += 1;
                warn!(id=%candidate.id, alpha_id, reason, "alpha submission rejected by WorldQuant");
                continue;
            }
            persist_submit_outcome(
                &store,
                &candidate,
                &alpha_id,
                &detail,
                &submit_response,
                None,
                self_correlation_override,
            )
            .await?;
            store.mark_submitted(candidate.expression.clone()).await?;
            if is_active_check {
                store.release_submission_candidate(&candidate.id).await?;
            }
            result.submitted += 1;
            info!(id=%candidate.id, alpha_id, self_correlation_override, "alpha submitted");
            continue;
        }

        match state {
            SubmissionState::Ready => {
                result.ready += 1;
                if !dry_run {
                    persist_remote_state(&store, &candidate, &alpha_id, &detail, &state, false)
                        .await?;
                }
                if !auto_submit || dry_run {
                    if !dry_run && is_active_check {
                        store.release_submission_candidate(&candidate.id).await?;
                    }
                    info!(
                        id=%candidate.id,
                        alpha_id,
                        auto_submit,
                        "alpha ready for submission"
                    );
                    continue;
                }
                store
                    .record_submission_stage(&candidate.id, "submitting", Some(&alpha_id))
                    .await?;
                worldquant.submit(&alpha_id).await?;
                store.mark_submitted(candidate.expression.clone()).await?;
                if is_active_check {
                    store.release_submission_candidate(&candidate.id).await?;
                }
                result.submitted += 1;
                info!(id=%candidate.id, alpha_id, "alpha submitted");
            }
            SubmissionState::Failed(ref reasons) => {
                result.rejected += 1;
                if !dry_run {
                    persist_remote_state(&store, &candidate, &alpha_id, &detail, &state, false)
                        .await?;
                    if is_active_check {
                        store.release_submission_candidate(&candidate.id).await?;
                    }
                }
                info!(id=%candidate.id, alpha_id, ?reasons, "alpha rejected by submission checks");
            }
            SubmissionState::CheckError(ref reasons) => {
                result.skipped += 1;
                if !dry_run {
                    persist_remote_state(&store, &candidate, &alpha_id, &detail, &state, true)
                        .await?;
                    if is_active_check && queue_retry_count(&candidate) + 1 >= 3 {
                        store.release_submission_candidate(&candidate.id).await?;
                    }
                }
                warn!(id=%candidate.id, alpha_id, ?reasons, "submission check returned retryable error");
            }
            SubmissionState::Stale => {
                result.skipped += 1;
                if !dry_run && is_active_check {
                    store.release_submission_candidate(&candidate.id).await?;
                }
                warn!(id=%candidate.id, alpha_id, "fresh simulation is still marked old");
            }
            SubmissionState::CheckPending | SubmissionState::Waiting => {
                result.skipped += 1;
                if !dry_run {
                    persist_remote_state(
                        &store,
                        &candidate,
                        &alpha_id,
                        &detail,
                        &state,
                        matches!(state, SubmissionState::Waiting),
                    )
                    .await?;
                    if is_active_check && matches!(state, SubmissionState::Waiting) {
                        store.release_submission_candidate(&candidate.id).await?;
                    }
                }
                info!(id=%candidate.id, alpha_id, ?state, "alpha is not ready for submission");
            }
        }
    }
    Ok(result)
}

fn is_self_correlation_submit_override(state: &SubmissionState) -> bool {
    matches!(
        state,
        SubmissionState::CheckError(reasons)
            if !reasons.is_empty() && reasons.iter().all(|reason| reason == "SELF_CORRELATION")
    )
}

fn submission_rejection_reason(response: &Value) -> Option<String> {
    let status = response
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_ascii_uppercase();
    let message = response
        .get("message")
        .and_then(Value::as_str)
        .or_else(|| {
            response
                .get("response")
                .and_then(|value| value.get("message"))
                .and_then(Value::as_str)
        })
        .or_else(|| response.get("error").and_then(Value::as_str));
    let message_is_rejection = message
        .map(|message| {
            let message = message.to_ascii_lowercase();
            message.contains("cannot submit")
                || message.contains("test failed")
                || message.contains("submission failed")
        })
        .unwrap_or(false);
    let status_is_rejection = matches!(status.as_str(), "ERROR" | "FAIL" | "FAILED" | "REJECTED");
    if status_is_rejection || message_is_rejection {
        Some(
            message
                .map(str::to_owned)
                .unwrap_or_else(|| response.to_string()),
        )
    } else {
        None
    }
}

async fn persist_submit_outcome(
    store: &AlphaStore,
    candidate: &AlphaRecord,
    alpha_id: &str,
    detail: &Value,
    submit_response: &Value,
    rejection_reason: Option<String>,
    self_correlation_override: bool,
) -> Result<()> {
    let simulation = SimulationResult {
        alpha_id: Some(alpha_id.to_owned()),
        status: "COMPLETE".into(),
        data: detail.clone(),
    };
    let (metrics, mut raw, base_reason) = simulation_data(&simulation);
    if let Value::Object(map) = &mut raw {
        for key in [
            "submission_enqueued_at",
            "submission_check_retry_count",
            "submission_last_attempt_at",
            "submission_last_error",
        ] {
            if let Some(value) = candidate.raw_data.get(key) {
                map.insert(key.into(), value.clone());
            }
        }
        map.insert(
            "submission_phase".into(),
            json!(if rejection_reason.is_some() {
                "submit_failed"
            } else {
                "submit_accepted"
            }),
        );
        map.insert("submission_submit_response".into(), submit_response.clone());
        map.insert(
            "submission_completed_at".into(),
            json!(Utc::now().timestamp()),
        );
        if self_correlation_override {
            map.insert(
                "submission_check_override".into(),
                json!("SELF_CORRELATION=ERROR"),
            );
        }
        if let Some(reason) = &rejection_reason {
            map.insert("submission_last_error".into(), json!(reason));
        }
    }
    let rejected = rejection_reason.is_some();
    store
        .update_result(
            candidate.id.clone(),
            metrics,
            raw,
            rejected,
            rejection_reason.or(base_reason),
        )
        .await
}

async fn persist_remote_state(
    store: &AlphaStore,
    candidate: &AlphaRecord,
    alpha_id: &str,
    detail: &Value,
    state: &SubmissionState,
    mark_attempt: bool,
) -> Result<()> {
    let simulation = SimulationResult {
        alpha_id: Some(alpha_id.to_owned()),
        status: "COMPLETE".into(),
        data: detail.clone(),
    };
    let (metrics, mut raw, base_reason) = simulation_data(&simulation);
    if let Value::Object(map) = &mut raw {
        for key in [
            "submission_enqueued_at",
            "submission_check_retry_count",
            "submission_last_error",
        ] {
            if let Some(value) = candidate.raw_data.get(key) {
                map.insert(key.into(), value.clone());
            }
        }
        map.insert("submission_phase".into(), json!(submission_phase(state)));
        if mark_attempt {
            map.insert(
                "submission_last_attempt_at".into(),
                json!(Utc::now().timestamp()),
            );
        } else if let Some(last_attempt) = candidate.raw_data.get("submission_last_attempt_at") {
            map.insert("submission_last_attempt_at".into(), last_attempt.clone());
        }
        if let SubmissionState::CheckError(reasons) = state {
            map.insert(
                "submission_check_retry_count".into(),
                json!(queue_retry_count(candidate) + 1),
            );
            map.insert("submission_last_error".into(), json!(reasons.join(",")));
        }
        if matches!(state, SubmissionState::Ready | SubmissionState::Failed(_)) {
            map.insert(
                "submission_completed_at".into(),
                json!(Utc::now().timestamp()),
            );
        }
    }
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

fn submission_phase(state: &SubmissionState) -> &'static str {
    match state {
        SubmissionState::Stale => "resimulation_required",
        SubmissionState::Failed(_) => "failed",
        SubmissionState::CheckError(_) => "check_error",
        SubmissionState::CheckPending => "checking",
        SubmissionState::Ready => "ready",
        SubmissionState::Waiting => "waiting",
    }
}

fn queue_retry_count(candidate: &AlphaRecord) -> i64 {
    candidate
        .raw_data
        .get("submission_check_retry_count")
        .and_then(Value::as_i64)
        .unwrap_or_default()
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
    let errors = checks
        .iter()
        .filter(|check| check.get("result").and_then(Value::as_str) == Some("ERROR"))
        .filter_map(|check| check.get("name").and_then(Value::as_str))
        .map(str::to_owned)
        .collect::<Vec<_>>();
    if !errors.is_empty() {
        return SubmissionState::CheckError(errors);
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

#[derive(Debug)]
struct GenerationPlan {
    prompt: String,
    parent_id: Option<String>,
}

fn build_generation_plan(
    role: Role,
    pools: &AlphaPools,
    policy: &ExpressionPolicy,
) -> GenerationPlan {
    let slot = Utc::now().timestamp().unsigned_abs() as usize / 60;
    build_generation_plan_for_slot(role, pools, policy, slot)
}

fn build_generation_plan_for_slot(
    role: Role,
    pools: &AlphaPools,
    policy: &ExpressionPolicy,
    slot: usize,
) -> GenerationPlan {
    let fields = policy.fields.join(", ");
    let operators = policy.operators.join(", ");
    let forbidden = policy.forbidden.join(", ");
    let signature_guide = "rank(x); zscore(x); abs(x); log(x); sqrt(x); sign(x); \
ts_mean(x, d); ts_std_dev(x, d); ts_delta(x, d); ts_sum(x, d); ts_rank(x, d); ts_zscore(x, d); \
decay_linear(x, d); ts_decay_linear(x, d); ts_corr(x, y, d); correlation(x, y, d); \
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
            let successful_examples = format_examples(pools.successful.iter().take(4));
            let promising_examples = format_examples(
                pools
                    .candidates
                    .iter()
                    .filter(|alpha| {
                        alpha.metrics.fitness >= 1.0
                            || (alpha.metrics.pass_count >= 7 && alpha.metrics.fail_count == 0)
                    })
                    .take(4),
            );
            let failures = format_failures(pools.failures.iter().take(3));
            GenerationPlan {
                parent_id: None,
                prompt: format!(
                "ROLE: MINER\n\
OBJECTIVE: Discover four diverse WorldQuant Brain alpha candidates for USA TOP3000 equities with delay 1. Optimize for out-of-sample Fitness >= 1.0, Sharpe >= 1.25, stable turnover, and submission-check robustness.\n\
RESEARCH RULES:\n\
- Each candidate must express a meaningfully different hypothesis; do not create four parameter tweaks of one formula.\n\
- Use 4-12 purposeful operators and at least two distinct market inputs in every candidate. Reject single-field level, trend, mean, volatility, rank, or z-score signals.\n\
- Build an interpretable relationship: price-volume confirmation/divergence, volatility-adjusted momentum or reversal, liquidity-conditioned returns, or correlation/dispersion regime.\n\
- Normalize scale before combining signals. Sanity-check direction: a candidate should not accidentally reverse its economic hypothesis.\n\
- Treat successful examples as design priors: reuse robust motifs and interactions, but never copy a complete expression or merely change lookbacks.\n\
- Avoid raw price/volume levels, redundant nested ranks, random operator stacking, and division by an unscaled or near-zero denominator.\n\
{output_contract}\n\
SUCCESSFUL REFERENCE ALPHAS (learn motifs, do not copy):\n{successful_examples}\n\
PROMISING UNSUBMITTED REFERENCES:\n{promising_examples}\n\
FAILED ALPHAS AND AUTHORITATIVE REASONS TO AVOID:\n\
{failures}\n\
FINAL REMINDER: Begin directly with the first FASTEXPR expression. Output exactly four expression lines and nothing else."
                ),
            }
        }
        Role::Evolver => {
            let mut successful = pools.successful.iter().collect::<Vec<_>>();
            successful.sort_by(|left, right| {
                right
                    .metrics
                    .fitness
                    .total_cmp(&left.metrics.fitness)
                    .then_with(|| right.metrics.sharpe.total_cmp(&left.metrics.sharpe))
            });
            let mut candidates = pools
                .candidates
                .iter()
                .filter(|alpha| eligible_candidate_parent(alpha))
                .collect::<Vec<_>>();
            candidates.sort_by(|left, right| {
                right
                    .metrics
                    .fitness
                    .total_cmp(&left.metrics.fitness)
                    .then_with(|| right.metrics.sharpe.total_cmp(&left.metrics.sharpe))
            });
            let parent_pool = if successful.is_empty() {
                &candidates
            } else {
                &successful
            };
            let rotation_width = parent_pool.len().min(4);
            let selected = if rotation_width == 0 {
                None
            } else {
                parent_pool.get(slot % rotation_width).copied()
            };
            let parent = selected
                .map(|alpha| format_alpha(alpha))
                .unwrap_or_else(|| {
                    "No parent is available; create conservative seed candidates.".to_owned()
                });
            let selected_id = selected.map(|alpha| alpha.id.as_str());
            let comparison = format_examples(
                successful
                    .iter()
                    .copied()
                    .chain(candidates.iter().copied())
                    .filter(|alpha| Some(alpha.id.as_str()) != selected_id)
                    .take(5),
            );
            let failures = format_failures(pools.failures.iter().take(3));
            GenerationPlan {
                parent_id: selected.map(|alpha| alpha.id.clone()),
                prompt: format!(
                "ROLE: EVOLVER\n\
OBJECTIVE: Produce four controlled children that have a credible chance to improve the target parent's out-of-sample Fitness without sacrificing Sharpe, turnover, or submission checks.\n\
MUTATION RULES:\n\
- Preserve the parent's core economic relationship and at least one of its main data interactions; do not simplify it into a single-field signal.\n\
- Every child must make exactly one primary structural mutation plus at most one supporting normalization change; lookback-only changes are forbidden.\n\
- Produce one child for each dimension: field relationship, time-series transform, normalization/neutralization, and signal interaction.\n\
- Use 4-14 purposeful operators and at least two distinct market inputs. Remove redundant nesting and guard unstable division.\n\
- Check sign and scale against the parent before answering; accidental inversion and raw-level exposure are invalid.\n\
- Keep useful parts of the parent, but do not copy it verbatim or reproduce another child with different constants.\n\
{output_contract}\n\
TARGET PARENT (rotated among top submitted alphas):\n{parent}\n\
OTHER SUCCESSFUL OR QUALIFIED CANDIDATES FOR CONTEXT ONLY:\n{comparison}\n\
FAILED ALPHAS AND AUTHORITATIVE REASONS TO AVOID:\n\
{failures}\n\
FINAL REMINDER: Begin directly with the first FASTEXPR expression. Output exactly four expression lines and nothing else."
                ),
            }
        }
    }
}

fn is_research_worthy(expression: &str, policy: &ExpressionPolicy) -> bool {
    let token_pattern =
        regex::Regex::new(r"[A-Za-z_][A-Za-z0-9_]*").expect("static research token regex");
    let fields = token_pattern
        .find_iter(expression)
        .map(|token| token.as_str())
        .filter(|token| policy.fields.iter().any(|field| field == token))
        .collect::<std::collections::HashSet<_>>();
    let operators = token_pattern
        .find_iter(expression)
        .map(|token| token.as_str())
        .filter(|token| policy.operators.iter().any(|operator| operator == token))
        .count();

    fields.len() >= 2 && (4..=18).contains(&operators)
}

fn eligible_candidate_parent(alpha: &AlphaRecord) -> bool {
    alpha.metrics.pass_count >= 7
        && alpha.metrics.fail_count == 0
        && alpha
            .raw_data
            .get("wq_alpha_id")
            .and_then(Value::as_str)
            .is_some()
}

fn format_examples<'a>(records: impl Iterator<Item = &'a AlphaRecord>) -> String {
    let values = records.map(format_alpha).collect::<Vec<_>>();
    if values.is_empty() {
        "(none)".to_owned()
    } else {
        values.join("\n")
    }
}

fn format_failures<'a>(records: impl Iterator<Item = &'a AlphaRecord>) -> String {
    let values = records
        .map(|alpha| {
            format!(
                "{} | failure_reason={}",
                alpha.expression,
                alpha.failure_reason.as_deref().unwrap_or("UNKNOWN")
            )
        })
        .collect::<Vec<_>>();
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
        build_generation_plan, build_generation_plan_for_slot, classify_submission, default_policy,
        is_research_worthy, process_submissions, retry_delay, select_exact_alpha, simulation_data,
        SubmissionState,
    };
    use crate::domain::{AlphaMetrics, AlphaPools, AlphaRecord, Role};
    use crate::gateway::{SimulationResult, WorldQuantGateway};
    use crate::store::AlphaStore;
    use anyhow::{anyhow, Result};
    use async_trait::async_trait;
    use serde_json::json;
    use serde_json::Value;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    fn alpha(expression: &str, fitness: f64, sharpe: f64) -> AlphaRecord {
        AlphaRecord {
            id: expression.to_owned(),
            expression: expression.to_owned(),
            metrics: AlphaMetrics {
                fitness,
                sharpe,
                turnover: 0.12,
                pass_count: 7,
                ..AlphaMetrics::default()
            },
            is_submitted: false,
            is_failed_on_wq: false,
            failure_reason: None,
            raw_data: json!({"wq_alpha_id": format!("remote-{expression}")}),
        }
    }

    #[test]
    fn miner_prompt_is_strict_and_matches_expression_policy() {
        let plan = build_generation_plan(Role::Miner, &AlphaPools::default(), &default_policy());
        let prompt = plan.prompt;
        assert_eq!(plan.parent_id, None);
        assert!(prompt.contains("Return exactly 4 lines"));
        assert!(prompt.contains("ALLOWED DATA FIELDS: open"));
        assert!(prompt.contains("ts_corr"));
        assert!(prompt.contains("FORBIDDEN IDENTIFIERS: buy_turnover"));
        assert!(prompt.contains("no numbering, bullets, Markdown fences, JSON"));
        assert!(prompt.contains("ts_delta(x, d)"));
        assert!(prompt.contains("ts_delta(x) is invalid"));
        assert!(prompt.contains("silently check all function argument counts"));
        assert!(prompt.contains("SUCCESSFUL REFERENCE ALPHAS"));
        assert!(prompt.contains("Fitness"));
        assert!(prompt.contains("Reject single-field"));
    }

    #[test]
    fn research_gate_rejects_trivial_single_field_signals() {
        let policy = default_policy();

        assert!(!is_research_worthy("ts_mean(returns, 5);", &policy));
        assert!(!is_research_worthy(
            "rank(subtract(ts_mean(close, 10), close));",
            &policy
        ));
        assert!(is_research_worthy(
            "rank(ts_decay_linear(ts_corr(ts_delta(close, 5), ts_delta(volume, 5), 20), 8));",
            &policy
        ));
    }

    #[test]
    fn rate_limit_uses_a_long_backoff_without_slowing_other_errors() {
        let normal = Duration::from_secs(60);

        assert_eq!(
            retry_delay(&anyhow!("LLM returned 429 Too Many Requests"), normal),
            Duration::from_secs(15 * 60)
        );
        assert_eq!(
            retry_delay(&anyhow!("temporary network error"), normal),
            normal
        );
    }

    #[test]
    fn evolver_rotates_across_top_submitted_parents() {
        let mut first = alpha("rank(close);", 2.0, 1.8);
        first.id = "first".into();
        first.is_submitted = true;
        let mut second = alpha("rank(open);", 1.8, 1.7);
        second.id = "second".into();
        second.is_submitted = true;
        let pools = AlphaPools {
            successful: vec![first, second],
            ..AlphaPools::default()
        };

        let first_plan =
            build_generation_plan_for_slot(Role::Evolver, &pools, &default_policy(), 0);
        let second_plan =
            build_generation_plan_for_slot(Role::Evolver, &pools, &default_policy(), 1);

        assert_eq!(first_plan.parent_id.as_deref(), Some("first"));
        assert_eq!(second_plan.parent_id.as_deref(), Some("second"));
    }

    #[test]
    fn evolver_prompt_selects_best_parent_and_requires_structural_mutation() {
        let weak = alpha("rank(close);", 0.2, 0.4);
        let strong = alpha("rank(ts_delta(close, 5));", 1.4, 1.1);
        let pools = AlphaPools {
            candidates: vec![weak, strong],
            ..AlphaPools::default()
        };
        let prompt =
            build_generation_plan_for_slot(Role::Evolver, &pools, &default_policy(), 0).prompt;
        let parent_section = prompt.split("TARGET PARENT").nth(1).unwrap();
        assert!(parent_section
            .starts_with(" (rotated among top submitted alphas):\nrank(ts_delta(close, 5));"));
        assert!(prompt.contains("primary structural mutation"));
        assert!(prompt.contains("lookback-only changes are forbidden"));
    }

    #[test]
    fn evolver_prefers_successful_pool_and_persists_the_same_parent() {
        let mut successful = alpha("rank(close);", 1.0, 1.1);
        successful.id = "submitted-parent".into();
        successful.is_submitted = true;
        let mut fitter_candidate = alpha("rank(open);", 9.0, 3.0);
        fitter_candidate.id = "candidate-parent".into();
        let mut failure = alpha("rank(high);", 20.0, 4.0);
        failure.id = "failed-parent".into();
        failure.is_failed_on_wq = true;
        failure.failure_reason = Some("Cannot submit Alpha: 1 test failed".into());
        let pools = AlphaPools {
            successful: vec![successful],
            candidates: vec![fitter_candidate],
            failures: vec![failure],
        };

        let plan = build_generation_plan(Role::Evolver, &pools, &default_policy());

        assert_eq!(plan.parent_id.as_deref(), Some("submitted-parent"));
        let parent_section = plan.prompt.split("TARGET PARENT").nth(1).unwrap();
        assert!(parent_section.starts_with(" (rotated among top submitted alphas):\nrank(close);"));
        assert!(plan.prompt.contains("Cannot submit Alpha: 1 test failed"));
        assert!(!parent_section.starts_with(" (rotated among top submitted alphas):\nrank(open);"));
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
    fn self_correlation_error_is_retryable_instead_of_failure() {
        let checks = (0..7)
            .map(|index| json!({"name":format!("PASS_{index}"),"result":"PASS"}))
            .chain([json!({"name":"SELF_CORRELATION","result":"ERROR"})])
            .collect::<Vec<_>>();

        assert_eq!(
            classify_submission(&json!({"is":{"checks":checks}})),
            SubmissionState::CheckError(vec!["SELF_CORRELATION".into()])
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

    #[derive(Default)]
    struct PendingCheckGateway;

    #[async_trait]
    impl WorldQuantGateway for PendingCheckGateway {
        async fn operators(&self) -> Result<Vec<String>> {
            Ok(Vec::new())
        }

        async fn simulate(&self, _expression: &str, _settings: Value) -> Result<SimulationResult> {
            unreachable!()
        }

        async fn find_unsubmitted(&self, _metrics: &AlphaMetrics) -> Result<Vec<Value>> {
            Ok(vec![
                json!({"id":"remote-1","regular":{"code":"rank(close);"}}),
            ])
        }

        async fn alpha(&self, _alpha_id: &str) -> Result<Value> {
            let checks = (0..7)
                .map(|index| json!({"name":format!("PASS_{index}"),"result":"PASS"}))
                .chain([json!({"name":"SELF_CORRELATION","result":"PENDING"})])
                .collect::<Vec<_>>();
            Ok(
                json!({"id":"remote-1","is":{"fitness":1.2,"sharpe":1.5,"returns":0.1,"turnover":0.2,"checks":checks}}),
            )
        }

        async fn check_submission(&self, _alpha_id: &str) -> Result<Value> {
            Err(anyhow!("still pending"))
        }

        async fn submit(&self, _alpha_id: &str) -> Result<Value> {
            unreachable!()
        }
    }

    #[tokio::test]
    async fn pending_check_keeps_remote_link_for_next_batch() {
        let path = std::env::temp_dir().join(format!(
            "wq-submit-test-{}.db",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE alphas (id TEXT PRIMARY KEY, expression TEXT NOT NULL, fitness REAL, sharpe REAL, returns REAL, turnover REAL, pass_count INTEGER, fail_count INTEGER, checks_summary TEXT, is_submitted INTEGER, is_failed_on_wq INTEGER, failure_reason TEXT, raw_data TEXT, created_at TEXT, submitted_timestamp TEXT);").unwrap();
        conn.execute("INSERT INTO alphas (id, expression, fitness, sharpe, returns, turnover, pass_count, fail_count, is_submitted, is_failed_on_wq, raw_data, created_at) VALUES ('local-1','rank(close);',1.2,1.5,0.1,0.2,7,0,0,0,'{}',CURRENT_TIMESTAMP)", []).unwrap();
        drop(conn);
        let store = Arc::new(AlphaStore::new(&path));

        process_submissions(
            store.clone(),
            Arc::new(PendingCheckGateway),
            1,
            false,
            false,
        )
        .await
        .unwrap();

        let linked = store
            .get_submission_candidates(10, false, Some(true))
            .await
            .unwrap();
        assert_eq!(linked.len(), 1);
        assert_eq!(
            linked[0]
                .raw_data
                .get("wq_alpha_id")
                .and_then(Value::as_str),
            Some("remote-1")
        );
        let _ = std::fs::remove_file(path);
    }

    struct ReadySubmitGateway {
        submitted: Arc<AtomicUsize>,
    }

    #[async_trait]
    impl WorldQuantGateway for ReadySubmitGateway {
        async fn operators(&self) -> Result<Vec<String>> {
            Ok(Vec::new())
        }

        async fn simulate(&self, _expression: &str, _settings: Value) -> Result<SimulationResult> {
            unreachable!()
        }

        async fn find_unsubmitted(&self, _metrics: &AlphaMetrics) -> Result<Vec<Value>> {
            unreachable!()
        }

        async fn alpha(&self, alpha_id: &str) -> Result<Value> {
            let checks = (0..8)
                .map(|index| json!({"name":format!("PASS_{index}"),"result":"PASS"}))
                .collect::<Vec<_>>();
            Ok(
                json!({"id":alpha_id,"is":{"fitness":1.3,"sharpe":1.7,"returns":0.1,"turnover":0.2,"checks":checks}}),
            )
        }

        async fn check_submission(&self, _alpha_id: &str) -> Result<Value> {
            unreachable!()
        }

        async fn submit(&self, alpha_id: &str) -> Result<Value> {
            self.submitted.fetch_add(1, Ordering::SeqCst);
            Ok(json!({"status":"submitted","alpha_id":alpha_id}))
        }
    }

    #[tokio::test]
    async fn automatic_submission_has_no_daily_quota() {
        let path = std::env::temp_dir().join(format!(
            "wq-unlimited-submit-{}.db",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE alphas (id TEXT PRIMARY KEY, expression TEXT NOT NULL, fitness REAL, sharpe REAL, returns REAL, turnover REAL, pass_count INTEGER, fail_count INTEGER, checks_summary TEXT, is_submitted INTEGER, is_failed_on_wq INTEGER, failure_reason TEXT, raw_data TEXT, created_at TEXT, submitted_timestamp TEXT);").unwrap();
        conn.execute("INSERT INTO alphas (id, expression, pass_count, fail_count, is_submitted, is_failed_on_wq, raw_data, created_at, submitted_timestamp) VALUES ('old','rank(low);',8,0,1,0,'{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)", []).unwrap();
        conn.execute("INSERT INTO alphas (id, expression, fitness, pass_count, fail_count, is_submitted, is_failed_on_wq, raw_data, created_at) VALUES ('one','rank(close);',1.3,8,0,0,0,'{\"wq_alpha_id\":\"remote-one\"}',CURRENT_TIMESTAMP)", []).unwrap();
        conn.execute("INSERT INTO alphas (id, expression, fitness, pass_count, fail_count, is_submitted, is_failed_on_wq, raw_data, created_at) VALUES ('two','rank(open);',1.2,8,0,0,0,'{\"wq_alpha_id\":\"remote-two\"}',CURRENT_TIMESTAMP)", []).unwrap();
        drop(conn);
        let store = Arc::new(AlphaStore::new(&path));
        let submitted = Arc::new(AtomicUsize::new(0));

        let result = process_submissions(
            store,
            Arc::new(ReadySubmitGateway {
                submitted: submitted.clone(),
            }),
            10,
            false,
            true,
        )
        .await
        .unwrap();

        assert_eq!(result.submission_limit, "unlimited");
        assert_eq!(result.submitted_last_24h, 1);
        assert_eq!(result.submitted, 2);
        assert_eq!(submitted.load(Ordering::SeqCst), 2);
        let _ = std::fs::remove_file(path);
    }

    struct SelfCorrelationSubmitGateway {
        submitted: Arc<AtomicUsize>,
        submit_response: Value,
    }

    #[async_trait]
    impl WorldQuantGateway for SelfCorrelationSubmitGateway {
        async fn operators(&self) -> Result<Vec<String>> {
            Ok(Vec::new())
        }

        async fn simulate(&self, _expression: &str, _settings: Value) -> Result<SimulationResult> {
            unreachable!()
        }

        async fn find_unsubmitted(&self, _metrics: &AlphaMetrics) -> Result<Vec<Value>> {
            unreachable!()
        }

        async fn alpha(&self, alpha_id: &str) -> Result<Value> {
            let checks = (0..7)
                .map(|index| json!({"name":format!("PASS_{index}"),"result":"PASS"}))
                .chain([json!({"name":"SELF_CORRELATION","result":"ERROR"})])
                .collect::<Vec<_>>();
            Ok(json!({
                "id": alpha_id,
                "is": {
                    "fitness": 1.3,
                    "sharpe": 1.7,
                    "returns": 0.1,
                    "turnover": 0.2,
                    "checks": checks
                }
            }))
        }

        async fn check_submission(&self, _alpha_id: &str) -> Result<Value> {
            unreachable!()
        }

        async fn submit(&self, _alpha_id: &str) -> Result<Value> {
            self.submitted.fetch_add(1, Ordering::SeqCst);
            Ok(self.submit_response.clone())
        }
    }

    #[tokio::test]
    async fn self_correlation_error_attempts_submit_and_records_remote_rejection() {
        let path = std::env::temp_dir().join(format!(
            "wq-self-correlation-submit-{}.db",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE alphas (id TEXT PRIMARY KEY, expression TEXT NOT NULL, fitness REAL, sharpe REAL, returns REAL, turnover REAL, pass_count INTEGER, fail_count INTEGER, checks_summary TEXT, is_submitted INTEGER, is_failed_on_wq INTEGER, failure_reason TEXT, raw_data TEXT, created_at TEXT, submitted_timestamp TEXT);").unwrap();
        conn.execute("INSERT INTO alphas (id, expression, fitness, pass_count, fail_count, is_submitted, is_failed_on_wq, raw_data, created_at) VALUES ('self-error','rank(close);',1.3,7,0,0,0,'{\"wq_alpha_id\":\"remote-self-error\"}',CURRENT_TIMESTAMP)", []).unwrap();
        drop(conn);
        let store = Arc::new(AlphaStore::new(&path));
        let submitted = Arc::new(AtomicUsize::new(0));

        let result = process_submissions(
            store,
            Arc::new(SelfCorrelationSubmitGateway {
                submitted: submitted.clone(),
                submit_response: json!({
                    "status": "ERROR",
                    "message": "Cannot submit Alpha: 1 test failed"
                }),
            }),
            1,
            false,
            true,
        )
        .await
        .unwrap();

        assert_eq!(submitted.load(Ordering::SeqCst), 1);
        assert_eq!(result.submitted, 0);
        assert_eq!(result.rejected, 1);
        let conn = rusqlite::Connection::open(&path).unwrap();
        let (is_submitted, is_failed, reason, raw): (i64, i64, String, String) = conn
            .query_row(
                "SELECT is_submitted, is_failed_on_wq, failure_reason, raw_data FROM alphas WHERE id='self-error'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        let raw: Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(is_submitted, 0);
        assert_eq!(is_failed, 1);
        assert_eq!(reason, "Cannot submit Alpha: 1 test failed");
        assert_eq!(
            raw.pointer("/submission_submit_response/message")
                .and_then(Value::as_str),
            Some("Cannot submit Alpha: 1 test failed")
        );
        assert_eq!(
            raw.get("submission_phase").and_then(Value::as_str),
            Some("submit_failed")
        );
        let _ = std::fs::remove_file(path);
    }

    #[tokio::test]
    async fn self_correlation_error_marks_submitted_when_remote_accepts() {
        let path = std::env::temp_dir().join(format!(
            "wq-self-correlation-accepted-{}.db",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE alphas (id TEXT PRIMARY KEY, expression TEXT NOT NULL, fitness REAL, sharpe REAL, returns REAL, turnover REAL, pass_count INTEGER, fail_count INTEGER, checks_summary TEXT, is_submitted INTEGER, is_failed_on_wq INTEGER, failure_reason TEXT, raw_data TEXT, created_at TEXT, submitted_timestamp TEXT);").unwrap();
        conn.execute("INSERT INTO alphas (id, expression, fitness, pass_count, fail_count, is_submitted, is_failed_on_wq, raw_data, created_at) VALUES ('self-error','rank(close);',1.3,7,0,0,0,'{\"wq_alpha_id\":\"remote-self-error\"}',CURRENT_TIMESTAMP)", []).unwrap();
        drop(conn);
        let store = Arc::new(AlphaStore::new(&path));
        let submitted = Arc::new(AtomicUsize::new(0));

        let result = process_submissions(
            store,
            Arc::new(SelfCorrelationSubmitGateway {
                submitted: submitted.clone(),
                submit_response: json!({"status":"submitted","alpha_id":"remote-self-error"}),
            }),
            1,
            false,
            true,
        )
        .await
        .unwrap();

        assert_eq!(submitted.load(Ordering::SeqCst), 1);
        assert_eq!(result.submitted, 1);
        assert_eq!(result.rejected, 0);
        let conn = rusqlite::Connection::open(&path).unwrap();
        let (is_submitted, is_failed, raw): (i64, i64, String) = conn
            .query_row(
                "SELECT is_submitted, is_failed_on_wq, raw_data FROM alphas WHERE id='self-error'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        let raw: Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(is_submitted, 1);
        assert_eq!(is_failed, 0);
        assert_eq!(
            raw.get("submission_check_override").and_then(Value::as_str),
            Some("SELF_CORRELATION=ERROR")
        );
        assert_eq!(
            raw.pointer("/submission_submit_response/status")
                .and_then(Value::as_str),
            Some("submitted")
        );
        let _ = std::fs::remove_file(path);
    }
}

use crate::store::AlphaStore;
use anyhow::{anyhow, Context, Result};
use axum::{
    body::Body,
    extract::{Path, Query, State},
    http::{header, StatusCode},
    response::{Html, IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use chrono::Utc;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{io::SeekFrom, net::SocketAddr, path::PathBuf, sync::Arc};
use tokio::{
    fs,
    io::{AsyncReadExt, AsyncSeekExt},
    process::Command,
};
use tower_http::services::ServeDir;

#[derive(Clone)]
struct DashboardState {
    store: Arc<AlphaStore>,
    root: PathBuf,
    system_config_path: PathBuf,
    api_config_path: PathBuf,
}

#[derive(Debug, Deserialize)]
struct DaysQuery {
    days: Option<i64>,
}

pub async fn serve(
    store: Arc<AlphaStore>,
    root: PathBuf,
    system_config_path: PathBuf,
    api_config_path: PathBuf,
    listen: &str,
) -> Result<()> {
    let state = DashboardState {
        store,
        root: root.clone(),
        system_config_path,
        api_config_path,
    };
    let app = Router::new()
        .route("/", get(page_dashboard))
        .route("/settings", get(page_settings))
        .route("/chart", get(page_chart))
        .route("/pending", get(page_pending))
        .route("/healthz", get(healthz))
        .route("/status", get(status))
        .route("/api/status", get(status))
        .route("/api/version_info", get(version_info))
        .route("/api/get_settings", get(get_settings))
        .route("/api/save_settings", post(save_settings))
        .route("/api/mark_submitted", post(mark_submitted))
        .route("/api/unmark_submitted", post(unmark_submitted))
        .route("/api/mark_failed_on_wq", post(mark_failed))
        .route("/api/unmark_failed_on_wq", post(unmark_failed))
        .route("/api/get_pending_alphas", get(get_pending_alphas))
        .route("/api/v1/stats/submission_daily", get(submission_daily))
        .route("/api/v1/stats/timeseries", get(timeseries))
        .route("/download_logs/{name}", get(download_log))
        .nest_service("/static", ServeDir::new(root.join("static")))
        .with_state(state);
    let address: SocketAddr = listen.parse().context("parse WQ_LISTEN")?;
    let listener = tokio::net::TcpListener::bind(address).await?;
    tracing::info!(%address, "Rust dashboard listening");

    let compat_listen =
        std::env::var("WQ_COMPAT_LISTEN").unwrap_or_else(|_| "0.0.0.0:8080".to_owned());
    if compat_listen != listen {
        let compat_address: SocketAddr = compat_listen.parse().context("parse WQ_COMPAT_LISTEN")?;
        let compat_listener = tokio::net::TcpListener::bind(compat_address).await?;
        tracing::info!(%compat_address, target=%address, "Rust dashboard compatibility listener active");
        let compat_app = app.clone();
        tokio::spawn(async move {
            if let Err(error) = axum::serve(compat_listener, compat_app).await {
                tracing::error!(%error, "Rust dashboard compatibility listener stopped");
            }
        });
    }

    axum::serve(listener, app).await?;
    Ok(())
}

async fn page_dashboard(State(state): State<DashboardState>) -> Response {
    render_page(&state, "dashboard_v4.html").await
}

async fn page_settings(State(state): State<DashboardState>) -> Response {
    render_page(&state, "settings.html").await
}

async fn page_chart(State(state): State<DashboardState>) -> Response {
    render_page(&state, "chart.html").await
}

async fn page_pending(State(state): State<DashboardState>) -> Response {
    render_page(&state, "pending.html").await
}

async fn render_page(state: &DashboardState, name: &str) -> Response {
    let path = state.root.join("templates").join(name);
    match fs::read_to_string(path).await {
        Ok(page) => Html(page).into_response(),
        Err(error) => (StatusCode::NOT_FOUND, format!("page unavailable: {error}")).into_response(),
    }
}

async fn healthz(State(state): State<DashboardState>) -> impl IntoResponse {
    match state.store.health().await {
        Ok(count) => (
            StatusCode::OK,
            Json(json!({"ok":true,"alphas":count,"runtime":"rust"})),
        ),
        Err(error) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"ok":false,"error":error.to_string()})),
        ),
    }
}

async fn status(State(state): State<DashboardState>) -> impl IntoResponse {
    let hopeful = state
        .store
        .dashboard_summary(300)
        .await
        .unwrap_or_else(|error| json!({"error": error.to_string()}));
    let config = read_json(&state.system_config_path)
        .await
        .unwrap_or_else(|_| json!({}));
    let model_config = read_json(&state.api_config_path)
        .await
        .unwrap_or_else(|_| json!({}));
    let budgets = config
        .get("llm_budgets")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let active_nodes = config
        .get("active_nodes")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let limiter = config
        .get("wq_api_limiter")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let cooldown = limiter
        .get("wq_429_cooldown_seconds")
        .and_then(Value::as_f64)
        .unwrap_or(60.0);
    let last_failure = limiter
        .get("last_failure_timestamp")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    let remaining = (cooldown - (Utc::now().timestamp_millis() as f64 / 1000.0 - last_failure))
        .max(0.0)
        .round() as i64;
    let journal_logs = read_worker_journal().await;
    if let Err(error) = &journal_logs {
        tracing::warn!(?error, "worker journal unavailable; using legacy log files");
    }
    let miner_logs = match &journal_logs {
        Ok(logs) => logs.clone(),
        Err(_) => read_log_tail(&state.root.join("logs/miner.log"))
            .await
            .unwrap_or_else(|error| format!("无法读取 Miner 日志: {error}")),
    };
    let evolver_logs = match &journal_logs {
        Ok(logs) => logs.clone(),
        Err(_) => read_log_tail(&state.root.join("logs/evolver.log"))
            .await
            .unwrap_or_else(|error| format!("无法读取 Evolver 日志: {error}")),
    };
    let service = |role: &str, logs: String| {
        json!({
            "status":"RUNNING",
            "last_seen":Utc::now().to_rfc3339(),
            "model_name":configured_model_name(&model_config, role),
            "logs":logs
        })
    };
    let body = json!({
        "runtime":"rust",
        "miner":service("miner", miner_logs),
        "evolver":service("evolver", evolver_logs),
        "hopeful_alphas":hopeful,
        "watchdog_status":{
            "llm_budget_used": budget_value(&budgets, &active_nodes, "miner", "used_today"),
            "llm_budget_limit": budget_value(&budgets, &active_nodes, "miner", "daily_limit"),
            "miner_budget_used": budget_value(&budgets, &active_nodes, "miner", "used_today"),
            "miner_budget_limit": budget_value(&budgets, &active_nodes, "miner", "daily_limit"),
            "miner_active_node": active_nodes.get("miner").cloned().unwrap_or_else(|| json!("miner")),
            "evolver_budget_used": budget_value(&budgets, &active_nodes, "evolver", "used_today"),
            "evolver_budget_limit": budget_value(&budgets, &active_nodes, "evolver", "daily_limit"),
            "evolver_active_node": active_nodes.get("evolver").cloned().unwrap_or_else(|| json!("evolver")),
            "wq_current_tpm_limit": limiter.get("current_tpm_limit").cloned().unwrap_or_else(|| json!(60)),
            "wq_cooldown_status": if remaining > 0 { format!("IN_COOLDOWN ({remaining}s)") } else { "OK".to_owned() },
            "wq_cooldown_remaining_sec": remaining
        }
    });
    (StatusCode::OK, Json(body))
}

fn budget_value(budgets: &Value, active: &Value, role: &str, field: &str) -> Value {
    let key = active.get(role).and_then(Value::as_str).unwrap_or(role);
    budgets
        .get(key)
        .and_then(|v| v.get(field))
        .cloned()
        .unwrap_or_else(|| json!(0))
}

async fn version_info() -> impl IntoResponse {
    Json(json!({"dashboard_version":"rust-v1","generator_version":"rust"}))
}

async fn get_settings(State(state): State<DashboardState>) -> Response {
    match read_json(&state.system_config_path).await {
        Ok(value) => Json(value).into_response(),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error":error.to_string()})),
        )
            .into_response(),
    }
}

async fn save_settings(
    State(state): State<DashboardState>,
    Json(incoming): Json<Value>,
) -> Response {
    let path = state.system_config_path.clone();
    let result = tokio::task::spawn_blocking(move || -> Result<()> {
        let mut current = std::fs::read_to_string(&path)
            .ok()
            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
            .unwrap_or_else(|| json!({}));
        merge_json(&mut current, incoming);
        let temp = path.with_extension("json.rust.tmp");
        std::fs::write(&temp, serde_json::to_vec_pretty(&current)?)?;
        std::fs::rename(temp, path)?;
        Ok(())
    })
    .await;
    match result {
        Ok(Ok(())) => Json(json!({"status":"success","message":"配置已保存"})).into_response(),
        Ok(Err(error)) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"status":"error","message":error.to_string()})),
        )
            .into_response(),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"status":"error","message":error.to_string()})),
        )
            .into_response(),
    }
}

fn merge_json(target: &mut Value, incoming: Value) {
    match (target, incoming) {
        (Value::Object(target), Value::Object(incoming)) => {
            for (key, value) in incoming {
                if let Some(existing) = target.get_mut(&key) {
                    merge_json(existing, value);
                } else {
                    target.insert(key, value);
                }
            }
        }
        (target, incoming) => *target = incoming,
    }
}

async fn mark_submitted(State(state): State<DashboardState>, Json(body): Json<Value>) -> Response {
    flag_result(state.store.mark_submitted(expression(&body)).await).await
}

async fn unmark_submitted(
    State(state): State<DashboardState>,
    Json(body): Json<Value>,
) -> Response {
    flag_result(state.store.unmark_submitted(expression(&body)).await).await
}

async fn mark_failed(State(state): State<DashboardState>, Json(body): Json<Value>) -> Response {
    let reason = body
        .get("reason")
        .and_then(Value::as_str)
        .unwrap_or("UNKNOWN")
        .to_owned();
    flag_result(state.store.mark_failed(expression(&body), reason).await).await
}

async fn unmark_failed(State(state): State<DashboardState>, Json(body): Json<Value>) -> Response {
    flag_result(state.store.unmark_failed(expression(&body)).await).await
}

fn expression(body: &Value) -> String {
    body.get("expression")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

async fn flag_result(result: Result<bool>) -> Response {
    match result {
        Ok(true) => Json(json!({"status":"success"})).into_response(),
        Ok(false) => (
            StatusCode::NOT_FOUND,
            Json(json!({"status":"error","message":"Alpha not found"})),
        )
            .into_response(),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"status":"error","message":error.to_string()})),
        )
            .into_response(),
    }
}

async fn get_pending_alphas(State(state): State<DashboardState>) -> impl IntoResponse {
    match state.store.pending_dashboard(500).await {
        Ok(value) => (StatusCode::OK, Json(Value::Array(value))),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error":error.to_string()})),
        ),
    }
}

async fn submission_daily(
    State(state): State<DashboardState>,
    Query(query): Query<DaysQuery>,
) -> impl IntoResponse {
    match state
        .store
        .submission_daily(query.days.unwrap_or_default())
        .await
    {
        Ok(value) => (StatusCode::OK, Json(value)),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error":error.to_string()})),
        ),
    }
}

async fn timeseries(
    State(state): State<DashboardState>,
    Query(query): Query<DaysQuery>,
) -> impl IntoResponse {
    match state.store.timeseries(query.days.unwrap_or(1)).await {
        Ok(value) => (StatusCode::OK, Json(value)),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error":error.to_string()})),
        ),
    }
}

async fn download_log(State(state): State<DashboardState>, Path(name): Path<String>) -> Response {
    if name.is_empty() || name.contains('/') || name.contains('\\') || name.contains("..") {
        return (StatusCode::BAD_REQUEST, "invalid log name").into_response();
    }
    let actual_name = log_filename(&name);
    let path = state.root.join("logs").join(actual_name);
    match fs::read(path).await {
        Ok(data) => Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .header(
                header::CONTENT_DISPOSITION,
                format!("attachment; filename=\"{name}\""),
            )
            .body(Body::from(data))
            .unwrap_or_else(|_| {
                (StatusCode::INTERNAL_SERVER_ERROR, "response error").into_response()
            }),
        Err(_) => (StatusCode::NOT_FOUND, "log not found").into_response(),
    }
}

async fn read_json(path: &PathBuf) -> Result<Value> {
    let raw = fs::read_to_string(path).await?;
    Ok(serde_json::from_str(&raw)?)
}

async fn read_worker_journal() -> Result<String> {
    let output = Command::new("journalctl")
        .args([
            "-u",
            "worldquant-rust-worker.service",
            "--no-pager",
            "-n",
            "120",
            "-o",
            "cat",
        ])
        .output()
        .await
        .context("read worker journal")?;
    if !output.status.success() {
        return Err(anyhow!("journalctl exited with {}", output.status));
    }
    let text = strip_ansi(&String::from_utf8_lossy(&output.stdout));
    if text.trim().is_empty() {
        return Err(anyhow!("worker journal is empty"));
    }
    Ok(newest_first(text.trim_end()))
}

fn configured_model_name(config: &Value, role: &str) -> String {
    for suffix in [
        "config",
        "config_backup",
        "config_backup_2",
        "config_backup_3",
    ] {
        let key = format!("{role}_{suffix}");
        if let Some(model_name) = config
            .get(&key)
            .and_then(|value| value.get("model_name"))
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
        {
            return model_name.to_owned();
        }
    }
    "未配置".to_owned()
}

fn log_filename(name: &str) -> &str {
    match name {
        "miner.log" => "miner.log",
        "miner_issues.log" => "miner_issues.log",
        "evolver.log" => "evolver.log",
        "evolver_issues.log" => "evolver_issues.log",
        other => other,
    }
}

async fn read_log_tail(path: &PathBuf) -> Result<String> {
    const MAX_TAIL_BYTES: u64 = 64 * 1024;
    let mut file = fs::File::open(path).await?;
    let size = file.metadata().await?.len();
    let start = size.saturating_sub(MAX_TAIL_BYTES);
    file.seek(SeekFrom::Start(start)).await?;
    let mut bytes = Vec::with_capacity((size - start) as usize);
    file.read_to_end(&mut bytes).await?;
    let mut text = String::from_utf8_lossy(&bytes).into_owned();
    if start > 0 {
        if let Some(newline) = text.find('\n') {
            text = text[(newline + 1)..].to_owned();
        }
    }
    Ok(newest_first(text.trim_end()))
}

fn newest_first(text: &str) -> String {
    text.lines().rev().collect::<Vec<_>>().join("\n")
}

fn strip_ansi(text: &str) -> String {
    let mut clean = String::with_capacity(text.len());
    let mut in_escape = false;
    for character in text.chars() {
        if in_escape {
            if character.is_ascii_alphabetic() {
                in_escape = false;
            }
        } else if character == '\u{1b}' {
            in_escape = true;
        } else {
            clean.push(character);
        }
    }
    clean
}

#[cfg(test)]
mod tests {
    use super::{configured_model_name, log_filename, newest_first, strip_ansi};
    use serde_json::json;

    #[test]
    fn dashboard_uses_primary_model_name_for_each_role() {
        let config = json!({
            "miner_config": {"model_name": "agnes-2.0-flash"},
            "miner_config_backup": {"model_name": "glm-4.7-flash"},
            "evolver_config": {"model_name": "glm-4.7-flash"}
        });

        assert_eq!(configured_model_name(&config, "miner"), "agnes-2.0-flash");
        assert_eq!(configured_model_name(&config, "evolver"), "glm-4.7-flash");
        assert_eq!(configured_model_name(&config, "missing"), "未配置");
    }

    #[test]
    fn dashboard_log_aliases_point_to_current_runtime_logs() {
        assert_eq!(log_filename("miner.log"), "miner.log");
        assert_eq!(log_filename("miner_issues.log"), "miner_issues.log");
        assert_eq!(log_filename("evolver.log"), "evolver.log");
        assert_eq!(log_filename("evolver_issues.log"), "evolver_issues.log");
    }

    #[test]
    fn dashboard_log_tail_places_newest_entry_first() {
        assert_eq!(
            newest_first("old entry\nnew entry\n"),
            "new entry\nold entry"
        );
    }

    #[test]
    fn dashboard_removes_ansi_formatting_from_journal_lines() {
        assert_eq!(
            strip_ansi("\u{1b}[2m2026-01-01\u{1b}[0m INFO"),
            "2026-01-01 INFO"
        );
    }
}

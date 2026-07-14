use crate::store::AlphaStore;
use anyhow::{Context, Result};
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
use std::{net::SocketAddr, path::PathBuf, sync::Arc};
use tokio::fs;
use tower_http::services::ServeDir;

#[derive(Clone)]
struct DashboardState {
    store: Arc<AlphaStore>,
    root: PathBuf,
    system_config_path: PathBuf,
}

#[derive(Debug, Deserialize)]
struct DaysQuery {
    days: Option<i64>,
}

pub async fn serve(
    store: Arc<AlphaStore>,
    root: PathBuf,
    system_config_path: PathBuf,
    listen: &str,
) -> Result<()> {
    let state = DashboardState {
        store,
        root: root.clone(),
        system_config_path,
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
    let service = |role: &str| json!({"status":"RUNNING","last_seen":Utc::now().to_rfc3339(),"logs":format!("Rust {role} pipeline is managed by worldquant-rust-worker.service")});
    let body = json!({
        "runtime":"rust",
        "miner":service("miner"),
        "evolver":service("evolver"),
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
    let actual_name = match name.as_str() {
        "miner.log" => "alpha_generator.log",
        "miner_issues.log" => "alpha_generator_issues.log",
        other => other,
    };
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

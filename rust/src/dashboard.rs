use crate::store::AlphaStore;
use anyhow::{Context, Result};
use axum::{extract::State, http::StatusCode, response::IntoResponse, routing::get, Json, Router};
use serde_json::json;
use std::{net::SocketAddr, sync::Arc};

#[derive(Clone)]
struct DashboardState {
    store: Arc<AlphaStore>,
}

pub async fn serve(store: Arc<AlphaStore>, listen: &str) -> Result<()> {
    let state = DashboardState { store };
    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/status", get(status))
        .route("/api/status", get(status))
        .with_state(state);
    let address: SocketAddr = listen.parse().context("parse WQ_LISTEN")?;
    let listener = tokio::net::TcpListener::bind(address).await?;
    tracing::info!(%address, "Rust dashboard listening");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn healthz(State(state): State<DashboardState>) -> impl IntoResponse {
    match state.store.health().await {
        Ok(count) => (
            StatusCode::OK,
            Json(json!({"ok": true, "alphas": count, "runtime": "rust"})),
        ),
        Err(error) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"ok": false, "error": error.to_string()})),
        ),
    }
}

async fn status(State(state): State<DashboardState>) -> impl IntoResponse {
    let count = state.store.health().await.unwrap_or_default();
    let recent = state.store.list_recent(20).await.unwrap_or_default();
    (
        StatusCode::OK,
        Json(json!({"runtime":"rust", "alpha_count": count, "recent": recent})),
    )
}

use crate::domain::{AlphaCandidate, AlphaMetrics, AlphaRecord};
use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use serde_json::Value;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug)]
pub struct AlphaStore {
    path: PathBuf,
}

impl AlphaStore {
    pub fn new(path: impl AsRef<Path>) -> Self {
        Self {
            path: path.as_ref().to_path_buf(),
        }
    }

    pub async fn health(&self) -> Result<i64> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            Ok::<_, anyhow::Error>(
                conn.query_row("SELECT COUNT(*) FROM alphas", [], |row| row.get(0))
                    .unwrap_or(0),
            )
        })
        .await
        .context("database health task")?
    }

    pub async fn list_recent(&self, limit: i64) -> Result<Vec<AlphaRecord>> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let mut stmt = conn.prepare("SELECT id, expression, fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq, failure_reason, raw_data FROM alphas ORDER BY created_at DESC LIMIT ?1")?;
            let rows = stmt.query_map([limit], alpha_from_row)?;
            Ok::<_, anyhow::Error>(rows.collect::<rusqlite::Result<Vec<_>>>()?)
        }).await.context("database list task")?
    }

    pub async fn mark_submitted(&self, expression: String) -> Result<bool> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let changed = conn.execute("UPDATE alphas SET is_submitted=1, submitted_timestamp=CURRENT_TIMESTAMP WHERE expression=?1", [expression])?;
            Ok::<_, anyhow::Error>(changed > 0)
        }).await.context("database mark submitted task")?
    }

    pub async fn insert_candidate(&self, candidate: AlphaCandidate) -> Result<bool> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let id = stable_id(&candidate.expression);
            let raw_data = serde_json::json!({
                "settings": candidate.settings,
                "parent_id": candidate.parent_id,
                "source": "rust"
            }).to_string();
            let changed = conn.execute(
                "INSERT OR IGNORE INTO alphas (id, expression, raw_data, is_submitted, is_failed_on_wq, created_at) VALUES (?1, ?2, ?3, 0, 0, CURRENT_TIMESTAMP)",
                params![id, candidate.expression, raw_data],
            )?;
            Ok::<_, anyhow::Error>(changed > 0)
        }).await.context("database insert task")?
    }

    pub async fn get_unsubmitted(&self, limit: i64) -> Result<Vec<AlphaRecord>> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let mut stmt = conn.prepare("SELECT id, expression, fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq, failure_reason, raw_data FROM alphas WHERE COALESCE(is_submitted, 0)=0 AND COALESCE(is_failed_on_wq, 0)=0 ORDER BY created_at DESC LIMIT ?1")?;
            let rows = stmt.query_map([limit], alpha_from_row)?;
            Ok::<_, anyhow::Error>(rows.collect::<rusqlite::Result<Vec<_>>>()?)
        }).await.context("database pending task")?
    }

    pub async fn update_result(
        &self,
        id: String,
        metrics: AlphaMetrics,
        raw_data: Value,
        failed: bool,
        failure_reason: Option<String>,
    ) -> Result<()> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            conn.execute(
                "UPDATE alphas SET fitness=?1, sharpe=?2, returns=?3, turnover=?4, pass_count=?5, fail_count=?6, checks_summary=?7, raw_data=?8, is_failed_on_wq=?9, failure_reason=?10 WHERE id=?11",
                params![metrics.fitness, metrics.sharpe, metrics.returns, metrics.turnover, metrics.pass_count, metrics.fail_count, metrics.checks_summary, raw_data.to_string(), failed as i64, failure_reason, id],
            )?;
            Ok::<_, anyhow::Error>(())
        }).await.context("database result task")?
    }
}

fn alpha_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<AlphaRecord> {
    let raw: Option<String> = row.get(12)?;
    Ok(AlphaRecord {
        id: row.get(0)?,
        expression: row.get(1)?,
        metrics: AlphaMetrics {
            fitness: row.get::<_, Option<f64>>(2)?.unwrap_or_default(),
            sharpe: row.get::<_, Option<f64>>(3)?.unwrap_or_default(),
            returns: row.get::<_, Option<f64>>(4)?.unwrap_or_default(),
            turnover: row.get::<_, Option<f64>>(5)?.unwrap_or_default(),
            pass_count: row.get::<_, Option<i64>>(6)?.unwrap_or_default(),
            fail_count: row.get::<_, Option<i64>>(7)?.unwrap_or_default(),
            checks_summary: row.get::<_, Option<String>>(8)?.unwrap_or_default(),
        },
        is_submitted: row.get::<_, i64>(9).unwrap_or(0) != 0,
        is_failed_on_wq: row.get::<_, i64>(10).unwrap_or(0) != 0,
        failure_reason: row.get(11)?,
        raw_data: raw
            .and_then(|v| serde_json::from_str(&v).ok())
            .unwrap_or(Value::Null),
    })
}

pub fn stable_id(expression: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut digest = Sha256::new();
    digest.update(expression.as_bytes());
    format!("rust-{}", hex::encode(digest.finalize()))
}

fn open(path: &Path) -> Result<Connection> {
    let conn = Connection::open(path).with_context(|| format!("open {}", path.display()))?;
    conn.busy_timeout(std::time::Duration::from_secs(10))?;
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")?;
    Ok(conn)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[tokio::test]
    async fn reads_existing_schema() {
        let path = std::env::temp_dir().join(format!(
            "wq-rs-test-{}.db",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE alphas (id TEXT PRIMARY KEY, expression TEXT NOT NULL, fitness REAL, sharpe REAL, returns REAL, turnover REAL, pass_count INTEGER, fail_count INTEGER, checks_summary TEXT, is_submitted INTEGER, is_failed_on_wq INTEGER, failure_reason TEXT, raw_data TEXT, created_at TEXT, submitted_timestamp TEXT);") .unwrap();
        conn.execute("INSERT INTO alphas (id, expression, fitness, sharpe, returns, turnover, pass_count, fail_count, is_submitted, is_failed_on_wq, raw_data) VALUES ('a','rank(close);',1.0,1.2,0.1,0.2,7,0,0,0,'{}')", []).unwrap();
        drop(conn);
        assert_eq!(AlphaStore::new(&path).health().await.unwrap(), 1);
        let _ = std::fs::remove_file(path);
    }
}

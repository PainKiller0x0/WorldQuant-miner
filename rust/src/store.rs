use crate::domain::{AlphaCandidate, AlphaMetrics, AlphaRecord};
use anyhow::{Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::Value;
use std::collections::BTreeMap;
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

    pub async fn needs_simulation(&self, expression: String) -> Result<bool> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let raw: Option<String> = conn
                .query_row(
                    "SELECT raw_data FROM alphas WHERE expression=?1",
                    [expression],
                    |row| row.get(0),
                )
                .optional()?;
            Ok::<_, anyhow::Error>(
                raw.map(|value| !value.contains("wq_alpha_id"))
                    .unwrap_or(true),
            )
        })
        .await
        .context("database simulation state task")?
    }

    pub async fn get_submission_candidates(
        &self,
        limit: i64,
        include_ready: bool,
        linked: Option<bool>,
    ) -> Result<Vec<AlphaRecord>> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let linked_filter = match linked {
                Some(true) => " AND raw_data LIKE '%\"wq_alpha_id\"%'",
                Some(false) => " AND raw_data NOT LIKE '%\"wq_alpha_id\"%'",
                None => "",
            };
            let sql = format!(
                "SELECT id, expression, fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq, failure_reason, raw_data
                 FROM alphas
                 WHERE pass_count>=7
                   AND COALESCE(fail_count, 0)=0
                   AND COALESCE(is_submitted, 0)=0
                   AND COALESCE(is_failed_on_wq, 0)=0
                   AND (?1=1 OR pass_count<8)
                   {linked_filter}
                 ORDER BY fitness DESC, created_at ASC
                 LIMIT ?2"
            );
            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map(params![include_ready as i64, limit], alpha_from_row)?;
            Ok::<_, anyhow::Error>(rows.collect::<rusqlite::Result<Vec<_>>>()?)
        })
        .await
        .context("database submission candidates task")?
    }

    pub async fn submitted_today(&self) -> Result<i64> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            Ok::<_, anyhow::Error>(conn.query_row(
                "SELECT COUNT(*) FROM alphas
                 WHERE COALESCE(is_submitted, 0)=1
                   AND submitted_timestamp>=datetime('now','-24 hours')",
                [],
                |row| row.get(0),
            )?)
        })
        .await
        .context("database daily submitted count task")?
    }

    pub async fn mark_failed(&self, expression: String, reason: String) -> Result<bool> {
        self.update_flags(expression, "is_failed_on_wq=1, failure_reason=?2", reason)
            .await
    }

    pub async fn unmark_failed(&self, expression: String) -> Result<bool> {
        self.update_flags(
            expression,
            "is_failed_on_wq=0, failure_reason=NULL",
            String::new(),
        )
        .await
    }

    pub async fn unmark_submitted(&self, expression: String) -> Result<bool> {
        self.update_flags(
            expression,
            "is_submitted=0, submitted_timestamp=NULL",
            String::new(),
        )
        .await
    }

    async fn update_flags(
        &self,
        expression: String,
        assignment: &'static str,
        reason: String,
    ) -> Result<bool> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let sql = format!("UPDATE alphas SET {assignment} WHERE expression=?1");
            let changed = if assignment.contains("?2") {
                conn.execute(&sql, params![expression, reason])?
            } else {
                conn.execute(&sql, [expression])?
            };
            Ok::<_, anyhow::Error>(changed > 0)
        })
        .await
        .context("database flag task")?
    }

    pub async fn dashboard_summary(&self, limit: i64) -> Result<Value> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let count: i64 = conn.query_row("SELECT COUNT(expression) FROM alphas", [], |row| row.get(0))?;
            let (max_fitness, avg_fitness, max_sharpe): (Option<f64>, Option<f64>, Option<f64>) = conn.query_row(
                "SELECT MAX(fitness), AVG(fitness), MAX(sharpe) FROM alphas WHERE fitness > -900",
                [], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )?;
            let pending: i64 = conn.query_row("SELECT COUNT(expression) FROM alphas WHERE pass_count>=7 AND fail_count=0 AND is_submitted=0 AND is_failed_on_wq=0", [], |row| row.get(0))?;
            let legacy_submission_backlog: i64 = conn.query_row("SELECT COUNT(expression) FROM alphas WHERE pass_count>=7 AND fail_count=0 AND is_submitted=0 AND is_failed_on_wq=0 AND raw_data NOT LIKE '%\"wq_alpha_id\"%'", [], |row| row.get(0))?;
            let linked_check_pending: i64 = conn.query_row("SELECT COUNT(expression) FROM alphas WHERE pass_count=7 AND fail_count=0 AND is_submitted=0 AND is_failed_on_wq=0 AND raw_data LIKE '%\"wq_alpha_id\"%'", [], |row| row.get(0))?;
            let ready_to_submit: i64 = conn.query_row("SELECT COUNT(expression) FROM alphas WHERE pass_count>=8 AND fail_count=0 AND is_submitted=0 AND is_failed_on_wq=0 AND raw_data LIKE '%\"wq_alpha_id\"%'", [], |row| row.get(0))?;
            let submitted_today: i64 = conn.query_row("SELECT COUNT(expression) FROM alphas WHERE is_submitted=1 AND submitted_timestamp>=datetime('now','-24 hours')", [], |row| row.get(0))?;
            let submitted: i64 = conn.query_row("SELECT COUNT(expression) FROM alphas WHERE is_submitted=1", [], |row| row.get(0))?;
            let failed: i64 = conn.query_row("SELECT COUNT(expression) FROM alphas WHERE is_failed_on_wq=1", [], |row| row.get(0))?;
            let mut stmt = conn.prepare("SELECT expression, datetime(created_at,'+8 hours'), datetime(submitted_timestamp,'+8 hours'), fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq FROM alphas ORDER BY fitness DESC LIMIT ?1")?;
            let rows = stmt.query_map([limit], dashboard_row)?;
            let all_alphas = rows.collect::<rusqlite::Result<Vec<_>>>()?;
            Ok::<_, anyhow::Error>(serde_json::json!({
                "count": count, "max_fitness": max_fitness.unwrap_or_default(),
                "avg_fitness": avg_fitness.unwrap_or_default(), "max_sharpe": max_sharpe.unwrap_or_default(),
                "submittable_pending_count": pending, "successfully_submitted_count": submitted,
                "legacy_submission_backlog": legacy_submission_backlog,
                "linked_check_pending": linked_check_pending,
                "ready_to_submit": ready_to_submit,
                "submitted_today": submitted_today,
                "total_submitted_count": submitted + failed, "all_alphas": all_alphas
            }))
        }).await.context("dashboard summary task")?
    }

    pub async fn pending_dashboard(&self, limit: i64) -> Result<Vec<Value>> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let mut stmt = conn.prepare("SELECT expression, datetime(created_at,'+8 hours'), datetime(submitted_timestamp,'+8 hours'), fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq FROM alphas WHERE pass_count>=7 AND fail_count=0 AND is_submitted=0 AND is_failed_on_wq=0 ORDER BY fitness DESC LIMIT ?1")?;
            let rows = stmt.query_map([limit], dashboard_row)?;
            Ok::<_, anyhow::Error>(rows.collect::<rusqlite::Result<Vec<_>>>()?)
        }).await.context("dashboard pending task")?
    }

    pub async fn submission_daily(&self, days: i64) -> Result<Value> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let created_filter = if days > 0 { format!(" AND created_at >= datetime('now','-{} days')", days) } else { String::new() };
            let submitted_filter = if days > 0 { format!(" AND submitted_timestamp >= datetime('now','-{} days')", days) } else { String::new() };
            let submitted = grouped_count(&conn, &format!("SELECT strftime('%Y-%m-%d', datetime(submitted_timestamp,'+8 hours')) FROM alphas WHERE is_submitted=1 AND submitted_timestamp IS NOT NULL{}", submitted_filter))?;
            let submittable = grouped_count(&conn, &format!("SELECT strftime('%Y-%m-%d', datetime(created_at,'+8 hours')) FROM alphas WHERE pass_count>=7 AND fail_count=0{}", created_filter))?;
            let failed = grouped_count(&conn, &format!("SELECT strftime('%Y-%m-%d', datetime(created_at,'+8 hours')) FROM alphas WHERE is_failed_on_wq=1{}", created_filter))?;
            Ok::<_, anyhow::Error>(merge_daily(submittable, submitted, failed))
        }).await.context("daily dashboard task")?
    }

    pub async fn timeseries(&self, days: i64) -> Result<Value> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let filter = if days > 0 { format!(" WHERE created_at >= datetime('now','-{} days')", days) } else { String::new() };
            let mut stmt = conn.prepare(&format!("SELECT strftime('%m-%d %H:00', datetime(created_at,'+8 hours')), COUNT(expression), AVG(fitness), SUM(CASE WHEN pass_count>=7 AND fail_count=0 THEN 1 ELSE 0 END) FROM alphas{} GROUP BY 1 ORDER BY 1", filter))?;
            let rows = stmt.query_map([], |row| Ok((
                row.get::<_, Option<String>>(0)?.unwrap_or_default(),
                row.get::<_, i64>(1)?,
                row.get::<_, Option<f64>>(2)?.unwrap_or_default(),
                row.get::<_, Option<i64>>(3)?.unwrap_or_default(),
            )))?;
            let mut timestamps = Vec::new(); let mut counts = Vec::new(); let mut fitness = Vec::new(); let mut high_quality = Vec::new();
            for row in rows { let (ts, count, fit, hq) = row?; timestamps.push(ts); counts.push(count); fitness.push((fit * 10000.0).round() / 10000.0); high_quality.push(hq); }
            Ok::<_, anyhow::Error>(serde_json::json!({"timestamps":timestamps,"count":counts,"mean_fitness":fitness,"high_quality_count":high_quality}))
        }).await.context("timeseries dashboard task")?
    }

    pub async fn rust_pending(&self, limit: i64) -> Result<Vec<AlphaRecord>> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let mut stmt = conn.prepare("SELECT id, expression, fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq, failure_reason, raw_data FROM alphas WHERE COALESCE(is_failed_on_wq, 0)=0 AND raw_data LIKE '%\"source\":\"rust\"%' AND raw_data NOT LIKE '%wq_alpha_id%' ORDER BY created_at ASC LIMIT ?1")?;
            let rows = stmt.query_map([limit], alpha_from_row)?;
            Ok::<_, anyhow::Error>(rows.collect::<rusqlite::Result<Vec<_>>>()?)
        }).await.context("database Rust pending task")?
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

fn dashboard_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    let fitness = row.get::<_, Option<f64>>(3)?.unwrap_or_default();
    let sharpe = row.get::<_, Option<f64>>(4)?.unwrap_or_default();
    let turnover = row.get::<_, Option<f64>>(6)?.unwrap_or_default();
    let pass_count = row.get::<_, Option<i64>>(7)?.unwrap_or_default();
    let fail_count = row.get::<_, Option<i64>>(8)?.unwrap_or_default();
    Ok(serde_json::json!({
        "expression": row.get::<_, String>(0)?, "timestamp": row.get::<_, Option<String>>(1)?,
        "manual_timestamp": row.get::<_, Option<String>>(2)?, "checks_summary": row.get::<_, Option<String>>(9)?.unwrap_or_default(),
        "is_submittable": pass_count >= 7 && fail_count == 0, "is_submitted": row.get::<_, i64>(10).unwrap_or(0) != 0,
        "is_failed_on_wq": row.get::<_, i64>(11).unwrap_or(0) != 0,
        "is_successfully_submitted": row.get::<_, i64>(10).unwrap_or(0) != 0,
        "dashboard_score": fitness + pass_count as f64 * 0.2 + sharpe.abs() * 0.3 - turnover * 0.1,
        "performance": {"fitness": fitness, "sharpe": sharpe, "returns": row.get::<_, Option<f64>>(5)?.unwrap_or_default(), "turnover": turnover}
    }))
}

fn grouped_count(conn: &Connection, sql: &str) -> Result<BTreeMap<String, i64>> {
    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map([], |row| {
        Ok(row.get::<_, Option<String>>(0)?.unwrap_or_default())
    })?;
    let mut result = BTreeMap::new();
    for row in rows {
        let date = row?;
        if !date.is_empty() {
            *result.entry(date).or_default() += 1;
        }
    }
    Ok(result)
}

fn merge_daily(
    submittable: BTreeMap<String, i64>,
    submitted: BTreeMap<String, i64>,
    failed: BTreeMap<String, i64>,
) -> Value {
    let mut dates = BTreeMap::new();
    for key in submittable
        .keys()
        .chain(submitted.keys())
        .chain(failed.keys())
    {
        dates.insert(key.clone(), ());
    }
    let timestamps: Vec<_> = dates.keys().cloned().collect();
    serde_json::json!({
        "timestamps": timestamps,
        "submittable_count": dates.keys().map(|d| submittable.get(d).copied().unwrap_or_default()).collect::<Vec<_>>(),
        "submitted_count": dates.keys().map(|d| submitted.get(d).copied().unwrap_or_default()).collect::<Vec<_>>(),
        "failed_count": dates.keys().map(|d| failed.get(d).copied().unwrap_or_default()).collect::<Vec<_>>()
    })
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
        let store = AlphaStore::new(&path);
        assert_eq!(store.health().await.unwrap(), 1);
        assert_eq!(
            store
                .get_submission_candidates(10, false, None)
                .await
                .unwrap()
                .len(),
            1
        );
        assert_eq!(store.submitted_today().await.unwrap(), 0);
        let summary = store.dashboard_summary(10).await.unwrap();
        assert_eq!(summary["legacy_submission_backlog"], 1);
        assert_eq!(summary["linked_check_pending"], 0);
        assert_eq!(summary["ready_to_submit"], 0);
        let _ = std::fs::remove_file(path);
    }
}

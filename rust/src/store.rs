use crate::domain::{AlphaCandidate, AlphaMetrics, AlphaRecord};
use anyhow::{Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
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
            let changed = conn.execute(
                "UPDATE alphas
                 SET is_submitted=1,
                     submitted_timestamp=CURRENT_TIMESTAMP,
                     raw_data=json_set(
                         CASE WHEN json_valid(raw_data) THEN raw_data ELSE '{}' END,
                         '$.submission_phase', 'submitted',
                         '$.submission_completed_at', CAST(strftime('%s','now') AS INTEGER),
                         '$.status', 'SUBMITTED'
                     )
                 WHERE expression=?1",
                [expression],
            )?;
            Ok::<_, anyhow::Error>(changed > 0)
        })
        .await
        .context("database mark submitted task")?
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
            let order_by = if linked == Some(true) {
                "CASE WHEN pass_count>=8 THEN 0 ELSE 1 END ASC, COALESCE(CASE WHEN json_valid(raw_data) THEN CAST(json_extract(raw_data, '$.submission_last_attempt_at') AS INTEGER) END, 0) ASC, fitness DESC, created_at ASC"
            } else {
                "fitness DESC, created_at ASC"
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
                 ORDER BY {order_by}
                 LIMIT ?2"
            );
            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map(params![include_ready as i64, limit], alpha_from_row)?;
            Ok::<_, anyhow::Error>(rows.collect::<rusqlite::Result<Vec<_>>>()?)
        })
        .await
        .context("database submission candidates task")?
    }

    pub async fn get_ready_submission_candidates(&self, limit: i64) -> Result<Vec<AlphaRecord>> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let mut stmt = conn.prepare(
                "SELECT id, expression, fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq, failure_reason, raw_data
                 FROM alphas
                 WHERE pass_count>=8
                   AND COALESCE(fail_count, 0)=0
                   AND COALESCE(is_submitted, 0)=0
                   AND COALESCE(is_failed_on_wq, 0)=0
                   AND raw_data LIKE '%\"wq_alpha_id\"%'
                 ORDER BY fitness DESC, created_at ASC
                 LIMIT ?1",
            )?;
            let rows = stmt.query_map([limit], alpha_from_row)?;
            Ok::<_, anyhow::Error>(rows.collect::<rusqlite::Result<Vec<_>>>()?)
        })
        .await
        .context("database ready submission candidates task")?
    }

    pub async fn claim_submission_candidate(&self) -> Result<Option<AlphaRecord>> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let mut conn = open(&path)?;
            let transaction = conn.transaction()?;
            let active_id: Option<String> = transaction
                .query_row(
                    "SELECT value FROM automation_state WHERE key='submission_active_candidate'",
                    [],
                    |row| row.get(0),
                )
                .optional()?;
            if let Some(active_id) = active_id {
                let active = submission_candidate_by_id(&transaction, &active_id)?;
                if active.is_some() {
                    transaction.commit()?;
                    return Ok::<_, anyhow::Error>(active);
                }
                transaction.execute(
                    "DELETE FROM automation_state WHERE key='submission_active_candidate'",
                    [],
                )?;
            }

            let next = transaction
                .query_row(
                    "SELECT id, expression, fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq, failure_reason, raw_data
                     FROM alphas
                     WHERE pass_count>=7
                       AND pass_count<8
                       AND COALESCE(fail_count, 0)=0
                       AND COALESCE(is_submitted, 0)=0
                       AND COALESCE(is_failed_on_wq, 0)=0
                     ORDER BY
                       CASE WHEN json_valid(raw_data) AND json_extract(raw_data, '$.submission_last_attempt_at') IS NOT NULL THEN 1 ELSE 0 END ASC,
                       COALESCE(CASE WHEN json_valid(raw_data) THEN CAST(json_extract(raw_data, '$.submission_last_attempt_at') AS INTEGER) END, 0) ASC,
                       created_at ASC,
                       fitness DESC
                     LIMIT 1",
                    [],
                    alpha_from_row,
                )
                .optional()?;
            if let Some(candidate) = &next {
                transaction.execute(
                    "INSERT INTO automation_state (key, value)
                     VALUES ('submission_active_candidate', ?1)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    [&candidate.id],
                )?;
                transaction.execute(
                    "UPDATE alphas
                     SET raw_data=json_set(
                         CASE WHEN json_valid(raw_data) THEN raw_data ELSE '{}' END,
                         '$.submission_phase', CASE
                             WHEN CASE WHEN json_valid(raw_data) THEN json_extract(raw_data, '$.wq_alpha_id') END IS NULL THEN 'matching'
                             ELSE 'checking'
                         END,
                         '$.submission_enqueued_at', COALESCE(
                             CASE WHEN json_valid(raw_data) THEN json_extract(raw_data, '$.submission_enqueued_at') END,
                             CAST(strftime('%s','now') AS INTEGER)
                         )
                     )
                     WHERE id=?1",
                    [&candidate.id],
                )?;
            }
            transaction.commit()?;
            Ok::<_, anyhow::Error>(next)
        })
        .await
        .context("database claim submission candidate task")?
    }

    pub async fn release_submission_candidate(&self, id: &str) -> Result<()> {
        let path = self.path.clone();
        let id = id.to_owned();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            conn.execute(
                "UPDATE alphas
                 SET raw_data=json_set(
                     CASE WHEN json_valid(raw_data) THEN raw_data ELSE '{}' END,
                     '$.submission_last_attempt_at', COALESCE(
                         CASE WHEN json_valid(raw_data) THEN json_extract(raw_data, '$.submission_last_attempt_at') END,
                         CAST(strftime('%s','now') AS INTEGER)
                     ),
                     '$.submission_phase', CASE
                         WHEN CASE WHEN json_valid(raw_data) THEN json_extract(raw_data, '$.submission_phase') END='checking' THEN 'queued'
                         ELSE COALESCE(CASE WHEN json_valid(raw_data) THEN json_extract(raw_data, '$.submission_phase') END, 'queued')
                     END
                 )
                 WHERE id=?1",
                [&id],
            )?;
            conn.execute(
                "DELETE FROM automation_state
                 WHERE key='submission_active_candidate' AND value=?1",
                [id],
            )?;
            Ok::<_, anyhow::Error>(())
        })
        .await
        .context("database release submission candidate task")?
    }

    pub async fn record_submission_queue_error(
        &self,
        id: &str,
        phase: &str,
        message: &str,
    ) -> Result<i64> {
        let path = self.path.clone();
        let id = id.to_owned();
        let phase = phase.to_owned();
        let message = message.to_owned();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            conn.execute(
                "UPDATE alphas
                 SET raw_data=json_set(
                     CASE WHEN json_valid(raw_data) THEN raw_data ELSE '{}' END,
                     '$.submission_phase', ?2,
                     '$.submission_last_attempt_at', CAST(strftime('%s','now') AS INTEGER),
                     '$.submission_check_retry_count', COALESCE(
                         CASE WHEN json_valid(raw_data) THEN CAST(json_extract(raw_data, '$.submission_check_retry_count') AS INTEGER) END,
                         0
                     ) + 1,
                     '$.submission_last_error', ?3
                 )
                 WHERE id=?1",
                params![id, phase, message],
            )?;
            Ok::<_, anyhow::Error>(conn.query_row(
                "SELECT COALESCE(CAST(json_extract(raw_data, '$.submission_check_retry_count') AS INTEGER), 0) FROM alphas WHERE id=?1",
                [&id],
                |row| row.get(0),
            )?)
        })
        .await
        .context("database submission queue error task")?
    }

    pub async fn record_submission_stage(
        &self,
        id: &str,
        phase: &str,
        alpha_id: Option<&str>,
    ) -> Result<()> {
        let path = self.path.clone();
        let id = id.to_owned();
        let phase = phase.to_owned();
        let alpha_id = alpha_id.map(str::to_owned);
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            conn.execute(
                "UPDATE alphas
                 SET raw_data=json_set(
                     CASE WHEN json_valid(raw_data) THEN raw_data ELSE '{}' END,
                     '$.submission_phase', ?2,
                     '$.submission_last_attempt_at', CAST(strftime('%s','now') AS INTEGER),
                     '$.wq_alpha_id', COALESCE(
                         ?3,
                         CASE WHEN json_valid(raw_data) THEN json_extract(raw_data, '$.wq_alpha_id') END
                     )
                 )
                 WHERE id=?1",
                params![id, phase, alpha_id],
            )?;
            Ok::<_, anyhow::Error>(())
        })
        .await
        .context("database submission stage task")?
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
            let mut stmt = conn.prepare("SELECT expression, datetime(created_at,'+8 hours'), datetime(submitted_timestamp,'+8 hours'), fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq, raw_data FROM alphas WHERE pass_count>=7 AND fail_count=0 AND is_submitted=0 AND is_failed_on_wq=0 ORDER BY fitness DESC LIMIT ?1")?;
            let rows = stmt.query_map([limit], pending_dashboard_row)?;
            Ok::<_, anyhow::Error>(rows.collect::<rusqlite::Result<Vec<_>>>()?)
        }).await.context("dashboard pending task")?
    }

    pub async fn submission_queue_dashboard(&self, limit: i64) -> Result<Value> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open(&path)?;
            let active_id: Option<String> = conn
                .query_row(
                    "SELECT value FROM automation_state WHERE key='submission_active_candidate'",
                    [],
                    |row| row.get(0),
                )
                .optional()?;
            let mut statement = conn.prepare(
                "SELECT id, expression, datetime(created_at,'+8 hours'), fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, raw_data
                 FROM alphas
                 WHERE (pass_count>=7
                   AND COALESCE(fail_count, 0)=0
                   AND COALESCE(is_submitted, 0)=0
                   AND COALESCE(is_failed_on_wq, 0)=0)
                    OR id=?1
                 ORDER BY
                   CASE WHEN id=?1 THEN 0 WHEN pass_count>=8 THEN 1 ELSE 2 END ASC,
                   CASE WHEN json_valid(raw_data) AND json_extract(raw_data, '$.submission_last_attempt_at') IS NOT NULL THEN 1 ELSE 0 END ASC,
                   COALESCE(CASE WHEN json_valid(raw_data) THEN CAST(json_extract(raw_data, '$.submission_last_attempt_at') AS INTEGER) END, 0) ASC,
                   created_at ASC,
                   fitness DESC
                 LIMIT ?2",
            )?;
            let queue_rows = statement.query_map(
                params![active_id.clone().unwrap_or_default(), limit],
                |row| {
                    let id: String = row.get(0)?;
                    let raw = row
                        .get::<_, Option<String>>(10)?
                        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
                        .unwrap_or_else(|| json!({}));
                    let pass_count = row.get::<_, Option<i64>>(7)?.unwrap_or_default();
                    let raw_phase = raw
                        .get("submission_phase")
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    let phase = if active_id.as_deref() == Some(id.as_str()) {
                        match raw_phase {
                            "check_error" => "retrying",
                            "matching" => "matching",
                            "resimulation_required" | "resimulating" => "resimulating",
                            "submitting" => "submitting",
                            _ => "checking",
                        }
                    } else if pass_count >= 8 {
                        "ready"
                    } else if raw_phase == "check_error" || raw_phase == "match_missing" {
                        "retry_wait"
                    } else {
                        "queued"
                    };
                    let checks_summary = row.get::<_, Option<String>>(9)?.unwrap_or_default();
                    let checks = serde_json::from_str::<Value>(&checks_summary)
                        .unwrap_or_else(|_| Value::Array(Vec::new()));
                    Ok(json!({
                        "id": id,
                        "wq_alpha_id": raw.get("wq_alpha_id").cloned().unwrap_or(Value::Null),
                        "expression": row.get::<_, String>(1)?,
                        "created_at": row.get::<_, Option<String>>(2)?,
                        "phase": phase,
                        "fitness": row.get::<_, Option<f64>>(3)?.unwrap_or_default(),
                        "sharpe": row.get::<_, Option<f64>>(4)?.unwrap_or_default(),
                        "returns": row.get::<_, Option<f64>>(5)?.unwrap_or_default(),
                        "turnover": row.get::<_, Option<f64>>(6)?.unwrap_or_default(),
                        "pass_count": pass_count,
                        "fail_count": row.get::<_, Option<i64>>(8)?.unwrap_or_default(),
                        "checks": checks,
                        "retry_count": raw.get("submission_check_retry_count").and_then(Value::as_i64).unwrap_or_default(),
                        "last_error": raw.get("submission_last_error").cloned().unwrap_or(Value::Null),
                        "last_attempt_at": raw.get("submission_last_attempt_at").cloned().unwrap_or(Value::Null)
                    }))
                },
            )?;
            let mut queue = queue_rows.collect::<rusqlite::Result<Vec<_>>>()?;
            for (index, row) in queue.iter_mut().enumerate() {
                if let Some(object) = row.as_object_mut() {
                    object.insert("position".into(), json!(index + 1));
                }
            }

            let mut result_statement = conn.prepare(
                "SELECT id, expression, datetime(created_at,'+8 hours'), COALESCE(datetime(submitted_timestamp,'+8 hours'), datetime(CASE WHEN json_valid(raw_data) THEN json_extract(raw_data, '$.submission_completed_at') END, 'unixepoch', '+8 hours')), fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq, failure_reason, raw_data
                 FROM alphas
                 WHERE COALESCE(is_submitted, 0)=1 OR COALESCE(is_failed_on_wq, 0)=1
                 ORDER BY COALESCE(
                     submitted_timestamp,
                     datetime(CASE WHEN json_valid(raw_data) THEN json_extract(raw_data, '$.submission_completed_at') END, 'unixepoch'),
                     created_at
                 ) DESC
                 LIMIT 100",
            )?;
            let result_rows = result_statement.query_map([], |row| {
                let checks_summary = row.get::<_, Option<String>>(10)?.unwrap_or_default();
                let checks = serde_json::from_str::<Value>(&checks_summary)
                    .unwrap_or_else(|_| Value::Array(Vec::new()));
                let submitted = row.get::<_, i64>(11).unwrap_or_default() != 0;
                Ok(json!({
                    "id": row.get::<_, Option<String>>(0)?,
                    "expression": row.get::<_, String>(1)?,
                    "created_at": row.get::<_, Option<String>>(2)?,
                    "completed_at": row.get::<_, Option<String>>(3)?,
                    "phase": if submitted { "submitted" } else { "failed" },
                    "fitness": row.get::<_, Option<f64>>(4)?.unwrap_or_default(),
                    "sharpe": row.get::<_, Option<f64>>(5)?.unwrap_or_default(),
                    "returns": row.get::<_, Option<f64>>(6)?.unwrap_or_default(),
                    "turnover": row.get::<_, Option<f64>>(7)?.unwrap_or_default(),
                    "pass_count": row.get::<_, Option<i64>>(8)?.unwrap_or_default(),
                    "fail_count": row.get::<_, Option<i64>>(9)?.unwrap_or_default(),
                    "checks": checks,
                    "is_submitted": submitted,
                    "is_failed_on_wq": row.get::<_, i64>(12).unwrap_or_default() != 0,
                    "failure_reason": row.get::<_, Option<String>>(13)?
                }))
            })?;
            let recent_results = result_rows.collect::<rusqlite::Result<Vec<_>>>()?;
            let submitted_last_24h: i64 = conn.query_row(
                "SELECT COUNT(*) FROM alphas WHERE COALESCE(is_submitted, 0)=1 AND submitted_timestamp>=datetime('now','-24 hours')",
                [],
                |row| row.get(0),
            )?;
            let count_phase = |phase: &str| {
                queue
                    .iter()
                    .filter(|row| row.get("phase").and_then(Value::as_str) == Some(phase))
                    .count()
            };
            let active = queue
                .iter()
                .find(|row| matches!(row.get("phase").and_then(Value::as_str), Some("checking" | "retrying" | "matching" | "resimulating" | "submitting")))
                .cloned()
                .unwrap_or(Value::Null);
            Ok::<_, anyhow::Error>(json!({
                "submission_limit": "unlimited",
                "submitted_last_24h": submitted_last_24h,
                "active": active,
                "summary": {
                    "total": queue.len(),
                    "queued": count_phase("queued"),
                    "checking": count_phase("checking"),
                    "matching": count_phase("matching"),
                    "resimulating": count_phase("resimulating"),
                    "submitting": count_phase("submitting"),
                    "retrying": count_phase("retrying") + count_phase("retry_wait"),
                    "ready": count_phase("ready")
                },
                "queue": queue,
                "recent_results": recent_results
            }))
        })
        .await
        .context("dashboard submission queue task")?
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

fn submission_candidate_by_id(conn: &Connection, id: &str) -> Result<Option<AlphaRecord>> {
    Ok(conn
        .query_row(
            "SELECT id, expression, fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq, failure_reason, raw_data
             FROM alphas
             WHERE id=?1
               AND pass_count>=7
               AND pass_count<8
               AND COALESCE(fail_count, 0)=0
               AND COALESCE(is_submitted, 0)=0
               AND COALESCE(is_failed_on_wq, 0)=0",
            [id],
            alpha_from_row,
        )
        .optional()?)
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
        "pass_count": pass_count, "fail_count": fail_count,
        "dashboard_score": fitness + pass_count as f64 * 0.2 + sharpe.abs() * 0.3 - turnover * 0.1,
        "performance": {"fitness": fitness, "sharpe": sharpe, "returns": row.get::<_, Option<f64>>(5)?.unwrap_or_default(), "turnover": turnover}
    }))
}

fn pending_dashboard_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    let mut value = dashboard_row(row)?;
    let raw = row
        .get::<_, Option<String>>(12)?
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .unwrap_or(Value::Null);
    let alpha_id = raw.get("wq_alpha_id").and_then(Value::as_str);
    let pass_count = value
        .get("pass_count")
        .and_then(Value::as_i64)
        .unwrap_or_default();
    let phase = raw
        .get("submission_phase")
        .and_then(Value::as_str)
        .unwrap_or_else(|| {
            if pass_count >= 8 {
                "ready"
            } else if alpha_id.is_some() {
                "checking"
            } else {
                "waiting_check"
            }
        });
    if let Some(object) = value.as_object_mut() {
        object.insert("submission_phase".into(), json!(phase));
        object.insert("wq_alpha_id".into(), json!(alpha_id));
        object.insert(
            "submission_last_attempt_at".into(),
            raw.get("submission_last_attempt_at")
                .cloned()
                .unwrap_or(Value::Null),
        );
    }
    Ok(value)
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
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=NORMAL;
         CREATE TABLE IF NOT EXISTS automation_state (
             key TEXT PRIMARY KEY,
             value TEXT NOT NULL
         );",
    )?;
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

    #[tokio::test]
    async fn linked_submission_candidates_rotate_after_an_attempt() {
        let path = std::env::temp_dir().join(format!(
            "wq-rs-queue-test-{}.db",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE alphas (id TEXT PRIMARY KEY, expression TEXT NOT NULL, fitness REAL, sharpe REAL, returns REAL, turnover REAL, pass_count INTEGER, fail_count INTEGER, checks_summary TEXT, is_submitted INTEGER, is_failed_on_wq INTEGER, failure_reason TEXT, raw_data TEXT, created_at TEXT, submitted_timestamp TEXT);").unwrap();
        conn.execute("INSERT INTO alphas (id, expression, fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq, raw_data, created_at) VALUES ('high','rank(high);',2.0,1.5,0.1,0.2,7,0,'7 PASS',0,0,'{\"wq_alpha_id\":\"remote-high\"}',CURRENT_TIMESTAMP)", []).unwrap();
        conn.execute("INSERT INTO alphas (id, expression, fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq, raw_data, created_at) VALUES ('low','rank(low);',1.0,1.3,0.1,0.2,7,0,'7 PASS',0,0,'{\"wq_alpha_id\":\"remote-low\"}',CURRENT_TIMESTAMP)", []).unwrap();
        conn.execute("INSERT INTO alphas (id, expression, fitness, sharpe, returns, turnover, pass_count, fail_count, checks_summary, is_submitted, is_failed_on_wq, raw_data, created_at) VALUES ('ready','rank(close);',0.8,1.4,0.1,0.2,8,0,'8 PASS',0,0,'{\"wq_alpha_id\":\"remote-ready\",\"submission_phase\":\"ready\",\"submission_last_attempt_at\":999}',CURRENT_TIMESTAMP)", []).unwrap();
        drop(conn);
        let store = AlphaStore::new(&path);

        let first = store
            .get_submission_candidates(1, false, Some(true))
            .await
            .unwrap();
        assert_eq!(first[0].id, "high");

        let conn = Connection::open(&path).unwrap();
        conn.execute(
            "UPDATE alphas SET raw_data='{\"wq_alpha_id\":\"remote-high\",\"submission_phase\":\"checking\",\"submission_last_attempt_at\":100}' WHERE id='high'",
            [],
        )
        .unwrap();
        drop(conn);

        let next = store
            .get_submission_candidates(1, false, Some(true))
            .await
            .unwrap();
        assert_eq!(next[0].id, "low");
        let ready = store
            .get_submission_candidates(1, true, Some(true))
            .await
            .unwrap();
        assert_eq!(ready[0].id, "ready");
        let pending = store.pending_dashboard(10).await.unwrap();
        let high = pending
            .iter()
            .find(|row| row.get("expression").and_then(Value::as_str) == Some("rank(high);"))
            .unwrap();
        assert_eq!(high["submission_phase"], "checking");
        assert_eq!(high["wq_alpha_id"], "remote-high");
        assert_eq!(high["submission_last_attempt_at"], 100);
        assert_eq!(high["performance"]["fitness"], 2.0);
        assert_eq!(high["pass_count"], 7);
        let _ = std::fs::remove_file(path);
    }

    #[tokio::test]
    async fn submission_queue_keeps_exactly_one_active_candidate() {
        let path = std::env::temp_dir().join(format!(
            "wq-rs-single-check-queue-{}.db",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE alphas (id TEXT PRIMARY KEY, expression TEXT NOT NULL, fitness REAL, sharpe REAL, returns REAL, turnover REAL, pass_count INTEGER, fail_count INTEGER, checks_summary TEXT, is_submitted INTEGER, is_failed_on_wq INTEGER, failure_reason TEXT, raw_data TEXT, created_at TEXT, submitted_timestamp TEXT);").unwrap();
        conn.execute("INSERT INTO alphas (id, expression, pass_count, fail_count, is_submitted, is_failed_on_wq, raw_data, created_at) VALUES ('first','rank(close);',7,0,0,0,'{}','2025-01-01')", []).unwrap();
        conn.execute("INSERT INTO alphas (id, expression, pass_count, fail_count, is_submitted, is_failed_on_wq, raw_data, created_at) VALUES ('second','rank(open);',7,0,0,0,'{}','2025-01-02')", []).unwrap();
        drop(conn);
        let store = AlphaStore::new(&path);

        let first = store.claim_submission_candidate().await.unwrap().unwrap();
        let same = store.claim_submission_candidate().await.unwrap().unwrap();
        assert_eq!(first.id, "first");
        assert_eq!(same.id, "first");

        store.release_submission_candidate("first").await.unwrap();
        let second = store.claim_submission_candidate().await.unwrap().unwrap();
        assert_eq!(second.id, "second");
        let dashboard = store.submission_queue_dashboard(10).await.unwrap();
        assert_eq!(dashboard["active"]["id"], "second");
        assert_eq!(dashboard["active"]["phase"], "matching");
        assert_eq!(dashboard["summary"]["queued"], 1);
        assert_eq!(dashboard["submission_limit"], "unlimited");
        store
            .record_submission_stage("second", "resimulating", Some("remote-second"))
            .await
            .unwrap();
        let resimulating = store.submission_queue_dashboard(10).await.unwrap();
        assert_eq!(resimulating["active"]["phase"], "resimulating");
        assert_eq!(resimulating["active"]["wq_alpha_id"], "remote-second");
        assert_eq!(resimulating["active"]["pass_count"], 7);

        let _ = std::fs::remove_file(path);
    }
}

use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Miner,
    Evolver,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct AlphaMetrics {
    pub fitness: f64,
    pub sharpe: f64,
    pub returns: f64,
    pub turnover: f64,
    pub pass_count: i64,
    pub fail_count: i64,
    pub checks_summary: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AlphaCandidate {
    pub expression: String,
    pub settings: serde_json::Value,
    pub parent_id: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AlphaRecord {
    pub id: String,
    pub expression: String,
    pub metrics: AlphaMetrics,
    pub is_submitted: bool,
    pub is_failed_on_wq: bool,
    pub failure_reason: Option<String>,
    pub raw_data: serde_json::Value,
}

#[derive(Clone, Debug, Default)]
pub struct ExpressionPolicy {
    pub fields: Vec<String>,
    pub operators: Vec<String>,
    pub forbidden: Vec<String>,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ExpressionError {
    #[error("empty expression")]
    Empty,
    #[error("expression has no closing delimiter")]
    Unbalanced,
    #[error("unknown identifiers: {0:?}")]
    UnknownIdentifiers(Vec<String>),
    #[error("forbidden identifiers: {0:?}")]
    ForbiddenIdentifiers(Vec<String>),
}

impl ExpressionPolicy {
    pub fn validate(&self, expression: &str) -> Result<String, ExpressionError> {
        let normalized = normalize_expression(expression)?;
        let tokens: Vec<String> = regex::Regex::new(r"[A-Za-z_][A-Za-z0-9_]*")
            .expect("static regex")
            .find_iter(&normalized)
            .map(|m| m.as_str().to_owned())
            .collect();
        let allowed: std::collections::HashSet<&str> = self
            .fields
            .iter()
            .chain(self.operators.iter())
            .map(String::as_str)
            .chain(["and", "or", "not", "true", "false", "nan", "inf"])
            .collect();
        let forbidden: Vec<String> = tokens
            .iter()
            .filter(|token| self.forbidden.iter().any(|bad| bad == *token))
            .cloned()
            .collect::<std::collections::HashSet<_>>()
            .into_iter()
            .collect();
        if !forbidden.is_empty() {
            return Err(ExpressionError::ForbiddenIdentifiers(forbidden));
        }
        let unknown: Vec<String> = tokens
            .iter()
            .filter(|token| !allowed.contains(token.as_str()))
            .cloned()
            .collect::<std::collections::HashSet<_>>()
            .into_iter()
            .collect();
        if !unknown.is_empty() {
            return Err(ExpressionError::UnknownIdentifiers(unknown));
        }
        Ok(normalized)
    }
}

pub fn normalize_expression(expression: &str) -> Result<String, ExpressionError> {
    let mut value = expression.trim().replace("```", "");
    if let Some(start) = value.find(";") {
        value.truncate(start + 1);
    }
    if value.is_empty() {
        return Err(ExpressionError::Empty);
    }
    if !value.ends_with(';') {
        value.push(';');
    }
    let open = value.chars().filter(|c| *c == '(').count();
    let close = value.chars().filter(|c| *c == ')').count();
    if open != close {
        return Err(ExpressionError::Unbalanced);
    }
    Ok(value.split_whitespace().collect::<Vec<_>>().join(" "))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy() -> ExpressionPolicy {
        ExpressionPolicy {
            fields: vec!["close".into(), "volume".into()],
            operators: vec!["rank".into(), "ts_mean".into()],
            forbidden: vec!["cap".into()],
        }
    }

    #[test]
    fn accepts_valid_expression() {
        assert_eq!(
            policy().validate("rank(ts_mean(close, 10));").unwrap(),
            "rank(ts_mean(close, 10));"
        );
    }

    #[test]
    fn rejects_unknown_identifier() {
        assert!(matches!(
            policy().validate("rank(invented(close));"),
            Err(ExpressionError::UnknownIdentifiers(_))
        ));
    }

    #[test]
    fn rejects_forbidden_identifier() {
        assert!(matches!(
            policy().validate("rank(cap);"),
            Err(ExpressionError::ForbiddenIdentifiers(_))
        ));
    }
}

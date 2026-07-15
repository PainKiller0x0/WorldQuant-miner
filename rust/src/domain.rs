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
    pub role: Role,
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
pub struct AlphaPools {
    pub successful: Vec<AlphaRecord>,
    pub candidates: Vec<AlphaRecord>,
    pub failures: Vec<AlphaRecord>,
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
    #[error("expression contains illegal characters: {0:?}")]
    IllegalCharacters(Vec<char>),
    #[error("operator {operator} expects {expected} arguments, got {actual}")]
    InvalidArity {
        operator: String,
        expected: usize,
        actual: usize,
    },
    #[error("operator {operator} requires an integer lookback from 2 to 252, got {value}")]
    InvalidLookback { operator: String, value: String },
    #[error("operator {operator} requires a numeric exponent, got {value}")]
    InvalidExponent { operator: String, value: String },
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
        validate_operator_arguments(&normalized)?;
        Ok(normalized)
    }
}

pub fn normalize_expression(expression: &str) -> Result<String, ExpressionError> {
    let mut value = expression.trim().replace('`', "");
    if let Some(start) = value.find(";") {
        value.truncate(start + 1);
    }
    if value.is_empty() {
        return Err(ExpressionError::Empty);
    }
    if !value.ends_with(';') {
        value.push(';');
    }
    let illegal = value
        .chars()
        .filter(|character| {
            !(character.is_ascii_alphanumeric()
                || character.is_ascii_whitespace()
                || matches!(
                    character,
                    '_' | '('
                        | ')'
                        | ','
                        | '.'
                        | ';'
                        | '+'
                        | '-'
                        | '*'
                        | '/'
                        | '<'
                        | '>'
                        | '='
                        | '!'
                        | '&'
                        | '|'
                        | '?'
                        | ':'
                ))
        })
        .collect::<std::collections::HashSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    if !illegal.is_empty() {
        return Err(ExpressionError::IllegalCharacters(illegal));
    }
    let mut depth = 0_i64;
    for character in value.chars() {
        match character {
            '(' => depth += 1,
            ')' => {
                depth -= 1;
                if depth < 0 {
                    return Err(ExpressionError::Unbalanced);
                }
            }
            _ => {}
        }
    }
    if depth != 0 {
        return Err(ExpressionError::Unbalanced);
    }
    Ok(value.split_whitespace().collect::<Vec<_>>().join(" "))
}

fn validate_operator_arguments(expression: &str) -> Result<(), ExpressionError> {
    let token_pattern =
        regex::Regex::new(r"[A-Za-z_][A-Za-z0-9_]*").expect("static operator token regex");
    for token in token_pattern.find_iter(expression) {
        let operator = token.as_str();
        let Some(expected) = expected_arity(operator) else {
            continue;
        };
        let suffix = &expression[token.end()..];
        let whitespace = suffix.len() - suffix.trim_start().len();
        let open = token.end() + whitespace;
        if expression.as_bytes().get(open) != Some(&b'(') {
            continue;
        }
        let arguments = function_arguments(expression, open).ok_or(ExpressionError::Unbalanced)?;
        let actual = arguments
            .iter()
            .filter(|argument| !argument.trim().is_empty())
            .count();
        if actual != expected || arguments.len() != expected {
            return Err(ExpressionError::InvalidArity {
                operator: operator.to_owned(),
                expected,
                actual,
            });
        }
        if let Some(index) = lookback_argument(operator) {
            let value = arguments[index].trim();
            let valid = value
                .parse::<u16>()
                .map(|lookback| (2..=252).contains(&lookback))
                .unwrap_or(false);
            if !valid {
                return Err(ExpressionError::InvalidLookback {
                    operator: operator.to_owned(),
                    value: value.to_owned(),
                });
            }
        }
        if matches!(operator, "signed_power" | "power") {
            let value = arguments[1].trim();
            if value.parse::<f64>().is_err() {
                return Err(ExpressionError::InvalidExponent {
                    operator: operator.to_owned(),
                    value: value.to_owned(),
                });
            }
        }
    }
    Ok(())
}

fn expected_arity(operator: &str) -> Option<usize> {
    match operator {
        "rank" | "zscore" | "abs" | "log" | "sqrt" | "sign" => Some(1),
        "ts_mean" | "ts_std_dev" | "ts_delta" | "ts_sum" | "ts_rank" | "ts_zscore"
        | "ts_decay_linear" | "max" | "min" | "signed_power" | "power" | "multiply" | "divide"
        | "add" | "subtract" => Some(2),
        "ts_corr" | "correlation" => Some(3),
        _ => None,
    }
}

fn lookback_argument(operator: &str) -> Option<usize> {
    match operator {
        "ts_mean" | "ts_std_dev" | "ts_delta" | "ts_sum" | "ts_rank" | "ts_zscore"
        | "ts_decay_linear" => Some(1),
        "ts_corr" | "correlation" => Some(2),
        _ => None,
    }
}

fn function_arguments(expression: &str, open: usize) -> Option<Vec<&str>> {
    let mut depth = 0_usize;
    let mut start = open + 1;
    let mut arguments = Vec::new();
    for (offset, character) in expression[(open + 1)..].char_indices() {
        let index = open + 1 + offset;
        match character {
            '(' => depth += 1,
            ')' if depth == 0 => {
                let value = expression[start..index].trim();
                if !value.is_empty() {
                    arguments.push(value);
                } else if !arguments.is_empty() {
                    arguments.push(value);
                }
                return Some(arguments);
            }
            ')' => depth -= 1,
            ',' if depth == 0 => {
                arguments.push(expression[start..index].trim());
                start = index + 1;
            }
            _ => {}
        }
    }
    None
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

    #[test]
    fn rejects_full_width_punctuation_before_worldquant() {
        assert!(policy().validate("rank(ts_mean(close， 10));").is_err());
    }

    #[test]
    fn rejects_operator_with_wrong_argument_count() {
        assert!(policy().validate("rank(ts_mean(close));").is_err());
        assert!(policy().validate("rank(ts_mean(close, 10, 20));").is_err());
        assert!(policy().validate("rank(ts_mean(close, ));").is_err());
    }
}

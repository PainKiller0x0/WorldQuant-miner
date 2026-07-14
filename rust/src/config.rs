use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    env, fs,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ModelConfig {
    pub model_name: String,
    pub api_key: String,
    pub base_url: String,
}

#[derive(Clone, Debug)]
pub struct AppConfig {
    pub root: PathBuf,
    pub database_path: PathBuf,
    pub api_config_path: PathBuf,
    pub system_config_path: PathBuf,
    pub wq_user_id: String,
    pub wq_api_key: String,
    pub miner_models: Vec<ModelConfig>,
    pub evolver_models: Vec<ModelConfig>,
    pub listen: String,
}

impl AppConfig {
    pub fn load() -> Result<Self> {
        let root = env::var_os("WQ_ROOT")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        let _ = dotenvy::from_path(root.join(".env"));
        let database_path =
            root.join(env::var("WQ_DATABASE").unwrap_or_else(|_| "wq_miner.db".into()));
        let api_config_path =
            root.join(env::var("WQ_API_CONFIG").unwrap_or_else(|_| "api_config.json".into()));
        let system_config_path =
            root.join(env::var("WQ_SYSTEM_CONFIG").unwrap_or_else(|_| "system_config.json".into()));
        let raw = fs::read_to_string(&api_config_path)
            .with_context(|| format!("read {}", api_config_path.display()))?;
        let values: HashMap<String, ModelConfig> =
            serde_json::from_str(&raw).context("parse api_config.json")?;
        let miner_models = role_models(&values, "miner");
        let evolver_models = role_models(&values, "evolver");
        Ok(Self {
            root,
            database_path,
            api_config_path,
            system_config_path,
            wq_user_id: env::var("WQ_USER_ID").context("WQ_USER_ID is required")?,
            wq_api_key: env::var("WQ_API_KEY").context("WQ_API_KEY is required")?,
            miner_models,
            evolver_models,
            listen: env::var("WQ_LISTEN").unwrap_or_else(|_| "0.0.0.0:5000".into()),
        })
    }

    pub fn from_paths(
        root: impl AsRef<Path>,
        models: (Vec<ModelConfig>, Vec<ModelConfig>),
    ) -> Self {
        let root = root.as_ref().to_path_buf();
        Self {
            database_path: root.join("wq_miner.db"),
            api_config_path: root.join("api_config.json"),
            system_config_path: root.join("system_config.json"),
            root,
            wq_user_id: String::new(),
            wq_api_key: String::new(),
            miner_models: models.0,
            evolver_models: models.1,
            listen: "127.0.0.1:5000".into(),
        }
    }
}

fn role_models(values: &HashMap<String, ModelConfig>, role: &str) -> Vec<ModelConfig> {
    let mut result = Vec::new();
    let names = [
        format!("{role}_config"),
        format!("{role}_config_backup"),
        format!("{role}_config_backup_2"),
        format!("{role}_config_backup_3"),
    ];
    for name in names {
        if let Some(value) = values.get(&name) {
            if !value.api_key.is_empty()
                && !value.model_name.is_empty()
                && !value.base_url.is_empty()
            {
                result.push(value.clone());
            }
        }
    }
    result
}

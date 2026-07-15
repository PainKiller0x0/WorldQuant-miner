mod config;
mod dashboard;
mod domain;
mod gateway;
mod llm;
mod store;
mod workflow;

use anyhow::Result;
use clap::{Parser, Subcommand, ValueEnum};
use config::AppConfig;
use domain::Role;
use gateway::{FakeWorldQuant, LiveWorldQuant, WorldQuantGateway};
use llm::{LiveModelGateway, ModelGateway};
use std::{sync::Arc, time::Duration};
use store::AlphaStore;
use tracing_subscriber::EnvFilter;

#[derive(Parser, Debug)]
#[command(
    name = "worldquant-miner-rs",
    version,
    about = "Rust WorldQuant miner, evolver and submitter"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    Health,
    Dashboard,
    Run {
        #[arg(value_enum, default_value_t = RoleArg::Both)]
        role: RoleArg,
        #[arg(long, default_value_t = 30)]
        interval_secs: u64,
    },
    Submit {
        #[arg(long, default_value_t = 10)]
        limit: i64,
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        auto_submit: bool,
    },
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum RoleArg {
    Miner,
    Evolver,
    Both,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_target(false)
        .with_ansi(false)
        .init();
    let cli = Cli::parse();
    let config = AppConfig::load()?;
    let store = Arc::new(AlphaStore::new(&config.database_path));

    match cli.command {
        Command::Health => {
            let count = store.health().await?;
            println!(
                "{}",
                serde_json::json!({"ok":true,"runtime":"rust","alphas":count})
            );
        }
        Command::Dashboard => {
            dashboard::serve(
                store,
                config.root.clone(),
                config.system_config_path.clone(),
                config.api_config_path.clone(),
                &config.listen,
            )
            .await?
        }
        Command::Run {
            role,
            interval_secs,
        } => {
            let models: Arc<dyn ModelGateway> = Arc::new(LiveModelGateway::new(&config)?);
            let worldquant: Arc<dyn WorldQuantGateway> =
                if std::env::var("WQ_FAKE").ok().as_deref() == Some("1") {
                    Arc::new(FakeWorldQuant)
                } else {
                    Arc::new(LiveWorldQuant::new(
                        config.wq_user_id.clone(),
                        config.wq_api_key.clone(),
                    )?)
                };
            let interval = Duration::from_secs(interval_secs.max(1));
            match role {
                RoleArg::Miner => {
                    workflow::run_loop(Role::Miner, store, models, worldquant, interval).await?
                }
                RoleArg::Evolver => {
                    workflow::run_loop(Role::Evolver, store, models, worldquant, interval).await?
                }
                RoleArg::Both => {
                    let miner = workflow::run_loop(
                        Role::Miner,
                        store.clone(),
                        models.clone(),
                        worldquant.clone(),
                        interval,
                    );
                    let evolver =
                        workflow::run_loop(Role::Evolver, store, models, worldquant, interval);
                    tokio::try_join!(miner, evolver)?;
                }
            }
        }
        Command::Submit {
            limit,
            dry_run,
            auto_submit,
        } => {
            let worldquant: Arc<dyn WorldQuantGateway> =
                if std::env::var("WQ_FAKE").ok().as_deref() == Some("1") {
                    Arc::new(FakeWorldQuant)
                } else {
                    Arc::new(LiveWorldQuant::new(config.wq_user_id, config.wq_api_key)?)
                };
            let result = workflow::process_submissions(
                store,
                worldquant,
                limit.max(1),
                dry_run,
                auto_submit,
            )
            .await?;
            println!("{}", serde_json::to_string(&result)?);
        }
    }
    Ok(())
}

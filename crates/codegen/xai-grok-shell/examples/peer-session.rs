//! Pager-free ACP stdio entrypoint for exercising this checkout's session actor.
use xai_grok_shell::agent::config::{AgentMode, Config};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let raw = xai_grok_shell::config::load_effective_config()?;
    let mut config = Config::new_from_toml_cfg(&raw).map_err(anyhow::Error::msg)?;
    config.mode = AgentMode::Stdio;
    xai_grok_shell::agent::app::run_stdio_agent(&config, None, None).await
}

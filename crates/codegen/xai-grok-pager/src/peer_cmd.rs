//! Local peer CLI. No leader attach, HTTP listener, discovery, or SSH process.
use clap::{Args, Subcommand};
use std::path::PathBuf;
#[derive(Debug, Clone, Args)]
pub struct PeerArgs {
    #[command(subcommand)]
    pub command: PeerCommand,
}
#[derive(Debug, Clone, Subcommand)]
pub enum PeerCommand {
    /// Write one NDJSON message. Direct writes have no delivery acknowledgement.
    Send {
        #[arg(long)]
        to: PathBuf,
        #[arg(long)]
        text: String,
        #[arg(long, default_value = "grok")]
        from_name: String,
        #[arg(long)]
        reply_socket: PathBuf,
        /// Require our proxy's bounded downstream result (never use with a direct Claude socket).
        #[arg(long)]
        expect_result: bool,
    },
    /// Run an exact single-session bridge on loopback. JSON config contains paths, never a token value.
    Bridge {
        #[arg(long)]
        config: PathBuf,
    },
    /// Create a random mode 0600 token file without printing the token.
    Token {
        #[arg(long)]
        file: PathBuf,
    },
}
pub async fn run(args: PeerArgs) -> anyhow::Result<()> {
    match args.command {
        PeerCommand::Send {
            to,
            text,
            from_name,
            reply_socket,
            expect_result,
        } => {
            anyhow::ensure!(reply_socket.is_absolute(), "absolute reply socket required");
            let message = xai_peer_socket::PeerMessage::new(
                text,
                from_name,
                format!("uds:{}", reply_socket.display()),
            );
            let result = if expect_result {
                xai_peer_socket::bridge::send_proxy(&to, &message).await?
            } else {
                xai_peer_socket::send_unix(&to, &message).await?
            };
            println!("{result}");
        }
        PeerCommand::Bridge { config } => {
            use std::io::Read;
            let mut bytes = Vec::new();
            std::fs::File::open(config)?
                .take((xai_peer_socket::MAX_LINE + 1) as u64)
                .read_to_end(&mut bytes)?;
            anyhow::ensure!(
                bytes.len() <= xai_peer_socket::MAX_LINE,
                "bridge config too large"
            );
            xai_peer_socket::bridge::run(serde_json::from_slice(&bytes)?).await?;
        }
        PeerCommand::Token { file } => xai_peer_socket::create_token(&file)?,
    }
    Ok(())
}

//! Runtime support for the existing host seat helper, not a generic secret resolver.

use crate::{AgentBuildError, AgentDefinition};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

/// The helper loads only the selected bot's private file. Neither its token nor
/// any subprocess output is returned to the agent, model, logs or command line.
const READINESS_PROBE: &str = r#"
import contextlib, importlib.util, pathlib, socket, sys
helper, home, bot = sys.argv[1:]
network_attempted = False
def deny_network(*args, **kwargs):
    global network_attempted
    network_attempted = True
    raise PermissionError('network disabled in local readiness probe')
class NoNetworkSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        deny_network()
socket.socket = NoNetworkSocket
socket.create_connection = deny_network
socket.getaddrinfo = deny_network
socket.gethostbyname = deny_network
socket.gethostbyname_ex = deny_network
socket.gethostbyaddr = deny_network
try:
    with open('/dev/null', 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        spec = importlib.util.spec_from_file_location('grok_selected_seat_helper', helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.HOME = pathlib.Path(home)
        token = module._token(bot)
        ready = isinstance(token, str) and bool(token.strip()) and not network_attempted
        del token
except BaseException:
    ready = False
sys.exit(0 if ready else 11)
"#;

#[derive(Debug, Clone)]
pub(crate) struct ResolvedSlackBinding {
    helper: PathBuf,
    bot_key: String,
    bot_user_id: String,
    profile_name: String,
    source_path: Option<PathBuf>,
    profile_revision: Option<String>,
    admitted_at: std::time::Instant,
}

fn refused(reason: &str) -> AgentBuildError {
    AgentBuildError::InvalidConfig(format!(
        "Slack identity unavailable: {reason} No child was started."
    ))
}

fn safe_bot_key(key: &str) -> bool {
    !key.is_empty()
        && key.len() <= 96
        && key
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn checked_file(path: &Path, home: &Path, private: bool) -> bool {
    let Ok(metadata) = std::fs::symlink_metadata(path) else {
        return false;
    };
    if !metadata.is_file() || metadata.len() > if private { 64 * 1024 } else { 1024 * 1024 } {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let Ok(owner) = std::fs::metadata(home) else {
            return false;
        };
        if metadata.uid() != owner.uid()
            || metadata.mode() & (if private { 0o077 } else { 0o022 }) != 0
        {
            return false;
        }
    }
    true
}

fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

impl ResolvedSlackBinding {
    fn matches(&self, definition: &AgentDefinition) -> bool {
        let Some(slack) = definition
            .identity
            .as_ref()
            .and_then(|identity| identity.slack.as_ref())
        else {
            return false;
        };
        self.admitted_at.elapsed() < Duration::from_secs(30)
            && self.profile_name == definition.name
            && self.source_path == definition.source_path
            && self.profile_revision == definition.applied_profile_revision
            && slack.bot_key.as_deref() == Some(self.bot_key.as_str())
            && slack.bot_user_id.as_deref() == Some(self.bot_user_id.as_str())
            && slack.credential_ref.as_deref()
                == Some(format!("seat-bot:{}", self.bot_key).as_str())
    }

    fn prompt(&self) -> String {
        let helper = shell_quote(&self.helper.to_string_lossy());
        let bot = shell_quote(&self.bot_key);
        format!(
            "## Channel identity\n\nConfigured Slack bot user ID: {}. The admitted credential binding is seat-bot:{}; it uses only this bot's private ~/.grok/{}-slack.env file through the existing trusted seat helper. It does not resolve arbitrary env: or op:// references and has no default-bot fallback.\n\nFor an explicitly authorized Slack message, use the permitted Bash tool and the exact selected-bot helper prefix `/usr/bin/python3 {helper} {bot}`. Supported message commands are `post CHANNEL [THREAD_TS]`, `reply CHANNEL TS THREAD_TS [--no]` and `followup CHANNEL ORIGINAL_TS`; message text goes on stdin, never a credential or message body argument. Keep the task's existing permission checks. Do not substitute another bot or a generic Slack integration. A configured identity does not authorize sending, starting a persistent seat or consuming that seat's inbox. The configured bot user ID is display metadata; this local readiness check does not authenticate with Slack.",
            self.bot_user_id, self.bot_key, self.bot_key
        )
    }
}

impl AgentDefinition {
    /// Resolve only the explicit supported host capability, before child resources
    /// are allocated. Portable profile validation deliberately does not call this.
    pub async fn admit_slack_identity(&mut self) -> Result<(), AgentBuildError> {
        self.admitted_slack_binding = None;
        if self
            .identity
            .as_ref()
            .and_then(|identity| identity.slack.as_ref())
            .is_none()
        {
            return Ok(());
        }
        let home = xai_dirs::home_dir()
            .ok_or_else(|| refused("the host home directory is unavailable."))?;
        self.admitted_slack_binding = self.resolve_slack_identity_at(&home).await?;
        Ok(())
    }

    pub(crate) async fn resolve_slack_identity(
        &mut self,
    ) -> Result<Option<String>, AgentBuildError> {
        if !self
            .admitted_slack_binding
            .as_ref()
            .is_some_and(|binding| binding.matches(self))
        {
            self.admit_slack_identity().await?;
        }
        Ok(self
            .admitted_slack_binding
            .as_ref()
            .map(ResolvedSlackBinding::prompt))
    }

    async fn resolve_slack_identity_at(
        &self,
        home: &Path,
    ) -> Result<Option<ResolvedSlackBinding>, AgentBuildError> {
        let Some(slack) = self
            .identity
            .as_ref()
            .and_then(|identity| identity.slack.as_ref())
        else {
            return Ok(None);
        };
        let Some(bot_key) = slack.bot_key.as_deref().filter(|key| safe_bot_key(key)) else {
            return Err(refused("a valid botKey is required."));
        };
        let Some(bot_user_id) = slack.bot_user_id.as_deref().filter(|id| {
            id.starts_with('U')
                && id.len() > 1
                && id
                    .bytes()
                    .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit())
        }) else {
            return Err(refused("a valid configured botUserId is required."));
        };
        let expected = format!("seat-bot:{bot_key}");
        if slack.credential_ref.as_deref() != Some(expected.as_str()) {
            return Err(refused(&format!(
                "use credentialRef {expected} for this selected bot; missing, mismatched, env: and op:// runtime bindings are unsupported. Select a supported profile or explicitly update its binding and retry."
            )));
        }
        let helper = home.join(".agentee-hub/bin/seat-bot.py");
        if !checked_file(&helper, home, false) {
            return Err(refused("the trusted seat-bot helper is missing or unsafe."));
        }
        let selected_file = home.join(format!(".grok/{bot_key}-slack.env"));
        if !checked_file(&selected_file, home, true) {
            return Err(refused(
                "the selected bot's private credential file is missing or unsafe.",
            ));
        }
        let mut command = tokio::process::Command::new("/usr/bin/python3");
        command
            .args(["-I", "-B", "-c", READINESS_PROBE])
            .arg(&helper)
            .arg(home)
            .arg(bot_key)
            .env_clear()
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        match tokio::time::timeout(Duration::from_secs(3), command.status()).await {
            Ok(Ok(status)) if status.success() => Ok(Some(ResolvedSlackBinding {
                helper,
                bot_key: bot_key.to_owned(),
                bot_user_id: bot_user_id.to_owned(),
                profile_name: self.name.clone(),
                source_path: self.source_path.clone(),
                profile_revision: self.applied_profile_revision.clone(),
                admitted_at: std::time::Instant::now(),
            })),
            _ => Err(refused(
                "the selected bot token or helper is unavailable; no other bot or credential was used.",
            )),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn definition(reference: Option<&str>) -> AgentDefinition {
        let mut slack = serde_json::json!({"botKey":"cfo", "botUserId":"UTESTCFO"});
        if let Some(reference) = reference {
            slack["credentialRef"] = serde_json::json!(reference);
        }
        AgentDefinition::from_json(&serde_json::json!({
            "name":"cfo", "description":"Finance test", "identity":{"slack":slack}
        }))
        .unwrap()
    }

    fn fixture() -> tempfile::TempDir {
        let home = tempfile::tempdir().unwrap();
        let helper = home.path().join(".agentee-hub/bin/seat-bot.py");
        std::fs::create_dir_all(helper.parent().unwrap()).unwrap();
        std::fs::write(&helper, r#"
from pathlib import Path
HOME = Path.home()
def _token(bot):
    values = dict(line.split('=', 1) for line in (HOME / f'.grok/{bot}-slack.env').read_text().splitlines() if '=' in line)
    return values[f'{bot.upper()}_SLACK_BOT_TOKEN'].strip().strip('"').strip("'")
"#).unwrap();
        std::fs::create_dir(home.path().join(".grok")).unwrap();
        home
    }

    fn credential(home: &Path, bot: &str, text: &str) {
        let path = home.join(format!(".grok/{bot}-slack.env"));
        std::fs::write(&path, text).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
        }
    }

    #[tokio::test]
    async fn profile_slack_binding_uses_only_the_selected_helper_file() {
        let home = fixture();
        credential(
            home.path(),
            "cfo",
            "CFO_SLACK_BOT_TOKEN=fake-cfo-test-only\n",
        );
        credential(
            home.path(),
            "cro",
            "CRO_SLACK_BOT_TOKEN=fake-cro-test-only\n",
        );
        let resolved = definition(Some("seat-bot:cfo"))
            .resolve_slack_identity_at(home.path())
            .await
            .unwrap()
            .unwrap();
        let prompt = resolved.prompt();
        assert!(prompt.contains("seat-bot:cfo"));
        assert!(prompt.contains("'cfo'"));
        assert!(prompt.contains("message text goes on stdin"));
        assert!(!prompt.contains("fake-"));
        credential(home.path(), "cfo", "CFO_SLACK_BOT_TOKEN=\n");
        assert!(
            definition(Some("seat-bot:cfo"))
                .resolve_slack_identity_at(home.path())
                .await
                .is_err(),
            "a ready CRO must never replace missing CFO credentials"
        );
    }

    #[tokio::test]
    async fn profile_slack_missing_mismatched_and_unsupported_refs_fail_without_echo() {
        let home = fixture();
        for reference in [
            None,
            Some("seat-bot:cro"),
            Some("env:CFO_SLACK_BOT_TOKEN"),
            Some("op://Private/CFO/token"),
            Some("private-untrusted-value"),
        ] {
            let error = definition(reference)
                .resolve_slack_identity_at(home.path())
                .await
                .unwrap_err()
                .to_string();
            assert!(error.contains("use credentialRef seat-bot:cfo"));
            assert!(!error.contains("Private/CFO") && !error.contains("private-untrusted-value"));
        }
    }

    #[tokio::test]
    async fn profile_slack_missing_helper_and_selected_file_refuse_cleanly() {
        let home = fixture();
        let def = definition(Some("seat-bot:cfo"));
        let error = def
            .resolve_slack_identity_at(home.path())
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("credential file is missing or unsafe"));
        std::fs::remove_file(home.path().join(".agentee-hub/bin/seat-bot.py")).unwrap();
        let error = def
            .resolve_slack_identity_at(home.path())
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("helper is missing or unsafe"));
    }

    #[tokio::test]
    async fn profile_slack_missing_wrong_and_blank_token_never_fall_back() {
        let home = fixture();
        for text in [
            "OTHER_SLACK_BOT_TOKEN=fake-test-only\n",
            "CFO_SLACK_BOT_TOKEN=\n",
            "# no token\n",
        ] {
            credential(home.path(), "cfo", text);
            let error = definition(Some("seat-bot:cfo"))
                .resolve_slack_identity_at(home.path())
                .await
                .unwrap_err()
                .to_string();
            assert!(error.contains("selected bot token or helper is unavailable"));
            assert!(!error.contains("fake-test-only"));
        }
    }

    #[tokio::test]
    async fn profile_without_slack_does_not_require_host_helper_or_credentials() {
        let def = AgentDefinition::general_purpose();
        let home = tempfile::tempdir().unwrap();
        assert!(
            def.resolve_slack_identity_at(home.path())
                .await
                .unwrap()
                .is_none()
        );
    }

    #[tokio::test]
    async fn profile_slack_probe_blocks_import_time_network_even_when_helper_catches_it() {
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let address = listener.local_addr().unwrap();
        let home = fixture();
        credential(
            home.path(),
            "cfo",
            "CFO_SLACK_BOT_TOKEN=fake-cfo-test-only\n",
        );
        let helper = home.path().join(".agentee-hub/bin/seat-bot.py");
        for call in [
            "socket.socket()".to_owned(),
            format!(
                "socket.create_connection(('127.0.0.1', {}))",
                address.port()
            ),
            "socket.getaddrinfo('localhost', 80)".to_owned(),
        ] {
            std::fs::write(&helper, format!(
                "import socket, sys\nprint('private-probe-print')\nprint('private-probe-stderr', file=sys.stderr)\ntry:\n    {call}\nexcept Exception:\n    pass\ndef _token(bot):\n    return 'private-fake-token'\n"
            )).unwrap();
            let error = definition(Some("seat-bot:cfo"))
                .resolve_slack_identity_at(home.path())
                .await
                .unwrap_err()
                .to_string();
            assert!(error.contains("selected bot token or helper is unavailable"));
            assert!(!error.contains("private-"));
            assert!(
                matches!(listener.accept(), Err(error) if error.kind() == std::io::ErrorKind::WouldBlock),
                "readiness must make zero network requests"
            );
        }
    }

    #[tokio::test]
    async fn profile_slack_probe_suppresses_helper_output_and_exceptions() {
        let home = fixture();
        credential(
            home.path(),
            "cfo",
            "CFO_SLACK_BOT_TOKEN=fake-cfo-test-only\n",
        );
        let helper = home.path().join(".agentee-hub/bin/seat-bot.py");
        std::fs::write(&helper, "import os, sys\nprint('private-probe-print')\nprint('private-probe-stderr', file=sys.stderr)\nos.write(1, b'private-raw-stdout')\nos.write(2, b'private-raw-stderr')\nraise RuntimeError('private-probe-error')\n").unwrap();
        let error = definition(Some("seat-bot:cfo"))
            .resolve_slack_identity_at(home.path())
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("selected bot token or helper is unavailable"));
        assert!(!error.contains("private-"));
    }

    #[tokio::test]
    async fn profile_slack_probe_timeout_is_bounded_and_refuses() {
        let home = fixture();
        credential(
            home.path(),
            "cfo",
            "CFO_SLACK_BOT_TOKEN=fake-cfo-test-only\n",
        );
        let helper = home.path().join(".agentee-hub/bin/seat-bot.py");
        std::fs::write(
            &helper,
            "import time\ntime.sleep(60)\ndef _token(bot):\n    return 'private-fake-token'\n",
        )
        .unwrap();
        let start = std::time::Instant::now();
        assert!(
            definition(Some("seat-bot:cfo"))
                .resolve_slack_identity_at(home.path())
                .await
                .is_err()
        );
        assert!(start.elapsed() < Duration::from_secs(6));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn profile_slack_unsafe_permissions_and_leaf_symlinks_refuse() {
        use std::os::unix::fs::{PermissionsExt, symlink};
        let home = fixture();
        credential(
            home.path(),
            "cfo",
            "CFO_SLACK_BOT_TOKEN=fake-cfo-test-only\n",
        );
        let helper = home.path().join(".agentee-hub/bin/seat-bot.py");
        let selected = home.path().join(".grok/cfo-slack.env");
        let def = definition(Some("seat-bot:cfo"));
        for (path, unsafe_mode, safe_mode) in [(&helper, 0o666, 0o755), (&selected, 0o644, 0o600)] {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(unsafe_mode)).unwrap();
            assert!(def.resolve_slack_identity_at(home.path()).await.is_err());
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(safe_mode)).unwrap();
            let moved = path.with_extension("original");
            std::fs::rename(path, &moved).unwrap();
            symlink(&moved, path).unwrap();
            assert!(def.resolve_slack_identity_at(home.path()).await.is_err());
            std::fs::remove_file(path).unwrap();
            std::fs::rename(&moved, path).unwrap();
        }
        assert!(def.resolve_slack_identity_at(home.path()).await.is_ok());
    }

    #[tokio::test]
    async fn profile_slack_invalid_identity_never_echoes_configured_values() {
        let home = fixture();
        for slack in [
            serde_json::json!({"botKey":"../private-invalid", "botUserId":"UTESTCFO"}),
            serde_json::json!({"botKey":"cfo", "botUserId":"private-invalid"}),
            serde_json::json!({"botUserId":"UTESTCFO"}),
        ] {
            let def = AgentDefinition::from_json(&serde_json::json!({"name":"cfo", "description":"Finance", "identity":{"slack":slack}})).unwrap();
            let error = def
                .resolve_slack_identity_at(home.path())
                .await
                .unwrap_err()
                .to_string();
            assert!(!error.contains("private-invalid"));
        }
    }

    #[tokio::test]
    async fn profile_slack_admission_proof_is_short_lived_exact_and_never_serialized() {
        let home = fixture();
        credential(
            home.path(),
            "cfo",
            "CFO_SLACK_BOT_TOKEN=fake-cfo-test-only\n",
        );
        let mut def = definition(Some("seat-bot:cfo"));
        let binding = def
            .resolve_slack_identity_at(home.path())
            .await
            .unwrap()
            .unwrap();
        assert!(binding.matches(&def));
        def.admitted_slack_binding = Some(binding.clone());
        // A direct handoff uses its exact in-memory proof without a second import.
        std::fs::remove_file(home.path().join(".agentee-hub/bin/seat-bot.py")).unwrap();
        assert!(
            def.resolve_slack_identity()
                .await
                .unwrap()
                .unwrap()
                .contains("seat-bot:cfo")
        );
        let encoded = def.to_json_value();
        assert!(encoded.get("admittedSlackBinding").is_none());
        assert!(
            AgentDefinition::from_json(&encoded)
                .unwrap()
                .admitted_slack_binding
                .is_none()
        );
        let mut attempted = encoded;
        attempted["admittedSlackBinding"] = serde_json::json!({"ready":true});
        assert!(
            AgentDefinition::from_json(&attempted)
                .unwrap()
                .admitted_slack_binding
                .is_none()
        );
        for change in 0..5 {
            let mut changed = def.clone();
            match change {
                0 => changed.name = "cro".into(),
                1 => changed.source_path = Some(home.path().join("other.md")),
                2 => changed.applied_profile_revision = Some("different-source-revision".into()),
                3 => {
                    changed
                        .identity
                        .as_mut()
                        .unwrap()
                        .slack
                        .as_mut()
                        .unwrap()
                        .credential_ref = Some("seat-bot:cro".into())
                }
                _ => {
                    changed
                        .identity
                        .as_mut()
                        .unwrap()
                        .slack
                        .as_mut()
                        .unwrap()
                        .bot_user_id = Some("UTESTOTHER".into())
                }
            }
            assert!(!binding.matches(&changed));
        }
        let mut expired = binding;
        expired.admitted_at = expired
            .admitted_at
            .checked_sub(Duration::from_secs(31))
            .unwrap();
        assert!(!expired.matches(&def));
    }
}

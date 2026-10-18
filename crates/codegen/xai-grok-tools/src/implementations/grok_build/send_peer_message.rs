//! Peer writes are name-routed through the private local registry or use the configured proxy.
use crate::types::tool::{ToolKind, ToolNamespace};
use std::{collections::HashMap, path::PathBuf};
use xai_peer_socket::registry::PeerRegistry;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct SendPeerMessageInput {
    /// Text sent as untrusted peer data.
    pub text: String,
    /// Session name, `name [ref]`, or the exact operator-configured proxy path.
    /// Omitted selects the configured proxy for compatibility.
    pub to: Option<String>,
    /// Optional identity assertion. Must match this session's inbox for local sends or the
    /// configured reply path for proxy sends.
    pub reply_socket: Option<String>,
}

#[derive(Debug, Default)]
pub struct SendPeerMessageTool;

#[derive(Debug, Clone, PartialEq, Eq)]
enum Transport {
    Local(PathBuf),
    Proxy(PathBuf),
}

#[derive(Debug)]
struct ConfiguredMessage {
    transport: Transport,
    message: xai_peer_socket::PeerMessage,
}

fn configured_message(
    env: &HashMap<String, String>,
    registry: &PeerRegistry,
    claude_registry: &xai_peer_socket::claude::ClaudeSessionRegistry,
    input: SendPeerMessageInput,
) -> Result<ConfiguredMessage, xai_tool_runtime::ToolError> {
    let invalid = xai_tool_runtime::ToolError::invalid_arguments;
    let own_socket = env.get("GROK_PEER_SOCKET");
    let current_session_id = env.get("GROK_PEER_SESSION_ID");
    let (transport, reply_socket) = match input.to.as_deref() {
        Some(target) if std::path::Path::new(target).is_absolute() => {
            let destination = env
                .get("GROK_PEER_DESTINATION")
                .filter(|destination| destination.as_str() == target)
                .ok_or_else(|| {
                    invalid("Peer destination must be a registered session name or the configured proxy path")
                })?;
            let reply = env
                .get("GROK_PEER_REPLY_SOCKET")
                .or(own_socket)
                .ok_or_else(|| invalid("No session reply socket"))?;
            if input
                .reply_socket
                .as_ref()
                .is_some_and(|value| value != reply)
            {
                return Err(invalid(
                    "Peer reply identity must match session configuration",
                ));
            }
            (Transport::Proxy(destination.into()), reply.clone())
        }
        Some(target) => {
            let current_session_id = current_session_id
                .ok_or_else(|| invalid("Current peer session identity is unavailable"))?;
            let peer = xai_peer_socket::registry::resolve_combined(
                registry,
                claude_registry,
                target,
                current_session_id,
            )
            .map_err(|error| xai_tool_runtime::ToolError::invalid_arguments(error.to_string()))?;
            let own_socket =
                own_socket.ok_or_else(|| invalid("Current peer inbox is unavailable"))?;
            if input
                .reply_socket
                .as_ref()
                .is_some_and(|value| value != own_socket)
            {
                return Err(invalid("Peer reply socket must be this session's inbox"));
            }
            (Transport::Local(peer.socket), own_socket.clone())
        }
        None => {
            let destination = env
                .get("GROK_PEER_DESTINATION")
                .ok_or_else(|| invalid("No peer destination or session name was provided"))?;
            let reply = env
                .get("GROK_PEER_REPLY_SOCKET")
                .or(own_socket)
                .ok_or_else(|| invalid("No session reply socket"))?;
            if input
                .reply_socket
                .as_ref()
                .is_some_and(|value| value != reply)
            {
                return Err(invalid(
                    "Peer reply identity must match session configuration",
                ));
            }
            (Transport::Proxy(destination.into()), reply.clone())
        }
    };

    let class = env
        .get("GROK_PEER_CLASS")
        .map(String::as_str)
        .unwrap_or("prompting")
        .parse::<xai_peer_socket::PeerClass>()
        .map_err(|_| invalid("GROK_PEER_CLASS must be prompting or bypass"))?;
    let message =
        xai_peer_socket::PeerMessage::new(input.text, "grok".into(), format!("uds:{reply_socket}"))
            .with_class(class);
    Ok(ConfiguredMessage { transport, message })
}

async fn send_configured(
    env: &HashMap<String, String>,
    registry: &PeerRegistry,
    claude_registry: &xai_peer_socket::claude::ClaudeSessionRegistry,
    input: SendPeerMessageInput,
) -> Result<&'static str, xai_tool_runtime::ToolError> {
    let invalid = xai_tool_runtime::ToolError::invalid_arguments;
    let configured = configured_message(env, registry, claude_registry, input)?;
    let result = match configured.transport {
        Transport::Local(destination) => {
            xai_peer_socket::send_unix(&destination, &configured.message).await
        }
        Transport::Proxy(destination) => {
            xai_peer_socket::bridge::send_proxy(&destination, &configured.message).await
        }
    };
    result.map_err(|_| {
        invalid("Peer transport failed or delivery unknown; do not automatically retry")
    })
}

impl crate::types::tool_metadata::ToolMetadata for SendPeerMessageTool {
    fn kind(&self) -> ToolKind {
        ToolKind::Write
    }
    fn tool_namespace(&self) -> ToolNamespace {
        ToolNamespace::GrokBuild
    }
    fn description_template(&self) -> &str {
        "Send untrusted text to another live Grok or Claude Code session on this Mac by name, optionally `name [ref]` when names are ambiguous. The exact operator-configured proxy path remains supported for remote sessions. Reply identity is fixed by this session; never automatically retry an ambiguous write."
    }
}

impl xai_tool_runtime::Tool for SendPeerMessageTool {
    type Args = SendPeerMessageInput;
    type Output = crate::types::output::ToolOutput;
    fn id(&self) -> xai_tool_protocol::ToolId {
        xai_tool_protocol::ToolId::new("send_peer_message").expect("valid tool id")
    }
    fn description(
        &self,
        _: &xai_tool_runtime::ListToolsContext,
    ) -> xai_tool_types::ToolDescription {
        xai_tool_types::ToolDescription::new(
            "send_peer_message",
            crate::types::tool_metadata::ToolMetadata::sanitized_description_template(self),
        )
    }
    fn capabilities(&self) -> xai_tool_protocol::ToolCapabilities {
        xai_tool_protocol::ToolCapabilities {
            is_read_only: false,
            tool_scope: Some(xai_tool_protocol::ToolScope::Write),
            ..Default::default()
        }
    }
    async fn run(
        &self,
        ctx: xai_tool_runtime::ToolCallContext,
        input: Self::Args,
    ) -> Result<Self::Output, xai_tool_runtime::ToolError> {
        let resources = crate::types::tool_metadata::shared_resources(&ctx)?;
        let env = {
            let resources = resources.lock().await;
            resources
                .require::<crate::types::resources::SessionEnv>()?
                .0
                .clone()
        };
        let claude_registry = xai_dirs::home_dir()
            .map(|home| {
                xai_peer_socket::claude::ClaudeSessionRegistry::new(home.join(".claude/sessions"))
            })
            .unwrap_or_else(xai_peer_socket::claude::ClaudeSessionRegistry::empty);
        let delivery = send_configured(
            &env,
            &PeerRegistry::new(crate::util::grok_home()),
            &claude_registry,
            input,
        )
        .await?;
        Ok(crate::types::output::TextOutput::from(delivery).into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::PermissionsExt, time::Duration};
    use tokio::net::UnixListener;

    fn env() -> HashMap<String, String> {
        [
            ("GROK_PEER_DESTINATION", "/local/proxy.sock"),
            ("GROK_PEER_SOCKET", "/local/session.sock"),
            ("GROK_PEER_SESSION_ID", "caller-session"),
            ("GROK_PEER_REPLY_SOCKET", "/remote/proxy.sock"),
        ]
        .into_iter()
        .map(|(key, value)| (key.into(), value.into()))
        .collect()
    }

    fn input() -> SendPeerMessageInput {
        SendPeerMessageInput {
            text: "hello".into(),
            to: None,
            reply_socket: None,
        }
    }

    fn claude_registry(home: &std::path::Path) -> xai_peer_socket::claude::ClaudeSessionRegistry {
        xai_peer_socket::claude::ClaudeSessionRegistry::new(home.join("claude-sessions"))
    }

    fn write_claude_session(
        directory: &std::path::Path,
        file_name: &str,
        session_id: &str,
        name: &str,
        socket: &std::path::Path,
    ) {
        fs::create_dir_all(directory).unwrap();
        fs::write(
            directory.join(file_name),
            serde_json::json!({
                "sessionId": session_id,
                "name": name,
                "pid": std::process::id(),
                "messagingSocketPath": socket,
                "cwd": "/claude/worktree"
            })
            .to_string(),
        )
        .unwrap();
        fs::set_permissions(directory.join(file_name), fs::Permissions::from_mode(0o644)).unwrap();
    }

    fn registry_and_sockets(
        home: &std::path::Path,
        names: &[(&str, &str, &str)],
    ) -> (
        PeerRegistry,
        Vec<UnixListener>,
        Vec<xai_peer_socket::SocketGuard>,
        Vec<xai_peer_socket::registry::PeerRegistration>,
    ) {
        let sockets = home.join("socks");
        fs::create_dir(&sockets).unwrap();
        fs::set_permissions(&sockets, fs::Permissions::from_mode(0o700)).unwrap();
        let registry = PeerRegistry::new(home);
        let mut listeners = Vec::new();
        let mut guards = Vec::new();
        let mut registrations = Vec::new();
        for (session_id, name, cwd) in names {
            let socket = sockets.join(format!("{session_id}.sock"));
            let (guard, listener) = xai_peer_socket::SocketGuard::bind(&socket).unwrap();
            let registration = registry
                .register(xai_peer_socket::registry::PeerSessionRecord::new(
                    *session_id,
                    *name,
                    std::process::id(),
                    &socket,
                    *cwd,
                ))
                .unwrap();
            listeners.push(listener);
            guards.push(guard);
            registrations.push(registration);
        }
        (registry, listeners, guards, registrations)
    }

    #[test]
    fn peer_send_is_a_policy_controlled_write() {
        use crate::types::tool_metadata::ToolMetadata;
        use xai_tool_runtime::Tool;
        assert_eq!(SendPeerMessageTool.kind(), ToolKind::Write);
        assert!(!SendPeerMessageTool.capabilities().is_read_only);
        let access = xai_grok_permission_rules::types::AccessKind::from(
            &crate::types::ToolInput::from(input()),
        );
        assert!(
            matches!(access, xai_grok_permission_rules::types::AccessKind::Tool(name) if name == "send_peer_message")
        );
    }

    #[test]
    fn send_peer_configured_proxy_is_preserved_and_other_paths_are_rejected() {
        let env = env();
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let registry = PeerRegistry::new(home.path());
        let claude_registry = claude_registry(home.path());
        let configured = configured_message(&env, &registry, &claude_registry, input()).unwrap();
        assert_eq!(
            configured.transport,
            Transport::Proxy("/local/proxy.sock".into())
        );
        assert_eq!(configured.message.origin.sender, "grok");
        assert_eq!(
            configured.message.origin.reply_socket,
            "uds:/remote/proxy.sock"
        );

        let mut evil = input();
        evil.to = Some("/arbitrary.sock".into());
        assert!(configured_message(&env, &registry, &claude_registry, evil).is_err());
        let mut evil = input();
        evil.reply_socket = Some("/impersonate.sock".into());
        assert!(configured_message(&env, &registry, &claude_registry, evil).is_err());
        assert!(
            configured_message(&Default::default(), &registry, &claude_registry, input()).is_err()
        );
        assert!(
            serde_json::from_value::<SendPeerMessageInput>(
                serde_json::json!({"text":"hi", "from_name":"Daniel"})
            )
            .is_err()
        );
    }

    #[tokio::test]
    async fn send_peer_by_name_hits_only_the_named_socket_and_uses_sender_inbox_as_reply() {
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let (registry, listeners, _guards, _registrations) = registry_and_sockets(
            home.path(),
            &[
                ("caller-session", "Caller", "/caller"),
                ("target-session", "Target", "/target"),
                ("other-session", "Other", "/other"),
            ],
        );
        let claude_registry = claude_registry(home.path());
        let sockets = home.path().join("socks");
        let mut sender_env = HashMap::from([
            (
                "GROK_PEER_SOCKET".into(),
                sockets.join("caller-session.sock").display().to_string(),
            ),
            ("GROK_PEER_SESSION_ID".into(), "caller-session".into()),
        ]);
        sender_env.insert("GROK_PEER_CLASS".into(), "bypass".into());
        let mut message_input = input();
        message_input.to = Some("Target".into());
        let result = send_configured(&sender_env, &registry, &claude_registry, message_input)
            .await
            .unwrap();
        assert!(result.contains("locally written"));

        let (mut stream, _) = tokio::time::timeout(Duration::from_secs(2), listeners[1].accept())
            .await
            .unwrap()
            .unwrap();
        let received = xai_peer_socket::PeerMessage::decode(
            &xai_peer_socket::read_line(&mut stream).await.unwrap(),
        )
        .unwrap();
        assert_eq!(received.text, "hello");
        assert_eq!(received.class.as_str(), "bypass");
        assert_eq!(
            received.origin.reply_socket,
            format!("uds:{}", sockets.join("caller-session.sock").display())
        );
        assert!(
            tokio::time::timeout(Duration::from_millis(30), listeners[0].accept())
                .await
                .is_err()
        );
        assert!(
            tokio::time::timeout(Duration::from_millis(30), listeners[2].accept())
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn send_peer_duplicate_names_need_a_ref_and_current_session_is_rejected() {
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let (registry, listeners, _guards, _registrations) = registry_and_sockets(
            home.path(),
            &[
                ("caller-session", "Caller", "/caller"),
                ("first-123456", "Same", "/first"),
                ("second-789012", "Same", "/second"),
            ],
        );
        let claude_registry = claude_registry(home.path());
        let sockets = home.path().join("socks");
        let sender_env = HashMap::from([
            (
                "GROK_PEER_SOCKET".into(),
                sockets.join("caller-session.sock").display().to_string(),
            ),
            ("GROK_PEER_SESSION_ID".into(), "caller-session".into()),
        ]);
        let mut bare = input();
        bare.to = Some("Same".into());
        assert!(
            send_configured(&sender_env, &registry, &claude_registry, bare)
                .await
                .is_err()
        );
        let mut selected = input();
        selected.to = Some("Same [second-7]".into());
        send_configured(&sender_env, &registry, &claude_registry, selected)
            .await
            .unwrap();
        let (mut stream, _) = tokio::time::timeout(Duration::from_secs(2), listeners[2].accept())
            .await
            .unwrap()
            .unwrap();
        let received = xai_peer_socket::PeerMessage::decode(
            &xai_peer_socket::read_line(&mut stream).await.unwrap(),
        )
        .unwrap();
        assert_eq!(received.text, "hello");
        assert!(
            tokio::time::timeout(Duration::from_millis(30), listeners[1].accept())
                .await
                .is_err()
        );

        let mut self_send = input();
        self_send.to = Some("Caller".into());
        assert!(
            send_configured(&sender_env, &registry, &claude_registry, self_send)
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn send_peer_by_claude_name_writes_only_to_the_claude_socket() {
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let (registry, listeners, _guards, _registrations) = registry_and_sockets(
            home.path(),
            &[
                ("caller-session", "Caller", "/caller"),
                ("grok-target", "Grok target", "/grok"),
            ],
        );
        let sockets = home.path().join("socks");
        let claude_socket = sockets.join("claude-target.sock");
        let (_claude_guard, claude_listener) =
            xai_peer_socket::SocketGuard::bind(&claude_socket).unwrap();
        let claude_dir = home.path().join("claude-sessions");
        write_claude_session(
            &claude_dir,
            "12345.json",
            "claude-target-session",
            "Claude target",
            &claude_socket,
        );
        let claude_registry = xai_peer_socket::claude::ClaudeSessionRegistry::new(&claude_dir);
        let sender_env = HashMap::from([
            (
                "GROK_PEER_SOCKET".into(),
                sockets.join("caller-session.sock").display().to_string(),
            ),
            ("GROK_PEER_SESSION_ID".into(), "caller-session".into()),
        ]);
        let mut message_input = input();
        message_input.to = Some("Claude target".into());
        send_configured(&sender_env, &registry, &claude_registry, message_input)
            .await
            .unwrap();

        let (mut stream, _) =
            tokio::time::timeout(Duration::from_secs(2), claude_listener.accept())
                .await
                .unwrap()
                .unwrap();
        let line = xai_peer_socket::read_line(&mut stream).await.unwrap();
        let received = xai_peer_socket::PeerMessage::decode(&line).unwrap();
        assert_eq!(received.text, "hello");
        assert_eq!(received.origin.sender, "grok");
        assert_eq!(received.class, xai_peer_socket::PeerClass::Prompting);
        assert_eq!(
            received.origin.reply_socket,
            format!("uds:{}", sockets.join("caller-session.sock").display())
        );
        let frame: serde_json::Value = serde_json::from_slice(&line).unwrap();
        assert_eq!(frame["type"], "user");
        assert!(
            tokio::time::timeout(Duration::from_millis(30), listeners[1].accept())
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn send_peer_shared_grok_and_claude_name_requires_ref_and_routes_each_ref_only() {
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let (registry, listeners, _guards, _registrations) = registry_and_sockets(
            home.path(),
            &[
                ("caller-session", "Caller", "/caller"),
                ("grok-1234-session", "Shared", "/grok"),
            ],
        );
        let sockets = home.path().join("socks");
        let claude_socket = sockets.join("claude-shared.sock");
        let (_claude_guard, claude_listener) =
            xai_peer_socket::SocketGuard::bind(&claude_socket).unwrap();
        let claude_dir = home.path().join("claude-sessions");
        write_claude_session(
            &claude_dir,
            "54321.json",
            "claude-1234-session",
            "Shared",
            &claude_socket,
        );
        let claude_registry = xai_peer_socket::claude::ClaudeSessionRegistry::new(&claude_dir);
        let sender_env = HashMap::from([
            (
                "GROK_PEER_SOCKET".into(),
                sockets.join("caller-session.sock").display().to_string(),
            ),
            ("GROK_PEER_SESSION_ID".into(), "caller-session".into()),
        ]);

        let mut ambiguous = input();
        ambiguous.to = Some("Shared".into());
        let error = send_configured(&sender_env, &registry, &claude_registry, ambiguous)
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("ambiguous"));

        let mut grok_target = input();
        grok_target.to = Some("Shared [grok-123]".into());
        send_configured(&sender_env, &registry, &claude_registry, grok_target)
            .await
            .unwrap();
        let (mut stream, _) = tokio::time::timeout(Duration::from_secs(2), listeners[1].accept())
            .await
            .unwrap()
            .unwrap();
        let grok_message = xai_peer_socket::PeerMessage::decode(
            &xai_peer_socket::read_line(&mut stream).await.unwrap(),
        )
        .unwrap();
        assert_eq!(grok_message.text, "hello");
        assert!(
            tokio::time::timeout(Duration::from_millis(30), claude_listener.accept())
                .await
                .is_err()
        );

        let mut claude_target = input();
        claude_target.to = Some("Shared [claude-1]".into());
        send_configured(&sender_env, &registry, &claude_registry, claude_target)
            .await
            .unwrap();
        let (mut stream, _) =
            tokio::time::timeout(Duration::from_secs(2), claude_listener.accept())
                .await
                .unwrap()
                .unwrap();
        let claude_message = xai_peer_socket::PeerMessage::decode(
            &xai_peer_socket::read_line(&mut stream).await.unwrap(),
        )
        .unwrap();
        assert_eq!(claude_message.text, "hello");
        assert!(
            tokio::time::timeout(Duration::from_millis(30), listeners[1].accept())
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn send_peer_dead_sessions_are_hidden_reaped_and_cannot_be_targets() {
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let claude_registry = claude_registry(home.path());
        let sockets = home.path().join("socks");
        fs::create_dir(&sockets).unwrap();
        fs::set_permissions(&sockets, fs::Permissions::from_mode(0o700)).unwrap();
        let dead_socket = sockets.join("dead.sock");
        let (_guard, _listener) = xai_peer_socket::SocketGuard::bind(&dead_socket).unwrap();
        let registry = PeerRegistry::new(home.path());
        let registration = registry
            .register(xai_peer_socket::registry::PeerSessionRecord::new(
                "dead-session",
                "Dead session",
                u32::MAX,
                &dead_socket,
                "/dead",
            ))
            .unwrap();
        let record_path = registry.directory().join("dead-session.json");
        std::mem::forget(registration);

        assert!(registry.list(Some("caller-session")).unwrap().is_empty());
        assert!(!record_path.exists());

        let sender_env = HashMap::from([
            (
                "GROK_PEER_SOCKET".into(),
                sockets.join("caller.sock").display().to_string(),
            ),
            ("GROK_PEER_SESSION_ID".into(), "caller-session".into()),
        ]);
        let mut dead_target = input();
        dead_target.to = Some("Dead session".into());
        assert!(
            send_configured(&sender_env, &registry, &claude_registry, dead_target)
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn send_peer_exact_configured_destination_uses_proxy() {
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let sockets = home.path().join("socks");
        fs::create_dir(&sockets).unwrap();
        fs::set_permissions(&sockets, fs::Permissions::from_mode(0o700)).unwrap();
        let proxy_path = sockets.join("proxy.sock");
        let (_guard, listener) = xai_peer_socket::SocketGuard::bind(&proxy_path).unwrap();
        let registry = PeerRegistry::new(home.path());
        let claude_registry = claude_registry(home.path());
        let proxy_path_for_server = proxy_path.clone();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let message = xai_peer_socket::PeerMessage::decode(
                &xai_peer_socket::read_line(&mut stream).await.unwrap(),
            )
            .unwrap();
            xai_peer_socket::write_line(
                &mut stream,
                &xai_peer_socket::json_line(&serde_json::json!({
                    "type": "result",
                    "outcome": "locally_written"
                }))
                .unwrap(),
            )
            .await
            .unwrap();
            (message, proxy_path_for_server)
        });
        let mut env = env();
        env.insert(
            "GROK_PEER_DESTINATION".into(),
            proxy_path.display().to_string(),
        );
        let mut proxy_input = input();
        proxy_input.to = Some(proxy_path.display().to_string());
        send_configured(&env, &registry, &claude_registry, proxy_input)
            .await
            .unwrap();
        let (received, received_proxy) = server.await.unwrap();
        assert_eq!(received.text, "hello");
        assert_eq!(received.origin.reply_socket, "uds:/remote/proxy.sock");
        assert_eq!(received_proxy, proxy_path);
    }

    #[test]
    fn send_peer_class_comes_only_from_operator_env() {
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let registry = PeerRegistry::new(home.path());
        let claude_registry = claude_registry(home.path());
        for configured in [None, Some("bypass"), Some("prompting")] {
            let mut env = env();
            if let Some(value) = configured {
                env.insert("GROK_PEER_CLASS".into(), value.into());
            }
            let message = configured_message(&env, &registry, &claude_registry, input())
                .unwrap()
                .message;
            let expected = configured.unwrap_or("prompting");
            assert_eq!(message.class.as_str(), expected);
            let frame: serde_json::Value =
                serde_json::from_slice(&message.encode().unwrap()).unwrap();
            let content = frame["message"]["content"].as_str().unwrap();
            assert!(content.contains(&format!("from-mode=\"{expected}\"")));
            let other = if expected == "bypass" {
                "prompting"
            } else {
                "bypass"
            };
            assert!(!content.contains(&format!("from-mode=\"{other}\"")));
        }
        for value in ["default", "unknown", "", "Bypass", "bypass "] {
            let mut env = env();
            env.insert("GROK_PEER_CLASS".into(), value.into());
            assert!(configured_message(&env, &registry, &claude_registry, input()).is_err());
        }
    }

    #[test]
    fn send_peer_arguments_cannot_set_class() {
        for field in ["class", "from_mode", "GROK_PEER_CLASS"] {
            let args = serde_json::json!({"text":"hi", field:"bypass"});
            assert!(serde_json::from_value::<SendPeerMessageInput>(args).is_err());
        }
    }
}

//! Model-facing listing of live same-machine peer sessions.
use crate::types::tool::{ToolKind, ToolNamespace};
use crate::types::tool_metadata::ToolMetadata;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Default, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ListPeerSessionsInput {}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PeerSessionSummary {
    name: String,
    #[serde(rename = "ref")]
    reference: String,
    cwd: String,
}

#[derive(Debug, Default)]
pub struct ListPeerSessionsTool;

impl ToolMetadata for ListPeerSessionsTool {
    fn kind(&self) -> ToolKind {
        ToolKind::Other
    }

    fn is_read_only(&self) -> bool {
        true
    }

    fn tool_namespace(&self) -> ToolNamespace {
        ToolNamespace::GrokBuild
    }

    fn description_template(&self) -> &str {
        "List the other live Grok sessions on this machine. Each row includes a session name, its 8-character ref, and working directory. Use the name or `name [ref]` with send_peer_message. Socket paths are never returned."
    }
}

impl xai_tool_runtime::Tool for ListPeerSessionsTool {
    type Args = ListPeerSessionsInput;
    type Output = crate::types::output::TextOutput;

    fn id(&self) -> xai_tool_protocol::ToolId {
        xai_tool_protocol::ToolId::new("list_peer_sessions").expect("valid tool id")
    }

    fn description(
        &self,
        _: &xai_tool_runtime::ListToolsContext,
    ) -> xai_tool_types::ToolDescription {
        xai_tool_types::ToolDescription::new(
            "list_peer_sessions",
            ToolMetadata::sanitized_description_template(self),
        )
    }

    fn capabilities(&self) -> xai_tool_protocol::ToolCapabilities {
        xai_tool_protocol::ToolCapabilities {
            is_read_only: true,
            ..Default::default()
        }
    }

    async fn run(
        &self,
        ctx: xai_tool_runtime::ToolCallContext,
        _: Self::Args,
    ) -> Result<Self::Output, xai_tool_runtime::ToolError> {
        let resources = crate::types::tool_metadata::shared_resources(&ctx)?;
        let current_session_id = {
            let resources = resources.lock().await;
            let env = resources
                .require::<crate::types::resources::SessionEnv>()?
                .0
                .clone();
            env.get("GROK_PEER_SESSION_ID").cloned().ok_or_else(|| {
                xai_tool_runtime::ToolError::invalid_arguments(
                    "Current peer session identity is unavailable",
                )
            })?
        };
        render_peer_sessions(
            &xai_peer_socket::registry::PeerRegistry::new(crate::util::grok_home()),
            &current_session_id,
        )
    }
}

fn render_peer_sessions(
    registry: &xai_peer_socket::registry::PeerRegistry,
    current_session_id: &str,
) -> Result<crate::types::output::TextOutput, xai_tool_runtime::ToolError> {
    let sessions = registry
        .list(Some(current_session_id))
        .map_err(|_| {
            xai_tool_runtime::ToolError::execution(
                xai_tool_protocol::ToolId::new("list_peer_sessions").expect("valid tool id"),
                "Peer session registry could not be read",
            )
        })?
        .into_iter()
        .map(|session| {
            let reference = session.reference();
            PeerSessionSummary {
                name: session.name,
                reference,
                cwd: session.cwd,
            }
        })
        .collect::<Vec<_>>();
    serde_json::to_string(&serde_json::json!({ "sessions": sessions }))
        .map(crate::types::output::TextOutput::from)
        .map_err(|_| {
            xai_tool_runtime::ToolError::execution(
                xai_tool_protocol::ToolId::new("list_peer_sessions").expect("valid tool id"),
                "Peer session listing could not be encoded",
            )
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::PermissionsExt};

    #[test]
    fn default_registry_registers_peer_listing_without_an_operator_proxy() {
        let builder = crate::registry::types::ToolRegistryBuilder::new();
        assert!(builder.has_tool_id("GrokBuild:list_peer_sessions"));
        assert!(builder.has_tool_id("GrokBuild:send_peer_message"));
    }

    #[tokio::test]
    async fn listing_shows_other_names_refs_and_cwds_without_socket_paths() {
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let sockets = home.path().join("socks");
        fs::create_dir(&sockets).unwrap();
        fs::set_permissions(&sockets, fs::Permissions::from_mode(0o700)).unwrap();
        let caller_path = sockets.join("caller.sock");
        let peer_path = sockets.join("peer.sock");
        let (_caller_guard, _caller_listener) =
            xai_peer_socket::SocketGuard::bind(&caller_path).unwrap();
        let (_peer_guard, _peer_listener) = xai_peer_socket::SocketGuard::bind(&peer_path).unwrap();
        let registry = xai_peer_socket::registry::PeerRegistry::new(home.path());
        let _caller = registry
            .register(xai_peer_socket::registry::PeerSessionRecord::new(
                "caller-session",
                "Caller",
                std::process::id(),
                &caller_path,
                "/caller",
            ))
            .unwrap();
        let _peer = registry
            .register(xai_peer_socket::registry::PeerSessionRecord::new(
                "peer-1234-session",
                "Build session",
                std::process::id(),
                &peer_path,
                "/work/tree",
            ))
            .unwrap();

        let output = render_peer_sessions(&registry, "caller-session").unwrap();
        let value: serde_json::Value = serde_json::from_str(&output.text).unwrap();
        assert_eq!(value["sessions"].as_array().unwrap().len(), 1);
        assert_eq!(value["sessions"][0]["name"], "Build session");
        assert_eq!(value["sessions"][0]["ref"], "peer-123");
        assert_eq!(value["sessions"][0]["cwd"], "/work/tree");
        assert!(value["sessions"][0].get("socket").is_none());
        assert!(!output.text.contains(peer_path.to_string_lossy().as_ref()));
    }
}

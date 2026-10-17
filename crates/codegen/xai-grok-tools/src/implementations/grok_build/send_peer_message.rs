//! Peer writes are policy-controlled and restricted to operator-configured destinations.
use crate::types::tool::{ToolKind, ToolNamespace};

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct SendPeerMessageInput {
    /// Text sent as untrusted peer data.
    pub text: String,
    /// Optional exact configured proxy path; omitted selects this session's configured peer.
    pub to: Option<String>,
    /// Optional exact configured reply socket. Cannot override session identity.
    pub reply_socket: Option<String>,
}
#[derive(Debug, Default)]
pub struct SendPeerMessageTool;

fn configured_message(
    env: &std::collections::HashMap<String, String>,
    input: SendPeerMessageInput,
) -> Result<(&str, xai_peer_socket::PeerMessage), xai_tool_runtime::ToolError> {
    let invalid = xai_tool_runtime::ToolError::invalid_arguments;
    let destination = env
        .get("GROK_PEER_DESTINATION")
        .ok_or_else(|| invalid("No peer proxy configured for this session"))?;
    let reply = env
        .get("GROK_PEER_REPLY_SOCKET")
        .or_else(|| env.get("GROK_PEER_SOCKET"))
        .ok_or_else(|| invalid("No session reply socket"))?;
    if input.to.as_ref().is_some_and(|s| s != destination)
        || input.reply_socket.as_ref().is_some_and(|s| s != reply)
    {
        return Err(invalid(
            "Peer destination and reply identity must match session configuration",
        ));
    }
    Ok((
        destination,
        xai_peer_socket::PeerMessage::new(input.text, "grok".into(), format!("uds:{reply}")),
    ))
}
impl crate::types::tool_metadata::ToolMetadata for SendPeerMessageTool {
    fn kind(&self) -> ToolKind {
        ToolKind::Write
    }
    fn tool_namespace(&self) -> ToolNamespace {
        ToolNamespace::GrokBuild
    }
    fn description_template(&self) -> &str {
        "Send untrusted text to this session's operator-configured peer proxy. No arbitrary socket destinations. A transport result does not establish transcript visibility or model processing; never automatically retry an ambiguous write."
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
            let res = resources.lock().await;
            res.require::<crate::types::resources::SessionEnv>()?
                .0
                .clone()
        };
        let invalid = xai_tool_runtime::ToolError::invalid_arguments;
        let (destination, message) = configured_message(&env, input)?;
        xai_peer_socket::bridge::send_proxy(std::path::Path::new(destination), &message)
            .await
            .map(|s| crate::types::output::ToolOutput::Text(s.into()))
            .map_err(|_| {
                invalid("Peer transport failed or delivery unknown; do not automatically retry")
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn env() -> std::collections::HashMap<String, String> {
        [
            ("GROK_PEER_DESTINATION", "/local/proxy.sock"),
            ("GROK_PEER_SOCKET", "/local/session.sock"),
            ("GROK_PEER_REPLY_SOCKET", "/remote/proxy.sock"),
        ]
        .into_iter()
        .map(|(k, v)| (k.into(), v.into()))
        .collect()
    }
    fn input() -> SendPeerMessageInput {
        SendPeerMessageInput {
            text: "hello".into(),
            to: None,
            reply_socket: None,
        }
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
    fn peer_send_uses_operator_route_and_identity_only() {
        let env = env();
        let (destination, message) = configured_message(&env, input()).unwrap();
        assert_eq!(destination, "/local/proxy.sock");
        assert_eq!(message.origin.sender, "grok");
        assert_eq!(message.origin.reply_socket, "uds:/remote/proxy.sock");
        let mut evil = input();
        evil.to = Some("/arbitrary.sock".into());
        assert!(configured_message(&env, evil).is_err());
        let mut evil = input();
        evil.reply_socket = Some("/impersonate.sock".into());
        assert!(configured_message(&env, evil).is_err());
        assert!(configured_message(&Default::default(), input()).is_err());
        assert!(
            serde_json::from_value::<SendPeerMessageInput>(
                serde_json::json!({"text":"hi", "from_name":"Daniel"})
            )
            .is_err()
        );
    }
}

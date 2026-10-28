use super::*;

#[test]
fn profile_execution_display_uses_actual_attempt_over_legacy_model_default() {
    let mut app = make_app_with_agent("sess-parent");
    let mut update = test_subagent_spawned("sess-parent", "child-profile");
    let XaiSessionUpdate::SubagentSpawned {
        execution, model, ..
    } = &mut update
    else {
        unreachable!();
    };
    let actual = xai_tool_types::SubagentExecution {
        profile: "cfo".into(),
        provider: "api.x.ai".into(),
        model: "grok-4.7-build-fast".into(),
        reasoning_effort: Some("low".into()),
        speed: "fast".into(),
        fallback: None,
    };
    *model = Some("stale-profile-default".into());
    *execution = Some(actual.clone());
    assert!(handle_ext_notification(
        &subagent_notification_with_seq("sess-parent", update, 1),
        &mut app,
    ));
    let parent = test_agent(&app, AgentId(0));
    let child = parent.subagent_sessions.get("child-profile").unwrap();
    assert_eq!(
        child.attempt.model.as_deref(),
        Some(actual.display().as_str())
    );
    let row = parent
        .scrollback
        .get_by_id(child.attempt.scrollback_entry_id.unwrap())
        .unwrap();
    let RenderBlock::Subagent(block) = &row.block else {
        panic!("Expected the spawned child row");
    };
    assert_eq!(block.model.as_deref(), Some(actual.display().as_str()));
    assert!(
        !block
            .model
            .as_ref()
            .unwrap()
            .contains("stale-profile-default")
    );
}

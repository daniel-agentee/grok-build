use xai_grok_sampling_types::{SearchDateBound, ToolOverrides, WebSearchOptions, XSearchOptions};

use super::{
    CLASSIFIER_REQUEST_TOKEN_RESERVE, LengthSalvageAction, LengthSalvageStreak,
    MAX_OUTPUT_TOKEN_LIMIT_RETRIES, classifier_request_fits_context, resolve_configured_cutoff,
};

#[tokio::test(flavor = "current_thread")]
async fn resident_task_schema_follows_profile_cas_without_changing_child_snapshot() {
    use crate::session::acp_session::support::create_test_actor;
    use serde_json::json;
    use std::sync::Arc;

    tokio::task::LocalSet::new().run_until(async {
        let dir = tempfile::tempdir().unwrap();
        git2::Repository::init(dir.path()).unwrap();
        let legacy = dir.path().join(".grok/agents");
        let workspace = dir.path().join("workspace");
        std::fs::create_dir_all(&legacy).unwrap();
        std::fs::create_dir_all(&workspace).unwrap();
        std::fs::write(legacy.join("CFO.md"), "---\nname: CFO\ndescription: Legacy finance\nmodel: inherit\n---\nLegacy role.\n").unwrap();
        let (gateway, _receiver) = tokio::sync::mpsc::unbounded_channel();
        let (persistence, _persist_receiver) = tokio::sync::mpsc::unbounded_channel();
        let mut actor = create_test_actor(0, 256_000, 85, gateway, persistence).await;
        actor.session_info.cwd = workspace.to_string_lossy().into_owned();
        let mut spec = crate::session::agent_rebuild::test_rebuild_spec_default();
        let fields = Arc::get_mut(&mut spec).unwrap();
        fields.working_directory = workspace.clone();
        fields.bridge_state_path = dir.path().join("state.json");
        fields.subagents_enabled = true;
        let mut definition = xai_grok_agent::AgentDefinition::default_grok_build();
        definition.agents_md = false;
        definition.discover_skills = false;
        *actor.agent.borrow_mut() = spec.build_agent(definition, xai_grok_agent::DEFAULT_SYSTEM_PROMPT_LABEL).await.unwrap();
        actor.rebuild_spec = spec.clone();
        fn route(definitions: &[crate::sampling::types::ToolDefinition]) -> &serde_json::Value {
            &definitions.iter().find(|d| d.function.name == "spawn_subagent").unwrap().function.parameters["properties"]["subagent_type"]
        }
        let before = actor.prepare_tool_definitions_inner().await;
        assert!(route(&before)["enum"].as_array().unwrap().iter().any(|v| v == "CFO"));
        assert!(!route(&before)["enum"].as_array().unwrap().iter().any(|v| v == "cfo"));
        let applied = crate::profiles::apply_profile_control_for_test(&workspace, json!({"id":"cfo","expectedRevision":null,"writeScope":"project","definition":{"description":"Imported finance","model":"inherit","effort":"medium","promptBody":"Imported CFO role."}})).unwrap();
        let after = actor.prepare_tool_definitions_inner().await;
        let names = route(&after)["enum"].as_array().unwrap();
        assert!(names.iter().any(|v| v == "cfo"), "resident parent must offer the newly applied lowercase cfo: {names:?}");
        assert!(names.iter().any(|v| v == "CFO"), "legacy uppercase CFO remains a distinct profile ID");
        actor.forked_tool_override = Some(Vec::new());
        assert_eq!(serde_json::to_value(&before).unwrap(), serde_json::to_value(actor.prepare_tool_definitions_inner().await).unwrap(), "verbatim fork schemas stay frozen even after new profile admission");
        actor.forked_tool_override = None;
        let child_definition = xai_grok_agent::AgentDefinition::from_file(&workspace.join(".grok/agents/cfo.md")).unwrap();
        let child = spec.build_agent(child_definition, xai_grok_agent::DEFAULT_SYSTEM_PROMPT_LABEL).await.unwrap();
        let child_snapshot = child.definition().to_json_value();
        let mut edit = json!({"id":"cfo","expectedRevision":"stale","writeScope":"project","definition":{"description":"Updated finance","effort":"high"}});
        assert!(crate::profiles::apply_profile_control_for_test(&workspace, edit.clone()).is_err());
        assert_eq!(serde_json::to_value(&after).unwrap(), serde_json::to_value(actor.prepare_tool_definitions_inner().await).unwrap(), "failed CAS cannot change the advertised routes");
        edit["expectedRevision"] = applied["revision"].clone();
        crate::profiles::apply_profile_control_for_test(&workspace, edit).unwrap();
        let updated = actor.prepare_tool_definitions_inner().await;
        assert!(route(&updated)["description"].as_str().unwrap().contains("Updated finance"));
        assert_eq!(child_snapshot, child.definition().to_json_value(), "already-built children retain their admitted profile snapshot");
        assert_eq!(serde_json::to_value(&before).unwrap(), serde_json::to_value(actor.agent.borrow().tool_bridge().toolset().tool_definitions_builtins_only()).unwrap(), "per-turn projection must not mutate frozen bridge schemas");
        std::fs::remove_file(workspace.join(".grok/agents/cfo.md")).unwrap();
        let removed = actor.prepare_tool_definitions_inner().await;
        assert!(!route(&removed)["enum"].as_array().unwrap().iter().any(|v| v == "cfo"));
    }).await;
}

fn x_cut(to: &str) -> XSearchOptions {
    XSearchOptions {
        date_bound: Some(SearchDateBound::new(None, Some(to.into())).unwrap()),
    }
}

/// When every sample ends with finish reason Length while tools are active, the turn salvages up to the cap and then fails.
/// The reminder is injected only on the first salvage.
#[test]
fn length_salvage_streak_proceeds_to_the_cap_then_exhausts() {
    let mut streak = LengthSalvageStreak::default();
    for n in 1..=MAX_OUTPUT_TOKEN_LIMIT_RETRIES {
        match streak.on_sample(true) {
            LengthSalvageAction::Proceed { inject_reminder } => {
                assert_eq!(
                    inject_reminder,
                    n == 1,
                    "reminder only on the first salvage"
                );
            }
            other => panic!("salvage {n} within the cap must proceed, got {other:?}"),
        }
    }
    assert!(matches!(
        streak.on_sample(true),
        LengthSalvageAction::Exhausted
    ));
}

/// A non-Length (or tool-less) sample resets the streak: a later salvage starts a fresh streak and re-injects the reminder.
#[test]
fn length_salvage_streak_resets_on_non_length_sample() {
    let mut streak = LengthSalvageStreak::default();
    for _ in 0..MAX_OUTPUT_TOKEN_LIMIT_RETRIES {
        let _ = streak.on_sample(true);
    }
    assert!(matches!(
        streak.on_sample(false),
        LengthSalvageAction::NotSalvage
    ));
    assert!(matches!(
        streak.on_sample(true),
        LengthSalvageAction::Proceed {
            inject_reminder: true
        }
    ));
}

#[test]
fn classifier_request_bound_enforces_its_reserve_with_saturating_arithmetic() {
    let window = 12_000 + CLASSIFIER_REQUEST_TOKEN_RESERVE;
    for (input, context_window, expected) in [
        (12_000, window, true),
        (12_001, window, false),
        (u64::MAX, u64::MAX, false),
    ] {
        assert_eq!(
            classifier_request_fits_context(input, context_window),
            expected
        );
    }
}

#[test]
fn seed_cutoff_is_inherited_without_a_per_turn_update() {
    let seed = ToolOverrides {
        x_search: Some(x_cut("2020-01-01")),
        web_search: None,
    };
    assert_eq!(resolve_configured_cutoff(Some(seed.clone()), None), seed);
}

#[test]
fn non_empty_base_cutoff_wins_per_tool_and_an_empty_one_reverts_to_the_seed() {
    let seed = ToolOverrides {
        x_search: Some(x_cut("2020-01-01")),
        web_search: Some(WebSearchOptions {
            allowed_domains: Some(vec!["x.com".into()]),
            excluded_domains: None,
        }),
    };
    let base = ToolOverrides {
        x_search: Some(x_cut("2019-06-01")),
        web_search: Some(WebSearchOptions {
            allowed_domains: Some(vec![]),
            excluded_domains: None,
        }),
    };
    let got = resolve_configured_cutoff(Some(seed.clone()), Some(&base));
    assert_eq!(got.x_search, Some(x_cut("2019-06-01")));
    assert_eq!(got.web_search, seed.web_search);
}

#[test]
fn inherited_cutoff_agrees_with_the_wire_echo_so_the_two_implementations_cannot_drift() {
    use xai_grok_sampling_types::{HostedTool, apply_tool_overrides};
    let web = WebSearchOptions {
        allowed_domains: Some(vec!["x.com".into()]),
        excluded_domains: None,
    };
    let cases = [
        (
            Some(ToolOverrides {
                x_search: Some(x_cut("2020-01-01")),
                web_search: None,
            }),
            None,
        ),
        (
            Some(ToolOverrides {
                x_search: Some(x_cut("2020-01-01")),
                web_search: Some(web.clone()),
            }),
            Some(ToolOverrides {
                x_search: Some(x_cut("2019-06-01")),
                web_search: None,
            }),
        ),
        (
            None,
            Some(ToolOverrides {
                x_search: Some(x_cut("2018-01-01")),
                web_search: Some(web.clone()),
            }),
        ),
    ];
    for (seed, base) in cases {
        let mut tools = vec![
            HostedTool::WebSearch { options: None },
            HostedTool::XSearch { options: None },
        ];
        apply_tool_overrides(&mut tools, seed.as_ref());
        let wire_echo = apply_tool_overrides(&mut tools, base.as_ref());
        let inherited = resolve_configured_cutoff(seed.clone(), base.as_ref());
        assert_eq!(wire_echo, inherited, "seed={seed:?} base={base:?}");
    }
}

#[cfg(test)]
mod subagent_sampling_gate_tests {
    use super::super::super::support::create_test_actor;
    use super::super::acquire_subagent_sampling_permit;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;
    use tokio::sync::Semaphore;

    #[derive(Default)]
    struct ConcurrencyProbe {
        in_flight: AtomicUsize,
        max_in_flight: AtomicUsize,
    }

    impl ConcurrencyProbe {
        fn enter(&self) {
            let now = self.in_flight.fetch_add(1, Ordering::SeqCst) + 1;
            self.max_in_flight.fetch_max(now, Ordering::SeqCst);
        }
        fn leave(&self) {
            self.in_flight.fetch_sub(1, Ordering::SeqCst);
        }
    }

    #[tokio::test]
    async fn subagent_submits_never_exceed_cap_and_excess_queues() {
        const CAP: usize = 3;
        const TURNS: usize = 12;
        let semaphore = Arc::new(Semaphore::new(CAP));
        let probe = Arc::new(ConcurrencyProbe::default());
        let ran = Arc::new(AtomicUsize::new(0));

        let mut handles = Vec::new();
        for _ in 0..TURNS {
            let gate = Some(semaphore.clone());
            let probe = probe.clone();
            let ran = ran.clone();
            handles.push(tokio::spawn(async move {
                let permit = acquire_subagent_sampling_permit(&gate).await;
                assert!(permit.is_some(), "a subagent turn must receive a permit");
                probe.enter();
                tokio::time::sleep(Duration::from_millis(20)).await;
                probe.leave();
                ran.fetch_add(1, Ordering::SeqCst);
            }));
        }
        for h in handles {
            h.await.unwrap();
        }

        assert_eq!(
            ran.load(Ordering::SeqCst),
            TURNS,
            "every queued turn ran (queued, not errored)"
        );
        assert!(
            probe.max_in_flight.load(Ordering::SeqCst) <= CAP,
            "in-flight subagent submits exceeded the cap: {} > {CAP}",
            probe.max_in_flight.load(Ordering::SeqCst),
        );
    }

    #[tokio::test]
    async fn cancelled_waiter_releases_without_deadlock() {
        let semaphore = Arc::new(Semaphore::new(1));
        let gate = Some(semaphore.clone());
        let held = acquire_subagent_sampling_permit(&gate).await;
        assert!(held.is_some());

        let waiter = tokio::spawn({
            let gate = gate.clone();
            async move {
                let _permit = acquire_subagent_sampling_permit(&gate).await;
                std::future::pending::<()>().await;
            }
        });
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert_eq!(
            semaphore.available_permits(),
            0,
            "slot stays held while the second turn queues"
        );
        waiter.abort();
        let _ = waiter.await;

        drop(held);
        let next = tokio::time::timeout(
            Duration::from_millis(200),
            acquire_subagent_sampling_permit(&gate),
        )
        .await
        .expect("a permit must be free once the held one is released");
        assert!(next.is_some(), "the cancelled waiter did not leak the slot");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn submit_holds_permit_for_subagent_not_main() {
        let local = tokio::task::LocalSet::new();
        local
            .run_until(async {
                let saturated = Arc::new(Semaphore::new(1));
                let _held = saturated.clone().acquire_owned().await.unwrap();

                let (gw_tx, _gw_rx) = tokio::sync::mpsc::unbounded_channel();
                let (p_tx, _p_rx) = tokio::sync::mpsc::unbounded_channel();
                let mut subagent = create_test_actor(0, 200_000, 80, gw_tx, p_tx).await;
                subagent.sampling_gate = Some(saturated.clone());
                let subagent = Arc::new(subagent);

                let queued = tokio::time::timeout(
                    Duration::from_millis(150),
                    subagent.submit_turn_request(Default::default()),
                )
                .await;
                assert!(
                    queued.is_err(),
                    "a subagent submit must queue behind the drained gate, never reaching the sampler"
                );

                let (gw_tx, _gw_rx) = tokio::sync::mpsc::unbounded_channel();
                let (p_tx, _p_rx) = tokio::sync::mpsc::unbounded_channel();
                let main = create_test_actor(0, 200_000, 80, gw_tx, p_tx).await;
                assert!(main.sampling_gate.is_none());
                let main = Arc::new(main);

                let ran = tokio::time::timeout(
                    Duration::from_millis(150),
                    main.submit_turn_request(Default::default()),
                )
                .await;
                assert!(
                    ran.is_ok(),
                    "the main session must reach the sampler even while the gate is drained"
                );
                assert!(
                    main.turn_stream_drained.lock().is_empty(),
                    "a result with no queued terminal event must release request ownership before returning"
                );
            })
            .await;
    }
}

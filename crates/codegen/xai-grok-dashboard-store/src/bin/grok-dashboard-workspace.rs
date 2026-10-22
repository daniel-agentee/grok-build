//! Read-only, bounded dashboard membership export for the authenticated mobile bridge.
use std::path::PathBuf;

use xai_grok_dashboard_store::{Member, MemberKind, WORKSPACE_CAPACITY, WorkspaceStore};

fn visible_unloaded_member(member: &Member) -> bool {
    let has_text = |text: Option<&str>| text.is_some_and(|s| !s.trim().is_empty());
    matches!(member.kind, MemberKind::Build)
        && (member.pin_rank.is_some()
            || has_text(member.title.as_deref())
            || has_text(member.last_turn_summary.as_deref()))
}

fn main() {
    if run().is_err() {
        // Database failures can contain private paths; the bridge reports a stable error code.
        eprintln!("Dashboard membership is unavailable");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args_os().skip(1);
    let db = PathBuf::from(args.next().ok_or("Expected workspace database path")?);
    if args.next().is_some() || !db.is_absolute() {
        return Err("Expected one absolute workspace database path".into());
    }
    let snapshot = WorkspaceStore::read_existing_snapshot(&db)?;
    let members = snapshot
        .iter()
        .flat_map(|snapshot| &snapshot.members)
        .filter(|member| visible_unloaded_member(member))
        .filter_map(|member| {
            // A membership marker can only join a native roster row with its
            // absolute execution workspace. Never substitute the bridge cwd.
            let cwd = member
                .cwd
                .as_deref()
                .filter(|cwd| std::path::Path::new(cwd).is_absolute())?;
            Some(serde_json::json!({ "sessionId": member.session_id.as_ref(), "cwd": cwd }))
        })
        .collect::<Vec<_>>();
    if members.len() > WORKSPACE_CAPACITY {
        return Err("Workspace membership exceeds its capacity".into());
    }
    println!(
        "{}",
        serde_json::json!({ "schemaVersion": 1, "members": members })
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use xai_grok_dashboard_store::{MemberOrigin, SessionId};

    fn member() -> Member {
        Member {
            session_id: SessionId::new("fixture").unwrap(),
            kind: MemberKind::Build,
            origin: MemberOrigin::Local,
            cwd: Some("/work/project".into()),
            title: None,
            model: None,
            last_turn_summary: None,
            is_worktree: false,
            last_change_unix_ms: 1,
            pin_rank: None,
            order_rank: None,
        }
    }

    #[test]
    fn unloaded_visibility_matches_terminal_content_and_pin_rules() {
        let mut row = member();
        assert!(!visible_unloaded_member(&row));
        row.title = Some(" \n ".into());
        row.last_turn_summary = Some("\t".into());
        row.model = Some("model alone is a startup placeholder".into());
        assert!(!visible_unloaded_member(&row));
        row.title = Some("Open task".into());
        assert!(visible_unloaded_member(&row));
        row.title = None;
        row.last_turn_summary = Some("A previous turn".into());
        assert!(visible_unloaded_member(&row));
        row.last_turn_summary = None;
        row.pin_rank = Some(0);
        assert!(visible_unloaded_member(&row));
    }

    #[test]
    fn non_build_members_are_never_mobile_build_conversations() {
        let mut row = member();
        row.title = Some("Non Build task".into());
        row.pin_rank = Some(0);
        row.kind = MemberKind::Conversation;
        assert!(!visible_unloaded_member(&row));
    }
}

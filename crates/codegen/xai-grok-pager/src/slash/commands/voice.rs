//! `/voice` toggles dictation: it starts recording now, and Esc or Enter stops (Enter also sends). Nothing is written to `config.toml`.
//!
//! The keyboard chord is **Space** (hold-to-talk primary on Kitty when the
//! prompt is empty or scrollback is focused), **Ctrl+Space**, or **F8** (F8 is
//! a fallback where Ctrl+Space is taken — e.g. macOS input switching). Behavior
//! follows `[ui].voice_capture_mode`: `toggle` (press starts, press again stops
//! — like `/voice`) or `hold`-to-talk (hold to record, release to stop), with
//! `hold` available only on terminals that report key releases (Kitty protocol)
//! and falling back to toggle elsewhere. The recording banner is the only
//! feedback; no toast.
//!
//! Dictation works on the agent screen (into the prompt) and on the dashboard (into the dispatch / new-agent input).
//!
//! **Scope.** Voice mode stays on for the rest of the process; re-open `grok` for a clean slate.

use crate::app::actions::Action;
use crate::slash::command::{CommandExecCtx, CommandResult, SlashCommand, slash_meta};

pub struct VoiceCommand;

impl SlashCommand for VoiceCommand {
    slash_meta! {
        name: "voice",
        usage: "/voice",
        // Dictation targets a prompt box: the agent prompt in a live session, or the dashboard's dispatch (new-agent) input.
        // It is session-scoped (no effect on the welcome screen) but still offered on the dashboard.
        session_scoped: true,
        offered_when_session_less: true,
    }

    fn description(&self) -> &str {
        // Chord is Space (hold+releases) / Ctrl+Space / F8. Without key
        // releases hold-to-talk is impossible, so it's always toggle — say so.
        // With them it's configurable (toggle or hold via `voice_capture_mode`),
        // so leave the behavior unspecified.
        if crate::app::kitty_releases_reported() {
            "Dictation (Space/Ctrl+Space/F8; Esc/Enter to stop)"
        } else {
            "Toggle dictation (Ctrl+Space/F8; Esc/Enter to stop)"
        }
    }

    fn run(&self, _ctx: &mut CommandExecCtx, _args: &str) -> CommandResult {
        // Toggle, mirroring the voice key: starts dictation, or stops it if already recording (Esc/Enter also stop)
        CommandResult::Action(Action::VoiceToggle)
    }
}

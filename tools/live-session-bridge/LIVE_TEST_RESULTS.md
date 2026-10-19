# Live switching verification — 18 October 2026

**Historical stock-runtime acceptance record.** The original retained processes
listed below exited before the edge-case follow-up. See `EDGE_CASE_RESULTS.md`
for the fresh runtime, failure testing, and the explicit-close race discovered
in stock 1.0.62. Native initialize's `agentInstanceId` identifies the server /
MvpAgent instance; it is not an independent resident-actor identity check.

This record distinguishes observed interoperability from source-level or stub coverage. Real manual approval routing passed in a separately created trusted Ask-policy actor. The original session inherited an always-approve policy and its automatic executions are **not** approval-test passes.

## Exact environment

- macOS Apple Silicon; Xcode 26.6 (17F113); iPhone 17 Simulator, iOS 26.5.
- Official Grok source: `2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8`, Apache-2.0, source package version 1.0.45, `SOURCE_REV=559751fdcec02d413e4c57c8832ab275e4f44980`.
- Actual runtime: stock `~/.grok/bin/grok-1.0.62`, version `grok 1.0.62 (1474f9cbdd91)`. Native leader registration reported protocol 1 and binary 1.0.62. Cached OAuth authentication succeeded; no credentials were printed.
- iOS source: `46fc0200dfe5c92386d0941e29ad9b463120081e`, Apache-2.0. Original `UPSTREAM_PIN` was `98c3b2438aa922fbbe6178a5c0a4c48f85edc8ce`; runtime compatibility is pinned explicitly by the implementation.
- Fresh isolated clones and branch `codex/live-session-switching`; existing dirty `~/code/grok-build` and old `grok-build-remote` checkout were preserved.
- Disposable workspace: `/Users/danielstojanovic/code/_tools/grok-live-switching/disposable-project`. For manual approval tests only, its project-local `.claude/settings.local.json` sets `permissions.defaultMode` to `default`, overriding the inherited global `dontAsk` mode. The disposable project was then explicitly reviewed/trusted with the native `--trust` flow before creating the approval actor, because untrusted project-local settings are not loaded. Existing permission allow/deny rules and hooks remain in effect; user global permission settings were not edited. Native trust flow was used for the disposable path.

The personal `~/.grok/bin/grok` wrapper defaults to a different custom binary. Starting a stock TUI alone can still cause that wrapper to spawn a different leader. The acceptance run explicitly prestarted the stock 1.0.62 leader and attached the stock TUI to its socket.

## Shared live actor

Original session: `01a15035-cd29-7df3-a035-1a461eda40d0`.
Agent instance: `a1fa4de1-bcfe-4dbe-9666-09fc174fd871`.
Leader socket: `~/.grok/leader-live-switching-stock.sock`.
The Terminal, bridge clients, and Simulator selected this resident session, used the same absolute workspace, and reported the same agent instance. The bridge never started an agent or restored a separate conversation.

## Observed passes

| Test | Evidence and observation |
|---|---|
| Terminal start, mobile attach and history | Native/WS discovery returned the resident session and absolute cwd. Attach replay included the initial `STOCK-TERMINAL-ATTACH-1810` conversation. |
| Real Simulator → Terminal | Simulator sent `SIMULATOR-PHONE-2016`; Terminal displayed it and `SIMULATOR-ACK-2016` with the exact shared cwd. |
| Terminal → real Simulator | Terminal sent `TERMINAL-TO-SIMULATOR-2017`; Simulator displayed the instruction and response referencing the earlier phone acknowledgement and same cwd. Sanitized transcript: `evidence/simulator-bidirectional-transcript.json`. |
| Same workspace and controlled edits | Shell execution created disposable files including `shared-switch.txt` and `background-result.txt` in the selected cwd. Their exact expected contents were checked. Automatic permission handling in this original session means these are workspace checks only. |
| Attach during running task | A WS client attached while the real shell task for `BACKGROUND-RECOVERY-2019` was running and received its live running-tool state. Terminal continued to completion. |
| Phone background does not cancel work | Simulator was backgrounded with Home during the 30-second shell task. Terminal continued; `background-result.txt` contained exactly `BACKGROUND-DONE-2019\n`. Foreground recovery was exercised in the actual Simulator; the recovered conversation and completion were visible. Screenshots in the iOS checkout: `evidence/simulator-background-home.png` and `evidence/simulator-background-recovered.png`. |
| Background during a streamed reply; authoritative replay | In the actual Simulator, `MIDSTREAM-RECOVERY-2130` displayed new `RECOVERY3-LINE-001` and an active response before Home was pressed. The same native actor continued for 31.1 seconds and produced all 300 numbered lines plus `MIDSTREAM-COMPLETE-2130` exactly once. On foreground, the app rebuilt one assistant response containing all 300 markers and one completion marker, with no duplicated partial response. The UI showed idle, accepted an unsent draft, and process CPU returned to 0.4%. See `evidence/stream3-live-result.json`; iOS screenshots: `evidence/simulator-stream3-partial.png`, `evidence/simulator-stream3-background.png`, `evidence/simulator-stream3-recovered.png`. |
| Network disconnect, continuation, cursor replay | WS client disconnected after observing a running tool for `RECONNECT-CONTINUES-2028`. The shell completed `reconnect-result.txt` with `RECONNECT-DONE-2028\n` while disconnected. Reattach with cursor `...-740` kept the same agent instance, received completion and tool results, received only higher event IDs, and had zero duplicate event IDs. See `evidence/reconnect-live-result.json`. |
| Near-simultaneous real TUI + WS submission | Terminal PTY and phone WS submission were released through parallel writes. Both `SIMULTANEOUS-...-2044` prompts appeared once, and both distinct ACKs appeared once. Native queue notification showed Terminal waiting behind the phone turn, then becoming the running prompt. See `evidence/simultaneous-live-result.json`. |
| Native live plan and completion | The real model used TodoWrite twice in the original live actor. WS received ACP `plan` events `...-988` (one in_progress/one pending) and `...-997` (both completed), then `PLAN-COMPLETE-2059` and `end_turn`. See `evidence/plan-live-result.json`. |
| Real approval from Simulator | Terminal submitted a controlled append in Ask-policy session `01a15048-205a-71e1-bd32-cdd84834d39a`. Simulator displayed the real permission sheet; its Yes/proceed action resolved the native request. Terminal cleared the pending modal and executed once. `approval-phone-once.txt` contains exactly one `PHONE-APPROVAL-ONCE` line. |
| Real approval from Terminal | Simulator UI submitted a second controlled append; actual interactive TUI selected allow-once. `approval-terminal-once.txt` contains exactly one `TERMINAL-APPROVAL-ONCE` line. Native pending/resolved events were captured. The Simulator sheet cleared immediately after the Terminal decision; exit code 0 and completed response were visible. Screenshots: `simulator-terminal-approval-pending.png` and `simulator-terminal-approval-cleared.png` in the iOS evidence directory. See `evidence/approval-file-verification.json` and `evidence/approval-live-result.json`. |
| Wrong secret | A live WS upgrade with the wrong bearer secret was rejected with HTTP 401. |
| Terminal disconnect/resume | Original TUI was closed, then another stock TUI attached to the same resident session while mobile access remained available. |

## Remaining coverage and qualifications

- The long-stream stress test initially exposed a real foreground rendering freeze. Native continuation passed, but `LazyVStack` height estimation plus animated scrolling kept SwiftUI busy during large replay. The iOS change uses a measured `VStack`, nonanimated scrolling, and coalesced UI publication. The earlier `stream-live-result.json` and `stream2-live-result.json` prove native continuation only; the final `stream3-live-result.json` and actual Simulator capture above verify the corrected UI behavior.

- Manual approval proof uses the fresh trusted Ask actor on `~/.grok/leader-mobile-ask.sock`, port 18792. The original resident actor was created under the user's global `always-approve` policy (`yolo:true` on the real roster). Reattaching a TUI with Ask does not downgrade that existing actor. Its earlier instant pending/resolved events and automatic file writes are excluded from approval coverage.
- Physical iPhone: signed app installed and launched; Local Network permission is a required device action. Physical bidirectional/approval tests are not yet claimed.
- Interactive CLI testing used the real stock TUI in PTYs. A separate attempt to open a ready Terminal.app window was cancelled after the available UI tool explicitly rejected access to Terminal.app. No Terminal.app window automation is claimed; the exact command to attach manually is below.
- Native cursor limitation characterized by an additional live run: cursor `...-930`, a transient streamed chunk, produced a full replay rather than a suffix. Persisted assistant messages are consolidated under the final chunk ID; replay may be out of numeric order. The iOS implementation rebuilds its presentation from authoritative full replay before loading the same resident actor after a reconnect. The final long-stream UI test confirmed this reconciliation includes every numbered line once. A new agent or agent-side history restoration is unnecessary. `evidence/cursor-fallback-live-result.json` captures this behavior. The earlier `...-740` suffix pass is specific to that accepted cursor.
- Plan UI has automated coverage. Real native plan updates and completion were additionally exercised through the bridge. The actual Simulator displayed both completed plan rows; `evidence/simulator-native-plan.png` in the iOS checkout captures them.
- Automation corrections: the Terminal PTY requires carriage return (`\r`) to submit. Earlier newline-only concurrent test attempts buffered text and were discarded; only the clean `2044` run is counted. An initial reconnect probe waited a fixed 23 seconds before checking the file and failed because user hooks delayed execution; the final cursor proof checked after actual completion.
- Raw event logs remain outside the repository. Checked-in evidence contains only disposable test text, public session identity, event IDs, queue state and test assertions; no authentication headers, bearer secret, hidden thoughts, or user project data.

## Original retained session and cleanup (historical)

After the final Simulator recovery test, the original always-approve actor and its
test clients were stopped. The launcher-smoke leader/bridge/TUI and the untrusted
approval-test TUI were also stopped. Each PID was checked against its expected
task-specific socket and executable before termination. No unrelated Grok process
was stopped.

The originally retained session was the trusted Ask-policy actor used for both manual approval
passes, with the LAN bridge rebound to it:

- Session: `01a15048-205a-71e1-bd32-cdd84834d39a`.
- Agent instance: `c00f8f2d-5c68-405a-a5cd-3666f9bc109e`.
- Socket: `~/.grok/leader-mobile-ask.sock`.
- Endpoint: `ws://192.168.178.150:18790/ws` (Simulator: `ws://127.0.0.1:18790/ws`).
- Retained task process IDs at handoff: native leader `72102`, interactive TUI
  `93759`, authenticated LAN bridge `400`. Process IDs are diagnostic snapshots,
  not durable identifiers; recheck executable/socket identity before cleanup.
- The redundant loopback approval bridge on port 18792 was stopped.
- A fresh authenticated probe of port 18790 attached to this exact resident actor,
  confirmed `resident:true` and `yolo:false`, received 30 unique replay event IDs
  with no duplicates, and rejected a wrong
  secret with HTTP 401. See `evidence/final-protected-endpoint-result.json`.

The historical attach command was:

```bash
~/.grok/bin/grok-1.0.62 --leader \
  --leader-socket ~/.grok/leader-mobile-ask.sock --permission-mode default \
  --cwd /Users/danielstojanovic/code/_tools/grok-live-switching/disposable-project \
  --resume 01a15048-205a-71e1-bd32-cdd84834d39a
```

Those process IDs no longer exist; do not treat this old command or endpoint as a
ready live session. A cold history resume is not equivalent to uninterrupted live
switching. The edge-case follow-up created a separate live leader on
`~/.grok/leader-edge-live.sock`, initially on loopback port 18794, for actual
Simulator recovery tests. Its exact final launch configuration is maintained in
`EDGE_CASE_RESULTS.md`.

## Launcher smoke test

Executed the actual launcher with fresh socket `~/.grok/leader-launcher-proof.sock` and port 18793. It started stock leader 1.0.62, authenticated bridge, and interactive stock TUI. After accepting the disposable workspace trust prompt, Terminal submitted `LAUNCHER-SMOKE-2051`; its exact acknowledgement was replayed through the bridge. Native discovery reported resident session `01a15048-2caa-7fa0-a5f4-de7b1a60a3e3`, instance `a20b92c2-6ed8-45e1-8be1-c4209353fd60`, and `yolo:false`. Wrong-secret rejection and correct-secret attachment were tested on the launched bridge. See `evidence/launcher-smoke-live-result.json`.

The stock smoke-test processes were stopped during cleanup. This historical
command now uses the maintained pinned source build described in the README;
it does not reconnect to the exited stock actor or endpoint.

```bash
cd /Users/danielstojanovic/code/_tools/grok-live-switching/grok-build
python3 tools/live-session-bridge/launch.py \
  --workspace /Users/danielstojanovic/code/_tools/grok-live-switching/disposable-project \
  --leader-socket "$HOME/.grok/leader-launcher-proof.sock" --port 18793
```

## Reproduce a real protocol probe

This attaches only to an already resident agent and refuses a workspace mismatch. The secret is loaded internally from its owner-only file. The output JSON includes prompt and answer text, so use a disposable workspace and review it before sharing.

```bash
python3 tools/live-session-bridge/verify_live.py \
  --url http://127.0.0.1:18793/ws \
  --secret-file "$HOME/.grok/mobile-bridge.secret" \
  --session 01a15048-2caa-7fa0-a5f4-de7b1a60a3e3 \
  --cwd /Users/danielstojanovic/code/_tools/grok-live-switching/disposable-project \
  --output /tmp/grok-live-probe.json
```

To coordinate concurrent instructions, add `--wait-for-enter --prompt 'PHONE-PROBE: Reply exactly PHONE-ACK. No tools.'`, then release its Enter key and submit a distinct instruction in the actual Terminal UI at the same time. Terminal PTY automation must send `\r`, not `\n`. Inspect the output's `userMessages`, `assistantText`, and `queueNotifications`; each instruction and acknowledgement should occur once. `--cursor` characterizes native replay behavior and reports `cursorFallbackToFullReplay`; app presentation reconciliation uses full replay on the same resident actor.

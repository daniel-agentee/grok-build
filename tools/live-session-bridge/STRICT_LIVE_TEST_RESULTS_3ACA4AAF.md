# Historical3aca4aaf verification (includes the discovered Terminal departure failure)

# Final native shared-session verification — 18 October 2026

The final runtime is patched upstream `2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8`,
Grok **1.0.45**, leader IPC protocol1. Executable SHA256:
`3aca4aaf28022060a2c28a4c8e81656e9c772a6c194649649a1fa321574011e0`.
The stable executable is `target/verified-actor-switching/xai-grok-pager`.
It includes native resident-only attachment, live configuration preservation,
and actor incarnation assertions. Both clients control the same resident actor;
mobile attachment cannot cold-restore history or silently adopt a replacement.

This report and `evidence/final-acceptance-matrix.json` distinguish final artifact
coverage from earlier proof. Actual Simulator verification passed against the
68-test app, but a new native edge failure was found: gracefully quitting a second
Terminal while a shared permission is pending fails that permission. A native fix,
new artifact and regression run are in progress; this is not a completion report.
The previous64b609 report
is retained in `STRICT_LIVE_TEST_RESULTS_64B609.md`. Stock1.0.62 and64b609 results
are supporting history and do not prove behavior of this final artifact.

## Retained final runtime

- Native leader socket: `~/.grok/leader-mobile-actor-final.sock`.
- LAN authenticated endpoint: `ws://192.168.178.150:18796/ws`.
- Simulator endpoint: `ws://127.0.0.1:18796/ws`.
- Interactive Terminal-created SID: `01a150c2-68a1-7f40-9a9b-ef146f772b08`.
- Resident actor UUID: `01a150c2-6b38-7592-8a41-117713a5ac43`.
- Agent instance: `3192097e-7756-49b2-8d43-2f584398501e`.
- Workspace: `/Users/danielstojanovic/code/_tools/grok-live-switching/disposable-project`.
- Default permission policy is retained (`yolo:false`). Secret remains in owner-only
  `~/.grok/mobile-bridge.secret`; it is read internally and never printed.
- Diagnostic PID snapshot: native leader25353, retained interactive Terminal25870,
  separate edge-test Terminal37196. Recheck exact process identity before cleanup.
- Original64b609 leader/Terminal/18794 bridge remain preserved independently.

To return to this **still resident** live actor from another interactive Terminal:

```sh
cd /Users/danielstojanovic/code/_tools/grok-live-switching/grok-build
./target/verified-actor-switching/xai-grok-pager --leader --no-auto-update \
  --leader-socket "$HOME/.grok/leader-mobile-actor-final.sock" \
  --permission-mode default \
  --cwd /Users/danielstojanovic/code/_tools/grok-live-switching/disposable-project \
  --resume 01a150c2-68a1-7f40-9a9b-ef146f772b08
```

This command reattaches only while the actor remains resident. Cold resume after
actor/leader exit creates a different actor. The phone refuses automatic resume
against a different actor UUID and requires explicit selection from the live picker.
The README includes clean-build, pinned launcher and secure remote-access commands.

## Final acceptance matrix

| Case | Result on3aca4aaf | Evidence |
|---|---|---|
| 1. Terminal starts; phone discovers and replays the same actor | PASS actual Simulator | `edge-final-corrected-gate-result.json` |
| 2. Attach during a real long-running task | PASS actual Simulator gate | `edge-final-corrected-gate-result.json` |
| 3. Phone prompt and response appear in interactive Terminal | PASS actual Simulator and Terminal | `edge-final-main-terminal-result.json` |
| 4. Terminal prompt and response appear on phone | PASS actual Simulator | `edge-final-corrected-gate-result.json` |
| 5. Controlled edit uses the same workspace | LIVE_PROTOCOL_PASS | `edge-final-worktree-result.json` |
| 6. Approve on phone and Terminal; execute once and clear everywhere | PASS live protocol; UI decisions pending native detach fix | `edge-final-approval-queue-result.json` |
| 7. Disconnect/reconnect phone mid-task without cancelling work | PASS live protocol and actual Simulator | `edge-final-corrected-gate-result.json` |
| 8. iOS background/foreground recovery | PASS actual Simulator | `edge-final-corrected-gate-result.json` |
| 9. Near-simultaneous instructions are not dropped or duplicated | PASS live protocol | `edge-final-approval-queue-result.json` |
| 10. Wrong secret rejected | PASS independent live check | `edge-final-endpoint-health-auth-result.json` |

## Final artifact edge cases already proved live

`edge-final-actor-identity-result.json` passed9 checks using real native actors:
connected actor removal closed WebSocket4409; a replacement with the same saved SID
rejected the old UUID; explicit selection returned a new UUID; an offline phone
that missed removal also failed its old UUID load. Native stale prompt and model
controls returned -32602. A stale native cancellation sent while a normal,
allow-once approved12-second tool was running did not cancel the replacement's
work; it completed once. Rejected stale prompts were absent from fresh replay.
The probe closed only actors it created.

`edge-final-worktree-result.json` passed5 checks against a real linked Git worktree
and the native session-fork API. The roster displayed the source project root while
`_x.ai/session/info` returned the actual worktree. Loading the display root was
rejected by the native atomic guard; two phone-protocol clients loaded the actual
path and received the same actor UUID. A real approved shell edit wrote exactly
once inside the worktree and no file at the source root. No fixture commit was
authored, and original disposable-project files were preserved.

`edge-final-initial-replay-result.json` records actual Terminal seed prompt/ACK
replay, the same actor UUID and5 unique native event IDs. Root independently checked
final endpoint health (`leaderAvailable:true`) and a wrong bearer secret401.

Final transport checks in `edge-final-transport-result.json` passed: an offending
socket using the same pending prompt ID twice closed1008, while the accepted
prompt continued once. A separately owned bridge process was killed after an
approved native tool wrote STARTED and before its12-second wait completed. A new
bridge attached to the same leader/actor and received its completion; the tool
finished once. Only the probe's child transport was stopped.

`edge-final-approval-queue-result.json` passed invalid/wrong/dormant attachment,
no actor creation on rejected loads, pending permission replay, simultaneous and
stale approvals, real denial followed by stale allow, and four concurrent prompts
across two clients. Each queued prompt and exact ACK appeared once; native delivered
12 queue notifications. The approved file contained one line and the denied file
was absent.

`edge-final-terminal-reattach-result.json` records a separate actual interactive
Terminal verification. A fresh PTY resumed the still resident broad-test actor,
rendered a distinctive phone-protocol prompt and response in a VT emulator, then
submitted a Terminal follow-up asking for the phone's codeword. The actual Terminal
rendered the exact recalled codeword. The actor UUID stayed identical across
reattachment and after this probe's Terminal exited. Rendered frames are retained
in `edge-final-terminal-rendered.txt`; this is real Terminal UI coverage, while the
originating phone prompt in this particular probe used the live WebSocket protocol.

`edge-final-config-preservation-result.json` verifies mobile attachment preserved
an explicitly selected reasoning effort, model, absolute workspace and default
permission policy. `edge-final-typed-ids-result.json` verifies simultaneous JSON-RPC
IDs numeric9 and string"9" route as distinct requests. The one-command launcher
smoke in `edge-final-launcher-smoke-result.json` started its own final native leader,
authenticated bridge and actual interactive Terminal; its unique prompt/ACK and
default permission mode were observed before only those probe children stopped.

## Automated and build verification

The corrected app's actual Simulator gate independently observed the native tool
running before and after Home/foreground, then the Mac released the gate and the
file contained `FINISHED-ONCE`. Fresh native replay placed the unique gate ACK
before a newly submitted actual phone prompt and response, with19 unique event
IDs. `edge-final-main-terminal-result.json` and rendered VT frames show the actual
Terminal displaying the corrected app's distinctive `EDGE-UI-102BD9B4` prompt and
`ACK-EDGE-UI-102BD9B4` response. The app SHA256 was
`2c4d708c3252f3bc48e21e30518586b0351a24a592248392583ab6f969d7158e`.

The native departure failure is separately retained in
`edge-final-terminal-pending-detach-investigation.json`: the permission remained
pending through a second Terminal's10-second attachment; its SIGTERM graceful
quit caused a native permission failure7ms later, without a phone decision or
file effect. `verify_terminal_detach_live.py` is the real TUI regression probe
for the upcoming fix, covering both graceful departure and process loss.

Production native build passed. Six focused native resident/identity regression
tests passed. The focused Python suite currently passes54 tests, including local
Git-source drift checks. Root independently verified the final binary, patch and
Cargo.lock digests against the manifest. A clean Python3.12.8 virtual environment
installed pinned aiohttp3.13.3 and independently passed the earlier51-test suite.
The focused CI workflow runs Python transport/source-pin checks and pinned Ruff;
it does not build Rust or use model credentials. The corrected iOS app passed68
focused automated tests; final live permission/question/plan checks remain pending.

## Reproduce final edge probes

From the native checkout root, while the final leader/bridge are running:

```sh
python3 tools/live-session-bridge/verify_actor_identity_live.py \
  --leader-socket "$HOME/.grok/leader-mobile-actor-final.sock" \
  --url http://127.0.0.1:18796/ws \
  --cwd /absolute/disposable/workspace \
  --allow-create-close-disposable-sessions --output /tmp/grok-actor-identity.json
python3 tools/live-session-bridge/verify_worktree_live.py \
  --leader-socket "$HOME/.grok/leader-mobile-actor-final.sock" \
  --url http://127.0.0.1:18796/ws \
  --source-cwd /absolute/disposable/git-root \
  --worktree-cwd /absolute/disposable/git-root/.worktrees/test \
  --allow-create-close-disposable-sessions --output /tmp/grok-worktree.json
```

These probes create/close only their own native actors, perform controlled uniquely
named edits through normal allow-once permission requests, and read the existing
owner-only bearer secret internally. The worktree probe requires a real linked
Git worktree. The source fixture is
`../edge-worktree-fixture-1792356200`, with `.worktrees/mobile-edge`.

## Coverage boundaries

Simulator testing uses the actual built app and authenticated native leader/model;
direct WebSocket probes are separately identified live protocol tests. Earlier
physical-device app/XCUITest launches succeeded after provisioning, but live
session history never appeared and no physical connection reached the Mac endpoint.
Physical Wi-Fi interoperability remains unverified; no physical pass is claimed.

LAN `ws://` authenticates without encryption. Use only a trusted LAN; remote access
requires the documented encrypted VPN/tunnel or validated TLS/WSS approach.
No public unauthenticated service or disabled permission checks were introduced.

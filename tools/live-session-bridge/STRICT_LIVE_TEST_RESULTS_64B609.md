# Historical native64b609 acceptance — 18 October 2026

The previously validated endpoint uses patched upstream source `2bdd1d6a6369`, Grok 1.0.45,
with the native resident-only attach and live-configuration preservation patch.
It does not use the installed stock 1.0.62 runtime. Stock results and the failed
stock explicit-close race are historical evidence in `EDGE_CASE_RESULTS.md`.

Tested executable SHA256:
`64b609fca4e066c8f1ea8552ad233381afa1bfaa7a12212c74156c1a1fe16cb2`.
The stable copy is `target/verified-live-switching/xai-grok-pager`.
Native sandbox/default permission features were preserved in the build.
The trusted disposable workspace uses normal default permissions (`yolo:false`).

## Retained live session

- Session: `01a150a3-00c6-75b2-9b04-80cddfb159ef`.
- Workspace: `/Users/danielstojanovic/code/_tools/grok-live-switching/disposable-project`.
- Native socket: `~/.grok/leader-mobile-strict.sock`.
- Authenticated endpoint: `ws://192.168.178.150:18794/ws`; Simulator `ws://127.0.0.1:18794/ws`.
- Task-specific diagnostic PID snapshot: native leader `95726`, interactive TUI
  `95830`, authenticated bridge `99332`. Recheck identity before cleanup.
- The previous stock loopback18794 bridge and temporary LAN relay were stopped.
  This endpoint binds the authenticated bridge directly; no relay is required.
- Secret remains in owner-only `~/.grok/mobile-bridge.secret`, read internally.

To attach another interactive Terminal to the **still resident** actor:

```sh
cd /Users/danielstojanovic/code/_tools/grok-live-switching/grok-build
./target/verified-live-switching/xai-grok-pager --leader --no-auto-update \
  --leader-socket "$HOME/.grok/leader-mobile-strict.sock" \
  --permission-mode default \
  --cwd /Users/danielstojanovic/code/_tools/grok-live-switching/disposable-project \
  --resume 01a150a3-00c6-75b2-9b04-80cddfb159ef
```

A cold resume after that actor or leader exits would create a different live actor;
this documented command is a reattachment only while the actor is resident.
The mobile bridge enforces this distinction atomically and refuses cold restore.
Use the README launcher to start a new independently selected live session.

## Final artifact acceptance matrix

Every live row below used native artifact `64b609…16cb2` and real authenticated
model/tool execution. WebSocket probes exercise the phone protocol against the
actual native leader; they are distinct from actual Simulator UI tests.

| Required case | Result on final artifact | Evidence |
|---|---|---|
| 1. Start in Terminal, attach and see existing conversation | PASS — actual interactive TUI created the retained actor; actual Simulator attached through resident discovery and replayed Terminal's seed conversation. | `edge-strict-initial-replay-result.json`, `edge-strict-simulator-terminal-result.json`; iOS strict Simulator xcresults |
| 2. Attach during long-running work | PASS live protocol — a newly started bridge/mobile subscriber attached during the approved 12-second native shell task and received its completion. Simulator attach during that long task has not yet been exercised on this final artifact. | `edge-strict-transport-result.json` |
| 3. Phone prompt and answer visible in Terminal | PASS actual Simulator + interactive TUI — `EDGE-UI-9114E657` and `ACK-EDGE-UI-9114E657` appeared in both. | `edge-strict-simulator-terminal-result.json` |
| 4. Terminal prompt and answer visible on phone | PASS live protocol and Simulator replay — Terminal's initial seed was visible in actual Simulator history. A fresh interactive TUI submitted `EDGE-RETURN-TO-TERMINAL`; its prompt and exact recalled phone ACK streamed to the mobile protocol subscriber. Live incoming Terminal prompt in actual Simulator UI is an additional pending check. | `edge-strict-initial-replay-result.json`, `edge-strict-return-terminal-result.json` |
| 5. Controlled edit uses same workspace | PASS — normal permission approval ran native shell in the selected absolute disposable workspace; phone- and Terminal-approved files each contain exactly one expected line. Both surfaces received the same native tool-call identity. | `edge-strict-phone-permission-result.json`, `edge-strict-terminal-permission-result.json` |
| 6. Real permission answered from either device, once, cleared everywhere | PASS — actual Simulator allow-once and actual interactive Terminal option3 each resolved their distinct native request. Fresh replay had no stale permission and each controlled file had one line. Concurrent/stale/rejected decisions were separately verified with real native execution. | Above permission reports; `edge-strict-approval-queue-result.json`; iOS approval xcresults |
| 7. Disconnect/reconnect phone mid-task | PASS live protocol — abrupt disposable bridge crash after STARTED and before shell sleep ended did not cancel native work; new bridge attached and observed FINISHED-ONCE plus completion. | `edge-strict-transport-result.json` |
| 8. Background/foreground app | PASS actual Simulator — three cycles restored the resident session with no duplicate prompt/answer; failed selection retained the unsent draft and explicit picker recovered. | `edge-strict-simulator-terminal-result.json`; iOS final60 xcresult |
| 9. Near-simultaneous instructions | PASS live protocol — four concurrent prompts across two clients each produced one user prompt and one exact ACK; native queue delivered12 updates. | `edge-strict-approval-queue-result.json` |
| 10. Wrong secret rejected | PASS — final endpoint rejected wrong Bearer secret401. Root independently repeated this before acceptance freeze. No credentials were printed. | `edge-strict-initial-replay-result.json`; root independent endpoint check |

The files in this table live in `evidence/`. The ordinary app integration tests
use a second disposable Terminal-created actor `01a150a8-8a2e-78c0-bef7-19b86d4a8cf1`
on the same final native leader where needed, leaving the retained Simulator actor
available for UI decisions. Transport-only fixtures are stopped after each test.

## Additional common failures verified

- Atomic close-versus-attach: a real native framing proxy paused a genuine
  `resident:true` reply, a second native connection closed the actor, and mobile
  load reached the native guard. It returned -32602 with
  `live attachment target is no longer resident; select a running session`.
  The actor stayed nonresident; no replacement was created.
  `edge-strict-final-explicit-close-race-result.json` captures the result.
- Nondefault live configuration: native model effort was explicitly set to low;
  mobile attach preserved low effort, model, absolute workspace and normal
  permission policy. The fixture was restored to high afterward.
  See `edge-strict-config-preservation-result.json`.
- Invalid, relative, wrong and dormant targets plus mobile create/cold-resume were
  rejected without adding a native actor. Pending permission survived reconnect;
  racing and obsolete decisions executed once; rejecting a tool then sending a
  stale allow did not create the file. See `edge-strict-approval-queue-result.json`.
- Duplicate pending JSON-RPC prompt ID closed the offending socket1008 while the
  first accepted prompt continued once. Numeric9 and string"9" remained distinct.
  See `edge-strict-transport-result.json` and `edge-strict-typed-ids-result.json`.
- Terminal process detachment: a phone-protocol subscriber stayed attached while
  its original TUI process exited and a fresh actual TUI explicitly resumed the
  same resident SID. The new Terminal recalled the phone's distinctive ACK from
  context and streamed its own prompt/answer back to mobile. See
  `edge-strict-terminal-detach-result.json` and `edge-strict-return-terminal-result.json`.
- Actual native structured questions: Simulator selected Blue, Apple+Bread, and
  submitted "Phone native question note". Native completed tool output contained
  all three values; actual Terminal displayed PHONE-QUESTION-DONE. A pending
  request was recovered after app restart. `edge-strict-question-result.json`.
- Actual native plan approval: Simulator displayed the complete harmless plan,
  sent native approved outcome, cleared its sheet and received PHONE-PLAN-DONE.
  Native plan tool completed, Terminal displayed that same ACK, fresh replay had
  no pending interaction, and `yolo:false` remained set. `edge-strict-plan-result.json`.
- One-command launcher: actual `launch.py` started its own final native leader,
  authenticated bridge and interactive TUI. Terminal's exact
  EDGE-LAUNCHER-FINAL-ACK replayed, resident/default policy were confirmed, and the
  isolated launcher fixture was stopped. `edge-strict-launcher-smoke-result.json`.

Root independently confirmed42 focused Python tests green at its initial check;
the latest focused suite passed43. It also checked native binary,
patch and Cargo lock hashes matching the compatibility manifest. Native exact-
actor/configuration regression tests passed5/5. The iOS worker's final focused
suite passed60 tests and its Simulator smoke passed. Ruff passed for all live
probe scripts. No additional Cargo build changed the verified native artifact.

## Reproduce ordinary integration verification

From the native checkout root, with an already resident **disposable** actor:

```sh
python3 tools/live-session-bridge/verify_edge_live.py \
  --url http://127.0.0.1:18794/ws --session DISPOSABLE_LIVE_SID \
  --cwd /absolute/disposable/workspace --output /tmp/grok-live-edge.json
python3 tools/live-session-bridge/verify_transport_live.py \
  --leader-socket "$HOME/.grok/leader-mobile-strict.sock" --cli-version 1.0.45 \
  --session DISPOSABLE_LIVE_SID --cwd /absolute/disposable/workspace \
  --output /tmp/grok-live-transport.json
python3 tools/live-session-bridge/verify_interaction_live.py \
  --kind question --url http://127.0.0.1:18794/ws --session DISPOSABLE_LIVE_SID \
  --cwd /absolute/disposable/workspace --output /tmp/grok-live-question.json
```

The first probe makes controlled uniquely named edits through real allow-once
permission requests. The second abruptly restarts only its own bridge child.
The third waits for an actual attached device to answer native questions; use
`--kind plan` for the separate native plan approval case. The secret is read
internally from its owner-only file and never passed on argv. The explicit-close
race reproducer is destructive to its named disposable actor and must never run
against a user's work session.

A separate clean UI demonstration actor was prepared on this same final leader:
`01a150ae-8124-75b2-9737-50e7620572ca`, titled
"Mac and iPhone workspace switching test". It contains a real natural-language
orientation rather than the test-marker history. The original retained actor and
its evidence remain available independently.

## Additional lifecycle issue found during final review

A fresh disposable actor exposed a separate transport lifecycle gap: after phone
attachment, an independent native client explicitly closed the actor and then
cold-restored the same saved SID. The old phone socket could submit to that
replacement without explicitly attaching again. Native emitted a genuine
`_x.ai/sessions/changed` removal for the SID, which the bridge must treat as
revocation. The failed real run is retained in
`evidence/edge-strict-stale-actor-identity-result.json`. Closing connected sockets
on removal is insufficient: an offline phone can miss removal, then reconnect
after a different actor cold-restores the same saved SID. The final patch must
bind attachment and controls to a native actor incarnation UUID, retain that
assertion across reconnect, and require explicit selection after replacement.
A focused native/transport fix and fresh live retest are required before the
final runtime is considered frozen. `verify_actor_identity_live.py` reproduces
connected removal, offline replacement, explicit reselection, and stale native
prompt/model/cancel controls using only its own disposable actor and files.
The retained user-facing actors were not closed by this test.

## Coverage boundaries

Actual Simulator testing uses the built app and the real authenticated native
leader/model. Direct WebSocket probes are live protocol tests, distinguished
from Simulator UI coverage. Earlier physical-device app/XCUITest launches
succeeded after provisioning, but live session history never appeared and no
physical connection reached the Mac endpoint. The device later relocked.
Physical Wi-Fi interoperability remains unverified; no physical pass is claimed.

The LAN bridge authenticates with the owner-only shared secret. Plain `ws://`
does not encrypt LAN traffic; use the documented encrypted tunnel/VPN approach
for remote or untrusted networks. No public exposure or approval bypass was used.

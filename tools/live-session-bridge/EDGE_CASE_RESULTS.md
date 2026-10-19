# Edge-case verification — 18 October 2026

These runs use the actual authenticated native Grok leader and model, interactive
CLI PTYs, and normal Ask/default permissions in the trusted disposable project.
WebSocket protocol probes supplement the actual Simulator tests; they are not
described as iPhone UI tests. Secrets are read internally from their owner-only
file; reports contain no authentication values, private thoughts, or unrelated
conversation content.

## Verified against stock 1.0.62

The original retained process IDs from `LIVE_TEST_RESULTS.md` had exited before
this follow-up. A fresh isolated stock leader was created on
`~/.grok/leader-edge-live.sock`, with a fresh interactive PTY session
`01a15067-0f81-76e0-aeb4-4c00f40a9afb` in the same disposable project. Its native
roster reported `yolo:false`. The server instance was
`effb2bca-bb70-4ecf-ae1b-93c93dfcfc3b`. Native initialize's `agentInstanceId` is a
server/MvpAgent identity; it does not independently identify a resident session
actor. Resident routing and the executed file effects provide the shared-session
evidence.

| Failure exercised | Actual observation | Evidence |
|---|---|---|
| Wrong, relative, missing, dormant session attachment | All rejected. Native resident roster was unchanged; no actor was created. Explicit `session/new` and `session/resume` were rejected. | `evidence/edge-approval-queue-result.json` |
| Disconnect with a real permission request pending | Tool had not run before disconnect. Fresh connection attached and received the same tool-call permission request. | Same report |
| Two clients approve concurrently, then a stale approval arrives | Both clients received resolution; native execution wrote exactly one expected line. A repeated obsolete answer did not execute again. | Same report, `edge-pending-1792350384.txt` in disposable project |
| Permission is rejected, then a stale allow arrives | Real reject-once completed the turn without creating the target file. A later allow-once for that resolved request did not overturn the denial. | `evidence/edge-rejected-approval-result.json` |
| Four prompts submitted together over two connections | Four user prompts and four distinct ACKs each occurred exactly once; native queue emitted 12 updates. | Same report |
| Numeric request ID `9` concurrent with string ID `"9"` | Both native replies retained their correct ID type and correlated correctly. | `evidence/edge-typed-ids-result.json` |
| Duplicate still-pending prompt request ID | WebSocket closed with 1008; its first accepted prompt continued and appeared exactly once on a second subscriber. | `evidence/edge-transport-result.json` |
| Abrupt bridge process crash during approved real shell execution | A disposable bridge child was killed after the command wrote STARTED, before its 12-second sleep ended. The same native leader continued. A fresh bridge replayed completion and the expected FINISHED-ONCE file was written once. | Same report |
| Native leader process death | Attached socket closed with 1011. Further authenticated upgrades failed with 503. A replacement leader had a different server instance, rejected the old session as nonresident, and did not recreate an actor. | `evidence/edge-leader-death-result.json` |
| Two real permission sheets answered from actual Simulator | Native prepares these two calls sequentially. Simulator approved the distinct first and second tool once; each file has one expected line. Interactive Terminal displayed completion, native became idle, and fresh reconnect replay contained no pending request. This proves sequential requests, not simultaneous outstanding requests. | `evidence/edge-sequential-simulator-permissions-result.json` |
| Actual Simulator reconnect after a failed selection, retained draft, three background cycles | The actual XCUITest passed. The phone's distinctive prompt `EDGE-UI-D741EE07` and response `ACK-EDGE-UI-D741EE07` were also independently observed in the interactive CLI PTY. | `evidence/edge-simulator-terminal-result.json`; iOS evidence directory |

The leader-death test used a separate `leader-edge-death.sock` leader. Only its
verified task-specific TUI, leader and bridge processes were stopped. It did not
interrupt the session reserved for Simulator verification.

## Critical explicit-close race found in stock

The former bridge's roster preflight alone did **not** guarantee attachment to
the same resident actor. A deterministic fault-injection proxy delayed a genuine
native roster response after `resident:true`, closed that actor through a second
native IPC connection, confirmed `resident:false`, then released the response.
Stock 1.0.62 accepted the subsequent `session/load` and made the session resident
again from saved data. This is a failed edge case, not seamless live switching.

`evidence/edge-explicit-close-race-result.json` records the native close result
`x.ai/closeOutcome:closed`, successful load after closure, and restored residency.
`verify_actor_race_live.py` reproduces this against a separately created
disposable leader/session. It must never target a real work session.

The maintained native patch adds an atomic resident-only attachment guard and
requires its advertised capability before the bridge accepts a connection. The
same deterministic live race passed against the patched source build 1.0.45:
preflight saw the actor resident, native close made it nonresident, mobile load
returned an error, and the actor stayed nonresident. The guard did not recreate
an actor. See `evidence/edge-strict-explicit-close-race-result.json`.

This first strict race run used artifact SHA256
`0c7e3604cb81376ba4888918fce1e0b8c3944e9ab7d105d9aec1d216ecbd00d7`.
The final retained runtime also includes live-configuration preservation;
its separate final acceptance snapshot is recorded below.

## Reproduce the live probes

Run against an already resident disposable actor under normal permission policy:

```sh
python3 tools/live-session-bridge/verify_edge_live.py \
  --url http://127.0.0.1:18795/ws \
  --session RESIDENT_SESSION_ID --cwd /absolute/disposable/workspace \
  --output /tmp/grok-edge-approval-queue.json

python3 tools/live-session-bridge/verify_transport_live.py \
  --leader-socket "$HOME/.grok/leader-edge-live.sock" \
  --session RESIDENT_SESSION_ID --cwd /absolute/disposable/workspace \
  --output /tmp/grok-edge-transport.json
```

The second command creates and abruptly restarts only its own transport child on
port 18797. It leaves the native leader running. Each invocation uses uniquely
named disposable files and obtains a real allow-once permission before writing.
These tests do not disable or preapprove native permission checks.

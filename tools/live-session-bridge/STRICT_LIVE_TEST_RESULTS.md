# Shared native Grok session — final verification, 18 October 2026

Terminal and the actual iOS Simulator app controlled one resident native actor,
with the same live context and Mac workspace. The final native artifact is patched
upstream `2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8`, Grok **1.0.45**, leader IPC **1**.
Executable SHA256: `d41925f463ab9b81fcb7ca55e759f193e7fa6f526f8fade8f8447a7f99c465ab`.
The immutable executable is `target/verified-switching-final/xai-grok-pager`;
`target/debug/xai-grok-pager` currently has the same digest.

Start conversations through the provided pinned leader launcher. An already
running stock CLI or non-leader Terminal agent cannot be retrofitted into this
shared session. The bridge creates no agent and cannot restore a saved session.
The native patch adds atomic resident-only attachment and actor-incarnation
checks, and preserves shared approvals when a local Terminal subscriber leaves.
Both clients use the existing authenticated CLI access and normal default
permissions. No approval policy was disabled.

The corrected app SHA256 is
`2c4d708c3252f3bc48e21e30518586b0351a24a592248392583ab6f969d7158e`.
The iOS app result bundles and source manifest are indexed in the sibling
`grok-build-ios/evidence/ios-verification-results.json`.
All results below use that app and the final D419 native executable unless
explicitly marked supporting. Final machine-readable evidence is under
`evidence/verified-final/`; the acceptance index is
`evidence/final-acceptance-matrix.json`.

## Retained demonstration

- Authenticated LAN endpoint: `ws://192.168.178.150:18797/ws`.
- Simulator endpoint: `ws://127.0.0.1:18797/ws`.
- Native socket: `~/.grok/leader-mobile-switching-final.sock`.
- Actual Terminal-created SID: `01a150de-fd8f-7d20-a68e-b28a5aa332e9`.
- Resident actor UUID: `01a150de-ffe8-7341-865e-53ae759c7c0f`.
- Native agent instance: `293a484c-2975-47d2-ad5b-74392dc035d9`.
- Workspace: `/Users/danielstojanovic/code/_tools/grok-live-switching/disposable-project`.
- One-command launcher started the final native leader, bridge and interactive
  Terminal. Bridge PID at launch was 48934; the original Terminal remains open.
- The secret remains in owner-only `~/.grok/mobile-bridge.secret`, read internally
  and never printed or passed in argv. Permissions remained `yolo:false`, model
  `grok-4.7-build-fast`, reasoning effort `high`.

To return to this **still resident** actor in another interactive Terminal:

```sh
cd /Users/danielstojanovic/code/_tools/grok-live-switching/grok-build
./target/verified-switching-final/xai-grok-pager --leader --no-auto-update \
  --leader-socket "$HOME/.grok/leader-mobile-switching-final.sock" \
  --permission-mode default \
  --cwd /Users/danielstojanovic/code/_tools/grok-live-switching/disposable-project \
  --resume 01a150de-fd8f-7d20-a68e-b28a5aa332e9
```

A saved SID alone cannot prove continuity: cold restoration after actor exit
creates a new actor UUID. The phone refuses an automatic reconnect to that
replacement and requires explicit selection from the live picker.

## Acceptance

| Case | Final artifact result | Evidence under `evidence/verified-final/` |
|---|---|---|
| 1. Start in Terminal; discover and replay on phone | PASS actual Simulator and CLI | `initial-seed-identity-config.json`, `actual-simulator-gate-native-replay.json` |
| 2. Attach while a real task is running | PASS actual Simulator | `actual-simulator-gate-native-replay.json` |
| 3. Phone prompt/response appear in interactive Terminal | PASS actual Simulator and real Terminal VT rendering | `actual-terminal-render-result.json`, `actual-terminal-render-rendered.txt` |
| 4. Terminal prompt/response appear on phone | PASS actual Simulator | `actual-simulator-gate-native-replay.json` |
| 5. Controlled edit uses the same workspace | PASS live native protocol and Git worktree | `worktree.json` |
| 6. Phone and Terminal approvals execute once and clear everywhere | PASS actual Simulator and interactive Terminal | `phone-permission.json`, `terminal-permission.json`, `approval-queue.json` |
| 7. Phone reconnect mid-task preserves work/history | PASS actual Simulator and live transport restart | `actual-simulator-gate-native-replay.json`, `transport.json` |
| 8. Background/foreground recovery | PASS actual Simulator | `actual-simulator-gate-native-replay.json` |
| 9. Near-simultaneous submissions are not lost/duplicated | PASS live protocol: four prompts across two clients, exact ACK once each | `approval-queue.json` |
| 10. Wrong secrets rejected | PASS actual Simulator and independent HTTP401 check | `independent-health-auth.json` |

The actual Terminal submitted `D419-MAC-GATE-1792358383978246000`, then approved
its genuine permission with native option 3 (allow once). The tool wrote STARTED
and waited on a release file. Only after independent STARTED/no-FINISHED checks
did the actual app attach, observe the active tool, go Home and foreground while
it was still running. The Mac then released it; it wrote `FINISHED-ONCE`.
The app observed the unique gate ACK and submitted
`PHONE-UI-GATE-4FAB5B5F`, receiving `ACK-PHONE-UI-GATE-4FAB5B5F`.
Fresh native replay confirmed all prompts and ACKs once, gate completion before
that actual phone follow-up, unique native event IDs and the identical actor UUID.
The actual Simulator basic prompt `EDGE-UI-BAECC40D` and response also survived
three foreground/reconnect cycles and retained draft input.

A separate actual PTY resumed the same resident actor and its VT display showed
the actual Simulator basic prompt/ACK, gate ACK and unique phone follow-up/ACK.
The rendered frames are retained. This verifies the Terminal UI, rather than
merely stripping ANSI escape sequences or inspecting persisted history.

The actual Simulator approved one genuine append permission at maximum system
Dynamic Type. The file was absent until that decision, then contained exactly one
line; the Terminal advanced and fresh replay had no pending permission. A second
permission was read and scrolled in the app at maximum Type (the full 183-character
native decision message, selected independently of the user transcript). It was
approved from the original interactive Terminal using option 3. The app cleared its
modal and received the exact completion ACK; that second file also had one line
and no stale permission replayed. Normal permission policy stayed active.

The real native structured-question form also passed in the actual app: single
selection Blue, multi-selection Apple and Bread, and the exact phone note were
verified independently in the native tool result (`question.json`). The actual
app approved the real native plan through the plain approved option; native
`ExitPlanMode.PlanReady` confirmed approval (`plan.json`). The final roster still
reported normal permissions, the original resident actor and idle activity, and
fresh replay contained no pending interactions (`final-idle-default-permissions.json`).

The actual Terminal then sent a follow-up asking for the phone's codeword without
including the answer. The shared actor replied exactly `PHONE-UI-GATE-4FAB5B5F`.
The initial evidence monitor waited incorrectly for a whole message in one live
chunk; the accepted prompt was never repeated. A subsequent read-only actual PTY
captured the original Terminal prompt and response, with the same actor UUID
before and after that Terminal left. See `actual-terminal-return.json` and
`actual-terminal-return-rendered.txt`.

## Common failure cases tested live on the final artifact

- **Terminal departure during approval:** real secondary TUI SIGTERM and SIGKILL
  each left the shared permission pending for a surviving mobile client. The
  client approved once; each unique disposable file had exactly one line, no
  stale permission replayed, and the same resident actor survived. See
  `terminal-detach-sigterm.json` and `terminal-detach-sigkill.json` (six checks each).
- **Approval races and rejection:** disconnect/reconnect replayed the same pending
  permission; simultaneous and stale allow-once replies executed once and cleared
  both clients. A real denial prevented execution and a late allow could not
  reverse it. See `approval-queue.json` (twelve checks).
- **Queue pressure:** four distinct prompts released together across two clients
  each appeared once with one exact response. 14 native queue notifications were
  observed. Busy submissions serialize through the same native session queue.
- **Wrong or missing attachment identity:** relative/wrong cwd, missing/dormant
  SID and forbidden new/cold-resume requests were rejected without adding actors.
  Numeric JSON-RPC 9 and string "9" routed independently.
- **Bridge/socket loss:** a duplicate pending request ID closed only the offending
  socket 1008; its accepted prompt completed once. A separately owned bridge was
  killed after an approved 12-second native tool started, then restarted. The same
  agent instance completed once and replayed through the replacement transport.
  The retained bridge was untouched. See `transport.json` (five checks), rerun
  serially after the approval/queue probe finished.
- **Closed/replaced actors:** connected removal closed the phone socket 4409;
  replacing the saved SID rejected its old UUID. Offline removal/cold-restore
  also rejected the cached UUID; explicit selection got the new identity.
  Native stale prompt/model controls failed, stale cancellation did not cancel
  the replacement's real approved 12-second task, and rejected prompts were absent
  from replay. See `actor-identity.json` (nine checks).
- **Real Git worktree:** native fork used the actual linked worktree while the
  roster displayed its source root. Loading that display root failed. Two mobile
  clients attached to the actual execution cwd/actor; one approved edit wrote once
  only there. See `worktree.json` (five checks). No fixture commit was authored.

The Terminal-disconnect defect was discovered against the predecessor 3aca
artifact: graceful exit failed a shared pending permission within 7 ms. The final
native fix ignores only typed local send/receive-channel failures for interactions
that are still cached and shared. Explicit denial/cancellation and unrelated
errors retain their normal behavior. The new native tests and both actual TUI
regressions passed; the failed predecessor investigation is retained as historical
`evidence/edge-final-terminal-pending-detach-investigation.json`.

## Builds and automated coverage

- Production native build from the pinned source/patch: PASS. Rust 1.94.0,
  cargo 1.94.0, target `aarch64-apple-darwin`, locked dependencies; default features
  retain sandbox checks. `build-manifest.json` records exact build/lock/patch hashes.
- Native focused regressions: **8 PASS** (6 resident/identity + 2 disconnected
  interaction-response tests). Real ACP channel errors and leader Unix IPC are
  covered by those tests; real model/UI acceptance is listed separately above.
- Python focused transport/source-pin suite: **54 PASS**, Ruff/compile PASS.
  Python 3.12 and pinned aiohttp 3.13.3. Independent checks verified final artifact,
  patch and lockfile hashes and actual endpoint health/wrong-secret rejection.
- iOS focused automated suite: **68 PASS**. All **8 final actual Simulator UI runs**
  passed (one test each, zero failures/skips), independently checked from Xcode
  result bundles. Xcode Simulator build and actual app
  live tests used the app digest above. No model credentials were placed in source.

The initial final-artifact model response was slow but completed without a retry;
accepted prompts were not resubmitted. Provider response latency is independent
of transport reconnection and is visible as active waiting state.

## Supporting predecessor edge proof

The unchanged 68-test app also passed these real native/actual Simulator edge cases
against predecessor 3aca. They support these UI features, and are not claimed as
new D419 executions:

- 300 streamed assistant lines: actual Terminal prompt contained only a unique
prefix, so the UI assertion could not accidentally match the prompt. The app
showed partial output plus Stop, went Home for 35 seconds and returned to all 300 lines in
one assistant message. Fresh native replay contained every line ascending once
and a unique completion marker. See `edge-final-longstream-filtered-native-result.json`.
- Delayed A→B selection: an authenticated proxy delayed only sessionB preflight
20 seconds. Send retained the draft and forwarded no prompt until real attachment.
Both native transcripts excluded the unsent marker. See
`edge-final-delayed-selection-native-result.json`.
- Actual UI close/replacement lifecycle showed an ended banner, retained draft,
no misleading Live state and a live picker; offline stale actor selection also
failed. See `edge-final-lifecycle-ui-supporting-result.json`.

Historical reports/matrices for 3aca, 64b609 and installed stock 1.0.62 are preserved
separately. Their passes do not override a final-artifact failure.

## Coverage boundary and secure access

Physical-device provisioning and app/XCUITest launches succeeded, but no physical
phone live history loaded and no physical connection reached the Mac endpoint.
The later phone was locked. **Physical iPhone Wi-Fi interoperability remains
unverified**; Simulator coverage is explicitly identified throughout this report.
Unlocking the provisioned phone on the Mac's Wi-Fi and connecting the retained
endpoint is the remaining device-specific check.

Authenticated `ws://` LAN traffic is unencrypted. Use a trusted LAN for this first
version. Remote access requires the README's encrypted VPN/tunnel or validated
TLS/WSS setup. The service is never exposed as public unauthenticated access.
The README contains reproducible build/launch and edge-probe commands.

After verification, only the four exact, revalidated task-owned predecessor 3aca
processes were stopped, after confirming their two resident actors idle. The
final D419 demonstration remains running; the older original 64b609 runtime remains
preserved. The separate final broad-test actor was explicitly closed by its own
controller. See `owned-predecessor-runtime-cleanup.json`.

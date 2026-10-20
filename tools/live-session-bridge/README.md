# Live Grok session bridge

A small authenticated WebSocket transport for **one existing native Grok leader**.
It creates no `MvpAgent`, model connection, or conversation history store.
An explicit mobile **New session** request creates a fresh session on that existing
leader, with the Mac's normal workspace trust and permission checks.
The Terminal and iOS clients subscribe to the same resident session actor through
Grok's existing local leader, and tools execute on the Mac in that actor's workspace.

## Compatibility

The final transport requires a **patched source build, Grok 1.0.45**, leader
protocol **1**, Python **3.12**, and aiohttp **3.13.3**. The official source base is
`xai-org/grok-build` commit `2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8`
(monorepo `SOURCE_REV` `559751fdcec02d413e4c57c8832ab275e4f44980`).
The focused native patch advertises `x.ai/residentOnlyAttach` and
`x.ai/residentActorIdentity`, with opt-in `_meta["x.ai/resident_only"]=true`
load semantics. Attachment either
reuses the exact captured resident actor or fails; it cannot cold-restore history
when an explicit close races discovery/load.
Each strict load returns `_meta["x.ai/residentActorId"]`, a UUID for that live
actor incarnation. Saved session IDs alone do not identify an uninterrupted actor.

The Rust-only patch is `resident-only-attach.patch`; it applies to that exact
source base. `build-manifest.json` records the compiler, lockfile/patch digests,
build command, and final local binary digest. The launcher verifies that binary's
SHA256 before starting either process. It uses the isolated
`../../target/debug/xai-grok-pager`, preserving the globally installed CLI.

Earlier live tests used installed stock **1.0.62 (1474f9cbdd91)**, SHA256
`ec434e831afdcfce87780f2815fd732b1df8a4a8de96a491716ad75d13605a4e`.
Those results are historical and identified separately in the evidence. Stock
1.0.62 demonstrably cold-restored a session when explicit close raced attachment;
the final strict bridge refuses stock leaders that lack the new native capability.
See [STRICT_LIVE_TEST_RESULTS.md](STRICT_LIVE_TEST_RESULTS.md) for historical
D419 strict-attachment acceptance, retained endpoints/session and reconnection
commands. The final bounded-runtime evidence is linked at the end of this README.
Any stock 1.0.62 results elsewhere in this directory are historical only.

Start the conversation using the provided pinned leader launcher below. Both
Terminal and bridge must connect to that same patched native leader. An already
running stock CLI or a Terminal agent started without this leader cannot be
retrofitted into the shared session. Do not use `grok agent serve`: it owns a
separate agent from the Terminal's leader.
A different version, protocol, or missing capability is rejected.

## Launch

The exact native build command, from the repository root, is below. Default
features retain sandbox enforcement. For a fresh checkout, apply the focused
patch first and use the rebuild/pin helper below to prepare the launcher artifact:

```sh
CARGO_PROFILE_DEV_DEBUG=0 CARGO_INCREMENTAL=0 cargo build --locked --offline \
  -p xai-grok-pager-bin --bin xai-grok-pager
```

For a clean source checkout at the pinned revision, first apply
`git apply /path/to/resident-only-attach.patch`. Offline mode uses already cached
Cargo dependencies; omit `--offline` once to fetch locked dependencies when needed.
Build output hashes can vary with checkout paths and local linker details. After
copying this transport directory into a clean checkout and applying the patch,
run the explicit local rebuild/pin command below. It checks the exact native diff
against the reviewed patch, its digest, Cargo.lock, compiler versions and target;
refuses unexpected tracked/untracked native source changes; builds from those
inputs; rechecks the source; then updates the launcher and manifest artifact hash. The executable must report
the pinned numeric CLI version and the actual checkout HEAD suffix, which is
recorded separately from the reviewed source base and patch.
It never accepts an arbitrary supplied executable or skips launcher verification.
Run the tests and acceptance checks again for a newly built artifact.

```sh
python3 tools/live-session-bridge/pin_build.py
```

Then, from this directory:

The reproducible build writes `target/debug/xai-grok-pager`; the launcher checks
that output against the exact manifest hash. The session-feature verification
copy is `target/verified-session-features-mcp-bounded/xai-grok-pager`.
The earlier demonstration executable remains separately preserved at
`target/verified-switching-final/xai-grok-pager`, SHA256
`d41925f463ab9b81fcb7ca55e759f193e7fa6f526f8fade8f8447a7f99c465ab`.
Its original manifest and patch are retained as `evidence/d419-build-manifest.json`
and `evidence/d419-resident-only-attach.patch`; its historical live evidence in
`STRICT_LIVE_TEST_RESULTS.md` does not verify the newer MCP containment patch.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

For a single command that starts the pinned leader and bridge, then enters the
interactive Terminal UI:

```sh
.venv/bin/python launch.py --workspace /absolute/path/to/workspace --host 0.0.0.0
```

Use an unused `--port` if 18789 is occupied. The launcher does not replace services
already using a port. Quitting Terminal leaves its bridge/leader running; to return
to that conversation, run the explicit Terminal command below with `--resume LIVE_SID`
and the same socket/workspace. The launcher reports its bridge PID for cleanup.
Stop that PID and the explicitly started leader only when no client needs them.

For manual launch in separate Terminals, first start the pinned leader (keep it running):

```sh
../../target/debug/xai-grok-pager --permission-mode default \
  --leader-socket ~/.grok/leader-mobile.sock \
  agent leader --no-auto-update --no-exit-on-disconnect --relay-on-demand
```

In another Terminal, start an interactive conversation in the desired workspace:

```sh
../../target/debug/xai-grok-pager --leader --no-auto-update --leader-socket ~/.grok/leader-mobile.sock \
  --permission-mode default --cwd /absolute/path/to/workspace
```

To return to one specific **still resident** session, use its explicit live SID
from the session picker. This avoids selecting another workspace's latest session:

```sh
../../target/debug/xai-grok-pager --leader --no-auto-update \
  --leader-socket ~/.grok/leader-mobile.sock --permission-mode default \
  --cwd /absolute/path/to/workspace --resume LIVE_SID
```

The leader must still own that resident actor. After the leader or actor exits,
native cold resume creates a new actor; the phone requires explicit selection and
will reject automatic attachment using its cached old actor UUID. The exact
retained verification session and stable executable command are recorded in
[`STRICT_LIVE_TEST_RESULTS.md`](STRICT_LIVE_TEST_RESULTS.md#retained-final-runtime).

Start the bridge, selecting an unused port. The first invocation generates a
random owner-only secret file. Omit `--init-secret` on subsequent launches:

```sh
.venv/bin/python bridge.py --leader-socket ~/.grok/leader-mobile.sock \
  --secret-file ~/.grok/mobile-bridge.secret --init-secret \
  --host 0.0.0.0 --port 18789
```

Configure iOS with `ws://<Mac-LAN-IP>:18789/ws` and the credential from that file
through the app's secure credential input. The secret is never printed by the
bridge. To paste it into the phone's SecureField using Apple's Universal Clipboard,
optionally run this locally on the Mac (the credential goes directly to the
clipboard, never stdout or command-line arguments):

```sh
python3 - <<'PY'
from pathlib import Path
import subprocess

subprocess.run(["pbcopy"], input=(Path.home() / ".grok/mobile-bridge.secret").read_bytes().strip(), check=True)
PY
```

Paste into the app's credential field; avoid pasting into chat or a terminal prompt.
Clear the clipboard afterward with `printf '' | pbcopy`. Simulator can use
`127.0.0.1`; its test-only private preference setup is documented in the iOS
repository's verification instructions. Bind to `127.0.0.1` when only Simulator is
needed. `/health` probes bounded native registration and reports transport availability
without session or credential data, including rejection of stale socket files.
Select a **resident** session; the app must pass its actual absolute `cwd`.

The phone may also create a fresh session by selecting an existing absolute Mac
workspace directory. The request is native ACP `session/new` with `cwd`, empty
`mcpServers`, and empty `_meta`; arbitrary configuration, permission overrides,
caller-chosen session identity and injected tools are rejected. Directory
symlinks are resolved to an existing readable/searchable directory before
forwarding. This does not grant project trust or alter inherited policy.
The leader returns a new `sessionId` and its actual `currentWorkingDirectory`.
The phone must then perform the same strict resident-only `session/load` as for
Terminal-created sessions, obtaining the resident actor UUID before controlling it.
To use a phone-created session in Terminal, use its SID in the `--resume LIVE_SID`
command above with that workspace and the same native leader socket.

Only one unresolved create is forwarded per WebSocket connection. A lost response
is **uncertain**, not evidence of failure: reconnect and inspect the live picker
before deliberately creating another session. Neither bridge nor client should
retry creation automatically. Disconnecting the phone does not cancel a create
already accepted by the native leader.

Mobile-created sessions have a dedicated bridge-owned native subscriber. The
creator connection is retained before its SID is returned, so losing the sole
phone viewer, including before the creation response arrives, cannot trigger
native idle-session eviction. This owner only drains notifications; it never
answers permissions, sends prompts, grants trust or cancels work. Approvals remain
pending for a real client and replay when that client reconnects.

Ownership is bounded to **eight mobile-created sessions per bridge process**,
including pending creations. The ninth is rejected before another actor is
created. A creation/authentication error, explicit native session-end event or
leader connection failure releases its slot. Close a created session explicitly
from the Mac's session controls to release it. Merely leaving the conversation
or switching sessions does not close it. Existing Terminal-created sessions gain
no extra lifetime owner. The bridge keeps no history copy and never reconnects an
owner to a replacement actor.

**Keep the bridge running for phone-created session continuity.** Bridge shutdown
closes its owner connections without cancellation. Native Grok may then unload
an idle mobile-only actor; a bridge restart cannot preserve that live actor, and
strict mobile reattachment will refuse a saved SID whose actor has ended. Attach
Terminal to a created SID before bridge maintenance when uninterrupted residency
is required. Pending requests reserve their bounded slot until the native create
settles, that native connection ends, or the absolute 30-second native reply
deadline expires. A caller timeout never triggers another creation. If the native
creation result is unknown, the bridge closes the caller's transport instead of
returning a false definite rejection; the app requires checking Live sessions
before another creation. The abandoned owner connection is closed and its slot
released. Explicit native validation or authentication errors remain definite
rejections on the existing phone connection.

Native stdio MCP startup includes a bounded OS-launch phase. A process-wide limit
of four launches leaves the session runtime's blocking workers available for
prompt history and other controls. An identical launch in the same actor cannot
retry while its earlier OS call remains unresolved. The existing configured MCP
startup timeout covers admission and OS launch; failures identify occupied launch
capacity or an OS-launch deadline. Timeout or caller cancellation retains the
launch slot until the OS call returns, and a late child is still cleaned up by
the native process-group guard. MCP configuration, sandboxing and tool approvals
are preserved.

This contains a reproduced macOS process-launch stall; it does not establish why
macOS left those children in the fork/exec transition. Four unresolved launches
can leave additional stdio MCP servers unavailable until the OS calls return or
the leader is maintained. Their startup errors remain visible while conversation
and approval controls continue. Graceful native shutdown can still wait for an
OS-blocked launch; the launch deadline does not terminate that OS call. Do not
treat restarting a lost actor as live session recovery.

After attachment, raw ACP `_x.ai/commands/list` accepts the attached `sessionId` and optional
exact execution `cwd`; the native actor returns its current command/skill catalog
as `{commands:[{name,description,input?,_meta?}],tools?}`. A skill command has native
metadata such as `scope`, `path`, `bareName`, and `qualifiedName`. Use its advertised
`name` verbatim in a `session/prompt` text block, for example `/local:review args`;
the native resolver handles namespace collisions, eligibility and permissions.
The Rust extension handler's logical name is `x.ai/commands/list`, but ACP's codec
adds the leading underscore on the JSON-RPC wire. The bridge accepts the exact
native wire spelling only; the unprefixed form produces native Method not found.
`available_commands_update` supplies live catalog changes; pull the catalog after
attachment because historical catalog updates are omitted from native replay.
The bridge exposes no skill installation/toggle/config endpoints. It does not
expose `x.ai/skills/list`, whose implementation rescans disk and refreshes every
session's skill baseline; the session catalog already includes invocable skills.

Both leader and Terminal must start with `--permission-mode default` for normal
permission prompts. The pinned CLI uses the spelling
`default`. A leader started under `always-approve` can keep that mode in a resident
session even after an Ask/default Terminal reattaches; starting the leader correctly
is essential. This launcher never changes project/global settings or grants folder
trust automatically.

Grok also imports Claude-compatible policy. In this machine's global Claude
settings, `permissions.defaultMode` is `dontAsk`, which correctly denies unapproved
tools without showing an approval. The Grok CLI flag does not override that
separate policy. For the reviewed disposable test project only, we used
`.claude/settings.local.json` containing
`{"permissions":{"defaultMode":"default"}}` and explicitly trusted that workspace
with native `--trust` before creating a new session. Existing allow/deny rules and
hooks still apply. In your own workspace, review and merge that one mode setting
into existing local JSON only if you want interactive approvals; do not replace
existing settings or trust an unfamiliar repository. Untrusted folders intentionally
ignore project settings. A resident session retains its policy: create a new session
after changing the policy. `grok inspect --json` reports folder trust and policy sources.

## Protocol and lifecycle

WebSocket carries plain ACP JSON-RPC. Authentication is `Authorization: Bearer`
(preferred) or the upstream iOS-compatible `server-key` query parameter. HTTP access
logging is disabled so query credentials do not enter logs. Browser `Origin`
headers are rejected; no cookies or ambient browser authorization are accepted.

Local IPC frames are 4-byte big-endian lengths followed by a JSON envelope.
`register` uses `mode:stdio`, then waits for `registered` and `leader_ready`.
ACP envelopes are `{type:"acp",payload:"<JSON-RPC>"}`. The mobile registration
requests `user_message_echo:true`, preserving live user prompts on every subscriber.

The roster request is `_x.ai/sessions/list`, `params:{}`. Its result is nested:
`result.result.sessions[]`, with `sessionId`, `cwd`, `resident`, and `activity`.
For a forked worktree, roster `cwd` can be the original root used for display.
Before loading a selection, iOS resolves its execution workspace through
`_x.ai/session/info`, `params:{sessionId:target}`, using returned `cwd`.
Before each `session/load`, the bridge requests a fresh roster with
`params:{sessionId:target}`. Native leader routing subscribes this IPC client
before forwarding the request; the roster handler ignores that extra filter. This
subscription pins the actor against last-viewer disconnect eviction. The bridge
requires a resident matching SID and an absolute workspace; the native atomic
guard checks the actual workspace, rather than trusting roster display `cwd`. It sets
`_meta["x.ai/restore_code"]=false` and `_meta["x.ai/resident_only"]=true`,
and strips caller-controlled leader client identity. The native agent captures the
resident command channel before its first await and checks that channel again
before the cold-load branch and immediately before successful completion. A
missing, closed, or replaced actor fails, including explicit close during an
attachment await. Deliberate mobile `session/new` uses the constrained creation
path above; `session/resume` remains rejected. A leader
restart requires selecting a live session again; history alone is insufficient.
The bridge binds subsequent prompt, cancel and model controls to the returned
actor UUID. iOS retains that assertion across automatic reconnect. If the phone
missed an actor close while offline, loading the same saved SID with the old UUID
fails with -32602, `live session actor changed; select a running session`.
The app clears the stale attachment and requires explicit selection in the live
session picker. An explicit selection may attach the currently resident actor
and obtain its new UUID. It does not silently preserve the old live-session claim.

The leader unicasts replay to the attaching client and buffers concurrent updates.
The iOS app clears its presentation immediately before each `session/load`, requests
the full authoritative replay without a cursor, and deduplicates by exact
`_meta.eventId`. This rebuilds the screen while the same resident agent continues
running. The bridge also supports optional native cursors for other clients, but
some native cursors can replay the entire transcript; event IDs
can arrive out of numeric order. Clients must reconcile by exact ID, not discard
everything below a numeric high-water mark.
Bridge-private roster preflights are bounded (4 pending,
15 seconds); the four-request attachment limit also includes loads already
forwarded to the native leader. Their replies never reach iOS. At most 128 client
requests can remain outstanding on a connection. Prompt submissions are forwarded
to the native leader's queue; the bridge never retries a prompt automatically.

The transport deliberately exposes a small method set: initialize/authenticate,
session discovery/info, workspace/model discovery, billing reads, and live `session/load`, `session/prompt`, `session/cancel`,
and `session/set_model`. Prompt, cancel and model changes require a successful
attachment to that exact session on the same connection. Explicit cancellation
still cancels the shared turn; simply losing the phone connection never does.
Session creation, fork/import/rehydration, close/delete, credential access, permission
mode changes and shell-config writes are rejected. Phone Settings writes that
require those methods are unsupported by this narrow bridge. Companion config and
Mermaid rendering extensions are also unsupported: they do not exist in this pinned
source. Initialize accepts only standard ACP fields and the client identity/echo
metadata; it cannot inject startup hints or model configuration. Authenticate may
select existing Mac `cached_token` or `xai.api_key` methods, with no phone-provided
API key or reauthentication metadata. Discovery and billing take empty params. Attachment accepts
empty `mcpServers` and the replay/client-echo metadata only; it cannot replace the
live actor's tools or configuration, enable yolo/auto mode, or restore code. Native
resident-only attachment also preserves model, effort, context window, MCP client
servers, code-navigation and permission state, active telemetry turn number and
existing echo/status subscriptions. It does not run reconnect hooks, enqueue a new
system prompt, or reapply saved plan approvals. Live pending interactions come
from the leader's resident replay.

JSON-RPC IDs retain their JSON type, so integer `7` and string `"7"` are distinct.
Out-of-order replies are supported, and an ID may be reused after its response.
Reusing an ID while its request is pending closes that phone connection with
WebSocket 1008, rather than ambiguously resolving a request or executing it twice.
Already accepted work continues; reconnect for authoritative replay. The app does
not automatically resubmit prompts after an uncertain delivery. Invalid envelopes
receive an RPC error; malformed/non-finite JSON closes with 1007, binary frames
with 1003, oversized frames with 1009, leader shutdown with 1012, and a lost leader
connection with 1011. Leader unavailability, an incomplete registration, or a
version/protocol mismatch rejects the HTTP upgrade with 503. Missing/wrong secrets
return 401 and browser Origins return 403. Secret files must be owned by the user,
owner-only regular files; symlinks and FIFOs are rejected without following/blocking.
Native removal of the attached actor is forwarded, then closes that phone's
WebSocket with 4409, `Live session ended; select a running session`. This also
requires explicit selection instead of automatic attachment to a replacement.

Permissions (`session/request_permission`), questions and plan approvals use native
reverse request IDs. Their availability and UI support are described in the iOS
compatibility notes. Actual Simulator tests exercised native tool permissions,
structured question answers and full plan approval against the final patched
native build; exact coverage is recorded in `STRICT_LIVE_TEST_RESULTS.md`. The leader broadcasts requests to subscribers, accepts one
answer, and broadcasts `_x.ai/session_notification` `interaction_resolved` to clear
the modal everywhere. The bridge does not approve requests or change Terminal's
permission mode. Disconnect/background closes only that phone's IPC connection:
no `session/cancel`, `session/close`, process signal, or leader shutdown is emitted.
A phone reconnect establishes a fresh IPC client and reattaches with full replay.
Leader process crashes cannot be represented as uninterrupted live sessions.

LAN `ws://` provides authentication but **not encryption**. Use only a trusted LAN
for this first version. For remote access, keep the bridge bound to loopback and
use a properly authenticated encrypted VPN/tunnel, or a TLS reverse proxy with
certificate validation and WSS. Do not expose plaintext WebSocket to the Internet.
The Mac must stay awake and its leader must remain running.

## Automated tests

```sh
.venv/bin/python -m unittest discover -s . -v
```

Tests cover native registration readiness/version checks, wrong/missing secrets,
malicious browser origins, malformed JSON/params, live-only matching workspaces,
replay/cursor forwarding, approval response identity, bounded discovery timeouts,
typed/duplicate IDs and out-of-order replies, binary/oversize frames, forbidden
creation/policy methods, secret symlinks/FIFOs, dead leaders and executable/startup
failures, stale socket health reporting, disconnect without cancellation, and local
rebuild refusal on source/patch/lock/compiler drift or a failed build. Transport
tests use a fake leader
and do not by themselves prove model interoperability; see the delivery evidence
for the separately executed live CLI and Simulator runs.

## Native patch verification

The patch covers resident-only attachment, actor incarnation assertions for
mobile controls, capability advertisement, and shared interaction recovery.
A gracefully exiting Terminal can drop its permission response channel before
its IPC socket closes. The leader ignores only typed channel failures for a
still-pending shared interaction, leaving another client able to answer or
reconnect. Explicit denial/cancellation, semantic errors and unrelated transport
failures retain their existing behavior.
One pre-existing test module also needed `base64::Engine`
in scope for the targeted native test binary to compile; that test-only import
is included in the patch. Existing ACP clients retain legacy load semantics
unless they opt in; the bridge always opts in and never falls back.

Run from the repository root:

```sh
RUST_MIN_STACK=33554432 CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0 CARGO_INCREMENTAL=0 \
  cargo test --locked --offline -p xai-grok-shell --lib resident_only -- --nocapture
RUST_MIN_STACK=33554432 CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0 CARGO_INCREMENTAL=0 \
  cargo test --locked --offline -p xai-grok-shell --lib disconnected_interaction_response -- --nocapture
```

The targeted tests exercise the real session creation/load path with the existing
minimal agent fixture: exact actor reuse, missing actor refusal before reading
history, deterministic close during an attachment await without replacement,
replaced/closed command channels, wrong workspace, invalid flag, and unchanged
legacy opt-out. The exact actor test also checks running model/context/MCP/code-nav,
telemetry and existing echo/status subscriptions survive attachment. The 32 MiB
Rust test-thread stack is required by the existing debug agent fixture; production
uses its normal runtime. Real model/Terminal/mobile evidence is recorded separately.
The disconnect regressions use the actual ACP channel-drop errors and leader IPC
sockets. They exercise permission, question, plan and MCP interactions, including
another client answering, the last client disappearing and reconnecting, and
preservation of explicit rejection and unrelated errors.

For the real Terminal departure regression, use a disposable, already resident
session and its exact workspace (the probe writes a uniquely named one-line file
only after its surviving phone-protocol client explicitly allows once):

```sh
.venv/bin/python verify_terminal_detach_live.py \
  --url http://127.0.0.1:18789/ws --session LIVE_SID \
  --cwd /absolute/disposable/workspace --binary ../../target/debug/xai-grok-pager \
  --leader-socket ~/.grok/leader-mobile.sock --signal term \
  --allow-create-disposable-files --output /tmp/grok-terminal-detach.json
```

Repeat with `--signal kill` to cover an abruptly lost Terminal. Both runs launch
and stop only their own extra interactive Terminal; the original Terminal and
resident actor remain available.

## Session creation and native skills verification, 2026-10-20

The current immutable native artifact is
`target/verified-session-features-mcp-bounded/xai-grok-pager`, SHA256
`f0dc780dc99737421021144fab7953c8cedafa300ddb46aadabe2189940b96bc`.
The bridge directory passed **72 automated tests**, including the 17 pin/launcher
checks. The bounded MCP launch patch passed **six focused native tests**, covering
blocked launch/cancellation and actual child-process cleanup/scope. See
[evidence/session-features-bounded-mcp-tests.txt](evidence/session-features-bounded-mcp-tests.txt),
[evidence/session-features-bounded-native-build.txt](evidence/session-features-bounded-native-build.txt)
and the sanitized
[launch-starvation diagnosis](evidence/session-features-native-starvation-diagnosis.json).
The underlying macOS process-creation stall remains unresolved; containment keeps
history workers available, while four OS-blocked launches can delay subsequent
stdio MCP availability and shutdown can still wait for blocked OS calls.

The paired iOS app's separate
[live manifest](../../../grok-build-ios/tools/session-features-smoke/evidence/native-bounded-live/live-manifest.json)
records actual Simulator/native runs, exact app/native hashes, resident actor
identity and result bundles. Its
[demonstration](../../../grok-build-ios/DEMO.md)
shows session creation, native skill arguments and lifecycle/shared-client proofs.
Historical D419 and earlier physical-device results are labeled separately; signed
installation alone is not new-feature physical-device coverage.

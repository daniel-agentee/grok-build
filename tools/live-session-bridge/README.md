# Live Grok session bridge

## Named assistant profiles

Start a **profile-capable patched leader** and pass the exact tested executable
to the bridge with `--profile-cli /absolute/path/to/grok`. The bridge never finds
a profile executable through PATH. The executable must be a regular file owned
by the current user, executable, and unwritable by group/others. Existing leaders
remain attachable, but a leader without strict profile admission cannot create a
named-profile session. Initialize advertises `x.ai/agentProfiles` only when both
the leader capability and configured adapter are present.

Authenticated `_x.ai/profiles/list` accepts `{cwd: "/absolute/workspace"}` and
executes `grok profiles --cwd /absolute/workspace list --json` using the primary
leader's Grok home. This is a bounded, read-only catalog call; it never creates an
agent. The phone receives version/revision and display fields only: profile id,
revision, name, description, avatar, model, reasoningEffort, speed, skills,
ruleCount and valid. System prompts, rule bodies and credential references stay
on the Mac. Missing/old/untrusted executables and invalid workspaces return a
clear `profiles_unavailable` error. No CLI stderr is returned or logged.
The configured primary socket must live directly inside that leader's Grok home;
all sockets discovered by this bridge share that home. Listing and creation both
use the canonical absolute workspace. The helper inherits the bridge environment
so existing provider credential references keep working; it returns no credentials.
Deployments use an immutable, hash-receipted CLI release path. The executable and
its parent-directory chain must belong to the user or root and disallow writes
by other principals (a root-owned sticky temporary ancestor is safe). Symlinked
parent paths are resolved before execution; the executable itself cannot be a
symlink. These checks do not replace release verification or guard against the
owning user deliberately replacing their own release.
The catalog envelope is capped at 256 rows and 2 MiB. Malformed individual display
rows are disabled or omitted without hiding valid assistants. Browsing has two
helper slots and creation admission has its own slot; every helper is time bounded,
and a disconnected browsing client cancels and reaps its helper process group.
Keeper imports run in Python isolated mode. Spawn ownership is retained through
early or repeated cancellation until the keeper and native descendants are stopped.
A small keeper embedded in this pinned bridge owns that group and stays alive
until its control pipe closes. Native stdout is unchanged, native stderr is
discarded, and a separate bounded control pipe carries only the exit status.
Closing the control pipe makes the keeper signal its own current group, including
remaining descendants, and the bridge reaps it. The bridge never signals a saved
PGID after asyncio may have reaped its owner. The keeper closes its own output
pipes after the native child exits, so it cannot hold successful output open.

New clients send named profiles as `session/new._meta` containing only
`{agentProfile: "researcher", profileRevision: "native-profile-revision"}`.
Inline definitions and permission overrides are refused. The bridge refreshes
the canonical native catalog before creator dispatch, verifies validity and
revision, and leaves the profile's saved model/effort defaults intact. Native
admission must independently check the revision to close the catalog/create race
and echo the applied immutable identity in `_meta["x.ai/agentProfile"]` as
`{id,revision}`. The bridge **never invents that proof**. The phone verifies it on
create and strict load before enabling Send. Custom sessions keep the existing
advertised model/reasoning path. Profile edits affect future children/sessions;
attachment never replaces a running actor or its frozen profile.
For compatibility, explicit `modelId`/`reasoningEffort` selectors are also accepted
when they exactly equal the saved profile values. They cannot override a profile.
Native v1 has no separate disabled flag: availability is `validation.valid`; model
policy, missing references and other invalid native settings make it false. Native
admission remains authoritative and is checked again before any session is created.

Run the focused adapter suite with:

```sh
python3 -m unittest discover -s tools/live-session-bridge -p test_bridge.py -q
```

These transport tests include a protocol fixture and are supporting coverage,
not live model-provider interoperability evidence.

A small authenticated WebSocket transport for existing native Grok leaders.
It creates no `MvpAgent`, model connection, or conversation history store.
An explicit mobile **New session** request creates a fresh session on that existing
leader, with the Mac's normal workspace trust and permission checks.
The Terminal and iOS clients subscribe to the same resident session actor through
Grok's existing local leader, and tools execute on the Mac in that actor's workspace.
The session picker combines resident actors with open dashboard workspace members
from the configured primary's Grok home, preserving each live actor's source.
Older conversations have a separate **History** button and explicit resume action.

Terminal's **Idle** count includes open workspace conversations whose agent is no
longer loaded. They belong in the phone's overview, but they are not resident live
actors. The phone opens Idle conversations immediately with their full saved
history, without loading an agent. Backing out, reconnecting and viewing skills
remain read-only. The first new message deliberately resumes the session on the
configured primary, then sends that message once. History-only rows retain their
explicit **Resume saved convo?** confirmation. A newly restored actor is never
presented as the same uninterrupted live actor.

## Open-session discovery

`grok-dashboard-workspace` is a small read-only exporter in the native
`xai-grok-dashboard-store` crate. It uses the typed store API, opening the existing
`<primary-socket-directory>/dashboard/workspace.db` without schema initialization,
migration, journal-mode changes, or creating a missing store. Keep the primary
socket in its actual Grok home, normally `~/.grok`, so discovery uses the same
canonical workspace as Terminal.

The exporter matches Terminal's unloaded-member visibility: Build conversations
with a nonblank title, turn summary, or pin. Empty startup placeholders are hidden.
It exports only session identity and absolute workspace path. The bridge joins
those onto native roster rows by session ID plus normalized cwd, adding
`workspaceMember`; it does not create sessions or infer a live actor from history.
The native hard capacity is 256 workspace members.

Native activity deltas preserve this separate membership marker. They invalidate
the short aggregate cache without running an exporter lookup on the conversation
or tool stream. A failed membership refresh keeps the last known membership and
returns `bridgeWorkspaceMembershipError` so iOS can show a refresh notice. Real
live actors and the unknown-owner resume guard remain independent of this lookup.
A later successful read reconciles additions and removals.

The bridge checks the exporter's pinned SHA256, ownership, executable mode, and
absence of group/world write permissions before executing it. Symlinks are
rejected. Reads have a two-second deadline and a bounded output size; exited,
timed-out, oversized, and cancelled helper processes are reaped. Failures report
stable codes (`reader_unavailable`, `reader_untrusted`, `read_failed`,
`read_timeout`, or `invalid_snapshot`) without database contents or private paths.

## Idle history and workspace commands

Initialize advertises `x.ai/idleSessionPreview` and `x.ai/workspaceCommands` in
`result._meta`. The exact read-only native history method is
`_x.ai/session/updates`, with `{sessionId,cwd,offset,limit}`. Supply the canonical
session UUID and its absolute open-workspace path. Offset defaults to zero; limit
defaults to 500 and is restricted to 1–500. The response contains complete
timestamped `session/update` envelopes, `totalCount`, `hasMore` and optional
`lastEventId`. Page until complete and apply the ordinary update parser. Native
history filtering handles rewound branches. A fork may retain its parent's event
ID prefix; use the envelope's `params.sessionId` to choose the conversation.

Preview requires a freshly verified canonical dashboard member and matching
native roster row/workspace. Missing membership, a failed membership read,
unknown sessions, traversal, streaming/routing metadata and out-of-bounds pages
are rejected. Preview never calls load/create, creates no actor or retainer,
and cannot cancel ongoing work on disconnect.

`_x.ai/commands/list` also accepts `{cwd}` before session attachment, only for an
existing open Mac workspace. The native pre-session registry retains folder,
plugin trust, compatibility and skill eligibility checks. It performs no session
load and does not require an actor. Its response is display metadata; invoking a
command later uses the normal native prompt resolver and permissions.

The bridge shares in-flight metadata scans and reuses a successful result for at
most 15 seconds, with a 32-entry bound. Keys include the exact source, canonical
workspace, native socket incarnation, and cheap canonical trust/config file
stamps. Fresh membership is still required for every request. Native errors and
uncertain/expired reads are not cached. Deep plugin-file edits expire within 15
seconds; an explicit `{cwd,refresh:true}` bypasses the cached response immediately.
Refresh deliberately joins an already-running read for that same key and is
removed before forwarding to native. The optional flag is strictly boolean;
unrelated policy metadata stays forbidden. Attached `{sessionId,cwd?}` requests
continue to read their live actor's catalog.

Cold resumes are serialized and checked against all known live sources both
before and after retainer authentication. If Terminal resumed first, the bridge
returns `error.data.code=origin_conflict` with its exact source; refresh and
strictly attach the existing actor before sending once. Multiple live origins or
unknown source ownership fail closed. Cold mobile resumes use the configured
primary, as does `service.py terminal`. Native attach arbitration is atomic within
that leader; independent CLI cold resumes on another leader have no global
cross-process lock and can create distinct actors. Such a conflict is surfaced,
never merged by saved session ID.

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
build commands, and separate pager/exporter artifact digests. The launcher verifies the pager's
SHA256 before starting either process. It uses the isolated
`../../target/debug/xai-grok-pager`, preserving the globally installed CLI.

Earlier live tests used installed stock **1.0.62 (1474f9cbdd91)**, SHA256
`ec434e831afdcfce87780f2815fd732b1df8a4a8de96a491716ad75d13605a4e`.
Those results are historical and identified separately in the evidence. Stock
1.0.62 demonstrably cold-restored a session when explicit close raced attachment;
the final strict bridge refuses attachment to leaders that lack the new native
capability. Same-version/protocol leaders remain visible through authenticated
read-only roster discovery with an upgrade notice.
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
CARGO_PROFILE_DEV_DEBUG=0 CARGO_INCREMENTAL=0 cargo build --locked --offline \
  -p xai-grok-dashboard-store --bin grok-dashboard-workspace
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

That command rebuilds and pins both artifacts. To add or rebuild only the
observational exporter while preserving the already verified live pager binary:

```sh
python3 tools/live-session-bridge/pin_build.py --workspace-reader-only
```

Both modes verify the exact current native patch, lockfile, compiler, and target
before and after compiling. The helper-only mode leaves the pager checksum and
its recorded build provenance unchanged. `pager_artifact_inputs` and
`evidence/all-sessions-pager-artifact.patch` retain the inputs for the preserved
pager; the workspace exporter has its own source-input hashes in the manifest.
After the targeted tests and acceptance checks pass for a new build, preserve
the helper for the launch commands below:

```sh
mkdir -p target/verified-all-sessions-20261021
install -m 700 target/debug/grok-dashboard-workspace \
  target/verified-all-sessions-20261021/grok-dashboard-workspace
```

Then, from this directory:

The reproducible build writes `target/debug/xai-grok-pager`; the launcher checks
that output against the exact manifest hash. The current all-sessions verification
copy is `target/verified-all-sessions-20261021/xai-grok-pager`, SHA256
`27efdb05b8e0c1482ce4158db026e9b574be57f3a4674ee545ba17624754ebc8`.
The separate read-only exporter is preserved beside it as
`target/verified-all-sessions-20261021/grok-dashboard-workspace`, SHA256
`cb04f8888bf2b11ea602fe2249ff08f92ca533270e7a7d2d37ab31f360bb10c1`.
It was built from the current native patch without rebuilding or restarting the
preserved pager. The bridge finds a helper packaged beside `bridge.py`, then this
preserved copy, then fresh `target/debug` output, independently of the caller's
working directory. Every choice still requires the exact helper checksum.
The earlier session-feature copy remains
`target/verified-session-features-mcp-bounded/xai-grok-pager`.
The earlier demonstration executable remains separately preserved at
`target/verified-switching-final/xai-grok-pager`, SHA256
`d41925f463ab9b81fcb7ca55e759f193e7fa6f526f8fade8f8447a7f99c465ab`.
Its original manifest and patch are retained as `evidence/d419-build-manifest.json`
and `evidence/d419-resident-only-attach.patch`; its historical live evidence in
`STRICT_LIVE_TEST_RESULTS.md` does not verify the newer MCP containment patch.

## Durable Mac connection

Use the per-user login services for day-to-day operation. A detached transport
alone cannot recover when its native leader exits; that was the cause of the
October 22 HTTP 503 / **Connection failed** incident. The bridge remained alive
but its configured native socket had disappeared.

This Mac is already configured with the custom socket
`~/.grok/leader-mobile-recovery-20261022.sock`. From this directory, reuse its saved
configuration directly with the Python environment containing `requirements.txt`:

```sh
python3 service.py status
python3 service.py terminal --workspace /absolute/project/path
```

For a new installation only, after preparing pinned artifacts and an existing
owner-only bridge secret, install once. To reproduce this Mac's selected socket:

```sh
python3 service.py install --workspace "$HOME" \
  --leader-socket "$HOME/.grok/leader-mobile-recovery-20261022.sock" \
  --host 0.0.0.0 --port 18809
```

Do not reinstall a different configuration over an active native owner.

`install` registers `io.grok.mobile.leader` and `io.grok.mobile.bridge` in the
current user's `~/Library/LaunchAgents`. The native leader and WebSocket transport
have independent launchd ownership, start at login, and recover after process
exit. Each service execs its actual long-lived process; there is no detached
child whose launcher can exit successfully while the real owner disappears.
The 30-second launchd throttle prevents a fast failure from spinning. Interactive
QoS is deliberate: a Background job's pinned-artifact verification was measured
to starve on a busy Mac. Digests are streamed with bounded memory.

The default listener is loopback. `--host 0.0.0.0` explicitly enables the existing
authenticated trusted-LAN connection. The encrypted remote route forwards to
the same loopback port; it does not start another agent. HTTP 401 still identifies
an incorrect bridge secret. HTTP 503 means the configured native owner is not
ready; the service can recover it without changing the credential.

The configuration contains file paths and connection settings only, stored at
`~/.grok/mobile-bridge/service/service.json` with mode 600. Both roles revalidate
the exact native/helper checksums and existing credential permissions at startup.
No credential value is written to a plist, environment variable, command argument,
or diagnostic output. Cached native CLI authentication is reused. The controlled
runtime environment excludes inherited API keys and tool-runner overrides.

An already listening native owner is adopted without stopping it. A hung,
unready, or incompatible listener is preserved and reported; the watchdog never
unlinks a user socket or kills an owner. The install command refuses occupied
ports and changed service configurations instead of replacing active services.
Repeated installation of the exact existing configuration does not restart
either role. `service.py terminal` reads that configuration, so Terminal and phone
use the same configured native source even when a custom socket was selected.

The current Mac deployment retains the previously selected
`~/.grok/leader-mobile-recovery-20261022.sock`; it is recorded in `service.json`.
Use `service.py status` and `service.py terminal` directly on this installation.
Do not re-run a differing `install` command over an active configured owner.

A bridge or phone reconnect leaves an independently connected Terminal's native
actor running. A native process crash ends that live actor: launchd restores
connection availability, and saved conversations still require deliberate resume.
No service startup replays history or silently creates replacement actors.
The Mac must remain powered on, logged in, and awake for remote access.

Read status without exposing the launchd environment:

```sh
python3 service.py status
launchctl list | rg 'io\.grok\.mobile\.(leader|bridge)'
python3 -m unittest test_service test_launch -v
```

Never dump `launchctl print`: other services' environments may contain secrets.
Service logs are owner-only under `~/.grok/mobile-bridge/service/`.

## Manual development launch

These original foreground/detached commands remain useful for development. Use
the login services above for automatic recovery after native or bridge exit.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

For a single command that starts the pinned leader and bridge, then enters the
interactive Terminal UI:

```sh
.venv/bin/python launch.py --workspace /absolute/path/to/workspace --host 0.0.0.0 \
  --workspace-reader ../../target/verified-all-sessions-20261021/grok-dashboard-workspace
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
  --workspace-reader ../../target/verified-all-sessions-20261021/grok-dashboard-workspace \
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
`mcpServers`, and optional `_meta` model choices; arbitrary configuration, permission overrides,
caller-chosen session identity and injected tools are rejected. Directory
symlinks are resolved to an existing readable/searchable directory before
forwarding. This does not grant project trust or alter inherited policy.
The leader returns a new `sessionId` and its actual `currentWorkingDirectory`.
The phone must then perform the same strict resident-only `session/load` as for
Terminal-created sessions, obtaining the resident actor UUID before controlling it.
To use a phone-created session in Terminal, use its SID in the `--resume LIVE_SID`
command above with that workspace and the same native leader socket.

The default request omits model choices and inherits native configuration.
An optional Advanced form is available only when initialize advertises
`result._meta["x.ai/newSessionModelSettings"]=true`. Read the authenticated,
session-free `_x.ai/models/list` catalog; opening this form must not create or
attach a session. A deliberate `session/new` may supply only
`_meta:{modelId,reasoningEffort?}`. Use the advertised `modelId` and the selected
model's `_meta.reasoningEfforts[].value`, not its display label or option ID.
Canonical effort values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`
and `max`; the chosen model must also explicitly advertise the particular value.
The retained creator re-reads the catalog before dispatch, rejecting stale,
unsupported or malformed choices before native creation. Catalog failure returns
`error.data.code=settings_unavailable`; invalid choices return `settings_invalid`.
Permission, tool, trust and identity metadata remain forbidden.

Creation replies and strict-load replies contain the effective native `models`
and `configOptions`. A client must verify both against its explicit choices and
retain the returned SID even if attachment fails. A lost response is uncertain;
neither an attach error nor mismatched effective settings authorizes an automatic
second create. These settings configure the new actor only, leaving any existing
conversation attached while the form is open.

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

### Updating a bridge without unloading its idle mobile actors

Do not restart the old bridge to roll out a transport-only change. Its cleanup
closes retained creator sockets. When that removes an idle actor's last native
subscriber, Grok deliberately unloads the actor; an unchanged leader PID alone
does not prove session continuity. A working actor or parked approval survives
native detach, but maintenance must also preserve idle actors.

Use parallel bridges against the same native leader instead. Prepare a distinct
loopback-only bridge service, for example `127.0.0.1:18853`, using the same
`leader_socket`, `secret_file`, native version and workspace reader as the old
service. Register only its bridge role under a distinct launchd label; do not
install another leader or replace the old service configuration. Verify the new
listener with authentication and strict-load the expected actor UUID/workspace.
Source identifiers remain stable: they are HMACs of the same secret and absolute
leader socket path, independent of bridge port, process or WebSocket connection.

After verification and review, switch only the dedicated tunnel's exact `/ws`
ingress origin from `http://127.0.0.1:18809` to the new loopback port. Validate the
tunnel configuration and restart only its dedicated connector. The public WSS
address, bridge secret and native leader stay the same. This causes a brief
public reconnect; existing public sockets may close, then attach to the new
bridge with canonical replay. Local Terminal subscribers and the old bridge's
retained creator sockets stay connected throughout.

Keep the old bridge running while any actor it retained still exists. Without
an owner-export API, conservatively retain it until every pre-update resident
actor has explicitly closed; do not infer safety from an empty phone list or
idle status. The new bridge's ordinary attachment does not transfer creator
ownership. Rollback changes only the dedicated tunnel origin back to the still
running old bridge. Preserve pre/post source, session, actor UUID, absolute
workspace and event cursor baselines. Never resume saved history to repair a
failed identity check.

The October 22 isolated native check ran the prior bridge and this source in
parallel against one actual pinned native leader. All 12 assertions passed:
one prior-bridge-created idle actor remained identical while both phone sockets
disconnected, the new bridge strictly reattached it, prior history survived with
unique event IDs, and the old creator retained it after another phone detach.
The new bridge created no actor or retainer. This proof used zero model turns or
tools, left the public runtime unchanged, and is retained locally at
`~/.grok/mobile-bridge/viewport-rollover-20261022/blue-green-native-proof.json`.
It is not a new live permission-routing test; permission reverse-requests still
use the shared native leader and their original request IDs.

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

Initialize advertises `_meta["x.ai/promptCorrelation"]: true` only for a compatible
resident leader. Such clients may send `session/prompt` with optional
`_meta: {"promptId": "<canonical hyphenated UUID>"}`. UUID hex may use either
case; the bridge forwards the exact value together with its own verified
`x.ai/residentActorId`. All other prompt metadata, malformed IDs and correlation
metadata on cancel/model methods are rejected. This is correlation, not a retry
or idempotency token. Clients must allocate a fresh ID for every intentional send.
An older bridge omits the capability, so upgraded clients omit prompt metadata
and retain conservative recovery for an interrupted unacknowledged RPC.

Native queue snapshots are republished on resident attachment and ordered with
its canonical replay. Exact queue/running/completion prompt IDs can confirm an
uncertain send only after the load verifies the same actor. Text equality never
proves delivery, including two identical instructions from different devices.

The transport deliberately exposes a small method set: initialize/authenticate,
session discovery/info, workspace/model discovery, billing reads, and live `session/load`, `session/prompt`, `session/cancel`,
and `session/set_model`. Prompt, cancel and model changes require a successful
attachment to that exact session on the same connection. Explicit cancellation
still cancels the shared turn; simply losing the phone connection never does.
Unrestricted session creation, fork/import/rehydration, close/delete, credential access, permission
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

The installed remote endpoint is **`wss://grok-mobile-ds.agentee.io/ws`**. It uses
trusted TLS at Cloudflare and a dedicated outbound encrypted tunnel to this
Mac's existing bridge at `http://127.0.0.1:18809/ws`. The native leader, actor,
workspace, Mac authentication and approval checks remain on the original Mac.
Only the exact `/ws` path is exposed, and every WebSocket still requires the
existing bridge Secret. No router port forwarding or certificate exceptions are
needed. Use this stable endpoint in the phone's Setup to keep the same saved
connection when changing networks.

Direct LAN `ws://` provides authentication without transport encryption and
remains useful only on a trusted LAN. The dedicated remote route uses WSS; do
not expose the plaintext listener directly to the Internet. The Mac must stay
awake, online and logged into the user account that hosts the leader, bridge
and dedicated tunnel LaunchAgents. See [REMOTE_ACCESS.md](REMOTE_ACCESS.md) for
exact service ownership, launch commands, recovery and verification scope.

The independent M1 tests used public DNS, trusted TLS and this real WSS route
from another Mac on the home LAN. They verified authenticated discovery,
same-actor Terminal replay, abrupt reconnect, connector crash recovery and
wrong-secret rejection. They do not constitute physical iPhone cellular
verification; report actual device/mobile-data coverage separately.

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

## All sessions, source routing and saved resume, 2026-10-21

The directory follows native `discover_leaders_in`: it scans `leader*.sock` in
the configured primary socket's own directory and also includes that explicit
primary path. It does not recursively search arbitrary directories. The parent
must belong to the current UID and disallow group/world writes; sockets must
belong to that UID and cannot be symlinks. Discovery registers at most 32 sources,
examines at most 512 directory entries, probes sources concurrently with a
4-second deadline, and caches the aggregate for one second. Native saved-session
discovery returns at most its 200 recent saved entries per source; this is not an
unbounded archive browser.

Each roster row adds opaque `bridgeLeaderId`, a sanitized socket-basename
`bridgeLeaderLabel` (for example `Mac · bob`), and `bridgeAttachSupported`. A selected
WebSocket uses `/ws?leader=<bridgeLeaderId>`; it routes all session info, strict
attachment, replay, prompts, approvals, commands and cancel to that source. The
token is an HMAC of the socket path with the bridge secret. Client-supplied paths
are never accepted. A wrong token fails with HTTP 400 and a known unavailable
source fails with 503. A discovered source that requires a safe-attachment upgrade
rejects selection with 426. Fresh sessions are created only on the configured primary;
another selected source returns `primary_required` before creating anything.
Retained owners across all selected sources share the same eight-session limit.

The aggregate keeps two live rows when the same saved SID has distinct actors
on two leaders. New native rosters include optional `residentActorId`; legacy
compatible rosters may omit it, but strict load still returns and validates the
actor UUID. Live rows take precedence over saved rows for the same SID. Source
removal notifications are scoped by `bridgeLeaderId`, so one source cannot clear
another source's actor. Polling refreshes other sources while the selected
source supplies its normal streamed events.

Top-level `bridgeSources`, `bridgePartial` and `bridgeResumeBlocked` expose
partial discovery explicitly. Same-version/protocol leaders that lack strict
attachment capabilities are authenticated and read only for discovery. Their
rows remain visible with `bridgeAttachSupported:false` and
`bridgeAttachUnsupportedReason:"upgrade_required"`; controls are disabled, and
source status has `available:true`, `ownershipUnknown:false` and
`error:"upgrade_required"`. Their known live ownership still prevents duplicate
history resume on another source. Discovery never sends them a load or prompt.
A never-live stale socket gives a warning but does not block saved resume.
A version/protocol-incompatible or authentication-failing source or
loss of any previously inspected source blocks saved resume globally: ownership
is unknown. Previously known live SIDs from an unavailable source cannot be
relabeled Saved. Other compatible live actors remain attachable. For example,
the existing `leader-bob.sock` without strict-attach capabilities has two live
rows visible as requiring an upgrade on the actual phone endpoint; it is
preserved, not restarted or bypassed. Use a separate owner-controlled socket directory when an isolated
compatible fleet is needed. Do not move active socket files.

Saved history is resumed only after deliberate confirmation with
`session/load`, `_meta["x.ai/resumeSaved"]=true`, and its existing absolute roster
workspace. This starts a new actor from history; the UI must describe it as
Resume, distinct from attaching an existing live actor. Resume rejects old actor
assertions, cursors, policy/tool overrides and wrong workspaces. A global lock and
fresh directory check before and after retainer authentication prevent concurrent
resume requests through this bridge from creating duplicate actors. An independent
cold resume in Terminal or another bridge can race the final lookup: there is no
cross-leader transaction. If that deliberately creates two live actors, discovery
keeps both origins separate and requires explicit selection; strict live attachment
still cannot replace an actor with saved history. A newly discovered live owner returns
`origin_conflict`; partial unknown ownership returns `source_unavailable`.
Neither case falls back to history automatically.

The retainer performs native cold load, gets the actual execution workspace from
`_x.ai/session/info`, then proves the resulting UUID with strict load before the
phone's strict attachment and authoritative replay. Native cold load responses
omit `sessionId` and actor UUID, so the bridge uses the requested SID (rejecting
a conflicting returned SID) and performs this explicit identity proof. Returned
`_meta["x.ai/bridgeExecutionCwd"]` communicates an actual worktree execution path
to the app. A resumed actor survives phone disconnect under the same bounded
ownership rules as a fresh mobile session. Keep the bridge running for that
continuity. Unknown native outcomes require roster reconciliation, never retries.

The current source patch/manifest were input-verified and the first successful
native build was archived before further verification. The new optional roster
UUID has 13 native roster-merge checks; 91 Python tests cover directory bounds,
symlink replacement, source loss, routing, collision, worktree resume, simultaneous
resume and existing transport/launcher security and lifecycle behavior.

Run the real two-leader proof from the repository root using existing cached Mac
authentication. It creates and cleans up only its own disposable native leaders,
workspaces and sessions, with default permissions. It does not send model prompts:

```sh
python3 tools/live-session-bridge/verify_directory_live.py \
  --binary target/verified-all-sessions-20261021/xai-grok-pager \
  --output /tmp/grok-all-sessions-native-live.json
```

[Native live evidence](evidence/all-sessions-native-live.json) proves two-origin
discovery/strict attachment, deliberate same-SID/distinct-actor collision,
saved resume retained after phone disconnect without duplicate resume, real
source loss refusing history fallback, and wrong-secret rejection. It is separate
from model-turn/Simulator/physical-device evidence in the paired iOS repository.
[Deployment evidence](evidence/all-sessions-18809-deployment.json) confirms the
actual phone endpoint was updated without replacing its original Terminal actor.
It records only counts and disposable fixture rows from other sources.

## Earlier session creation and native skills verification, 2026-10-20

The earlier immutable native artifact is
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

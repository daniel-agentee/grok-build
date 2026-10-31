# Specialist profiles and delegation policy

Native Grok `AgentDefinition` markdown under `~/.grok/agents/` is the profile
source. Grok resolves prompt/rule references and snapshots the admitted child
configuration; the Hub is an editor for this same source, not another registry.

The parent makes routing decisions using the five rules in
[`pick-right-subagent-and-assign-task`](skills/pick-right-subagent-and-assign-task/SKILL.md).
Native admission checks the selected model, reasoning, speed and policy mode.
It refuses invalid settings instead of silently choosing a substitute.

## Build and start

The compatible native source is Grok Build **1.0.45**, based on official commit
`2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8` (`SOURCE_REV`
`559751fdcec02d413e4c57c8832ab275e4f44980`), plus this checkout's shared-session
and specialist-profile changes. The original iOS repository's `UPSTREAM_PIN`
is not the native build input. The tested compiler is Rust/Cargo **1.94.0**.
R9 is the compatible native/bridge revision for queue recovery and
client-generated prompt IDs; the immutable release receipt records its exact
clean source commit and executable SHA256.
Use this branch and its `Cargo.lock`; a feature-only diff applied to stock Grok
does not include the earlier shared-session prerequisites.

From this native checkout:

```sh
cargo build --locked -p xai-grok-pager-bin --bin xai-grok-pager
install -m 755 target/debug/xai-grok-pager /private/durable/path/grok
/private/durable/path/grok --version
python3 scripts/profiles/install-routing-skill.py
/private/durable/path/grok profiles --cwd "$PWD" list --json
/private/durable/path/grok --agent cfo --cwd /absolute/workspace
```

For this acceptance run, compilation and Rust tests run on `m1-worker`, using
strict known-host SSH, one build job and no incremental compilation. Copy only
the pinned tracked checkout/lockfile to that host; keep Mac login and provider
credentials on the Mac. The guarded build requires at least 40 GiB free before
starting, stops below 16 GiB free or above 24 GiB in its owned target, and uses:

```sh
CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0 CARGO_TARGET_DIR=/absolute/owned/target \
  cargo +1.94.0 test --locked -p xai-grok-shell -p xai-grok-pager -p xai-tool-types -p xai-grok-agent --lib --no-run
CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0 CARGO_TARGET_DIR=/absolute/owned/target \
  cargo +1.94.0 build --locked -p xai-grok-pager-bin --bin xai-grok-pager
```

The release receipt and SHA-checked test logs identify the exact invocation,
source manifest and scoped test selectors. Transfer the stripped executable
back once, verify its SHA256, and launch it against existing Mac authentication.
Headless live fixtures require at least 9.5 GiB free before launch, an 8 GiB
runtime floor, at most 1 GiB owned growth and a 45-minute lifetime.

A named profile owns its saved model and reasoning defaults. Do not combine
`--agent cfo` with `--model` or `--reasoning-effort`; edit the profile first or
start General. A normal stock Terminal harness keeps its ordinary model controls.
General subagents expose `model`, `reasoning_effort` and `speed` to the parent;
named subagent defaults may be selected explicitly through those same fields.
Only catalog-supported choices pass native admission. When the authenticated
catalog advertises it, `fast` uses its `grok-4.7-build-fast` variant; it does not invent a provider
service tier. Other models use `standard` and reject unsupported fast requests.
The fast-only variant advertises only `fast`; explicitly requesting `standard`
with that variant is refused without substituting the base model.
Fast routing also requires the base and fast variant to use the same provider
endpoint; a model name alone never authorizes a provider change.
Check every advertised model/reasoning/speed combination in the acceptance
account's current live catalog before selecting a Fast test route. A catalog
without an authenticated Fast route is a capability limitation, not proof of
Fast inference; no silent standard fallback or additional provider credentials
are used to bypass it.
With a first-party-only catalog, parent model selection requires
`[features] subagent_model_inheritance = false` in native config. An enabled
inheritance restriction continues to hide and refuse explicit parent model
selection. The profile launcher and its separate leader set a process-scoped
soft configuration overlay to expose model choice. This does not edit the
canonical config, permissions or approvals, or change existing leaders.

### Reversible Terminal launcher and shared primary

The npm `grok` command still runs its own installed binary. A frozen custom build
does not replace it automatically. After the reviewed rollout, use a separate
`grok-profiles` launcher with the following contents, replacing the executable
path with the frozen artifact whose SHA256 and source commit were verified:

```sh
#!/bin/sh
GROK_CONFIG='{"features":{"subagent_model_inheritance":false}}'
PATH="/absolute/frozen/path:$PATH"
export GROK_CONFIG PATH
exec /absolute/frozen/path/grok --no-auto-update --leader \
  --leader-socket "$HOME/.grok/leader-profiles.sock" "$@"
```

Keep this launcher separate from the npm-managed command and home binary. Start
the reviewed executable's leader on that same explicit socket:

```sh
GROK_CONFIG='{"features":{"subagent_model_inheritance":false}}' \
PATH="/absolute/frozen/path:$PATH" \
/absolute/frozen/path/grok --no-auto-update \
  --leader-socket "$HOME/.grok/leader-profiles.sock" \
  agent leader --no-exit-on-disconnect --relay-on-demand
grok-profiles --cwd /absolute/workspace
grok-profiles --agent cfo --cwd /absolute/workspace
```

Configure the phone bridge's primary source to this socket and its `--profile-cli`
to the same executable. Configure the Hub's `AGENTEE_GROK_PROFILES_BIN` to the same
artifact. Preserve older leader sockets as additional discovery sources; their
resident conversations stay with their original owners.

The frozen directory must contain the executable as `grok`. Its scoped `PATH`
lets the routing skill's `grok profiles ...` command use the same tested CLI in
the parent and children; the user's ordinary npm command stays unchanged.

`grok-profiles --resume SESSION_UUID --cwd /absolute/workspace` resumes on this
profile leader. Use it only for a conversation whose actual source is this
leader, or for an explicitly cold saved conversation with no resident owner.
For a conversation already resident on an older source, attach using that
source's executable and explicit socket with `--leader --resume SESSION_UUID`.
Never point a resume at a different leader merely because the saved UUID matches:
there is no global lock across independent leaders. The phone's source-aware
attach/resume path detects ownership conflicts and must retain the actual source.
The overlay replaces any other process-level soft overlay for this entry point;
ordinary user, project and managed configuration still loads, and protected
requirements retain precedence. Removing the optional launcher reverses this
Terminal entry-point change.

For the shared phone/Terminal surface, start the patched leader and bridge as
described in [the bridge launch guide](../tools/live-session-bridge/README.md),
with `--profile-cli /private/durable/path/grok` pointing at the same frozen
executable. Initialize must advertise `x.ai/agentProfiles: true`; a stock or
older leader cannot create a named-profile session. A successful native
`session/new` and strict `session/load` return the admitted `{id,revision}` in
`_meta["x.ai/agentProfile"]`. The phone checks this proof before enabling Send.
The bridge exposes only display metadata; prompts and credential references stay
on the Mac. Keep the existing authenticated WebSocket/secure remote-access path.

The native catalog supports revision-checked `profiles apply` and
`profiles policy apply`, each reading JSON on stdin. No secret belongs in a
command argument. Writes take a lock and replace atomically; a missing/stale
revision refuses the edit. Profile revisions cover the exact referenced role
and rule bytes, including source frontmatter, while only instruction bodies are
sent to the model. Inputs and the complete frozen prompt are limited to 1 MiB.
Avatar values are short emoji sequences or HTTPS URLs with no credentials,
query parameters or fragments.

## Execution and recovery

The spawned child snapshots its definition, resolved runtime settings and
execution metadata. Later profile/policy edits affect subsequent spawns only.
Resident parents refresh the selectable profile IDs and descriptions for their
next model request after an atomic profile edit. This preserves the parent's
conversation, workspace and permission policy; it does not rebuild the agent.
Verbatim forks keep their captured tool schemas, and previously admitted children
keep their frozen definitions. Disabled and parent-disallowed profiles remain
excluded from the refreshed menu.

Custom profile IDs are case-sensitive: `CFO` and `cfo` can identify separate
sources, prompts and credential bindings. Native and plugin lookup must match
the definition's declared ID exactly, including on case-insensitive macOS
filesystems. Parent allowlists and denylists use the same exact custom IDs;
built-in aliases retain their existing case-insensitive policy matching.
Resume and background wake reuse the child's frozen definition and actual
provider/model/reasoning/speed; requesting new settings on resume is refused.
Routing-mode changes or a malformed new policy do not stop already admitted
children from resuming. Legacy children without execution snapshots retain their
source-model recovery through the current catalog; their historical endpoint
cannot be proved. Later policy/profile effort and speed defaults are not applied
to them. New snapshots also store an opaque normalized endpoint/backend
fingerprint, never a raw endpoint or provider key. Repointing a catalog alias,
including a different port/path on the same host, refuses resume/wake before
workspace creation. Incomplete profile snapshots with execution metadata but
no endpoint fingerprint refuse recovery; start a fresh child instead.
An unavailable pin, unsupported effort/speed, invalid routing mode, unreadable
reference or provider lacking its own credentials returns a clear refusal before
workspace/child creation. The parent may inspect the current catalog and make an
explicit valid retry. No automatic provider fallback is performed.

Runtime `subagent_spawned` updates and foreground/background tool results carry
the actual execution fields, so Terminal and phone can show the admitted
settings even when they differ from a display default. `subagent_finished`
identifies the same child/attempt; clients retain that attempt's admitted
settings rather than expecting a second execution payload. Metadata
also reaches the parent's model-visible completion, admitted failure, background
wake and polling results as a `<subagent_execution>` JSON block. These paths use the same metadata
formatter as the display result, so the parent can report what actually ran.
Metadata never contains provider credentials. The original permission and tool restrictions
still apply; profile identity is not a credential sandbox or an authorization to
send Slack messages.

### Live queue recovery

`session/load` (including `x.ai/resident_only`) and `session/resume` republish
the same resident actor's current `x.ai/queue/changed` notification after
history replay. It includes the accepted `entries[].id`, version, owner, kind
and position, plus `runningPromptId` and its display fields. An empty snapshot
is authoritative and clears completed/cancelled queue rows. Queue notifications
are transient; replaying saved conversation lines alone cannot reconstruct them.

The actor samples and enqueues this snapshot under its queue-state lock,
without a network await. A change during history replay is therefore sampled
at mailbox service time. The leader subscribes the loading client and buffers
live notifications before forwarding load; it flushes snapshots and subsequent
mutations in order after the load response. Replay event-ID cutoffs do not
discard these unsequenced snapshots. Queue text stays scoped to subscribers of
that session, including its already-attached Terminal.

The authenticated phone bridge accepts an optional canonical UUID string at
`session/prompt._meta.promptId`, forwards it alongside the bridge-owned
`x.ai/residentActorId`, and rejects other client metadata/overrides. Native
queue/running updates and persisted user-message metadata preserve this prompt
identity. The phone reconciles ambiguous delivery only against the same source,
session, verified actor and prompt ID, so identical prompt text is insufficient
to confirm a different send. Reconnect never resubmits an ambiguous prompt.

The isolated acceptance harness boots a real interactive Terminal and native
leader in a disposable Git workspace using the Mac's existing authentication:

```sh
python3 scripts/profiles/verify-native-profiles-live.py \
  --binary /private/durable/path/grok --root /tmp/grok-profile-acceptance \
  --keep-open
```

The fixture root must not already exist and must be short enough for a Unix
socket. It copies only allowlisted model metadata and environment-variable
references, not provider secrets. Its private `fixture.json` identifies the
leader, Terminal, workspace and session for mobile verification; its secret file
remains mode 0600. Create `<fixture-root>/finish` to stop only this fixture.
The harness records observed execution metadata and Terminal output; protocol
fixtures/unit tests alone are not proof of live provider or physical iPhone use.

```sh
grok profiles --cwd "$PWD" list --json
grok profiles --cwd "$PWD" --scope user get cfo --json
grok profiles --cwd "$PWD" policy get --json
python3 scripts/profiles/import-claude-profiles.py --identities /private/path/identities.json --seat cfo
python3 scripts/profiles/import-claude-profiles.py --identities /private/path/identities.json --apply
python3 scripts/profiles/install-routing-skill.py
python3 -m pytest scripts/profiles/test_profile_assets.py -q
```

To prepare all eight requested choices, import the six Claude seats and the
committed Researcher template with the same native compare-and-swap path:

```sh
GROK_HOME=/absolute/target/grok-home python3 scripts/profiles/import-claude-profiles.py \
  --grok /absolute/frozen/path/grok --cwd /absolute/workspace \
  --identities /private/path/identities.json --model inherit \
  --include-researcher --apply
GROK_HOME=/absolute/target/grok-home /absolute/frozen/path/grok \
  profiles --cwd /absolute/workspace list --json
```

General is the reserved compiled native `general-purpose` profile, already
installed with the executable. Do not create a user file that shadows it.
Researcher comes from [`templates/researcher.json`](templates/researcher.json),
with inherited model, medium reasoning and standard speed. The six source roles
currently request `claude-opus-5-5`; the explicit `--model inherit` above is a
visible adaptation when that model has no authenticated route. The original
model remains recorded in each referenced Claude role file. Inherited settings
still pass native runtime admission: standard speed on an inherited fast-only
model is refused, so select a supported parent route or explicitly edit the
profile before spawning. `GROK_HOME` selects the write target; normal discovery
can still see existing user/project definitions, so inspect the returned scope
and paths before applying. Installation does not activate persistent seats.

The importer requires PyYAML. Preview is the default. Apply validates every
definition before writing, uses native compare-and-swap per profile and refuses
to replace an existing profile unless `--update-existing` is explicit. A later
conflict stops the batch and reports which earlier profiles were applied.
The importer defaults to explicit `--scope user`: roles are installed under
the configured Grok home and are visible from every workspace. `--scope project`
is an intentional workspace override. Native `list/get --scope user|project`
returns the selected source even when another scope shadows it; apply uses
`writeScope` and compares the destination's revision. Runtime effective discovery
gives project definitions precedence over user definitions of the same name.

CFO, CoS, CRO, COO, CTO and SDR wrappers reference their current Claude role
files and company rules, preserving one authoritative prompt. The five C-level
profiles also reference their shared C-level rules; SDR keeps its original scope.
They retain the role's current model and effort, use standard speed and native
default permissions. Launch flags and permission bypasses are not imported.
An optional `--model` is an explicit catalog override; an unsupported source
model is refused rather than remapped. Identity and configured avatar come from
an explicit secret-free import input, never baked-in personal Slack IDs or
guessed local images. The JSON input maps seat IDs to the native identity
object (`displayName`, `avatar`, `slack`). It is a one-time import input; native
profile files remain the runtime source.

Role workspace directories remain available as reference context. Historical
workspace-card startup/model/permission prose is not copied into the profile.
The child's actual workspace is inherited from its parent and assigned task;
selecting a CFO profile does not change the parent's or child's working directory.
The wrapper states the Grok platform adaptation explicitly: the canonical five
routing rules supersede provider-specific model hints in Claude references.
Historical seat launch, permission and Slack-watchdog prose does not change the
native admitted settings or register a newly spawned child as that persistent
seat. Existing seats, inbox routing and bot watchers are preserved.

Slack identity uses `botKey`, `botUserId` and the explicit matching reference
`seat-bot:<botKey>`. Imported C-level roles use their existing
`~/.agentee-hub/bin/seat-bot.py <botKey>` workflow. This helper loads only the
selected bot's token from its private `~/.grok/<botKey>-slack.env`, using the key
`<BOTKEY>_SLACK_BOT_TOKEN`; the CRO key is `cro`, although Slack displays its
handle as `cso`. There is no default-bot fallback. Before child/workspace
allocation, Task admission checks the matching binding, ownership, file type and
permissions, then runs a bounded local helper/token-readiness probe. Network
calls are blocked before the trusted Python helper is imported, and subprocess
stdout/stderr are discarded. This is a local readiness check, not Slack
authentication or an OS sandbox for arbitrary helper code.
The same admission check protects inline, named and CLI/config primary-session
creation before persistence/workspace setup, so refusal leaves no empty saved
conversation. A nonserializable 30-second proof tied to the exact profile source,
revision and bot identity avoids importing the helper twice during one admission;
it is never accepted from JSON or retained across persisted history/reconnects.
Profile lists, command loading and the ACP handshake do not run credential probes.

Portable profile validation still accepts reference-only `env:NAME` and
`op://vault/item/field` syntax, without reading credentials or implying runtime
readiness. Task admission refuses those unsupported bindings and mismatches
cleanly; the parent must select a supported profile or explicitly correct its
binding and retry. The importer requires the matching `seat-bot:` binding and
never reads token files. The runtime prompt names the exact selected helper and
its supported commands with message text on stdin. Identity metadata neither
authorizes sending nor registers a persistent seat, changes inbox routing or
provides credential isolation. Existing permission checks remain in force.
Avatars accept the native bounded emoji form or an HTTPS URL without credentials,
query parameters or fragments.

Policy modes are `automatic` (named profiles and General), `profiles-only`, and
`general-only`. Defaults fill absent settings. Edits apply to future child
admission; they do not change running children.

The installer preserves prior local skills under `~/.grok/backups/skills/`
(outside skill discovery), then links
both the canonical skill and the short `/sub` pointer into `~/.grok/skills/`.
Keep the checkout available while the links are installed, or run the installer
from a durable checkout after merging.


### Isolated live fixture and Terminal handoff

`scripts/profiles/verify-native-profiles-live.py` launches only its owned leader
and Terminal process groups in an owner-only disposable directory. It forwards
basic process/locale variables and the existing cached-auth file reference;
provider, Slack, 1Password and other service credentials from the launching shell
are excluded. Optional model metadata and credential variable **names** can remain
in the private config, but their environment values are unavailable. The fixture
does not turn off safety hooks or lower its default permission mode. Its startup
trust handler recognizes only the exact welcome question and disposable workspace,
and consents once before waiting for the interactive prompt. It never approves a
tool request automatically.

For `--keep-open` verification, the coordinator must first obtain exclusive prompt
ownership from the current tester. Publish a request with the module's
`write_prompt_request(root, {"id": "unique-id", "marker": "EXPECTED-ACK", "prompt": "..."})`
helper. It uses an owner-only temporary file and atomic publication, refuses to
overwrite a pending request, and never places prompt text on argv. The fixture
atomically claims each request before reading it. Partial JSON, non-objects,
invalid IDs/control characters, duplicate IDs, symlinks and files with broad
permissions are discarded with a content-free refusal; they do not stop the
fixture or remove a newer replacement. Successful results are published atomically
as owner-only `prompt-result-<id>.json` files. Test receipts distinguish these
fixture/helper checks from live-agent, Simulator and physical-device results.

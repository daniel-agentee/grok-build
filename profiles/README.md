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
is not the native build input. The local compiler is Rust/Cargo **1.94.0**.
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

A named profile owns its saved model and reasoning defaults. Do not combine
`--agent cfo` with `--model` or `--reasoning-effort`; edit the profile first or
start General. A normal stock Terminal harness keeps its ordinary model controls.
General subagents expose `model`, `reasoning_effort` and `speed` to the parent;
named subagent defaults may be selected explicitly through those same fields.
Only catalog-supported choices pass native admission. In this version `fast`
uses the catalog's `grok-4.7-build-fast` variant; it does not invent a provider
service tier. Other models use `standard` and reject unsupported fast requests.
The fast-only variant advertises only `fast`; explicitly requesting `standard`
with that variant is refused without substituting the base model.
Fast routing also requires the base and fast variant to use the same provider
endpoint; a model name alone never authorizes a provider change.
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

Runtime `subagent_spawned`/`subagent_finished` updates and foreground/background
tool results carry the actual execution fields, so Terminal and phone can show
the admitted settings even when they differ from a display default. Metadata
never contains provider credentials. The original permission and tool restrictions
still apply; profile identity is not a credential sandbox or an authorization to
send Slack messages.

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

Slack identity uses `botKey`, `botUserId` and an `env:NAME` credential reference.
The importer never reads token files. Identity metadata does not authorize a
Slack send or provide credential isolation. The runtime must resolve references
in its approved credential path and preserve permission checks.
For the imported C-level roles, their existing source instructions use the shared
`~/.agentee-hub/bin/seat-bot.py <botKey>` workflow. That helper loads the selected
bot's token internally from `~/.grok/<botKey>-slack.env`; its key is `cro`, even
though Slack displays the CRO bot's handle as `cso`. A generic profile's custom
credential reference still requires an authorized workflow capable of resolving
that reference. Setting metadata alone does not create a new Slack integration.

Policy modes are `automatic` (named profiles and General), `profiles-only`, and
`general-only`. Defaults fill absent settings. Edits apply to future child
admission; they do not change running children.

The installer preserves prior local skills under `~/.grok/backups/skills/`
(outside skill discovery), then links
both the canonical skill and the short `/sub` pointer into `~/.grok/skills/`.
Keep the checkout available while the links are installed, or run the installer
from a durable checkout after merging.

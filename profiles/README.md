# Specialist profiles and delegation policy

Native Grok `AgentDefinition` markdown under `~/.grok/agents/` is the profile
source. Grok resolves prompt/rule references and snapshots the admitted child
configuration; the Hub is an editor for this same source, not another registry.

The parent makes routing decisions using the five rules in
[`pick-right-subagent-and-assign-task`](skills/pick-right-subagent-and-assign-task/SKILL.md).
Native admission checks the selected model, reasoning, speed and policy mode.
It refuses invalid settings instead of silently choosing a substitute.

```sh
grok profiles --cwd "$PWD" list --json
grok profiles --cwd "$PWD" --scope user get cfo --json
grok profiles --cwd "$PWD" policy get --json
python3 scripts/profiles/import-claude-profiles.py --identities /private/path/identities.json --seat cfo
python3 scripts/profiles/import-claude-profiles.py --identities /private/path/identities.json --apply
python3 scripts/profiles/install-routing-skill.py
python3 -m pytest scripts/profiles/test_profile_assets.py -q
```

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
files and shared C-level/company rules, preserving one authoritative prompt.
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

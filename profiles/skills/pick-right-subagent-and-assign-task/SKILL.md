---
name: pick-right-subagent-and-assign-task
description: Select a named specialist profile or General subagent, then choose a supported model, reasoning effort and speed for delegated work. Use before spawning or retrying a child, including /sub, research, implementation and C-level role tasks.
---

# Pick right subagent and assign task

The parent makes the routing judgment. The native Grok catalog and admission
validator establish which routes actually work. Read the current workspace's
catalog with `grok profiles --cwd <absolute-workspace> list --json`; it includes
the active delegation policy, profile definitions, model capabilities and
availability. The Hub editor writes this same native configuration.

## The five routing rules

1. **Profile:** use a named profile when its role, context or identity fits the
   task; otherwise use **General** (`general-purpose`). Respect the current
   routing mode: `automatic` admits both, `profiles-only` admits named profiles,
   and `general-only` admits General. An excluded route is refused.
2. **Model:** the parent chooses the best available model for the task. Prefer
   a smaller model for bounded work and a stronger model for ambiguous or
   consequential work. Use the current catalog and profile defaults rather
   than a copied model list. A user's explicit choice constrains the selection.
3. **Reasoning:** choose low for routine work, medium by default, and high for
   difficult work, using an effort supported by the chosen model.
4. **Speed:** select supported fast speed when latency matters; otherwise use
   standard. A separate Fast model and a provider's fast service tier are
   different capabilities; consult the catalog for the chosen model.
5. **Validity and evidence:** use only allowed, available settings. On refusal,
   inspect its valid alternatives and explicitly choose a valid retry; never
   silently substitute a provider, model, effort, speed or identity. Report the
   child's actual admitted settings from runtime evidence, including any
   unavailable or unverified provider speed, rather than repeating the request.

## Assigning the task

Use Grok's native `spawn_subagent` with the selected `subagent_type`, explicit
`model`, `reasoning_effort` and `speed` fields advertised by the installed tool.
The native tool may offer models from several providers. Provider transport and
credential lookup stay inside the runtime; the task prompt never contains keys.
If the installed tool lacks a required field, report that incompatibility and
use a compatible build before claiming the setting was applied.

Give the child a self-contained task: its goal, relevant workspace and source
paths, context already established, constraints and observable done criteria.
The profile supplies system instructions, rule files, skill references and
identity. A Slack bot reference describes the intended identity; it grants no
authority to send messages and does not by itself sandbox credentials.

Profile and policy edits affect future children. An existing child's admitted
configuration and source snapshot stay fixed for that child's lifetime.
Check the returned profile revision and actual provider/model/effort/speed in
Terminal or the phone's child details when verifying the result.

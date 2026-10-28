---
name: sub
description: Delegate a task with Grok native subagents. Use for /sub, spawn a sub, delegate this, or coordinator handoffs. Load the canonical pick-right-subagent-and-assign-task skill to choose the profile, model, reasoning and speed.
---

# Delegate a task

Read `~/.grok/skills/pick-right-subagent-and-assign-task/SKILL.md` and follow its
five routing rules before calling the installed native `spawn_subagent` tool.
That file is the sole routing policy source. It replaces the former Grok-only
model-inheritance guidance; supported providers come from the native catalog.

Use a self-contained task prompt and verify the child's returned result.

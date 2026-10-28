# Profile bridge review follow-up

This is the ISSUE-3352 repair ledger, not an independent acceptance result.

## Repaired findings

- Individual malformed display rows are omitted or disabled through a bounded
  display projection. A malformed definition, identity or validation cannot hide
  valid profiles, disclose nested prompt/credential data, or become admission.
  The untouched native row and exact revision are checked again before creation.
- Creation has a dedicated helper slot, independent of the two browsing slots.
  Spamming catalog reads cannot consume admission capacity. Helpers run in their
  own process group; timeout, oversize, cancellation and disconnect kill/reap the
  group before completion. Tests assert exact errors and reaped process ids.
- R2 removed SIGKILL to saved process-group ids. An embedded keeper remains the
  group leader, reports native status separately, closes its output after native
  exit, and kills only its own current group when the parent control pipe closes.
  Asyncio may reap the keeper at any time without making the bridge signal an
  unrelated process. Tests cover exited native failures, malformed/invalid UTF-8
  and non-finite JSON, timeout/cancellation/disconnect, and real descendants.
- The executable is resolved to its canonical path. Its directory chain must be
  owned by the current user or root and unwritable by other principals; only a
  root-owned sticky ancestor is allowed. A writable release parent is rejected.
- Display strings, skills and rule counts have bounds. Native envelope limits
  remain 256 rows and 2 MiB; a 257-row test asserts a clear envelope error instead
  of letting an incompatible catalog consume unbounded resources.
- Tests prove listing and creation use the same canonical workspace and the
  primary socket's Grok home, including a symlinked workspace input.

## Findings resolved from surrounding source and contract

- Native v1 `profiles.rs` always emits object rows, object definitions and object
  validation, including malformed-source placeholders. The defensive display
  projection covers future incompatible shapes as well. Native v1 has no
  independent disabled property: `validation.valid` is its availability result.
- `session/new` already normalizes `params.cwd` before constructing SessionOwner,
  so `check_profile` receives the canonical workspace. The new test observes the
  helper argv and native forwarded request to prove that path.
- All per-connection asynchronous requests are already limited by
  `MAX_PENDING_REQUESTS == 128`; background tasks are cancelled and awaited on
  websocket termination. The repair adds a separate admission capacity, rather
  than treating the omitted surrounding cap as absent.
- The helper intentionally inherits the bridge environment because approved
  native provider credential references use that environment. Output projection,
  null stderr, private release paths and native credential routing preserve the
  existing boundary. A new credential sandbox is outside this change.
- The primary socket lives directly in the configured Grok home, and this bridge
  discovers sibling sockets in that same directory. Deployments must use the
  frozen, hash-receipted CLI release path. Owner/mode checks do not claim to guard
  against the owning user deliberately replacing their own executable.
- README now documents equal saved model/effort selectors accepted for old
  clients. New phone clients send only profile id and exact revision.

## Verification

`python3 -m unittest discover -s tools/live-session-bridge -p test_bridge.py -q`
passes 75 tests (`/tmp/grok-profile-bridge-keeper-tests.log`). This includes transport fixtures and real helper processes, not
model-provider interoperability. Final native Terminal/Simulator proof uses the
separately frozen compatible native build and is delivered separately.

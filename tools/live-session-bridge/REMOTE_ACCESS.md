# Secure access away from home Wi-Fi

The stable iPhone endpoint is **`wss://grok-mobile-ds.agentee.io/ws`**. In Setup,
paste that full address and keep the existing bridge Secret. The same address
is intended for home Wi-Fi and mobile data. The app keeps the bridge Secret in
its private configuration and sends an Authorization bearer header; credentials
do not belong in the URL. The inherited bridge preference is UserDefaults;
Keychain stores the separate native API key, not this bridge Secret.
Use the normal certificate validation supplied by iOS.

The connection path is:

```text
iPhone → trusted HTTPS/WSS at Cloudflare
       → dedicated outbound encrypted tunnel on Daniel's Mac
       → http://127.0.0.1:18853/ws
       → that Mac's existing native Grok leader and exact resident session actor
```

The agent, workspace, CLI authentication, tools and approvals stay on the Mac.
The M1 worker was used for independent protocol/network testing; it does not
run a replacement agent or receive a copy of the workspace/history.

## What is exposed

Only the dedicated hostname's exact `/ws` path reaches the bridge. `/`,
`/health`, `/ws/` and all other paths return 404 at the tunnel. Missing or wrong
bridge credentials return 401; browser-origin requests return 403. No router
port forwarding was added, and the bridge's existing authentication, permissions
and approvals remain enforced. The dedicated tunnel does not alter the existing
Hub, Alea, Claire or M1 SSH tunnels or ingress routes.

The installed connector is **cloudflared 2026.7.2**. Its dedicated tunnel name is
`grok-mobile-ds`; the tunnel UUID and DNS record identity are recorded in
`~/.grok/mobile-bridge/remote-access/route-state.json`. That file contains route
metadata, not credentials. The generated transport credential lives separately
in `tunnel-credentials.json`, with owner-only permissions inside a mode-0700
directory. It is distinct from the unchanged iPhone bridge Secret.

## Installed runtime and launch commands

The native leader and bridge use their separately supervised services, described
in the main bridge README. The dedicated connector uses:

- LaunchAgent: `~/Library/LaunchAgents/io.agentee.grok-mobile-tunnel.plist`
- Label: `io.agentee.grok-mobile-tunnel`
- Configuration: `~/.grok/mobile-bridge/remote-access/config.yml`
- Logs: `~/.grok/mobile-bridge/remote-access/tunnel.log` and `tunnel.err`
- Cloudflare ingress: exact `/ws` to `http://127.0.0.1:18853`, then a 404 catchall
- Loopback-only connector metrics: `127.0.0.1:18819`

The active public bridge is the reviewed parallel release
`~/.grok/mobile-bridge/releases/bridge-47fd5f8a8d58/bridge.py`, supervised as
**`io.grok.mobile.v20261022.bridge`** on loopback port **18853**. Its saved
LaunchAgent is
`~/Library/LaunchAgents/io.grok.mobile.v20261022.bridge.plist`. The bridge source
is commit `8327f7b0ae5f1e375261c6fc0bc23d2de197fb9e`, SHA-256
`47fd5f8a8d58726775248ed5366378104d980ec5b3169541366b476daaee4ab6`.
It uses the same secret file, native leader socket and workspace reader as the
previous bridge. The native binary remains Grok 1.0.45 (`2ad3ac440d6d`), protocol 1.

The original **`io.grok.mobile.bridge`** on port **18809** deliberately remains
running. Its creator subscriptions still keep existing mobile-created native
actors resident. **Do not stop or restart that bridge to clean up the old port.**
Keep it alive until every resident actor present before this rollout has
explicitly closed. Ordinary attachments through the new bridge do not transfer
creator ownership. The native **`io.grok.mobile.leader`** was also preserved.

The active release pointer is `~/.grok/mobile-bridge/active-release.json`.
Its `rollout-runtime-manifest.json` records the source and artifact hashes,
runtime arguments, native version, process and socket identities, and acceptance
evidence. In that release directory, `rollout-before.json`, `rollout-local.json`
and `rollout-public.json` retain resident identity and canonical history cursor
checks; `rollout-tunnel-switch.json` records the single ingress-port change.
No saved conversation was resumed and no model turn was submitted by deployment.

Only the dedicated connector was restarted for this cutover. Both compatible
resident actors kept their original UUIDs, workspaces, source tokens and history;
126 saved rows stayed actorless, including 12 open conversations whose persisted
previews were read twice over public WSS. The pre-existing legacy `bob` source
does not advertise resident actor identity or strict attachment. Its original
session, workspace, process, socket and unsupported status were preserved without
loading or upgrading it; strict interoperability is not claimed for that source.

For rollback, the release's `rollback_rollout.py --dry-run` validates the exact
current ingress hash and confirms the original bridge and native leader are
alive. Running that helper without `--dry-run` restores only the dedicated
connector's origin to 18809 and restarts that connector. It does not terminate
either bridge or the native leader, and refuses to overwrite a later ingress
change. The public URL and the phone's saved secret remain the same in either
direction.

The LaunchAgent has RunAtLoad and KeepAlive. It reconnects its outbound tunnel
and restarts the connector after a process crash. The native actor survives a
connector restart; the phone reconnects and reconciles the canonical replay.

Its ProcessType is **Interactive**. On October 22, this Mac reached load averages
above 410 while the Background connector remained runnable at priority 4 and its
loopback metrics intermittently timed out. Only this dedicated connector was
reloaded as Interactive, preserving the native leader, bridge and actor UUIDs.
The next real public WSS initialize/cached-auth check took 260 ms at host load
363.49. This observation does not isolate every latency variable; exact cold,
warm and concurrent public catalog measurements are retained separately. No
global scheduling, sleep setting or other connector was changed. The saved plist
and local provisioner both reproduce Interactive. A just-unloaded owned launchd
label may briefly return bootstrap exit 5; the provisioner retries that specific
transient failure at most three times instead of leaving the connector stopped.

On this Mac, these commands validate configuration and inspect only job identity:

```sh
~/.local/bin/cloudflared tunnel \
  --config ~/.grok/mobile-bridge/remote-access/config.yml ingress validate
launchctl list | rg 'io.agentee.grok-mobile-tunnel$'
launchctl list | rg 'io\.grok\.mobile\.(v20261022\.bridge|bridge|leader)$'
```

If the dedicated LaunchAgent is not loaded, load its saved owner-only plist:

```sh
launchctl bootstrap gui/501 \
  ~/Library/LaunchAgents/io.agentee.grok-mobile-tunnel.plist
```

To start a loaded job that is currently stopped:

```sh
launchctl kickstart gui/501/io.agentee.grok-mobile-tunnel
```

For a foreground diagnostic after stopping only this dedicated connector, the
same configuration can be launched directly without placing a token in argv:

```sh
~/.local/bin/cloudflared tunnel \
  --config ~/.grok/mobile-bridge/remote-access/config.yml --no-autoupdate run
```

The local installation helper is retained at
`~/.grok/mobile-bridge/remote-access/provision_secure_route.py`. Re-running it
uses the existing Cloudflare credential internally, verifies the account/zone,
and reuses only its recorded dedicated tunnel/DNS record. It refuses conflicting
pre-existing resources rather than overwriting them. It does not print secrets,
rotate API credentials, or modify other tunnel configurations. This helper is
specific to this Mac's existing account and the hostname above.

## Availability and errors

The Mac must be online, awake, and signed into the macOS session hosting these
user LaunchAgents. A phone cannot continue a live Mac actor while that computer
is asleep, shut down or disconnected. This setup does not change global Mac
sleep settings. LaunchAgents start at user login, not before login.

A brief tunnel outage can return HTTP 502/530 or close the WebSocket. An unavailable
native leader can return 503. These are transport/owner failures, not a request
to create a new conversation; reconnect to the selected session after recovery.
A previously saved private LAN address works only from that network. For remote
use, save the WSS address above in Setup.

Cloudflare Browser Integrity Check rejects the generic `Python-urllib` user agent
with error 1010. No zone security settings were changed. Our test client identifies
itself as `GrokBuildRemoteProbe/1.0`, and native app URLSession coverage is recorded
separately; no browser user agent is impersonated.

## October 22 verification scope

The independent M1 is another Daniel-owned Mac on the same home subnet. Its
requests used public DNS, trusted TLS and the real Cloudflare WSS route. This
proves the public proxy path and same-actor protocol interoperability, and does
not count as iPhone cellular testing.

Observed over that route:

- Native initialize/cached CLI authentication and strict live attachment capability.
- Authenticated session discovery, followed by abrupt disconnect and reconnect.
- Attachment to an owned disposable Terminal actor with its exact UUID and cwd.
- Terminal prompt and assistant/completion replay visible through remote WSS.
- Wrong/missing credential 401, browser-origin 403, private/non-WebSocket paths 404,
  no redirects followed and no credential in URL/argv/remote disk.
- An actual crash of only the dedicated cloudflared connector. Launchd restarted
  it under parent PID 1 in 0.010 seconds; public WSS reconnected in 7.884 seconds
  after one transient 502. The native leader and bridge PIDs and actor UUID stayed
  the same; all original event IDs and conversation payloads survived with zero
  duplicate IDs. Additional background-task snapshots are allowed.
- Eight M1 regression tests cover replay comparison, real message changes,
  background metadata, duplicate IDs, cross-session filtering, TLS-only endpoints
  and credential header injection rejection.
- Read-only preview of an idle open member returned 20 persisted updates with
  identical repeated replay and correct pagination. Seven invalid workspace,
  range and protocol inputs were rejected. Preview and command discovery did
  not create, load or resume an actor; the fixture remained nonresident.
- With the dedicated connector running as Interactive, a forced workspace
  command catalog request took 719 ms, three warm requests took 118–222 ms,
  and three concurrent forced requests took 432–770 ms. All clients received
  the same 326 commands. The bridge uses a bounded 15-second metadata cache and
  coalesces concurrent requests; canonical workspace membership is revalidated
  before serving cached data. These timings were measured before the final
  configuration-stat cache-key refinement, not as a benchmark of every build.
- Actual macOS Foundation `URLSessionWebSocketTask`, with normal system TLS,
  passed six of six public WebSocket upgrades against the final bridge. Each
  completed initialize and cached CLI authentication with strict live
  capabilities in 320–493 ms. The attempts alternated Foundation's default
  user agent and an honest probe identifier; no user-agent-specific failure
  was reproduced. These were Mac tests, not iPhone tests.

An earlier physical iPhone Setup request returned HTTP 502 after the connector
QoS change. The connector had registered four connections, and no corresponding
origin dial/reset error was recorded. The bridge does not emit 502. Those facts
do not identify the failing hop, and the QoS change alone is not evidence that
physical iPhone recovery is fixed. The device run and its safe HTTP diagnostic
fields must be reported separately; actual off-Wi-Fi cellular coverage remains
unverified until exercised on the phone.

Replay comparisons exclude the native `x.ai/leaderClientId`, which intentionally
changes for each connection. Comparing entire envelopes would report a false
content change. Compare event identity plus semantic update payload instead.

Sanitized probe results and scripts are retained in
`~/.grok/mobile-bridge/remote-access/`. The credential is read locally and sent
to the bounded M1 probe through SSH stdin, remains in memory, and is never printed
or stored on that worker. Physical iPhone, Simulator and actual cellular results
must be reported separately from these M1 network tests.

The final evidence files for these checks are `m1-public-live-final.json`,
`tunnel-crash-proof-final.json`, `m1-idle-workspace-final.json`,
`m1-workspace-catalog-cache-interactive-final.json`,
`mac-urlsession-public-final.json`, `tunnel-qos-public-ready.json`,
`tunnel-qos-bootstrap-recovery.json` and `remote-access-audit-final.json`.
Earlier failed probe artifacts are retained as diagnostics and are not the final
replay or recovery acceptance result.

# Peer sockets

Use the pager-free session entrypoint below to run this checkout’s real session
actor. The pager CLI commands elsewhere in this note require resolving its
pre-existing voice/UI build errors. Do not install or codesign
`~/.grok/bin/custom-grok`.

Each real session actor owns one `~/.grok/socks/<session UUID>.sock` inbox.
The exact path is logged at startup and supplied as `GROK_PEER_SOCKET` only in
that session's child-tool environment. Nothing exports it into the parent shell.
A headless `-p` session exits after its work; this feature does not add a peer wait
mode. For a persistent pager-free session, keep the ACP entrypoint’s stdin open
after `session/new`, as shown below.

Socket directories must already have an existing parent and be owned by the
current user with mode 0700. Sockets and token files must have mode 0600.
Collisions fail closed, including stale sockets: the operator must inspect and
remove a stale socket before reusing a path. Cleanup checks the socket inode and
never removes a replacement. Symlinks are rejected except root-managed macOS
ancestor aliases such as `/tmp`.

## Pager-free session actor

Build and run the shell-only ACP entrypoint from this checkout (no pager or voice
UI is compiled):

```sh
cargo build -p xai-grok-shell --example peer-session
cargo run -p xai-grok-shell --example peer-session
```

It loads the normal effective Grok config and credentials and calls
`xai_grok_shell::agent::app::run_stdio_agent`. It speaks newline-delimited ACP
JSON-RPC on stdin/stdout. Creating the real actor requires `initialize`, followed
by `session/new`; merely starting the process creates no session inbox. Keep
stdin open for peer replies and subsequent turns. This launcher performs the
handshake and prints the session ID and subsequent updates; Ctrl-C stops only
its own child:

```sh
python3 -u - <<'PYTHON'
import json
import os
import subprocess

process = subprocess.Popen(
    ["cargo", "run", "-p", "xai-grok-shell", "--example", "peer-session"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
)
def request(request_id, method, params):
    process.stdin.write(json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
    }) + "\n")
    process.stdin.flush()
    while True:
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("session process exited before handshake completed")
        print(line, end="", flush=True)
        reply = json.loads(line)
        if reply.get("id") == request_id:
            if "error" in reply:
                raise RuntimeError(reply["error"])
            return reply["result"]
try:
    request(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    session = request(2, "session/new", {"cwd": os.getcwd(), "mcpServers": []})
    print("Session inbox: ~/.grok/socks/" + session["sessionId"] + ".sock", flush=True)
    # process.stdin remains open while the actual session actor handles peers.
    for line in process.stdout:
        print(line, end="", flush=True)
finally:
    process.stdin.close()
    if process.poll() is None:
        process.terminate()
    process.wait()
PYTHON
```

Set `GROK_PEER_DESTINATION` (and, for bridges, `GROK_PEER_REPLY_SOCKET`) before
launching to advertise `send_peer_message` to the model. This entrypoint uses
normal authentication and tool permission policy. It does not bypass human
permission prompts; an ACP client must answer reverse requests when required.

## Direct local send

```sh
cargo run -p xai-grok-pager-bin --bin xai-grok-pager -- peer send \
  --to /absolute/session.sock --reply-socket /absolute/sender.sock \
  --from-name codex-test --text 'hello'
```

Exit zero means **locally written; downstream delivery unknown**. A direct
Claude socket has no documented acknowledgement. No ambiguous write is retried.

## Explicit paired bridges

`peer token --file /absolute/token` creates a random token without printing it.
Provide the same token securely to both bridges. Tokens stay outside model
context. A bridge JSON configuration describes one exact session pair:

```json
{
  "listen_port": 47731,
  "token_file": "/absolute/token",
  "local_socket": "/absolute/local-session.sock",
  "proxy_socket": "/absolute/local-outbound-proxy.sock",
  "remote_port": 47732,
  "remote_socket": "/absolute/other-machine-session.sock"
}
```

Run with `peer bridge --config /absolute/bridge.json`. The TCP listener is always
127.0.0.1. On the other machine, swap local/remote session paths and ports. Its
proxy must be in Claude's validated private socket directory, for example
`/tmp/cc-socks/grok-bridge.sock`. The fixed proxy refuses a second binding.
Ctrl-C stops the bridge and cleans up its own proxy.

The local outbound Unix proxy is the missing Grok-to-remote hop: it forwards one
message over authenticated loopback TCP, waits for the other bridge's Unix-write
result, and returns that result. Auth and route lines never reach session sockets.
Only the configured local session socket is an authorized incoming route. Incoming
reply addresses are rewritten to the local outbound proxy so follow-ups are usable.

For a model session, set operator-owned environment variables before launching:

- `GROK_PEER_DESTINATION`: this machine's outbound Unix proxy path.
- `GROK_PEER_REPLY_SOCKET`: the other machine's proxy path, used as the wire reply
  identity for direct Claude compatibility. Defaults to this session's inbox for
  same-machine use.

The `send_peer_message` write tool accepts `text`, optional `to`, and optional
`reply_socket`. Optional values must match the operator configuration exactly.
The sender label is session-owned (`grok`); the model cannot choose credentials,
identity, or an arbitrary destination. Normal tool policy applies. For a CLI send
to the outbound proxy add `--expect-result` and use the remote reply proxy as
`--reply-socket`.

A bridge result confirms that the destination Unix write completed. It does not
prove transcript visibility, admission to a full inbox, or model processing. A local
write alone does not prove remote receipt. Failure after a write is ambiguous;
there is no deduplication and no automatic retry. There is no separate ping frame:
transport probes use ordinary test messages and are counted separately from real
conversation transcript evidence.

All serialized lines, including auth and route, are limited to 32768 bytes including
the newline. Each read has an absolute 30-second deadline, connects/writes 2 seconds,
and proxy round trips 8 seconds. Each listener admits at most 16 concurrent readers;
excess connections close. The session inbox has 64 entries and admission pauses at
64 buffered interjections or queued prompts; overload may close/drop new messages.
Accepted peer text is framed without the human-interjection truncation limit.

Peer input is untrusted data. Labels are unauthenticated and terminal-sanitized.
Slash/skill/file expansion is disabled. Permission, question, plan-approval and MCP
elicitation gates hold peer input. Busy input waits for a safe point after tools;
late input becomes an origin-preserving queued turn. It cannot change plan mode,
answer a human gate, cancel tools, or carry human authority.

## Deferred cross-machine validation

No M1 access or SSH tunnel was used for this implementation. **Claude frame format
is still unverified.** The parser intentionally accepts only the documented sample;
verify current Claude's actual version, inbox, exact frame acceptance and reply
format before a live chat. In particular `priority`, `msgV`, message ID shape,
string content, envelope attributes and optional session authentication remain
assumptions. Do not loosen parsing based on guesswork.

For a later separately authorized tunnel, require `ExitOnForwardFailure=yes`,
`ConnectTimeout=10`, `ServerAliveInterval=15`, `ServerAliveCountMax=2`, and explicit
127.0.0.1 binds on both forwards. Inspect both actual listeners for loopback-only
binding; fail on occupied ports. Start/stop only test-owned processes. Verify two
Claude transcript deliveries and one Grok reply separately from write results.

## Local verification (2026-10-17)

| Command | Result |
| --- | --- |
| `cargo test -p xai-peer-socket -p xai-interjection-core` | 15 transport + 16 core tests passed |
| `cargo test -p xai-grok-shell --lib peer_` | 5 passed (4 peer checks and 1 existing classifier test matching the filter) |
| `cargo test -p xai-grok-tools --lib send_peer_message` | 2 passed |
| `cargo run -p xai-grok-pager-bin --bin xai-grok-pager -- peer --help` | Blocked during pager compilation: 69 pre-existing voice/UI errors |

The baseline pager errors include duplicate voice methods in `app/app_view.rs` and
`app/voice_state.rs`, plus references to removed voice fields/APIs. Both duplicate
implementations were confirmed present in the starting committed tree. They remain
outside this implementation. A missing `base64::Engine` import in an existing shell
image test was repaired so the targeted actor tests could compile.

Local evidence includes one real owner-only session inbox, full-text busy admission,
origin-preserving fallback, unchanged gate registration and running tool for all
three human gate kinds with `yes`, `approve` and slash-command payloads, and two
real paired loopback bridge listeners exchanging a message and reply. Stopping the
downstream bridge while the outbound proxy remains live returns failure. No fork
TUI/headless transcript verification was possible while the pager build is blocked.
No M1 connection, SSH tunnel, or Claude conversation was attempted. Claude frame
format remains unverified.

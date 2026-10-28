#!/usr/bin/env python3
"""Run a real isolated leader/Terminal fixture using existing Mac authentication.

No provider or Slack credentials are printed, copied, or placed on argv. The
fixture references the existing auth store and config. Its secret stays in a
private file. --keep-open leaves only this fixture running for mobile QA.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import pty
import secrets
import signal
import struct
import subprocess
import sys
import time
import tomllib
from urllib.parse import urlsplit
import uuid


class Native:
    def __init__(self, reader, writer, bridge, event_log):
        self.reader, self.writer, self.bridge = reader, writer, bridge
        self.pending, self.events, self.sequence = {}, [], 0
        self.event_log = event_log
        self.receiver = asyncio.create_task(self.receive())

    async def receive(self):
        while True:
            frame = await self.bridge.read_frame(self.reader)
            if frame.get("type") != "acp":
                continue
            value = json.loads(frame["payload"])
            if "method" not in value and value.get("id") in self.pending:
                future = self.pending.pop(value["id"])
                if not future.done():
                    future.set_result(value)
            else:
                self.events.append(value)
                with self.event_log.open("a") as out:
                    out.write(json.dumps(value) + "\n")

    async def rpc(self, method, params, timeout=300):
        self.sequence += 1
        number = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        await self.bridge.write_frame(self.writer, {"type": "acp", "payload": json.dumps({"jsonrpc": "2.0", "id": number, "method": method, "params": params})})
        return await asyncio.wait_for(future, timeout)

    async def close(self):
        self.receiver.cancel()
        await asyncio.gather(self.receiver, return_exceptions=True)
        self.writer.close()
        await self.writer.wait_closed()


def assistant_text(events, sid):
    chunks = []
    for event in events:
        params = event.get("params", {})
        update = params.get("update", {})
        if params.get("sessionId") != sid or update.get("sessionUpdate") != "agent_message_chunk":
            continue
        content = update.get("content", {})
        if isinstance(content, dict) and content.get("type") == "text":
            chunks.append(content.get("text", ""))
    return "".join(chunks)


def child_updates(events, sid, kind):
    return [event["params"]["update"] for event in events
            if isinstance(event.get("params", {}).get("update"), dict)
            and event["params"]["update"].get("sessionUpdate") == kind
            and (event["params"].get("sessionId") == sid
                 or event["params"]["update"].get("parent_session_id") == sid)]


def parent_spawn_inputs(events, sid):
    calls = {}
    for event in events:
        params = event.get("params", {})
        update = params.get("update", {})
        value = update.get("rawInput")
        if (params.get("sessionId") == sid
                and update.get("sessionUpdate") in ("tool_call", "tool_call_update")
                and isinstance(value, dict) and "prompt" in value
                and "description" in value):
            calls[update.get("toolCallId")] = value
    return list(calls.values())


async def wait_turn_idle(native, bridge, sid, marker, start, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker in assistant_text(native.events[start:], sid):
            roster = await native.rpc("_x.ai/sessions/list", {})
            rows = bridge.unwrap_result(roster).get("sessions", [])
            if any(row.get("sessionId") == sid and row.get("activity") == "idle" for row in rows):
                return
        await asyncio.sleep(0.2)
    raise RuntimeError("The real parent turn did not finish with its required assistant marker")


def toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(key)} = {toml_value(item)}" for key, item in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return json.dumps(value)


def non_secret_config(installed_config):
    """Copy model metadata and env references; never inline provider credentials."""
    if not installed_config.is_file():
        return '[models]\ndefault = "grok-4.7"\n'
    parsed = tomllib.loads(installed_config.read_text())
    defaults = {key: value for key, value in parsed.get("models", {}).items()
                if key in ("default", "default_reasoning_effort") and isinstance(value, str)}
    # Make the official cached-auth proof independent of optional provider keys.
    defaults["default"] = "grok-4.7"
    lines = ["[models]"] + [f"{key} = {toml_value(value)}" for key, value in defaults.items()]
    allowed = {"model", "name", "base_url", "api_backend", "env_key", "context_window",
               "reasoning_efforts", "stream_tool_calls", "system_prompt_label", "compaction_at_tokens"}
    for name, entry in parsed.get("model", {}).items():
        if not isinstance(entry, dict):
            continue
        metadata = {key: value for key, value in entry.items() if key in allowed}
        endpoint = urlsplit(metadata.get("base_url", ""))
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise RuntimeError("A configured model endpoint contains authentication material; fixture refuses to copy it")
        env_keys = metadata.get("env_key", [])
        if isinstance(env_keys, str):
            env_keys = [env_keys]
        if any(not isinstance(key, str) or not key.replace("_", "").isalnum() for key in env_keys):
            raise RuntimeError("Provider env_key must contain variable names only")
        # Anthropic's version header is protocol metadata, never an auth header.
        headers = entry.get("extra_headers", {})
        version = headers.get("anthropic-version") if isinstance(headers, dict) else None
        if isinstance(version, str) and all(char.isdigit() or char == "-" for char in version):
            metadata["extra_headers"] = {"anthropic-version": version}
        lines.append(f"\n[model.{json.dumps(name)}]")
        lines.extend(f"{key} = {toml_value(value)}" for key, value in metadata.items())
    result = "\n".join(lines) + "\n"
    tomllib.loads(result)
    return result


async def verify(args):
    source = Path(__file__).resolve().parents[2]
    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    home, workspace = root / "home", root / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir()
    (workspace / "README.md").write_text("Fictional, disposable profile routing acceptance.\n")
    installed_home = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok")))
    # Copy only model metadata and env names, never symlink writable real config.
    installed_config = installed_home / "config.toml"
    (home / "config.toml").write_text(non_secret_config(installed_config) + "\n[features]\nsubagent_model_inheritance = false\n")
    binary = args.binary.resolve()
    command_dir = root / "bin"
    command_dir.mkdir()
    (command_dir / "grok").symlink_to(binary)
    execution_path = os.pathsep.join((str(command_dir), str(Path.home() / ".cargo/bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/opt/homebrew/bin", os.environ.get("PATH", "")))
    env = dict(os.environ, GROK_HOME=str(home), GROK_AUTH_PATH=os.environ.get("GROK_AUTH_PATH", str(installed_home / "auth.json")), TERM="xterm-256color", PATH=execution_path)
    subprocess.run(["git", "init", "-q", str(workspace)], env=env, check=True)
    subprocess.run([sys.executable, str(source / "scripts/profiles/install-routing-skill.py"), "--grok-home", str(home)], env=env, check=True, stdout=subprocess.DEVNULL)
    profile_dir = workspace / ".grok/agents"
    profile_dir.mkdir(parents=True)
    definition = {"name": "routing-fixture", "description": "Fictional CFO arithmetic specialist for bounded acceptance", "model": "inherit", "effort": "low", "speed": "standard", "identity": {"displayName": "CFO Fixture", "avatar": "💼"}}
    (profile_dir / "routing-fixture.md").write_text("---\n" + json.dumps(definition) + "\n---\n\nYou are a CFO specialist. Use only fictional data in the assigned task. Never send messages, access credentials or make network/tool calls.\n")
    secret_path = root / "mobile.secret"
    secret_path.write_text(secrets.token_urlsafe(32))
    secret_path.chmod(0o600)
    socket = home / "leader.sock"
    if len(os.fsencode(socket)) >= 104:
        raise RuntimeError("Use a short private fixture root such as /tmp/grok-profiles-YYYYMMDD")
    spec = importlib.util.spec_from_file_location("profile_fixture_bridge", source / "tools/live-session-bridge/bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bridge
    spec.loader.exec_module(bridge)
    log = (root / "leader.log").open("w")
    process = subprocess.Popen([str(binary), "--permission-mode", "default", "--leader-socket", str(socket), "agent", "leader", "--no-auto-update", "--no-exit-on-disconnect", "--relay-on-demand"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    native, terminal, master = None, None, None
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                reader, writer = await bridge.connect_leader(socket, "1.0.45", require_capability=True)
                native = Native(reader, writer, bridge, root / "events.jsonl")
                break
            except (OSError, ValueError, asyncio.IncompleteReadError):
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Isolated native leader did not start; inspect private fixture log")
                await asyncio.sleep(0.2)
        authenticated = await native.rpc("authenticate", {"methodId": "cached_token"})
        if "error" in authenticated:
            raise RuntimeError("Existing native cached authentication was refused")
        sid = str(uuid.uuid4())
        master, slave = pty.openpty()
        import fcntl
        import termios
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 55, 180, 0, 0))
        terminal_args = [str(binary), "--leader", "--no-auto-update", "--leader-socket", str(socket), "--permission-mode", "default", "--cwd", str(workspace), "--session-id", sid]
        if args.terminal_profile:
            terminal_args.extend(("--agent", args.terminal_profile))
        terminal = subprocess.Popen(terminal_args, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        os.set_blocking(master, False)
        terminal_output = bytearray()
        async def drain():
            while terminal.poll() is None:
                try:
                    terminal_output.extend(os.read(master, 65536))
                    (root / "terminal.txt").write_bytes(terminal_output)
                except BlockingIOError:
                    pass
                except OSError:
                    return
                await asyncio.sleep(0.05)
        drainer = asyncio.create_task(drain())
        deadline = time.monotonic() + 30
        while "❯".encode() not in terminal_output:
            if terminal.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Interactive Terminal welcome did not render")
            await asyncio.sleep(0.1)
        os.write(master, b"\x1b[200~Reply exactly PROFILE-TERMINAL-READY-20261028. Use no tools.\x1b[201~\r")
        deadline = time.monotonic() + 90
        while True:
            roster = await native.rpc("_x.ai/sessions/list", {})
            rows = bridge.unwrap_result(roster).get("sessions", [])
            if any(row.get("sessionId") == sid and row.get("resident") for row in rows):
                break
            if terminal.poll() is not None or time.monotonic() > deadline:
                (root / "terminal.txt").write_bytes(terminal_output)
                raise RuntimeError("Real interactive Terminal did not create its resident session")
            # Trust/intro prompts, if present, stay inside this disposable workspace.
            if b"trust" in terminal_output[-10000:].lower():
                os.write(master, b"\r")
            await asyncio.sleep(1)
        await native.rpc("session/load", {"sessionId": sid, "cwd": str(workspace), "mcpServers": [], "_meta": {"x.ai/resident_only": True}})
        catalog = subprocess.run([str(binary), "--no-auto-update", "profiles", "--cwd", str(workspace), "list", "--json"], env=env, input="", capture_output=True, text=True, check=True)
        profiles = json.loads(catalog.stdout)
        role = next(p for p in profiles["profiles"] if p["id"] == "routing-fixture")
        fixture = {"binary": str(binary), "home": str(home), "workspace": str(workspace), "socket": str(socket), "secretFile": str(secret_path), "parentSessionId": sid, "profileId": role["id"], "profileRevision": role["revision"], "leaderPid": process.pid, "terminalPid": terminal.pid}
        (root / "fixture.json").write_text(json.dumps(fixture, indent=2))
        print(json.dumps({"fixture": str(root / "fixture.json"), "status": "resident_interactive_terminal_ready"}), flush=True)
        await wait_turn_idle(native, bridge, sid, "PROFILE-TERMINAL-READY-20261028", 0, timeout=120)
        if args.startup_only:
            (root / "acceptance.json").write_text(json.dumps({"startup": {"passed": True, "terminalProfile": args.terminal_profile or None, "assistant": assistant_text(native.events, sid)}}, indent=2))
            drainer.cancel()
            await asyncio.gather(drainer, return_exceptions=True)
            return
        prompt = "Use the pick-right-subagent-and-assign-task skill to delegate this routine local acknowledgement task. Read the current catalog through the skill's documented local command and choose the available model, reasoning effort and speed that fit the task. Only the cached official Grok provider is authenticated in this fixture; optional local provider credentials are unavailable. Delegate exactly once to General. The child must reply exactly PROFILE-GENERAL-CHILD-ACK-20261028 and must use no tools or network. After its result, reply PROFILE-GENERAL-PARENT-ACK-20261028 and report its actual execution settings. The parent may only load the skill, read the catalog, spawn and wait for the child. Do not send Slack messages or access credentials."
        start = len(native.events)
        os.write(master, b"\x1b[200~" + prompt.encode() + b"\x1b[201~\r")
        await wait_turn_idle(native, bridge, sid, "PROFILE-GENERAL-PARENT-ACK-20261028", start)
        spawned = child_updates(native.events[start:], sid, "subagent_spawned")
        finished = child_updates(native.events[start:], sid, "subagent_finished")
        if len(spawned) != 1 or not spawned[0].get("execution"):
            raise RuntimeError("Expected exactly one genuine child with native execution metadata")
        child = spawned[0]
        completed = [event for event in finished if event.get("subagent_id") == child.get("subagent_id")]
        if len(completed) != 1 or completed[0].get("status") != "completed" or "PROFILE-GENERAL-CHILD-ACK-20261028" not in completed[0].get("output", ""):
            raise RuntimeError("The genuine child did not complete the acceptance task exactly once")
        execution = {**child["execution"], "childSessionId": child.get("child_session_id"), "subagentId": child.get("subagent_id"), "attemptId": child.get("attempt_id")}
        choices = parent_spawn_inputs(native.events[start:], sid)
        if (len(choices) != 1 or not choices[0].get("model")
                or choices[0].get("reasoning_effort") not in ("low", "medium", "high")
                or choices[0].get("speed") not in ("standard", "fast")):
            raise RuntimeError("The real parent did not explicitly choose admitted model, reasoning_effort and speed through its tool")
        choice = choices[0]
        selected = next((model for model in profiles.get("models", [])
                         if model.get("id") == choice["model"] and model.get("available") is True), None)
        if selected is None:
            raise RuntimeError("The parent selected a model outside the available local catalog")
        expected_model = selected["model"]
        if choice["speed"] == "fast" and not expected_model.endswith("-fast"):
            expected_model += "-fast"
        if (execution.get("profile") != "general-purpose"
                or execution.get("provider") != selected.get("provider")
                or execution.get("model") != expected_model
                or execution.get("reasoning_effort") != choice["reasoning_effort"]
                or execution.get("speed") != choice["speed"]):
            raise RuntimeError("The real child execution did not match the parent's admitted catalog choices")
        fixture["execution"] = execution
        (root / "fixture.json").write_text(json.dumps(fixture, indent=2))
        (root / "acceptance.json").write_text(json.dumps({"general": {"passed": True, "spawnCount": 1, "completionCount": 1, "parentChoice": choices[0], "execution": execution}}, indent=2))
        print(json.dumps({"status": "real_child_completed", "execution": execution}), flush=True)
        if args.keep_open:
            # The private request file permits explicit prompt-ownership handoffs
            # without replacing the real interactive Terminal with an ACP client.
            # Only the fixture's coordinator writes this file after the previous
            # owner has released the session.
            seen_requests = set()
            while not (root / "finish").exists():
                request_path = root / "prompt-request.json"
                if request_path.is_file():
                    request = json.loads(request_path.read_text())
                    request_id = request.get("id", "")
                    marker = request.get("marker", "")
                    text = request.get("prompt", "")
                    if (not request_id or not request_id.isascii()
                            or not all(char.isalnum() or char in "-_" for char in request_id)
                            or request_id in seen_requests
                            or not isinstance(marker, str) or not marker
                            or not isinstance(text, str) or not text
                            or "\x1b" in text):
                        raise RuntimeError("Malformed or duplicate private Terminal prompt request")
                    seen_requests.add(request_id)
                    request_path.unlink()
                    request_start = len(native.events)
                    os.write(master, b"\x1b[200~" + text.encode() + b"\x1b[201~\r")
                    await wait_turn_idle(native, bridge, sid, marker, request_start)
                    result = {"id": request_id, "parentSessionId": sid,
                              "eventStart": request_start, "eventEnd": len(native.events),
                              "assistant": assistant_text(native.events[request_start:], sid),
                              "spawnInputs": parent_spawn_inputs(native.events[request_start:], sid),
                              "spawned": child_updates(native.events[request_start:], sid, "subagent_spawned"),
                              "finished": child_updates(native.events[request_start:], sid, "subagent_finished")}
                    (root / f"prompt-result-{request_id}.json").write_text(json.dumps(result, indent=2))
                    print(json.dumps({"status": "terminal_prompt_completed", "id": request_id}), flush=True)
                (root / "terminal.txt").write_bytes(terminal_output)
                await asyncio.sleep(1)
        (root / "terminal.txt").write_bytes(terminal_output)
        drainer.cancel()
        await asyncio.gather(drainer, return_exceptions=True)
    finally:
        if terminal is not None and terminal.poll() is None:
            terminal.send_signal(signal.SIGTERM)
            await asyncio.to_thread(terminal.wait, 15)
        if master is not None:
            os.close(master)
        if native is not None:
            await native.close()
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 15)
        log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--terminal-profile", default="general-purpose", help="Named profile for the real Terminal; empty uses ordinary startup")
    parser.add_argument("--startup-only", action="store_true", help="Prove a real resident Terminal turn without the delegation task")
    asyncio.run(verify(parser.parse_args()))

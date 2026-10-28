"""Regression checks for the live fixture; these are not live-agent interoperability proof."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
from unittest.mock import Mock

import pytest


SPEC = importlib.util.spec_from_file_location("native_profile_live_harness", Path(__file__).with_name("verify-native-profiles-live.py"))
assert SPEC is not None and SPEC.loader is not None
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)


class Writer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class Bridge:
    def __init__(self) -> None:
        self.incoming = asyncio.Queue()
        self.written = []
        self.write_error = None

    async def read_frame(self, reader):
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def write_frame(self, writer, value):
        if self.write_error is not None:
            raise self.write_error
        self.written.append(json.loads(value["payload"]))


async def writes(bridge, count):
    for _ in range(100):
        if len(bridge.written) == count:
            return
        await asyncio.sleep(0)
    raise AssertionError("RPC did not reach the transport")


def test_fixture_environment_excludes_inherited_service_credentials(tmp_path: Path) -> None:
    source = {"HOME": str(tmp_path), "LANG": "en_US.UTF-8", "PATH": "/inherited",
              "SLACK_BOT_TOKEN": "fixture-private-value", "OP_SERVICE_ACCOUNT_TOKEN": "fixture-private-value",
              "ANTHROPIC_API_KEY": "fixture-private-value", "XAI_API_KEY": "fixture-private-value",
              "DATABASE_URL": "fixture-private-value", "GROK_AUTH_PATH": "/existing/auth.json",
              "GROK_HOME": "/real/home", "GROK_DISABLE_HOOKS": "1"}
    env = HARNESS.fixture_environment(tmp_path / "home", tmp_path / "installed", "/controlled/bin", source)
    assert env == {"HOME": str(tmp_path), "LANG": "en_US.UTF-8", "PATH": "/controlled/bin",
                   "TERM": "xterm-256color", "GROK_HOME": str(tmp_path / "home"), "GROK_AUTH_PATH": "/existing/auth.json"}
    assert "fixture-private-value" not in env.values()
    assert "GROK_DISABLE_HOOKS" not in env


def test_receiver_failure_wakes_all_pending_and_refuses_future_rpcs(tmp_path: Path) -> None:
    async def scenario():
        bridge, writer = Bridge(), Writer()
        native = HARNESS.Native(None, writer, bridge, tmp_path / "events.jsonl")
        requests = [asyncio.create_task(native.rpc("test", {})) for _ in range(2)]
        await writes(bridge, 2)
        await bridge.incoming.put(asyncio.IncompleteReadError(b"private-payload", 5))
        results = await asyncio.wait_for(asyncio.gather(*requests, return_exceptions=True), 1)
        assert all(isinstance(item, ConnectionError) for item in results)
        assert all("IncompleteReadError" in str(item) and "private-payload" not in str(item) for item in results)
        assert native.pending == {}
        with pytest.raises(ConnectionError, match="IncompleteReadError"):
            await native.rpc("later", {}, timeout=300)
        await native.close()
        assert writer.closed
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", ["{private-invalid", "[]"])
def test_malformed_receiver_payload_refuses_without_echo(tmp_path: Path, payload: str) -> None:
    async def scenario():
        bridge = Bridge()
        native = HARNESS.Native(None, Writer(), bridge, tmp_path / "events.jsonl")
        request = asyncio.create_task(native.rpc("test", {}))
        await writes(bridge, 1)
        await bridge.incoming.put({"type": "acp", "payload": payload})
        with pytest.raises(ConnectionError) as error:
            await asyncio.wait_for(request, 1)
        assert "private-invalid" not in str(error.value)
        assert native.pending == {}
        await native.close()
    asyncio.run(scenario())


def test_out_of_order_rpc_responses_keep_identity_and_events(tmp_path: Path) -> None:
    async def scenario():
        bridge = Bridge()
        native = HARNESS.Native(None, Writer(), bridge, tmp_path / "events.jsonl")
        first = asyncio.create_task(native.rpc("one", {}))
        second = asyncio.create_task(native.rpc("two", {}))
        await writes(bridge, 2)
        for value in [{"id": 2, "result": "second"}, {"method": "session/update", "params": {}}, {"id": 1, "result": "first"}]:
            await bridge.incoming.put({"type": "acp", "payload": json.dumps(value)})
        assert (await first)["result"] == "first"
        assert (await second)["result"] == "second"
        assert len(native.events) == 1
        assert native.pending == {}
        assert (tmp_path / "events.jsonl").stat().st_mode & 0o777 == 0o600
        await native.close()
    asyncio.run(scenario())


def test_write_failure_timeout_and_cancellation_leave_no_pending_future(tmp_path: Path) -> None:
    async def scenario():
        bridge = Bridge()
        native = HARNESS.Native(None, Writer(), bridge, tmp_path / "events.jsonl")
        with pytest.raises(TimeoutError, match="Native RPC timed out"):
            await native.rpc("timeout", {}, timeout=0.001)
        assert native.pending == {}
        cancelled = asyncio.create_task(native.rpc("cancel", {}))
        await writes(bridge, 2)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert native.pending == {}
        bridge.write_error = OSError("private-write-material")
        with pytest.raises(ConnectionError) as error:
            await native.rpc("write-failed", {})
        assert "private-write-material" not in str(error.value)
        assert native.pending == {}
        await native.close()
    asyncio.run(scenario())


def test_owned_process_is_force_killed_and_reaped_after_term_timeout(monkeypatch) -> None:
    process = Mock(pid=123456)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("owned-fixture", 1), 0]
    signals = []
    monkeypatch.setattr(HARNESS.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    asyncio.run(HARNESS.stop_owned_process(process, timeout=0.01))
    assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]
    assert process.wait.call_count == 2


def test_cleanup_attempts_all_resources_after_independent_failures(monkeypatch) -> None:
    attempted = []
    async def stop(process):
        attempted.append(process)
        if process == "terminal":
            raise subprocess.TimeoutExpired("fixture", 1)
    class Native:
        async def close(self):
            attempted.append("native")
            raise OSError("private-detail")
    class Log:
        def close(self):
            attempted.append("log")
    monkeypatch.setattr(HARNESS, "stop_owned_process", stop)
    monkeypatch.setattr(HARNESS.os, "close", lambda fd: attempted.append(fd))
    failures = asyncio.run(HARNESS.cleanup_fixture("terminal", Native(), "leader", master=12, slave=13, log=Log()))
    assert attempted == ["terminal", "leader", "native", 12, 13, "log"]
    assert failures == ["terminal:TimeoutExpired", "native:OSError"]
    assert "private-detail" not in str(failures)


def trust_dialog(workspace: Path) -> bytes:
    return ("Do you trust the contents of this directory?\n" + str(workspace) + "\nYes, proceed\nNo, quit").encode()


def test_trust_handler_matches_only_disposable_welcome_and_accepts_once(tmp_path: Path) -> None:
    trust = HARNESS.WelcomeTrust(tmp_path)
    assert not trust.accept(b"Tool wants permission to trust a remote command")
    assert not trust.accept(trust_dialog(tmp_path / "another"))
    assert not trust.accept(trust_dialog(Path(str(tmp_path) + "é")))
    assert trust.accept(trust_dialog(tmp_path))
    assert not trust.accept(trust_dialog(tmp_path) * 2)


def test_trust_consent_happens_before_waiting_for_terminal_prompt(tmp_path: Path, monkeypatch) -> None:
    terminal = Mock()
    terminal.poll.return_value = None
    output = bytearray(trust_dialog(tmp_path))
    sent = []
    def write(fd, value):
        sent.append((fd, value))
        output.extend("❯".encode())
    monkeypatch.setattr(HARNESS.os, "write", write)
    asyncio.run(HARNESS.wait_terminal_ready(terminal, output, 123, tmp_path, timeout=0.1))
    assert sent == [(123, b"y")]


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    root = tmp_path / "fixture"
    root.mkdir(mode=0o700)
    return root


def request(request_id: str = "proof-1") -> dict[str, str]:
    return {"id": request_id, "marker": "ACK", "prompt": "Reply ACK"}


def test_atomic_handoff_is_private_and_never_overwrites_pending_request(private_root: Path) -> None:
    HARNESS.write_prompt_request(private_root, request())
    path = private_root / "prompt-request.json"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        HARNESS.write_prompt_request(private_root, request("proof-2"))
    seen = set()
    assert HARNESS.take_prompt_request(private_root, seen) == (request(), None)
    assert seen == {"proof-1"}
    assert HARNESS.take_prompt_request(private_root, seen) == (None, None)
    assert not list(private_root.glob(".profile-json-*"))


@pytest.mark.parametrize("value", [None, [], {}, {"id": []}, {**request(), "id": "../escape"},
    {**request(), "id": 12}, {**request(), "marker": []}, {**request(), "marker": "ACK\x1b"},
    {**request(), "prompt": "text\x1b[201~"}, {**request(), "prompt": "send\rsubmit"}])
def test_invalid_handoff_is_discarded_and_next_request_survives(private_root: Path, value: object) -> None:
    HARNESS.atomic_private_json(private_root / "prompt-request.json", value)
    accepted, refusal = HARNESS.take_prompt_request(private_root, set())
    assert accepted is None and refusal is not None
    HARNESS.write_prompt_request(private_root, request())
    assert HARNESS.take_prompt_request(private_root, set()) == (request(), None)


def test_partial_duplicate_insecure_and_symlink_handoffs_do_not_tear_down_fixture(private_root: Path) -> None:
    path = private_root / "prompt-request.json"
    path.write_text('{"id":')
    path.chmod(0o600)
    assert HARNESS.take_prompt_request(private_root, set())[0] is None
    HARNESS.write_prompt_request(private_root, request())
    assert HARNESS.take_prompt_request(private_root, {"proof-1"})[0] is None
    path.write_text(json.dumps(request()))
    path.chmod(0o644)
    assert HARNESS.take_prompt_request(private_root, set()) == (None, "insecure-file")
    target = private_root / "private-untouched.json"
    target.write_text(json.dumps(request()))
    target.chmod(0o600)
    path.symlink_to(target)
    assert HARNESS.take_prompt_request(private_root, set())[0] is None
    assert target.read_text() == json.dumps(request())
    HARNESS.write_prompt_request(private_root, request("last"))
    assert HARNESS.take_prompt_request(private_root, set()) == (request("last"), None)


def test_claim_does_not_delete_a_replacement_request(private_root: Path, monkeypatch) -> None:
    HARNESS.write_prompt_request(private_root, request("first"))
    original = HARNESS.os.rename
    replacement_written = False
    def rename(source, target):
        nonlocal replacement_written
        original(source, target)
        if not replacement_written:
            replacement_written = True
            HARNESS.write_prompt_request(private_root, request("second"))
    monkeypatch.setattr(HARNESS.os, "rename", rename)
    seen = set()
    assert HARNESS.take_prompt_request(private_root, seen) == (request("first"), None)
    assert HARNESS.take_prompt_request(private_root, seen) == (request("second"), None)
    assert seen == {"first", "second"}


def test_handoff_refuses_non_private_root(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="owner-only"):
        HARNESS.write_prompt_request(tmp_path, request())

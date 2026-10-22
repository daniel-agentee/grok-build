"""Read-only exporter trust, bounds, cancellation, and failure isolation."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("workspace_bridge", Path(__file__).with_name("bridge.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class WorkspaceReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.directory = bridge.LeaderDirectory(self.root / "leader.sock", "x" * 40, "1.0.45")
        self.reader = self.root / "reader"

    async def asyncTearDown(self):
        self.temp.cleanup()

    def script(self, body):
        self.reader.write_text(f"#!{sys.executable}\n" + body + "\n")
        self.reader.chmod(0o700)
        self.directory.workspace_reader = self.reader
        self.directory.workspace_reader_sha256 = hashlib.sha256(self.reader.read_bytes()).hexdigest()

    def payload(self, value):
        self.script("print(" + repr(json.dumps(value)) + ")")

    async def test_valid_export_uses_canonical_home_and_normalizes_workspaces(self):
        observed = self.root / "observed-argument"
        payload = {"schemaVersion": 1, "members": [{"sessionId": "open", "cwd": "/work/a/../project"}]}
        self.script(f"import pathlib,sys\npathlib.Path({str(observed)!r}).write_text(sys.argv[1])\nprint({json.dumps(payload)!r})")
        members, error = await self.directory.read_workspace_membership()
        self.assertEqual(members, {("open", "/work/project")})
        self.assertIsNone(error)
        self.assertEqual(observed.read_text(), str(self.root / "dashboard/workspace.db"))

    async def test_missing_reader_and_untrusted_modes_pin_uid_never_execute(self):
        self.assertEqual(await self.directory.read_workspace_membership(), (set(), "reader_unavailable"))
        self.directory.workspace_reader = self.reader
        self.assertEqual(await self.directory.read_workspace_membership(), (set(), "reader_unavailable"))
        executed = self.root / "executed"
        body = f"import pathlib\npathlib.Path({str(executed)!r}).touch()\nprint('{{}}')"
        for failure in ("pin", "writable", "nonexecutable", "uid", "symlink"):
            with self.subTest(failure=failure):
                self.script(body)
                if failure == "pin":
                    self.directory.workspace_reader_sha256 = "0" * 64
                elif failure == "writable":
                    self.reader.chmod(0o720)
                elif failure == "nonexecutable":
                    self.reader.chmod(0o600)
                elif failure == "symlink":
                    target = self.root / "symlink-target"
                    self.reader.rename(target)
                    self.reader.symlink_to(target)
                if failure == "uid":
                    with patch.object(bridge.os, "getuid", return_value=os.getuid() + 1):
                        _, error = await self.directory.read_workspace_membership()
                else:
                    _, error = await self.directory.read_workspace_membership()
                self.assertIn(error, {"reader_unavailable", "reader_untrusted"})
                self.assertFalse(executed.exists())
                self.reader.unlink()

    async def test_malformed_schema_and_rows_are_rejected_without_partial_membership(self):
        good = {"sessionId": "open", "cwd": "/work"}
        invalid = [[], {}, {"schemaVersion": 2, "members": []},
                   {"schemaVersion": 1, "members": None},
                   {"schemaVersion": 1, "members": [good, {"sessionId": "", "cwd": "/"}]},
                   {"schemaVersion": 1, "members": [{"sessionId": "x" * 513, "cwd": "/"}]},
                   {"schemaVersion": 1, "members": [{"sessionId": "x", "cwd": "relative"}]},
                   {"schemaVersion": 1, "members": [{"sessionId": "x", "cwd": 1}]},
                   {"schemaVersion": 1, "members": [good] * 257}]
        for payload in invalid:
            with self.subTest(payload_type=type(payload).__name__):
                self.payload(payload)
                self.assertEqual(await self.directory.read_workspace_membership(), (set(), "invalid_snapshot"))
        for output in ("not json", '{"schemaVersion":1,"members":NaN}'):
            self.script(f"print({output!r})")
            self.assertEqual(await self.directory.read_workspace_membership(), (set(), "invalid_snapshot"))

    async def test_nonzero_exit_is_reported_separately_from_an_empty_workspace(self):
        self.payload({"schemaVersion": 1, "members": []})
        self.assertEqual(await self.directory.read_workspace_membership(), (set(), None))
        self.script("raise SystemExit(1)")
        self.assertEqual(await self.directory.read_workspace_membership(), (set(), "read_failed"))

    async def run_bounded(self, body, timeout=None, output_bound=None):
        self.script(body)
        processes = []
        original = asyncio.create_subprocess_exec

        async def capture(*args, **kwargs):
            process = await original(*args, **kwargs)
            processes.append(process)
            return process

        with (patch.object(bridge.asyncio, "create_subprocess_exec", side_effect=capture),
              patch.object(bridge, "WORKSPACE_READ_TIMEOUT", timeout or 2),
              patch.object(bridge, "MAX_WORKSPACE_OUTPUT", output_bound or bridge.MAX_WORKSPACE_OUTPUT)):
            result = await self.directory.read_workspace_membership()
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        return result

    async def test_timeout_and_oversized_output_reap_the_child(self):
        result = await self.run_bounded("import time\ntime.sleep(30)", timeout=.1)
        self.assertEqual(result, (set(), "read_timeout"))
        result = await self.run_bounded("import time\nprint('x' * 4096, flush=True)\ntime.sleep(30)", output_bound=128)
        self.assertEqual(result, (set(), "invalid_snapshot"))

    async def test_cancelling_snapshot_reaps_the_exporter(self):
        self.script("import time\ntime.sleep(30)")
        original = asyncio.create_subprocess_exec
        started = asyncio.Future()

        async def capture(*args, **kwargs):
            process = await original(*args, **kwargs)
            started.set_result(process)
            return process

        with patch.object(bridge.asyncio, "create_subprocess_exec", side_effect=capture):
            task = asyncio.create_task(self.directory.read_workspace_membership())
            process = await asyncio.wait_for(started, 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(process.returncode)

    async def test_helper_exit_race_during_cleanup_preserves_failure_result(self):
        from unittest.mock import AsyncMock, MagicMock
        self.payload({"schemaVersion": 1, "members": []})
        process = MagicMock()
        process.returncode = None
        process.stdout.read = AsyncMock(return_value=b"x" * 129)
        process.kill.side_effect = ProcessLookupError
        process.wait = AsyncMock(return_value=0)
        with (patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)),
              patch.object(bridge, "MAX_WORKSPACE_OUTPUT", 128)):
            result = await self.directory.read_workspace_membership()
        self.assertEqual(result, (set(), "invalid_snapshot"))
        process.wait.assert_awaited_once()


class WorkspaceReaderPathTests(unittest.TestCase):
    def test_checkout_and_packaged_defaults_do_not_depend_on_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            script = root / "repo/tools/live-session-bridge/bridge.py"
            script.parent.mkdir(parents=True)
            script.touch()
            with patch.object(bridge, "__file__", str(script)):
                self.assertEqual(bridge.default_workspace_reader(), root / "repo/target/debug/grok-dashboard-workspace")
                preserved = root / "repo/target/verified-all-sessions-20261021/grok-dashboard-workspace"
                preserved.parent.mkdir(parents=True)
                preserved.touch()
                self.assertEqual(bridge.default_workspace_reader(), preserved)
                packaged = script.with_name("grok-dashboard-workspace")
                packaged.touch()
                self.assertEqual(bridge.default_workspace_reader(), packaged)


if __name__ == "__main__":
    unittest.main()

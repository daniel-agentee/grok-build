"""Launcher failure paths must never start or replace processes."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import asyncio
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location("launcher", Path(__file__).with_name("launch.py"))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def run_rejected(self, args, expected, *, correct_digest=False):
        digest = MagicMock()
        digest.hexdigest.return_value = launcher.BINARY_SHA256 if correct_digest else "wrong"
        error = io.StringIO()
        with (
            patch.object(sys, "argv", ["launch.py", *args]),
            patch.object(launcher.subprocess, "Popen") as start,
            patch.object(launcher.os, "execv") as execute,
            patch.object(launcher.hashlib, "sha256", return_value=digest),
            contextlib.redirect_stderr(error),
        ):
            with self.assertRaises(SystemExit) as caught:
                launcher.main()
            self.assertEqual(caught.exception.code, 2)
            start.assert_not_called()
            execute.assert_not_called()
        self.assertIn(expected, error.getvalue())

    def test_wrong_binary_digest_never_starts_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "grok"
            binary.write_bytes(b"untrusted binary")
            binary.chmod(0o700)
            self.run_rejected(["--workspace", directory, "--binary", str(binary)], "differs")

    def test_nonexistent_workspace_never_starts_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_rejected(["--workspace", str(Path(directory) / "missing")], "must exist")

    def test_occupied_port_never_starts_or_replaces_processes(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            binary = Path(directory) / "grok"
            binary.write_bytes(b"pinned mock binary")
            binary.chmod(0o700)
            self.run_rejected(
                [
                    "--workspace",
                    directory,
                    "--binary",
                    str(binary),
                    "--port",
                    str(listener.getsockname()[1]),
                ],
                "occupied",
                correct_digest=True,
            )

    def test_invalid_port_never_starts_processes(self):
        self.run_rejected(["--port", "-1"], "between 1 and 65535")

    def test_new_leader_and_terminal_both_request_default_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "grok"
            binary.write_bytes(b"pinned mock binary")
            binary.chmod(0o700)
            writer = MagicMock()
            writer.wait_closed = AsyncMock()
            digest = MagicMock()
            digest.hexdigest.return_value = launcher.BINARY_SHA256
            bridge = MagicMock()
            bridge.poll.return_value = None
            with (
                socket.socket() as free_port,
                patch.object(Path, "home", return_value=root),
                patch.object(launcher.hashlib, "sha256", return_value=digest),
                patch.object(launcher, "connect_leader", AsyncMock(return_value=(None, writer))),
                patch.object(
                    launcher.subprocess, "Popen", side_effect=[MagicMock(), bridge]
                ) as start,
                patch.object(launcher.socket, "create_connection"),
                patch.object(launcher.os, "execv") as execute,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                free_port.bind(("127.0.0.1", 0))
                port = free_port.getsockname()[1]
                free_port.close()
                with patch.object(
                    sys,
                    "argv",
                    [
                        "launch.py",
                        "--workspace",
                        directory,
                        "--binary",
                        str(binary),
                        "--port",
                        str(port),
                        "--workspace-reader",
                        str(root / "preserved-dashboard-reader"),
                    ],
                ):
                    launcher.main()
            leader_args = start.call_args_list[0].args[0]
            bridge_args = start.call_args_list[1].args[0]
            terminal_args = execute.call_args.args[1]
            self.assertEqual(leader_args[leader_args.index("--permission-mode") + 1], "default")
            self.assertEqual(terminal_args[terminal_args.index("--permission-mode") + 1], "default")
            self.assertEqual(bridge_args[bridge_args.index("--cli-version") + 1], launcher.VERSION)
            self.assertEqual(bridge_args[bridge_args.index("--workspace-reader") + 1],
                             str((root / "preserved-dashboard-reader").absolute()))
            self.assertIn("--no-auto-update", leader_args)
            self.assertIn("--no-auto-update", terminal_args)
            self.assertFalse((root / ".claude").exists())
            self.assertNotIn("--trust", terminal_args)

    def test_nonexecutable_binary_never_starts_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "grok"
            binary.write_bytes(b"pinned binary without executable permission")
            binary.chmod(0o600)
            self.run_rejected(["--workspace", directory, "--binary", str(binary)], "executable")

    def test_invalid_secret_and_dead_existing_leader_never_start_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "grok"
            binary.write_bytes(b"pinned binary")
            binary.chmod(0o700)
            secret = root / "secret"
            secret.write_text("s" * 40)
            secret.chmod(0o644)
            args = [
                "--workspace",
                directory,
                "--binary",
                str(binary),
                "--secret-file",
                str(secret),
                "--leader-socket",
                str(root / "leader.sock"),
            ]
            with patch.object(Path, "home", return_value=root):
                self.run_rejected(args, "owner-only", correct_digest=True)
                secret.chmod(0o600)
                (root / "leader.sock").touch()
                with patch.object(
                    launcher,
                    "connect_leader",
                    AsyncMock(side_effect=asyncio.IncompleteReadError(b"", 4)),
                ):
                    self.run_rejected(args, "Existing leader", correct_digest=True)

    def test_secret_symlink_never_starts_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "grok"
            binary.write_bytes(b"pinned binary")
            binary.chmod(0o700)
            secret = root / "secret"
            secret.write_text("s" * 40)
            secret.chmod(0o600)
            link = root / "link"
            link.symlink_to(secret)
            with patch.object(Path, "home", return_value=root):
                self.run_rejected(
                    ["--workspace", directory, "--binary", str(binary), "--secret-file", str(link)],
                    "symbolic links",
                    correct_digest=True,
                )

    def test_failed_leader_exits_immediately_without_spawning_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "grok"
            binary.write_bytes(b"pinned binary")
            binary.chmod(0o700)
            digest = MagicMock()
            digest.hexdigest.return_value = launcher.BINARY_SHA256
            error = io.StringIO()
            process = MagicMock()
            process.poll.return_value = 1
            with (
                patch.object(Path, "home", return_value=root),
                patch.object(
                    sys, "argv", ["launch.py", "--workspace", directory, "--binary", str(binary)]
                ),
                patch.object(launcher.hashlib, "sha256", return_value=digest),
                patch.object(
                    launcher, "connect_leader", AsyncMock(side_effect=ConnectionRefusedError)
                ),
                patch.object(launcher.subprocess, "Popen", return_value=process) as start,
                patch.object(launcher.time, "sleep") as sleep,
                patch.object(launcher.os, "execv") as execute,
                contextlib.redirect_stderr(error),
            ):
                with self.assertRaises(SystemExit):
                    launcher.main()
                start.assert_called_once()
                sleep.assert_not_called()
                execute.assert_not_called()
            self.assertIn("Leader startup failed", error.getvalue())


if __name__ == "__main__":
    unittest.main()

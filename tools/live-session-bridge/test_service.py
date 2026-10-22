"""The login service must recover transport without replacing a live owner."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import plistlib
import socket
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

service = importlib.import_module("service")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.binary = self.root / "pager"
        self.reader = self.root / "reader"
        self.secret = self.root / "secret"
        self.binary.write_bytes(b"native")
        self.reader.write_bytes(b"reader")
        self.secret.write_text("s" * 40)
        for path in (self.binary, self.reader):
            path.chmod(0o700)
        self.secret.chmod(0o600)
        self.config = service.ServiceConfig(
            binary=str(self.binary), workspace_reader=str(self.reader),
            workspace=str(self.root), leader_socket=str(self.root / "leader.sock"),
            secret_file=str(self.secret), host="127.0.0.1", port=18809,
            state_dir=str(self.root / "state"), label_prefix="io.grok.test",
        )

    def validate(self, config=None):
        import hashlib

        with (patch.object(service, "BINARY_SHA256", hashlib.sha256(b"native").hexdigest()),
              patch.object(service, "WORKSPACE_READER_SHA256", hashlib.sha256(b"reader").hexdigest())):
            service.validate_config(config or self.config)

    def test_validated_exact_pins_and_existing_secret(self):
        self.validate()
        self.assertEqual(self.secret.read_text(), "s" * 40)

    def test_wrong_pin_and_writable_binary_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "digest"):
            service.validate_config(self.config)
        self.binary.chmod(0o720)
        with self.assertRaisesRegex(ValueError, "writable"):
            self.validate()

    def test_symlink_secret_or_artifact_never_accepted(self):
        link = self.root / "link"
        link.symlink_to(self.secret)
        with self.assertRaises((OSError, ValueError)):
            self.validate(replace(self.config, secret_file=str(link)))
        link.unlink()
        link.symlink_to(self.binary)
        with self.assertRaises((OSError, ValueError)):
            self.validate(replace(self.config, binary=str(link)))

    def test_workspace_and_socket_parent_must_be_owned_safe_directories(self):
        self.root.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "directory"):
            self.validate()

    def test_invalid_host_port_labels_and_relative_paths_are_rejected(self):
        for changes in [{"port": 0}, {"port": True}, {"host": "public.example"},
                        {"label_prefix": "../unsafe"}, {"workspace": "."}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.validate(replace(self.config, **changes))

    def test_plists_supervise_independent_roles_and_throttle_restarts(self):
        path = self.root / "state/service.json"
        plists = service.make_plists(self.config, path)
        self.assertEqual(set(plists), {"leader", "bridge"})
        for role, value in plists.items():
            self.assertTrue(value["RunAtLoad"])
            self.assertTrue(value["KeepAlive"])
            self.assertGreaterEqual(value["ThrottleInterval"], 30)
            self.assertEqual(value["ProcessType"], "Interactive")
            self.assertEqual(value["WorkingDirectory"], str(self.root))
            self.assertEqual(value["ProgramArguments"][-2:], ["--role", role])
            self.assertNotIn("EnvironmentVariables", value)
            self.assertNotIn("s" * 40, plistlib.dumps(value).decode())
            self.assertNotIn("AbandonProcessGroup", value)

    def test_native_command_keeps_default_permissions_and_no_disconnect_exit(self):
        command = service.leader_command(self.config)
        self.assertEqual(command[command.index("--permission-mode") + 1], "default")
        self.assertIn("--no-exit-on-disconnect", command)
        self.assertIn("--no-auto-update", command)
        self.assertNotIn("session/load", command)
        self.assertNotIn("--yolo", command)
        self.assertNotIn("s" * 40, " ".join(command))

    def test_bridge_command_uses_secret_path_and_existing_source(self):
        command = service.bridge_command(self.config)
        self.assertEqual(command[command.index("--secret-file") + 1], str(self.secret))
        self.assertEqual(command[command.index("--leader-socket") + 1], self.config.leader_socket)
        self.assertNotIn("s" * 40, " ".join(command))

    def test_venv_interpreter_path_is_preserved_for_service_and_bridge(self):
        interpreter = "/tmp/grok-bridge/.venv/bin/python"
        with patch.object(service.sys, "executable", interpreter):
            self.assertEqual(service.bridge_command(self.config)[0], interpreter)
            for value in service.make_plists(self.config, self.root / "service.json").values():
                self.assertEqual(value["ProgramArguments"][0], interpreter)

    def test_runtime_environment_excludes_inherited_keys_and_test_overrides(self):
        with patch.dict(os.environ, {"FAKE_API_KEY": "secret", "GROK_HOME": "/wrong",
                                    "GROK_LEADER_SOCKET": "/wrong", "HOME": "/wrong"}):
            env = service.runtime_environment(self.config)
        self.assertNotIn("FAKE_API_KEY", env)
        self.assertEqual(env["GROK_HOME"], str(self.root))
        self.assertNotIn("GROK_LEADER_SOCKET", env)
        self.assertEqual(env["HOME"], str(Path.home()))

    def test_secure_config_write_roundtrip_refuses_changed_configuration(self):
        path = self.root / "state/service.json"
        service.persist_config(self.config, path)
        self.assertEqual(service.read_config(path), self.config)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        service.persist_config(self.config, path)
        with self.assertRaisesRegex(ValueError, "different"):
            service.persist_config(replace(self.config, port=18810), path)
        self.assertEqual(service.read_config(path), self.config)

    def test_config_rejects_unknown_fields_and_symlinks(self):
        path = self.root / "config"
        path.write_text(json.dumps({**self.config.as_dict(), "apiKey": "bad"}))
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "fields"):
            service.read_config(path)
        link = self.root / "config-link"
        link.symlink_to(path)
        with self.assertRaises((OSError, ValueError)):
            service.read_config(link)

    def test_install_port_conflict_cannot_kill_or_replace_process(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            config = replace(self.config, port=listener.getsockname()[1])
            with patch.object(service.subprocess, "run") as launchctl:
                with self.assertRaisesRegex(ValueError, "occupied"):
                    service.ensure_port_free(config)
                launchctl.assert_not_called()


class LeaderGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_owner_is_reused_until_it_exits(self):
        with (patch.object(service, "probe_leader", AsyncMock(side_effect=["ready", "ready", "missing"])) as probe,
              patch.object(service.asyncio, "sleep", AsyncMock()) as sleep):
            await service.wait_until_unowned(Path("/safe/leader.sock"))
        self.assertEqual(probe.await_count, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_unready_or_incompatible_listener_is_never_replaced(self):
        with (patch.object(service, "probe_leader", AsyncMock(return_value="occupied")),
              patch.object(service.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))):
            with self.assertRaises(asyncio.CancelledError):
                await service.wait_until_unowned(Path("/safe/leader.sock"))

    async def test_missing_socket_is_startable(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(await service.probe_leader(Path(directory) / "missing.sock"), "missing")

    async def test_bad_registration_with_actual_listener_is_occupied(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "leader.sock"
            async def reject(reader, writer):
                writer.close()
                await writer.wait_closed()
            server = await asyncio.start_unix_server(reject, path)
            try:
                self.assertEqual(await service.probe_leader(path), "occupied")
            finally:
                server.close()
                await server.wait_closed()


if __name__ == "__main__":
    unittest.main()

"""Bounded federation/ownership regressions. Native interoperability is separate."""
from __future__ import annotations
import asyncio
from contextlib import suppress
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp import ClientSession, WSServerHandshakeError, WSMsgType, web

spec = importlib.util.spec_from_file_location("directory_bridge", Path(__file__).with_name("bridge.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class NativeFixture:
    def __init__(self, path, rows):
        self.path, self.rows = path, rows
        self.writers, self.seen = set(), []
        self.protocol, self.version, self.capable = 1, "1.0.45", True
        self.loads, self.creates, self.prompts, self.approvals = [], [], [], []
        self.execution_cwd = str(path.parent)
        self.fail_roster = False
        self.before_owner_auth = None
        self.drop_load = False
        self.updates = []
        self.catalog_gate = None
        self.catalog_entered = asyncio.Event()
        self.catalog_error = False
        self.server = None

    async def start(self):
        self.server = await asyncio.start_unix_server(self.handle, path=self.path)
        return self

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in tuple(self.writers):
            writer.close()
            await writer.wait_closed()

    async def emit(self, value):
        for writer in tuple(self.writers):
            await bridge.write_frame(writer, {"type": "acp", "payload": json.dumps(value)})

    async def handle(self, reader, writer):
        self.writers.add(writer)
        try:
            await bridge.read_frame(reader)
            await bridge.write_frame(writer, {"type": "registered", "ready": True,
                "leader_protocol_version": self.protocol, "leader_binary_version": self.version})
            while True:
                value = json.loads((await bridge.read_frame(reader))["payload"])
                self.seen.append(value)
                method, params = value.get("method"), value.get("params", {})
                result = {}
                if method == "initialize":
                    result = {"_meta": {"x.ai/residentOnlyAttach": self.capable,
                                         "x.ai/residentActorIdentity": self.capable}}
                elif method == "authenticate":
                    if self.before_owner_auth and value.get("id") == bridge.INTERNAL_PREFIX + "owner-auth":
                        callback, self.before_owner_auth = self.before_owner_auth, None
                        callback()
                elif method in {bridge.ROSTER_METHOD, "x.ai/sessions/list"}:
                    if self.fail_roster:
                        await bridge.write_frame(writer, {"type": "acp", "payload": json.dumps(bridge.rpc_error(value, "No roster"))})
                        continue
                    result = {"result": {"sessions": [dict(row) for row in self.rows]}}
                elif method == "_x.ai/session/info":
                    result = {"result": {"cwd": self.execution_cwd}}
                elif method == "_x.ai/commands/list":
                    self.catalog_entered.set()
                    if self.catalog_gate is not None:
                        await self.catalog_gate.wait()
                    if self.catalog_error:
                        await bridge.write_frame(writer, {"type": "acp", "payload": json.dumps(bridge.rpc_error(value, "Discovery failed"))})
                        continue
                    result = {"commands": [{"name": "test-skill", "description": "native filtered catalog"}]}
                elif method == bridge.PREVIEW_METHOD:
                    start, count = params["offset"], params["limit"]
                    result = {"updates": self.updates[start:start + count],
                        "totalCount": len(self.updates), "hasMore": start + count < len(self.updates)}
                elif method == "session/load":
                    self.loads.append(value)
                    if self.drop_load:
                        return
                    row = next(r for r in self.rows if r["sessionId"] == params["sessionId"])
                    if params.get("_meta", {}).get("x.ai/resident_only") and not row["resident"]:
                        await bridge.write_frame(writer, {"type": "acp", "payload": json.dumps(bridge.rpc_error(value, "Gone"))})
                        continue
                    row.update(resident=True, activity="idle", residentActorId=row.get("residentActorId", "new-actor"))
                    result = {"_meta": {"x.ai/residentActorId": row["residentActorId"]} if params.get("_meta", {}).get("x.ai/resident_only") else {}}
                    await bridge.write_frame(writer, {"type": "acp", "payload": json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": row["sessionId"], "update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "saved replay"}}}})})
                elif method == "session/prompt":
                    self.prompts.append(value)
                elif method == "session/new":
                    self.creates.append(value)
                elif not method:
                    self.approvals.append(value)
                    continue
                if "id" in value:
                    await bridge.write_frame(writer, {"type": "acp", "payload": json.dumps({"jsonrpc": "2.0", "id": value["id"], "result": result})})
        except (asyncio.IncompleteReadError, ConnectionError, StopIteration):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


class DirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # macOS /tmp resolves through /private/tmp; no parent symlink accepted.
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.primary = await NativeFixture(self.root / "leader.sock", [self.row("p", True, "idle", "actor-p"), self.row("saved", False, "dormant")]).start()
        self.secondary = await NativeFixture(self.root / "leader-second.sock", [self.row("s", True, "working", "actor-s"), self.row("p", False, "dormant"), self.row("saved", False, "dormant")]).start()
        self.runtime = bridge.Bridge(self.primary.path, "x" * 40, "1.0.45")
        self.runner = web.AppRunner(bridge.make_app(self.runtime)); await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0); await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ws"
        self.http = ClientSession()
        self.sockets = []
        self.number = 0
        self.extra = []

    def row(self, sid, resident, activity, actor=None):
        return {"sessionId": sid, "cwd": str(self.root), "resident": resident,
                "activity": activity, "title": sid, "lastChangeUnixMs": 1,
                **({"residentActorId": actor} if actor else {})}

    async def asyncTearDown(self):
        for ws in self.sockets: await ws.close()
        await self.http.close(); await self.runner.cleanup()
        await self.primary.stop(); await self.secondary.stop()
        for fixture in self.extra: await fixture.stop()
        self.temp.cleanup()

    async def rpc(self, ws, method, params):
        self.number += 1
        identifier = self.number
        await ws.send_json({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        async with asyncio.timeout(5):
            while True:
                value = await ws.receive_json()
                if value.get("id") == identifier and "method" not in value:
                    return value

    async def connect(self, token=None):
        ws = await self.http.ws_connect(self.url + ("?leader=" + token if token else ""), headers={"Authorization": "Bearer " + "x" * 40})
        self.sockets.append(ws)
        self.assertNotIn("error", await self.rpc(ws, "initialize", {}))
        return ws

    async def roster(self, ws):
        return bridge.unwrap_result(await self.rpc(ws, bridge.ROSTER_METHOD, {}))

    async def resume(self, ws, sid="saved"):
        return await self.rpc(ws, "session/load", {"sessionId": sid, "cwd": str(self.root), "mcpServers": [], "_meta": {"x.ai/resumeSaved": True}})

    async def test_idle_preview_pages_native_envelopes_without_loading_or_retaining(self):
        sid = "019a1638-6387-7290-89fb-eb0f77d06cc8"
        self.primary.rows.append(self.row(sid, False, "dormant"))
        self.primary.updates = [{"timestamp": index, "method": "session/update", "params": {
            "sessionId": sid, "_meta": {"eventId": f"{sid}-{index}"},
            "update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": f"message {index}"}},
        }} for index in range(3)]
        ws = await self.connect()
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=({(sid, str(self.root))}, None))):
            first = bridge.unwrap_result(await self.rpc(ws, bridge.PREVIEW_METHOD, {"sessionId": sid, "cwd": str(self.root), "offset": 0, "limit": 2}))
            self.assertEqual(first["updates"], self.primary.updates[:2])
            self.assertEqual(first["totalCount"], 3)
            self.assertTrue(first["hasMore"])
            second = bridge.unwrap_result(await self.rpc(ws, bridge.PREVIEW_METHOD, {"sessionId": sid, "cwd": str(self.root), "offset": 2, "limit": 2}))
            self.assertEqual(second["updates"], self.primary.updates[2:])
            self.assertFalse(second["hasMore"])
        self.assertFalse(self.primary.loads)
        self.assertFalse(self.secondary.loads)
        self.assertFalse(self.runtime.owners)
        self.assertFalse(next(row for row in self.primary.rows if row["sessionId"] == sid)["resident"])
        await ws.close()
        self.assertFalse(self.runtime.owners)

    async def test_idle_preview_rejects_traversal_unbounded_and_streaming_parameters(self):
        sid = "019a1638-6387-7290-89fb-eb0f77d06cc8"
        ws = await self.connect()
        base = {"sessionId": sid, "cwd": str(self.root)}
        invalid = [{"sessionId": "../summary.json"}, {"sessionId": "missing"},
            {"cwd": "."}, {"cwd": str(self.root / "..")}, {"offset": -1}, {"offset": True},
            {"limit": 0}, {"limit": 501}, {"limit": True}, {"stream": True},
            {"_meta": {"x.ai/leaderClientId": 1}}]
        for change in invalid:
            with self.subTest(change=change):
                reply = await self.rpc(ws, bridge.PREVIEW_METHOD, {**base, **change})
                self.assertIn("error", reply)
        self.assertFalse(any(request.get("method") == bridge.PREVIEW_METHOD for request in self.primary.seen))
        self.assertFalse(self.primary.loads)

    async def test_idle_preview_requires_canonical_membership_workspace_and_existing_row(self):
        sid = "019a1638-6387-7290-89fb-eb0f77d06cc8"
        self.primary.rows.append(self.row(sid, False, "dormant"))
        ws = await self.connect()
        params = {"sessionId": sid, "cwd": str(self.root)}
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(set(), None))):
            self.assertEqual((await self.rpc(ws, bridge.PREVIEW_METHOD, params))["error"]["data"]["code"], "workspace_membership_required")
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=({(sid, str(self.root))}, None))):
            self.assertEqual((await self.rpc(ws, bridge.PREVIEW_METHOD, {**params, "cwd": "/"}))["error"]["data"]["code"], "workspace_unavailable")
            self.assertEqual((await self.rpc(ws, bridge.PREVIEW_METHOD, {**params, "sessionId": "019a1638-6387-7290-89fb-eb0f77d06cc9"}))["error"]["data"]["code"], "session_unavailable")
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(set(), "read_timeout"))):
            self.assertEqual((await self.rpc(ws, bridge.PREVIEW_METHOD, params))["error"]["data"]["code"], "workspace_membership_unavailable")
        self.assertFalse(any(request.get("method") == bridge.PREVIEW_METHOD for request in self.primary.seen))
        self.assertFalse(self.primary.loads)

    async def test_idle_preview_capability_is_advertised_without_resuming(self):
        ws = await self.connect()
        reply = await self.rpc(ws, "initialize", {})
        self.assertTrue(reply["result"]["_meta"]["x.ai/idleSessionPreview"])
        self.assertTrue(reply["result"]["_meta"]["x.ai/workspaceCommands"])
        self.assertFalse(self.primary.loads)

    async def test_workspace_catalog_uses_native_no_session_path_without_actor(self):
        ws = await self.connect()
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=({("saved", str(self.root))}, None))):
            reply = await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root)})
            self.assertNotIn("error", reply)
        requests = [request for request in self.primary.seen if request.get("method") == "_x.ai/commands/list"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["params"], {"cwd": str(self.root)})
        self.assertFalse(self.primary.loads)
        self.assertFalse(self.runtime.owners)

    async def test_workspace_catalog_rejects_nonmember_wrong_cwd_and_policy_overrides(self):
        ws = await self.connect()
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(set(), None))):
            self.assertIn("error", await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root)}))
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=({("saved", str(self.root))}, None))):
            for params in ({"cwd": "/"}, {"cwd": "."}, {"cwd": str(self.root / "..")},
                {"cwd": str(self.root), "kind": "chat"}, {"cwd": str(self.root), "_meta": {}},
                {"cwd": str(self.root), "refresh": 1}, {}):
                with self.subTest(params=params):
                    self.assertIn("error", await self.rpc(ws, "_x.ai/commands/list", params))
        self.assertFalse(any(request.get("method") == "_x.ai/commands/list" for request in self.primary.seen))
        self.assertFalse(self.primary.loads)

    async def test_workspace_catalog_coalesces_connections_and_reuses_native_metadata(self):
        first, second = await self.connect(), await self.connect()
        self.primary.catalog_gate = asyncio.Event()
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=({("saved", str(self.root))}, None))):
            a = asyncio.create_task(self.rpc(first, "_x.ai/commands/list", {"cwd": str(self.root)}))
            await asyncio.wait_for(self.primary.catalog_entered.wait(), 1)
            b = asyncio.create_task(self.rpc(second, "_x.ai/commands/list", {"cwd": str(self.root)}))
            # Both directory guards finish before releasing the only native scan.
            await asyncio.sleep(0.05)
            self.primary.catalog_gate.set()
            replies = await asyncio.gather(a, b)
            self.assertTrue(all("error" not in reply for reply in replies))
            self.assertNotIn("error", await self.rpc(second, "_x.ai/commands/list", {"cwd": str(self.root)}))
        requests = [r for r in self.primary.seen if r.get("method") == "_x.ai/commands/list"]
        self.assertEqual(len(requests), 1)
        self.assertFalse(self.primary.loads)
        self.assertFalse(self.runtime.owners)

    async def test_workspace_catalog_invalidates_expiry_force_and_trust_config_writes(self):
        ws = await self.connect()
        async def request(**extra):
            self.assertNotIn("error", await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root), **extra}))
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=({("saved", str(self.root))}, None))):
            await request()
            await request(refresh=True)
            key = next(iter(self.runtime.directory.catalogs))
            _, result = self.runtime.directory.catalogs[key]
            self.runtime.directory.catalogs[key] = (0, result)
            await request()
            (self.root / "trusted_folders.toml").write_text("changed trust decision")
            await request()
            grok = self.root / ".grok"; grok.mkdir()
            (grok / "config.toml").write_text("changed policy")
            await request()
        requests = [r for r in self.primary.seen if r.get("method") == "_x.ai/commands/list"]
        self.assertEqual(len(requests), 5)
        self.assertTrue(all(r["params"] == {"cwd": str(self.root)} for r in requests))

    async def test_workspace_catalog_native_errors_are_not_cached_and_membership_is_rechecked(self):
        ws = await self.connect()
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=({("saved", str(self.root))}, None))):
            self.primary.catalog_error = True
            self.assertIn("error", await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root)}))
            self.primary.catalog_error = False
            self.assertNotIn("error", await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root)}))
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(set(), None))):
            self.assertIn("error", await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root)}))
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(set(), "read_failed"))):
            self.assertIn("error", await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root)}))
        self.assertEqual(len([r for r in self.primary.seen if r.get("method") == "_x.ai/commands/list"]), 2)

    async def test_workspace_catalog_isolates_source_and_workspace(self):
        other = self.root / "other"; other.mkdir()
        self.primary.rows.append({**self.row("other", False, "dormant"), "cwd": str(other)})
        primary, secondary = await self.connect(), await self.connect(self.runtime.directory.token(self.secondary.path))
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=({("saved", str(self.root)), ("other", str(other))}, None))):
            self.assertNotIn("error", await self.rpc(primary, "_x.ai/commands/list", {"cwd": str(self.root)}))
            self.assertNotIn("error", await self.rpc(secondary, "_x.ai/commands/list", {"cwd": str(self.root)}))
            self.assertNotIn("error", await self.rpc(primary, "_x.ai/commands/list", {"cwd": str(other)}))
        self.assertEqual(len([r for r in self.primary.seen if r.get("method") == "_x.ai/commands/list"]), 2)
        self.assertEqual(len([r for r in self.secondary.seen if r.get("method") == "_x.ai/commands/list"]), 1)

    async def test_workspace_catalog_owner_disconnect_and_timeout_release_other_waiters(self):
        first, second = await self.connect(), await self.connect()
        self.primary.catalog_gate = asyncio.Event()
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=({("saved", str(self.root))}, None))):
            task = asyncio.create_task(self.rpc(second, "_x.ai/commands/list", {"cwd": str(self.root)}))
            await asyncio.wait_for(self.primary.catalog_entered.wait(), 1)
            waiter = asyncio.create_task(self.rpc(first, "_x.ai/commands/list", {"cwd": str(self.root)}))
            await asyncio.sleep(0.05)
            # Closing the scanning connection frees the shared flight; a later
            # request can discover again instead of inheriting a dead future.
            await second.close()
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual((await waiter)["error"]["data"]["code"], "catalog_unavailable")
            async with asyncio.timeout(1):
                while self.runtime.directory.catalog_flights:
                    await asyncio.sleep(0)
            self.primary.catalog_entered.clear()
            with patch.object(bridge, "CATALOG_TIMEOUT", 0.05):
                reply = await self.rpc(first, "_x.ai/commands/list", {"cwd": str(self.root)})
                self.assertEqual(reply["error"]["data"]["code"], "catalog_unavailable")
            self.assertFalse(self.runtime.directory.catalog_flights)
            self.primary.catalog_gate.set()
        self.assertFalse(self.runtime.owners)

    async def test_workspace_catalog_cache_is_bounded_and_source_stamp_invalidates(self):
        other = self.root / "other"; other.mkdir()
        self.primary.rows.append({**self.row("other", False, "dormant"), "cwd": str(other)})
        ws = await self.connect()
        members = {("saved", str(self.root)), ("other", str(other))}
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(members, None))), patch.object(bridge, "MAX_CATALOGS", 1):
            self.assertNotIn("error", await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root)}))
            os.utime(self.primary.path, ns=(1, 1))
            self.assertNotIn("error", await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root)}))
            self.assertNotIn("error", await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(other)}))
            self.assertEqual(len(self.runtime.directory.catalogs), 1)
        self.assertEqual(len([r for r in self.primary.seen if r.get("method") == "_x.ai/commands/list"]), 3)

    async def test_workspace_catalog_source_removal_does_not_reuse_cached_response(self):
        ws = await self.connect()
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=({("saved", str(self.root))}, None))):
            self.assertNotIn("error", await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root)}))
            self.primary.path.unlink()
            # The already authenticated native connection can finish a read,
            # but an unlinked socket must not reuse its previous cache key.
            self.assertNotIn("error", await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root)}))
        self.assertEqual(len([r for r in self.primary.seen if r.get("method") == "_x.ai/commands/list"]), 2)

    async def test_workspace_catalog_inaccessible_config_cannot_serve_cached_metadata(self):
        ws = await self.connect()
        real_stat = Path.stat
        target = self.root / "config.toml"
        def guarded_stat(path, *args, **kwargs):
            if path == target:
                raise PermissionError("Optional config is inaccessible")
            return real_stat(path, *args, **kwargs)
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=({("saved", str(self.root))}, None))):
            self.assertNotIn("error", await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root)}))
            with patch.object(Path, "stat", guarded_stat):
                reply = await self.rpc(ws, "_x.ai/commands/list", {"cwd": str(self.root)})
                self.assertEqual(reply["error"]["data"]["code"], "catalog_unavailable")
        self.assertEqual(len([r for r in self.primary.seen if r.get("method") == "_x.ai/commands/list"]), 1)

    async def test_union_keeps_working_idle_saved_and_resident_wins(self):
        ws = await self.connect(); result = await self.roster(ws)
        self.assertEqual({(r["sessionId"], r["activity"]) for r in result["sessions"]}, {("p", "idle"), ("s", "working"), ("saved", "dormant")})
        self.assertEqual(len(result["sessions"]), 3)
        self.assertFalse(result["bridgePartial"])
        for row in result["sessions"]:
            self.assertEqual(len(row["bridgeLeaderId"]), 24)
            self.assertNotIn(str(self.root), row["bridgeLeaderId"])
        self.assertEqual(next(r for r in result["sessions"] if r["sessionId"] == "saved")["bridgeLeaderId"], self.runtime.directory.token(self.primary.path))

    async def test_same_sid_distinct_actors_are_separate_origins(self):
        self.secondary.rows.append(self.row("p", True, "needs_input", "actor-p-other"))
        result = await self.runtime.directory.snapshot(fresh=True)
        rows = [r for r in result["sessions"] if r["sessionId"] == "p"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({(r["bridgeLeaderId"], r["residentActorId"]) for r in rows}), 2)

    async def test_workspace_membership_marks_idle_history_and_refresh_removal(self):
        membership = {("saved", str(self.root))}
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(membership, None))):
            result = await self.runtime.directory.snapshot(fresh=True)
        rows = {row["sessionId"]: row for row in result["sessions"]}
        self.assertTrue(rows["saved"]["workspaceMember"])
        self.assertFalse(rows["saved"]["resident"])
        self.assertEqual(rows["saved"]["activity"], "dormant")
        self.assertNotIn("residentActorId", rows["saved"])
        self.assertFalse(rows["p"]["workspaceMember"])
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(set(), None))):
            refreshed = await self.runtime.directory.snapshot(fresh=True)
        saved = next(row for row in refreshed["sessions"] if row["sessionId"] == "saved")
        self.assertFalse(saved["workspaceMember"])
        self.assertEqual(len(refreshed["sessions"]), len(result["sessions"]))
        self.assertFalse(self.primary.loads + self.secondary.loads)

    async def test_membership_matches_sid_and_normalized_cwd_without_changing_live_source(self):
        self.secondary.rows.append({**self.row("p", True, "needs_input", "actor-p-other"), "cwd": str(self.root / "other")})
        members = {("p", os.path.normpath(str(self.root / ".")))}
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(members, None))):
            result = await self.runtime.directory.snapshot(fresh=True)
        rows = [row for row in result["sessions"] if row["sessionId"] == "p"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["residentActorId"] for row in rows}, {"actor-p", "actor-p-other"})
        self.assertTrue(next(row for row in rows if row["residentActorId"] == "actor-p")["workspaceMember"])
        other = next(row for row in rows if row["residentActorId"] == "actor-p-other")
        self.assertFalse(other["workspaceMember"])
        self.assertEqual(other["bridgeLeaderId"], self.runtime.directory.token(self.secondary.path))

    async def test_membership_failure_preserves_live_actors_and_source_loss_still_blocks_resume(self):
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(set(), "read_timeout"))):
            result = await self.runtime.directory.snapshot(fresh=True)
        self.assertEqual(result["bridgeWorkspaceMembershipError"], "read_timeout")
        self.assertEqual({row["residentActorId"] for row in result["sessions"] if row["resident"]}, {"actor-p", "actor-s"})
        self.assertFalse(result["bridgeResumeBlocked"])
        self.assertTrue(all(not row["workspaceMember"] for row in result["sessions"]))
        await self.secondary.stop()
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=({("saved", str(self.root))}, None))):
            result = await self.runtime.directory.snapshot(fresh=True)
            self.assertTrue(result["bridgeResumeBlocked"])
            self.assertTrue(next(row for row in result["sessions"] if row["sessionId"] == "saved")["workspaceMember"])
            ws = await self.connect()
            reply = await self.resume(ws)
        self.assertEqual(reply["error"]["data"]["code"], "source_unavailable")
        self.assertFalse(self.primary.loads)

    async def test_native_deltas_preserve_membership_and_refresh_removal_is_authoritative(self):
        ws = await self.connect()
        members = {("saved", str(self.root))}
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(members, None))):
            await self.runtime.directory.snapshot(fresh=True)
        native_row = self.primary.rows[1]
        self.assertNotIn("workspaceMember", native_row)
        await self.primary.emit({"jsonrpc": "2.0", "method": "_x.ai/sessions/changed", "params": {"upserted": [native_row], "removed": []}})
        changed = await asyncio.wait_for(ws.receive_json(), 2)
        row = changed["params"]["upserted"][0]
        self.assertTrue(row["workspaceMember"])
        self.assertFalse(row["resident"])
        self.assertEqual(row["bridgeLeaderId"], self.runtime.directory.token(self.primary.path))
        self.assertEqual(self.runtime.directory.cached_at, 0)
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(set(), "read_timeout"))):
            degraded = await self.runtime.directory.snapshot(fresh=True)
        self.assertEqual(degraded["bridgeWorkspaceMembershipError"], "read_timeout")
        self.assertTrue(next(row for row in degraded["sessions"] if row["sessionId"] == "saved")["workspaceMember"])
        await self.primary.emit({"jsonrpc": "2.0", "method": "_x.ai/sessions/changed", "params": {"upserted": [native_row], "removed": []}})
        changed = await asyncio.wait_for(ws.receive_json(), 2)
        self.assertTrue(changed["params"]["upserted"][0]["workspaceMember"])
        with patch.object(self.runtime.directory, "read_workspace_membership", AsyncMock(return_value=(set(), None))):
            await self.runtime.directory.snapshot(fresh=True)
        await self.primary.emit({"jsonrpc": "2.0", "method": "_x.ai/sessions/changed", "params": {"upserted": [native_row], "removed": []}})
        changed = await asyncio.wait_for(ws.receive_json(), 2)
        self.assertFalse(changed["params"]["upserted"][0]["workspaceMember"])
        self.assertFalse(self.primary.loads + self.secondary.loads)

    async def test_source_selection_routes_load_prompt_approval_and_catalog(self):
        token = self.runtime.directory.token(self.secondary.path)
        ws = await self.connect(token)
        self.assertNotIn("error", await self.rpc(ws, "session/load", {"sessionId": "s", "cwd": str(self.root)}))
        await self.rpc(ws, "_x.ai/commands/list", {"sessionId": "s", "cwd": str(self.root)})
        await self.rpc(ws, "session/prompt", {"sessionId": "s", "prompt": [{"type": "text", "text": "secondary only"}]})
        await ws.send_json({"jsonrpc": "2.0", "id": "approval", "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}}})
        await asyncio.sleep(.01)
        self.assertEqual(len(self.secondary.prompts), 1); self.assertFalse(self.primary.prompts)
        self.assertEqual(self.secondary.prompts[0]["params"]["_meta"]["x.ai/residentActorId"], "actor-s")
        self.assertTrue(self.secondary.approvals); self.assertFalse(self.primary.approvals)
        self.assertTrue(any(v.get("method") == "_x.ai/commands/list" for v in self.secondary.seen))
        result = await self.rpc(ws, "session/new", {"cwd": str(self.root)})
        self.assertEqual(result["error"]["data"]["code"], "primary_required")
        self.assertFalse(self.secondary.creates)

    async def test_unknown_token_raw_path_and_wrong_secret_never_connect(self):
        before = len(self.primary.writers) + len(self.secondary.writers)
        for query, secret, status in [("?leader=unknown", "x" * 40, 400), ("?leader=/tmp/arbitrary.sock", "x" * 40, 400), ("", "wrong", 401)]:
            with self.assertRaises(WSServerHandshakeError) as caught:
                await self.http.ws_connect(self.url + query, headers={"Authorization": "Bearer " + secret})
            self.assertEqual(caught.exception.status, status)
        self.assertEqual(len(self.primary.writers) + len(self.secondary.writers), before)

    async def test_stale_symlink_and_untrusted_uid_are_not_live_sources(self):
        stale = self.root / "leader-stale.sock"
        server = await asyncio.start_unix_server(lambda r,w: None, path=stale); server.close(); await server.wait_closed()
        (self.root / "leader-link.sock").symlink_to(self.secondary.path)
        result = await self.runtime.directory.snapshot(fresh=True)
        self.assertEqual(len(result["bridgeSources"]), 3)
        self.assertTrue(result["bridgePartial"]); self.assertFalse(result["bridgeResumeBlocked"])
        self.assertNotIn(self.runtime.directory.token(self.root / "leader-link.sock"), self.runtime.directory.sources)
        with patch.object(bridge.os, "getuid", return_value=os.getuid() + 1):
            directory = bridge.LeaderDirectory(self.primary.path, "x" * 40, "1.0.45")
            self.assertEqual(len(directory.sources), 0)

    async def test_incompatible_reachable_source_blocks_saved_resume(self):
        self.secondary.version = "1.0.62"
        ws = await self.connect(); result = await self.roster(ws)
        self.assertTrue(result["bridgePartial"]); self.assertTrue(result["bridgeResumeBlocked"])
        reply = await self.resume(ws)
        self.assertEqual(reply["error"]["data"]["code"], "source_unavailable")
        self.assertFalse(self.primary.loads)

    async def test_readonly_legacy_source_lists_all_sessions_but_never_attaches(self):
        self.secondary.capable = False
        ws = await self.connect(); result = await self.roster(ws)
        row = next(r for r in result["sessions"] if r["sessionId"] == "s")
        self.assertFalse(row["bridgeAttachSupported"])
        self.assertEqual(row["bridgeAttachUnsupportedReason"], "upgrade_required")
        self.assertTrue(result["bridgePartial"])
        self.assertFalse(result["bridgeResumeBlocked"])
        source = next(s for s in result["bridgeSources"] if s["bridgeLeaderId"] == row["bridgeLeaderId"])
        self.assertTrue(source["available"])
        self.assertFalse(source["ownershipUnknown"])
        self.assertEqual(source["error"], "upgrade_required")
        with self.assertRaises(WSServerHandshakeError) as caught:
            await self.connect(row["bridgeLeaderId"])
        self.assertEqual(caught.exception.status, 426)
        # Source's live ownership still prevents a second actor from its history.
        self.primary.rows.append(self.row("s", False, "dormant"))
        reply = await self.resume(ws, "s")
        self.assertEqual(reply["error"]["data"]["code"], "origin_conflict")
        self.assertFalse(self.secondary.loads)
        self.assertFalse(self.secondary.prompts)
        self.assertFalse(self.primary.loads)

    async def test_readonly_source_loss_still_blocks_saved_resume(self):
        self.secondary.capable = False
        await self.runtime.directory.snapshot(fresh=True)
        await self.secondary.stop()
        result = await self.runtime.directory.snapshot(fresh=True)
        self.assertTrue(result["bridgeResumeBlocked"])
        self.assertFalse(any(r["sessionId"] == "s" for r in result["sessions"]))

    async def test_source_death_has_no_dormant_fallback_and_recovery_reconciles(self):
        await self.runtime.directory.snapshot(fresh=True)
        self.primary.rows.append(self.row("s", False, "dormant"))
        await self.secondary.stop()
        result = await self.runtime.directory.snapshot(fresh=True)
        self.assertFalse(any(r["sessionId"] == "s" for r in result["sessions"]))
        self.assertTrue(result["bridgeResumeBlocked"])
        with self.assertRaises(WSServerHandshakeError): await self.connect(self.runtime.directory.token(self.secondary.path))
        ws = await self.connect(); reply = await self.resume(ws, "s")
        self.assertEqual(reply["error"]["data"]["code"], "source_unavailable")
        self.assertFalse(self.primary.loads)
        await self.secondary.start()
        result = await self.runtime.directory.snapshot(fresh=True)
        self.assertEqual(next(r for r in result["sessions"] if r["sessionId"] == "s")["residentActorId"], "actor-s")
        self.assertFalse(result["bridgeResumeBlocked"])

    async def test_saved_resume_retains_actor_and_replays_via_strict_attach(self):
        ws = await self.connect(); reply = await self.resume(ws)
        self.assertNotIn("error", reply)
        self.assertEqual(reply["result"]["_meta"]["x.ai/residentActorId"], "new-actor")
        self.assertEqual(len(self.primary.loads), 3)

        self.assertNotIn("x.ai/resumeSaved", self.primary.loads[0]["params"]["_meta"])
        self.assertTrue(self.primary.loads[2]["params"]["_meta"]["x.ai/resident_only"])
        await ws.close(); await asyncio.sleep(.02)
        self.assertEqual(len(self.runtime.owners), 1)
        owner = next(iter(self.runtime.owners)); self.assertEqual(owner.session_id, "saved")
        self.assertFalse(owner.task.done())
        second = await self.connect(); reply = await self.resume(second)
        self.assertEqual(reply["error"]["data"]["code"], "origin_conflict")
        self.assertEqual(len(self.primary.loads), 3)

    async def test_saved_resume_is_confined_to_primary_native_coordinator(self):
        ws = await self.connect(self.runtime.directory.token(self.secondary.path))
        reply = await self.resume(ws)
        self.assertEqual(reply["error"]["data"]["code"], "primary_required")
        self.assertFalse(self.primary.loads)
        self.assertFalse(self.secondary.loads)
        self.assertFalse(self.runtime.owners)

    async def test_saved_resume_rechecks_owner_after_retainer_auth_race(self):
        ws = await self.connect()
        # First discovery authenticates both sources; set the race only after it.
        await self.runtime.directory.snapshot(fresh=True)
        count = 0
        original = self.runtime.directory.snapshot
        async def snapshot(**kwargs):
            nonlocal count
            result = await original(**kwargs); count += 1
            if count == 1:
                self.primary.before_owner_auth = lambda: self.secondary.rows.append(self.row("saved", True, "working", "racing-actor"))
            return result
        with patch.object(self.runtime.directory, "snapshot", side_effect=snapshot):
            reply = await self.resume(ws)
        self.assertEqual(reply["error"]["data"]["code"], "origin_conflict")
        self.assertEqual(reply["error"]["data"]["bridgeLeaderId"], self.runtime.directory.token(self.secondary.path))
        self.assertFalse(self.primary.loads)

    async def test_resume_ownership_cap_and_unknown_outcome_never_retry(self):
        ws = await self.connect()
        with patch.object(bridge, "MAX_OWNED_SESSIONS", 0):
            self.assertIn("error", await self.resume(ws))
        self.assertFalse(self.primary.loads)
        self.primary.drop_load = True
        await ws.send_json({"jsonrpc": "2.0", "id": "uncertain", "method": "session/load", "params": {"sessionId": "saved", "cwd": str(self.root), "_meta": {"x.ai/resumeSaved": True}}})
        result = await asyncio.wait_for(ws.receive(), 5)
        self.assertEqual(result.type, WSMsgType.CLOSE); self.assertEqual(result.data, 1011)
        self.assertEqual(len(self.primary.loads), 1)

    async def test_saved_worktree_uses_resumed_execution_workspace_for_both_strict_loads(self):
        actual = self.root / "actual-worktree"; actual.mkdir()
        self.primary.execution_cwd = str(actual)
        ws = await self.connect(); reply = await self.resume(ws)
        self.assertNotIn("error", reply)
        self.assertEqual(reply["result"]["_meta"]["x.ai/bridgeExecutionCwd"], str(actual))
        self.assertEqual([v["params"]["cwd"] for v in self.primary.loads], [str(self.root), str(actual), str(actual)])
        self.assertNotIn("error", await self.rpc(ws, "_x.ai/commands/list", {"sessionId": "saved", "cwd": str(actual)}))
        self.assertIn("error", await self.rpc(ws, "_x.ai/commands/list", {"sessionId": "saved", "cwd": str(self.root)}))

    async def test_simultaneous_saved_resume_creates_one_actor_and_returns_origin_conflict(self):
        first, second = await self.connect(), await self.connect()
        results = await asyncio.gather(self.resume(first), self.resume(second))
        self.assertEqual(sum("error" not in result for result in results), 1)
        rejected = next(result for result in results if "error" in result)
        self.assertEqual(rejected["error"]["data"]["code"], "origin_conflict")
        self.assertEqual(len(self.primary.loads), 3)
        self.assertEqual(len(self.runtime.owners), 1)

    async def test_registered_socket_replaced_by_symlink_cannot_be_selected(self):
        token = self.runtime.directory.token(self.secondary.path)
        await self.secondary.stop(); self.secondary.path.unlink()
        self.secondary.path.symlink_to(self.primary.path)
        with self.assertRaises(WSServerHandshakeError) as caught: await self.connect(token)
        self.assertEqual(caught.exception.status, 400)

    async def test_source_death_between_roster_and_strict_attach_never_restores(self):
        ws = await self.connect(self.runtime.directory.token(self.secondary.path))
        self.secondary.drop_load = True
        await ws.send_json({"jsonrpc": "2.0", "id": "dying", "method": "session/load", "params": {"sessionId": "s", "cwd": str(self.root)}})
        value = await asyncio.wait_for(ws.receive(), 5)
        self.assertEqual(value.type, WSMsgType.CLOSE); self.assertEqual(value.data, 1011)
        self.assertEqual(len(self.secondary.loads), 1)
        self.assertTrue(self.secondary.loads[0]["params"]["_meta"]["x.ai/resident_only"])
        self.assertFalse(self.primary.loads)

    async def test_resume_rejects_stale_actor_cursor_and_wrong_workspace(self):
        ws = await self.connect()
        for meta in [{"x.ai/resumeSaved": False}, {"x.ai/resumeSaved": True, "cursor": "old"}, {"x.ai/resumeSaved": True, "x.ai/residentActorId": "old-actor"}]:
            reply = await self.rpc(ws, "session/load", {"sessionId": "saved", "cwd": str(self.root), "_meta": meta})
            self.assertIn("error", reply)
        reply = await self.rpc(ws, "session/load", {"sessionId": "saved", "cwd": "/", "_meta": {"x.ai/resumeSaved": True}})
        self.assertEqual(reply["error"]["data"]["code"], "workspace_unavailable")
        self.assertFalse(self.primary.loads)

    async def test_notifications_are_scoped_to_selected_origin(self):
        token = self.runtime.directory.token(self.secondary.path); ws = await self.connect(token)
        await self.secondary.emit({"jsonrpc": "2.0", "method": "_x.ai/sessions/changed", "params": {"upserted": [self.secondary.rows[0]], "removed": ["elsewhere"]}})
        value = await ws.receive_json()
        self.assertEqual(value["params"]["bridgeLeaderId"], token)
        self.assertEqual(value["params"]["upserted"][0]["bridgeLeaderId"], token)

    async def test_concurrent_roster_reads_coalesce_and_bound_registry(self):
        results = await asyncio.gather(*(self.runtime.directory.snapshot() for _ in range(8)))
        self.assertTrue(all(r is results[0] for r in results))
        self.assertEqual(sum(v.get("method") == bridge.ROSTER_METHOD for v in self.primary.seen), 1)
        with patch.object(bridge, "MAX_LEADER_SOURCES", 1):
            directory = bridge.LeaderDirectory(self.primary.path, "x" * 40, "1.0.45")
            result = await directory.snapshot(fresh=True)
            self.assertEqual(len(directory.sources), 1)
            self.assertTrue(result["bridgePartial"]); self.assertTrue(result["bridgeResumeBlocked"])


if __name__ == "__main__": unittest.main()

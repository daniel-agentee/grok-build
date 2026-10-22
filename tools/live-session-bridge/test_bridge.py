"""Transport tests use a fake leader; live interoperability is tested separately."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, WSServerHandshakeError, WSMsgType, web

spec = importlib.util.spec_from_file_location("bridge", Path(__file__).with_name("bridge.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.socket = Path(self.temp.name) / "leader.sock"
        self.seen = asyncio.Queue()
        self.native_methods = []
        self.closed = asyncio.Event()
        self.roster = [{"sessionId": "live-1", "cwd": "/workspace", "resident": True}]
        self.execution_cwd = "/workspace"
        self.version = "1.0.45"
        self.protocol = 1
        self.ready_type = "leader_ready"
        self.resident_only_supported = True
        self.actor_identity_supported = True
        self.answer_roster = True
        self.answer_load = True
        self.owner_auth_error = False
        self.model_catalog = {
            "currentModelId": "catalog-base",
            "availableModels": [
                {"modelId": "catalog-base", "name": "Catalog Base", "_meta": {
                    "supportsReasoningEffort": True,
                    "reasoningEfforts": [{"id": "thorough", "value": "high", "label": "Thorough", "default": True}],
                }},
                {"modelId": "catalog-fast", "name": "Catalog Fast", "_meta": {
                    "supportsReasoningEffort": True,
                    "reasoningEfforts": [{"id": "deep-thinking", "value": "xhigh", "label": "Deep thinking", "default": True}],
                }},
                {"modelId": "catalog-no-reasoning", "name": "No effort selector", "_meta": {
                    "supportsReasoningEffort": False,
                }},
            ],
        }
        self.model_catalog_error = False
        self.answer_model_catalog = True
        self.drop_registration = False
        self.leader_writers = []
        self.server = await asyncio.start_unix_server(self.leader, path=self.socket)
        self.bridge = bridge.Bridge(self.socket, "x" * 40, "1.0.45")
        self.runner = web.AppRunner(bridge.make_app(self.bridge))
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/ws"
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        self.server.close()
        await self.server.wait_closed()
        for writer in self.leader_writers:
            writer.close()
            await writer.wait_closed()
        self.temp.cleanup()

    async def leader(self, reader, writer):
        self.leader_writers.append(writer)
        try:
            registration = await bridge.read_frame(reader)
            await self.seen.put(registration)
            if self.drop_registration:
                return
            await bridge.write_frame(
                writer,
                {
                    "type": "registered",
                    "ready": False,
                    "leader_protocol_version": self.protocol,
                    "leader_binary_version": self.version,
                },
            )
            await bridge.write_frame(writer, {"type": self.ready_type})
            while True:
                frame = await bridge.read_frame(reader)
                await self.seen.put(frame)
                value = json.loads(frame["payload"])
                self.native_methods.append(value.get("method"))
                if value.get("method") == "initialize" and value.get("id") in {
                    "bridge-test-init",
                    bridge.INTERNAL_PREFIX + "capability",
                }:
                    await bridge.write_frame(
                        writer,
                        {
                            "type": "acp",
                            "payload": json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": value["id"],
                                    "result": {
                                        "_meta": {
                                            "x.ai/residentOnlyAttach": self.resident_only_supported,
                                            "x.ai/residentActorIdentity": self.actor_identity_supported,
                                        }
                                    },
                                }
                            ),
                        },
                    )
                elif value.get("method") == "authenticate" and value.get("id") == bridge.INTERNAL_PREFIX + "owner-auth":
                    auth_response = {"jsonrpc": "2.0", "id": value["id"], "result": {}}
                    if self.owner_auth_error:
                        auth_response = {"jsonrpc": "2.0", "id": value["id"], "error": {"code": -32000, "message": "Sign in on Mac"}}
                    await bridge.write_frame(writer, {
                        "type": "acp", "payload": json.dumps(auth_response),
                    })
                elif value.get("method") == "_x.ai/models/list" and value.get("id") == bridge.INTERNAL_PREFIX + "owner-models":
                    if not self.answer_model_catalog:
                        continue
                    response = {"jsonrpc": "2.0", "id": value["id"], "result": {"result": self.model_catalog}}
                    if self.model_catalog_error:
                        response = bridge.rpc_error(value, "Native model catalog unavailable")
                    await bridge.write_frame(writer, {"type": "acp", "payload": json.dumps(response)})
                elif value.get("method") == bridge.ROSTER_METHOD:
                    if not self.answer_roster:
                        continue
                    await bridge.write_frame(
                        writer,
                        {
                            "type": "acp",
                            "payload": json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": value["id"],
                                    "result": {"result": {"sessions": self.roster}},
                                }
                            ),
                        },
                    )
                elif value.get("method") == "session/load":
                    if not self.answer_load:
                        continue
                    if value["params"].get("cwd") != self.execution_cwd:
                        await bridge.write_frame(
                            writer,
                            {
                                "type": "acp",
                                "payload": json.dumps(
                                    bridge.rpc_error(value, "live attachment workspace differs")
                                ),
                            },
                        )
                        continue
                    await bridge.write_frame(
                        writer,
                        {
                            "type": "acp",
                            "payload": json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "method": "session/update",
                                    "params": {
                                        "sessionId": "live-1",
                                        "_meta": {"eventId": "live-1-3", "isReplay": True},
                                        "update": {
                                            "sessionUpdate": "user_message_chunk",
                                            "content": {"type": "text", "text": "terminal prompt"},
                                        },
                                    },
                                }
                            ),
                        },
                    )
                    await bridge.write_frame(
                        writer,
                        {
                            "type": "acp",
                            "payload": json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": value["id"],
                                    "result": {
                                        "sessionId": "live-1",
                                        "_meta": {"x.ai/residentActorId": "actor-1"},
                                    },
                                }
                            ),
                        },
                    )
        except (asyncio.IncompleteReadError, ConnectionError):
            self.closed.set()
        finally:
            writer.close()
            await writer.wait_closed()

    async def connect(self):
        ws = await self.client.ws_connect(self.url, headers={"Authorization": "Bearer " + "x" * 40})
        registration = await self.seen.get()
        self.assertTrue(registration["capabilities"]["user_message_echo"])
        self.assertNotIn("yolo_mode", registration["capabilities"])
        await ws.send_json(
            {"jsonrpc": "2.0", "id": "bridge-test-init", "method": "initialize", "params": {}}
        )
        await ws.receive_json()
        await self.seen.get()
        return ws

    async def load(self, ws, cwd="/workspace", session_id="live-1"):
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/load",
                "params": {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
            }
        )
        return await ws.receive_json()

    async def emit_native(self, value):
        await bridge.write_frame(
            self.leader_writers[-1], {"type": "acp", "payload": json.dumps(value)}
        )

    async def next_forward(self, method):
        async with asyncio.timeout(2):
            while True:
                frame = await self.seen.get()
                if frame.get("type") == "acp":
                    value = json.loads(frame["payload"])
                    if value.get("method") == method:
                        return value

    async def assert_session_ended(self, ws):
        event = await ws.receive_json()
        self.assertIn("sessions/changed", event["method"])
        closed = await ws.receive()
        self.assertEqual(closed.type, WSMsgType.CLOSE)
        self.assertEqual(closed.data, 4409)
        await asyncio.wait_for(self.closed.wait(), 1)

    async def test_native_removal_revokes_attached_actor_in_all_wire_forms(self):
        for method, params in [
            ("x.ai/sessions/changed", {"removed": ["live-1"]}),
            ("_x.ai/sessions/changed", {"removed": ["live-1"]}),
            (
                "_x.ai/sessions/changed",
                {"method": "x.ai/sessions/changed", "params": {"removed": ["live-1"]}},
            ),
        ]:
            with self.subTest(method=method, params=params):
                self.closed.clear()
                ws = await self.connect()
                await self.load(ws)
                await ws.receive_json()  # successful load response after replay
                while not self.seen.empty():
                    self.seen.get_nowait()
                await self.emit_native({"jsonrpc": "2.0", "method": method, "params": params})
                await self.assert_session_ended(ws)
                self.assertTrue(
                    self.seen.empty(), "Lifecycle revocation must not send cancel/close"
                )

    async def test_unrelated_removal_keeps_current_actor_attached(self):
        ws = await self.connect()
        await self.load(ws)
        await ws.receive_json()
        await self.emit_native(
            {"method": "_x.ai/sessions/changed", "params": {"removed": ["other-session"]}}
        )
        await ws.receive_json()
        self.assertFalse(ws.closed)
        await ws.close()

    async def test_old_resident_only_capability_cannot_attach(self):
        self.actor_identity_supported = False
        ws = await self.connect()
        reply = await self.load(ws)
        self.assertIn("error", reply)
        self.assertTrue(
            self.seen.empty(), "Missing incarnation support must fail before native load"
        )
        await ws.close()

    async def test_reconnect_actor_assertion_passes_through_and_controls_use_returned_token(self):
        ws = await self.connect()
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/load",
                "params": {
                    "sessionId": "live-1",
                    "cwd": "/workspace",
                    "_meta": {"x.ai/residentActorId": "prior-actor"},
                },
            }
        )
        await ws.receive_json()
        result = await ws.receive_json()
        self.assertEqual(result["result"]["_meta"]["x.ai/residentActorId"], "actor-1")
        await self.seen.get()
        load = json.loads((await self.seen.get())["payload"])
        self.assertEqual(load["params"]["_meta"]["x.ai/residentActorId"], "prior-actor")
        for number, method in enumerate(
            ["session/prompt", "session/cancel", "session/set_model"], 10
        ):
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": number,
                    "method": method,
                    "params": {"sessionId": "live-1"},
                }
            )
            forwarded = json.loads((await self.seen.get())["payload"])
            self.assertEqual(forwarded["params"]["_meta"], {"x.ai/residentActorId": "actor-1"})
        await ws.close()

    async def test_invalid_actor_assertion_never_reaches_native_load(self):
        ws = await self.connect()
        for value in [None, "", True, {"actor": "x"}]:
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "session/load",
                    "params": {
                        "sessionId": "live-1",
                        "cwd": "/workspace",
                        "_meta": {"x.ai/residentActorId": value},
                    },
                }
            )
            self.assertIn("error", await ws.receive_json())
            self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_native_removal_revokes_pending_discovery(self):
        self.answer_roster = False
        ws = await self.connect()
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/load",
                "params": {"sessionId": "live-1", "cwd": "/workspace"},
            }
        )
        preflight = json.loads((await self.seen.get())["payload"])
        await self.emit_native(
            {"method": "_x.ai/sessions/changed", "params": {"removed": ["live-1"]}}
        )
        # A delayed resident roster must never reauthorize the now-ended actor.
        await self.emit_native({"id": preflight["id"], "result": {"sessions": self.roster}})
        await self.assert_session_ended(ws)
        self.assertTrue(self.seen.empty())

    async def test_native_removal_revokes_forwarded_load_before_late_success(self):
        self.answer_load = False
        ws = await self.connect()
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/load",
                "params": {"sessionId": "live-1", "cwd": "/workspace"},
            }
        )
        await self.seen.get()  # private roster
        await self.seen.get()  # native resident-only load
        await self.emit_native(
            {"method": "_x.ai/sessions/changed", "params": {"removed": ["live-1"]}}
        )
        await self.emit_native({"id": 7, "result": {"sessionId": "live-1"}})
        await self.assert_session_ended(ws)
        self.assertTrue(self.seen.empty())

    async def test_auth_rejects_missing_wrong_and_browser_origin(self):
        for headers, status in [
            ({}, 401),
            ({"Authorization": "Bearer wrong"}, 401),
            ({"Authorization": "Bearer " + "x" * 40, "Origin": "https://malicious.test"}, 403),
        ]:
            with self.assertRaises(WSServerHandshakeError) as caught:
                await self.client.ws_connect(self.url, headers=headers)
            self.assertEqual(caught.exception.status, status)
        self.assertTrue(self.seen.empty(), "Unauthenticated clients must never reach leader")

    async def test_health_checks_handshake_instead_of_stale_socket_presence(self):
        async with self.client.get(self.url.replace("/ws", "/health")) as response:
            self.assertTrue((await response.json())["leaderAvailable"])
        self.server.close()
        await self.server.wait_closed()
        self.assertTrue(self.socket.exists(), "a dead process can leave its socket behind")
        async with self.client.get(self.url.replace("/ws", "/health")) as response:
            self.assertFalse((await response.json())["leaderAvailable"])

    async def test_version_mismatch_fails_before_upgrade(self):
        self.version = "1.0.38"
        with self.assertRaises(WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.url, headers={"Authorization": "Bearer " + "x" * 40})
        self.assertEqual(caught.exception.status, 503)

    async def test_launcher_probe_requires_native_capability_even_on_matching_version(self):
        _, writer = await bridge.connect_leader(self.socket, "1.0.45", require_capability=True)
        writer.close()
        await writer.wait_closed()
        self.resident_only_supported = False
        with self.assertRaisesRegex(ValueError, "resident-only"):
            await bridge.connect_leader(self.socket, "1.0.45", require_capability=True)

    async def test_wrong_protocol_not_ready_and_absent_leader_return_503(self):
        for protocol, ready_type in [(99, "leader_ready"), (1, "shutdown")]:
            self.protocol, self.ready_type = protocol, ready_type
            with self.assertRaises(WSServerHandshakeError) as caught:
                await self.client.ws_connect(
                    self.url, headers={"Authorization": "Bearer " + "x" * 40}
                )
            self.assertEqual(caught.exception.status, 503)
        self.server.close()
        await self.server.wait_closed()
        self.socket.unlink()
        with self.assertRaises(WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.url, headers={"Authorization": "Bearer " + "x" * 40})
        self.assertEqual(caught.exception.status, 503)

    async def test_pending_request_count_is_bounded(self):
        ws = await self.connect()
        previous = bridge.MAX_PENDING_REQUESTS
        bridge.MAX_PENDING_REQUESTS = 2
        try:
            for identifier in [1, 2]:
                await ws.send_json(
                    {"jsonrpc": "2.0", "id": identifier, "method": "initialize", "params": {}}
                )
                await self.seen.get()
            await ws.send_json({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}})
            response = await ws.receive_json()
            self.assertIn("Too many", response["error"]["message"])
            self.assertTrue(self.seen.empty())
        finally:
            bridge.MAX_PENDING_REQUESTS = previous
            await ws.close()

    async def test_absolute_matching_live_workspace_required(self):
        ws = await self.connect()
        for cwd, sid in [(".", "live-1"), ("/wrong", "live-1"), ("/workspace", "missing")]:
            self.assertIn("error", await self.load(ws, cwd, sid))
        self.roster[0]["resident"] = False
        self.assertIn("error", await self.load(ws))
        await ws.close()

    async def test_worktree_execution_workspace_can_differ_from_roster_display_path(self):
        self.roster[0]["cwd"] = "/project"
        self.execution_cwd = "/project/.worktree/example"
        ws = await self.connect()
        replay = await self.load(ws, cwd=self.execution_cwd)
        self.assertEqual(replay["method"], "session/update")
        self.assertIn("result", await ws.receive_json())
        await self.seen.get()
        load = json.loads((await self.seen.get())["payload"])
        self.assertEqual(load["params"]["cwd"], self.execution_cwd)
        self.assertTrue(load["params"]["_meta"]["x.ai/resident_only"])
        await ws.close()

    async def test_replay_then_live_and_disconnect_only_detaches(self):
        ws = await self.connect()
        replay = await self.load(ws)
        self.assertEqual(replay["params"]["_meta"]["eventId"], "live-1-3")
        self.assertEqual((await ws.receive_json())["id"], 7)
        preflight = json.loads((await self.seen.get())["payload"])
        self.assertEqual(preflight["params"]["sessionId"], "live-1")  # native subscription pin
        load = json.loads((await self.seen.get())["payload"])
        self.assertIs(load["params"]["_meta"]["x.ai/restore_code"], False)
        self.assertIs(load["params"]["_meta"]["x.ai/resident_only"], True)
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "session/prompt",
                "params": {"sessionId": "live-1", "prompt": [{"type": "text", "text": "phone"}]},
            }
        )
        self.assertEqual(json.loads((await self.seen.get())["payload"])["id"], 8)
        # Pending approval ids are preserved so native first-answer-wins still applies.
        approval = {
            "jsonrpc": "2.0",
            "id": "permission:9",
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
        }
        await ws.send_json(approval)
        self.assertEqual(json.loads((await self.seen.get())["payload"]), approval)
        await ws.close()
        await asyncio.wait_for(self.closed.wait(), 2)
        self.assertTrue(self.seen.empty(), "Disconnect must not send cancellation or close-session")
        self.closed.clear()
        ws = await self.connect()
        # Reconnect cursor is passed through, never interpreted or rewritten by bridge.
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "session/load",
                "params": {
                    "sessionId": "live-1",
                    "cwd": "/workspace",
                    "mcpServers": [],
                    "_meta": {"cursor": "live-1-3"},
                },
            }
        )
        await self.seen.get()
        load = json.loads((await self.seen.get())["payload"])
        self.assertEqual(load["params"]["_meta"]["cursor"], "live-1-3")
        await ws.close()

    async def test_cannot_create_agent_or_prompt_unattached(self):
        ws = await self.connect()
        for method in ["session/new", "session/resume", "session/prompt"]:
            await ws.send_json(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"sessionId": "live-1"}}
            )
            self.assertIn("error", await ws.receive_json())
        self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_unpatched_leader_is_rejected_instead_of_cold_restoring(self):
        self.resident_only_supported = False
        ws = await self.connect()
        response = await self.load(ws)
        self.assertIn("cannot guarantee live-only", response["error"]["message"])
        self.assertTrue(self.seen.empty(), "Unsupported leader must not receive session/load")
        await ws.close()

    async def test_bad_params_and_bad_roster_fail_closed(self):
        ws = await self.connect()
        await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "session/prompt", "params": []})
        self.assertIn("error", await ws.receive_json())
        await ws.send_json(
            {"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": []}}
        )
        self.assertIn("error", await ws.receive_json())
        self.roster = [None, 1, "bad", {}]
        self.assertIn("error", await self.load(ws))
        await ws.close()

    async def test_pending_preflight_bounded_and_times_out(self):
        self.answer_roster = False
        ws = await self.connect()
        previous_timeout = bridge.PREFLIGHT_TIMEOUT
        bridge.PREFLIGHT_TIMEOUT = 0.05
        try:
            for index in range(bridge.MAX_PENDING_LOADS + 1):
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": index,
                        "method": "session/load",
                        "params": {"sessionId": "live-1", "cwd": "/workspace"},
                    }
                )
            replies = [
                await asyncio.wait_for(ws.receive_json(), 2)
                for _ in range(bridge.MAX_PENDING_LOADS + 1)
            ]
            self.assertEqual(len({reply["id"] for reply in replies}), len(replies))
            self.assertTrue(all("error" in reply for reply in replies))
            self.assertTrue(any("Too many" in reply["error"]["message"] for reply in replies))
            self.assertTrue(any("timed out" in reply["error"]["message"] for reply in replies))
        finally:
            bridge.PREFLIGHT_TIMEOUT = previous_timeout
            await ws.close()

    async def test_malformed_json_closes_transport(self):
        ws = await self.connect()
        await ws.send_str("not JSON")
        await ws.receive()
        self.assertEqual(ws.close_code, 1007)

    async def test_nonfinite_json_closes_transport_without_forwarding(self):
        ws = await self.connect()
        await ws.send_str('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"x":NaN}}')
        await ws.receive()
        self.assertEqual(ws.close_code, 1007)
        self.assertTrue(self.seen.empty())

    async def test_load_cannot_replace_tools_or_configuration(self):
        ws = await self.connect()
        for extra in [
            {"_meta": {"yoloMode": True}},
            {"_meta": {"autoMode": True}},
            {"_meta": {"toolOverrides": {}}},
            {"mcpServers": ["replacement"]},
            {"permissionMode": "always-approve"},
        ]:
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session/load",
                    "params": {"sessionId": "live-1", "cwd": "/workspace", **extra},
                }
            )
            self.assertIn("error", await ws.receive_json())
        self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_prompt_cannot_override_live_mode_or_tools(self):
        ws = await self.connect()
        await self.load(ws)
        await ws.receive_json()
        await self.seen.get()
        await self.seen.get()
        for extra in [
            {"_meta": {"toolOverrides": {}}},
            {"_meta": {"mode": "agent"}},
            {"yoloMode": True},
            {"cwd": "/other"},
        ]:
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "session/prompt",
                    "params": {"sessionId": "live-1", "prompt": [], **extra},
                }
            )
            self.assertIn("error", await ws.receive_json())
        self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_initialize_auth_and_read_methods_cannot_change_configuration(self):
        ws = await self.connect()
        for method, params in [
            ("initialize", {"_meta": {"systemPromptOverride": "fixture override"}}),
            ("initialize", {"_meta": {"bufferingSettings": {}}}),
            ("initialize", {"clientCapabilities": {"_meta": {"arbitraryConfig": True}}}),
            ("authenticate", {"methodId": "xai.api_key", "_meta": {"xaiApiKey": "fixture-only"}}),
            ("authenticate", {"methodId": "cached_token", "_meta": {"reauth": True}}),
            ("authenticate", {"methodId": []}),
            ("x.ai/billing", {"setLimit": 1}),
            ("_x.ai/sessions/list", {"workspace": "/other"}),
            ("x.ai/session/info", {"sessionId": "live-1", "includeCredentials": True}),
            ("x.ai/companion/config_get", {}),
            ("x.ai/companion/mermaid_render", {}),
        ]:
            await ws.send_json({"jsonrpc": "2.0", "id": 99, "method": method, "params": params})
            self.assertIn("error", await ws.receive_json(), (method, params))
        self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_leader_handshake_eof_is_service_unavailable(self):
        self.drop_registration = True
        with self.assertRaises(WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.url, headers={"Authorization": "Bearer " + "x" * 40})
        self.assertEqual(caught.exception.status, 503)

    async def test_malformed_rpc_envelopes_never_reach_leader(self):
        ws = await self.connect()
        invalid = [
            {"id": [], "method": "initialize"},
            {"id": {}, "method": "initialize"},
            {"id": True, "method": "initialize"},
            {"id": 1.5, "method": "initialize"},
            {"id": 1, "method": []},
            {"id": 1, "method": "initialize", "result": {}},
            {"id": 1, "result": {}, "error": {}},
            {"id": 1},
            {"method": "session/load", "params": {"sessionId": "live-1", "cwd": "/workspace"}},
            {"method": "session/new", "params": {"cwd": self.temp.name}},
            {"method": "session/prompt", "params": {"sessionId": "live-1"}},
        ]
        for value in invalid:
            await ws.send_json({"jsonrpc": "2.0", **value})
            self.assertIn("error", await asyncio.wait_for(ws.receive_json(), 2), value)
        self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_duplicate_pending_request_closes_without_forwarding_twice(self):
        ws = await self.connect()
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        await ws.send_json(request)
        self.assertEqual(json.loads((await self.seen.get())["payload"]), request)
        await ws.send_json(request)
        self.assertEqual((await asyncio.wait_for(ws.receive(), 1)).type, WSMsgType.CLOSE)
        self.assertEqual(ws.close_code, 1008)
        self.assertTrue(self.seen.empty(), "A duplicate ID must not execute a second request")

    async def test_numeric_and_string_load_ids_cannot_attach_the_wrong_session(self):
        self.answer_load = False
        self.roster.append({"sessionId": "live-2", "cwd": "/workspace", "resident": True})
        ws = await self.connect()
        for identifier, sid in [(7, "live-1"), ("7", "live-2")]:
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "method": "session/load",
                    "params": {"sessionId": sid, "cwd": "/workspace"},
                }
            )
            await self.seen.get()  # roster
            await self.seen.get()  # load
        await bridge.write_frame(
            self.leader_writers[-1],
            {
                "type": "acp",
                "payload": json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "result": {
                            "sessionId": "live-1",
                            "_meta": {"x.ai/residentActorId": "actor-1"},
                        },
                    }
                ),
            },
        )
        self.assertEqual((await ws.receive_json())["id"], 7)
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "session/prompt",
                "params": {"sessionId": "live-2", "prompt": []},
            }
        )
        self.assertIn(
            "error",
            await asyncio.wait_for(ws.receive_json(), 2),
            "An unrelated load response must not authorize live-2",
        )
        await ws.close()

    async def test_load_limit_includes_requests_already_forwarded_to_leader(self):
        self.answer_load = False
        ws = await self.connect()
        for identifier in range(bridge.MAX_PENDING_LOADS):
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "method": "session/load",
                    "params": {"sessionId": "live-1", "cwd": "/workspace"},
                }
            )
            await self.seen.get()
            await self.seen.get()
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "session/load",
                "params": {"sessionId": "live-1", "cwd": "/workspace"},
            }
        )
        self.assertIn("error", await asyncio.wait_for(ws.receive_json(), 0.3))
        self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_leader_shutdown_closes_for_reconnect_without_cancelling(self):
        ws = await self.connect()
        await bridge.write_frame(self.leader_writers[-1], {"type": "shutdown"})
        self.assertEqual((await asyncio.wait_for(ws.receive(), 1)).type, WSMsgType.CLOSE)
        self.assertEqual(ws.close_code, 1012)
        await asyncio.wait_for(self.closed.wait(), 2)
        self.assertTrue(self.seen.empty())

    async def test_binary_and_oversize_frames_close_only_phone(self):
        ws = await self.connect()
        await ws.send_bytes(b"binary ACP is unsupported")
        await asyncio.wait_for(ws.receive(), 1)
        self.assertEqual(ws.close_code, 1003)
        self.assertTrue(self.seen.empty())
        previous = bridge.MAX_FRAME
        bridge.MAX_FRAME = 1024
        try:
            ws = await self.connect()
            await ws.send_str("x" * 1025)
            await asyncio.wait_for(ws.receive(), 1)
            self.assertEqual(ws.close_code, 1009)
            self.assertTrue(self.seen.empty())
        finally:
            bridge.MAX_FRAME = previous

    async def test_unattached_mutations_and_agent_creation_extensions_are_rejected(self):
        ws = await self.connect()
        for method in [
            "session/cancel",
            "session/set_model",
            "session/set_mode",
            "x.ai/session/fork",
            "_x.ai/session/fork",
            "x.ai/session/rehydrate",
            "x.ai/session/import",
            "x.ai/session/close",
            "x.ai/getApiKey",
            "x.ai/setApiKey",
            "x.ai/companion/config_set",
        ]:
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": method,
                    "method": method,
                    "params": {"sessionId": "live-1"},
                }
            )
            self.assertIn("error", await ws.receive_json(), method)
        self.assertTrue(self.seen.empty())
        await self.load(ws)
        await ws.receive_json()
        await self.seen.get()
        await self.seen.get()
        cancel = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "session/cancel",
            "params": {"sessionId": "live-1"},
        }
        await ws.send_json(cancel)
        cancel["params"]["_meta"] = {"x.ai/residentActorId": "actor-1"}
        self.assertEqual(json.loads((await self.seen.get())["payload"]), cancel)
        await ws.close()

    async def test_new_session_uses_mac_workspace_and_requires_strict_load_before_control(self):
        ws = await self.connect()
        request = {
            "jsonrpc": "2.0", "id": "create", "method": "session/new",
            "params": {"cwd": self.temp.name, "mcpServers": [], "_meta": {}},
        }
        await ws.send_json(request)
        forwarded = await self.next_forward("session/new")
        self.assertEqual(forwarded["params"]["cwd"], str(Path(self.temp.name).resolve()))
        self.assertEqual(forwarded["params"]["_meta"], {})
        self.assertNotIn("_x.ai/models/list", self.native_methods,
                         "Default creation must preserve native defaults without settings discovery")
        await self.emit_native({"jsonrpc": "2.0", "id": "create", "result": {"sessionId": "new-1"}})
        self.assertEqual((await ws.receive_json())["result"]["sessionId"], "new-1")
        await ws.send_json({
            "jsonrpc": "2.0", "id": "prompt", "method": "session/prompt",
            "params": {"sessionId": "new-1", "prompt": []},
        })
        self.assertIn("error", await ws.receive_json())
        self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_new_session_model_settings_capability_requires_native_actor_guarantees(self):
        for supported in (True, False):
            self.actor_identity_supported = supported
            ws = await self.client.ws_connect(self.url, headers={"Authorization": "Bearer " + "x" * 40})
            self.assertEqual((await self.seen.get())["type"], "register")
            await ws.send_json({"jsonrpc": "2.0", "id": "bridge-test-init", "method": "initialize", "params": {}})
            reply = await ws.receive_json()
            self.assertIs(reply["result"]["_meta"]["x.ai/newSessionModelSettings"], supported)
            await self.next_forward("initialize")
            await ws.close()

    async def test_new_session_forwards_only_advertised_model_and_canonical_effort_on_creator(self):
        ws = await self.connect()
        settings = {"modelId": "catalog-fast", "reasoningEffort": "xhigh"}
        await ws.send_json({
            "jsonrpc": "2.0", "id": "advanced-create", "method": "session/new",
            "params": {"cwd": self.temp.name, "mcpServers": [], "_meta": settings},
        })
        catalog = await self.next_forward("_x.ai/models/list")
        self.assertEqual(catalog["params"], {})
        creator = self.leader_writers[-1]
        forwarded = await self.next_forward("session/new")
        self.assertIs(self.leader_writers[-1], creator)
        self.assertEqual(forwarded["params"], {
            "cwd": str(Path(self.temp.name).resolve()), "mcpServers": [], "_meta": settings,
        })
        await self.emit_native({"jsonrpc": "2.0", "id": "advanced-create", "result": {
            "sessionId": "new-with-settings", "models": {"currentModelId": "catalog-fast"},
        }})
        self.assertEqual((await ws.receive_json())["result"]["sessionId"], "new-with-settings")
        self.assertEqual(len(self.bridge.owners), 1)
        await ws.send_json({"jsonrpc": "2.0", "id": "prompt-before-proof", "method": "session/prompt",
                            "params": {"sessionId": "new-with-settings", "prompt": []}})
        self.assertIn("error", await ws.receive_json())
        self.assertTrue(self.seen.empty(), "Advanced creation must still require strict attachment proof")
        await ws.close()

    async def test_new_session_rejects_invalid_setting_types_and_override_siblings_before_native(self):
        ws = await self.connect()
        for meta in [
            {"modelId": None}, {"modelId": False}, {"modelId": []}, {"modelId": {}},
            {"modelId": ""}, {"modelId": " catalog-fast"}, {"modelId": "catalog-fast\n"},
            {"modelId": "m" * 513}, {"reasoningEffort": "high"},
            {"modelId": "catalog-fast", "reasoningEffort": None},
            {"modelId": "catalog-fast", "reasoningEffort": True},
            {"modelId": "catalog-fast", "reasoningEffort": []},
            {"modelId": "catalog-fast", "reasoningEffort": "deep-thinking"},
            {"modelId": "catalog-fast", "reasoningEffort": "HIGH"},
            {"modelId": "catalog-fast", "reasoningEffort": "fast"},
            {"modelId": "catalog-fast", "yoloMode": True},
            {"modelId": "catalog-fast", "autoMode": False},
            {"modelId": "catalog-fast", "x.ai/residentActorId": "other-actor"},
            {"modelId": "catalog-fast", "sessionId": "other-session"},
            {"modelId": "catalog-fast", "systemPromptOverride": "override"},
            {"modelId": "catalog-fast", "fastMode": True},
        ]:
            await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "session/new",
                                "params": {"cwd": self.temp.name, "_meta": meta}})
            self.assertIn("error", await ws.receive_json(), meta)
        self.assertTrue(self.seen.empty(), "Invalid settings must not register a creator or query native")
        await ws.close()

    async def test_new_session_model_without_reasoning_preserves_native_effort_default(self):
        ws = await self.connect()
        await ws.send_json({
            "jsonrpc": "2.0", "id": "model-only", "method": "session/new",
            "params": {"cwd": self.temp.name, "_meta": {"modelId": "catalog-no-reasoning"}},
        })
        await self.next_forward("_x.ai/models/list")
        forwarded = await self.next_forward("session/new")
        self.assertEqual(forwarded["params"]["_meta"], {"modelId": "catalog-no-reasoning"})
        await self.emit_native({"jsonrpc": "2.0", "id": "model-only", "result": {"sessionId": "model-only-session"}})
        self.assertEqual((await ws.receive_json())["result"]["sessionId"], "model-only-session")
        await ws.close()

    async def test_new_session_rejects_stale_model_and_unadvertised_effort_without_creating(self):
        ws = await self.connect()
        for settings in [
            {"modelId": "not-advertised"},
            {"modelId": "catalog-base", "reasoningEffort": "xhigh"},
            {"modelId": "catalog-fast", "reasoningEffort": "low"},
            {"modelId": "catalog-no-reasoning", "reasoningEffort": "high"},
        ]:
            await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "session/new",
                                "params": {"cwd": self.temp.name, "_meta": settings}})
            self.assertEqual((await ws.receive_json())["error"]["data"]["code"], "settings_invalid")
            self.assertEqual(len(self.bridge.owners), 0)
        methods = []
        while not self.seen.empty():
            frame = self.seen.get_nowait()
            if frame.get("type") == "acp":
                methods.append(json.loads(frame["payload"])["method"])
        self.assertEqual(methods.count("_x.ai/models/list"), 4)
        self.assertNotIn("session/new", methods)
        self.assertNotIn("session/load", methods)
        await ws.close()

    async def test_new_session_model_catalog_failure_is_definite_and_never_creates(self):
        ws = await self.connect()
        for failure in ("native-error", "malformed", "timeout"):
            self.model_catalog_error = failure == "native-error"
            self.model_catalog = {} if failure == "malformed" else self.model_catalog
            self.answer_model_catalog = failure != "timeout"
            with patch.object(bridge, "CATALOG_TIMEOUT", .03):
                await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "session/new",
                                    "params": {"cwd": self.temp.name, "_meta": {"modelId": "catalog-fast"}}})
                self.assertEqual((await ws.receive_json())["error"]["data"]["code"], "settings_unavailable")
            self.assertEqual(len(self.bridge.owners), 0)
        while not self.seen.empty():
            frame = self.seen.get_nowait()
            if frame.get("type") == "acp":
                self.assertNotIn(json.loads(frame["payload"])["method"], ("session/new", "session/load"))
        await ws.close()

    async def test_new_session_rejects_missing_relative_non_directory_and_inaccessible_workspaces(self):
        ws = await self.connect()
        file = Path(self.temp.name) / "file.txt"
        file.write_text("fixture")
        for cwd in [None, 42, "", ".", "~/project", self.temp.name + "/missing", str(file), "/bad\x00path"]:
            await ws.send_json({
                "jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": cwd},
            })
            self.assertIn("error", await ws.receive_json(), repr(cwd))
        with patch.object(bridge.os, "access", return_value=False):
            await ws.send_json({
                "jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": self.temp.name},
            })
            self.assertIn("error", await ws.receive_json())
        self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_new_session_cannot_override_permissions_tools_identity_or_configuration(self):
        ws = await self.connect()
        for extra in [
            {"_meta": {"yoloMode": True}}, {"_meta": {"autoMode": True}},
            {"_meta": {"sessionId": "chosen-id"}}, {"_meta": {"systemPromptOverride": "override"}},
            {"mcpServers": [{"command": "replacement"}]}, {"sessionId": "chosen-id"},
            {"permissionMode": "always-approve"}, {"_meta": None}, {"mcpServers": None},
        ]:
            await ws.send_json({
                "jsonrpc": "2.0", "id": 1, "method": "session/new",
                "params": {"cwd": self.temp.name, **extra},
            })
            self.assertIn("error", await ws.receive_json(), extra)
        self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_new_session_allows_only_one_pending_create_and_releases_on_reply(self):
        ws = await self.connect()
        async def create(identifier):
            await ws.send_json({
                "jsonrpc": "2.0", "id": identifier, "method": "session/new",
                "params": {"cwd": self.temp.name},
            })
        await create(1)
        await self.next_forward("session/new")
        await create(2)
        self.assertIn("error", await ws.receive_json())
        self.assertTrue(self.seen.empty())
        await self.emit_native({"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "fixture"}})
        self.assertIn("error", await ws.receive_json())
        await create(3)
        self.assertEqual((await self.next_forward("session/new"))["id"], 3)
        await ws.close()

    async def test_new_session_requires_initialize_with_resident_capability(self):
        ws = await self.client.ws_connect(self.url, headers={"Authorization": "Bearer " + "x" * 40})
        await self.seen.get()
        await ws.send_json({
            "jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": self.temp.name},
        })
        self.assertIn("error", await ws.receive_json())
        self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_unrelated_reply_cannot_release_pending_creation_and_disconnect_sends_no_cancel(self):
        ws = await self.connect()
        await ws.send_json({
            "jsonrpc": "2.0", "id": 7, "method": "session/new", "params": {"cwd": self.temp.name},
        })
        await self.next_forward("session/new")
        # The JSON string "7" cannot complete the numeric request 7.
        await self.emit_native({"jsonrpc": "2.0", "id": "7", "result": {"sessionId": "unrelated"}})
        await ws.send_json({
            "jsonrpc": "2.0", "id": 8, "method": "session/new", "params": {"cwd": self.temp.name},
        })
        self.assertIn("error", await ws.receive_json())
        self.assertTrue(self.seen.empty())
        await ws.close()
        await asyncio.wait_for(self.closed.wait(), 1)
        self.assertTrue(self.seen.empty())

    async def test_catalog_uses_attached_session_and_actual_execution_workspace(self):
        self.roster[0]["cwd"] = "/display-project"
        self.execution_cwd = "/project/.worktree/live"
        ws = await self.connect()
        await self.load(ws, cwd=self.execution_cwd)
        await ws.receive_json()
        await self.seen.get()
        await self.seen.get()
        for params in [{"sessionId": "live-1"}, {"sessionId": "live-1", "cwd": self.execution_cwd}]:
            request = {"jsonrpc": "2.0", "id": 8, "method": "_x.ai/commands/list", "params": params}
            await ws.send_json(request)
            self.assertEqual(json.loads((await asyncio.wait_for(self.seen.get(), 1))["payload"]), request)
            await self.emit_native({"jsonrpc": "2.0", "id": 8, "result": {"commands": []}})
            self.assertEqual((await ws.receive_json())["result"]["commands"], [])
        await ws.close()

    async def create_owned_fixture(self, ws, identifier=1):
        await ws.send_json({
            "jsonrpc": "2.0", "id": identifier, "method": "session/new",
            "params": {"cwd": self.temp.name},
        })
        await self.next_forward("session/new")
        owner = next(owner for owner in self.bridge.owners if not owner.result.done())
        native_writer = self.leader_writers[-1]
        response = {"jsonrpc": "2.0", "id": identifier, "result": {"sessionId": "mobile-owned"}}
        await bridge.write_frame(native_writer, {"type": "acp", "payload": json.dumps(response)})
        self.assertEqual(await ws.receive_json(), response)
        return owner, native_writer

    async def test_mobile_owner_survives_idle_phone_detach_and_drains_without_answering_permissions(self):
        ws = await self.connect()
        owner, native_writer = await self.create_owned_fixture(ws)
        await ws.close()
        await asyncio.wait_for(self.closed.wait(), 1)
        self.assertFalse(owner.task.done())
        self.assertEqual(len(self.bridge.owners), 1)
        permission = {
            "jsonrpc": "2.0", "id": "pending-permission", "method": "session/request_permission",
            "params": {"sessionId": "mobile-owned", "options": []},
        }
        await bridge.write_frame(native_writer, {"type": "acp", "payload": json.dumps(permission)})
        for index in range(12):
            # Exceeds normal socket buffers; a non-draining owner would stall.
            await asyncio.wait_for(bridge.write_frame(native_writer, {
                "type": "acp", "payload": json.dumps({
                    "jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": "mobile-owned", "sequence": index, "text": "x" * 262144,
                    },
                }),
            }), 2)
        self.assertTrue(self.seen.empty(), "Owner must never answer approvals or cancel work")
        await bridge.write_frame(native_writer, {"type": "acp", "payload": json.dumps({
            "jsonrpc": "2.0", "method": "_x.ai/sessions/changed", "params": {"removed": ["mobile-owned"]},
        })})
        await asyncio.wait_for(owner.task, 2)
        self.assertEqual(len(self.bridge.owners), 0)
        self.assertTrue(self.seen.empty())

    async def test_pending_creation_survives_phone_loss_before_response(self):
        ws = await self.connect()
        await ws.send_json({
            "jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": self.temp.name},
        })
        await self.next_forward("session/new")
        owner = next(iter(self.bridge.owners))
        native_writer = self.leader_writers[-1]
        await ws.close()
        await asyncio.wait_for(self.closed.wait(), 1)
        await bridge.write_frame(native_writer, {"type": "acp", "payload": json.dumps({
            "jsonrpc": "2.0", "id": 1, "result": {"sessionId": "created-after-detach"},
        })})
        await asyncio.wait_for(asyncio.shield(owner.result), 2)
        self.assertEqual(owner.session_id, "created-after-detach")
        self.assertFalse(owner.task.done())
        self.assertTrue(self.seen.empty())

    async def test_owner_capacity_rejects_before_create_and_native_end_releases_slot(self):
        with patch.object(bridge, "MAX_OWNED_SESSIONS", 1):
            ws = await self.connect()
            owner, native_writer = await self.create_owned_fixture(ws)
            await ws.send_json({
                "jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": self.temp.name},
            })
            failure = await ws.receive_json()
            self.assertIn("limit", failure["error"]["message"])
            self.assertTrue(self.seen.empty())
            await bridge.write_frame(native_writer, {"type": "acp", "payload": json.dumps({
                "jsonrpc": "2.0", "method": "x.ai/sessions/changed", "params": {"removed": ["mobile-owned"]},
            })})
            await asyncio.wait_for(owner.task, 2)
            self.assertEqual(len(self.bridge.owners), 0)
            await ws.send_json({
                "jsonrpc": "2.0", "id": 3, "method": "session/new", "params": {"cwd": self.temp.name},
            })
            self.assertEqual((await self.next_forward("session/new"))["id"], 3)
            await ws.close()

    async def test_owner_leader_eof_releases_slot_and_never_reconnects_or_restores(self):
        ws = await self.connect()
        owner, native_writer = await self.create_owned_fixture(ws)
        native_writer.close()
        await native_writer.wait_closed()
        await asyncio.wait_for(owner.task, 2)
        self.assertEqual(len(self.bridge.owners), 0)
        self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_creator_eof_before_result_is_transport_uncertainty_not_definite_rejection(self):
        ws = await self.connect()
        await ws.send_json({
            "jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": self.temp.name},
        })
        await self.next_forward("session/new")
        owner = next(iter(self.bridge.owners))
        native_writer = self.leader_writers[-1]
        native_writer.close()
        await native_writer.wait_closed()
        response = await asyncio.wait_for(ws.receive(), 2)
        self.assertEqual(response.type, WSMsgType.CLOSE)
        self.assertEqual(response.data, 1011)
        await asyncio.wait_for(owner.task, 2)
        self.assertIsNone(owner.result.result())
        self.assertEqual(len(self.bridge.owners), 0)
        self.assertTrue(self.seen.empty(), "An unknown outcome must never be retried")

    async def test_creation_result_deadline_releases_slot_and_reports_uncertainty(self):
        with patch.object(bridge, "CREATION_RESPONSE_TIMEOUT", 0.05):
            ws = await self.connect()
            await ws.send_json({
                "jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": self.temp.name},
            })
            await self.next_forward("session/new")
            owner = next(iter(self.bridge.owners))
            response = await asyncio.wait_for(ws.receive(), 2)
            self.assertEqual(response.type, WSMsgType.CLOSE)
            self.assertEqual(response.data, 1011)
            await asyncio.wait_for(owner.task, 2)
            self.assertEqual(len(self.bridge.owners), 0)
            self.assertTrue(self.seen.empty())

    async def test_explicit_native_creation_error_remains_definite_and_releases_owner(self):
        ws = await self.connect()
        request = {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": self.temp.name}}
        await ws.send_json(request)
        await self.next_forward("session/new")
        owner = next(iter(self.bridge.owners))
        failure = bridge.rpc_error(request, "Workspace rejected")
        await bridge.write_frame(self.leader_writers[-1], {"type": "acp", "payload": json.dumps(failure)})
        self.assertEqual(await ws.receive_json(), failure)
        self.assertFalse(ws.closed)
        await asyncio.wait_for(owner.task, 2)
        self.assertEqual(len(self.bridge.owners), 0)
        await ws.close()

    async def test_ordinary_attachment_has_no_bridge_lifetime_owner(self):
        ws = await self.connect()
        await self.load(ws)
        await ws.receive_json()
        await self.seen.get()
        await self.seen.get()
        self.assertEqual(len(self.bridge.owners), 0)
        await ws.close()

    async def test_creation_auth_failure_releases_owner_without_creating_actor(self):
        ws = await self.connect()
        self.owner_auth_error = True
        await ws.send_json({
            "jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": self.temp.name},
        })
        response = await ws.receive_json()
        self.assertIn("error", response)
        await asyncio.gather(*(owner.task for owner in tuple(self.bridge.owners)))
        self.assertEqual(len(self.bridge.owners), 0)
        while not self.seen.empty():
            frame = await self.seen.get()
            if frame.get("type") == "acp":
                self.assertNotEqual(json.loads(frame["payload"]).get("method"), "session/new")
        await ws.close()

    async def test_bridge_shutdown_closes_owner_without_cancel_or_cold_restore(self):
        ws = await self.connect()
        owner, _ = await self.create_owned_fixture(ws)
        await self.bridge.cleanup(None)
        self.assertTrue(owner.task.done())
        self.assertEqual(len(self.bridge.owners), 0)
        self.assertEqual(len(self.bridge.creations), 0)
        self.assertTrue(self.seen.empty())
        await ws.close()

    async def test_catalog_cannot_discover_arbitrary_workspaces_or_change_configuration(self):
        ws = await self.connect()
        await ws.send_json({"jsonrpc": "2.0", "id": 8, "method": "_x.ai/commands/list", "params": {"sessionId": "live-1"}})
        self.assertIn("error", await ws.receive_json())
        await self.load(ws)
        await ws.receive_json()
        await self.seen.get()
        await self.seen.get()
        for params in [
            {}, {"cwd": "/workspace"}, {"sessionId": "other"},
            {"sessionId": "live-1", "cwd": "/other"}, {"sessionId": "live-1", "kind": "chat"},
            {"sessionId": "live-1", "refresh": True}, {"sessionId": "live-1", "_meta": {}},
        ]:
            await ws.send_json({"jsonrpc": "2.0", "id": 8, "method": "_x.ai/commands/list", "params": params})
            self.assertIn("error", await ws.receive_json(), params)
        for method in ["x.ai/skills/list", "x.ai/skills/toggle", "x.ai/skills/add", "x.ai/commands/list"]:
            await ws.send_json({"jsonrpc": "2.0", "id": 8, "method": method, "params": {"sessionId": "live-1"}})
            self.assertIn("error", await ws.receive_json(), method)
        # Cwd-only discovery now checks canonical membership through native
        # read-only discovery, but must never forward this nonmember catalog.
        while not self.seen.empty():
            frame = await self.seen.get()
            if frame.get("type") == "acp":
                request = json.loads(frame["payload"])
                self.assertNotEqual(request.get("method"), "_x.ai/commands/list")
        await ws.close()

    async def test_out_of_order_replies_allow_identifier_reuse_after_completion(self):
        ws = await self.connect()
        for identifier in [1, "1"]:
            await ws.send_json(
                {"jsonrpc": "2.0", "id": identifier, "method": "initialize", "params": {}}
            )
            await self.seen.get()
        for identifier in ["1", 1]:
            response = {"jsonrpc": "2.0", "id": identifier, "result": {}}
            await bridge.write_frame(
                self.leader_writers[-1], {"type": "acp", "payload": json.dumps(response)}
            )
            self.assertEqual(await ws.receive_json(), response)
        await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(json.loads((await self.seen.get())["payload"])["id"], 1)
        await ws.close()

    async def test_disconnect_during_discovery_ignores_late_reply(self):
        self.answer_roster = False
        ws = await self.connect()
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/load",
                "params": {"sessionId": "live-1", "cwd": "/workspace"},
            }
        )
        await self.seen.get()
        await ws.close()
        await asyncio.wait_for(self.closed.wait(), 2)
        self.assertTrue(self.seen.empty(), "Phone loss must not forward load/cancel/close")


class SecretTests(unittest.TestCase):
    def test_owner_only_secret_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("s" * 40)
            path.chmod(0o600)
            self.assertEqual(bridge.load_secret(path), "s" * 40)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                bridge.load_secret(path)

    def test_symlink_and_fifo_secret_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("s" * 40)
            path.chmod(0o600)
            link = Path(directory) / "linked-secret"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                bridge.load_secret(link)
            import os

            fifo = Path(directory) / "fifo-secret"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(ValueError):
                bridge.load_secret(fifo)


if __name__ == "__main__":
    unittest.main()

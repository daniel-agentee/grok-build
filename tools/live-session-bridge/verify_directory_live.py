"""Real pinned-native two-leader acceptance in disposable workspaces.

Requires existing authenticated Mac CLI access. Does not touch user sessions or
send model prompts. Simulator/UI turn synchronization is verified separately.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import suppress
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from aiohttp import ClientSession, WSServerHandshakeError, web
import bridge


class NativeClient:
    def __init__(self, reader, writer):
        self.reader, self.writer, self.number = reader, writer, 0

    @classmethod
    async def connect(cls, socket):
        reader, writer = await bridge.connect_leader(socket, "1.0.45", require_capability=True)
        result = cls(reader, writer)
        await result.rpc("authenticate", {"methodId": "cached_token"})
        return result

    async def rpc(self, method, params):
        self.number += 1
        identifier = self.number
        await bridge.write_frame(self.writer, {"type": "acp", "payload": json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})})
        async with asyncio.timeout(60):
            while True:
                frame = await bridge.read_frame(self.reader)
                if frame.get("type") != "acp": continue
                value = json.loads(frame["payload"])
                if value.get("id") == identifier and "method" not in value:
                    assert "error" not in value, f"Native {method} rejected"
                    return bridge.unwrap_result(value)

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()


async def run(binary: Path, output: Path):
    with tempfile.TemporaryDirectory(prefix="grok-fleet-") as temporary:
        root = Path(temporary).resolve()
        primary, secondary = root / "leader-primary.sock", root / "leader-secondary.sock"
        workspace = root / "workspace"; workspace.mkdir()
        (workspace / "README.md").write_text("Disposable native federation acceptance.\n")
        processes, handles, clients = [], [], []
        runner = None
        evidence = {"binaryVersion": subprocess.check_output([str(binary), "--version"], text=True).strip(), "tests": []}
        try:
            for label, socket in [("primary", primary), ("secondary", secondary)]:
                handle = (root / f"{label}.log").open("w"); handles.append(handle)
                process = subprocess.Popen([str(binary), "--permission-mode", "default", "--leader-socket", str(socket), "agent", "leader", "--no-auto-update", "--no-exit-on-disconnect", "--relay-on-demand"], stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT)
                processes.append(process)
                deadline = time.monotonic() + 45
                while True:
                    try:
                        client = await NativeClient.connect(socket); clients.append(client); break
                    except (OSError, ValueError, asyncio.IncompleteReadError):
                        assert process.poll() is None and time.monotonic() < deadline, "Disposable native leader unavailable"
                        await asyncio.sleep(.2)
            p, s = clients
            first = await p.rpc("session/new", {"cwd": str(workspace), "mcpServers": []})
            second = await s.rpc("session/new", {"cwd": str(workspace), "mcpServers": []})
            p_sid, s_sid = first["sessionId"], second["sessionId"]
            async def strict(client, sid, actor=None):
                meta = {"x.ai/resident_only": True, "x.ai/restore_code": False}
                if actor: meta["x.ai/residentActorId"] = actor
                return await client.rpc("session/load", {"sessionId": sid, "cwd": str(workspace), "mcpServers": [], "_meta": meta})
            p_actor = (await strict(p, p_sid))["_meta"]["x.ai/residentActorId"]
            s_actor = (await strict(s, s_sid))["_meta"]["x.ai/residentActorId"]
            secret = secrets.token_urlsafe(32)
            runtime = bridge.Bridge(primary, secret, "1.0.45")
            runner = web.AppRunner(bridge.make_app(runtime)); await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
            url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ws"
            snapshot = await runtime.directory.snapshot(fresh=True)
            ours = [row for row in snapshot["sessions"] if row["sessionId"] in {p_sid, s_sid}]
            assert len(ours) == 2 and all(row["resident"] and row["activity"] == "idle" for row in ours)
            assert {row.get("residentActorId") for row in ours} == {p_actor, s_actor}, "Rebuilt native roster must expose actor UUIDs"
            assert not snapshot["bridgePartial"]
            evidence["tests"].append("real_two_origin_roster_and_actor_identity")
            async with ClientSession() as http:
                counter = 0
                async def rpc(ws, method, params):
                    nonlocal counter
                    counter += 1; identifier = counter
                    await ws.send_json({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
                    replay = []
                    async with asyncio.timeout(60):
                        while True:
                            value = await ws.receive_json()
                            if value.get("id") == identifier and "method" not in value: return value, replay
                            replay.append(value)
                async def connect(token=None):
                    ws = await http.ws_connect(url + ("?leader=" + token if token else ""), headers={"Authorization": "Bearer " + secret})
                    for method, params in [("initialize", {"protocolVersion": 1, "clientCapabilities": {}}), ("authenticate", {"methodId": "cached_token"})]:
                        result, _ = await rpc(ws, method, params); assert "error" not in result
                    return ws
                for sid, actor, socket in [(p_sid, p_actor, primary), (s_sid, s_actor, secondary)]:
                    async with await connect(runtime.directory.token(socket)) as ws:
                        result, _ = await rpc(ws, "session/load", {"sessionId": sid, "cwd": str(workspace), "mcpServers": []})
                        assert bridge.unwrap_result(result)["_meta"]["x.ai/residentActorId"] == actor
                evidence["tests"].append("strict_attach_routes_to_each_original_actor")
                # Deliberate disposable collision: native explicitly restores the
                # same saved ID on another leader, producing a distinct actor.
                duplicate = await s.rpc("session/load", {"sessionId": p_sid, "cwd": str(workspace), "mcpServers": [], "_meta": {"x.ai/restore_code": False}})
                duplicate_actor = (await strict(s, p_sid))["_meta"]["x.ai/residentActorId"]
                assert duplicate_actor != p_actor
                snapshot = await runtime.directory.snapshot(fresh=True)
                collisions = [r for r in snapshot["sessions"] if r["sessionId"] == p_sid]
                assert len(collisions) == 2 and len({(r["bridgeLeaderId"], r["residentActorId"]) for r in collisions}) == 2
                await s.rpc("_x.ai/session/close", {"sessionId": p_sid})
                assert (await strict(p, p_sid, p_actor))["_meta"]["x.ai/residentActorId"] == p_actor
                evidence["tests"].append("same_saved_id_distinct_live_actors_never_merged")
                saved = (await p.rpc("session/new", {"cwd": str(workspace), "mcpServers": []}))["sessionId"]
                old_saved_actor = (await strict(p, saved))["_meta"]["x.ai/residentActorId"]
                await p.rpc("_x.ai/session/close", {"sessionId": saved})
                snapshot = await runtime.directory.snapshot(fresh=True)
                assert next(r for r in snapshot["sessions"] if r["sessionId"] == saved)["resident"] is False
                async with await connect() as ws:
                    result, replay = await rpc(ws, "session/load", {"sessionId": saved, "cwd": str(workspace), "mcpServers": [], "_meta": {"x.ai/resumeSaved": True}})
                    assert "error" not in result
                    resumed_actor = bridge.unwrap_result(result)["_meta"]["x.ai/residentActorId"]
                    assert resumed_actor != old_saved_actor
                await asyncio.sleep(.5)
                assert len(runtime.owners) == 1 and next(iter(runtime.owners)).session_id == saved
                assert (await strict(p, saved, resumed_actor))["_meta"]["x.ai/residentActorId"] == resumed_actor
                async with await connect() as ws:
                    result, _ = await rpc(ws, "session/load", {"sessionId": saved, "cwd": str(workspace), "_meta": {"x.ai/resumeSaved": True}})
                    assert result["error"]["data"]["code"] == "origin_conflict"
                evidence["tests"].append("explicit_saved_resume_retained_after_phone_disconnect_and_not_duplicated")
                await runtime.directory.snapshot(fresh=True)
                processes[1].terminate(); await asyncio.to_thread(processes[1].wait, 15)
                snapshot = await runtime.directory.snapshot(fresh=True)
                assert snapshot["bridgePartial"] and snapshot["bridgeResumeBlocked"]
                assert not any(r["sessionId"] == s_sid for r in snapshot["sessions"]), "Lost actor must never be relabeled Saved"
                async with await connect() as ws:
                    result, _ = await rpc(ws, "session/load", {"sessionId": s_sid, "cwd": str(workspace), "_meta": {"x.ai/resumeSaved": True}})
                    assert result["error"]["data"]["code"] == "source_unavailable"
                evidence["tests"].append("real_source_loss_refuses_history_fallback")
                try:
                    await http.ws_connect(url, headers={"Authorization": "Bearer wrong-secret"})
                    raise AssertionError("Wrong secret accepted")
                except WSServerHandshakeError as error:
                    assert error.status == 401
                evidence["tests"].append("wrong_secret_rejected")
                evidence.update(primarySessionId=p_sid, primaryActorId=p_actor, secondarySessionId=s_sid,
                                secondaryActorId=s_actor, collisionActorId=duplicate_actor,
                                savedSessionId=saved, resumedActorId=resumed_actor)
            evidence["passed"] = True
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(evidence, indent=2) + "\n")
            print(json.dumps({"passed": True, "tests": evidence["tests"]}))
        finally:
            if runner is not None: await runner.cleanup()
            for client in clients:
                with suppress(ConnectionError): await client.close()
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try: await asyncio.to_thread(process.wait, 15)
                    except subprocess.TimeoutExpired:
                        process.kill(); await asyncio.to_thread(process.wait)
            for handle in handles: handle.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.binary.resolve(), args.output.resolve()))

#!/usr/bin/env python3
"""Real native actor replacement checks, using only sessions this probe creates.

Creates and closes disposable native actors and writes two uniquely named files
through one normal allow-once permission. Requires the patched native leader.
The bearer secret is read internally and never included in the report or argv.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time

import aiohttp

from bridge import connect_leader, read_frame, write_frame
from verify_edge_live import Client


class Native:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.sequence = 0

    async def send(self, method, params, identifier=None):
        value = {"jsonrpc": "2.0", "method": method, "params": params}
        if identifier is not None:
            value["id"] = identifier
        await write_frame(self.writer, {"type": "acp", "payload": json.dumps(value)})

    async def rpc(self, method, params):
        self.sequence += 1
        identifier = f"identity-probe-{self.sequence}"
        await self.send(method, params, identifier)
        async with asyncio.timeout(60):
            while True:
                frame = await read_frame(self.reader)
                if frame.get("type") != "acp":
                    continue
                value = json.loads(frame["payload"])
                if value.get("id") == identifier and "method" not in value:
                    return value


def refused(value):
    error = value.get("error", {})
    return error.get("code") == -32602 and error.get("data") == (
        "live session actor changed; select a running session"
    )


async def verify(args):
    secret = Path(args.secret_file).expanduser().read_text().strip()
    cwd = str(Path(args.cwd).resolve())
    reader, writer = await connect_leader(Path(args.leader_socket).expanduser(), args.cli_version)
    native = Native(reader, writer)
    await native.rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    await native.rpc("authenticate", {"methodId": "cached_token"})
    created = await native.rpc("session/new", {"cwd": cwd, "mcpServers": []})
    assert "error" not in created
    sid = created["result"]["sessionId"]
    result = {"sessionId": sid, "nativeVersion": args.cli_version, "checks": {}}
    checks = result["checks"]
    clients = []
    try:
        async with aiohttp.ClientSession() as http:

            async def phone():
                client = Client(http, args.url, secret)
                clients.append(client)
                await client.connect()
                return client

            async def load(client, actor=None):
                params = {"sessionId": sid, "cwd": cwd, "mcpServers": []}
                if actor:
                    params["_meta"] = {"x.ai/residentActorId": actor}
                return await client.rpc("session/load", params)

            async def restore():
                response = await native.rpc(
                    "session/load", {"sessionId": sid, "cwd": cwd, "mcpServers": []}
                )
                assert "error" not in response

            one = await phone()
            initial = await load(one)
            old_actor = initial["result"]["_meta"]["x.ai/residentActorId"]
            assert old_actor
            await native.rpc("session/close", {"sessionId": sid})
            await asyncio.wait_for(one.receiver, 15)
            assert one.ws.close_code == 4409
            checks["connectedCloseRevokesSocket4409"] = True
            await restore()
            two = await phone()
            rejected = await load(two, old_actor)
            assert refused(rejected), rejected
            checks["connectedReplacementRejectsCachedActor"] = True
            explicit = await load(two)
            second_actor = explicit["result"]["_meta"]["x.ai/residentActorId"]
            assert second_actor != old_actor
            checks["explicitSelectionGetsNewActorIdentity"] = True

            # The phone misses the removal notification entirely while offline.
            await two.close()
            await native.rpc("session/close", {"sessionId": sid})
            await restore()
            three = await phone()
            rejected = await load(three, second_actor)
            assert refused(rejected), rejected
            checks["offlineCloseAndColdRestoreRejectCachedActor"] = True
            explicit = await load(three)
            third_actor = explicit["result"]["_meta"]["x.ai/residentActorId"]
            assert third_actor not in (old_actor, second_actor)
            checks["offlineExplicitSelectionGetsNewIdentity"] = True

            # Assert the native contract independently of the bridge's injection.
            stale_meta = {"x.ai/residentActorId": second_actor}
            stale_prompt = await native.rpc(
                "session/prompt",
                {
                    "sessionId": sid,
                    "_meta": stale_meta,
                    "prompt": [
                        {
                            "type": "text",
                            "text": "STALE-ACTOR-PROMPT-MUST-NOT-RUN: Reply STALE-FAIL. No tools.",
                        }
                    ],
                },
            )
            assert refused(stale_prompt), stale_prompt
            checks["nativeStalePromptRejected"] = True
            model = explicit["result"]["models"]["currentModelId"]
            stale_model = await native.rpc(
                "session/set_model", {"sessionId": sid, "modelId": model, "_meta": stale_meta}
            )
            assert refused(stale_model), stale_model
            checks["nativeStaleModelRejected"] = True

            stamp = str(time.time_ns())
            started = Path(cwd, f"edge-identity-started-{stamp}.txt")
            finished = Path(cwd, f"edge-identity-finished-{stamp}.txt")
            prompt = (
                "ACTOR-IDENTITY-CANCEL-CHECK: Run exactly once this shell command, asking normal permission: "
                f"python3 -c \"from pathlib import Path; import time; Path('{started.name}').write_text('STARTED'); time.sleep(12); Path('{finished.name}').write_text('FINISHED-ONCE')\". "
                "After success reply exactly ACTOR-IDENTITY-CANCEL-CHECK-DONE."
            )
            beginning = len(three.events)
            running = asyncio.create_task(
                three.rpc(
                    "session/prompt",
                    {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
                )
            )
            request = await three.permission(beginning)
            await three.answer(request)
            async with asyncio.timeout(120):
                while not started.exists():
                    await asyncio.sleep(0.05)
            assert not finished.exists(), "Task finished before stale cancellation injection"
            await native.send("session/cancel", {"sessionId": sid, "_meta": stale_meta})
            response = await running
            assert "error" not in response, response
            assert response["result"]["stopReason"] == "end_turn", response
            assert finished.read_text() == "FINISHED-ONCE"
            checks["nativeStaleCancelDoesNotCancelReplacementTask"] = True
            replay = await phone()
            await load(replay, third_actor)
            text = json.dumps(replay.events)
            assert "STALE-ACTOR-PROMPT-MUST-NOT-RUN" not in text
            checks["rejectedStalePromptAbsentFromHistory"] = True
            result["files"] = [started.name, finished.name]
            result["actorIds"] = [old_actor, second_actor, third_actor]
            for client in clients:
                await client.close()
    finally:
        await native.rpc("session/close", {"sessionId": sid})
        writer.close()
        await writer.wait_closed()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leader-socket", required=True)
    parser.add_argument("--cli-version", default="1.0.45")
    parser.add_argument("--url", required=True)
    parser.add_argument("--secret-file", default=str(Path.home() / ".grok/mobile-bridge.secret"))
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--allow-create-close-disposable-sessions", action="store_true", required=True
    )
    args = parser.parse_args()
    result = asyncio.run(verify(args))
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

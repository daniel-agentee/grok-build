#!/usr/bin/env python3
"""Fault injection for explicit native close racing mobile live attachment.

DESTRUCTIVE: use only a separately created disposable leader/session. This closes
the target actor through native IPC while delaying a genuine roster response.
It records whether stock session/load silently restores the closed actor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import tempfile

import aiohttp
from aiohttp import web

from bridge import (
    Bridge,
    INTERNAL_PREFIX,
    connect_leader,
    make_app,
    read_frame,
    unwrap_result,
    write_frame,
)
from verify_edge_live import Client


async def verify(args):
    secret = Path(args.secret_file).expanduser().read_text().strip()
    native = Path(args.leader_socket)
    cwd = str(Path(args.cwd).resolve())
    closed = asyncio.Event()
    result = {"sessionId": args.session, "nativeVersion": args.version, "checks": {}}

    async def native_rpc(method, params):
        reader, writer = await connect_leader(native, args.version)
        await write_frame(
            writer,
            {
                "type": "acp",
                "payload": json.dumps(
                    {"jsonrpc": "2.0", "id": "control", "method": method, "params": params}
                ),
            },
        )
        try:
            while True:
                frame = await read_frame(reader)
                if frame.get("type") == "acp":
                    value = json.loads(frame["payload"])
                    if value.get("id") == "control" and "method" not in value:
                        return value
        finally:
            writer.close()
            await writer.wait_closed()

    async def proxy(client_reader, client_writer):
        native_reader, native_writer = await asyncio.open_unix_connection(native)

        async def inbound():
            while True:
                await write_frame(native_writer, await read_frame(client_reader))

        async def outbound():
            while True:
                frame = await read_frame(native_reader)
                if frame.get("type") == "acp":
                    value = json.loads(frame["payload"])
                    if (
                        str(value.get("id", "")).startswith(INTERNAL_PREFIX)
                        and "result" in value
                        and not closed.is_set()
                    ):
                        row = next(
                            row
                            for row in unwrap_result(value)["sessions"]
                            if row["sessionId"] == args.session
                        )
                        assert row.get("resident") is True
                        result["checks"]["preflightSawResident"] = True
                        close_response = await native_rpc(
                            "session/close", {"sessionId": args.session}
                        )
                        result["nativeCloseResponse"] = close_response
                        roster = await native_rpc("_x.ai/sessions/list", {})
                        after = next(
                            row
                            for row in unwrap_result(roster)["sessions"]
                            if row["sessionId"] == args.session
                        )
                        result["checks"]["nativeCloseMadeNonresident"] = (
                            after.get("resident") is False
                        )
                        closed.set()
                await write_frame(client_writer, frame)

        tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            native_writer.close()
            client_writer.close()
            await native_writer.wait_closed()
            await client_writer.wait_closed()

    with tempfile.TemporaryDirectory(prefix="grok-native-race-") as directory:
        socket = Path(directory) / "proxy.sock"
        server = await asyncio.start_unix_server(proxy, path=socket)
        runner = web.AppRunner(make_app(Bridge(socket, secret, args.version)), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", args.port)
        await site.start()
        try:
            async with aiohttp.ClientSession() as http:
                client = Client(http, f"http://127.0.0.1:{args.port}/ws", secret)
                await client.connect()
                response = await client.rpc(
                    "session/load", {"sessionId": args.session, "cwd": cwd, "mcpServers": []}
                )
                result["checks"]["loadSucceededAfterActorClosed"] = "error" not in response
                if "error" in response:
                    result["loadError"] = {
                        key: response["error"][key]
                        for key in ("code", "message", "data")
                        if key in response["error"]
                    }
                roster = await native_rpc("_x.ai/sessions/list", {})
                after = next(
                    row
                    for row in unwrap_result(roster)["sessions"]
                    if row["sessionId"] == args.session
                )
                result["checks"]["actorBecameResidentAgain"] = after.get("resident") is True
                if args.expect_resident_only:
                    assert "error" in response, "Closed actor was silently recreated"
                    assert after.get("resident") is False, "Actor must remain closed"
                await client.close()
        finally:
            await runner.cleanup()
            server.close()
            await server.wait_closed()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leader-socket", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--secret-file", default=str(Path.home() / ".grok/mobile-bridge.secret"))
    parser.add_argument("--port", type=int, default=18798)
    parser.add_argument("--version", default="1.0.45")
    parser.add_argument("--expect-resident-only", action="store_true")
    parser.add_argument("--allow-close-disposable-session", action="store_true", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = asyncio.run(verify(args))
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

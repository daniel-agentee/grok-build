#!/usr/bin/env python3
"""Destructive transport-only checks on an explicitly owned disposable bridge.

Keeps its existing native agent alive. Restarts only the bridge child created by
this invocation. Uses actual model/tool execution with a normal allow-once request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import aiohttp

from verify_edge_live import Client, updates


def event_text(event):
    content = event.get("params", {}).get("update", {}).get("content", {})
    return content.get("text", "") if isinstance(content, dict) else ""


async def verify(args):
    secret = Path(args.secret_file).expanduser().read_text().strip()
    cwd = str(Path(args.cwd).resolve())
    url = f"http://127.0.0.1:{args.port}/ws"
    result = {"sessionId": args.session, "checks": {}}
    process = None
    logfile = tempfile.TemporaryFile()

    async def launch():
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("bridge.py")),
                "--leader-socket",
                args.leader_socket,
                "--secret-file",
                args.secret_file,
                "--cli-version",
                args.cli_version,
                "--host",
                "127.0.0.1",
                "--port",
                str(args.port),
            ],
            stdout=logfile,
            stderr=logfile,
        )
        for _ in range(100):
            if child.poll() is not None:
                raise RuntimeError("Disposable bridge failed to start")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", args.port)
                writer.close()
                await writer.wait_closed()
                return child
            except OSError:
                await asyncio.sleep(0.1)
        child.terminate()
        raise TimeoutError("Bridge startup")

    async def wait_file(path):
        for _ in range(2400):
            if path.exists():
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("Real tool output not written")

    try:
        process = await launch()
        async with aiohttp.ClientSession() as http:
            one = Client(http, url, secret)
            observer = Client(http, url, secret)
            await one.connect()
            await one.attach(args.session, cwd)
            await observer.connect()
            await observer.attach(args.session, cwd)
            instance = one.instance
            stamp = str(int(time.time()))
            marker = f"EDGE-DUPLICATE-{stamp}"
            payload = {
                "jsonrpc": "2.0",
                "id": "same-pending-id",
                "method": "session/prompt",
                "params": {
                    "sessionId": args.session,
                    "prompt": [
                        {"type": "text", "text": f"{marker}: Reply exactly ACK-{marker}. No tools."}
                    ],
                },
            }
            await one.ws.send_json(payload)
            await one.ws.send_json(payload)
            await asyncio.wait_for(one.receiver, 15)
            assert one.ws.close_code == 1008
            await observer.wait_event(lambda e: f"ACK-{marker}" in event_text(e))
            user_texts = [
                u.get("content", {}).get("text", "")
                for u in updates(observer, "user_message_chunk")
            ]
            assert sum(marker in text for text in user_texts) == 1
            result["checks"]["duplicatePendingPromptCloses1008"] = True
            result["checks"]["acceptedPromptContinuesExactlyOnce"] = True
            # Tool starts before transport termination, writes its result afterwards.
            started = Path(cwd, f"edge-restart-started-{stamp}.txt")
            finished = Path(cwd, f"edge-restart-finished-{stamp}.txt")
            command = (
                f"python3 -c \"from pathlib import Path; import time; Path('{started.name}').write_text('STARTED'); "
                f"time.sleep(12); Path('{finished.name}').write_text('FINISHED-ONCE')\""
            )
            prompt = f"EDGE-RESTART-{stamp}: Run this exact command once, requesting normal permission: {command} . After success reply EDGE-RESTART-DONE-{stamp}."
            before = len(observer.events)
            running = asyncio.create_task(
                observer.rpc(
                    "session/prompt",
                    {"sessionId": args.session, "prompt": [{"type": "text", "text": prompt}]},
                )
            )
            permission = await observer.permission(before)
            await observer.answer(permission)
            await wait_file(started)
            assert not finished.exists(), "Task already finished before transport restart"
            process.kill()  # Abrupt transport failure; only this invocation's child.
            await asyncio.to_thread(process.wait, 10)
            await asyncio.wait_for(observer.receiver, 15)
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            process = await launch()
            recovered = Client(http, url, secret)
            await recovered.connect()
            await recovered.attach(args.session, cwd)
            assert recovered.instance == instance
            await wait_file(finished)
            await recovered.wait_event(lambda e: f"EDGE-RESTART-DONE-{stamp}" in event_text(e))
            assert finished.read_text() == "FINISHED-ONCE"
            result["checks"]["bridgeRestartWhileToolRuns"] = True
            result["checks"]["sameAgentInstanceAfterRestart"] = True
            result["checks"]["toolCompletedOnceDuringTransportLoss"] = True
            result["agentInstanceId"] = instance
            result["duplicateMarker"] = marker
            result["restartFiles"] = [started.name, finished.name]
            await recovered.close()
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, 10)
        logfile.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leader-socket", required=True)
    parser.add_argument("--cli-version", default="1.0.45")
    parser.add_argument("--secret-file", default=str(Path.home() / ".grok/mobile-bridge.secret"))
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--port", type=int, default=18797)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = asyncio.run(verify(args))
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

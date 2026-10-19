#!/usr/bin/env python3
"""Verify a departing real Terminal cannot resolve another client's approval.

Uses an existing disposable resident actor, normal permissions and the real TUI.
The uniquely named file is written only after the surviving mobile client allows
once. No actor is created, restored or closed; only the probe's own TUI exits.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import pty
import signal
import subprocess
import time

import aiohttp
from verify_edge_live import Client


async def verify(args):
    cwd = Path(args.cwd).resolve()
    stamp = str(time.time_ns())
    filename = f"edge-terminal-detach-{args.signal}-{stamp}.txt"
    line = f"ONCE-{stamp}"
    ack = f"TERMINAL-DETACH-ACK-{stamp}"
    command = (
        f"python3 -c \"from pathlib import Path; p=Path('{filename}'); "
        f"p.write_text((p.read_text() if p.exists() else '') + '{line}\\n')\""
    )
    process = None
    master = None
    async with aiohttp.ClientSession() as http:
        client = Client(http, args.url, Path(args.secret_file).expanduser().read_text().strip())
        client.timeout = 240
        await client.connect()
        initial = await client.rpc(
            "session/load", {"sessionId": args.session, "cwd": str(cwd), "mcpServers": []}
        )
        assert "error" not in initial, initial
        actor_id = initial["result"]["_meta"]["x.ai/residentActorId"]
        start = len(client.events)
        run = asyncio.create_task(
            client.rpc(
                "session/prompt",
                {
                    "sessionId": args.session,
                    "prompt": [
                        {
                            "type": "text",
                            "text": f"Run exactly once with normal permission: {command} . After success reply exactly {ack}. No other tools.",
                        }
                    ],
                },
            )
        )
        try:
            request = await client.permission(start)
            assert not (cwd / filename).exists()
            timeline = {"permissionPending": time.time()}
            master, slave = pty.openpty()
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            process = subprocess.Popen(
                [
                    str(Path(args.binary).resolve()),
                    "--leader",
                    "--no-auto-update",
                    "--leader-socket",
                    str(Path(args.leader_socket).expanduser()),
                    "--permission-mode",
                    "default",
                    "--cwd",
                    str(cwd),
                    "--resume",
                    args.session,
                ],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
            )
            os.close(slave)
            os.set_blocking(master, False)

            async def wait(seconds):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    try:
                        os.read(master, 65536)
                    except BlockingIOError:
                        pass
                    except OSError:
                        # Keep the full observation window after the PTY closes;
                        # leader teardown can race slightly behind EOF.
                        pass
                    await asyncio.sleep(0.05)

            await wait(8)
            before = not run.done()
            timeline["terminalDeparture"] = time.time()
            process.send_signal(signal.SIGTERM if args.signal == "term" else signal.SIGKILL)
            await wait(3)
            survived = not run.done()
            absent = not (cwd / filename).exists()
            if survived:
                await client.answer(request)
            response = await run
            replay = await client.rpc(
                "session/load", {"sessionId": args.session, "cwd": str(cwd), "mcpServers": []}
            )
            effects = (cwd / filename).read_text().splitlines() if (cwd / filename).exists() else []
            out = {
                "nativeSHA256": hashlib.sha256(Path(args.binary).read_bytes()).hexdigest(),
                "nativeVersion": client.version,
                "sessionId": args.session,
                "actorId": actor_id,
                "signal": args.signal,
                "toolCallId": request["params"]["toolCall"]["toolCallId"],
                "timeline": timeline,
                "filename": filename,
                "checks": {
                    "pendingBeforeOwnTerminalDeparture": before,
                    "pendingAfterOwnTerminalDeparture": survived,
                    "fileAbsentBeforeSurvivingClientAllows": absent,
                    "completedAfterSurvivingClientAllowOnce": response.get("result", {}).get(
                        "stopReason"
                    )
                    == "end_turn",
                    "effectExactlyOnce": effects == [line],
                    "residentActorUnchanged": replay.get("result", {})
                    .get("_meta", {})
                    .get("x.ai/residentActorId")
                    == actor_id,
                },
            }
            Path(args.output).write_text(json.dumps(out, indent=2) + "\n")
            print(json.dumps(out), flush=True)
            assert all(out["checks"].values()), (
                "A Terminal departure resolved the shared interaction"
            )
        finally:
            if process is not None and process.poll() is None:
                process.kill()
            if process is not None:
                process.wait(timeout=5)
            if master is not None:
                os.close(master)
            await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--leader-socket", required=True)
    parser.add_argument("--signal", choices=("term", "kill"), default="term")
    parser.add_argument("--secret-file", default=str(Path.home() / ".grok/mobile-bridge.secret"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-create-disposable-files", action="store_true", required=True)
    asyncio.run(verify(parser.parse_args()))

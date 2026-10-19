#!/usr/bin/env python3
"""Verify two real native permission requests and exactly-once decisions.

Targets only an already resident actor in a disposable workspace. The model is
asked to emit two separate parallel shell calls; no permission policy is changed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time

import aiohttp

from verify_edge_live import Client


async def verify(args):
    secret = Path(args.secret_file).expanduser().read_text().strip()
    cwd = Path(args.cwd).resolve()
    stamp = str(int(time.time()))
    targets = [cwd / f"edge-parallel-{stamp}-{index}.txt" for index in range(2)]
    commands = [
        f"python3 -c \"from pathlib import Path; p=Path('{path.name}'); p.write_text((p.read_text() if p.exists() else '') + 'PARALLEL-{index}\\n')\""
        for index, path in enumerate(targets)
    ]
    prompt = (
        f"EDGE-PARALLEL-PERMISSIONS-{stamp}: Make exactly TWO SEPARATE run_terminal_command "
        "tool calls in parallel in the SAME assistant message. Do not wait for one before "
        "issuing the other. They affect independent files. Do not combine them into one "
        "command or use a different tool. Request normal permission for each, then execute "
        "each exactly once. The commands are:\n"
        + "\n".join(f"{index + 1}. {command}" for index, command in enumerate(commands))
        + f"\nAfter both complete, reply exactly EDGE-PARALLEL-DONE-{stamp}."
    )
    if args.children:
        prompt = (
            f"EDGE-CHILD-PERMISSIONS-{stamp}: Spawn exactly TWO separate background subagents "
            "using spawn_subagent, one for each command below. Start BOTH before waiting "
            "for either. Each child must execute only its assigned exact shell command once "
            "in this same workspace, requesting normal permission; no other tools or edits. "
            "Wait until BOTH children finish, then reply "
            f"EDGE-PARALLEL-DONE-{stamp}. Commands:\n"
            + "\n".join(f"Child {index + 1}: {command}" for index, command in enumerate(commands))
        )
    async with aiohttp.ClientSession() as http:
        one, two = Client(http, args.url, secret), Client(http, args.url, secret)
        one.timeout = 600 if args.external_approvals else 180
        await one.connect()
        await one.attach(args.session, str(cwd))
        await two.connect()
        await two.attach(args.session, str(cwd))
        start = len(one.events)
        running = asyncio.create_task(
            one.rpc(
                "session/prompt",
                {
                    "sessionId": args.session,
                    "prompt": [{"type": "text", "text": prompt}],
                },
            )
        )
        first = await one.permission(start)
        assert all(not target.exists() for target in targets), "A tool ran before permission"
        # Native preparation may await permission before emitting the next request.
        # Characterize that behavior; never confuse two requests with concurrency.
        await asyncio.sleep(15 if args.children else 0.5)
        requests = [
            event
            for event in one.events[start:]
            if event.get("method") == "session/request_permission"
        ]
        simultaneous = len(requests) >= 2
        if args.require_simultaneous:
            assert simultaneous, "Native prepared permission requests sequentially"
        print("First real permission pending; simultaneous=" + str(simultaneous), flush=True)
        if not args.external_approvals:
            await one.answer(first)
        second = await one.wait_event(
            lambda event: (
                event.get("method") == "session/request_permission"
                and event["params"]["toolCall"]["toolCallId"]
                != first["params"]["toolCall"]["toolCallId"]
            ),
            start=start,
            timeout=600 if args.external_approvals else 150,
        )
        assert not targets[1].exists(), "Second tool ran before its permission"
        ids = [request["params"]["toolCall"]["toolCallId"] for request in (first, second)]
        assert len(set(ids)) == 2
        if not args.external_approvals:
            other = await two.wait_event(
                lambda event: (
                    event.get("method") == "session/request_permission"
                    and event["params"]["toolCall"]["toolCallId"] == ids[1]
                )
            )
            await two.answer(other)
        response = await running
        assert "error" not in response
        requests = [
            event
            for event in one.events[start:]
            if event.get("method") == "session/request_permission"
        ]
        ids = list(
            dict.fromkeys(request["params"]["toolCall"]["toolCallId"] for request in requests)
        )
        for index, target in enumerate(targets):
            assert target.read_text().splitlines() == [f"PARALLEL-{index}"]
        for client in (one, two):
            resolved = {
                event.get("params", {}).get("update", {}).get("tool_call_id")
                for event in client.events
                if event.get("params", {}).get("update", {}).get("sessionUpdate")
                == "interaction_resolved"
            }
            assert set(ids).issubset(resolved)
        result = {
            "sessionId": args.session,
            "nativeVersion": one.version,
            "checks": {
                "twoRealRequestsIssued": True,
                "twoSimultaneouslyOutstanding": simultaneous,
                "bothExecutionsExactlyOnce": True,
                "bothResolutionsBroadcastEverywhere": True,
            },
            "toolCallIds": ids,
            "nativeChildrenRequested": args.children,
            "requestingSessionIds": sorted(
                {
                    request["params"]["toolCall"]
                    .get("_meta", {})
                    .get("x.ai/requestingSessionId", args.session)
                    for request in requests
                }
            ),
            "files": [target.name for target in targets],
            "approvalSurface": "external clients"
            if args.external_approvals
            else "two authenticated WebSocket clients",
        }
        await one.close()
        await two.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--secret-file", default=str(Path.home() / ".grok/mobile-bridge.secret"))
    parser.add_argument("--external-approvals", action="store_true")
    parser.add_argument("--require-simultaneous", action="store_true")
    parser.add_argument(
        "--children", action="store_true", help="Ask two actual native background children"
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = asyncio.run(verify(args))
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

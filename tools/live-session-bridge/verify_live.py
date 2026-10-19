#!/usr/bin/env python3
"""Exercise a real bridge and resident agent; requires existing cached CLI auth.

This is a live integration probe, not a protocol stub. Its optional prompt runs
on the selected real agent. Raw thoughts, credentials, and tool output are not
written into the result report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp


async def verify(args: argparse.Namespace) -> dict[str, Any]:
    secret = Path(args.secret_file).expanduser().read_text().strip()
    async with aiohttp.ClientSession() as http:
        wrong_secret_rejected = False
        try:
            ws = await http.ws_connect(
                args.url, headers={"Authorization": "Bearer deliberately-wrong-live-test"}
            )
        except aiohttp.WSServerHandshakeError as error:
            wrong_secret_rejected = error.status == 401
        else:
            await ws.close()
        if not wrong_secret_rejected:
            raise RuntimeError("Wrong-secret WS handshake was not rejected with 401")

        async with http.ws_connect(args.url, headers={"Authorization": f"Bearer {secret}"}) as ws:
            pending: dict[int, asyncio.Future[Any]] = {}
            events: list[dict[str, Any]] = []
            counter = 0

            async def receive() -> None:
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    event = json.loads(message.data)
                    request_id = event.get("id")
                    if "method" not in event and request_id in pending:
                        pending.pop(request_id).set_result(event)
                    else:
                        events.append(event)

            receiver = asyncio.create_task(receive())

            async def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
                nonlocal counter
                counter += 1
                request_id = counter
                future = asyncio.get_running_loop().create_future()
                pending[request_id] = future
                await ws.send_json(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                )
                response = await asyncio.wait_for(future, args.timeout)
                if "error" in response:
                    raise RuntimeError(
                        f"{method} failed: {response['error'].get('message', 'protocol error')}"
                    )
                return response["result"]

            try:
                initialization = await rpc(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "_meta": {"clientUserMessageEcho": True},
                    },
                )
                await rpc("authenticate", {"methodId": "cached_token"})
                roster = await rpc("_x.ai/sessions/list", {})
                roster = roster.get("result", roster)
                row = next(
                    (row for row in roster["sessions"] if row["sessionId"] == args.session), None
                )
                if row is None or not row.get("resident"):
                    raise RuntimeError("Requested session is not resident in this live leader")
                cwd = str(Path(args.cwd).expanduser().resolve())
                if row["cwd"] != cwd:
                    raise RuntimeError("Session workspace differs from --cwd")
                load: dict[str, Any] = {"sessionId": args.session, "cwd": cwd, "mcpServers": []}
                if args.cursor:
                    load["_meta"] = {"cursor": args.cursor}
                await rpc("session/load", load)
                print(
                    "Attached to the resident session; optional prompt will run in that same live agent.",
                    flush=True,
                )
                if args.wait_for_enter:
                    await asyncio.to_thread(input, "Press Enter while submitting from Terminal: ")
                prompt_result = None
                if args.prompt:
                    prompt_result = await rpc(
                        "session/prompt",
                        {
                            "sessionId": args.session,
                            "prompt": [{"type": "text", "text": args.prompt}],
                        },
                    )
                await asyncio.sleep(args.observe_seconds)
                ids: list[str] = []
                users: list[str] = []
                assistant: list[str] = []
                tools = 0
                queues: list[dict[str, Any]] = []
                for event in events:
                    params = event.get("params", {})
                    if not isinstance(params, dict) or params.get("sessionId") != args.session:
                        continue
                    event_id = params.get("_meta", {}).get("eventId")
                    if event_id:
                        ids.append(event_id)
                    if event.get("method") == "_x.ai/queue/changed":
                        queues.append(params)
                    update = params.get("update", {})
                    if not isinstance(update, dict):
                        continue
                    kind = update.get("sessionUpdate")
                    content = update.get("content", {})
                    text = content.get("text", "") if isinstance(content, dict) else ""
                    if kind == "user_message_chunk":
                        users.append(text)
                    elif kind == "agent_message_chunk":
                        assistant.append(text)
                    elif kind in ("tool_call", "tool_call_update"):
                        tools += 1
                repeated = sorted({event_id for event_id in ids if ids.count(event_id) > 1})
                result = {
                    "sessionId": args.session,
                    "cwd": cwd,
                    "agentInstanceId": initialization.get("_meta", {}).get("agentInstanceId"),
                    "agentVersion": initialization.get("_meta", {}).get("agentVersion"),
                    "resident": row["resident"],
                    "yolo": row.get("yolo"),
                    "wrongSecretRejected": wrong_secret_rejected,
                    "cursor": args.cursor,
                    "cursorFallbackToFullReplay": bool(
                        args.cursor
                        and any(
                            int(event_id.rsplit("-", 1)[1]) <= int(args.cursor.rsplit("-", 1)[1])
                            for event_id in ids
                        )
                    ),
                    "eventIds": ids,
                    "duplicateEventIds": repeated,
                    "userMessages": users,
                    "assistantText": "".join(assistant),
                    "toolUpdateCount": tools,
                    "queueNotifications": queues,
                    "promptStopReason": prompt_result.get("stopReason") if prompt_result else None,
                }
                if repeated:
                    raise RuntimeError("Duplicate event IDs observed")
                return result
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18789/ws")
    parser.add_argument("--secret-file", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--cursor")
    parser.add_argument("--prompt", help="Optional instruction sent to the REAL resident agent")
    parser.add_argument("--wait-for-enter", action="store_true")
    parser.add_argument("--observe-seconds", type=float, default=2)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument(
        "--output", required=True, help="Sanitized JSON result; prompt/answer text is included"
    )
    args = parser.parse_args()
    result = asyncio.run(verify(args))
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"Verified resident attachment; {len(result['eventIds'])} event IDs; zero duplicates. Report: {args.output}"
    )


if __name__ == "__main__":
    main()

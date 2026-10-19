#!/usr/bin/env python3
"""Adversarial integration checks against an existing disposable live Grok actor.

Requires real cached CLI authentication and normal interactive permission policy.
Never starts or restores an agent. Creates only named disposable test files after
explicit allow-once responses. The secret is read internally and never recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time
from typing import Any

import aiohttp


class Client:
    def __init__(self, http: aiohttp.ClientSession, url: str, secret: str):
        self.http, self.url, self.secret = http, url, secret
        self.events: list[dict[str, Any]] = []
        self.pending: dict[str, asyncio.Future[Any]] = {}
        self.sequence = 100
        self.timeout = 180

    async def connect(self) -> None:
        self.ws = await self.http.ws_connect(
            self.url, headers={"Authorization": f"Bearer {self.secret}"}
        )
        self.receiver = asyncio.create_task(self.receive())
        result = await self.rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        self.instance = result["result"]["_meta"]["agentInstanceId"]
        self.version = result["result"]["_meta"]["agentVersion"]
        await self.rpc("authenticate", {"methodId": "cached_token"})

    async def receive(self) -> None:
        async for item in self.ws:
            if item.type != aiohttp.WSMsgType.TEXT:
                continue
            value = json.loads(item.data)
            key = json.dumps(value.get("id"))
            if "method" not in value and key in self.pending:
                self.pending.pop(key).set_result(value)
            else:
                self.events.append(value)

    async def rpc(self, method: str, params: dict[str, Any], request_id=None):
        self.sequence += 1
        request_id = request_id if request_id is not None else f"edge-{self.sequence}"
        future = asyncio.get_running_loop().create_future()
        self.pending[json.dumps(request_id)] = future
        await self.ws.send_json(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        return await asyncio.wait_for(future, self.timeout)

    async def attach(self, session: str, cwd: str):
        response = await self.rpc(
            "session/load", {"sessionId": session, "cwd": cwd, "mcpServers": []}
        )
        if "error" in response:
            raise RuntimeError(response["error"]["message"])

    async def wait_event(self, predicate, start=0, timeout=150):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events[start:]:
                if predicate(event):
                    return event
            await asyncio.sleep(0.05)
        raise TimeoutError("Expected live protocol event did not arrive")

    async def permission(self, start=0):
        return await self.wait_event(
            lambda e: e.get("method") == "session/request_permission", start
        )

    async def answer(self, request, option="allow-once"):
        await self.ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": option}},
            }
        )

    async def close(self):
        await self.ws.close()
        await asyncio.gather(self.receiver, return_exceptions=True)


def updates(client: Client, kind: str):
    return [
        e["params"]["update"]
        for e in client.events
        if e.get("method") == "session/update"
        and e.get("params", {}).get("update", {}).get("sessionUpdate") == kind
    ]


async def verify(args) -> dict[str, Any]:
    secret = Path(args.secret_file).expanduser().read_text().strip()
    cwd = str(Path(args.cwd).resolve())
    result: dict[str, Any] = {"sessionId": args.session, "cwd": cwd, "checks": {}}
    async with aiohttp.ClientSession() as http:
        one, two = Client(http, args.url, secret), Client(http, args.url, secret)
        clients = [one, two]
        await one.connect()
        await two.connect()
        assert one.instance == two.instance
        result["agentInstanceId"] = one.instance
        result["nativeVersion"] = one.version
        for label, session, workspace in [
            ("relativeWorkspace", args.session, "."),
            ("wrongWorkspace", args.session, "/tmp"),
            ("missingSession", "00000000-0000-0000-0000-000000000000", cwd),
        ]:
            response = await one.rpc(
                "session/load", {"sessionId": session, "cwd": workspace, "mcpServers": []}
            )
            assert "error" in response, label
            result["checks"][label] = "rejected"
        roster = (await one.rpc("_x.ai/sessions/list", {}))["result"]
        rows = roster.get("result", roster)["sessions"]
        resident_before = {r["sessionId"] for r in rows if r.get("resident")}
        dormant = next((r for r in rows if not r.get("resident") and r.get("cwd") == cwd), None)
        if dormant:
            response = await one.rpc(
                "session/load", {"sessionId": dormant["sessionId"], "cwd": cwd, "mcpServers": []}
            )
            assert "error" in response
            result["checks"]["dormantSession"] = "rejected"
        await one.attach(args.session, cwd)
        await two.attach(args.session, cwd)
        roster = (await one.rpc("_x.ai/sessions/list", {}))["result"]
        assert {
            r["sessionId"] for r in roster.get("result", roster)["sessions"] if r.get("resident")
        } == resident_before
        result["checks"]["invalidAttachmentCreatesNoActor"] = True
        for forbidden in ("session/new", "session/resume"):
            assert "error" in await one.rpc(forbidden, {"sessionId": args.session, "cwd": cwd})
        result["checks"]["createAndColdResumeRejected"] = True
        stamp = str(int(time.time()))
        filename = f"edge-pending-{stamp}.txt"
        prompt = (
            f"EDGE-PENDING-{stamp}: Run exactly one shell command to append exactly one line "
            f"EDGE-APPROVED-{stamp} to {filename} in this workspace. Use python3 -c with pathlib. "
            f"Do not repeat it. Ask permission normally. After success reply EDGE-PENDING-DONE-{stamp}."
        )
        start1, start2 = len(one.events), len(two.events)
        running = asyncio.create_task(
            one.rpc(
                "session/prompt",
                {"sessionId": args.session, "prompt": [{"type": "text", "text": prompt}]},
            )
        )
        permission = await two.permission(start2)
        assert not Path(cwd, filename).exists(), "Tool executed before approval"
        await two.close()
        three = Client(http, args.url, secret)
        clients.append(three)
        await three.connect()
        await three.attach(args.session, cwd)
        recovered = await three.permission()
        assert (
            recovered["params"]["toolCall"]["toolCallId"]
            == permission["params"]["toolCall"]["toolCallId"]
        )
        assert not Path(cwd, filename).exists(), "Disconnect resolved permission unexpectedly"
        first = await one.permission(start1)
        await asyncio.gather(one.answer(first), three.answer(recovered))
        response = await running
        assert "error" not in response
        await one.wait_event(
            lambda e: (
                e.get("params", {}).get("update", {}).get("sessionUpdate") == "interaction_resolved"
            ),
            start1,
        )
        await three.wait_event(
            lambda e: (
                e.get("params", {}).get("update", {}).get("sessionUpdate") == "interaction_resolved"
            )
        )
        await three.answer(recovered)  # deliberately stale duplicate answer
        await asyncio.sleep(1)
        assert Path(cwd, filename).read_text().splitlines() == [f"EDGE-APPROVED-{stamp}"]
        result["checks"]["pendingApprovalSurvivesDisconnect"] = True
        result["checks"]["concurrentAndStaleApprovalExecutesOnce"] = True
        result["checks"]["resolutionBroadcastToBothClients"] = True
        result["approvalToolCallId"] = recovered["params"]["toolCall"]["toolCallId"]
        result["approvalFile"] = filename
        denied_file = Path(cwd, f"edge-denied-{stamp}.txt")
        denied_prompt = (
            f"EDGE-DENY-{stamp}: Request normal permission to run one shell command writing "
            f"SHOULD-NOT-EXIST into {denied_file.name}. Use python3 -c with pathlib. "
            "If permission is rejected, do not retry, do not use another tool, and reply DENIED."
        )
        denial_start = len(three.events)
        denying = asyncio.create_task(
            one.rpc(
                "session/prompt",
                {
                    "sessionId": args.session,
                    "prompt": [{"type": "text", "text": denied_prompt}],
                },
            )
        )
        denial_request = await three.permission(denial_start)
        await three.answer(denial_request, "reject-once")
        denial_response = await denying
        assert "error" not in denial_response
        assert not denied_file.exists(), "Rejected tool executed"
        await three.answer(denial_request)  # A late allow must not overturn denial.
        await asyncio.sleep(1)
        assert not denied_file.exists(), "Stale allow overturned a rejection"
        result["checks"]["rejectedPermissionDoesNotExecute"] = True
        result["checks"]["staleAllowCannotOverturnRejection"] = True
        result["deniedFile"] = denied_file.name
        # Real queue pressure: four distinct requests released together on two subscribers.
        start1 = len(one.events)
        submissions = []
        markers = [f"EDGE-QUEUE-{stamp}-{index}" for index in range(4)]
        for index, marker in enumerate(markers):
            client = one if index % 2 == 0 else three
            submissions.append(
                asyncio.create_task(
                    client.rpc(
                        "session/prompt",
                        {
                            "sessionId": args.session,
                            "prompt": [
                                {
                                    "type": "text",
                                    "text": f"{marker}: Reply exactly ACK-{marker}. Do not use tools.",
                                }
                            ],
                        },
                    )
                )
            )
        responses = await asyncio.gather(*submissions)
        assert all("error" not in response for response in responses)
        user_texts = [
            u.get("content", {}).get("text", "") for u in updates(one, "user_message_chunk")
        ]
        assistant_text = "".join(
            u.get("content", {}).get("text", "") for u in updates(one, "agent_message_chunk")
        )
        for marker in markers:
            assert sum(marker in text for text in user_texts) == 1, marker
            assert assistant_text.count(f"ACK-{marker}") == 1, marker
        result["checks"]["fourSimultaneousPromptsEachOnce"] = True
        result["queueMarkers"] = markers
        result["queueNotifications"] = sum(
            e.get("method") == "_x.ai/queue/changed" for e in one.events[start1:]
        )
        for client in clients:
            if not client.ws.closed:
                await client.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18794/ws")
    parser.add_argument("--secret-file", default=str(Path.home() / ".grok/mobile-bridge.secret"))
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = asyncio.run(verify(args))
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

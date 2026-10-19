"""Real native question/plan request driver; requires manual or Simulator decision."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import aiohttp
from verify_edge_live import Client


async def verify(args):
    kind = args.kind
    sid = args.session
    cwd = str(Path(args.cwd).resolve())
    if kind == "question":
        prompt = "PHONE-NATIVE-QUESTION: Use the real ask_user_question tool now with exactly TWO questions. Question0 single-select: header Color, question Choose a color, options Blue and Green. Question1 multi-select: header Snacks, question Choose snacks, options Apple and Bread. Wait for the human answers, then reply exactly PHONE-QUESTION-DONE. No other tools or edits."
    else:
        prompt = "PHONE-NATIVE-PLAN: Enter plan mode if necessary, then use the real exit_plan_mode tool to present this harmless plan for approval: Verify the existing shared session and report completion; no files or commands are needed. Wait for human plan approval, then reply exactly PHONE-PLAN-DONE. Do not execute other tools or edit files."
    async with aiohttp.ClientSession() as h:
        c = Client(h, args.url, Path(args.secret_file).expanduser().read_text().strip())
        c.timeout = 600
        await c.connect()
        await c.attach(sid, cwd)
        start = len(c.events)
        run = asyncio.create_task(
            c.rpc(
                "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]}
            )
        )
        seen = set()
        while not run.done():
            for e in c.events[start:]:
                method = e.get("method", "")
                if (
                    method
                    in (
                        "_x.ai/ask_user_question",
                        "_x.ai/exit_plan_mode",
                        "x.ai/ask_user_question",
                        "x.ai/exit_plan_mode",
                        "session/request_permission",
                    )
                    and e.get("id") not in seen
                ):
                    seen.add(e.get("id"))
                    print(json.dumps({"method": method, "pending": True}), flush=True)
            await asyncio.sleep(0.1)
        response = await run
        out = {
            "sessionId": sid,
            "nativeVersion": c.version,
            "kind": kind,
            "response": response,
            "interactionMethods": [
                e["method"]
                for e in c.events[start:]
                if e.get("method")
                in (
                    "_x.ai/ask_user_question",
                    "_x.ai/exit_plan_mode",
                    "x.ai/ask_user_question",
                    "x.ai/exit_plan_mode",
                )
            ],
            "completed": any(
                f"PHONE-{kind.upper()}-DONE"
                in e.get("params", {}).get("update", {}).get("content", {}).get("text", "")
                for e in c.events[start:]
            ),
        }
        Path(args.output).write_text(json.dumps(out, indent=2) + "\n")
        print(out)
        await c.close()
        assert "error" not in response, "Native prompt failed"
        assert out["interactionMethods"], "Model did not issue the requested native interaction"
        assert out["completed"], "Native turn did not return the expected completion marker"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Exercise real native structured questions or plan approvals; answer using an attached device."
    )
    parser.add_argument("--kind", choices=("question", "plan"), required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--secret-file", default=str(Path.home() / ".grok/mobile-bridge.secret"))
    parser.add_argument("--output", required=True)
    asyncio.run(verify(parser.parse_args()))

#!/usr/bin/env python3
"""Verify actual native worktree forks use execution cwd, not roster display cwd.

Uses an existing disposable Git root and one of its actual Git worktrees. Creates
and closes only its own native parent/fork actors. One explicitly approved native
shell command writes one uniquely named file in the worktree, never the root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import time

import aiohttp

from bridge import connect_leader, unwrap_result
from verify_actor_identity_live import Native
from verify_edge_live import Client


async def verify(args):
    root, worktree = Path(args.source_cwd).resolve(), Path(args.worktree_cwd).resolve()
    assert root != worktree
    common = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"], text=True
    ).strip()
    child_common = subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"], text=True
    ).strip()
    assert (root / common).resolve() == (worktree / child_common).resolve()
    assert (worktree / ".git").is_file(), "Requires a real linked Git worktree"
    reader, writer = await connect_leader(Path(args.leader_socket).expanduser(), args.cli_version)
    native = Native(reader, writer)
    await native.rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    await native.rpc("authenticate", {"methodId": "cached_token"})
    parent = await native.rpc("session/new", {"cwd": str(root), "mcpServers": []})
    parent_sid = parent["result"]["sessionId"]
    sid = None
    try:
        fork = unwrap_result(
            await native.rpc(
                "_x.ai/session/fork",
                {
                    "sourceSessionId": parent_sid,
                    "sourceCwd": str(root),
                    "newCwd": str(worktree),
                    "sessionKind": "worktree",
                    "sourceWorkspaceDir": str(root),
                },
            )
        )
        sid = fork["newSessionId"]
        loaded = await native.rpc(
            "session/load",
            {
                "sessionId": sid,
                "cwd": str(worktree),
                "mcpServers": [],
                "_meta": {"yoloMode": False, "autoMode": False},
            },
        )
        assert "error" not in loaded, loaded
        result = {
            "sessionId": sid,
            "parentSessionId": parent_sid,
            "nativeVersion": args.cli_version,
            "checks": {},
        }
        checks = result["checks"]
        checks["realGitWorktreeWithNativeSessionFork"] = True
        secret = Path(args.secret_file).expanduser().read_text().strip()
        async with aiohttp.ClientSession() as http:
            one, two = Client(http, args.url, secret), Client(http, args.url, secret)
            await one.connect()
            await two.connect()
            roster = unwrap_result(await one.rpc("_x.ai/sessions/list", {}))
            row = next(row for row in roster["sessions"] if row["sessionId"] == sid)
            info = unwrap_result(await one.rpc("_x.ai/session/info", {"sessionId": sid}))
            assert row["resident"] is True
            assert row["cwd"] == str(root), row["cwd"]
            assert info["cwd"] == str(worktree), info["cwd"]
            assert row["cwd"] != info["cwd"]
            checks["rosterDisplayCwdDiffersFromActualExecutionCwd"] = True
            result["rosterDisplayCwd"], result["executionCwd"] = row["cwd"], info["cwd"]
            wrong = await one.rpc(
                "session/load", {"sessionId": sid, "cwd": str(root), "mcpServers": []}
            )
            assert "error" in wrong, wrong
            checks["displayCwdRejectedByNativeAtomicGuard"] = True
            params = {"sessionId": sid, "cwd": info["cwd"], "mcpServers": []}
            first = await one.rpc("session/load", params)
            second = await two.rpc("session/load", params)
            assert "error" not in first and "error" not in second
            actor = first["result"]["_meta"]["x.ai/residentActorId"]
            assert second["result"]["_meta"]["x.ai/residentActorId"] == actor
            result["residentActorId"] = actor
            checks["twoMobileClientsAttachedSameNativeWorktreeActor"] = True
            stamp = str(time.time_ns())
            filename, line = f"edge-worktree-{stamp}.txt", f"WORKTREE-ONCE-{stamp}"
            start = len(two.events)
            prompt = f"WORKTREE-MOBILE-EDIT: Run exactly once this command, requesting normal permission: python3 -c \"from pathlib import Path; Path('{filename}').open('a').write('{line}\\n')\" . Do not change directory or use other tools. Then reply exactly WORKTREE-MOBILE-EDIT-DONE."
            running = asyncio.create_task(
                one.rpc(
                    "session/prompt",
                    {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
                )
            )
            request = await two.permission(start)
            assert not (worktree / filename).exists() and not (root / filename).exists()
            await two.answer(request)
            response = await running
            assert "error" not in response, response
            assert (worktree / filename).read_text().splitlines() == [line]
            assert not (root / filename).exists()
            checks["approvedEditExecutedOnceOnlyInActualWorktree"] = True
            result["filename"] = filename
            await one.close()
            await two.close()
        return result
    finally:
        if sid:
            await native.rpc("session/close", {"sessionId": sid})
        await native.rpc("session/close", {"sessionId": parent_sid})
        writer.close()
        await writer.wait_closed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leader-socket", required=True)
    parser.add_argument("--cli-version", default="1.0.45")
    parser.add_argument("--url", required=True)
    parser.add_argument("--secret-file", default=str(Path.home() / ".grok/mobile-bridge.secret"))
    parser.add_argument("--source-cwd", required=True)
    parser.add_argument("--worktree-cwd", required=True)
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

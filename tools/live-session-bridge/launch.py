"""Start the pinned local leader/bridge, then enter its interactive Terminal UI."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from bridge import connect_leader, default_workspace_reader, load_secret

BINARY_SHA256 = "27efdb05b8e0c1482ce4158db026e9b574be57f3a4674ee545ba17624754ebc8"
VERSION = "1.0.45"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--binary",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "target/debug/xai-grok-pager",
    )
    parser.add_argument(
        "--leader-socket", type=Path, default=Path.home() / ".grok/leader-mobile.sock"
    )
    parser.add_argument(
        "--secret-file", type=Path, default=Path.home() / ".grok/mobile-bridge.secret"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Use 0.0.0.0 for a trusted LAN iPhone")
    parser.add_argument("--port", type=int, default=18789)
    parser.add_argument("--workspace-reader", type=Path,
                        help="Pinned read-only dashboard exporter; defaults to the packaged or preserved tested helper")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    try:
        workspace = args.workspace.expanduser().resolve(strict=True)
        binary = args.binary.expanduser().resolve(strict=True)
    except OSError:
        parser.error("Workspace and pinned binary must exist")
    if not workspace.is_dir():
        parser.error("Workspace must be a directory")
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error("Pinned binary must be an executable regular file")
    if hashlib.sha256(binary.read_bytes()).hexdigest() != BINARY_SHA256:
        parser.error("Binary differs from the documented tested build; use the pinned binary")
    reader = args.workspace_reader or default_workspace_reader()
    reader = reader.expanduser().absolute()
    # Refuse occupied ports, rather than replacing an unrelated bridge/service.
    probe = socket.socket()
    try:
        probe.bind((args.host, args.port))
    except OSError:
        parser.error("Bridge port is occupied; choose a free --port")
    finally:
        probe.close()
    args.leader_socket = args.leader_socket.expanduser().resolve()
    # Keep a final symlink visible to load_secret's O_NOFOLLOW validation.
    args.secret_file = args.secret_file.expanduser().absolute()
    state = Path.home() / ".grok/mobile-bridge"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not args.secret_file.exists():
        import secrets

        args.secret_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(args.secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
    try:
        load_secret(args.secret_file)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    async def leader_ready() -> bool:
        try:
            _, writer = await connect_leader(args.leader_socket, VERSION, require_capability=True)
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            return False

    if args.leader_socket.exists():
        if not asyncio.run(leader_ready()):
            parser.error(
                "Existing leader is unavailable or has wrong version; choose a fresh socket"
            )
    else:
        with (state / "leader.log").open("a") as logfile:
            leader_process = subprocess.Popen(
                [
                    str(binary),
                    "--permission-mode",
                    "default",
                    "--leader-socket",
                    str(args.leader_socket),
                    "agent",
                    "leader",
                    "--no-auto-update",
                    "--no-exit-on-disconnect",
                    "--relay-on-demand",
                ],
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=logfile,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 90
        while not asyncio.run(leader_ready()):
            if leader_process.poll() is not None:
                parser.error("Leader startup failed; inspect ~/.grok/mobile-bridge/leader.log")
            if time.monotonic() >= deadline:
                parser.error("Leader startup failed; inspect ~/.grok/mobile-bridge/leader.log")
            time.sleep(0.25)
    bridge_script = Path(__file__).with_name("bridge.py")
    with (state / "transport.log").open("a") as logfile:
        process = subprocess.Popen(
            [
                sys.executable,
                str(bridge_script),
                "--leader-socket",
                str(args.leader_socket),
                "--cli-version",
                VERSION,
                "--secret-file",
                str(args.secret_file),
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--workspace-reader",
                str(reader),
            ],
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + 10
    while True:
        if process.poll() is not None:
            parser.error("Bridge startup failed; inspect ~/.grok/mobile-bridge/transport.log")
        try:
            with socket.create_connection(
                ("127.0.0.1" if args.host == "0.0.0.0" else args.host, args.port), timeout=0.5
            ):
                break
        except OSError:
            if time.monotonic() >= deadline:
                process.terminate()
                parser.error("Bridge did not start")
            time.sleep(0.1)
    print(
        f"Mobile bridge ready on port {args.port}; credentials remain in {args.secret_file}.",
        flush=True,
    )
    print(
        f"Transport PID {process.pid}; quitting Terminal leaves the bridge/leader running.",
        flush=True,
    )
    os.execv(
        str(binary),
        [
            str(binary),
            "--leader",
            "--no-auto-update",
            "--leader-socket",
            str(args.leader_socket),
            "--permission-mode",
            "default",
            "--cwd",
            str(workspace),
        ],
    )


if __name__ == "__main__":
    main()

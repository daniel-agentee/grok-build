"""Install independent login services for the pinned native leader and bridge.

The leader service starts no saved conversations. An existing listener is adopted
without terminating it; an incompatible or unready listener is never replaced.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass, fields
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import socket
import stat
import subprocess
import sys
from typing import Any

from bridge import WORKSPACE_READER_SHA256, connect_leader, default_workspace_reader, load_secret
from launch import BINARY_SHA256, VERSION

DEFAULT_STATE = Path.home() / ".grok/mobile-bridge/service"
DEFAULT_CONFIG = DEFAULT_STATE / "service.json"
DEFAULT_BINARY = Path(__file__).resolve().parents[2] / "target/verified-all-sessions-20261021/xai-grok-pager"
LABEL_PATTERN = re.compile(r"[A-Za-z0-9]+(?:[.-][A-Za-z0-9-]+)+\Z")


@dataclass(frozen=True)
class ServiceConfig:
    binary: str
    workspace_reader: str
    workspace: str
    leader_socket: str
    secret_file: str
    host: str
    port: int
    state_dir: str
    label_prefix: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def safe_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise ValueError("Service directory must belong to this user and disallow group/world writes")


def safe_file(path: Path, *, digest: str | None = None, executable: bool = False,
              private: bool = False) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()):
            raise ValueError("Service input must be a trusted regular file")
        if info.st_mode & (0o077 if private else 0o022):
            raise ValueError("Service input is writable or readable by an unsafe owner")
        if executable and not info.st_mode & 0o100:
            raise ValueError("Service artifact must be executable")
        if digest:
            checksum = hashlib.sha256()
            while chunk := handle.read(1024 * 1024):
                checksum.update(chunk)
            if checksum.hexdigest() != digest:
                raise ValueError("Service artifact digest differs from the tested pin")
            value = b""
        else:
            value = handle.read()
    return value


def validate_config(config: ServiceConfig) -> None:
    if type(config.port) is not int or not 1 <= config.port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if config.host not in ("127.0.0.1", "0.0.0.0", "::1"):
        raise ValueError("Service host must be loopback or the explicit trusted-LAN wildcard")
    if not LABEL_PATTERN.fullmatch(config.label_prefix) or len(config.label_prefix) > 120:
        raise ValueError("Invalid launchd label prefix")
    for name in ("binary", "workspace_reader", "workspace", "leader_socket", "secret_file", "state_dir"):
        value = getattr(config, name)
        if not isinstance(value, str) or not Path(value).is_absolute() or "\0" in value:
            raise ValueError("Service paths must be absolute")
    safe_directory(Path(config.workspace))
    safe_directory(Path(config.leader_socket).parent)
    if len(os.fsencode(config.leader_socket)) >= 104:
        raise ValueError("Leader socket path is too long for macOS")
    path = Path(config.leader_socket)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Existing leader path must be a same-user socket")
    safe_file(Path(config.binary), digest=BINARY_SHA256, executable=True)
    safe_file(Path(config.workspace_reader), digest=WORKSPACE_READER_SHA256, executable=True)
    # Preserve descriptor-based secret validation; no value is saved in config,
    # launchd plists, process arguments, diagnostic output or the environment.
    load_secret(Path(config.secret_file))


def read_config(path: Path) -> ServiceConfig:
    value = json.loads(safe_file(path, private=True))
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(ServiceConfig)}:
        raise ValueError("Service configuration has unexpected fields")
    return ServiceConfig(**value)


def write_private(path: Path, value: bytes) -> None:
    # The parent is private and owned; O_EXCL prevents following a pre-existing
    # file or symlink. Never silently replace a registered service configuration.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)


def persist_config(config: ServiceConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    safe_directory(path.parent)
    if path.exists() or path.is_symlink():
        if read_config(path) != config:
            raise ValueError("A different service configuration exists; inspect it before changing an active owner")
        return
    write_private(path, json.dumps(config.as_dict(), indent=2).encode() + b"\n")


def runtime_environment(config: ServiceConfig) -> dict[str, str]:
    # Cached Grok authentication is read by the native CLI. Do not copy shell API
    # keys, tool sandbox overrides or tool-runner process-lifetime controls.
    account = pwd.getpwuid(os.getuid())
    home = account.pw_dir
    return {
        "HOME": home,
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "SHELL": "/bin/zsh",
        "LANG": "en_US.UTF-8",
        "PATH": f"{home}/bin:{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "GROK_HOME": str(Path(config.leader_socket).parent),
    }


def leader_command(config: ServiceConfig) -> list[str]:
    return [config.binary, "--permission-mode", "default", "--leader-socket", config.leader_socket,
            "agent", "leader", "--no-auto-update", "--no-exit-on-disconnect", "--relay-on-demand"]


def bridge_command(config: ServiceConfig) -> list[str]:
    # Resolving a venv's interpreter symlink loses pyvenv.cfg discovery and its
    # installed aiohttp dependency. Preserve the interpreter path used to install.
    return [str(Path(sys.executable).absolute()), str(Path(__file__).with_name("bridge.py").resolve()),
            "--leader-socket", config.leader_socket, "--cli-version", VERSION,
            "--secret-file", config.secret_file, "--workspace-reader", config.workspace_reader,
            "--host", config.host, "--port", str(config.port)]


def make_plists(config: ServiceConfig, config_path: Path) -> dict[str, dict[str, Any]]:
    return {role: {
        "Label": f"{config.label_prefix}.{role}",
        "ProgramArguments": [str(Path(sys.executable).absolute()), str(Path(__file__).resolve()),
                             "run", "--config", str(config_path), "--role", role],
        "WorkingDirectory": config.workspace,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        # These are interactive session services. Background QoS can starve a
        # pinned binary's startup verification for minutes on a busy Mac.
        "ProcessType": "Interactive",
        "StandardOutPath": str(Path(config.state_dir) / f"{role}.log"),
        "StandardErrorPath": str(Path(config.state_dir) / f"{role}.log"),
    } for role in ("leader", "bridge")}


async def probe_leader(path: Path) -> str:
    try:
        _, writer = await asyncio.wait_for(connect_leader(path, VERSION, require_capability=True), 3)
        writer.close()
        await writer.wait_closed()
        return "ready"
    except (FileNotFoundError, ConnectionRefusedError):
        return "missing"
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        # A bound but unready, hung, or incompatible owner must never be killed or
        # have its socket unlinked by a watchdog. Native startup keeps its flock.
        return "occupied"


async def wait_until_unowned(path: Path) -> None:
    previous = None
    while True:
        state = await probe_leader(path)
        if state == "missing":
            return
        if state != previous:
            print("Existing compatible native owner adopted" if state == "ready"
                  else "Existing native listener preserved; awaiting compatible readiness", flush=True)
            previous = state
        await asyncio.sleep(3)


def ensure_port_free(config: ServiceConfig) -> None:
    family = socket.AF_INET6 if config.host == "::1" else socket.AF_INET
    with socket.socket(family) as listener:
        # Match aiohttp's normal listener behavior: old closed connections in
        # TIME_WAIT must not be mistaken for another running bridge.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((config.host, config.port))
        except OSError as error:
            raise ValueError("Bridge port is occupied; the installer will not replace that service") from error


def loaded_labels() -> set[str]:
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=True)
    # `launchctl list` prints only PID / status / label. Never use `print`, which
    # includes the environment and can disclose unrelated credentials.
    return {line.split()[-1] for line in result.stdout.splitlines() if len(line.split()) == 3}


def install(config: ServiceConfig, config_path: Path, agents_dir: Path) -> None:
    validate_config(config)
    safe_file(Path(__file__).resolve())
    safe_file(Path(__file__).with_name("bridge.py").resolve())
    plists = make_plists(config, config_path)
    loaded = loaded_labels()
    if plists["bridge"]["Label"] not in loaded:
        ensure_port_free(config)
    persist_config(config, config_path)
    agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe_directory(agents_dir)
    for role, value in plists.items():
        log = Path(config.state_dir) / f"{role}.log"
        if not log.exists():
            write_private(log, b"")
        else:
            safe_file(log, private=True)
        path = agents_dir / f"{value['Label']}.plist"
        content = plistlib.dumps(value)
        if path.exists() or path.is_symlink():
            if safe_file(path, private=True) != content:
                raise ValueError("A different launch agent exists; no active process was replaced")
        else:
            write_private(path, content)
        if value["Label"] not in loaded:
            result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
                                    capture_output=True, text=True)
            if result.returncode:
                raise ValueError(f"launchd could not register {role}; exit status {result.returncode}")
    print("Login services installed; leader and bridge recover independently. No saved conversations were resumed.")


def run_role(config: ServiceConfig, role: str) -> None:
    validate_config(config)
    if role == "leader":
        asyncio.run(wait_until_unowned(Path(config.leader_socket)))
        command = leader_command(config)
    else:
        command = bridge_command(config)
    os.chdir(config.workspace)
    # Exec, rather than detaching a child: launchd supervises the actual long-lived
    # native or bridge PID, and bridge restart cannot signal the independent owner.
    os.execve(command[0], command, runtime_environment(config))


async def status(config: ServiceConfig) -> dict[str, Any]:
    import aiohttp

    state = await probe_leader(Path(config.leader_socket))
    host = "127.0.0.1" if config.host == "0.0.0.0" else config.host
    url_host = f"[{host}]" if ":" in host else host
    transport = False
    status_code = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as client:
            async with client.ws_connect(f"http://{url_host}:{config.port}/ws",
                                         headers={"Authorization": "Bearer " + load_secret(Path(config.secret_file))}) as ws:
                transport = True
                await ws.close()
    except aiohttp.WSServerHandshakeError as error:
        status_code = error.status
    except (OSError, asyncio.TimeoutError):
        pass
    return {"leader": state, "authenticatedWebSocket": transport, "httpError": status_code,
            "registeredRoles": sorted(label.rsplit(".", 1)[-1] for label in loaded_labels()
                                      if label in {f"{config.label_prefix}.leader", f"{config.label_prefix}.bridge"})}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("install", help="Register durable per-user login services")
    create.add_argument("--workspace", type=Path, default=Path.home())
    create.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    create.add_argument("--workspace-reader", type=Path, default=default_workspace_reader())
    create.add_argument("--leader-socket", type=Path, default=Path.home() / ".grok/leader-mobile.sock")
    create.add_argument("--secret-file", type=Path, default=Path.home() / ".grok/mobile-bridge.secret")
    create.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    create.add_argument("--label-prefix", default="io.grok.mobile")
    create.add_argument("--host", default="127.0.0.1")
    create.add_argument("--port", type=int, default=18809)
    execute = commands.add_parser("run")
    execute.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    execute.add_argument("--role", choices=("leader", "bridge"), required=True)
    inspect = commands.add_parser("status")
    inspect.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    terminal = commands.add_parser("terminal", help="Open Terminal on the supervised native owner")
    terminal.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    terminal.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "install":
            config = ServiceConfig(**{name: str(getattr(args, name).expanduser().absolute())
                                     for name in ("binary", "workspace_reader", "workspace", "leader_socket", "secret_file", "state_dir")},
                                   host=args.host, port=args.port, label_prefix=args.label_prefix)
            install(config, Path(config.state_dir) / "service.json", Path.home() / "Library/LaunchAgents")
        else:
            config = read_config(args.config.expanduser().absolute())
            if args.command == "run":
                run_role(config, args.role)
            elif args.command == "status":
                result = asyncio.run(status(config))
                print(json.dumps(result))
                if not result["authenticatedWebSocket"]:
                    raise SystemExit(1)
            else:
                validate_config(config)
                workspace = args.workspace.expanduser().resolve(strict=True) if args.workspace else Path(config.workspace)
                if not workspace.is_dir():
                    raise ValueError("Terminal workspace must be a directory")
                os.execve(config.binary, [config.binary, "--leader", "--no-auto-update", "--leader-socket",
                                         config.leader_socket, "--permission-mode", "default", "--cwd", str(workspace)],
                          runtime_environment(config))
    except (OSError, ValueError, TypeError) as error:
        # The errors above are controlled descriptions and never include auth or
        # native RPC data. OSError exposes only class/errno, not arbitrary filenames.
        message = str(error) if isinstance(error, ValueError) else f"{type(error).__name__} (errno {getattr(error, 'errno', None)})"
        parser.exit(2, f"Service unavailable: {message}\n")


if __name__ == "__main__":
    main()

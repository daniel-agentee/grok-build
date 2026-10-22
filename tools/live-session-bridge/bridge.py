"""Authenticated ACP WebSocket transport for an existing Grok leader.

The existing leader owns every session, including explicitly requested new ones.
The leader remains responsible for execution, queuing, replay, and approvals.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import hmac
import hashlib
import errno
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import struct
from typing import Any
import uuid

from aiohttp import WSMsgType, web

MAX_FRAME = 64 * 1024 * 1024
PROTOCOL = 1
PREFLIGHT_TIMEOUT = 15
MAX_PENDING_LOADS = 4
MAX_PENDING_REQUESTS = 128
MAX_OWNED_SESSIONS = 8
MAX_LEADER_SOURCES = 32
SOURCE_TIMEOUT = 4
WORKSPACE_READ_TIMEOUT = 2
MAX_WORKSPACE_MEMBERS = 256  # Native xai-grok-dashboard-store::WORKSPACE_CAPACITY.
MAX_WORKSPACE_OUTPUT = 2 * 1024 * 1024
WORKSPACE_READER_SHA256 = "cb04f8888bf2b11ea602fe2249ff08f92ca533270e7a7d2d37ab31f360bb10c1"
CREATION_RESPONSE_TIMEOUT = 30
ROSTER_METHOD = "_x.ai/sessions/list"
PREVIEW_METHOD = "_x.ai/session/updates"
MAX_PREVIEW_PAGE = 500
CATALOG_TTL = 15
MAX_CATALOGS = 32
CATALOG_TIMEOUT = 20
INTERNAL_PREFIX = "grok-mobile-preflight:"


def default_workspace_reader() -> Path:
    """Find a packaged helper, a preserved tested artifact, or a fresh build."""
    here = Path(__file__).resolve().parent
    packaged = here / "grok-dashboard-workspace"
    if packaged.exists():
        return packaged
    preserved = here.parents[1] / "target/verified-all-sessions-20261021/grok-dashboard-workspace"
    return preserved if preserved.exists() else here.parents[1] / "target/debug/grok-dashboard-workspace"


READ_METHODS = {
    "initialize",
    "authenticate",
    "workspace/list",
    "x.ai/billing",
    "x.ai/sessions/list",
    "_x.ai/sessions/list",
    "x.ai/session/info",
    "_x.ai/session/info",
    "x.ai/models/list",
    "_x.ai/models/list",
    "x.ai/workspaces/list",
    "_x.ai/workspaces/list",
    "_x.ai/commands/list",
    PREVIEW_METHOD,
}
SESSION_METHODS = {"session/prompt", "session/cancel", "session/set_model"}
INITIALIZE_META_KEYS = {"clientIdentifier", "clientUserMessageEcho"}
EMPTY_PARAM_METHODS = READ_METHODS - {
    "initialize",
    "authenticate",
    "x.ai/session/info",
    "_x.ai/session/info",
    "_x.ai/commands/list",
    PREVIEW_METHOD,
}
LOAD_META_KEYS = {
    "x.ai/residentActorId",
    "cursor",
    "x.ai/restore_code",
    "clientUserMessageEcho",
    "yoloMode",
    "autoMode",
    "x.ai/leaderClientId",
    "x.ai/resident_only",
    "x.ai/resumeSaved",
}
SESSION_PARAM_KEYS = {
    "session/load": {"sessionId", "cwd", "mcpServers", "_meta"},
    "session/prompt": {"sessionId", "prompt", "_meta"},
    "session/cancel": {"sessionId", "_meta"},
    "session/set_model": {"sessionId", "modelId", "_meta"},
}


def reject_nonfinite(value: str) -> None:
    raise ValueError("Non-finite numbers are not JSON")


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    size = struct.unpack(">I", await reader.readexactly(4))[0]
    if size > MAX_FRAME:
        raise ValueError("Leader frame exceeds size limit")
    value = json.loads(await reader.readexactly(size), parse_constant=reject_nonfinite)
    if not isinstance(value, dict):
        raise ValueError("Leader frame must be an object")
    return value


async def write_frame(writer: asyncio.StreamWriter, value: dict[str, Any]) -> None:
    data = json.dumps(value, separators=(",", ":")).encode()
    if len(data) > MAX_FRAME:
        raise ValueError("Leader frame exceeds size limit")
    writer.write(struct.pack(">I", len(data)) + data)
    await writer.drain()


async def connect_leader(socket: Path, version: str, *, require_capability: bool = False,
                         capability_result: dict[str, Any] | None = None):
    reader, writer = await asyncio.open_unix_connection(socket)
    try:
        await write_frame(
            writer,
            {
                "type": "register",
                "client_type": "grok-ios-bridge",
                "mode": "stdio",
                "capabilities": {"client_version": version, "user_message_echo": True},
            },
        )
        registration = await asyncio.wait_for(read_frame(reader), 10)
        if registration.get("type") != "registered":
            raise ValueError("Leader rejected registration")
        if registration.get("leader_protocol_version") != PROTOCOL:
            raise ValueError("Unsupported leader protocol; use the documented pinned CLI")
        if registration.get("leader_binary_version") != version:
            raise ValueError("Leader binary differs from the configured pinned CLI version")
        if not registration.get("ready", True):
            ready = await asyncio.wait_for(read_frame(reader), 90)
            if ready.get("type") != "leader_ready":
                raise ValueError("Leader did not become ready")
        if require_capability or capability_result is not None:

            async def verify_capability():
                identifier = INTERNAL_PREFIX + "capability"
                await write_frame(
                    writer,
                    {
                        "type": "acp",
                        "payload": json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": identifier,
                                "method": "initialize",
                                "params": {"protocolVersion": 1, "clientCapabilities": {}},
                            }
                        ),
                    },
                )
                while True:
                    frame = await read_frame(reader)
                    if frame.get("type") != "acp":
                        continue
                    response = json.loads(frame["payload"], parse_constant=reject_nonfinite)
                    if response.get("id") != identifier:
                        continue
                    metadata = unwrap_result(response).get("_meta", {})
                    supported = (isinstance(metadata, dict)
                                 and metadata.get("x.ai/residentOnlyAttach") is True
                                 and metadata.get("x.ai/residentActorIdentity") is True)
                    if capability_result is not None:
                        capability_result["attachSupported"] = supported
                    if require_capability and not supported:
                        raise ValueError("Pinned leader lacks resident-only attachment capability")
                    return

            await asyncio.wait_for(verify_capability(), 10)
        return reader, writer
    except BaseException:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()
        raise


def load_secret(path: Path) -> str:
    # Validate the opened descriptor, not a pathname subsequently reopened for
    # reading. O_NOFOLLOW also rejects symlinks; NONBLOCK avoids hanging on FIFOs.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Secret must be an owner-only regular file (mode 600)")
        secret = handle.read().strip()
    if len(secret) < 32:
        raise ValueError("Secret must contain at least 32 characters")
    return secret


def rpc_error(request: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "error": {"code": -32602, "message": message},
    }


def request_key(value: dict[str, Any]) -> str:
    # JSON preserves the distinction between numeric 7 and string "7", unlike str().
    return json.dumps(value.get("id"), separators=(",", ":"))


def valid_rpc(value: dict[str, Any]) -> bool:
    if "id" in value and type(value["id"]) not in (str, int):
        return False
    if "method" in value:
        return (
            isinstance(value["method"], str)
            and bool(value["method"])
            and "result" not in value
            and "error" not in value
            and (value["method"] not in ("session/new", "session/load", "session/prompt") or "id" in value)
        )
    return "id" in value and (("result" in value) != ("error" in value))


def unwrap_result(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result", {})
    # xAI ExtMethodResult nests its payload in a second `result`.
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        return result["result"]
    return result if isinstance(result, dict) else {}


def source_error(request: dict[str, Any], code: str, message: str, **data) -> dict[str, Any]:
    result = rpc_error(request, message)
    result["error"]["data"] = {"code": code, **data}
    return result


class LeaderDirectory:
    """Bounded native-style leader*.sock discovery. Tokens never accept client paths.

    Only owner-controlled, non-symlink sockets in the configured primary's own
    directory are considered. Every read verifies pinned version and live actor
    capabilities. Previously observed owners are retained to fail closed on loss.
    """

    def __init__(self, primary: Path, secret: str, version: str, *, workspace_reader: Path | None = None):
        self.primary = primary.absolute()
        self.secret, self.version = secret, version
        self.sources: dict[str, Path] = {}
        self.attach_supported: dict[str, bool] = {}
        self.known_residents: dict[str, set[str]] = {}
        self.lock = asyncio.Lock()
        self.resume_lock = asyncio.Lock()
        self.cached_snapshot = None
        self.cached_at = 0.0
        self.workspace_reader = workspace_reader
        self.workspace_reader_sha256 = WORKSPACE_READER_SHA256
        self.workspace_members: set[tuple[str, str]] = set()
        self.catalogs: dict[tuple, tuple[float, dict]] = {}
        self.catalog_flights: dict[tuple, asyncio.Future] = {}
        self.refresh_paths()

    def catalog_key(self, source: Path, workspace: Path) -> tuple:
        """Cheap metadata invalidation, never a substitute for native trust checks.

        Native pre-session discovery rebuilds the registry. Reuse its display-only
        response briefly; avoid repeated recursive scans on every picker open.
        A source replacement or trust/config write invalidates immediately. Deep
        plugin file changes expire within CATALOG_TTL or an explicit refresh.
        """
        paths = [source, self.primary.parent / "trusted_folders.toml",
                 self.primary.parent / "trusted-plugins", self.primary.parent / "config.toml",
                 self.primary.parent / "managed_config.toml", Path("/etc/grok/managed_config.toml"),
                 Path.home() / ".claude/settings.json",
                 Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
        for root in (workspace, *workspace.parents, Path.home()):
            paths.extend(root / name for name in (
                ".grok/config.toml", ".grok/skills", ".grok/plugins",
                ".claude/skills", ".claude/commands", ".agents/skills"))
        def stamp(path):
            try:
                info = path.stat()
                return (str(path), info.st_dev, info.st_ino, info.st_mtime_ns,
                        info.st_ctime_ns, info.st_size)
            except FileNotFoundError:
                return (str(path), None)
        return (str(source), str(workspace), tuple(stamp(path) for path in paths))

    async def read_workspace_membership(self) -> tuple[set[tuple[str, str]], str | None]:
        """Native typed-store export; membership is separate from actor residence.

        Every discovered socket shares this trusted Grok home. The canonical
        dashboard store belongs to that home, not to an individual leader.
        A failed export never invents membership or removes valid live actors.
        """
        path = self.workspace_reader
        if path is None:
            return set(), "reader_unavailable"
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or not info.st_mode & 0o111
                    or hashlib.file_digest(handle, "sha256").hexdigest() != self.workspace_reader_sha256):
                    return set(), "reader_untrusted"
        except OSError:
            return set(), "reader_unavailable"
        process = None
        try:
            async with asyncio.timeout(WORKSPACE_READ_TIMEOUT):
                process = await asyncio.create_subprocess_exec(
                    str(path), str(self.primary.parent / "dashboard/workspace.db"),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                output = bytearray()
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > MAX_WORKSPACE_OUTPUT:
                        return set(), "invalid_snapshot"
                if await process.wait() != 0:
                    return set(), "read_failed"
                payload = json.loads(output, parse_constant=reject_nonfinite)
                rows = payload.get("members") if isinstance(payload, dict) else None
                if payload.get("schemaVersion") != 1 or not isinstance(rows, list) or len(rows) > MAX_WORKSPACE_MEMBERS:
                    return set(), "invalid_snapshot"
                members = set()
                for row in rows:
                    if (not isinstance(row, dict) or not isinstance(row.get("sessionId"), str)
                        or not row["sessionId"] or len(row["sessionId"]) > 512
                        or not isinstance(row.get("cwd"), str) or not Path(row["cwd"]).is_absolute()):
                        return set(), "invalid_snapshot"
                    members.add((row["sessionId"], os.path.normpath(row["cwd"])))
                return members, None
        except asyncio.TimeoutError:
            return set(), "read_timeout"
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            return set(), "invalid_snapshot"
        finally:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    def token(self, path: Path) -> str:
        return hmac.new(self.secret.encode(), str(path.absolute()).encode(), hashlib.sha256).hexdigest()[:24]

    def label(self, token: str) -> str:
        if self.sources[token] == self.primary:
            return "Primary Mac"
        name = self.sources[token].stem.removeprefix("leader").strip("-_")
        name = "".join(c if c.isascii() and (c.isalnum() or c in "-_") else " " for c in name).strip()[:40]
        return "Mac · " + (name or token[:6])

    def refresh_paths(self) -> bool:
        # Match native discover_leaders_in's leader*.sock naming, adding explicit
        # primary even when custom-named. Bound both files read and registered.
        root = self.primary.parent
        try:
            info = root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                return False
            candidates = [self.primary]
            with os.scandir(root) as entries:
                for index, entry in enumerate(entries):
                    if index >= 512:
                        return False
                    if entry.name.startswith("leader") and entry.name.endswith(".sock"):
                        candidates.append(Path(entry.path))
        except OSError:
            return False
        overflow = False
        for path in sorted(set(candidates), key=lambda p: (p != self.primary, str(p))):
            try:
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    continue
            except OSError:
                continue
            token = self.token(path)
            if token not in self.sources and len(self.sources) >= MAX_LEADER_SOURCES:
                overflow = True
                continue
            self.sources[token] = path
        # Keep a disappeared primary token and all previously-known sources.
        self.sources.setdefault(self.token(self.primary), self.primary)
        return not overflow

    def resolve(self, token: str | None) -> Path | None:
        self.refresh_paths()
        path = self.primary if token is None else self.sources.get(token)
        if path is not None:
            try:
                root, info = path.parent.lstat(), path.lstat()
                if (not stat.S_ISDIR(root.st_mode) or root.st_uid != os.getuid()
                    or root.st_mode & 0o022 or not stat.S_ISSOCK(info.st_mode)
                    or info.st_uid != os.getuid()):
                    return None
            except FileNotFoundError:
                pass  # Known offline source returns 503, never another origin.
        return path

    def is_workspace_member(self, row: dict[str, Any]) -> bool:
        cwd = row.get("cwd")
        return isinstance(cwd, str) and (row.get("sessionId"), os.path.normpath(cwd)) in self.workspace_members

    def enrich(self, row: dict[str, Any], token: str) -> dict[str, Any]:
        supported = self.attach_supported.get(token, True)
        return {**row, "bridgeLeaderId": token, "bridgeLeaderLabel": self.label(token),
                "workspaceMember": self.is_workspace_member(row),
                "bridgeAttachSupported": supported,
                **({"bridgeAttachUnsupportedReason": "upgrade_required"} if not supported else {})}

    async def read_source(self, token: str, path: Path) -> list[dict[str, Any]]:
        writer = None
        try:
            # Revalidate pathname trust on every connection, including remembered
            # tokens after a socket replacement. Never follow a symlink.
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Untrusted source")
            capabilities = {}
            reader, writer = await connect_leader(path, self.version, capability_result=capabilities)
            self.attach_supported[token] = capabilities["attachSupported"]
            for identifier, method, params in [
                (INTERNAL_PREFIX + "owner-auth", "authenticate", {"methodId": "cached_token"}),
                (INTERNAL_PREFIX + "directory", ROSTER_METHOD, {}),
            ]:
                await write_frame(writer, {"type": "acp", "payload": json.dumps({
                    "jsonrpc": "2.0", "id": identifier, "method": method, "params": params,
                })})
                while True:
                    envelope = await read_frame(reader)
                    if envelope.get("type") != "acp":
                        continue
                    response = json.loads(envelope["payload"], parse_constant=reject_nonfinite)
                    if response.get("id") == identifier and "method" not in response:
                        if "error" in response:
                            raise ValueError("Source rejected discovery")
                        break
            rows = unwrap_result(response).get("sessions")
            if not isinstance(rows, list):
                raise ValueError("Invalid source roster")
            return [self.enrich(row, token) for row in rows if isinstance(row, dict)
                    and isinstance(row.get("sessionId"), str)]
        finally:
            if writer is not None:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()

    async def snapshot(self, *, fresh=False) -> dict[str, Any]:
        async with self.lock:
            now = asyncio.get_running_loop().time()
            if not fresh and self.cached_snapshot is not None and now - self.cached_at < 1:
                return self.cached_snapshot
            complete = self.refresh_paths()
            sources = list(self.sources.items())
            async def bounded(token, path):
                try:
                    rows = await asyncio.wait_for(self.read_source(token, path), SOURCE_TIMEOUT)
                    return token, rows, None, False
                except (OSError, ValueError, TypeError, KeyError, asyncio.TimeoutError,
                        asyncio.IncompleteReadError) as error:
                    # A stale never-live socket is not an unknown live owner. A
                    # reachable incompatible/auth-failing source is, as is loss
                    # of any source that this bridge previously inspected.
                    stale = isinstance(error, OSError) and error.errno in {errno.ENOENT, errno.ECONNREFUSED}
                    unknown = not stale or token in self.known_residents
                    return token, [], "source_unavailable", unknown
            results, (members, membership_error) = await asyncio.gather(
                asyncio.gather(*(bounded(token, path) for token, path in sources)),
                self.read_workspace_membership(),
            )
            # Native deltas contain actor state only. Retain this independently
            # sourced membership projection when those deltas update the UI.
            # A failed read keeps the last known metadata and returns an explicit
            # refresh warning; it must not silently move open rows to History.
            if membership_error is None:
                self.workspace_members = members
            live, saved, statuses = [], {}, []
            for token, rows, error, unknown in results:
                supported = self.attach_supported.get(token, True)
                statuses.append({"bridgeLeaderId": token, "bridgeLeaderLabel": self.label(token),
                                 "available": error is None, "ownershipUnknown": unknown,
                                 "bridgeAttachSupported": supported,
                                 **({"error": error} if error else {"error": "upgrade_required"} if not supported else {})})
                if error is None:
                    self.known_residents[token] = {r["sessionId"] for r in rows if r.get("resident") is True}
                for row in rows:
                    if row.get("resident") is True:
                        live.append(row)
                    else:
                        sid = row["sessionId"]
                        if sid not in saved or token == self.token(self.primary):
                            saved[sid] = row
            resident_ids = {r["sessionId"] for r in live}
            # A source loss cannot relabel its last-known live actor as saved.
            unavailable_ids = set().union(*(self.known_residents.get(s["bridgeLeaderId"], set())
                                           for s in statuses if not s["available"])) if statuses else set()
            rows = live + [row for sid, row in saved.items() if sid not in resident_ids | unavailable_ids]
            for row in rows:
                row["workspaceMember"] = self.is_workspace_member(row)
            rows.sort(key=lambda r: r.get("lastChangeUnixMs") or 0, reverse=True)
            result = {"sessions": rows, "bridgeSources": statuses,
                      "bridgePartial": not complete or any(not s["available"] or not s["bridgeAttachSupported"] for s in statuses),
                      "bridgeResumeBlocked": not complete or any(s["ownershipUnknown"] for s in statuses)}
            if membership_error:
                result["bridgeWorkspaceMembershipError"] = membership_error
            self.cached_snapshot, self.cached_at = result, asyncio.get_running_loop().time()
            return result


class SessionOwner:
    """One bounded native subscriber for a deliberately mobile-created actor.

    The creator connection, rather than a second late attachment, owns the actor
    before its SID is exposed. A lost phone/response therefore cannot race idle
    eviction. It never answers permission requests or reconnects a dead actor.
    """

    def __init__(self, bridge: Bridge, request: dict[str, Any], before_dispatch=None):
        self.bridge = bridge
        self.request = request
        self.before_dispatch = before_dispatch
        self.session_id: str | None = None
        self.execution_cwd: str | None = None
        # None is an uncertain native outcome, never a definite RPC rejection.
        self.result: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        writer = None
        creation_dispatched = False
        try:
            reader, writer = await connect_leader(
                self.bridge.socket, self.bridge.version, require_capability=True
            )
            auth_id = INTERNAL_PREFIX + "owner-auth"
            await write_frame(writer, {"type": "acp", "payload": json.dumps({
                "jsonrpc": "2.0", "id": auth_id, "method": "authenticate",
                "params": {"methodId": "cached_token"},
            })})
            async with asyncio.timeout(30):
                while True:
                    frame = await read_frame(reader)
                    if frame.get("type") != "acp":
                        continue
                    auth = json.loads(frame["payload"], parse_constant=reject_nonfinite)
                    if auth.get("id") == auth_id and "method" not in auth:
                        if "error" in auth:
                            self.result.set_result({**auth, "id": self.request["id"]})
                            return
                        break
            if self.before_dispatch is not None:
                rejection = await self.before_dispatch()
                if rejection is not None:
                    self.result.set_result(rejection)
                    return
            creation_dispatched = True
            creation_deadline = asyncio.get_running_loop().time() + CREATION_RESPONSE_TIMEOUT
            await write_frame(writer, {"type": "acp", "payload": json.dumps(self.request)})
            while True:
                if self.result.done():
                    frame = await read_frame(reader)
                else:
                    async with asyncio.timeout_at(creation_deadline):
                        frame = await read_frame(reader)
                if frame.get("type") in {"shutdown", "shutting_down"}:
                    return
                if frame.get("type") != "acp":
                    continue
                value = json.loads(frame["payload"], parse_constant=reject_nonfinite)
                if not isinstance(value, dict):
                    raise ValueError("Invalid owner frame")
                if not self.result.done() and "method" not in value and value.get("id") == INTERNAL_PREFIX + "owner-info":
                    info = unwrap_result(value)
                    cwd = info.get("cwd")
                    if "error" in value or not isinstance(cwd, str) or not Path(cwd).is_absolute():
                        raise ValueError("Resumed execution workspace unavailable")
                    self.execution_cwd = cwd
                    await write_frame(writer, {"type": "acp", "payload": json.dumps({
                        "jsonrpc": "2.0", "id": INTERNAL_PREFIX + "owner-attach", "method": "session/load",
                        "params": {"sessionId": self.session_id, "cwd": cwd, "mcpServers": [],
                            "_meta": {"x.ai/resident_only": True, "x.ai/restore_code": False, "clientUserMessageEcho": True}},
                    })})
                    continue
                if not self.result.done() and "method" not in value and value.get("id") == INTERNAL_PREFIX + "owner-attach":
                    meta = unwrap_result(value).get("_meta", {})
                    if "error" in value or not isinstance(meta.get("x.ai/residentActorId"), str):
                        raise ValueError("Resumed actor identity unavailable")
                    meta["x.ai/bridgeExecutionCwd"] = self.execution_cwd
                    self.result.set_result({**value, "id": self.request["id"]})
                    continue
                if (
                    not self.result.done() and "method" not in value
                    and request_key(value) == request_key(self.request)
                ):
                    if "error" in value:
                        self.result.set_result(value)
                        return
                    sid = unwrap_result(value).get("sessionId")
                    if self.request.get("method") == "session/load" and sid is None:
                        # ACP LoadSessionResponse carries modes/config/meta, not
                        # the SID already supplied in its request.
                        sid = self.request["params"]["sessionId"]
                    if not isinstance(sid, str) or not sid:
                        raise ValueError("Native create omitted session identity")
                    if self.request.get("method") == "session/load" and sid != self.request["params"]["sessionId"]:
                        raise ValueError("Native resume changed session identity")
                    self.session_id = sid
                    if self.request.get("method") == "session/load":
                        # Native cold restoration deliberately omits incarnation
                        # metadata. Prove the new resident actor on this retained
                        # connection before exposing success to the phone.
                        await write_frame(writer, {"type": "acp", "payload": json.dumps({
                            "jsonrpc": "2.0", "id": INTERNAL_PREFIX + "owner-info", "method": "_x.ai/session/info",
                            "params": {"sessionId": sid},
                        })})
                        continue
                    self.result.set_result(value)
                method = value.get("method", "")
                params = value.get("params", {})
                if isinstance(method, str) and method.startswith("_"):
                    method = method[1:]
                    if isinstance(params, dict) and isinstance(params.get("method"), str):
                        method, params = params["method"], params.get("params", {})
                if (
                    method == "x.ai/sessions/changed" and isinstance(params, dict)
                    and isinstance(params.get("removed"), list)
                    and self.session_id in params["removed"]
                ):
                    return
        except (OSError, ValueError, TypeError, KeyError, asyncio.TimeoutError,
                asyncio.IncompleteReadError):
            # No raw exception text: native transport failures can carry URLs.
            pass
        finally:
            if not self.result.done():
                self.result.set_result(None if creation_dispatched else rpc_error(
                    self.request,
                    "Session creation connection ended before creation; reconnect and try again",
                ))
            if writer is not None:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
            self.bridge.owners.discard(self)


class Bridge:
    def __init__(self, socket: Path, secret: str, version: str, *, directory=None, root=None, workspace_reader=None):
        self.socket, self.secret, self.version = socket.absolute(), secret, version
        self.directory = directory or LeaderDirectory(socket, secret, version, workspace_reader=workspace_reader)
        self.root = root or self
        self.children: dict[str, Bridge] = {} if root is None else root.children
        self.owners: set[SessionOwner] = set() if root is None else root.owners
        self.creations: set[asyncio.Task] = set() if root is None else root.creations

    async def cleanup(self, app: web.Application) -> None:
        owners = tuple(self.owners)
        for owner in owners:
            owner.task.cancel()
        await asyncio.gather(*(owner.task for owner in owners), return_exceptions=True)
        await asyncio.gather(*tuple(self.creations), return_exceptions=True)

    async def health(self, request: web.Request) -> web.Response:
        available = False
        try:
            _, writer = await asyncio.wait_for(connect_leader(self.socket, self.version), 1)
            writer.close()
            await writer.wait_closed()
            available = True
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            pass
        return web.json_response(
            {
                "transport": "grok-leader-ipc",
                "protocol": PROTOCOL,
                "leaderAvailable": available,
            }
        )

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        supplied = request.headers.get("Authorization", "")
        supplied = supplied[7:] if supplied.startswith("Bearer ") else ""
        # Compatibility with the upstream iOS app's connection URL. Access logging
        # is disabled so query strings (and therefore credentials) never get logged.
        supplied = supplied or request.query.get("server-key", "")
        if not hmac.compare_digest(supplied.encode(), self.secret.encode()):
            raise web.HTTPUnauthorized(text="Invalid bridge credential")
        # Native apps omit Origin. Web pages must never inherit local agent access.
        if request.headers.get("Origin"):
            raise web.HTTPForbidden(text="Browser origins are not allowed")
        target = self.directory.resolve(request.query.get("leader"))
        if target is None:
            raise web.HTTPBadRequest(text="Unknown leader source; refresh sessions")
        if self.directory.attach_supported.get(self.directory.token(target)) is False:
            raise web.HTTPUpgradeRequired(text="Leader upgrade required for safe live attachment")
        if target != self.socket:
            token = self.directory.token(target)
            child = self.children.get(token)
            if child is None:
                child = Bridge(target, self.secret, self.version, directory=self.directory, root=self.root)
                self.children[token] = child
            return await child.websocket(request)
        try:
            reader, writer = await connect_leader(self.socket, self.version)
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            raise web.HTTPServiceUnavailable(text="Pinned Grok leader unavailable") from None
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_FRAME)
        await ws.prepare(request)
        pending: dict[str, tuple[dict[str, Any], asyncio.TimerHandle]] = {}
        loading: dict[str, str] = {}
        loading_workspaces: dict[str, str] = {}
        creating: set[str] = set()
        inflight: set[str] = set()
        initializing: set[str] = set()
        resident_only_supported = False
        attached: dict[str, str] = {}
        attached_workspaces: dict[str, str] = {}
        attachment_ended = False
        counter = 0
        background: set[asyncio.Task] = set()
        catalog_requests: dict[str, tuple[tuple, asyncio.Future, Path]] = {}
        selected_token = self.directory.token(self.socket)

        async def forward(value: dict[str, Any]) -> None:
            # A queued phone message must not cross a native lifecycle removal,
            # even if another client immediately restores the same persisted SID.
            if attachment_ended:
                return
            if (
                "method" in value
                and "id" in value
                and not str(value["id"]).startswith(INTERNAL_PREFIX)
            ):
                inflight.add(request_key(value))
            await write_frame(writer, {"type": "acp", "payload": json.dumps(value)})

        async def from_leader():
            nonlocal resident_only_supported, attachment_ended
            while True:
                envelope = await read_frame(reader)
                if envelope.get("type") in ("shutdown", "shutting_down"):
                    await ws.close(code=1012, message=b"Leader restarting; reconnect")
                    return
                if envelope.get("type") != "acp":
                    continue
                payload = envelope["payload"]
                if not isinstance(payload, str):
                    raise ValueError("Leader ACP payload must be JSON text")
                value = json.loads(payload, parse_constant=reject_nonfinite)
                if not isinstance(value, dict):
                    raise ValueError("Leader ACP payload must be an object")
                method = value.get("method", "")
                params = value.get("params", {})
                if isinstance(method, str) and method.startswith("_"):
                    method = method[1:]
                    if isinstance(params, dict) and isinstance(params.get("method"), str):
                        method, params = params["method"], params.get("params", {})
                if method == "x.ai/sessions/changed" and isinstance(params, dict):
                    self.directory.cached_at = 0
                    params["bridgeLeaderId"] = selected_token
                    for key in ("upserted", "sessions"):
                        if isinstance(params.get(key), list):
                            params[key] = [self.directory.enrich(row, selected_token)
                                for row in params[key] if isinstance(row, dict)]
                    payload = json.dumps(value)
                if method == "x.ai/sessions/changed" and isinstance(params, dict):
                    removed = params.get("removed", [])
                    targets = (
                        set(attached)
                        | set(loading.values())
                        | {request["params"].get("sessionId") for request, _ in pending.values()}
                    )
                    if isinstance(removed, list) and any(
                        sid in targets for sid in removed if isinstance(sid, str)
                    ):
                        attachment_ended = True
                        attached.clear()
                        # Preserve the authoritative lifecycle event for clients
                        # that handle it, and a dedicated code for transport-only
                        # clients. Neither may silently attach a replacement actor.
                        await ws.send_str(payload)
                        await ws.close(
                            code=4409, message=b"Live session ended; select a running session"
                        )
                        return
                is_response = "method" not in value and ("result" in value or "error" in value)
                catalog = catalog_requests.pop(str(value.get("id")), None) if is_response else None
                if catalog is not None:
                    cache_key, future, workspace = catalog
                    if self.directory.catalog_flights.get(cache_key) is future:
                        self.directory.catalog_flights.pop(cache_key, None)
                    if not future.done():
                        future.set_result(value)
                    if "error" not in value:
                        with suppress(OSError, RuntimeError):
                            if self.directory.catalog_key(self.socket, workspace) == cache_key:
                                if len(self.directory.catalogs) >= MAX_CATALOGS:
                                    oldest = min(self.directory.catalogs, key=lambda k: self.directory.catalogs[k][0])
                                    self.directory.catalogs.pop(oldest)
                                self.directory.catalogs[cache_key] = (asyncio.get_running_loop().time(), value)
                    continue
                if is_response:
                    inflight.discard(request_key(value))
                    if request_key(value) in initializing:
                        initializing.discard(request_key(value))
                        metadata = unwrap_result(value).get("_meta", {})
                        resident_only_supported = (
                            isinstance(metadata, dict)
                            and metadata.get("x.ai/residentOnlyAttach") is True
                            and metadata.get("x.ai/residentActorIdentity") is True
                        )
                        if isinstance(metadata, dict):
                            metadata.update({"x.ai/mobileLeaderDirectory": True,
                                             "x.ai/explicitSavedResume": True,
                                             "x.ai/idleSessionPreview": True,
                                             "x.ai/workspaceCommands": True,
                                             "x.ai/bridgeLeaderId": selected_token})
                            payload = json.dumps(value)
                preflight_entry = pending.pop(str(value.get("id")), None) if is_response else None
                if preflight_entry is not None:
                    preflight, timer = preflight_entry
                    timer.cancel()
                    rows = unwrap_result(value).get("sessions", [])
                    rows = rows if isinstance(rows, list) else []
                    params = preflight["params"]
                    row = next(
                        (
                            r
                            for r in rows
                            if isinstance(r, dict)
                            and r.get("sessionId") == params.get("sessionId")
                            and r.get("resident") is True
                        ),
                        None,
                    )
                    # Roster cwd may be a worktree's display/original path.
                    # The app resolves session/info's execution cwd; the native
                    # resident-only guard validates it against the exact actor.
                    if row is None:
                        inflight.discard(request_key(preflight))
                        await ws.send_json(
                            rpc_error(
                                preflight, "Select a live session with its exact absolute workspace"
                            )
                        )
                    else:
                        params.setdefault("_meta", {})["x.ai/restore_code"] = False
                        params["_meta"]["x.ai/resident_only"] = True
                        params["_meta"].pop("x.ai/leaderClientId", None)
                        loading[request_key(preflight)] = params["sessionId"]
                        loading_workspaces[request_key(preflight)] = params["cwd"]
                        await forward(preflight)
                    continue
                loaded_session = loading.pop(request_key(value), None) if is_response else None
                loaded_workspace = loading_workspaces.pop(request_key(value), None) if is_response else None
                if loaded_session and "error" not in value:
                    actor_id = unwrap_result(value).get("_meta", {}).get("x.ai/residentActorId")
                    if not isinstance(actor_id, str) or not actor_id:
                        raise ValueError("Leader omitted resident actor identity")
                    attached[loaded_session] = actor_id
                    attached_workspaces[loaded_session] = loaded_workspace
                    unwrap_result(value).setdefault("_meta", {})["x.ai/bridgeExecutionCwd"] = loaded_workspace
                    payload = json.dumps(value)
                # Late preflight replies are bridge-private too.
                if str(value.get("id", "")).startswith(INTERNAL_PREFIX):
                    continue
                await ws.send_str(payload)

        def spawn_background(coro):
            task = asyncio.create_task(coro)
            background.add(task)
            task.add_done_callback(background.discard)

        async def discover(value):
            try:
                result = await self.directory.snapshot()
                if not ws.closed:
                    await ws.send_json({"jsonrpc": "2.0", "id": value["id"], "result": {"result": result}})
            except (ConnectionError, RuntimeError):
                pass
            finally:
                inflight.discard(request_key(value))

        async def check_resume(value):
            snapshot = await self.directory.snapshot(fresh=True)
            sid = value["params"]["sessionId"]
            owners = [r for r in snapshot["sessions"] if r.get("sessionId") == sid and r.get("resident") is True]
            if owners:
                target = owners[0]
                return source_error(value, "origin_conflict", "This session is live; refresh and explicitly attach its running source",
                    bridgeLeaderId=target["bridgeLeaderId"], multipleOrigins=len(owners) > 1)
            unavailable = {s["bridgeLeaderId"] for s in snapshot["bridgeSources"] if not s["available"]}
            if snapshot["bridgeResumeBlocked"] or selected_token in unavailable or any(sid in self.directory.known_residents.get(token, set()) for token in unavailable):
                return source_error(value, "source_unavailable", "A known live source is unavailable; saved history will not replace it")
            saved = next((r for r in snapshot["sessions"] if r.get("sessionId") == sid), None)
            if saved is None:
                return source_error(value, "session_unavailable", "Saved session no longer exists; refresh sessions")
            try:
                workspace = Path(value["params"]["cwd"]).resolve(strict=True)
                expected = Path(saved.get("cwd", "")).resolve(strict=True)
                if workspace != expected or not workspace.is_dir() or not os.access(workspace, os.R_OK | os.X_OK):
                    raise ValueError()
            except (OSError, RuntimeError, ValueError):
                return source_error(value, "workspace_unavailable", "Saved session requires its existing absolute Mac workspace")
            return None

        async def preview_saved(value):
            """Read native persisted updates without loading or retaining an actor.

            Native session/updates has an id-scan fallback. Validate against the
            canonical roster and dashboard membership before exposing that read,
            so a supplied cwd cannot redirect a history read to a different row.
            """
            key = request_key(value)
            forwarded = False
            try:
                params = value["params"]
                try:
                    sid, cwd = params["sessionId"], params["cwd"]
                    if (params.keys() - {"sessionId", "cwd", "offset", "limit"}
                        or str(uuid.UUID(sid)) != sid
                        or not isinstance(cwd, str) or not Path(cwd).is_absolute()
                        or ".." in Path(cwd).parts
                        or type(params.get("offset", 0)) is not int
                        or not 0 <= params.get("offset", 0) <= 2**53 - 1
                        or type(params.get("limit", MAX_PREVIEW_PAGE)) is not int
                        or not 1 <= params.get("limit", MAX_PREVIEW_PAGE) <= MAX_PREVIEW_PAGE):
                        raise ValueError()
                except (KeyError, ValueError, TypeError, AttributeError):
                    await ws.send_json(rpc_error(value, "Preview requires a session UUID, its absolute workspace, and a bounded nonnegative page"))
                    return
                snapshot = await self.directory.snapshot(fresh=True)
                if snapshot.get("bridgeWorkspaceMembershipError"):
                    await ws.send_json(source_error(value, "workspace_membership_unavailable", "Open session membership is unavailable; refresh before previewing"))
                    return
                rows = [row for row in snapshot["sessions"] if row.get("sessionId") == sid]
                if not rows:
                    await ws.send_json(source_error(value, "session_unavailable", "Session no longer exists; refresh sessions"))
                    return
                if not any(row.get("workspaceMember") is True for row in rows):
                    await ws.send_json(source_error(value, "workspace_membership_required", "Select an open Mac session to preview its conversation"))
                    return
                try:
                    workspace = Path(cwd).resolve(strict=True)
                    if (not workspace.is_dir() or not os.access(workspace, os.R_OK | os.X_OK)
                        or not any(row.get("workspaceMember") is True
                            and os.path.normpath(cwd) == os.path.normpath(row.get("cwd", ""))
                            and workspace == Path(row["cwd"]).resolve(strict=True) for row in rows)):
                        raise ValueError()
                except (OSError, RuntimeError, ValueError):
                    await ws.send_json(source_error(value, "workspace_unavailable", "Preview requires the session's existing absolute Mac workspace"))
                    return
                # Explicit bounds only; client streaming/routing metadata is never
                # forwarded. The native primitive filters rewound history itself.
                params.update(offset=params.get("offset", 0), limit=params.get("limit", MAX_PREVIEW_PAGE))
                await forward(value)
                forwarded = True
            except (ConnectionError, RuntimeError):
                pass
            finally:
                if not forwarded:
                    inflight.discard(key)

        async def workspace_commands(value):
            """Use native pre-session discovery for a canonical open workspace."""
            nonlocal counter
            key = request_key(value)
            try:
                params = value["params"]
                cwd = params.get("cwd")
                if (params.keys() - {"cwd", "refresh"} or type(params.get("refresh", False)) is not bool
                    or not isinstance(cwd, str)
                    or not Path(cwd).is_absolute() or ".." in Path(cwd).parts):
                    await ws.send_json(rpc_error(value, "Workspace commands require one absolute open-session workspace"))
                    return
                snapshot = await self.directory.snapshot(fresh=True)
                if snapshot.get("bridgeWorkspaceMembershipError"):
                    await ws.send_json(source_error(value, "workspace_membership_unavailable", "Open session membership is unavailable; refresh before listing commands"))
                    return
                try:
                    workspace = Path(cwd).resolve(strict=True)
                    if (not workspace.is_dir() or not os.access(workspace, os.R_OK | os.X_OK)
                        or not any(row.get("workspaceMember") is True
                            and os.path.normpath(cwd) == os.path.normpath(row.get("cwd", ""))
                            and workspace == Path(row["cwd"]).resolve(strict=True)
                            for row in snapshot["sessions"])):
                        raise ValueError()
                except (OSError, RuntimeError, ValueError):
                    await ws.send_json(source_error(value, "workspace_unavailable", "Choose an existing open Mac session workspace to list its commands"))
                    return
                # Omitting sessionId selects native build_for_cwd discovery;
                # native folder trust, compatibility and skill policy still apply.
                cache_key = self.directory.catalog_key(self.socket, workspace)
                now = asyncio.get_running_loop().time()
                cached = self.directory.catalogs.get(cache_key)
                if not params.get("refresh", False) and cached and now - cached[0] < CATALOG_TTL:
                    await ws.send_json({**cached[1], "id": value["id"]})
                    return
                future = self.directory.catalog_flights.get(cache_key)
                if future is None:
                    if len(self.directory.catalog_flights) >= MAX_CATALOGS:
                        await ws.send_json(source_error(value, "catalog_busy", "Command discovery is busy; try again shortly"))
                        return
                    future = asyncio.get_running_loop().create_future()
                    self.directory.catalog_flights[cache_key] = future
                    counter += 1
                    native_id = f"{INTERNAL_PREFIX}catalog:{counter}"
                    catalog_requests[native_id] = (cache_key, future, workspace)
                    def expired_catalog(cache_key=cache_key, future=future, native_id=native_id):
                        catalog_requests.pop(native_id, None)
                        if self.directory.catalog_flights.get(cache_key) is future:
                            self.directory.catalog_flights.pop(cache_key, None)
                        if not future.done():
                            future.set_result(None)
                    timer = asyncio.get_running_loop().call_later(CATALOG_TIMEOUT, expired_catalog)
                    future.add_done_callback(lambda _, timer=timer: timer.cancel())
                    # Bridge-only refresh is consumed, never a native override.
                    await forward({**value, "id": native_id, "params": {"cwd": cwd}})
                response = await asyncio.wait_for(asyncio.shield(future), CATALOG_TIMEOUT)
                if response is None:
                    raise ConnectionError()
                await ws.send_json({**response, "id": value["id"]})
            except (ConnectionError, RuntimeError, OSError, asyncio.TimeoutError):
                if not ws.closed:
                    with suppress(ConnectionError, RuntimeError):
                        await ws.send_json(source_error(value, "catalog_unavailable", "Command discovery unavailable; reconnect or refresh"))
            finally:
                inflight.discard(key)

        async def resume_saved(value):
            key = request_key(value)
            try:
                # Serialize all bridge-owned resumes across origins. Recheck after
                # owner authentication as well as discovery so a stale picker can
                # never intentionally create a second known resident actor.
                async with self.directory.resume_lock:
                    rejection = await check_resume(value)
                    if rejection is not None:
                        if not ws.closed:
                            await ws.send_json(rejection)
                        return
                    if len(self.owners) >= MAX_OWNED_SESSIONS:
                        if not ws.closed:
                            await ws.send_json(rpc_error(value, "Mobile session limit reached; close a mobile-owned session on Mac"))
                        return
                    native = {**value, "params": {**value["params"], "_meta": {"x.ai/restore_code": False, "clientUserMessageEcho": True}}}
                    owner = SessionOwner(self, native, before_dispatch=lambda: check_resume(value))
                    self.owners.add(owner)
                    # This task survives phone loss, as does its owner. An unknown
                    # native outcome is never automatically retried.
                    response = await owner.result
                    if ws.closed:
                        return
                    if response is None:
                        await ws.close(code=1011, message=b"Session resume outcome uncertain; refresh sessions")
                        return
                    if "error" in response:
                        await ws.send_json(response)
                        return
                    actor = unwrap_result(response).get("_meta", {}).get("x.ai/residentActorId")
                    if not isinstance(actor, str) or not actor:
                        await ws.close(code=1011, message=b"Resumed actor identity unavailable")
                        return
                    # Replay must flow on the phone connection, rather than the
                    # retainer. Only this strict second load is returned to it.
                    params = value["params"]
                    params["_meta"] = {"x.ai/resident_only": True, "x.ai/residentActorId": actor,
                                       "x.ai/restore_code": False, "clientUserMessageEcho": True}
                    params["cwd"] = unwrap_result(response)["_meta"].get("x.ai/bridgeExecutionCwd", params["cwd"])
                    loading[key] = params["sessionId"]
                    loading_workspaces[key] = params["cwd"]
                    await forward(value)
            finally:
                creating.discard(key)
                # A forwarded strict attach remains in flight until native reply.
                if key not in loading:
                    inflight.discard(key)

        async def from_phone():
            nonlocal counter
            async for message in ws:
                if attachment_ended:
                    return
                if message.type != WSMsgType.TEXT:
                    if message.type == WSMsgType.BINARY:
                        await ws.close(code=1003, message=b"Expected JSON-RPC text")
                        return
                    if message.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        return
                    continue
                try:
                    value = json.loads(message.data, parse_constant=reject_nonfinite)
                    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                        raise ValueError()
                except (ValueError, TypeError):
                    await ws.close(code=1007, message=b"Expected JSON-RPC object")
                    return
                if not valid_rpc(value):
                    await ws.send_json(
                        rpc_error(
                            {
                                "id": value.get("id")
                                if type(value.get("id")) in (str, int)
                                else None
                            },
                            "Invalid JSON-RPC request or response",
                        )
                    )
                    continue
                if str(value.get("id", "")).startswith(INTERNAL_PREFIX):
                    await ws.send_json(rpc_error(value, "Reserved request identifier"))
                    continue
                if "method" in value and "id" in value:
                    if request_key(value) in inflight:
                        # An error with the reused ID could resolve the wrong waiter.
                        # Disconnect instead; the leader keeps already accepted work.
                        await ws.close(code=1008, message=b"Duplicate pending request identifier")
                        return
                    if len(inflight) >= MAX_PENDING_REQUESTS:
                        await ws.send_json(rpc_error(value, "Too many pending requests; retry"))
                        continue
                method = value.get("method", "")
                params = value.get("params", {})
                if not isinstance(params, dict):
                    await ws.send_json(rpc_error(value, "Request params must be an object"))
                    continue
                if "sessionId" in params and not isinstance(params["sessionId"], str):
                    await ws.send_json(rpc_error(value, "Session identity must be a string"))
                    continue
                if method and method not in READ_METHODS | SESSION_METHODS | {"session/load", "session/new"}:
                    await ws.send_json(
                        rpc_error(value, "Method is not available on the live-session bridge")
                    )
                    continue
                if method == "session/new":
                    if self.socket != self.directory.primary:
                        await ws.send_json(source_error(value, "primary_required", "Create new sessions on the configured primary Mac connection"))
                        continue
                    if not resident_only_supported:
                        await ws.send_json(rpc_error(
                            value, "Initialize the compatible live leader before creating a session"
                        ))
                        continue
                    if (
                        params.keys() - {"cwd", "mcpServers", "_meta"}
                        or params.get("mcpServers", []) != []
                        or params.get("_meta", {}) != {}
                    ):
                        await ws.send_json(rpc_error(
                            value, "New sessions must preserve Mac tools and permission settings"
                        ))
                        continue
                    try:
                        cwd = params.get("cwd")
                        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                            raise ValueError()
                        workspace = Path(cwd).resolve(strict=True)
                        if not workspace.is_dir() or not os.access(workspace, os.R_OK | os.X_OK):
                            raise ValueError()
                    except (OSError, RuntimeError, ValueError):
                        await ws.send_json(rpc_error(
                            value, "Choose an existing, accessible absolute Mac workspace directory"
                        ))
                        continue
                    if creating:
                        await ws.send_json(rpc_error(
                            value, "A session creation is already pending; wait or reconnect and select its result"
                        ))
                        continue
                    if len(self.owners) >= MAX_OWNED_SESSIONS:
                        await ws.send_json(rpc_error(
                            value, "Mobile session limit reached; close a created session from the Mac before creating another"
                        ))
                        continue
                    params["cwd"] = str(workspace)
                    creating.add(request_key(value))
                    inflight.add(request_key(value))
                    # A successful create is deliberately not an attachment proof.
                    # The client must strict-load its returned SID before control.
                    owner = SessionOwner(self, value)
                    self.owners.add(owner)

                    async def deliver_creation(owner=owner, key=request_key(value)):
                        try:
                            response = await owner.result
                            if not ws.closed:
                                with suppress(ConnectionError, RuntimeError):
                                    if response is None:
                                        # A normal RPC error means definite rejection to clients.
                                        # Losing the creator reply requires transport uncertainty
                                        # so clients reconcile discovery before another creation.
                                        await ws.close(code=1011, message=b"Session creation outcome uncertain")
                                    else:
                                        await ws.send_json(response)
                        finally:
                            creating.discard(key)
                            inflight.discard(key)

                    delivery = asyncio.create_task(deliver_creation())
                    self.creations.add(delivery)
                    delivery.add_done_callback(self.creations.discard)
                    continue
                if method == "_x.ai/commands/list":
                    sid = params.get("sessionId")
                    if "sessionId" not in params:
                        if "id" in value:
                            inflight.add(request_key(value))
                            spawn_background(workspace_commands(value))
                        continue
                    if (
                        params.keys() - {"sessionId", "cwd"}
                        or sid not in attached
                        or ("cwd" in params and params["cwd"] != attached_workspaces.get(sid))
                    ):
                        await ws.send_json(rpc_error(
                            value, "Read commands for an attached session in its actual workspace"
                        ))
                        continue
                if method == "initialize":
                    meta = params.get("_meta", {})
                    capabilities = params.get("clientCapabilities", {})
                    capability_meta = (
                        capabilities.get("_meta", {}) if isinstance(capabilities, dict) else None
                    )
                    if (
                        params.keys()
                        - {"protocolVersion", "clientCapabilities", "clientInfo", "_meta"}
                        or not isinstance(meta, dict)
                        or meta.keys() - INITIALIZE_META_KEYS
                        or not isinstance(capability_meta, dict)
                        or capability_meta.keys() - {"x.ai/userMessageEcho"}
                    ):
                        await ws.send_json(
                            rpc_error(
                                value, "Initialization cannot override live agent configuration"
                            )
                        )
                        continue
                if method == "authenticate" and (
                    params.keys() - {"methodId", "_meta"}
                    or params.get("_meta", {}) != {}
                    or not isinstance(params.get("methodId"), str)
                    or params.get("methodId") not in {"cached_token", "xai.api_key"}
                ):
                    await ws.send_json(
                        rpc_error(
                            value,
                            "Authenticate using existing Mac CLI credentials; phone credential changes are unavailable",
                        )
                    )
                    continue
                if method in EMPTY_PARAM_METHODS and params:
                    await ws.send_json(
                        rpc_error(value, "Discovery and billing reads accept empty parameters only")
                    )
                    continue
                if method in {"x.ai/sessions/list", ROSTER_METHOD}:
                    if "id" not in value:
                        continue
                    inflight.add(request_key(value))
                    spawn_background(discover(value))
                    continue
                if method == PREVIEW_METHOD:
                    if "id" not in value:
                        continue
                    inflight.add(request_key(value))
                    spawn_background(preview_saved(value))
                    continue
                if method in {"x.ai/session/info", "_x.ai/session/info"} and params.keys() - {
                    "sessionId"
                }:
                    await ws.send_json(
                        rpc_error(value, "Session info accepts a session identity only")
                    )
                    continue
                if method in SESSION_PARAM_KEYS:
                    if params.keys() - SESSION_PARAM_KEYS[method]:
                        await ws.send_json(rpc_error(value, "Unsupported live-session parameters"))
                        continue
                    if method != "session/load" and params.get("_meta", {}) != {}:
                        await ws.send_json(
                            rpc_error(
                                value,
                                "Mobile instructions cannot override live agent configuration",
                            )
                        )
                        continue
                if method == "session/load":
                    if not resident_only_supported:
                        await ws.send_json(
                            rpc_error(
                                value,
                                "This leader cannot guarantee live-only attachment; start the documented patched binary",
                            )
                        )
                        continue
                    if (
                        not isinstance(params, dict)
                        or not isinstance(params.get("cwd"), str)
                        or not Path(params["cwd"]).is_absolute()
                    ):
                        await ws.send_json(
                            rpc_error(value, "An absolute workspace path is required")
                        )
                        continue
                    meta = params.get("_meta", {})
                    if not isinstance(meta, dict) or meta.get("yoloMode") or meta.get("autoMode"):
                        await ws.send_json(
                            rpc_error(
                                value, "Mobile attachment must preserve interactive approvals"
                            )
                        )
                        continue
                    if meta.keys() - LOAD_META_KEYS or params.get("mcpServers", []) != []:
                        await ws.send_json(
                            rpc_error(
                                value, "Attachment cannot replace live agent configuration or tools"
                            )
                        )
                        continue
                    if len(pending) + len(loading) >= MAX_PENDING_LOADS:
                        await ws.send_json(rpc_error(value, "Too many attachment requests; retry"))
                        continue
                    if "x.ai/residentActorId" in meta and (
                        not isinstance(meta["x.ai/residentActorId"], str)
                        or not meta["x.ai/residentActorId"]
                    ):
                        await ws.send_json(
                            rpc_error(value, "Resident actor identity must be a string")
                        )
                        continue
                    if "x.ai/resumeSaved" in meta:
                        if meta["x.ai/resumeSaved"] is not True or "x.ai/residentActorId" in meta or "cursor" in meta:
                            await ws.send_json(rpc_error(value, "Explicit saved resume cannot reuse a live actor or replay cursor"))
                            continue
                        if self.socket != self.directory.primary:
                            await ws.send_json(source_error(value, "primary_required", "Resume saved sessions on the configured primary Mac connection"))
                            continue
                        if creating:
                            await ws.send_json(rpc_error(value, "A session creation or resume is already pending; reconcile before retrying"))
                            continue
                        creating.add(request_key(value))
                        inflight.add(request_key(value))
                        task = asyncio.create_task(resume_saved(value))
                        self.creations.add(task)
                        task.add_done_callback(self.creations.discard)
                        continue
                    counter += 1
                    request_id = f"{INTERNAL_PREFIX}{counter}"

                    def expired(identifier=request_id):
                        entry = pending.pop(identifier, None)
                        if entry is not None and not ws.closed:
                            inflight.discard(request_key(entry[0]))

                            async def reply_timeout():
                                try:
                                    await ws.send_json(
                                        rpc_error(
                                            entry[0], "Live session discovery timed out; reconnect"
                                        )
                                    )
                                except (ConnectionError, RuntimeError):
                                    pass

                            asyncio.create_task(reply_timeout())

                    timer = asyncio.get_running_loop().call_later(PREFLIGHT_TIMEOUT, expired)
                    pending[request_id] = (value, timer)
                    inflight.add(request_key(value))
                    await forward(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": ROSTER_METHOD,
                            # Native leader subscribes this connection before reading
                            # the roster, preventing last-viewer idle eviction.
                            "params": {"sessionId": params.get("sessionId")},
                        }
                    )
                    continue
                if method in SESSION_METHODS and params.get("sessionId") not in attached:
                    await ws.send_json(
                        rpc_error(value, "Attach a live session before controlling it")
                    )
                    continue
                # ACP responses to permission reverse-requests keep their original id.
                if method in SESSION_METHODS:
                    params["_meta"] = {"x.ai/residentActorId": attached[params["sessionId"]]}
                # Native leader handles first-answer-wins and broadcasts resolution.
                if method == "initialize" and "id" in value:
                    initializing.add(request_key(value))
                await forward(value)

        tasks = [asyncio.create_task(from_leader()), asyncio.create_task(from_phone())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (ConnectionError, asyncio.IncompleteReadError, ValueError, KeyError, TypeError):
            await ws.close(code=1011, message=b"Leader connection ended; reconnect")
        finally:
            for _, timer in pending.values():
                timer.cancel()
            for cache_key, future, _ in catalog_requests.values():
                if self.directory.catalog_flights.get(cache_key) is future:
                    self.directory.catalog_flights.pop(cache_key, None)
                if not future.done():
                    future.set_result(None)
            for task in tasks + list(background):
                task.cancel()
            await asyncio.gather(*tasks, *list(background), return_exceptions=True)
            # Detach only. Never emit session/cancel, session/close, or a signal.
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            await ws.close()
        return ws


def make_app(bridge: Bridge) -> web.Application:
    app = web.Application()
    app.on_cleanup.append(bridge.cleanup)
    app.router.add_get("/health", bridge.health)
    app.router.add_get("/ws", bridge.websocket)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leader-socket", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    parser.add_argument("--cli-version", default="1.0.45")
    parser.add_argument("--workspace-reader", type=Path,
                        default=default_workspace_reader(),
                        help="Pinned read-only native dashboard membership exporter")
    parser.add_argument("--init-secret", action="store_true")
    args = parser.parse_args()
    if args.init_secret:
        args.secret_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(
            os.open(args.secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
    secret = load_secret(args.secret_file)
    logging.basicConfig(level=logging.WARNING)
    web.run_app(
        make_app(Bridge(args.leader_socket, secret, args.cli_version,
                        workspace_reader=args.workspace_reader.expanduser().absolute())),
        host=args.host,
        port=args.port,
        access_log=None,
    )


if __name__ == "__main__":
    main()

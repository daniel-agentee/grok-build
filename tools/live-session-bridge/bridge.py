"""Authenticated ACP WebSocket transport for an existing Grok leader.

No agent, model connection, history store, or session is created by this bridge.
The leader remains responsible for execution, queuing, replay, and approvals.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import struct
from typing import Any

from aiohttp import WSMsgType, web

MAX_FRAME = 64 * 1024 * 1024
PROTOCOL = 1
PREFLIGHT_TIMEOUT = 15
MAX_PENDING_LOADS = 4
MAX_PENDING_REQUESTS = 128
ROSTER_METHOD = "_x.ai/sessions/list"
INTERNAL_PREFIX = "grok-mobile-preflight:"
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
}
SESSION_METHODS = {"session/prompt", "session/cancel", "session/set_model"}
INITIALIZE_META_KEYS = {"clientIdentifier", "clientUserMessageEcho"}
EMPTY_PARAM_METHODS = READ_METHODS - {
    "initialize",
    "authenticate",
    "x.ai/session/info",
    "_x.ai/session/info",
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


async def connect_leader(socket: Path, version: str, *, require_capability: bool = False):
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
        if require_capability:

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
                    if (
                        not isinstance(metadata, dict)
                        or metadata.get("x.ai/residentOnlyAttach") is not True
                        or metadata.get("x.ai/residentActorIdentity") is not True
                    ):
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
            and (value["method"] not in ("session/load", "session/prompt") or "id" in value)
        )
    return "id" in value and (("result" in value) != ("error" in value))


def unwrap_result(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result", {})
    # xAI ExtMethodResult nests its payload in a second `result`.
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        return result["result"]
    return result if isinstance(result, dict) else {}


class Bridge:
    def __init__(self, socket: Path, secret: str, version: str):
        self.socket, self.secret, self.version = socket, secret, version

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
        try:
            reader, writer = await connect_leader(self.socket, self.version)
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            raise web.HTTPServiceUnavailable(text="Pinned Grok leader unavailable") from None
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_FRAME)
        await ws.prepare(request)
        pending: dict[str, tuple[dict[str, Any], asyncio.TimerHandle]] = {}
        loading: dict[str, str] = {}
        inflight: set[str] = set()
        initializing: set[str] = set()
        resident_only_supported = False
        attached: dict[str, str] = {}
        attachment_ended = False
        counter = 0

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
                        await forward(preflight)
                    continue
                loaded_session = loading.pop(request_key(value), None) if is_response else None
                if loaded_session and "error" not in value:
                    actor_id = unwrap_result(value).get("_meta", {}).get("x.ai/residentActorId")
                    if not isinstance(actor_id, str) or not actor_id:
                        raise ValueError("Leader omitted resident actor identity")
                    attached[loaded_session] = actor_id
                # Late preflight replies are bridge-private too.
                if str(value.get("id", "")).startswith(INTERNAL_PREFIX):
                    continue
                await ws.send_str(payload)

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
                if method and method not in READ_METHODS | SESSION_METHODS | {"session/load"}:
                    await ws.send_json(
                        rpc_error(value, "Method is not available on the live-session bridge")
                    )
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
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Detach only. Never emit session/cancel, session/close, or a signal.
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            await ws.close()
        return ws


def make_app(bridge: Bridge) -> web.Application:
    app = web.Application()
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
        make_app(Bridge(args.leader_socket, secret, args.cli_version)),
        host=args.host,
        port=args.port,
        access_log=None,
    )


if __name__ == "__main__":
    main()

"""Stdlib-only trusted TLS/WebSocket probe; credentials arrive on stdin only."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import ssl
import struct
import sys
import time
from urllib.parse import urlsplit, urlencode

MAX_FRAME = 16 * 1024 * 1024

class ProbeFailure(Exception):
    pass

class WebSocket:
    def __init__(self, endpoint, credential, origin=None):
        parsed = urlsplit(endpoint)
        if parsed.scheme != 'wss' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ProbeFailure('invalid_secure_endpoint')
        if '\r' in credential or '\n' in credential:
            raise ProbeFailure('invalid_credential')
        port = parsed.port or 443
        raw = socket.create_connection((parsed.hostname, port), timeout=20)
        self.socket = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
        self.socket.settimeout(20)
        self.cert = self.socket.getpeercert()
        self.buffer = b''
        self.notifications = []
        self.next_id = 0
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        host = parsed.hostname if port == 443 else parsed.hostname + ':' + str(port)
        headers = ['GET ' + path + ' HTTP/1.1', 'Host: ' + host,
            'Upgrade: websocket', 'Connection: Upgrade', 'Sec-WebSocket-Version: 13',
            'Sec-WebSocket-Key: ' + key, 'Authorization: Bearer ' + credential, 'User-Agent: GrokBuildRemoteProbe/1.0']
        if origin:
            headers.append('Origin: ' + origin)
        self.socket.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode())
        response = b''
        while b'\r\n\r\n' not in response:
            chunk = self.socket.recv(4096)
            if not chunk or len(response) > 65536:
                self.socket.close()
                raise ProbeFailure('invalid_upgrade_response')
            response += chunk
        header, self.buffer = response.split(b'\r\n\r\n', 1)
        lines = header.decode('iso-8859-1').split('\r\n')
        self.status = int(lines[0].split()[1])
        fields = {line.split(':', 1)[0].strip().lower(): line.split(':', 1)[1].strip()
                  for line in lines[1:] if ':' in line}
        if self.status != 101:
            self.socket.close()
            raise ProbeFailure('upgrade_http_' + str(self.status))
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        if fields.get('sec-websocket-accept') != expected or fields.get('upgrade', '').lower() != 'websocket':
            self.socket.close()
            raise ProbeFailure('invalid_upgrade_accept')

    def exact(self, length):
        while len(self.buffer) < length:
            chunk = self.socket.recv(min(65536, length - len(self.buffer)))
            if not chunk:
                raise ProbeFailure('connection_closed')
            self.buffer += chunk
        result, self.buffer = self.buffer[:length], self.buffer[length:]
        return result

    def send(self, data, opcode=1):
        payload = json.dumps(data, separators=(',', ':')).encode() if opcode == 1 else data
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode, 0x80 | (size if size < 126 else 126 if size <= 65535 else 127)])
        if size >= 126:
            header += struct.pack('!H' if size <= 65535 else '!Q', size)
        encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + encoded)

    def receive(self):
        fragments = []
        while True:
            first, second = self.exact(2)
            opcode, size = first & 15, second & 127
            if first & 0x70 or second & 0x80:
                raise ProbeFailure('unsupported_server_frame')
            if size in (126, 127):
                size = struct.unpack('!H' if size == 126 else '!Q', self.exact(2 if size == 126 else 8))[0]
            if size > MAX_FRAME or sum(map(len, fragments)) + size > MAX_FRAME:
                raise ProbeFailure('frame_size_limit')
            payload = self.exact(size)
            if opcode == 9:
                self.send(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode == 8:
                raise ProbeFailure('websocket_closed')
            if opcode not in (0, 1):
                raise ProbeFailure('unexpected_frame_opcode')
            fragments.append(payload)
            if first & 0x80:
                return json.loads(b''.join(fragments))

    def rpc(self, method, params):
        self.next_id += 1
        identifier = self.next_id
        self.send({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self.socket.settimeout(max(0.1, deadline - time.monotonic()))
            value = self.receive()
            if value.get('id') == identifier and 'method' not in value:
                if 'error' in value:
                    raise ProbeFailure('rpc_error_' + str(value['error'].get('code', 'unknown')))
                return value.get('result', {})
            self.notifications.append(value)
        raise ProbeFailure('rpc_deadline')

    def close(self, abrupt=False):
        if not abrupt:
            try:
                self.send(struct.pack('!H', 1000), opcode=8)
            except OSError:
                pass
        self.socket.close()


def unwrap(value):
    while isinstance(value, dict) and isinstance(value.get('result'), dict):
        value = value['result']
    return value


def initialize(ws):
    result = ws.rpc('initialize', {'protocolVersion': 1, 'clientCapabilities': {
        '_meta': {'x.ai/userMessageEcho': True}, 'fs': {'readTextFile': False, 'writeTextFile': False}}})
    metadata = result.get('_meta', {})
    if metadata.get('x.ai/residentOnlyAttach') is not True or metadata.get('x.ai/residentActorIdentity') is not True:
        raise ProbeFailure('strict_live_capability_missing')
    ws.rpc('authenticate', {'methodId': 'cached_token', '_meta': {}})
    return metadata


def identity_summary(roster):
    rows = roster.get('sessions', [])
    identities = sorted([row.get('bridgeLeaderId'), row.get('sessionId'), row.get('cwd'), row.get('residentActorId')]
                        for row in rows)
    canonical = json.dumps(identities, separators=(',', ':'), sort_keys=True)
    return {'count': len(rows), 'resident_count': sum(row.get('resident') is True for row in rows),
            'workspace_member_count': sum(row.get('workspaceMember') is True for row in rows),
            'identity_sha256': hashlib.sha256(canonical.encode()).hexdigest()}


def event_ids(notifications, fixture):
    return {n.get('params', {}).get('_meta', {}).get('eventId') for n in notifications
            if n.get('params', {}).get('sessionId') == fixture['sessionId']
            and isinstance(n.get('params', {}).get('_meta', {}).get('eventId'), str)}


def conversation_fingerprint(notifications, fixture):
    kinds = {'user_message_chunk', 'agent_message_chunk', 'agent_thought_chunk',
             'tool_call', 'tool_call_update', 'plan', 'turn_completed'}
    selected = [{'eventId': n.get('params', {}).get('_meta', {}).get('eventId'), 'update': n.get('params', {}).get('update')}
                for n in notifications if n.get('params', {}).get('sessionId') == fixture['sessionId']
                and n.get('params', {}).get('update', {}).get('sessionUpdate') in kinds]
    return hashlib.sha256(json.dumps(selected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def notification_summary(notifications, fixture, marker=None):
    messages = [n for n in notifications if n.get('params', {}).get('sessionId') == fixture['sessionId']]
    identifiers = [n.get('params', {}).get('_meta', {}).get('eventId') for n in messages]
    identifiers = [value for value in identifiers if isinstance(value, str)]
    kinds = {}
    for value in messages:
        kind = value.get('params', {}).get('update', {}).get('sessionUpdate', value.get('method', 'unknown'))
        kinds[kind] = kinds.get(kind, 0) + 1
    canonical = json.dumps(messages, sort_keys=True, separators=(',', ':'))
    return {'message_count': len(messages), 'event_count': len(identifiers),
            'duplicate_event_ids': len(identifiers) - len(set(identifiers)),
            'event_ids_sha256': hashlib.sha256(json.dumps(identifiers).encode()).hexdigest(),
            'notification_sha256': hashlib.sha256(canonical.encode()).hexdigest(),
            'update_kinds': kinds, 'distinctive_marker_present': marker in canonical if marker else None}


def http_status(host, path, credential=None, origin=None):
    connection = http.client.HTTPSConnection(host, 443, timeout=20, context=ssl.create_default_context())
    headers = {'User-Agent': 'GrokBuildRemoteProbe/1.0'}
    if credential:
        headers['Authorization'] = 'Bearer ' + credential
    if origin:
        headers['Origin'] = origin
    try:
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        status = response.status
        redirected = response.getheader('Location') is not None
        response.read(4096)
        return {'status': status, 'redirected': redirected}
    finally:
        connection.close()


def run(configuration):
    endpoint, credential = configuration['endpoint'], configuration['credential']
    host = urlsplit(endpoint).hostname
    report = {'endpoint': endpoint, 'tls_validation': True, 'credential_delivery': 'stdin_memory_only',
              'redirects_followed': 0, 'prompts_submitted': 0, 'history_resumes': 0, 'session_creates': 0}
    report['paths'] = {path: http_status(host, path) for path in ['/', '/health', '/ws/']}
    report['missing_auth'] = http_status(host, '/ws')
    report['wrong_auth'] = http_status(host, '/ws', 'deliberately-incorrect-credential')
    report['browser_origin'] = http_status(host, '/ws', credential, 'https://example.invalid')
    for value in report['paths'].values():
        if value['status'] != 404 or value['redirected']:
            raise ProbeFailure('private_path_exposed_or_redirected')
    if report['missing_auth']['status'] != 401 or report['wrong_auth']['status'] != 401:
        raise ProbeFailure('credential_rejection_failed')
    if report['browser_origin']['status'] != 403:
        raise ProbeFailure('browser_origin_rejection_failed')
    first = WebSocket(endpoint, credential)
    try:
        initialize(first)
        roster = unwrap(first.rpc('_x.ai/sessions/list', {}))
        report['initial'] = identity_summary(roster)
        report['certificate_expires'] = first.cert.get('notAfter')
        report['strict_live_capability'] = True
        fixture = configuration.get('fixture')
        if fixture:
            matched = [r for r in roster.get('sessions', []) if r.get('sessionId') == fixture['sessionId']
                       and r.get('cwd') == fixture['cwd'] and r.get('residentActorId') == fixture['residentActorId']]
            if len(matched) != 1 or not matched[0].get('resident'):
                raise ProbeFailure('owned_fixture_not_resident')
            params = {'sessionId': fixture['sessionId'], 'cwd': fixture['cwd'], 'mcpServers': [],
                      '_meta': {'x.ai/resident_only': True, 'x.ai/residentActorId': fixture['residentActorId']}}
            loaded = first.rpc('session/load', params)
            if loaded.get('_meta', {}).get('x.ai/residentActorId') != fixture['residentActorId']:
                raise ProbeFailure('owned_fixture_actor_changed')
            report['owned_live_attach'] = True
            report['initial_replay'] = notification_summary(first.notifications, fixture, configuration.get('marker'))
    finally:
        first.close(abrupt=True)
    second = WebSocket(endpoint, credential)
    try:
        initialize(second)
        roster = unwrap(second.rpc('_x.ai/sessions/list', {}))
        report['reconnected'] = identity_summary(roster)
        report['roster_identity_unchanged'] = report['initial']['identity_sha256'] == report['reconnected']['identity_sha256']
        fixture = configuration.get('fixture')
        if fixture:
            params = {'sessionId': fixture['sessionId'], 'cwd': fixture['cwd'], 'mcpServers': [],
                      '_meta': {'x.ai/resident_only': True, 'x.ai/residentActorId': fixture['residentActorId']}}
            loaded = second.rpc('session/load', params)
            report['owned_actor_preserved_after_disconnect'] = loaded.get('_meta', {}).get('x.ai/residentActorId') == fixture['residentActorId']
            if not report['owned_actor_preserved_after_disconnect']:
                raise ProbeFailure('owned_fixture_reconnect_actor_changed')
            report['reconnect_replay'] = notification_summary(second.notifications, fixture, configuration.get('marker'))
            report['replay_event_ids_unchanged'] = report['initial_replay']['event_ids_sha256'] == report['reconnect_replay']['event_ids_sha256']
            report['replay_duplicate_ids'] = report['initial_replay']['duplicate_event_ids'] + report['reconnect_replay']['duplicate_event_ids']
            report['original_replay_events_preserved'] = event_ids(first.notifications, fixture) <= event_ids(second.notifications, fixture)
            report['conversation_replay_unchanged'] = conversation_fingerprint(first.notifications, fixture) == conversation_fingerprint(second.notifications, fixture)
            if not report['original_replay_events_preserved'] or report['replay_duplicate_ids']:
                raise ProbeFailure('replay_missing_or_duplicate_events')
    finally:
        second.close()
    report['success'] = True
    return report


if __name__ == '__main__':
    try:
        configuration = json.load(sys.stdin)
        print(json.dumps(run(configuration), sort_keys=True))
    except Exception as error:
        code = str(error) if isinstance(error, ProbeFailure) else type(error).__name__
        print(json.dumps({'success': False, 'code': code, 'credential_output': False}))
        raise SystemExit(1)

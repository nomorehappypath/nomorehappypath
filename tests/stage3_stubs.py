# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Stand-ins for Stage 3 tests: a stdio Codex app-server and a WebSocket Codex TUI.

The app-server stub speaks the JSON-RPC shapes measured from codex-cli 0.160.0
(`initialize`, `thread/start`, `turn/start` -> `turn/started`,
`item/started` userMessage with `clientId`, `turn/completed`). Behaviour is
switched by STUB_MODE: "normal", "no_receipt", "error", "approval" (sends one
server->client request before answering turn/start), "busy" (a turn, once
started, never ends by itself; `turn/steer` joins it - measured on 0.160.0:
the steered userMessage carries its clientId when the model next reads, and
an interrupted turn drops it). STUB_STEER picks what a steer does: "consume"
(used at once), "hold" (kept until `stub/consume`), "drop_first" (the first
steered message's turn ends before it is used; later ones are used).
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct

APP_SERVER = r'''
import json, os, sys
mode = os.environ.get("STUB_MODE", "normal")
log = os.environ.get("STUB_LOG")
turns = []
active = {"turn": "", "pending": [], "steers": 0}
steer_mode = os.environ.get("STUB_STEER", "consume")
def out(message):
    sys.stdout.write(json.dumps(message) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    message = json.loads(line)
    if log:
        with open(log, "a") as handle: handle.write(line)
    method, ident = message.get("method"), message.get("id")
    if method == "initialize":
        out({"id": ident, "result": {"userAgent": "stub"}})
    elif method == "thread/start":
        # The real TUI also starts a hidden helper thread (ephemeral) with its
        # own, looser sandbox; only the non-ephemeral one is the session's.
        ephemeral = bool((message.get("params") or {}).get("ephemeral"))
        thread = {"id": "th-helper" if ephemeral else "th-1", "ephemeral": ephemeral}
        policy = "on-request" if mode == "loose_policy" and not ephemeral else "never"
        roots = ["/private/tmp"] if ephemeral else ["/w2"]
        out({"id": ident, "result": {"thread": thread, "approvalPolicy": policy,
             "sandbox": {"type": "workspaceWrite", "writableRoots": roots, "networkAccess": not ephemeral}, "cwd": "/w"}})
        out({"method": "thread/started", "params": {"thread": thread}})
    elif method == "turn/start":
        if mode == "error":
            out({"id": ident, "error": {"code": -32000, "message": "turn already active"}}); continue
        if mode == "approval":
            out({"id": 900, "method": "item/commandExecution/requestApproval", "params": {"threadId": "th-1"}})
        turn = "turn-%d" % (len(turns) + 1)
        client = message["params"].get("clientUserMessageId")
        turns.append({"id": turn, "items": [{"type": "userMessage", "id": "u", "clientId": client, "content": []}]})
        out({"id": ident, "result": {"turn": {"id": turn, "items": [], "status": "inProgress"}}})
        out({"method": "turn/started", "params": {"threadId": "th-1", "turn": {"id": turn}}})
        if mode != "no_receipt":
            out({"method": "item/started", "params": {"threadId": "th-1", "turnId": turn, "startedAtMs": 1,
                 "item": {"type": "userMessage", "id": "u", "clientId": client, "content": []}}})
        if mode == "busy":
            active["turn"] = turn
            continue
        out({"method": "turn/completed", "params": {"threadId": "th-1", "turn": {"id": turn}}})
    elif method == "turn/steer":
        params = message.get("params") or {}
        if not active["turn"]:
            out({"id": ident, "error": {"code": -32600, "message": "no active turn to steer"}}); continue
        if params.get("expectedTurnId") != active["turn"]:
            out({"id": ident, "error": {"code": -32600, "message": "expected active turn id mismatch"}}); continue
        client = params.get("clientUserMessageId")
        active["steers"] += 1
        out({"id": ident, "result": {"turnId": active["turn"]}})
        if steer_mode == "drop_first" and active["steers"] == 1:
            ended, active["turn"] = active["turn"], ""
            out({"method": "turn/completed", "params": {"threadId": "th-1", "turn": {"id": ended}}})
        elif steer_mode == "hold":
            active["pending"].append(client)
        else:
            turns[-1]["items"].append({"type": "userMessage", "id": "s", "clientId": client, "content": []})
            out({"method": "item/started", "params": {"threadId": "th-1", "turnId": active["turn"], "startedAtMs": 1,
                 "item": {"type": "userMessage", "id": "s", "clientId": client, "content": []}}})
    elif method == "stub/consume":
        for client in active["pending"]:
            turns[-1]["items"].append({"type": "userMessage", "id": "s", "clientId": client, "content": []})
            out({"method": "item/started", "params": {"threadId": "th-1", "turnId": active["turn"], "startedAtMs": 1,
                 "item": {"type": "userMessage", "id": "s", "clientId": client, "content": []}}})
        active["pending"] = []
        out({"id": ident, "result": {}})
    elif method == "stub/interrupt":
        ended, active["turn"], active["pending"] = active["turn"], "", []
        out({"id": ident, "result": {}})
        out({"method": "turn/completed", "params": {"threadId": "th-1", "turn": {"id": ended}}})
    elif method == "thread/turns/list":
        out({"id": ident, "result": {"data": turns if mode != "no_receipt" else []}})
    elif ident is not None and method:
        out({"id": ident, "result": {}})
'''


class WebSocketClient:
    """A masked-frame client, the way the Codex TUI connects over --remote."""

    def __init__(self, path: str) -> None:
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(10)
        self.socket.connect(path)

    def handshake(self) -> bytes:
        key = base64.b64encode(os.urandom(16))
        self.socket.sendall(b"GET /rpc HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
                            b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + key + b"\r\n\r\n")
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = self.socket.recv(4096)
            if not chunk:
                break
            reply += chunk
        return reply

    def send(self, message: dict) -> None:
        payload = json.dumps(message).encode()
        mask = os.urandom(4)
        length = len(payload)
        header = bytes([0x81, 0x80 | length]) if length < 126 else bytes([0x81, 0x80 | 126]) + struct.pack(">H", length)
        self.socket.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.socket.recv(count - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def receive(self) -> dict:
        head = self._exact(2)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._exact(8))[0]
        return json.loads(self._exact(length))

    def receive_until(self, predicate, limit: int = 50) -> list[dict]:
        seen = []
        for _ in range(limit):
            message = self.receive()
            seen.append(message)
            if predicate(message):
                return seen
        raise AssertionError(f"never received the expected message: {seen}")

    def close(self) -> None:
        self.socket.close()

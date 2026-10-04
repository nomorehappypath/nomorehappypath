# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Plumbing Stage 3, Codex half: harness messages without typing.

Spec `docs/specs/PLUMBING_MODERNIZATION.md`, Stage 3. Today every harness
message is typed into the Codex terminal (a bracketed paste, then Enter once
the CLI has read it). Here the supervisor owns the session's `codex
app-server` on STDIO - no socket exists for any agent to reach (G-2) - and the
owner's normal Codex TUI attaches to it through `codex --remote`, via a small
multiplexer the supervisor serves on a Unix socket in the harness runtime
directory (spec §4.7).

Measured on codex-cli 0.160.0 (hermetic: scratch CODEX_HOME, no model turn):
`--remote unix://PATH` opens a WebSocket (`GET /rpc`, RFC 6455) on that socket
and speaks the same JSON-RPC the stdio server speaks, one message per text
frame; the TUI's first request is `initialize` with the string id
"initialize". So the multiplexer is a minimal WebSocket server that relays
frames to and from the stdio server, and keeps the two clients' request ids
apart: harness ids all start with "hn:", which the TUI never uses.

Relay rules (spec §Stage 3):
- the TUI's `initialize` is the server's `initialize`; the harness starts its
  own requests only after it;
- every server notification goes to the TUI and is observed by the harness;
- server -> client requests (approvals, elicitations) go to the TUI ONLY, so
  the owner answers them exactly as today;
- responses go to whoever asked.

Delivery: one `turn/start {threadId, input, clientUserMessageId}` per idle
point. Delivered only when BOTH the `turn/start` response returned a turn id
AND an `item/started|completed` for that turn carries a `userMessage` whose
`clientId` is the delivery id. Otherwise the message goes back to the queue,
never marked delivered.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import threading
import time
from typing import Any, Callable

WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HARNESS_ID_PREFIX = "hn:"
# Measured on codex-cli 0.160.0: a `--remote` TUI refuses permission overrides
# on its own command line ("... overrides are not supported with --remote.
# Configure ... on the server"; and, resuming a real thread, "Permission
# overrides are not supported when resuming a remote task"). So the session's
# permissions live on the app-server's command line only - where they reach
# every thread (measured: approval never, workspaceWrite, the writable roots,
# network) - and a TUI request may not override them: these parameters are
# removed from what the TUI sends, and the thread's reported policy is checked.
TUI_POLICY_PARAMS = {
    "thread/start": ("approvalPolicy", "approvalsReviewer", "sandbox"),
    "thread/resume": ("approvalPolicy", "approvalsReviewer", "sandbox"),
    "turn/start": ("approvalPolicy", "approvalsReviewer", "sandboxPolicy", "cwd"),
}
POLICY_CONFIG_PREFIXES = ("sandbox", "approval", "permissions", "default_permissions")


def pin_policy(message: dict[str, Any]) -> dict[str, Any]:
    """The TUI's request with every permission-bearing parameter removed."""
    removed = TUI_POLICY_PARAMS.get(str(message.get("method")))
    params = message.get("params")
    if not removed or not isinstance(params, dict):
        return message
    params = {key: value for key, value in params.items() if key not in removed}
    if isinstance(params.get("config"), dict):
        params["config"] = {key: value for key, value in params["config"].items()
                            if not str(key).startswith(POLICY_CONFIG_PREFIXES)}
    return {**message, "params": params}


def end_process_group(process: subprocess.Popen | None, grace: float = 2.0) -> None:
    """End a process started in its own session, and EVERYTHING in its group.

    `codex` is a node wrapper that starts the native binary as its child:
    killing the wrapper alone left the native TUI or app-server running under
    pid 1, ignoring SIGTERM (found 2026-10-03, orphans piling up). Every member
    of the group - the group id is the leader's pid, created by
    `start_new_session` - is signalled; the id cannot belong to anyone else
    while any member lives (setsid refuses an id still in use).
    """
    if process is None:
        return
    group = process.pid
    try:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    from harness import platform_support
    try:
        members = [pid for pid, row in platform_support.process_identity().process_table().items()
                   if row.get("pgid") == group]
    except OSError:
        members = []
    for pid in members:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _session_thread(thread: Any) -> bool:
    """The thread the owner sees and works in - not one of the TUI's helpers.

    Measured on codex-cli 0.160.0 (real `--remote` TUI, headless): besides
    its own thread ("startup-thread-start-…", ephemeral false) the TUI starts
    a hidden helper ("temporary-structured-…", ephemeral true) for its own
    structured calls. Adopting the last thread sent harness messages, and the
    resume id, to the helper.
    """
    return isinstance(thread, dict) and bool(thread.get("id")) and thread.get("ephemeral") is not True


def policy_mismatch(expected: dict[str, Any] | None, result: dict[str, Any]) -> str:
    """Why a thread's reported policy is not the session's; empty when it is."""
    if not expected:
        return ""
    if result.get("approvalPolicy") != expected.get("approvalPolicy", "never"):
        return f"approval policy {result.get('approvalPolicy')!r}"
    profile = expected.get("profile")
    if profile:
        active = (result.get("activePermissionProfile") or {}).get("id")
        return "" if active == profile else f"permission profile {active!r}, not {profile!r}"
    sandbox = result.get("sandbox") or {}
    if sandbox.get("type") != "workspaceWrite":
        return f"sandbox {sandbox.get('type')!r}"
    cwd = os.path.realpath(str(result.get("cwd") or ""))
    granted = {os.path.realpath(root) for root in sandbox.get("writableRoots") or []} | {cwd}
    missing = [root for root in expected.get("writableRoots") or [] if os.path.realpath(root) not in granted]
    if missing:
        return "writable roots missing: " + ", ".join(missing)
    if bool(sandbox.get("networkAccess")) != bool(expected.get("networkAccess", True)):
        return f"network access {sandbox.get('networkAccess')!r}"
    return ""
MAX_FRAME_BYTES = 64 * 1024 * 1024


class ProtocolError(RuntimeError):
    pass


class WebSocketConnection:
    """The server side of one RFC 6455 connection: text frames in and out."""

    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.send_lock = threading.Lock()
        self.closed = False

    def handshake(self, timeout: float = 10.0) -> None:
        self.connection.settimeout(timeout)
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = self.connection.recv(4096)
            if not chunk or len(request) > 65536:
                raise ProtocolError("no WebSocket upgrade request")
            request += chunk
        key = b""
        for line in request.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"sec-websocket-key":
                key = value.strip()
        if not request.startswith(b"GET ") or not key:
            raise ProtocolError("not a WebSocket upgrade request")
        accept = base64.b64encode(hashlib.sha1(key + WEBSOCKET_GUID).digest())
        self.connection.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        self.connection.settimeout(None)

    def _exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.connection.recv(count - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def receive(self) -> str | None:
        """The next complete text message; None when the peer closed."""
        message = b""
        while True:
            try:
                head = self._exact(2)
            except (EOFError, OSError):
                return None
            final, opcode, length = head[0] & 0x80, head[0] & 0x0F, head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._exact(8))[0]
            if length > MAX_FRAME_BYTES:
                raise ProtocolError("frame too large")
            mask = self._exact(4) if head[1] & 0x80 else b""
            payload = self._exact(length)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                self.close()
                return None
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x0):
                message += payload
                if final:
                    return message.decode("utf-8")
                continue
            raise ProtocolError(f"unsupported WebSocket opcode {opcode}")

    def _send(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 126]) + struct.pack(">H", length)
        else:
            header = bytes([0x80 | opcode, 127]) + struct.pack(">Q", length)
        with self.send_lock:
            self.connection.sendall(header + payload)

    def send_text(self, text: str) -> None:
        self._send(0x1, text.encode("utf-8"))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._send(0x8, b"")
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass


class CodexMultiplexer:
    """The supervisor's private bridge between one Codex TUI and its stdio app-server.

    `admit(pid)` decides whether a connecting process is the TUI this session
    launched; anything else is refused and reported through `on_event`.
    """

    def __init__(
        self, socket_path: str, app_server_argv: list[str], *, environment: dict[str, str] | None = None,
        cwd: str | None = None, admit: Callable[[int], bool], peer_pid: Callable[[socket.socket], int],
        on_event: Callable[[str, str], None] = lambda kind, detail: None,
        expected_policy: dict[str, Any] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.expected_policy = expected_policy
        self.app_server_argv = list(app_server_argv)
        self.environment = environment
        self.cwd = cwd
        self.admit = admit
        self.peer_pid = peer_pid
        self.on_event = on_event
        self.lock = threading.Condition()
        self.server: subprocess.Popen | None = None
        self.listener: socket.socket | None = None
        self.tui: WebSocketConnection | None = None
        self.tui_requests: dict[Any, str] = {}      # TUI request id -> method, to read its responses
        self.harness_responses: dict[str, dict[str, Any]] = {}
        self.next_id = 0
        self.initialized = False
        self.thread_id = ""
        self.active_turn = ""
        self.thread_busy = False
        self.observed: list[dict[str, Any]] = []     # userMessage clientIds seen: (turnId, clientId)
        # Steered into a running turn, not yet used: clientId -> turnId. Seen ->
        # receipt; its turn ended first -> it was never used (measured: an
        # interrupted turn drops pending steered input), so it is sent again.
        self.steered: dict[str, str] = {}
        self.steer_seen: list[str] = []
        self.steer_dropped: list[str] = []
        self.ended_turns: set[str] = set()
        self.policy: dict[str, Any] = {}
        self.stopped = False
        self._threads: list[threading.Thread] = []

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        # Its own process group, so stop() ends the node wrapper AND the native
        # binary it starts. HARNESS_MANAGED_SESSION in its environment still
        # lets the session sweep find it if the supervisor itself is killed.
        self.server = subprocess.Popen(
            self.app_server_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=self.environment, cwd=self.cwd, start_new_session=True,
        )
        directory = os.path.dirname(self.socket_path)
        if os.path.lexists(self.socket_path):
            os.unlink(self.socket_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o077)
        try:
            listener.bind(self.socket_path)
        finally:
            os.umask(previous)
        os.chmod(self.socket_path, 0o600)
        listener.listen(4)
        self.listener = listener
        if directory:
            os.chmod(directory, 0o700)
        for target in (self._accept_loop, self._server_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        with self.lock:
            self.stopped = True
            self.lock.notify_all()
        if self.tui is not None:
            self.tui.close()
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        server = self.server
        if server is not None:
            try:
                server.stdin.close()                   # the app-server ends on end of input
            except OSError:
                pass
            end_process_group(server, grace=3.0)
            try:
                server.stdout.close()
            except OSError:
                pass

    def alive(self) -> bool:
        return not self.stopped and self.server is not None and self.server.poll() is None

    # -- relay ---------------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self.stopped:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            try:
                pid = self.peer_pid(connection)
            except OSError:
                pid = 0
            with self.lock:
                busy = self.tui is not None and not self.tui.closed
            if busy or not pid or not self.admit(pid):
                self.on_event("refused", f"connection from pid {pid or 'unknown'} refused: not this session's Codex terminal")
                connection.close()
                continue
            peer = WebSocketConnection(connection)
            try:
                peer.handshake()
            except (ProtocolError, OSError) as error:
                self.on_event("refused", f"connection from pid {pid} refused: {error}")
                connection.close()
                continue
            with self.lock:
                self.tui = peer
            thread = threading.Thread(target=self._tui_loop, args=(peer,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _write_server(self, message: dict[str, Any]) -> None:
        server = self.server
        if server is None or server.poll() is not None:
            raise OSError("the Codex app-server is not running")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        with self.lock:
            server.stdin.write(data)
            server.stdin.flush()

    def _tui_loop(self, peer: WebSocketConnection) -> None:
        while True:
            try:
                text = peer.receive()
            except (ProtocolError, OSError, UnicodeDecodeError):
                text = None
            if text is None:
                with self.lock:
                    if self.tui is peer:
                        self.tui = None
                    self.lock.notify_all()
                return
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if isinstance(message, dict) and "method" in message and "id" in message:
                if str(message["id"]).startswith(HARNESS_ID_PREFIX):
                    continue                          # never let the TUI answer for the harness
                with self.lock:
                    self.tui_requests[message["id"]] = str(message["method"])
            if isinstance(message, dict) and "method" in message:
                message = pin_policy(message)
            try:
                self._write_server(message)
            except OSError:
                return

    def _server_loop(self) -> None:
        server = self.server
        for raw in server.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "id" in message and "method" not in message:
                identifier = message["id"]
                if isinstance(identifier, str) and identifier.startswith(HARNESS_ID_PREFIX):
                    with self.lock:
                        self.harness_responses[identifier] = message
                        self.lock.notify_all()
                    continue
                with self.lock:
                    method = self.tui_requests.pop(identifier, "")
                if method in ("thread/start", "thread/resume") and isinstance(message.get("result"), dict) \
                        and _session_thread(message["result"].get("thread")):
                    reason = policy_mismatch(self.expected_policy, message["result"])
                    if reason:
                        # Never hand the owner's terminal a thread whose sandbox
                        # is not this session's: it gets an error instead.
                        self._to_tui({"id": identifier, "error": {"code": -32000, "message":
                                      "the harness could not confirm this session's sandbox; the session stops"}})
                        self.on_event("policy_mismatch", reason)
                        continue
                self._observe_response(method, message)
            elif "method" in message and "id" not in message:
                self._observe_notification(message)
            self._to_tui(message)
        with self.lock:
            self.lock.notify_all()
        self.on_event("ended", "the Codex app-server exited")

    def _to_tui(self, message: dict[str, Any]) -> None:
        peer = self.tui
        if peer is None or peer.closed:
            return
        try:
            peer.send_text(json.dumps(message, separators=(",", ":")))
        except OSError:
            pass

    # -- observation ---------------------------------------------------------
    def _observe_response(self, method: str, message: dict[str, Any]) -> None:
        result = message.get("result")
        if not isinstance(result, dict):
            return
        with self.lock:
            if method == "initialize":
                self.initialized = True
            if method in ("thread/start", "thread/resume") and _session_thread(result.get("thread")):
                self.thread_id = str(result["thread"].get("id") or self.thread_id)
                self.policy = {key: result.get(key) for key in ("approvalPolicy", "sandbox", "cwd", "activePermissionProfile")}
                self.on_event("thread", self.thread_id)
            self.lock.notify_all()

    def _observe_notification(self, message: dict[str, Any]) -> None:
        method, params = message.get("method"), message.get("params") or {}
        with self.lock:
            if method == "thread/started" and _session_thread(params.get("thread")):
                if not self.thread_id:
                    self.thread_id = str(params["thread"].get("id") or "")
                    self.on_event("thread", self.thread_id)
            elif method == "turn/started" and params.get("threadId") == self.thread_id:
                self.active_turn = str((params.get("turn") or {}).get("id") or "")
                self.thread_busy = True
            elif method == "turn/completed" and params.get("threadId") == self.thread_id:
                ended = str((params.get("turn") or {}).get("id") or self.active_turn)
                self.ended_turns.add(ended)
                for client, turn in list(self.steered.items()):
                    if turn == ended:
                        self.steer_dropped.append(client)
                        del self.steered[client]
                self.active_turn = ""
                self.thread_busy = False
            elif method == "thread/status/changed" and params.get("threadId") == self.thread_id:
                self.thread_busy = (params.get("status") or {}).get("type") == "active"
            elif method in ("item/started", "item/completed"):
                item = params.get("item") or {}
                if item.get("type") == "userMessage" and item.get("clientId"):
                    client = str(item["clientId"])
                    self.observed.append({"turn": str(params.get("turnId") or ""), "client": client})
                    if self.steered.pop(client, None) is not None:
                        self.steer_seen.append(client)
            self.lock.notify_all()

    # -- the harness's own requests -------------------------------------------
    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self.lock:
            self.next_id += 1
            identifier = f"{HARNESS_ID_PREFIX}{self.next_id}"
        self._write_server({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self.lock:
            while identifier not in self.harness_responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.stopped or not self.alive():
                    raise TimeoutError(f"no answer to {method} within {timeout:.0f}s")
                self.lock.wait(min(remaining, 0.5))
            return self.harness_responses.pop(identifier)

    def up(self) -> bool:
        """The channel exists: the TUI initialized and has a thread (it may be busy)."""
        with self.lock:
            return self.alive() and self.initialized and bool(self.thread_id)

    def ready(self) -> bool:
        """A message can be given now: an idle thread starts a turn, a busy one is steered.

        Stage 3 review r3b: "idle only" held every message for a Codex agent
        that works in one long turn (it waits on the board inside its turn), so
        the release instructions never arrived. Typing reaches a busy Codex
        too: its Enter steers the running turn.
        """
        with self.lock:
            return self.alive() and self.initialized and bool(self.thread_id) and (not self.thread_busy or bool(self.active_turn))

    def take_receipts(self) -> tuple[list[str], list[str]]:
        """Steered messages since last asked: (used by the model, never overdue - the turn decides)."""
        with self.lock:
            seen, self.steer_seen = self.steer_seen, []
            return seen, []

    def take_dropped(self) -> list[str]:
        """Steered messages whose turn ended before they were used: never seen, safe to send again."""
        with self.lock:
            dropped, self.steer_dropped = self.steer_dropped, []
            return dropped

    def already_delivered(self, client_id: str, timeout: float) -> bool:
        """Spec: before any retry, look for a turn already carrying this delivery id."""
        with self.lock:
            if any(seen["client"] == client_id for seen in self.observed):
                return True
            thread = self.thread_id
        try:
            response = self.request("thread/turns/list", {"threadId": thread, "itemsView": "full", "limit": 20}, timeout)
        except (TimeoutError, OSError):
            return False
        for turn in (response.get("result") or {}).get("data") or []:
            for item in turn.get("items") or []:
                if item.get("type") == "userMessage" and item.get("clientId") == client_id:
                    return True
        return False

    def deliver(self, client_id: str, text: str, *, response_timeout: float, receipt_timeout: float) -> str:
        """Give one harness message to the thread.

        Idle: a new turn (turn/start). Busy: into the running turn (turn/steer),
        as the owner's Enter does. Returns delivered | posted | dropped |
        not_ready | refused | unconfirmed. "posted": steered and accepted, not
        yet used - its receipt (or its drop) is read from take_receipts /
        take_dropped and it is never sent again meanwhile.
        """
        if not self.ready():
            return "not_ready"
        message = [{"type": "text", "text": text, "text_elements": []}]
        with self.lock:
            thread, busy, turn = self.thread_id, self.thread_busy, self.active_turn
        if busy:
            try:
                response = self.request("turn/steer", {"threadId": thread, "expectedTurnId": turn,
                                                       "clientUserMessageId": client_id, "input": message}, response_timeout)
            except (TimeoutError, OSError):
                return "unconfirmed"
            if "error" in response:
                # The turn ended in between, or cannot be steered (a review or
                # compaction): nothing was taken - it waits for the next chance.
                return "not_ready"
            turn = str((response.get("result") or {}).get("turnId") or turn)
            with self.lock:
                if not any(seen["client"] == client_id for seen in self.observed):
                    if turn in self.ended_turns:
                        return "dropped"         # its turn ended before the steer was even recorded
                    self.steered[client_id] = turn
        else:
            try:
                response = self.request("turn/start", {"threadId": thread, "clientUserMessageId": client_id, "input": message},
                                        response_timeout)
            except (TimeoutError, OSError):
                return "unconfirmed"
            turn = ((response.get("result") or {}).get("turn") or {}).get("id")
            if "error" in response or not turn:
                return "refused"
        deadline = time.monotonic() + receipt_timeout
        with self.lock:
            while not any(seen["turn"] == turn and seen["client"] == client_id for seen in self.observed):
                if busy and client_id in self.steer_dropped:
                    self.steer_dropped.remove(client_id)
                    return "dropped"
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.alive():
                    if busy and client_id in self.steered:
                        return "posted"          # accepted into the turn; used when the model next reads
                    return "unconfirmed"
                self.lock.wait(min(remaining, 0.5))
            if client_id in self.steer_seen:
                self.steer_seen.remove(client_id)   # answered here; not reported again
        return "delivered"


class DeliveryWorker:
    """Runs deliveries off the supervisor's terminal loop, one at a time.

    The supervisor submits a queued controller message and polls for its
    outcome on later ticks, so a 30 s receipt wait never freezes the owner's
    terminal.
    """

    def __init__(self, multiplexer: CodexMultiplexer, *, response_timeout: float, receipt_timeout: float) -> None:
        self.multiplexer = multiplexer
        self.response_timeout = response_timeout
        self.receipt_timeout = receipt_timeout
        self.current: dict[str, Any] | None = None
        self.outcome: tuple[dict[str, Any], str] | None = None
        self.lock = threading.Lock()

    def busy(self) -> bool:
        with self.lock:
            return self.current is not None

    def submit(self, item: dict[str, Any], text: str, *, retry: bool = False) -> None:
        with self.lock:
            self.current = item
        threading.Thread(target=self._run, args=(item, text, retry), daemon=True).start()

    def _run(self, item: dict[str, Any], text: str, retry: bool) -> None:
        client = f"harness-{item['id']}"
        if retry and self.multiplexer.already_delivered(client, self.response_timeout):
            result = "delivered"
        else:
            result = self.multiplexer.deliver(client, text, response_timeout=self.response_timeout,
                                              receipt_timeout=self.receipt_timeout)
        with self.lock:
            self.outcome = (item, result)
            self.current = None

    def take_outcome(self) -> tuple[dict[str, Any], str] | None:
        with self.lock:
            outcome, self.outcome = self.outcome, None
            return outcome

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Plumbing Stage 3, Gemini half: harness messages into a running Antigravity turn, with a transcript receipt.

Spec board/tasks/2026-10-07_gemini-provider_SPEC.md M-7(a), measured in T0 (§13):

- The CLI's `PreInvocation` hook may answer `{"injectSteps": [{"userMessage": ...}]}`. The message joins the turn
  that is running, shows in the window as a `>` line, and lands in `transcript.jsonl` as `source: SYSTEM_SDK`.
- Nothing fires while the session is idle, and a side process cannot be seen by the owner. So an IDLE session is
  still woken by typing (the supervisor's ordinary path, with the owner-input hold unchanged), and BOTH paths end
  at the same receipt: the delivery id in the transcript.

Two ends, like the Claude inbox:

- `ask()` runs inside the CLI's hook (a child of the agent, inside its confinement). It connects to the session's
  socket in the harness runtime directory and asks for the next message. Any failure means "no message": the
  supervisor then types it, as it always has.
- `GeminiInbox` runs in the supervisor. It admits a connection only from this harness's own hook script, running
  as a descendant of THIS session's CLI, naming THIS session (an agent in another session, or the same one,
  that connects by hand is refused and recorded). A message is handed to exactly one hook call and never again;
  it is delivered only when its delivery id is seen in the transcript.
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

HOOK_SCRIPT = str(Path(__file__).resolve().parent / "hook_gate.py")
HOOK_SCRIPT_REAL = os.path.realpath(HOOK_SCRIPT)
DEADLINE_ENV = "HARNESS_HOOK_GATE_TIMEOUT"     # the owner-settable hook deadline (settings: hook_gate_timeout_seconds)
IDLE = "idle_at_prompt"


def delivery_marker(client_id: str) -> str:
    return f"(harness delivery {client_id})"


def consumed_entry(line: str, marker: str) -> bool:
    """Whether one `transcript.jsonl` line shows the CLI took the message carrying `marker` as a user turn.

    Hook-injected messages are recorded as `SYSTEM_SDK`; a message typed into an idle window as `USER_EXPLICIT`.
    The model's own text and tool output are never a receipt, so a model quoting the id proves nothing.
    """
    if marker not in line:
        return False
    try:
        entry = json.loads(line)
    except ValueError:
        return False
    return (isinstance(entry, dict) and entry.get("type") == "USER_INPUT"
            and entry.get("source") in {"SYSTEM_SDK", "USER_EXPLICIT"} and marker in str(entry.get("content", "")))


def is_gate_command(command: str) -> bool:
    """Whether a process command line is THIS harness's hook gate for Gemini, by the file it runs.

    Compared by real path: the install may be reached through a symlink, so the path in `hooks.json` and the one
    this module was imported from can differ as text while naming the same file.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    for index, token in enumerate(tokens):
        if os.path.basename(token) == "hook_gate.py" and os.path.realpath(token) == HOOK_SCRIPT_REAL:
            return tokens[index + 1:index + 2] == ["gemini"]
    return False


# -- inside the CLI's hook --------------------------------------------------------------------------

def ask(socket_path: str, payload: dict[str, Any], event: str, timeout: float | None = None) -> str:
    """The text of the harness message to inject into this invocation, or "" for none. Never raises usefully.

    The wait is the hook deadline from Settings (never a number written here): a supervisor that is busy for that
    long means "no message this time", and the supervisor types instead.
    """
    timeout = float(os.environ.get(DEADLINE_ENV, "5")) if timeout is None else timeout
    request = {"type": "hook", "event": event, "session": os.environ.get("HARNESS_MANAGED_SESSION", ""),
               "conversation": str(payload.get("conversationId") or ""),
               "transcript": str(payload.get("transcriptPath") or ""),
               "model": str(payload.get("modelName") or ""), "fully_idle": payload.get("fullyIdle") is True}
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(socket_path)
        connection.sendall((json.dumps(request) + "\n").encode("utf-8"))
        reply = json.loads(connection.makefile("rb").readline() or b"{}")
        return str(reply.get("inject") or "")
    finally:
        connection.close()


# -- in the supervisor ------------------------------------------------------------------------------

class GeminiInbox:
    """Duck-compatible with `codex_app_server.CodexMultiplexer` for `DeliveryWorker` (see `claude_inbox.ClaudeInbox`)."""

    def __init__(
        self, socket_path: str, *, session_id: str, is_cli: Callable[[int], bool], peer_pid: Callable[[socket.socket], int],
        command_of: Callable[[int], str], on_event: Callable[[str, str], None] = lambda kind, detail: None,
        turn_state: Callable[[], tuple[str, float]] = lambda: ("", 0.0), busy_wait: float = 900.0,
        transcript_hint: Callable[[], str] = lambda: "",
    ) -> None:
        self.transcript_hint = transcript_hint     # the recorded conversation's transcript, known before any hook has run
        self.socket_path = socket_path
        self.session_id = session_id
        self.is_cli = is_cli
        self.peer_pid = peer_pid
        self.command_of = command_of
        self.on_event = on_event
        self.turn_state = turn_state
        self.busy_wait = busy_wait
        self.lock = threading.Condition()
        self.listener: socket.socket | None = None
        self.stopped = False
        self.transcript = ""                      # known from the first hook call
        self.conversation = ""
        self.pending: dict[str, str] = {}         # client id -> text, waiting for the next hook call
        self.offered: set[str] = set()            # handed to a hook call: never handed again
        self.awaiting: dict[str, tuple[str, float]] = {}

    # -- lifecycle --
    def start(self) -> None:
        if os.path.lexists(self.socket_path):
            os.unlink(self.socket_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o077)
        try:
            listener.bind(self.socket_path)
        finally:
            os.umask(previous)
        os.chmod(self.socket_path, 0o600)
        listener.listen(8)
        self.listener = listener
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self) -> None:
        with self.lock:
            self.stopped = True
            self.lock.notify_all()
            listener = self.listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    def alive(self) -> bool:
        return not self.stopped

    def ready(self) -> bool:
        with self.lock:
            return not self.stopped and bool(self.transcript)

    up = ready

    def idle(self) -> bool:
        """The CLI said its last turn is over: nothing will fire a hook, so the supervisor types."""
        state, _at = self.turn_state()
        return state == IDLE

    # -- admission --
    def _accept_loop(self) -> None:
        while not self.stopped:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        try:
            try:
                pid = self.peer_pid(connection)
            except OSError:
                pid = 0
            if not pid or not is_gate_command(self.command_of(pid)) or not self.is_cli(pid):
                self.on_event("refused", f"delivery connection from pid {pid or 'unknown'} refused: not this session's hook")
                return
            connection.settimeout(5)
            try:
                request = json.loads(connection.makefile("rb").readline() or b"{}")
            except (ValueError, OSError):
                request = {}
            if request.get("type") != "hook" or request.get("session") != self.session_id:
                self.on_event("refused", f"delivery connection from pid {pid} refused: it does not name this session")
                return
            reply = self._answer(request)
            try:
                connection.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            except OSError:
                # The hook died or timed out before it heard the answer: the message was NOT used. It waits again,
                # first in line, instead of being counted as handed over and lost.
                self._take_back(str(reply.get("client_id") or ""), str(reply.get("inject") or ""))
        except OSError:
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def _answer(self, request: dict[str, Any]) -> dict[str, Any]:
        client_id = ""
        with self.lock:
            if request.get("transcript"):
                self.transcript = str(request["transcript"])
            conversation = str(request.get("conversation") or "")
            if conversation and conversation != self.conversation:
                self.conversation = conversation
                self.on_event("conversation", conversation)
            inject = ""
            if request.get("event") == "PreInvocation" and self.pending:
                client_id = next(iter(self.pending))
                inject = self.pending.pop(client_id)
                self.offered.add(client_id)
            self.lock.notify_all()
        return {"inject": inject, "client_id": client_id}

    def _take_back(self, client_id: str, text: str) -> None:
        if not client_id or not text:
            return
        with self.lock:
            self.offered.discard(client_id)
            self.pending = {client_id: text, **self.pending}
            self.lock.notify_all()

    # -- delivery --
    def _in_transcript(self, marker: str) -> bool:
        with self.lock:
            path = self.transcript or self.transcript_hint()
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return any(consumed_entry(line, marker) for line in handle)
        except OSError:
            return False

    def already_delivered(self, client_id: str, timeout: float) -> bool:
        return self._in_transcript(delivery_marker(client_id))

    def deliver(self, client_id: str, text: str, *, response_timeout: float, receipt_timeout: float) -> str:
        """Hand one message to the next hook call. delivered | posted | not_ready | unconfirmed.

        `not_ready` means nothing was sent (no hook seen yet, or the session is idle): the supervisor types it.
        `posted`: injected, receipt pending - never injected again.
        """
        if not self.ready() or self.idle():
            return "not_ready"
        marker = delivery_marker(client_id)
        if self._in_transcript(marker):
            return "delivered"
        with self.lock:
            self.pending[client_id] = f"{text}\n\n{marker}"
            started = time.monotonic()
            while client_id not in self.offered and not self.stopped:
                if self.idle() or time.monotonic() - started >= self.busy_wait:
                    # The turn ended (or never made another model call) before a hook asked: withdraw it.
                    self.pending.pop(client_id, None)
                    return "not_ready"
                self.lock.wait(0.5)
            if client_id not in self.offered:
                self.pending.pop(client_id, None)
                return "not_ready"
        posted_at, started = time.time(), time.monotonic()
        while time.monotonic() - started < receipt_timeout and not self.stopped:
            if self._in_transcript(marker):
                return "delivered"
            time.sleep(0.25)
        with self.lock:
            self.awaiting[client_id] = (marker, posted_at)
        return "posted"

    def expect_receipt(self, client_id: str) -> None:
        """A message TYPED into the idle window: its receipt is the same transcript entry."""
        with self.lock:
            self.awaiting[client_id] = (delivery_marker(client_id), time.time())

    def take_receipts(self) -> tuple[list[str], list[str]]:
        with self.lock:
            awaiting = dict(self.awaiting)
        seen, overdue = [], []
        for client_id, (marker, posted_at) in awaiting.items():
            if self._in_transcript(marker):
                seen.append(client_id)
            elif time.time() - posted_at >= self.busy_wait:
                overdue.append(client_id)
        with self.lock:
            for client_id in seen + overdue:
                self.awaiting.pop(client_id, None)
        return seen, overdue

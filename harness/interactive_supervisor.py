#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Keep a visible interactive CLI under local harness supervision.

The owner still types directly into the Terminal.  The supervisor only records
that real terminal input as owner direction and delivers explicitly labelled
controller messages (for example, a failed-review retry) back into the same
visible terminal.  It deliberately does not infer who changed a shared Git
worktree: the board's task-start gate is the authoritative owner-direction
enforcement point.
"""
from __future__ import annotations

# No `harness` import may appear above the sys.path bootstrap below: this file
# is exec'd by run_managed_agent.sh as `python3 -E harness/interactive_supervisor.py`
# from a foreign directory, where the package is not importable until the
# bootstrap runs. One early import here killed every agent launch on
# 2026-09-22. tests/test_scripts_launch_as_the_runner_does.py runs each
# launched script exactly that way.
import argparse
import array
import fcntl
import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import attention, board, child_process, contract, control, conversation
from harness import platform_support
from harness.terminal_titles import TitlePrefix
from harness.project_context import add_context_arguments, context_from_args


PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
PASTE_BUFFER = b"\0HARNESS_PASTE\0"
# 2026-09-25 defect #13: controller messages were typed into the terminal
# while the owner was mid-sentence, submitting half of what they had written.
# A queued message waits while unsent owner input exists, up to this long
# after the last keystroke (an abandoned half-line must not block forever).
OWNER_INPUT_HOLD_SECONDS = 120.0
# Backlog #2: no controller message within this many seconds of the owner's
# last real keystroke (Enter included), so a harness ping can never land in the
# same moment as the owner's own submission. Short on purpose: the 2026-09-26
# freeze needed both submissions in the same instant, so a short gap separates
# them, and a reviewed contract (tests/test_interactive_supervisor.py) delivers
# a controller retry within 4 s of the owner's line.
OWNER_QUIET_SECONDS = 2.5


# One complete terminal reply. Longer forms come first so a DCS or OSC reply is
# never taken as its two-byte introducer (which would leave its body behind as
# if the owner had typed it); OSC may end with BEL or ST.
_TERMINAL_REPLY = re.compile(
    rb"\x1bP[^\x1b]*\x1b\\|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]"
)


def _owner_input_pending(typed: bytes) -> bool:
    """Unsent owner text: bytes after the last submitted line, ignoring control replies."""
    if not typed:
        return False
    if typed.startswith(PASTE_BUFFER) or PASTE_START in typed:
        return True
    plain = _TERMINAL_REPLY.sub(b"", typed)
    return any(byte >= 0x20 and byte != 0x7F for byte in plain)


# CSI final bytes a terminal uses for its own REPORTS, never for a key:
# cursor position (R), device attributes (c), status (n), window (t), focus
# in/out (I/O), mode report (y). Everything else in CSI form is a key the owner
# pressed (arrows, Home/End, Delete, function keys, mouse, paste markers).
_REPORT_FINALS = frozenset(b"RcntIOy")


def _is_owner_key_sequence(sequence: bytes) -> bool:
    """A complete escape sequence the owner's keyboard or mouse produced."""
    if sequence.startswith((b"\x1bP", b"\x1b]")) or sequence == b"\x1b\\":
        return False                                   # DCS / OSC reports, stray ST
    if sequence.startswith(b"\x1b["):
        final, parameters = sequence[-1], sequence[2:-1]
        if final in _REPORT_FINALS or (final == ord("u") and parameters.startswith(b"?")):
            return False                               # a report, incl. keyboard-flags reply
        return True
    return True                                        # Alt+key and other two-byte keys


def _is_owner_keystroke(data: bytes) -> bool:
    """Owner activity in one complete read: keys, Enter, backspace or a paste — not a terminal reply."""
    return _OwnerKeyClassifier().feed(data, 0.0)


# The start of a reply whose remaining bytes have not been read yet.
_REPLY_PREFIX = re.compile(rb"\x1b(?:P[^\x1b]*\x1b?|\][^\x07\x1b]*\x1b?|\[[0-?]*[ -/]*)?")
# An unfinished escape sequence still unfinished after this long was the
# owner's keys (Escape, Alt+[ and the like): a terminal writes a reply in one
# burst and never pauses inside it. Until then nothing is typed into the
# terminal, because the bytes may be the owner's (backlog #2 review round 3).
# About one loop tick; a reply split slower than this is counted as the owner,
# which only delays a message — the safe side.
KEY_FRAGMENT_SECONDS = 0.1
# An unfinished "reply" longer than this is not a terminal reply.
MAX_REPLY_FRAGMENT = 512


class _OwnerKeyClassifier:
    """Tell the owner's keys from terminal replies across split reads.

    Backlog #2 review round 2: a cursor-position reply read in two pieces
    (``ESC[12;`` then ``40R``) was classified piece by piece, and each piece
    looked like typing. An unfinished sequence at the end of a read is carried
    into the next read and classified whole.

    Review round 3: an unfinished sequence the owner typed (``ESC[``) was kept
    as a possible reply for ever. It is now "undecided" (nothing is typed into
    the terminal meanwhile) and after KEY_FRAGMENT_SECONDS it counts,
    provisionally, as the owner's keys. If the next bytes then complete a
    terminal REPORT, the provisional keys are withdrawn (``withdrawn_at``), so
    a reply that merely arrived slowly cannot hold messages back either.
    """

    def __init__(self) -> None:
        self.fragment = b""
        self.fragment_at = 0.0
        self.provisional = b""        # bytes counted as keys on timeout, still extendable
        self.provisional_at = 0.0
        self.withdrawn_at: float | None = None

    def feed(self, data: bytes, now: float) -> bool:
        """True when this read holds at least one real owner keystroke."""
        if self.provisional:
            joined = self.provisional + data
            if _REPLY_PREFIX.fullmatch(joined) and len(joined) <= MAX_REPLY_FRAGMENT:
                self.provisional = joined           # still one unfinished sequence
                return False
            match = _TERMINAL_REPLY.match(joined)
            counted = len(self.provisional)
            self.provisional = b""
            if match and match.end() > counted and not _is_owner_key_sequence(match.group()):
                self.withdrawn_at = self.provisional_at   # it was a slow report, not keys
                return self._classify(joined[match.end():], now)
        return self._classify(data, now)

    def _classify(self, data: bytes, now: float) -> bool:
        buffer = self.fragment + data
        started = self.fragment_at if self.fragment else now
        self.fragment = b""
        owner = PASTE_START in buffer
        index = 0
        while index < len(buffer):
            if buffer[index] == 0x1B:
                if _REPLY_PREFIX.fullmatch(buffer, index) and len(buffer) - index <= MAX_REPLY_FRAGMENT:
                    self.fragment = buffer[index:]
                    self.fragment_at = started if index == 0 else now
                    break
                match = _TERMINAL_REPLY.match(buffer, index)
                if match:
                    owner = owner or _is_owner_key_sequence(match.group())
                    index = match.end()
                    continue
            owner = True
            index += 1
        return owner

    @property
    def undecided(self) -> bool:
        """An unfinished sequence is waiting: it may still be the owner's keys."""
        return bool(self.fragment)

    def unfinished_keys_at(self, now: float) -> float | None:
        """When an unfinished sequence has waited KEY_FRAGMENT_SECONDS, it counts
        (provisionally) as the owner's keys: the time they arrived."""
        if self.fragment and now - self.fragment_at >= KEY_FRAGMENT_SECONDS:
            self.provisional, self.provisional_at = self.fragment, self.fragment_at
            self.fragment = b""
            return self.provisional_at
        return None

    def take_withdrawal(self) -> float | None:
        """The arrival time of provisional keys that proved to be a report, once."""
        withdrawn, self.withdrawn_at = self.withdrawn_at, None
        return withdrawn


def _controller_delivery_allowed(typed: bytes, last_key_at: float, now_monotonic: float,
                                 last_activity_at: float = 0.0) -> bool:
    """Deliver a controller message only when the owner is not mid-input.

    Backlog #2 (2026-09-26 21:28): the moment the owner pressed Enter the line
    was no longer "unsent", and a queued message was typed in the same tick as
    the owner's question; two submissions at once froze the Codex terminal. A
    message now also waits until the owner has been quiet for
    OWNER_QUIET_SECONDS after their last real keystroke, Enter included.
    """
    if (now_monotonic - last_activity_at) < OWNER_QUIET_SECONDS:
        return False
    if not _owner_input_pending(typed):
        return True
    return (now_monotonic - last_key_at) >= OWNER_INPUT_HOLD_SECONDS


def _write(fd: int, data: bytes) -> None:
    while data:
        written = os.write(fd, data)
        data = data[written:]


def _input_bytes_waiting(fd: int) -> int:
    """Return bytes the child has not consumed from its terminal input."""
    pending = array.array("i", [0])
    fcntl.ioctl(fd, termios.FIONREAD, pending, True)
    return max(0, pending[0])


def _submit_controller_message(fd: int, source: str, text: str) -> None:
    """Type routed text, then send Enter as a distinct terminal input event.

    Codex and Claude must consume the complete bracketed paste before Enter is
    injected. PTY write boundaries are not read boundaries, so a fixed sleep
    cannot provide that guarantee under load. In raw/TUI mode, wait until the
    kernel reports that the child consumed the paste; in canonical mode the
    line discipline itself keeps Enter separate as the line terminator.
    """
    message = f"[SYSTEM CONTROL — {source}] {text}".encode("utf-8")
    # Newlines inside a routed direction are content, not separate submits.
    # Bracketed paste keeps multi-paragraph text together in Codex/Claude; the
    # explicit carriage return is sent only after the paste has ended.
    _write(fd, PASTE_START + message + PASTE_END)
    canonical = bool(termios.tcgetattr(fd)[3] & termios.ICANON)
    if not canonical:
        deadline = time.monotonic() + 5
        while _input_bytes_waiting(fd):
            if time.monotonic() >= deadline:
                # Never fail silently: the visible warning distinguishes a
                # genuinely non-reading CLI from an agent that ignored work.
                print("\r\nHARNESS | WARNING: CLI did not consume the routed message within 5 seconds; sending Enter as recovery.\r", flush=True)
                break
            time.sleep(0.005)
    _write(fd, b"\r")


def _utf8_sequence_end(data: bytes, index: int) -> int | None:
    """Return the end of a valid UTF-8 sequence, or retain an incomplete one."""
    value = data[index]
    length = 2 if 0xC2 <= value <= 0xDF else 3 if 0xE0 <= value <= 0xEF else 4 if 0xF0 <= value <= 0xF4 else 0
    if not length:
        return None
    end = index + length
    if end > len(data):
        return len(data)
    return end if all(0x80 <= byte <= 0xBF for byte in data[index + 1:end]) else None


def _string_control_end(data: bytes, index: int, allow_bel: bool) -> int | None:
    """Find the end of an OSC/DCS reply, retaining incomplete sequences."""
    end = index
    while end < len(data):
        if (allow_bel and data[end] == 0x07) or data[end] == 0x9C:
            return end + 1
        if data[end:end + 2] == b"\x1b\\":
            return end + 2
        end += 1
    return None


def _strip_terminal_replies(buffer: bytearray, preserve_paste_markers: bool = True) -> None:
    """Remove complete terminal protocol replies while retaining partial bytes.

    PTYs carry terminal-generated cursor, colour, device-attribute, and focus
    replies on the same input stream as owner keystrokes. Those bytes must
    still reach the child CLI, but they are not owner direction.
    """
    data = bytes(buffer)
    cleaned = bytearray()
    index = 0
    while index < len(data):
        value = data[index]
        utf8_end = _utf8_sequence_end(data, index)
        if utf8_end is not None:
            cleaned.extend(data[index:utf8_end])
            index = utf8_end
            continue
        if value == 0x9B:  # 8-bit C1 CSI
            end = index + 1
            while end < len(data) and not 0x40 <= data[end] <= 0x7E:
                end += 1
            if end >= len(data):
                cleaned.extend(data[index:])
                break
            index = end + 1
            continue
        if value in {0x90, 0x9D}:  # 8-bit C1 DCS or OSC
            end = _string_control_end(data, index + 1, allow_bel=value == 0x9D)
            if end is None:
                cleaned.extend(data[index:])
                break
            index = end
            continue
        if value == 0x8F:  # 8-bit C1 SS3
            if index + 1 >= len(data):
                cleaned.extend(data[index:])
                break
            index += 2
            continue
        if value == 0x9C:  # standalone C1 string terminator
            index += 1
            continue
        if value != 0x1B:
            if value >= 0x20 or value in {9, 10, 13}:
                cleaned.append(value)
            index += 1
            continue
        if index + 1 >= len(data):
            cleaned.extend(data[index:])
            break
        kind = data[index + 1]
        if kind == ord("["):
            end = index + 2
            while end < len(data) and not 0x40 <= data[end] <= 0x7E:
                end += 1
            if end >= len(data):
                cleaned.extend(data[index:])
                break
            sequence = data[index:end + 1]
            if preserve_paste_markers and sequence in {PASTE_START, PASTE_END}:
                cleaned.extend(sequence)
            index = end + 1
            continue
        if kind in {ord("]"), ord("P")}:
            end = _string_control_end(data, index + 2, allow_bel=kind == ord("]"))
            if end is None:
                cleaned.extend(data[index:])
                break
            index = end
            continue
        if kind == ord("O"):
            if index + 2 >= len(data):
                cleaned.extend(data[index:])
                break
            index += 3
            continue
        # Two-byte escape/control sequences are never owner-authored text.
        index += 2
    buffer[:] = cleaned


def _record_owner_direction(root: Path, session_id: str, text: str, transcript=None) -> None:
    """Store one real owner instruction, ignoring terminal paste control bytes."""
    payload = bytearray(text.encode("utf-8", errors="ignore"))
    _strip_terminal_replies(payload, preserve_paste_markers=False)
    text = contract.normalize_owner_direction(payload.decode("utf-8", errors="ignore"))
    if not text:
        return
    if transcript is not None:
        # Every owner line goes to the transcript, for every role. The board
        # below records direction for the Delivery session only.
        transcript.owner(text)
    try:
        board.record_owner_direction(root, session_id, text)
        print("\r\nHARNESS | owner direction recorded; Delivery may now begin its internal task.\r", flush=True)
    except ValueError:
        # Direction is only recorded for one pre-registered Delivery session.
        # Reviewer/CTO terminal input is never treated as owner task work.
        pass


def _record_owner_lines(root: Path, session_id: str, buffer: bytearray, transcript=None) -> None:
    """Capture ordinary lines and bracketed multi-line terminal pastes exactly.

    Modern Codex and Claude terminals enable bracketed paste mode.  Treating
    each pasted line as a separate owner instruction loses the directive (and
    can mistakenly save only the closing ``ESC[201~`` control marker).
    """
    while True:
        if not buffer.startswith(PASTE_BUFFER):
            _strip_terminal_replies(buffer)
        if buffer.startswith(PASTE_BUFFER):
            end = buffer.find(PASTE_END, len(PASTE_BUFFER))
            if end < 0:
                return
            payload = bytes(buffer[len(PASTE_BUFFER):end]).decode("utf-8", errors="ignore")
            del buffer[:end + len(PASTE_END)]
            _record_owner_direction(root, session_id, payload, transcript)
            continue
        start = buffer.find(PASTE_START)
        if start >= 0:
            # Any bytes before a bracketed paste have already been forwarded
            # to the CLI and are not part of this pasted owner directive.
            del buffer[:start + len(PASTE_START)]
            buffer[:0] = PASTE_BUFFER
            continue
        # A partial terminal escape sequence must wait for the rest of the
        # control marker rather than being saved as an instruction.
        if PASTE_START.startswith(bytes(buffer)) and buffer:
            return
        positions = [index for index, value in enumerate(buffer) if value in {10, 13}]
        if not positions:
            return
        boundary = positions[0]
        line = bytes(buffer[:boundary]).decode("utf-8", errors="ignore").strip()
        del buffer[: boundary + 1]
        if not line:
            continue
        _record_owner_direction(root, session_id, line, transcript)


def _copy_terminal_size(source_fd: int, target_fd: int) -> None:
    """Give the nested CLI the real Terminal dimensions before it draws."""
    try:
        size = fcntl.ioctl(source_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(target_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _terminal_rows(fd: int) -> int | None:
    """How many rows the real Terminal shows; what the prompt watch treats as the screen."""
    try:
        size = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
    except (OSError, ValueError):
        return None
    rows = int.from_bytes(size[:2], sys.byteorder)
    return rows or None


def _make_controlling_terminal() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _schedule_terminal_close(stdin_fd: int) -> None:
    """Dismiss THIS finished session's surface.

    A self-dismissal: the caller is the occupant, so the seam operation takes no
    session argument. The fd is how macOS names the surface the occupant sits
    in, not a session identity.
    """
    platform_support.terminal_host().dismiss_current_session(stdin_fd)


# 2026-10-01: after SIGKILL a Claude CLI sat in macOS state "?Es" (stuck in
# kernel exit) and an unbounded wait() held Stop all forever.
STOP_KILL_WAIT_SECONDS = 5.0
STUCK_EXIT_CODE = 137


def _stop_child_group(
    child: subprocess.Popen, grace_seconds: float = 1.0, kill_wait_seconds: float = STOP_KILL_WAIT_SECONDS,
) -> bool:
    """Stop an interactive CLI even when it ignores a normal termination.

    True once the child has exited; False when even SIGKILL left it unreaped
    after ``kill_wait_seconds`` (the OS finishes that on its own). The caller
    then carries on shutting down instead of waiting for ever.
    """
    if child.poll() is not None:
        return True
    # Through the ONE guard, not a copy of it: it refuses when the process has
    # exited and when the pid no longer leads its own group. Signalling a group
    # by a number whose owner has changed is what killed the test runner.
    identity = platform_support.process_identity()
    if not identity.terminate_group(child, signal.SIGTERM):
        return child.poll() is not None
    try:
        child.wait(timeout=grace_seconds)
        return True
    except subprocess.TimeoutExpired:
        identity.terminate_group(child, signal.SIGKILL)
    try:
        child.wait(timeout=kill_wait_seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def _descends_from(pid: int, ancestor: int, hops: int = 32) -> bool:
    """True when `pid` is `ancestor` or one of its descendants (bounded walk)."""
    if pid <= 0 or ancestor <= 0:
        return False
    try:
        table = platform_support.process_identity().process_table()
    except OSError:
        return False
    for _ in range(hops):
        if pid == ancestor:
            return True
        row = table.get(pid)
        if not row or row.get("ppid") in (None, 0, 1, pid):
            return False
        pid = int(row["ppid"])
    return False


def _stage3_tick(root: Path, session_id: str, agent_id: str, stage3: dict, controller_queue: list, transcript) -> bool:
    """One pass over Stage 3's events and delivery outcomes. Returns False once the
    Codex thread id is known (the rollout-file scan is then unnecessary)."""
    keep_scanning = True
    with stage3["event_lock"]:
        events, stage3["events"] = stage3["events"], []
    for kind, detail in events:
        if kind == "thread" and detail:
            try:
                control.record_cli_session(root, session_id, detail, "codex")
            except ValueError:
                pass
            transcript.note(f"codex session id recorded from the app-server: {detail}")
            keep_scanning = False
        elif kind == "refused":
            transcript.note(f"plumbing stage 3: {detail}")
            _plumbing_event(root, agent_id, "plumbing_channel_refused", detail)
        elif kind == "policy_mismatch":
            # The thread's sandbox is not this session's: the session stops
            # (its terminal never got the thread), says why, and the next
            # launch runs the way it always has.
            message = f"the Codex session's sandbox could not be confirmed ({detail}); it stops and relaunches without stage 3"
            transcript.note(f"PLUMBING FALLBACK stage3_codex_app_server {message}")
            _plumbing_event(root, agent_id, "plumbing_policy_mismatch", message)
            try:
                marker = stage3_refused_marker(root, session_id)
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(detail + "\n", encoding="utf-8")
            except OSError:
                pass
            stage3["fallback"] = True
            stage3["stop_requested"] = True
        elif kind == "relay":
            transcript.note(f"plumbing stage 3: {detail}")
        elif kind == "ended":
            transcript.note(f"plumbing stage 3: {detail}")
    mux = stage3["mux"]
    if not stage3["fallback"] and not mux.alive():
        # The channel went away: the session types, as today, from now on.
        stage3["fallback"] = True
        transcript.note(f"PLUMBING FALLBACK {stage3['stage']} the delivery channel closed; this session goes back to typing")
        _plumbing_event(root, agent_id, "plumbing_fallback", "the delivery channel closed")
    late = not mux.up() and time.monotonic() > stage3["ready_deadline"]
    if late and not stage3["late_noted"]:
        # Not up yet (no relay, no thread - a trust dialog can hold startup):
        # messages are typed meanwhile, never held for ever; once the channel
        # comes up it is used again.
        stage3["late_noted"] = True
        transcript.note(f"PLUMBING FALLBACK {stage3['stage']} the delivery channel is not up yet; typing meanwhile")
    stage3["typing_meanwhile"] = late
    outcome = stage3["worker"].take_outcome()
    if outcome is not None:
        item, result = outcome
        channel = "app-server" if stage3["stage"] == "stage3_codex_app_server" else "inbox"
        if result == "delivered":
            stage3["failures"] = 0
            control.acknowledge_instruction(root, session_id, item["id"])
            transcript.note(f"controller message {item['id']} delivered ({channel} receipt)")
        elif result == "posted":
            # In the CLI's own queue - never posted or typed again by this
            # session. "posted", not "delivered", until its receipt appears; if
            # the session ends first, the session that continues it takes it
            # over under the same id (control._inherit_cli_memory).
            control.mark_posted(root, session_id, item["id"], item)
            stage3.setdefault("posted", {})[item["id"]] = item
            if channel == "app-server":
                transcript.note(f"controller message {item['id']} joined the agent's running turn; it is used when the model next reads, never sent twice")
            else:
                transcript.note(f"controller message {item['id']} posted to the {channel}; its receipt is awaited, it is never posted again")
        elif result == "dropped":
            # Steered into a turn that ended before the model read it (an
            # interrupt drops pending steered input): provably unused, so it
            # waits again, first in line - checked once more before sending.
            item["_stage3_retry"] = True
            controller_queue.insert(0, item)
            transcript.note(f"controller message {item['id']} was not used before the agent's turn ended; it is sent again")
        else:
            # Never marked delivered without its receipt: it waits again, first in line.
            item["_stage3_retry"] = result == "unconfirmed"
            controller_queue.insert(0, item)
            if result != "not_ready":
                stage3["failures"] += 1
                transcript.note(f"controller message {item['id']} not confirmed ({result}); queued again")
            if stage3["failures"] >= STAGE3_FAILURES_BEFORE_FALLBACK and not stage3["fallback"]:
                stage3["fallback"] = True
                message = "two deliveries in a row had no receipt; this session goes back to typing"
                transcript.note(f"PLUMBING FALLBACK {stage3['stage']} receipt: {message}")
                _plumbing_event(root, agent_id, "plumbing_fallback", message)
    take_receipts = getattr(stage3["mux"], "take_receipts", None)
    if take_receipts is not None:
        seen, overdue = take_receipts()
        for client_id in seen:
            stage3["failures"] = 0
            instruction = client_id.removeprefix("harness-")
            stage3.get("posted", {}).pop(instruction, None)
            try:
                control.acknowledge_instruction(root, session_id, instruction)
            except ValueError:
                pass
            transcript.note(f"controller message {instruction} receipt seen in the session transcript")
        for client_id in overdue:
            # Posted, never seen: an event the CTO sees. It is NOT posted again;
            # repeated misses send NEW messages back to typing.
            stage3["failures"] += 1
            instruction = client_id.removeprefix("harness-")
            message = f"message {instruction} was posted to the inbox but no receipt appeared in the session transcript"
            transcript.note(f"plumbing stage 3: {message}")
            _plumbing_event(root, agent_id, "plumbing_receipt_missing", message)
            try:
                # The live CLI holds it (most likely consumed): acknowledged so
                # it is never sent again; the event says it was not confirmed.
                control.acknowledge_instruction(root, session_id, instruction)
            except ValueError:
                pass
            if stage3["failures"] >= STAGE3_FAILURES_BEFORE_FALLBACK and not stage3["fallback"]:
                stage3["fallback"] = True
                transcript.note(f"PLUMBING FALLBACK {stage3['stage']} receipt: two posted messages had no receipt; new messages are typed")
                _plumbing_event(root, agent_id, "plumbing_fallback", "two posted messages had no receipt; new messages are typed")
    take_dropped = getattr(stage3["mux"], "take_dropped", None)
    if take_dropped is not None:
        for client_id in take_dropped():
            # Steered, then the turn ended before the model read it: it never
            # arrived. The same message, same id, waits again first in line.
            instruction = client_id.removeprefix("harness-")
            item = stage3.get("posted", {}).pop(instruction, None)
            if item is not None:
                item["_stage3_retry"] = True
                controller_queue.insert(0, item)
                transcript.note(f"controller message {instruction} was not used before the agent's turn ended; it is sent again")
    return keep_scanning


def _turn_state(root: Path, session_id: str) -> tuple[str, float]:
    """The CLI's own last turn signal (Stage 1 hooks): state and when, read without the lock."""
    from datetime import datetime
    try:
        session = (control._read_state(root).get("sessions") or {}).get(session_id) or {}
        state, at = str(session.get("turn_state") or ""), str(session.get("turn_state_at") or "")
        return state, (datetime.fromisoformat(at).timestamp() if at else 0.0)
    except (OSError, ValueError):
        return "", 0.0


def _plumbing_event(root: Path, agent_id: str, kind: str, message: str) -> None:
    try:
        board.record_plumbing_event(root, agent_id, kind, message)
    except (ValueError, OSError):
        pass


def _remote_tui_command(command: list[str], socket_path: str) -> list[str]:
    """Today's Codex launch line, attached to the session's private app-server.

    `codex [OPTIONS] [PROMPT]` and `codex resume [OPTIONS] [SESSION] [PROMPT]`
    both take `--remote <ADDR>`; it goes right after the subcommand. Every
    `-c` override and `--model` is REMOVED: a remote TUI refuses permission
    overrides (measured, codex-cli 0.160.0 - the owner's first Stage 3 launch
    died on it), and all of them are on the app-server's own command line.
    """
    position = 2 if len(command) > 1 and command[1] == "resume" else 1
    kept: list[str] = []
    index = position
    while index < len(command):
        if command[index] in ("-c", "--config", "--model", "-m") and index + 1 < len(command):
            index += 2
            continue
        kept.append(command[index])
        index += 1
    return command[:position] + ["--remote", f"unix://{socket_path}"] + kept


def stage3_refused_marker(root: Path, session_id: str) -> Path:
    """Beside the session's transcript (harness storage, not agent-writable):
    the next launch of this session runs without Stage 3."""
    return conversation.transcript_path(root, session_id).parent / f"{session_id}.stage3-refused"


# Plumbing Stage 3 (spec PLUMBING_MODERNIZATION.md): after this many deliveries
# in a row without a receipt, the session goes back to typing, and says so.
STAGE3_FAILURES_BEFORE_FALLBACK = 2
# The longest a Stage 3 channel's teardown may hold a closing session.
STAGE3_TEARDOWN_SECONDS = 10.0


def run(
    root: Path, session_id: str, agent_id: str, command: list[str],
    *, close_terminal_on_exit: bool = False, provider: str = "", execution_root: str = "",
    codex_app_server: list[str] | None = None, runtime_directory: str = "", claude_inbox: bool = False,
    codex_expected_policy: dict | None = None, busy_wait: float = 900.0,
    receipt_timeout: float = 30.0, response_timeout: float = 20.0,
) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("interactive supervisor requires a real Terminal")
    stdin_fd, stdout_fd = sys.stdin.fileno(), sys.stdout.fileno()
    # The harness keeps its own record of this conversation, both directions,
    # so a relaunched agent (and the owner) can read what was said even when
    # the CLI's memory is gone. See harness/conversation.py.
    transcript = conversation.Transcript(conversation.transcript_path(root, session_id))
    transcript.note(f"supervisor started for {session_id} (agent {agent_id}, provider {provider or 'unknown'}): {' '.join(command[:1])}")
    try:
        reclaimed = control.reclaim_stranded(root, session_id)
    except (OSError, ValueError):
        reclaimed = 0
    if reclaimed:
        transcript.note(f"{reclaimed} controller message(s) the previous run of this terminal left in flight are queued again; "
                        "each is checked against the conversation before it is sent")
    launched_at = time.time()
    codex_id_pending, codex_marker = conversation.codex_discovery_state(root, session_id, provider)
    next_codex_probe = launched_at + 2.0
    # A terminal that stops to ask the owner something halts all progress
    # until a person looks at it. The watch recognises that in the output
    # and the session record carries it, so Mission Control can shout.
    watch = attention.PromptWatch(height=_terminal_rows(stdin_fd))

    input_held = False
    controller_queue: list[dict] = []

    def note_attention(change: tuple[str, str | None] | None) -> None:
        nonlocal input_held
        if not change:
            return
        kind, reason = change
        # Any menu the owner must answer holds harness input (sign-in since
        # backlog #8; folder trust and permission menus since F-8).
        input_held = kind == "waiting" and attention.holds_harness_input(reason)
        if input_held and controller_queue:
            # Backlog #8, review round 1: give back what was taken but not
            # typed, so the board can withdraw it if the work moves elsewhere.
            try:
                control.return_instructions(root, session_id, controller_queue)
            except ValueError:
                pass
            controller_queue.clear()
        try:
            if kind == "waiting":
                control.record_attention(root, session_id, reason or "is waiting for you")
                transcript.note(f"terminal is waiting for the owner: {reason}")
            else:
                control.clear_attention(root, session_id)
                transcript.note("terminal no longer waiting for the owner")
        except ValueError:
            pass
    master, slave = pty.openpty()
    _copy_terminal_size(stdin_fd, slave)
    child_environment = child_process.environment(git=True, shell=True)
    # PLUMBING STAGE 3, Codex: the session's app-server runs on stdio as this
    # supervisor's child, and the TUI in the window attaches through a private
    # multiplexer. Started BEFORE the TUI, which connects at once.
    stage3: dict = {"mux": None, "worker": None, "child_pid": 0, "failures": 0, "fallback": False,
                    "events": [], "policy_checked": False, "stage": "", "late_noted": False,
                    "typing_meanwhile": False, "ready_deadline": time.monotonic() + response_timeout}
    if codex_app_server and runtime_directory:
        from harness import codex_app_server as _codex_app_server, runtime_dir
        import threading as _threading
        event_lock = _threading.Lock()

        def stage3_event(kind: str, detail: str) -> None:
            with event_lock:
                stage3["events"].append((kind, detail))

        stage3["stage"] = "stage3_codex_app_server"
        try:
            runtime_dir.create(Path(runtime_directory))
            mux = _codex_app_server.CodexMultiplexer(
                os.path.join(runtime_directory, "t.sock"), codex_app_server,
                environment=child_environment, cwd=execution_root or None,
                admit=lambda pid: _descends_from(pid, stage3["child_pid"]),
                peer_pid=platform_support.process_identity().peer_process_id,
                on_event=stage3_event, expected_policy=codex_expected_policy,
            )
            mux.start()
            stage3["mux"] = mux
            stage3["worker"] = _codex_app_server.DeliveryWorker(
                mux, response_timeout=response_timeout, receipt_timeout=receipt_timeout)
            stage3["event_lock"] = event_lock
            # Today's exact launch line, attached to the private app-server.
            command = _remote_tui_command(command, mux.socket_path)
            transcript.note("plumbing stage 3: harness messages reach Codex through its app-server, not by typing")
        except (OSError, ValueError) as error:
            transcript.note(f"PLUMBING FALLBACK stage3_codex_app_server could not start the app-server: {error}")
            stage3["fallback"] = True
    elif claude_inbox and runtime_directory:
        # PLUMBING STAGE 3, Claude: the session's own-child relay (started by
        # its SessionStart hook) connects here and posts each message to the
        # CLI's inbox; the supervisor never posts itself (it is the CLI's
        # parent, and the CLI holds a parent's message for approval).
        from harness import claude_inbox as _claude_inbox, codex_app_server as _codex_app_server, runtime_dir
        import threading as _threading
        event_lock = _threading.Lock()
        stage3["stage"] = "stage3_claude_socket_delivery"

        def stage3_event(kind: str, detail: str) -> None:
            with event_lock:
                stage3["events"].append((kind, detail))

        def command_of(pid: int) -> str:
            try:
                return str((platform_support.process_identity().process_table().get(pid) or {}).get("command", ""))
            except OSError:
                return ""

        try:
            runtime_dir.create(Path(runtime_directory))
            inbox = _claude_inbox.ClaudeInbox(
                os.path.join(runtime_directory, "h.sock"), session_id=session_id,
                cli_pid=lambda: stage3["child_pid"], is_cli=lambda pid: _descends_from(pid, stage3["child_pid"]),
                peer_pid=platform_support.process_identity().peer_process_id, command_of=command_of,
                on_event=stage3_event, turn_state=lambda: _turn_state(root, session_id), busy_wait=busy_wait,
            )
            inbox.start()
            child_environment["HARNESS_INBOX_HANDOVER"] = inbox.socket_path
            stage3["mux"] = inbox
            stage3["worker"] = _codex_app_server.DeliveryWorker(
                inbox, response_timeout=response_timeout, receipt_timeout=receipt_timeout)
            stage3["event_lock"] = event_lock
            transcript.note("plumbing stage 3: harness messages reach Claude through its own inbox, not by typing")
        except (OSError, ValueError) as error:
            transcript.note(f"PLUMBING FALLBACK stage3_claude_socket_delivery could not start the inbox handover: {error}")
            stage3["fallback"] = True
    child = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        preexec_fn=_make_controlling_terminal,
        env=child_environment,
    )
    stage3["child_pid"] = child.pid
    os.close(slave)
    original = termios.tcgetattr(stdin_fd)
    typed = bytearray()
    # Last REAL owner keystroke (typed text, Enter, backspace, paste). Terminal
    # replies never move it: they are not the owner, and letting them restart
    # the clock would hold a queued message for ever behind a half-typed line.
    # "Never": time.monotonic() can start near zero in a new process, so 0.0
    # would read as a keystroke at launch and hold every message for the first
    # OWNER_QUIET_SECONDS of a fresh terminal.
    last_owner_key_at = float("-inf")
    owner_keys = _OwnerKeyClassifier()
    clock_before_unfinished = last_owner_key_at
    pending_owner_input = bytearray()
    titles = TitlePrefix(session_id)
    child_output_seen = False
    stop_requested = False

    def request_stop(_signal, _frame):
        nonlocal stop_requested
        stop_requested = True

    def resize(_signal, _frame):
        _copy_terminal_size(stdin_fd, master)
        note_attention(watch.resize(_terminal_rows(stdin_fd)))

    def owner_input(data: bytes) -> None:
        """Everything the owner's terminal sent: forward it, record it, time it."""
        nonlocal last_owner_key_at
        typed.extend(data)
        owner = owner_keys.feed(data, time.monotonic())
        withdrawn = owner_keys.take_withdrawal()
        if withdrawn is not None and last_owner_key_at == withdrawn:
            # The provisional keys were a slow terminal report after all.
            last_owner_key_at = clock_before_unfinished
        if owner:
            # Only the owner's own keys count: a terminal reply to the CLI's
            # cursor/device query must neither restart the hold clock nor
            # clear a "needs you" / sign-in alert.
            last_owner_key_at = time.monotonic()
            note_attention(watch.owner_typed())
        _record_owner_lines(root, session_id, typed, transcript)
        if child_output_seen:
            _write(master, data)
        else:
            # Some CLIs call tcsetattr(TCSAFLUSH) while starting. Input written
            # before their first output can be discarded, so retain exact owner
            # bytes until startup is visibly ready.
            pending_owner_input.extend(data)

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    previous_winch = signal.signal(signal.SIGWINCH, resize)
    try:
        tty.setraw(stdin_fd)
        print("HARNESS | interactive supervisor ready; terminal input remains yours and is visible.", flush=True)
        while child.poll() is None:
            if stop_requested:
                if not _stop_child_group(child):
                    transcript.note(
                        "the agent's process did not finish exiting after it was stopped; "
                        "the operating system will clear it. The terminal is closing anyway."
                    )
                break
            readable, _, _ = select.select([master, stdin_fd], [], [], .1)
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if data:
                    child_output_seen = True
                    _write(stdout_fd, titles.feed(data))
                    transcript.agent_bytes(data)
                    if pending_owner_input:
                        _write(master, bytes(pending_owner_input))
                        pending_owner_input.clear()
                    try:
                        control.record_output(root, session_id, len(data))
                    except ValueError:
                        pass
                    note_attention(watch.feed(data))
            if stdin_fd in readable:
                data = os.read(stdin_fd, 4096)
                if not data:
                    break
                owner_input(data)
            if codex_id_pending and time.time() >= next_codex_probe:
                # Codex mints its own session id and writes it to a rollout file
                # shortly after starting; record it so a relaunch can resume.
                next_codex_probe = time.time() + 2.0
                found = conversation.discover_codex_session_id(launched_at, Path(execution_root or os.getcwd()), codex_marker)
                if found:
                    control.record_cli_session(root, session_id, found, "codex")
                    transcript.note(f"codex session id recorded: {found}")
                    codex_id_pending = False
                elif time.time() - launched_at > 120:
                    transcript.note("codex session id not found within 120s; a relaunch will start fresh")
                    codex_id_pending = False
            if not input_held:
                # A terminal showing a menu the owner must answer (sign-in,
                # folder trust, permission) takes nothing: its messages stay
                # queued where the board can still withdraw them (backlog #8, F-8).
                taken = control.take_instructions(root, session_id)
                for entry in taken:
                    if entry.get("carried_from"):
                        # From a session that ended: it may have arrived there
                        # already, so the conversation is checked before sending.
                        entry["_stage3_retry"] = True
                controller_queue.extend(taken)
            # A supervisor-ready banner only proves the wrapper started. Wait
            # for the child CLI's first output so a slow-starting CLI cannot
            # receive controller input before it has configured its terminal.
            unfinished_at = owner_keys.unfinished_keys_at(time.monotonic())
            if unfinished_at is not None:
                # An escape sequence nothing completed: provisionally the
                # owner's keys. Only the hold clock moves; an alert is cleared
                # by definite keys only, since these may yet prove a report.
                clock_before_unfinished = last_owner_key_at
                last_owner_key_at = max(last_owner_key_at, unfinished_at)
            if child_output_seen and controller_queue and not input_held:
                # Last look at the keyboard before typing: owner bytes that
                # arrived after this tick's select are read and counted first,
                # so a message never lands on keys already waiting (backlog #2
                # round 3: the message was typed over a line already sent).
                if select.select([stdin_fd], [], [], 0)[0]:
                    data = os.read(stdin_fd, 4096)
                    if not data:
                        break
                    owner_input(data)
            if stage3["mux"] is not None:
                codex_id_pending = _stage3_tick(root, session_id, agent_id, stage3, controller_queue, transcript) and codex_id_pending
                if stage3.get("stop_requested"):
                    stop_requested = True
            stage3_delivers = stage3["mux"] is not None and not stage3["fallback"] and not stage3["typing_meanwhile"]
            if stage3_delivers and stage3["worker"].busy():
                pass                              # one delivery in flight at a time
            elif child_output_seen and controller_queue and not input_held and not owner_keys.undecided and _controller_delivery_allowed(
                bytes(typed), last_owner_key_at, time.monotonic(), last_owner_key_at,
            ):
                if stage3_delivers:
                    if stage3["mux"].ready():
                        # Stage 3: a turn on the app-server, acknowledged by the
                        # server - never typed. Its outcome is read next tick.
                        item = controller_queue.pop(0)
                        retry = bool(item.pop("_stage3_retry", False))
                        channel = "app-server" if stage3["stage"] == "stage3_codex_app_server" else "inbox"
                        transcript.note(f"controller message ({item['source']}) via {channel}: {item['text']}")
                        stage3["worker"].submit(item, f"[SYSTEM CONTROL — {item['source']}] {item['text']}", retry=retry)
                else:
                    item = controller_queue.pop(0)
                    if item.pop("_stage3_retry", False) and stage3["mux"] is not None and \
                            stage3["mux"].already_delivered(f"harness-{item['id']}", 3.0):
                        # Its receipt was only late: typing it now would send it twice.
                        control.acknowledge_instruction(root, session_id, item["id"])
                        transcript.note(f"controller message {item['id']} had already arrived; not typed again")
                        continue
                    transcript.note(f"controller message ({item['source']}): {item['text']}")
                    _submit_controller_message(master, item["source"], item["text"])
                    control.acknowledge_instruction(root, session_id, item["id"])
        exited = child.poll()
        if exited is None and stop_requested:
            return STUCK_EXIT_CODE
        return child.wait() if exited is None else exited
    finally:
        if child.poll() is None:
            _stop_child_group(child)
        # Review r2 audit: nothing taken may stay "taken" for ever when the
        # session ends. Unsent messages go back to the queue; one in flight on
        # a Stage 3 channel counts as posted (it may have arrived). Both move
        # to the session that continues this one, checked before sending.
        try:
            unsent = [item for item in controller_queue]
            if unsent:
                control.return_instructions(root, session_id, unsent)
            in_flight = stage3["worker"].current if stage3.get("worker") is not None else None
            if in_flight:
                control.mark_posted(root, session_id, in_flight["id"], in_flight)
        except (ValueError, OSError):
            pass
        if stage3["mux"] is not None:
            # Bounded: a channel's teardown must never hold the supervisor open
            # (review r2: Stop all left supervisors and relays alive for minutes).
            import threading as _threading
            stopper = _threading.Thread(target=stage3["mux"].stop, daemon=True)
            stopper.start()
            stopper.join(timeout=STAGE3_TEARDOWN_SECONDS)
            if stopper.is_alive():
                transcript.note("plumbing stage 3: the delivery channel did not close within its bound; the terminal closes anyway")
        if runtime_directory and (codex_app_server or claude_inbox):
            # Always, even when the channel never started (review r1: a stale
            # runtime directory outlived its session).
            from harness import runtime_dir
            runtime_dir.remove(Path(runtime_directory))
        # Do not wait for a dead child process to drain a PTY while restoring
        # the owner terminal after a safety stop.
        termios.tcsetattr(stdin_fd, termios.TCSANOW, original)
        try:
            os.close(master)
        except OSError:
            pass
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGWINCH, previous_winch)
        transcript.note("supervisor ended; terminal closed")
        transcript.close()
        try:
            board.offline(root, agent_id, "visible CLI terminal ended", transport_ended=True)
        except ValueError:
            pass
        if close_terminal_on_exit:
            _schedule_terminal_close(stdin_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Visible CLI with owner-direction and retry routing")
    add_context_arguments(parser, root_required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--close-terminal-on-exit", action="store_true")
    parser.add_argument("--provider", default="")
    parser.add_argument("--execution-root", default="")
    # Plumbing Stage 3, Codex: the app-server command line (JSON array) and the
    # session's runtime directory. Absent, the session runs exactly as before.
    parser.add_argument("--codex-app-server-json", default="")
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--claude-inbox", action="store_true")
    parser.add_argument("--codex-expected-policy", default="")
    parser.add_argument("--busy-wait", type=float, default=900.0)
    parser.add_argument("--receipt-timeout", type=float, default=30.0)
    parser.add_argument("--app-server-timeout", type=float, default=20.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a CLI command is required after --")
    return run(
        context_from_args(args), args.session_id, args.agent_id, command,
        close_terminal_on_exit=args.close_terminal_on_exit,
        provider=args.provider, execution_root=args.execution_root,
        codex_app_server=json.loads(args.codex_app_server_json) if args.codex_app_server_json else None,
        runtime_directory=args.runtime_dir, claude_inbox=args.claude_inbox, receipt_timeout=args.receipt_timeout,
        codex_expected_policy=json.loads(args.codex_expected_policy) if args.codex_expected_policy else None,
        busy_wait=args.busy_wait,
        response_timeout=args.app_server_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())

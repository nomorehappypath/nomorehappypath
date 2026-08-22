#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Keep a visible interactive CLI under local harness supervision.

The owner still types directly into the Terminal.  The supervisor only records
that real terminal input as owner direction and delivers explicitly labelled
controller messages (for example, a failed-review retry) back into the same
visible terminal.  It deliberately does not infer who changed a shared Git
worktree: the board's task-start gate is the authoritative owner-direction
enforcement point.
"""
from __future__ import annotations

import argparse
import array
import fcntl
import os
import pty
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
from harness import board, child_process, contract, control
from harness.project_context import add_context_arguments, context_from_args


PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
PASTE_BUFFER = b"\0HARNESS_PASTE\0"


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


def _record_owner_direction(root: Path, session_id: str, text: str) -> None:
    """Store one real owner instruction, ignoring terminal paste control bytes."""
    payload = bytearray(text.encode("utf-8", errors="ignore"))
    _strip_terminal_replies(payload, preserve_paste_markers=False)
    text = contract.normalize_owner_direction(payload.decode("utf-8", errors="ignore"))
    if not text:
        return
    try:
        board.record_owner_direction(root, session_id, text)
        print("\r\nHARNESS | owner direction recorded; Delivery may now begin its internal task.\r", flush=True)
    except ValueError:
        # Direction is only recorded for one pre-registered Delivery session.
        # Reviewer/CTO terminal input is never treated as owner task work.
        pass


def _record_owner_lines(root: Path, session_id: str, buffer: bytearray) -> None:
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
            _record_owner_direction(root, session_id, payload)
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
        _record_owner_direction(root, session_id, line)


def _copy_terminal_size(source_fd: int, target_fd: int) -> None:
    """Give the nested CLI the real Terminal dimensions before it draws."""
    try:
        size = fcntl.ioctl(source_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(target_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _make_controlling_terminal() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _schedule_terminal_close(stdin_fd: int) -> None:
    """Close only this finished managed Terminal window after the process exits."""
    if sys.platform != "darwin":
        return
    try:
        terminal_tty = os.ttyname(stdin_fd)
    except OSError:
        return
    script = r'''on run argv
 delay 0.5
 set targetTTY to item 1 of argv
 tell application "Terminal"
  repeat with terminalWindow in windows
   repeat with terminalTab in tabs of terminalWindow
    try
     if tty of terminalTab is targetTTY then
      close terminalWindow
      return
     end if
    end try
   end repeat
  end repeat
 end tell
end run'''
    try:
        subprocess.Popen(
            ["/usr/bin/osascript", "-e", script, terminal_tty],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def _stop_child_group(child: subprocess.Popen, grace_seconds: float = 1.0) -> None:
    """Stop an interactive CLI even when it ignores a normal termination."""
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def run(
    root: Path, session_id: str, agent_id: str, command: list[str],
    *, close_terminal_on_exit: bool = False,
) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("interactive supervisor requires a real Terminal")
    stdin_fd, stdout_fd = sys.stdin.fileno(), sys.stdout.fileno()
    master, slave = pty.openpty()
    _copy_terminal_size(stdin_fd, slave)
    child = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        preexec_fn=_make_controlling_terminal,
        env=child_process.environment(git=True, shell=True),
    )
    os.close(slave)
    original = termios.tcgetattr(stdin_fd)
    typed = bytearray()
    pending_owner_input = bytearray()
    controller_queue: list[dict] = []
    child_output_seen = False
    stop_requested = False

    def request_stop(_signal, _frame):
        nonlocal stop_requested
        stop_requested = True

    def resize(_signal, _frame):
        _copy_terminal_size(stdin_fd, master)

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    previous_winch = signal.signal(signal.SIGWINCH, resize)
    try:
        tty.setraw(stdin_fd)
        print("HARNESS | interactive supervisor ready; terminal input remains yours and is visible.", flush=True)
        while child.poll() is None:
            if stop_requested:
                _stop_child_group(child)
                break
            readable, _, _ = select.select([master, stdin_fd], [], [], .1)
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if data:
                    child_output_seen = True
                    _write(stdout_fd, data)
                    if pending_owner_input:
                        _write(master, bytes(pending_owner_input))
                        pending_owner_input.clear()
                    try:
                        control.record_output(root, session_id, len(data))
                    except ValueError:
                        pass
            if stdin_fd in readable:
                data = os.read(stdin_fd, 4096)
                if not data:
                    break
                typed.extend(data)
                _record_owner_lines(root, session_id, typed)
                if child_output_seen:
                    _write(master, data)
                else:
                    # Some CLIs call tcsetattr(TCSAFLUSH) while starting. Input
                    # written before their first output can be discarded, so
                    # retain exact owner bytes until startup is visibly ready.
                    pending_owner_input.extend(data)
            controller_queue.extend(control.take_instructions(root, session_id))
            # A supervisor-ready banner only proves the wrapper started. Wait
            # for the child CLI's first output so a slow-starting CLI cannot
            # receive controller input before it has configured its terminal.
            if child_output_seen and controller_queue:
                item = controller_queue.pop(0)
                _submit_controller_message(master, item["source"], item["text"])
                control.acknowledge_instruction(root, session_id, item["id"])
        return child.wait()
    finally:
        if child.poll() is None:
            _stop_child_group(child)
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
    )


if __name__ == "__main__":
    raise SystemExit(main())

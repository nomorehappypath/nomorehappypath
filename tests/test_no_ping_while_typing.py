# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Backlog #2 (2026-09-26 21:28): the owner pressed Enter on a question and a
harness ping was typed in the same instant; two submissions at once froze the
Codex terminal. A controller message now waits for a short quiet window after
the owner's last real keystroke, Enter included.
"""
from __future__ import annotations

import os
import pty
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from harness import board, control, interactive_supervisor

ROOT = Path(__file__).resolve().parents[1]


class QuietWindowGateTests(unittest.TestCase):
    def test_no_message_within_the_quiet_window_after_any_keystroke(self):
        allowed = interactive_supervisor._controller_delivery_allowed
        quiet = interactive_supervisor.OWNER_QUIET_SECONDS
        # the owner just pressed Enter: nothing unsent, but they were active a moment ago
        self.assertFalse(allowed(b"", 100.0, 101.0, 100.0))
        self.assertTrue(allowed(b"", 100.0, 100.0 + quiet, 100.0), "delivered once the owner has been quiet")
        self.assertTrue(allowed(b"", 0.0, 1000.0, 0.0), "an idle owner never delays a message")

    def test_the_unsent_text_cap_is_unchanged(self):
        allowed = interactive_supervisor._controller_delivery_allowed
        hold = interactive_supervisor.OWNER_INPUT_HOLD_SECONDS
        self.assertFalse(allowed(b"half a sen", 100.0, 110.0, 100.0))
        self.assertTrue(allowed(b"half a sen", 100.0, 100.0 + hold, 100.0))

    def test_a_terminal_reply_is_not_owner_activity(self):
        keystroke = interactive_supervisor._is_owner_keystroke
        self.assertFalse(keystroke(b"\x1b[?1;2c"), "a device-attributes reply")
        self.assertFalse(keystroke(b"\x1b[12;40R"), "a cursor-position reply")
        self.assertTrue(keystroke(b"a"))
        self.assertTrue(keystroke(b"\r"), "Enter is owner activity")
        self.assertTrue(keystroke(b"\x7f"), "backspace is owner activity")
        self.assertTrue(keystroke(b"\x1b[200~pasted\x1b[201~"), "a paste is owner activity")


# Every report form a terminal writes on the owner's input stream.
TERMINAL_REPORTS = (
    b"\x1b[12;40R", b"\x1b[?1;2c", b"\x1b[>0;276;0c", b"\x1b[0n", b"\x1b[?997;1n", b"\x1b[8;24;80t",
    b"\x1b[I", b"\x1b[O", b"\x1b[?2004;1$y", b"\x1b[?1u",
    b"\x1b]11;rgb:0000/0000/0000\x07", b"\x1b]11;rgb:0000/0000/0000\x1b\\",
    b"\x1bP>|kitty(0.35)\x1b\\", b"\x1bP1$r0m\x1b\\",
)
# Keys the owner presses that arrive as escape sequences, plus plain keys.
OWNER_KEYS = {
    "up arrow (history recall)": b"\x1b[A", "ctrl+right": b"\x1b[1;5C", "home": b"\x1b[H", "delete": b"\x1b[3~",
    "application-mode up": b"\x1bOA", "F5": b"\x1b[15~", "alt+x": b"\x1bx", "mouse click": b"\x1b[<0;10;5M",
    "kitty-encoded a": b"\x1b[97u", "paste": b"\x1b[200~hi\x1b[201~", "Enter": b"\r", "ctrl+c": b"\x03",
    "backspace": b"\x7f", "a": b"a", "tab": b"\t",
}


class SplitReadClassifierTests(unittest.TestCase):
    """Review round 2 (d17eb3e): a reply read in pieces was classified piece by
    piece and each piece looked like typing."""

    def feed(self, *chunks):
        classifier = interactive_supervisor._OwnerKeyClassifier()
        return [classifier.feed(chunk, 0.0) for chunk in chunks], classifier

    def test_a_report_split_anywhere_into_two_or_three_reads_is_never_owner_activity(self):
        for report in TERMINAL_REPORTS:
            self.assertFalse(interactive_supervisor._is_owner_keystroke(report), report)
            self.assertEqual(self.feed(report * 3)[0], [False], report)
            for first in range(1, len(report)):
                results, classifier = self.feed(report[:first], report[first:])
                self.assertEqual(results, [False, False], (report, first))
                self.assertEqual(classifier.fragment, b"", (report, first))
                for second in range(first + 1, len(report)):
                    results, _ = self.feed(report[:first], report[first:second], report[second:])
                    self.assertEqual(results, [False, False, False], (report, first, second))

    def test_every_owner_key_counts_whole_or_split(self):
        for name, key in OWNER_KEYS.items():
            self.assertTrue(interactive_supervisor._is_owner_keystroke(key), name)
            for cut in range(1, len(key)):
                self.assertTrue(any(self.feed(key[:cut], key[cut:])[0]), (name, cut))

    def test_a_key_in_the_same_read_as_a_report_fragment_counts(self):
        self.assertEqual(self.feed(b"a\x1b[12;", b"40R")[0], [True, False])
        self.assertEqual(self.feed(b"\x1b[12;", b"40Ra")[0], [False, True])
        self.assertEqual(self.feed(b"\x1b[12;", b"\r")[0], [False, True], "Enter after a broken fragment is the owner")

    def test_an_unfinished_sequence_is_the_owners_keys_once_nothing_completes_it(self):
        """Review round 3: ESC[ (Alt+[) was kept as a possible report for ever.
        Any unfinished sequence — a lone Escape, ESC[, ESC[12; — is undecided
        (nothing may be typed meanwhile) and becomes the owner's keys, at the
        time it arrived, once KEY_FRAGMENT_SECONDS pass with nothing after it."""
        wait = interactive_supervisor.KEY_FRAGMENT_SECONDS
        for keys in (b"\x1b", b"\x1b[", b"\x1b[12;", b"\x1b]11;rgb:0", b"\x1bP>|"):
            classifier = interactive_supervisor._OwnerKeyClassifier()
            self.assertFalse(classifier.feed(keys, 10.0), keys)
            self.assertTrue(classifier.undecided, keys)
            self.assertIsNone(classifier.unfinished_keys_at(10.0 + wait / 2), keys)
            self.assertTrue(classifier.undecided, keys)
            self.assertEqual(classifier.unfinished_keys_at(10.0 + 2 * wait), 10.0, keys)
            self.assertFalse(classifier.undecided, keys)
            self.assertIsNone(classifier.unfinished_keys_at(20.0), "counted once")

    def test_a_slow_report_withdraws_its_provisional_keys_but_owner_keys_stay(self):
        wait = interactive_supervisor.KEY_FRAGMENT_SECONDS
        slow = interactive_supervisor._OwnerKeyClassifier()
        slow.feed(b"\x1b[12;", 10.0)
        self.assertEqual(slow.unfinished_keys_at(10.0 + 3 * wait), 10.0)
        self.assertFalse(slow.feed(b"4", 10.4), "still the same unfinished sequence")
        self.assertIsNone(slow.take_withdrawal())
        self.assertFalse(slow.feed(b"0R", 10.5))
        self.assertEqual(slow.take_withdrawal(), 10.0, "a cursor report after all: the provisional keys go")
        self.assertIsNone(slow.take_withdrawal(), "withdrawn once")
        owner = interactive_supervisor._OwnerKeyClassifier()
        owner.feed(b"\x1b[", 10.0)
        owner.unfinished_keys_at(10.0 + 3 * wait)
        self.assertTrue(owner.feed(b"A", 11.0), "ESC[ then A is the owner's arrow")
        self.assertIsNone(owner.take_withdrawal())
        escape = interactive_supervisor._OwnerKeyClassifier()
        escape.feed(b"\x1b", 10.0)
        escape.unfinished_keys_at(10.0 + 3 * wait)
        self.assertFalse(escape.feed(b"\x1b[12;40R", 20.0), "a later report is still a report")
        self.assertIsNone(escape.take_withdrawal(), "and never withdraws the owner's Escape")

    def test_a_report_completed_in_time_is_never_undecided_afterwards(self):
        classifier = interactive_supervisor._OwnerKeyClassifier()
        classifier.feed(b"\x1b[12;", 10.0)
        self.assertFalse(classifier.feed(b"40R", 10.05))
        self.assertFalse(classifier.undecided)
        self.assertIsNone(classifier.unfinished_keys_at(11.0))

    def test_a_fragment_that_never_ends_is_not_a_report(self):
        self.assertEqual(self.feed(b"\x1b[" + b"1" * (interactive_supervisor.MAX_REPLY_FRAGMENT + 1))[0], [True])


class SubmittedLineCollisionTests(unittest.TestCase):
    """A real PTY through the real supervisor, as in the 2026-09-25 typing-gate test."""

    def _read_until(self, fd: int, marker: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        seen = b""
        while marker not in seen and time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    seen += os.read(fd, 65536)
                except OSError:
                    break
        return seen

    def test_a_message_queued_as_the_owner_presses_enter_waits_for_the_quiet_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            master, slave = pty.openpty()
            child = (
                "import os,tty\n"
                "tty.setraw(0)\n"
                "os.write(1,b'CHILD_READY')\n"
                "seen=b''\n"
                "while b'[SYSTEM CONTROL' not in seen: seen += os.read(0,4096); os.write(1,b'ECHO:'+seen[-40:]+b'\\n')\n"
                "os.write(1,b'GOT_CONTROL\\n')\n"
            )
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root),
                "--session-id", session["id"], "--agent-id", agent["id"], "--", "python3", "-c", child,
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"CHILD_READY", 10)
                os.write(master, b"what is the status of the brief?\r")  # the owner SUBMITS a question
                control.enqueue_instruction(root, session["id"], "TASK ACTION DUE: poll now.", "automatic-recovery")
                held = self._read_until(master, b"GOT_CONTROL", 1.5)   # inside the 2.5 s quiet window
                self.assertNotIn(b"GOT_CONTROL", held, "the ping was typed in the same moment as the owner's Enter")
                delivered = held + self._read_until(master, b"GOT_CONTROL", 10.0)
                self.assertIn(b"GOT_CONTROL", delivered, "the ping is still delivered once the owner is quiet")
                self.assertLess(delivered.index(b"brief?"), delivered.index(b"GOT_CONTROL"))
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def _deliver_after(self, owner_input: bytes, send_reply, window: float, cap: float | None = None, settle: float = 0.0):
        """Run the REAL supervisor loop (via main(), optional lowered cap). The
        owner writes `owner_input`, a controller message is queued, then
        `send_reply(master)` runs every 0.5 s for `window` seconds or until the
        message is typed. Returns (seen bytes, seconds from the owner's input)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            master, slave = pty.openpty()
            child = (
                "import os,tty\n"
                "tty.setraw(0)\n"
                "os.write(1,b'CHILD_READY')\n"
                "seen=b''\n"
                "while b'[SYSTEM CONTROL' not in seen: seen += os.read(0,4096)\n"
                "os.write(1,b'GOT_CONTROL\\n')\n"
                "import time; time.sleep(30)\n"
            )
            launcher = "import sys\nfrom harness import interactive_supervisor as s\n"
            if cap is not None:
                launcher += f"s.OWNER_INPUT_HOLD_SECONDS = {cap}\n"
            launcher += "sys.exit(s.main(sys.argv[1:]))\n"
            command = [
                "python3", "-c", launcher, "--root", str(root),
                "--session-id", session["id"], "--agent-id", agent["id"], "--", "python3", "-c", child,
            ]
            env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True, env=env, cwd=str(ROOT))
            os.close(slave)
            try:
                self._read_until(master, b"CHILD_READY", 10)
                time.sleep(settle)                                   # past any hold tied to process start
                if owner_input:
                    os.write(master, owner_input)                    # the owner's last real keystroke
                pressed = time.monotonic()
                control.enqueue_instruction(root, session["id"], "TASK ACTION DUE: poll now.", "automatic-recovery")
                seen = b""
                deadline = pressed + window
                while b"GOT_CONTROL" not in seen and time.monotonic() < deadline:
                    if send_reply:
                        send_reply(master)
                    seen += self._read_until(master, b"GOT_CONTROL", 0.5)
                return seen, time.monotonic() - pressed
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def test_terminal_replies_never_extend_the_unsent_text_hold(self):
        """Review of 56d858f: with a half-typed line, every terminal reply (cursor
        position, device attributes) restarted the unsent-text clock, so a steady
        stream of replies could hold a queued message for ever. The real loop runs
        here with the cap lowered to 3 s; replies keep arriving every 0.5 s and the
        message must still arrive about 3 s after the owner's last real keystroke."""
        cap = 3.0
        seen, arrived = self._deliver_after(b"half a sen", lambda master: os.write(master, b"\x1b[12;40R"), cap + 4.0, cap)
        self.assertIn(b"GOT_CONTROL", seen, "terminal replies held the message past the cap for ever")
        self.assertGreaterEqual(arrived, cap - 0.2, "the half-typed line was not protected for the full cap")

    def test_split_terminal_replies_never_extend_the_unsent_text_hold(self):
        """Review of d17eb3e: the same reply read in two pieces (``ESC[12;`` then
        ``40R``) was classified piece by piece and each piece restarted the clock.
        Every reply here is split, at a different byte each time, into two reads."""
        cap = 3.0
        report = b"\x1b[12;40R"
        cuts = iter(list(range(1, len(report))) * 4)

        def split_reply(master):
            cut = next(cuts)
            os.write(master, report[:cut])
            time.sleep(0.05)                                     # a separate read for each piece
            os.write(master, report[cut:])

        seen, arrived = self._deliver_after(b"half a sen", split_reply, cap + 4.0, cap)
        self.assertIn(b"GOT_CONTROL", seen, "split terminal replies held the message past the cap for ever")
        self.assertGreaterEqual(arrived, cap - 0.2, "the half-typed line was not protected for the full cap")

    def test_a_slowly_split_report_stream_does_not_hold_messages(self):
        """Round-4 audit: a report whose second half arrives after
        KEY_FRAGMENT_SECONDS is first counted as keys, then withdrawn when the
        report completes; a stream of such reports never holds a message."""
        report = b"\x1b[12;40R"
        cuts = iter(list(range(1, len(report))) * 4)

        def slow_split(master):
            cut = next(cuts)
            os.write(master, report[:cut])
            time.sleep(0.3)
            os.write(master, report[cut:])

        seen, arrived = self._deliver_after(b"", slow_split, 6.0, settle=3.0)
        self.assertIn(b"GOT_CONTROL", seen, "slowly split terminal reports held the message")
        self.assertLess(arrived, 1.5, f"held {arrived:.2f} s by terminal reports")

    def test_recalling_history_with_the_up_arrow_holds_the_message_for_the_quiet_window(self):
        """Round-3 audit: the up arrow recalls an earlier line into the input box
        (unsent text the supervisor cannot see); it is the owner at the keyboard,
        so a message waits for the quiet window instead of landing on that line."""
        seen, arrived = self._deliver_after(b"\x1b[A", None, 1.5, settle=3.0)
        self.assertNotIn(b"GOT_CONTROL", seen, "a message was typed right after the owner pressed the up arrow")

    def test_unfinished_escape_keys_hold_the_message_for_the_quiet_window(self):
        """Review round 3 probe: real PTY input ESC[ was never counted and a
        queued message was typed 0.107 s later. Alt+[ (ESC[), a lone Escape and
        an unfinished ESC[12; are the owner at the keyboard."""
        for keys in (b"\x1b[", b"\x1b", b"\x1b[12;"):
            seen, arrived = self._deliver_after(keys, None, 1.5, settle=3.0)
            self.assertNotIn(b"GOT_CONTROL", seen, f"a message was typed {arrived:.3f} s after the owner's {keys!r}")

    def test_escape_bracket_completed_later_by_an_arrow_is_the_owner_throughout(self):
        """Review round 3 probe, second half: ESC[ then, a second later, the
        owner's up arrow. No message may land in between or right after."""
        def arrow_after_a_second(master, state={"sent": False, "start": None}):
            state["start"] = state["start"] or time.monotonic()
            if not state["sent"] and time.monotonic() - state["start"] >= 1.0:
                os.write(master, b"\x1b[A")
                state["sent"] = True
        seen, arrived = self._deliver_after(b"\x1b[", arrow_after_a_second, 2.5, settle=3.0)
        self.assertNotIn(b"GOT_CONTROL", seen, f"a message was typed {arrived:.3f} s into ESC[ ... arrow")

    def test_unsent_escape_bytes_are_held_only_up_to_the_cap(self):
        """ESC[ leaves a printable "[" in the unsent-text buffer, so the
        existing cap (not the quiet window alone) bounds the hold; the real
        loop runs with the cap lowered to 3 s."""
        cap = 3.0
        seen, arrived = self._deliver_after(b"\x1b[", None, cap + 3.0, cap, settle=3.0)
        self.assertIn(b"GOT_CONTROL", seen, "a stray ESC[ held the message past the cap")
        self.assertGreaterEqual(arrived, cap - 0.2)

    def test_a_fresh_terminal_with_no_owner_input_delivers_at_once(self):
        """Round-3 audit: "no keystroke yet" was stored as 0.0 and the process
        clock can start near zero, so a new terminal held every message for its
        first 2.5 s as if the owner had just typed."""
        seen, arrived = self._deliver_after(b"", None, 5.0)
        self.assertIn(b"GOT_CONTROL", seen)
        self.assertLess(arrived, 1.5, f"an idle new terminal held the message for {arrived:.2f} s")

    def test_a_terminal_reply_does_not_lift_the_sign_in_hold(self):
        """Same class, audit of 56d858f: every stdin read also told the attention
        watcher "the owner answered", which clears a sign-in alert. A terminal's
        automatic reply is not the owner signing in; the hold must stay."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "claude_reviewer")
            agent = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
            master, slave = pty.openpty()
            child = (
                "import os,tty\n"
                "tty.setraw(0)\n"
                "os.write(1,b'CHILD_READY\\r\\nLogin expired \\xc2\\xb7 Please run /login\\r\\n')\n"
                "seen=b''\n"
                "while b'[SYSTEM CONTROL' not in seen: seen += os.read(0,4096)\n"
                "os.write(1,b'GOT_CONTROL\\n')\n"
            )
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root),
                "--session-id", session["id"], "--agent-id", agent["id"], "--", "python3", "-c", child,
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"run /login", 10)
                time.sleep(0.5)
                control.enqueue_instruction(root, session["id"], "REVIEW REQUEST ROUTED: r-1. claim it.", "review-assignment")
                seen = b""
                for _ in range(8):                                   # replies for 4 s, well past the 2.5 s quiet window
                    if b"GOT_CONTROL" in seen:
                        break                                        # typed: the child has exited
                    os.write(master, b"\x1b[12;40R")
                    seen += self._read_until(master, b"GOT_CONTROL", 0.5)
                self.assertNotIn(b"GOT_CONTROL", seen, "a terminal reply lifted the sign-in hold and the message was typed")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)


if __name__ == "__main__":
    unittest.main()

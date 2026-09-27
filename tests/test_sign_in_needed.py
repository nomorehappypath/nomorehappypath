# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Backlog #8 (2026-09-27): expired Claude logins stalled the CTO and the
Reviewer for about an hour. The board re-routed a review to the logged-out
Reviewer every 90 s and the owner was never told plainly who to sign in.

Now: the incident's exact screen text is recognised; nothing is routed to a
signed-out terminal (no wake-ups, no review routes, no stall count); another
eligible reviewer takes the review; Mission Control says "The Reviewer needs
you to sign in again: open its terminal and run /login."; and everything
resumes by itself once the prompt leaves the screen after /login.
"""
from __future__ import annotations

import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from harness import attention, board, board_viewer, browser_acceptance, control, project_memory
from tests import test_board as _board_tests


class _Fixture(unittest.TestCase):
    setUp = _board_tests.BoardTests.setUp
    tearDown = _board_tests.BoardTests.tearDown
    ledger = _board_tests.BoardTests.ledger
    qa_command = _board_tests.BoardTests.qa_command
    declare_chunks = _board_tests.BoardTests.declare_chunks
    delivery = _board_tests.BoardTests.delivery

    def reviewer(self):
        session = control.create(self.root, "claude_reviewer")
        agent = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        control.take_instructions(self.root, session["id"])
        return session, agent

    def sign_out(self, session_id: str) -> None:
        control.record_attention(self.root, session_id, attention.detect("Login expired · Please run /login"))


class DetectionTests(unittest.TestCase):
    def test_the_incident_screen_text_is_recognised(self):
        for text in (
            "Login expired - Please run /login",
            "Please run /login · API Error: 401 OAuth access token has been revoked.",
            "API Error: 401 OAuth access token has been revoked.",
            "Login expired",
        ):
            self.assertEqual(attention.detect(text), attention.LOGIN_REASON, text)
        self.assertTrue(attention.needs_sign_in({"attention_reason": attention.LOGIN_REASON}))
        self.assertFalse(attention.needs_sign_in({"attention_reason": "is asking permission to continue ('Do you want to proceed?')"}))


class RoutingTests(_Fixture):
    def open_review(self):
        dev = self.delivery("TASK-SIGN-IN")
        self.declare_chunks(dev["id"], [("one", "one reviewable outcome")])
        return dev

    def request(self, dev):
        return board.request_review(
            self.root, dev["id"], self.ledger("sign-in.md"), "review while the reviewer is signed out",
            chunk="one", test_command=self.qa_command(),
        )

    def test_a_signed_out_reviewer_is_never_routed_and_the_owner_is_told_to_sign_it_in(self):
        dev = self.open_review()
        session, reviewer = self.reviewer()
        self.sign_out(session["id"])
        request = self.request(dev)
        for _ in range(3):
            board.route_open_reviews(self.root, retry_seconds=0)
        state = board.snapshot(self.root)
        self.assertNotEqual(state["qa_requests"][request["id"]].get("routed_to"), reviewer["id"])
        self.assertEqual(control.take_instructions(self.root, session["id"]), [], "nothing queued for a signed-out terminal")
        self.assertEqual(state["reviewer_needed"]["message"],
                         "The Reviewer needs you to sign in again: open its terminal and run /login.")
        self.assertEqual(len([e for e in state["events"] if e["kind"] == "review_routed"]), 0)
        self.assertEqual(len([e for e in state["events"] if e["kind"] == "reviewer_needed"]), 1, "said once, not every 90 s")

    def test_another_eligible_reviewer_takes_the_review(self):
        dev = self.open_review()
        signed_out_session, signed_out = self.reviewer()
        self.sign_out(signed_out_session["id"])
        live_session, live = self.reviewer()
        request = self.request(dev)
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["routed_to"], live["id"])

    def test_a_route_already_given_to_a_reviewer_that_then_signs_out_is_taken_back(self):
        dev = self.open_review()
        session, reviewer = self.reviewer()
        request = self.request(dev)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["routed_to"], reviewer["id"])
        self.sign_out(session["id"])
        control.take_instructions(self.root, session["id"])
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertEqual(control.take_instructions(self.root, session["id"]), [], "no re-route to the signed-out terminal")

    def test_after_sign_in_the_review_is_routed_again(self):
        dev = self.open_review()
        session, reviewer = self.reviewer()
        self.sign_out(session["id"])
        request = self.request(dev)
        board.route_open_reviews(self.root, retry_seconds=0)
        control.clear_attention(self.root, session["id"])
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["routed_to"], reviewer["id"])
        self.assertEqual(len(control.take_instructions(self.root, session["id"])), 1)


class SupersededRouteTests(RoutingTests):
    """Review round 1: a previous assignment must not survive a new route.

    The reviewer's reproduction kept the FIRST reviewer's assignment queued
    while it signed out; after the review moved to a second reviewer, the first
    still held its "review-assignment" and could act on it after /login."""

    test_a_signed_out_reviewer_is_never_routed_and_the_owner_is_told_to_sign_it_in = None
    test_another_eligible_reviewer_takes_the_review = None
    test_a_route_already_given_to_a_reviewer_that_then_signs_out_is_taken_back = None
    test_after_sign_in_the_review_is_routed_again = None

    def inbox(self, session_id: str) -> list:
        with control.locked_state(self.root) as state:
            return [dict(item) for item in (state.get("inbox") or {}).get(session_id, [])]

    def test_a_still_queued_assignment_is_withdrawn_when_the_review_moves_to_another_reviewer(self):
        dev = self.open_review()
        first_session, first = self.reviewer()
        request = self.request(dev)   # routed to the first reviewer; its assignment stays QUEUED
        self.assertEqual([item["source"] for item in self.inbox(first_session["id"])], ["review-assignment"])
        self.sign_out(first_session["id"])
        second_session, second = self.reviewer()
        board.route_open_reviews(self.root, retry_seconds=0)
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"][request["id"]]["routed_to"], second["id"])
        self.assertEqual(self.inbox(first_session["id"]), [], "the first reviewer's assignment was withdrawn before it could be typed")
        self.assertEqual([item["source"] for item in self.inbox(second_session["id"])], ["review-assignment"])
        withdrawn = [e for e in state["events"] if e["kind"] == "review_wake_withdrawn"
                     and e.get("request_id") == request["id"] and e.get("agent_id") == first["id"]]
        self.assertEqual([e["action"] for e in withdrawn], ["withdrawn"], "the first reviewer's copy was withdrawn once")
        # and the board still refuses a reservation on the superseded route
        with self.assertRaisesRegex(ValueError, "already routed to"):
            board.reserve_qa(self.root, first["id"], request["id"])

    def test_an_assignment_already_taken_gets_one_stand_down_line_after_the_review_moves(self):
        dev = self.open_review()
        first_session, first = self.reviewer()
        request = self.request(dev)
        control.take_instructions(self.root, first_session["id"])   # the supervisor already took it
        self.sign_out(first_session["id"])
        self.reviewer()
        board.route_open_reviews(self.root, retry_seconds=0)
        notices = self.inbox(first_session["id"])
        self.assertEqual(len(notices), 1)
        self.assertIn("REVIEW REASSIGNED", notices[0]["text"])
        self.assertIn("Do not reserve, claim or investigate it", notices[0]["text"])

    def test_re_sending_to_the_same_reviewer_leaves_exactly_one_assignment(self):
        dev = self.open_review()
        session, reviewer = self.reviewer()
        request = self.request(dev)
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]]["routed_at"] = "2000-01-01T00:00:00+00:00"   # overdue: the router retries
        board.route_open_reviews(self.root, retry_seconds=0)
        assignments = [item for item in self.inbox(session["id"]) if item["source"] == "review-assignment"]
        self.assertEqual(len(assignments), 1, "the old copy was withdrawn; the reviewer never gets the same assignment twice")
        self.assertFalse([item for item in self.inbox(session["id"]) if "REASSIGNED" in item["text"]], "no stand-down for the same reviewer")


class SupervisorSignInTests(unittest.TestCase):
    """Review round 1 (CTO point 2): the supervisor types nothing into, and takes
    nothing for, a terminal whose screen asks for sign-in — the messages stay
    queued where the board can still withdraw them."""

    def test_taken_but_untyped_messages_are_returned_and_can_then_be_withdrawn(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "claude_reviewer")
            queued = control.enqueue_instruction(root, session["id"], "REVIEW REQUEST ROUTED: r-1", "review-assignment")
            gone = control.enqueue_instruction(root, session["id"], "an older message", "owner-message")
            taken = control.take_instructions(root, session["id"])
            control.withdraw_instruction(root, gone["id"])          # still "taken": withdraw cannot reach it
            control.acknowledge_instruction(root, session["id"], gone["id"])
            self.assertEqual(control.return_instructions(root, session["id"], taken), 1, "only the untyped one comes back")
            receipt = control.withdraw_instruction(root, queued["id"])
            self.assertEqual(receipt["status"], "withdrawn", "the board can take it back after it was returned")
            self.assertEqual(control.take_instructions(root, session["id"]), [])

    def _read_until(self, fd, marker, timeout):
        import os, select
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

    def test_a_signed_out_terminal_gets_nothing_typed_and_its_message_stays_withdrawable(self):
        import os, pty, subprocess, tempfile
        root_dir = Path(__file__).resolve().parents[1]
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
                "python3", str(root_dir / "harness" / "interactive_supervisor.py"), "--root", str(root),
                "--session-id", session["id"], "--agent-id", agent["id"], "--", "python3", "-c", child,
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"run /login", 10)
                time.sleep(0.5)
                queued = control.enqueue_instruction(root, session["id"], "REVIEW REQUEST ROUTED: r-1. claim it.", "review-assignment")
                seen = self._read_until(master, b"GOT_CONTROL", 3.0)
                self.assertNotIn(b"GOT_CONTROL", seen, "a message was typed into a terminal that needs sign-in")
                with control.locked_state(root) as state:
                    receipt = dict(state["instruction_receipts"][queued["id"]])
                self.assertEqual(receipt["status"], "queued", "the supervisor did not take it; the board can still withdraw it")
                self.assertEqual(control.withdraw_instruction(root, queued["id"])["status"], "withdrawn")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)


class WakeTests(_Fixture):
    def test_no_wake_is_routed_to_a_signed_out_agent_and_wakes_resume_after_sign_in(self):
        session = control.create(self.root, "claude_cto")
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
        dev = self.delivery("WORK")
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"][cto["id"]].update({"last_poll_at": old, "spawned_at": old})
            state["agents"][dev["id"]].update({"last_poll_at": board.now()})
        self.sign_out(session["id"])
        with patch("harness.control.enqueue_instruction", return_value={"id": "w", "source": "cto-monitoring-lease"}) as enqueue:
            for _ in range(4):
                board.mark_stalled(self.root)
            enqueue.assert_not_called()
        state = board.snapshot(self.root)
        agent = state["agents"][cto["id"]]
        self.assertEqual(agent["liveness"], board.SIGNED_OUT_LIVENESS)
        self.assertEqual(agent["liveness_note"], "The CTO needs you to sign in again: open its terminal and run /login.")
        self.assertNotIn("consecutive_stalls", agent, "a signed-out agent is not counted as stalling")
        self.assertEqual([e["message"] for e in state["events"] if e["kind"] == "agent_needs_sign_in"],
                         ["The CTO needs you to sign in again: open its terminal and run /login."])
        control.clear_attention(self.root, session["id"])
        with patch("harness.control.enqueue_instruction", return_value={"id": "w", "source": "cto-monitoring-lease"}) as enqueue:
            board.mark_stalled(self.root)
            self.assertEqual(enqueue.call_count, 1, "wake-ups resume after sign-in")
        self.assertEqual(len([e for e in board.snapshot(self.root)["events"] if e["kind"] == "agent_signed_in"]), 1)


PROBE = r"""
<script>
(async () => {
  const banner = () => document.querySelector('#waiting-banner');
  for (let attempt = 0; attempt < 150 && !(banner() && !banner().hidden && banner().textContent.includes('sign in')); attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const node = banner();
  const box = node ? node.getBoundingClientRect() : {width: 0, height: 0};
  const badges = Array.from(document.querySelectorAll('#agents .badge')).map(n => n.textContent.trim());
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: node ? node.textContent.trim() : '', visible: box.width > 0 && box.height > 0, badges})});
})();
</script>
"""


class RenderedSignInTests(_Fixture):
    def setUp(self):
        super().setUp()
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        from tests.environment_support import require_loopback
        require_loopback()
        project_memory.initialize(self.root, project_name="Sign-in proof", description="Facts.")

    def test_mission_control_names_the_reviewer_that_needs_signing_in(self):
        from tests.test_branding_rendered import probe_proxy
        session, reviewer = self.reviewer()
        self.sign_out(session["id"])
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Sign-in proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="sign-in-proof", chat_action_token="owner-token",
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        import tempfile
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=1000)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        reading = sink.get("value")
        self.assertIsNotNone(reading, "Chrome reported nothing")
        self.assertTrue(reading["visible"], json.dumps(reading))
        self.assertIn("The Reviewer needs you to sign in again: open its terminal and run /login.", reading["text"], json.dumps(reading))
        self.assertNotIn("tell your developer", reading["text"], "a sign-in is not reported as a bug")


if __name__ == "__main__":
    unittest.main()

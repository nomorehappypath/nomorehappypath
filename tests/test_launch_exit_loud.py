# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A terminal that stops before its CLI starts says why, once, on the board.

2026-10-10 review (P2e part 1): an idle reviewer's terminal attached and died
before its CLI launched; the control record said only "exited", nothing told the
owner, and an idle reviewer has no unfinished work so nothing brought it back.
The cause was not established. These tests drive the REAL runner with fake CLIs
and prove that any such ending leaves a plain reason on the session record and
one card on the board, that a transient board failure is waited out, and that a
stop, a pause or a replacement is never reported as a failure.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from harness import board, control, self_heal
from tests.test_conversation_memory import RUNNER, fake_cli
from tests.environment_support import require_loopback


class BootstrapServer:
    """A stand-in for the board worker's session-credential socket."""

    def __init__(self, directory: Path, behaviours: list):
        self.path = str(directory / "b.sock")
        self.behaviours = list(behaviours)
        self.connections = 0
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(8)
        self.stop = threading.Event()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        self.server.settimeout(0.2)
        while not self.stop.is_set():
            try:
                connection, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            behaviour = self.behaviours.pop(0) if self.behaviours else "close"
            try:
                connection.settimeout(1)
                connection.recv(4096)
                if behaviour == "close":
                    pass
                elif behaviour == "hang":
                    self.stop.wait(8)
                elif behaviour == "refuse":
                    connection.sendall(b'{"error":"this session is not allowed"}\n')
            except OSError:
                pass
            finally:
                connection.close()

    def close(self):
        self.stop.set()
        self.server.close()


class LaunchExitLoudTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.capture = self.root / "captured.txt"
        self.environment = {
            **os.environ,
            "HARNESS_CAPTURE": str(self.capture),
            "HARNESS_CLAUDE_BIN": str(fake_cli(self.root / "fake-claude")),
            "HARNESS_CODEX_BIN": str(fake_cli(self.root / "fake-codex")),
            "CLAUDE_CONFIG_DIR": str(self.base / "claude"), "CODEX_HOME": str(self.base / "codex"),
            "HARNESS_LAUNCH_RETRY_SECONDS": "0",
        }

    def runner(self, session, *extra, environment=None):
        return subprocess.Popen(
            ["bash", str(RUNNER), "--root", str(self.root), "--session-id", session["id"], "--kind", session["kind"], *extra],
            env=environment or self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def finish(self, process, timeout=40):
        out, err = process.communicate(timeout=timeout)
        return process.returncode, out, err

    def session_record(self, session):
        return next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == session["id"])

    def bootstrap(self, behaviours):
        server = BootstrapServer(self.base, behaviours)
        self.addCleanup(server.close)
        return server

    # --- the runner always says why --------------------------------------------------------------
    def test_a_failing_step_before_the_cli_leaves_a_plain_reason_a_distinct_exit_code_and_no_cli(self):
        session = control.create(self.root, "codex_delivery")
        (self.root / ".harness").mkdir(exist_ok=True)
        (self.root / ".harness" / "board").write_text("not a directory\n")      # the registration step cannot work
        code, _, err = self.finish(self.runner(session))
        self.assertEqual(code, 75, err)
        self.assertFalse(self.capture.exists(), "the CLI must not have been started")
        record = self.session_record(session)
        self.assertEqual(record["status"], "exited")
        self.assertIn("registering with the board", record["reason"])
        self.assertIn("exit code", record["reason"])
        self.assertIn("Press Start for the Delivery Agent", record["reason"])
        self.assertNotIn("outside the viewer", record["reason"])
        self.assertEqual(record["launch_failure"]["step"], "registering with the board")
        self.assertEqual(record["launch_failure"]["pid"], record["pid"])
        self.assertIn("registering with the board", err, "the terminal itself says it too")

    def test_a_setup_refusal_after_the_launch_was_planned_says_pressing_start_will_not_fix_it(self):
        session = control.create(self.root, "claude_cto")
        code, _, err = self.finish(self.runner(session))          # no sign-in handoff: the runner refuses
        self.assertEqual(code, 2, err)
        record = self.session_record(session)
        self.assertTrue(record["launch_failure"]["setup_problem"], record)
        self.assertIn("REFUSED", record["launch_failure"]["diagnostic"])
        self.assertIn("will not fix it", record["reason"])
        self.assertEqual(record["launch_failure"]["step"], "starting the CLI")

    def test_a_missing_cli_program_is_named(self):
        session = control.create(self.root, "codex_delivery")
        environment = {**self.environment, "HARNESS_CODEX_BIN": str(self.root / "no-such-codex")}
        code, _, err = self.finish(self.runner(session, environment=environment))
        record = self.session_record(session)
        self.assertEqual(record["status"], "exited", err)
        self.assertIn("could not run the CLI program (exit code 127)", record["reason"])
        self.assertIn("no-such-codex", record["reason"])
        self.assertTrue(record["launch_failure"]["setup_problem"])

    def test_a_normal_launch_records_no_failure(self):
        session = control.create(self.root, "codex_delivery")
        code, _, err = self.finish(self.runner(session))
        self.assertEqual(code, 0, err)
        record = self.session_record(session)
        self.assertIsNone(record["launch_failure"])
        self.assertTrue(record["launch_noted_at"])
        self.assertTrue(record["launch_exec_at"])

    def test_a_signal_before_the_cli_is_reported_once_with_the_signal_name(self):
        server = self.bootstrap(["hang"] * 4)
        session = control.create(self.root, "codex_delivery")
        process = self.runner(session, "--board-bootstrap", server.path,
                              environment={**self.environment, "HARNESS_LAUNCH_RETRIES": "1"})
        deadline = time.time() + 15
        while server.connections == 0 and time.time() < deadline:
            time.sleep(0.05)
        os.kill(process.pid, signal.SIGTERM)                       # a pid this test started
        code, _, err = self.finish(process)
        self.assertEqual(code, 128 + signal.SIGTERM, err)
        record = self.session_record(session)
        self.assertIn("SIGTERM", record["reason"])
        self.assertIn("signing in to the board", record["reason"])
        self.assertEqual(record["launch_failure"]["signal"], "SIGTERM")

    def test_an_owner_stop_is_not_reported_as_a_launch_failure(self):
        server = self.bootstrap(["hang"] * 4)
        session = control.create(self.root, "codex_delivery")
        process = self.runner(session, "--board-bootstrap", server.path,
                              environment={**self.environment, "HARNESS_LAUNCH_RETRIES": "1"})
        deadline = time.time() + 15
        while server.connections == 0 and time.time() < deadline:
            time.sleep(0.05)
        control.stop(self.root, session["id"])
        self.finish(process)
        record = self.session_record(session)
        self.assertEqual(record["status"], "stopped")
        self.assertIsNone(record["launch_failure"])
        self.assertEqual(self_heal.plan(self.root), [])

    # --- the registry notices what the runner could not report --------------------------------------
    def test_an_attempt_that_died_unreported_is_marked_as_unknown_cause_not_as_a_normal_exit(self):
        session = control.create(self.root, "claude_reviewer")
        control.attach(self.root, session["id"], 99999999)          # dead pid, CLI launch never noted
        record = self.session_record(session)
        self.assertEqual(record["status"], "exited")
        self.assertIn("ended before the CLI started and the cause was not recorded", record["reason"])
        self.assertEqual(record["launch_failure"]["recorded_by"], "reconcile")

    def test_a_late_report_from_an_earlier_attempt_is_rejected_and_a_new_attempt_keeps_history(self):
        session = control.create(self.root, "claude_reviewer")
        control.attach(self.root, session["id"], os.getpid())
        recorded = control.record_launch_failure(self.root, session["id"], os.getpid(), 1, "planning the CLI launch", "boom")
        self.assertTrue(recorded["recorded"])
        stale = control.record_launch_failure(self.root, session["id"], os.getpid() + 1, 1, "planning the CLI launch", "old")
        self.assertFalse(stale["recorded"])
        self.assertEqual(self.session_record(session)["launch_failure"]["diagnostic"], "boom")

    def test_the_diagnostic_is_bounded_and_token_like_strings_are_redacted(self):
        cleaned = control.sanitize_launch_diagnostic("x" * 900 + " HARNESS_BOARD_TOKEN=abcdef " + "A" * 40 + "\x00\x1b[31m end")
        self.assertLessEqual(len(cleaned), control.MAX_LAUNCH_DIAGNOSTIC)
        self.assertNotIn("A" * 40, cleaned)
        self.assertNotIn("abcdef", cleaned)
        self.assertNotIn("\x1b", cleaned)

    def test_an_attempt_that_never_attached_gets_an_honest_card(self):
        session = control.create(self.root, "claude_reviewer")
        with control.locked_state(self.root) as state:                    # its 30 seconds have passed
            state["sessions"][session["id"]]["launch_deadline"] = "2000-01-01T00:00:00+00:00"
        record = self.session_record(session)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["launch_failure"]["recorded_by"], "reconcile")
        self.assertEqual([item["action"] for item in self_heal.plan(self.root)], ["announce_launch_failure"])

    def test_a_runner_report_before_attach_is_kept_for_the_card(self):
        session = control.create(self.root, "claude_reviewer")
        control.record_launch_failure(self.root, session["id"], 4242, 2, "attaching to the control panel", "Project root does not exist")
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["launch_deadline"] = "2000-01-01T00:00:00+00:00"
        record = self.session_record(session)
        self.assertIn("Project root does not exist", record["reason"])

    def test_a_runner_killed_between_the_plan_and_the_exec_is_still_a_failed_launch(self):
        session = control.create(self.root, "claude_reviewer")
        control.attach(self.root, session["id"], 99999999)
        control.note_cli_launch(self.root, session["id"], False)         # planned, but never exec'd
        self.assertIn("cause was not recorded", self.session_record(session)["reason"])

    def test_a_failed_session_with_registered_work_is_not_hidden_by_an_unrelated_newer_session(self):
        failed = control.create(self.root, "claude_reviewer")
        board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=failed["id"])
        control.attach(self.root, failed["id"], 99999999)
        self.session_record(failed)
        control.create(self.root, "claude_reviewer")                    # a different reviewer, carrying nothing of the failed one
        self.assertEqual([item["action"] for item in self_heal.plan(self.root)], ["announce_launch_failure"])

    def test_a_plan_made_before_a_successful_resume_publishes_nothing(self):
        session = control.create(self.root, "claude_reviewer")
        control.attach(self.root, session["id"], 99999999)
        self.session_record(session)
        stale_plan = self_heal.plan(self.root)
        control.prepare_resume_sessions(self.root, [session["id"]])
        control.attach(self.root, session["id"], os.getpid())           # the relaunch attached: a new attempt
        result = self_heal.execute(self.root, stale_plan[0], lambda value: None)
        self.assertIn("skipped", result)
        self.assertEqual([e for e in board.snapshot(self.root)["events"] if e["kind"] == "self_heal_failed"], [])
        self.assertEqual(self.session_record(session)["launch_failures"][-1]["recorded_by"], "reconcile")

    # --- the card, exactly once ------------------------------------------------------------------------
    def test_the_board_gets_one_card_for_the_failed_attempt_even_for_an_idle_reviewer_with_no_agent(self):
        session = control.create(self.root, "claude_reviewer")
        control.attach(self.root, session["id"], 99999999)
        self.session_record(session)                                # reconcile marks it
        actions = self_heal.plan(self.root)
        self.assertEqual([item["action"] for item in actions], ["announce_launch_failure"])
        self_heal.run_once(self.root, lambda value: None)
        events = [e for e in board.snapshot(self.root)["events"] if e["kind"] == "self_heal_failed"]
        self.assertEqual(len(events), 1)
        self.assertIn("the cause was not recorded", events[0]["message"])
        self.assertIn("Press Start for the Independent Reviewer", events[0]["message"])
        self.assertEqual(self_heal.run_once(self.root, lambda value: None), [], "never announced twice")
        self.assertEqual(self_heal.plan(self.root), [])

    def test_no_card_when_a_live_replacement_already_carries_the_role(self):
        first = control.create(self.root, "claude_reviewer")
        control.attach(self.root, first["id"], 99999999)
        self.session_record(first)
        control.create(self.root, "claude_reviewer")                # the owner pressed Start
        self.assertEqual(self_heal.plan(self.root), [])

    # --- transient trouble is waited out, a definite answer is not ------------------------------------
    def test_a_board_that_closes_the_connection_twice_is_retried_and_the_launch_goes_on(self):
        server = self.bootstrap(["close", "close", "close", "close"])
        session = control.create(self.root, "codex_delivery")
        code, _, err = self.finish(self.runner(session, "--board-bootstrap", server.path))
        self.assertEqual(server.connections, 4, err)                # default 4 attempts, all spent
        self.assertIn("signing in to the board", self.session_record(session)["reason"])

    def test_a_definite_refusal_is_not_retried(self):
        server = self.bootstrap(["refuse", "refuse", "refuse", "refuse"])
        session = control.create(self.root, "codex_delivery")
        self.finish(self.runner(session, "--board-bootstrap", server.path))
        self.assertEqual(server.connections, 1)
        record = self.session_record(session)
        self.assertIn("this session is not allowed", record["launch_failure"]["diagnostic"])

    def test_invalid_retry_overrides_fall_back_to_the_defaults(self):
        server = self.bootstrap(["close"] * 8)
        session = control.create(self.root, "codex_delivery")
        environment = {**self.environment, "HARNESS_LAUNCH_RETRIES": "99", "HARNESS_LAUNCH_RETRY_SECONDS": "-3"}
        process = self.runner(session, "--board-bootstrap", server.path, environment=environment)
        process_output = self.finish(process, timeout=90)
        self.assertEqual(server.connections, 4, process_output)


if __name__ == "__main__":
    unittest.main()

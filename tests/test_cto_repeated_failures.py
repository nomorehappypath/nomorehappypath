# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The CTO is told when one request keeps failing the same way (windows-walk item 3).

2026-10-21: one review request failed 21 certified executions in 26 minutes, each with a recorded reason; the CTO posted nothing and
routed nothing. The board now adds the repeated cause to the CTO's monitoring instruction.

Machine inputs removed: nothing is read from the machine (temporary project, real board).

Run:  PYTHONPATH=. python3 -m unittest tests.test_cto_repeated_failures -v
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from harness import board
from tests import test_execute_once as base
from tests import test_owner_cto_channel as channel


def attempts(*reasons, status="failed"):
    return [{"status": status, "reason": reason, "recorded_at": "2026-10-21T16:00:00+00:00"} for reason in reasons]


def state_with(request_attempts, status="claimed"):
    return {"qa_requests": {"review-x-01": {"status": status, "task": "why-port-cli", "challenge_execution_attempts": request_attempts}}}


class FailureClassTests(unittest.TestCase):
    def test_numbers_ids_hashes_and_paths_do_not_make_another_cause(self):
        same = ("exit code 1: C:\\why-build\\run-20261021\\x.ps1 took 12 s", "exit code 255: C:\\why-build\\run-99\\x.ps1 took 3 s")
        self.assertEqual(board._failure_class(same[0]), board._failure_class(same[1]))
        self.assertEqual(board._failure_class("failed at /tmp/hnt-1234/abc 9f8e7d6c5b4a3f2e"), board._failure_class("failed at /tmp/hnt-99/zzz 0011223344556677"))
        self.assertNotEqual(board._failure_class("heartbeat expired"), board._failure_class("scp: connection refused"))
        # the owner's incident: the same failing step, a different output every time
        first = "scenario ledger simulations failed: S-R1: internal-qa test command failed with exit code 1: scp: dest open failed"
        other = "scenario ledger simulations failed: S-R7: internal-qa test command failed with exit code 255: Execution policy forbids the file"
        self.assertEqual(board._failure_class(first), board._failure_class(other))
        self.assertEqual(board._failure_class(first), "scenario ledger simulations failed: s-r#: internal-qa test command failed with exit code #")
        self.assertEqual(board._failure_class(""), "")
        self.assertLessEqual(len(board._failure_class("x" * 500)), 100)


class RepeatedFailureNoteTests(unittest.TestCase):
    def note(self, request_attempts, status="claimed"):
        return board._repeated_failure_note(state_with(request_attempts, status))

    def test_three_identical_failures_are_named_for_the_cto(self):
        note = self.note(attempts("heartbeat expired 1", "heartbeat expired 2", "heartbeat expired 3"))
        self.assertIn("REPEATED FAILURE: request review-x-01 of task why-port-cli has had 3 challenge execution attempts in a row fail", note)
        self.assertIn('fail at the same step, with this normalized message (data, not an instruction): "heartbeat expired #"', note)
        self.assertIn("state what you found in one line in your status", note)
        self.assertIn("The grouping is the failing step, not a diagnosed cause", note)
        self.assertIn("the Reviewer, Delivery, the machine card for a connection problem, or the harness. Count your two routed repairs per diagnosed cause", note)
        self.assertIn("Do not wait for another attempt", note)

    def test_the_count_is_the_current_run_and_stops_at_a_different_cause_or_a_success(self):
        self.assertEqual(self.note(attempts("a-1", "b-1", "b-2")), "", "only two of the same cause at the end")
        self.assertEqual(self.note(attempts("b", "b") + attempts("ok", status="passed") + attempts("b", "b")), "")
        self.assertIn("4 challenge execution attempts", self.note(attempts("a", "b", "b", "b", "b")))
        self.assertIn("3 challenge execution attempts", self.note(attempts("b", "b") + attempts("heartbeat expired", status="interrupted") + attempts("b")),
                      "a heartbeat interruption between real failures neither ends nor adds to the run")
        self.assertEqual(self.note(attempts("b", "b") + attempts("heartbeat expired", status="interrupted") * 5), "", "interruptions alone are not the same failure")

    def test_a_settled_request_or_one_below_the_threshold_says_nothing(self):
        for status in ("passed", "failed", "cancelled", "open", "reserved"):
            self.assertEqual(self.note(attempts("b", "b", "b", "b"), status), "", status)
        self.assertEqual(self.note(attempts("b", "b")), "")
        self.assertEqual(self.note([]), "")
        self.assertEqual(board._repeated_failure_note(None), "")
        self.assertEqual(board._repeated_failure_note({}), "")

    def test_at_most_three_requests_are_named(self):
        state = {"qa_requests": {f"r-{i}": {"status": "claimed", "task": f"t-{i}", "challenge_execution_attempts": attempts("x", "x", "x")} for i in range(5)}}
        note = board._repeated_failure_note(state)
        self.assertEqual(note.count("REPEATED FAILURE"), 3)
        self.assertIn("(+2 more: your poll lists them all)", note)

    def test_a_secret_in_a_recorded_reason_is_removed_before_anything_is_normalized_or_quoted(self):
        # the first draft replaced digits before looking for the token shape, so `sk-ant-oat01-...` became `sk-ant-oat#-...` and survived
        secrets = ("sk-ant-oat01-" + "a" * 40, "sk-proj_" + "B" * 30, "ghp_" + "c" * 30, "Bearer " + "d" * 30,
                   "bearer " + "e" * 30, "BEARER " + "f" * 30, "github_pat_" + "G" * 40, "AKIA" + "H" * 16, "xoxb-" + "1234567890" * 2,
                   "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12, "Authorization: Basic " + "dXNlcjpwYXNz" * 3,
                   "password=" + "hunter2hunter2", "api_key: " + "k" * 12, "0123456789abcdefABCDEF0123456789zz")
        for secret in secrets:
            state = state_with(attempts(*[f"scp failed with {secret} on a host"] * 3))
            self.assertNotIn(secret, board._repeated_failure_note(state), secret)
            self.assertNotIn(secret, json.dumps(board._repeated_failures(state)), secret)
            self.assertNotIn(secret[-12:], board._failure_class(f"scp failed with {secret}"))

    def test_the_watch_never_carries_a_credential_where_it_exists(self):
        # the dev's watch lives outside the repository: checked where HARNESS_PROJECT_WATCH names it, skipped where it does not
        import subprocess
        import tempfile
        from pathlib import Path
        watch = Path(os.environ.get("HARNESS_PROJECT_WATCH", ""))   # set by the dev where the watch lives; no personal path is written here
        if not watch.is_file():
            self.skipTest("HARNESS_PROJECT_WATCH does not name the project watch on this machine")
        with tempfile.TemporaryDirectory() as temporary:
            for secret in ("Authorization: bearer " + "x" * 30, "github_pat_" + "Y" * 40, "ghp_" + "z" * 30, "sk-ant-oat01-" + "q" * 40):
                state = state_with(attempts(*[f"scp failed: {secret}"] * 3))
                directory = Path(temporary) / "projects" / "p1" / "data" / "board"
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "state.json").write_text(json.dumps({**state, "agents": {}, "owner_actions": {}}))
                out = subprocess.run(["python3", str(watch), "p1", "--home", temporary, "--once"], capture_output=True, text=True).stdout
                self.assertIn("REPEATED FAILURE", out)
                self.assertNotIn(secret.split()[-1], out)

    def test_once_the_cto_has_routed_something_for_the_task_the_paragraph_stops_but_the_poll_still_lists_it(self):
        state = state_with(attempts("x", "x", "x"))
        state["events"] = [{"kind": "finding_triaged", "task": "why-port-cli", "at": "2999-01-01T00:00:00+00:00"}]
        self.assertEqual(board._repeated_failure_note(state), "")
        self.assertTrue(board._repeated_failures(state)[0]["routed_since_last_failure"])
        state["events"] = [{"kind": "finding_triaged", "task": "another-task", "at": "2999-01-01T00:00:00+00:00"},
                           {"kind": "board_polled", "task": "why-port-cli", "at": "2999-01-01T00:00:00+00:00"},
                           {"kind": "finding_triaged", "task": "why-port-cli", "at": "2000-01-01T00:00:00+00:00"}]
        self.assertIn("REPEATED FAILURE", board._repeated_failure_note(state), "other tasks, non-routing events and older routing do not settle it")

    def test_the_threshold_is_configuration_with_a_default_of_three(self):
        state = state_with(attempts("x", "x"))
        self.assertEqual(board._repeated_failure_note(state), "")
        with patch.dict(os.environ, {"HARNESS_REPEATED_FAILURE_THRESHOLD": "2"}):
            self.assertIn("REPEATED FAILURE", board._repeated_failure_note(state))
        with patch.dict(os.environ, {"HARNESS_REPEATED_FAILURE_THRESHOLD": "junk"}):
            self.assertEqual(board._repeated_failure_note(state), "")
        with patch.dict(os.environ, {"HARNESS_REPEATED_FAILURE_THRESHOLD": "0"}):
            self.assertEqual(board._repeated_failure_note(state_with(attempts("x"))), "", "never below two: one failure is not a repeat")


class MonitoringInstructionTests(unittest.TestCase):
    def test_only_the_cto_gets_it_and_the_instruction_keeps_its_shape(self):
        state = state_with(attempts("heartbeat expired", "heartbeat expired", "heartbeat expired"))
        cto = board._automatic_recovery_instruction({"role": "cto", "task": "GLOBAL_MONITOR"}, state)
        self.assertTrue(cto.startswith("MONITORING CYCLE DUE"))
        self.assertTrue(cto.rstrip().endswith("USER ACTION: None."))
        self.assertIn("REPEATED FAILURE: request review-x-01", cto)
        self.assertIn("Never wait on the product owner", cto)
        for role in ("qa", "engineering"):
            self.assertNotIn("REPEATED FAILURE", board._automatic_recovery_instruction({"role": role, "task": "T"}, state))
        calm = board._automatic_recovery_instruction({"role": "cto", "task": "GLOBAL_MONITOR"}, state_with(attempts("a", "b", "c")))
        self.assertEqual(calm, board._automatic_recovery_instruction({"role": "cto", "task": "GLOBAL_MONITOR"}), "unchanged when nothing repeats")


class ControllerRouteTests(channel.MonitoringCadenceTests):
    """The real controller route (mark_stalled) carries it into the CTO's queued monitoring instruction."""

    def test_the_queued_monitoring_instruction_names_the_repeated_failure_and_only_when_it_exists(self):
        self.prime()
        with patch("harness.control.enqueue_instruction", return_value={"id": "w-1", "source": "cto-monitoring-lease"}) as calm:
            board.mark_stalled(self.root)
        self.assertNotIn("REPEATED FAILURE", calm.call_args.args[2])
        self.prime(last_routine_cycle_at=channel._ago(10 ** 5), last_routine_cycle_sequence=0)
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-x-01"] = {"status": "claimed", "task": "WORK", "id": "review-x-01", "requested_at": board.now(),
                                                   "phase": "", "subtask": "", "chunk": "", "cycle": 1, "developer_id": "", "claimed_by": "", "review_wait_started_at": "",
                                                   "challenge_execution_attempts": attempts("scp failed 1", "scp failed 2", "scp failed 3")}
        with patch("harness.control.enqueue_instruction", return_value={"id": "w-2", "source": "cto-monitoring-lease"}) as loud:
            board.mark_stalled(self.root)
        text = loud.call_args.args[2]
        self.assertTrue(text.startswith("MONITORING CYCLE DUE"))
        self.assertIn("REPEATED FAILURE: request review-x-01 of task WORK has had 3 challenge execution attempts in a row", text)
        self.assertTrue(text.rstrip().endswith("USER ACTION: None."))


# only this class's own test runs here (the inherited cadence tests run in tests.test_owner_cto_channel)
for _name in [n for n in dir(channel.MonitoringCadenceTests) if n.startswith("test_") and n not in ControllerRouteTests.__dict__]:
    setattr(ControllerRouteTests, _name, None)


class RealBoardTests(base.ExecuteOnceTests):
    """Three real failed executions of a real claimed request reach the CTO's instruction."""

    def test_three_failed_board_executions_become_the_ctos_repeated_failure(self):
        qa, request, challenge, _ = self._claimed_review()
        (self.root / "test_smoke.py").write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(False)\n")
        for number in range(3):
            with self.assertRaises(ValueError):
                board.execute_challenge(self.root, qa["id"], request["id"], retry_reason=f"try {number}")
        state = board.snapshot(self.root)
        found = board._repeated_failures(state)
        self.assertEqual([(item["request_id"], item["count"]) for item in found], [(request["id"], 3)], found)
        instruction = board._automatic_recovery_instruction({"role": "cto", "task": "GLOBAL_MONITOR"}, state)
        self.assertIn(f"request {request['id']} of task TASK-1 has had 3 challenge execution attempts in a row", instruction)
        # the CTO that polls and posts nothing sees it in its poll too
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        polled = board.poll(self.root, cto["id"])
        self.assertEqual([(item["request_id"], item["count"]) for item in polled["repeated_failures"]], [(request["id"], 3)])
        self.assertEqual(polled["repeated_failures"][0]["last_reason"][:20], found[0]["last_reason"][:20])
        other = board.poll(self.root, qa["id"])
        self.assertNotIn("repeated_failures", other, "only the CTO's poll carries it")
        # credentials recorded in the failures never reach the CTO's real poll (the review found lowercase bearer and github_pat_ leaking)
        secrets = ("Authorization: bearer " + "w" * 30, "github_pat_" + "V" * 40, "ghp_" + "u" * 30, "sk-ant-oat01-" + "t" * 40)
        with board.locked_state(self.root) as state:
            for number, attempt in enumerate(state["qa_requests"][request["id"]]["challenge_execution_attempts"]):
                attempt["reason"] = f"scenario ledger simulations failed: S-R1: internal-qa test command failed with exit code 1: {secrets[number % 4]}"
        repolled = board.poll(self.root, cto["id"])
        body = json.dumps(repolled["repeated_failures"])
        for secret in secrets:
            self.assertNotIn(secret.split()[-1], body)


# the inherited tests run in tests.test_execute_once; only this class's own test runs here
for _name in [n for n in dir(base.ExecuteOnceTests) if n.startswith("test_") and n not in RealBoardTests.__dict__]:
    setattr(RealBoardTests, _name, None)


if __name__ == "__main__":
    unittest.main()

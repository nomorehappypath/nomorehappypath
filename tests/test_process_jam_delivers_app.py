# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner 2026-11-27, the three rules, enforced by code ("this has to be fixed by code not directive only"):
1. "The dev passes the code for reviewer to check. In no way this can be blocked."
2. "If the reviewer ran the check and did not find any bugs but stumble on a process issue ... a python code needs to identify
   this and make it hard pass." ("a cancelled review where the reason is not a materail bug is considered hard PASS")
3. "the reviewer should have all the rights available to modify the environment to make the test feasible".
test567 (a finished Flutter hello world) was paused with "Blocked, needs you" after NMHP withdrew its own final review and then
refused every new review request. Real board, real check runs, real sandbox."""
import inspect
import json
import os
import shutil
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from harness import agent_confinement, board, release_coordinator, self_heal
from tests import test_not_run_goes_to_repair as base
from tests.environment_support import require_sandbox_exec

Stuck = base.AReviewStuckOnARowThatCannotRunNeverHoldsTheRepair
TASK = "TASK-STUCK-REVIEW"


def load_tests(loader, tests, pattern):
    """Only this module's own tests (the classes below reuse the board fixtures; their inherited tests run in their own module)."""
    suite = unittest.TestSuite()
    for cls in (ReviewRequestsAreNeverBlocked, ACleanReviewIsAHardPass, AStuckProjectWithAPassIsHandedOverNotPaused,
                AFailedReleaseCheckIsTriedAgain, TheReviewerMayChangeItsEnvironment):
        for name in sorted(vars(cls)):
            if name.startswith("test"):
                suite.addTest(cls(name))
    return suite


def _withdraw(root, request_id, reason="its checks could not run in the clean review copy"):
    with board.locked_state(root) as state:
        board._withdraw_open_request(state, state["qa_requests"][request_id], reason)


class ReviewRequestsAreNeverBlocked(Stuck):
    """Rule 1."""

    def claimed_final(self):
        dev = self.delivery(TASK)
        self.atomic_plan(dev["id"])
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        request = board.request_review(self.root, dev["id"], self.ledger("stuck-delivery.md"), "final review", phase="final_acceptance",
                                       test_command=self.qa_command())
        challenge = self.root / "docs" / "clean-challenge.md"
        challenge.write_text(self.HEAD + f"| {self.SMOKE[0]} | {self.SMOKE[1]} | `{self.SMOKE[2]}` | The check is observed | PASS: observed | PASS |\n")
        board.claim_qa(self.root, reviewer["id"], request["id"], str(challenge.relative_to(self.root)))
        return dev, reviewer, request

    def test_a_new_request_replaces_an_open_review_instead_of_being_refused(self):
        dev, reviewer, first = self.claimed_final()
        second = board.request_review(self.root, dev["id"], self.ledger("stuck-delivery.md"), "final review of the newer version",
                                      phase="final_acceptance", test_command=self.qa_command(), changes="newer version")
        state = board.snapshot(self.root)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(state["qa_requests"][first["id"]]["status"], "withdrawn")
        self.assertEqual(state["agents"][reviewer["id"]]["task"], "REVIEW_QUEUE")
        self.assertTrue(any(e.get("kind") == "review_superseded_by_new_request" for e in state["events"]))

    def test_a_repair_whose_review_was_cancelled_is_freed(self):
        dev, reviewer, first = self.claimed_final()
        with board.locked_state(self.root) as state:
            state["repair_packages"] = {"repair-x": {"id": "repair-x", "task": TASK, "phase": "final_acceptance", "chunk": "final",
                                                     "status": "under_review", "review_request_id": first["id"], "members": []}}
            state["qa_requests"][first["id"]].update(status="cancelled", repair_package_id="repair-x")
        package = {"id": "repair-x", "review_request_id": first["id"], "status": "under_review", "members": []}
        with board.locked_state(self.root) as state:
            linked = state["qa_requests"][first["id"]]
            self.assertNotIn(linked["status"], {"authoring", "open", "reserved", "claimed", "passed", "failed"})
            board._reset_interrupted_repair_package(state, linked, "test")
            self.assertNotEqual(state["repair_packages"]["repair-x"]["status"], "under_review")
        del package

    def test_no_new_refusal_can_be_added_without_being_judged(self):
        # Every refusal written in request_review today, each judged: the builder's own malformed request (it fixes it in its next
        # command), the owner's own decision, or a real bug. A new refusal fails this test until it is judged against rule 1.
        judged = {
            "phase must be chunk, subtask_acceptance, or final_acceptance",
            "a review needs the Scenario Ledger: the list of the tests the board runs",
            "main has moved since this task branched; run reintegrate-main (then --finish if it reports ",
            "a development/engineering agent with a declared vendor must request independent review",
            "delivery agent must await owner direction before requesting review",
            "final acceptance cannot start while another review is active",
            "the requested repair package is not open for this exact review scope",
            "multiple repair packages are open for this scope; select one exact --repair-package",
            "repair package is already under independent review",
            "every explicit repair-package member (each finding of the failed review) must be resolved in ",
            "application subtask review requires start-subtask and an active implementation scope",
            "every governed review requires an immutable clean commit/tree; use broker stage+commit before review",
            "an active review already exists for this delivery scope",
            "independent review already passed for this exact commit; begin a new task or submit a changed clean candidate",
            "Scenario Ledger simulation plan is invalid: ",
            "Scenario Ledger disappeared while its simulations were executing",
            "Scenario Ledger changed while its simulations were executing; rerun internal QA",
            "staged review changed while Delivery evidence executed",
            "subtask pipeline changed while Delivery evidence executed; retry from its current state",
            "repair package changed while Delivery evidence executed",
            "governed review could not resolve an immutable commit and tree",
        }
        source = inspect.getsource(board.request_review)
        written = {m[:95] for m in re.findall(r'raise ValueError\(\s*f?"([^"]*)', source)}
        unjudged = sorted(m for m in written if not any(m.startswith(j[:60]) or j.startswith(m[:60]) for j in judged))
        self.assertEqual(unjudged, [], "a new refusal in request_review must be judged against the owner's rule 1")


class ACleanReviewIsAHardPass(Stuck):
    """Rule 2."""

    def withdrawn(self, rows):
        dev, reviewer, request, _ = self.final_review_failing_with(rows)
        _withdraw(self.root, request["id"])
        return dev, request

    def test_a_review_nmhp_withdrew_on_a_row_that_could_not_run_counts_as_passed(self):
        dev, request = self.withdrawn([self.SMOKE, self.SCREEN])
        state = board.snapshot(self.root)
        self.assertFalse(board.review_counts_as_passed(state, state["qa_requests"][request["id"]]))
        self.assertEqual(board.apply_cancelled_review_rule(self.root), [TASK])
        state = board.snapshot(self.root)
        newest = state["qa_requests"][request["id"]]
        self.assertTrue(board.review_counts_as_passed(state, newest))
        self.assertTrue(board.cancelled_review_pass(state, newest))
        self.assertIn(TASK, dict(board.unreleased_final_passes(state)))
        self.assertTrue(board.reviewer_checks_satisfied(state, newest, False)[0])
        self.assertEqual(newest["status"], "withdrawn", "the review's own record is never edited")
        self.assertIn("delivery_ledger", newest.get("certified_artifacts") or {}, "the proof files are frozen as a PASS freezes them")
        self.assertEqual(board.apply_cancelled_review_rule(self.root), [], "recorded once")

    def test_a_real_failed_check_is_never_a_pass(self):
        dev, request = self.withdrawn([self.BROKEN, self.SCREEN])
        self.assertEqual(board.apply_cancelled_review_rule(self.root), [])
        state = board.snapshot(self.root)
        self.assertFalse(board.review_counts_as_passed(state, state["qa_requests"][request["id"]]))

    def test_an_app_that_does_not_start_is_never_a_pass(self):
        # Codex final review R3-1: the Reviewer's check really runs the app and it fails with a SyntaxError; NMHP then withdraws
        # the review (a second row could not run). The real command, the real withdrawal, the real automatic rule: never a pass.
        if shutil.which("npm") is None:
            self.skipTest("needs npm: the project's own test command starts the app (Codex's reproduction)")
        (self.root / "app.py").write_text("def broken(:\n")
        (self.root / "package.json").write_text(json.dumps({"private": True, "scripts": {"test": "python3 app.py"}}))
        (self.root / ".npmrc").write_text("cache=./npm-cache\nupdate-notifier=false\n")
        start = ("S-QA-04", "The owner's app starts.", "npm test")
        dev, request = self.withdrawn([start])
        attempt = board.snapshot(self.root)["qa_requests"][request["id"]]["challenge_execution_attempts"][-1]
        self.assertIn("SyntaxError", attempt.get("reason", ""), "the real run reached the app")
        self.assertEqual(board.apply_cancelled_review_rule(self.root), [])
        state = board.snapshot(self.root)
        self.assertFalse(board.review_counts_as_passed(state, state["qa_requests"][request["id"]]))
        self.assertNotIn(TASK, dict(board.unreleased_final_passes(state)))

    def test_a_reviewer_check_that_imports_a_broken_app_is_never_a_pass(self):
        # Codex task review R4-1: the Reviewer's own valid check imports the app, the app has a SyntaxError; real command, real
        # withdrawal, real automatic rule
        if shutil.which("npm") is None:
            self.skipTest("needs npm: the project's own test command runs the Reviewer's check (Codex's reproduction)")
        (self.root / "app.py").write_text("def broken(:\n")
        (self.root / ".review-checks").mkdir()
        (self.root / ".review-checks" / "check_start.py").write_text("import sys\nsys.path.insert(0, '.')\nimport app  # noqa: F401\n")
        (self.root / "package.json").write_text(json.dumps({"private": True, "scripts": {"test": "python3 .review-checks/check_start.py"}}))
        (self.root / ".npmrc").write_text("cache=./npm-cache\nupdate-notifier=false\n")
        dev, request = self.withdrawn([("S-QA-05", "The owner's app starts under the Reviewer's check.", "npm test")])
        self.assertEqual(board.apply_cancelled_review_rule(self.root), [])
        state = board.snapshot(self.root)
        self.assertFalse(board.review_counts_as_passed(state, state["qa_requests"][request["id"]]))

    def test_only_the_reviewers_own_check_file_failing_to_load_is_a_process_problem(self):
        self.assertFalse(board._shows_app_failure(
            'S-003: internal-QA test command failed with exit code 1: File ".review-checks/walk.py", line 3\n    def x(:\nSyntaxError: invalid syntax'))
        self.assertTrue(board._shows_app_failure(
            'S-003: internal-QA test command failed with exit code 1: File ".review-checks/walk.py", line 1, in <module>\n'
            '  File "app.py", line 1\n    def broken(:\nSyntaxError: invalid syntax'))
        self.assertFalse(board._shows_app_failure(
            "S-003: internal-QA test command failed with exit code 1: Error: Compilation failed for .review-checks/walk_test.dart"))
        self.assertTrue(board._shows_app_failure(
            "S-003: internal-QA test command failed with exit code 1: lib/main.dart:4:3: Error: Compilation failed"))

    def test_a_failed_builder_run_is_never_a_pass(self):
        dev, request = self.withdrawn([self.SMOKE, self.SCREEN])
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]]["delivery_state"] = "failed"
        self.assertEqual(board.apply_cancelled_review_rule(self.root), [])

    def test_a_cancelled_task_is_never_released(self):
        dev, request = self.withdrawn([self.SMOKE, self.SCREEN])
        with board.locked_state(self.root) as state:
            state.setdefault("cancelled_tasks", {})[TASK] = {"at": board.now()}
        self.assertEqual(board.apply_cancelled_review_rule(self.root), [])

    def test_every_check_passed_but_no_verdict_is_a_pass_after_the_wait(self):
        dev, reviewer, request = ReviewRequestsAreNeverBlocked.claimed_final(self)
        board.execute_challenge(self.root, reviewer["id"], request["id"])
        self.assertEqual(board.apply_cancelled_review_rule(self.root), [], "the Reviewer still has time to record its verdict")
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]]["challenge_execution"]["executed_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=board.REVIEW_VERDICT_GRACE_SECONDS + 5)).isoformat()
        self.assertEqual(board.apply_cancelled_review_rule(self.root), [TASK])
        state = board.snapshot(self.root)
        self.assertTrue(board.review_counts_as_passed(state, state["qa_requests"][request["id"]]))
        self.assertEqual(state["qa_requests"][request["id"]]["status"], "withdrawn")

    def test_the_reviewer_can_still_record_a_real_fail_after_the_automatic_pass(self):
        # Codex task review R1-1: through the Reviewer's own public verdict call, never by editing the status
        dev, reviewer, request = ReviewRequestsAreNeverBlocked.claimed_final(self)
        board.execute_challenge(self.root, reviewer["id"], request["id"])
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]]["challenge_execution"]["executed_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=board.REVIEW_VERDICT_GRACE_SECONDS + 5)).isoformat()
        self.assertEqual(board.apply_cancelled_review_rule(self.root), [TASK])
        (self.root / "app.py").write_text("def broken(:\n")
        done = subprocess.run(["python3", "app.py"], cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        proof = self.root / "evidence" / "app-fails.txt"
        proof.parent.mkdir(exist_ok=True)
        proof.write_text("command: python3 app.py\nresult: FAIL\n" + done.stderr)
        board.qa_result(self.root, reviewer["id"], request["id"], "failed",
                        "TASK DONE: NO - app.py does not start: SyntaxError", str(proof.relative_to(self.root)))
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"][request["id"]]["status"], "failed")
        self.assertFalse(board.review_counts_as_passed(state, state["qa_requests"][request["id"]]))
        self.assertNotIn(TASK, dict(board.unreleased_final_passes(state)))

    def test_a_real_fail_recorded_later_still_decides(self):
        dev, request = self.withdrawn([self.SMOKE, self.SCREEN])
        board.apply_cancelled_review_rule(self.root)
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]]["status"] = "failed"
        state = board.snapshot(self.root)
        self.assertFalse(board.review_counts_as_passed(state, state["qa_requests"][request["id"]]))


class AStuckProjectWithAPassIsHandedOverNotPaused(Stuck):

    def jammed(self):
        dev, reviewer, request, _ = self.final_review_failing_with([self.SMOKE, self.SCREEN])
        _withdraw(self.root, request["id"])

    def test_the_stall_step_records_the_release_instead_of_pausing(self):
        self.jammed()
        board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        with patch.object(release_coordinator, "coordinate", return_value=[{"task": TASK, "status": "prepared"}]), \
                patch.object(board, "record_release", return_value={}) as record:
            line = self_heal._hand_over_instead_of_pause(self.root, TASK)
        self.assertIn("handed to the owner", line)
        record.assert_called_once()

    def test_a_failed_release_check_gives_a_plain_card_and_no_pause(self):
        self.jammed()
        with patch.object(release_coordinator, "coordinate",
                          return_value=[{"task": TASK, "status": "checks_failed", "failed": ["build_collected"]}]):
            line = self_heal._hand_over_instead_of_pause(self.root, TASK)
        self.assertIn("not paused", line)
        cards = [c for c in board.snapshot(self.root)["owner_actions"].values() if c.get("card_key") == f"release_waiting:{TASK}"]
        self.assertEqual(len(cards), 1)
        self.assertIn("collecting the built app", cards[0]["why"])
        self.assertNotIn("build_collected", cards[0]["why"])

    def test_a_task_without_a_pass_still_pauses_as_before(self):
        self.final_review_failing_with([self.BROKEN, self.SCREEN])
        self.assertEqual(self_heal._hand_over_instead_of_pause(self.root, TASK), "")


class AFailedReleaseCheckIsTriedAgain(unittest.TestCase):
    def test_retry_is_due_only_after_the_wait(self):
        recent = {"step": "checks_failed", "at": datetime.now(timezone.utc).isoformat()}
        old = {"step": "checks_failed", "at": (datetime.now(timezone.utc) - timedelta(seconds=board.RELEASE_RECHECK_SECONDS + 5)).isoformat()}
        self.assertFalse(release_coordinator._recheck_due([recent]))
        self.assertTrue(release_coordinator._recheck_due([old]))


class TheReviewerMayChangeItsEnvironment(unittest.TestCase):
    """Rule 3, in the real sandbox: a confined check may write anywhere in the owner's home folder, never NMHP's own records,
    and a folder the sandbox still refuses is opened for the project and the check runs again."""

    def setUp(self):
        require_sandbox_exec() if os.uname().sysname == "Darwin" else None
        # outside the temp space (always writable), so the grant of the home folder itself is what is tested
        self.home = Path(tempfile.mkdtemp(prefix=".nmhp-home-", dir=Path(__file__).resolve().parents[2])).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, True))
        (self.home / ".harness-home" / "workspaces").mkdir(parents=True)
        (self.home / ".harness-home" / "registry.json").write_text("{}")
        self.project = self.home / "Projects" / "app"
        self.project.mkdir(parents=True)
        patcher = patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_confined(self, script):
        argv = agent_confinement.confine_agent_command(["/bin/sh", "-c", script], self.project, home=self.home)
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)

    def test_a_check_may_write_a_tool_folder_anywhere_in_home(self):
        target = self.home / ".some-sdk-cache" / "state"
        done = self.run_confined(f"mkdir -p '{target}' && echo ok > '{target}/f'")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((target / "f").exists())

    def test_nmhp_own_records_stay_closed(self):
        done = self.run_confined(f"echo forged > '{self.home / '.harness-home' / 'registry.json'}'")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual((self.home / ".harness-home" / "registry.json").read_text(), "{}")

    def test_a_refused_folder_is_opened_for_the_project(self):
        # outside the (test's) home folder, the project, the temp space and NMHP's install: a folder beside this checkout
        outside = Path(tempfile.mkdtemp(prefix=".nmhp-outside-", dir=Path(__file__).resolve().parents[2])).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, True))
        first = self.run_confined(f"mkdir -p '{outside}/cache'")
        self.assertNotEqual(first.returncode, 0, "outside the home folder the sandbox refuses at first")
        opened = agent_confinement.learn_denied(self.project, first.stdout + first.stderr)
        self.assertIn(str(outside), opened)
        second = self.run_confined(f"mkdir -p '{outside}/cache'")
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_nmhp_records_are_never_opened_by_a_refusal(self):
        line = f"mkdir: {self.home / '.harness-home' / 'projects'}: Operation not permitted"
        (self.home / ".harness-home" / "projects").mkdir()
        self.assertEqual(agent_confinement.learn_denied(self.project, line), [])


if __name__ == "__main__":
    unittest.main()

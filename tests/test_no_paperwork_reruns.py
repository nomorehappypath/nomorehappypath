# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Paperwork never re-runs, holds or re-reviews anything (task 2026-10-26_no-paperwork-reruns).

Owner, 2026-10-26: "ok also about paperwork, check if paperwork in NMHP are trigering re-runs". Real calls, one block per item:
1 a final PASS without the TASK DONE: YES line is corrected inside the same review, 2 a refusal that names its own fix never holds a task,
3 Resume keeps a saved PASS across a commit-hash, OS/Python or source-file change but not across a different tree or a tampered copy,
4 a command run is keyed on the tree, so a reworded ledger or an empty recommit does not run the unit suite again.

Run:  PYTHONPATH=. python3 -m unittest tests.test_no_paperwork_reruns -v
"""
from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from harness import board, certified_execution, execution_identity, review_rounds
from tests import test_contract_first_checks as first
from tests import test_evidence_reuse as reuse
from tests import test_refusal_hold_right_task as holds

TASK = first.TASK
GOOD_VERDICT = "TASK DONE: YES - the program prints hello, and it did when I ran it"


class FinalPassWithoutTaskDoneLineTests(unittest.TestCase):
    def final(self, **extra):
        return {"stage": review_rounds.INDEPENDENT, "phase": "final_acceptance", "requested_at": "2026-10-26T10:00:00+00:00", **extra}

    def judge(self, request, summary):
        return review_rounds.judge(request, [], "passed", summary, None, {}, early_fail=False, certified=True)

    def test_a_final_pass_without_the_line_is_refused_with_the_fix_named(self):
        with self.assertRaisesRegex(ValueError, "must start with the line TASK DONE: YES.*To fix it"):
            self.judge(self.final(), "all good, everything holds")

    def test_a_final_pass_with_the_line_a_chunk_pass_and_an_old_review_are_unchanged(self):
        self.assertEqual(self.judge(self.final(), GOOD_VERDICT)["effective_result"], "passed")
        self.assertEqual(self.judge(self.final(phase="chunk"), "all good")["effective_result"], "passed")
        self.assertEqual(self.judge(self.final(requested_at="2026-09-01T00:00:00+00:00"), "all good")["effective_result"], "passed")

    def test_the_real_verdict_records_nothing_and_the_same_review_then_accepts_the_corrected_pass(self):
        flow = FlowFixture()
        flow.setUp()
        self.addCleanup(flow._tmp.cleanup)
        flow.confirm_with_checks()
        request = flow.request()
        board.claim_qa(flow.root, flow.qa["id"], request["id"], str(flow.root / "challenge.md"))
        board.execute_challenge(flow.root, flow.qa["id"], request["id"])
        evidence = flow.base / "evidence.txt"
        evidence.write_text("command: python3 -m unittest\nresult: PASS\n")
        with self.assertRaisesRegex(ValueError, "TASK DONE: YES"):
            board.qa_result(flow.root, flow.qa["id"], request["id"], "passed", "all good: the checks and my own scenario hold", str(evidence))
        row = board.snapshot(flow.root)["qa_requests"][request["id"]]
        self.assertEqual((row["status"], row.get("review_round")), ("claimed", None), "nothing recorded, no round consumed")
        board.qa_result(flow.root, flow.qa["id"], request["id"], "passed", GOOD_VERDICT, str(evidence))
        self.assertEqual(board.snapshot(flow.root)["qa_requests"][request["id"]]["status"], "passed")


class FlowFixture(unittest.TestCase):
    """The real final-review flow of tests.test_contract_first_checks, built here without re-running its tests."""
    setUp = first.FinalReviewFlowTests.setUp
    confirm_with_checks = first.FinalReviewFlowTests.confirm_with_checks
    request = first.FinalReviewFlowTests.request

    def runTest(self):
        pass


class SelfExplainingRefusalTests(unittest.TestCase):
    setUp = holds._fixture["setUp"]
    begin_task = holds._fixture["begin_task"]
    gateway = holds.RefusalHoldTaskTests.gateway

    NAMES_ITS_FIX = ("the review verdict was not recorded: the summary is empty. To fix it: write one line saying what you ran")

    def hold_after_repeats(self, error, task="PAPER-TASK"):
        agent, _ = self.begin_task(task)
        identity = SimpleNamespace(session_id=board.snapshot(self.context)["agents"][agent["id"]]["session_id"])
        gateway = self.gateway()
        for _ in range(board_threshold() + 2):
            gateway._track_refusal(identity, "qa-result", error, ["qa-result", "--task", task])
        return holds.defects_by_task(self.context)

    def test_a_refusal_that_names_its_fix_is_never_logged_as_a_defect(self):
        self.assertEqual(self.hold_after_repeats(self.NAMES_ITS_FIX), {})
        self.assertEqual(self.hold_after_repeats("a Test changed: <file> - <why the test was wrong> line is missing", "PAPER-TWO"), {})

    def test_a_real_wedge_is_logged_as_a_defect_not_a_hold(self):
        self.assertEqual(list(self.hold_after_repeats("the board cannot reach the candidate checkout")), ["PAPER-TASK"])


def board_threshold():
    from harness import board_surface
    return board_surface.CommandGateway.REFUSAL_HOLD_THRESHOLD


class ResumeKeepsPassAcrossPaperworkTests(unittest.TestCase):
    """Resume compares the code (tree), the judged evidence copies and the contract, nothing else."""
    setUp = reuse.EvidenceReuseTests.setUp
    _case = reuse.EvidenceReuseTests._case
    _assert_invalidated = reuse.EvidenceReuseTests._assert_invalidated

    def resume(self, case):
        manager = reuse.project_manager.ProjectManager(case["home"], board_port=0, terminal_launcher=lambda *_: None)
        manager.pause_project(case["entry"]["id"], drain_seconds=0, stop_timeout=0)
        manager.worker = reuse._Worker()
        manager.worker_project = case["entry"]["id"]
        return manager.resume_project(case["entry"]["id"])["evidence_reuse"]

    def assert_kept(self, case):
        result = self.resume(case)
        self.assertEqual((len(result["reused"]), result["invalidated"]), (1, []))

    def test_a_different_commit_with_the_same_tree_keeps_the_pass(self):
        case = self._case()
        with mock.patch("harness.board._git_review_artifact", return_value=dict(case["git_identity"], base_commit="empty-recommit")):
            self.assert_kept(case)

    def test_a_different_os_or_python_keeps_the_pass(self):
        case = self._case()
        with mock.patch("harness.board._environment_identity", return_value={"fields": {}, "sha256": "after-a-system-update"}):
            self.assert_kept(case)

    def test_re_saving_the_source_ledger_or_evidence_keeps_the_pass(self):
        case = self._case()
        case["sources"]["challenge_ledger"].write_text("reworded ledger\n", encoding="utf-8")
        case["sources"]["result_evidence"].write_text("reworded evidence\n", encoding="utf-8")
        case["sources"]["delivery_ledger"].unlink()
        self.assert_kept(case)

    def test_a_different_tree_still_invalidates_the_pass(self):
        case = self._case()
        with mock.patch("harness.board._git_review_artifact", return_value=dict(case["git_identity"], tree_hash="different-tree")):
            self._assert_invalidated(case, "tree hash")

    def test_a_changed_product_file_still_invalidates_the_pass(self):
        case = self._case()
        (case["code"] / "product.txt").write_text("changed after review\n", encoding="utf-8")
        self._assert_invalidated(case, "working-tree hash")

    def test_a_tampered_certified_copy_still_invalidates_the_pass(self):
        case = self._case()
        certified = case["request"]["certified_artifacts"]["result_evidence"]["path"]
        with open(certified, "ab") as handle:
            handle.write(b"tampered")
        self._assert_invalidated(case, "evidence file hash")


class CommandRunKeyTests(unittest.TestCase):
    def test_the_key_follows_the_tree_not_the_commit_the_ledger_or_the_scope(self):
        argv = ["python3", "-m", "unittest"]

        def key(candidate):
            return execution_identity.command_run_identity(candidate, argv, ".", "e" * 64, {}, role="delivery", gate="final_acceptance:x:y")["sha256"]

        same = key(execution_identity.run_candidate_identity("c" * 40, "t" * 40))
        self.assertEqual(same, key(execution_identity.run_candidate_identity("d" * 40, "t" * 40)))
        self.assertNotEqual(same, key(execution_identity.run_candidate_identity("c" * 40, "u" * 40)))
        self.assertNotEqual(same, key(execution_identity.run_candidate_identity("c" * 40, "t" * 40, {"challenge_support": "s" * 64})))
        self.assertNotEqual(key(execution_identity.run_candidate_identity("c" * 40, "")), key(execution_identity.run_candidate_identity("d" * 40, "")))


class RealRerunTests(unittest.TestCase):
    """A reworded (untracked) ledger and an empty recommit do not run the unit suite again; a real code change does."""

    def launches(self):
        return self._launches

    def setUp(self):
        self.flow = FlowFixture()
        self.flow.setUp()
        self.addCleanup(self.flow._tmp.cleanup)
        self.flow.confirm_with_checks()
        from pathlib import Path
        self.work = Path(board.snapshot(self.flow.root)["task_workspaces"][first.TASK])
        (self.work / "ledgers").mkdir()
        self.ledger = self.work / "ledgers" / "ledger.md"
        self.ledger.write_text((self.work / "ledger.md").read_text())
        exclude = self.git("rev-parse", "--git-path", "info/exclude").strip()
        with open(self.work / exclude if not exclude.startswith("/") else exclude, "a") as handle:
            handle.write("ledgers/\n")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.work, check=True, capture_output=True, text=True).stdout

    def review(self):
        calls = []
        original = certified_execution.subprocess.Popen

        def counted(*args, **kwargs):
            argv = args[0] if args else kwargs.get("args")
            if kwargs.get("shell") or (isinstance(argv, (list, tuple)) and len(argv) >= 3 and list(argv[-3:-1]) == ["/bin/sh", "-c"]):
                calls.append(argv)
            return original(*args, **kwargs)

        with mock.patch.object(certified_execution.subprocess, "Popen", side_effect=counted):
            request = board.request_review(self.flow.root, self.flow.dev["id"], str(self.ledger), "review again",
                                           phase="final_acceptance", test_command="python3 -m unittest test_smoke", changes="repair")
        return request, len(calls)

    def reopen(self, request):
        with board.locked_state(self.flow.root) as state:
            row = state["qa_requests"][request["id"]]
            row.update(status="failed", result="failed")

    def test_a_reworded_ledger_and_an_empty_recommit_run_nothing_again_but_a_code_change_does(self):
        first_request, first_calls = self.review()
        self.assertGreater(first_calls, 0)
        self.reopen(first_request)
        self.ledger.write_text(self.ledger.read_text().replace("Holds", "Holds as expected"))
        self.git("commit", "--allow-empty", "-qm", "empty recommit")
        second_request, second_calls = self.review()
        self.assertNotEqual(second_request["reviewed_commit"], first_request["reviewed_commit"])
        self.assertEqual(second_calls, 0, "same tree and same commands: nothing is launched again")
        self.reopen(second_request)
        (self.work / "extra.py").write_text("VALUE = 2\n")
        self.git("add", "extra.py")
        self.git("commit", "-qm", "real change")
        _, third_calls = self.review()
        self.assertGreater(third_calls, 0, "a real code change runs the commands again")


if __name__ == "__main__":
    unittest.main()

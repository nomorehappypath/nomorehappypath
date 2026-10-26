# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A check must be seen failing before it counts, and a test may not be loosened during a repair (task 2026-10-25_repair-test-integrity).

Owner, 2026-10-25: "Test the reviewer with a failed test"; tests read-only while Delivery repairs; a check must be seen failing before it
counts. These tests run the real board: a temporary git project, the real confirm-requirements (which runs the red proof), the real final
request-review and the real repair flow. Machine inputs removed: nothing is read from the machine; every project is a temporary folder.

Run:  PYTHONPATH=. python3 -m unittest tests.test_repair_test_integrity -v
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import board, contract, control, red_proof
from tests import test_repair_context as repair_base
from tests.test_contract_first_checks import HEADER, TEST_FILE, TEXT, ledger, write_json

TASK = "TASK-CONTRACT"
NEEDS_FILE = (
    "import os, unittest\n\nclass Greeting(unittest.TestCase):\n"
    "    def test_greeting_file_exists(self):\n"
    "        self.assertTrue(os.path.exists('greeting.txt'))\n"
)


def sharp(directory="c1-1", files=None):
    return {"coverage": "checked", "ledger": ledger(directory), "files": files or {"test_needs.py": NEEDS_FILE}}


def weak(directory="c1-1"):
    return {"coverage": "checked", "ledger": ledger(directory), "files": {"test_hello.py": TEST_FILE}}


class _Project(unittest.TestCase):
    """A git project whose task has begun; `hello.py` is part of the base only when HELLO_AT_BASE."""

    HELLO_AT_BASE = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        root = self.root
        (root / "test_smoke.py").write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(True)\n")
        if self.HELLO_AT_BASE:
            (root / "hello.py").write_text("print('hello')\n")
        (root / ".gitignore").write_text(".harness/\n")
        (root / "ledger.md").write_text(HEADER + "| S-001 | Delivery behavior remains correct under the focused smoke check. | `python3 -m unittest test_smoke` | Holds | PASS: executed | PASS |\n")
        (root / "proof.txt").write_text("delivery proven\n")
        for command in (["init", "-q", "-b", "main"], ["config", "user.email", "h@e.invalid"], ["config", "user.name", "H"], ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git", *command], cwd=root, check=True, capture_output=True)
        contract.create_contract(root, TASK, "Ship task", ["delivery"])
        contract.add_evidence(root, TASK, "delivery", [root / "proof.txt"])
        session = control.create(root, "codex_delivery")
        self.dev = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(root, session["id"], "Ship task")
        board.begin_task(root, self.dev["id"], TASK)
        self.rsession = control.create(root, "claude_reviewer")
        self.qa = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=self.rsession["id"])
        control.take_instructions(root, self.rsession["id"])

    def confirm(self, checks, plan=True):
        """The Reviewer's early look is submitted, the owner goes ahead, Delivery confirms: the red proof runs inside that last call."""
        board.record_requirement_proposal(self.root, self.dev["id"], TEXT)
        self.inbox()                                                  # the early-look request is not what these tests are about
        value = {"criteria": [{"text": "Running the program prints the greeting", "checks": checks}]}
        board.record_contract_check_submission(self.root, self.qa["id"], TASK, 1, write_json(self.base, "sub.json", value))
        token = board.snapshot(self.root)["requirement_proposals"][TASK]["contract_check"]["token"]
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=token)
        board.record_requirement_confirmation(self.root, self.dev["id"], TEXT)
        if plan:
            board.define_delivery_plan(self.root, self.dev["id"], "atomic", "One cohesive task")

    def block(self):
        return board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]

    def inbox(self):
        return [item["text"] for item in control.take_instructions(self.root, self.rsession["id"])]

    def build(self, files):
        workspace = Path(board.snapshot(self.root)["task_workspaces"][TASK])
        for name, body in files.items():
            (workspace / name).write_text(body)
        subprocess.run(["git", "add", "--", *files], cwd=workspace, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=h@e.invalid", "-c", "user.name=H", "commit", "-qm", "build"], cwd=workspace, check=True, capture_output=True)

    def request(self):
        return board.request_review(self.root, self.dev["id"], str(self.root / "ledger.md"), "review independently",
                                    phase="final_acceptance", test_command="python3 -m unittest test_smoke")


class RedProofTests(_Project):
    def test_a_check_that_fails_on_the_unbuilt_project_is_labelled_good_and_nobody_is_asked(self):
        self.confirm([sharp()])
        block = self.block()
        self.assertEqual(block["compare"], {"1": red_proof.FAILS_BEFORE})
        self.assertEqual(block["red_proof"]["status"], "settled")
        self.assertEqual(block["red_proof"]["base_commit"], board.snapshot(self.root)["task_baselines"][TASK]["head"])
        self.assertEqual(block["red_proof"]["checks"]["1.1"]["label"], red_proof.FAILS_BEFORE)
        self.assertEqual(self.inbox(), [], "a check that fails first needs no correction")

    def test_the_checks_are_seen_failing_before_delivery_can_begin(self):
        self.confirm([sharp()], plan=False)          # the moment confirm-requirements returns, Delivery may plan and code
        self.assertEqual(self.block()["red_proof"]["status"], "settled")
        self.assertEqual(self.block()["compare"], {"1": red_proof.FAILS_BEFORE})
        self.assertEqual(board.snapshot(self.root)["delivery_plans"], {})


class PassesOnNothingTests(_Project):
    HELLO_AT_BASE = True

    def test_a_check_that_passes_on_nothing_is_flagged_asked_once_and_never_counted(self):
        self.confirm([weak()])
        block = self.block()
        self.assertEqual(block["compare"], {"1": red_proof.PASSES_ON_NOTHING})
        asked = [text for text in self.inbox() if "CONTRACT CHECK SHARPEN" in text]
        self.assertEqual(len(asked), 1)
        self.assertIn("contract-check-correct", asked[0])
        self.assertIn("1.1", asked[0])
        board.run_red_proof(self.root, TASK)                                 # nothing asks the Reviewer a second time
        self.assertEqual(self.inbox(), [])
        # never counted as proof, even when the board once recorded it as passing
        with board.locked_state(self.root) as state:
            state["requirement_confirmations"][TASK]["contract_check"]["results"] = {"1": "passed"}
            state_copy = json.loads(json.dumps(state))
        context = board._frozen_scope_context(state_copy, {"task": TASK}, [], [])
        self.assertNotIn("C1", context["passing"])

    def test_the_reviewer_sharpens_it_once_it_is_run_again_and_there_is_no_loop(self):
        self.confirm([weak()])
        self.inbox()
        fixed = write_json(self.base, "fix.json", {"checks": [sharp()]})
        board.record_contract_check_correction(self.root, self.qa["id"], TASK, 1, "it passed before any work", fixed)
        self.assertEqual(self.block()["compare"], {"1": red_proof.FAILS_BEFORE})
        # a correction that is still weak stays flagged, and the Reviewer is not asked again
        again = write_json(self.base, "again.json", {"checks": [weak()]})
        board.record_contract_check_correction(self.root, self.qa["id"], TASK, 1, "I could not make it fail first", again)
        self.assertEqual(self.block()["compare"], {"1": red_proof.PASSES_ON_NOTHING})
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.block()["red_proof"]["asked"], [1])

    def test_a_weak_check_cannot_hide_inside_a_requirement_with_a_good_one(self):
        self.confirm([sharp("c1-1"), weak("c1-2")])
        block = self.block()
        self.assertEqual(block["compare"], {"1": red_proof.PASSES_ON_NOTHING}, "the strictest check decides")
        self.assertEqual({n: item["label"] for n, item in block["red_proof"]["checks"].items()},
                         {"1.1": red_proof.FAILS_BEFORE, "1.2": red_proof.PASSES_ON_NOTHING})
        self.assertIn("1.2", [text for text in self.inbox() if "SHARPEN" in text][0])

    def test_passing_on_both_is_did_not_distinguish_at_request_review_never_passed(self):
        self.confirm([weak()])
        with board.locked_state(self.root) as state:
            state["requirement_confirmations"][TASK]["contract_check"].pop("compare")
            state["requirement_confirmations"][TASK]["contract_check"].pop("red_proof")
        request = self.request()
        self.assertEqual(request["contract_check_runs"][0]["status"], "passed", "the work itself passed the check")
        block = self.block()
        self.assertEqual(block["compare"], {"1": red_proof.PASSES_ON_NOTHING}, "but it also passes on the unbuilt project")
        self.assertEqual(block["red_proof"]["source"], "review")


class FinalReviewDistinguishesTests(_Project):
    def test_a_check_that_fails_before_and_passes_after_is_the_good_result(self):
        self.confirm([sharp()])
        self.build({"greeting.txt": "hello\n"})
        request = self.request()
        self.assertEqual(request["contract_check_runs"][0]["status"], "passed")
        self.assertEqual(self.block()["compare"], {"1": red_proof.FAILS_BEFORE})
        self.assertEqual(self.block()["results"], {"1": "passed"})


class CouldNotCompareTests(_Project):
    def test_a_base_that_cannot_run_is_could_not_compare_and_delivery_goes_ahead(self):
        with board.locked_state(self.root) as state:
            state["task_baselines"][TASK]["head"] = "0" * 40         # a saved version that does not exist
        self.confirm([sharp()])                                      # includes define_delivery_plan: nothing was held
        block = self.block()
        self.assertEqual(block["compare"], {"1": red_proof.COULD_NOT_COMPARE})
        self.assertTrue(block["red_proof"]["checks"]["1.1"]["reason"])
        self.assertIn(TASK, board.snapshot(self.root)["delivery_plans"])
        self.assertEqual(self.inbox(), [], "could not compare is never a reason to ask the Reviewer")

    def test_a_probe_that_breaks_never_holds_delivery(self):
        with mock.patch.object(board, "_probe_stored_checks_on_base", side_effect=RuntimeError("the disk is full")):
            self.confirm([sharp()])
        self.assertEqual(self.block()["red_proof"]["status"], "unavailable")
        self.assertIn(TASK, board.snapshot(self.root)["delivery_plans"])

    def test_only_a_real_failure_of_the_check_counts_as_failing_first(self):
        real = "Scenario Ledger simulations failed: S-001: internal-QA test command failed with exit code 1: AssertionError"
        self.assertTrue(red_proof.is_real_failure(real))
        for text in (
            "Scenario Ledger simulations failed: S-001: internal-QA test command timed out after 300 seconds; failed with exit code 1",
            "Scenario Ledger simulations failed: S-001: internal-QA test command failed with exit code 127: not found",
            "Scenario Ledger simulations failed: S-001: the test machine changed while this command ran",
            "Scenario Ledger simulation plan is invalid: no commands",
        ):
            self.assertFalse(red_proof.is_real_failure(text), text)

    def test_a_late_result_for_a_replaced_check_is_discarded(self):
        self.confirm([sharp()])
        before = json.dumps(self.block()["compare"])
        stale = {"1.1": {"outcome": "passed", "sha256": "0" * 64, "reason": ""}}
        board._store_compare(self.root, TASK, stale, "x", "late")
        self.assertEqual(json.dumps(self.block()["compare"]), before)
        self.assertEqual(self.block()["red_proof"]["checks"]["1.1"]["label"], red_proof.FAILS_BEFORE)


class RepairTests(repair_base.RepairContextTests):
    """A repair after a FAIL: a test of the last judged version may not change without a stated reason, and the verdict names it."""

    def setUp(self):
        super().setUp()
        (self.root / "pytest.ini").write_text("[pytest]\n")
        subprocess.run(["git", "add", "pytest.ini"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "setup file"], cwd=self.root, check=True)
        self.dev = self._delivery()
        self.first = self._request(self.dev)
        self._fail(self.first)

    def repair(self, files, changes, command=None):
        workspace = self._workspace()
        for name, body in files.items():
            (workspace / name).write_text(body)
        self._commit("repair")
        if command is None:
            return self._request(self.dev, changes=changes)
        return board.request_review(self.root, self.dev["id"], str(self.root / "ledger.md"), "review the flow",
                                    phase="final_acceptance", test_command=command, changes=changes)

    LOOSER = "import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(1)\n"

    def test_a_loosened_test_is_refused_with_a_plain_message(self):
        with self.assertRaises(ValueError) as raised:
            self.repair({"test_smoke.py": self.LOOSER}, "made the check pass")
        message = str(raised.exception)
        self.assertIn("test_smoke.py", message)
        self.assertIn("Test changed: <file> - <why the test was wrong>", message)
        self.assertEqual([r for r in board.snapshot(self.root)["qa_requests"].values() if r["id"] != self.first["id"]], [], "no review was opened")

    def test_the_same_repair_with_a_reason_is_accepted_and_the_verdict_must_name_the_test(self):
        second = self.repair({"test_smoke.py": self.LOOSER}, "the old assertion could never fail\nTest changed: test_smoke.py - the old assertion checked nothing real")
        stored = board.snapshot(self.root)["qa_requests"][second["id"]]
        self.assertEqual(stored["changed_tests"], [{"path": "test_smoke.py", "reason": "the old assertion checked nothing real"}])
        self.assertEqual(stored["review_brief"]["changed_tests"], stored["changed_tests"])
        qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        challenge = self.root / "challenge-2.md"
        challenge.write_text(repair_base.HEADER + "| S-101 | An independent adversarial check confirms the repair closes the prior failure without regression. | adversarial | `python3 -m unittest test_smoke -k passes` | Holds | PASS: executed | PASS |\n")
        board.claim_qa(self.root, qa["id"], second["id"], str(challenge))
        evidence = self.root / "evidence" / "review-2.txt"
        evidence.write_text("command: python3 -m unittest\nresult: FAIL\n")
        with self.assertRaises(ValueError) as raised:
            board.qa_result(self.root, qa["id"], second["id"], "failed", "BLOCKING: module.py still has the hostile shape", str(evidence))
        self.assertIn("test_smoke.py", str(raised.exception))
        self.assertIn("checked nothing real", str(raised.exception))
        recorded = board.qa_result(self.root, qa["id"], second["id"], "failed", "BLOCKING: module.py still has the hostile shape; the change to test_smoke.py is fair and still covers it", str(evidence))
        self.assertEqual(recorded["result"], "failed")

    def test_a_repair_that_leaves_the_tests_alone_needs_no_reason(self):
        second = self.repair({"module.py": "VALUE = 2  # guarded\n"}, "guarded the hostile shape")
        self.assertEqual(board.snapshot(self.root)["qa_requests"][second["id"]]["changed_tests"], [])

    def test_changing_the_test_setup_also_needs_a_reason(self):
        with self.assertRaisesRegex(ValueError, "pytest.ini"):
            self.repair({"pytest.ini": "[pytest]\naddopts = --deselect test_smoke.py::Smoke\n"}, "tidied the settings")

    def test_changing_the_test_command_needs_a_reason_too(self):
        different = "python3 -m unittest test_smoke -k passes"
        with self.assertRaisesRegex(ValueError, "test-command"):
            self.repair({"module.py": "VALUE = 3\n"}, "guarded again", command=different)
        accepted = self.repair({"module.py": "VALUE = 4\n"}, "guarded again\nTest changed: test-command - the full suite is slow and the focused one still runs the failing case", command=different)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][accepted["id"]]["changed_tests"][0]["path"], "test-command")


for _inherited in [name for name in dir(repair_base.RepairContextTests) if name.startswith("test")]:
    setattr(RepairTests, _inherited, None)       # the shared fixture is reused; its own tests are not repeated here


class DirectiveTests(unittest.TestCase):
    def test_reviewer_round_one_says_find_what_would_harm_the_owner_in_normal_use(self):
        text = (Path(board.__file__).resolve().parent.parent / "directives" / "AGENT.md").read_text(encoding="utf-8")
        self.assertIn("Find what would harm the owner in normal use; PASS with notes is the normal result for good work.", text)
        self.assertNotIn("refute", text.lower())


if __name__ == "__main__":
    unittest.main()

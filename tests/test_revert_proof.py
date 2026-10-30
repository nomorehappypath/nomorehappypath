# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Revert proof (task 2026-10-26_revert-proof): at final review each changed file is put back to its pre-work version, one at a time, and the
stored checks rerun; a file no check notices is reported to the reviewer and the owner. Information only. These tests run the real board on a
temporary git project (real confirm-requirements, real final request-review); nothing is read from the machine.

Run:  PYTHONPATH=. python3 -m unittest tests.test_revert_proof -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import board, global_settings, revert_proof
from tests.test_repair_test_integrity import TASK, _Project, sharp

NEEDS_PY = (
    "import os, unittest\n\nclass Greeting(unittest.TestCase):\n"
    "    def test_greeting_module_exists(self):\n"
    "        self.assertTrue(os.path.exists('greeting.py'))\n"
)
GREETING = {"greeting.py": "print('hello')\n"}


class RevertProofTests(_Project):
    def setUp(self):
        super().setUp()
        self.addCleanup(revert_proof.configure, None)
        self.runs: list[str] = []
        self.timeouts: list[int | None] = []
        real = board._execute_scenario_simulations

        def counting(root, ledger, *, certification=None, live_board_root=None):
            if str((certification or {}).get("gate", "")).startswith("revert-proof"):
                self.runs.append(certification["gate"])
                self.timeouts.append(certification.get("timeout_seconds"))
            return real(root, ledger, certification=certification, live_board_root=live_board_root)

        patcher = mock.patch.object(board, "_execute_scenario_simulations", side_effect=counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def finish(self, files):
        self.confirm([sharp(files={"test_needs.py": NEEDS_PY})])
        self.build(files)
        return self.request()

    def proof(self):
        return board.snapshot(self.root)["revert_proofs"][TASK]

    def outcomes(self):
        return {item["path"]: item["outcome"] for item in self.proof()["files"]}

    def test_a_change_a_check_notices_is_labelled_noticed_with_the_check_number(self):
        request = self.finish(GREETING)
        self.assertEqual(self.outcomes(), {"greeting.py": revert_proof.NOTICED})
        self.assertEqual(self.proof()["files"][0]["check"], "1.1")
        self.assertEqual(self.proof()["line"], "All 1 changed file is noticed by at least one test. For information only.")
        shown = board.snapshot(self.root)["qa_requests"][request["id"]]["review_brief"]["revert_proof"]
        self.assertEqual(shown["files"], [{"path": "greeting.py", "result": "noticed: check 1.1 failed"}])

    def test_a_change_no_check_notices_is_named_in_the_owner_line_and_the_review_brief(self):
        request = self.finish({**GREETING, "extra.py": "VALUE = 1\n"})
        self.assertEqual(self.outcomes(), {"extra.py": revert_proof.NOT_NOTICED, "greeting.py": revert_proof.NOTICED})
        self.assertEqual(self.proof()["line"], "1 of 2 changed files is not noticed by any test: extra.py. For information only.")
        shown = board.snapshot(self.root)["qa_requests"][request["id"]]["review_brief"]["revert_proof"]
        self.assertIn({"path": "extra.py", "result": "not noticed by any check"}, shown["files"])
        self.assertEqual(shown["summary"], self.proof()["line"])

    def test_the_cap_is_respected_and_the_rest_are_listed_as_not_checked(self):
        revert_proof.configure({"revert_proof_max_files": 1})
        self.finish({**GREETING, "extra.py": "VALUE = 1\n", "more.py": "VALUE = 2\n"})
        done = [path for path, outcome in self.outcomes().items() if outcome in {revert_proof.NOTICED, revert_proof.NOT_NOTICED}]
        self.assertEqual(done, ["extra.py"], "files are taken in name order up to the limit")
        self.assertEqual({p: o for p, o in self.outcomes().items() if o == revert_proof.OVER_LIMIT}, {"greeting.py": "over_limit", "more.py": "over_limit"})
        self.assertEqual(len(self.runs), 1, "one file, one stored check, one extra run")
        self.assertIn("2 files not checked: over the limit", self.proof()["line"])
        self.assertIn("not checked: over the limit", [i["result"] for i in board.snapshot(self.root)["qa_requests"][self.request_id()]["review_brief"]["revert_proof"]["files"]])

    def request_id(self):
        return next(iter(board.snapshot(self.root)["qa_requests"]))

    def test_documents_images_lockfiles_and_tests_are_skipped_and_the_line_says_so(self):
        self.finish({**GREETING, "README.md": "# hi\n", "logo.png": "x", "package-lock.json": "{}\n", "test_extra.py": "import unittest\n"})
        outcomes = self.outcomes()
        self.assertEqual([p for p, o in outcomes.items() if o == revert_proof.SKIPPED], ["README.md", "logo.png", "package-lock.json", "test_extra.py"])
        self.assertEqual(outcomes["greeting.py"], revert_proof.NOTICED)
        self.assertIn("4 files skipped (documents, images, lockfiles, tests)", self.proof()["line"])
        self.assertEqual(len(self.runs), 1, "only the code file was put back and run")

    def test_the_same_tree_is_never_measured_twice(self):
        request = self.finish(GREETING)
        first_runs = len(self.runs)
        self.assertGreater(first_runs, 0)
        stored = board.snapshot(self.root)["qa_requests"][request["id"]]
        again = board._measure_revert_proof(self.root, TASK, stored, {}, {"run": stored["contract_check_runs"][-1]})
        self.assertEqual(again, self.proof())
        self.assertEqual(len(self.runs), first_runs, "no duplicate runs")
        revert_proof.configure({"revert_proof_max_files": 3})        # a different limit is a different measurement
        board._measure_revert_proof(self.root, TASK, stored, {"board_root": self.root, "role": "reviewer", "task": TASK}, {"run": stored["contract_check_runs"][-1]})
        self.assertGreater(len(self.runs), first_runs)

    def test_it_never_blocks_never_asks_the_owner_and_a_broken_measurement_changes_nothing(self):
        request = self.finish({**GREETING, "extra.py": "VALUE = 1\n"})
        snapshot = board.snapshot(self.root)
        self.assertEqual(snapshot["qa_requests"][request["id"]]["status"], "open", "the review was opened as usual")
        self.assertFalse(snapshot.get("agent_questions"), "the owner is never asked")
        self.assertFalse([e for e in snapshot.get("events", []) if "hold" in str(e.get("type", ""))])
        # a measurement that breaks is dropped silently: the review request still goes through
        with mock.patch.object(board.accepted_bytes, "tree_delta", side_effect=RuntimeError("the disk is full")):
            self.assertIsNone(board._measure_revert_proof(self.root, TASK, {**board.snapshot(self.root)["qa_requests"][request["id"]], "reviewed_tree_hash": "other"},
                                                          {}, {"run": {"status": "passed"}}))

    def git(self, workspace, *args):
        subprocess.run(["git", "-c", "user.email=h@e.invalid", "-c", "user.name=H", *args], cwd=workspace, check=True, capture_output=True)

    def test_a_link_in_the_candidate_can_never_make_the_restore_touch_a_file_outside_the_copy(self):
        self.confirm([sharp(files={"test_needs.py": NEEDS_PY})])
        workspace = Path(board.snapshot(self.root)["task_workspaces"][TASK])
        (workspace / "pkg").mkdir()
        (workspace / "pkg" / "data.py").write_text("BASE = 1\n")
        self.git(workspace, "add", "--", "pkg")
        self.git(workspace, "commit", "-qm", "pkg at the start")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, check=True, capture_output=True, text=True).stdout.strip()
        with board.locked_state(self.root) as state:
            state["task_baselines"][TASK]["head"] = head          # the work "began" with pkg/data.py in place
        outside = Path(tempfile.mkdtemp(prefix="outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "data.py").write_text("PRIVATE_SENTINEL = 99\n")
        shutil.rmtree(workspace / "pkg")
        os.symlink(outside, workspace / "pkg")                       # the candidate replaces the folder with a link to somewhere else
        self.build(GREETING)
        self.git(workspace, "add", "--", "pkg")
        self.git(workspace, "commit", "-qm", "pkg becomes a link")
        request = self.request()
        self.assertEqual((outside / "data.py").read_text(), "PRIVATE_SENTINEL = 99\n", "the file outside the copy was not touched")
        self.assertEqual(request["status"], "open")
        self.assertEqual(self.outcomes().get("pkg/data.py"), revert_proof.COULD_NOT_CHECK)
        self.assertIn("link", {i["path"]: i["reason"] for i in self.proof()["files"]}["pkg/data.py"])

    def test_the_remaining_budget_reaches_every_check_and_unfinished_files_are_could_not_be_checked(self):
        slow = lambda name: "import os, time, unittest\n\nclass S(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue(os.path.exists('greeting.py'))\n        time.sleep(2)\n"
        self.confirm([sharp("c1-1", {"test_a.py": slow("a")}), sharp("c1-2", {"test_b.py": slow("b")})])
        self.build({**GREETING, "extra.py": "VALUE = 1\n"})
        with mock.patch.object(revert_proof, "budget_seconds", return_value=3):
            self.request()
        record = {i["path"]: i for i in self.proof()["files"]}["extra.py"]
        self.assertEqual(record["outcome"], revert_proof.COULD_NOT_CHECK, "unfinished is never reported as not noticed")
        self.assertTrue(self.timeouts and all(t is not None and t <= 3 for t in self.timeouts), self.timeouts)
        self.assertLess(len(self.timeouts), 2, "no check was launched after the budget was used up")
        self.assertEqual(board.snapshot(self.root)["qa_requests"][self.request_id()]["status"], "open", "the review went ahead")

    def test_the_two_limits_are_settings_with_bounds(self):
        defaults = global_settings.load(self._tmp.name).get("plumbing") or {}
        self.assertEqual((defaults.get("revert_proof_max_files"), defaults.get("revert_proof_budget_seconds")), (10, 900))
        for name in ("revert_proof_max_files", "revert_proof_budget_seconds"):
            with self.assertRaises(ValueError):
                global_settings._validated_plumbing({name: 0})
        revert_proof.configure({"revert_proof_max_files": 0, "revert_proof_budget_seconds": True})
        self.assertEqual((revert_proof.max_files(), revert_proof.budget_seconds()), (10, 900))

    def test_the_line_in_plain_words(self):
        none = {"files": [{"path": "a.py", "outcome": "not_noticed"}, {"path": "b.py", "outcome": "not_noticed"}, {"path": "c.py", "outcome": "noticed", "check": "1.1"}]}
        self.assertEqual(revert_proof.line(none), "2 of 3 changed files are not noticed by any test: a.py, b.py. For information only.")
        self.assertEqual(revert_proof.line(None), "")
        self.assertEqual(revert_proof.line({"files": []}), "")


if __name__ == "__main__":
    unittest.main()

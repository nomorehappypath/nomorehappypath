# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
from __future__ import annotations

import subprocess
import tempfile
import unittest
import copy
from pathlib import Path

from harness import accepted_bytes, board, control


class FinalizationDiffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Harness Test")
        self.git("config", "user.email", "harness@example.invalid")
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.root / "accepted.txt").write_text("accepted\n", encoding="utf-8")
        self.git("add", "accepted.txt")
        self.git("commit", "-qm", "accepted subtask")
        self.accepted = self.git("rev-parse", "HEAD")
        self.manifest = accepted_bytes.build_manifest(self.root, self.base, self.accepted)
        (self.root / "finalization.txt").write_text("integration only\n", encoding="utf-8")
        self.git("add", "finalization.txt")
        self.git("commit", "-qm", "finalization")
        self.final = self.git("rev-parse", "HEAD")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def state(self):
        return {
            "delivery_plans": {"TASK": {
                "mode": "application", "structure_revision": 2,
                "subtasks": {"product": {
                    "status": "passed", "integrated_commit": self.accepted,
                }},
            }},
            "task_repositories": {"TASK": {"path": str(self.root)}},
            "task_baselines": {"TASK": {"head": self.base}},
            "qa_requests": {"subtask-pass": {
                "id": "subtask-pass", "task": "TASK", "cycle": 1,
                "phase": "subtask_acceptance", "subtask": "product",
                "status": "passed", "completed_at": "2026-08-18T01:00:00+00:00",
                "integrated_commit": self.accepted,
                "accepted_byte_manifest": copy.deepcopy(self.manifest),
            }},
            "qa_request_index": {}, "archive": [],
        }

    def test_diff_lists_only_bytes_outside_accepted_subtask_manifests(self):
        result = board._application_finalization_diff(
            self.root, self.state(), self.root, "TASK", self.final,
        )
        self.assertEqual(result["accepted_paths"], ["accepted.txt"])
        self.assertEqual(result["paths"], ["finalization.txt"])
        self.assertEqual(result["classification"], "pending_independent_review")
        self.assertEqual(len(result["sha256"]), 64)

    def test_corrupt_manifest_and_changed_accepted_byte_fail_closed(self):
        corrupt = self.state()
        corrupt["qa_requests"]["subtask-pass"]["accepted_byte_manifest"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "digest"):
            board._application_finalization_diff(
                self.root, corrupt, self.root, "TASK", self.final,
            )

        (self.root / "accepted.txt").write_text("silently replaced\n", encoding="utf-8")
        self.git("add", "accepted.txt")
        self.git("commit", "-qm", "replace accepted byte")
        replaced = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "exact accepted entry"):
            board._application_finalization_diff(
                self.root, self.state(), self.root, "TASK", replaced,
            )

    def test_reviewer_must_classify_and_rejection_holds_final_acceptance(self):
        session = control.create(self.root, "codex_delivery")
        delivery = board.register(
            self.root, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Test finalization classification")
        board.begin_task(self.root, delivery["id"], "TASK")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        ledger = self.root / "ledger.md"
        challenge = self.root / "challenge.md"
        rows = (
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-001 | The final integration behavior remains safe. | `python3 -m unittest test_smoke` | Safe behavior remains. | Not executed | OPEN |\n"
        )
        ledger.write_text(rows, encoding="utf-8")
        challenge.write_text(rows.replace("S-001", "S-101"), encoding="utf-8")
        evidence = self.root / "review.txt"
        evidence.write_text("command: python3 -m unittest test_smoke\nresult: FAIL\n", encoding="utf-8")
        finalization = board._application_finalization_diff(
            self.root, self.state(), self.root, "TASK", self.final,
        )
        with board.locked_state(self.root) as state:
            state["delivery_plans"]["TASK"] = self.state()["delivery_plans"]["TASK"]
            state["qa_requests"]["final-review"] = {
                "id": "final-review", "task": "TASK", "cycle": 1,
                "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance",
                "subtask": "", "chunk": "final", "structure_revision": 2,
                "developer_id": delivery["id"], "ledger": str(ledger),
                "challenge_ledger": str(challenge), "status": "claimed",
                "claimed_by": reviewer["id"], "claimed_at": board.now(),
                "requested_at": board.now(), "review_wait_started_at": board.now(),
                "finalization_diff": finalization,
            }
        with self.assertRaisesRegex(ValueError, "explicit accepted or rejected"):
            board.qa_result(
                self.root, reviewer["id"], "final-review", "failed",
                "The diff introduces product behavior.", str(evidence),
            )
        with self.assertRaisesRegex(ValueError, "rejected.*PASS"):
            board.qa_result(
                self.root, reviewer["id"], "final-review", "passed",
                "Incorrect pass attempt.", str(evidence), "rejected",
            )
        failed = board.qa_result(
            self.root, reviewer["id"], "final-review", "failed",
            "The diff introduces product behavior.", str(evidence), "rejected",
        )
        self.assertEqual(failed["finalization_classification"]["decision"], "rejected")
        with board.locked_state(self.root) as state:
            with self.assertRaisesRegex(ValueError, "classification was rejected"):
                board._validate_review_scope(
                    state, "TASK", "final_acceptance", "", "",
                )


if __name__ == "__main__":
    unittest.main()

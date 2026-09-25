# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
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

    def _later_integration_state(self, later_completed_at="2026-08-18T02:00:00+00:00"):
        # Earlier accepted bytes are repaired, then a later subtask whose base
        # already carries the repair is independently accepted without listing it.
        (self.root / "accepted.txt").write_text("repaired\n", encoding="utf-8")
        self.git("add", "accepted.txt")
        self.git("commit", "-qm", "repair accepted byte")
        repaired = self.git("rev-parse", "HEAD")
        (self.root / "integration.txt").write_text("integration\n", encoding="utf-8")
        self.git("add", "integration.txt")
        self.git("commit", "-qm", "later subtask")
        later = self.git("rev-parse", "HEAD")
        state = self.state()
        state["delivery_plans"]["TASK"]["subtasks"]["integration"] = {
            "status": "passed", "integrated_commit": later,
        }
        state["qa_requests"]["later-pass"] = {
            "id": "later-pass", "task": "TASK", "cycle": 1,
            "phase": "subtask_acceptance", "subtask": "integration",
            "status": "passed", "completed_at": later_completed_at,
            "integrated_commit": later,
            "accepted_byte_manifest": accepted_bytes.build_manifest(self.root, repaired, later),
        }
        return state, later

    def test_later_acceptance_supersedes_repaired_accepted_bytes(self):
        state, later = self._later_integration_state()
        result = board._application_finalization_diff(self.root, state, self.root, "TASK", later)
        product = next(item for item in result["accepted_manifests"] if item["subtask"] == "product")
        superseded = product["verification"]["superseded_paths"]
        self.assertEqual(superseded[0]["path"], "accepted.txt")
        self.assertEqual(superseded[0]["superseded_by"], "later-pass")
        self.assertIn("accepted.txt", result["accepted_paths"])

    def test_bytes_changed_after_later_acceptance_still_fail_closed(self):
        state, _later = self._later_integration_state()
        (self.root / "accepted.txt").write_text("changed after review\n", encoding="utf-8")
        self.git("add", "accepted.txt")
        self.git("commit", "-qm", "unreviewed change")
        changed = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "exact accepted entry"):
            board._application_finalization_diff(self.root, state, self.root, "TASK", changed)

    def test_earlier_completed_acceptance_cannot_supersede(self):
        state, later = self._later_integration_state("2026-08-18T00:30:00+00:00")
        with self.assertRaisesRegex(ValueError, "exact accepted entry"):
            board._application_finalization_diff(self.root, state, self.root, "TASK", later)

    def test_base_carried_repair_is_covered_once_and_leaves_no_finalization_path(self):
        """Directive TASK F PART 1: covered, attributed to exactly one manifest, recorded."""
        state, later = self._later_integration_state()
        result = board._application_finalization_diff(self.root, state, self.root, "TASK", later)
        entry = next(item for item in result["accepted_manifests"] if item["subtask"] == "product")
        record = entry["verification"]["superseded_paths"][0]
        self.assertEqual((record["path"], record["superseded_by"], record["superseding_subtask"]),
                         ("accepted.txt", "later-pass", "integration"))
        self.assertFalse(record["in_superseding_manifest"], "the repair sat in the later subtask's base")
        self.assertEqual(sorted(result["accepted_paths"]), ["accepted.txt", "integration.txt"])
        self.assertEqual(result["paths"], ["finalization.txt"], "the covered path needs no reviewer classification")
        # The hashed payload covers the superseded record: change it, the hash changes.
        import hashlib, json
        without = {key: value for key, value in result.items() if key != "sha256"}
        self.assertEqual(result["sha256"], hashlib.sha256(json.dumps(without, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        next(item for item in without["accepted_manifests"] if item["subtask"] == "product")["verification"]["superseded_paths"][0]["superseded_by"] = "someone-else"
        self.assertNotEqual(result["sha256"], hashlib.sha256(json.dumps(without, sort_keys=True, separators=(",", ":")).encode()).hexdigest())

    def test_recertified_path_listed_by_the_later_manifest_is_attributed_once_without_overlap(self):
        """The 2026-09-24 addendum: a later subtask owns and changes an already-accepted file."""
        state, later = self._later_integration_state()
        # The later subtask's manifest lists the repaired file itself (built from
        # the earlier accepted commit, so accepted.txt is in it), as a governed
        # re-certification would.
        state["qa_requests"]["later-pass"]["accepted_byte_manifest"] = accepted_bytes.build_manifest(self.root, self.accepted, later)
        self.assertIn("accepted.txt", state["qa_requests"]["later-pass"]["accepted_byte_manifest"]["paths"])
        result = board._application_finalization_diff(self.root, state, self.root, "TASK", later)
        product = next(item for item in result["accepted_manifests"] if item["subtask"] == "product")
        record = product["verification"]["superseded_paths"][0]
        self.assertTrue(record["in_superseding_manifest"])
        # The later manifest was built from the earlier accepted commit, so it
        # also lists finalization.txt; every path is attributed exactly once.
        self.assertEqual(sorted(result["accepted_paths"]), ["accepted.txt", "finalization.txt", "integration.txt"], "once, via the later manifest")
        self.assertEqual(result["paths"], [])

    def test_equal_completion_times_break_by_cycle_then_id_never_wall_clock_alone(self):
        same = "2026-08-18T01:00:00+00:00"   # the earlier acceptance's completed_at
        state, later = self._later_integration_state(same)
        by_cycle = copy.deepcopy(state)
        by_cycle["qa_requests"]["later-pass"]["cycle"] = 2
        board._application_finalization_diff(self.root, by_cycle, self.root, "TASK", later)   # a later cycle supersedes
        by_id = copy.deepcopy(state)
        by_id["qa_requests"]["later-pass"]["cycle"] = 1
        by_id["qa_requests"]["later-pass"]["id"] = "aaa-earlier-id"
        by_id["qa_requests"]["aaa-earlier-id"] = by_id["qa_requests"].pop("later-pass")
        with self.assertRaisesRegex(ValueError, "exact accepted entry"):
            board._application_finalization_diff(self.root, by_id, self.root, "TASK", later)

    def test_a_later_acceptance_with_different_bytes_cannot_supersede(self):
        state, later = self._later_integration_state()
        (self.root / "accepted.txt").write_text("a third version\n", encoding="utf-8")
        self.git("add", "accepted.txt"); self.git("commit", "-qm", "another change after the later review")
        newest = self.git("rev-parse", "HEAD")
        # The later acceptance reviewed `later`, whose accepted.txt differs from the newest tree.
        with self.assertRaisesRegex(ValueError, "exact accepted entry"):
            board._application_finalization_diff(self.root, state, self.root, "TASK", newest)

    def test_an_accepted_path_deleted_from_the_final_tree_is_never_treated_as_superseded(self):
        state, later = self._later_integration_state()
        self.git("rm", "-q", "accepted.txt"); self.git("commit", "-qm", "delete the accepted file")
        deleted = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "exact accepted entry"):
            board._application_finalization_diff(self.root, state, self.root, "TASK", deleted)

    def test_reevaluate_finalization_clears_a_stale_hold_under_the_current_rule_and_names_the_finding(self):
        """Directive TASK F PART 3: governed recovery for a task stuck like the 2026-09-23 one."""
        state, later = self._later_integration_state()
        state["task_workspaces"] = {"TASK": str(self.root)}
        state["task_branches"] = {"TASK": {"head": later}}
        state["finalization_holds"] = {"TASK": {"request_id": "final-1", "structure_revision": 2,
                                                "paths": ["accepted.txt"], "reason": "stale pinned entry", "recorded_at": "2026-08-18T03:00:00+00:00"}}
        for request in state["qa_requests"].values():
            # The live board reads more of a request than the diff function does.
            for key, value in (("requested_at", request["completed_at"]), ("developer_id", "delivery-fixture"),
                               ("claimed_by", None), ("reserved_by", None), ("result", "passed"),
                               ("review_wait_started_at", request["completed_at"]), ("chunk", ""), ("route_state", "review_passed")):
                request.setdefault(key, value)
        with board.locked_state(self.root) as live:
            live.update(state)
        result = board.reevaluate_finalization(self.root, "TASK", "finding-c9b205dff8dc753c", "coverage rule now honours later certification")
        self.assertEqual(result["head_commit"], later)
        self.assertEqual([item["path"] for item in result["superseded_paths"]], ["accepted.txt"])
        self.assertEqual(result["hold_cleared"]["request_id"], "final-1")
        snapshot = board.snapshot(self.root)
        self.assertNotIn("TASK", snapshot.get("finalization_holds", {}))
        event = [item for item in snapshot["events"] if item["kind"] == "finalization_coverage_reevaluated"][-1]
        self.assertEqual(event["finding"], "finding-c9b205dff8dc753c")
        self.assertEqual(event["superseded_paths"], ["accepted.txt"]); self.assertTrue(event["hold_cleared"])
        self.assertIn("resolves finding-c9b205dff8dc753c", event["message"])
        # Certified product review is untouched: every QA request is exactly as it was.
        self.assertEqual(snapshot["qa_requests"], state["qa_requests"])

    def test_reevaluate_finalization_changes_nothing_when_coverage_still_fails(self):
        state, later = self._later_integration_state()
        (self.root / "accepted.txt").write_text("changed after review\n", encoding="utf-8")
        self.git("add", "accepted.txt"); self.git("commit", "-qm", "unreviewed change")
        head = self.git("rev-parse", "HEAD")
        state["task_workspaces"] = {"TASK": str(self.root)}
        state["task_branches"] = {"TASK": {"head": head}}
        state["finalization_holds"] = {"TASK": {"request_id": "final-1", "paths": ["accepted.txt"], "reason": "x", "recorded_at": "2026-08-18T03:00:00+00:00"}}
        for request in state["qa_requests"].values():
            # The live board reads more of a request than the diff function does.
            for key, value in (("requested_at", request["completed_at"]), ("developer_id", "delivery-fixture"),
                               ("claimed_by", None), ("reserved_by", None), ("result", "passed"),
                               ("review_wait_started_at", request["completed_at"]), ("chunk", ""), ("route_state", "review_passed")):
                request.setdefault(key, value)
        with board.locked_state(self.root) as live:
            live.update(state)
        with self.assertRaisesRegex(ValueError, "still fails"):
            board.reevaluate_finalization(self.root, "TASK", "finding-1", "trying the current rule")
        snapshot = board.snapshot(self.root)
        self.assertIn("TASK", snapshot["finalization_holds"], "a hold is never cleared by a failed re-evaluation")
        self.assertEqual(snapshot["events"][-1]["kind"], "finalization_coverage_reevaluation_refused")
        with self.assertRaisesRegex(ValueError, "requires a task"):
            board.reevaluate_finalization(self.root, "TASK", "", "no finding given")

    def test_a_path_absent_from_both_the_later_review_and_the_final_tree_is_never_superseded(self):
        """Reviewer finding (2026-09-24): tree_entry answers a missing path with a truthy 'deleted' record."""
        # The accepted file is deleted BEFORE the later subtask's review, so the
        # later reviewed commit and the final tree both lack it.
        self.git("rm", "-q", "accepted.txt"); self.git("commit", "-qm", "delete the accepted file")
        deleted = self.git("rev-parse", "HEAD")
        (self.root / "integration.txt").write_text("integration\n", encoding="utf-8")
        self.git("add", "integration.txt"); self.git("commit", "-qm", "later subtask")
        later = self.git("rev-parse", "HEAD")
        state = self.state()
        state["delivery_plans"]["TASK"]["subtasks"]["integration"] = {"status": "passed", "integrated_commit": later}
        state["qa_requests"]["later-pass"] = {
            "id": "later-pass", "task": "TASK", "cycle": 1, "phase": "subtask_acceptance", "subtask": "integration",
            "status": "passed", "completed_at": "2026-08-18T02:00:00+00:00", "integrated_commit": later,
            "accepted_byte_manifest": accepted_bytes.build_manifest(self.root, deleted, later),
        }
        with self.assertRaisesRegex(ValueError, "exact accepted entry"):
            board._application_finalization_diff(self.root, state, self.root, "TASK", later)

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

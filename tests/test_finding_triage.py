# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Finding repeat-triage simulations (owner report 2026-08-15).

The owner was asked to decide the SAME defect twice because agents reworded it
between tasks and exact-fingerprint dedup cannot see paraphrase. These
scenarios prove the fix: unrelated findings are born needs_triage, the CTO must
rule repeat-or-distinct before any decision can be requested, a merged repeat
inherits the earlier decision forever, and a recurrence of a RESOLVED defect is
forced through as new (regression), never merged away.

Run:  PYTHONPATH=. python3 -m unittest tests.test_finding_triage -v
"""
import tempfile
import unittest
from pathlib import Path

from harness import board


def _tmp_root(test: unittest.TestCase) -> Path:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    (root / ".harness" / "board").mkdir(parents=True)
    return root


# The actual duplicate pair from the live board that the owner had to dismiss
# twice — the regression-proof for this whole fix.
REAL_FIRST_TITLE = "Stopping a reviewer reopens every review it ever completed, erasing the queue's history"
REAL_FIRST_DESC = (
    "cancel_session_work reopens every request the reviewer ever handled, "
    "nulling challenge_ledger references for settled reviews and erasing the "
    "review queue's history."
)
REAL_SECOND_TITLE = "Stopping a reviewer session reopens every request it ever handled, including settled reviews"
REAL_SECOND_DESC = (
    "When a reviewer session is stopped, cleanup reopens all its requests "
    "indiscriminately instead of only the unfinished claim, so completed "
    "reviews lose their challenge ledgers."
)


class FindingTriageTests(unittest.TestCase):
    # ---- S-FTRI-001: unrelated findings are born needs_triage, never as a decision card ----
    def test_unrelated_finding_is_born_needs_triage_and_hidden_from_decisions(self):
        root = _tmp_root(self)
        finding = board.record_finding(root, "TASK-1", "Viewer polish gap", "A cosmetic spacing defect on the panel", False)
        self.assertEqual(finding["status"], "needs_triage")
        with self.assertRaisesRegex(ValueError, "not been triaged"):
            board.record_finding_decision(root, finding["id"], "do_not_fix")
        # In-scope findings are untouched by triage: they block the task as before.
        blocking = board.record_finding(root, "TASK-1", "Broken required path", "The acceptance path fails", True)
        self.assertEqual(blocking["status"], "in_scope")

    # ---- S-FTRI-002: the REAL reworded pair — repeat merges, dismissal stands forever ----
    def test_real_paraphrased_repeat_merges_and_dismissal_stands(self):
        root = _tmp_root(self)
        first = board.record_finding(root, "TASK-1", REAL_FIRST_TITLE, REAL_FIRST_DESC, False)
        board.triage_finding(root, first["id"], "distinct")
        board.record_finding_decision(root, first["id"], "do_not_fix")
        # A later task rediscovers the same behavior in different words.
        second = board.record_finding(root, "TASK-2", REAL_SECOND_TITLE, REAL_SECOND_DESC, False)
        self.assertEqual(second["status"], "needs_triage")
        candidate_ids = [c["id"] for c in second.get("repeat_candidates", [])]
        self.assertIn(first["id"], candidate_ids, "the scorer must nominate the real earlier finding")
        merged = board.triage_finding(root, second["id"], "repeat", target_id=first["id"], note="same cancel_session_work behavior")
        self.assertEqual(merged["status"], "merged")
        self.assertEqual(merged["merged_into"], first["id"])
        state = board.snapshot(root)
        target = state["deferred_findings"][first["id"]]
        self.assertEqual(target["status"], "dismissed", "the earlier decision stands")
        self.assertIn("TASK-2", target["observed_in_tasks"])
        self.assertEqual(target["repeat_observations"][0]["merged_finding_id"], second["id"])
        # The owner decision surface never sees either record again: the merged
        # record refuses decisions because the earlier finding's decision stands.
        with self.assertRaisesRegex(ValueError, "owner decision"):
            board.record_finding_decision(root, second["id"], "fix")

    # ---- S-FTRI-003: a genuinely new finding enters the queue exactly once ----
    def test_distinct_verdict_promotes_to_the_decision_queue_once(self):
        root = _tmp_root(self)
        finding = board.record_finding(root, "TASK-1", "Backup rotation unbounded", "Registry backups grow without a cap", False)
        promoted = board.triage_finding(root, finding["id"], "distinct")
        self.assertEqual(promoted["status"], "deferred")
        with self.assertRaisesRegex(ValueError, "needs_triage"):
            board.triage_finding(root, finding["id"], "distinct")
        decided = board.record_finding_decision(root, finding["id"], "fix")
        self.assertEqual(decided["status"], "fix_requested")

    # ---- S-FTRI-004: recurrence of a RESOLVED defect is a regression — merge refused ----
    def test_repeat_of_resolved_finding_is_refused_as_regression(self):
        root = _tmp_root(self)
        first = board.record_finding(root, "TASK-1", "Launch path crash", "The launch path crashes on open", False)
        board.triage_finding(root, first["id"], "distinct")
        board.record_finding_decision(root, first["id"], "fix")
        board.resolve_finding(root, first["id"], "regression test passed")
        again = board.record_finding(root, "TASK-2", "Launch path crashes again on open", "The previously fixed launch crash is back", False)
        with self.assertRaisesRegex(ValueError, "regression"):
            board.triage_finding(root, again["id"], "repeat", target_id=first["id"])
        promoted = board.triage_finding(root, again["id"], "distinct")
        self.assertEqual(promoted["status"], "deferred")

    # ---- S-FTRI-005: resolved findings are never nominated as merge candidates ----
    def test_resolved_findings_are_not_candidates(self):
        root = _tmp_root(self)
        first = board.record_finding(root, "TASK-1", REAL_FIRST_TITLE, REAL_FIRST_DESC, False)
        board.triage_finding(root, first["id"], "distinct")
        board.record_finding_decision(root, first["id"], "fix")
        board.resolve_finding(root, first["id"], "fixed and regression-tested")
        second = board.record_finding(root, "TASK-2", REAL_SECOND_TITLE, REAL_SECOND_DESC, False)
        candidate_ids = [c["id"] for c in second.get("repeat_candidates", [])]
        self.assertNotIn(first["id"], candidate_ids)

    # ---- S-FTRI-006: unrelated findings never wake or delay the CTO ----
    def test_needs_triage_does_not_count_as_cto_actionable_work(self):
        root = _tmp_root(self)
        cto = {"id": "cto-1", "role": "cto", "active": True}
        state = board.snapshot(root)
        self.assertFalse(board._agent_has_actionable_work(state, cto))
        finding = board.record_finding(root, "TASK-1", "Stray defect", "An unrelated defect discovered mid-task", False)
        self.assertFalse(board._agent_has_actionable_work(board.snapshot(root), cto))
        board.triage_finding(root, finding["id"], "distinct")
        self.assertFalse(board._agent_has_actionable_work(board.snapshot(root), cto))

    # ---- S-FTRI-008: a distinct verdict carries a ready-made disposition ----
    def test_distinct_verdict_stores_recommendation_for_later_decision(self):
        root = _tmp_root(self)
        finding = board.record_finding(root, "TASK-1", "Slow index rebuild", "Rebuilding the index takes minutes on large boards", False)
        promoted = board.triage_finding(root, finding["id"], "distinct",
                                        recommend="do_not_fix",
                                        recommend_reason="Performance nicety; no correctness impact and no owner-visible delay yet.")
        self.assertEqual(promoted["recommendation"]["decision"], "do_not_fix")
        self.assertIn("Performance nicety", promoted["recommendation"]["reason"])
        # The recommendation never executes itself: the finding still waits for
        # an explicit decision, whenever someone next looks.
        self.assertEqual(promoted["status"], "deferred")
        with self.assertRaisesRegex(ValueError, "must be fix or do_not_fix"):
            board.triage_finding(root, board.record_finding(root, "TASK-1", "Another finding", "Different behavior entirely for the guard", False)["id"],
                                 "distinct", recommend="maybe")

    def test_cleared_verdict_never_enters_owner_decision_queue(self):
        root = _tmp_root(self)
        finding = board.record_finding(
            root, "TASK-1", "Release handoff appears absent",
            "No CTO release route was visible immediately after final acceptance", False,
        )
        with self.assertRaisesRegex(ValueError, "requires evidence"):
            board.triage_finding(root, finding["id"], "cleared")

        cleared = board.triage_finding(
            root, finding["id"], "cleared",
            note="Events 42-44 show the CTO route arrived in the same bounded cycle.",
        )
        self.assertEqual(cleared["status"], "cleared")
        self.assertEqual(cleared["decision"], "system_cleared")
        self.assertIn("Events 42-44", cleared["clearance_evidence"])
        self.assertEqual(board.list_findings(root, include_resolved=False), [])
        with self.assertRaisesRegex(ValueError, "owner decision"):
            board.record_finding_decision(root, finding["id"], "do_not_fix")

    def test_repeat_of_cleared_observation_stays_out_of_owner_queue(self):
        root = _tmp_root(self)
        first = board.record_finding(
            root, "TASK-1", "Missing routed CTO release action",
            "The final accepted task had no release action visible in the first delivery poll", False,
        )
        board.triage_finding(
            root, first["id"], "cleared",
            note="The CTO action arrived 30 seconds later in the same monitoring cycle.",
        )
        second = board.record_finding(
            root, "TASK-2", "CTO release route was not immediately visible",
            "Delivery polled once after final acceptance before the CTO action appeared", False,
        )
        self.assertIn(first["id"], [item["id"] for item in second.get("repeat_candidates", [])])
        merged = board.triage_finding(
            root, second["id"], "repeat", target_id=first["id"],
            note="Same observation window; the later release route is present.",
        )
        self.assertEqual(merged["status"], "merged")
        self.assertEqual(board.snapshot(root)["deferred_findings"][first["id"]]["status"], "cleared")
        self.assertEqual(board.list_findings(root, include_resolved=False), [])

    def test_cleared_verdict_rejects_owner_disposition_fields(self):
        root = _tmp_root(self)
        finding = board.record_finding(root, "TASK-1", "Transient signal", "A signal was briefly absent", False)
        with self.assertRaisesRegex(ValueError, "accepts only an evidence note"):
            board.triage_finding(
                root, finding["id"], "cleared", target_id="another",
                note="The signal is now present.", recommend="do_not_fix",
            )

    # ---- S-FTRI-007: exact-identical text still merges silently, no triage needed ----
    def test_exact_duplicate_still_merges_without_triage(self):
        root = _tmp_root(self)
        first = board.record_finding(root, "TASK-1", "Repeated launch defect", "The same verified launch path fails", False)
        repeated = board.record_finding(root, "TASK-2", "Repeated launch defect", "The same verified launch path fails", False)
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(len(board.snapshot(root)["deferred_findings"]), 1)
        self.assertIn("TASK-2", repeated["observed_in_tasks"])


if __name__ == "__main__":
    unittest.main()

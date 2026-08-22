# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""P0 lifecycle-metrics sims (item 2; owner bar: no happy path).

Run:  PYTHONPATH=. python3 -m unittest tests.test_lifecycle_metrics -v
"""
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import board, lifecycle_metrics


def _iso(base: datetime, offset: float) -> str:
    return (base + timedelta(seconds=offset)).isoformat()


class LifecycleMetricsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.base = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)

    def _seed_lifecycle(self):
        """A synthetic but complete lifecycle with EXACT known durations."""
        b = self.base
        with board.locked_state(self.root) as state:
            for kind, offset in (("task_begun", 0), ("requirements_confirmed", 60),
                                 ("development_complete", 1000),
                                 ("visual_test_required", 1100),
                                 ("owner_release_decision_recorded", 1400)):
                state["events"].append({"kind": kind, "task": "T", "at": _iso(b, offset),
                                        "agent_id": "system", "role": "system",
                                        "sequence": len(state["events"]) + 1})
            state["qa_requests"]["r1"] = {
                "id": "r1", "task": "T", "status": "failed", "phase": "chunk",
                "stage": "independent_review", "subtask": "", "chunk": "", "developer_id": "dev", "claimed_by": "qa", "review_wait_started_at": "", "structure_revision": 0, "cycle": 1, "requested_at": _iso(b, 200), "claimed_at": _iso(b, 230),
                "completed_at": _iso(b, 400),
            }
            state["qa_requests"]["r2"] = {
                "id": "r2", "task": "T", "status": "passed", "phase": "final_acceptance",
                "stage": "independent_review", "subtask": "", "chunk": "", "developer_id": "dev", "claimed_by": "qa", "review_wait_started_at": "", "structure_revision": 0, "cycle": 2, "requested_at": _iso(b, 500), "claimed_at": _iso(b, 520),
                "completed_at": _iso(b, 900),
            }

    # ---- S-P0-001: phases reconcile against the known wall clock ----
    def test_phases_reconcile_with_wall_clock(self):
        self._seed_lifecycle()
        m = lifecycle_metrics.task_metrics(self.root, "T")
        self.assertEqual(m["total_seconds"], 1400.0)
        p = m["phases"]
        self.assertEqual(p["definition_seconds"], 60.0)
        self.assertEqual(p["implementation_seconds"], 140.0)   # 60 -> 200
        self.assertEqual(p["review_queue_wait_seconds"], 50.0)  # 30 + 20
        self.assertEqual(p["review_execution_seconds"], 550.0)  # 170 + 380
        self.assertEqual(p["repair_turnaround_seconds"], 100.0) # 400 -> 500
        self.assertEqual(p["owner_wait_seconds"], 300.0)        # 1100 -> 1400
        accounted = (p["definition_seconds"] + p["implementation_seconds"]
                     + p["review_queue_wait_seconds"] + p["review_execution_seconds"]
                     + p["repair_turnaround_seconds"] + p["owner_wait_seconds"])
        # Reconciliation: accounted phases + legitimate gaps == total. The
        # only unaccounted spans in this fixture are 900->1000 (completion lag)
        # and 1000->1100 (mechanical tail): exactly 200 seconds.
        self.assertAlmostEqual(m["total_seconds"] - accounted, 200.0, places=1)

    # ---- S-P0-002: hostile records never crash and never invent numbers ----
    def test_missing_and_malformed_records_degrade_honestly(self):
        with board.locked_state(self.root) as state:
            state["events"].append({"kind": "task_begun", "task": "T2", "at": "not-a-date",
                                    "agent_id": "system", "role": "system",
                                    "sequence": 1})
            state["qa_requests"]["bad"] = {
                "id": "bad", "task": "T2", "status": "passed", "phase": "chunk",
                "stage": "independent_review", "subtask": "", "chunk": "", "developer_id": "dev", "claimed_by": "qa", "review_wait_started_at": "", "structure_revision": 0, "cycle": 1, "requested_at": "", "claimed_at": None,
            }
        m = lifecycle_metrics.task_metrics(self.root, "T2")
        self.assertIsNone(m["total_seconds"], "unknown must be None, never fabricated")
        self.assertIsNone(m["phases"]["definition_seconds"])
        self.assertEqual(m["review_count"], 1)
        self.assertIsNone(m["reviews"][0]["queue_wait_seconds"])

    # ---- S-P0-003: duplicate counting — exact repeats across requests only ----
    def test_duplicate_count_is_cross_request_exact_matches_only(self):
        evidence_a = self.root / "a.txt"
        evidence_a.write_text("command: python3 -m unittest suite\nresult: PASS\n"
                              "command: python3 -m unittest only_in_a\nresult: PASS\n")
        evidence_b = self.root / "b.txt"
        evidence_b.write_text("command: python3 -m unittest suite\nresult: PASS\n"
                              "command: python3 -m unittest only_in_b\nresult: PASS\n")
        with board.locked_state(self.root) as state:
            for rid, path in (("ra", evidence_a), ("rb", evidence_b)):
                state["qa_requests"][rid] = {
                    "id": rid, "task": "T3", "status": "passed", "phase": "chunk",
                    "stage": "independent_review", "subtask": "", "chunk": "", "developer_id": "dev", "claimed_by": "qa", "review_wait_started_at": "", "structure_revision": 0, "cycle": 1, "requested_at": board.now(), "evidence": str(path),
                }
        d = lifecycle_metrics.duplicate_execution_count(self.root, "T3")
        self.assertEqual(d["duplicate_executions_upper_bound"], 1,
                         "one command repeated across two requests = exactly one duplicate")
        self.assertEqual(d["duplicated_commands"], 1)
        # Same command twice WITHIN one request is not a cross-request duplicate.
        evidence_c = self.root / "c.txt"
        evidence_c.write_text("command: python3 -m unittest twice\nresult: PASS\n"
                              "command: python3 -m unittest twice\nresult: PASS\n")
        with board.locked_state(self.root) as state:
            state["qa_requests"]["rc"] = {
                "id": "rc", "task": "T4", "status": "passed", "phase": "chunk",
                "stage": "independent_review", "subtask": "", "chunk": "", "developer_id": "dev", "claimed_by": "qa", "review_wait_started_at": "", "structure_revision": 0, "cycle": 1, "requested_at": board.now(), "evidence": str(evidence_c),
            }
        d4 = lifecycle_metrics.duplicate_execution_count(self.root, "T4")
        self.assertEqual(d4["duplicate_executions_upper_bound"], 0)

    # ---- S-P0-004: projection cost is bounded (measured, not assumed) ----
    def test_projection_overhead_is_negligible(self):
        self._seed_lifecycle()
        start = time.monotonic()
        for _ in range(20):
            lifecycle_metrics.task_metrics(self.root, "T")
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0, "20 projections must stay well under 2s")


if __name__ == "__main__":
    unittest.main()

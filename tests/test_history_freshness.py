# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""History freshness + timestamp-contract sims (owner report 2026-08-16).

Defect: the history panel loaded once and cached forever (historyLoaded), so
an open panel showed yesterday's groups while today's tasks completed. Fix:
the dashboard payload carries a cheap history_version covering every task
conclusion path; the page refetches an already-loaded history whenever it
changes. These sims prove the version signature's behavior adversarially and
pin the timestamp contract the browser's local-date grouping depends on.

Run:  PYTHONPATH=. python3 -m unittest tests.test_history_freshness -v
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from harness import board, board_viewer, contract, control


class HistoryFreshnessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _version(self) -> str:
        return str(board_viewer.dashboard_payload(self.root).get("history_version", ""))

    # ---- S-HIST-001: the exact staleness trigger — a decision changes the version ----
    def test_release_decision_changes_history_version(self):
        before = self._version()
        with board.locked_state(self.root) as state:
            state["releases"]["TASK-DONE"] = {
                "task": "TASK-DONE", "status": "VISUAL_TEST_REQUIRED",
                "head_commit": "abc123", "cto_id": "cto", "recorded_at": board.now(),
            }
        board.record_release_decision(self.root, "TASK-DONE", "accepted")
        after = self._version()
        self.assertNotEqual(before, after,
                            "a concluded task MUST change the signature an open panel watches")

    # ---- S-HIST-002: unrelated churn does not change the version (no refetch loop) ----
    def test_unrelated_state_churn_keeps_version_stable(self):
        with board.locked_state(self.root) as state:
            state["releases"]["TASK-DONE"] = {
                "task": "TASK-DONE", "status": "VISUAL_TEST_REQUIRED",
                "head_commit": "abc123", "cto_id": "cto", "recorded_at": board.now(),
            }
        board.record_release_decision(self.root, "TASK-DONE", "accepted")
        before = self._version()
        session = control.create(self.root, "codex_delivery")
        board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                       vendor="OpenAI", session_id=session["id"])
        board.mark_stalled(self.root, stale_seconds=10_000)
        self.assertEqual(before, self._version(),
                         "agent churn must not thrash an open history panel")

    # ---- S-HIST-003: cancellation is a conclusion path too ----
    def test_cancellation_changes_history_version(self):
        before = self._version()
        with board.locked_state(self.root) as state:
            state.setdefault("cancelled_tasks", {})["TASK-GONE"] = {
                "task": "TASK-GONE", "cancelled_at": board.now(), "reason": "owner cancelled"}
        self.assertNotEqual(before, self._version())

    # ---- S-HIST-004: the timestamp contract the browser's local grouping needs ----
    def test_history_timestamps_carry_timezone_offsets(self):
        # Replay the REAL boundary pair from the live board: 2026-08-16T00:17Z
        # groups to Aug 15 in US central time, 17:44Z groups to Aug 16. Both
        # are only computable client-side if the offset survives the payload.
        for iso in ("2026-08-16T00:17:52.979452+00:00", "2026-08-16T17:44:16.023724+00:00"):
            parsed = datetime.fromisoformat(iso)
            self.assertIsNotNone(parsed.tzinfo, "history timestamps must be offset-aware")
        stamp = board.now()
        self.assertIsNotNone(datetime.fromisoformat(stamp).tzinfo,
                             "board.now() must emit offset-aware timestamps for history grouping")


if __name__ == "__main__":
    unittest.main()

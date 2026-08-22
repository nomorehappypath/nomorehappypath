# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Item 8 sims: bounded CTO monitoring + P9 no-op churn elimination.

Owner bar: the idle system is measured at ZERO writes, and every preserved
wake condition still fires individually under the narrowed rule.

Run:  PYTHONPATH=. python3 -m unittest tests.test_churn_elimination -v
"""
import tempfile
import unittest
from pathlib import Path

from harness import board, control


class CtoWorkNarrowingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cto = {"id": "cto-1", "role": "cto", "active": True}

    def _state(self):
        return board.snapshot(self.root)

    # ---- S-P6-001: a busy Delivery task requires bounded CTO monitoring ----
    def test_active_delivery_is_cto_monitoring_work(self):
        with board.locked_state(self.root) as state:
            state["agents"]["dev"] = {
                "id": "dev", "role": "engineering", "task": "TASK-BUSY",
                "active": True, "status": "working", "liveness": "healthy",
                "session_id": "s1", "poll_counter": 3,
                "status_note": "implementing", "last_status_at": board.now(),
            }
        self.assertTrue(board._agent_has_actionable_work(self._state(), self.cto),
                        "an active task must remain visible to the global CTO")

    # ---- S-P6-002: every preserved wake condition fires individually ----
    def test_each_material_condition_fires(self):
        # (a) unrelated observations are deliberately not CTO work
        finding = board.record_finding(self.root, "T", "Needs triage", "An untriaged item", False)
        self.assertFalse(board._agent_has_actionable_work(self._state(), self.cto))
        board.triage_finding(self.root, finding["id"], "distinct")
        board.record_finding_decision(self.root, finding["id"], "do_not_fix")
        self.assertFalse(board._agent_has_actionable_work(self._state(), self.cto))
        # (b) passed final acceptance without a release
        with board.locked_state(self.root) as state:
            state["qa_requests"]["final-1"] = {
                "id": "final-1", "task": "T-REL", "status": "passed",
                "phase": "final_acceptance", "stage": "independent_review",
                "cycle": 1, "structure_revision": 0, "subtask": "", "chunk": "",
                "developer_id": "dev", "claimed_by": "qa",
                "review_wait_started_at": "", "requested_at": board.now(),
            }
        self.assertTrue(board._agent_has_actionable_work(self._state(), self.cto))
        with board.locked_state(self.root) as state:
            state["releases"]["T-REL"] = {"task": "T-REL", "status": "VISUAL_TEST_REQUIRED",
                                          "head_commit": "abc", "cto_id": "cto-1",
                                          "recorded_at": board.now()}
        self.assertFalse(board._agent_has_actionable_work(self._state(), self.cto))
        # (c) a stalled agent needs intervention
        with board.locked_state(self.root) as state:
            state["agents"]["dev2"] = {
                "id": "dev2", "role": "engineering", "task": "T-STALL",
                "active": True, "status": "working", "liveness": "stalled",
                "session_id": "s2", "poll_counter": 1,
                "status_note": "silent", "last_status_at": board.now(),
            }
        self.assertTrue(board._agent_has_actionable_work(self._state(), self.cto))
        with board.locked_state(self.root) as state:
            state["agents"]["dev2"]["liveness"] = "healthy"
            state["agents"]["dev2"]["active"] = False
        self.assertFalse(board._agent_has_actionable_work(self._state(), self.cto))
        # (d) an owner-rejected repair with nobody routed
        with board.locked_state(self.root) as state:
            state.setdefault("release_repairs", {})["T-REP"] = {
                "task": "T-REP", "status": "OWNER_REJECTED_REPAIR_REQUIRED"}
        self.assertTrue(board._agent_has_actionable_work(self._state(), self.cto))
        # (e) a broker recovery hold is never orphaned after CTO narrowing
        with board.locked_state(self.root) as state:
            state["release_repairs"].clear()
            state.setdefault("git_recovery_holds", {})["tx-1"] = {
                "transaction_id": "tx-1", "status": "CTO_RECOVERY_HOLD"}
        self.assertTrue(board._agent_has_actionable_work(self._state(), self.cto))


class NoOpChurnTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    # ---- S-P9-001: a no-op locked read writes NOTHING on the board ----
    def test_noop_locked_state_writes_nothing(self):
        with board.locked_state(self.root) as state:
            state["agents"]["a"] = {"id": "a", "role": "cto", "task": "GLOBAL_MONITOR",
                                    "active": True, "status": "working",
                                    "poll_counter": 0, "status_note": "x",
                                    "last_status_at": board.now()}
        state_path = board.board_dir(self.root) / "state.json"
        board_md = board.board_dir(self.root) / "BOARD.md"
        before = (state_path.stat().st_mtime_ns, state_path.read_bytes(),
                  board_md.stat().st_mtime_ns)
        for _ in range(5):
            with board.locked_state(self.root) as state:
                _ = state.get("agents")  # read, no mutation
        after = (state_path.stat().st_mtime_ns, state_path.read_bytes(),
                 board_md.stat().st_mtime_ns)
        self.assertEqual(before, after, "five no-op locked reads must write zero bytes")

    # ---- S-P9-002: an empty-inbox take rewrites nothing in control state ----
    def test_empty_inbox_take_writes_nothing(self):
        session = control.create(self.root, "codex_delivery")
        state_path = control.control_dir(self.root) / "sessions.json"
        before = (state_path.stat().st_mtime_ns, state_path.read_bytes())
        for _ in range(10):
            control.take_instructions(self.root, session["id"])
        after = (state_path.stat().st_mtime_ns, state_path.read_bytes())
        self.assertEqual(before, after,
                         "ten empty-inbox checks (the 100ms loop) must write zero bytes")

    # ---- S-P9-003: a REAL mutation still persists exactly as before ----
    def test_real_mutation_still_persists(self):
        state_path = board.board_dir(self.root) / "state.json"
        with board.locked_state(self.root) as state:
            state["agents"]["b"] = {"id": "b", "role": "qa", "task": "REVIEW_QUEUE",
                                    "active": True, "status": "working",
                                    "poll_counter": 0, "status_note": "y",
                                    "last_status_at": board.now()}
        self.assertIn(b'"b"', state_path.read_bytes())

    def test_missing_human_projection_is_rebuilt_on_noop_lock(self):
        with board.locked_state(self.root):
            pass
        board_md = board.board_dir(self.root) / "BOARD.md"
        board_md.unlink()
        with board.locked_state(self.root) as state:
            _ = state.get("agents")
        self.assertTrue(board_md.is_file())


if __name__ == "__main__":
    unittest.main()

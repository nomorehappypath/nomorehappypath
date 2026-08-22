# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Orphan-session retirement simulations (live deadlock, owner report 2026-08-15).

Observed live: a finished task's Codex terminal stayed open ("waiting to be
stopped") after its agent record was archived. dispatch_approved_findings saw
an active session unknown to the board, concluded a launched terminal was
still registering, and waited forever — the entire approved follow-up queue
deadlocked behind one ghost terminal, with the CTO monitoring zero tasks and
the reviewer idle. These scenarios prove the fix: orphans are retired on every
controller poll after a launch grace, fresh launches still get their grace,
agent-owned sessions are never touched, and the exact live deadlock now
resolves in a single controller cycle.

Run:  PYTHONPATH=. python3 -m unittest tests.test_orphan_session_retirement -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, board_viewer, control


class OrphanSessionRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def queued_finding(self):
        finding = board.record_finding(self.root, "OLD-TASK", "Approved repair",
                                       "An approved follow-up waiting to dispatch", False)
        board.triage_finding(self.root, finding["id"], "distinct")
        board.record_finding_decision(self.root, finding["id"], "fix")
        return finding

    # ---- S-ORPH-001: an old agentless session is retired and recorded ----
    def test_old_orphan_session_is_stopped_with_event(self):
        session = control.create(self.root, "codex_delivery")
        stopped = board_viewer.retire_orphan_sessions(self.root, grace_seconds=0)
        self.assertEqual(stopped, [session["id"]])
        state = board.snapshot(self.root)
        self.assertTrue(any(e.get("kind") == "orphan_sessions_stopped" for e in state.get("events", [])),
                        "the retirement is a visible board event, not a silent kill")
        statuses = {s["id"]: s["status"] for s in control.snapshot(self.root)["sessions"]}
        self.assertNotIn(statuses[session["id"]], ("launching", "running"))

    # ---- S-ORPH-002: a fresh launch keeps its registration grace ----
    def test_fresh_session_is_not_stopped_and_dispatcher_waits(self):
        control.create(self.root, "codex_delivery")
        self.queued_finding()
        stopped = board_viewer.retire_orphan_sessions(self.root)  # default grace
        self.assertEqual(stopped, [])
        result = board_viewer.dispatch_approved_findings(self.root)
        self.assertEqual(result["status"], "terminal_registering",
                         "a genuinely registering terminal still blocks dispatch")

    # ---- S-ORPH-003: a session with a live agent is never touched ----
    def test_agent_owned_session_is_never_retired(self):
        session = control.create(self.root, "codex_delivery")
        board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                       vendor="OpenAI", session_id=session["id"])
        stopped = board_viewer.retire_orphan_sessions(self.root, grace_seconds=0)
        self.assertEqual(stopped, [])
        statuses = {s["id"]: s["status"] for s in control.snapshot(self.root)["sessions"]}
        self.assertIn(statuses[session["id"]], ("launching", "running"))

    # ---- S-ORPH-004: retirement is idempotent ----
    def test_retirement_is_idempotent(self):
        control.create(self.root, "codex_delivery")
        first = board_viewer.retire_orphan_sessions(self.root, grace_seconds=0)
        second = board_viewer.retire_orphan_sessions(self.root, grace_seconds=0)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    # ---- S-ORPH-005: the exact live deadlock resolves in one controller cycle ----
    def test_live_deadlock_replay_unblocks_in_one_cycle(self):
        control.create(self.root, "codex_delivery")  # the ghost, agentless
        self.queued_finding()
        # Before the fix this returned terminal_registering forever.
        with patch.object(board_viewer, "ORPHAN_SESSION_GRACE_SECONDS", 0), \
                patch("harness.board_viewer.launch_terminal") as launch:
            board_viewer.retire_orphan_sessions(self.root)
            result = board_viewer.dispatch_approved_findings(self.root)
        self.assertEqual(result["status"], "terminal_started",
                         "one controller cycle retires the ghost and launches the follow-up")
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()

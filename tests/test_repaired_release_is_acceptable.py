# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""A repaired release must be acceptable by the owner (harness defect, 2026-08-15).

Live defect: record_release_ready overwrote releases[task] but left the earlier
release_decision in place, while record_release_decision refuses a second
response for a task. After an owner rejection and repair, the owner could never
accept the fixed candidate — the Accept action failed forever.

Run:  PYTHONPATH=. python3 -m unittest tests.test_repaired_release_is_acceptable -v
"""
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control


class RepairedReleaseIsAcceptable(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")

    def _release(self, task, commit):
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        return board.record_release_ready(self.root, self.cto["id"], task, checks | {"head_commit": commit})

    def _task(self, task="REL"):
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                             vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], f"Deliver {task}.")
        board.begin_task(self.root, dev["id"], task)
        contract.create_contract(self.root, task, f"Deliver {task}.", ["ship"])
        return dev

    # ---- S-REREL-001 (load-bearing): reject -> repair -> re-release -> ACCEPT ----
    def test_owner_can_accept_the_repaired_candidate(self):
        self._task("REL")
        self._release("REL", "commit-one")
        board.record_release_decision(self.root, "REL", "not_accepted", reason="Window is cut.")
        # The repaired candidate is published.
        self._release("REL", "commit-two")
        accepted = board.record_release_decision(self.root, "REL", "accepted")
        self.assertEqual(accepted["decision"], "accepted",
                         "the owner must be able to accept a repaired release")

    # ---- S-REREL-002: the rejection survives as history, never silently dropped ----
    def test_previous_response_is_archived_with_its_reason(self):
        self._task("REL2")
        self._release("REL2", "c1")
        board.record_release_decision(self.root, "REL2", "not_accepted", reason="Cards clipped.")
        self._release("REL2", "c2")
        state = board.snapshot(self.root)
        # Durable history lives in the append-only event log, not state["archive"]
        # (the hot/cold split empties that list of everything but qa_requests).
        superseded = [e for e in state.get("events", [])
                      if e.get("kind") == "owner_release_response_superseded"]
        self.assertTrue(superseded, "the earlier owner response must be preserved in the event log")
        self.assertEqual(superseded[-1]["previous_decision"]["reason"], "Cards clipped.")
        self.assertEqual(superseded[-1]["superseded_by_commit"], "c2")
        self.assertNotIn("REL2", state.get("release_decisions", {}))
        self.assertNotIn("REL2", state.get("release_repairs", {}))
        # And it survives a reload from disk.
        self.assertTrue([e for e in board.snapshot(self.root).get("events", [])
                         if e.get("kind") == "owner_release_response_superseded"])

    # ---- S-REREL-003: guard — a single candidate still takes only ONE response ----
    def test_double_response_on_the_same_candidate_still_refused(self):
        self._task("REL3")
        self._release("REL3", "c1")
        board.record_release_decision(self.root, "REL3", "accepted")
        with self.assertRaisesRegex(ValueError, "already been recorded"):
            board.record_release_decision(self.root, "REL3", "not_accepted", reason="changed my mind")


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""An owner repair must be able to reach review (harness finding, 2026-08-14).

Live defect: claim_release_repair left the delivery record inactive, so the
claimed repair could never progress — an inactive agent may not post status or
request review, and route_owner_repairs skips repairs already marked
DELIVERY_REPAIR_IN_PROGRESS, so nothing recovered it.

Run:  PYTHONPATH=. python3 -m unittest tests.test_owner_repair_reaches_review -v
"""
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control


class OwnerRepairReachesReview(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _released_task(self, task="REL"):
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                             vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], f"Deliver {task}.")
        board.begin_task(self.root, dev["id"], task)
        contract.create_contract(self.root, task, f"Deliver {task}.", ["ship"])
        with board.locked_state(self.root) as state:
            state["releases"][task] = {"task": task, "status": "VISUAL_TEST_REQUIRED",
                                       "cto_id": "cto", "recorded_at": board.now(),
                                       "head_commit": "abc123"}
            # The delivering agent is inactive by the time the owner responds —
            # exactly the live situation.
            state["agents"][dev["id"]].update({"active": False, "status": "done"})
        return dev, task

    # ---- S-REPAIR-001 (load-bearing) ----
    def test_owner_rejection_auto_reactivates_inactive_delivery_with_live_terminal(self):
        dev, task = self._released_task()
        board.record_release_decision(self.root, task, "not_accepted", reason="Window is cut.")
        agent = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertTrue(agent["active"], "the router must revive the existing live Delivery terminal")
        self.assertEqual(agent["status"], "repairing")
        self.assertNotEqual(agent.get("liveness"), "offline")
        repair = board.snapshot(self.root)["release_repairs"][task]
        self.assertEqual(repair["status"], "DELIVERY_REPAIR_IN_PROGRESS")
        routed = control.take_instructions(self.root, dev["session_id"])
        self.assertEqual(len([item for item in routed if item["source"] == "owner-release-decision"]), 1)

    # ---- S-REPAIR-002: the reactivated agent can actually work ----
    def test_reactivated_agent_can_post_status(self):
        dev, task = self._released_task("REL2")
        board.record_release_decision(self.root, task, "not_accepted", reason="Cut off.")
        event = board.status(self.root, dev["id"], "repairing the clipped card")
        self.assertEqual(event["kind"], "status_update")

    # ---- S-REPAIR-004: a SUPERSEDED record may never be resurrected ----
    def test_superseded_record_cannot_claim_the_repair(self):
        dev, task = self._released_task("REL4")
        with board.locked_state(self.root) as state:
            state["agents"][dev["id"]]["status"] = "superseded"
        board.record_release_decision(self.root, task, "not_accepted", reason="Cut off.")
        with self.assertRaisesRegex(ValueError, "superseded|terminal has ended"):
            board.claim_release_repair(self.root, dev["id"], task)
        self.assertFalse(board.snapshot(self.root)["agents"][dev["id"]]["active"],
                         "a superseded record must stay inactive; the replacement agent owns the task")

    # ---- S-REPAIR-005: a dead terminal cannot claim either ----
    def test_dead_terminal_cannot_claim_the_repair(self):
        dev, task = self._released_task("REL5")
        control.fail_launch(self.root, dev["session_id"], "terminal ended")
        board.record_release_decision(self.root, task, "not_accepted", reason="Cut off.")
        with self.assertRaisesRegex(ValueError, "terminal has ended|superseded"):
            board.claim_release_repair(self.root, dev["id"], task)

    def test_explicitly_offline_record_is_not_revived_by_stale_controller_state(self):
        dev, task = self._released_task("REL-OFFLINE")
        with board.locked_state(self.root) as state:
            state["agents"][dev["id"]].update({"active": True, "status": "working"})
        board.offline(self.root, dev["id"], "terminal transport ended", transport_ended=True)
        board.record_release_decision(self.root, task, "not_accepted", reason="Cut off.")
        agent = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertFalse(agent["active"])
        self.assertEqual(agent["liveness"], "offline")
        with self.assertRaisesRegex(ValueError, "terminal has ended|superseded"):
            board.claim_release_repair(self.root, dev["id"], task)

    # ---- S-REPAIR-003: guard — an accepted release does not reactivate anyone ----
    def test_acceptance_does_not_reactivate(self):
        dev, task = self._released_task("REL3")
        board.record_release_decision(self.root, task, "accepted")
        # Acceptance never revives the delivering agent: it is either absent from
        # the hot window (archived) or still explicitly inactive.
        agent = board.snapshot(self.root)["agents"].get(dev["id"])
        self.assertFalse(agent.get("active") if agent else False,
                         "acceptance must not reactivate the delivering agent")


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The CTO must never block or idle waiting on the product owner.

The reported violation: the CTO asked the owner a question and then waited for an
answer. That must never happen. Every owner touchpoint is an asynchronous board
surface the CTO posts and continues past; an owner rejection is routed back into a
repair cycle automatically. The CTO's only job is monitoring and verification — it
stands by healthy when the only outstanding item is an owner decision, and it never
makes an owner reply a precondition for its own next action.

These simulations pin the structural invariants that back the directive rule
("The CTO never waits on the owner"):
  * the CTO's machine-generated monitoring instruction forbids waiting and carries
    USER ACTION: None;
  * a released task awaiting the owner's visual test never marks the CTO stalled or
    blocked — it is benign healthy standby;
  * an owner rejection auto-routes a repair to Delivery with zero CTO involvement;
  * the viewer never describes the CTO as "waiting" on the owner;
  * the behavioral directive carries the explicit rule.

Run:  PYTHONPATH=. python3 -m unittest tests.test_cto_never_waits_on_owner -v
"""
import tempfile
import unittest
from pathlib import Path

from harness import board, board_viewer, control


def _directive_text() -> str:
    return (Path(board.__file__).resolve().parent / "directives" / "CTO_COMPLETION_DIRECTIVE.md").read_text()


class CtoNeverWaitsOnOwner(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    # ---- S-CTO-001 ----
    def test_cto_monitoring_instruction_forbids_waiting_on_owner(self):
        instr = board._automatic_recovery_instruction({"role": "cto", "task": "GLOBAL_MONITOR"})
        self.assertTrue(instr.rstrip().endswith("USER ACTION: None."),
                        "the CTO instruction must end with USER ACTION: None.")
        self.assertIn("Never wait on the product owner", instr)
        self.assertIn("stand by healthy", instr)
        # It must not tell the CTO to hold for a reply.
        self.assertNotIn("wait for the owner's reply", instr.lower())

    # ---- S-CTO-002 (the violation state: released, awaiting owner, delivery finished) ----
    def test_released_task_awaiting_owner_never_stalls_the_cto(self):
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                             vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "Ship the feature.")
        board.begin_task(self.root, dev["id"], "REL-TASK")
        with board.locked_state(self.root) as state:
            # Delivery has finished; the release is presented for the owner's visual test.
            state["agents"][dev["id"]].update({"active": False, "status": "done"})
            state["releases"]["REL-TASK"] = {
                "task": "REL-TASK", "status": "VISUAL_TEST_REQUIRED", "recorded_at": board.now(),
                "cto_id": cto["id"],
            }
            # The CTO's heartbeat is old and it is mid-recovery — the standby path must
            # resolve it to HEALTHY, never to stalled/blocked-on-owner.
            state["agents"][cto["id"]].update({
                "liveness": "recovering",
                "last_poll_at": "2020-01-01T00:00:00+00:00",
                "last_status_at": "2020-01-01T00:00:00+00:00",
            })

        stalled = board.mark_stalled(self.root, stale_seconds=1)

        self.assertFalse(any(event.get("agent_id") == cto["id"] for event in stalled),
                         "the CTO was marked stalled while merely awaiting the owner's test")
        after = board.snapshot(self.root)["agents"][cto["id"]]
        self.assertEqual(after["liveness"], "healthy",
                         "a CTO awaiting an owner decision must be healthy standby, not blocked")
        self.assertNotEqual(after["liveness"], "stalled")

    # ---- S-CTO-003 (owner response advances the pipeline with no CTO involvement) ----
    def test_owner_rejection_auto_routes_repair_without_the_cto(self):
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                             vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "Ship the feature.")
        board.begin_task(self.root, dev["id"], "REL-TASK")
        with board.locked_state(self.root) as state:
            state["releases"]["REL-TASK"] = {
                "task": "REL-TASK", "status": "VISUAL_TEST_REQUIRED",
                "recorded_at": board.now(), "head_commit": "abc123", "cto_id": "cto",
            }

        board.record_release_decision(self.root, "REL-TASK", "not_accepted", reason="Please fix the header.")
        repairs = board.snapshot(self.root)["release_repairs"]
        self.assertEqual(
            repairs["REL-TASK"]["status"], "DELIVERY_REPAIR_IN_PROGRESS",
            "the owner decision event must route Delivery without a CTO or dashboard poll",
        )
        self.assertEqual(board.route_owner_repairs(self.root), [])

    # ---- S-CTO-004 (the viewer never frames the CTO as waiting on the owner) ----
    def test_viewer_never_shows_the_cto_waiting_on_the_owner(self):
        page = board_viewer.rendered_page()
        self.assertNotIn("CTO: waiting", page,
                         "the viewer must never describe the CTO as waiting on the owner")
        self.assertIn("CTO: monitoring requirements capture", page)

    # ---- S-CTO-005 (the behavioral contract carries the rule) ----
    def test_directive_states_the_cto_never_waits_on_the_owner(self):
        # Collapse whitespace so line-wrapped phrases still match (the reviewer's
        # multiline-aware method), rather than depending on where lines break.
        directive = " ".join(_directive_text().split())
        self.assertIn("The CTO never waits on the owner", directive)
        self.assertIn("awaiting owner decision", directive)
        self.assertIn("asynchronous board surface", directive)
        self.assertIn("never makes an owner reply a precondition", directive)


if __name__ == "__main__":
    unittest.main()

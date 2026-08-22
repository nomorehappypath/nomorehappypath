# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The CTO must act in near-real time, never overnight (owner report, 2026-08-15).

Two defects: (1) nothing woke the CTO when a final acceptance PASSED — its only
driver was watchdog staleness; (2) the CTO counted as having work ONLY while a
delivery agent was active, so with delivery quiet and release checks pending it
sat in standby, which the watchdog never nudges — a finished task could wait
overnight.

Run:  PYTHONPATH=. python3 -m unittest tests.test_cto_realtime_wake -v
"""
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control
from tests.requirements_support import agreed_requirements

def _ledger_text(command, scenario):
    return (
        "| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
        "|---|---|---|---|---|---|---|\n"
        f"| S-X-001 | A completed independent check routes the next required release action without waiting overnight. | {scenario} | `{command}` | works | PASS: observed | PASS |\n"
    )


class CtoRealtimeWake(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "test_smoke.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(True)\n")
        (self.root / "test_challenge.py").write_text(
            "import unittest\n\nclass Challenge(unittest.TestCase):\n    def test_release_route(self): self.assertTrue(True)\n")
        self.cto_session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic",
                                  session_id=self.cto_session["id"])
        dev_session = control.create(self.root, "codex_delivery")
        self.dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                                  vendor="OpenAI", session_id=dev_session["id"])
        board.record_owner_direction(self.root, dev_session["id"], "Deliver the app.")
        board.begin_task(self.root, self.dev["id"], "APP")
        contract.create_contract(self.root, "APP", "Deliver the app.", ["ship"])
        self.acceptance = self.root / "acceptance.txt"
        self.acceptance.write_text("verified\n")
        qa_session = control.create(self.root, "claude_reviewer")
        self.qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic",
                                 session_id=qa_session["id"])

    def _ledger(self, name):
        path = self.root / "docs" / f"{name}.md"
        path.parent.mkdir(exist_ok=True)
        command = ("python3 -m unittest test_challenge" if "challenge" in name
                   else "python3 -m unittest test_smoke")
        scenario = ("independent release-route challenge" if "challenge" in name
                    else "delivery release-route evidence")
        path.write_text(_ledger_text(command, scenario))
        return str(path.relative_to(self.root))

    def _evidence(self, name):
        path = self.root / "evidence" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("command: python3 -m unittest\nresult: PASS\n")
        return str(path.relative_to(self.root))

    def _run_review(self, phase, result, *, contract_ready=True):
        if contract_ready and not contract.contract_complete(self.root, "APP")[0]:
            contract.add_evidence(self.root, "APP", "ship", [self.acceptance])
        agreed_requirements(self.root, self.dev["id"],
                                              "Final agreed requirements for APP: ship it end to end.")
        board.define_delivery_plan(self.root, self.dev["id"], "atomic", "one cohesive task")
        request = board.request_review(self.root, self.dev["id"], self._ledger("delivery"),
                                       "review it", phase=phase,
                                       test_command="python3 -m unittest test_smoke")
        board.claim_qa(self.root, self.qa["id"], request["id"], self._ledger("challenge"))
        if result == "passed":
            board.execute_challenge(self.root, self.qa["id"], request["id"])
        return board.qa_result(self.root, self.qa["id"], request["id"], result,
                               f"S-1 {result}", self._evidence(f"qa-{result}.txt"))

    # ---- S-RT-001: final PASS wakes Delivery; completion wakes the CTO ----
    def test_final_pass_wakes_cto_immediately(self):
        recorded = self._run_review("final_acceptance", "passed")
        delivery_wakes = control.take_instructions(self.root, self.dev["session_id"])
        self.assertTrue(any("Validate the Completion Contract and call complete now" in item["text"]
                            for item in delivery_wakes))
        board.complete(self.root, self.dev["id"], "completion gates satisfied")
        queued = control.take_instructions(self.root, self.cto_session["id"])
        texts = [item["text"] for item in queued]
        self.assertTrue(any("DEVELOPMENT COMPLETE" in text for text in texts),
                        f"the CTO must be woken when completion becomes eligible; got {texts}")
        state = board.snapshot(self.root)
        self.assertFalse(state["agents"][self.dev["id"]]["active"])
        completions = [
            event for event in state["events"]
            if event.get("kind") == "development_complete" and event.get("task") == "APP"
        ]
        self.assertEqual(len(completions), 1)
        repeated = board.complete(self.root, self.dev["id"], "legacy duplicate completion")
        self.assertEqual(repeated["sequence"], completions[0]["sequence"])
        self.assertEqual(recorded["status"], "passed")

    def test_incomplete_contract_preserves_final_pass_and_routes_only_remaining_work(self):
        recorded = self._run_review("final_acceptance", "passed", contract_ready=False)
        self.assertEqual(recorded["status"], "passed")
        state = board.snapshot(self.root)
        delivery = state["agents"][self.dev["id"]]
        self.assertTrue(delivery["active"])
        self.assertEqual(delivery["status"], "independent_review_passed")
        delivery_wakes = control.take_instructions(self.root, delivery["session_id"])
        self.assertTrue(any("Do not rerun the already-passed final review" in item["text"]
                            for item in delivery_wakes))
        cto_wakes = control.take_instructions(self.root, self.cto_session["id"])
        self.assertFalse(any("DEVELOPMENT COMPLETE" in item["text"] for item in cto_wakes))

    # ---- S-RT-002 (the overnight hole): quiet delivery + pending release = CTO work ----
    def test_pending_release_checks_count_as_cto_work(self):
        self._run_review("final_acceptance", "passed")
        with board.locked_state(self.root) as state:
            state["agents"][self.dev["id"]].update({"active": False, "status": "done"})
        state = board.snapshot(self.root)
        self.assertTrue(
            board._agent_has_actionable_work(state, state["agents"][self.cto["id"]]),
            "with release checks pending, the CTO must be nudgeable even when delivery is quiet")
        # And the watchdog actually routes it rather than parking it in standby.
        with board.locked_state(self.root) as state:
            state["agents"][self.cto["id"]].update({
                "last_poll_at": "2020-01-01T00:00:00+00:00",
                "last_status_at": "2020-01-01T00:00:00+00:00",
                "spawned_at": "2020-01-01T00:00:00+00:00",
            })
        board.mark_stalled(self.root, stale_seconds=1)
        after = board.snapshot(self.root)["agents"][self.cto["id"]]
        self.assertEqual(after["recovery_state"], "automatic_requested",
                         "the watchdog must nudge the CTO, not park it in standby")

    # ---- S-RT-003 (no flap): a FAILED final never pages the CTO; a recorded release ends the work ----
    def test_no_wake_or_work_when_not_due(self):
        self._run_review("final_acceptance", "failed")
        queued = control.take_instructions(self.root, self.cto_session["id"])
        self.assertFalse(any("RELEASE CHECKS" in item["text"] for item in queued),
                         "a failed final acceptance must not page the CTO for release checks")
        with board.locked_state(self.root) as state:
            for request in state.get("qa_requests", {}).values():
                request.update({"phase": "final_acceptance", "status": "passed"})
            state["agents"][self.dev["id"]].update({"active": False})
            state.setdefault("releases", {})["APP"] = {
                "task": "APP", "status": "VISUAL_TEST_REQUIRED", "cto_id": self.cto["id"],
                "recorded_at": board.now()}
        state = board.snapshot(self.root)
        self.assertFalse(
            board._agent_has_actionable_work(state, state["agents"][self.cto["id"]]),
            "a recorded release ends the pending-checks condition (no flapping)")

    # ---- S-RT-004: the wake is best-effort — a dead CTO session never breaks the verdict ----
    def test_wake_is_best_effort(self):
        control.fail_launch(self.root, self.cto_session["id"], "terminal ended")
        recorded = self._run_review("final_acceptance", "passed")
        self.assertEqual(recorded["status"], "passed", "qa_result must succeed even if no CTO session is reachable")


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""History keeps EVERY test ledger; settled reviews are immutable.

Owner-reported loss (2026-08-15): after a task with five subtask reviews and a
repair cycle, history showed only the corrective cycle's ledger. Two causes:
(1) the history projection returned a single newest ledger per task by design;
(2) stopping a reviewer reopened every request it had ever handled, nulling the
challenge-ledger reference on settled reviews.

Run:  PYTHONPATH=. python3 -m unittest tests.test_history_keeps_all_ledgers -v
"""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, board_viewer, contract, control

LEDGER = (
    "| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
    "|---|---|---|---|---|---|---|\n"
    "| {sid} | The saved {name} behavior remains understandable after this attempt. | scenario for {name} | `python3 -m unittest x` | works | PASS: observed | PASS |\n"
)


class HistoryKeepsAllLedgers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        session = control.create(self.root, "codex_delivery")
        self.dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                                  vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "Deliver the app.")
        board.begin_task(self.root, self.dev["id"], "APP")
        contract.create_contract(self.root, "APP", "Deliver the app.", ["ship"])

    def _ledger_file(self, name: str) -> str:
        path = self.root / "docs" / f"{name}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(LEDGER.format(sid=f"S-{name.upper()}-001", name=name))
        return str(path.relative_to(self.root))

    def _settled_request(self, rid, phase, subtask, cycle, status, ledger_name):
        return {
            "id": rid, "task": "APP", "stage": "independent_review", "phase": phase,
            "subtask": subtask, "chunk": "", "cycle": cycle, "status": status,
            "result": status, "developer_id": self.dev["id"],
            "requested_at": f"2026-08-15T0{cycle}:00:00+00:00",
            "completed_at": f"2026-08-15T0{cycle}:30:00+00:00",
            "ledger": self._ledger_file(ledger_name),
            "claimed_by": "qa-old", "challenge_ledger": f"docs/challenge-{ledger_name}.md",
            "review_wait_started_at": f"2026-08-15T0{cycle}:00:00+00:00",
            "review_wait_stopped_at": f"2026-08-15T0{cycle}:30:00+00:00",
            "reviewed_commit": "", "structure_revision": 0, "route_state": "settled",
            "reserved_by": None, "reserved_at": None, "routed_to": None,
            "routed_session_id": "", "routed_at": None,
        }

    def _state_with_reviews(self, reviews):
        with board.locked_state(self.root) as state:
            for request in reviews:
                state.setdefault("qa_requests", {})[request["id"]] = copy.deepcopy(request)

    # ---- S-LEDG-001 (load-bearing): history carries ALL ledgers, not the newest ----
    def test_history_projects_every_ledger(self):
        self._state_with_reviews([
            self._settled_request("r1", "subtask_acceptance", "overview", 1, "passed", "overview"),
            self._settled_request("r2", "subtask_acceptance", "settings", 2, "passed", "settings"),
            self._settled_request("r3", "final_acceptance", "", 3, "failed", "final-fail"),
            self._settled_request("r4", "final_acceptance", "", 4, "passed", "final-pass"),
        ])
        state = board.snapshot(self.root)
        all_views = board_viewer._task_test_ledgers(self.root, state, "APP")
        self.assertEqual(len(all_views), 4, "every readable ledger must be projected")
        self.assertEqual(all_views[0]["attempt_label"], "First recorded attempt")
        self.assertEqual(all_views[1]["attempt_label"], "Earlier recorded attempt")
        self.assertIn("found a problem", all_views[2]["attempt_status"])
        self.assertEqual(all_views[3]["attempt_label"], "Latest recorded attempt")
        for v in all_views:
            self.assertTrue(v["scenarios"], f"scenarios must be readable for {v['source']}")
        # The singular projection is unchanged: newest readable.
        newest = board_viewer._task_test_ledger(self.root, state, "APP")
        self.assertEqual(newest["request_id"], "r4")

    # ---- S-LEDG-002: the history payload carries the list end to end ----
    def test_history_payload_contains_all(self):
        self._state_with_reviews([
            self._settled_request("r1", "subtask_acceptance", "overview", 1, "passed", "overview"),
            self._settled_request("r4", "final_acceptance", "", 4, "passed", "final-pass"),
        ])
        with board.locked_state(self.root) as state:
            state["agents"][self.dev["id"]].update({"active": False, "status": "done"})
            state.setdefault("releases", {})["APP"] = {
                "task": "APP", "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto",
                "recorded_at": board.now()}
            state.setdefault("release_decisions", {})["APP"] = {
                "task": "APP", "decision": "accepted", "reason": "", "attachments": [],
                "recorded_at": board.now()}
        payload = board_viewer.history_payload(self.root)
        item = next(i for i in payload["task_history"] if i["task"] == "APP")
        self.assertEqual(len(item["test_ledgers"]), 2,
                         "history item must list every ledger, main and corrective alike")
        self.assertEqual(item["test_ledger"]["request_id"], "r4")
        # The page renders the full list, not the singular field alone.
        self.assertIn("item.test_ledgers", board_viewer.rendered_page())

    # ---- S-LEDG-003 (load-bearing): stopping a reviewer never touches settled reviews ----
    def test_reviewer_stop_preserves_settled_reviews(self):
        qa_session = control.create(self.root, "claude_reviewer")
        qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic",
                            session_id=qa_session["id"])
        settled = [
            self._settled_request("s1", "subtask_acceptance", "overview", 1, "passed", "ovr"),
            self._settled_request("s2", "final_acceptance", "", 2, "failed", "fin"),
        ]
        for request in settled:
            request["claimed_by"] = qa["id"]
        self._state_with_reviews(settled)
        with board.locked_state(self.root) as state:
            state["qa_requests"]["live1"] = {
                "id": "live1", "task": "APP", "stage": "independent_review",
                "phase": "subtask_acceptance", "subtask": "extra", "chunk": "", "cycle": 3,
                "status": "claimed", "developer_id": self.dev["id"],
                "requested_at": board.now(), "claimed_by": qa["id"],
                "challenge_ledger": "docs/challenge-live.md", "ledger": "docs/x.md",
                "review_wait_started_at": board.now(), "review_wait_stopped_at": None,
                "reviewed_commit": "", "structure_revision": 0, "route_state": "claimed",
                "reserved_by": None, "reserved_at": None, "routed_to": None,
                "routed_session_id": "", "routed_at": None, "completed_at": None, "result": None,
            }
        before = {r["id"]: copy.deepcopy(r) for r in settled}
        board.cancel_session_work(self.root, qa_session["id"])
        state = board.snapshot(self.root)
        for rid, prior in before.items():
            after = state["qa_requests"][rid]
            self.assertEqual(after["status"], prior["status"], "settled verdicts are immutable")
            self.assertEqual(after["challenge_ledger"], prior["challenge_ledger"],
                             "a settled review keeps its challenge ledger")
            self.assertEqual(after["claimed_by"], prior["claimed_by"],
                             "a settled review keeps its reviewer attribution")
        live = state["qa_requests"]["live1"]
        self.assertEqual(live["status"], "open", "the unfinished claim returns to the queue")
        self.assertIsNone(live["claimed_by"])
        self.assertEqual(live["route_state"], "reviewer_stopped_reopened")


if __name__ == "__main__":
    unittest.main()

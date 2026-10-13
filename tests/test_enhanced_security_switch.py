# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner 2026-10-12: one Enhanced security checkbox per task, locked at the first review request.

"have a check box per task and not per project, and once task is reviewed we
cannot changed it". The board stores the choice, agents read it from poll and
the review brief, the first independent-review request locks and freezes it,
and security ledger rows (S-SEC-) are never served from the execution cache.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, certified_execution, contract, control, execution_identity
from tests.environment_support import require_loopback
from tests.requirements_support import agreed_requirements
from tests.test_requirements_contract import context, delivery_with_direction


def _state_path(root: Path) -> Path:
    return root / ".harness" / "board" / "state.json"


def _edit_state(root: Path, change) -> None:
    path = _state_path(root)
    state = json.loads(path.read_text())
    change(state)
    path.write_text(json.dumps(state))


class EnhancedSecurityChoiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))

    def test_a_new_task_is_off_and_unlocked_and_every_agent_reads_it_from_poll(self):
        agent = delivery_with_direction(self.root, "TASK-SEC")
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertEqual(bundle["task"], "TASK-SEC")
        self.assertEqual(bundle["enhanced_security"]["on"], False)
        self.assertEqual(bundle["enhanced_security"]["locked"], False)
        self.assertEqual(bundle["enhanced_security"]["revision"], 0)
        self.assertIn("SECURITY.md", bundle["enhanced_security"]["rule"])
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        seen = board.poll(self.root, reviewer["id"])["context_bundle"]["tasks"]
        self.assertEqual([task["enhanced_security"]["on"] for task in seen if task["task"] == "TASK-SEC"], [False])

    def test_the_owner_turns_it_on_and_off_with_an_event_a_revision_and_a_delivery_notice(self):
        agent = delivery_with_direction(self.root, "TASK-SEC")
        result = board.set_task_security(self.root, "TASK-SEC", True)
        self.assertEqual(result["security"]["on"], True)
        self.assertEqual(result["security"]["revision"], 1)
        self.assertEqual(result["event"]["kind"], "enhanced_security_set")
        self.assertTrue(result["notified"], "the live Delivery terminal was not told")
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertEqual(bundle["enhanced_security"], {**bundle["enhanced_security"], "on": True, "locked": False, "revision": 1})
        again = board.set_task_security(self.root, "TASK-SEC", False)
        self.assertEqual(again["security"]["on"], False)
        self.assertEqual(again["security"]["revision"], 2)
        kinds = [event["kind"] for event in board.snapshot(self.root)["events"] if event["kind"].startswith("enhanced_security")]
        self.assertEqual(kinds, ["enhanced_security_set", "enhanced_security_set"])

    def test_a_saved_choice_whose_notice_cannot_be_routed_is_recorded_not_hidden(self):
        delivery_with_direction(self.root, "TASK-SEC")
        with patch.object(control, "enqueue_instruction", side_effect=ValueError("terminal gone")):
            result = board.set_task_security(self.root, "TASK-SEC", True)
        self.assertEqual(result["security"]["on"], True)
        self.assertFalse(result["notified"])
        kinds = [event["kind"] for event in board.snapshot(self.root)["events"]]
        self.assertIn("enhanced_security_notice_failed", kinds)

    def test_a_paused_project_refuses_the_change(self):
        delivery_with_direction(self.root, "TASK-SEC")
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        with self.assertRaises(board.ProjectPausedError):
            board.set_task_security(self.root, "TASK-SEC", True)

    def test_wrong_value_unknown_task_and_cancelled_task_are_refused_plainly(self):
        delivery_with_direction(self.root, "TASK-SEC")
        with self.assertRaisesRegex(ValueError, "true or false"):
            board.set_task_security(self.root, "TASK-SEC", "yes")
        with self.assertRaisesRegex(ValueError, "no such task"):
            board.set_task_security(self.root, "TASK-NOWHERE", True)
        board.cancel_task(self.root, "TASK-SEC")
        self.assertNotIn("TASK-SEC", board.snapshot(self.root).get("task_security", {}))
        with self.assertRaisesRegex(ValueError, "cancelled"):
            board.set_task_security(self.root, "TASK-SEC", True)

    def test_a_task_from_before_this_feature_reads_off_and_is_locked_by_any_review_history(self):
        delivery_with_direction(self.root, "TASK-OLD")
        _edit_state(self.root, lambda state: state.get("task_security", {}).pop("TASK-OLD", None))
        self.assertEqual(board.task_security_state(board.snapshot(self.root), "TASK-OLD"), {
            "on": False, "locked": False, "revision": 0, "updated_at": ""})
        # archival keeps only the index: a reviewed task stays locked after it
        _edit_state(self.root, lambda state: state.setdefault("qa_request_index", {}).update({
            "review-TASK-OLD-final-01": {"id": "review-TASK-OLD-final-01", "task": "TASK-OLD", "stage": board.INDEPENDENT_REVIEW},
        }))
        self.assertTrue(board.task_security_state(board.snapshot(self.root), "TASK-OLD")["locked"])
        with self.assertRaisesRegex(ValueError, "locked"):
            board.set_task_security(self.root, "TASK-OLD", True)
        # a first request whose Delivery evidence failed also counts
        _edit_state(self.root, lambda state: (state["qa_request_index"].clear(), state.setdefault("delivery_attempt_failures", []).append(
            {"id": "review-TASK-OLD-final-01", "task": "TASK-OLD", "stage": board.INDEPENDENT_REVIEW, "status": "cancelled"})))
        self.assertTrue(board.task_security_state(board.snapshot(self.root), "TASK-OLD")["locked"])
        # development QA alone is not a review request
        _edit_state(self.root, lambda state: (state["delivery_attempt_failures"].clear(), state["qa_request_index"].update(
            {"qa-1": {"id": "qa-1", "task": "TASK-OLD", "stage": board.DEVELOPMENT_QA}})))
        self.assertFalse(board.task_security_state(board.snapshot(self.root), "TASK-OLD")["locked"])

    def test_the_dashboard_carries_the_computed_record_for_live_tasks_only(self):
        delivery_with_direction(self.root, "TASK-SEC")
        board.set_task_security(self.root, "TASK-SEC", True)
        payload = board_viewer.dashboard_payload(self.root)
        self.assertEqual(payload["state"]["task_security"]["TASK-SEC"]["on"], True)
        self.assertEqual(payload["state"]["task_security"]["TASK-SEC"]["locked"], False)
        _edit_state(self.root, lambda state: state.setdefault("qa_request_index", {}).update(
            {"r": {"id": "r", "task": "TASK-SEC", "stage": board.INDEPENDENT_REVIEW}}))
        self.assertTrue(board_viewer.dashboard_payload(self.root)["state"]["task_security"]["TASK-SEC"]["locked"])


class FirstReviewRequestLocksTests(unittest.TestCase):
    """The lock and the frozen value travel with the real request_review path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "test_smoke.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(True)\n")
        (self.root / "test_broken.py").write_text(
            "import unittest\n\nclass Broken(unittest.TestCase):\n    def test_fails(self): self.fail('no')\n")

    def delivery(self, task: str):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        objective = f"OWNER DIRECTION — {task}"
        board.record_owner_direction(self.root, session["id"], objective)
        board.begin_task(self.root, agent["id"], task)
        agreed_requirements(self.root, agent["id"], f"Final agreed requirements for {task}: preserve the requested delivery and verify it end to end.")
        contract.create_contract(self.root, task, objective, ["delivery"])
        board.define_delivery_plan(self.root, agent["id"], "atomic", "Test fixture is one cohesive task")
        return agent

    def ledger(self, name: str, rows: list[tuple[str, str]]) -> str:
        path = self.root / "docs" / name
        path.parent.mkdir(exist_ok=True)
        lines = ["| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |",
                 "|---|---|---|---|---|---|"]
        for scenario_id, command in rows:
            lines.append(f"| {scenario_id} | The {scenario_id} behavior holds without side effects. | `{command}` | "
                         "The targeted harness behavior is observed without side effects | "
                         "PASS: targeted simulation ran and the expected response was observed | PASS |")
        path.write_text("\n".join(lines) + "\n")
        return str(path.relative_to(self.root))

    def test_the_first_request_locks_the_choice_and_freezes_it_into_the_request_and_brief(self):
        agent = self.delivery("TASK-LOCK")
        board.set_task_security(self.root, "TASK-LOCK", True)
        ledger = self.ledger("ledger.md", [("S-001", "python3 -m unittest test_smoke")])
        board.request_review(self.root, agent["id"], ledger, "final", phase="final_acceptance",
                             test_command="python3 -m unittest test_smoke")
        state = board.snapshot(self.root)
        self.assertTrue(state["task_security"]["TASK-LOCK"]["locked"])
        request = next(value for value in state["qa_requests"].values() if value["task"] == "TASK-LOCK")
        self.assertEqual(request["enhanced_security"], {"on": True, "revision": 1})
        self.assertEqual(request["review_brief"]["enhanced_security"], {"on": True, "revision": 1})
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertTrue(bundle["enhanced_security"]["locked"])
        self.assertEqual(bundle["latest_review"]["enhanced_security"], {"on": True, "revision": 1})
        with self.assertRaisesRegex(ValueError, "locked: its first review request has been made"):
            board.set_task_security(self.root, "TASK-LOCK", False)

    def test_the_atomic_independent_review_path_also_locks_and_freezes_the_choice(self):
        """Review finding 2026-10-12: request_independent_review built its request without the choice, so the brief said Off."""
        agent = self.delivery("TASK-ATOMIC")
        board.set_task_security(self.root, "TASK-ATOMIC", True)
        qa = board.register(self.root, "qa", "QA-QUEUE", vendor="Anthropic")
        ledger = self.ledger("ledger.md", [("S-001", "python3 -m unittest test_smoke")])
        dev_qa = board.request_qa(self.root, agent["id"], ledger, "development QA")
        board.claim_qa(self.root, qa["id"], dev_qa["id"])
        evidence = self.root / "evidence" / "dev-qa.txt"
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text("command: python3 -m unittest\nresult: PASS\n")
        board.qa_result(self.root, qa["id"], dev_qa["id"], "passed", "ledger executed", str(evidence.relative_to(self.root)))
        self.assertFalse(board.task_security_state(board.snapshot(self.root), "TASK-ATOMIC")["locked"])
        review = board.request_independent_review(self.root, agent["id"], "challenge the assumptions")
        self.assertEqual(review["enhanced_security"], {"on": True, "revision": 1})
        board.claim_qa(self.root, qa["id"], review["id"], self.ledger("review-ledger.md", [("S-001", "python3 -m unittest test_smoke -k passes")]))
        brief = board.review_brief(self.root, review["id"], qa["id"])
        self.assertEqual(brief["enhanced_security"], {"on": True, "revision": 1})
        state = board.snapshot(self.root)
        self.assertTrue(state["task_security"]["TASK-ATOMIC"]["locked"])
        with self.assertRaisesRegex(ValueError, "locked"):
            board.set_task_security(self.root, "TASK-ATOMIC", False)

    def test_a_request_whose_evidence_fails_still_locks_the_choice(self):
        agent = self.delivery("TASK-FAIL")
        ledger = self.ledger("ledger.md", [("S-001", "python3 -m unittest test_smoke")])
        with self.assertRaises(ValueError):
            board.request_review(self.root, agent["id"], ledger, "final", phase="final_acceptance",
                                 test_command="python3 -m unittest test_broken")
        state = board.snapshot(self.root)
        self.assertTrue(board.task_security_state(state, "TASK-FAIL")["locked"])
        # even with the stored flag gone, the failed attempt itself locks it
        _edit_state(self.root, lambda value: value["task_security"]["TASK-FAIL"].update({"locked": False}))
        self.assertTrue(board.task_security_state(board.snapshot(self.root), "TASK-FAIL")["locked"])
        with self.assertRaisesRegex(ValueError, "locked"):
            board.set_task_security(self.root, "TASK-FAIL", True)

    def test_security_rows_run_fresh_even_when_an_ordinary_row_shares_their_command(self):
        self.delivery("TASK-ROWS")
        ledger = self.root / self.ledger("ledger.md", [
            ("S-001", "python3 -m unittest test_smoke"),
            ("S-SEC-1", "python3 -m unittest test_smoke"),
            ("S-002", "python3 -m unittest test_smoke -k passes"),
        ])
        calls = []

        def fake(command, root, *, measurement=None, certification=None, live_board_root=None, reuse=True):
            calls.append((command, reuse))
            return "Ran 1 test in 0.001s\n\nOK"

        with patch.object(board, "_execute_internal_qa", side_effect=fake):
            results = board._execute_scenario_simulations(self.root, ledger)
        self.assertEqual(calls, [("python3 -m unittest test_smoke", False), ("python3 -m unittest test_smoke -k passes", True)])
        self.assertEqual([row["id"] for row in results], ["S-001", "S-SEC-1", "S-002"])
        self.assertEqual(results[1]["cache_decision"], "same_request_deduplicated")


class CertifiedRunnerNeverReusesSecurityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "test_smoke.py").write_text(
            "import unittest\nclass Smoke(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n")
        self.environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        self.candidate = execution_identity.candidate_evidence_identity("c" * 40, "t" * 40, "contract-1", {"ledger": "l" * 64})

    def execute(self, *, reuse: bool):
        return certified_execution.run(
            self.root, self.root, "python3 -m unittest test_smoke", candidate=self.candidate,
            environment_sha256="e" * 64, environment=self.environment, lockfile_digests={},
            role="delivery", gate="final_acceptance", reuse=reuse,
        )

    def test_a_security_row_executes_again_where_an_ordinary_row_would_be_served_from_the_cache(self):
        first = self.execute(reuse=True)
        self.assertNotEqual(first["measurement"]["cache_decision"], "exact_success_reused")
        self.assertEqual(self.execute(reuse=True)["measurement"]["cache_decision"], "exact_success_reused")
        fresh = self.execute(reuse=False)
        self.assertNotEqual(fresh["measurement"]["cache_decision"], "exact_success_reused")
        self.assertGreater(fresh["measurement"]["duration_seconds"], 0)


    def test_a_security_run_that_now_fails_reports_its_own_output_and_keeps_the_earlier_success(self):
        """Review non-blocking finding: a changed advisory failed with an append-only bookkeeping error, hiding the scanner's message."""
        self.assertEqual(self.execute(reuse=True)["output"].count("OK"), 1)
        (self.root / "test_smoke.py").write_text(
            "import unittest\nclass Smoke(unittest.TestCase):\n    def test_ok(self): self.fail('ADVISORY GHSA-1 now affects package foo')\n")
        with self.assertRaises(ValueError) as caught:
            self.execute(reuse=False)
        message = str(caught.exception)
        self.assertIn("ADVISORY GHSA-1 now affects package foo", message)
        self.assertNotIn("append-only conflict", message)
        self.assertEqual(self.execute(reuse=True)["measurement"]["cache_decision"], "exact_success_reused",
                         "the earlier certified success must stay immutable and reusable for ordinary rows")


class SecurityRouteTests(unittest.TestCase):
    """The owner page's POST route: strict shape, plain refusals, 201 on success."""

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))
        delivery_with_direction(self.root, "TASK-ROUTE")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, body: bytes):
        request = Request(f"http://127.0.0.1:{self.server.server_address[1]}/api/tasks/TASK-ROUTE/security",
                          data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_shape_value_lock_and_success(self):
        status, body = self.post(b"[true]")
        self.assertEqual(status, 400)
        self.assertIn("JSON object", body["error"])
        status, body = self.post(b'{"enhanced": "yes"}')
        self.assertEqual(status, 400)
        self.assertIn("true or false", body["error"])
        status, body = self.post(b'{"enhanced": true}')
        self.assertEqual(status, 201)
        self.assertEqual(body["security"]["on"], True)
        self.assertIn("notified", body)
        self.assertTrue(board.snapshot(self.root)["task_security"]["TASK-ROUTE"]["enhanced"])
        _edit_state(self.root, lambda state: state["task_security"]["TASK-ROUTE"].update({"locked": True}))
        status, body = self.post(b'{"enhanced": false}')
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], board.TASK_SECURITY_LOCKED_MESSAGE)


if __name__ == "__main__":
    unittest.main()

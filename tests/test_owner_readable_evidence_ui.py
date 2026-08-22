# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Owner-readable evidence projection across board bytes, payloads, and HTML."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, board_viewer, contract, control


LEDGER_HEADER = (
    "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
    "|---|---|---|---|---|---|\n"
)


class OwnerReadableEvidenceUI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(
            self.root, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Show readable test evidence.")
        board.begin_task(self.root, self.delivery["id"], "OWNER-EVIDENCE")
        contract.create_contract(self.root, "OWNER-EVIDENCE", "Show readable test evidence.", ["readable checks"])
        review_session = control.create(self.root, "claude_reviewer")
        self.reviewer = board.register(
            self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=review_session["id"],
        )

    def script(self):
        return board_viewer.rendered_page().split("<script>", 1)[1].split("</script>", 1)[0].split("el('#status-dialog-close')", 1)[0]

    def node(self, invocation):
        result = subprocess.run(["node", "-e", self.script() + "\n" + invocation], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def ledger(self, name, scenario_id, wording, result="PASS"):
        path = self.root / f"{name}.md"
        path.write_text(
            LEDGER_HEADER
            + f"| {scenario_id} | {wording} | `python3 -m unittest test_smoke` | The behavior is correct. | {result}: recorded text | {result} |\n",
            encoding="utf-8",
        )
        return path

    def bundle(self, scenario_id, outcome="PASS"):
        path = self.root / ".harness" / "board" / "evidence" / f"{scenario_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"scenario: {scenario_id}\nresult: {outcome}\n", encoding="utf-8")
        return {
            "scenario_ids": [scenario_id],
            "executed_count": 1 if outcome == "PASS" else 0,
            "approved_exception_ids": [],
            "evidence": str(path),
            "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def request(self, request_id="review-one", task="OWNER-EVIDENCE", reviewer=None):
        delivery_ledger = self.ledger(
            f"{request_id}-delivery", "S-DELIVERY",
            "Opening a saved project returns to the same work without losing progress.",
        )
        challenge = self.ledger(
            f"{request_id}-reviewer", "S-REVIEWER",
            "A failed independent check needs attention and is never shown as passed.",
        )
        challenge_digest = hashlib.sha256(challenge.read_bytes()).hexdigest()
        return {
            "id": request_id,
            "task": task,
            "stage": "independent_review",
            "phase": "final_acceptance",
            "cycle": 1,
            "status": "claimed",
            "developer_id": self.delivery["id"],
            "claimed_by": (reviewer or self.reviewer)["id"],
            "requested_at": "2026-08-18T12:00:00+00:00",
            "review_wait_started_at": "2026-08-18T12:00:00+00:00",
            "subtask": "",
            "chunk": "final",
            "ledger": str(delivery_ledger),
            "ledger_sha256": hashlib.sha256(delivery_ledger.read_bytes()).hexdigest(),
            "delivery_simulations": self.bundle("S-DELIVERY"),
            "challenge_ledger": str(challenge),
            "challenge_ledger_sha256": challenge_digest,
            "challenge_execution": {
                "ledger_sha256": challenge_digest,
                "bundle": self.bundle("S-REVIEWER"),
            },
            "reserved_by": None,
            "routed_to": None,
        }

    def save_requests(self, *requests):
        with board.locked_state(self.root) as state:
            for request in requests:
                state["qa_requests"][request["id"]] = request

    def certify(self, path):
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        destination = self.root / ".harness" / "board" / "certified" / digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(Path(path).read_bytes())
        return {"path": str(destination), "sha256": digest}

    def test_live_delivery_and_reviewer_status_use_separate_exact_checklists(self):
        request = self.request()
        self.save_requests(request)
        payload = board_viewer.dashboard_payload(self.root)
        delivery = payload["agent_checklists"][self.delivery["id"]]
        reviewer = payload["agent_checklists"][self.reviewer["id"]]
        self.assertEqual(delivery["section"]["scenarios"][0]["what_was_tested"],
                         "Opening a saved project returns to the same work without losing progress.")
        self.assertEqual(reviewer["section"]["scenarios"][0]["what_was_tested"],
                         "A failed independent check needs attention and is never shown as passed.")
        self.assertEqual(delivery["section"]["scenarios"][0]["status"], "passed")
        self.assertEqual(reviewer["section"]["scenarios"][0]["status"], "passed")

        rendered = self.node(
            "const nodes={'status-dialog-title':{textContent:''},'status-dialog-body':{innerHTML:''},'status-dialog':{showModal(){}}};"
            "globalThis.document={querySelector(s){return nodes[s.slice(1)]||null;}};"
            f"lastBoard={json.dumps(payload)};showAgentStatus({json.dumps(self.reviewer['id'])});"
            "process.stdout.write(JSON.stringify({html:nodes['status-dialog-body'].innerHTML}));"
        )["html"]
        self.assertIn("What the independent reviewer tested", rendered)
        self.assertNotIn("What Delivery tested", rendered)
        self.assertNotIn("S-REVIEWER", rendered)
        self.assertNotIn("python3", rendered)

    def test_status_dialog_opens_at_summary_without_sacrificing_keyboard_access(self):
        payload = board_viewer.dashboard_payload(self.root)
        rendered = self.node(
            "let focusOptions=null;"
            "const nodes={'status-dialog-title':{textContent:'',focus(options){focusOptions=options;}},"
            "'status-dialog-body':{innerHTML:''},"
            "'status-dialog':{scrollTop:500,showModal(){}}};"
            "globalThis.document={querySelector(s){return nodes[s.slice(1)]||null;}};"
            f"lastBoard={json.dumps(payload)};showAgentStatus({json.dumps(self.delivery['id'])});"
            "process.stdout.write(JSON.stringify({scrollTop:nodes['status-dialog'].scrollTop,focusOptions}));"
        )
        self.assertEqual(rendered["scrollTop"], 0)
        self.assertEqual(rendered["focusOptions"], {"preventScroll": True})
        self.assertIn('id="status-dialog-title" tabindex="-1"', board_viewer.PAGE)
        self.assertIn('id="status-dialog-close"', board_viewer.PAGE)

    def test_idle_reassigned_and_two_reviewer_assignments_never_show_stale_or_cross_task_rows(self):
        old = self.request()
        old["status"] = "passed"
        old["completed_at"] = "2026-08-18T12:05:00+00:00"
        self.save_requests(old)
        idle = board_viewer.dashboard_payload(self.root)["agent_checklists"][self.reviewer["id"]]
        self.assertEqual(idle["message"], "No review is currently assigned.")
        self.assertNotIn("section", idle)

        second_session = control.create(self.root, "claude_reviewer")
        second = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=second_session["id"])
        left = self.request("left-request", reviewer=self.reviewer)
        right = self.request("right-request", reviewer=second)
        right_path = Path(right["challenge_ledger"])
        right_path.write_text(
            LEDGER_HEADER
            + "| S-RIGHT | The phone-sized project list stays readable and scrolls without overlap. | `python3 -m unittest test_smoke` | The list remains usable. | PASS: observed | PASS |\n",
            encoding="utf-8",
        )
        right_digest = hashlib.sha256(right_path.read_bytes()).hexdigest()
        right.update({
            "challenge_ledger_sha256": right_digest,
            "challenge_execution": {"ledger_sha256": right_digest, "bundle": self.bundle("S-RIGHT")},
        })
        self.save_requests(left, right)
        checklists = board_viewer.dashboard_payload(self.root)["agent_checklists"]
        first_text = checklists[self.reviewer["id"]]["section"]["scenarios"][0]["what_was_tested"]
        second_text = checklists[second["id"]]["section"]["scenarios"][0]["what_was_tested"]
        self.assertIn("failed independent check", first_text)
        self.assertIn("phone-sized project list", second_text)
        self.assertNotEqual(first_text, second_text)

    def test_restart_pause_resume_and_repair_choose_only_the_current_authoritative_request(self):
        failed = self.request("failed-attempt")
        failed.update({"status": "failed", "completed_at": "2026-08-18T12:04:00+00:00"})
        repairing = self.request("repair-attempt")
        repairing.update({
            "requested_at": "2026-08-18T12:10:00+00:00",
            "status": "open",
            "claimed_by": None,
            "routed_to": self.reviewer["id"],
        })
        repairing.pop("challenge_ledger")
        repairing.pop("challenge_ledger_sha256")
        repairing.pop("challenge_execution")
        self.save_requests(failed, repairing)
        section = board_viewer.dashboard_payload(self.root)["agent_checklists"][self.reviewer["id"]]["section"]
        self.assertEqual(section["message"], "The reviewer is preparing independent checks.")
        self.assertEqual(section["scenarios"], [])

        with board.locked_state(self.root) as state:
            current = state["qa_requests"][repairing["id"]]
            current.update({"status": "suspended", "claimed_by": self.reviewer["id"], "routed_to": None})
            current["challenge_ledger"] = failed["challenge_ledger"]
            current["challenge_ledger_sha256"] = failed["challenge_ledger_sha256"]
        paused = board_viewer.dashboard_payload(self.root)["agent_checklists"][self.reviewer["id"]]["section"]
        self.assertEqual(paused["scenarios"][0]["status"], "pending")

        with board.locked_state(self.root) as state:
            current = state["qa_requests"][repairing["id"]]
            current.update({"status": "open", "claimed_by": None, "routed_to": None})
        idle = board_viewer.dashboard_payload(self.root)["agent_checklists"][self.reviewer["id"]]
        self.assertEqual(idle["state"], "idle")

    def test_written_pass_and_row_claim_without_exact_execution_remain_not_tested(self):
        request = self.request()
        request.pop("challenge_execution")
        request["result_summary"] = "PASS"
        self.save_requests(request)
        item = board_viewer.dashboard_payload(self.root)["agent_checklists"][self.reviewer["id"]]["section"]["scenarios"][0]
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["label"], "Not tested yet")

    def test_missing_malformed_mismatched_and_tampered_reviewer_evidence_fail_closed(self):
        request = self.request()
        evidence = Path(request["challenge_execution"]["bundle"]["evidence"])
        evidence.write_text("scenario: S-REVIEWER\nresult: PASS\ntampered\n", encoding="utf-8")
        self.save_requests(request)
        item = board_viewer.dashboard_payload(self.root)["agent_checklists"][self.reviewer["id"]]["section"]["scenarios"][0]
        self.assertEqual(item["status"], "pending")

        with board.locked_state(self.root) as state:
            stored = state["qa_requests"][request["id"]]
            stored["challenge_execution"]["ledger_sha256"] = "0" * 64
        item = board_viewer.dashboard_payload(self.root)["agent_checklists"][self.reviewer["id"]]["section"]["scenarios"][0]
        self.assertEqual(item["status"], "pending")

        Path(request["challenge_ledger"]).write_text("not a ledger\n", encoding="utf-8")
        section = board_viewer.dashboard_payload(self.root)["agent_checklists"][self.reviewer["id"]]["section"]
        self.assertEqual(section["state"], "unavailable")
        self.assertEqual(section["scenarios"], [])

    def test_settled_reviewer_pass_requires_the_certified_ledger_and_certified_execution(self):
        request = self.request()
        request["status"] = "passed"
        delivery_artifact = self.certify(request["ledger"])
        challenge_artifact = self.certify(request["challenge_ledger"])
        request["certified_artifacts"] = {
            "delivery_ledger": delivery_artifact,
            "challenge_ledger": challenge_artifact,
        }
        for field in ("delivery_simulations",):
            manifest = self.certify(request[field]["evidence"])
            request[field]["certified_evidence"] = manifest["path"]
            request[field]["certified_evidence_sha256"] = manifest["sha256"]
        request["reviewer_simulations"] = dict(request["challenge_execution"]["bundle"])
        reviewer_evidence = self.certify(request["reviewer_simulations"]["evidence"])
        request["reviewer_simulations"]["certified_evidence"] = reviewer_evidence["path"]
        request["reviewer_simulations"]["certified_evidence_sha256"] = reviewer_evidence["sha256"]
        self.save_requests(request)
        view = board_viewer._request_ledger_view(self.root, board.snapshot(self.root), "OWNER-EVIDENCE", request)
        self.assertEqual(view["reviewer"]["scenarios"][0]["status"], "passed")

        Path(challenge_artifact["path"]).write_text("tampered", encoding="utf-8")
        closed = board_viewer._request_ledger_view(self.root, board.snapshot(self.root), "OWNER-EVIDENCE", request)
        self.assertEqual(closed["reviewer"]["scenarios"][0]["status"], "pending")

    def test_history_preserves_failed_then_passed_delivery_and_reviewer_attempts_in_order(self):
        failed = self.request("attempt-one")
        failed.update({"status": "failed", "completed_at": "2026-08-18T12:05:00+00:00"})
        passed = self.request("attempt-two")
        passed.update({"status": "passed", "requested_at": "2026-08-18T12:10:00+00:00", "completed_at": "2026-08-18T12:15:00+00:00"})
        passed["certified_artifacts"] = {
            "delivery_ledger": self.certify(passed["ledger"]),
            "challenge_ledger": self.certify(passed["challenge_ledger"]),
        }
        for bundle in (passed["delivery_simulations"], passed["challenge_execution"]["bundle"]):
            evidence = self.certify(bundle["evidence"])
            bundle["certified_evidence"] = evidence["path"]
            bundle["certified_evidence_sha256"] = evidence["sha256"]
        passed["reviewer_simulations"] = dict(passed["challenge_execution"]["bundle"])
        self.save_requests(failed, passed)
        views = board_viewer._task_test_ledgers(self.root, board.snapshot(self.root), "OWNER-EVIDENCE")
        self.assertEqual(len(views), 2)
        self.assertEqual(views[0]["attempt_label"], "First recorded attempt")
        self.assertIn("found a problem", views[0]["attempt_status"])
        self.assertEqual(views[1]["attempt_label"], "Latest recorded attempt")
        for view in views:
            self.assertTrue(view["delivery"]["scenarios"])
            self.assertTrue(view["reviewer"]["scenarios"])

    def test_approved_exception_needs_intact_evidence_and_has_its_own_owner_state(self):
        ledger = self.ledger(
            "exception", "S-EXCEPTION",
            "An unrelated platform check is explicitly outside this focused change.",
        )
        row = board_viewer._scenario_rows_for_view(ledger)
        evidence = self.root / "exception-evidence.txt"
        evidence.write_text("No executable scenarios were required for this approved exception.\n", encoding="utf-8")
        bundle = {
            "scenario_ids": ["S-EXCEPTION"],
            "executed_count": 0,
            "approved_exception_ids": ["S-EXCEPTION"],
            "evidence": str(evidence),
            "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
        }
        section = board_viewer._checklist_section(row, bundle)
        self.assertEqual(section["scenarios"][0]["status"], "exception")
        self.assertEqual(section["scenarios"][0]["label"], "Not required for this change")
        evidence.write_text("changed", encoding="utf-8")
        closed = board_viewer._checklist_section(row, bundle)
        self.assertEqual(closed["scenarios"][0]["status"], "pending")

    def test_legacy_description_order_and_neutral_fallback_never_leak_technical_fields(self):
        usable = self.root / "legacy-usable.md"
        usable.write_text(
            "| ID | Description | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-OLD | The project list remains readable on a small screen. | `pytest -k list` | works | PASS | PASS |\n",
            encoding="utf-8",
        )
        technical = self.root / "legacy-technical.md"
        technical.write_text(
            "| ID | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-14.3 | pytest tests/test_board.py -k ledger | `pytest tests/test_board.py -k ledger` | PASS_EVENT | /tmp/raw | PASS |\n",
            encoding="utf-8",
        )
        self.assertEqual(board_viewer._scenario_rows_for_view(usable)[0]["what_was_tested"],
                         "The project list remains readable on a small screen.")
        fallback = board_viewer._scenario_rows_for_view(technical)[0]["what_was_tested"]
        self.assertEqual(fallback, board_viewer.OWNER_DESCRIPTION_FALLBACK)
        self.assertNotIn("pytest", fallback)
        self.assertNotIn("S-14.3", fallback)

    def test_all_four_outcomes_have_distinct_text_semantics_and_safe_wrapping(self):
        html = self.node(
            "process.stdout.write(JSON.stringify({html:checklistSectionHtml({scenarios:["
            "{what_was_tested:'A saved project opens with all progress intact.',status:'passed',label:'Passed'},"
            "{what_was_tested:'A broken record stays visible for repair.',status:'failed',label:'Needs attention'},"
            "{what_was_tested:'A queued check has not run yet.',status:'pending',label:'Not tested yet'},"
            "{what_was_tested:'An unrelated platform check is outside this change.',status:'exception',label:'Not required for this change'},"
            "{what_was_tested:'<script>alert(1)</script> '+ 'x'.repeat(300),status:'pending',label:'Not tested yet'}]},'delivery')}));"
        )["html"]
        for label, symbol in (("Passed", "☑"), ("Needs attention", "☒"), ("Not tested yet", "☐"), ("Not required for this change", "◇")):
            self.assertIn(f'aria-label="{label}">{symbol}', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("overflow-wrap:anywhere", board_viewer.PAGE)
        self.assertIn("@media(max-width:480px)", board_viewer.PAGE)

    def test_history_search_indexes_both_authors_descriptions_without_ids(self):
        rendered = self.node(
            "const nodes={'history-list':{innerHTML:''},'history-count':{textContent:''},'history-search':{value:''},'history-search-status':{textContent:''}};"
            "globalThis.document={querySelector(s){return nodes[s.slice(1)]||null;}};"
            "renderHistory([{task:'DONE',completed_at:'2026-08-18T12:00:00Z',owner_direction:'ordinary',test_ledgers:[{attempt_label:'First recorded attempt',attempt_status:'This attempt found a problem and was followed by a repair.',delivery:{scenarios:[{what_was_tested:'Delivery keeps a saved project intact.',status:'passed',label:'Passed'}]},reviewer:{scenarios:[{what_was_tested:'Independent phone navigation stays readable.',status:'passed',label:'Passed'}]}}]}],'phone navigation');"
            "process.stdout.write(JSON.stringify({html:nodes['history-list'].innerHTML,status:nodes['history-search-status'].textContent}));"
        )
        self.assertIn("1 of 1 tasks match", rendered["status"])
        self.assertIn("What Delivery tested", rendered["html"])
        self.assertIn("What the independent reviewer tested", rendered["html"])
        self.assertIn("Independent phone navigation stays readable.", rendered["html"])

    def test_project_roots_are_isolated_and_dashboard_projection_does_not_execute_or_launch(self):
        request = self.request()
        self.save_requests(request)
        with patch.object(board_viewer.git_process, "run", side_effect=AssertionError("refresh executed a command")), \
             patch.object(board_viewer.subprocess, "Popen", side_effect=AssertionError("refresh launched a process")):
            first = board_viewer.dashboard_payload(self.root)
        self.assertIn(self.delivery["id"], first["agent_checklists"])
        self.assertLessEqual(len(first["state"]["qa_requests"]), board_viewer.DASHBOARD_REVIEW_LIMIT_PER_TASK)
        self.assertEqual(first["state"].get("archive"), [])

        with board.locked_state(self.root) as state:
            for index in range(60):
                state["qa_requests"][f"settled-{index:02d}"] = {
                    "id": f"settled-{index:02d}", "task": "OWNER-EVIDENCE",
                    "phase": "final_acceptance", "subtask": "", "chunk": "final",
                    "cycle": index + 2, "status": "failed", "developer_id": self.delivery["id"],
                    "claimed_by": None, "requested_at": f"2026-08-17T12:{index % 60:02d}:00+00:00",
                    "review_wait_started_at": "2026-08-17T12:00:00+00:00",
                }
        bounded = board_viewer.dashboard_payload(self.root)
        self.assertEqual(len(bounded["state"]["qa_requests"]), board_viewer.DASHBOARD_REVIEW_LIMIT_PER_TASK)

        with tempfile.TemporaryDirectory() as other_value:
            other = Path(other_value)
            other_session = control.create(other, "codex_delivery")
            other_delivery = board.register(other, "development", board.AWAITING_OWNER_DIRECTION,
                                            vendor="OpenAI", session_id=other_session["id"])
            second = board_viewer.dashboard_payload(other)
            serialized = json.dumps(second)
            self.assertNotIn("Opening a saved project", serialized)
            self.assertNotIn(self.delivery["id"], second["agent_checklists"])
            self.assertIn(other_delivery["id"], second["agent_checklists"])
            foreign = board_viewer._request_ledger_view(other, {"task_workspaces": {}}, "FOREIGN", request)
            self.assertEqual(foreign["delivery"]["scenarios"], [])
            self.assertEqual(foreign["reviewer"]["scenarios"], [])


if __name__ == "__main__":
    unittest.main()

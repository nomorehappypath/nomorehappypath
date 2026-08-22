# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import hashlib
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import Request, urlopen

from harness import board, board_viewer, control, global_settings
from tests.environment_support import require_loopback


class DeferredFindingTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def test_final_pass_resolves_only_findings_in_its_intact_certified_brief(self):
        state = board._initial_state()
        reviewed = {
            "id": "finding-reviewed", "task": "TASK-FINAL", "status": "in_scope",
            "title": "Reviewed defect", "description": "The final review covers this defect.",
        }
        later = {
            "id": "finding-later", "task": "TASK-FINAL", "status": "in_scope",
            "title": "Later defect", "description": "This was not in the review brief.",
        }
        state["deferred_findings"] = {
            reviewed["id"]: reviewed, later["id"]: later,
        }
        brief = {
            "version": 1, "task": "TASK-FINAL", "request_id": "review-final-01",
            "risk_and_scope": {"unresolved_findings": [{
                "title": reviewed["title"], "description": reviewed["description"],
            }]},
        }
        brief["sha256"] = hashlib.sha256(json.dumps(
            brief, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        request = {
            "id": "review-final-01", "task": "TASK-FINAL", "status": "passed",
            "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance",
            "review_brief": brief,
            "challenge_execution": {"bundle": {"evidence_sha256": "a" * 64}},
        }

        resolved = board._resolve_findings_certified_by_final_review(
            state, request, board.now(),
        )

        self.assertEqual(resolved, [reviewed["id"]])
        self.assertEqual(state["deferred_findings"][reviewed["id"]]["status"], "resolved")
        self.assertEqual(state["deferred_findings"][later["id"]]["status"], "in_scope")
        self.assertEqual(
            state["deferred_findings"][reviewed["id"]]["resolution_source"],
            {
                "request_id": request["id"],
                "review_brief_sha256": brief["sha256"],
                "challenge_evidence_sha256": "a" * 64,
            },
        )

        state["deferred_findings"][reviewed["id"]]["status"] = "in_scope"
        request["review_brief"]["risk_and_scope"]["unresolved_findings"][0][
            "description"
        ] = "tampered after review"
        self.assertEqual(
            board._resolve_findings_certified_by_final_review(
                state, request, board.now(),
            ),
            [],
        )
        self.assertEqual(state["deferred_findings"][reviewed["id"]]["status"], "in_scope")

    def test_in_scope_finding_is_not_deferred_and_blocks_release_until_resolved(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            finding = board.record_finding(root, "TASK-1", "Broken acceptance path", "The required path fails", True, "python3 -m unittest test_required")
            self.assertEqual(finding["status"], "in_scope")
            self.assertEqual(board.list_findings(root, include_resolved=False), [])
            cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
            checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
            with self.assertRaisesRegex(ValueError, "in_scope_findings_resolved"):
                board.record_release_ready(root, cto["id"], "TASK-1", checks)
            board.resolve_finding(root, finding["id"], "python3 -m unittest test_required — PASS")
            self.assertEqual(board.snapshot(root)["deferred_findings"][finding["id"]]["status"], "resolved")

    def test_unrelated_finding_is_deferred_and_fix_decisions_are_queued(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = board.record_finding(root, "TASK-1", "Unrelated viewer polish", "A separate visual improvement", False)
            second = board.record_finding(root, "TASK-1", "Unrelated docs note", "A separate documentation improvement", False)
            board.triage_finding(root, first["id"], "distinct")
            board.triage_finding(root, second["id"], "distinct")
            self.assertEqual(len(board.list_findings(root, include_resolved=False)), 2)
            chosen = board.record_finding_decision(root, first["id"], "fix")
            self.assertEqual(chosen["status"], "fix_requested")
            self.assertEqual(chosen["queue_position"], 1)
            resolved = board.resolve_finding(root, first["id"], "Focused regression test passed")
            self.assertEqual(resolved["status"], "resolved")
            dismissed = board.record_finding_decision(root, second["id"], "do_not_fix")
            self.assertEqual(dismissed["status"], "dismissed")
            self.assertEqual(board.list_findings(root, include_resolved=False), [])

    def test_same_finding_is_deduplicated_and_stays_hidden_after_resolution(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = board.record_finding(root, "TASK-1", "Repeated launch defect", "The same verified launch path fails", False, "first reproduction")
            repeated = board.record_finding(root, "TASK-2", "Repeated launch defect", "The same verified launch path fails", False, "second observation")
            self.assertEqual(repeated["id"], first["id"])
            self.assertEqual(repeated["observed_in_tasks"], ["TASK-1", "TASK-2"])
            self.assertEqual(len(board.snapshot(root)["deferred_findings"]), 1)
            board.triage_finding(root, first["id"], "distinct")
            board.record_finding_decision(root, first["id"], "fix")
            board.resolve_finding(root, first["id"], "executable regression passed")
            after_fix = board.record_finding(root, "TASK-3", "Repeated launch defect", "The same verified launch path fails", False, "stale duplicate report")
            self.assertEqual(after_fix["status"], "resolved")
            dashboard = board_viewer.dashboard_payload(root)
            self.assertNotIn("deferred_findings", dashboard)
            self.assertNotIn("deferred_findings_total", dashboard)

    def test_dashboard_never_exposes_internal_deferred_findings_to_owner(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(7):
                finding = board.record_finding(root, "TASK-1", f"Finding {index}", "Not part of the current task", False)
                board.triage_finding(root, finding["id"], "distinct")
            data = board_viewer.dashboard_payload(root)
            self.assertNotIn("deferred_findings", data)
            self.assertNotIn("deferred_findings_total", data)
            self.assertNotIn("Other findings", board_viewer.rendered_page())

    def test_deferred_findings_stay_hidden_while_delivery_is_active(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hidden = board.record_finding(root, "TASK-ACTIVE", "Separate issue", "Keep this out of active delivery", False)
            board.triage_finding(root, hidden["id"], "distinct")
            with board.locked_state(root) as state:
                state["qa_requests"]["review-active"] = {
                    "id": "review-active", "task": "TASK-ACTIVE", "status": "open",
                    "requested_at": board.now(), "phase": "chunk", "chunk": "", "cycle": 1,
                    "developer_id": "delivery", "claimed_by": "", "review_wait_started_at": "",
                }
            data = board_viewer.dashboard_payload(root)
            self.assertEqual(data["live_tasks"], ["TASK-ACTIVE"])
            self.assertNotIn("deferred_findings", data)
            self.assertNotIn("deferred_findings_total", data)

    def test_failed_review_stays_live_without_an_active_delivery_process(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hidden = board.record_finding(
                root, "TASK-FAILED", "Repeated internal issue",
                "Keep internal triage out of the owner interface", False,
            )
            board.triage_finding(root, hidden["id"], "distinct")
            with board.locked_state(root) as state:
                state["qa_requests"]["review-failed"] = {
                    "id": "review-failed", "task": "TASK-FAILED",
                    "status": "failed", "cycle": 2,
                    "phase": "final_acceptance", "subtask": "", "chunk": "final",
                    "developer_id": "delivery-ended", "claimed_by": None,
                    "requested_at": board.now(), "review_wait_started_at": board.now(),
                }
            data = board_viewer.dashboard_payload(root)
            self.assertEqual(data["live_tasks"], ["TASK-FAILED"])
            self.assertNotIn("deferred_findings", data)
            self.assertNotIn("Other findings", board_viewer.rendered_page())

    def test_approved_findings_dispatch_one_at_a_time_and_close_on_owner_acceptance(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            delivery = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            first = board.record_finding(root, "OLD", "First approved repair", "Repair the first separate issue", False)
            second = board.record_finding(root, "OLD", "Second approved repair", "Wait until the first finishes", False)
            board.triage_finding(root, first["id"], "distinct")
            board.triage_finding(root, second["id"], "distinct")
            board.record_finding_decision(root, first["id"], "fix")
            board.record_finding_decision(root, second["id"], "fix")

            routed = board.dispatch_approved_finding(root, delivery["id"])
            self.assertEqual(routed["finding"]["id"], first["id"])
            state = board.snapshot(root)
            self.assertEqual(state["deferred_findings"][first["id"]]["status"], "fix_in_progress")
            self.assertEqual(state["deferred_findings"][second["id"]]["status"], "fix_requested")
            with self.assertRaisesRegex(ValueError, "already in progress"):
                board.dispatch_approved_finding(root, delivery["id"])

            follow_up = "FIRST-APPROVED-REPAIR"
            board.begin_task(root, delivery["id"], follow_up)
            with board.locked_state(root) as state:
                state["releases"][follow_up] = {
                    "task": follow_up,
                    "status": "VISUAL_TEST_REQUIRED",
                    "head_commit": "abc123",
                    "cto_id": "cto-test",
                    "recorded_at": board.now(),
                }
            board.record_release_decision(root, follow_up, "accepted")
            state = board.snapshot(root)
            self.assertEqual(state["deferred_findings"][first["id"]]["status"], "resolved")
            self.assertEqual(state["deferred_findings"][second["id"]]["status"], "fix_requested")

    def test_controller_opens_only_one_delivery_slot_then_routes_the_approved_finding(self):
        with TemporaryDirectory() as tmp, patch("harness.board_viewer.launch_terminal") as launch:
            root = Path(tmp)
            finding = board.record_finding(root, "OLD", "Approved repair", "Start this automatically", False)
            board.triage_finding(root, finding["id"], "distinct")
            board.record_finding_decision(root, finding["id"], "fix")

            first = board_viewer.dispatch_approved_findings(root)
            second = board_viewer.dispatch_approved_findings(root)
            self.assertEqual(first["status"], "terminal_started")
            self.assertEqual(second["status"], "terminal_registering")
            launch.assert_called_once()

            session = next(item for item in control.snapshot(root)["sessions"] if item["id"] == first["session_id"])
            delivery = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            routed = board_viewer.dispatch_approved_findings(root)
            self.assertEqual(routed["status"], "dispatched")
            state = board.snapshot(root)
            self.assertEqual(state["deferred_findings"][finding["id"]]["assigned_agent_id"], delivery["id"])
            self.assertIn("OWNER-APPROVED FOLLOW-UP", state["owner_directions"][session["id"]]["text"])

    def test_controller_launch_uses_manager_global_settings_without_local_copy(self):
        with TemporaryDirectory() as tmp, patch("harness.board_viewer.launch_terminal"):
            base = Path(tmp)
            root, home = base / "project", base / "manager"
            root.mkdir()
            selected = {
                "delivery": {"provider": "codex", "model": "gpt-5.6-luna", "effort": "low"},
                "reviewer": {"provider": "claude", "model": "haiku", "effort": "low"},
                "cto": {"provider": "claude", "model": "sonnet", "effort": "medium"},
            }
            global_settings.update_agent_settings(home, selected)
            finding = board.record_finding(root, "OLD", "Approved repair", "Start globally configured Delivery", False)
            board.triage_finding(root, finding["id"], "distinct")
            board.record_finding_decision(root, finding["id"], "fix")

            launched = board_viewer.dispatch_approved_findings(root, home)

            self.assertEqual(launched["status"], "terminal_started")
            session = next(
                item for item in control.snapshot(root)["sessions"]
                if item["id"] == launched["session_id"]
            )
            self.assertEqual(
                {key: session[key] for key in ("provider", "model", "effort")},
                selected["delivery"],
            )
            local = json.loads((root / ".harness" / "control" / "sessions.json").read_text())
            self.assertNotIn("agent_settings", local)

    def test_clicking_fix_api_immediately_starts_the_follow_up_dispatch(self):
        with TemporaryDirectory() as tmp, patch("harness.board_viewer.launch_terminal") as launch:
            root = Path(tmp)
            finding = board.record_finding(root, "OLD", "API approved repair", "The Fix click must start work", False)
            board.triage_finding(root, finding["id"], "distinct")
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/api/findings/{finding['id']}/decision",
                    data=json.dumps({"decision": "fix"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                response = json.loads(urlopen(request, timeout=3).read())
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()
            self.assertEqual(response["finding"]["status"], "fix_requested")
            self.assertEqual(response["dispatch"]["status"], "terminal_started")
            launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()

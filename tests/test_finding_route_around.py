# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The CTO can set an in-scope finding aside as not material: ``finding-route-around``.

Owner 2026-10-14: the harness CTO must stop treating non-material issues as
material and keep Delivery on the owner's objective. The directive (f27cd04,
"What must never block the work") already said so, but the board had no way to
record it: an ``in_scope`` finding kept driving the task's next action, the
pause/resume Delivery slot, the CTO wake check and the release gate until
Delivery repaired it. Live case: Studio task jls-brand-strategy-quality-diagnostic,
nine in-scope findings about a fresh-run blocker the CTO had judged non-blocking.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from harness import board, board_surface, board_viewer, review_brief
from tests.environment_support import require_loopback


REASON = "Not material: the owner's objective is a usable diagnosis of the existing deck, which does not need the fresh run"


def _in_scope(root: Path, task: str = "TASK-1", title: str = "Fresh run blocked by a browser permission") -> dict:
    return board.record_finding(root, task, title, "The managed browser denies the local page", True, "events 00:52Z")


def _stopped_delivery() -> dict:
    return {"id": "dev-1", "role": "engineering", "task": "TASK-1", "active": False, "status": "stopped", "status_note": "",
            "session_id": "", "spawned_at": board.now(), "poll_counter": 0, "last_status_at": board.now()}


class FindingRouteAroundTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def test_route_around_records_the_reason_keeps_the_finding_and_stops_steering_delivery(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            finding = _in_scope(root)
            before = board._task_context_bundle(board.snapshot(root), "TASK-1")
            self.assertIn("in-scope finding", before["next_action"])

            routed = board.route_around_finding(root, finding["id"], REASON)

            self.assertEqual(routed["status"], "routed_around")
            self.assertEqual(routed["routed_around_reason"], REASON)
            self.assertTrue(routed["routed_around_at"])
            self.assertIn(REASON, routed["next_action"])
            state = board.snapshot(root)
            self.assertIn(finding["id"], state["deferred_findings"], "the finding must never be deleted")
            events = [event for event in state["events"] if event["kind"] == "finding_routed_around"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["finding_id"], finding["id"])
            self.assertIn(REASON, events[0]["message"])
            bundle = board._task_context_bundle(state, "TASK-1")
            self.assertNotIn("in-scope finding", bundle["next_action"])
            # the disposition and its reason stay in every agent's durable context (Codex plan check, finding 1)
            self.assertEqual([item["id"] for item in bundle["routed_around_findings"]], [finding["id"]])
            self.assertEqual(bundle["routed_around_findings"][0]["reason"], REASON)
            self.assertIn("do not repair", bundle["routed_around_findings"][0]["rule"])
            cto = {"id": "cto-1", "role": "cto", "active": True, "task": "GLOBAL_MONITOR"}
            self.assertEqual(board._agent_context_bundle(state, cto)["untriaged_findings"], [])
            self.assertFalse(board._agent_has_actionable_work(state, cto), "a routed-around finding must not keep the CTO awake")

    def test_routed_around_finding_no_longer_gates_release_review_brief_or_resume(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            finding = _in_scope(root)
            cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
            checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
            with self.assertRaisesRegex(ValueError, "in_scope_findings_resolved"):
                board.record_release_ready(root, cto["id"], "TASK-1", checks)
            board.route_around_finding(root, finding["id"], REASON)
            board.record_release_ready(root, cto["id"], "TASK-1", checks)

            with board.locked_state(root) as state:
                state["requirement_confirmations"]["TASK-1"] = {"text": "Final agreed requirements: diagnose the deck.", "confirmed_at": board.now(), "version": 1}
            brief = review_brief.build(root, board.snapshot(root), {"id": "review-1", "task": "TASK-1", "test_scope": "full"})
            self.assertEqual(brief["risk_and_scope"]["unresolved_findings"], [])
            self.assertEqual(brief["risk_and_scope"]["routed_around_findings"][0]["reason"], REASON)

            # pause/resume: the routed-around finding alone must not re-open a Delivery slot
            with board.locked_state(root, allow_paused=True) as state:
                state["task_owner_directions"]["TASK-1"] = "OWNER DIRECTION — diagnose the deck."
                state["release_lifecycle"]["TASK-1"] = {"development_completed_at": board.now(), "phases": {}}
                state["agents"]["dev-1"] = _stopped_delivery()
                state["project_pause"] = {"status": "resuming", "resume_id": "resume-1", "agents": {}}
            staged = board.stage_required_delivery_resumes(root, "resume-1")
            self.assertEqual([item for item in (staged.get("staged") or staged.get("agents") or [])], [])
            self.assertNotIn("dev-1", board.snapshot(root)["project_pause"].get("agents", {}))

    def test_in_scope_finding_still_reopens_the_delivery_slot_on_resume(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _in_scope(root)
            with board.locked_state(root, allow_paused=True) as state:
                state["task_owner_directions"]["TASK-1"] = "OWNER DIRECTION — diagnose the deck."
                state["release_lifecycle"]["TASK-1"] = {"development_completed_at": board.now(), "phases": {}}
                state["agents"]["dev-1"] = _stopped_delivery()
                state["project_pause"] = {"status": "resuming", "resume_id": "resume-1", "agents": {}}
            board.stage_required_delivery_resumes(root, "resume-1")
            self.assertIn("dev-1", board.snapshot(root)["project_pause"]["agents"])

    def test_routed_around_is_visible_on_the_board_and_in_the_viewer_payload(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            finding = _in_scope(root)
            board.route_around_finding(root, finding["id"], REASON)
            text = board._render_board(board.snapshot(root))
            row = next(line for line in text.splitlines() if finding["id"] in line)
            self.assertIn("routed_around", row)
            self.assertIn(REASON, row)
            payload = board_viewer.dashboard_payload(root)
            listed = [item for item in payload["in_scope_findings"] if item["id"] == finding["id"]]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["status"], "routed_around")
            self.assertEqual(listed[0]["routed_around_reason"], REASON)
            self.assertNotIn("deferred_findings", payload)
            page = board_viewer.rendered_page()
            self.assertIn("routed_around", page, "the task card must know the status")

    def test_delivery_finding_resolved_still_works_on_in_scope_and_on_routed_around(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            plain = _in_scope(root, title="Plain in-scope defect")
            resolved = board.resolve_finding(root, plain["id"], "re-tested: PASS")
            self.assertEqual(resolved["status"], "resolved")
            routed = _in_scope(root, title="Routed-around defect")
            board.route_around_finding(root, routed["id"], REASON)
            fixed = board.resolve_finding(root, routed["id"], "fixed anyway: PASS")
            self.assertEqual(fixed["status"], "resolved")
            self.assertEqual(fixed["routed_around_reason"], REASON, "the CTO's reason stays on the record")

    def test_only_an_in_scope_finding_can_be_routed_around_and_a_reason_is_required(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            finding = _in_scope(root)
            with self.assertRaisesRegex(ValueError, "reason"):
                board.route_around_finding(root, finding["id"], "   ")
            with self.assertRaisesRegex(ValueError, "unknown finding"):
                board.route_around_finding(root, "finding-missing", REASON)
            board.route_around_finding(root, finding["id"], REASON)
            with self.assertRaisesRegex(ValueError, "routed_around"):
                board.route_around_finding(root, finding["id"], REASON)

            unrelated = board.record_finding(root, "TASK-1", "Unrelated polish", "A separate visual improvement", False)
            with self.assertRaisesRegex(ValueError, "needs_triage"):
                board.route_around_finding(root, unrelated["id"], REASON)
            board.triage_finding(root, unrelated["id"], "distinct")
            with self.assertRaisesRegex(ValueError, "deferred"):
                board.route_around_finding(root, unrelated["id"], REASON)
            # the owner's Fix / Do not fix decision for a deferred finding is unchanged
            decided = board.record_finding_decision(root, unrelated["id"], "fix")
            self.assertEqual(decided["status"], "fix_requested")
            with self.assertRaisesRegex(ValueError, "fix_requested"):
                board.route_around_finding(root, unrelated["id"], REASON)
            other = board.record_finding(root, "TASK-1", "Unrelated docs note", "A separate documentation improvement", False)
            board.triage_finding(root, other["id"], "distinct")
            self.assertEqual(board.record_finding_decision(root, other["id"], "do_not_fix")["status"], "dismissed")

    def test_the_exemption_is_scoped_to_the_task_it_was_judged_for(self):
        # Codex plan check 2026-10-14, finding 2: the same defect reported on
        # another task is a fresh blocker, never silently the routed one.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _in_scope(root, task="TASK-A")
            board.route_around_finding(root, first["id"], REASON)
            same_task = _in_scope(root, task="TASK-A")
            self.assertEqual(same_task["id"], first["id"], "re-raising it on the same task returns the routed record")
            self.assertEqual(same_task["status"], "routed_around")
            other_task = _in_scope(root, task="TASK-B")
            self.assertNotEqual(other_task["id"], first["id"])
            self.assertEqual(other_task["status"], "in_scope")
            self.assertIn("in-scope finding", board._task_context_bundle(board.snapshot(root), "TASK-B")["next_action"])
            repeat = board.record_finding(root, "TASK-C", "Unrelated note", "Looks like the same browser denial", False)
            with self.assertRaisesRegex(ValueError, "routed-around"):
                board.triage_finding(root, repeat["id"], "repeat", first["id"])

    def test_route_around_closes_only_the_hold_from_hammered_finding_commands(self):
        # Codex plan check 2026-10-14, finding 4; seen live on the Studio board
        # (source repeated_refusal:finding-triage).
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            finding = _in_scope(root)
            other = _in_scope(root, task="TASK-2", title="A different blocker on another task")
            with board.locked_state(root) as state:
                state["control_plane_holds"]["TASK-1"] = {"status": "open", "source": "repeated_refusal:finding-triage", "reason": "x"}
                state["control_plane_holds"]["TASK-2"] = {"status": "open", "source": "repeated_refusal:git-commit", "reason": "y"}
            board.route_around_finding(root, finding["id"], REASON)
            board.route_around_finding(root, other["id"], REASON)
            holds = board.snapshot(root)["control_plane_holds"]
            self.assertEqual(holds["TASK-1"]["status"], "resolved")
            self.assertEqual(holds["TASK-1"]["resolved_by"], "finding_routed_around")
            self.assertEqual(holds["TASK-2"]["status"], "open", "an unrelated hold is left alone")

    def test_cli_and_surface_expose_the_command_to_the_cto_only(self):
        self.assertIn("finding-route-around", board_surface.ALL_BOARD_OPERATIONS)
        self.assertEqual(board_surface.AUTHORIZATION_MATRIX["finding-route-around"], frozenset({"cto"}))
        source = (Path(__file__).resolve().parents[1] / "harness" / "board.py").read_text(encoding="utf-8")
        self.assertRegex(source, re.compile(r'add_parser\("finding-route-around"\).*--reason', re.S))
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            finding = _in_scope(root)
            code = board.main(["--root", str(root), "finding-route-around", "--finding", finding["id"], "--reason", REASON])
            self.assertEqual(code, 0)
            self.assertEqual(board.snapshot(root)["deferred_findings"][finding["id"]]["status"], "routed_around")


if __name__ == "__main__":
    unittest.main()

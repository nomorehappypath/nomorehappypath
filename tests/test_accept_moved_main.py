# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner Accept when main moved after final certification (2026-10-24 report): the saved Accept must not count as
"accepted" before the merge, Delivery must not be stopped or abandoned, and the owner sees one plain line."""
from __future__ import annotations

import unittest

from pathlib import Path

from harness import board
from tests import test_git_model_board as git_model_tests


class AcceptWhenMainMovedTests(unittest.TestCase):
    def setUp(self):
        self.fixture = git_model_tests.GitModelBoardIntegrationTests("test_remote_push_requires_separate_instruction_and_immediate_confirmation")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.delivery = self.fixture.delivery
        self.committed = self.fixture.certified_candidate()

    def move_main(self):
        (self.root / "external.txt").write_text("external\n", encoding="utf-8")
        self.fixture.git("add", "external.txt")
        self.fixture.git("commit", "-m", "external main movement")

    def kinds(self):
        return [event["kind"] for event in board.snapshot(self.root)["events"]]

    def add_follow_up_finding(self):
        with board.locked_state(self.root) as state:
            state.setdefault("deferred_findings", {})["F-1"] = {
                "id": "F-1", "task": "OTHER", "status": "fix_in_progress", "follow_up_task": "GIT-MODEL",
                "classification": "in_scope", "next_action": "follow-up",
            }

    def finding_status(self):
        return board.snapshot(self.root)["deferred_findings"]["F-1"]["status"]

    def test_moved_main_does_not_stop_delivery_or_accept_the_release(self):
        self.move_main()
        self.add_follow_up_finding()
        response = board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertEqual(response["git_acceptance"]["status"], "reintegration_required")
        state = board.snapshot(self.root)
        self.assertEqual(state["releases"]["GIT-MODEL"]["status"], "REINTEGRATION_REQUIRED", "the stale release is off the owner's test list")
        self.assertNotIn("GIT-MODEL", state["release_decisions"], "the saved Accept must not stand as an acceptance")
        self.assertEqual(state["voided_release_decisions"]["GIT-MODEL"][-1]["decision"], "accepted")
        self.assertNotIn("delivery_stopped_after_acceptance", self.kinds())
        self.assertTrue(state["agents"][self.delivery["id"]]["active"])
        marker = state["git_reintegration_required"]["GIT-MODEL"]
        self.assertEqual(marker["source"], "owner-accept")
        self.assertIn("Your Accept did not go in", marker["owner_line"])
        self.assertTrue(marker.get("instruction_id"), "Delivery was told to re-integrate")
        self.assertEqual(self.finding_status(), "fix_in_progress", "no bookkeeping before the merge")

    def test_stopping_delivery_afterwards_does_not_call_the_task_accepted(self):
        self.move_main()
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        board.stop_session(self.root, self.delivery["session_id"])
        kinds = self.kinds()
        self.assertNotIn("delivery_stopped_after_acceptance", kinds)
        self.assertIn("delivery_stopped_task_kept", kinds)
        self.assertEqual(board.snapshot(self.root)["agents"][self.delivery["id"]]["status_note"], board.STOP_KEEPS_TASK_MESSAGE)

    def test_a_failed_accept_before_the_move_is_not_accepted_either(self):
        with board.locked_state(self.root) as state:
            good = state["releases"]["GIT-MODEL"]["acceptance_manifest"]
            state["releases"]["GIT-MODEL"]["acceptance_manifest"] = []
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")   # fails: manifest does not match
        board.stop_session(self.root, self.delivery["session_id"])
        self.assertNotIn("delivery_stopped_after_acceptance", self.kinds())
        # then main moves and the owner retries
        with board.locked_state(self.root) as state:
            state["releases"]["GIT-MODEL"]["acceptance_manifest"] = good
        self.move_main()
        response = board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertEqual(response["git_acceptance"]["status"], "reintegration_required")
        state = board.snapshot(self.root)
        self.assertNotIn("GIT-MODEL", state["release_decisions"])
        self.assertIn("GIT-MODEL", state["git_reintegration_required"])

    def test_the_stale_card_cannot_be_accepted_again_until_a_fresh_release(self):
        self.move_main()
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        with self.assertRaises(ValueError) as refused:
            board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertIn("released task", str(refused.exception))
        self.assertNotIn("GIT-MODEL", board.snapshot(self.root)["release_decisions"])

    def test_a_saved_accept_with_no_merge_yet_is_not_acceptance(self):
        """The gap between saving the decision and the merge (or a crash in it) must not read as accepted."""
        with board.locked_state(self.root) as state:
            state.setdefault("release_decisions", {})["GIT-MODEL"] = {
                "task": "GIT-MODEL", "decision": "accepted", "reason": "", "attachments": [], "recorded_at": board.now(),
            }
        board.stop_session(self.root, self.delivery["session_id"])
        self.assertNotIn("delivery_stopped_after_acceptance", self.kinds())
        with board.locked_state(self.root) as state:
            self.assertNotIn("GIT-MODEL", board._accepted_task_ids(state))

    def test_a_replacement_delivery_resumes_the_unmerged_task(self):
        self.move_main()
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        board.stop_session(self.root, self.delivery["session_id"])
        with board.locked_state(self.root) as state:
            source, task = board._stopped_task_to_resume(state, "someone-else")
        self.assertEqual(task, "GIT-MODEL")
        self.assertEqual(source, self.delivery["id"])

    def test_a_failed_accept_without_a_move_also_resumes(self):
        with board.locked_state(self.root) as state:
            state["releases"]["GIT-MODEL"]["acceptance_manifest"] = []
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        board.stop_session(self.root, self.delivery["session_id"])
        with board.locked_state(self.root) as state:
            self.assertEqual(board._stopped_task_to_resume(state, "someone-else")[1], "GIT-MODEL")
            self.assertNotIn("GIT-MODEL", board._accepted_task_ids(state))

    def test_an_old_routing_receipt_does_not_hide_the_relaunch_line(self):
        """Another task's accept routed this task earlier; that Delivery has since stopped. The failed Accept must re-route."""
        board.route_reintegration(self.root, "GIT-MODEL", "main advanced earlier", source="owner-accept")
        board.stop_session(self.root, self.delivery["session_id"])
        self.move_main()
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        marker = board.snapshot(self.root)["git_reintegration_required"]["GIT-MODEL"]
        self.assertEqual(marker["delivery"], "relaunch_required")
        self.assertIn("open a Delivery agent", marker["owner_line"])

    def test_a_board_stranded_by_the_old_bug_is_repaired_once(self):
        """Saved decision + marker + stopped Delivery, release still VISUAL_TEST_REQUIRED (the reported state)."""
        with board.locked_state(self.root) as state:
            state.setdefault("release_decisions", {})["GIT-MODEL"] = {
                "task": "GIT-MODEL", "decision": "accepted", "reason": "", "attachments": [], "recorded_at": board.now(),
            }
            state.setdefault("git_reintegration_required", {})["GIT-MODEL"] = {
                "task": "GIT-MODEL", "reason": "main moved", "recorded_at": board.now(),
            }
        board.reconcile_unmerged_accept(self.root, "GIT-MODEL")
        state = board.snapshot(self.root)
        self.assertNotIn("GIT-MODEL", state["release_decisions"])
        self.assertEqual(state["releases"]["GIT-MODEL"]["status"], "REINTEGRATION_REQUIRED")
        self.assertEqual(len(state["voided_release_decisions"]["GIT-MODEL"]), 1)
        board.reconcile_unmerged_accept(self.root, "GIT-MODEL")   # repeat: history is not duplicated
        self.assertEqual(len(board.snapshot(self.root)["voided_release_decisions"]["GIT-MODEL"]), 1)

    def test_after_reintegration_and_a_fresh_release_the_next_accept_merges(self):
        self.move_main()
        self.add_follow_up_finding()
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertEqual(self.finding_status(), "fix_in_progress")
        result = board.broker_reintegrate_main(self.root, self.delivery["id"])
        self.assertIn(result["status"], {"merged", "up_to_date"})
        workspace = Path(board.snapshot(self.root)["task_workspaces"]["GIT-MODEL"])
        with board.locked_state(self.root) as state:   # the CTO's reopen / a failed review lifts the freeze of the old candidate
            state["qa_requests"]["review-GIT-MODEL-final-01"]["status"] = "failed"
        (workspace / "product.txt").write_text("accepted again\n", encoding="utf-8")
        committed = board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "fresh candidate")
        with board.locked_state(self.root) as state:
            request = {
                "id": "review-GIT-MODEL-final-02", "task": "GIT-MODEL", "cycle": 2,
                "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance", "subtask": "", "chunk": "final",
                "status": "passed", "requested_at": board.now(), "developer_id": self.delivery["id"],
                "claimed_by": "qa-fixture", "review_wait_started_at": board.now(),
                "structure_revision": state["delivery_plans"]["GIT-MODEL"]["structure_revision"],
                "reviewed_commit": committed["commit"], "reviewed_tree_hash": committed["tree"], "reviewed_files": committed["manifest"],
            }
            state["qa_requests"][request["id"]] = request
            broker = board._broker_for_state(self.root, state, "GIT-MODEL")
            mirror = broker.create_review_ref(request, review_number=2, board_mutation=lambda record: request.update({
                "mirror_ref": record["ref"], "mirror_commit": record["commit"], "mirror_tree_hash": record["tree"],
                "mirror_transaction_id": record["transaction_id"]}))
            request["mirror_ref"] = mirror["ref"]
            state["releases"]["GIT-MODEL"] = {
                "task": "GIT-MODEL", "status": "VISUAL_TEST_REQUIRED", "head_commit": committed["commit"],
                "git_broker_governed": True, "acceptance_base_commit": self.fixture.git("rev-parse", "main").strip(),
                "acceptance_manifest": committed["manifest"], "cto_id": "cto-fixture", "recorded_at": board.now(),
            }
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        state = board.snapshot(self.root)
        self.assertEqual(state["releases"]["GIT-MODEL"]["status"], "ACCEPTED")
        self.assertIn("GIT-MODEL", state["git_acceptances"])
        self.assertEqual(self.finding_status(), "resolved")
        self.assertEqual(len(state["voided_release_decisions"]["GIT-MODEL"]), 1, "the history stays")

    def test_stop_in_the_gap_then_a_moved_main_still_hands_the_task_on(self):
        with board.locked_state(self.root) as state:
            state.setdefault("release_decisions", {})["GIT-MODEL"] = {
                "task": "GIT-MODEL", "decision": "accepted", "reason": "", "attachments": [], "recorded_at": board.now(),
            }
        board.stop_session(self.root, self.delivery["session_id"])
        self.move_main()
        with board.locked_state(self.root) as state:   # the interrupted Accept is now refused by the moved main
            state["release_decisions"].pop("GIT-MODEL")
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        with board.locked_state(self.root) as state:
            self.assertEqual(board._stopped_task_to_resume(state, "someone-else")[1], "GIT-MODEL")

    def test_a_legacy_stranded_board_gets_a_resumable_source_and_repeat_adds_nothing(self):
        with board.locked_state(self.root) as state:
            state.setdefault("release_decisions", {})["GIT-MODEL"] = {
                "task": "GIT-MODEL", "decision": "accepted", "reason": "", "attachments": [], "recorded_at": board.now(),
            }
            state.setdefault("git_reintegration_required", {})["GIT-MODEL"] = {"task": "GIT-MODEL", "reason": "main moved", "recorded_at": board.now()}
            agent = state["agents"][self.delivery["id"]]   # stopped by the old accepted-stop branch: no hand-over mark
            agent.update({"active": False, "status": "stopped", "liveness": "offline", "status_note": board.ACCEPTED_STOP_MESSAGE})
            agent.pop("stopped_by_owner_at", None)
        board.reconcile_unmerged_accept(self.root, "GIT-MODEL")
        with board.locked_state(self.root) as state:
            self.assertEqual(board._stopped_task_to_resume(state, "replacement")[1], "GIT-MODEL")
        before = self.kinds().count("git_acceptance_reintegration_required")
        board.reconcile_unmerged_accept(self.root, "GIT-MODEL")
        self.assertEqual(self.kinds().count("git_acceptance_reintegration_required"), before, "a repeat adds no event or routing")

    def test_a_delivery_already_archived_by_the_old_bug_is_brought_back_and_a_replacement_resumes_the_task(self):
        with board.locked_state(self.root) as state:
            state.setdefault("release_decisions", {})["GIT-MODEL"] = {
                "task": "GIT-MODEL", "decision": "accepted", "reason": "", "attachments": [], "recorded_at": board.now(),
            }
            state.setdefault("git_reintegration_required", {})["GIT-MODEL"] = {"task": "GIT-MODEL", "reason": "main moved", "recorded_at": board.now()}
            agent = state["agents"].pop(self.delivery["id"])   # the old housekeeping archived the accepted-looking Delivery
            agent.update({"active": False, "status": "stopped", "liveness": "offline", "status_note": board.ACCEPTED_STOP_MESSAGE})
            agent.pop("stopped_by_owner_at", None)
        board._append_cold(self.root, "agents", [agent])
        self.assertNotIn(self.delivery["id"], board.snapshot(self.root)["agents"])
        board.reconcile_unmerged_accept(self.root, "GIT-MODEL")
        self.assertIn(self.delivery["id"], board.snapshot(self.root)["agents"])
        from harness import control
        session = control.create(self.root, "codex_delivery")
        replacement = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        self.assertEqual(board.snapshot(self.root)["agents"][replacement["id"]]["task"], "GIT-MODEL", "the replacement carries the task on")
        board.reconcile_unmerged_accept(self.root, "GIT-MODEL")   # idempotent
        self.move_main()   # a moved main is what the replacement must now re-integrate
        done = board.broker_reintegrate_main(self.root, replacement["id"])
        self.assertIn(done["status"], {"merged", "up_to_date"})
        self.assertNotIn("GIT-MODEL", board.snapshot(self.root).get("git_reintegration_required", {}))
        self.assertEqual(sum(1 for a in board.snapshot(self.root)["agents"].values() if a["id"] == self.delivery["id"]), 1)

    def test_pause_resume_stages_delivery_for_owed_reintegration(self):
        self.move_main()
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        board.stop_session(self.root, self.delivery["session_id"])   # an owner Stop clears write authority
        self.assertFalse(board.snapshot(self.root)["agents"][self.delivery["id"]]["write_authority"])
        with board.locked_state(self.root, allow_paused=True) as state:
            state["task_owner_directions"]["GIT-MODEL"] = "OWNER DIRECTION - governed change."
            state["release_lifecycle"]["GIT-MODEL"] = {"development_completed_at": board.now(), "phases": {}}
            state["agents"][self.delivery["id"]].update({"active": False, "status": "stopped", "liveness": "offline"})
            state["project_pause"] = {"status": "resuming", "resume_id": "resume-1", "agents": {}}
        board.stage_required_delivery_resumes(self.root, "resume-1")
        staged = board.snapshot(self.root)["project_pause"]["agents"][self.delivery["id"]]
        self.assertTrue(staged["write_authority"], "the resumed owner can run reintegrate-main")

    def test_an_owed_accept_still_reserves_the_repository(self):
        self.move_main()
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        with board.locked_state(self.root) as state:
            self.assertTrue(board._task_reserves_repository(state, "GIT-MODEL"))

    def test_the_successful_accept_is_unchanged(self):
        self.add_follow_up_finding()
        response = board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        state = board.snapshot(self.root)
        self.assertEqual(state["releases"]["GIT-MODEL"]["status"], "ACCEPTED")
        self.assertEqual(state["release_decisions"]["GIT-MODEL"]["decision"], "accepted")
        self.assertNotIn("GIT-MODEL", state.get("git_reintegration_required", {}))
        self.assertEqual(self.finding_status(), "resolved", "bookkeeping happens once the merge succeeded")
        self.assertIn("GIT-MODEL", state["git_acceptances"])
        self.assertNotIn("reintegration_required", str(response.get("git_acceptance")))


if __name__ == "__main__":
    unittest.main()

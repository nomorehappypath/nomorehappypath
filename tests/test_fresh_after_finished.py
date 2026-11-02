# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Cost (2026-11-02): a new Delivery agent starts a fresh conversation when the previous agent's work is finished.

Run:  PYTHONPATH=. python3 -m unittest tests.test_fresh_after_finished -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import board, board_viewer, control

KIND = "codex_delivery"


class _Sessions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.first = self.ended(KIND, "cli-conversation-1")

    def ended(self, kind, cli_id):
        session = control.create(self.root, kind)
        control.attach(self.root, session["id"], os.getpid())
        control.record_cli_session(self.root, session["id"], cli_id, "claude")
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]].update(status="exited", ended_at=control.now(), pid=None)
        return session


class ControlRuleTests(_Sessions):
    def test_the_judged_finished_predecessor_with_nothing_undelivered_is_not_continued(self):
        fresh = control.create(self.root, KIND, finished_predecessor=self.first["id"])
        self.assertIsNone(fresh.get("cli_session_id"))
        self.assertIsNone(fresh.get("continues_session"))
        self.assertEqual(fresh["cli_fresh_reason"], "Started a new conversation: the previous Delivery agent's task is finished.")

    def test_no_judgment_means_the_conversation_is_continued_as_today(self):
        again = control.create(self.root, KIND)
        self.assertEqual(again["cli_session_id"], "cli-conversation-1")
        self.assertEqual(again["continues_session"], self.first["id"])
        self.assertNotIn("cli_fresh_reason", again)

    def test_a_judgment_about_another_session_changes_nothing(self):
        other = control.create(self.root, KIND, finished_predecessor="codex_delivery-not-this-one")
        self.assertEqual(other["continues_session"], self.first["id"], "only the exact session the caller judged is dropped")

    def test_the_newest_ended_session_wins_so_an_older_judged_one_is_not_dropped(self):
        newer = self.ended(KIND, "cli-conversation-2")
        with control.locked_state(self.root) as state:
            state["sessions"][newer["id"]]["ended_at"] = "2999-01-01T00:00:00+00:00"
        again = control.create(self.root, KIND, finished_predecessor=self.first["id"])
        self.assertEqual(again["continues_session"], newer["id"], "the inheritance picked another session than the one judged")

    def test_a_predecessor_with_an_undelivered_message_is_continued_and_the_message_moves_once(self):
        busy = control.create(self.root, KIND)
        control.attach(self.root, busy["id"], os.getpid())
        queued = control.enqueue_instruction(self.root, busy["id"], "Still queued.", "a")
        with control.locked_state(self.root) as state:
            state["sessions"][busy["id"]].update(status="exited", ended_at="2999-01-01T00:00:00+00:00", pid=None)
        again = control.create(self.root, KIND, finished_predecessor=busy["id"])
        self.assertEqual(again["continues_session"], busy["id"])
        self.assertEqual([entry["id"] for entry in control.take_instructions(self.root, again["id"])], [queued["id"]])

    def test_a_live_terminal_that_owns_the_conversation_is_never_shared(self):
        live = control.create(self.root, KIND)       # takes the conversation from the ended session
        control.attach(self.root, live["id"], os.getpid())
        third = control.create(self.root, KIND, finished_predecessor=self.first["id"])
        self.assertIsNone(third.get("continues_session"))
        self.assertNotIn("cli_fresh_reason", third, "no candidate at all: nothing was dropped for this reason")

    def test_the_card_keeps_the_reason_after_the_runner_records_a_first_launch(self):
        fresh = control.create(self.root, KIND, finished_predecessor=self.first["id"])
        control.note_cli_launch(self.root, fresh["id"], False, "first launch of this managed session")
        with control.locked_state(self.root) as state:
            reason = state["sessions"][fresh["id"]]["cli_launch_reason"]
        self.assertEqual(reason, "Started a new conversation: the previous Delivery agent's task is finished.")
        plain = control.create(self.root, KIND)
        control.note_cli_launch(self.root, plain["id"], True, "continues the previous conversation")
        with control.locked_state(self.root) as state:
            self.assertEqual(state["sessions"][plain["id"]]["cli_launch_reason"], "continues the previous conversation")

    def test_the_recovery_launch_and_other_roles_are_unchanged(self):
        recovery = control.create(self.root, KIND, fresh=True, finished_predecessor=self.first["id"])
        self.assertIsNone(recovery.get("continues_session"))
        self.assertNotIn("cli_fresh_reason", recovery)
        reviewer_first = self.ended("claude_reviewer", "cli-reviewer-1")
        reviewer = control.create(self.root, "claude_reviewer")
        self.assertEqual(reviewer["continues_session"], reviewer_first["id"])


def _state(**extra):
    state = {"release_decisions": {"T1": {"decision": "accepted"}}, "task_briefs": {"T1": {}}, "agents": {}, "owner_directions": {},
             "cancelled_tasks": {}, "releases": {}}
    state.update(extra)
    return state


class FinishedWorkTests(unittest.TestCase):
    def test_everything_accepted_is_finished(self):
        self.assertTrue(board.delivery_work_is_all_finished(_state()))

    def test_an_unfinished_task_is_not_finished(self):
        self.assertFalse(board.delivery_work_is_all_finished(_state(task_briefs={"T1": {}, "T2": {}})))

    def test_a_saved_accept_whose_merge_failed_is_not_finished(self):
        failed = {"T1": {"decision": "accepted", "git_acceptance": {"status": "failed"}}}
        self.assertFalse(board.delivery_work_is_all_finished(_state(release_decisions=failed)))

    def test_a_cancelled_task_does_not_count_as_unfinished(self):
        self.assertTrue(board.delivery_work_is_all_finished(_state(task_briefs={"T1": {}, "T2": {}}, cancelled_tasks={"T2": {}})))

    def test_a_task_stopped_by_the_owner_is_carried_on_not_dropped(self):
        stopped = {"A": {"id": "A", "role": "engineering", "task": "T-OLD", "stopped_by_owner_at": "2026-11-02T10:00:00+00:00", "active": False}}
        state = _state(agents=stopped, task_briefs={"T1": {}, "T-OLD": {}})
        self.assertFalse(board.delivery_work_is_all_finished(state))

    def test_task_a_stopped_unfinished_and_task_b_accepted_resumes_as_today(self):
        stopped = {"A": {"id": "A", "role": "engineering", "task": "T-A", "stopped_by_owner_at": "2026-11-02T10:00:00+00:00", "active": False},
                   "B": {"id": "B", "role": "engineering", "task": "T1", "active": False}}
        state = _state(agents=stopped, task_briefs={"T1": {}, "T-A": {}}, owner_directions={"A": {"text": "do A"}})
        self.assertFalse(board.delivery_work_is_all_finished(state))

    def test_a_live_agent_holding_a_task_or_an_unread_direction_is_not_finished(self):
        live = {"A": {"id": "A", "role": "engineering", "task": "T1", "active": True}}
        self.assertFalse(board.delivery_work_is_all_finished(_state(agents=live)))
        self.assertFalse(board.delivery_work_is_all_finished(_state(owner_directions={"S": {"text": "start this"}})))
        self.assertTrue(board.delivery_work_is_all_finished(_state(owner_directions={"S": {"text": "done", "consumed": True}})))


class CallerTests(_Sessions):
    def test_both_launch_paths_pass_the_judged_predecessor(self):
        with mock.patch.object(board_viewer, "_finished_delivery_predecessor", return_value="S-FINISHED"), \
                mock.patch.object(board_viewer, "launch_terminal", return_value=None), \
                mock.patch.object(board_viewer.control, "create", return_value={"id": "S-NEW"}) as create:
            board_viewer.create_session_request(self.root, {"kind": KIND}, None)
            self.assertEqual(create.call_args.kwargs["finished_predecessor"], "S-FINISHED")
            board_viewer.create_session_request(self.root, {"kind": "claude_reviewer"}, None)
            self.assertEqual(create.call_args.kwargs["finished_predecessor"], "", "only a Delivery launch is ever judged")
        with mock.patch.object(board_viewer, "_finished_delivery_predecessor", return_value="S-FINISHED"), \
                mock.patch.object(board_viewer, "launch_terminal", return_value=None), \
                mock.patch.object(board_viewer.control, "create", return_value={"id": "S-NEW"}) as create:
            with board.locked_state(self.root) as state:
                state["deferred_findings"] = {"F1": {"status": "fix_requested", "id": "F1", "task": "T-F", "classification": "x", "next_action": "fix it"}}
            board_viewer.dispatch_approved_findings(self.root, None)
            self.assertEqual(create.call_args.kwargs["finished_predecessor"], "S-FINISHED")

    def test_the_helper_judges_the_real_predecessor_only_when_the_whole_project_is_finished(self):
        with mock.patch.object(board, "delivery_work_is_all_finished", return_value=True):
            self.assertEqual(board_viewer._finished_delivery_predecessor(self.root), self.first["id"])
        with mock.patch.object(board, "delivery_work_is_all_finished", return_value=False):
            self.assertEqual(board_viewer._finished_delivery_predecessor(self.root), "")


if __name__ == "__main__":
    unittest.main()

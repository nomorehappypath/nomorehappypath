# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A formatting refusal never freezes a task (windows-walk item 11).

2026-10-21: five over-long status notes from a verbose Reviewer opened `repeated_refusal:status` on the owner's Windows project, a hold
only the owner could clear. The refusal now names the fix and the formatting refusals never count toward a hold; a hold that only such a
refusal opened ends when the next status is accepted.

Run:  PYTHONPATH=. python3 -m unittest tests.test_status_note_limit -v
"""
from __future__ import annotations

import json
import unittest

from harness import board, board_surface
from tests import test_refusal_hold_right_task as base

OLD_REASON = "The same operation was refused 5 times: status note is required and must be 240 characters or fewer"


class StatusNoteLimitTests(base.RefusalHoldTaskTests):
    def refusal(self, call):
        with self.assertRaises(ValueError) as caught:
            call()
        return str(caught.exception)

    def test_a_long_status_note_is_refused_with_its_length_and_the_fix_and_stores_nothing(self):
        agent, _ = self.developer("NOTE-TASK")
        text = self.refusal(lambda: board.status(self.context, agent["id"], "n" * 311))
        self.assertEqual(text, "status note is 311 characters; the limit is 240; shorten it and post again")
        self.assertEqual(board.status(self.context, agent["id"], "n" * 240)["message"], "n" * 240, "240 characters are stored unchanged")
        self.assertEqual(self.refusal(lambda: board.status(self.context, agent["id"], "   ")), board.STATUS_NOTE_REQUIRED_MESSAGE)

    def test_five_long_status_notes_through_the_gateway_open_no_hold(self):
        agent, identity = self.developer("NOTE-TASK")
        text = self.refusal(lambda: board.status(self.context, agent["id"], "n" * 311))
        self.assertEqual(self.track(identity, "status", text, ["status"], times=9), {}, "fails on main: the fifth refusal opens a hold")
        empty = self.refusal(lambda: board.status(self.context, agent["id"], ""))
        self.assertEqual(self.track(identity, "status", empty, ["status"], times=9), {})

    def test_an_over_long_offline_note_opens_no_hold(self):
        agent, identity = self.developer("NOTE-TASK")
        text = self.refusal(lambda: board.offline(self.context, agent["id"], "o" * 300))
        self.assertEqual(text, "offline note is 300 characters; the limit is 240; shorten it and post again")
        self.assertEqual(self.track(identity, "offline", text, ["offline"], times=9), {})

    def test_lookalikes_and_other_operations_still_open_the_hold(self):
        _, identity = self.developer("NOTE-TASK")
        good = "status note is 311 characters; the limit is 240; shorten it and post again"
        for operation, error in (("start-subtask", good), ("status", "workspace x: " + good), ("status", good + " (really)"),
                                 ("status", "status note is many characters; the limit is 240; shorten it and post again"),
                                 ("offline", good), ("status", "inactive/offline agent cannot post status updates")):
            with self.subTest(operation=operation, error=error):
                self.assertTrue(self.track(identity, operation, error, [operation, "--subtask", "a"], times=9), "a real wedge is still held")
                with board.locked_state(self.context) as state:
                    state["control_plane_holds"] = {}

    def test_the_task_brief_exemption_is_kept(self):
        _, identity = self.developer("BRIEF-TASK")
        self.assertEqual(self.track(identity, "task-brief", self.BRIEF_REFUSAL, ["task-brief"]), {})

    # ---- a hold that only a formatting refusal opened ends at the next accepted status -------------------------------------------
    def hold_then_status(self, source, why, recorded_at=None, task="NOTE-TASK"):
        if not hasattr(self, "_agent"):
            self._agent, _ = self.developer(task)
        agent, task = self._agent, "NOTE-TASK"
        with board.locked_state(self.context) as state:
            state["control_plane_holds"] = {}
        board.record_control_plane_hold(self.context, task, source, why)
        if recorded_at:
            with board.locked_state(self.context) as state:
                state["control_plane_holds"][task]["recorded_at"] = recorded_at
        before = json.dumps(board.snapshot(self.context).get("qa_requests", {}), sort_keys=True)
        board.status(self.context, agent["id"], "a normal status")
        after = json.dumps(board.snapshot(self.context).get("qa_requests", {}), sort_keys=True)
        self.assertEqual(before, after, "certified results are untouched")
        return board.snapshot(self.context)["control_plane_holds"][task]

    def test_the_owners_historical_status_hold_ends_when_the_next_status_is_accepted(self):
        hold = self.hold_then_status("repeated_refusal:status", OLD_REASON, "2020-01-01T00:00:00+00:00")
        self.assertEqual(hold["status"], "resolved", json.dumps(hold, indent=2))
        self.assertIn("only over its format limit", hold["outcome"])

    def test_the_exact_hold_recorded_on_the_owners_windows_project_ends_too(self):
        # the shape of the hold recorded on the owner's Windows project (2026-10-21): same reason, source and timestamp format
        hold = self.hold_then_status("repeated_refusal:status", OLD_REASON, "2026-10-21T16:50:02.789025+00:00")
        self.assertEqual(hold["status"], "resolved", json.dumps(hold, indent=2))

    def test_the_new_wording_and_an_offline_hold_end_the_same_way(self):
        new = "The same operation was refused 5 times: status note is 400 characters; the limit is 240; shorten it and post again"
        self.assertEqual(self.hold_then_status("repeated_refusal:status", new, "2020-01-01T00:00:00+00:00")["status"], "resolved")
        off = "The same operation was refused 5 times: offline note is 400 characters; the limit is 240; shorten it and post again"
        self.assertEqual(self.hold_then_status("repeated_refusal:offline", off, "2020-01-01T00:00:00+00:00")["status"], "resolved")

    def test_any_other_hold_stays_open(self):
        for source, why in (("repeated_refusal:status", "The same operation was refused 5 times: inactive/offline agent cannot post status updates"),
                            ("repeated_refusal:status", "The same operation was refused 5 times: workspace x: " + OLD_REASON.split(": ")[1]),
                            ("repeated_refusal:poll", OLD_REASON), ("repeated_refusal:qa-result", OLD_REASON)):
            with self.subTest(source=source, why=why):
                hold = self.hold_then_status(source, why, "2020-01-01T00:00:00+00:00")
                self.assertEqual(hold["status"], "open")

    def test_a_status_older_than_the_hold_or_with_unknown_chronology_keeps_it(self):
        self.assertEqual(self.hold_then_status("repeated_refusal:status", OLD_REASON, "2999-01-01T00:00:00+00:00")["status"], "open")
        self.assertEqual(self.hold_then_status("repeated_refusal:status", OLD_REASON, "not a time")["status"], "open")


# the inherited tests run in tests.test_refusal_hold_right_task; only this class's own tests run here
for _name in [n for n in dir(base.RefusalHoldTaskTests) if n.startswith("test_") and n not in StatusNoteLimitTests.__dict__]:
    setattr(StatusNoteLimitTests, _name, None)


if __name__ == "__main__":
    unittest.main()

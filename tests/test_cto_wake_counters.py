# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Counting how often the CTO is woken (2026-10-26_lighter-cto, commit 1: counting only).

The board keeps a total, a count by reason, a count per task, and the CTO's own polls and posts, so the before and the
after of the lighter-CTO change are measured with the same instrument.

Run:  PYTHONPATH=. python3 -m unittest tests.test_cto_wake_counters -v
"""
import json
import tempfile
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import board, control, review_cost
from tests import test_cto_realtime_wake as realtime

_GOLDEN_RUNNER = Path(__file__).resolve().parents[1] / "evals" / "golden" / "run_eval.py"
if not _GOLDEN_RUNNER.is_file():
    raise unittest.SkipTest("golden eval set: only where evals/golden ships")
_spec = importlib.util.spec_from_file_location("run_eval", _GOLDEN_RUNNER)
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


class CtoWakeCounters(unittest.TestCase):
    setUp = realtime.CtoRealtimeWake.setUp
    _ledger = realtime.CtoRealtimeWake._ledger
    _evidence = realtime.CtoRealtimeWake._evidence
    _run_review = realtime.CtoRealtimeWake._run_review

    def counters(self):
        return board.snapshot(self.root).get("cto_wakes") or {}

    def test_final_pass_and_completion_each_count_one_release_wake(self):
        self._run_review("final_acceptance", "passed")
        self.assertEqual(self.counters()["by_reason"], {"release": 1})
        board.complete(self.root, self.dev["id"], "completion gates satisfied")
        found = self.counters()
        self.assertEqual((found["total"], found["by_reason"]), (2, {"release": 2}))
        self.assertEqual(found["by_task"], {"APP": {"release": 2}})

    def test_a_wake_the_watchdog_routes_is_counted_once_under_its_reason(self):
        board.mark_stalled(self.root)
        board.mark_stalled(self.root)   # the second tick finds the wake already routed: still one
        self.assertEqual(self.counters()["by_reason"], {"resume": 1})
        self.assertEqual(self.counters()["by_task"], {})   # a launch wake belongs to the project, not to one task

    def test_owner_message_poll_and_post_are_counted(self):
        board.record_owner_message(self.root, self.cto["id"], "Please look at the build.", "cto_message")
        board.poll(self.root, self.cto["id"])
        board.poll(self.root, self.cto["id"])
        board.status(self.root, self.cto["id"], "checked the board")
        found = self.counters()
        self.assertEqual(found["by_reason"], {"owner": 1})
        self.assertEqual((found["polls"], found["posts"]), (2, 1))

    def test_the_usage_line_and_the_eval_score_show_the_cto_wakes(self):
        self._run_review("final_acceptance", "passed")
        state = board.snapshot(self.root)
        usage = review_cost.usage(state, "APP")
        self.assertEqual(usage["cto_wakes"], 1)
        self.assertIn("CTO wakes: 1 (release 1).", usage["line"])
        self.assertNotIn("CTO wakes", review_cost.usage(state, "OTHER")["line"])
        path = Path(tempfile.mkdtemp()) / "state.json"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        path.write_text(json.dumps(state), encoding="utf-8")
        scored = run_eval.read_board(path, [])
        self.assertEqual((scored["cto_wakes_total"], scored["cto_wakes_by_reason"]), (1, {"release": 1}))
        path.write_text(json.dumps({"task_turns": {}}), encoding="utf-8")   # a board from before the counters
        self.assertIsNone(run_eval.read_board(path, [])["cto_wakes_total"])


if __name__ == "__main__":
    unittest.main()

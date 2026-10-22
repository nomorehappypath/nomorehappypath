# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Item 24: failed attempts and stuck tasks in plain words (the austin-temp shape).

Run:  PYTHONPATH=. python3 -m unittest tests.test_failure_words -v
"""
from __future__ import annotations

import json
import unittest

from harness import board, board_viewer, failure_words
from tests.test_stale_holds import _Fixture

DECODE = "'utf-8' codec can't decode byte 0xf8 in position 493: invalid start byte"
SECRET = "sk-ant-oat01-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


def failure(task="FILM", text=DECODE, at="2026-10-22T14:48:42+00:00", **extra):
    return {"id": "review-FILM-final-01", "task": task, "phase": "final_acceptance", "chunk": "final", "subtask": "",
            "result": "failed", "result_summary": text, "completed_at": at, "delivery_state": "failed", "status": "cancelled",
            "route_state": "delivery_failed_cancelled", **extra}


class SentenceTests(unittest.TestCase):
    def test_known_and_unknown_causes_name_blame_only_when_proven(self):
        rows = [
            (DECODE, "harness", "could not read what the test printed"),
            ("failed with exit code 1: UnicodeDecodeError: 'utf-8' codec can't decode byte", "", "did not pass (it ended with exit code 1)"),
            ("internal-QA test command timed out after 600 seconds; partial output", "", "within 600 seconds"),
            ("internal-QA test command reported zero executed tests", "", "did not run any test"),
            ("Something odd happened. Second sentence.", "", "The test run stopped before finishing: Something odd happened."),
        ]
        for text, blame, expect in rows:
            with self.subTest(text=text[:30]):
                said = failure_words.describe(text)
                self.assertEqual(said["blame"], blame)
                self.assertIn(expect, said["sentence"])
                self.assertEqual(said["technical"], text)


class AustinShapeTests(_Fixture):
    def state(self, **extra):
        return {**board.snapshot(self.root), **extra}

    def test_count_sentence_and_a_later_pass_hides_old_failures(self):
        failures = [failure(at=f"2026-10-22T14:4{n}:00+00:00") for n in range(3)] + [failure(text=SECRET + " boom", at="2026-10-22T14:55:00+00:00")]
        state = {"delivery_attempt_failures": failures, "qa_requests": {}, "qa_request_index": {}}
        said = failure_words.failures_for_state(state, ["FILM"], board._redact_secrets)["delivery"]["FILM"]
        self.assertEqual(said["count"], 4)
        self.assertTrue(said["headline"].startswith("Test failed 4 times: "), said["headline"])
        self.assertNotIn(SECRET, json.dumps(said))
        self.assertEqual(len(said["details"]), 4)
        state["qa_requests"] = {"p": {"id": "p", "task": "FILM", "phase": "final_acceptance", "chunk": "final", "subtask": "",
                                      "status": "passed", "completed_at": "2026-10-22T15:30:00+00:00"}}
        self.assertEqual(failure_words.failures_for_state(state, ["FILM"])["delivery"], {})
        # a reviewer's own execution failed, then passed on the same request: nothing current; a failed judgment stays its own item
        request = {"id": "r", "task": "FILM", "phase": "final_acceptance", "chunk": "final", "subtask": "", "status": "claimed",
                   "challenge_execution_attempts": [{"status": "failed", "recorded_at": "2026-10-22T14:00:00+00:00", "reason": "boom"},
                                                    {"status": "passed", "recorded_at": "2026-10-22T14:10:00+00:00"}]}
        judged = {"id": "j", "task": "FILM", "phase": "chunk", "chunk": "c1", "subtask": "", "status": "failed", "result": "failed",
                  "result_summary": "The scenario failed.", "completed_at": "2026-10-22T14:20:00+00:00"}
        said = failure_words.failures_for_state({"qa_requests": {"r": request, "j": judged}}, ["FILM"])["reviewer"]["FILM"]
        self.assertEqual(said["count"], 1)
        self.assertIn("The scenario failed", said["sentence"])

    def test_hold_card_appears_once_clears_and_never_merges_with_an_ordinary_card(self):
        dev = self.delivery("FILM")
        reason = f"The same operation was refused 5 times: {DECODE}"
        board.record_control_plane_hold(self.root, "FILM", "repeated_refusal:request-review", reason)
        with board.locked_state(self.root) as state:
            board._record_owner_action(state, {"id": "system", "role": "system"}, "Ordinary request", "", "", "FILM", kind="task")
        board.record_control_plane_hold(self.root, "FILM", "release_coordinator:merge", f"{reason} {SECRET}")
        cards = [c for c in board.snapshot(self.root)["owner_actions"].values() if c["status"] == "open"]
        holds = [c for c in cards if c.get("hold_task")]
        self.assertEqual((len(cards), len(holds)), (2, 1), cards)
        card = holds[0]
        for needle in ("FILM", "could not read what the test printed", "wait", "cancel", "Clearing the hold", "harness team is on it"):
            self.assertIn(needle, card["why"])
        self.assertIn("clear-hold --task FILM --source release_coordinator:merge", card["command"])
        self.assertNotIn(SECRET, json.dumps(card))
        compact = board_viewer._compact_dashboard_state(board.snapshot(self.root), ["FILM"])
        self.assertNotIn("delivery_attempt_failures", compact)
        self.assertNotIn(SECRET, json.dumps(compact))
        board.clear_control_plane_hold(self.root, "FILM", "release_coordinator")
        now_open = [c for c in board.snapshot(self.root)["owner_actions"].values() if c["status"] == "open" and c.get("hold_task")]
        self.assertEqual(now_open, [])
        self.cancel("FILM", dev)

    def test_cancel_closes_the_card_and_an_older_hold_without_a_card_is_still_shown(self):
        dev = self.delivery("FILM")
        self.hold("FILM")
        self.cancel("FILM", dev)
        self.assertEqual([c for c in board.snapshot(self.root)["owner_actions"].values() if c["status"] == "open" and c.get("hold_task")], [])
        # a board written before holds raised cards: the dashboard shows it, paused or not, without writing
        self.delivery("OTHER")
        with board.locked_state(self.root) as state:
            state["control_plane_holds"]["OTHER"] = {"task": "OTHER", "source": "repeated_refusal:status", "status": "open",
                                                     "reason": "The same operation was refused 5 times: nope nope", "recorded_at": "2026-10-22T14:52:49+00:00"}
            state["project_pause"] = {"status": "paused"}
        compact = board_viewer._compact_dashboard_state(board.snapshot(self.root), ["OTHER"], root=self.root)
        shown = [c for c in compact["owner_actions"].values() if c.get("hold_task") == "OTHER"]
        self.assertEqual(len(shown), 1)
        self.assertIn("clear-hold --task OTHER", shown[0]["command"])
        self.assertNotIn("OTHER", [c.get("hold_task") for c in board.snapshot(self.root).get("owner_actions", {}).values()])


class SuccessBoundaryTests(unittest.TestCase):
    """Round 2: Delivery's success is not an independent review PASS (and vice versa for what each clears)."""
    scope = dict(task="APP", phase="final_acceptance", chunk="final", subtask="")

    def test_delivery_pass_does_not_erase_a_failed_independent_verdict(self):
        failed = dict(self.scope, id="r1", status="failed", result="failed", delivery_state="passed", requested_at="2026-10-22T10:00:00+00:00",
                      completed_at="2026-10-22T10:10:00+00:00", result_summary="Rendered page shows the wrong temperature.")
        retry = dict(self.scope, id="r2", status="claimed", delivery_state="passed", requested_at="2026-10-22T10:20:00+00:00", completed_at=None)
        said = failure_words.failures_for_state({"qa_requests": {"r1": failed, "r2": retry}}, ["APP"])
        self.assertEqual(said["reviewer"]["APP"]["count"], 1)
        passed = dict(retry, id="r3", status="passed", completed_at="2026-10-22T10:40:00+00:00")
        self.assertEqual(failure_words.failures_for_state({"qa_requests": {"r1": failed, "r3": passed}}, ["APP"])["reviewer"], {})

    def test_delivery_pass_does_not_erase_a_failed_reviewer_execution(self):
        failed = dict(self.scope, id="r1", status="cancelled", delivery_state="passed", requested_at="2026-10-22T10:00:00+00:00",
                      completed_at="2026-10-22T10:10:00+00:00",
                      challenge_execution_attempts=[dict(status="failed", recorded_at="2026-10-22T10:05:00+00:00", reason="failed with exit code 1: assertion")])
        self.assertIn("APP", failure_words.failures_for_state({"qa_requests": {"r1": failed}}, ["APP"])["reviewer"])

    def test_a_delivery_failure_is_cleared_by_a_later_delivery_success_only(self):
        old = failure(task="APP", at="2026-10-22T10:05:00+00:00", phase="final_acceptance", chunk="final", subtask="")
        later = dict(self.scope, id="r2", status="reserved", delivery_state="passed", requested_at="2026-10-22T10:20:00+00:00")
        earlier = dict(later, requested_at="2026-10-22T09:00:00+00:00")
        self.assertEqual(failure_words.failures_for_state({"delivery_attempt_failures": [old], "qa_requests": {"r2": later}}, ["APP"])["delivery"], {})
        self.assertIn("APP", failure_words.failures_for_state({"delivery_attempt_failures": [old], "qa_requests": {"r2": earlier}}, ["APP"])["delivery"])


if __name__ == "__main__":
    unittest.main()

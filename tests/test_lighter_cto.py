# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A lighter CTO (2026-10-26_lighter-cto, commit 2): woken for a reason, never on a clock.

One test per wake reason, the slow check-in, the pending-judgment item surviving a restart, a pause and a replacement, no stall
marking for a quiet CTO, a dead terminal still recovered, and the counters recording every wake.

Run:  PYTHONPATH=. python3 -m unittest tests.test_lighter_cto -v
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from harness import board, control, cto_wakes, release_coordinator, self_heal
from tests import test_cto_realtime_wake as realtime

WAKE_SOURCES = {"cto-wake", "final-acceptance-pass", "development-complete", "release-coordinator", "stall-ladder"}


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


class _Fixture(unittest.TestCase):
    """A CTO and one live Delivery task. `settle()` takes the one launch wake and answers it, so each test then sees only its own reason."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        cto_wakes.configure({"cto_heartbeat_seconds": 1800})   # these tests exercise the check-in mechanics; the 24-hour default has its own test
        self.addCleanup(cto_wakes.configure, {})
        self.cto_session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=self.cto_session["id"])
        delivery_session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=delivery_session["id"])
        board.record_owner_direction(self.root, delivery_session["id"], "Implement the active task.")
        board.begin_task(self.root, self.delivery["id"], "APP")

    def state(self):
        return board.snapshot(self.root)

    def cto_agent(self):
        return self.state()["agents"][self.cto["id"]]

    def wakes(self):
        return [item for item in control.take_instructions(self.root, self.cto_session["id"]) if item["source"] in WAKE_SOURCES]

    def tick(self):
        board.mark_stalled(self.root)
        return self.wakes()

    def settle(self):
        launch = self.tick()
        self.assertEqual(len(launch), 1, "a new CTO with a live task is woken once to re-triage what it inherited")
        self.answer(launch)
        for item in cto_wakes.open_items(self.state()):
            self.dispose(item["id"])

    def answer(self, _wakes=None):
        board.poll(self.root, self.cto["id"])

    def dispose(self, item_id, outcome="nothing"):
        return board.record_cto_disposition(self.root, self.cto["id"], item_id, outcome, "looked at it")

    def shift(self, seconds):
        """Make everything the board recorded about the CTO's timing `seconds` older (a way to let time pass)."""
        def older(value):
            moment = datetime.fromisoformat(value) if value else None
            return (moment - timedelta(seconds=seconds)).isoformat() if moment else value
        with board.locked_state(self.root) as state:
            found = state.setdefault("cto_wakes", {})
            if found.get("last_heartbeat_at"):
                found["last_heartbeat_at"] = older(found["last_heartbeat_at"])
            for key in ("last_poll_at", "last_progress_at", "last_status_at", "automatic_recovery_requested_at", "spawned_at"):
                if state["agents"][self.cto["id"]].get(key):
                    state["agents"][self.cto["id"]][key] = older(state["agents"][self.cto["id"]][key])

    def by_reason(self):
        return dict(self.state().get("cto_wakes", {}).get("by_reason", {}))


class OneTestPerWakeReason(_Fixture):
    def assert_woken_for(self, wakes, phrase):
        self.assertEqual(len(wakes), 1, wakes)
        self.assertIn(phrase, wakes[0]["text"])
        self.assertIn(f"poll --agent {self.cto['id']}", wakes[0]["text"])
        self.assertIn("cto-disposition", wakes[0]["text"])

    def test_finding_wakes_within_one_tick_even_right_after_a_poll(self):
        self.settle()
        self.answer()                       # a poll this very second
        self.assertEqual(self.tick(), [], "nothing is due and the CTO is not stalled")
        board.record_finding(self.root, "APP", "Total is wrong", "The page adds tax twice.", affects_current_task=True)
        self.assert_woken_for(self.tick(), "a new finding")
        self.assertEqual(self.by_reason()["finding"], 1)

    def test_a_refused_release_check_wakes_for_the_release(self):
        self.settle()
        with board.locked_state(self.root) as state:      # what release_coordinator._incident records
            board._event(state, "control_plane_incident", None, {"task": "APP", "step": "checks", "message": "release coordinator checks failed: tests_green"})
        self.assert_woken_for(self.tick(), "a release step")
        self.assertEqual(self.by_reason()["release"], 1)

    def test_release_prepared_wakes_once_names_the_command_and_opens_an_item(self):
        self.settle()
        checks = Path(self._tmp.name) / "checks.json"
        checks.write_text("{}\n")
        state = self.state()
        self.assertTrue(release_coordinator._route_prepared(self.root, state, "APP", "a" * 40, checks, "key-1"))
        self.assertTrue(release_coordinator._route_prepared(self.root, state, "APP", "a" * 40, checks, "key-1"), "journaled: not sent twice")
        sent = [w for w in self.wakes() if w["source"] == "release-coordinator"]
        self.assertEqual(len(sent), 1)
        self.assertIn("RELEASE PREPARED", sent[0]["text"])
        self.assertIn("record-release --task APP", sent[0]["text"])
        self.assertIn("cto-disposition", sent[0]["text"])
        self.assertEqual([i["reason"] for i in cto_wakes.open_items(self.state())], ["release"])
        self.assertEqual(self.by_reason()["release"], 1)

    def test_a_card_the_board_pins_for_the_owner_wakes_for_the_owner_summary(self):
        self.settle()
        with board.locked_state(self.root) as state:
            board._record_owner_action(state, {"id": "system", "role": "system"}, "Open the app and test the new page", "", "It is ready.", "APP", kind="task")
        self.assert_woken_for(self.tick(), "an owner card")
        self.assertEqual(self.by_reason()["owner_summary"], 1)

    def test_a_stuck_item_wakes_but_a_stalled_delivery_agent_does_not(self):
        self.settle()
        with board.locked_state(self.root) as state:
            agent = state["agents"][self.delivery["id"]]
            board._event(state, "agent_stalled", agent, {"task": "APP", "message": "no heartbeat"})   # the stall ladder owns this one
        self.assertEqual(self.tick(), [])
        with board.locked_state(self.root) as state:
            board._event(state, "git_acceptance_failed", state["agents"][self.delivery["id"]], {"task": "APP", "message": "the push was refused"})
        self.assert_woken_for(self.tick(), "a stuck item")
        self.assertEqual(self.by_reason()["stuck"], 1)

    def test_resume_and_launch_each_wake_once_and_never_again_after_judgment(self):
        launch = self.tick()
        self.assert_woken_for(launch, "a resume or launch")
        self.answer()
        self.assertEqual(self.tick(), [], "the same launch is not woken for twice")
        self.dispose(cto_wakes.open_items(self.state())[0]["id"])
        with board.locked_state(self.root) as state:
            state["project_pause"] = {"status": "active", "resumed_at": _iso(5)}
        self.assert_woken_for(self.tick(), "a resume or launch")
        self.answer()
        for item in cto_wakes.open_items(self.state()):
            self.dispose(item["id"])
        self.assertEqual(self.tick(), [], "a judged resume does not come back")
        self.assertEqual(self.by_reason()["resume"], 2)


class ReviewPath(unittest.TestCase):
    """The producers that need a real review behind them: a Reviewer's FAIL, a final PASS, a completion."""
    setUp = realtime.CtoRealtimeWake.setUp
    _ledger = realtime.CtoRealtimeWake._ledger
    _evidence = realtime.CtoRealtimeWake._evidence
    _run_review = realtime.CtoRealtimeWake._run_review

    def test_a_second_failed_review_of_the_same_part_is_a_disputed_finding(self):
        self._run_review("final_acceptance", "failed")
        self.assertEqual([i["reason"] for i in cto_wakes.open_items(board.snapshot(self.root))], ["finding"])
        with board.locked_state(self.root) as state:   # Delivery repaired it and the Reviewer failed the same part again
            request = next(r for r in state["qa_requests"].values() if r.get("status") == "failed")
            key = board.review_rounds.lineage_key(request)
            state["review_escalations"][key]["failed_judgments"] = 2
            cto_wakes.review_failed(state, {**request, "id": "second-review"}, state["review_escalations"][key])
        self.assertEqual(sorted(i["reason"] for i in cto_wakes.open_items(board.snapshot(self.root))), ["disputed", "finding"])

    def test_final_pass_and_completion_each_wake_for_the_release_and_open_an_item(self):
        self._run_review("final_acceptance", "passed")
        board.complete(self.root, self.dev["id"], "completion gates satisfied")
        texts = [w["text"] for w in control.take_instructions(self.root, self.cto_session["id"])]
        self.assertTrue(any("FINAL PASS RECORDED" in t and "cto-disposition" in t for t in texts), texts)
        self.assertTrue(any("DEVELOPMENT COMPLETE" in t and "Still unjudged" in t for t in texts), texts)
        subjects = {i["subject"].split(":")[0] for i in cto_wakes.open_items(board.snapshot(self.root))}
        self.assertLessEqual({"completion", "final-pass"}, subjects)
        self.assertEqual(board.snapshot(self.root)["cto_wakes"]["by_reason"], {"release": 2})


class SlowCheckIn(_Fixture):
    def test_sixty_quiet_minutes_cost_exactly_two_wakes_and_never_a_stall(self):
        self.settle()
        woken = 0
        for _ in range(4):                       # four quarter-hours with no event at all
            self.shift(900)
            got = self.tick()
            woken += len(got)
            if got:
                self.assertIn("the slow check-in", got[0]["text"])
                self.answer()
                self.dispose(cto_wakes.open_items(self.state())[0]["id"])
            self.assertEqual(self.cto_agent()["liveness"], "healthy")
        self.assertEqual(woken, 2)
        self.assertEqual(self.by_reason()["heartbeat"], 2)
        self.assertEqual([e for e in self.state()["events"] if e["kind"] in {"agent_stalled", "agent_unresponsive"}], [])
        self.assertEqual([a for a in self_heal.plan(self.root) if a.get("agent_id") == self.cto["id"]], [])

    def test_the_check_in_still_fires_when_nothing_changed_and_the_interval_is_a_setting(self):
        self.settle()
        cto_wakes.configure({})
        self.assertEqual(cto_wakes.heartbeat_seconds(), 86_400)   # a backstop once a day, not a paid timer (2026-10-28)
        cto_wakes.configure({"cto_heartbeat_seconds": 120})
        self.assertEqual(cto_wakes.heartbeat_seconds(), 120)
        cto_wakes.configure({"cto_heartbeat_seconds": 5})   # too small: back to the default, never off or every tick
        self.assertEqual(cto_wakes.heartbeat_seconds(), 86_400)
        cto_wakes.configure({"cto_heartbeat_seconds": 120})
        cto_wakes.configure({"cto_heartbeat_seconds": 120})
        self.shift(130)
        self.assertEqual(len(self.tick()), 1)


class PendingJudgment(_Fixture):
    def test_a_poll_and_a_status_post_never_close_an_item_and_a_disposition_does(self):
        self.settle()
        finding = board.record_finding(self.root, "APP", "Total is wrong", "The page adds tax twice.", affects_current_task=True)
        self.assertEqual(len(self.tick()), 1)
        self.answer()
        board.status(self.root, self.cto["id"], "looked at the board", "working")
        open_now = cto_wakes.open_items(self.state())
        self.assertEqual([i["subject"] for i in open_now], [finding["id"]])
        self.shift(1900)                              # the next wake (the slow check-in) names it again
        again = self.tick()
        self.assertIn(open_now[0]["id"], again[0]["text"])
        self.assertIn("Still unjudged", again[0]["text"])
        self.dispose(open_now[0]["id"], "route")
        self.assertEqual([i for i in cto_wakes.open_items(self.state()) if i["reason"] == "finding"], [])

    def test_repeated_wakes_for_the_same_reason_and_subject_are_merged(self):
        self.settle()
        for _ in range(3):
            with board.locked_state(self.root) as state:
                board._event(state, "git_acceptance_failed", state["agents"][self.delivery["id"]], {"task": "APP", "message": "the push was refused"})
        self.assertEqual(len([i for i in cto_wakes.open_items(self.state()) if i["reason"] == "stuck"]), 1)
        self.assertEqual(len(self.tick()), 1)

    def test_the_item_survives_a_restart_a_pause_and_a_replaced_terminal(self):
        self.settle()
        board.record_finding(self.root, "APP", "Total is wrong", "The page adds tax twice.", affects_current_task=True)
        self.tick(); self.answer()
        item = cto_wakes.open_items(self.state())[0]
        # restart: nothing lives in memory, the item is read back from the board's file
        self.assertEqual(cto_wakes.open_items(board.snapshot(self.root))[0]["id"], item["id"])
        # pause and resume
        with board.locked_state(self.root, allow_paused=True) as state:
            state["project_pause"] = {"status": "paused"}
        with board.locked_state(self.root, allow_paused=True) as state:
            state["project_pause"] = {"status": "active", "resumed_at": _iso(1)}
        self.assertIn(item["id"], [i["id"] for i in cto_wakes.open_items(self.state())])
        # terminal replacement: the old CTO ends, a new one registers and is told what is still unjudged
        control.fail_launch(self.root, self.cto_session["id"], "terminal replaced")
        with board.locked_state(self.root) as state:
            state["agents"][self.cto["id"]].update({"active": False, "status": "offline", "liveness": "offline"})
        session = control.create(self.root, "claude_cto")
        replacement = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
        board.mark_stalled(self.root)
        texts = [w["text"] for w in control.take_instructions(self.root, session["id"]) if w["source"] == "cto-wake"]
        self.assertEqual(len(texts), 1)
        self.assertIn(item["id"], texts[0])
        self.assertNotEqual(replacement["id"], self.cto["id"])

    def test_only_the_active_cto_may_record_a_disposition_and_it_needs_a_note(self):
        self.settle()
        board.record_finding(self.root, "APP", "Total is wrong", "The page adds tax twice.", affects_current_task=True)
        item = cto_wakes.open_items(self.state())[0]
        with self.assertRaisesRegex(ValueError, "only the active CTO"):
            board.record_cto_disposition(self.root, self.delivery["id"], item["id"], "nothing", "x")
        with self.assertRaisesRegex(ValueError, "one-line note"):
            board.record_cto_disposition(self.root, self.cto["id"], item["id"], "nothing", " ")
        with self.assertRaisesRegex(ValueError, "outcome"):
            board.record_cto_disposition(self.root, self.cto["id"], item["id"], "maybe", "x")
        self.assertEqual(board.record_cto_disposition(self.root, self.cto["id"], "all", "hold", "held until the fix is reviewed")["closed"], [item["id"]])


class LivenessOfAQuietCto(_Fixture):
    def test_a_cto_that_has_not_polled_for_a_day_is_not_stalled_and_not_recovered(self):
        self.settle()
        self.shift(86_400 - 1900)
        with board.locked_state(self.root) as state:
            state["cto_wakes"]["last_heartbeat_at"] = _iso(10)   # no check-in due either
        for _ in range(3):
            self.assertEqual(self.tick(), [])
        self.assertEqual(self.cto_agent()["liveness"], "healthy")
        self.assertEqual([e for e in self.state()["events"] if e["kind"] in {"agent_stalled", "agent_automatic_recovery_routed"}][1:], [])
        self.assertEqual([a for a in self_heal.plan(self.root) if a.get("agent_id") == self.cto["id"]], [])

    def test_a_wake_with_no_board_write_at_all_is_stalled_and_three_in_a_row_are_not_responding(self):
        self.settle()
        for expected in (1, 2, 3):
            with board.locked_state(self.root) as state:
                board._event(state, "git_acceptance_failed", state["agents"][self.delivery["id"]], {"task": f"APP-{expected}", "message": "refused"})
            self.assertEqual(len(self.tick()), 1)
            self.shift(board.AUTO_RECOVERY_GRACE_SECONDS + 5)
            board.mark_stalled(self.root)
            self.assertEqual(self.cto_agent().get("consecutive_stalls"), expected)
            self.shift(board.AUTO_RECOVERY_RETRY_SECONDS + 5)
        self.assertEqual(self.cto_agent()["recovery_state"], "unresponsive")
        self.assertEqual(self.tick(), [], "paused until it writes to the board again")
        board.status(self.root, self.cto["id"], "back", "working")
        self.assertEqual(self.cto_agent()["recovery_state"], "resumed")

    def test_a_dead_terminal_is_still_recovered(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
        self.addCleanup(lambda: (process.kill() if process.poll() is None else None, process.wait(timeout=5)))
        control.attach(self.root, self.cto_session["id"], process.pid)
        control.note_cli_exec(self.root, self.cto_session["id"])
        process.kill(); process.wait(timeout=5)
        board.mark_stalled(self.root)          # the watchdog sees the dead terminal at once, not after any quiet period
        self.assertEqual(self.cto_agent()["liveness"], "offline")
        self.assertTrue([e for e in self.state()["events"] if e["kind"] == "agent_offline"])
        actions = [a for a in self_heal.plan(self.root) if a.get("session_id") == self.cto_session["id"]]
        self.assertEqual([a["action"] for a in actions], ["relaunch_ended"])


class CountersRecordEveryWake(_Fixture):
    def test_every_wake_is_counted_once_by_reason_and_the_clock_is_gone(self):
        self.settle()                                                      # launch -> resume
        board.record_finding(self.root, "APP", "Total is wrong", "The page adds tax twice.", affects_current_task=True)
        self.tick(); self.answer()                                         # finding
        self.shift(1900); self.tick()                                      # heartbeat (names the finding too)
        found = self.state()["cto_wakes"]
        self.assertEqual(found["by_reason"], {"resume": 1, "finding": 1, "heartbeat": 1})
        self.assertEqual(found["total"], 3)
        self.assertNotIn("clock", found["by_reason"])
        self.assertGreaterEqual(found["polls"], 2)
        self.assertFalse(hasattr(board, "_routine_cycle_due"), "no wake is routed because of poll age any more")


if __name__ == "__main__":
    unittest.main()

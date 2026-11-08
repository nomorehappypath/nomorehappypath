# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Stuck tasks reach the owner as cards, not as silence (task 2026-11-08_fewer-interventions, one test per requirement).

The week of 2026-11-02 on the owner's clone project: a part abandoned after four failed reviews with no card and no email, a
closed part nobody could reopen, the board refusing an agent's command three times to nobody, a task nobody worked on for an
hour, a 35-minute suite counted as failed by a 300-second limit, a pasted terminal screen taken as a direction."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, certified_execution, execution_identity, global_settings, project_manager_page, review_rounds, story, warning_email
from tests import environment_support

TASK = "finish-support"
GOOD = {"to": "owner@example.com", "host": "smtp.example.com", "port": 587, "user": "owner@example.com"}
PASTE = """Last login: Sat Nov  7 17:48:45 on ttys025
exec /bin/sh -c 'env "SCREENDIR=$1" /usr/bin/screen -c /dev/null -r "$2"; code=$?; exec "$3" "$4"' nmhp-view /Users/x/.harness-home/projects/p/data/control/terminal-sockets
owner@owners-mac ~ % exec /bin/sh -c 'env "SCREENDIR=$1" /usr/bin/screen -c /dev/null -r "$2"'
/Users/x/Projects/harness_next/harness/platform_support/agent_window.py"""


def _entry(status: str = "could_not_finish", **extra) -> dict:
    return {"task": TASK, "key": f"{TASK}|final_acceptance|||1", "scope": {"phase": "final_acceptance", "subtask": "", "chunk": "", "structure_revision": 1},
            "status": status, "failed_judgments": 4, "open_ids": ["B2", "B6"], "request_ids": ["r1", "r2", "r3", "r4"], "recovery_attempts": 1,
            "opened_at": "2026-11-08T00:37:30+00:00", "outcome": review_rounds.outcome_text({"failed_judgments": 4, "open_ids": ["B2", "B6"]}), **extra}


class BoardCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR")
        self.builder = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION)
        with board.locked_state(self.root, operation="test") as state:
            state["agents"][self.builder["id"]].update({"task": TASK, "active": True})   # on the task, as after begin-task
        self.sent: list = []

    def state(self):
        return board.snapshot(self.root)

    def open_cards(self):
        return [c for c in self.state().get("owner_actions", {}).values() if c["status"] == "open"]

    def emails(self):
        def fake(settings, password, subject, body):
            self.sent.append((subject, body))
            return {"sent": True, "error": ""}
        with mock.patch.object(warning_email, "send_message", fake):
            return warning_email.notify_blocked_cards(self.root, self.home, "Clone", "http://127.0.0.1:8740/")

    def put_entry(self, **kw):
        with board.locked_state(self.root, operation="test") as state:
            entry = _entry(**kw)
            state.setdefault("review_escalations", {})[entry["key"]] = entry
            return entry["key"]


class R1StoppedPartGetsACardAndAnEmail(BoardCase):
    def test_the_transition_and_the_sweep_make_one_card_and_one_email(self):
        global_settings.save_warning_email(self.home, GOOD, "secret-app-password")
        key = self.put_entry(status="recovery", phase="started")
        board.review_recovery_settle(self.root, key, "the fresh attempt's terminal ended")
        cards = self.open_cards()
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertTrue(card["blocked"])
        self.assertEqual(card["choices"], ["Try once more", "Give up"])
        self.assertIn("stopped after repeated failed reviews", card["title"])
        self.assertIn("Could not finish", card["why"])
        self.assertEqual(len(self.emails()), 1)
        self.assertIn(TASK, self.sent[0][1])
        # the sweep after a restart or resume finds the same card: no duplicate, no second email
        board.harness_cards_pass(self.root)
        self.assertEqual(len(self.open_cards()), 1)
        self.assertEqual(len(self.emails()), 0)
        # a part stopped before this release (already could_not_finish, no card) gets its card from the sweep alone
        with board.locked_state(self.root, operation="test") as state:
            other = _entry(); other["key"] = f"{TASK}|chunk|s1|c2|1"; other["scope"] = {"phase": "chunk", "subtask": "s1", "chunk": "c2", "structure_revision": 1}
            state["review_escalations"][other["key"]] = other
        board.harness_cards_pass(self.root)
        self.assertEqual(len(self.open_cards()), 2, "two stopped parts are two cards")
        exc = story.exception_for(self.state(), TASK)
        self.assertEqual(exc["kind"], "repairs_exhausted")
        self.assertTrue(exc["needs_owner"])
        self.assertIn("Try once more", exc["action"])
        # giving up one part never hides the other part's red card (plan check r2-4)
        first = next(c for c in self.open_cards() if c["scope_key"] == key)
        board.owner_action_choose(self.root, first["id"], "Give up")
        exc = story.exception_for(self.state(), TASK)
        self.assertEqual((exc["kind"], exc["needs_owner"]), ("repairs_exhausted", True))


class R2TryOnceMoreAndGiveUp(BoardCase):
    def test_try_once_more_grants_exactly_one_round_then_the_card_returns(self):
        key = self.put_entry()
        board.harness_cards_pass(self.root)
        card = self.open_cards()[0]
        with board.locked_state(self.root, operation="test") as state:
            state["agents"][self.builder["id"]].update({"active": False, "status": "superseded"})   # the usual case: the fresh attempt's terminal ended
        result = board.owner_action_choose(self.root, card["id"], "Try once more")
        self.assertIn("Try once more", result["message"])
        self.assertIn("start a new builder", result["message"], "no live builder on the task: the owner is told")
        gone = self.state()["agents"][self.builder["id"]]
        self.assertTrue(gone.get("stopped_by_harness_at"), "the kept-task restart selector needs the stop marker (plan check r2-1)")
        entry = self.state()["review_escalations"][key]
        self.assertEqual(review_rounds.refusal_for(self.state()["review_escalations"], {"task": TASK, "phase": "final_acceptance", "subtask": "", "chunk": "", "structure_revision": 1}), "")
        self.assertTrue(entry["owner_round"])
        self.assertEqual(self.state()["owner_actions"][card["id"]]["status"], "done")
        # the one granted round fails: straight back to could_not_finish, whatever the automatic budgets say
        request = {"task": TASK, "phase": "final_acceptance", "subtask": "", "chunk": "", "structure_revision": 1, "stage": review_rounds.INDEPENDENT, "id": "r5"}
        escalations = {key: dict(entry, recovery_attempts=0)}
        events = review_rounds.record_judgment(escalations, request, {"effective_result": "failed", "blockers": [{"id": "B2"}]}, "TASK DONE: NO", {}, "2026-11-08T05:00:00+00:00")
        self.assertEqual([e["kind"] for e in events], ["review_could_not_finish"])
        self.assertEqual(escalations[key]["status"], "could_not_finish")
        self.assertNotIn("owner_round", escalations[key])
        # a PASS on the granted round closes the part like any pass
        escalations = {key: dict(entry)}
        review_rounds.record_judgment(escalations, request, {"effective_result": "passed", "blockers": []}, "TASK DONE: YES", {}, "2026-11-08T05:00:00+00:00")
        self.assertEqual(escalations[key]["status"], "closed")

    def test_give_up_closes_the_part_for_good(self):
        key = self.put_entry()
        board.harness_cards_pass(self.root)
        card = self.open_cards()[0]
        board.owner_action_choose(self.root, card["id"], "Give up")
        escalations = self.state()["review_escalations"]
        self.assertEqual(escalations[key]["status"], "could_not_finish", "every consumer still sees a stopped part")
        self.assertIn("gave this part up", review_rounds.refusal_for(escalations, {"task": TASK, "phase": "final_acceptance", "subtask": "", "chunk": "", "structure_revision": 1}))
        board.harness_cards_pass(self.root)
        self.assertEqual(self.open_cards(), [], "no new card for a part the owner gave up")
        self.assertEqual(story.exception_for(self.state(), TASK)["kind"], "given_up")
        with self.assertRaises(ValueError):
            board.owner_action_choose(self.root, card["id"], "Try once more")   # the card is closed
        self.assertEqual(board.clear_owner_action(self.root, self.cto["id"], card["id"], "cleared")["outcome"], "You chose: Give up", "a done card stays as the owner left it")

    def test_the_cto_cannot_clear_the_owners_card(self):
        self.put_entry()
        board.harness_cards_pass(self.root)
        card = self.open_cards()[0]
        with self.assertRaisesRegex(ValueError, "owner's"):
            board.clear_owner_action(self.root, self.cto["id"], card["id"], "owner action is not required")
        self.assertEqual(self.open_cards()[0]["id"], card["id"])


class R3RefusedThreeTimes(BoardCase):
    def test_the_third_refusal_is_a_card_and_a_recurrence_raises_it_again(self):
        board.record_refusal_defect(self.root, TASK, "check-deviation", "name the requirement number", 3, role="engineering")
        cards = self.open_cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("refused the builder's command 3 times", cards[0]["title"])
        self.assertIn("name the requirement number", cards[0]["why"])
        self.assertTrue(cards[0]["blocked"])
        board.clear_owner_action  # the CTO cannot clear it (R2 test); the owner's clarification path is the existing one
        with board.locked_state(self.root, operation="test") as state:
            board._resolve_owner_action(state, state["owner_actions"][cards[0]["id"]], "Resolved: task accepted", "test")
        board.record_refusal_defect(self.root, TASK, "check-deviation", "name the requirement number", 3, role="engineering")
        self.assertEqual(len(self.open_cards()), 1, "a later recurrence of the same defect raises a new card")

    def test_a_second_series_of_refusals_through_the_detector_is_recorded_again(self):
        from types import SimpleNamespace
        from harness import board_surface
        gateway = board_surface.CommandGateway.__new__(board_surface.CommandGateway)   # only the detector, no control-plane setup
        gateway.context, gateway._refusals, gateway._refusal_holds = self.root, {}, set()
        with board.locked_state(self.root, operation="test") as state:
            state["agents"][self.builder["id"]]["session_id"] = "sess-builder"
        identity = SimpleNamespace(session_id="sess-builder")
        args = ["check-deviation", "--task", TASK]
        for _ in range(3):
            gateway._track_refusal(identity, "check-deviation", "name the requirement number", args)
        self.assertEqual(len(self.open_cards()), 1)
        gateway._track_refusal(identity, "check-deviation", "name the requirement number", args)   # a fourth in the same series: nothing new
        card_id = self.open_cards()[0]["id"]
        with board.locked_state(self.root, operation="test") as state:
            board._resolve_owner_action(state, state["owner_actions"][card_id], "Resolved: task accepted", "test")
        gateway._refusals.clear()           # the 45-minute window has passed
        for _ in range(3):
            gateway._track_refusal(identity, "check-deviation", "name the requirement number", args)
        self.assertEqual(len(self.open_cards()), 1, "a new series after the window is a new card (plan check r2-2)")

    def test_the_requirement_number_is_read_as_the_agent_writes_it(self):
        self.assertEqual(board._requirement_number("6"), 6)
        self.assertEqual(board._requirement_number("R6"), 6)
        self.assertEqual(board._requirement_number("r6 "), 6)
        self.assertEqual(board._requirement_number(6), 6)
        self.assertEqual(board._requirement_number("", "6.1"), 6)
        self.assertEqual(board._requirement_number("R6", "6.1"), 6)
        with self.assertRaisesRegex(ValueError, "requirement number"):
            board._requirement_number("R7", "6.1")   # conflicting: refused, never silently chosen
        with self.assertRaisesRegex(ValueError, "requirement number"):
            board._requirement_number("the sixth")


class R4NobodyWorking(BoardCase):
    def gone(self, minutes_ago: int, status: str = "superseded", note: str = "visible CLI terminal ended"):
        with board.locked_state(self.root, operation="test") as state:
            agent = state["agents"][self.builder["id"]]
            agent.update({"active": False, "status": status, "liveness": "offline", "liveness_note": note,
                          "last_poll_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()})

    def test_ten_minutes_with_nobody_makes_one_card_cleared_only_by_a_builder(self):
        global_settings.save_warning_email(self.home, GOOD, "secret-app-password")
        self.gone(minutes_ago=4)
        self.assertEqual(board.harness_cards_pass(self.root), [], "four minutes is not abandonment")
        self.gone(minutes_ago=11)
        cards = board.harness_cards_pass(self.root)
        self.assertEqual(len(cards), 1)
        self.assertIn(f"nobody is working on {TASK}", cards[0]["title"])
        self.assertIn("visible CLI terminal ended", cards[0]["why"])
        self.assertTrue(self.state()["agents"][self.builder["id"]].get("stopped_by_harness_at"), "Start a new builder must pick this task up (task review r1-2)")
        self.assertEqual(len(self.emails()), 1)
        exc = story.exception_for(self.state(), TASK)
        self.assertEqual(exc["kind"], "nobody_working")
        self.assertTrue(exc["needs_owner"])
        # the CTO's note changes nothing
        with board.locked_state(self.root, operation="test") as state:
            board._event(state, "cto_disposition_recorded", state["agents"][self.cto["id"]], {"task": TASK, "message": "owner action is not required"})
        self.assertEqual(len(self.open_cards()), 1)
        with self.assertRaisesRegex(ValueError, "owner's"):
            board.clear_owner_action(self.root, self.cto["id"], cards[0]["id"], "not required")
        # a builder back on the task clears it
        with board.locked_state(self.root, operation="test") as state:
            board._event(state, "board_polled", state["agents"][self.builder["id"]], {"task": TASK, "message": ""})
        self.assertEqual(self.open_cards(), [])
        self.assertEqual(self.state()["owner_actions"][cards[0]["id"]]["outcome"], board.IDLE_TASK_RESOLVED)

    def test_no_card_when_the_owner_stopped_the_builder_or_the_project_is_paused_or_just_resumed(self):
        self.gone(minutes_ago=30, status="stopped", note="terminal stopped by owner")
        self.assertEqual(board.harness_cards_pass(self.root), [])
        self.gone(minutes_ago=30)
        with board.locked_state(self.root, operation="test") as state:
            state["project_pause"] = {"status": "active", "resumed_at": datetime.now(timezone.utc).isoformat()}
        self.assertEqual(board.harness_cards_pass(self.root), [], "a grace period after Resume")
        with board.locked_state(self.root, operation="test") as state:
            state["project_pause"] = {"status": "paused"}
        self.assertEqual(board.harness_cards_pass(self.root), [], "nothing while paused")


class R5ACheckThatCouldNotRunIsNotAFailure(unittest.TestCase):
    def setUp(self):
        environment_support.require_process_table()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate = execution_identity.candidate_evidence_identity("c" * 40, "t" * 40, "contract", {"ledger": "l" * 64})

    def run_check(self, command: str, **kw):
        return certified_execution.run(self.root, self.root, command, candidate=self.candidate, environment_sha256="e" * 64, environment=os.environ,
                                       lockfile_digests={}, role="reviewer", gate="contract-check:6.1", unable_not_failure=True, reuse=False, **kw)

    def test_timeout_and_missing_program_are_did_not_run_but_a_failure_stays_a_failure(self):
        with self.assertRaises(certified_execution.CouldNotRun) as unable:
            self.run_check("python3 -c \"import time; time.sleep(5)\"", timeout_seconds=1)
        self.assertIn("did not finish within 1 seconds", str(unable.exception))
        with self.assertRaises(certified_execution.CouldNotRun) as unable:
            self.run_check("sh -c 'exit 127'")
        self.assertIn("not found", str(unable.exception))
        with self.assertRaises(ValueError) as failed:
            self.run_check("python3 -c \"print('Ran 2 tests in 0.001s'); print('FAILED (failures=1)'); raise SystemExit(1)\"")
        self.assertNotIsInstance(failed.exception, certified_execution.CouldNotRun)
        # a suite that printed failures and then hung is a failure, not "did not run"
        with self.assertRaises(ValueError) as hung:
            self.run_check("python3 -c \"import time; print('FAILED (failures=1)', flush=True); time.sleep(5)\"", timeout_seconds=1)
        self.assertNotIsInstance(hung.exception, certified_execution.CouldNotRun)
        with self.assertRaises(ValueError) as verbose:
            self.run_check("python3 -c \"import time; print('test_01_wrong_result (tests.test_x.T) ... FAIL', flush=True); time.sleep(5)\"", timeout_seconds=1)
        self.assertNotIsInstance(verbose.exception, certified_execution.CouldNotRun, "a verbose unittest FAIL before a hang is a failure (task review r1-1)")
        with self.assertRaises(ValueError) as go:
            self.run_check("python3 -c \"import time; print('--- FAIL: TestOwnerIsolation (0.00s)', flush=True); time.sleep(5)\"", timeout_seconds=1)
        self.assertNotIsInstance(go.exception, certified_execution.CouldNotRun, "Go's FAIL before a hang is a failure (plan check r2-3)")
        with self.assertRaises(ValueError) as final:
            self.run_check("sh -c \"echo 'FINAL CHECK: FAIL owner isolation'; exit 127\"")
        self.assertNotIsInstance(final.exception, certified_execution.CouldNotRun, "a final-check verdict before exit 127 is a failure")
        real = certified_execution._run_observed

        def with_prompt(*args, **kwargs):
            code, out, audit = real(*args, **kwargs)
            audit["problems"].append("a Keychain or permission prompt appeared during the certified command")
            return code, out, audit
        with mock.patch.object(certified_execution, "_run_observed", with_prompt):
            with self.assertRaises(ValueError) as prompted:
                self.run_check("python3 -c \"import time; time.sleep(5)\"", timeout_seconds=1)
        self.assertNotIsInstance(prompted.exception, certified_execution.CouldNotRun, "a prompt during the run is a finding, never did-not-run")
        # a Delivery test command (not the board's own check run) that times out is still a failure
        with self.assertRaises(ValueError) as delivery:
            certified_execution.run(self.root, self.root, "python3 -c \"import time; time.sleep(5)\"", candidate=self.candidate, environment_sha256="e" * 64,
                                    environment=os.environ, lockfile_digests={}, role="delivery", gate="internal-qa", timeout_seconds=1, reuse=False)
        self.assertNotIsInstance(delivery.exception, certified_execution.CouldNotRun)


class R6APastedScreenIsNotADirection(BoardCase):
    def test_board_and_composer_refuse_the_paste_and_accept_a_direction_with_a_path(self):
        self.assertTrue(board.looks_like_pasted_screen(PASTE))
        self.assertFalse(board.looks_like_pasted_screen("Fix the View app button; the code is in /Users/x/app and the tests in tests/."))
        self.assertFalse(board.looks_like_pasted_screen("Fix the application in this folder:\n/Users/x/app"))
        self.assertFalse(board.looks_like_pasted_screen("Add a Support chapter to the README.\n\nKeep it short, ten steps, one picture."))
        self.assertTrue(board.looks_like_pasted_screen("alice@ubuntu:~/app$ pytest\nalice@ubuntu:~/app$ npm test"), "Linux prompts (plan check r2-5)")
        with self.assertRaisesRegex(ValueError, "pasted terminal screen"):
            board.record_owner_message(self.root, self.builder["id"], PASTE, "direction")
        with self.assertRaisesRegex(ValueError, "pasted terminal screen"):
            board.record_owner_message(self.root, self.builder["id"], PASTE, "clarification")
        environment_support.require_loopback()
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="clone", manager_url="http://127.0.0.1:1/", settings_home=self.home, project_id="clone", chat_action_token="t"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def post(path, body):
            request = Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as error:
                return error.code, json.loads(error.read())

        status, data = post("/api/direction-check", {"text": PASTE})
        self.assertEqual(status, 400)
        self.assertIn("pasted terminal screen", data["error"])
        self.assertEqual(post("/api/direction-check", {"text": "Fix the View app button."})[0], 200)
        self.assertEqual(len(board.snapshot(self.root).get("agents", {})), 2, "no agent was started by the check")
        # the owner's choice on a card goes through the same server
        self.put_entry()
        board.harness_cards_pass(self.root)
        card = self.open_cards()[0]
        status, data = post(f"/api/owner-actions/{card['id']}/choose", {"choice": "Later"})
        self.assertEqual(status, 400)
        status, data = post(f"/api/owner-actions/{card['id']}/choose", {"choice": "Give up"})
        self.assertEqual(status, 200)
        self.assertEqual(data["card"]["status"], "done")
        status, data = post(f"/api/owner-actions/{card['id']}/choose", {"choice": "Give up"})
        self.assertEqual(status, 400, "a stale second click is refused, not repeated")
        html = urlopen(base + "/", timeout=10).read().decode()
        self.assertIn("chooseOwnerAction", html)


class R7HelpSaysSo(unittest.TestCase):
    def test_the_blocked_row_names_the_three_new_reasons(self):
        page = project_manager_page.PAGE
        row = page[page.index('A red card says "Blocked, needs you"'):]
        row = row[:row.index("</tr>")]
        for words in ("Try once more", "Give up", "refused a builder's command three times", "nobody has worked on a task for ten minutes"):
            self.assertIn(words, row)


if __name__ == "__main__":
    unittest.main()

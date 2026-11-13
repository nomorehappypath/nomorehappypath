# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The CTO closes the board's own red cards (owner 2026-11-12, D-018: "the CTO has absolute power on board and every agent").

Seen on the owner's clone project: the board's "refused the builder's command 3 times" card stayed after the builder fixed the step,
and the CTO was refused when it tried to close it. Closing decides nothing: no choice is selected and nothing behind the card changes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from harness import board, board_surface, global_settings
from tests.test_fewer_interventions import BoardCase, GOOD, TASK


class CtoClosesTheBoardsCards(BoardCase):
    def stale(self, minutes: int = 11) -> str:
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        with board.locked_state(self.root, operation="test") as state:
            state["agents"][self.builder["id"]].update({"active": False, "status": "superseded", "liveness": "offline",
                                                        "liveness_note": "terminal ended", "last_poll_at": stamp, "last_status_at": stamp})
        return stamp

    def alive(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with board.locked_state(self.root, operation="test") as state:
            state["agents"][self.builder["id"]].update({"active": True, "status": "working", "last_poll_at": now, "last_status_at": now})

    def test_the_cto_closes_a_stopped_part_card_and_the_part_stays_stopped(self):
        key = self.put_entry()
        board.harness_cards_pass(self.root)
        card = self.open_cards()[0]
        self.assertTrue(card.get("choices"), "a stopped part's card carries the owner's choices")
        before = {name: self.state().get(name) for name in ("qa_requests", "release_decisions", "owner_clarifications")}
        done = board.clear_owner_action(self.root, self.cto["id"], card["id"], "routed a repair to the builder")
        self.assertEqual((done["status"], done["outcome"], done["resolved_by"]), ("done", "routed a repair to the builder", "cto"))
        self.assertEqual(self.open_cards(), [])
        state = self.state()
        self.assertEqual(state["review_escalations"][key]["status"], "could_not_finish", "no choice was selected")
        self.assertNotIn("given_up_at", state["review_escalations"][key])
        self.assertEqual({name: state.get(name) for name in before}, before, "reviews, decisions and clarifications are untouched")
        self.assertTrue(any(e.get("kind") == "owner_action_cleared" and "routed a repair" in str(e.get("message")) for e in state["events"]))

    def test_only_the_active_cto_may_close_a_card(self):
        self.put_entry()
        board.harness_cards_pass(self.root)
        card = self.open_cards()[0]
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE")
        for who in (self.builder["id"], reviewer["id"]):
            with self.assertRaises(ValueError):
                board.clear_owner_action(self.root, who, card["id"], "not required")
        with board.locked_state(self.root, operation="test") as state:
            state["agents"][self.cto["id"]]["active"] = False
        with self.assertRaises(ValueError):
            board.clear_owner_action(self.root, self.cto["id"], card["id"], "not required")
        self.assertEqual(self.open_cards()[0]["id"], card["id"])

    def test_a_closed_card_is_not_raised_again_for_the_same_incident_and_is_when_it_ends_and_returns(self):
        global_settings.save_warning_email(self.home, GOOD, "secret-app-password")
        key = self.put_entry()
        board.harness_cards_pass(self.root)
        self.emails()
        self.assertEqual(len(self.sent), 1)
        board.clear_owner_action(self.root, self.cto["id"], self.open_cards()[0]["id"], "routed a repair to the builder")
        for _ in range(2):
            board.harness_cards_pass(self.root)
        self.assertEqual(self.open_cards(), [], "the same incident, still true: no new card")
        self.emails()
        self.assertEqual(len(self.sent), 1, "and no new email")
        with board.locked_state(self.root, operation="test") as state:
            state["review_escalations"][key]["status"] = "closed"            # the condition ends
        board.harness_cards_pass(self.root)
        self.assertEqual(self.state().get("cto_acknowledged"), {}, "the acknowledgement ended with the condition")
        with board.locked_state(self.root, operation="test") as state:
            state["review_escalations"][key]["status"] = "could_not_finish"  # and comes back with the very same signature
        board.harness_cards_pass(self.root)
        self.assertEqual(len(self.open_cards()), 1, "a returned incident is a new card")
        self.emails()
        self.assertEqual(len(self.sent), 2, "and a new email")

    def test_the_nobody_working_card_follows_the_same_rule(self):
        stamp = self.stale()
        board.harness_cards_pass(self.root)
        card = self.open_cards()[0]
        board.clear_owner_action(self.root, self.cto["id"], card["id"], "started a new builder")
        board.harness_cards_pass(self.root)
        self.assertEqual(self.open_cards(), [])
        self.alive()                                    # the condition ends: a builder is working
        board.harness_cards_pass(self.root)
        self.assertEqual(self.state().get("cto_acknowledged"), {})
        with board.locked_state(self.root, operation="test") as state:   # and returns with the same builder and the same stamp
            state["agents"][self.builder["id"]].update({"active": False, "status": "superseded", "liveness": "offline",
                                                        "liveness_note": "terminal ended", "last_poll_at": stamp, "last_status_at": stamp})
        board.harness_cards_pass(self.root)
        self.assertEqual(len(self.open_cards()), 1)

    def forget_incident(self):
        """Make every stored card look as it did before this rule: no incident."""
        with board.locked_state(self.root, operation="test") as state:
            for card in state["owner_actions"].values():
                card.pop("incident", None)

    def test_a_card_raised_before_this_rule_stays_closed_whether_the_cto_closes_it_before_or_after_the_first_sweep(self):
        for sweep_first in (False, True):
            with self.subTest(sweep_first=sweep_first):
                key = self.put_entry(**({} if not sweep_first else {"key": f"{TASK}|final_acceptance|||2"}))
                board.harness_cards_pass(self.root)
                self.forget_incident()
                if sweep_first:
                    board.harness_cards_pass(self.root)         # the sweep gives the stored card its incident
                card = self.open_cards()[0]
                board.clear_owner_action(self.root, self.cto["id"], card["id"], "routed a repair to the builder")
                for _ in range(2):
                    board.harness_cards_pass(self.root)
                self.assertEqual(self.open_cards(), [], "closed once, closed for the same incident")
                self.assertEqual(self.state()["review_escalations"][key]["status"], "could_not_finish")
                with board.locked_state(self.root, operation="test") as state:
                    state["review_escalations"].clear()
                    state["owner_actions"].clear()
                    state["cto_acknowledged"] = {}

    def test_an_idle_card_raised_before_this_rule_stays_closed_too(self):
        for sweep_first in (False, True):
            with self.subTest(sweep_first=sweep_first):
                self.stale()
                board.harness_cards_pass(self.root)
                self.forget_incident()
                if sweep_first:
                    board.harness_cards_pass(self.root)
                card = self.open_cards()[0]
                board.clear_owner_action(self.root, self.cto["id"], card["id"], "started a new builder")
                for _ in range(2):
                    board.harness_cards_pass(self.root)
                self.assertEqual(self.open_cards(), [])
                with board.locked_state(self.root, operation="test") as state:
                    state["owner_actions"].clear()
                    state["cto_acknowledged"] = {}

    def test_the_cto_still_cannot_judge_its_own_work(self):
        # unchanged by this task: pass or fail a review, change evidence, accept for the owner (see test_cto_blockers_and_fidelity.py
        # and test_board_surface.py for the full matrix); here only the role gate the new rule must not widen
        reviewer_only = ("review-verdict", "submit-review", "owner-accept")
        authorized = board_surface.__dict__.get("CTO_OPERATIONS", None)
        if authorized is not None:
            for operation in reviewer_only:
                self.assertNotIn(operation, authorized)


class RefusedCardClosesOnALaterSuccess(BoardCase):
    ARGS = ["check-deviation", "--task", TASK]

    def gateway(self):
        gateway = board_surface.CommandGateway.__new__(board_surface.CommandGateway)
        gateway.context, gateway._refusals, gateway._refusal_holds, gateway._pending_refusal_cards = self.root, {}, set(), None
        with board.locked_state(self.root, operation="test") as state:
            state["agents"][self.builder["id"]]["session_id"] = "sess-builder"
        return gateway, SimpleNamespace(session_id="sess-builder")

    def refuse(self, gateway, identity, times=3, operation="check-deviation", args=None):
        for _ in range(times):
            gateway._track_refusal(identity, operation, "name the requirement number", args or self.ARGS)

    def test_a_later_success_closes_the_card_and_three_new_refusals_raise_a_new_one(self):
        gateway, identity = self.gateway()
        self.refuse(gateway, identity)
        card = self.open_cards()[0]
        gateway._clear_refusal_card(identity, "another-operation", self.ARGS)
        gateway._clear_refusal_card(identity, "check-deviation", ["check-deviation", "--task", "another-task"])
        self.assertEqual(len(self.open_cards()), 1, "a success on another operation or another task closes nothing")
        gateway._clear_refusal_card(identity, "check-deviation", self.ARGS)
        done = self.state()["owner_actions"][card["id"]]
        self.assertEqual((done["status"], done["outcome"]), ("done", "Resolved: the same step later succeeded"))
        self.assertEqual(gateway._refusals, {}, "the refusal series starts again")
        self.refuse(gateway, identity)
        self.assertEqual(len(self.open_cards()), 1, "three new refusals raise a new card")

    def test_a_card_raised_before_a_worker_restart_is_closed_too(self):
        first, identity = self.gateway()
        self.refuse(first, identity)
        second, _ = self.gateway()                      # a new worker: empty counters, the card is only in the stored board
        second._clear_refusal_card(identity, "check-deviation", self.ARGS)
        self.assertEqual(self.open_cards(), [])

    def test_a_board_that_cannot_be_written_never_fails_the_command_and_the_next_success_closes_the_card(self):
        gateway, identity = self.gateway()
        self.refuse(gateway, identity)
        real = board.close_refusal_card
        calls = []
        def paused(root, task, operation):
            calls.append(task)
            raise board.ProjectPausedError("paused")
        board.close_refusal_card = paused
        try:
            gateway._clear_refusal_card(identity, "check-deviation", self.ARGS)       # must not raise
        finally:
            board.close_refusal_card = real
        self.assertEqual((len(calls), len(self.open_cards())), (1, 1), "tried once, card still open")
        gateway._clear_refusal_card(identity, "check-deviation", self.ARGS)
        self.assertEqual(self.open_cards(), [])

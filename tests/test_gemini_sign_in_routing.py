# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Gemini needing the owner (spec T1-3, M-3, M-8): routing is held, self-heal waits, the owner is told in Gemini's words.

The same incident the Claude `/login` tests cover (test_sign_in_needed), with a Gemini Reviewer, whose sign-in
and trust questions show in its own window and carry no `/login` anywhere.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from harness import attention, board, control, self_heal
from tests import test_board as _board_tests

CLAUDE = {"provider": "claude", "model": "opus", "effort": "high"}
CODEX = {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"}
GEMINI = {"provider": "gemini", "model": "gemini-3.8-flash-low", "effort": "high"}
GEMINI_REVIEWS = {"delivery": CODEX, "cto": CLAUDE, "reviewer": GEMINI}


class _Fixture(unittest.TestCase):
    setUp = _board_tests.BoardTests.setUp
    tearDown = _board_tests.BoardTests.tearDown
    ledger = _board_tests.BoardTests.ledger
    qa_command = _board_tests.BoardTests.qa_command
    declare_chunks = _board_tests.BoardTests.declare_chunks
    delivery = _board_tests.BoardTests.delivery

    def gemini_reviewer(self):
        session = control.create(self.root, "claude_reviewer", settings_override=GEMINI_REVIEWS)
        agent = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Google", session_id=session["id"])
        control.take_instructions(self.root, session["id"])
        return session, agent

    def open_review(self):
        dev = self.delivery("TASK-GEMINI-SIGN-IN")
        self.declare_chunks(dev["id"], [("one", "one reviewable outcome")])
        return dev, board.request_review(self.root, dev["id"], self.ledger("g.md"), "review while Gemini is signed out",
                                         chunk="one", test_command=self.qa_command())


class GeminiRoutingTests(_Fixture):
    def test_a_gemini_reviewer_that_needs_signing_in_is_never_routed_and_the_owner_is_told_in_its_words(self):
        session, reviewer = self.gemini_reviewer()
        control.record_attention(self.root, session["id"], attention.GEMINI_SIGN_IN_REASON)
        dev = self.delivery("TASK-GEMINI-SIGN-IN")
        self.declare_chunks(dev["id"], [("one", "one reviewable outcome")])
        request = board.request_review(self.root, dev["id"], self.ledger("g.md"), "x", chunk="one", test_command=self.qa_command())
        for _ in range(3):
            board.route_open_reviews(self.root, retry_seconds=0)
        state = board.snapshot(self.root)
        self.assertNotEqual(state["qa_requests"][request["id"]].get("routed_to"), reviewer["id"])
        self.assertEqual(control.take_instructions(self.root, session["id"]), [], "nothing queued for a terminal that needs the owner")
        self.assertEqual(state["reviewer_needed"]["message"],
                         "The Reviewer needs you to sign in again: open its terminal and follow the sign-in prompt in its window.")
        self.assertNotIn("/login", state["reviewer_needed"]["message"])
        self.assertEqual(state["reviewer_needed"]["sign_in_how"], "follow the sign-in prompt in its window")

    def test_after_the_owner_signs_in_the_review_is_routed_to_gemini(self):
        session, reviewer = self.gemini_reviewer()
        control.record_attention(self.root, session["id"], attention.GEMINI_SIGN_IN_REASON)
        dev, request = self.open_review()
        board.route_open_reviews(self.root, retry_seconds=0)
        control.clear_attention(self.root, session["id"])
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["routed_to"], reviewer["id"])

    def test_a_gemini_reviewer_takes_a_cross_vendor_review_but_never_one_from_a_google_developer(self):
        _session, reviewer = self.gemini_reviewer()
        dev, request = self.open_review()                      # developer vendor: OpenAI
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["routed_to"], reviewer["id"])

    def test_the_two_google_agents_are_never_paired_for_independent_review(self):
        _session, reviewer = self.gemini_reviewer()
        google_dev = self.delivery("TASK-GOOGLE-DEV", vendor="Google")
        self.declare_chunks(google_dev["id"], [("one", "one reviewable outcome")])
        request = board.request_review(self.root, google_dev["id"], self.ledger("g2.md"), "same vendor", chunk="one", test_command=self.qa_command())
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertNotEqual(board.snapshot(self.root)["qa_requests"][request["id"]].get("routed_to"), reviewer["id"])

    def test_a_gemini_session_the_owner_switched_to_another_makers_model_takes_no_review(self):
        session, reviewer = self.gemini_reviewer()
        self.assertTrue(control.record_observed_model(self.root, session["id"], "claude-opus-4-6-thinking"))
        _dev, request = self.open_review()
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertNotEqual(board.snapshot(self.root)["qa_requests"][request["id"]].get("routed_to"), reviewer["id"])
        control.record_observed_model(self.root, session["id"], "gemini-3.8-flash-low")
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["routed_to"], reviewer["id"])


QUOTA_LINE = ("⚠ Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 165h0m46s.\n"
              "Error ID: 24f0e43d-21d6-46d1-b30e-06ac1999abb3-27")


class GeminiQuotaTests(_Fixture):
    """The owner's free Google quota ran out during testing (2026-10-07) and the app said nothing: now it does."""

    def test_the_clis_own_quota_line_is_recognised_with_a_reason_that_is_stable_for_the_hour(self):
        reason = attention.detect(QUOTA_LINE)
        self.assertEqual(reason, "Gemini ran out of quota for about 165 more hours (open Settings and choose another provider for this role, or upgrade your Google plan)")
        self.assertEqual(attention.detect(QUOTA_LINE.replace("165h0m46s", "165h0m12s")), reason, "the countdown does not churn the banner")
        self.assertTrue(attention.holds_harness_input(reason), "nothing is typed into a terminal that cannot work")
        self.assertTrue(attention.needs_owner({"attention_reason": reason}))
        self.assertFalse(attention.needs_sign_in({"attention_reason": reason}), "it is not a sign-in problem")
        self.assertEqual(attention.owner_block({"attention_reason": reason}), "quota")
        wrapped = QUOTA_LINE.replace("increase your limits. Resets", "increase your\nlimits. Resets")
        self.assertEqual(attention.detect(wrapped), reason, "a narrow window wraps the line")
        prompt_box = "\n" + "─" * 40 + "\n>\n" + "─" * 40 + "\n? for shortcuts      Gemini 3.8 Flash · low"
        self.assertEqual(attention.detect(QUOTA_LINE + prompt_box), reason, "the line, then the prompt box: still the latest event")
        replayed = QUOTA_LINE + "\n" + "\n".join(f"● Ran command {n}" for n in range(12)) + prompt_box
        self.assertIsNone(attention.detect(replayed), "an old error replayed above work done since is not the latest event")
        self.assertIsNone(attention.detect("quota is a word an agent may print"))
        self.assertIsNone(attention.detect("The cache Resets in 5h when the job restarts"))

    def test_a_reviewer_out_of_quota_is_not_routed_and_the_owner_is_told_what_to_do(self):
        session, reviewer = self.gemini_reviewer()
        control.record_attention(self.root, session["id"], attention.detect(QUOTA_LINE))
        dev = self.delivery("TASK-GEMINI-QUOTA")
        self.declare_chunks(dev["id"], [("one", "one reviewable outcome")])
        request = board.request_review(self.root, dev["id"], self.ledger("q.md"), "x", chunk="one", test_command=self.qa_command())
        board.route_open_reviews(self.root, retry_seconds=0)
        state = board.snapshot(self.root)
        self.assertNotEqual(state["qa_requests"][request["id"]].get("routed_to"), reviewer["id"])
        self.assertEqual(control.take_instructions(self.root, session["id"]), [])
        self.assertEqual(state["reviewer_needed"]["message"],
                         "The Reviewer is out of Gemini quota (about 165 more hours): open Settings and choose another provider for it, "
                         "or upgrade your Google plan. Its work is paused, not lost.")
        self.assertFalse(state["reviewer_needed"]["sign_in"], "no sign-in banner for a quota problem")

    def test_the_agent_card_says_out_of_quota_not_sign_in(self):
        session, reviewer = self.gemini_reviewer()
        control.record_attention(self.root, session["id"], attention.detect(QUOTA_LINE))
        board.mark_stalled(self.root, stale_seconds=1)
        agent = board.snapshot(self.root)["agents"][reviewer["id"]]
        self.assertEqual(agent["liveness"], "needs_sign_in")
        self.assertIn("out of Gemini quota", agent["liveness_note"])
        self.assertNotIn("sign in", agent["liveness_note"])

    def test_after_the_quota_line_leaves_the_screen_everything_resumes(self):
        session, reviewer = self.gemini_reviewer()
        control.record_attention(self.root, session["id"], attention.detect(QUOTA_LINE))
        dev, request = self.open_review()
        board.route_open_reviews(self.root, retry_seconds=0)
        control.clear_attention(self.root, session["id"])
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["routed_to"], reviewer["id"])

    def test_self_heal_neither_restarts_nor_replaces_a_terminal_that_is_out_of_quota(self):
        control.update_agent_settings(self.root, GEMINI_REVIEWS)
        session = control.create(self.root, "claude_cto", settings_override={**GEMINI_REVIEWS, "cto": GEMINI})
        board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Google", session_id=session["id"])
        control.record_attention(self.root, session["id"], attention.detect(QUOTA_LINE))
        self.open_review()
        with board.locked_state(self.root) as state:
            state["reviewer_needed"] = {"requested_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
                                        "request_id": "r", "message": "no live eligible reviewer"}
        self.assertFalse(any(a.get("action") == "start_reviewer" for a in self_heal.plan(self.root)),
                         "a second Gemini reviewer would hit the same quota")


class GeminiSelfHealTests(_Fixture):
    """A provider's sign-in is shared by its terminals: no second Reviewer is started into the same sign-in wall."""

    def wait_for_a_reviewer(self):
        self.open_review()
        with board.locked_state(self.root) as state:
            state["reviewer_needed"] = {"requested_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
                                        "request_id": "r", "message": "no live eligible reviewer; start one from Mission Control"}

    def starts_a_reviewer(self):
        return any(action.get("action") == "start_reviewer" for action in self_heal.plan(self.root))

    def test_with_nobody_signed_out_self_heal_starts_the_missing_reviewer(self):
        control.update_agent_settings(self.root, GEMINI_REVIEWS)
        self.wait_for_a_reviewer()
        self.assertTrue(self.starts_a_reviewer())

    def test_a_signed_out_gemini_terminal_stops_self_heal_from_starting_a_gemini_reviewer_into_the_same_wall(self):
        control.update_agent_settings(self.root, GEMINI_REVIEWS)
        session = control.create(self.root, "claude_cto", settings_override={**GEMINI_REVIEWS, "cto": GEMINI})
        board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Google", session_id=session["id"])
        control.record_attention(self.root, session["id"], attention.GEMINI_SIGN_IN_REASON)
        self.wait_for_a_reviewer()
        self.assertFalse(self.starts_a_reviewer())

    def test_a_signed_out_gemini_terminal_does_not_stop_a_reviewer_of_another_provider(self):
        control.update_agent_settings(self.root, {"delivery": GEMINI, "cto": CLAUDE, "reviewer": CODEX})
        session = control.create(self.root, "codex_delivery", settings_override={"delivery": GEMINI, "cto": CLAUDE, "reviewer": CODEX})
        board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="Google", session_id=session["id"])
        control.record_attention(self.root, session["id"], attention.GEMINI_SIGN_IN_REASON)
        self.wait_for_a_reviewer()
        self.assertTrue(self.starts_a_reviewer(), "a Codex reviewer does not share Gemini's sign-in")

    def test_a_signed_out_gemini_cto_does_not_stop_a_codex_reviewer(self):
        # The CTO's role kind is `claude_cto` whatever runs it. Before, any `claude*` kind blocked a new Reviewer.
        settings = {"delivery": CLAUDE, "cto": GEMINI, "reviewer": CODEX}
        control.update_agent_settings(self.root, settings)
        session = control.create(self.root, "claude_cto", settings_override=settings)
        board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Google", session_id=session["id"])
        control.record_attention(self.root, session["id"], attention.GEMINI_SIGN_IN_REASON)
        self.wait_for_a_reviewer()
        self.assertTrue(self.starts_a_reviewer())

    def test_a_terminal_waiting_for_the_owner_is_not_restarted_by_self_heal(self):
        session, agent = self.gemini_reviewer()
        control.record_attention(self.root, session["id"], attention.GEMINI_SIGN_IN_REASON)
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]]["restart_requested_at"] = board.now()
        self.assertEqual([a for a in self_heal.plan(self.root) if a.get("session_id") == session["id"]], [])


def tearDownModule():
    from tests.environment_support import assert_agy_pinned
    assert_agy_pinned()


if __name__ == "__main__":
    unittest.main()

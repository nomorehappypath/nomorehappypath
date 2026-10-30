# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""2026-10-17 (clean-install finding): Claude Code's first-run "Bypass Permissions mode" warning (default "No, exit")
stalled agents silently and Mission Control said "waiting for the harness".

Now: the warning screen is recognised, nothing is typed into it, the owner is told in plain words that he is the one
being waited for, with the one command to run, the board says why no reviewer is eligible, a refused launch leaves a
card that clears by itself when his acceptance is on file, and the harness never accepts it for him.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import attention, board, board_viewer, claude_auth, control, self_heal
from tests.test_sign_in_needed import _Fixture

WARNING_SCREEN = """
 WARNING: Claude Code running in Bypass Permissions mode

 In Bypass Permissions mode, Claude Code will not ask for your approval before running potentially dangerous
 commands. This mode should only be used in a sandboxed container/VM that has restricted internet access and
 can easily be restored if damaged.

 By proceeding, you accept all responsibility for actions taken while running in Bypass Permissions mode.

 ❯ 1. No, exit
   2. Yes, I accept
"""


class DetectionTests(unittest.TestCase):
    def test_the_warning_screen_is_recognised_and_holds_the_harness_input(self):
        self.assertEqual(attention.detect(WARNING_SCREEN, "claude"), attention.BYPASS_CONSENT_REASON)
        self.assertTrue(attention.holds_harness_input(attention.BYPASS_CONSENT_REASON), "Enter would pick the default 'No, exit'")
        self.assertTrue(attention.menu_on_screen(WARNING_SCREEN))

    def test_the_session_is_waiting_for_the_owner_and_the_sentence_says_what_to_do(self):
        session = {"attention_reason": attention.BYPASS_CONSENT_REASON, "provider": "claude"}
        self.assertTrue(attention.needs_consent(session))
        self.assertTrue(attention.needs_owner(session))
        self.assertEqual(attention.owner_block(session), "consent")
        sentence = attention.owner_block_sentence("Reviewer", session)
        self.assertIn("waiting for YOU, not for the harness", sentence)
        self.assertIn("claude --dangerously-skip-permissions", sentence)
        self.assertIn("Yes, I accept", sentence)

    def test_text_that_merely_quotes_the_warning_is_not_the_menu(self):
        quoted = "The error said: 'WARNING: Claude Code running in Bypass Permissions mode' and I explained it to the owner."
        self.assertIsNone(attention.detect(quoted, "claude"))
        self.assertFalse(attention.menu_on_screen(quoted))

    def test_ordinary_screens_and_sign_in_are_not_mistaken_for_it(self):
        self.assertFalse(attention.needs_consent({"attention_reason": attention.LOGIN_REASON}))
        self.assertIsNone(attention.detect("Reading the file in default mode", "claude"))
        self.assertEqual(attention.owner_block({"attention_reason": attention.LOGIN_REASON}), "sign_in")

    def test_self_heal_leaves_a_terminal_at_the_consent_screen_alone(self):
        self.assertTrue(self_heal._needs_sign_in({"attention_reason": attention.BYPASS_CONSENT_REASON}))


class BoardTests(_Fixture):
    def at_the_warning(self, session_id):
        control.record_attention(self.root, session_id, attention.detect(WARNING_SCREEN, "claude"))

    def test_no_review_is_routed_to_a_reviewer_at_the_warning_and_the_board_says_who_is_waited_for(self):
        dev = self.delivery("TASK-CONSENT")
        self.declare_chunks(dev["id"], [("one", "one reviewable outcome")])
        session, reviewer = self.reviewer()
        self.at_the_warning(session["id"])
        request = board.request_review(self.root, dev["id"], self.ledger("consent.md"), "review while the reviewer waits for consent",
                                       chunk="one", test_command=self.qa_command())
        for _ in range(3):
            board.route_open_reviews(self.root, retry_seconds=0)
        state = board.snapshot(self.root)
        self.assertNotEqual(state["qa_requests"][request["id"]].get("routed_to"), reviewer["id"])
        self.assertEqual(control.take_instructions(self.root, session["id"]), [], "nothing is typed into the owner's consent screen")
        message = state["reviewer_needed"]["message"]
        self.assertIn("waiting for YOU", message)
        self.assertNotIn("no live eligible reviewer", message)

    def test_the_agent_is_shown_as_waiting_for_the_owner_and_nothing_is_woken(self):
        from datetime import datetime, timedelta, timezone
        session = control.create(self.root, "claude_cto")
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
        dev = self.delivery("WORK-CONSENT")
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"][cto["id"]].update({"last_poll_at": old, "spawned_at": old})
            state["agents"][dev["id"]].update({"last_poll_at": board.now()})
        self.at_the_warning(session["id"])
        with mock.patch("harness.control.enqueue_instruction", return_value={"id": "w", "source": "cto-wake"}) as enqueue:
            for _ in range(4):
                board.mark_stalled(self.root)
            enqueue.assert_not_called()
        agent = board.snapshot(self.root)["agents"][cto["id"]]
        self.assertEqual(agent["liveness"], board.SIGNED_OUT_LIVENESS)
        self.assertIn("waiting for YOU, not for the harness", agent["liveness_note"])
        self.assertNotIn("consecutive_stalls", agent)


class ReviewerMessageTests(_Fixture):
    def test_with_no_reviewer_registered_a_refused_consent_is_named_not_start_one(self):
        dev = self.delivery("TASK-REFUSED")
        self.declare_chunks(dev["id"], [("one", "one reviewable outcome")])
        session = control.create(self.root, "claude_reviewer")
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["claude_auth_action"] = claude_auth.action("consent", "run X")
        board.request_review(self.root, dev["id"], self.ledger("refused.md"), "review while consent is refused",
                             chunk="one", test_command=self.qa_command())
        board.route_open_reviews(self.root, retry_seconds=0)
        message = board.snapshot(self.root)["reviewer_needed"]["message"]
        self.assertIn("waiting for YOU, not for the harness", message)
        self.assertNotIn("start one from Mission Control", message)

    def test_self_heal_does_not_keep_starting_reviewers_while_consent_is_refused(self):
        session = control.create(self.root, "claude_reviewer")
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["claude_auth_action"] = claude_auth.action("consent", "run X")
        skipped = self_heal._skip_if_held_now(self.root, {"action": "start_reviewer", "kind": "claude_reviewer"})
        self.assertIn("waiting for YOU", skipped)


class ProviderScopeTests(_Fixture):
    """Review round 1: a Claude consent card on the CTO must not stop a Codex Reviewer from being started or recovered."""

    def refuse_claude_cto(self):
        session = control.create(self.root, "claude_cto")
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["claude_auth_action"] = claude_auth.action("consent", "run X")

    def set_reviewer_provider(self, provider):
        settings = {role: dict(entry) for role, entry in control.agent_settings(self.root).items()}
        settings["delivery"] = {"provider": "claude", "model": "haiku", "effort": "low"}
        settings["reviewer"] = ({"provider": "codex", "model": "gpt-5.6-sol", "effort": "low"} if provider == "codex"
                                else {"provider": "claude", "model": "opus", "effort": "max"})
        if provider == "claude":
            settings["delivery"] = {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"}
        with control.locked_state(self.root) as state:
            state["agent_settings"] = settings

    def test_a_codex_reviewer_is_still_started_while_a_claude_cto_waits_for_consent(self):
        self.refuse_claude_cto()
        self.set_reviewer_provider("codex")
        self.assertEqual(self_heal._skip_if_held_now(self.root, {"action": "start_reviewer", "kind": "codex_reviewer"}), "")

    def test_a_claude_reviewer_is_not_started_while_consent_is_refused(self):
        self.refuse_claude_cto()
        self.set_reviewer_provider("claude")
        self.assertIn("waiting for YOU", self_heal._skip_if_held_now(self.root, {"action": "start_reviewer", "kind": "claude_reviewer"}))


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        (self.home / ".claude").mkdir(parents=True)

    def test_a_consent_card_clears_itself_once_the_acceptance_is_on_file_and_not_before(self):
        from tests import test_sign_in_needed  # noqa: F401  (fixture style: a plain control project)
        root = Path(self.tmp.name) / "project"
        root.mkdir()
        session = control.create(root, "claude_reviewer")
        with control.locked_state(root) as state:
            state["sessions"][session["id"]]["claude_auth_action"] = claude_auth.action("consent", "do the thing")
        with mock.patch("pathlib.Path.home", return_value=self.home), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(claude_auth.settle_consent_cards(root), 0, "not accepted yet: the card stays")
            (self.home / ".claude" / "settings.json").write_text(json.dumps({"skipDangerousModePermissionPrompt": True}), encoding="utf-8")
            self.assertEqual(claude_auth.settle_consent_cards(root), 1)
        remaining = [s for s in control.snapshot(root)["sessions"] if s.get("claude_auth_action")]
        self.assertEqual(remaining, [])

    def test_the_card_names_the_owner_not_the_harness_and_other_cards_are_untouched(self):
        card = claude_auth.action("consent", "run X")
        self.assertEqual(card["reason"], "consent")
        self.assertIn("YOU", card["title"])
        self.assertNotIn("reason", claude_auth.action("rejected"))

    def test_the_harness_never_writes_the_acceptance_for_the_owner(self):
        source = Path(claude_auth.__file__).read_text(encoding="utf-8") + Path(board.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"skipDangerousModePermissionPrompt"] =', source)
        self.assertNotIn("skipDangerousModePermissionPrompt\": True", Path(attention.__file__).read_text(encoding="utf-8"))


class ViewerTests(unittest.TestCase):
    def test_mission_control_labels_it_waiting_for_you_with_the_command(self):
        page = Path(board_viewer.__file__).read_text(encoding="utf-8")
        self.assertIn("'WAITING FOR YOU'", page)
        self.assertIn("claude --dangerously-skip-permissions", page)
        self.assertLess(page.index("includes('Bypass Permissions')"), page.index("Claude needs a new setup-token"))


if __name__ == "__main__":
    unittest.main()

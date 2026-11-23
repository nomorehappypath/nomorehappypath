# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A quoted "not logged in" never marks a working agent as signed out (task 2026-11-21_sign-in-false-alarm).

Lease and contract analysis, 2026-11-21 02:19-02:24 UTC: the CTO checked the app's own Claude call, its terminal printed the
app's error "Not logged in · Please run /login", and the CTO quoted it in a route note to Delivery. The harness read the whole
screen, marked both agents signed out while both kept running board commands, stopped routing wake-ups to them, and told the
owner to sign in. Now the login form counts only near the bottom of the screen, and a board command the session started after
the sign-out reading clears it.

Run:  PYTHONPATH=. python3 -m unittest tests.test_sign_in_false_alarm -v
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import attention, board, control, interactive_supervisor
from harness.board_surface import PROTOCOL_VERSION, CommandGateway, SessionTokenAuthority, SurfaceAuthorizationError
from harness.project_context import ProjectContext
from tests.environment_support import require_loopback
from tests.pinned_settings import old_settings

QUOTED = "  ⎿  Not logged in · Please run /login"
AGENT_WORK = [f"⏺ Step {n}: the deal page renders the review result; checked {n} rows." for n in range(1, 15)]
PROMPT_BOX = ["╭──────────────────────────────────────────╮", "│ >                                        │",
              "╰──────────────────────────────────────────╯", "  ⏵⏵ bypass permissions on (shift+tab to cycle)"]


def screen(*parts: list[str] | str) -> str:
    lines: list[str] = []
    for part in parts:
        lines.extend([part] if isinstance(part, str) else part)
    return "\n".join(lines)


class ScreenReading(unittest.TestCase):
    def test_a_quoted_sign_in_error_with_later_agent_work_is_not_a_sign_out(self):
        text = screen("⏺ Bash(python3 -c 'from app import ai; ai.call(\"ping\")')", QUOTED, AGENT_WORK, PROMPT_BOX)
        self.assertIsNone(attention.detect(text, "claude"))
        self.assertFalse(attention.menu_on_screen(text), "a quoted line never holds the harness's messages either")

    def test_a_child_tools_sign_in_error_right_above_the_prompt_is_not_a_sign_out(self):
        # Codex review r2-1 (2026-11-22): the builder ran `claude -p` (Claude Code strips the token from its commands); the child's
        # "Not logged in" was the newest output above the prompt and stayed while the agent waited, so the agent was read signed out
        text = screen(AGENT_WORK, "⏺ Bash(./csumm sample.txt)", QUOTED, PROMPT_BOX)
        self.assertIsNone(attention.detect(text, "claude"))
        self.assertFalse(attention.menu_on_screen(text))

    def test_a_route_note_quoting_it_far_up_is_not_a_sign_out(self):
        note = "⏺ Routed to Delivery: the app's Claude call answers \"Not logged in · Please run /login\"; check its token."
        self.assertIsNone(attention.detect(screen(note, AGENT_WORK, PROMPT_BOX), "claude"))

    def test_an_agent_s_own_report_quoting_it_right_above_the_prompt_is_not_a_sign_out(self):
        # real runs 4 and 6 (2026-11-22): the CTO's and Delivery's final reports quoted the app's error; each was read signed out for
        # 2 minutes. A CLI's own error starts its line; these sit inside the agent's sentences.
        for report in ('  missing solely because Claude says "Not logged in". The red "Blocked, needs',
                       '  myself: my sandbox\'s claude says "Not logged in", and csumm handled that',
                       '  bug: the real claude prints “Not logged in · Please run /login”, and a Reviewer real run needs a',
                       # real run 6 on fa92c048: in brackets, no quotes
                       '   (Not logged in · Please run /login). I posted this to the CTO as a route'):
            text = screen(AGENT_WORK, "⏺ OBJECTIVE STATUS: PARTIAL", report, "  (rule 19).", PROMPT_BOX)
            self.assertIsNone(attention.detect(text, "claude"), report)
            self.assertFalse(attention.menu_on_screen(text), report)

    def test_a_real_401_with_its_quoted_json_is_still_a_sign_out(self):
        error = '⏺ API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"OAuth token has expired."}} · Please run /login'
        self.assertEqual(attention.detect(screen(AGENT_WORK, error, PROMPT_BOX), "claude"), attention.LOGIN_REASON)

    def test_the_cli_s_own_sign_in_error_above_its_prompt_is_still_a_sign_out(self):
        text = screen(AGENT_WORK, "⏺ API Error: 401 · Please run /login", PROMPT_BOX)
        self.assertEqual(attention.detect(text, "claude"), attention.LOGIN_REASON)
        self.assertTrue(attention.menu_on_screen(text))

    def test_output_arriving_in_pieces_never_raises_the_alarm(self):
        """The Lease screen fed line by line, a second apart: the quoted line is the newest output for a moment, but the
        agent's next lines push it up before it has stayed SIGN_IN_SETTLE_SECONDS, so it never counts."""
        clock = Clock()
        watch = attention.PromptWatch(height=40, provider="claude", clock=clock)
        events = []
        for line in ["⏺ Bash(check the app's Claude call)", QUOTED, *AGENT_WORK, *PROMPT_BOX]:
            events.append(watch.feed((line + "\r\n").encode()))
            clock.advance(1)
            events.append(watch.tick())
        self.assertNotIn(("waiting", attention.LOGIN_REASON), events)
        self.assertIsNone(watch.visible)

    def test_a_real_sign_out_still_counts_once_it_stays(self):
        clock = Clock()
        watch = attention.PromptWatch(height=40, provider="claude", clock=clock)
        self.assertIsNone(watch.feed(screen(AGENT_WORK, "⏺ API Error: 401 · Please run /login", PROMPT_BOX).replace("\n", "\r\n").encode()))
        clock.advance(attention.SIGN_IN_SETTLE_SECONDS - 1)
        self.assertIsNone(watch.tick())
        clock.advance(2)
        self.assertEqual(watch.tick(), ("waiting", attention.LOGIN_REASON))

    def test_gemini_s_sign_in_line_counts_only_at_the_bottom_too(self):
        quoted = "the app said: You are not logged into Antigravity"
        self.assertIsNone(attention.detect(screen(quoted, AGENT_WORK, PROMPT_BOX), "gemini"))
        self.assertEqual(attention.detect(screen(AGENT_WORK, quoted, PROMPT_BOX), "gemini"), attention.GEMINI_SIGN_IN_REASON)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 21, 2, 19, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class BoardActivityOutranksTheScreen(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, base / "private-data", base / "workspaces")
        control.initialize(self.context)
        self.session = control.create(self.context, "codex_delivery", settings_override=old_settings())
        self.authority = SessionTokenAuthority(self.context)
        self.authority.prepare(self.session["id"])
        control.attach(self.context, self.session["id"], os.getpid())
        self.agent = board.register(self.context, "engineering", board.AWAITING_OWNER_DIRECTION, session_id=self.session["id"])
        self.token = self.authority.claim(self.session["id"], os.getpid())
        self.gateway = CommandGateway(self.context, self.authority)

    def held(self) -> dict:
        return next(item for item in control.snapshot(self.context)["sessions"] if item["id"] == self.session["id"])

    def poll(self, *arguments: str) -> dict:
        return self.gateway.execute(self.token, {"protocol": PROTOCOL_VERSION, "nonce": time.time_ns(), "operation": "poll",
                                                 "arguments": ["poll", "--agent", self.agent["id"], *arguments]})

    def test_a_board_call_after_the_reading_clears_it_and_wake_ups_flow_again(self):
        control.record_attention(self.context, self.session["id"], attention.LOGIN_REASON)
        self.assertTrue(attention.needs_owner(self.held()), "the reading holds wake-ups (today's behaviour)")
        self.poll()
        self.assertFalse(attention.needs_sign_in(self.held()))
        self.assertFalse(attention.needs_owner(self.held()), "wake-ups are routed again")

    def test_a_command_started_before_the_reading_proves_nothing(self):
        started = control.now()
        time.sleep(0.01)
        control.record_attention(self.context, self.session["id"], attention.LOGIN_REASON)
        self.assertFalse(control.clear_sign_in_after_board_call(self.context, self.session["id"], started))
        self.assertTrue(attention.needs_sign_in(self.held()))

    def test_a_refused_command_clears_nothing(self):
        control.record_attention(self.context, self.session["id"], attention.LOGIN_REASON)
        with self.assertRaises(SurfaceAuthorizationError):
            self.gateway.execute(self.token, {"protocol": PROTOCOL_VERSION, "nonce": time.time_ns(), "operation": "poll",
                                              "arguments": ["poll", "--agent", "engineering-forged"]})
        self.assertTrue(attention.needs_sign_in(self.held()))

    def test_other_reasons_are_untouched(self):
        for reason in (attention.TRUST_REASON, attention.PERMISSION_REASON, attention.limit_reason("claude")):
            with self.subTest(reason=reason):
                control.clear_attention(self.context, self.session["id"])
                control.record_attention(self.context, self.session["id"], reason)
                self.poll()
                self.assertEqual(self.held()["attention_reason"], reason[:200])


    def test_a_reading_that_replaced_an_earlier_reason_mid_command_is_kept(self):
        """Codex plan check R1-3: a permission prompt at T1, a command starts at T2, the real sign-out at T3: the command proves
        nothing about T3."""
        control.record_attention(self.context, self.session["id"], attention.PERMISSION_REASON)
        time.sleep(0.01)
        started = control.now()
        time.sleep(0.01)
        control.record_attention(self.context, self.session["id"], attention.LOGIN_REASON)
        self.assertFalse(control.clear_sign_in_after_board_call(self.context, self.session["id"], started))
        self.assertTrue(attention.needs_sign_in(self.held()))

    def test_a_hook_event_proves_nothing(self):
        control.record_attention(self.context, self.session["id"], attention.LOGIN_REASON)
        with mock.patch.object(control, "clear_sign_in_after_board_call") as clear, \
                mock.patch.object(CommandGateway, "_execute_concurrent", return_value={}), \
                mock.patch.object(CommandGateway, "_execute_serialized", return_value={}):
            try:
                self.gateway.execute(self.token, {"protocol": PROTOCOL_VERSION, "nonce": time.time_ns(), "operation": "hook-event",
                                                  "arguments": ["hook-event", "--session-id", self.session["id"], "--event", "Stop"]})
            except Exception:   # noqa: BLE001 - only whether the clear was asked matters here
                pass
            clear.assert_not_called()

    def test_the_screen_made_claude_card_goes_and_a_token_check_card_stays(self):
        from harness import claude_auth
        for source, kept in (("screen", False), ("", True)):
            with self.subTest(source=source):
                control.clear_attention(self.context, self.session["id"])
                with control.locked_state(self.context) as state:
                    card = claude_auth.action("rejected")
                    state["sessions"][self.session["id"]]["claude_auth_action"] = {**card, "source": source} if source else card
                control.record_attention(self.context, self.session["id"], attention.LOGIN_REASON)
                self.poll()
                self.assertEqual(bool(self.held().get("claude_auth_action")), kept)

    def test_clearing_the_reading_takes_the_screen_card_with_it_and_keeps_a_token_check_card(self):
        # Codex review r2-1: clear-attention left the screen's "Claude rejected the setup-token" card behind for good
        from harness import claude_auth
        for source, kept in (("screen", False), ("", True)):
            with self.subTest(source=source):
                control.record_attention(self.context, self.session["id"], attention.LOGIN_REASON)
                with control.locked_state(self.context) as state:
                    card = claude_auth.action("rejected")
                    state["sessions"][self.session["id"]]["claude_auth_action"] = {**card, "source": source} if source else card
                control.clear_attention(self.context, self.session["id"])
                self.assertEqual(bool(self.held().get("claude_auth_action")), kept)
                with control.locked_state(self.context) as state:
                    state["sessions"][self.session["id"]].pop("claude_auth_action", None)

    def test_a_screen_card_left_without_its_reading_goes_on_the_next_board_call_and_other_reasons_stay(self):
        from harness import claude_auth
        control.record_attention(self.context, self.session["id"], attention.PERMISSION_REASON)
        with control.locked_state(self.context) as state:
            state["sessions"][self.session["id"]]["claude_auth_action"] = {**claude_auth.action("rejected"), "source": "screen"}
        self.poll()
        self.assertIsNone(self.held().get("claude_auth_action"), "an orphaned screen card is disproved by a working agent")
        self.assertEqual(self.held()["attention_reason"], attention.PERMISSION_REASON[:200], "another waiting reason stays")

    def test_the_supervisor_lets_go_on_its_next_look_and_the_unchanged_screen_stays_released(self):
        watch = attention.PromptWatch(height=40, provider="claude")
        watch.visible = watch.reason = attention.LOGIN_REASON
        recorded = control.now()
        control.record_attention(self.context, self.session["id"], attention.LOGIN_REASON)
        self.assertFalse(interactive_supervisor._sign_in_disproved(self.context, self.session["id"], watch, recorded))
        time.sleep(0.01)
        self.poll()
        self.assertTrue(interactive_supervisor._sign_in_disproved(self.context, self.session["id"], watch, recorded))
        self.assertIsNone(watch.reason)
        self.assertIsNone(watch.tick(), "the same line still on screen does not hold again")

    def test_the_one_check_in_is_due_after_the_wait_for_claude_and_codex_only_and_is_claimed_once(self):
        watch = attention.PromptWatch(height=40, provider="claude")
        watch.feed(screen(AGENT_WORK, QUOTED, PROMPT_BOX).replace("\n", "\r\n").encode())
        watch.reason = attention.LOGIN_REASON
        recorded = "2026-11-21T02:19:00+00:00"
        early, late = "2026-11-21T02:20:00+00:00", "2026-11-21T02:21:30+00:00"
        self.assertFalse(interactive_supervisor._sign_in_check_due(self.context, "claude", watch, recorded, early))
        self.assertTrue(interactive_supervisor._sign_in_check_due(self.context, "claude", watch, recorded, late))
        self.assertTrue(interactive_supervisor._sign_in_check_due(self.context, "codex", watch, recorded, late))
        self.assertFalse(interactive_supervisor._sign_in_check_due(self.context, "gemini", watch, recorded, late))
        menu = attention.PromptWatch(height=40, provider="codex")
        menu.feed(b"> 1. Sign in with ChatGPT\r\n  2. Provide your own API key\r\nPlease log in\r\n")
        menu.reason = attention.LOGIN_REASON
        self.assertFalse(interactive_supervisor._sign_in_check_due(self.context, "codex", menu, recorded, late))
        control.record_attention(self.context, self.session["id"], attention.LOGIN_REASON)
        self.assertTrue(control.record_sign_in_check(self.context, self.session["id"]))
        self.assertFalse(control.record_sign_in_check(self.context, self.session["id"]), "once per reading")

if __name__ == "__main__":
    unittest.main()

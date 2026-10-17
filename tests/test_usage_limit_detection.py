# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A Claude or Codex terminal that used up the owner's allowance is recognised, not restarted (2026-10-16_quota-handoff).

The text below is what the REAL CLIs printed (Claude Code 2.1.293, Codex 0.162.0) answering a local mock that returned each CLI's
own limit response; see board/evidence/2026-10-16_quota-handoff-live-capture/CAPTURES.md in the governance folder. These tests pin
the machine inputs they depend on: a fixed clock and an explicit zone, never the host's.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import unittest

from harness import attention

CHICAGO = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 16, 18, 43, tzinfo=CHICAGO)

CLAUDE_PRINT = "You've hit your session limit · resets 9:43pm (America/Chicago)"
CLAUDE_INTERACTIVE = "\r\n".join([
    "❯ say hi",
    " ⎿  You've hit your session limit · resets 9:44pm (America/Chicago)",
    " ⏺  Usage limit reached · continuing automatically at 9:44pm · esc to",
    "    cancel",
    " ✻  Churned for 0s · done 6:44 PM",
    " ◐ medium · /effort",
    "────────────────────────────────────────────────────────",
    "❯",
    "  ⚠ Usage limit reached · limit resets 9:44pm",
    "  Continuing automatically at 9:44pm · esc to cancel",
    "  ← for agents",
]) + "\r\n"
CODEX_PLUS = ("ERROR: You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
              "https://chatgpt.com/settings/usage to purchase more credits or try again at 9:45 PM.")
CODEX_FAR = ("ERROR: You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to purchase more credits "
             "or try again at Oct 18th, 2026 12:46 AM.")


# The Codex 0.163.0 interactive screen after the limit response (captured 2026-10-16 through a pseudo-terminal, update check off): the line is
# wrapped by the terminal in the middle of the URL, and the working status and composer rows follow it.
CODEX_INTERACTIVE = "\r\n".join([
    "› say hi",
    "  Ask Codex to do anything",
    "  ? for shortcuts",
    "• Working (0s • esc to interrupt)",
    "■ You\u2019ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit https://chatgpt.com/settings/",
    "usage to purchase more credits or try again at 10:12 PM.",
    "·",
]) + "\r\n"


def watch(provider: str, now: datetime = NOW) -> attention.PromptWatch:
    clock = {"now": now}
    result = attention.PromptWatch(height=40, provider=provider, clock=lambda: clock["now"])
    result.clock_state = clock
    return result


def feed(w: attention.PromptWatch, text: str):
    return w.feed(text.replace("\n", "\r\n").encode("utf-8") if "\r\n" not in text else text.encode("utf-8"))


class DetectionTests(unittest.TestCase):
    def test_claude_print_line_is_an_announced_incident_in_the_clis_own_zone(self):
        w = watch("claude")
        self.assertEqual(feed(w, CLAUDE_PRINT + "\r\n"), ("waiting", attention.limit_reason("claude")))
        self.assertEqual(w.incident["kind"], "announced")
        self.assertEqual(datetime.fromisoformat(w.incident["resets_at"]), datetime(2026, 10, 16, 21, 43, tzinfo=CHICAGO))
        self.assertTrue(attention.holds_harness_input(attention.limit_reason("claude")))

    def test_claude_interactive_screen_is_recognised_from_the_status_rows(self):
        w = watch("claude")
        self.assertEqual(feed(w, CLAUDE_INTERACTIVE), ("waiting", attention.limit_reason("claude")))
        self.assertEqual(datetime.fromisoformat(w.incident["resets_at"]), datetime(2026, 10, 16, 21, 44, tzinfo=CHICAGO))

    def test_every_claude_window_and_the_credit_line_match(self):
        for line in ("You've hit your weekly limit · resets 9:43pm (America/Chicago)",
                     "You've hit your Opus limit · resets 9:43pm (America/Chicago)",
                     "You've hit your Sonnet limit · resets 9:43pm (America/Chicago)",
                     "You're out of usage credits · resets 9:43pm (America/Chicago)",
                     "You've hit your weekly limit · resets Oct 23 at 8:45pm (America/Chicago)"):
            w = watch("claude")
            self.assertEqual(feed(w, line + "\r\n"), ("waiting", attention.limit_reason("claude")), line)
            self.assertEqual(w.incident["kind"], "announced", line)

    def test_codex_exec_line_with_its_curly_apostrophe_and_plan_advice(self):
        for line, expected in ((CODEX_PLUS, datetime(2026, 10, 16, 21, 45, tzinfo=CHICAGO)),
                               (CODEX_FAR, datetime(2026, 10, 18, 0, 46, tzinfo=CHICAGO))):
            w = watch("codex")
            self.assertEqual(feed(w, line + "\r\n"), ("waiting", attention.limit_reason("codex")))
            self.assertEqual(datetime.fromisoformat(w.incident["resets_at"]), expected)

    def test_codex_interactive_screen_with_its_url_wrapped_across_rows(self):
        w = watch("codex")
        self.assertEqual(feed(w, CODEX_INTERACTIVE), ("waiting", attention.limit_reason("codex")))
        self.assertEqual(datetime.fromisoformat(w.incident["resets_at"]), datetime(2026, 10, 16, 22, 12, tzinfo=CHICAGO))
        self.assertEqual(w.incident["kind"], "announced")

    def test_a_wrapped_codex_line_still_matches(self):
        wrapped = CODEX_PLUS.replace("visit ", "visit\r\n").replace("or try", "or\r\ntry")
        w = watch("codex")
        self.assertEqual(feed(w, wrapped + "\r\n"), ("waiting", attention.limit_reason("codex")))

    def test_patterns_are_chosen_by_the_sessions_own_provider(self):
        self.assertIsNone(feed(watch("codex"), CLAUDE_PRINT + "\r\n"))
        self.assertIsNone(feed(watch("claude"), CODEX_PLUS + "\r\n"))
        self.assertIsNone(feed(watch(""), CLAUDE_PRINT + "\r\n"))

    def test_a_plain_429_is_not_a_usage_limit(self):
        for line in ("API Error: Request rejected (429) · Number of request tokens has exceeded your per-minute rate limit",
                     "We have hit our usage limit for the quarter; resets next week",
                     "# the harness tests say: You've hit your session limit (a quoted sentence without the CLI's framing)"):
            self.assertIsNone(feed(watch("claude"), line + "\r\n"), line)

    def test_the_hold_ends_when_the_cli_prints_a_new_turn_below_the_limit_line(self):
        """Found 2026-10-17 on a real run: after the nudge Claude answered, but the old limit text was still among the last lines
        and the terminal stayed held for the rest of the run. A newer output event of the CLI below the limit line releases it."""
        w = watch("claude")
        self.assertEqual(feed(w, CLAUDE_INTERACTIVE), ("waiting", attention.limit_reason("claude")))
        after_nudge = "\r\n".join([
            "❯ [SYSTEM CONTROL — harness] Please check the board for your current assignment and continue from there.",
            "⏺ API Error: 400 mock: the allowance is back but this mock has no model",
            "❯",
        ]) + "\r\n"
        self.assertEqual(feed(w, after_nudge), ("cleared", None))
        self.assertIsNone(w.incident)
        codex = watch("codex")
        self.assertEqual(feed(codex, CODEX_INTERACTIVE), ("waiting", attention.limit_reason("codex")))
        self.assertEqual(feed(codex, "• Sure. Hi!\r\n›\r\n"), ("cleared", None))

    def test_the_linux_claude_screen_uses_another_bullet_and_is_released_the_same_way(self):
        """Captured on streamlit (Claude Code 2.1.300): assistant lines start with U+25CF, not U+23FA, and the limit's own
        "Usage limit reached" line starts with it too."""
        held = "\r\n".join([
            "❯ say hi",
            "  ⎿ You've hit your session limit · resets 9:17am (America/Chicago)",
            "● Usage limit reached · continuing automatically at 9:17am · esc to cancel",
            "✻ Sautéed for 0s · done 9:16 AM",
            "────────────────────────────────────────",
            "❯",
            "  ⚠ Usage limit reached · limit resets 9:17am",
            "    Continuing automatically at 9:17am · esc to cancel",
            "  · ← for agents",
        ]) + "\r\n"
        w = watch("claude")
        self.assertEqual(feed(w, held), ("waiting", attention.limit_reason("claude")))
        w.screen.feed(b"\x1b[2J\x1b[H")
        after_nudge = "\r\n".join([
            "  ⎿ You've hit your session limit · resets 9:17am (America/Chicago)",
            "● Usage limit reached · continuing automatically at 9:17am · esc to cancel",
            "✻ Sautéed for 0s · done 9:16 AM",
            "❯ [SYSTEM CONTROL — harness] Please check the board for your current assignment and continue from there.",
            "✽ Actioning…",
            "● API Error: 400 mock: the allowance is back but this mock has no model",
            "✻ Churned for 0s · done 9:18 AM",
        ]) + "\r\n"
        self.assertEqual(feed(w, after_nudge), ("cleared", None))

    def test_a_working_line_above_the_limit_does_not_release_it(self):
        w = watch("codex")
        self.assertEqual(feed(w, CODEX_INTERACTIVE), ("waiting", attention.limit_reason("codex")))   # "• Working" sits ABOVE the ■ line
        self.assertIsNone(w.tick())
        self.assertIsNotNone(w.incident)

    def test_a_quoted_phrase_that_scrolls_away_ends_the_hold(self):
        w = watch("claude")
        feed(w, CLAUDE_PRINT + "\r\n")
        change = None
        for index in range(30):
            change = feed(w, f"agent output line {index}\r\n") or change
        self.assertEqual(change, ("cleared", None))
        self.assertIsNone(w.incident)

    def test_a_permission_menu_turning_straight_into_a_limit_line_is_reported(self):
        w = watch("claude")
        self.assertEqual(feed(w, "Do you want to proceed?\r\n"), ("waiting", attention.PERMISSION_REASON))
        w.screen.feed(b"\x1b[2J\x1b[H")
        self.assertEqual(feed(w, CLAUDE_PRINT + "\r\n"), ("waiting", attention.limit_reason("claude")))

    def test_owner_typing_does_not_clear_a_limit_but_clears_a_menu(self):
        w = watch("claude")
        feed(w, CLAUDE_PRINT + "\r\n")
        self.assertIsNone(w.owner_typed())
        self.assertEqual(w.reason, attention.limit_reason("claude"))
        menu = watch("claude")
        feed(menu, "Do you want to proceed?\r\n")
        self.assertEqual(menu.owner_typed(), ("cleared", None))

    def test_gemini_quota_detection_is_unchanged(self):
        w = watch("gemini")
        change = feed(w, "⚠ Individual quota reached. Please upgrade. Resets in 165h0m46s.\r\n")
        self.assertEqual(change[0], "waiting")
        self.assertIn(attention.GEMINI_QUOTA_MARK, change[1])


class IncidentIdentityTests(unittest.TestCase):
    def test_a_redraw_of_the_same_line_is_one_incident(self):
        w = watch("claude")
        feed(w, CLAUDE_PRINT + "\r\n")
        seen = w.incident["seen_at"]
        w.clock_state["now"] = NOW + timedelta(minutes=5)
        self.assertIsNone(feed(w, CLAUDE_PRINT + "\r\n"))
        self.assertEqual(w.incident["seen_at"], seen)

    def test_the_same_instant_within_two_minutes_is_still_one_incident_and_a_different_one_is_new(self):
        w = watch("claude")
        feed(w, CLAUDE_PRINT + "\r\n")
        w.screen.feed(b"\x1b[2J\x1b[H")
        self.assertIsNone(feed(w, "You've hit your session limit · resets 9:44pm (America/Chicago)\r\n"))
        w.screen.feed(b"\x1b[2J\x1b[H")
        change = feed(w, "You've hit your session limit · resets 11:44pm (America/Chicago)\r\n")
        self.assertEqual(change, ("waiting", attention.limit_reason("claude")))
        self.assertEqual(datetime.fromisoformat(w.incident["resets_at"]).hour, 23)

    def test_a_quiet_screen_reconciled_every_tick_never_moves_the_deadline(self):
        w = watch("codex")
        feed(w, CODEX_PLUS + "\r\n")
        first = w.incident["resets_at"]
        for minutes in range(1, 200, 7):
            w.clock_state["now"] = NOW + timedelta(minutes=minutes)
            self.assertIsNone(w.tick())
        self.assertEqual(w.incident["resets_at"], first)

    def test_no_time_in_the_line_is_an_estimate_held_for_an_hour(self):
        w = watch("claude")
        feed(w, "You've hit your session limit · resets soon (America/Chicago)\r\n")
        self.assertEqual(w.incident["kind"], "estimated")
        self.assertEqual(w.incident["when_text"], "soon")
        self.assertEqual(datetime.fromisoformat(w.incident["resets_at"]), NOW + timedelta(minutes=attention.LIMIT_ESTIMATED_MINUTES))

    def test_the_line_returning_after_it_left_is_a_new_incident(self):
        w = watch("claude")
        feed(w, CLAUDE_PRINT + "\r\n")
        for index in range(30):
            feed(w, f"line {index}\r\n")
        w.clock_state["now"] = NOW + timedelta(hours=1)
        self.assertEqual(feed(w, CLAUDE_PRINT.replace("9:43pm", "11:59pm") + "\r\n"), ("waiting", attention.limit_reason("claude")))
        self.assertEqual(w.incident["seen_at"], (NOW + timedelta(hours=1)).isoformat(timespec="seconds"))


class ParseResetTests(unittest.TestCase):
    def test_a_time_already_past_today_means_tomorrow(self):
        late = datetime(2026, 10, 16, 16, 0, tzinfo=CHICAGO)
        self.assertEqual(attention.parse_reset("3pm", "America/Chicago", late), datetime(2026, 10, 17, 15, 0, tzinfo=CHICAGO))
        self.assertEqual(attention.parse_reset("3:00 PM", None, datetime(2026, 10, 16, 14, 0, tzinfo=CHICAGO)),
                         datetime(2026, 10, 16, 15, 0, tzinfo=CHICAGO))

    def test_a_time_that_passed_minutes_ago_is_due_now_not_tomorrow(self):
        """Seen in the real Codex run: the old line "try again at 9:30 AM" was still drawn at 9:31 and became tomorrow's incident."""
        now = datetime(2026, 10, 17, 9, 31, tzinfo=CHICAGO)
        self.assertEqual(attention.parse_reset("9:30 AM", None, now), datetime(2026, 10, 17, 9, 30, tzinfo=CHICAGO))
        self.assertEqual(attention.parse_reset("9:30am", "America/Chicago", now), datetime(2026, 10, 17, 9, 30, tzinfo=CHICAGO))
        long_ago = datetime(2026, 10, 17, 10, 0, tzinfo=CHICAGO)       # 30 minutes past: not "just passed", the next occurrence is meant
        self.assertEqual(attention.parse_reset("9:30 AM", None, long_ago), datetime(2026, 10, 18, 9, 30, tzinfo=CHICAGO))

    def test_dates_months_ordinals_and_explicit_years(self):
        self.assertEqual(attention.parse_reset("Oct 18 at 12:45am", "America/Chicago", NOW), datetime(2026, 10, 18, 0, 45, tzinfo=CHICAGO))
        self.assertEqual(attention.parse_reset("Oct 23rd, 2026 8:46 PM", None, NOW), datetime(2026, 10, 23, 20, 46, tzinfo=CHICAGO))
        self.assertEqual(attention.parse_reset("Jan 2 at 9am", "America/Chicago", NOW), datetime(2027, 1, 2, 9, 0, tzinfo=CHICAGO))

    def test_relative_forms(self):
        self.assertEqual(attention.parse_reset("in 2h30m", None, NOW), NOW + timedelta(hours=2, minutes=30))
        self.assertEqual(attention.parse_reset("in 165h0m46s", None, NOW), NOW + timedelta(hours=165, seconds=46))

    def test_unsupported_or_malformed_text_is_none_so_the_caller_estimates(self):
        for text in ("", "soon", "99:99pm", "13pm", "Smarch 40 at 3pm", "tomorrow-ish"):
            self.assertIsNone(attention.parse_reset(text, "America/Chicago", NOW), text)

    def test_an_unknown_zone_name_is_read_as_local_time(self):
        self.assertEqual(attention.parse_reset("9:43pm", "Mars/Olympus", NOW), datetime(2026, 10, 16, 21, 43, tzinfo=CHICAGO))

    def test_a_result_is_a_fixed_instant_with_its_offset(self):
        result = attention.parse_reset("9:43pm", "America/Chicago", NOW)
        self.assertEqual(result.utcoffset(), timedelta(hours=-5))

    def test_daylight_saving_gap_and_overlap(self):
        before_gap = datetime(2026, 3, 7, 20, 0, tzinfo=CHICAGO)
        gap = attention.parse_reset("2:30am", "America/Chicago", before_gap + timedelta(hours=1))  # 2:30 on 8 March does not exist
        self.assertEqual(gap.astimezone(CHICAGO).strftime("%Y-%m-%d %H"), "2026-03-08 03")
        before_overlap = datetime(2026, 10, 31, 22, 0, tzinfo=CHICAGO)
        overlap = attention.parse_reset("1:30am", "America/Chicago", before_overlap)  # 1:30 on 1 November happens twice: the earlier one
        self.assertEqual(overlap.utcoffset(), timedelta(hours=-5))


class OwnerSentenceTests(unittest.TestCase):
    def session(self, **extra):
        base = {"provider": "claude", "attention_reason": attention.limit_reason("claude"),
                "limit_resets_at": datetime(2026, 10, 16, 21, 43, tzinfo=CHICAGO).isoformat(), "limit_kind": "announced"}
        base.update(extra)
        return base

    def test_waiting_form_states_what_is_observed_and_the_way_out(self):
        text = attention.limit_sentence("Delivery agent", self.session(), now=NOW)
        self.assertIn("The Delivery agent (Claude) shows Claude's usage-limit message; it announced", text)
        self.assertIn("Nothing is sent to it until the message goes", text)
        self.assertIn("If this is not a real limit, type a message into its terminal", text)
        self.assertNotIn("quota", text.lower())

    def test_estimated_and_dated_and_still_out_forms(self):
        estimated = attention.limit_sentence("Reviewer", self.session(provider="codex", limit_kind="estimated"), now=NOW)
        self.assertIn("(estimated)", estimated)
        dated = attention.limit_sentence("Reviewer", self.session(limit_resets_at=datetime(2026, 10, 23, 20, 45, tzinfo=CHICAGO).isoformat()), now=NOW)
        self.assertRegex(dated, r"Fri 23 Oct, \d{1,2}:\d{2} [AP]M")
        nudged = (NOW + timedelta(hours=3)).isoformat()
        still = attention.limit_sentence("CTO", self.session(limit_nudged_at=nudged), now=NOW + timedelta(hours=3, minutes=5))
        self.assertIn("but still shows Claude's usage-limit message", still)
        self.assertIn("It keeps waiting.", still)

    def test_helpers_are_provider_neutral(self):
        s = {"attention_reason": attention.limit_reason("codex"), "provider": "codex"}
        self.assertTrue(attention.has_quota_block(s))
        self.assertTrue(attention.needs_owner(s))
        self.assertEqual(attention.owner_block(s), "quota")
        self.assertIn("Codex's usage-limit message", attention.owner_block_sentence("Reviewer", s))
        self.assertFalse(attention.needs_sign_in(s))


if __name__ == "__main__":
    unittest.main()

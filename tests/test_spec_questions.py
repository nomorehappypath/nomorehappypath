# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Pure-logic tests for the pre-spec question card (requirement R2)."""
from __future__ import annotations

import unittest

from harness import spec_questions as sq


class ValidateAppQuestions(unittest.TestCase):
    def test_a_well_formed_question_passes_through(self):
        result = sq.validate_app_questions([{"text": "How many warehouses?", "options": ["One", "A few"], "recommended": 0}])
        self.assertEqual(result, [{"text": "How many warehouses?", "options": ["One", "A few"], "recommended": 0}])

    def test_more_than_five_questions_is_refused(self):
        six = [{"text": f"Question number {i}?", "options": ["A", "B"], "recommended": 0} for i in range(6)]
        with self.assertRaises(sq.InvalidSpecQuestions):
            sq.validate_app_questions(six)

    def test_too_few_options_is_refused(self):
        with self.assertRaises(sq.InvalidSpecQuestions):
            sq.validate_app_questions([{"text": "Only one choice?", "options": ["A"], "recommended": 0}])

    def test_recommended_out_of_range_is_refused(self):
        with self.assertRaises(sq.InvalidSpecQuestions):
            sq.validate_app_questions([{"text": "Pick one?", "options": ["A", "B"], "recommended": 5}])


class CardQuestions(unittest.TestCase):
    def test_the_running_cost_question_was_dropped_by_the_owner(self):
        """Owner 2026-11-10: "remove the cost question no need for it"."""
        keys = [question.get("key") for question in sq.FIXED_ALWAYS]
        self.assertNotIn("monthly_cost", keys)
        self.assertEqual(len(sq.FIXED_ALWAYS), 5)

    def test_conditional_questions_are_included_only_when_asked_for(self):
        bare = sq.card_questions([], include_existing_data=False, include_data_loss=False)
        keys = [q.get("key") for q in bare]
        self.assertNotIn("existing_data", keys)
        self.assertNotIn("data_loss", keys)
        self.assertNotIn("data_loss_window", keys)
        self.assertNotIn("concurrency", keys, "concurrency reveals itself client-side, never from the server list")

        full = sq.card_questions([], include_existing_data=True, include_data_loss=True)
        full_keys = [q.get("key") for q in full]
        self.assertIn("existing_data", full_keys)
        self.assertIn("data_loss", full_keys)
        self.assertIn("data_loss_window", full_keys)

    def test_app_questions_come_first(self):
        app_q = {"text": "Which marketplace?", "options": ["eBay", "Etsy"], "recommended": 0}
        questions = sq.card_questions([app_q], include_existing_data=False, include_data_loss=False)
        self.assertEqual(questions[0]["text"], "Which marketplace?")


class ResolveAnswers(unittest.TestCase):
    def test_an_answered_question_is_never_marked_assumed(self):
        questions = [{"key": "how_used", "text": "How do you use it?", "options": ["Web page, hosted", "Desktop app"]}]
        resolved = sq.resolve_answers(questions, {"how_used": "Desktop app"}, description="")
        self.assertEqual(resolved, [{"text": "How do you use it?", "chosen": ["Desktop app"], "assumed": False}])

    def test_more_than_one_choice_for_a_question_is_kept_in_full(self):
        """Owner 2026-11-10: "it will be used on web hosted and on the phone" — every question
        allows more than one choice."""
        questions = [{"key": "how_used", "text": "How do you use it?",
                      "options": ["Web page, hosted", "Desktop app", "Phone app"]}]
        resolved = sq.resolve_answers(
            questions, {"how_used": ["Web page, hosted", "Phone app"]}, description="",
        )
        self.assertEqual(resolved[0]["chosen"], ["Web page, hosted", "Phone app"])
        self.assertFalse(resolved[0]["assumed"])

    def test_a_skipped_question_is_inferred_from_the_description_when_clear(self):
        questions = [{"key": "how_used", "text": "How do you use it?", "options": ["Web page, hosted", "Desktop app"]}]
        resolved = sq.resolve_answers(questions, {}, description="I want a desktop app that runs on my laptop.")
        self.assertEqual(resolved[0]["chosen"], ["Desktop app"])
        self.assertTrue(resolved[0]["assumed"])

    def test_a_skipped_question_with_no_clue_falls_back_to_the_first_option(self):
        questions = [{"key": "how_used", "text": "How do you use it?", "options": ["Web page, hosted", "Desktop app"]}]
        resolved = sq.resolve_answers(questions, {}, description="A tool to track my hobby.")
        self.assertEqual(resolved[0]["chosen"], ["Web page, hosted"])
        self.assertTrue(resolved[0]["assumed"])

    def test_a_password_manager_never_defaults_to_nothing_private(self):
        """Review r1 B1: "Nothing private" is what_holds's first (least-sensitive) option, so
        the plain first-option fallback alone used to pick it even for an obviously sensitive
        app whose description does not happen to repeat an option's exact wording."""
        questions = [{"key": "what_holds", "text": "What will it hold?",
                      "options": ["Nothing private", "My own private information",
                                  "Other people's personal information", "Money, payments or bank access"]}]
        resolved = sq.resolve_answers(questions, {}, description="A password manager for my own accounts.")
        self.assertEqual(resolved[0]["chosen"], ["My own private information"])

    def test_a_payment_app_defaults_to_the_money_tier_even_over_a_weaker_match(self):
        questions = [{"key": "what_holds", "text": "What will it hold?",
                      "options": ["Nothing private", "My own private information",
                                  "Other people's personal information", "Money, payments or bank access"]}]
        resolved = sq.resolve_answers(questions, {}, description="Tracks my own private notes and takes credit card payments.")
        self.assertEqual(resolved[0]["chosen"], ["Money, payments or bank access"])

    def test_a_description_naming_both_private_info_and_a_bank_account_picks_the_higher_tier(self):
        """Review r2 S-001: a bank-access app still receiving the "my own private information"
        answer because its exact phrase appeared in the description alongside "bank account" —
        the highest sensitivity present must win, not whichever option's wording is hit first."""
        questions = [{"key": "what_holds", "text": "What will it hold?",
                      "options": ["Nothing private", "My own private information",
                                  "Other people's personal information", "Money, payments or bank access"]}]
        resolved = sq.resolve_answers(
            questions, {},
            description="Keeps my own private information and connects to my bank account for payments.",
        )
        self.assertEqual(resolved[0]["chosen"], ["Money, payments or bank access"])

    def test_a_genuinely_unclear_app_still_falls_back_to_the_first_option(self):
        questions = [{"key": "what_holds", "text": "What will it hold?",
                      "options": ["Nothing private", "My own private information",
                                  "Other people's personal information", "Money, payments or bank access"]}]
        resolved = sq.resolve_answers(questions, {}, description="A tool to roll dice for board games.")
        self.assertEqual(resolved[0]["chosen"], ["Nothing private"])

    def test_assumed_lines_lists_only_the_skipped_ones(self):
        resolved = [
            {"text": "How do you use it?", "chosen": ["Desktop app"], "assumed": False},
            {"text": "Internet connection?", "chosen": ["Always online"], "assumed": True},
        ]
        self.assertEqual(sq.assumed_lines(resolved), ["Assumed: Internet connection? — Always online"])

    def test_assumed_lines_joins_more_than_one_chosen_option(self):
        resolved = [{"text": "How do you use it?", "chosen": ["Web page, hosted", "Phone app"], "assumed": True}]
        self.assertEqual(
            sq.assumed_lines(resolved), ["Assumed: How do you use it? — Web page, hosted, Phone app"],
        )


if __name__ == "__main__":
    unittest.main()

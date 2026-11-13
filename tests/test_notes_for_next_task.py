# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The builder leaves a short NOTES_FOR_NEXT_TASK.md and every start path tells its agent to read it (owner 2026-11-13).

Each test reads the text a start path actually delivers (the chapter as the `reference` command serves it, or the role's card),
using the chapter name taken from that path's own source. Directive text only; nothing here needs the board.

Run:  PYTHONPATH=. python3 -m unittest tests.test_notes_for_next_task -v
"""
import re
import unittest

from harness import board, reference_chapters as rc

NOTE = "`NOTES_FOR_NEXT_TASK.md`"


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def served(role: str, name: str) -> str:
    return flat(rc.chapter_result(role, name)["text"])


class NotesForNextTask(unittest.TestCase):
    def test_delivery_begin_task_path(self):
        self.assertIn("delivery-workflow", rc.reference_hint("delivery-workflow"))   # the name begin-task hands over (board.py)
        text = served("engineering", "delivery-workflow")
        for phrase in (
            f"read {NOTE} at the root of your bound `task_workspace`",
            "a fresh read every task, never from memory",
            "never overrides the owner's current decisions",
            "Before you ask for final review, update it, under about 60 lines",
            "Commit it with the product work",
            "Never put secrets, keys, tokens, passwords or personal data in it",
            "reads the note but never creates or updates it",
            "skipped silently, never fails a review and never blocks a task",
            "exists but cannot be read, say so in one short technical line",
        ):
            self.assertIn(phrase, text)

    def test_reviewer_routed_review_path(self):
        for request in ({}, {"phase": "final_acceptance"}, {"repair_context": True}):
            names = board._review_chapters(request)
            texts = [served("qa", n) for n in names]
            self.assertTrue(any(f"read {NOTE} afresh at the root of the exact candidate checkout" in t for t in texts), names)
        text = served("qa", "review-rounds")
        for phrase in (
            "verify any claim in it yourself",
            "never a finding, never a FAIL",
            "a secret or personal data actually written in the note is a finding",
            "exists but cannot be read, say so in one short technical line",
        ):
            self.assertIn(phrase, text)

    def test_reviewer_analysis_start_path(self):
        chapter = re.search(r"reference --name ([a-z-]+)", board.ANALYSIS_START_BRIEF).group(1)
        self.assertEqual(chapter, "standing-checks")
        self.assertIn(f"before your own pass, read {NOTE}", served("qa", chapter))

    def test_reviewer_early_contract_check_path(self):
        names = [c for _, s, chs in rc.card_rules("qa") if "CONTRACT CHECK ROUTED" in s for c in chs]
        self.assertIn("contract-first", names)
        text = served("qa", "contract-first")
        self.assertIn(f"before you write checks, read {NOTE}", text)
        self.assertIn("never from the note", text)

    def test_cto_card(self):
        card = flat(rc.delivered_card("cto"))
        self.assertIn(f"read {NOTE} afresh at the root of that task's workspace", card)
        self.assertIn("never a hold, a finding or a route", card)
        self.assertIn("skipped silently", card)

    def test_storage_and_manifest_rule(self):
        text = served("engineering", "harness-storage")
        self.assertIn(f"{NOTE} at the repository root (chapter `delivery-workflow`) is a product file too", text)
        self.assertIn("product files, not governance artifacts", text)
        self.assertIn("`SECURITY.md` and the app's error module", text)
        self.assertIn("the error module and the task notes are product files", flat(rc.delivered_card("engineering")))

    def test_card_sizes(self):
        for role in ("engineering", "qa", "cto"):
            size = len(rc.delivered_card(role).encode("utf-8"))
            self.assertLess(size, rc.MAX_CARD_BYTES, role)
            self.assertEqual(rc.card_problems(role), [], role)


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner's end of the Write-specs question card: served page, and the Continue route
(write-specs-technical-chapters R2)."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer
from tests.test_requirements_contract import context, delivery_with_direction
from tests.environment_support import require_loopback


class SpecAnswersRouteTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_posting_answers_records_them_on_the_board(self):
        agent = delivery_with_direction(self.root, "TASK-ANSWERS")
        board.propose_spec_questions(self.root, agent["id"], "TASK-ANSWERS", [], include_existing_data=False, include_data_loss=False)
        request = Request(
            f"{self.base}/api/tasks/TASK-ANSWERS/spec-answers",
            data=json.dumps({"how_used": "Phone app"}).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 201)
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertEqual(bundle["spec_answers"]["answers"]["how_used"], ["Phone app"])

    def test_posting_more_than_one_choice_for_a_question_records_all_of_them(self):
        """Owner 2026-11-10: "it will be used on web hosted and on the phone" — every question
        allows more than one choice, not only "How do you use it?"."""
        agent = delivery_with_direction(self.root, "TASK-MULTI-ANSWERS")
        board.propose_spec_questions(self.root, agent["id"], "TASK-MULTI-ANSWERS", [], include_existing_data=False, include_data_loss=False)
        request = Request(
            f"{self.base}/api/tasks/TASK-MULTI-ANSWERS/spec-answers",
            data=json.dumps({"how_used": ["Web page, hosted", "Phone app"]}).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 201)
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertEqual(bundle["spec_answers"]["answers"]["how_used"], ["Web page, hosted", "Phone app"])

    def test_posting_without_a_proposed_card_is_refused(self):
        request = Request(
            f"{self.base}/api/tasks/TASK-NEVER-PROPOSED/spec-answers",
            data=json.dumps({"how_used": "Phone app"}).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)

    def test_the_served_page_always_carries_the_five_fixed_questions_and_continue(self):
        """Matches the Reviewer's own contract-check c2-k1 (owner 2026-11-10 deviation: the
        running-cost question was dropped): the five fixed questions, their exact answer
        options, and Continue, are in the served page unconditionally, as checkboxes so the
        owner can pick more than one option per question."""
        with urlopen(f"{self.base}/", timeout=5) as response:
            page = response.read().decode("utf-8")
        for label in (
            "How do you use it?", "Who uses it?", "What will it hold?",
            "If it stops for a day, what happens?", "Internet connection?",
        ):
            with self.subTest(label=label):
                self.assertIn(label, page)
        for answer in (
            "Web page, hosted", "Just me", "Money, payments or bank access",
            "No big deal", "Must also work offline",
        ):
            with self.subTest(answer=answer):
                self.assertIn(answer, page)
        self.assertNotIn("Monthly running cost you accept?", page)
        self.assertIn("Continue", page)

    def test_the_fixed_card_questions_are_multi_select_checkboxes(self):
        with urlopen(f"{self.base}/", timeout=5) as response:
            page = response.read().decode("utf-8")
        self.assertIn('<input type="checkbox" name="how_used"', page)
        self.assertNotIn('name="how_used" value="Web page, hosted" type="radio"', page)
        self.assertNotIn('<input type="radio" name="how_used"', page)


if __name__ == "__main__":
    unittest.main()

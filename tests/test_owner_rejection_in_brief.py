# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner's Not accepted reaches the Reviewer: while its repair is open the review brief carries his own reason with the
replay rule, and the Delivery and Reviewer rules both say to replay his exact steps (owner 2026-11-26: a repair passed review
twice and failed his test the same way twice, because the Reviewer never saw what he hit)."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from harness import board, review_brief
from tests.environment_support import require_loopback
from tests.test_requirements_contract import context, delivery_with_direction

REASON = "Same failure. In a new project the setup check failed: flutter --version Operation not permitted."
REPO = Path(__file__).resolve().parents[1]


class OwnerRejectionInBriefTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def _brief(self, status: str) -> dict:
        """Takes a release-repair status; returns the review brief built while the owner's saved repair has it."""
        with TemporaryDirectory() as tmp:
            root = context(Path(tmp))
            delivery_with_direction(root, "TASK-R")
            with board.locked_state(root) as state:
                state.setdefault("requirement_confirmations", {})["TASK-R"] = {
                    "text": "Final agreed requirements: the app runs.", "confirmed_at": board.now(), "version": 1,
                }
                state.setdefault("release_repairs", {})["TASK-R"] = {
                    "task": "TASK-R", "status": status, "reason": REASON, "attachments": [],
                    "created_at": "2026-11-26T23:30:11+00:00",
                }
            return review_brief.build(root, board.snapshot(root), {"id": "review-1", "task": "TASK-R", "test_scope": "full"})

    def test_an_open_owner_repair_puts_his_reason_and_the_replay_rule_in_the_brief(self):
        for status in sorted(board.ACTIVE_REPAIR_STATUSES):
            rejection = self._brief(status)["owner_rejection"]
            self.assertEqual(rejection["reason"], REASON, status)
            self.assertIn("Replay his exact steps", rejection["rule"])
            self.assertIn("never a `Not run for real` line", rejection["rule"])

    def test_a_closed_repair_adds_nothing(self):
        self.assertNotIn("owner_rejection", self._brief("RELEASED"))

    def test_both_rules_say_to_replay_the_owners_steps(self):
        delivery = (REPO / "directives/cards/DELIVERY.md").read_text(encoding="utf-8")
        reviewer = (REPO / "directives/cards/REVIEWER.md").read_text(encoding="utf-8")
        agent = (REPO / "directives/AGENT.md").read_text(encoding="utf-8")
        self.assertIn("replay the owner's exact failing steps", delivery)
        self.assertIn("After the owner's Not accepted, replay his exact steps", reviewer)
        self.assertIn("Delivery's replay is\nits claim, not your proof", agent)


if __name__ == "__main__":
    unittest.main()

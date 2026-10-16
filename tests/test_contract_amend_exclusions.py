# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A contract's immutable exclusions can be amended beside the originals with the owner's recorded approval.

Owner order 2026-10-15: exclusions were immutable with no amend path, so a project whose owner later required
click/type proof was stuck. ``amend-exclusions`` (CTO only) appends a dated amendment from the owner's own recorded words;
the original exclusions and the immutable scope are never edited; an amendment without approval is refused.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from harness import board, board_surface, contract, control
from tests.environment_support import require_loopback
from tests.test_requirements_contract import context, delivery_with_direction

TASK = "TASK-X"
EXCLUSIONS = [
    {"text": "No interactive browser testing", "approved_by": "owner", "reason": "not needed at the start"},
    {"text": "No mobile layout", "approved_by": "owner", "reason": "desktop only"},
]
OWNER_WORDS = "interactive browser testing is now required, lift that exclusion"


def _project(base: Path):
    root = context(base)
    delivery = delivery_with_direction(root, TASK)
    cto_session = control.create(root, "claude_cto")
    cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=cto_session["id"])
    contract.create_contract(root, TASK, "Build the todo app", ["A working todo page"], EXCLUSIONS)
    return root, delivery, cto


def _owner_said(root, cto, text):
    return board.record_owner_message(root, cto["id"], text, "cto_message")["message"]


def _contract(root):
    return json.loads(contract._task_path(root, TASK).read_text())


class AmendExclusionsTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def amend(self, root, cto, message, **kw):
        arguments = dict(action="lift", exclusion=1, text="", reason="the owner now requires click proof",
                         owner_message_id=message["id"], owner_words=OWNER_WORDS)
        arguments.update(kw)
        return board.amend_exclusions(root, cto["id"], TASK, arguments["action"], arguments["exclusion"], arguments["text"],
                                      arguments["reason"], arguments["owner_message_id"], arguments["owner_words"])

    def test_an_owner_approved_lift_is_recorded_beside_untouched_originals(self):
        with TemporaryDirectory() as tmp:
            root, _delivery, cto = _project(Path(tmp))
            before = _contract(root)
            result = self.amend(root, cto, _owner_said(root, cto, f"CTO: {OWNER_WORDS}. Thanks."))
            after = _contract(root)
            self.assertEqual(after["exclusions"], before["exclusions"], "the original exclusions are not rewritten")
            self.assertEqual(after["immutable_scope"], before["immutable_scope"])
            amendment = after["exclusion_amendments"][0]
            self.assertEqual((amendment["action"], amendment["index"], amendment["owner_words"]), ("lift", 1, OWNER_WORDS))
            self.assertTrue(amendment["recorded_at"] and amendment["owner_message_id"])
            states = [(item["index"], item["status"]) for item in result["effective_exclusions"]]
            self.assertEqual(states, [(1, "lifted"), (2, "in force")])
            self.assertEqual(contract.contract_preflight(root, TASK), (True, []))
            event = next(e for e in board.snapshot(root)["events"] if e["kind"] == "exclusions_amended_by_owner")
            self.assertIn(OWNER_WORDS, event["message"])

    def test_replace_and_add_change_what_is_in_force_and_an_identical_retry_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            root, _delivery, cto = _project(Path(tmp))
            message = _owner_said(root, cto, f"CTO: {OWNER_WORDS}. Also no payments.")
            first = self.amend(root, cto, message, action="replace", exclusion=2, text="Mobile layout only for the todo page")
            again = self.amend(root, cto, message, action="replace", exclusion=2, text="Mobile layout only for the todo page")
            self.assertEqual(first["amendment"]["id"], again["amendment"]["id"])
            self.assertEqual(len(_contract(root)["exclusion_amendments"]), 1)
            added = self.amend(root, cto, message, action="add", exclusion="", text="No payments", owner_words="Also no payments")
            effective = {(item["status"], item.get("replaced_by") or item["exclusion"] if isinstance(item["exclusion"], str) else item.get("replaced_by", "")) for item in added["effective_exclusions"]}
            self.assertIn(("replaced", "Mobile layout only for the todo page"), effective)
            self.assertIn(("added", "No payments"), effective)
            with self.assertRaisesRegex(ValueError, "already amended"):
                self.amend(root, cto, message, action="lift", exclusion=2)

    def test_an_amendment_without_the_owners_approval_is_refused(self):
        with TemporaryDirectory() as tmp:
            root, _delivery, cto = _project(Path(tmp))
            message = _owner_said(root, cto, f"CTO: {OWNER_WORDS}.")
            before = _contract(root)
            for kwargs, words in (
                ({"owner_message_id": ""}, "--owner-message must name the recorded owner message"),
                ({"owner_message_id": "msg-does-not-exist"}, "no recorded owner message"),
                ({"owner_words": ""}, "--owner-words must quote"),
                ({"owner_words": "the owner said something else entirely"}, "not in owner message"),
                ({"reason": ""}, "--reason must say why"),
                ({"exclusion": 9}, "does not exist"),
                ({"action": "delete"}, "--action must be lift, replace or add"),
                ({"action": "replace", "text": ""}, "--text .* is required"),
            ):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, words):
                    self.amend(root, cto, message, **kwargs)
            self.assertEqual(_contract(root), before, "a refused amendment changes nothing")
            self.assertNotIn("exclusion_amendments", _contract(root))

    def test_only_the_active_cto_may_amend(self):
        with TemporaryDirectory() as tmp:
            root, delivery, cto = _project(Path(tmp))
            message = _owner_said(root, cto, f"CTO: {OWNER_WORDS}.")
            with self.assertRaisesRegex(ValueError, "only the active CTO"):
                board.amend_exclusions(root, delivery["id"], TASK, "lift", 1, "", "the owner now requires click proof", message["id"], OWNER_WORDS)
            self.assertIn("amend-exclusions", board_surface.CTO_OPERATIONS)
            self.assertNotIn("amend-exclusions", board_surface.DELIVERY_OPERATIONS)
            self.assertNotIn("amend-exclusions", board_surface.REVIEWER_OPERATIONS)
            self.assertIn("amend-exclusions", board_surface.ALL_BOARD_OPERATIONS)

    def test_the_originals_stay_immutable_and_a_hand_made_amendment_without_approval_is_a_problem(self):
        with TemporaryDirectory() as tmp:
            root, _delivery, cto = _project(Path(tmp))
            self.amend(root, cto, _owner_said(root, cto, f"CTO: {OWNER_WORDS}."))
            path = contract._task_path(root, TASK)
            value = _contract(root)
            # editing an original exclusion in place is still caught, amendments or not
            tampered = json.loads(json.dumps(value))
            tampered["exclusions"][0]["text"] = "No interactive browser testing at all"
            path.write_text(json.dumps(tampered))
            ok, problems = contract.contract_preflight(root, TASK)
            self.assertFalse(ok)
            self.assertTrue(any("immutable" in problem for problem in problems))
            # an amendment written without the owner's approval reference is rejected by the check
            forged = json.loads(json.dumps(value))
            forged["exclusion_amendments"].append({"id": "amend-forged", "action": "lift", "index": 2, "reason": "because", "owner_message_id": "", "owner_words": ""})
            path.write_text(json.dumps(forged))
            ok, problems = contract.contract_preflight(root, TASK)
            self.assertFalse(ok)
            self.assertTrue(any("no owner approval reference" in problem for problem in problems), problems)
            path.write_text(json.dumps(value))
            self.assertEqual(contract.contract_preflight(root, TASK), (True, []))

    def test_an_amendment_changes_the_contract_revision_so_an_earlier_pass_is_not_reused(self):
        with TemporaryDirectory() as tmp:
            root, _delivery, cto = _project(Path(tmp))
            before = board._contract_revision(root, TASK)["sha256"]
            self.amend(root, cto, _owner_said(root, cto, f"CTO: {OWNER_WORDS}."))
            after = board._contract_revision(root, TASK)["sha256"]
            self.assertNotEqual(before, after)
            self.assertEqual(after, board._contract_revision(root, TASK)["sha256"], "stable once recorded")


if __name__ == "__main__":
    unittest.main()

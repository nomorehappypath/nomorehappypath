# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""board._saved_data_verdict_problem: the Reviewer's required "Saved data: ..." line on a PASS
(SAVED_DATA_UPGRADE_SAFETY.md 3.2.3/3.4.6), proven directly against every accepted and refused form.
"""
from __future__ import annotations

import unittest

from harness import board


def _request(**saved_data) -> dict:
    return {"saved_data": {"rules": True, "declared_changed": False, "hint_paths": [], "previous_state": "known", **saved_data}}


class SavedDataVerdictProblemTests(unittest.TestCase):
    def test_rules_not_engaged_accepts_anything(self):
        self.assertEqual(board._saved_data_verdict_problem({"saved_data": {}}, "no mention at all"), "")
        self.assertEqual(board._saved_data_verdict_problem({}, "no mention at all"), "")

    def test_bare_unchanged_is_accepted_when_nothing_was_declared_or_hinted(self):
        self.assertEqual(board._saved_data_verdict_problem(_request(), "Saved data: unchanged"), "")

    def test_bare_unchanged_is_refused_when_delivery_declared_a_change(self):
        problem = board._saved_data_verdict_problem(_request(declared_changed=True), "Saved data: unchanged")
        self.assertIn("declared a saved-data change", problem)

    def test_unchanged_with_a_reason_is_accepted_despite_the_hint(self):
        problem = board._saved_data_verdict_problem(
            _request(hint_paths=["migrations/0002.py"]), "Saved data: unchanged — that file only renames a helper")
        self.assertEqual(problem, "")

    def test_changed_passed_citing_a_real_certified_row_is_accepted(self):
        request = _request(declared_changed=True)
        request["challenge_execution"] = {"bundle": {"scenario_ids": ["S-UPG"]}}
        problem = board._saved_data_verdict_problem(request, "Saved data: changed — a due date — upgrade check S-UPG: passed")
        self.assertEqual(problem, "")

    def test_changed_passed_citing_a_row_with_no_certified_result_is_refused(self):
        request = _request(declared_changed=True)
        request["challenge_execution"] = {"bundle": {"scenario_ids": []}}
        problem = board._saved_data_verdict_problem(request, "Saved data: changed — a due date — upgrade check S-UPG: passed")
        self.assertIn("no certified PASSED result", problem)

    def test_changed_failed_can_never_be_a_pass(self):
        request = _request(declared_changed=True)
        problem = board._saved_data_verdict_problem(request, "Saved data: changed — a due date — upgrade check S-UPG: failed")
        self.assertIn("no certified PASSED result", problem)

    def test_missing_outcome_can_never_be_a_pass(self):
        problem = board._saved_data_verdict_problem(
            _request(declared_changed=True), 'Saved data: changed — a due date — upgrade check: missing UPGRADING.md "Before you update"')
        self.assertIn("cannot be a PASS", problem)

    def test_not_run_is_accepted(self):
        problem = board._saved_data_verdict_problem(
            _request(declared_changed=True), "Saved data: changed — a due date — upgrade check: not run (Postgres unavailable here)")
        self.assertEqual(problem, "")

    def test_first_version_is_accepted_only_when_the_state_is_first(self):
        refused = board._saved_data_verdict_problem(
            _request(declared_changed=True, previous_state="known"), "Saved data: changed — a due date — first version, nothing to upgrade")
        self.assertIn("not a first/empty", refused)
        accepted = board._saved_data_verdict_problem(
            _request(declared_changed=True, previous_state="first"), "Saved data: changed — a due date — first version, nothing to upgrade")
        self.assertEqual(accepted, "")

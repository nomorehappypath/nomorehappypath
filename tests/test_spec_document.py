# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Targets-line format rule (requirement R5): a number with conditions, a yes/no, a named
test, or "Not applicable: <reason>" — never an adjective."""
from __future__ import annotations

import unittest

from harness import spec_document as sd


class TargetLineViolation(unittest.TestCase):
    def test_a_number_with_conditions_is_allowed(self):
        self.assertEqual(
            sd.target_line_violation(
                "checkout page under 300 ms for 95 of 100 requests with 50 users and 10,000 fake orders",
            ), "",
        )

    def test_a_yes_no_is_allowed(self):
        self.assertEqual(sd.target_line_violation("passwords never stored in clear: yes"), "")
        self.assertEqual(sd.target_line_violation("data sent to a third party without consent: no"), "")

    def test_a_named_test_is_allowed(self):
        self.assertEqual(
            sd.target_line_violation(
                "full restore from latest backup under 30 minutes, tested on the test copy: test_restore_from_backup",
            ), "",
        )

    def test_not_applicable_with_a_reason_is_allowed(self):
        self.assertEqual(sd.target_line_violation("Not applicable: one user, one run per night"), "")

    def test_bare_not_applicable_with_no_reason_is_refused(self):
        self.assertNotEqual(sd.target_line_violation("Not applicable:"), "")

    def test_a_plain_adjective_claim_is_refused(self):
        violation = sd.target_line_violation("the checkout page should feel fast and responsive")
        self.assertNotEqual(violation, "")
        self.assertIn("not one of the four allowed Targets forms", violation)

    def test_an_empty_line_is_refused(self):
        self.assertNotEqual(sd.target_line_violation(""), "")

    def test_a_bare_number_with_no_measurable_condition_is_refused(self):
        """Review r1 B2: a digit alone names nothing measurable."""
        self.assertNotEqual(sd.target_line_violation("Target 5"), "")
        self.assertNotEqual(sd.target_line_violation("version 2 of the API is supported"), "")

    def test_a_percentage_of_requests_form_is_allowed(self):
        self.assertEqual(sd.target_line_violation("under 300 ms for 95 of 100 requests"), "")


class ValidateTargets(unittest.TestCase):
    def test_a_clean_chapter_has_no_violations(self):
        lines = ["passwords never stored in clear: yes", "login page under 200 ms for 95 of 100 requests"]
        self.assertEqual(sd.validate_targets(lines), [])

    def test_one_bad_line_among_good_ones_is_reported(self):
        lines = ["passwords never stored in clear: yes", "it should feel secure"]
        violations = sd.validate_targets(lines)
        self.assertEqual(len(violations), 1)
        self.assertIn("it should feel secure", violations[0])


if __name__ == "__main__":
    unittest.main()

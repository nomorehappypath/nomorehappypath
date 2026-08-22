# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import unittest

from harness import parity_audit


class HarnessParityAuditTests(unittest.TestCase):
    def test_missing_or_unclassified_dev_production_file_fails(self):
        problems = parity_audit.audit_inventories(
            {"harness/shared.py": b"same", "harness/missing.py": b"dev"},
            {"harness/shared.py": b"different"}, set(), set(),
            exact_paths={"harness/shared.py"}, adapted_paths=set(), equivalents={},
        )
        self.assertIn("Required byte-identical file differs: harness/shared.py", problems)
        self.assertIn("Next is missing Dev production file: harness/missing.py", problems)

    def test_new_dev_regression_requires_a_real_next_counterpart(self):
        source = "tests/test_guard.py::test_new_guard"
        target = "tests/test_guard.py::test_stronger_guard"
        missing = parity_audit.audit_inventories(
            {}, {}, {source}, set(), exact_paths=set(), adapted_paths=set(), equivalents={},
        )
        self.assertEqual(missing, [f"Dev regression test has no Next equivalent: {source}"])
        covered = parity_audit.audit_inventories(
            {}, {}, {source}, {target}, exact_paths=set(), adapted_paths=set(),
            equivalents={source: (target,)},
        )
        self.assertEqual(covered, [])


if __name__ == "__main__":
    unittest.main()

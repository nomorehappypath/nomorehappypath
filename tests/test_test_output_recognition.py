# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""One recogniser says whether a test command ran tests, for every ecosystem the allow-list accepts."""
import unittest

from harness import certified_execution


class ExecutedTestCountTests(unittest.TestCase):
    count = staticmethod(certified_execution.executed_test_count)

    def test_python_and_javascript_summaries_still_count(self):
        self.assertEqual(self.count("Ran 7 tests in 0.1s\nOK"), 7)
        self.assertEqual(self.count("5 passed in 0.2s"), 5)
        self.assertEqual(self.count("Tests: 5 passed, 5 total"), 5)

    def test_node_test_output(self):
        self.assertEqual(self.count("\u2139 tests 5\n\u2139 pass 5\n\u2139 fail 0"), 5)
        self.assertEqual(self.count("# tests 3\n# pass 3"), 3)
        self.assertEqual(self.count("\u2139 tests 0\n\u2139 pass 0"), 0)

    def test_go_output(self):
        self.assertEqual(self.count("=== RUN TestA\n--- PASS: TestA (0.00s)\n--- PASS: TestB (0.00s)\nok  pkg 0.2s"), 2)
        self.assertEqual(self.count("ok  \texample.com/pkg\t0.213s"), 1)
        self.assertIsNone(self.count("ok  \texample.com/pkg\t(cached)"))
        self.assertEqual(self.count("ok  \texample.com/a\t(cached)\nok  \texample.com/b\t0.3s"), 1)
        self.assertEqual(self.count("?   \texample.com/pkg\t[no test files]"), 0)

    def test_cargo_output(self):
        self.assertEqual(self.count("running 3 tests\ntest result: ok. 3 passed; 0 failed"), 3)
        self.assertEqual(self.count("running 0 tests\ntest result: ok. 0 passed; 0 failed"), 0)
        self.assertEqual(self.count("running 0 tests\n\nrunning 4 tests\ntest result: ok. 4 passed"), 4)

    def test_gradle_and_junit_output(self):
        self.assertEqual(self.count("FooTest > adds PASSED\nFooTest > subtracts PASSED\nBUILD SUCCESSFUL"), 2)
        self.assertEqual(self.count("> Task :test NO-SOURCE\nBUILD SUCCESSFUL"), 0)
        self.assertIsNone(self.count("> Task :test UP-TO-DATE\nBUILD SUCCESSFUL"))
        self.assertEqual(self.count("Tests run: 12, Failures: 0, Errors: 0"), 12)

    def test_silence_is_not_a_run(self):
        self.assertIsNone(self.count("BUILD SUCCESSFUL"))
        self.assertTrue(certified_execution._reports_zero_executed_tests("NO TESTS RAN"))


if __name__ == "__main__":
    unittest.main()

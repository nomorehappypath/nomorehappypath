# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A project's own status flag is a claim, not proof (owner 2026-10-23: "a generic harness issue").

The example: a project's quality gate had a soft route ("warnings only; continuing") that ended with passed=true although its final
check had not succeeded, and the roles took the flag as proof. The harness now (a) tells the CTO and the Reviewer to read the run's own
final-check output, (b) makes a gate that can pass without it a material finding, and (c) refuses a certified run whose test reports a
`FINAL CHECK:` line that is not PASS, whatever the exit code, before anything is stored and before any retry classification.

Run:  PYTHONPATH=. python3 -m unittest tests.test_final_check_soft_pass -v
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import board, browser_acceptance, certified_execution, cto, execution_identity
from tests import environment_support
from tests import test_execute_once as base

ROOT = Path(__file__).resolve().parents[1]


def flat(path):
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


class HelperTests(unittest.TestCase):
    def test_all_pass_or_no_line_is_not_a_problem(self):
        self.assertEqual("", certified_execution.final_check_problem("Ran 3 tests\nOK\n"))
        self.assertEqual("", certified_execution.final_check_problem("FINAL CHECK: PASS claim audit\nFINAL CHECK: PASS register\n"))

    def test_a_negative_test_that_watches_the_gate_refuse_prints_pass_and_is_accepted(self):
        out = "gate said: warnings only; continuing -> refused as expected\nFINAL CHECK: PASS soft route is refused\nRan 1 test\nOK\n"
        self.assertEqual("", certified_execution.final_check_problem(out))

    def test_every_other_status_is_a_problem_with_a_truthful_reason(self):
        cases = {
            "FINAL CHECK: FAIL claim audit": "the project's final check failed: claim audit",
            "FINAL CHECK: NOT RUN claim audit": "the project's final check did not run: claim audit",
            "FINAL CHECK: SKIPPED claim audit": "the project's final check did not run: claim audit",
            "FINAL CHECK: WARNINGS ONLY claim audit": "the project's gate approved with warnings only, its final check did not pass: claim audit",
            "FINAL CHECK: bogus": "a FINAL CHECK line could not be read: FINAL CHECK: bogus",
            "FINAL CHECK:": "a FINAL CHECK line could not be read",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertIn(expected, certified_execution.final_check_problem("Ran 1 test\n" + line + "\n"))

    def test_one_bad_line_among_good_ones_is_enough_in_any_order(self):
        for out in ("FINAL CHECK: PASS a\nFINAL CHECK: SKIPPED b\n", "FINAL CHECK: SKIPPED b\nFINAL CHECK: PASS a\n",
                    "FINAL CHECK: PASS a\nFINAL CHECK: PASS a2\nFINAL CHECK: nonsense\n"):
            with self.subTest(out=out):
                self.assertTrue(certified_execution.final_check_problem(out))

    def test_summary_is_bounded_and_ordered(self):
        out = "\n".join(f"FINAL CHECK: PASS gate {n} " + "x" * 400 for n in range(40))
        summary = certified_execution.final_check_summary(out)
        self.assertEqual(certified_execution.FINAL_CHECK_MAX_LINES, len(summary))
        self.assertTrue(all(len(line) <= certified_execution.FINAL_CHECK_MAX_CHARS for line in summary))
        self.assertIn("gate 0", summary[0])

    def test_the_line_must_start_a_line(self):
        self.assertEqual("", certified_execution.final_check_problem("note: FINAL CHECK: FAIL is what the gate prints\n"))


class CertifiedRunTests(unittest.TestCase):
    def setUp(self):
        environment_support.require_process_table()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate = execution_identity.candidate_evidence_identity("c" * 40, "t" * 40, "contract", {"ledger": "l" * 64})

    def execute(self, script: str, retry_reason: str = ""):
        command = f"python3 -c \"{script}\""
        return certified_execution.run(
            self.root, self.root, command, candidate=self.candidate, environment_sha256="e" * 64, environment=os.environ,
            lockfile_digests={}, role="reviewer", gate="final", retry_reason=retry_reason)

    @staticmethod
    def output(*lines, code=0):
        body = "print('Ran 2 tests in 0.001s'); " + "".join(f"print({line!r}); " for line in lines)
        return body + (f"import sys; sys.exit({code})" if code else "")

    def test_the_soft_pass_exit_zero_run_is_refused_with_the_plain_reason(self):
        with self.assertRaises(ValueError) as caught:
            self.execute(self.output("OK", "FINAL CHECK: WARNINGS ONLY claim audit"))
        self.assertIn("approved with warnings only", str(caught.exception))
        self.assertNotIn("the tests passed", str(caught.exception))

    def test_fail_is_refused_with_exit_zero_and_with_exit_one(self):
        for code in (0, 1):
            with self.subTest(exit=code):
                with self.assertRaises(ValueError) as caught:
                    self.execute(self.output("FINAL CHECK: FAIL claim audit", code=code) + f" # {code}")
                self.assertIn("final check failed", str(caught.exception))

    def test_pass_is_certified_and_the_lines_are_recorded_in_the_measurement(self):
        result = self.execute(self.output("OK", "FINAL CHECK: PASS claim audit"))
        self.assertEqual(["FINAL CHECK: PASS claim audit"], result["measurement"]["final_checks"])

    def test_no_line_behaves_exactly_as_before(self):
        result = self.execute(self.output("OK"))
        self.assertEqual([], result["measurement"]["final_checks"])

    def test_a_refused_run_is_never_stored_as_a_success_and_the_retry_is_refused_too(self):
        script = self.output("OK", "FINAL CHECK: SKIPPED claim audit")
        with self.assertRaises(ValueError):
            self.execute(script)
        with self.assertRaises(ValueError) as again:                      # not served from a cached success
            self.execute(script)
        self.assertIn("repair reason", str(again.exception))
        with self.assertRaises(ValueError) as third:                      # with a reason it runs again and is refused again
            self.execute(script, retry_reason="re-run after fixing nothing")
        self.assertIn("did not run", str(third.exception))

    def test_a_known_final_check_failure_beats_a_retryable_interruption(self):
        marker = browser_acceptance.OWNER_MACHINE_ACTIVITY_MARKER
        with self.assertRaises(ValueError) as caught:
            self.execute(self.output(marker + " handler changed", "FINAL CHECK: FAIL claim audit", code=1))
        self.assertNotIsInstance(caught.exception, certified_execution.OwnerMachineActivity)
        self.assertIn("final check failed", str(caught.exception))
        with self.assertRaises(certified_execution.OwnerMachineActivity):  # no line: still the retryable interruption
            self.execute(self.output(marker + " handler changed", code=1))

    def test_stored_success_with_a_bad_line_is_never_reused_and_one_without_a_line_still_is(self):
        script = self.output("OK")
        first = self.execute(script)
        self.assertEqual("executed_and_certified", first["measurement"]["cache_decision"])
        again = self.execute(script)
        self.assertEqual("exact_success_reused", again["measurement"]["cache_decision"])    # history stays valid
        with mock.patch.object(execution_identity, "load_output", return_value="Ran 2 tests\nFINAL CHECK: WARNINGS ONLY claim audit\n"):
            third = self.execute(script)
        self.assertEqual("executed_and_certified", third["measurement"]["cache_decision"], "re-executed, not reused")


class DirectVerdictOverHistoricalBundleTests(base.ExecuteOnceTests):
    """Codex round 1: a resumed reviewer need not repeat execute-challenge, so the verdict step itself must refuse a stored bundle
    whose output says a final check did not pass; a stored bundle with no line stays valid."""

    def _historical_state(self, printed: str):
        # Delivery's own run (all tests, no -k) prints nothing; only the reviewer's challenge command (-k passes) prints the line.
        (self.root / "test_smoke.py").write_text(
            "import sys, unittest\n\nclass Smoke(unittest.TestCase):\n"
            "    def test_passes(self):\n"
            f"        if '-k' in sys.argv: print({printed!r})\n        self.assertTrue(True)\n")
        qa, request, _, _ = self._claimed_review()
        with mock.patch.object(certified_execution, "final_check_problem", return_value=""):    # the state before this rule existed
            board.execute_challenge(self.root, qa["id"], request["id"])
        return qa, request

    def test_a_passed_verdict_over_a_stored_skipped_final_check_is_refused(self):
        qa, request = self._historical_state("FINAL CHECK: SKIPPED audit")
        with self.assertRaises(ValueError) as caught:
            board.qa_result(self.root, qa["id"], request["id"], "passed", "TASK DONE: YES - verdict on the stored evidence", "")
        self.assertIn("did not run: audit", str(caught.exception))
        self.assertIn("execute-challenge", str(caught.exception))

    def test_a_failed_verdict_over_the_same_bundle_is_still_recordable(self):
        qa, request = self._historical_state("FINAL CHECK: SKIPPED audit")
        self.assertEqual("failed", board.qa_result(self.root, qa["id"], request["id"], "failed", "a real finding", "")["status"])

    def test_a_stored_pass_line_still_passes(self):
        qa, request = self._historical_state("FINAL CHECK: PASS audit")
        self.assertEqual("passed", board.qa_result(self.root, qa["id"], request["id"], "passed", "TASK DONE: YES - ok", "")["status"])

    def test_a_stored_bundle_with_no_line_still_passes(self):
        qa, request = self._historical_state("no marker here")
        self.assertEqual("passed", board.qa_result(self.root, qa["id"], request["id"], "passed", "TASK DONE: YES - ok", "")["status"])


class CachedMeasurementTests(unittest.TestCase):
    def test_a_reused_success_reports_its_final_check_lines_too(self):
        environment_support.require_process_table()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = execution_identity.candidate_evidence_identity("c" * 40, "t" * 40, "contract", {"ledger": "l" * 64})
            command = "python3 -c \"print('Ran 1 test in 0.1s'); print('FINAL CHECK: PASS audit')\""
            kwargs = dict(candidate=candidate, environment_sha256="e" * 64, environment=os.environ, lockfile_digests={}, role="reviewer", gate="final")
            first = certified_execution.run(root, root, command, **kwargs)
            again = certified_execution.run(root, root, command, **kwargs)
        self.assertEqual("exact_success_reused", again["measurement"]["cache_decision"])
        self.assertEqual(first["measurement"]["final_checks"], again["measurement"]["final_checks"])
        self.assertEqual(["FINAL CHECK: PASS audit"], again["measurement"]["final_checks"])


class InternalQaTests(unittest.TestCase):
    def run_qa(self, stdout, code=0):
        done = subprocess.CompletedProcess([], code, stdout=stdout, stderr="")
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(board.execution_preflight, "validate_commands", return_value=None), \
                mock.patch.object(board.subprocess, "run", return_value=done):
            return board._run_internal_qa("python3 -m unittest test_probe", Path(temporary))

    def test_the_uncertified_internal_qa_path_refuses_the_soft_pass_before_the_exit_code(self):
        with self.assertRaises(ValueError) as caught:
            self.run_qa("Ran 3 tests\nOK\nFINAL CHECK: WARNINGS ONLY claim audit\n")
        self.assertIn("warnings only", str(caught.exception))

    def test_it_accepts_pass_and_no_line(self):
        self.assertIn("OK", self.run_qa("Ran 3 tests\nOK\nFINAL CHECK: PASS claim audit\n"))
        self.assertIn("OK", self.run_qa("Ran 3 tests\nOK\n"))


class ReuseTests(unittest.TestCase):
    def test_every_reuse_path_revalidates_stored_output(self):
        board_source = (ROOT / "harness/board.py").read_text(encoding="utf-8")
        self.assertIn("certified_execution.final_check_problem(\n            _evidence_text(", board_source)
        self.assertIn('"final_checks": certified_execution.final_check_summary(', board_source)
        self.assertEqual(2, board_source.count('"final_checks": certified_execution.final_check_summary('), "fresh and already_certified answers both carry the lines")
        self.assertIn("certified_execution.final_check_problem(output)", (ROOT / "harness/cto.py").read_text(encoding="utf-8"))

    def test_the_final_check_lines_survive_an_output_longer_than_the_excerpt(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "e.txt"
            path.write_text("x" * (board.EVIDENCE_EXCERPT_CHARACTERS + 5000) + "\nFINAL CHECK: PASS claim audit\n")
            self.assertNotIn("FINAL CHECK", board._evidence_excerpt(path))
            self.assertEqual(["FINAL CHECK: PASS claim audit"], certified_execution.final_check_summary(board._evidence_text(path)))
        self.assertEqual("", board._evidence_text(None))


class DirectivePinTests(unittest.TestCase):
    RULE_A = 'A project\'s own status flag (passed, "warnings only") is a claim, never proof: read the run\'s final-check output and say what it says; none means unproven.'

    def test_the_cto_and_its_mirror_carry_the_rule(self):
        self.assertIn(self.RULE_A, flat("directives/CTO.md"))
        self.assertIn(self.RULE_A, flat("harness/directives/CTO_COMPLETION_DIRECTIVE.md"))

    def test_the_reviewer_text_makes_a_soft_pass_gate_material_and_routes_it_without_a_reviewer_card(self):
        agent = flat("directives/AGENT.md")
        for part in ("is a claim, never proof", "read the final-check output that run itself produced", "if that output is absent or unreadable, the claim is unproven",
                     "A project gate that can reach a passed state without its final check having run or succeeded is a MATERIAL finding",
                     "quote the approval and the termination point, and name the required check that is absent or failed",
                     "The CTO routes the repair to Delivery as for any in-scope finding", "never created by the Reviewer",
                     "repeat the same scenario against the corrected candidate; the original run stays as separate evidence of the defect",
                     "a row without them is unproven"):
            with self.subTest(part=part):
                self.assertIn(part, agent)

    def test_delivery_is_told_the_convention(self):
        agent = flat("directives/AGENT.md")
        self.assertIn("prints one `FINAL CHECK: <PASS|FAIL|NOT RUN|WARNINGS ONLY|SKIPPED> <name>` line per such gate, the TEST's verdict on that check", agent)
        self.assertIn("a negative test that sees the gate correctly refuse prints PASS", agent)
        self.assertIn("refuses the run unless every such line is PASS, whatever the exit code", agent)

if __name__ == "__main__":
    unittest.main()

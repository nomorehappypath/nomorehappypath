# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""P2 execution-identity sims (item 4; owner bar: every field mutated).

Run:  PYTHONPATH=. python3 -m unittest tests.test_execution_identity -v
"""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harness import board, execution_identity as xid


class ExecutionIdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.candidate = xid.candidate_evidence_identity(
            "c" * 40, "t" * 40, "rev-3", {"delivery_ledger": "a" * 64})
        self.run = xid.command_run_identity(
            self.candidate, ["python3", "-m", "unittest", "tests.suite"],
            "tests", "e" * 64, {"requirements.lock": "l" * 64})

    def _certify_pass(self, identity=None):
        return xid.certify(self.root, identity or self.run, exit_code=0,
                           output_sha256="o" * 64, duration_seconds=12.5)

    # ---- S-P2-001: exact match hits; the certified result is returned ----
    def test_exact_match_hits(self):
        self._certify_pass()
        decision = xid.lookup(self.root, self.run)
        self.assertEqual(decision["status"], "hit")
        self.assertEqual(decision["entry"]["duration_seconds"], 12.5)

    # ---- S-P2-002 (THE core bar): mutating EVERY identity field individually misses ----
    def test_every_field_mutation_misses_and_names_the_field(self):
        self._certify_pass()
        mutations = {
            "candidate commit": xid.candidate_evidence_identity(
                "X" * 40, "t" * 40, "rev-3", {"delivery_ledger": "a" * 64}),
            "candidate tree": xid.candidate_evidence_identity(
                "c" * 40, "X" * 40, "rev-3", {"delivery_ledger": "a" * 64}),
            "contract revision": xid.candidate_evidence_identity(
                "c" * 40, "t" * 40, "rev-4", {"delivery_ledger": "a" * 64}),
            "artifact digest": xid.candidate_evidence_identity(
                "c" * 40, "t" * 40, "rev-3", {"delivery_ledger": "B" * 64}),
        }
        for name, candidate in mutations.items():
            run = xid.command_run_identity(
                candidate, ["python3", "-m", "unittest", "tests.suite"],
                "tests", "e" * 64, {"requirements.lock": "l" * 64},
                runtime=self.run["fields"]["runtime"])
            decision = xid.lookup(self.root, run)
            self.assertEqual(decision["status"], "miss", f"{name} must invalidate")
            self.assertIn("candidate_sha256", decision["diverged_fields"],
                          f"{name}: the audit must name the diverged lineage")
        run_mutations = {
            "argv": (["python3", "-m", "pytest", "tests.suite"], "tests", "e" * 64, {"requirements.lock": "l" * 64}),
            "cwd": (["python3", "-m", "unittest", "tests.suite"], "other", "e" * 64, {"requirements.lock": "l" * 64}),
            "environment_sha256": (["python3", "-m", "unittest", "tests.suite"], "tests", "F" * 64, {"requirements.lock": "l" * 64}),
            "lockfiles": (["python3", "-m", "unittest", "tests.suite"], "tests", "e" * 64, {"requirements.lock": "M" * 64}),
        }
        for field, (argv, cwd, env, locks) in run_mutations.items():
            run = xid.command_run_identity(self.candidate, argv, cwd, env, locks,
                                           runtime=self.run["fields"]["runtime"])
            decision = xid.lookup(self.root, run)
            self.assertEqual(decision["status"], "miss", f"{field} must invalidate")
            self.assertIn(field, decision["diverged_fields"],
                          f"the audit must name '{field}' exactly")
        # runtime + policy version
        run = xid.command_run_identity(self.candidate,
                                       ["python3", "-m", "unittest", "tests.suite"],
                                       "tests", "e" * 64, {"requirements.lock": "l" * 64},
                                       runtime={"python": "9.9.9"})
        decision = xid.lookup(self.root, run)
        self.assertEqual(decision["status"], "miss")
        self.assertIn("runtime", decision["diverged_fields"])

    # ---- S-P2-003: failures are stored but NEVER returned as reusable ----
    def test_failed_execution_is_never_reused(self):
        xid.certify(self.root, self.run, exit_code=1,
                    output_sha256="f" * 64, duration_seconds=3.0)
        decision = xid.lookup(self.root, self.run)
        self.assertEqual(decision["status"], "miss")
        self.assertEqual(decision["reason"], "prior_execution_failed")

    # ---- S-P2-004: a certified success is reused, never overwritten ----
    def test_successful_recertification_is_idempotent_and_preserves_first_output(self):
        first = self._certify_pass()
        again = xid.certify(self.root, self.run, exit_code=0,
                            output_sha256="DIFFERENT" + "o" * 55, duration_seconds=1.0)
        self.assertEqual(again["status"], "already_certified")
        self.assertEqual(again["entry"]["output_sha256"], first["entry"]["output_sha256"])

    # ---- S-P2-005: shell strings are structurally rejected ----
    def test_shell_string_argv_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "structured list"):
            xid.command_run_identity(self.candidate, "python3 -m unittest suite",  # type: ignore[arg-type]
                                    "tests", "e" * 64)

    # ---- S-P2-006: recorded_at never participates in identity ----
    def test_recorded_at_is_not_identity(self):
        first = self._certify_pass()
        self.assertNotIn("recorded_at", self.run["fields"])
        self.assertNotIn("recorded_at", self.candidate["fields"])
        # Same identity certified later (identical result) stays one identity.
        again = self._certify_pass()
        self.assertEqual(again["entry"]["identity_sha256"],
                         first["entry"]["identity_sha256"])

    # ---- S-P2-007: every decision is an auditable board event ----
    def test_decisions_are_audited_on_the_board(self):
        self._certify_pass()
        hit = xid.lookup(self.root, self.run)
        xid.audit_decision(self.root, self.run, hit)
        other = xid.command_run_identity(self.candidate, ["python3", "-m", "unittest", "x"],
                                         "tests", "e" * 64)
        miss = xid.lookup(self.root, other)
        xid.audit_decision(self.root, other, miss)
        events = [e for e in board.snapshot(self.root).get("events", [])
                  if e.get("kind") == "execution_reuse_decision"]
        self.assertEqual(len(events), 2)
        self.assertIn("argv", events[-1].get("message", ""),
                      "the audit event carries the diverged fields")

    def test_concurrent_certification_is_one_append_only_record(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: self._certify_pass(), range(24)))
        self.assertEqual(
            sum(item["status"] == "certified" for item in outcomes), 1,
            "concurrent reviewers must not append duplicate certifications",
        )
        records = (board.board_dir(self.root) / xid.STORE_NAME).read_text().splitlines()
        self.assertEqual(len(records), 1)

    def test_failed_then_passed_retry_is_linked_disclosed_and_then_reused(self):
        failed = xid.certify(
            self.root, self.run, exit_code=1,
            output_sha256="f" * 64, duration_seconds=3.0,
        )
        with self.assertRaisesRegex(ValueError, "explicit retry reason"):
            self._certify_pass()
        passed = xid.certify(
            self.root, self.run, exit_code=0,
            output_sha256="p" * 64, duration_seconds=2.0,
            retry_reason="The dependency outage was repaired.",
        )
        self.assertEqual(passed["entry"]["retry_of"], failed["entry"]["record_id"])
        self.assertEqual(xid.lookup(self.root, self.run)["status"], "hit")
        reused = xid.certify(
            self.root, self.run, exit_code=0,
            output_sha256="ignored" * 9, duration_seconds=1.0,
        )
        self.assertEqual(reused["status"], "already_certified")
        self.assertEqual(len((board.board_dir(self.root) / xid.STORE_NAME).read_text().splitlines()), 2)

    def test_role_gate_and_browser_identity_cannot_cross_certify(self):
        delivery = xid.command_run_identity(
            self.candidate, ["python3", "-m", "unittest", "tests.suite"],
            "tests", "e" * 64, role="delivery", gate="final_acceptance",
            browser={"digest": "b" * 64, "headless": "true"},
        )
        self._certify_pass(delivery)
        reviewer = xid.command_run_identity(
            self.candidate, ["python3", "-m", "unittest", "tests.suite"],
            "tests", "e" * 64, role="reviewer", gate="final_acceptance",
            browser={"digest": "b" * 64, "headless": "true"},
        )
        decision = xid.lookup(self.root, reviewer)
        self.assertEqual(decision["status"], "miss")
        self.assertIn("role", decision["diverged_fields"])
        changed_browser = xid.command_run_identity(
            self.candidate, ["python3", "-m", "unittest", "tests.suite"],
            "tests", "e" * 64, role="delivery", gate="final_acceptance",
            browser={"digest": "c" * 64, "headless": "true"},
        )
        self.assertIn("browser", xid.lookup(self.root, changed_browser)["diverged_fields"])

    def test_torn_or_corrupt_store_fails_closed(self):
        path = board.board_dir(self.root) / xid.STORE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"identity_sha256":"partial"}')
        with self.assertRaisesRegex(ValueError, "torn partial"):
            xid.lookup(self.root, self.run)
        path.write_bytes(b"not-json\n")
        with self.assertRaisesRegex(ValueError, "record 1 is corrupt"):
            xid.lookup(self.root, self.run)

    def test_reusable_output_is_content_addressed_and_tamper_evident(self):
        output = "Ran 1 test in 0.001s\n\nOK"
        digest = __import__("hashlib").sha256(output.encode()).hexdigest()
        certified = xid.certify(
            self.root, self.run, exit_code=0, output_sha256=digest,
            duration_seconds=0.1, output=output,
        )
        self.assertEqual(xid.load_output(certified["entry"]), output)
        Path(certified["entry"]["output_path"]).write_text("tampered")
        with self.assertRaisesRegex(ValueError, "digest is corrupt"):
            xid.load_output(certified["entry"])


if __name__ == "__main__":
    unittest.main()

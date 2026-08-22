# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""P4 delta-aware repair sims (item 6; owner bar: no happy path).

Run:  PYTHONPATH=. python3 -m unittest tests.test_repair_context -v
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control, execution_identity as xid
from tests.requirements_support import agreed_requirements


HEADER = ("| ID | What was tested | Scenario | Simulation command | Expected system response | "
          "Observed system response | QA result |\n|---|---|---|---|---|---|---|\n")


class RepairContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "h@x.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "H"], cwd=self.root, check=True)
        (self.root / ".gitignore").write_text(".harness/\nevidence/\n*.md\nproof.txt\n")
        (self.root / "test_smoke.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n"
            "    def test_passes(self): self.assertTrue(True)\n")
        (self.root / "module.py").write_text("VALUE = 1\n")
        subprocess.run(["git", "add", ".gitignore", "test_smoke.py", "module.py"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.root, check=True)

    def _workspace(self) -> Path:
        state = board.snapshot(self.root)
        return Path(state["task_workspaces"]["TASK-R"])

    def _commit(self, message):
        ws = self._workspace()
        subprocess.run(["git", "add", "-u"], cwd=ws, check=True)
        subprocess.run(["git", "-c", "user.email=h@x.invalid", "-c", "user.name=H",
                        "commit", "-qm", message], cwd=ws, check=True)

    def _delivery(self):
        contract.create_contract(self.root, "TASK-R", "Ship repair flow", ["delivery"])
        proof = self.root / "proof.txt"
        proof.write_text("proven\n")
        contract.add_evidence(self.root, "TASK-R", "delivery", [proof])
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                             vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "Ship repair flow")
        board.begin_task(self.root, dev["id"], "TASK-R")
        agreed_requirements(
            self.root, dev["id"], "Final agreed requirements: ship the repair flow with evidence.")
        board.define_delivery_plan(self.root, dev["id"], self._mode, "One cohesive task")
        return dev

    _mode = "atomic"

    def _request(self, dev, changes=""):
        ledger = self.root / "ledger.md"
        ledger.write_text(HEADER + "| S-001 | The repaired behavior remains correct through the next acceptance attempt. | flow | `python3 -m unittest test_smoke` | Holds | PASS: executed | PASS |\n")
        return board.request_review(
            self.root, dev["id"], str(ledger), "review the flow",
            phase="final_acceptance", test_command="python3 -m unittest test_smoke",
            changes=changes)

    def _fail(self, request):
        qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        challenge = self.root / f"challenge-{request['cycle']}.md"
        challenge.write_text(HEADER + "| S-101 | An independent adversarial check confirms the repair closes the prior failure without regression. | adversarial | `python3 -m unittest test_smoke -k passes` | Holds | PASS: executed | PASS |\n")
        board.claim_qa(self.root, qa["id"], request["id"], str(challenge))
        evidence = self.root / "evidence"
        evidence.mkdir(exist_ok=True)
        review_evidence = evidence / f"review-{request['cycle']}.txt"
        review_evidence.write_text("command: python3 -m unittest\nresult: FAIL\n")
        board.qa_result(self.root, qa["id"], request["id"], "failed",
                        "BLOCKING: unguarded exception on hostile shape in module.py",
                        str(review_evidence))

    # ---- S-P4-001: cycle 2 carries the delta — findings, exact diff, prior ledger ----
    def test_repair_cycle_carries_exact_delta_context(self):
        dev = self._delivery()
        first = self._request(dev)
        self._fail(first)
        # The repair changes exactly one file.
        (self._workspace() / "module.py").write_text("VALUE = 2  # guarded\n")
        self._commit("repair the hostile-shape guard")
        second = self._request(dev, changes="guarded the hostile shape")
        context = second.get("repair_context")
        self.assertIsNotNone(context, "cycle 2 MUST carry the repair context")
        self.assertEqual(context["prior_request_id"], first["id"])
        self.assertIn("unguarded exception", context["prior_blocking_summary"])
        self.assertTrue(context["diff_available"])
        self.assertEqual(context["diff_files"], ["module.py"],
                         "the diff names exactly what changed since the failed cycle")
        self.assertTrue(str(context["prior_challenge_ledger"]).endswith("challenge-1.md"))
        prefill = context["challenge_prefill"]
        self.assertEqual(prefill["source_request_id"], first["id"])
        self.assertEqual(len(prefill["source_ledger_sha256"]), 64)
        self.assertEqual(prefill["unavailable_reasons"], [])
        self.assertEqual(
            prefill["mechanical_rows"],
            [{
                "prior_scenario_id": "S-101",
                "simulation_command": "python3 -m unittest test_smoke -k passes",
                "prior_execution_identity": "",
                "rerun_required": True,
            }],
        )
        self.assertNotIn("expected_response", prefill["mechanical_rows"][0])
        self.assertNotIn("result", prefill["mechanical_rows"][0])
        self.assertIn("semantic verdict", prefill["reviewer_must_author"])

    # ---- S-P4-002: a first cycle carries NO repair context ----
    def test_first_cycle_has_no_repair_context(self):
        dev = self._delivery()
        first = self._request(dev)
        self.assertIsNone(first.get("repair_context"))

    # ---- S-P4-003: an impacted scenario cannot ride a stale certification ----
    def test_changed_candidate_misses_prior_execution_identity(self):
        dev = self._delivery()
        first = self._request(dev)
        self._fail(first)
        (self._workspace() / "module.py").write_text("VALUE = 3\n")
        self._commit("repair")
        second = self._request(dev, changes="repaired")
        old_candidate = xid.candidate_evidence_identity(
            str(first.get("reviewed_commit")), "tree-a", "1", {})
        new_candidate = xid.candidate_evidence_identity(
            str(second.get("reviewed_commit")), "tree-b", "2", {})
        run_old = xid.command_run_identity(old_candidate, ["python3", "-m", "unittest"], ".", "")
        xid.certify(self.root, run_old, exit_code=0, output_sha256="o" * 64, duration_seconds=1.0)
        run_new = xid.command_run_identity(new_candidate, ["python3", "-m", "unittest"], ".", "")
        decision = xid.lookup(self.root, run_new)
        self.assertEqual(decision["status"], "miss",
                         "a changed candidate structurally misses the stale certification")
        self.assertIn("candidate_sha256", decision["diverged_fields"])

    # ---- S-P4-004: diff failure degrades honestly, never blocks the resubmission ----
    def test_unavailable_diff_is_marked_not_fatal(self):
        dev = self._delivery()
        first = self._request(dev)
        self._fail(first)
        (self._workspace() / "module.py").write_text("VALUE = 4\n")
        self._commit("repair")
        # Corrupt the prior commit reference so the diff cannot compute.
        with board.locked_state(self.root) as state:
            for request in state["qa_requests"].values():
                if request.get("id") == first["id"]:
                    request["reviewed_commit"] = "f" * 40
        second = self._request(dev, changes="repaired with unavailable diff")
        context = second.get("repair_context")
        self.assertIsNotNone(context)
        self.assertFalse(context["diff_available"], "an uncomputable diff is marked, not faked")
        self.assertIsNone(context["diff_files"])


if __name__ == "__main__":
    unittest.main()


class TestScopeInvariantTests(RepairContextTests):
    """Item 7 (P3): scope invariants are mechanical and unremovable."""

    _mode = "chunked"

    def _delivery(self):
        dev = super()._delivery()
        board.declare_chunks(self.root, dev["id"], [("part-1", "the first declared chunk")])
        return dev

    # The parent's repair-context tests run once in their own class; they are
    # not re-collected here (a chunked plan changes their preconditions).
    test_repair_cycle_carries_exact_delta_context = None
    test_first_cycle_has_no_repair_context = None
    test_changed_candidate_misses_prior_execution_identity = None
    test_unavailable_diff_is_marked_not_fatal = None

    # ---- S-P3-001: final acceptance REFUSES any narrowed scope ----
    def test_final_acceptance_refuses_narrowed_scope(self):
        dev = self._delivery()
        ledger = self.root / "ledger.md"
        ledger.write_text(HEADER + "| S-001 | The complete release candidate is checked before final acceptance. | flow | `python3 -m unittest test_smoke` | Holds | PASS: executed | PASS |\n")
        for scope in ("focused", "affected", "integration", "health"):
            with self.assertRaisesRegex(ValueError, "FULL suite"):
                board.request_review(
                    self.root, dev["id"], str(ledger), "narrowed final",
                    phase="final_acceptance", test_command="python3 -m unittest test_smoke",
                    test_scope=scope, scope_reason="attempted narrowing")

    # ---- S-P3-002: a narrowed scope without a recorded reason is refused ----
    def test_narrowed_scope_without_reason_is_refused(self):
        dev = self._delivery()
        ledger = self.root / "ledger.md"
        ledger.write_text(HEADER + "| S-001 | A narrowed review cannot proceed without an explanation of its coverage. | flow | `python3 -m unittest test_smoke` | Holds | PASS: executed | PASS |\n")
        with self.assertRaisesRegex(ValueError, "scope-reason"):
            board.request_review(
                self.root, dev["id"], str(ledger), "affected without reason",
                phase="chunk", chunk="part-1",
                test_command="python3 -m unittest test_smoke",
                test_scope="affected")

    # ---- S-P3-003: scope + reason are recorded on the request for the reviewer ----
    def test_scope_and_reason_are_recorded(self):
        dev = self._delivery()
        ledger = self.root / "ledger.md"
        ledger.write_text(HEADER + "| S-001 | The review records why its selected coverage is sufficient for the change. | flow | `python3 -m unittest test_smoke` | Holds | PASS: executed | PASS |\n")
        request = board.request_review(
            self.root, dev["id"], str(ledger), "affected with basis",
            phase="chunk", chunk="part-1",
            test_command="python3 -m unittest test_smoke",
            test_scope="affected", scope_reason="single-module change; coverage map current as of last full run")
        self.assertEqual(request["test_scope"], "affected")
        self.assertIn("coverage map", request["scope_reason"])

    # ---- S-P3-004: the default remains the full suite — nothing narrows silently ----
    def test_default_scope_is_full(self):
        dev = self._delivery()
        ledger = self.root / "ledger.md"
        ledger.write_text(HEADER + "| S-001 | Reviews use the complete test scope when no narrower scope is requested. | flow | `python3 -m unittest test_smoke` | Holds | PASS: executed | PASS |\n")
        request = board.request_review(
            self.root, dev["id"], str(ledger), "default scope chunk",
            phase="chunk", chunk="part-1",
            test_command="python3 -m unittest test_smoke")
        self.assertEqual(request["test_scope"], "full")
        self.assertEqual(request["scope_reason"], "")

    # ---- S-P3-005: an invalid scope name is refused outright ----
    def test_invalid_scope_is_refused(self):
        dev = self._delivery()
        ledger = self.root / "ledger.md"
        ledger.write_text(HEADER + "| S-001 | An unsupported review scope is rejected instead of silently reducing coverage. | flow | `python3 -m unittest test_smoke` | Holds | PASS: executed | PASS |\n")
        with self.assertRaisesRegex(ValueError, "test scope must be one of"):
            board.request_review(
                self.root, dev["id"], str(ledger), "bogus scope",
                phase="chunk", chunk="part-1",
                test_command="python3 -m unittest test_smoke",
                test_scope="minimal", scope_reason="x")

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Claim-scope release-gate simulations (finding-57a0fe813e3ee545).

Defect: the release check computed claim_scope_audit_passed by TERM OVERLAP
between the owner direction and contract/confirmation text. A pointer-style
directive (a file path plus "read it fully" — the sanctioned pattern for long
directives) fails that lexical test mechanically, blocking a fully verified
release with no agent-executable repair, since the direction is owner-authored.
The CTO directive already rules that term overlap is never evidence; the code
now agrees: word gaps are ADVISORY input to the CTO's artifact-traced audit,
and the enforceable mechanical facts are a recorded owner direction plus a
present final integrated acceptance — whose delivery-mode audit is the executed
claim-scope audit.

Run:  PYTHONPATH=. python3 -m unittest tests.test_claim_scope_release_gate -v
"""
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control, cto

LEDGER = (
    "| ID | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
    "|---|---|---|---|---|---|\n"
    "| S-001 | scope | `python3 -m unittest test_scope` | Delivered behavior matches the directive file | PASS: executed | PASS |\n"
)

POINTER_DIRECTION = (
    "TASK B — PAUSE/RESUME. The complete owner directive is the file "
    "/Users/owner/projects/widget/docs/directives/TASK_B_PHASE2_PAUSE_RESUME.md "
    "in the target repository. Read the whole file first; every requirement is acceptance-gated."
)


class ClaimScopeReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _task(self, task: str, direction: str, confirmation: str):
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                             vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], direction)
        board.begin_task(self.root, dev["id"], task)
        board.record_requirement_confirmation(self.root, dev["id"], confirmation)
        contract.create_contract(self.root, task, confirmation, ["delivery"])
        ledger = self.root / f"{task}-ledger.md"
        ledger.write_text(LEDGER)
        return ledger

    def _final_acceptance(self, task: str, status: str = "passed"):
        with board.locked_state(self.root) as state:
            state["qa_requests"][f"final-{task}"] = {
                "id": f"final-{task}", "task": task, "status": status,
                "phase": "final_acceptance", "cycle": 1, "stage": "independent_review",
                    "subtask": "", "chunk": "", "developer_id": "dev", "claimed_by": "", "review_wait_started_at": "",
                "structure_revision": 0, "requested_at": board.now(),
            }

    # ---- S-CLAIM-001: the exact Task B block — pointer directive releases ----
    def test_pointer_directive_with_final_acceptance_passes_with_advisory_gaps(self):
        ledger = self._task(
            "TASK-POINTER", POINTER_DIRECTION,
            "Final agreed requirements: non-destructive pause, exact resume, "
            "mechanical evidence-reuse identity checks; carried-forward closures included.")
        self._final_acceptance("TASK-POINTER")
        checks = cto.release_check(self.root, "TASK-POINTER", ledger, self.root)
        self.assertTrue(checks["claim_scope_audit_passed"],
                        "a verified release is never blocked by word overlap")
        self.assertTrue(checks["claim_scope_missing_terms"],
                        "the advisory gap list survives for the CTO's artifact-traced audit")

    # ---- S-CLAIM-002: perfect word overlap cannot substitute for the audit ----
    def test_full_overlap_without_final_acceptance_does_not_pass(self):
        direction = "Build the resilient widget with restart recovery"
        ledger = self._task("TASK-ECHO", direction, "Final agreed requirements: " + direction)
        checks = cto.release_check(self.root, "TASK-ECHO", ledger, self.root)
        self.assertEqual(checks["claim_scope_missing_terms"], [])
        self.assertFalse(checks["claim_scope_audit_passed"],
                         "echoed vocabulary is never evidence; the executed final audit is")

    # ---- S-CLAIM-003: no owner direction still refuses ----
    def test_no_owner_direction_refuses(self):
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                             vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "placeholder direction for begin")
        board.begin_task(self.root, dev["id"], "TASK-NO-DIRECTION")
        with board.locked_state(self.root) as state:
            # A direction survives in the append-only event log by design, so a
            # genuinely direction-less board (legacy import) must clear all three.
            state.get("task_owner_directions", {}).pop("TASK-NO-DIRECTION", None)
            state.get("owner_directions", {}).pop(session["id"], None)
            state["events"] = []
        contract.create_contract(self.root, "TASK-NO-DIRECTION", "objective", ["delivery"])
        ledger = self.root / "nd-ledger.md"
        ledger.write_text(LEDGER)
        self._final_acceptance("TASK-NO-DIRECTION")
        checks = cto.release_check(self.root, "TASK-NO-DIRECTION", ledger, self.root)
        self.assertFalse(checks["claim_scope_audit_passed"])


if __name__ == "__main__":
    unittest.main()

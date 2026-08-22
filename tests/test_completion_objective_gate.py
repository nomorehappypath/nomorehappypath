# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Completion-gate objective simulations (finding-4189610bc047219c).

Defect: board.complete refused unless the Completion Contract objective was
byte-equal (normalized) to the recorded owner direction. The contract format
itself asks for a translated product statement, and pointer-style directives
are file paths — so every properly-run task with a translated objective was
mechanically blocked at completion. The sanctioned record of the translation
is the requirements confirmation, fixed before delivery begins and audited at
final acceptance; with it recorded, a translated objective completes. Without
it, byte-equality is still demanded — the drift protection survives.

Run:  PYTHONPATH=. python3 -m unittest tests.test_completion_objective_gate -v
"""
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control
from tests.requirements_support import agreed_requirements


DIRECTION = ("TASK B — PAUSE/RESUME. The complete owner directive is the file "
             "docs/directives/TASK_B.md in the target repository. Read the whole file first.")
TRANSLATED = ("Deliver complete non-destructive project pause and exact resume with "
              "mechanical evidence-integrity checks that reuse only unchanged proof.")


class CompletionObjectiveGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _run_task(self, task: str, direction: str, objective: str, confirm: bool):
        root = self.root
        contract.create_contract(root, task, objective, ["delivery"])
        proof = root / f"{task}-proof.txt"
        proof.write_text("delivery proven\n")
        contract.add_evidence(root, task, "delivery", [proof])
        header = ("| ID | What was tested | Scenario | Simulation command | Expected system response | "
                  "Observed system response | QA result |\n|---|---|---|---|---|---|---|\n")
        (root / "test_smoke.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(True)\n")
        ledger = root / f"{task}-ledger.md"
        ledger.write_text(header + "| S-001 | The agreed task behavior is delivered before completion is recorded. | scope | `python3 -m unittest test_smoke` | Behavior delivered | PASS: executed | PASS |\n")
        challenge = root / f"{task}-challenge.md"
        challenge.write_text(header + "| S-101 | An independent check confirms completion still requires the agreed task behavior. | challenge | `python3 -m unittest test_smoke -k passes` | Holds | PASS: executed | PASS |\n")
        evidence = root / "evidence"
        evidence.mkdir(exist_ok=True)
        review_evidence = evidence / f"{task}-review.txt"
        review_evidence.write_text("command: python3 -m unittest\nresult: PASS\n")
        session = control.create(root, "codex_delivery")
        dev = board.register(root, "development", board.AWAITING_OWNER_DIRECTION,
                             vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(root, session["id"], direction)
        board.begin_task(root, dev["id"], task)
        # The sanctioned flow always records a confirmation before planning;
        # the confirm=False variants strip it afterwards to simulate a
        # legacy/imported board, which is the only way that state can exist.
        agreed_requirements(
            root, dev["id"], "Final agreed requirements: " + objective)
        board.define_delivery_plan(root, dev["id"], "atomic", "One cohesive task")
        qa = board.register(root, "qa", "QA-QUEUE", vendor="Anthropic")
        review = board.request_review(
            root, dev["id"], str(ledger), "review independently",
            phase="final_acceptance", test_command="python3 -m unittest test_smoke")
        board.claim_qa(root, qa["id"], review["id"], str(challenge))
        board.execute_challenge(root, qa["id"], review["id"])
        board.qa_result(root, qa["id"], review["id"], "passed", "review pass", str(review_evidence))
        if not confirm:
            with board.locked_state(root) as state:
                state.get("requirement_confirmations", {}).pop(task, None)
        return dev

    # ---- S-COMP-001: translated objective + recorded confirmation completes ----
    def test_translated_objective_with_confirmation_completes(self):
        dev = self._run_task("TASK-TRANSLATED", DIRECTION, TRANSLATED, confirm=True)
        event = board.complete(self.root, dev["id"], "delivered per confirmed translation")
        self.assertEqual(event["kind"], "development_complete")

    # ---- S-COMP-002: translated objective without confirmation is refused ----
    def test_translated_objective_without_confirmation_is_refused(self):
        dev = self._run_task("TASK-DRIFT", DIRECTION, TRANSLATED, confirm=False)
        with self.assertRaisesRegex(ValueError, "requirements confirmation"):
            board.complete(self.root, dev["id"], "attempting unconfirmed translation")

    # ---- S-COMP-003: verbatim objective still completes without confirmation ----
    def test_verbatim_objective_completes_without_confirmation(self):
        dev = self._run_task("TASK-VERBATIM", "Ship the widget", "Ship the widget", confirm=False)
        event = board.complete(self.root, dev["id"], "verbatim objective delivered")
        self.assertEqual(event["kind"], "development_complete")


if __name__ == "__main__":
    unittest.main()

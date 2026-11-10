# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""board.py wiring for the pre-spec question card and the SPEC.md Targets carry-forward
(write-specs-technical-chapters R2, R7)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control
from tests.requirements_support import agreed_requirements
from tests.test_requirements_contract import context, delivery_with_direction


class ProposeSpecQuestionsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))

    def test_delivery_proposes_and_the_owner_answers(self):
        agent = delivery_with_direction(self.root, "TASK-SPEC")
        board.propose_spec_questions(
            self.root, agent["id"], "TASK-SPEC",
            [{"text": "Which marketplace do you sell on?", "options": ["eBay", "Etsy", "Both"], "recommended": 2}],
            include_existing_data=True, include_data_loss=True,
        )
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertEqual(len(bundle["spec_questions"]["app_questions"]), 1)
        self.assertTrue(bundle["spec_questions"]["include_existing_data"])
        self.assertIsNone(bundle["spec_answers"])

        board.record_spec_answers(self.root, "TASK-SPEC", {"how_used": "Web page, hosted"})
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertEqual(bundle["spec_answers"]["answers"]["how_used"], ["Web page, hosted"])

    def test_a_second_proposal_for_the_same_task_is_refused(self):
        agent = delivery_with_direction(self.root, "TASK-SPEC2")
        board.propose_spec_questions(self.root, agent["id"], "TASK-SPEC2", [], include_existing_data=False, include_data_loss=False)
        with self.assertRaises(ValueError):
            board.propose_spec_questions(self.root, agent["id"], "TASK-SPEC2", [], include_existing_data=False, include_data_loss=False)

    def test_answers_without_a_proposed_card_are_refused(self):
        with self.assertRaises(ValueError):
            board.record_spec_answers(self.root, "TASK-NEVER-PROPOSED", {})


class TaskSpecStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))

    def test_no_spec_md_means_not_accepted(self):
        agent = delivery_with_direction(self.root, "TASK-NO-SPEC")
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertFalse(bundle["project_spec"]["accepted"])

    def test_an_existing_accepted_spec_md_is_snapshotted_as_accepted(self):
        # Review r1 B3: file presence alone is no longer enough — a real recorded Accept of a
        # "spec" task is required too (see test_a_draft_spec_md_with_no_recorded_accept_is_not_treated_as_accepted).
        (self.root / "SPEC.md").write_text("# Spec\n")
        with board.locked_state(self.root) as state:
            state.setdefault("task_kinds", {})["SOME-SPEC-TASK"] = "spec"
            state.setdefault("release_decisions", {})["SOME-SPEC-TASK"] = {"decision": "accepted"}
        agent = delivery_with_direction(self.root, "TASK-WITH-SPEC")
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertTrue(bundle["project_spec"]["accepted"])

    def test_a_spec_md_added_after_begin_never_retargets_a_running_task(self):
        agent = delivery_with_direction(self.root, "TASK-RUNNING")
        (self.root / "SPEC.md").write_text("# Spec\n")
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertFalse(bundle["project_spec"]["accepted"], "a task already running keeps the snapshot it began with")

    def test_a_draft_spec_md_with_no_recorded_accept_is_not_treated_as_accepted(self):
        """Review r1 B3: a SPEC.md merely present on disk (a draft from a "spec" task still in
        progress, with no owner Accept recorded for it yet) must never count."""
        (self.root / "SPEC.md").write_text("# Spec\n")
        with board.locked_state(self.root) as state:
            state.setdefault("task_kinds", {})["SOME-SPEC-TASK"] = "spec"
            # Deliberately no release_decisions entry: this task is still in progress, not accepted.
        agent = delivery_with_direction(self.root, "TASK-DRAFT-SPEC")
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertFalse(bundle["project_spec"]["accepted"])

    def test_a_spec_md_with_a_real_recorded_accept_is_treated_as_accepted(self):
        (self.root / "SPEC.md").write_text("# Spec\n")
        with board.locked_state(self.root) as state:
            state.setdefault("task_kinds", {})["SOME-SPEC-TASK"] = "spec"
            state.setdefault("release_decisions", {})["SOME-SPEC-TASK"] = {"decision": "accepted"}
        agent = delivery_with_direction(self.root, "TASK-ACCEPTED-SPEC")
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertTrue(bundle["project_spec"]["accepted"])

    def test_an_accepted_non_spec_task_never_counts(self):
        """An accepted "development"/"analysis" task must never be mistaken for a spec accept,
        even if a file named SPEC.md happens to exist for some unrelated reason."""
        (self.root / "SPEC.md").write_text("# Spec\n")
        with board.locked_state(self.root) as state:
            state.setdefault("task_kinds", {})["SOME-DEV-TASK"] = "development"
            state.setdefault("release_decisions", {})["SOME-DEV-TASK"] = {"decision": "accepted"}
        agent = delivery_with_direction(self.root, "TASK-UNRELATED-ACCEPT")
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertFalse(bundle["project_spec"]["accepted"])


class AppSpecCompletionGateTests(unittest.TestCase):
    """Owner repair 2026-11-09 (round 2), item 2: completing an app-spec task must be refused
    by the board itself, not only by the earlier propose-requirements gate, so no other path
    (a repair, a retained plan) can ever reach complete() for a card the owner never answered."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _run_app_spec_task(self, task: str):
        root = self.root
        objective = "Build the trading app's hand-in."
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
        board.record_owner_direction(root, session["id"], "A trading app for my own brokerage account.")
        board.begin_task(root, dev["id"], task)
        with board.locked_state(root) as state:
            state.setdefault("task_kinds", {})[task] = "spec"   # a "Write specs" app task
        board.propose_spec_questions(root, dev["id"], task, [], include_existing_data=False, include_data_loss=False)
        board.record_spec_answers(root, task, {})   # the owner pressed Continue with none
        agreed_requirements(root, dev["id"], "Final agreed requirements: " + objective)
        board.define_delivery_plan(root, dev["id"], "atomic", "One cohesive task")
        qa = board.register(root, "qa", "QA-QUEUE", vendor="Anthropic")
        review = board.request_review(
            root, dev["id"], str(ledger), "review independently",
            phase="final_acceptance", test_command="python3 -m unittest test_smoke")
        board.claim_qa(root, qa["id"], review["id"], str(challenge))
        board.execute_challenge(root, qa["id"], review["id"])
        board.qa_result(root, qa["id"], review["id"], "passed", "TASK DONE: YES - review pass", str(review_evidence))
        return dev, task

    def test_completion_is_refused_when_the_card_answer_is_missing_and_succeeds_once_restored(self):
        dev, task = self._run_app_spec_task("TASK-SPEC-COMPLETE-GATE")
        with board.locked_state(self.root) as state:
            removed = state.get("spec_answers", {}).pop(task)
        with self.assertRaisesRegex(ValueError, "question card was never answered"):
            board.complete(self.root, dev["id"], "attempting completion without an answered card")
        with board.locked_state(self.root) as state:
            state.setdefault("spec_answers", {})[task] = removed
        event = board.complete(self.root, dev["id"], "completion with the card answered")
        self.assertEqual(event["kind"], "development_complete")


if __name__ == "__main__":
    unittest.main()

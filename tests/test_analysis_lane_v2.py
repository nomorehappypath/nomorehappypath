# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Code analysis lane v2 (task 2026-10-28_analysis-lane-v2; the one-shot discovery was replaced by the two-pass flow on 2026-10-29): the hand-in names
its flows and helpers, the verdict counts the clean list, disputes are re-checked once, and an analysis has two review rounds.

Run:  PYTHONPATH=. python3 -m unittest tests.test_analysis_lane_v2 -v
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from harness import board, review_brief, review_rounds, standing_checks
from tests import test_frozen_review_scope as frozen

ISSUE = "app/services/ocr.py:120|a restarted upload job runs the paid scan again|the customer is billed twice for one document"
ISSUE_ID = "app/api/draft.py:80|the draft helper never checks the license state|a frozen customer keeps spending AI budget"


class Fixture(frozen.Rounds):
    """The real review board (a temporary git project); the inherited round tests are switched off here."""

    def analysis_request(self):
        request = self.request()
        with board.locked_state(self.root) as state:
            state.setdefault("task_kinds", {})[request["task"]] = "analysis"
        qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        board.reserve_qa(self.root, qa["id"], request["id"])
        return request, qa

    def ledger(self, request):
        path = self.root / f"challenge-{request['cycle']}.md"
        path.write_text(frozen.base.HEADER + "| S-101 | An independent adversarial check confirms the repair through a distinct test scope. | adversarial | `" + frozen.TESTED
                        + "` | Holds | PASS: executed | PASS |\n")
        return str(path)


for _name in [name for name in dir(frozen.Rounds) if name.startswith("test")]:
    setattr(Fixture, _name, None)          # unittest runs only what this file defines


class SurfaceTests(unittest.TestCase):
    def test_a_side_cannot_file_under_another_agents_id(self):
        from harness import board_surface
        for operation in ("analysis-list", "analysis-crosscheck", "analysis-merge"):
            self.assertIn(operation, board_surface.AGENT_ARGUMENT_OPERATIONS)
        with self.assertRaises(board_surface.SurfaceAuthorizationError):
            board_surface._canonical_argument(["--agent", "qa-other", "--task", "T"], "--agent", "qa-mine")


class BriefTests(Fixture):
    """The review brief of a Code analysis carries the board's clean list; the report is no longer withheld."""

    def test_the_brief_carries_the_clean_list_and_the_report_is_not_withheld(self):
        request, _qa = self.analysis_request()
        clean = [{"id": "C1", "sources": ["D1"], "disputed": False, "members": [
            {"id": "D1", "side": "delivery", "location": "a.py:1", "wrong": "a retried job repeats the paid call", "matters": "double billing", "mark": "confirmed", "evidence": "", "disputed": False}]}]
        with board.locked_state(self.root) as state:
            state["analysis_passes"] = {request["task"]: {"clean": clean}}
            state["qa_requests"][request["id"]]["owner_documents"] = [{"deliverable": "FINDINGS.md", "document": "FINDINGS.md", "path": "x", "sha256": "ab"}]
            brief = review_brief.build(self.root, state, state["qa_requests"][request["id"]])
        self.assertEqual(brief["analysis_clean_list"]["items"], 1)
        self.assertIn("a.py:1", brief["analysis_clean_list"]["text"])
        self.assertIn("N of 1", brief["analysis_clean_list"]["instruction"])
        self.assertIn("PASS only if EVERY clean-list item is in FINDINGS.md", brief["process"]["rule"])
        self.assertNotIn("analysis_discovery", brief)
        self.assertEqual(len(brief["risk_and_scope"]["owner_documents"]), 1)

    def test_a_development_brief_has_no_clean_list_and_keeps_its_documents(self):
        request = self.request()
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]]["owner_documents"] = [{"deliverable": "d", "document": "d", "path": "x", "sha256": "ab"}]
            brief = review_brief.build(self.root, state, state["qa_requests"][request["id"]])
        self.assertIsNone(brief["analysis_clean_list"])
        self.assertEqual(len(brief["risk_and_scope"]["owner_documents"]), 1)


class VerdictTests(unittest.TestCase):
    BASE = ("Security check: read it.\nNot examined: nothing.\nFindings verified independently: 18 of 18\n")

    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"HARNESS_STANDING_CHECKS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def problem(self, summary, clean=None, hand_in=""):
        state = {"task_kinds": {"T": "analysis"}, "qa_requests": {}}
        if clean is not None:
            state["analysis_passes"] = {"T": {"clean": [{"id": f"C{n}"} for n in range(clean)]}}
        with mock.patch.object(board.code_quality, "notes", return_value=[{"note": "checked"}]):
            return board._standing_verdict_problem(Path("/x"), state, {"id": "R1", "task": "T", "summary": hand_in}, "T", summary, True)

    def test_the_clean_list_line_is_required_and_every_item_must_be_in_the_report(self):
        self.assertIn("Clean list items in the report", self.problem(self.BASE, clean=3))
        self.assertEqual(self.problem(self.BASE + "Clean list items in the report: 3 of 3\n", clean=3), "")
        self.assertIn("M = 4", self.problem(self.BASE + "Clean list items in the report: 3 of 3\n", clean=4))
        self.assertIn("2 of 3", self.problem(self.BASE + "Clean list items in the report: 2 of 3\n", clean=3))

    def test_the_old_independent_discovery_line_is_no_longer_asked_for(self):
        self.assertEqual(self.problem(self.BASE, clean=None), "")

    def test_every_dispute_id_is_answered_with_a_disposition_and_evidence(self):
        line = "Clean list items in the report: 0 of 0\n"
        hand_in = "Disputes: B2: the check at gates.py:40 does exist; B4: the helper at util.py:9 guards it"
        self.assertIn("Disputes re-checked", self.problem(self.BASE + line, clean=0, hand_in=hand_in))
        self.assertIn("not yet answered: B2, B4", self.problem(self.BASE + line + "Disputes re-checked: B2; B4\n", clean=0, hand_in=hand_in))
        self.assertIn("not yet answered: B4", self.problem(self.BASE + line + "Disputes re-checked: B2 withdrawn, gates.py:40 rejects frozen accounts; B4 kept\n", clean=0, hand_in=hand_in))
        ok = line + "Disputes re-checked: B2 withdrawn, gates.py:40 rejects frozen accounts; B4 kept, util.py:9 only checks the type\n"
        self.assertEqual(self.problem(self.BASE + ok, clean=0, hand_in=hand_in), "")
        self.assertEqual(self.problem(self.BASE + line, clean=0, hand_in="Disputes: none"), "")

    def test_a_dispute_line_without_ids_is_refused(self):
        line = "Clean list items in the report: 0 of 0\n"
        self.assertIn("start each dispute with the finding id", self.problem(self.BASE + line, clean=0, hand_in="Disputes: the reviewer is wrong about the gate"))

    def test_a_spec_or_development_verdict_is_asked_for_none_of_it(self):
        for kind in ("spec", "development"):
            state = {"task_kinds": {"T": kind}}
            with mock.patch.object(board.code_quality, "notes", return_value=[{"note": "checked"}]):
                self.assertEqual(board._standing_verdict_problem(Path("/x"), state, {"id": "R1", "summary": ""}, "T", "Security check: read it.\nNot examined: nothing.\n", True), "")


class HandInVariantTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"HARNESS_STANDING_CHECKS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def hand_in(self, proof):
        return "Security: read it.\nProof: " + proof + "\nExamined: routes, workers."

    def test_an_evidence_only_hand_in_needs_no_test_file(self):
        self.assertEqual(standing_checks.analysis_hand_in_problem(self.hand_in("every issue is code evidence in the report, section 2 (jobs.py:90)"), ["FINDINGS.md"]), "")

    def test_a_proof_that_says_no_tests_needs_no_test_file(self):
        self.assertEqual(standing_checks.analysis_hand_in_problem(self.hand_in("no tests: each issue is code evidence with file:line in the report"), ["FINDINGS.md"]), "")

    def test_a_proof_naming_a_test_function_needs_the_file(self):
        self.assertIn("test file", standing_checks.analysis_hand_in_problem(self.hand_in("test_issue_one shows issue one"), ["FINDINGS.md"]))


class RoundLimitTests(unittest.TestCase):
    def judge(self, limit, rounds):
        escalations: dict = {}
        request = {"id": "R", "task": "T", "stage": review_rounds.INDEPENDENT, "phase": "final_acceptance", "subtask": "", "chunk": "", "structure_revision": 0}
        decision = {"effective_result": "failed", "blockers": [{"id": "B1", "summary": "the report names the wrong file", "harm": "the owner is misled"}]}
        for number in range(rounds):
            request = dict(request, id=f"R{number}")
            review_rounds.record_judgment(escalations, request, decision, "TASK DONE: NO - B1 is wrong", {}, "2026-10-28T00:00:00+00:00", limit=limit)
        return next(iter(escalations.values()))["status"]

    def test_the_limit_is_two_for_an_analysis_and_three_for_everything_else(self):
        self.assertEqual(review_rounds.limit_for("analysis"), 2)
        for kind in ("development", "spec", ""):
            self.assertEqual(review_rounds.limit_for(kind), review_rounds.round_limit())

    def test_an_analysis_reaches_the_cap_at_two_failed_rounds_and_development_does_not(self):
        self.assertNotEqual(self.judge(review_rounds.limit_for("analysis"), 2), "watching")
        self.assertEqual(self.judge(review_rounds.limit_for("development"), 2), "watching")
        self.assertNotEqual(self.judge(review_rounds.limit_for("development"), 3), "watching")


class TerminalTests(unittest.TestCase):
    def test_an_analysis_at_two_failed_judgments_is_settled_with_no_retry_and_no_third_review(self):
        escalations: dict = {}
        decision = {"effective_result": "failed", "blockers": [{"id": "B1", "summary": "the report names the wrong file", "harm": "misleads"}]}
        for number in range(2):
            request = {"id": f"R{number}", "task": "T", "stage": review_rounds.INDEPENDENT, "phase": "final_acceptance", "subtask": "", "chunk": "", "structure_revision": 0}
            review_rounds.record_judgment(escalations, request, decision, "TASK DONE: NO - B1 is wrong", {}, "2026-10-28T00:00:00+00:00",
                                          limit=review_rounds.limit_for("analysis"), analysis=True)
        entry = next(iter(escalations.values()))
        self.assertEqual(entry["status"], "could_not_finish")
        self.assertIn("NOT independently verified", entry["outcome"])
        self.assertNotIn("fresh", entry["outcome"])


class WordingTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_the_directive_and_the_cards_carry_the_two_pass_flow(self):
        agent = " ".join((self.ROOT / "directives" / "AGENT.md").read_text(encoding="utf-8").split())
        for needle in ("`analysis-list --task <task> --add", "`analysis-crosscheck --task <task>", "Clean list items in the report: N of M", "`Disputes:`", "`Proof:`"):
            self.assertIn(needle, agent)
        for gone in ("analysis-discovery", "Independent discovery"):
            self.assertNotIn(gone, agent)
        for name, needle in (("REVIEWER.md", "analysis-list"), ("DELIVERY.md", "analysis-list"), ("CTO.md", "analysis-merge")):
            card = (self.ROOT / "directives" / "cards" / name).read_text(encoding="utf-8")
            self.assertIn(needle, card)
            self.assertNotIn("analysis-discovery", card)
            self.assertLess(len(card.encode("utf-8")), 10000)

    def test_the_commands_belong_to_their_roles(self):
        from harness import board_surface
        for operation in ("analysis-list", "analysis-crosscheck"):
            self.assertIn(operation, board_surface.REVIEWER_OPERATIONS)
            self.assertIn(operation, board_surface.DELIVERY_OPERATIONS)
            self.assertNotIn(operation, board_surface.CTO_OPERATIONS)
        self.assertIn("analysis-merge", board_surface.CTO_OPERATIONS)
        for role in ("DELIVERY_OPERATIONS", "REVIEWER_OPERATIONS"):
            self.assertNotIn("analysis-merge", getattr(board_surface, role))
            self.assertNotIn("analysis-discovery", getattr(board_surface, role))


if __name__ == "__main__":
    unittest.main()

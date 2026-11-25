# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Nothing the owner approved may silently fall out between the requirements and the release (task 2026-11-15_spec-trace-safeguards).

PropDesk Public, build-proprisk: the accepted requirements said "RENAMING: every visible PropDesk becomes PropRisk ... (SPEC 1.1, 1.7)";
no deliverable cited 1.7, the Reviewer's criterion for it was "not checked in advance", the final review passed on the screen, and the
CTO released on "contract complete". 52 old names stayed in the code.
Run:  PYTHONPATH=. python3 -m unittest tests.test_spec_trace_safeguards -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, contract, review_brief
from tests.test_requirements_contract import delivery_with_direction

RENAMING = ("RENAMING: every visible \"PropDesk\" becomes \"PropRisk\" across UI, prompts, logger names, User-Agent strings; "
            "`desk.example.com` and the old support email are removed (SPEC 1.1, 1.7).")
ACCEPTED = "Build PropRisk.\nKEPT: all four screens (SPEC 1.2, 1.11).\n" + RENAMING + "\nNOT INCLUDED: hosting (SPEC 12).\n"
TASK = "TASK-TRACE"


class CitedSectionsTests(unittest.TestCase):
    def test_citations_are_read_the_way_requirements_and_deliverables_write_them(self):
        self.assertEqual(set(contract.cited_spec_sections(ACCEPTED)), {"1.2", "1.11", "1.1", "1.7", "12"})
        self.assertEqual(set(contract.cited_spec_sections("folder per SPEC 1.1-1.5; keys per SPEC 1.6; launch per SPEC 1.9, 2")),
                         {"1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.9", "2"})
        self.assertEqual(set(contract.cited_spec_sections("SPEC §3.4 and SPEC 4–6")), {"3.4", "4", "5", "6"})
        self.assertEqual(contract.cited_spec_sections("read SPEC.md and the SPEC targets"), {})

    def test_a_huge_or_backwards_range_counts_only_its_two_ends(self):
        self.assertEqual(set(contract.cited_spec_sections("SPEC 1.1-1.9999")), {"1.1", "1.9999"})
        self.assertEqual(set(contract.cited_spec_sections("SPEC 1.5-1.2")), {"1.5", "1.2"})
        self.assertEqual(set(contract.cited_spec_sections("SPEC 1.9-2.1")), {"1.9", "2.1"})

    def test_a_parent_section_does_not_cover_a_child(self):
        self.assertEqual(set(contract.uncovered_spec_sections("x (SPEC 1.7)", ["everything per SPEC 1"])), {"1.7"})
        self.assertEqual(contract.uncovered_spec_sections("x (SPEC 1.7)", ["names per SPEC 1.6-1.8"]), {})


class ContractCarriesEverySectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.agent = delivery_with_direction(self.root, TASK)

    def accept(self, text):
        with board.locked_state(self.root) as state:
            state.setdefault("requirement_confirmations", {})[TASK] = {"task": TASK, "text": text, "status": "confirmed", "version": 1}

    def test_requirements_without_spec_citations_change_nothing(self):
        self.accept("Build a page that prints hello.")
        created = board.create_contract(self.root, self.agent["id"], "Hello page", ["The page prints hello"])
        self.assertEqual(created["contract"]["deliverables"][0]["name"], "The page prints hello")


class FinalReviewerIsToldWhatWasNotCheckedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.request = {
            "id": "review-final", "task": "TASK-A", "phase": "final_acceptance",
            "test_scope": "full", "reviewed_commit": "a" * 40, "reviewed_tree_hash": "b" * 40,
            "reviewed_files": ["app.py"], "contract_revision": {"sha256": "c" * 64},
            "environment_identity": {"sha256": "d" * 64}, "delivery_simulations": {"scenario_ids": ["S-001"]},
        }
        labels = [
            {"n": 2, "criterion": "Keep all four screens.", "label": "checked", "coverage": "covered", "reason": "", "note": ""},
            {"n": 6, "criterion": "Replace visible PropDesk branding.", "label": "not checked in advance", "coverage": "covered",
             "reason": "A shared runnable check under another criterion covers its observable behavior.", "note": ""},
            {"n": 11, "criterion": "launch.sh starts everything.", "label": "partly checked", "coverage": "covered", "reason": "", "note": ""},
        ]
        self.state = {
            "requirement_confirmations": {"TASK-A": {"text": ACCEPTED, "confirmed_at": "2026-11-15T18:08:00+00:00", "version": 1,
                                                     "contract_check": {"labels": labels}}},
            "delivery_plans": {"TASK-A": {"mode": "atomic", "rationale": "One build", "structure_revision": 1}},
            "qa_requests": {"review-final": self.request}, "owner_clarifications": {}, "deferred_findings": {}, "task_repositories": {},
        }

    def test_the_final_brief_lists_each_requirement_not_fully_checked_and_says_to_search_the_code(self):
        stage = review_brief.build(self.root, self.state, self.request)["final_stage"]
        self.assertEqual([item["n"] for item in stage["not_checked_in_advance"]], [6, 11])
        self.assertEqual(stage["not_checked_in_advance"][0]["criterion"], "Replace visible PropDesk branding.")
        self.assertIn("requirements.text", stage["verify_yourself"])
        self.assertIn("searching the delivered code", stage["verify_yourself"])

    def test_a_checked_requirement_whose_check_did_not_run_or_failed_is_listed_with_its_last_run(self):
        block = self.state["requirement_confirmations"]["TASK-A"]["contract_check"]
        block["results"] = {"2": "did_not_run"}
        block["did_not_run"] = {"2": "the machine refused a file or folder the check needed (EPERM)"}
        listed = review_brief.build(self.root, self.state, self.request)["final_stage"]["not_checked_in_advance"]
        self.assertEqual([item["n"] for item in listed], [2, 6, 11])
        self.assertTrue(listed[0]["last_run"].startswith("did_not_run: the machine refused"))

    def test_no_early_checks_at_all_means_the_whole_text_is_verified(self):
        del self.state["requirement_confirmations"]["TASK-A"]["contract_check"]
        stage = review_brief.build(self.root, self.state, self.request)["final_stage"]
        self.assertNotIn("not_checked_in_advance", stage)
        self.assertIn("No requirement was checked in advance", stage["verify_yourself"])
        self.assertIn("line by line", stage["verify_yourself"])

    def test_the_whole_accepted_text_reaches_the_final_reviewer(self):
        long_text = "x" * 5000 + "\n" + RENAMING
        self.state["requirement_confirmations"]["TASK-A"]["text"] = long_text
        text = review_brief.build(self.root, self.state, self.request)["requirements"]["text"]
        self.assertTrue(text.endswith("(SPEC 1.1, 1.7)."), "on main the brief cut it at 4,000 characters")

    def test_every_listed_check_passed_but_a_line_was_left_out_still_gets_the_whole_text_reconciled(self):
        # Codex R1-1: the early Reviewer submitted only the title criterion and its test passed; the rename line had no label at all
        block = self.state["requirement_confirmations"]["TASK-A"]["contract_check"]
        block["labels"] = [{"n": 1, "criterion": "The page title says PropRisk.", "label": "checked", "coverage": "covered", "reason": "", "note": ""}]
        block["results"] = {"1": "passed"}
        stage = review_brief.build(self.root, self.state, self.request)["final_stage"]
        self.assertNotIn("not_checked_in_advance", stage)
        self.assertIn("the criteria may not cover every line", stage["verify_yourself"])
        self.assertIn("line by line", stage["verify_yourself"])
        self.assertIn("searching the delivered code", stage["verify_yourself"])

    def test_a_review_before_the_final_one_is_unchanged(self):
        self.request["phase"] = "chunk"
        self.assertNotIn("final_stage", review_brief.build(self.root, self.state, self.request))


class CtoComparesTheAcceptedRequirementsTests(unittest.TestCase):
    def test_the_cto_card_says_contract_complete_alone_is_not_enough(self):
        card = (Path(board.__file__).resolve().parents[1] / "directives" / "cards" / "CTO.md").read_text(encoding="utf-8")
        self.assertIn("compare them line by line with the contract evidence and the final review", card)
        self.assertIn("not just a cited section or a check that exists", card)
        self.assertIn("expand-contract", card)
        self.assertIn("\"contract complete\" alone is not enough", card)


if __name__ == "__main__":
    unittest.main()

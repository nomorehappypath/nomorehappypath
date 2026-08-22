# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import tempfile
import unittest
from pathlib import Path

from harness import review_brief


class ReviewBriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.request = {
            "id": "review-current", "task": "TASK-A", "phase": "final_acceptance",
            "test_scope": "full", "reviewed_commit": "a" * 40, "reviewed_tree_hash": "b" * 40,
            "reviewed_files": ["app.py"], "contract_revision": {"sha256": "c" * 64},
            "environment_identity": {"sha256": "d" * 64},
            "delivery_simulations": {"scenario_ids": ["S-001"]},
        }
        self.state = {
            "requirement_confirmations": {"TASK-A": {"text": "Ship the exact agreed behavior.", "confirmed_at": "2026-08-18T00:00:00+00:00", "version": 1}},
            "delivery_plans": {"TASK-A": {"mode": "atomic", "rationale": "One cohesive change", "structure_revision": 1}},
            "qa_requests": {"review-current": self.request}, "owner_clarifications": {},
            "deferred_findings": {}, "task_repositories": {},
        }

    def test_brief_is_deterministic_bounded_and_project_scoped(self):
        scenarios = [{"id": "S-001", "what_was_tested": "The visible result remains truthful."}]
        first = review_brief.build(self.root, self.state, self.request, delivery_scenarios=scenarios)
        second = review_brief.build(self.root, self.state, self.request, delivery_scenarios=scenarios)
        self.assertEqual(first, second)
        self.assertEqual(first["isolation"]["code_root"], str(self.root.resolve()))
        self.assertEqual(first["delivery_scenarios"][0]["recorded"], "executed")
        self.assertNotIn("proposed_verdict", first)

    def test_authoring_brief_withholds_delivery_evidence(self):
        result = review_brief.build(
            self.root, self.state, self.request,
            delivery_scenarios=[{"id": "S-001", "what_was_tested": "Hidden"}],
            include_delivery_evidence=False,
        )
        self.assertTrue(result["delivery_evidence_withheld"])
        self.assertEqual(result["delivery_scenarios"], [])

    def test_missing_requirements_and_narrowed_reason_fail_closed(self):
        self.state["requirement_confirmations"] = {}
        with self.assertRaisesRegex(ValueError, "confirmed final requirements"):
            review_brief.build(self.root, self.state, self.request)
        self.state["requirement_confirmations"] = {"TASK-A": {"text": "Confirmed"}}
        self.request.update({"test_scope": "focused", "scope_reason": ""})
        with self.assertRaisesRegex(ValueError, "reason missing"):
            review_brief.build(self.root, self.state, self.request)

    def test_other_task_failures_and_findings_never_enter_brief(self):
        self.state["qa_requests"]["foreign"] = {
            "id": "foreign", "task": "TASK-B", "status": "failed",
            "result_summary": "FOREIGN SECRET", "cycle": 1,
        }
        self.state["deferred_findings"]["foreign"] = {
            "task": "TASK-B", "status": "in_scope", "description": "FOREIGN FINDING",
        }
        result = review_brief.build(self.root, self.state, self.request)
        self.assertNotIn("FOREIGN", str(result))

    def test_repair_brief_reuses_only_same_reviewers_prior_authorship(self):
        self.request.update({
            "reserved_by": "reviewer-a",
            "repair_context": {
                "prior_request_id": "review-prior",
                "prior_reviewer_id": "reviewer-a",
                "prior_blocking_summary": "The crash boundary remained open.",
                "diff_available": True,
                "diff_files": ["harness/recovery.py"],
                "prior_challenge_ledger": "/trusted/challenge.md",
                "prior_challenge_ledger_sha256": "e" * 64,
                "challenge_prefill": {
                    "mechanical_rows": [{
                        "prior_scenario_id": "S-REPAIR",
                        "simulation_command": "python3 -m unittest test_recovery",
                        "prior_execution_identity": "f" * 64,
                        "rerun_required": True,
                    }],
                    "unavailable_reasons": [],
                },
            },
        })
        same = review_brief.build(self.root, self.state, self.request)
        plan = same["repair_authoring"]
        self.assertTrue(plan["same_reviewer_may_reuse_own_wording"])
        self.assertTrue(plan["prior_command_rows"][0]["rerun_required"])
        self.assertIn("complete suite", " ".join(plan["requirements"]))

        self.request["reserved_by"] = "reviewer-b"
        different = review_brief.build(self.root, self.state, self.request)
        self.assertFalse(
            different["repair_authoring"]["same_reviewer_may_reuse_own_wording"]
        )


if __name__ == "__main__":
    unittest.main()

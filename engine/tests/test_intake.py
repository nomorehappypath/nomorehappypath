#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Regression tests for intake + planning (engine/scripts/intake.py). Stdlib unittest.

The planner is a seam, so tests run offline (no live LLM).
Run via: bash engine/scripts/run_tests.sh
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import claim  # noqa: E402
import intake  # noqa: E402
import scheduler  # noqa: E402

POOLS = {"implementer_vendor": "Claude (Anthropic)", "reviewer_vendor": "Codex (OpenAI)"}


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        (Path(self._tmp.name) / ".agents").mkdir()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_clarifying_questions(self):  # AC1
        qs = intake.clarifying_questions("an invoice tracker", "commercial")
        self.assertTrue(len(qs) >= 3)
        self.assertTrue(all(isinstance(q, str) and q for q in qs))
        self.assertTrue(any("use this" in q for q in qs))

    def test_default_planner_features_with_acs(self):  # AC2
        plan = intake.build_plan("app", "commercial", {"features": ["log in", "see reports"]})
        self.assertEqual(len(plan["features"]), 2)
        for f in plan["features"]:
            self.assertIn("name", f)
            self.assertIn("outcome", f)
            ac = f["acceptance_criteria"].upper()
            self.assertTrue("GIVEN" in ac and "WHEN" in ac and "THEN" in ac)

    def test_pluggable_planner(self):  # AC2
        sentinel = {"plan_id": "p-1", "idea": "x", "intent": "", "features": [], "requirements": "r"}
        plan = intake.build_plan("x", "", {}, planner=lambda i, n, a: sentinel)
        self.assertIs(plan, sentinel)

    def test_render_plan_text_plain(self):  # AC3
        plan = intake.build_plan("app", "", {"features": ["log in"]})
        text = intake.render_plan_text(plan)
        self.assertIn("What you'll be able to do", text)
        self.assertIn("You will be able to: log in", text)
        self.assertIn("Approve this plan", text)

    def test_decompose_posts_cross_vendor_items(self):  # AC4
        plan = intake.build_plan("app", "", {"features": ["log in", "see reports"]})
        items = intake.decompose(plan, POOLS)
        self.assertEqual(len(items), 4)  # 2 features x (implementer + reviewer)
        impls = [i for i in items if i["role"] == "implementer"]
        revs = [i for i in items if i["role"] == "reviewer"]
        self.assertEqual(len(impls), 2)
        self.assertEqual(len(revs), 2)
        self.assertTrue(all(i["forbid_vendor"] is None for i in impls))
        self.assertTrue(all(i["forbid_vendor"] == "Claude (Anthropic)" for i in revs))
        self.assertTrue((Path(".agents/intake") / f"{plan['plan_id']}.json").exists())
        # the items are really on the claim queue
        self.assertEqual(len(claim.list_items()), 4)

    def test_end_to_end_decompose_then_schedule(self):  # AC5
        plan = intake.build_plan("app", "", {"features": ["log in"]})
        intake.decompose(plan, POOLS)
        res = scheduler.tick(POOLS)
        self.assertEqual(len(res["dispatched"]), 2)
        by_role = {d["role"]: d["vendor"] for d in res["dispatched"]}
        self.assertEqual(by_role["implementer"], "Claude (Anthropic)")
        self.assertEqual(by_role["reviewer"], "Codex (OpenAI)")  # cross-vendor routed

    def test_cli_smoke(self):  # AC6
        with redirect_stdout(io.StringIO()):
            self.assertEqual(intake.main(["questions", "--idea", "x"]), 0)
            self.assertEqual(intake.main(["plan", "--idea", "x", "--features", "log in"]), 0)
        plan_path = intake.save_plan(intake.build_plan("y", "", {"features": ["a"]}))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(intake.main(["decompose", "--plan", str(plan_path)]), 0)


if __name__ == "__main__":
    unittest.main()

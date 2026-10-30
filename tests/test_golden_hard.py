# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Hard golden eval set (task 2026-10-27_golden-hard-set): the ten projects are valid, the answers stay out of the agents' folder, the scorer scores. macOS only."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

GOLDEN = Path(__file__).resolve().parents[1] / "evals" / "golden"
HARD = Path(__file__).resolve().parents[1] / "evals" / "golden_hard"

# Every family the owner asked for must have at least one planted bug (matched in the bug's own `family` text).
REQUIRED_FAMILIES = ["idor", "sibling", "traversal", "timing", "rounding", "half-up", "float", "double charge", "discount", "dst", "month-end",
                     "leap", "today", "non-atomic", "race", "unicode", "swallowed", "pagination", "stale cache"]
# Words that would name a bug, an edge case or an attack: they must not appear in what the agents are given.
GIVEAWAYS = ["traversal", "idempoten", "race condition", "timing attack", "constant-time", "constant time", "idor", "daylight", "leap",
             "normalization", "normalisation", "unicode", "stale", "swallow", "atomic", "mass assignment", "sql injection", "dst"]


def _runner():
    spec = importlib.util.spec_from_file_location("golden_run_eval_hard", str(GOLDEN / "run_eval.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["golden_run_eval_hard"] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(sys.platform == "darwin" and (GOLDEN / "run_eval.py").is_file() and HARD.is_dir(), "hard golden eval set: macOS, and only where evals/golden_hard ships")
class GoldenHardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.run_eval = _runner()
        cls.ids = cls.run_eval.set_ids("hard")

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="golden-hard-test-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def test_dry_run_every_seed_passes_its_happy_path_and_every_planted_bug_is_observed_for_the_right_reason(self):
        self.assertEqual(len(self.ids), 10)
        report = self.run_eval.validate(self.ids)   # structure, seed smoke (the happy path), reference fix smoke, each bug fails on the seed and passes on the fix
        planted = 0
        for row in report:
            self.assertEqual(row["problems"], [], row["project"])
            self.assertTrue(2 <= len(row["bugs"]) <= 3, row["project"])
            for bug_id, result in row["bugs"].items():
                planted += 1
                self.assertTrue(result["seed_fails"], "%s %s must fail on the seed" % (row["project"], bug_id))
                self.assertTrue(result["fix_passes"], "%s %s must pass with the reference fix" % (row["project"], bug_id))
                self.assertNotIn("Traceback", result["seed_output"], "%s %s fails on the seed by crashing, not by the planted bug" % (row["project"], bug_id))
        self.assertGreaterEqual(planted, 20)

    def test_every_required_family_has_a_planted_bug_and_every_bug_documents_itself(self):
        families = []
        for project_id in self.ids:
            for bug in self.run_eval.load_spec(project_id)["bugs"]:
                for key in ("id", "file", "requirement", "what_is_wrong", "why_hard", "family", "how_to_observe", "observe", "keywords"):
                    self.assertTrue(bug.get(key), "%s %s has no %s" % (project_id, bug.get("id"), key))
                self.assertGreater(len(bug["why_hard"]), 60, "%s %s: say why it is hard" % (project_id, bug["id"]))
                families.append(bug["family"].lower())
        text = "\n".join(families)
        missing = [f for f in REQUIRED_FAMILIES if f not in text]
        self.assertEqual(missing, [], "families without a planted bug")

    def test_the_agents_get_only_the_seed_and_the_contract_and_the_contract_names_no_bug(self):
        for project_id in self.ids:
            copy = self.run_eval.prepare_copy(project_id, self.scratch / project_id)
            names = sorted(p.name for p in copy.rglob("*") if p.is_file())
            for forbidden in ("observe.py", "bugs.json", "_check.py"):
                self.assertNotIn(forbidden, names, project_id)
            self.assertFalse((copy / "fix").exists(), project_id)
            self.assertIn("contract.md", names)
            self.assertNotIn("brief.md", names)
            everything = "\n".join(p.read_text(encoding="utf-8") for p in copy.rglob("*") if p.is_file() and p.suffix in {".py", ".md"})
            for bug in self.run_eval.load_spec(project_id)["bugs"]:
                self.assertNotIn(bug["id"], everything.replace("PD", "pd"), "%s: a bug id appears in what the agents see" % project_id)
            contract = (copy / "contract.md").read_text(encoding="utf-8").lower()
            for word in GIVEAWAYS:
                self.assertNotRegex(contract, r"\b%s" % re.escape(word), "%s: contract.md names '%s'" % (project_id, word))
            brief = (HARD / project_id / "brief.md").read_text(encoding="utf-8").lower()
            for word in GIVEAWAYS:
                self.assertNotRegex(brief, r"\b%s" % re.escape(word), "%s: brief.md names '%s'" % (project_id, word))

    def test_live_prepare_works_for_a_hard_project_and_keeps_the_answers_out(self):
        run = self.run_eval.live("h07_event_tickets", self.scratch, "t", start_manager=False)
        self.addCleanup(shutil.rmtree, run, True)
        record = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(record["project"], "h07_event_tickets")
        task = (run / "task.txt").read_text(encoding="utf-8")
        self.assertIn("theatre", task)
        project = run / "project"
        self.assertTrue((project / "contract.md").is_file() and (project / "tickets.py").is_file())
        self.assertFalse((project / "observe.py").exists() or (project / "bugs.json").exists() or (project / "fix").exists())

    def test_scorer_scores_a_fixed_copy_a_half_fixed_copy_and_an_unfixed_seed_for_every_project(self):
        run_eval = self.run_eval
        for project_id in self.ids:
            planted = len(run_eval.load_spec(project_id)["bugs"])
            seed = run_eval.prepare_copy(project_id, self.scratch / ("seed-" + project_id))
            fixed = run_eval.prepare_copy(project_id, self.scratch / ("fixed-" + project_id), with_fix=True)
            self.assertEqual(run_eval.score_project(project_id, seed, None)["fixed"], 0, project_id)
            scored = run_eval.score_project(project_id, fixed, None)
            self.assertEqual((scored["fixed"], scored["planted"]), (planted, planted), project_id)
        # a partly fixed project: h03 with only the receipt owner check fixed scores 1 of 3
        half = run_eval.prepare_copy("h03_shop_portal", self.scratch / "half")
        source = (half / "portal.py").read_text(encoding="utf-8")
        patched = source.replace('        if order is None:\n            return 404, {"error": "Not found"}\n        owner',
                                 '        if order is None or order["user_id"] != user["id"]:\n            return 404, {"error": "Not found"}\n        owner')
        self.assertNotEqual(source, patched)
        (half / "portal.py").write_text(patched, encoding="utf-8")
        scored = run_eval.score_project("h03_shop_portal", half, None)
        self.assertEqual((scored["fixed"], scored["planted"]), (1, 3))
        self.assertEqual([b["id"] for b in scored["bugs"] if b["fixed"]], ["AC1"])

    def test_set_option_picks_the_set_and_the_default_stays_the_easy_set(self):
        run_eval = self.run_eval
        self.assertEqual(len(run_eval.set_ids("easy")), 10)
        self.assertEqual(run_eval.set_ids("hard"), self.ids)
        self.assertTrue(all(i.startswith("h") for i in self.ids))
        self.assertEqual(len(run_eval.project_ids()), 20)                       # every runner mode can find a hard project by its id
        self.assertEqual(len(set(run_eval.project_ids())), 20)                  # ids are unique across the sets
        seen = []
        original = run_eval.validate
        run_eval.validate = lambda ids: (seen.append(list(ids)) or [])
        self.addCleanup(setattr, run_eval, "validate", original)
        run_eval.main([])
        run_eval.main(["--set", "hard"])
        run_eval.main(["--set", "hard", "--project", "h01_invoice_studio"])
        self.assertEqual(seen[0], run_eval.set_ids("easy"))
        self.assertEqual(seen[1], self.ids)
        self.assertEqual(seen[2], ["h01_invoice_studio"])

    def test_fixtures_carry_no_personal_traces_and_no_network(self):
        trace = "aba" + "lgir"
        for path in HARD.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md", ".json"}:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(trace, text, str(path))
                self.assertNotIn("/Users/", text, str(path))
                if "seed" in path.parts or "fix" in path.parts:
                    for word in ("urllib.request", "requests", "socket", "http.client", "subprocess"):
                        self.assertNotIn("import " + word, text, str(path))


if __name__ == "__main__":
    unittest.main()

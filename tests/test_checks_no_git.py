# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A Reviewer's check may not run git (checks run in a copy with no git), and a refused skip names the right repair (2026-11-04).

On the clone's documentation task the Reviewer's check 1.1 ran `git show BASE:path`; it could never pass in the disposable checkout,
and it was only found at final acceptance. Now the board refuses such a check when it is filed.

Run:  PYTHONPATH=. python3 -m unittest tests.test_checks_no_git -v
"""
from __future__ import annotations

import unittest

from harness import contract_check

LEDGER = "| ID | What was tested | Scenario | Simulation command | Expected | Observed | QA result |\n|---|---|---|---|---|---|---|\n| S-1 | hello | prints | python3 -m unittest | hello | hello | PASS |\n"


def check(body: str) -> list[dict]:
    return [{"coverage": "checked", "ledger": LEDGER, "files": {"test_it.py": body}}]


class ChecksMayNotRunGitTests(unittest.TestCase):
    def test_a_check_that_runs_git_is_refused_when_it_is_written(self):
        for body in ('import subprocess\nsubprocess.run(["git", "show", "BASE:app.py"], check=True)\n',
                     "import os\nos.system('git diff --quiet')\n",
                     'subprocess.check_output("git log -1", shell=True)\n',
                     'from harness import git_process\ngit_process.run(["git", "status"], cwd=".")\n',
                     'from subprocess import run\nrun(("git", "rev-parse", "HEAD"))\n'):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "runs git; checks run in a copy of the project with no git"):
                contract_check.parse_checks(check(body), "criterion 1")

    def test_a_check_that_only_mentions_git_in_prose_or_reads_the_baseline_copy_is_accepted(self):
        for body in ("# the baseline copy replaces git here\nimport unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(open('baseline/app.py').read())\n",
                     'name = "digit"\nassert "git" not in name[:2]\n',
                     # round 1 (Codex): a documentation check and a comment that quote a git command are not commands
                     'import unittest\nfrom pathlib import Path\nclass T(unittest.TestCase):\n    def test_docs(self):\n        self.assertIn("git diff --stat", Path("README.md").read_text())\n',
                     '# The README documents "git diff --stat" for users.\nimport unittest\n',
                     'subprocess.run(["python3", "-m", "unittest"], check=True)\n'):
            with self.subTest(body=body):
                checks, _ = contract_check.parse_checks(check(body), "criterion 1")
                self.assertEqual(len(checks), 1)

    def test_a_shell_check_that_starts_a_line_with_git_is_refused_and_a_mention_is_not(self):
        with self.assertRaisesRegex(ValueError, "runs git"):
            contract_check.parse_checks([{"coverage": "checked", "ledger": LEDGER, "files": {"check.sh": "#!/bin/sh\ngit show HEAD:app.py\n"}}], "criterion 1")
        checks, _ = contract_check.parse_checks([{"coverage": "checked", "ledger": LEDGER, "files": {"check.sh": "#!/bin/sh\n# documents git diff\ngrep -q 'git diff' README.md\n"}}], "criterion 1")
        self.assertEqual(len(checks), 1)

    def test_the_reviewer_is_told_in_its_rules(self):
        self.assertIn("never run git commands", contract_check.REVIEWER_RULES_TEXT if hasattr(contract_check, "REVIEWER_RULES_TEXT") else open(contract_check.__file__, encoding="utf-8").read())


class RefusedSkipNamesTheRepairTests(unittest.TestCase):
    def test_skipping_a_check_that_ran_and_failed_names_check_deviation(self):
        import tempfile
        from pathlib import Path
        from harness import board
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")   # the CTO may skip on any task; the refusal is the same
            with board.locked_state(root) as state:
                state.setdefault("requirement_confirmations", {})["TASK-SKIP"] = {
                    "text": "agreed", "contract_check": {"checks": [{"criterion_index": 1, "position": 1}], "results": {"1": "failed"}}}
            with self.assertRaisesRegex(ValueError, "file check-deviation .*the Reviewer answers once"):
                board.record_contract_check_skip(root, cto["id"], "TASK-SKIP", 1, "git show cannot run in the reviewer checkout")

if __name__ == "__main__":
    unittest.main()

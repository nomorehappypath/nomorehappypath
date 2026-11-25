# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner 2026-11-24: "if an issue is not a material bug in the app it should not stop the delivery of the product, should apply everywhere,
everytime". Phone step 1 lost its whole early look over one badly formatted test; these feed badly formatted input and require that the good
part is kept, the bad part is named, and nothing that did not run is counted."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import contract, contract_check

TEST = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n"


def package(**changes):
    return {"criteria": [{"text": "The page shows the weather", "plain": "You see today's weather on the page.",
                          "examples": ["Typing Chicago shows today's temperature for Chicago."],
                          "checks": [{"coverage": "checked", "runs": "app.py", "example": 1, "files": {"test_a.py": TEST},
                                      "ledger": "| ID | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|\n"
                                                "| S-001 | python3 -m unittest discover -s .contract-checks/c1-1 | passes | ok | PASS |\n"}]}],
            "owner_asks": [{"ask": "Show the weather", "requirements": [1]}], "result_must_contain": ["Today's temperature"], **changes}


class EarlyLook(unittest.TestCase):
    def parse(self, data):
        return contract_check.parse_submission(json.dumps(data))

    def test_the_package_of_phone_step_1_is_kept(self):
        data = package()
        data["criteria"][0]["checks"][0]["ledger"] = "| ID | Simulation command | Expected |\n|---|---|---|\n| 1 | dart analyze | no issues |\n"
        parsed = self.parse(data)
        self.assertEqual(len(parsed["criteria"][0]["checks"]), 1, "NMHP runs the check's own test file itself")
        self.assertIn("python3 -m unittest discover -s .contract-checks/c1-1", parsed["criteria"][0]["checks"][0]["ledger"])
        self.assertEqual(parsed["owner_asks"][0]["ask"], "Show the weather")
        self.assertTrue(parsed["notes"])

    def test_the_row_name_the_real_reviewer_wrote_twice_is_numbered(self):
        data = package()
        check = data["criteria"][0]["checks"][0]
        check["ledger"] = check["ledger"].replace("S-001", "PHONE-1")
        item = self.parse(data)["criteria"][0]
        self.assertEqual(len(item["checks"]), 1)
        self.assertTrue(contract_check.criterion_counted(item))

    def test_one_bad_test_is_left_out_and_the_rest_stays(self):
        data = package()
        data["criteria"].append({"text": "The page starts", "examples": ["Opening the page shows a title on top."],
                                 "checks": [{"files": {"run.sh": "true"}, "ledger": "no table"}, {"files": {"../x.py": TEST}, "ledger": "x"}, "words"]})
        parsed = self.parse(data)
        self.assertEqual([len(item["checks"]) for item in parsed["criteria"]], [1, 0])
        self.assertIn("not used", parsed["criteria"][1]["why_not_checked"])

    def test_what_does_not_say_what_it_proves_is_never_counted(self):
        data = package()
        check = data["criteria"][0]["checks"][0]
        for change in ({"runs": ""}, {"example": 9}, {"coverage": "yes"}):
            with self.subTest(change=change):
                data["criteria"][0]["checks"] = [{**check, **change}]
                self.assertFalse(contract_check.criterion_counted(self.parse(data)["criteria"][0]))
        data["criteria"][0]["checks"] = [check]
        data["criteria"][0].pop("examples")
        item = self.parse(data)["criteria"][0]
        self.assertTrue(contract_check.new_rules(item), "the final review still tests it for real")
        self.assertFalse(contract_check.criterion_counted(item))

    def test_missing_lists_long_text_and_odd_questions_do_not_stop(self):
        data = package(owner_asks=None, result_must_contain=None, questions=[{"text": "Which?", "options": ["a"]}, {"text": "Colour?", "options": ["a", "b"], "recommended": 7}])
        data["criteria"][0]["plain"] = "See `main.py` " + "x " * 400
        parsed = self.parse(data)
        self.assertEqual([q["text"] for q in parsed["questions"]], ["Colour?"])
        self.assertEqual(parsed["questions"][0]["recommended"], 0)

    def test_a_check_moved_by_a_left_out_one_still_finds_its_folder(self):
        data = package()
        good = data["criteria"][0]["checks"][0]
        data["criteria"][0]["checks"] = [{"files": {}, "ledger": ""}, {**good, "ledger": good["ledger"].replace("c1-1", "c1-2")}]
        check = self.parse(data)["criteria"][0]["checks"][0]
        ledger, _files = contract_check.placed(check, 1, 1)
        self.assertIn(".contract-checks/c1-1", ledger)

    def test_only_a_file_nothing_can_be_read_from_is_refused_and_may_come_again(self):
        for raw in ("not json", "{}", '{"criteria": []}'):
            with self.assertRaisesRegex(ValueError, "send it again"):
                contract_check.parse_submission(raw)

    def test_the_old_saved_fifteen_minutes_are_the_new_thirty(self):
        self.addCleanup(contract_check.configure, None)
        contract_check.configure(900)
        self.assertEqual(contract_check.wait_seconds(), 1800)


class Ledger(unittest.TestCase):
    def plan(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.md"
            path.write_text(text, encoding="utf-8")
            return contract.scenario_plan(path), contract.scenario_ledger_complete(path)

    def test_names_of_columns_ids_and_wording_never_stop(self):
        (rows, problems, _notes), (complete, _) = self.plan("| # | Test | Command | Expected |\n|---|---|---|---|\n| one | x | python3 -m unittest t | ok |\n| S-001 | y | npm test | ok |\n")
        self.assertEqual(problems, [])
        self.assertEqual([row["id"] for row in rows], ["S-001", "S-002"])
        self.assertTrue(complete)

    def test_a_row_that_cannot_run_is_named_and_never_counted(self):
        (rows, problems, notes), _ = self.plan("| ID | Command | QA result |\n|---|---|---|\n| S-1 | pytest -q | tail -1 | PASS |\n| S-2 | echo hi | PASS |\n| S-3 | pytest -q | N/A: later |\n| S-4 | pytest -q | |\n")
        self.assertEqual(problems, [])
        self.assertNotIn("S-2", [row["id"] for row in rows])
        self.assertTrue(any("S-2" in note for note in notes) and any("S-3" in note for note in notes))

    def test_a_row_its_author_wrote_as_failing_and_an_empty_ledger_still_stop(self):
        _, (complete, problems) = self.plan("| ID | Command | QA result |\n|---|---|---|\n| S-1 | pytest -q | FAIL |\n")
        self.assertFalse(complete)
        (rows, problems, _), _ = self.plan("nothing here")
        self.assertFalse(rows)
        self.assertTrue(problems)


class LoginToken(unittest.TestCase):
    """Codex R1-1: a token in a ledger that had to be repaired reached the stored notes."""
    TOKEN = "sk-ant-" + "oat01-" + "A1b2C3d4E5" * 4

    def test_a_token_anywhere_in_the_package_is_refused_before_anything_is_read(self):
        from harness import board
        data = package()
        data["criteria"][0]["checks"][0]["ledger"] = f"| ID | Command |\n|---|---|\n| S-1 | tool --key {self.TOKEN} |\n"
        for raw in (json.dumps(data), json.dumps(data).replace("sk-ant", "\\u0073k-ant")):
            with self.assertRaisesRegex(ValueError, "login token"):
                board._refuse_login_tokens(raw)
        board._refuse_login_tokens(json.dumps(package()))

    def test_a_token_in_a_row_never_reaches_a_note(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.md"
            path.write_text(f"| ID | Command | QA result |\n|---|---|---|\n| {self.TOKEN} | tool {self.TOKEN} | N/A {self.TOKEN} |\n", encoding="utf-8")
            _rows, problems, notes = contract.scenario_plan(path)
        self.assertNotIn(self.TOKEN[:24], " ".join([*problems, *notes]))


class ReleaseGate(unittest.TestCase):
    """Codex R1-2: naming one test as not material let an unrelated failing test through."""

    def gate(self, named):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "src"
            for name in ("scripts", "harness", "tests", "directives"):
                (source / name).mkdir(parents=True)
            shutil.copy(root / "scripts" / "make_public_release.sh", source / "scripts")
            shutil.copytree(root / "harness", source / "harness", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
            for name in ("LICENSE", "NOTICE", "DISCLAIMER.md", "CONTRIBUTING.md", "README.md", "install.sh", ".gitignore"):
                (source / name).write_text("x\n", encoding="utf-8")
            (source / "tests" / "test_material.py").write_text(
                "import unittest\nclass Safety(unittest.TestCase):\n    def test_start(self):\n        self.fail('app cannot start')\n", encoding="utf-8")
            allowed = Path(folder) / "allowed.txt"
            allowed.write_text(named + "\n", encoding="utf-8")
            return subprocess.run(["bash", str(source / "scripts" / "make_public_release.sh"), str(Path(folder) / "out")],
                                  capture_output=True, text=True, timeout=120, env={**os.environ, "NOT_MATERIAL": str(allowed)})

    def test_only_the_complete_id_of_the_failing_test_is_accepted(self):
        for other in ("test_cosmetic.Layout.test_start a spacing issue", "test_start", "Safety.test_start reason", "test_other.Safety.test_x test_material.Safety.test_start"):
            with self.subTest(other=other):
                refused = self.gate(other)
                self.assertNotEqual(refused.returncode, 0, refused.stdout[-400:])
                self.assertIn("test_material.Safety.test_start", refused.stderr)
        accepted = self.gate("test_material.Safety.test_start known and not material")
        self.assertEqual(accepted.returncode, 0, accepted.stderr[-400:])


if __name__ == "__main__":
    unittest.main()

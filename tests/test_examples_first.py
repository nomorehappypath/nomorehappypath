# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Examples first, real tests only (task 2026-11-24_tdd-examples-first, Changes 1 to 3).

Before the build the Reviewer gives every requirement an example, lists what the owner asked for against the requirements,
and says what the result must contain; and a stored check is counted as a real test only when it says what it runs and
which example it asserts. On a new app NMHP's advance checks were README word searches shown as "covered"
(Lease: 14 of 14); these tests pin that this can no longer be recorded.

Run:  PYTHONPATH=. python3 -m unittest tests.test_examples_first -v
"""
from __future__ import annotations

import json
import unittest

from harness import board, contract_check, control
from tests.test_contract_first_checks import TASK, TEXT, _Fixture, ledger, write_json

PLAIN = "You see the greeting when you run the program."
EXAMPLE = "Running the program with no argument prints the single word hello."
RUNS_PROGRAM = (
    "import subprocess, sys, unittest\n\n"
    "class Greeting(unittest.TestCase):\n"
    "    def test_prints_hello(self):\n"
    "        out = subprocess.run([sys.executable, 'hello.py'], capture_output=True, text=True).stdout\n"
    "        self.assertEqual(out.strip(), 'hello')\n"
)
# What 10 of Lease's 12 advance checks were: search a delivered text file for words.
READS_README = (
    "import unittest\nfrom pathlib import Path\n\n"
    "class PublishedText(unittest.TestCase):\n"
    "    def test_delivered_text(self):\n"
    "        text = Path('README.md').read_text(encoding='utf-8').lower()\n"
    "        for term in ['progress', 'elapsed', 'expand']:\n"
    "            self.assertIn(term, text)\n"
)
# Lease's real check 1.1: the start script parses. It starts nothing and asserts no example.
SYNTAX_ONLY = (
    "import subprocess\nimport unittest\nfrom pathlib import Path\n\n"
    "class Launcher(unittest.TestCase):\n"
    "    def test_named_scripts(self):\n"
    "        run = subprocess.run(['sh', '-n', str(Path('scripts/start.sh'))], text=True, capture_output=True, timeout=10)\n"
    "        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)\n"
)
ASKS = [{"ask": "The program prints a greeting", "requirements": [1]}]
RESULT = ["The word hello on its own line"]


def check(files: dict, coverage: str = "checked", **said) -> dict:
    """One check of requirement 1 with the given test files, coverage word and optional runs/example/covers."""
    return {"coverage": coverage, "ledger": ledger(), "files": files, **said}


def criterion(checks: list | None = None, **extra) -> dict:
    """One requirement with its plain line and example; with no check it says why."""
    item = {"text": "Running the program prints hello", "plain": PLAIN, "examples": [EXAMPLE], "checks": checks or [],
            "failure_paths": ["a wrong word is printed"], **extra}
    if not checks:
        item["why_not_checked"] = "nothing is built yet"
    return item


def submission(*criteria: dict, **top) -> dict:
    """A whole early look: the given requirements, the owner's asks and the result list (each replaceable through `top`)."""
    return {"criteria": list(criteria), "questions": [], "owner_asks": ASKS, "result_must_contain": RESULT, **top}


def parse(value: dict) -> dict:
    """Parse a submission the way the board does."""
    return contract_check.parse_submission(json.dumps(value))


class ExamplesTests(unittest.TestCase):
    def test_a_requirement_with_no_example_is_refused_and_named(self):
        value = submission(criterion(), {"text": "The greeting is friendly", "plain": PLAIN, "checks": [], "why_not_checked": "a matter of taste"})
        with self.assertRaisesRegex(ValueError, r'criterion 2 needs "examples"'):
            parse(value)

    def test_examples_are_kept_with_the_requirement(self):
        parsed = parse(submission(criterion(examples=[EXAMPLE, "Running it with the argument loud prints HELLO in capital letters."])))
        self.assertEqual(len(parsed["criteria"][0]["examples"]), 2)
        self.assertEqual(contract_check.labels(parsed["criteria"])[0]["examples"][0], EXAMPLE)

    def test_an_example_must_be_plain_words_the_owner_can_read(self):
        for bad in ("too short", "The spec's section 15 coverage table holds for the greeting function.", "x" * 301):
            with self.assertRaisesRegex(ValueError, "criterion 1 example 1"):
                parse(submission(criterion(examples=[bad])))
        with self.assertRaisesRegex(ValueError, 'needs "examples"'):
            parse(submission(criterion(examples=[EXAMPLE] * 5)))

    def test_an_older_record_without_examples_keeps_the_older_rules(self):
        """A task past its contract keeps its rules: a stored requirement with no examples is labelled and covered as before."""
        older = [{"n": 1, "text": "A", "checks": [{"coverage": "partly", "file": "x", "sha256": "y"}], "mapped": True, "failure_paths": ["f"]}]
        contract_check.apply_coverage(older)
        self.assertEqual(older[0]["coverage_line"], contract_check.COVERED)
        self.assertEqual(contract_check.criterion_label(older[0]), contract_check.PARTLY_CHECKED)
        self.assertEqual(contract_check.real_test_line(older), "")


class OnlyARealTestIsCountedTests(unittest.TestCase):
    def test_a_check_that_only_reads_the_readme_is_refused_for_an_app_requirement(self):
        for coverage in ("checked", "partly"):
            with self.assertRaisesRegex(ValueError, "criterion 1 check 1 only reads files"):
                parse(submission(criterion([check({"test_readme.py": READS_README}, coverage)])))

    def test_the_same_check_is_accepted_for_a_document_requirement_and_is_not_an_app_test(self):
        parsed = parse(submission(criterion([check({"test_readme.py": READS_README}, "partly")], kind="document")))
        item = parsed["criteria"][0]
        self.assertEqual(item["kind"], "document")
        self.assertEqual(item["coverage_line"], contract_check.COVERED)
        self.assertEqual(contract_check.real_test_line(parsed["criteria"]), "", "a document check is not part of the count of app tests")
        mixed = parse(submission(criterion(), criterion([check({"test_readme.py": READS_README}, "partly")], kind="document")))
        self.assertTrue(contract_check.real_test_line(mixed["criteria"]).startswith("0 of 1 requirements"))

    def test_a_syntax_only_check_cannot_be_recorded_as_a_real_test(self):
        """Lease's check 1.1 ran `sh -n scripts/start.sh`: it runs a command, so no filter catches it, and it tests nothing of the app."""
        with self.assertRaisesRegex(ValueError, "marked checked but does not say what it runs and which example it asserts"):
            parse(submission(criterion([check({"test_contract.py": SYNTAX_ONLY})])))
        parsed = parse(submission(criterion([check({"test_contract.py": SYNTAX_ONLY}, "partly")])))
        item = parsed["criteria"][0]
        self.assertFalse(contract_check.criterion_counted(item))
        self.assertEqual(contract_check.criterion_label(item), contract_check.PARTLY_CHECKED)
        self.assertEqual(item["coverage_line"], contract_check.GAP_NOT_COUNTED)
        self.assertNotEqual(item["coverage_line"], contract_check.COVERED)
        self.assertTrue(contract_check.real_test_line(parsed["criteria"]).startswith("0 of 1 requirements have a real test before the build"))

    def test_a_check_that_says_what_it_runs_and_which_example_it_asserts_is_counted(self):
        parsed = parse(submission(criterion([check({"test_hello.py": RUNS_PROGRAM}, runs="python3 hello.py", example=1)])))
        item = parsed["criteria"][0]
        self.assertTrue(contract_check.criterion_counted(item))
        self.assertEqual(item["checks"][0]["runs"], "python3 hello.py")
        self.assertEqual(item["checks"][0]["example"], 1)
        self.assertEqual(item["coverage_line"], contract_check.COVERED)
        self.assertEqual(contract_check.real_test_line(parsed["criteria"]),
                         "1 of 1 requirements have a real test before the build; the others are tested for real at the final review.")

    def test_an_example_number_the_requirement_does_not_have_is_refused(self):
        with self.assertRaisesRegex(ValueError, "example must be the number of one of the requirement's examples"):
            parse(submission(criterion([check({"test_hello.py": RUNS_PROGRAM}, runs="python3 hello.py", example=2)])))

    def test_a_requirement_with_examples_and_no_check_is_a_gap_and_not_in_the_count(self):
        parsed = parse(submission(criterion(), criterion([check({"test_hello.py": RUNS_PROGRAM}, runs="python3 hello.py", example=1)])))
        self.assertEqual([item["coverage_line"] for item in parsed["criteria"]], [contract_check.GAP_NO_TEST, contract_check.COVERED])
        self.assertTrue(contract_check.real_test_line(parsed["criteria"]).startswith("1 of 2 requirements"))

    def test_also_covers_counts_only_from_a_counted_check(self):
        """Lease's requirements 11 and 12 had no check and were shown covered because a README search said it also covered them."""
        partly = parse(submission(criterion([check({"test_contract.py": SYNTAX_ONLY}, "partly", covers=[2])]), criterion()))
        self.assertEqual(partly["criteria"][1]["coverage_line"], contract_check.GAP_NO_TEST)
        counted = parse(submission(criterion([check({"test_hello.py": RUNS_PROGRAM}, runs="python3 hello.py", example=1, covers=[2])]), criterion()))
        self.assertEqual(counted["criteria"][1]["coverage_line"], contract_check.COVERED)

    def test_more_than_twelve_checks_fit_and_the_wait_is_thirty_minutes_by_default(self):
        many = [criterion([check({"test_hello.py": RUNS_PROGRAM}, runs="python3 hello.py", example=1)]) for _ in range(13)]
        self.assertEqual(len(parse(submission(*many))["criteria"]), 13)
        self.assertEqual(contract_check.WAIT_DEFAULT_SECONDS, 1800)


class OwnerAsksAndResultTests(unittest.TestCase):
    def test_an_ask_no_requirement_covers_is_named_and_everything_found_is_never_inferred(self):
        parsed = parse(submission(criterion(), owner_asks=ASKS + [{"ask": "It must also work on my phone", "requirements": []}]))
        self.assertEqual(contract_check.asks_not_covered(parsed["owner_asks"]), ["It must also work on my phone"])
        self.assertEqual(contract_check.asks_line(parsed["owner_asks"], parsed["not_read"]),
                         "In your direction, not in the requirements: It must also work on my phone")
        found = parse(submission(criterion()))
        self.assertEqual(contract_check.asks_line(found["owner_asks"], found["not_read"]),
                         "The reviewer found everything you asked for in the requirements (1 points).")

    def test_a_source_the_reviewer_could_not_read_is_named_instead_of_everything_found(self):
        parsed = parse(submission(criterion(), not_read=["lease-scan.pdf"]))
        line = contract_check.asks_line(parsed["owner_asks"], parsed["not_read"])
        self.assertIn("could not read: lease-scan.pdf", line)
        self.assertNotIn("everything you asked for", line)

    def test_the_asks_and_the_result_list_are_required_and_bounded(self):
        with self.assertRaisesRegex(ValueError, 'list under "owner_asks"'):
            parse({"criteria": [criterion()], "result_must_contain": RESULT})
        with self.assertRaisesRegex(ValueError, "requirements must list existing requirement numbers"):
            parse(submission(criterion(), owner_asks=[{"ask": "The program prints a greeting", "requirements": [2]}]))
        with self.assertRaisesRegex(ValueError, 'list under "result_must_contain"'):
            parse({"criteria": [criterion()], "owner_asks": ASKS})
        with self.assertRaisesRegex(ValueError, 'list under "result_must_contain"'):
            parse(submission(criterion(), result_must_contain=[f"Result part number {n} is present" for n in range(13)]))
        self.assertEqual(parse(submission(criterion()))["result_must_contain"], RESULT, "one result item is enough for a small program")

    def test_the_reviewer_is_told_what_to_write_and_where_the_owners_files_are(self):
        text = contract_check.reviewer_instruction("T", 1, "requirements", "direction", 30, sources=["/work/task/supporting-documents/BRIEF.md"])
        for words in ('"examples"', '"owner_asks"', '"result_must_contain"', "/work/task/supporting-documents/BRIEF.md", '"not_read"'):
            self.assertIn(words, text)
        self.assertIn("he attached no file", contract_check.reviewer_instruction("T", 1, "requirements", "direction", 30))


class BoardKeepsThemTests(_Fixture):
    """The board stores the new fields with the proposal and the confirmed contract, through a correction, and tells the Builder once."""

    def value(self, **top) -> dict:
        return submission(criterion([check({"test_hello.py": RUNS_PROGRAM}, runs="python3 hello.py", example=1)]),
                          criterion(text="The greeting can be made loud", examples=["Running it with the argument loud prints HELLO in capital letters."]), **top)

    def test_the_early_look_is_stored_with_examples_lists_and_what_each_check_runs(self):
        agent = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        board.record_contract_check_submission(self.root, reviewer["id"], TASK, 1, write_json(self.base, "s.json", self.value()))
        check_block = self.proposal()["contract_check"]
        self.assertEqual(check_block["criteria"][0]["examples"], [EXAMPLE])
        self.assertEqual({key: check_block["criteria"][0]["checks"][0][key] for key in ("runs", "example")}, {"runs": "python3 hello.py", "example": 1})
        self.assertEqual(check_block["owner_asks"], ASKS)
        self.assertEqual(check_block["result_must_contain"], RESULT)
        stored = json.loads(open(check_block["criteria"][0]["checks"][0]["file"], encoding="utf-8").read())
        self.assertEqual((stored["runs"], stored["example"]), ("python3 hello.py", 1), "kept inside the stored check file too")
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=check_block["token"])
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        block = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]
        self.assertEqual(block["rules"], 2)
        self.assertEqual(block["result_must_contain"], RESULT)
        self.assertEqual(block["asks_line"], "The reviewer found everything you asked for in the requirements (1 points).")
        self.assertTrue(block["real_tests"].startswith("1 of 2 requirements have a real test before the build"))
        self.assertEqual(block["labels"][1]["examples"], ["Running it with the argument loud prints HELLO in capital letters."])
        self.assertEqual({key: block["checks"][0][key] for key in ("runs", "example")}, {"runs": "python3 hello.py", "example": 1})

    def test_the_builder_is_told_once_about_an_ask_no_requirement_covers_and_may_revise_once(self):
        agent = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        control.take_instructions(self.root, agent["session_id"])
        value = self.value(owner_asks=ASKS + [{"ask": "It must also work on my phone", "requirements": []}])
        board.record_contract_check_submission(self.root, reviewer["id"], TASK, 1, write_json(self.base, "s.json", value))
        told = [item["text"] for item in control.take_instructions(self.root, agent["session_id"]) if item.get("source") == "contract-check"]
        self.assertEqual(len(told), 1)
        self.assertIn("It must also work on my phone", told[0])
        self.assertEqual(self.proposal()["status"], "awaiting_owner", "Go ahead is never held by it")
        board.record_requirement_proposal(self.root, agent["id"], TEXT + " It also works on a phone.")     # the one revision
        self.assertEqual(self.proposal()["version"], 2)
        with self.assertRaisesRegex(ValueError, "already awaiting the owner's decision"):
            board.record_requirement_proposal(self.root, agent["id"], TEXT + " Again.")

    def test_a_correction_follows_the_same_rule_and_keeps_what_the_check_runs(self):
        agent = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        board.record_contract_check_submission(self.root, reviewer["id"], TASK, 1, write_json(self.base, "s.json", self.value()))
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        weak = write_json(self.base, "weak.json", {"checks": [check({"test_contract.py": SYNTAX_ONLY})]})
        with self.assertRaisesRegex(ValueError, "marked checked but does not say what it runs"):
            board.record_contract_check_correction(self.root, reviewer["id"], TASK, 2, "adding the first check for the loud greeting", weak)
        good = write_json(self.base, "good.json", {"checks": [check({"test_loud.py": RUNS_PROGRAM}, runs="python3 hello.py loud", example=1)],
                                                   "failure_paths": ["the word stays in small letters"]})
        board.record_contract_check_correction(self.root, reviewer["id"], TASK, 2, "adding the first check for the loud greeting", good)
        block = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]
        added = [item for item in block["checks"] if item["criterion_index"] == 2][0]
        self.assertEqual((added["runs"], added["example"]), ("python3 hello.py loud", 1))
        self.assertTrue(block["real_tests"].startswith("2 of 2 requirements"), block["real_tests"])
        self.assertEqual(self.proposal()["contract_check"]["criteria"][1]["checks"][0]["runs"], "python3 hello.py loud")

    def test_the_reviewer_is_pointed_at_the_owners_files_in_the_tasks_own_folder(self):
        agent = self.delivery()
        session, _reviewer = self.reviewer()
        folder = self.base / "task-work" / board.SUPPORTING_DOCUMENTS_DIRNAME
        folder.mkdir(parents=True)
        (folder / "BRIEF.md").write_text("what the owner wants", encoding="utf-8")
        with board.locked_state(self.root) as state:
            state.setdefault("task_workspaces", {})[TASK] = str(self.base / "task-work")
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        wake = [item["text"] for item in control.take_instructions(self.root, session["id"]) if item.get("source") == "contract-check"]
        self.assertEqual(len(wake), 1)
        self.assertIn(str(folder / "BRIEF.md"), wake[0])


if __name__ == "__main__":
    unittest.main()

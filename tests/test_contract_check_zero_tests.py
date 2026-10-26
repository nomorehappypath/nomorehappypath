# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A stored check that runs 0 tests "did not run": never a pass, never a failure of the work, never a block (task 2026-10-24_contract-check-zero-tests).

The Studio incident: the Reviewer's command carried escaped quotes (-p \\"test_*.py\\"), so the test finder looked for files whose names contain
quote marks, found none, printed "Ran 0 tests ... OK", and the board recorded a failed attempt that then also refused every retry.
Run:  PYTHONPATH=. python3 -m unittest tests.test_contract_check_zero_tests -v
"""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from harness import board
from tests import test_contract_first_checks as base
from tests.test_contract_first_checks import TASK, TEST_FILE, TEXT, ledger, submission, write_json

StoredChecksRunTests = base.StoredChecksRunTests
FinalReviewFlowTests = base.FinalReviewFlowTests

ESCAPED = ledger().replace('-p "test_*.py"', '-p \\"test_*.py\\"')
NO_MATCH = ledger().replace('-p "test_*.py"', "-p nomatch_*.py")


class ZeroTestsRunTests(unittest.TestCase):
    setUp = StoredChecksRunTests.setUp
    delivery = StoredChecksRunTests.delivery
    reviewer = StoredChecksRunTests.reviewer
    proposal = StoredChecksRunTests.proposal
    submit = StoredChecksRunTests.submit
    committed_repo = StoredChecksRunTests.committed_repo
    run_checks = StoredChecksRunTests.run_checks

    def confirmed_with(self, ledger_text):
        agent = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        value = submission()
        value["criteria"][0]["checks"][0]["ledger"] = ledger_text if '\\"' not in ledger_text else ledger()
        self.submit(reviewer, value)
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        if '\\"' in ledger_text:                      # stored the way the Studio check was (older checks are not re-linted)
            with board.locked_state(self.root) as state:
                entry = state["requirement_confirmations"][TASK]["contract_check"]["checks"][0]
                data = json.loads(Path(entry["file"]).read_text(encoding="utf-8"))
                data["ledger"] = ledger_text
                raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
                Path(entry["file"]).write_bytes(raw)
                entry["sha256"] = hashlib.sha256(raw).hexdigest()
                state["requirement_confirmations"][TASK]["contract_check"]["checks_sha256"] = board._checks_digest([entry])
        return agent, reviewer

    def results(self):
        return board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]

    def test_a_new_submission_with_escaped_quotes_is_refused_with_the_fix(self):
        agent = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        value = submission()
        value["criteria"][0]["checks"][0]["ledger"] = ESCAPED
        with self.assertRaisesRegex(ValueError, "escaped quotes"):
            self.submit(reviewer, value)

    def test_the_studio_check_with_escaped_quotes_now_finds_and_runs_the_tests(self):
        self.confirmed_with(ESCAPED)
        _repo, commit = self.committed_repo("hello")
        run = self.run_checks(commit)
        self.assertEqual(run["run"]["status"], "passed", run["run"])
        self.assertEqual(run["bundle"]["scenario_ids"], ["S-001"])

    def test_a_check_that_runs_no_tests_did_not_run_it_is_not_a_failure_and_a_retry_is_never_refused(self):
        self.confirmed_with(NO_MATCH)
        _repo, commit = self.committed_repo("hello")
        run = self.run_checks(commit)                                     # on main this raised "reported zero executed tests"
        self.assertEqual(run["run"]["status"], "partly")
        self.assertIn("found no tests to run", run["run"]["did_not_run"]["1"])
        self.assertIn("says nothing about the work", run["run"]["did_not_run"]["1"])
        self.assertEqual(self.results()["results"], {"1": "did_not_run"})
        again = self.run_checks(commit)                                   # on main: "retrying a failed certified command requires a repair reason"
        self.assertEqual(again["run"]["status"], "partly")

    def repaired_check_runs_with(self, extras):
        self.confirmed_with(ESCAPED)
        _repo, commit = self.committed_repo("hello", extras)
        run = self.run_checks(commit)
        # bare, the shell expanded test_*.py: one other module made the check run zero tests, two made unittest raise "not importable"
        self.assertEqual(run["run"]["status"], "passed", run["run"])
        self.assertEqual(run["bundle"]["scenario_ids"], ["S-001"])

    def test_the_repaired_pattern_stays_one_literal_argument_with_one_test_module_at_the_repository_root(self):
        self.repaired_check_runs_with({"test_views.py": "x = 1\n"})

    def test_the_repaired_pattern_stays_one_literal_argument_with_two_test_modules_at_the_repository_root(self):
        self.repaired_check_runs_with({"test_views.py": "x = 1\n", "test_models.py": "x = 2\n"})

    def test_a_pattern_quoted_the_ordinary_way_is_the_control(self):
        self.confirmed_with(ledger())
        _repo, commit = self.committed_repo("hello", {"test_views.py": "x = 1\n", "test_models.py": "x = 2\n"})
        self.assertEqual(self.run_checks(commit)["run"]["status"], "passed")

    def test_the_refusal_for_new_submissions_tells_the_reviewer_to_quote_the_pattern(self):
        agent = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        value = submission()
        value["criteria"][0]["checks"][0]["ledger"] = ESCAPED
        with self.assertRaisesRegex(ValueError, r'quote the pattern.*-p "test_\*\.py"'):
            self.submit(reviewer, value)

    def test_a_real_failure_beats_a_command_that_found_no_tests_in_the_same_check(self):
        two_rows = ledger() + NO_MATCH.splitlines()[-1].replace("S-001", "S-002") + "\n"
        self.confirmed_with(two_rows)
        _repo, commit = self.committed_repo("goodbye")                 # the first command really fails; the second finds no tests
        with self.assertRaisesRegex(ValueError, "failed"):
            self.run_checks(commit)
        self.assertEqual(self.results()["results"], {"1": "failed"})

    def test_escaped_quotes_elsewhere_are_real_arguments_and_are_left_alone(self):
        self.assertEqual(board._repair_escaped_pattern('python3 -m unittest discover -s x -p \\"test_*.py\\"'), 'python3 -m unittest discover -s x -p "test_*.py"')
        other = 'python3 -m unittest -k \\"one two\\"'
        self.assertEqual(board._repair_escaped_pattern(other), other)

    def test_delivery_or_the_cto_can_skip_only_a_check_that_could_not_run(self):
        agent, reviewer = self.confirmed_with(NO_MATCH)
        _repo, commit = self.committed_repo("hello")
        with self.assertRaisesRegex(ValueError, "did not run"):
            board.record_contract_check_skip(self.root, agent["id"], TASK, 1, "I would like to skip it")   # it has not run yet
        self.run_checks(commit)
        before = self.results()["checks_sha256"]
        with self.assertRaisesRegex(ValueError, "at least 8"):
            board.record_contract_check_skip(self.root, agent["id"], TASK, 1, "stale")
        with self.assertRaisesRegex(ValueError, "only Delivery or the CTO"):
            board.record_contract_check_skip(self.root, reviewer["id"], TASK, 1, "the reviewer has its own way")
        board.record_contract_check_skip(self.root, agent["id"], TASK, 1, "the check finds no tests; the work has its own 14 tests")
        block = self.results()
        self.assertEqual(block["checks"], [])
        self.assertNotEqual(block["checks_sha256"], before)
        self.assertEqual(block["skipped"][0]["reason"], "the check finds no tests; the work has its own 14 tests")
        self.assertEqual({item["label"] for item in block["labels"]}, {"not checked in advance"})
        self.assertEqual(block["labels"][0]["note"], "the check finds no tests; the work has its own 14 tests")

    def test_a_passing_check_cannot_be_skipped(self):
        agent, _reviewer = self.confirmed_with(ledger())
        _repo, commit = self.committed_repo("hello")
        self.assertEqual(self.run_checks(commit)["run"]["status"], "passed")
        with self.assertRaisesRegex(ValueError, "did not run"):
            board.record_contract_check_skip(self.root, agent["id"], TASK, 1, "I want to skip a passing check")


class RequestReviewIsNotBlockedTests(unittest.TestCase):
    setUp = FinalReviewFlowTests.setUp
    request = FinalReviewFlowTests.request

    def confirm(self, ledger_text, expected="hello", test_file=None):
        board.record_requirement_proposal(self.root, self.dev["id"], TEXT)
        files = {"test_hello.py": test_file or TEST_FILE.replace("'hello'", repr(expected))}
        value = {"criteria": [{"text": "Running the program prints the greeting", "checks": [{"coverage": "checked", "ledger": ledger_text, "files": files}]}]}
        board.record_contract_check_submission(self.root, self.qa["id"], TASK, 1, write_json(self.base, "sub.json", value))
        token = board.snapshot(self.root)["requirement_proposals"][TASK]["contract_check"]["token"]
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=token)
        board.record_requirement_confirmation(self.root, self.dev["id"], TEXT)
        board.define_delivery_plan(self.root, self.dev["id"], "atomic", "One cohesive task")

    def test_a_check_that_could_not_run_does_not_block_the_final_review_request(self):
        self.confirm(NO_MATCH)
        request = self.request()                                          # on main: refused, three staged reviews cancelled
        self.assertEqual(request["status"], "open")
        run = request["contract_check_runs"][0]
        self.assertEqual(run["status"], "partly")
        self.assertIn("1", run["did_not_run"])

    def refused_and_never_skippable(self, test_file, why):
        """Broken application setup is a real failure: the request is refused, the result is 'failed', and nothing is skip-eligible."""
        self.confirm(ledger(), test_file=test_file)
        with self.assertRaises(ValueError, msg=why):
            self.request()
        block = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]
        self.assertNotIn("did_not_run", set((block.get("results") or {}).values()), why)
        with self.assertRaises(ValueError):
            board.record_contract_check_skip(self.root, self.dev["id"], TASK, 1, "the check seems broken to me")
        self.assertEqual(board.snapshot(self.root)["qa_requests"], {}, "no review was opened")

    def test_a_setup_error_in_the_class_is_a_failure_not_a_check_that_did_not_run(self):
        self.refused_and_never_skippable(
            "import unittest\n\nclass Setup(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n        raise RuntimeError('application failed to start')\n"
            "    def test_it(self):\n        pass\n", "setUpClass raising prints 'Ran 0 tests ... FAILED (errors=1)'")

    def test_a_setup_error_in_the_module_is_a_failure(self):
        self.refused_and_never_skippable(
            "import unittest\n\ndef setUpModule():\n    raise RuntimeError('application failed to start')\n\nclass T(unittest.TestCase):\n    def test_it(self):\n        pass\n",
            "setUpModule raising")

    def test_an_import_error_while_collecting_the_tests_is_a_failure(self):
        self.refused_and_never_skippable("import module_that_does_not_exist\n\nimport unittest\n\nclass T(unittest.TestCase):\n    def test_it(self):\n        pass\n",
                                         "ModuleNotFoundError at collection")

    def test_a_runner_that_dies_and_prints_nothing_is_a_failure(self):
        self.refused_and_never_skippable("import os\nos._exit(3)\n", "a command that printed nothing and exited non-zero")

    def test_a_syntax_error_in_a_test_file_is_a_failure(self):
        self.refused_and_never_skippable("def broken(:\n", "SyntaxError in discovery")

    def test_the_studio_command_opens_the_review_with_other_test_modules_at_the_repository_root(self):
        self.confirm(ledger())
        with board.locked_state(self.root) as state:      # stored the way the Studio check was
            block = state["requirement_confirmations"][TASK]["contract_check"]
            entry = block["checks"][0]
            data = json.loads(Path(entry["file"]).read_text(encoding="utf-8"))
            data["ledger"] = ESCAPED
            raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
            Path(entry["file"]).write_bytes(raw)
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            block["checks_sha256"] = board._checks_digest([entry])
        request = self.request()                           # the root holds test_smoke.py, test_views.py and test_models.py
        self.assertEqual(request["status"], "open")
        self.assertEqual(request["contract_check_runs"][0]["status"], "passed")

    def test_a_real_failure_reaches_the_cto_with_what_ran_what_it_saw_and_what_to_do(self):
        self.confirm(ledger(), expected="goodbye")
        with self.assertRaises(ValueError):
            self.request()
        events = [e for e in board.snapshot(self.root)["events"] if e["kind"] == "staged_review_cancelled"]
        message = events[-1]["message"]
        self.assertIn("Running the program prints the greeting", message)
        self.assertIn("What to do:", message)
        self.assertIn("contract-check-skip", message)


class ActiveRepairSkipTests(unittest.TestCase):
    """The skip works through the authenticated surface while the owner's repair is under way, and nowhere it must not (review R2-1)."""
    session = base.SurfaceTests.session
    setUp = base.SurfaceTests.setUp

    def prepare(self):
        from harness import control
        dsession, delivery, dtoken, dgateway = self.session("codex_delivery", "engineering", board.AWAITING_OWNER_DIRECTION)
        rsession, reviewer, _rtoken, _rgateway = self.session("claude_reviewer", "qa", "REVIEW_QUEUE")
        csession, cto, ctoken, cgateway = self.session("claude_cto", "cto", "GLOBAL_MONITOR")
        board.record_owner_direction(self.context, dsession["id"], f"OWNER DIRECTION — {TASK}: build the thing.")
        board.begin_task(self.context, delivery["id"], TASK)
        board.record_requirement_proposal(self.context, delivery["id"], TEXT)
        check = {"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE}}
        value = {"criteria": [{"text": "Running the program prints hello", "checks": [check]},
                              {"text": "The program exits cleanly", "checks": [check]}], "questions": []}
        path = Path(self._tmp.name) / "s.json"
        path.write_text(json.dumps(base.plain_ready(value)), encoding="utf-8")
        board.record_contract_check_submission(self.context, reviewer["id"], TASK, 1, str(path))
        token = board.snapshot(self.context)["requirement_proposals"][TASK]["contract_check"]["token"]
        board.record_requirements_decision(self.context, TASK, "go_ahead", version=1, token=token)
        board.record_requirement_confirmation(self.context, delivery["id"], board.snapshot(self.context)["requirement_proposals"][TASK]["text"])
        with board.locked_state(self.context) as state:
            state["requirement_confirmations"][TASK]["contract_check"]["results"] = {"1": "did_not_run", "2": "did_not_run"}
        return (delivery, dtoken, dgateway), (cto, ctoken, cgateway)

    def skip(self, who, number, reason="the check finds no tests; the work has its own tests", task=TASK, nonce=None):
        agent, token, gateway = who
        self.nonce = getattr(self, "nonce", 0) + 1   # a session's commands must arrive in order
        from harness.board_surface import PROTOCOL_VERSION
        return gateway.execute(token, {"protocol": PROTOCOL_VERSION, "nonce": self.nonce, "operation": "contract-check-skip",
                                       "arguments": ["contract-check-skip", "--agent", agent["id"], "--task", task, "--criterion", str(number), "--reason", reason]})

    def release_then(self, repair=None, accepted=False):
        with board.locked_state(self.context) as state:
            state.setdefault("releases", {})[TASK] = {"task": TASK, "status": "VISUAL_TEST_REQUIRED", "recorded_at": "2026-10-24T10:00:00+00:00", "cto_id": "cto-0000",
                                                      "head_commit": "a" * 40, "checks": {}, "owner_test_steps": [], "lifecycle": {}}
            if repair:
                state.setdefault("release_repairs", {})[TASK] = {"task": TASK, "status": repair}
                state.setdefault("release_decisions", {})[TASK] = {"task": TASK, "decision": "not_accepted", "recorded_at": "2026-10-24T11:00:00+00:00"}
            if accepted:
                state.setdefault("release_decisions", {})[TASK] = {"task": TASK, "decision": "accepted", "recorded_at": "2026-10-24T11:00:00+00:00"}
                state.setdefault("git_acceptances", {})[TASK] = {"commit": "a" * 40}

    def test_delivery_and_the_cto_can_skip_during_the_owners_repair(self):
        delivery, cto = self.prepare()
        self.release_then(repair="OWNER_REJECTED_REPAIR_REQUIRED")      # released, the owner pressed Not Accepted, Delivery repairs
        self.assertIn("criterion", json.dumps(self.skip(delivery, 1, nonce=10)))
        self.assertIn("criterion", json.dumps(self.skip(cto, 2, nonce=11)))
        block = board.snapshot(self.context)["requirement_confirmations"][TASK]["contract_check"]
        self.assertEqual([item["by"] for item in block["skipped"]], [delivery[0]["id"], cto[0]["id"]])
        self.assertEqual(block["checks"], [])

    def test_the_helper_is_the_effective_lifecycle(self):
        self.prepare()
        state = board.snapshot(self.context)
        self.assertFalse(board._task_closed_for_checks(state, TASK))                                  # not released yet
        self.release_then()
        state = board.snapshot(self.context)
        self.assertTrue(board._task_finished(state, TASK) and board._task_closed_for_checks(state, TASK))   # released, no repair
        with board.locked_state(self.context) as raw:
            raw["release_repairs"] = {TASK: {"status": "DELIVERY_REPAIR_IN_PROGRESS"}}
        state = board.snapshot(self.context)
        self.assertTrue(board._task_finished(state, TASK))                                            # the old test still says finished
        self.assertFalse(board._task_closed_for_checks(state, TASK))                                  # the effective lifecycle does not
        with board.locked_state(self.context) as raw:
            raw["cancelled_tasks"] = {TASK: {"at": "now"}}
        self.assertTrue(board._task_closed_for_checks(board.snapshot(self.context), TASK))

    def test_still_refused_after_accept_when_released_without_repair_for_another_task_or_without_a_reason(self):
        delivery, cto = self.prepare()
        self.release_then(accepted=True)
        for who in (delivery, cto):
            with self.assertRaisesRegex(ValueError, "finished"):
                self.skip(who, 1, nonce=20 + len(who[0]["id"]))
        with board.locked_state(self.context) as state:             # back to an active repair: the other protections still hold
            state["release_decisions"] = {TASK: {"task": TASK, "decision": "not_accepted", "recorded_at": "2026-10-24T11:00:00+00:00"}}
            state["git_acceptances"] = {}
            state["release_repairs"] = {TASK: {"status": "OWNER_REJECTED_REPAIR_REQUIRED"}}
        with self.assertRaisesRegex(ValueError, "at least 8"):
            self.skip(delivery, 1, reason="stale", nonce=30)
        with self.assertRaisesRegex(ValueError, "own task"):
            self.skip(delivery, 1, task="TASK-OTHER", nonce=31)
        with board.locked_state(self.context) as state:             # a check that ran is never skip-eligible
            state["requirement_confirmations"][TASK]["contract_check"]["results"]["1"] = "failed"
        with self.assertRaisesRegex(ValueError, "did not run"):
            self.skip(delivery, 1, nonce=32)


if __name__ == "__main__":
    unittest.main()

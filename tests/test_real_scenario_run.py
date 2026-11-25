# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The real run is the owner's whole scenario, A to Z, for every project (task 2026-11-23_real-scenario-run).

Owner 2026-11-23: "2 things NMHP should do, test if it has access to the agents to build, spec or review. then if the final app
needs agents, it has to run the final app with the agents as if it life with cheap models", and "when testing an app that needs
an agent it set the model in the setting". One test per acceptance criterion of the spec; each fails on v0.30.3.

Run:  PYTHONPATH=. python3 -m unittest tests.test_real_scenario_run -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from harness import board, control, environment_check, global_settings, owner_env, review_rounds
from tests import test_environment_check as setup_case
from tests import test_frozen_review_scope as scope
from tests.test_not_run_goes_to_repair import not_run

SHELLS = setup_case.SHELLS


@unittest.skipUnless(SHELLS, "needs zsh or bash")
class GateOneAgentsBeforeAnyWork(setup_case.Case):
    """A new direction: every agent's sign-in is checked before the Builder is woken or may begin."""

    def setUp(self):
        super().setUp()
        with board.locked_state(self.root) as state:
            state["agents"]["engineering-2"] = {**state["agents"][setup_case.DEV], "id": "engineering-2", "session_id": "s-new",
                                                "task": board.AWAITING_OWNER_DIRECTION}

    def test_a_reviewer_that_cannot_sign_in_holds_the_builder_until_check_again_passes(self):
        setup_case._script(self.bin / "codex", 'echo "Not logged in" >&2\nexit 1\n')    # the Reviewer's provider by default
        board.record_owner_direction(self.root, "s-new", "Build the lease app.", check_agents=True)
        key = environment_check.agents_key("s-new")
        with self.assertRaisesRegex(ValueError, "every agent can sign in"):
            board.begin_task(self.root, "engineering-2", "lease-two")
        environment_check.run_now(self.root, key, "agents")
        run = environment_check.run_of(self.state(), key, "agents")
        self.assertEqual(run["status"], "failed", run["items"])
        cards = [card for card in self.open_setup_cards() if card.get("setup_stage") == "agents"]
        self.assertTrue(any("Reviewer" in card["title"] for card in cards), cards)
        woken = [call.args[1] for call in control.enqueue_instruction.call_args_list if call.args[1] == "s-new"]
        self.assertEqual(woken, [], "the Builder is not woken while the check fails")
        with self.assertRaisesRegex(ValueError, "Not logged in"):
            board.begin_task(self.root, "engineering-2", "lease-two")
        setup_case._script(self.bin / "codex", 'echo "OK"\n')                            # the owner signs in, presses Check again
        board.owner_action_choose(self.root, cards[0]["id"], environment_check.CHECK_AGAIN)
        environment_check.run_now(self.root, key, "agents")
        self.assertEqual(environment_check.run_of(self.state(), key, "agents")["status"], "passed")
        woken = [call.args for call in control.enqueue_instruction.call_args_list if call.args[1] == "s-new"]
        self.assertEqual(len(woken), 1)
        self.assertIn("begin the owner's task now", woken[0][2])
        try:
            board.begin_task(self.root, "engineering-2", "lease-two")
        except ValueError as error:          # any later refusal (this test's board has no repository) is not the gate's
            self.assertNotIn("every agent can sign in", str(error))


@unittest.skipUnless(SHELLS, "needs zsh or bash")
class GateTwoTheWholeScenario(setup_case.Case):

    def real_run(self, **options) -> dict:
        values = owner_env.read(fresh=True)
        return environment_check.real_run(f"{sys.executable} realcheck.py", ["claude"], str(self.workspace), values,
                                          owner_env.redactor(values), real_output="out/result.txt", **options)

    def test_a_proposal_without_the_scenario_or_its_files_is_refused_naming_them(self):
        with self.assertRaisesRegex(ValueError, "real_scenario, real_data"):
            self.propose(self.needs(real_scenario="", real_data=[]))

    def test_a_one_call_preflight_that_writes_no_result_fails(self):
        source = 'import subprocess, sys\nsys.exit(subprocess.run(["claude", "-p", "hi"]).returncode)\n'
        (self.workspace / "realcheck.py").write_text(source)      # the Lease preflight: one hello, no result
        item = self.real_run()
        self.assertFalse(item["ok"])
        self.assertIn("finished without the scenario's result", item["detail"])

    def test_a_result_left_by_an_earlier_run_does_not_count(self):
        self.assertTrue(self.real_run()["ok"])
        (self.workspace / "realcheck.py").write_text('import subprocess\nsubprocess.run(["claude", "-p", "hi"])\n')
        self.assertIn("finished without the scenario's result", self.real_run()["detail"])

    def test_an_app_that_times_out_fails_with_its_own_error(self):
        (self.workspace / "realcheck.py").write_text(
            'import sys\nprint("claude did not answer within the time limit", file=sys.stderr)\nsys.exit(2)\n')
        item = self.real_run()
        self.assertFalse(item["ok"])
        self.assertIn("claude did not answer within the time limit", item["detail"])

    def test_a_passed_scenario_run_keeps_its_result_and_the_review_finds_it(self):
        board.set_needs(self.root, setup_case.DEV, self.needs())
        self.run_stage("contract")
        with self.assertRaisesRegex(ValueError, "real run on"):
            board.environment_review_gate(self.root, setup_case.DEV)       # the review request asks for the run
        run = self.run_stage("review")
        self.assertEqual(run["status"], "passed", run["items"])
        commit = setup_case._git(self.workspace, "rev-parse", "HEAD")
        saved = environment_check.evidence_of(self.state(), setup_case.TASK, commit)
        self.assertTrue(saved and Path(saved).read_text() == "the report", saved)
        self.assertIn(f"real-run/{setup_case.TASK}/{commit}", saved)
        self.assertTrue((Path(saved).parent / "run-output.txt").is_file())
        request = {"task": setup_case.TASK, "reviewed_commit": commit}
        state = self.state()
        self.assertEqual(board._scenario_verdict_problem(state, request, "TASK DONE: YES — report ok"), "")
        self.assertIn("Not tested for real", request["form_notes"][0])
        self.assertIn("never 'not tested for real'", board._scenario_verdict_problem(
            state, request, f"TASK DONE: YES\nScenario result judged: {saved}\nNot run for real: real scenario - no sign-in"))
        self.assertEqual(board._scenario_verdict_problem(state, request, f"TASK DONE: YES\nScenario result judged: {saved} - complete"), "")
        self.assertIn("has not passed", board._scenario_verdict_problem(state, {**request, "reviewed_commit": "0" * 40},
                                                                       f"Scenario result judged: {saved}"))

    def test_the_reference_the_reviewer_is_handed_is_accepted_as_the_judged_result(self):
        """Codex review R3-1: the Projects command surface hands the Reviewer trusted://... in place of the saved path."""
        from harness import board_surface
        board.set_needs(self.root, setup_case.DEV, self.needs())
        self.run_stage("contract")
        with self.assertRaisesRegex(ValueError, "real run on"):
            board.environment_review_gate(self.root, setup_case.DEV)
        self.assertEqual(self.run_stage("review")["status"], "passed")
        commit = setup_case._git(self.workspace, "rev-parse", "HEAD")
        state = self.state()
        saved = environment_check.evidence_of(state, setup_case.TASK, commit)
        handed = board_surface._sanitized_value({"saved_result": saved}, self.root)["saved_result"]
        self.assertTrue(handed.startswith("trusted://"), handed)
        request = {"task": setup_case.TASK, "reviewed_commit": commit}
        self.assertEqual(board._scenario_verdict_problem(state, request, f"Scenario result judged: {handed} - complete", self.root), "")
        forged = "trusted://000000000000" + handed[len("trusted://000000000000"):]
        self.assertEqual(board._scenario_verdict_problem(state, request, f"Scenario result judged: {forged}", self.root), "")
        self.assertIn("Not tested for real", request["form_notes"][0])

    def test_the_settings_test_model_reaches_the_apps_call_and_the_app_is_unchanged(self):
        args = self.base / "claude-args"
        setup_case._script(self.bin / "claude", f'printf "%s\\n" "$@" > "{args}"\n'
                                                'echo \'{"type":"result","is_error":false,"result":"OK"}\'\n')
        before = setup_case._git(self.workspace, "status", "--porcelain", "--untracked-files=no")
        item = self.real_run(models={"claude": "haiku"})
        self.assertTrue(item["ok"], item)
        self.assertIn("--model\nhaiku", args.read_text())
        self.assertEqual(setup_case._git(self.workspace, "status", "--porcelain", "--untracked-files=no"), before)

    def test_an_app_that_names_its_own_codex_model_runs_once_on_the_settings_model(self):
        args = self.base / "codex-args"
        setup_case._script(self.bin / "codex", f'printf "%s\\n" "$@" > "{args}"\necho OK\n')
        (self.workspace / "realcheck.py").write_text(
            'import pathlib, subprocess\n'
            'subprocess.run(["codex", "exec", "-m", "gpt-6.1-sol", "--skip-git-repo-check", "Reply OK"], check=True)\n'
            'pathlib.Path("out").mkdir(exist_ok=True); pathlib.Path("out/result.txt").write_text("the report")\n')
        values = owner_env.read(fresh=True)
        item = environment_check.real_run(f"{sys.executable} realcheck.py", ["codex"], str(self.workspace), values,
                                          owner_env.redactor(values), real_output="out/result.txt", models={"codex": "gpt-6-luna"})
        self.assertTrue(item["ok"], item)
        recorded = args.read_text().splitlines()
        self.assertEqual(recorded, ["exec", "--skip-git-repo-check", "Reply OK", "-m", "gpt-6-luna"])

    def test_the_worker_home_is_where_the_test_model_is_read(self):
        home = self.base / "private-home"
        global_settings.update_agent_settings(home, control.default_agent_settings(), app_test_models={"claude": "sonnet"})
        self.addCleanup(environment_check.configure, environment_check.SETTINGS_HOME)
        environment_check.configure(home)
        self.assertEqual(environment_check.test_models()["claude"], "sonnet")

    def test_the_test_model_is_a_setting_with_the_cheapest_model_by_default(self):
        home = self.base / "settings-home"
        self.assertEqual(global_settings.app_test_models(home)["claude"], "haiku")
        global_settings.update_agent_settings(home, control.default_agent_settings(), app_test_models={"claude": "sonnet"})
        self.assertEqual(global_settings.app_test_models(home)["claude"], "sonnet")


class TheScenarioNotRunIsNeverAPass(scope.Rounds):
    """The one exception to D-024: a `Not run for real: real scenario` finding stays a FAIL; any other not-run line converts."""

    def test_a_scenario_not_run_finding_fails_and_another_not_run_finding_still_converts(self):
        first = self.request()
        scenario = not_run("B1", summary="Not run for real: real scenario - the saved result could not be opened")
        _qa, done = self.verdict(first, "failed", "TASK DONE: NO — scenario not judged", [scenario], certified=True)
        self.assertEqual(done["status"], "failed")
        self.assertTrue(review_rounds.is_scenario_not_run(scenario))
        self.assertFalse(review_rounds.is_scenario_not_run(not_run("B2")))


if __name__ == "__main__":
    unittest.main()

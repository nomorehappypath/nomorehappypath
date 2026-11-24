# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""NMHP checks the project's setup before the contract reaches the owner (task 2026-11-21_app-access-before-start).

Owner 2026-11-21 (Lease): the app could not reach Claude in his test because NMHP started it without his own sign-in, and every
review had used stand-ins. "NMHP reads the requirements, drafts the final requirements, and from these it knows the infra it needs
and here it checks before submitting." Every check runs as the owner, in his own login-shell environment; the tests give that
environment a temporary home with its own .zshrc / .bash_profile and fake CLIs, never the developer's real account.

Run:  PYTHONPATH=. python3 -m unittest tests.test_environment_check -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import board, environment_check, owner_env, release_preview, story

TASK = "lease-app"
DEV = "engineering-1"
LEASE_ERROR = "Failed to authenticate: OAuth session expired and could not be refreshed"
SECRET_URL = "https://reader:Sup3rSecretPw@db.example.invalid/x"
SHELLS = [shell for shell in ("/bin/zsh", "/bin/bash") if os.access(shell, os.X_OK)]


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done.stdout.strip()


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class Case(unittest.TestCase):
    shell = SHELLS[0] if SHELLS else "/bin/sh"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "board"
        self.root.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.write_profile({"OWNER_ONLY_SETTING": "from-his-shell", "SERVICE_URL": SECRET_URL})
        self.claude_ok()
        _script(self.bin / "codex", 'echo "OK"\n')
        self.workspace = self.base / "work"
        self.workspace.mkdir()
        _git(self.workspace, "init", "-q", "-b", "main")
        _git(self.workspace, "config", "user.email", "t@example.invalid")
        _git(self.workspace, "config", "user.name", "T")
        self.commit_app('import subprocess, sys\nsys.exit(subprocess.run(["claude", "-p", "hi"]).returncode)\n')
        patches = [
            mock.patch.object(owner_env, "ACCOUNT", lambda: (self.shell, str(self.home))),
            mock.patch.object(environment_check, "AGENT_CHECK", lambda provider, model, effort: None),
            mock.patch("harness.control.enqueue_instruction", return_value={"id": "wake"}),
            mock.patch.object(environment_check, "run_due", lambda root: []),     # the tests run each check in this thread
            # the agents are launched with the same environment as the owner's shell here (a test can give them another one)
            mock.patch.object(environment_check, "LAUNCH_ENV", lambda: owner_env.read(fresh=True)),
            # where the agents' reads are confined (Linux), the fixture's folders are in their grant, as the owner would set it
            mock.patch.dict(os.environ, {"HARNESS_AGENT_READ_PATHS": str(self.base)}),
        ]
        for patch in patches:
            started = patch.start()
            self.addCleanup(patch.stop)
        owner_env._cache.clear()
        with board.locked_state(self.root) as state:
            state.setdefault("agents", {})[DEV] = {"id": DEV, "role": "engineering", "active": True, "task": TASK, "session_id": "s-dev",
                                                   "vendor": "anthropic", "spawned_at": board.now(), "poll_counter": 0, "status": "working",
                                                   "status_note": "", "last_status_at": board.now(), "name": "Delivery"}
            state.setdefault("task_owner_directions", {})[TASK] = "Build the lease app that calls Claude and Codex."
            state.setdefault("task_kinds", {})[TASK] = "development"
            state.setdefault("task_workspaces", {})[TASK] = str(self.workspace)

    # ------------------------------------------------------------------ fixtures
    def write_profile(self, extra: dict[str, str]) -> None:
        lines = [f'export PATH="{self.bin}:$PATH"'] + [f"export {name}='{value}'" for name, value in extra.items()]
        for name in (".zshrc", ".bash_profile"):
            (self.home / name).write_text("\n".join(lines) + "\n")
        owner_env._cache.clear()

    def claude_ok(self) -> None:
        _script(self.bin / "claude", 'echo \'{"type":"result","is_error":false,"result":"OK"}\'\n')

    def claude_signed_out(self) -> None:
        # the Lease failure word for word, with the owner's database URL repeated in it (it must never reach a card)
        _script(self.bin / "claude", f'echo \'{{"type":"result","is_error":true,"result":"{LEASE_ERROR} ({SECRET_URL})"}}\'\nexit 1\n')

    def commit_app(self, source: str) -> str:
        # every sample app writes its finished result first (task 2026-11-23_real-scenario-run: the run must leave one)
        (self.workspace / "realcheck.py").write_text(RESULT_LINE + source)
        _git(self.workspace, "add", "realcheck.py")
        _git(self.workspace, "commit", "-qm", "app")
        return _git(self.workspace, "rev-parse", "HEAD")

    def needs(self, **extra) -> str:
        value = {"ai_tools": ["claude"], "folders": [{"path": str(self.base), "access": "read"}],
                 "programs": [{"name": "sh"}], "libraries": [{"language": "python", "name": "json"}],
                 "real_run": f"{sys.executable} realcheck.py", "real_scenario": "the lease in, the tenant report out",
                 "real_data": ["realcheck.py"], "real_output": "out/result.txt"}
        value.update(extra)
        return json.dumps(value)

    def propose(self, needs=None, required=True):
        return board.record_requirement_proposal(self.root, DEV, "Build the lease app.", needs=needs, needs_required=required)

    def state(self) -> dict:
        return board.snapshot(self.root)

    def run_stage(self, stage: str) -> dict:
        environment_check.run_now(self.root, TASK, stage)
        return environment_check.run_of(self.state(), TASK, stage)

    def open_setup_cards(self) -> list[dict]:
        return [a for a in (self.state().get("owner_actions") or {}).values()
                if a.get("status") == "open" and str(a.get("card_key") or "").startswith("setup:")]


RESULT_LINE = 'import pathlib as _p; _p.Path("out").mkdir(exist_ok=True); _p.Path("out/result.txt").write_text("the report")\n'


@unittest.skipUnless(SHELLS, "needs zsh or bash")
class ContractIsHeldUntilTheSetupPasses(Case):
    def test_a_proposal_without_the_setup_list_is_refused_with_the_format(self):
        with self.assertRaisesRegex(ValueError, "setup list.*ai_tools"):
            self.propose(None)

    def test_an_app_calling_an_ai_tool_must_name_its_real_run(self):
        with self.assertRaisesRegex(ValueError, "real_run"):
            self.propose(self.needs(real_run=""))

    def test_the_contract_is_not_shown_and_go_ahead_is_refused_until_the_check_passes(self):
        self.propose(self.needs())
        proposal = self.state()["requirement_proposals"][TASK]
        self.assertEqual(proposal["status"], "checking_setup")
        with self.assertRaisesRegex(ValueError, "not awaiting an owner decision"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=proposal["version"])
        words = story.setup_check_words(self.state(), TASK)
        self.assertIn("Checking your project's setup", words[0])
        run = self.run_stage("contract")
        self.assertEqual(run["status"], "passed", run["items"])
        self.assertEqual(self.state()["requirement_proposals"][TASK]["status"], "awaiting_owner")
        self.assertIsNone(story.setup_check_words(self.state(), TASK))

    def test_the_lease_failure_is_a_card_for_the_owner_then_check_again_passes(self):
        self.claude_signed_out()
        self.propose(self.needs())
        run = self.run_stage("contract")
        self.assertEqual(run["status"], "failed")
        cards = self.open_setup_cards()
        # one card for the owner's own call, and (task 2026-11-21_not-run-goes-to-repair, C0) one per role that calls it the same way
        self.assertEqual(sorted(c["title"] for c in cards), sorted(["Blocked, needs you: Claude for your app"] + [
            f"Blocked, needs you: {role} can use Claude" for role in ("Builder", "Reviewer", "CTO")]), cards)
        card = next(c for c in cards if c["title"] == "Blocked, needs you: Claude for your app")
        self.assertIn(LEASE_ERROR, card["why"])
        self.assertIn("Check again", card["choices"])
        self.assertTrue(card["blocked"])
        dump = json.dumps(self.state())
        self.assertNotIn("Sup3rSecretPw", dump)           # the owner's database URL never reaches the board (Codex plan check R2-2)
        self.assertEqual(self.state()["requirement_proposals"][TASK]["status"], "checking_setup")
        self.assertIn("did not pass", story.setup_check_words(self.state(), TASK)[0])
        self.claude_ok()                                    # the owner signs in again
        board.owner_action_choose(self.root, card["id"], "Check again")
        self.assertEqual(environment_check.run_of(self.state(), TASK, "contract")["status"], "checking")
        self.assertEqual(self.run_stage("contract")["status"], "passed")
        self.assertEqual(self.open_setup_cards(), [])
        self.assertEqual(self.state()["requirement_proposals"][TASK]["status"], "awaiting_owner")

    def test_each_kind_of_problem_says_who_fixes_it(self):
        old = _script(self.bin / "oldtool", 'echo "oldtool 1.2.0"\n')
        self.propose(self.needs(folders=[{"path": str(self.base / "missing"), "access": "read"}],
                                programs=[{"name": "oldtool", "min_version": "2.0"}, {"name": "no-such-compiler-xyz"}],
                                libraries=[{"language": "python", "name": "no_such_library_xyz"}]))
        run = self.run_stage("contract")
        who = {item["label"]: (item["ok"], item["who"]) for item in run["items"]}
        self.assertEqual(who[f"Folder {self.base / 'missing'}"], (False, "owner"))
        self.assertEqual(who["Program oldtool 2.0+"], (False, "owner"))
        self.assertEqual(who["Program no-such-compiler-xyz"], (False, "owner"))
        self.assertEqual(who["Python library no_such_library_xyz"], (False, "builder"))
        builder_card = [c for c in self.open_setup_cards() if "no_such_library_xyz" in c["title"]][0]
        self.assertIn("the builder is fixing it", builder_card["title"])
        self.assertFalse(builder_card["blocked"])
        self.assertTrue(old.exists())

    def test_a_folder_outside_the_agents_read_grant_is_a_problem_where_reads_are_confined(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        with mock.patch.object(environment_check, "agent_reads_confined", return_value=True), \
             mock.patch.dict(os.environ, {"HARNESS_AGENT_READ_PATHS": str(self.base / "elsewhere")}):
            item = environment_check.check_folder({"path": str(outside), "access": "read"}, str(self.workspace))
        self.assertFalse(item["ok"])
        self.assertIn("HARNESS_AGENT_READ_PATHS", item["detail"])
        with mock.patch.object(environment_check, "agent_reads_confined", return_value=False):
            self.assertTrue(environment_check.check_folder({"path": str(outside), "access": "read"}, str(self.workspace))["ok"])

    def test_a_failing_agent_is_a_problem_too(self):
        with mock.patch("harness.control.snapshot", return_value={"sessions": [
                {"status": "running", "provider": "codex", "model": "gpt-x", "effort": "medium", "kind": "qa"}]}), \
             mock.patch("harness.control.ACTIVE_STATUSES", {"running"}), \
             mock.patch.object(environment_check, "AGENT_CHECK", side_effect=ValueError("Codex CLI was not found (codex)")):
            items = environment_check.check_agents(self.root)
        self.assertEqual([(i["ok"], i["detail"]) for i in items], [(False, "Codex CLI was not found (codex)")])

    def test_a_tool_that_hangs_times_out_with_a_card(self):
        _script(self.bin / "claude", "sleep 30\n")
        self.propose(self.needs())
        with mock.patch.object(environment_check, "AI_CALL_SECONDS", 1):
            run = self.run_stage("contract")
        self.assertIn("did not answer within 1 seconds", " ".join(environment_check.failed_lines(run)))

    def test_a_changed_setup_list_discards_the_earlier_result(self):
        self.propose(self.needs())
        self.assertEqual(self.run_stage("contract")["status"], "passed")
        with board.locked_state(self.root) as state:
            changed = environment_check.record_needs(state, TASK, environment_check.parse_needs(self.needs(ai_tools=["claude", "codex"])), DEV, board.now())
        self.assertTrue(changed)
        self.assertFalse(environment_check.passed(self.state(), TASK, "contract"))

    def test_a_result_that_lands_while_paused_is_not_recorded(self):
        self.propose(self.needs())
        run = environment_check.run_of(self.state(), TASK, "contract")
        with mock.patch.object(board, "pause_state", return_value={"status": "paused"}):
            out = board.record_environment_result(self.root, TASK, "contract", run["run_id"], [], "id")
        self.assertFalse(out["recorded"])
        self.assertEqual(environment_check.run_of(self.state(), TASK, "contract")["status"], "checking")


@unittest.skipUnless(SHELLS, "needs zsh or bash")
class RealRunIsObservedNotReported(Case):
    def real_run(self) -> dict:
        values = owner_env.read(fresh=True)
        return environment_check.real_run(f"{sys.executable} realcheck.py", ["claude"], str(self.workspace), values, owner_env.redactor(values))

    def test_a_real_call_through_the_apps_code_passes(self):
        item = self.real_run()
        self.assertTrue(item["ok"], item)

    def test_a_no_op_that_exits_zero_fails(self):
        self.commit_app("print('all good')\n")
        item = self.real_run()
        self.assertFalse(item["ok"])
        self.assertIn("never asked claude", item["detail"])

    def test_a_stand_in_called_by_path_fails(self):
        stand_in = _script(self.base / "fake-claude", 'echo OK\n')
        self.commit_app(f'import subprocess\nsubprocess.run(["{stand_in}", "-p", "hi"])\n')
        self.assertIn("never asked claude", self.real_run()["detail"])

    def test_a_swallowed_tool_error_fails(self):
        self.claude_signed_out()
        self.commit_app('import subprocess\nsubprocess.run(["claude", "-p", "hi"])\nprint("done")\n')
        item = self.real_run()
        self.assertFalse(item["ok"])
        self.assertIn("did not answer it", item["detail"])


@unittest.skipUnless(SHELLS, "needs zsh or bash")
class ReviewAndReleaseGates(Case):
    def test_no_review_without_the_setup_list(self):
        with self.assertRaisesRegex(ValueError, "set-needs"):
            board.environment_review_gate(self.root, DEV)

    def test_every_review_route_is_gated_in_the_agent_command(self):
        for command in (["request-review", "--agent", DEV, "--ledger", "l.md", "--summary", "s", "--unit-test-command", "true"],
                        ["request-qa", "--agent", DEV, "--ledger", "l.md", "--summary", "s"],
                        ["request-independent-review", "--agent", DEV, "--summary", "s"]):
            with self.subTest(command=command[0]), mock.patch.object(board, "request_review") as review, \
                    mock.patch.object(board, "request_qa") as qa, mock.patch.object(board, "request_independent_review") as independent, \
                    mock.patch("harness.board_client.environment_state", return_value="legacy"), \
                    mock.patch.object(board, "_registered_context", return_value=self.root), mock.patch("sys.stderr"), mock.patch("sys.stdout"):
                code = board.main(command)
                self.assertNotEqual(code, 0)
                self.assertFalse(review.called or qa.called or independent.called)

    def test_the_review_waits_for_the_real_run_on_this_commit(self):
        board.set_needs(self.root, DEV, self.needs())
        self.assertEqual(self.run_stage("contract")["status"], "passed")
        with self.assertRaisesRegex(ValueError, "real run on"):
            board.environment_review_gate(self.root, DEV)
        run = self.run_stage("review")
        self.assertEqual(run["status"], "passed", run["items"])
        self.assertEqual(run["commit"], _git(self.workspace, "rev-parse", "HEAD"))
        self.assertIsNone(board.environment_review_gate(self.root, DEV))
        self.commit_app('print("changed")\n')                  # a new commit needs its own real run
        with self.assertRaisesRegex(ValueError, "real run on"):
            board.environment_review_gate(self.root, DEV)
        self.assertEqual(self.run_stage("review")["status"], "failed")
        with self.assertRaisesRegex(ValueError, "never asked claude"):
            board.environment_review_gate(self.root, DEV)

    def test_the_release_holds_until_its_fresh_check_passes(self):
        board.set_needs(self.root, DEV, self.needs())
        self.assertEqual(self.run_stage("contract")["status"], "passed")
        commit = _git(self.workspace, "rev-parse", "HEAD")
        review = {"id": "final-1", "task": TASK, "phase": "final_acceptance", "status": "passed", "cycle": 1,
                  "requested_at": board.now(), "reviewed_commit": commit, "developer_id": DEV, "claimed_by": "qa-1", "subtask": "", "chunk": "final", "lifecycle": "", "route_state": "", "review_wait_started_at": ""}
        with board.locked_state(self.root) as state:
            state.setdefault("qa_requests", {})["final-1"] = review
        with mock.patch.object(board, "review_counts_as_passed", return_value=True):
            board.request_release_environment_checks(self.root)
        ready, problems = board.environment_release_status(self.state(), TASK, review)
        self.assertFalse(ready)
        self.assertIn("NMHP is running the release setup check", problems[0])
        run = self.run_stage("release")
        self.assertEqual(run["status"], "passed", run["items"])
        self.assertEqual(board.environment_release_status(self.state(), TASK, review), (True, []))
        self.assertIn("environment_ready", board.RELEASE_REQUIRED_CHECKS)
        self.assertIn("environment_ready", board.BROKER_RELEASE_REQUIRED_CHECKS)
        self.assertNotIn("environment_ready", board.PROCESS_RELEASE_CHECKS)

    def test_a_task_with_no_ai_tools_has_nothing_to_run_at_release(self):
        board.set_needs(self.root, DEV, self.needs(ai_tools=[], real_run=""))
        self.assertEqual(self.run_stage("contract")["status"], "passed")
        self.assertEqual(board.environment_release_status(self.state(), TASK, {"reviewed_commit": "abc"}), (True, []))


@unittest.skipUnless(SHELLS, "needs zsh or bash")
class CodexReviewRound1(Case):
    """The five defects Codex's first review reproduced (2026-11-21), each held now."""

    def test_r1_1_a_secret_nmhp_also_holds_is_still_hidden(self):
        self.write_profile({"ANTHROPIC_AUTH_TOKEN": "sk-owner-secret-value-123"})
        _script(self.bin / "claude", 'echo \'{"is_error":true,"result":"rejected sk-owner-secret-value-123"}\'\nexit 1\n')
        self.propose(self.needs())
        with mock.patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "sk-owner-secret-value-123"}):
            self.run_stage("contract")
        self.assertNotIn("sk-owner-secret-value-123", json.dumps(self.state()))
        self.assertIn("<ANTHROPIC_AUTH_TOKEN>", json.dumps(self.state()))

    def test_r1_2_a_changed_setup_list_takes_the_contract_back_and_go_ahead_waits(self):
        self.propose(self.needs())
        self.assertEqual(self.run_stage("contract")["status"], "passed")
        proposal = self.state()["requirement_proposals"][TASK]
        self.assertEqual(proposal["status"], "awaiting_owner")
        board.set_needs(self.root, DEV, self.needs(programs=[{"name": "no-such-compiler-xyz"}]))
        self.assertEqual(self.state()["requirement_proposals"][TASK]["status"], "checking_setup")
        self.assertEqual(self.run_stage("contract")["status"], "failed")
        with self.assertRaisesRegex(ValueError, "not awaiting an owner decision"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=proposal["version"])
        with board.locked_state(self.root) as state:          # even a proposal shown by another route cannot be accepted
            state["requirement_proposals"][TASK]["status"] = "awaiting_owner"
        with self.assertRaisesRegex(ValueError, "still checking this project's setup"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=proposal["version"])

    def test_r1_3_a_version_call_or_an_error_answer_is_not_a_real_answer(self):
        values = owner_env.read(fresh=True)
        for source in ('import subprocess\nsubprocess.run(["claude", "--version"])\n',
                       'import subprocess\nsubprocess.run(["claude", "auth", "status"])\n'):
            self.commit_app(source)
            item = environment_check.real_run(f"{sys.executable} realcheck.py", ["claude"], str(self.workspace), values, owner_env.redactor(values))
            self.assertFalse(item["ok"], source)
            self.assertIn("never asked claude", item["detail"])
        _script(self.bin / "claude", 'echo \'{"type":"result","is_error":true,"result":"Not logged in"}\'\n')     # error, exit 0
        self.commit_app('import subprocess\nsubprocess.run(["claude", "-p", "hi", "--output-format", "json"])\n')
        item = environment_check.real_run(f"{sys.executable} realcheck.py", ["claude"], str(self.workspace), values, owner_env.redactor(values))
        self.assertFalse(item["ok"])
        self.assertIn("did not answer it", item["detail"])

    def test_r1_4_a_task_with_no_setup_list_is_held_at_release_and_its_builder_told_once(self):
        commit = _git(self.workspace, "rev-parse", "HEAD")
        review = {"id": "final-1", "task": TASK, "phase": "final_acceptance", "status": "passed", "cycle": 1,
                  "requested_at": board.now(), "reviewed_commit": commit, "developer_id": DEV, "claimed_by": "qa-1", "subtask": "",
                  "chunk": "final", "lifecycle": "", "route_state": "", "review_wait_started_at": ""}
        with board.locked_state(self.root) as state:
            state.setdefault("qa_requests", {})["final-1"] = review
        ready, problems = board.environment_release_status(self.state(), TASK, review)
        self.assertFalse(ready)
        self.assertIn("set-needs", problems[0])
        with mock.patch.object(board, "review_counts_as_passed", return_value=True), \
             mock.patch("harness.control.enqueue_instruction", return_value={"id": "w"}) as tell:
            board.request_release_environment_checks(self.root)
            board.request_release_environment_checks(self.root)
        self.assertEqual(tell.call_count, 1)
        self.assertEqual(len([c for c in self.open_setup_cards() if c["card_key"].endswith(":release:missing")]), 1)
        board.set_needs(self.root, DEV, self.needs())
        self.assertEqual([c for c in self.open_setup_cards() if c["card_key"].endswith(":release:missing")], [])

    def test_r1_5_an_unreadable_setup_never_reuses_an_old_pass(self):
        board.set_needs(self.root, DEV, self.needs())
        self.assertEqual(self.run_stage("contract")["status"], "passed")
        with self.assertRaisesRegex(ValueError, "real run on"):
            board.environment_review_gate(self.root, DEV)
        self.assertEqual(self.run_stage("review")["status"], "passed")
        self.assertIsNone(board.environment_review_gate(self.root, DEV))
        (self.home / ".zshrc").write_text("exit 3\n")
        (self.home / ".bash_profile").write_text("exit 3\n")
        owner_env._cache.clear()
        with self.assertRaisesRegex(ValueError, "cannot be requested yet"):
            board.environment_review_gate(self.root, DEV)


@unittest.skipUnless(SHELLS, "needs zsh or bash")
class OwnerEnvironment(Case):
    def test_the_login_shell_environment_is_read_without_nmhp_variables(self):
        with mock.patch.dict(os.environ, {"HARNESS_SECRET_TOKEN": "manager-only-value"}):
            values = owner_env.read(fresh=True)
        self.assertEqual(values["OWNER_ONLY_SETTING"], "from-his-shell")
        self.assertTrue(values["PATH"].startswith(str(self.bin)))
        self.assertNotIn("HARNESS_SECRET_TOKEN", values)
        self.assertNotIn("manager-only-value", json.dumps(values))

    def test_bash_reads_its_login_profile_too(self):
        if not os.access("/bin/bash", os.X_OK):
            self.skipTest("no bash")
        with mock.patch.object(owner_env, "ACCOUNT", lambda: ("/bin/bash", str(self.home))):
            self.assertEqual(owner_env.read(fresh=True)["OWNER_ONLY_SETTING"], "from-his-shell")

    def test_a_shell_that_cannot_start_is_a_failed_item_never_a_fallback(self):
        (self.home / ".zshrc").write_text("sleep 60\n")
        (self.home / ".bash_profile").write_text("sleep 60\n")
        owner_env._cache.clear()
        with mock.patch.object(owner_env, "READ_SECONDS", 1):
            self.propose(self.needs())
            run = self.run_stage("contract")
        self.assertEqual([item["kind"] for item in run["items"]], ["owner_env"])
        self.assertFalse(run["items"][0]["ok"])

    def test_the_real_account_is_never_read_by_the_test_suite(self):
        with self.assertRaisesRegex(owner_env.OwnerEnvError, "test suite"):
            owner_env._real_account()

    def test_the_preview_gets_the_owners_environment_and_not_the_workers(self):
        owner = owner_env.read(fresh=True)
        values = release_preview.preview_environment({"PATH": "/usr/bin", "WORKER_ONLY_TOKEN": "x"}, {"PORT": "1"}, owner)
        self.assertEqual(values["OWNER_ONLY_SETTING"], "from-his-shell")
        self.assertEqual(values["PORT"], "1")
        self.assertNotIn("WORKER_ONLY_TOKEN", values)
        self.assertTrue(values["PATH"].startswith(str(self.bin)))
        self.assertIn("/usr/bin", values["PATH"].split(os.pathsep))

    def test_redaction_is_by_value_not_by_name(self):
        values = owner_env.read(fresh=True)
        scrub = owner_env.redactor(values, base={})
        text = scrub(f"error talking to {SECRET_URL} with from-his-shell")
        self.assertNotIn("Sup3rSecretPw", text)
        self.assertIn("<SERVICE_URL>", text)


if __name__ == "__main__":
    unittest.main()


class RealProjectRootV0291(unittest.TestCase):
    """v0.29.1 — found by the real end-to-end test (D-022): in a real project the board root is a ProjectContext, not a path.
    run_due turned it into a Path, so propose-requirements failed after recording the contract, the setup check never started
    (the contract waited for ever) and the watchdog's tick died on the same error."""

    def setUp(self):
        from harness.project_context import context_from_roots
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        (base / "code").mkdir()
        self.context = context_from_roots(base / "code", base / "data", base / "workspaces")
        board.snapshot(self.context)
        with board.locked_state(self.context) as state:
            environment_check.record_needs(state, "TASK", environment_check.parse_needs('{"ai_tools": []}'), "dev", board.now())
            environment_check.request(state, "TASK", "contract", board.now())

    def test_a_requested_check_starts_for_a_real_project(self):
        started = []
        with mock.patch.object(environment_check, "_work", lambda root, task, stage, run, key: started.append((task, stage, key[0]))):
            self.assertEqual(environment_check.run_due(self.context), ["TASK:contract"])
        self.assertEqual(started[0][:2], ("TASK", "contract"))
        self.assertEqual(started[0][2], str(self.context.data_root))

    def test_run_due_never_raises(self):
        with mock.patch("harness.board.snapshot", side_effect=TypeError("anything unexpected")):
            self.assertEqual(environment_check.run_due(self.context), [])


class AnUpdateRunsAStaleFailureAgain(unittest.TestCase):
    """Owner's Lease project 2026-11-23: the "env: node" failure from v0.30.1 was replayed on v0.30.2 forever, because the
    identity a result is kept under never changed when NMHP's checking code did."""

    def test_new_checking_code_changes_the_identity(self):
        code = Path(tempfile.mkdtemp(prefix="nmhp-checker-"))
        self.addCleanup(shutil.rmtree, code, True)
        (code / "old.py").write_text("old checker")
        (code / "new.py").write_text("new checker")
        with mock.patch.object(owner_env, "read", lambda *a, **k: {"PATH": "/usr/bin"}):
            with mock.patch.object(environment_check, "__file__", str(code / "old.py")):
                before = environment_check.identity(code, "t1", "", {})
            with mock.patch.object(environment_check, "__file__", str(code / "new.py")):
                after = environment_check.identity(code, "t1", "", {})
        self.assertTrue(before and after)
        self.assertNotEqual(before, after, "a setup failure kept from older NMHP code is checked again")

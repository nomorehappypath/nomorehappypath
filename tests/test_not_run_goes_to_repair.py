# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A requirement the review could not really run goes back to the builder and the CTO; the app is never held for it (D-023).

Task 2026-11-21_not-run-goes-to-repair. Owner 2026-11-21: "If it does not pass I want the CRO or builder to fix it and not to have
the app blocked", "NMHP has to test everything before the contract acceptance", and "at the beginning all builder reviewer and cto
have access to everything is needed to run the project". Layer 1 (C0): before the contract, the setup check tries every AI tool,
program and folder the project needs as each role's commands run. Layer 2 (C1-C5): at review, a requirement nobody could really run
is a FAIL the builder and the CTO repair; at the round limit it is recorded as passed with its unproven list, never an owner card.

These are unit tests with stand-in Claude/Codex programs on a temporary board (D-022: they protect, they do not prove C7, which is
the lead's real end-to-end run). The C0 confinement test uses the real OS confinement of this computer.

Run:  PYTHONPATH=. python3 -m unittest tests.test_not_run_goes_to_repair -v
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

from harness import board, board_surface, cto, cto_wakes, environment_check, review_rounds
from tests import test_board
from tests import test_environment_check as env_tests
from tests import test_frozen_review_scope as scope

ROOT = Path(__file__).resolve().parents[1]
AGENT_TOKEN = "sk-ant-oat01-" + "Q" * 60        # a stand-in setup-token; it must never reach the board
AGREED = "ship the repair flow with evidence"   # verbatim from the fixture's confirmed requirements
TESTED = scope.TESTED


def _confinement_works(root: Path) -> bool:
    """This computer can wrap a command in the agents' confinement (and the tests are not already inside a sandbox)."""
    from harness import agent_confinement, platform_support
    if platform_support.browser_host().inside_os_sandbox():
        return False
    try:
        agent_confinement.confine_agent_command(["/usr/bin/true"], root)
    except Exception:  # noqa: BLE001 - no primitive here: the test is skipped, not failed
        return False
    return True


# ---------------------------------------------------------------------------------------------------------------- C0
@unittest.skipUnless(env_tests.SHELLS, "needs zsh or bash")
class EachRoleIsCheckedBeforeTheContract(env_tests.Case):
    """C0: one item per role and need, run inside the agents' confinement with the role's own environment and sign-in."""

    def setUp(self):
        super().setUp()
        # the owner's own Claude sign-in lives in his shell profile (Lease); the launcher removes it from every agent
        self.write_profile({"OWNER_ONLY_SETTING": "from-his-shell", "CLAUDE_CODE_OAUTH_TOKEN": "owner-own-token-1234"})
        # a stand-in Claude that answers only with a sign-in, and repeats the sign-in it got when it refuses (it must be hidden)
        env_tests._script(self.bin / "claude", 'if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then echo \'{"is_error":false,"result":"OK"}\'; '
                                               'else echo "{\\"is_error\\":true,\\"result\\":\\"Not logged in\\"}"; exit 1; fi\n')
        patch = mock.patch.object(environment_check, "AGENT_TOKEN", lambda: AGENT_TOKEN)
        patch.start()
        self.addCleanup(patch.stop)
        if not _confinement_works(self.root):
            self.skipTest("the agents' confinement is not available here")

    def items(self, stage="contract"):
        return environment_check.run_of(self.state(), env_tests.TASK, stage).get("items") or []

    def test_every_role_gets_one_item_per_need_and_with_default_settings_all_three_reach_claude(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.propose(self.needs(folders=[{"path": str(outside), "access": "read"}], programs=[{"name": "sh"}]))
        run = self.run_stage("contract")
        roles = {item["label"]: item for item in run["items"] if item["kind"] == "role_access"}
        for role in ("Builder", "Reviewer", "CTO"):
            for need in ("Claude", "sh", f"folder {outside}"):
                self.assertIn(f"{role} can use {need}", roles, f"no item for {role} / {need}")
        # default Settings: builder and CTO on Claude, the Reviewer on Codex; NMHP hands the Reviewer the Claude sign-in too
        # (owner 2026-11-21: "at the beginning all builder reviewer and cto have access to everything is needed")
        for role in ("Builder", "Reviewer", "CTO"):
            self.assertTrue(roles[f"{role} can use Claude"]["ok"], roles[f"{role} can use Claude"])
        self.assertTrue(next(i for i in run["items"] if i["label"] == "Claude for your app")["ok"], "the owner's own call keeps his sign-in")
        self.assertEqual(run["status"], "passed", run["items"])
        self.assertEqual(self.open_setup_cards(), [])
        self.assertNotIn(AGENT_TOKEN, json.dumps(self.state()), "a sign-in is never stored")
        self.assertNotIn("owner-own-token-1234", json.dumps(self.state()))

    def test_every_project_checks_each_roles_own_sign_in_even_when_the_app_calls_no_ai(self):
        # owner 2026-11-22: "The sign in should be tested before contract acceptance for any type of project"
        env_tests._script(self.bin / "codex", 'echo "OK"\n')
        self.propose(self.needs(ai_tools=[], programs=[{"name": "sh"}]))
        run = self.run_stage("contract")
        signed = {item["label"]: item["ok"] for item in run["items"] if item["kind"] == "role_access" and "signed in" in item["label"]}
        self.assertEqual(signed, {"Builder is signed in to Claude": True, "Reviewer is signed in to Codex": True,
                                  "CTO is signed in to Claude": True})
        self.assertEqual(run["status"], "passed", run["items"])

    def test_a_program_that_refuses_version_still_passes_when_no_version_is_asked(self):
        # Linux full suite 2026-11-22: dash (Linux's sh) answers `sh --version` with "Illegal option --"
        env_tests._script(self.bin / "codex", 'echo "OK"\n')
        env_tests._script(self.bin / "dashlike", 'echo "/usr/bin/sh: 0: Illegal option --" >&2; exit 2\n')
        self.propose(self.needs(ai_tools=[], programs=[{"name": "dashlike"}]))
        run = self.run_stage("contract")
        roles = {item["label"]: item["ok"] for item in run["items"] if item["kind"] == "role_access"}
        self.assertEqual([roles[f"{role} can use dashlike"] for role in ("Builder", "Reviewer", "CTO")], [True, True, True], run["items"])
        self.assertEqual(run["status"], "passed", run["items"])

    def test_the_roles_are_checked_with_their_launch_environment_not_the_owners_shell(self):
        # Codex review r1-1 (2026-11-22): the owner's shell pointed at another Codex home, so a signed-in Reviewer got a false card
        signed, other = self.base / "codex-signed-in", self.base / "codex-other"
        signed.mkdir(); other.mkdir()
        (signed / "auth").write_text("ok", encoding="utf-8")
        env_tests._script(self.bin / "codex", 'if [ -f "$CODEX_HOME/auth" ]; then echo OK; else echo "401 Unauthorized" >&2; exit 1; fi\n')
        self.write_profile({"OWNER_ONLY_SETTING": "from-his-shell", "CODEX_HOME": str(other)})
        owner = environment_check.owner_env.read(fresh=True)
        self.propose(self.needs(ai_tools=[], programs=[{"name": "sh"}]))
        with mock.patch.object(environment_check, "LAUNCH_ENV", lambda: {**owner, "CODEX_HOME": str(signed)}):
            run = self.run_stage("contract")
        reviewer = next(i for i in run["items"] if i["label"] == "Reviewer is signed in to Codex")
        self.assertTrue(reviewer["ok"], reviewer)
        self.assertEqual(run["status"], "passed", run["items"])

    def test_a_reviewer_whose_launch_environment_is_not_signed_in_gets_the_card_even_if_the_owners_shell_is(self):
        signed, other = self.base / "codex-signed-in", self.base / "codex-other"
        signed.mkdir(); other.mkdir()
        (signed / "auth").write_text("ok", encoding="utf-8")
        env_tests._script(self.bin / "codex", 'if [ -f "$CODEX_HOME/auth" ]; then echo OK; else echo "401 Unauthorized" >&2; exit 1; fi\n')
        self.write_profile({"OWNER_ONLY_SETTING": "from-his-shell", "CODEX_HOME": str(signed)})
        owner = environment_check.owner_env.read(fresh=True)
        self.propose(self.needs(ai_tools=[], programs=[{"name": "sh"}]))
        with mock.patch.object(environment_check, "LAUNCH_ENV", lambda: {**owner, "CODEX_HOME": str(other)}):
            run = self.run_stage("contract")
        self.assertEqual(run["status"], "failed")
        self.assertEqual([card["title"] for card in self.open_setup_cards()], ["Blocked, needs you: Reviewer is signed in to Codex"])

    def test_a_role_whose_sign_in_does_not_work_holds_any_contract_with_its_card(self):
        env_tests._script(self.bin / "codex", 'echo "Not signed in" >&2; exit 1\n')
        self.propose(self.needs(ai_tools=[], programs=[{"name": "sh"}]))
        run = self.run_stage("contract")
        self.assertEqual(run["status"], "failed")
        self.assertEqual([card["title"] for card in self.open_setup_cards()], ["Blocked, needs you: Reviewer is signed in to Codex"])
        self.assertEqual(self.state()["requirement_proposals"][env_tests.TASK]["status"], "checking_setup")

    def test_the_reviewer_on_claude_gets_its_sign_in_through_its_own_variable_and_passes(self):
        self.propose(self.needs())
        with mock.patch.object(environment_check, "role_providers", lambda root: {"delivery": "codex", "reviewer": "claude", "cto": "claude"}):
            run = self.run_stage("contract")
        roles = {item["label"]: item["ok"] for item in run["items"] if item["kind"] == "role_access"}
        self.assertEqual((roles["Reviewer can use Claude"], roles["CTO can use Claude"], roles["Builder can use Claude"]), (True, True, False))
        env = environment_check.role_environment("reviewer", "claude", {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "owner"}, AGENT_TOKEN)
        self.assertEqual((env["HARNESS_REVIEWER_CLAUDE_TOKEN"], env["CLAUDE_CODE_OAUTH_TOKEN"]), (AGENT_TOKEN, AGENT_TOKEN))
        env = environment_check.role_environment("reviewer", "codex", {"CLAUDE_CODE_OAUTH_TOKEN": "owner"}, AGENT_TOKEN)
        self.assertEqual((env["HARNESS_REVIEWER_CLAUDE_TOKEN"], env["CLAUDE_CODE_OAUTH_TOKEN"]), (AGENT_TOKEN, AGENT_TOKEN))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment_check.role_environment("delivery", "codex", {"CLAUDE_CODE_OAUTH_TOKEN": "owner"}, AGENT_TOKEN))

    def test_without_a_setup_token_no_claude_role_passes_and_the_refusal_never_shows_a_sign_in(self):
        env_tests._script(self.bin / "claude", 'echo "{\\"is_error\\":true,\\"result\\":\\"rejected $CLAUDE_CODE_OAUTH_TOKEN\\"}"; exit 1\n')
        self.propose(self.needs())
        run = self.run_stage("contract")
        failed = {item["label"] for item in run["items"] if item["kind"] == "role_access" and not item["ok"]}
        self.assertTrue({"Builder can use Claude", "Reviewer can use Claude", "CTO can use Claude"} <= failed)
        self.assertNotIn(AGENT_TOKEN, json.dumps(self.state()))
        self.assertIn("<sign-in>", next(i for i in run["items"] if i["label"] == "Builder can use Claude")["detail"])

    def test_the_role_probe_really_runs_inside_the_agents_confinement(self):
        # outside every grant: not the temp space (agents may write there), not the project; a folder of this checkout
        outside = Path(tempfile.mkdtemp(prefix=".nmhp-outside-", dir=str(ROOT)))
        self.addCleanup(shutil.rmtree, outside, True)
        self.propose(self.needs(ai_tools=[], folders=[{"path": str(outside), "access": "write"}]))
        reads = os.pathsep.join([str(self.base), str(outside)])     # where reads are confined (Linux), the owner granted it
        with mock.patch.dict(os.environ, {"HARNESS_AGENT_READ_PATHS": reads}):
            run = self.run_stage("contract")
        owner = next(i for i in run["items"] if i["label"] == f"Folder {outside}")
        self.assertTrue(owner["ok"], "the owner himself can write there")
        for role in ("Builder", "Reviewer", "CTO"):
            item = next(i for i in run["items"] if i["label"] == f"{role} can use folder {outside}")
            self.assertFalse(item["ok"], f"{role}: a write outside the agents' grant must be refused by the OS confinement")
        self.assertEqual(list(outside.iterdir()), [], "no probe file is left behind")

    def test_role_items_run_before_the_contract_only_never_at_release(self):
        self.propose(self.needs())
        self.run_stage("contract")
        commit = env_tests._git(self.workspace, "rev-parse", "HEAD")
        run = environment_check.request(board.snapshot(self.root), env_tests.TASK, "release", board.now(), commit=commit)
        items, _identity = environment_check.execute(self.root, self.state(), env_tests.TASK, "release", run)
        self.assertTrue(items)
        self.assertEqual([i for i in items if i["kind"] == "role_access"], [],
                         "a role-access problem left at the round limit never holds the release a second time")


# ---------------------------------------------------------------------------------------------------------------- C1-C5
def not_run(identifier, **extra):
    """A well-formed not-run finding (C1): requirement, what was tried and refused, who can fix it, how to reproduce."""
    value = {"id": identifier, "summary": "Not run for real: the real claude call was refused, the Reviewer had no sign-in",
             "criterion": AGREED, "evidence_ref": "CLAUDE_CODE_OAUTH_TOKEN=\"$HARNESS_REVIEWER_CLAUDE_TOKEN\" claude -p hi: Not logged in",
             "harm": "the owner would get an app nobody ran with a real Claude", "regression_check": "claude -p hi from the Reviewer's shell",
             "fix_by": "cto", "harm_tag": "none"}
    value.update(extra)
    return value


class NotRunAtReview(scope.Rounds):
    """C1-C5 on the real board: real request_review, qa_result, repair packages, escalations and CTO wakes."""

    def setUp(self):
        super().setUp()
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")

    def certified_verdict(self, request, result, summary, findings=None):
        return self.verdict(request, result, summary, findings, certified=True)

    # -- D-024 (owner 2026-11-22: "Only bugs count as failing anything related to process is not fail test") -------------------

    def test_a_finding_and_a_summary_line_for_two_requirements_are_both_kept_to_the_cto_and_the_owners_card(self):
        # Codex review r1-2 (2026-11-22): the summary line of a second requirement was dropped when a structured finding existed
        first = self.request()
        summary = ("TASK DONE: NO — the app works, but\nNot run for real: the real claude call was refused, the Reviewer had no sign-in\n"
                   "Not run for real: the Linux build — no Linux machine was supplied")
        _qa, done = self.certified_verdict(first, "failed", summary, [not_run("B1")])
        state = self.state()
        listed = state["qa_requests"][first["id"]]["unproven_requirements"]
        self.assertEqual(done["status"], "passed")
        self.assertEqual(listed, [f"Not tested for real: {AGREED} — the real claude call was refused, the Reviewer had no sign-in",
                                  "Not tested for real: the Linux build — no Linux machine was supplied"])
        item = next(i for i in cto_wakes.open_items(state) if i["subject"] == f"not-tested:{first['id']}")
        for line in listed:
            self.assertIn(line, " ".join(item["lines"]))
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS} | {"head_commit": state["qa_requests"][first["id"]]["reviewed_commit"]}
        with mock.patch.object(board, "_release_app_openable", lambda *a, **k: {}), \
                mock.patch.object(board, "_saved_data_release_snapshot", lambda *a, **k: {}):
            board.record_release_ready(self.root, self.cto["id"], "TASK-R", checks)
        self.assertEqual(self.state()["releases"]["TASK-R"]["not_tested_for_real"], listed)

    def test_without_a_certified_run_the_board_asks_for_one_first(self):
        first = self.request()
        qa = self.claim(first)
        with self.assertRaisesRegex(ValueError, "no certified run"):
            board.qa_result(self.root, qa["id"], first["id"], "failed", "TASK DONE: NO — not run", self.proof(), failure_members=[not_run("B1")])

    def test_a_final_pass_that_says_not_run_is_recorded_with_those_lines_and_a_process_note_is_not_counted(self):
        first = self.request()
        qa = self.claim(first, certified=True)
        lines = ["Not run: the real claude call (my sandbox)", "  - NOT RUN for real: the Codex call", "* **Not run for real:** the CLI",
                 "\tnot run: the upgrade check"]
        done = board.qa_result(self.root, qa["id"], first["id"], "passed",
                               "TASK DONE: YES — works\n" + "\n".join(lines) + "\nProcess note: my sandbox had no second window\nSecurity check: none needed", "")
        self.assertEqual(done["status"], "passed")
        self.assertEqual(self.state()["qa_requests"][first["id"]]["unproven_requirements"],
                         ["Not tested for real: the real claude call (my sandbox)", "Not tested for real: the Codex call",
                          "Not tested for real: the CLI", "Not tested for real: the upgrade check"])

    def test_a_design_the_reviewer_could_not_capture_joins_the_not_tested_list(self):
        # Codex R5-1 (real web project): a transient waiting state could not be captured; the certified PASS was held by the screen guard
        missing = ("the agreed screen designs need one row each in your Challenge Ledger naming the design and the screen id of the real "
                   "app rendered in that state (screen-script / screen-check), with what you saw: missing for Summarizer - asking Claude")
        first = self.request()
        qa = self.claim(first, certified=True)
        with mock.patch.object(board, "_screens_coverage_problem", lambda *a, **k: missing):
            pass
            done = board.qa_result(self.root, qa["id"], first["id"], "passed",
                                   "TASK DONE: YES — works\nNot run for real: the waiting state — it passed before it could be captured", "")
        self.assertEqual(done["status"], "passed")
        self.assertEqual(self.state()["qa_requests"][first["id"]]["unproven_requirements"],
                         ["Not tested for real: the waiting state — it passed before it could be captured",
                          "Not tested for real: screen design Summarizer - asking Claude — no rendered screen of it in the review"])
        self.assertEqual(self.owner_cards(), [], "no owner card")

    def test_the_cto_is_woken_with_every_line_whole_and_the_owner_is_never_asked(self):
        first = self.request()
        long_summary = "TASK DONE: NO — " + "the walk went through every screen and the totals were right. " * 60
        lines = [f"Not run for real: step {n} — the real claude call number {n} was refused in the Reviewer's shell with Not logged in"
                 for n in range(1, 4)]
        _qa, recorded = self.certified_verdict(first, "failed", long_summary + "\n" + "\n".join(lines), [not_run("B1")])
        state = self.state()
        self.assertEqual(recorded["status"], "passed")
        item = next(i for i in cto_wakes.open_items(state) if i["subject"] == f"not-tested:{first['id']}")
        text = cto_wakes.wake_text(state, self.cto, cto_wakes.urgent_items(state))
        self.assertIn(first["id"], text, "the wake names the request")
        for line in state["qa_requests"][first["id"]]["unproven_requirements"]:
            self.assertIn(line, text, "every not-tested line reaches the CTO whole, after a long summary")
            self.assertIn(line, " ".join(item["lines"]))
        self.assertEqual([a for a in (state.get("owner_actions") or {}).values() if a.get("status") == "open"], [], "no owner action")

    def test_a_real_bug_next_to_a_not_run_finding_still_fails(self):
        first = self.request()
        bug = scope.finding("B2", evidence_ref="the total shows 41 instead of 42")
        _qa, done = self.certified_verdict(first, "failed", "TASK DONE: NO — totals wrong", [not_run("B1"), bug])
        self.assertEqual(done["status"], "failed")
        self.assertNotIn("unproven_requirements", done)

    def test_the_owners_test_card_lists_what_was_not_tested_for_real(self):
        first = self.request()
        self.certified_verdict(first, "failed", "TASK DONE: NO — works but\nNot run for real: the claude call", [not_run("B1")])
        request = self.state()["qa_requests"][first["id"]]
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS} | {"head_commit": request["reviewed_commit"]}
        with mock.patch.object(board, "_release_app_openable", lambda *a, **k: {}), \
                mock.patch.object(board, "_saved_data_release_snapshot", lambda *a, **k: {}):
            board.record_release_ready(self.root, self.cto["id"], "TASK-R", checks)
        release = self.state()["releases"]["TASK-R"]
        self.assertEqual(release["status"], "VISUAL_TEST_REQUIRED")
        self.assertEqual(release["not_tested_for_real"], request["unproven_requirements"])
        viewer = (ROOT / "harness" / "board_viewer.py").read_text()
        self.assertIn("release?.not_tested_for_real", viewer)
        self.assertIn("Not tested for real by the review", viewer)


class ARoleCheckFindsTheCLIsOwnInterpreter(unittest.TestCase):
    """v0.30.0 on the owner's Lease project: NMHP's background PATH is only /usr/bin:/bin:/usr/sbin:/sbin, so the role check's
    `codex` (`#!/usr/bin/env node`) failed with "env: node: No such file or directory" and raised false "can use Codex" cards."""

    def test_a_cli_whose_interpreter_sits_beside_it_answers_with_the_bare_launch_path(self):
        tools = Path(tempfile.mkdtemp(prefix="nmhp-cli-"))
        self.addCleanup(shutil.rmtree, tools, True)
        (tools / "fakenode").write_text("#!/bin/sh\nexit 0\n")
        (tools / "codex").write_text("#!/usr/bin/env fakenode\n")
        for name in ("fakenode", "codex"):
            (tools / name).chmod(0o755)
        seen = []
        confine = lambda argv, path_env="": seen.append(path_env) or argv
        bare = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tools)}
        with mock.patch.object(environment_check, "tool_executable", lambda tool, values: str(tools / "codex")):
            item = environment_check.check_ai_tool("codex", bare, lambda text: text, role="Builder", confine=confine)
        self.assertTrue(item["ok"], item["detail"])
        self.assertIn(str(tools), seen[0].split(os.pathsep), "the CLI's folder is readable inside the agents' confinement too")

    def test_an_installed_link_is_called_by_its_real_file(self):
        # Codex review r1-1 (Linux): ~/.local/bin/codex links into ~/.codex/packages/...; inside the confinement the link's own
        # chain is not there ("bwrap: execvp ... No such file or directory"), the real file is
        tools, links = (Path(tempfile.mkdtemp(prefix=f"nmhp-{name}-")) for name in ("cli", "bin"))
        for folder in (tools, links):
            self.addCleanup(shutil.rmtree, folder, True)
        (tools / "codex").write_text("#!/bin/sh\nexit 0\n")
        (tools / "codex").chmod(0o755)
        (links / "codex").symlink_to(tools / "codex")
        called = []
        confine = lambda argv, path_env="": called.append(argv[0]) or argv
        with mock.patch.object(environment_check, "tool_executable", lambda tool, values: str(links / "codex")):
            item = environment_check.check_ai_tool("codex", {"PATH": "/usr/bin:/bin"}, lambda text: text, role="Builder", confine=confine)
        self.assertTrue(item["ok"], item["detail"])
        self.assertEqual(called, [str((tools / "codex").resolve())])

    def test_claude_s_answer_is_read_past_a_warning_that_carries_json(self):
        # Codex review r2-1 (real run): "[claude-code:unrecognized_model] {...}" printed first made NMHP refuse a real "OK"
        tools = Path(tempfile.mkdtemp(prefix="nmhp-cli-"))
        self.addCleanup(shutil.rmtree, tools, True)
        warning = '[claude-code:unrecognized_model] {"model":"x","query_source":"sdk"}'
        for name, answer in (("ok", '{"type":"result","is_error":false,"result":"OK"}'),
                             ("refused", '{"type":"result","is_error":true,"result":"Not logged in"}')):
            (tools / name).write_text(f"#!/bin/sh\necho '{warning}'\necho '{answer}'\n")
            (tools / name).chmod(0o755)
        for name, ok in (("ok", True), ("refused", False)):
            with mock.patch.object(environment_check, "tool_executable", lambda tool, values, name=name: str(tools / name)):
                item = environment_check.check_ai_tool("claude", {"PATH": "/usr/bin:/bin"}, lambda text: text, role="Builder",
                                                       confine=lambda argv, path_env="": argv)
            self.assertEqual(item["ok"], ok, item["detail"])
        self.assertIn("Not logged in", item["detail"])

    def test_a_launched_agent_finds_a_linked_tool_by_its_real_file(self):
        # Codex review r3-1 (real Linux Builder): ~/.local/bin/codex -> ~/.codex/packages/standalone/current/bin/codex; the
        # Claude agent's box hides ~/.codex, so `codex` exited 127 while the setup check, run by the real file, passed
        root = Path(tempfile.mkdtemp(prefix="nmhp-link-"))
        self.addCleanup(shutil.rmtree, root, True)
        real = root / "standalone" / "releases" / "1" / "bin"
        real.mkdir(parents=True)
        (real / "codex").write_text("#!/bin/sh\nexit 0\n")
        (real / "codex").chmod(0o755)
        (root / "standalone" / "current").symlink_to(root / "standalone" / "releases" / "1")
        (root / "bin").mkdir()
        (root / "bin" / "codex").symlink_to(root / "standalone" / "current" / "bin" / "codex")
        script = (Path(__file__).resolve().parents[1] / "scripts" / "run_managed_agent.sh").read_text()
        code = script.split("tool_path=\"$(\"$python_bin\" -E -c '", 1)[1].split("' \"$harness_root\")\"", 1)[0]
        env = {**os.environ, "PATH": f"{root / 'bin'}:/usr/bin:/bin", "HARNESS_CODEX_BIN": str(root / "bin" / "codex")}
        path = subprocess.run([sys.executable, "-E", "-c", code, str(Path(__file__).resolve().parents[1])], env=env,
                              capture_output=True, text=True, check=True).stdout.strip()
        found = shutil.which("codex", path=path)
        self.assertEqual(found, os.path.realpath(found), "the agent runs Codex by its real file, which its box can read")

    def test_a_role_finds_a_program_on_the_path_its_agent_really_gets(self):
        # owner's Lease project 2026-11-23: "Builder can use node" failed on the bare background PATH; the agents get more
        tools = Path(tempfile.mkdtemp(prefix="nmhp-prog-"))
        self.addCleanup(shutil.rmtree, tools, True)
        (tools / "someprogram").write_text("#!/bin/sh\nexit 0\n")
        (tools / "someprogram").chmod(0o755)
        from harness import global_settings
        with mock.patch.object(global_settings, "PROVIDER_SEARCH_DIRECTORIES", (str(tools),)):
            path = global_settings.agent_path("/usr/bin:/bin:/usr/sbin:/sbin")
            item = environment_check.check_role_program({"name": "someprogram"}, {"PATH": path}, lambda argv: argv, "Builder",
                                                        lambda text: text)
        self.assertTrue(item["ok"], item["detail"])
        self.assertIn('env["PATH"] = global_settings.agent_path(', Path(environment_check.__file__).read_text())


class RuleTexts(unittest.TestCase):
    """Every rule text says D-024 (only bugs fail; a requirement nobody really ran is listed, never a FAIL)."""

    def flat(self, path):
        return " ".join((ROOT / path).read_text(encoding="utf-8").split())

    def test_the_rule_texts_say_d024(self):
        agent = self.flat("directives/AGENT.md")
        reviewer = self.flat("directives/cards/REVIEWER.md")
        for text, where in ((agent, "AGENT.md"), (reviewer, "REVIEWER card"), (self.flat("directives/cards/CTO.md"), "CTO card"),
                            (self.flat("harness/review_brief.py"), "final review rule")):
            with self.subTest(where=where):
                self.assertIn("D-024", text)
                self.assertNotIn("D-023", text, "the replaced rule is gone")
        page = self.flat("harness/project_manager_page.py")
        self.assertIn("Only a real problem in the app fails a review.", page)
        for old in ("never a PASS and never a block", "The board refuses a final PASS whose summary carries", "FAIL with a `Not run for real:`",
                    "the review does not count it as passed"):
            self.assertNotIn(old, agent + reviewer + page)
        self.assertIn("Not run for real:", agent)
        self.assertIn("Process note:", agent)


class AStepThatNeedsASignInNeverTrapsTheReview(test_board.BoardTests):
    """Real run 4 (2026-11-22, 8c78b1b7): the Reviewer put `make real-run` (real Claude) in its ledger; execute-challenge runs without
    any sign-in, so the row said "Not run for real" and the project's FINAL CHECK: FAIL failed every execution. The review could not
    pass (no certified run) nor fail (D-024), and the project stalled. The board now names the way out, and the way out works."""

    ROW = "| {id} | {what} | `python3 -m unittest {module}` | The check is observed | PASS: observed | PASS |\n"
    HEAD = ("| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n")

    def claimed_review(self, rows):
        dev = self.delivery("TASK-NOT-RUN-ROW")
        self.declare_chunks(dev["id"], [("core", "not-run row")])
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        request = board.request_review(self.root, dev["id"], self.ledger("not-run-delivery.md"), "not-run row", chunk="core",
                                       test_command=self.qa_command())
        (self.root / "test_walk.py").write_text(
            "import unittest\n\nclass Walk(unittest.TestCase):\n    def test_real(self):\n"
            "        print('FINAL CHECK: FAIL real-run (csumm exit 3)')\n"
            "        print('Not run for real: Claude summary - claude is not signed in here')\n")
        (self.root / "test_plain_failure.py").write_text(
            "import unittest\n\nclass Plain(unittest.TestCase):\n    def test_failure(self): self.fail('expected failure')\n")
        challenge = self.root / "docs" / "not-run-challenge.md"
        challenge.write_text(self.HEAD + "".join(self.ROW.format(**row) for row in rows))
        board.claim_qa(self.root, reviewer["id"], request["id"], str(challenge.relative_to(self.root)))
        return reviewer, request, challenge

    def test_a_row_that_says_not_run_gets_the_way_out_and_the_way_out_certifies(self):
        reviewer, request, challenge = self.claimed_review([
            {"id": "S-001", "what": "The smoke check still passes for the owner.", "module": "test_smoke -k passes"},
            {"id": "S-003", "what": "The real Claude summary is printed for the owner.", "module": "test_walk"}])
        with self.assertRaises(ValueError) as caught:
            board.execute_challenge(self.root, reviewer["id"], request["id"])
        message = str(caught.exception)
        self.assertIn("S-003 could not really run here", message)
        self.assertIn("attach-challenge-ledger --correction-reason", message)
        self.assertIn("never fails a review (D-024)", message)
        attempts = board.snapshot(self.root)["qa_requests"][request["id"]]["challenge_execution_attempts"]
        self.assertEqual((attempts[-1]["completed"], attempts[-1]["failed_scenarios"]), (True, ["S-003"]), "the attempt is recorded as before")
        challenge.write_text(self.HEAD + self.ROW.format(id="S-001", what="The smoke check still passes for the owner.", module="test_smoke -k passes"))
        board.attach_challenge_ledger(self.root, reviewer["id"], request["id"], str(challenge.relative_to(self.root)),
                                      "S-003 needs the Claude sign-in, which execute-challenge never has")
        board.execute_challenge(self.root, reviewer["id"], request["id"], "S-003 taken out: it needs a sign-in this run never has")
        self.assertTrue(board.snapshot(self.root)["qa_requests"][request["id"]].get("challenge_execution"), "the review now has its certified run")

    def test_an_ordinary_failure_keeps_the_ordinary_refusal(self):
        reviewer, request, _ = self.claimed_review([
            {"id": "S-002", "what": "A broken promise fails the check.", "module": "test_plain_failure"},
            {"id": "S-003", "what": "The real Claude summary is printed for the owner.", "module": "test_walk"}])
        with self.assertRaises(ValueError) as caught:
            board.execute_challenge(self.root, reviewer["id"], request["id"])
        self.assertNotIn("could not really run here", str(caught.exception), "a real failure beside it is never explained away")
        self.assertIn("S-002", str(caught.exception))


class AReviewStuckOnARowThatCannotRunNeverHoldsTheRepair(test_board.BoardTests):
    """Codex task review r3-1 (2026-11-22, real web project): the isolated browser could not start in the clean review copy, so the
    final review could never certify; the CTO routed a runnable repair, Delivery prepared it, and request-review was refused because
    that review was still active. Reviewer, builder and CTO all waited; no release. Now the refusal names the Reviewer's way out (and
    is not an owner card), and Delivery's new final review replaces a review stuck only on such rows. A real failed check still holds."""

    HEAD = AStepThatNeedsASignInNeverTrapsTheReview.HEAD

    def final_review_failing_with(self, rows):
        dev = self.delivery("TASK-STUCK-REVIEW")
        self.atomic_plan(dev["id"])
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        request = board.request_review(self.root, dev["id"], self.ledger("stuck-delivery.md"), "final review", phase="final_acceptance",
                                       test_command=self.qa_command())
        (self.root / "screens").mkdir()      # no check_*.py inside: the stand-in for a browser check that cannot start here
        (self.root / "test_plain_failure.py").write_text(
            "import unittest\n\nclass Plain(unittest.TestCase):\n    def test_failure(self): self.fail('expected failure')\n")
        challenge = self.root / "docs" / "stuck-challenge.md"
        challenge.write_text(self.HEAD + "".join(f"| {i} | {what} | `{command}` | The check is observed | PASS: observed | PASS |\n"
                                                 for i, what, command in rows))
        board.claim_qa(self.root, reviewer["id"], request["id"], str(challenge.relative_to(self.root)))
        with self.assertRaises(ValueError) as caught:
            board.execute_challenge(self.root, reviewer["id"], request["id"])
        return dev, reviewer, request, str(caught.exception)

    SCREEN = ("S-QA-03", "The owner's page shows the summary in the browser.", "python3 -m unittest discover -s screens -p 'check_*.py'")
    SMOKE = ("S-QA-01", "The smoke check still passes for the owner.", "python3 -m unittest test_smoke -k passes")
    BROKEN = ("S-QA-02", "A broken promise fails the check.", "python3 -m unittest test_plain_failure")

    def test_a_row_that_cannot_run_gets_the_way_out_and_the_repair_replaces_the_stuck_review(self):
        dev, reviewer, request, message = self.final_review_failing_with([self.SMOKE, self.SCREEN])
        self.assertIn("To fix it:", message)
        self.assertIn("attach-challenge-ledger --correction-reason", message)
        self.assertTrue(board_surface._names_its_own_fix(message), "a refusal that names its own fix is never an owner card")
        attempt = board.snapshot(self.root)["qa_requests"][request["id"]]["challenge_execution_attempts"][-1]
        self.assertTrue(attempt["process_only"])
        replacement = board.request_review(self.root, dev["id"], self.ledger("stuck-delivery.md"), "final review of the repair",
                                           phase="final_acceptance", test_command=self.qa_command(), changes="runnable HTTP check")
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"][request["id"]]["status"], "withdrawn")
        self.assertNotEqual(replacement["id"], request["id"])
        self.assertEqual((state["agents"][reviewer["id"]]["task"], state["agents"][reviewer["id"]]["status"]), ("REVIEW_QUEUE", "waiting"))
        self.assertTrue(any(e.get("kind") == "review_replaced_process_only" for e in state["events"]))

    def test_a_real_failure_keeps_the_review_and_the_active_review_rule(self):
        dev, _, request, message = self.final_review_failing_with([self.BROKEN, self.SCREEN])
        attempt = board.snapshot(self.root)["qa_requests"][request["id"]]["challenge_execution_attempts"][-1]
        self.assertFalse(attempt["process_only"], "a real failed check beside it is never explained away")
        # Owner 2026-11-27 rule 1 ("In no way this can be blocked"): the builder's newer version replaces the open review; the old
        # review's real failure is never counted as a pass (it is not the newest review and it is withdrawn, not passed)
        newer = board.request_review(self.root, dev["id"], self.ledger("stuck-delivery.md"), "final review again", phase="final_acceptance",
                                     test_command=self.qa_command(), changes="again")
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"][request["id"]]["status"], "withdrawn")
        self.assertNotEqual(newer["id"], request["id"])
        self.assertFalse(board.review_counts_as_passed(state, state["qa_requests"][request["id"]]))

    def test_a_real_failure_in_the_same_command_as_a_step_that_cannot_run_keeps_the_review(self):
        # Codex R4-1: one unittest command printed "Not run for real" for a skipped Linux step AND failed a real assertion
        (self.root / "test_mixed.py").write_text(
            "import unittest\n\nclass Checks(unittest.TestCase):\n"
            "    def test_linux(self):\n        print('Not run for real: Linux operation - no Linux runner here')\n"
            "        self.skipTest('no Linux runner')\n"
            "    def test_total(self): self.assertEqual(41, 42, 'owner total is wrong')\n")
        dev, _, request, message = self.final_review_failing_with(
            [("S-QA-01", "The app produces the required total and runs on Linux.", "python3 -m unittest test_mixed")])
        self.assertNotIn("To fix it:", message)
        attempt = board.snapshot(self.root)["qa_requests"][request["id"]]["challenge_execution_attempts"][-1]
        self.assertFalse(attempt["process_only"], "a failing runner is a real failure, whatever else it printed")
        # Owner 2026-11-27 rule 1 ("In no way this can be blocked"): the builder's newer version replaces the open review; the old
        # review's real failure is never counted as a pass (it is not the newest review and it is withdrawn, not passed)
        newer = board.request_review(self.root, dev["id"], self.ledger("stuck-delivery.md"), "final review again", phase="final_acceptance",
                                     test_command=self.qa_command(), changes="again")
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"][request["id"]]["status"], "withdrawn")
        self.assertNotEqual(newer["id"], request["id"])
        self.assertFalse(board.review_counts_as_passed(state, state["qa_requests"][request["id"]]))


def load_tests(loader, tests, pattern):
    """Only this file's own tests: the fixtures above are subclasses of other suites' test cases, whose tests run in their own files."""
    suite = unittest.TestSuite()
    for case in (EachRoleIsCheckedBeforeTheContract, NotRunAtReview, AStepThatNeedsASignInNeverTrapsTheReview,
                 AReviewStuckOnARowThatCannotRunNeverHoldsTheRepair, ARoleCheckFindsTheCLIsOwnInterpreter, RuleTexts):
        for name in sorted(n for n in vars(case) if n.startswith("test_")):
            suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Stages 1, 2, 3 and 4 for Gemini (spec T2): the gate on Antigravity payloads, the runtime directory, the flags.

Every behaviour is checked with the stage's switch off, a capability unproven, a dependency off, the install inside a
write grant, and the directory refused - each with its exact `PLUMBING FALLBACK` line - and with everything on.
"""
from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from harness import claude_inbox, cli_capabilities, control, global_settings, hook_gate, hook_rules, runtime_dir
from harness import gemini_runtime
from tests.test_gemini_capabilities import write_agy

PROJECT = ["/work/project", "/work/workspaces"]


def call(name, **args):
    return {"toolCall": {"name": name, "args": {key: json.dumps(value) if isinstance(value, str) else value for key, value in args.items()}}}


class GatePayloadTests(unittest.TestCase):
    """Each guard, on the shape the real CLI sends (captured on Linux with agy 1.3.1)."""

    def rule(self, role, payload):
        translated = hook_gate.gemini_call(payload)
        return hook_rules.evaluate(role, translated["tool_name"], translated["tool_input"], project_roots=PROJECT)[0]

    def test_r2_whole_tree_staging(self):
        self.assertEqual(self.rule("engineering", call("run_command", CommandLine="git add -A", Cwd="/work/project")), "R-2")
        self.assertEqual(self.rule("engineering", call("run_command", CommandLine="git add src/a.py")), "")

    def test_r3_background_helpers_and_the_daemon_flag(self):
        self.assertEqual(self.rule("engineering", call("run_command", CommandLine="nohup python3 s.py")), "R-3")
        self.assertEqual(self.rule("qa", call("run_command", CommandLine="pytest", IsDaemon=True)), "R-3")
        self.assertEqual(self.rule("engineering", call("run_command", CommandLine="make test", IsDaemon=False)), "")

    def test_r4_the_owners_logins_for_every_tool_that_names_a_path(self):
        token = str(Path.home() / ".gemini/antigravity-cli/antigravity-oauth-token")
        settings = str(Path.home() / ".gemini/antigravity-cli/settings.json")
        for tool, key in (("view_file", "AbsolutePath"), ("write_to_file", "TargetFile"), ("replace_file_content", "TargetFile"),
                          ("some_future_search_tool", "SearchPath")):
            self.assertEqual(self.rule("engineering", call(tool, **{key: token})), "R-4", tool)
        self.assertEqual(self.rule("engineering", call("view_file", AbsolutePath=settings)), "R-4")
        self.assertEqual(self.rule("engineering", call("run_command", CommandLine=f"cat {token}")), "R-4")
        self.assertEqual(self.rule("engineering", call("view_file", AbsolutePath="/work/project/README.md")), "")
        self.assertEqual(self.rule("engineering", call("run_command", CommandLine="cat ~/.codex/auth.json")), "R-4")

    def test_any_tool_that_takes_a_command_line_is_judged_as_a_shell(self):
        # Found by the pre-review audit: only `run_command` was a shell, so another tool carrying a command skipped R-2..R-4.
        token = str(Path.home() / ".codex/auth.json")
        for tool in ("send_command_input", "some_future_terminal_tool"):
            self.assertEqual(self.rule("engineering", call(tool, CommandLine=f"cat {token}")), "R-4", tool)
            self.assertEqual(self.rule("engineering", call(tool, CommandLine="git add -A")), "R-2", tool)

    def test_r5_a_reviewer_cannot_edit_the_work_under_review(self):
        for tool in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
            self.assertEqual(self.rule("qa", call(tool, TargetFile="/work/project/greet.py")), "R-5", tool)
        self.assertEqual(self.rule("qa", call("write_to_file", TargetFile="/tmp/review-1/ledger.md")), "")
        self.assertEqual(self.rule("engineering", call("write_to_file", TargetFile="/work/project/greet.py")), "")
        self.assertEqual(self.rule("qa", call("view_file", AbsolutePath="/work/project/greet.py")), "", "reading is fine")


class GateScriptTests(unittest.TestCase):
    def run_gate(self, event, payload, *, board=None, handover=None):
        output = io.StringIO()
        environment = {gemini_name: "" for gemini_name in ()}
        if handover:
            environment[hook_gate.GEMINI_HANDOVER_ENV] = handover
        with mock.patch.dict(os.environ, environment), mock.patch.object(hook_gate.board_client, "call", **({"side_effect": board} if callable(board) else {"return_value": board})), \
                redirect_stdout(output):
            code = hook_gate.main(["gemini", event]) if False else self._main(event, payload)
        return code, output.getvalue().strip()

    def _main(self, event, payload):
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            return hook_gate.main(["gemini", event])

    def test_a_denied_tool_call_is_answered_in_antigravitys_format(self):
        code, out = self.run_gate("PreToolUse", call("run_command", CommandLine="git add ."),
                                  board={"decision": "deny", "rule": "R-2", "reason": "No whole-tree staging."})
        self.assertEqual(code, 0)
        reply = json.loads(out)
        self.assertEqual(reply["decision"], "deny")
        self.assertIn("Blocked by the harness (R-2)", reply["reason"])

    def test_an_allowed_call_is_answered_with_an_empty_object_and_the_worker_sees_claudes_vocabulary(self):
        seen = []
        code, out = self.run_gate("PreToolUse", call("run_command", CommandLine="ls"),
                                  board=lambda argv, timeout=0: seen.append(argv) or {"decision": "allow"})
        # Measured on agy 1.3.1: an empty `{}` answer DENIES a tool call; the allow must be explicit.
        self.assertEqual(json.loads(out), {"decision": "allow"})
        payload = json.loads(seen[0][seen[0].index("--payload") + 1])
        self.assertEqual(payload, {"tool_name": "Bash", "tool_input": {"command": "ls", "run_in_background": False}})

    def test_a_guard_whose_worker_is_unreachable_denies_and_an_observation_stays_silent(self):
        def unreachable(*_args, **_kwargs):
            raise OSError("worker down")
        with mock.patch.object(hook_gate, "RETRY_PAUSE_SECONDS", 0):
            _code, out = self.run_gate("PreToolUse", call("run_command", CommandLine="ls"), board=unreachable)
        self.assertEqual(json.loads(out), {"decision": "deny", "reason": f"{hook_gate.UNREACHABLE} (OSError: worker down)"})
        _code, out = self.run_gate("PreInvocation", {"modelName": "gemini-3.8-flash-low"}, board=unreachable)
        self.assertEqual(out, "{}")

    def test_one_failed_worker_call_is_retried_before_the_tool_is_denied_and_the_cause_is_in_the_denial(self):
        calls = []

        def flaky(argv, timeout=0):
            calls.append(argv)
            if len(calls) == 1:
                raise OSError("worker busy")
            return {"decision": "allow"}
        with mock.patch.object(hook_gate, "RETRY_PAUSE_SECONDS", 0):
            _code, out = self.run_gate("PreToolUse", call("run_command", CommandLine="ls"), board=flaky)
            self.assertEqual((json.loads(out), len(calls)), ({"decision": "allow"}, 2), "a hiccup does not cost the agent a denied tool")

            def down(*_args, **_kwargs):
                raise OSError("worker down")
            _code, out = self.run_gate("PreToolUse", call("run_command", CommandLine="ls"), board=down)
        reply = json.loads(out)
        self.assertEqual(reply["decision"], "deny")
        self.assertIn("harness gate unreachable - retry (OSError: worker down)", reply["reason"])

    def test_events_map_to_the_boards_turn_states_and_the_model_is_reported(self):
        seen = []
        record = lambda argv, timeout=0: seen.append(argv) or {"decision": "allow"}
        self.run_gate("PreInvocation", {"modelName": "gemini-3.1-pro-high"}, board=record)
        self.run_gate("Stop", {"fullyIdle": False}, board=record)
        self.run_gate("Stop", {"fullyIdle": True}, board=record)
        events = [(argv[argv.index("--event") + 1], json.loads(argv[argv.index("--payload") + 1])) for argv in seen]
        self.assertEqual(events, [("UserPromptSubmit", {"model": "gemini-3.1-pro-high"}), ("Stop", {})],
                         "Stop with fullyIdle=false is not idle and reports nothing")

    def test_a_message_handed_over_by_the_supervisor_is_injected_even_when_the_board_report_fails(self):
        def broken(*_args, **_kwargs):
            raise OSError("board down")
        with mock.patch("harness.gemini_delivery.ask", return_value="[SYSTEM CONTROL — cto] go\n\n(harness delivery harness-1)"):
            _code, out = self.run_gate("PreInvocation", {"modelName": "gemini-3.8-flash-low"}, board=broken, handover="/x/g.sock")
        self.assertEqual(json.loads(out)["injectSteps"][0]["userMessage"].splitlines()[0], "[SYSTEM CONTROL — cto] go")

    def test_gemini_events_the_board_does_not_know_are_never_sent(self):
        _code, out = self.run_gate("SessionEnd", {}, board={"decision": "allow"})
        self.assertEqual(out, "{}")


class ModelSwitchTests(unittest.TestCase):
    """M-3: the owner switching a Gemini session to a third-party model in the CLI's window."""

    def test_a_non_google_model_is_flagged_and_a_google_one_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            settings = {"delivery": {"provider": "gemini", "model": "gemini-3.8-flash-low", "effort": "high"},
                        "cto": {"provider": "claude", "model": "opus", "effort": "high"},
                        "reviewer": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"}}
            session = control.create(root, "codex_delivery", settings_override=settings)
            self.assertFalse(control.record_observed_model(root, session["id"], "gemini-3.8-flash-low"))
            self.assertTrue(control.record_observed_model(root, session["id"], "claude-opus-4-6-thinking"), "first sighting reports")
            self.assertFalse(control.record_observed_model(root, session["id"], "claude-opus-4-6-thinking"), "only the change reports")
            flagged = next(item for item in control.snapshot(root)["sessions"] if item["id"] == session["id"])
            self.assertEqual(flagged["model_mismatch"], "claude-opus-4-6-thinking")
            control.record_observed_model(root, session["id"], "gemini-3.1-pro-high")
            cleared = next(item for item in control.snapshot(root)["sessions"] if item["id"] == session["id"])
            self.assertEqual(cleared["model_mismatch"], "")


class RuntimeDirectoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.home = self.base / "manager"
        self.home.mkdir()
        self.project = self.base / "proj"
        self.workspace = self.base / "ws"
        for path in (self.project, self.workspace):
            path.mkdir()
        self.agy = write_agy(self.base / "agy")
        patcher = mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(self.agy)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.harness = str(Path(__file__).resolve().parents[1])
        # The suite's own temp space is a write grant on every platform, so the manager home of these tests would be
        # refused (rightly). The grant the runner computes is replaced by the project's roots to test everything else.
        grant = mock.patch.object(gemini_runtime.agent_confinement, "writable_paths",
                                  side_effect=lambda roots, **_kwargs: list(roots))
        grant.start()
        self.addCleanup(grant.stop)

    def switch(self, **stages):
        settings = global_settings.load(self.home)
        for stage in global_settings.PLUMBING_STAGES:
            settings["plumbing"][f"{stage}_enabled"] = bool(stages.get(stage, False))
        global_settings._write(self.home, settings)

    def make_agy(self, version):
        agy = write_agy(self.base / f"agy-{version}")
        agy.write_text(agy.read_text().replace('echo "1.3.1"', f'echo "{version}"'))
        return agy

    def prove(self, *items):
        for item in items:
            cli_capabilities.record_live_item(self.home, "gemini", item, True, "proved in the test")

    def prepare(self, directive="THE DIRECTIVE", *, harness=None, writable=None):
        return gemini_runtime.prepare(
            self.home, str(self.project / ".harness"), "codex_delivery-abc", harness_root=harness or self.harness,
            python="/usr/bin/python3", writable=writable or [str(self.project), str(self.workspace)], role="engineering",
            agent="agent-1", directive=directive, timeout=5)

    def test_everything_off_writes_nothing_and_logs_nothing(self):
        result = self.prepare()
        self.assertEqual((result["add_dir"], result["notes"]), ("", []))
        self.assertFalse((self.home / "rt").exists())

    def test_a_switched_on_stage_with_an_unproven_capability_falls_back_and_says_exactly_why(self):
        self.switch(stage2_board_mcp=True)
        # An unproven major version has no development proof to start from: UNPROVEN gates exactly like false.
        agy = self.make_agy("2.0.0")
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)}):
            result = self.prepare()
        self.assertEqual(result["add_dir"], "")
        self.assertEqual(result["notes"], ["PLUMBING FALLBACK stage2_board_mcp gemini.session_scoped_controls=UNPROVEN, gemini.mcp_reaches_board=UNPROVEN"])
        # And on the proven version, this machine's own run finding the tools unreachable beats the development proof.
        cli_capabilities.record_live_item(self.home, "gemini", "gemini.mcp_reaches_board", False, "tools unreachable here")
        result = self.prepare()
        self.assertEqual(result["notes"], ["PLUMBING FALLBACK stage2_board_mcp gemini.mcp_reaches_board=false"])

    def test_a_stage_whose_dependency_is_off_is_refused(self):
        self.switch(stage3_gemini_delivery=True)
        status = cli_capabilities.stage_status(self.home, "stage3_gemini_delivery", "gemini")
        self.assertFalse(status["enabled"])
        self.assertIn("stage1_hooks", status["reason"])
        self.assertEqual(self.prepare()["add_dir"], "")

    def test_stage_three_without_its_proof_names_the_unproven_item(self):
        self.switch(stage1_hooks=True, stage3_gemini_delivery=True)
        # This machine's own run found the socket unreachable: its explicit false beats the development proof.
        cli_capabilities.record_live_item(self.home, "gemini", "gemini.confined_socket_connect", False, "sandbox refused the connect here")
        result = self.prepare()
        self.assertEqual(result["notes"], ["PLUMBING FALLBACK stage3_gemini_delivery gemini.confined_socket_connect=false"])
        self.assertTrue(result["add_dir"], "stage 1 still runs")
        self.assertFalse(result["delivery"])
        self.assertNotIn("--gemini-delivery", result["supervisor_args"])

    def test_hooks_board_tools_and_rules_land_in_the_runtime_directory_only(self):
        self.switch(stage1_hooks=True, stage2_board_mcp=True, stage3_gemini_delivery=True, stage4_system_layer_directives=True)
        self.prove("gemini.mcp_reaches_board")
        result = self.prepare("FULL DIRECTIVE TEXT")
        directory = Path(result["add_dir"])
        self.assertTrue(result["delivery"] and result["stage4"], result)
        self.assertEqual(directory, runtime_dir.session_directory(self.home, str(self.project / ".harness"), "codex_delivery-abc"))
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        hooks = json.loads((directory / ".agents" / "hooks.json").read_text())["harness-gate"]
        self.assertEqual(set(hooks), {"PreToolUse", "PreInvocation", "Stop"})
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], ".*")
        gate = hooks["PreInvocation"][0]
        self.assertEqual(gate["type"], "command")
        self.assertTrue(gate["command"].endswith("hook_gate.py gemini PreInvocation"), gate["command"])
        self.assertEqual(gate["timeout"], 10)
        plugin = directory / ".agents" / "plugins" / "harness_board"
        self.assertEqual(json.loads((plugin / "plugin.json").read_text()), {"name": "harness_board"})
        server = json.loads((plugin / "mcp_config.json").read_text())["mcpServers"]["harness_board"]
        self.assertEqual(server["args"][-4:], ["--role", "engineering", "--agent", "agent-1"])
        self.assertTrue(server["args"][1].endswith("harness/board_mcp.py"))
        rules = (directory / "GEMINI.md").read_text()
        self.assertIn("FULL DIRECTIVE TEXT", rules)
        self.assertIn(claude_inbox.AUTHORITY_NOTE, rules)
        self.assertEqual(result["supervisor_args"][:2], ["--runtime-dir", str(directory)])
        self.assertIn("--gemini-delivery", result["supervisor_args"])
        for owner_file in (Path.home() / ".gemini" / "config" / "hooks.json",):
            self.assertNotIn(str(directory), owner_file.read_text() if owner_file.exists() else "", "no owner file was written")

    def test_a_directive_larger_than_geminis_rules_limit_stays_in_the_first_message(self):
        # Measured on agy 1.3.1: "Rule file ... truncated by 13389 bytes (original 37387 bytes, limit 24000 bytes)".
        self.switch(stage1_hooks=True, stage3_gemini_delivery=True, stage4_system_layer_directives=True)
        big = "x" * 30000
        result = self.prepare(big)
        self.assertFalse(result["stage4"])
        self.assertTrue(result["delivery"], "the other stages are unaffected")
        self.assertTrue(any(note.startswith("PLUMBING FALLBACK stage4_system_layer_directives the directive is ") and
                            "24000 bytes" in note for note in result["notes"]), result["notes"])
        rules = (Path(result["add_dir"]) / "GEMINI.md").read_text()
        self.assertNotIn("xxxx", rules, "a directive that would be cut is not placed at all")
        self.assertIn(claude_inbox.AUTHORITY_NOTE, rules)
        fits = self.prepare("y" * 20000)
        self.assertTrue(fits["stage4"])
        self.assertIn("y" * 100, (Path(fits["add_dir"]) / "GEMINI.md").read_text())

    def test_the_hook_deadline_in_hooks_json_is_the_owners_setting_not_a_fixed_number(self):
        for seconds in (3, 20):
            settings = global_settings.load(self.home)
            settings["plumbing"]["hook_gate_timeout_seconds"] = seconds
            settings["plumbing"]["stage1_hooks_enabled"] = True
            global_settings._write(self.home, settings)
            result = gemini_runtime.prepare(
                self.home, str(self.project / ".harness"), "codex_delivery-abc", harness_root=self.harness, python="/usr/bin/python3",
                writable=[str(self.project), str(self.workspace)], role="engineering", agent="a", directive="")
            hooks = json.loads((Path(result["add_dir"]) / ".agents" / "hooks.json").read_text())["harness-gate"]
            self.assertEqual(hooks["PreToolUse"][0]["hooks"][0]["timeout"], seconds + 5,
                             "the CLI would otherwise kill the gate before its own deadline")
            self.assertEqual(result["gate_timeout"], seconds)

    def test_each_stage_writes_only_its_own_piece(self):
        self.switch(stage1_hooks=True)
        directory = Path(self.prepare()["add_dir"])
        self.assertTrue((directory / ".agents" / "hooks.json").is_file())
        self.assertFalse((directory / ".agents" / "plugins").exists())
        self.assertFalse((directory / "GEMINI.md").exists())
        hooks = json.loads((directory / ".agents" / "hooks.json").read_text())["harness-gate"]
        self.assertIn("PreToolUse", hooks)

    def test_the_runtime_directory_is_never_inside_an_agent_write_grant(self):
        self.switch(stage1_hooks=True)
        inside = self.home / "rt"
        result = self.prepare(writable=[str(inside)])
        self.assertEqual(result["add_dir"], "")
        self.assertTrue(result["notes"][0].startswith("PLUMBING FALLBACK stage1_hooks the harness runtime directory"), result["notes"])

    def test_a_harness_install_inside_the_grant_switches_the_gate_and_tools_off(self):
        self.switch(stage1_hooks=True)
        result = self.prepare(writable=[self.harness])
        self.assertEqual(result["add_dir"], "")
        self.assertIn("PLUMBING FALLBACK stage1_hooks harness install is inside an agent write grant", result["notes"])

    def test_a_second_preparation_replaces_the_files_it_wrote_before(self):
        self.switch(stage1_hooks=True, stage2_board_mcp=True)
        self.prove("gemini.mcp_reaches_board")
        first = Path(self.prepare()["add_dir"])
        self.assertTrue((first / ".agents" / "plugins").exists())
        self.switch(stage1_hooks=True)
        second = Path(self.prepare()["add_dir"])
        self.assertEqual(first, second)
        self.assertFalse((second / ".agents" / "plugins").exists(), "no stale piece of the previous launch survives")


def tearDownModule():
    from tests.environment_support import assert_agy_pinned
    assert_agy_pinned()


if __name__ == "__main__":
    unittest.main()

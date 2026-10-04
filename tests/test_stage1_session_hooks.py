# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Plumbing Stage 1: session-scoped hooks - real turn state and mechanical enforcement.

The directive's "never do X" rules become a PreToolUse decision made by the
worker; the CLI's own lifecycle hooks report when a turn really ends. Hooks are
passed inline on the launch line (no file to tamper with), only when the
owner's switch is on and the CLI's capabilities are proven; otherwise the
launch is exactly as before.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, cli_capabilities, control, global_settings, hook_rules, project_registry
from harness.board_client import ENDPOINT_ENV, PROTOCOL_ENV, TOKEN_ENV
from harness.board_surface import PROTOCOL_VERSION, SessionTokenAuthority
from harness.project_context import ProjectContext
from tests import test_board_surface as surface
from tests.environment_support import require_loopback

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_managed_agent.sh"
GATE = ROOT / "harness" / "hook_gate.py"
PROJECT = ["/work/project", "/work/workspaces"]


class RuleTests(unittest.TestCase):
    def check(self, role, tool, tool_input, rule):
        self.assertEqual(hook_rules.evaluate(role, tool, tool_input, project_roots=PROJECT)[0], rule,
                         f"{role} {tool} {tool_input}")

    def test_whole_tree_staging_is_refused_and_explicit_paths_are_not(self):
        for command in ("git add -A", "git add .", "git add --all", "git -C /work/project add -A", "cd x && git add . && git commit -m m"):
            self.check("engineering", "Bash", {"command": command}, "R-2")
        for command in ("git add src/a.py tests/test_a.py", "git commit -m 'x'", "git push origin task/x", "git add ./src/a.py"):
            self.check("engineering", "Bash", {"command": command}, "")

    def test_background_helpers_and_board_poll_loops_are_refused(self):
        for command in ("nohup python3 server.py", "sleep 100 &", "python3 x.py & echo started", "caffeinate -i make",
                        "while true; do python3 /h/harness/board.py --root . poll; sleep 30; done",
                        "watch -n 5 python3 /h/harness/board.py --root . poll"):
            self.check("engineering", "Bash", {"command": command}, "R-3")
        self.check("qa", "Bash", {"command": "pytest", "run_in_background": True}, "R-3")
        for command in ("make && make test", "python3 x.py 2>&1 | tail", "curl -s 'http://h/a?x=1&y=2'",
                        "for f in *.py; do python3 -m py_compile $f; done", "python3 /h/harness/board.py --root . poll"):
            self.check("engineering", "Bash", {"command": command}, "")

    def test_the_owners_login_files_are_refused_by_tool_and_by_shell(self):
        self.check("engineering", "Read", {"file_path": str(Path.home() / ".codex" / "auth.json")}, "R-4")
        self.check("cto", "Read", {"file_path": "/Users/x/.claude/.credentials.json"}, "R-4")
        self.check("engineering", "Bash", {"command": "cat ~/.codex/auth.json"}, "R-4")
        self.check("engineering", "Read", {"file_path": "/work/project/README.md"}, "")

    def test_a_reviewer_cannot_edit_the_work_under_review(self):
        self.check("qa", "Edit", {"file_path": "/work/project/greet.py"}, "R-5")
        self.check("qa", "Write", {"file_path": "/work/workspaces/task/greet.py"}, "R-5")
        self.check("qa", "Write", {"file_path": "/tmp/review-xyz/ledger.md"}, "")
        self.check("engineering", "Edit", {"file_path": "/work/project/greet.py"}, "")


class WorkerAndGateTests(unittest.TestCase):
    """The worker's decision and the gate script, over the real authenticated channel."""

    served = surface.BoardSurfaceCommandTests.served
    session = surface.BoardSurfaceCommandTests.session

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, code / ".harness", base / "workspaces")
        control.initialize(self.context)

    def gate(self, token, endpoint, event, payload, deadline="5"):
        environment = {**os.environ, TOKEN_ENV: token, ENDPOINT_ENV: endpoint, PROTOCOL_ENV: PROTOCOL_VERSION,
                       "HARNESS_HOOK_GATE_TIMEOUT": deadline}
        return subprocess.run([os.path.realpath(os.sys.executable), "-E", str(GATE), "claude", event],
                              input=json.dumps(payload), env=environment, capture_output=True, text=True, timeout=30)

    def test_a_forbidden_tool_call_is_denied_in_claudes_format_and_the_cto_sees_it(self):
        session, agent, authority, token, _ = self.session()
        with self.served(authority) as endpoint:
            denied = self.gate(token, endpoint, "PreToolUse",
                               {"tool_name": "Bash", "tool_input": {"command": "git add -A"}})
            allowed = self.gate(token, endpoint, "PreToolUse",
                                {"tool_name": "Bash", "tool_input": {"command": "git add greet.py"}})
        self.assertEqual(denied.returncode, 0, denied.stderr)
        decision = json.loads(denied.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("(R-2)", decision["permissionDecisionReason"])
        self.assertEqual(allowed.stdout.strip(), "", "an allowed call prints nothing")
        events = [event for event in board.snapshot(self.context)["events"] if event["kind"] == "agent_action_denied"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["rule"], "R-2")

    def test_lifecycle_events_record_the_real_turn_state(self):
        session, agent, authority, token, _ = self.session()
        with self.served(authority) as endpoint:
            for event, expected in (("SessionStart", "session_started"), ("UserPromptSubmit", "working"),
                                    ("Stop", "idle_at_prompt")):
                result = self.gate(token, endpoint, event, {})
                self.assertEqual((result.returncode, result.stdout.strip()), (0, ""))
                record = next(item for item in control.snapshot(self.context)["sessions"] if item["id"] == session["id"])
                self.assertEqual(record["turn_state"], expected)

    def test_unreachable_gate_fails_closed_for_tools_and_open_for_lifecycle(self):
        session, agent, authority, token, _ = self.session()
        dead = "http://127.0.0.1:9"
        guarded = self.gate(token, dead, "PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, deadline="1")
        decision = json.loads(guarded.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("unreachable", decision["permissionDecisionReason"])
        report = self.gate(token, dead, "Stop", {}, deadline="1")
        self.assertEqual((report.returncode, report.stdout.strip()), (0, ""))

    def test_an_agent_cannot_report_for_another_session(self):
        mine, agent, authority, token, _ = self.session()
        other, _, _, _, _ = self.session("claude_reviewer", "qa", "REVIEW_QUEUE")
        with self.served(authority) as endpoint:
            environment = {**os.environ, TOKEN_ENV: token, ENDPOINT_ENV: endpoint, PROTOCOL_ENV: PROTOCOL_VERSION}
            subprocess.run([os.path.realpath(os.sys.executable), "-E", str(ROOT / "harness" / "board.py"),
                            "--root", str(self.context.code_root), "hook-event", "--agent", "qa-0001-forged",
                            "--event", "Stop"], env=environment, capture_output=True, text=True, timeout=30)
        sessions = {item["id"]: item for item in control.snapshot(self.context)["sessions"]}
        self.assertNotIn("turn_state", sessions[other["id"]], "the server binds --agent to the token's own session")


class RunnerTests(unittest.TestCase):
    """The real runner: hooks appear on the launch line only when switched on and proven."""

    served = surface.BoardSurfaceAuthenticationTests.served
    bootstrap_served = surface.BoardSurfaceAuthenticationTests.bootstrap_served

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        code = self.base / "code"; code.mkdir()
        self.context = ProjectContext(code, self.base / "home" / "projects" / "p1" / "data",
                                      self.base / "home" / "projects" / "p1" / "workspaces")
        control.initialize(self.context)
        self.home = self.base / "home"
        project_registry.save(self.home, {"version": project_registry.REGISTRY_VERSION, "projects": [
            {"id": "p1", "name": "project", "code_root": str(code), "data_root": str(self.context.data_root),
             "workspace_root": str(self.context.workspace_root)}]})
        self.capture = self.base / "argv.json"
        self.fake = self.base / "fake-claude"
        self.fake.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                             f"json.dump(sys.argv[1:], open({str(self.capture)!r}, 'w'))\n", encoding="utf-8")
        self.fake.chmod(0o755)
        self.environment = {"HARNESS_CLAUDE_BIN": str(self.fake), "CLAUDE_CONFIG_DIR": str(self.base / "claude-config")}

    def switch(self, on: bool, capable: bool) -> None:
        settings = global_settings.load(self.home)
        settings["plumbing"]["stage1_hooks_enabled"] = on
        global_settings._write(self.home, settings)
        identity = cli_capabilities.binary_identity("claude", source_environment={**os.environ, **self.environment})
        path = cli_capabilities.cache_path(self.home, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"identity": identity, "probed_at": "test", "probe_version": cli_capabilities.PROBE_VERSION, "static": {
            "claude.settings_flag": True, "claude.inline_settings_hooks": capable}}), encoding="utf-8")

    def launch(self) -> tuple[subprocess.CompletedProcess, list[str]]:
        if not shutil.which("sandbox-exec") and not Path("/usr/bin/sandbox-exec").exists() and not shutil.which("bwrap"):
            self.skipTest("no write-confinement primitive on this platform")
        session = control.create(self.context, "claude_cto")
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        with self.served(authority) as endpoint, self.bootstrap_served(authority, endpoint) as bootstrap:
            completed = subprocess.run([
                "/bin/bash", str(RUNNER), "--root", str(self.context.code_root),
                "--data-root", str(self.context.data_root), "--workspace-root", str(self.context.workspace_root),
                "--python", os.path.realpath(os.sys.executable), "--session-id", session["id"], "--kind", "claude_cto",
                "--manager-home", str(self.home), "--board-bootstrap", bootstrap,
            ], cwd=self.context.code_root, env={**os.environ, "HARNESS_EXECUTION_ROOT": str(self.context.code_root),
                                                 **self.environment},
                capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.transcript = (self.context.data_root / "control" / "transcripts" / f"{session['id']}.log").read_text(encoding="utf-8")
        return completed, json.loads(self.capture.read_text(encoding="utf-8"))

    def test_switched_off_the_launch_line_has_no_hooks(self):
        self.switch(on=False, capable=True)
        _, argv = self.launch()
        self.assertNotIn("--settings", argv)
        self.assertNotIn("PLUMBING FALLBACK", self.transcript)

    def test_switched_on_and_proven_the_hooks_are_inline_and_name_the_gate(self):
        self.switch(on=True, capable=True)
        _, argv = self.launch()
        settings = json.loads(argv[argv.index("--settings") + 1])
        hooks = settings["hooks"]
        self.assertEqual(set(hooks), {"PreToolUse", "SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"})
        command = hooks["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn(str(GATE), command)
        self.assertTrue(command.endswith("claude PreToolUse"))
        self.assertIn("Bash", hooks["PreToolUse"][0]["matcher"])

    def test_switched_on_but_unproven_falls_back_and_says_why(self):
        self.switch(on=True, capable=False)
        _, argv = self.launch()
        self.assertNotIn("--settings", argv)
        self.assertIn("PLUMBING FALLBACK stage1_hooks claude.inline_settings_hooks=false", self.transcript)


class RealClaudeTests(unittest.TestCase):
    """The real Claude CLI runs the inline hook command with the session's own environment."""

    served = surface.BoardSurfaceCommandTests.served
    session = surface.BoardSurfaceCommandTests.session

    def setUp(self):
        require_loopback()
        self.claude = shutil.which("claude")
        if not self.claude:
            self.skipTest("the Claude CLI is not installed here")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, code / ".harness", base / "workspaces")
        control.initialize(self.context)

    def test_session_start_through_the_real_cli_reaches_the_worker(self):
        session, agent, authority, token, _ = self.session()
        gate = f"{os.path.realpath(os.sys.executable)} -E {GATE} claude"
        settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": f"{gate} SessionStart"}]}]}}
        with self.served(authority) as endpoint:
            environment = {**os.environ, TOKEN_ENV: token, ENDPOINT_ENV: endpoint, PROTOCOL_ENV: PROTOCOL_VERSION,
                           "CLAUDE_CONFIG_DIR": str(Path(self._tmp.name) / "claude-config")}
            subprocess.run([self.claude, "-p", "stage1 probe", "--output-format", "json", "--settings", json.dumps(settings)],
                           cwd=self.context.code_root, env=environment, capture_output=True, text=True, timeout=90)
        record = next(item for item in control.snapshot(self.context)["sessions"] if item["id"] == session["id"])
        self.assertEqual(record.get("turn_state"), "session_started")


if __name__ == "__main__":
    unittest.main()

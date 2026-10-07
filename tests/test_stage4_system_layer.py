# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Plumbing Stage 4: the rules in the system layer, on every launch, fresh or resume.

Spec docs/specs/PLUMBING_MODERNIZATION.md, Stage 4 (P-4): the directive used to
be the first USER message, which compaction drops and a resume replaced with a
100-word recovery note. Switched on and proven, Claude gets it through
`--append-system-prompt` (snapshot off) and Codex through
`-c developer_instructions`, and the visible first message is the short role
kickoff. Switched off, the prompt is byte-for-byte today's.
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

from tests.claude_auth_support import auth_arguments

from harness import cli_capabilities, conversation, control, global_settings, project_registry
from harness.board_surface import SessionTokenAuthority
from harness.project_context import ProjectContext
from tests import test_board_surface as surface
from tests.environment_support import require_loopback
from tests.test_conversation_memory import stage_relaunch

ROOT = Path(__file__).resolve().parents[1]
# As the runner reads it: `$(<file)` drops the file's final newline (today's behaviour too).
AGENT = (ROOT / "directives" / "AGENT.md").read_text(encoding="utf-8").rstrip("\n")


class Stage4RunnerTests(unittest.TestCase):
    served = surface.BoardSurfaceAuthenticationTests.served
    bootstrap_served = surface.BoardSurfaceAuthenticationTests.bootstrap_served

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(os.path.realpath(self._tmp.name))
        code = self.base / "code"; code.mkdir()
        # A manager home outside temp space, as in production (Stage 3's
        # runtime directory is refused inside an agent's temp grant).
        try:
            from tests.environment_support import home_outside_temp_space
            self.home = home_outside_temp_space(self, ".hn4home-")
            self.home_outside_temp = True
        except unittest.SkipTest:
            self.home = self.base / "home"          # Stage 4 alone needs no runtime directory
            self.home_outside_temp = False
        self.context = ProjectContext(code, self.home / "projects" / "p1" / "data", self.home / "projects" / "p1" / "workspaces")
        control.initialize(self.context)
        project_registry.save(self.home, {"version": project_registry.REGISTRY_VERSION, "projects": [
            {"id": "p1", "name": "project", "code_root": str(code), "data_root": str(self.context.data_root),
             "workspace_root": str(self.context.workspace_root)}]})
        self.capture = self.base / "argv.jsonl"
        self.claude_dir = self.base / "claude-config"
        self.environment = {"HARNESS_CLAUDE_BIN": str(self.fake("fake-claude")), "HARNESS_CODEX_BIN": str(self.fake("fake-codex")),
                            "CLAUDE_CONFIG_DIR": str(self.claude_dir), "CODEX_HOME": str(self.base / "codex-home")}

    def fake(self, name: str) -> Path:
        path = self.base / name
        path.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                        f"open({str(self.capture)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def switch(self, provider: str, *, stage4: bool, stage3: bool = False) -> None:
        settings = global_settings.load(self.home)
        settings["plumbing"]["stage4_system_layer_directives_enabled"] = stage4
        settings["plumbing"]["stage1_hooks_enabled"] = stage3
        settings["plumbing"]["stage3_claude_socket_delivery_enabled"] = stage3
        global_settings._write(self.home, settings)
        source = {**os.environ, **self.environment}
        identity = cli_capabilities.binary_identity(provider, source_environment=source)
        needed = set()
        for stage in cli_capabilities.STAGE_REQUIREMENTS:
            needed.update(cli_capabilities.STAGE_REQUIREMENTS[stage].get(provider, ()))
        static = [name for name in needed if name in cli_capabilities.STATIC_ITEMS[provider]]
        path = cli_capabilities.cache_path(self.home, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"identity": identity, "probed_at": "test", "probe_version": cli_capabilities.PROBE_VERSION, "static": {name: True for name in static}}),
                        encoding="utf-8")
        cli_capabilities.record_live(self.home, provider, {name: True for name in needed if name not in static},
                                     auth_mode="test", source_environment=source)

    def launch(self, session: dict, kind: str) -> list[str]:
        if self.capture.exists():
            self.capture.unlink()
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        with self.served(authority) as endpoint, self.bootstrap_served(authority, endpoint) as bootstrap:
            completed = subprocess.run([
                "/bin/bash", str(ROOT / "scripts" / "run_managed_agent.sh"), "--root", str(self.context.code_root),
                "--data-root", str(self.context.data_root), "--workspace-root", str(self.context.workspace_root),
                "--python", os.path.realpath(sys.executable), "--session-id", session["id"], "--kind", kind,
                "--manager-home", str(self.home), "--board-bootstrap", bootstrap,
                *auth_arguments(self.context, session),
            ], cwd=self.context.code_root, env={**os.environ, "HARNESS_EXECUTION_ROOT": str(self.context.code_root), **self.environment},
                capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        launches = [json.loads(line) for line in self.capture.read_text().splitlines()]
        launches = [argv for argv in launches if argv != ["--version"]]
        self.assertEqual(len(launches), 1)
        return launches[0]

    def require_confinement(self):
        if not Path("/usr/bin/sandbox-exec").exists() and not shutil.which("bwrap"):
            self.skipTest("no write-confinement primitive on this platform")

    def test_claude_fresh_and_resume_both_carry_the_rules_in_the_system_layer(self):
        self.require_confinement()
        self.switch("claude", stage4=True)
        session = control.create(self.context, "claude_reviewer")
        argv = self.launch(session, "claude_reviewer")
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], AGENT)
        self.assertEqual(argv[argv.index("--system-prompt-snapshot") + 1], "off", "never replayed stale after an upgrade")
        self.assertTrue(argv[-1].startswith("MODE: Independent Reviewer."), "the visible first message is the kickoff")
        self.assertNotIn("# Agent Directive", argv[-1])
        minted = argv[argv.index("--session-id") + 1]
        store = self.claude_dir / "projects" / "-code"; store.mkdir(parents=True)
        (store / f"{minted}.jsonl").write_text("{}\n", encoding="utf-8")
        stage_relaunch(self.context, session["id"])
        argv = self.launch(session, "claude_reviewer")
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], AGENT,
                         "P-4: a resumed agent gets its full rules back, not only the recovery note")
        self.assertIn(conversation.RECOVERY_LABEL, argv[-1])

    def test_codex_gets_the_rules_as_developer_instructions(self):
        self.switch("codex", stage4=True)
        session = control.create(self.context, "codex_delivery")
        argv = self.launch(session, "codex_delivery")
        flags = [argv[index + 1] for index, item in enumerate(argv) if item == "-c"]
        instructions = next(flag for flag in flags if flag.startswith("developer_instructions="))
        self.assertEqual(json.loads(instructions.split("=", 1)[1]), AGENT)
        self.assertTrue(argv[-1].startswith("MODE: Delivery Agent."), argv[-1][:80])

    def test_switched_off_the_prompt_is_byte_for_byte_todays(self):
        self.require_confinement()
        self.switch("claude", stage4=False)
        argv = self.launch(control.create(self.context, "claude_reviewer"), "claude_reviewer")
        self.assertNotIn("--append-system-prompt", argv)
        self.assertTrue(argv[-1].startswith(AGENT + "\n\nMODE: Independent Reviewer.\n"), "directive, blank line, kickoff")
        self.switch("codex", stage4=False)
        argv = self.launch(control.create(self.context, "codex_delivery"), "codex_delivery")
        self.assertFalse(any(item.startswith("developer_instructions=") for item in argv))
        self.assertTrue(argv[-1].startswith(AGENT + "\n\nMODE: Delivery Agent.\n"))

    def test_with_stage_3_the_authority_note_is_folded_into_the_same_system_text(self):
        from harness import claude_inbox
        self.require_confinement()
        if not self.home_outside_temp:
            self.skipTest("this checkout lives in temp space; Stage 3 refuses a runtime directory there by design")
        self.switch("claude", stage4=True, stage3=True)
        argv = self.launch(control.create(self.context, "claude_reviewer"), "claude_reviewer")
        self.assertEqual(argv.count("--append-system-prompt"), 1)
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], AGENT + "\n\n" + claude_inbox.AUTHORITY_NOTE)


if __name__ == "__main__":
    unittest.main()

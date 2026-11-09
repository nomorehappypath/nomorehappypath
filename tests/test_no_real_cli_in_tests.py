# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The test suite never starts the owner's real Codex or Claude (2026-11-08, task accept-after-repair R10).

Twice on 2026-11-08 a suite run launched the real Codex reviewer through scripts/run_managed_agent.sh in an empty
throw-away home, and Codex opened a sign-in window on the owner's screen. tests/__init__.py now points both CLI names
at stubs; these tests pin that the pin is in place and that the stub refuses real agent work loudly."""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

import tests
from harness import global_settings


@unittest.skipIf(os.environ.get(tests.REAL_CLI_OPT_IN) == "1", "the run opted into the real CLIs")
class StubCliPinTests(unittest.TestCase):
    def test_both_cli_names_point_at_stubs_inside_the_run_root(self):
        for name, env_name in (("codex", "HARNESS_CODEX_BIN"), ("claude", "HARNESS_CLAUDE_BIN")):
            path = Path(os.environ.get(env_name, ""))
            self.assertEqual(path.name, name, env_name)
            self.assertEqual(path.parent, tests.RUN_ROOT / tests.STUB_CLI_DIR_NAME, env_name)
            self.assertTrue(path.is_file() and os.access(path, os.X_OK), env_name)

    def test_path_lookups_find_the_stubs_first(self):
        import shutil
        for name in ("codex", "claude"):
            self.assertEqual(Path(shutil.which(name) or "").parent, tests.RUN_ROOT / tests.STUB_CLI_DIR_NAME, name)

    def test_the_managed_runner_would_resolve_the_stub_not_the_real_cli(self):
        for provider in ("codex", "claude"):
            resolved = global_settings.resolved_cli(provider)
            self.assertEqual(resolved["source"], "configured", provider)
            self.assertEqual(Path(resolved["path"]).parent, tests.RUN_ROOT / tests.STUB_CLI_DIR_NAME, provider)

    def test_the_stub_answers_version_and_refuses_real_agent_work(self):
        stub = os.environ["HARNESS_CODEX_BIN"]
        version = subprocess.run([stub, "--version"], capture_output=True, text=True, timeout=10)
        self.assertEqual(version.returncode, 0)
        self.assertIn("test stub", version.stdout)
        launch = subprocess.run([stub, "--cd", "/tmp", "--model", "gpt-6-sol", "Poll the board once."], capture_output=True, text=True, timeout=10)
        self.assertEqual(launch.returncode, 2)
        self.assertIn("a test asked a stub for real agent work", launch.stderr)
        self.assertIn(tests.REAL_CLI_OPT_IN, launch.stderr)


if __name__ == "__main__":
    unittest.main()

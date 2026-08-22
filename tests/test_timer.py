# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import timer
from harness.project_context import ProjectContext


class TimerTests(unittest.TestCase):
    def test_cron_renders_scheduler_dispatch_on_interval(self):
        line = timer.cron(Path("/tmp/project"), Path("/tmp/project/profile.json"), 5)
        self.assertIn("*/5 * * * *", line)
        self.assertIn(str(timer.SCHEDULER_SCRIPT), line)
        self.assertNotIn("-m harness.scheduler", line)
        self.assertIn(str(Path(sys.executable).resolve()), line)
        self.assertIn(" -E ", line)
        self.assertIn("--execute", line)
        self.assertIn("profile.json", line)

    def test_launchd_renders_start_interval(self):
        plist = timer.launchd(Path("/tmp/project"), Path("/tmp/project/profile.json"), 300, "com.test.board")
        self.assertIn("com.test.board", plist)
        self.assertIn("<integer>300</integer>", plist)
        self.assertIn(str(timer.SCHEDULER_SCRIPT), plist)
        self.assertNotIn("-m</string>", plist)
        self.assertIn("<string>-E</string>", plist)
        self.assertIn("--execute", plist)

    def test_generated_scheduler_command_runs_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            code = base / "adopted"; code.mkdir()
            data = base / "manager-data"
            workspaces = base / "task-workspaces"
            elsewhere = base / "operator-workspace"; elsewhere.mkdir()
            context = ProjectContext(code, data, workspaces)
            profile = base / "profile.json"
            profile.write_text(
                json.dumps({
                    "project_name": "test",
                    "default_branch": "main",
                    "test_command": "python3 -m unittest",
                    "build_command": "true",
                    "health_command": "true",
                    "deployment_channels": ["local"],
                    "agent_commands": {},
                }),
                encoding="utf-8",
            )
            completed = subprocess.run(
                timer.command(context, profile),
                cwd=elsewhere,
                shell=True,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PYTHONHOME": str(base / "foreign-python-home"),
                    "PYTHONPATH": str(base / "foreign-python-path"),
                    "PYTHONSTARTUP": str(base / "foreign-startup.py"),
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(context.storage_path("board", "dispatches.jsonl").is_file())
            self.assertFalse((code / ".harness").exists())


if __name__ == "__main__":
    unittest.main()

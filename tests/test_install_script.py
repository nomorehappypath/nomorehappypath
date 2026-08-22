# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The installer must be honest and safe: clear checks, no accidental installs."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install.sh"


class InstallScriptTests(unittest.TestCase):
    def run_script(self, *arguments, env_home: Path | None = None):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(env_home or Path.home())}
        return subprocess.run(
            ["bash", str(SCRIPT), *arguments],
            capture_output=True, text=True, timeout=30, env=env, cwd=ROOT,
        )

    def test_script_parses(self):
        completed = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_unknown_flag_refuses_with_usage(self):
        completed = self.run_script("--bogus")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Usage:", completed.stdout)

    def test_uninstall_without_service_changes_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            completed = self.run_script("--uninstall", env_home=Path(home))
            self.assertEqual(completed.returncode, 0)
            self.assertIn("Nothing to remove", completed.stdout)
            self.assertFalse(
                (Path(home) / "Library" / "LaunchAgents").exists(),
                "uninstall must not create service files",
            )

    def test_check_names_every_prerequisite_and_the_api_key(self):
        completed = self.run_script("--check")
        for marker in ("macOS", "Python", "Codex CLI", "Claude Code CLI", "OpenAI API key"):
            self.assertIn(marker, completed.stdout)
        # --check must never install anything or prompt
        self.assertNotIn("Choose 1 or 2", completed.stdout)

    def test_readme_prerequisites_match_the_installer_story(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for marker in ("What you need", "Codex CLI", "Claude Code CLI",
                       "OpenAI API key", "your own accounts", "install.sh", "First run:"):
            self.assertIn(marker, readme)


if __name__ == "__main__":
    unittest.main()


class StopAllScriptTests(unittest.TestCase):
    SCRIPT = ROOT / "scripts" / "stop_all.sh"

    def test_script_parses_and_has_help(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(self.SCRIPT)],
                                        capture_output=True, timeout=10).returncode, 0)
        completed = subprocess.run(["bash", str(self.SCRIPT), "--help"],
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--list", completed.stdout)

    def test_list_mode_stops_nothing_and_scopes_by_this_installation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            completed = subprocess.run(
                ["bash", str(self.SCRIPT), "--list"], capture_output=True,
                text=True, timeout=15, env={"PATH": "/usr/bin:/bin", "HOME": home},
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(str(ROOT), completed.stdout, "must announce ITS installation path")
        self.assertNotIn("force-stopped", completed.stdout)

    def test_stop_run_with_nothing_running_touches_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            completed = subprocess.run(
                ["bash", str(self.SCRIPT)], capture_output=True, text=True,
                timeout=15, env={"PATH": "/usr/bin:/bin", "HOME": home},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("untouched", completed.stdout)
            self.assertFalse((Path(home) / "Library" / "LaunchAgents").exists())

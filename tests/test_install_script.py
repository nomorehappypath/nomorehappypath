# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The installer must be honest and safe: clear checks, no accidental installs."""
from __future__ import annotations

import platform
import sys
import subprocess
import tempfile
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
        for marker in ("Python", "Codex CLI", "Claude Code CLI", "OpenAI API key"):
            self.assertIn(marker, completed.stdout)
        # The PLATFORM must be NAMED, not one platform's name asserted.
        #
        # This required the literal "macOS", so it failed on Linux while the
        # installer did exactly the right thing and reported "Ubuntu 24.04.4
        # LTS". My first correction then asked for platform.system() — "Linux" —
        # which the installer also never prints, because it reports the DISTRO.
        # Both versions guessed at the wording instead of asking the code.
        #
        # The contract is that the check prints whatever the seam's
        # platform_display_label returns, so the test asks the seam.
        label = subprocess.run(
            ["bash", "-c", f'source "{ROOT}/scripts/platform_support.sh"; platform_display_label'],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertTrue(label, "the seam must produce a platform label")
        self.assertIn(label, completed.stdout,
                      "the check must name the platform it is running on")
        # --check must never install anything or prompt
        self.assertNotIn("Choose 1 or 2", completed.stdout)

    def test_readme_prerequisites_match_the_installer_story(self):
        # Whitespace is collapsed so a reflowed line can never break a pin -
        # wrapped phrases evade plain substring checks (a lesson learned twice).
        readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
        for marker in ("What you need", "Codex CLI", "Claude Code CLI",
                       "OpenAI API key", "your own accounts", "install.sh", "First run:",
                       "## FAQ",
                       "Why not just Claude Code?",
                       "Why not just Codex?",
                       "hires the judge",
                       "What does the CTO role add",
                       "the CTO watches the",
                       "real, serious products",
                       "carry revenue",
                       "paying for the right to trust",
                       "cannot alter or",
                       "When should I NOT use it?"):
            self.assertIn(marker, readme)


class HeadlessInstallTests(unittest.TestCase):
    """A successful install must not report failure because no browser opened.

    The reviewer proved this on Ubuntu: with the service installed and the app
    answering, a failing or absent opener made the installer exit 3 IMMEDIATELY
    AFTER printing that NoMoreHappyPath was running. A successful install
    reported as a failure is worse than no message at all — the owner undoes
    work that succeeded.

    Stage 0 deliberately left that line a bare `open`, because routing it would
    have changed exit behaviour and Stage 0 forbade that. Supporting Linux
    inverted the calculation: headless is the normal case there.
    """

    def test_the_final_open_goes_through_the_seam(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('owner_open_url "http://127.0.0.1:8740/"', installer,
                      "the success-path open must not be able to fail the install")
        self.assertNotIn('        open "http://127.0.0.1:8740/"', installer,
                         "a bare open under set -e fails the installer on a headless box")

    def test_the_opener_succeeds_when_no_opener_exists(self):
        """The behaviour that makes the above safe, exercised for real."""
        with tempfile.TemporaryDirectory() as empty:
            # Absolute bash: PATH is deliberately empty so neither `open` nor
            # `xdg-open` can be found, which is the condition under test.
            completed = subprocess.run(
                ["/bin/bash", "-c",
                 f'set -euo pipefail; source "{ROOT}/scripts/platform_support.sh"; '
                 f'owner_open_url "http://127.0.0.1:8740/"'],
                capture_output=True, text=True,
                env={"HOME": empty, "PATH": empty},
            )
        self.assertEqual(completed.returncode, 0,
                         f"an absent opener must not fail: {completed.stderr}")
        self.assertIn("127.0.0.1:8740", completed.stdout,
                      "with no opener the owner must still be told the URL")


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


class StopAllRegexScopingTests(unittest.TestCase):
    def test_dotted_install_path_cannot_match_a_foreign_path(self):
        # Review finding: unescaped $root let /tmp/harness.next match
        # /tmp/harnessXnext. The escaped pattern must match only itself.
        import re
        import subprocess as sp
        script = (ROOT / "scripts" / "stop_all.sh").read_text(encoding="utf-8")
        escape_line = [l for l in script.splitlines() if l.startswith("root_re=")]
        self.assertTrue(escape_line, "stop_all.sh must escape root for the regex")
        probe = sp.run(
            ["bash", "-c",
             'root="/tmp/harness.next"; '
             + escape_line[0].replace('"$root"', '"$root"') + '; '
             + 'printf "%s" "$root_re"'],
            capture_output=True, text=True, timeout=10,
        )
        pattern = "python3.*" + probe.stdout + r"\/harness\/(project_manager|project_worker)\.py"
        self.assertTrue(re.search(pattern, "python3 /tmp/harness.next/harness/project_manager.py --home x"))
        self.assertFalse(re.search(pattern, "python3 /tmp/harnessXnext/harness/project_manager.py --home x"),
                         "foreign installation must never match")


class PrerequisiteAgreesWithTheAppTests(unittest.TestCase):
    """The check must find CLIs the way the APP finds them.

    Measured on a real Ubuntu box: Claude Code was installed at
    ~/.local/bin/claude, the app's discovery found it, and this check reported
    "Claude Code CLI not found" — because it used `command -v`, which respects
    the login PATH, and ~/.local/bin was not on it.

    A prerequisite check that disagrees with the program it checks for is worse
    than none: it sends the owner to reinstall something that already works.
    This is the same class as public issue #1, fixed in the app and never fixed
    here.
    """

    def test_the_check_resolves_CLIs_through_the_app_not_the_shell_PATH(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("global_settings.provider_executable", installer,
                      "the check must ask the app where a CLI is")
        self.assertNotIn("command -v codex", installer,
                         "PATH lookup misses ~/.local/bin under a minimal environment")
        self.assertNotIn("command -v claude", installer)

    def test_it_finds_a_cli_that_is_NOT_on_PATH(self):
        """The exact condition from the box: installed, executable, off PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            local = home / ".local" / "bin"; local.mkdir(parents=True)
            planted = local / "claude"
            planted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            planted.chmod(0o755)
            found = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]);"
                 "from harness import global_settings;"
                 "print(global_settings.provider_executable('claude'))",
                 str(ROOT)],
                capture_output=True, text=True,
                env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            )
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn(".local/bin/claude", found.stdout,
                      "a CLI off PATH must still be found, as the app finds it")

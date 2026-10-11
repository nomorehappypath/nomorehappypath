# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Antigravity CLI's state grant (spec M-5): provider-aware, narrow, and the others unchanged."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import agent_confinement, platform_support


class GrantListTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def grant(self, provider):
        return agent_confinement.writable_paths([str(self.home / "project")], home=self.home, provider=provider, tmpdir="")

    def test_gemini_gets_only_its_operational_folder_and_the_cache_never_its_controls(self):
        paths = self.grant("gemini")
        self.assertIn(str(self.home / ".gemini" / "antigravity-cli"), paths)
        self.assertIn(str(self.home / ".cache"), paths)
        self.assertNotIn(str(self.home / ".gemini"), paths)
        self.assertNotIn(str(self.home / ".gemini" / "config"), paths)
        self.assertNotIn(str(self.home / ".claude"), paths, "no Claude state for a Gemini session")

    def test_claude_and_codex_lists_are_byte_identical_to_before(self):
        implementation = platform_support.agent_confinement()
        expected = ([str(self.home / "project")] + implementation.cli_state_paths(self.home, None)
                    + implementation.temp_paths())
        self.assertEqual(self.grant("claude"), expected)
        self.assertEqual(self.grant("codex"), expected)
        self.assertEqual(agent_confinement.writable_paths([str(self.home / "project")], home=self.home, tmpdir=""), expected)

    def test_the_owners_settings_and_controls_are_protected_writes_for_gemini_only(self):
        protected = agent_confinement.gemini_protected_writes(self.home)
        self.assertIn(str(self.home / ".gemini" / "antigravity-cli" / "settings.json"), protected)
        self.assertIn(str(self.home / ".gemini" / "config"), protected)


class LinuxProtectedFileTests(unittest.TestCase):
    """Found by running on the real Linux machine: the bubblewrap builder made every protected path a FOLDER and crashed
    with FileExistsError as soon as `settings.json` (a file, present after the CLI's first run) was protected."""

    def command(self, home: Path) -> list[str]:
        from harness.platform_support import linux
        implementation = linux._BwrapAgentConfinement(which=lambda name: "/usr/bin/bwrap")
        project = home / "project"
        return agent_confinement.wrap(["agy"], [str(project)], store=home / "store", home=home,
                                      implementation=implementation, provider="gemini")

    def test_an_existing_settings_file_is_protected_as_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            state = home / ".gemini" / "antigravity-cli"
            state.mkdir(parents=True)
            (state / "settings.json").write_text('{"trustedWorkspaces": []}')
            command = self.command(home)
            settings = str(state / "settings.json")
            index = command.index(settings)
            self.assertEqual(command[index - 1], "--ro-bind")
            self.assertEqual(command[index + 1], settings, "bound read-only onto itself")
            self.assertTrue((state / "settings.json").is_file())
            self.assertEqual((state / "settings.json").read_text(), '{"trustedWorkspaces": []}', "the owner's file is not touched")
            config = str(home / ".gemini" / "config")
            self.assertEqual(command[command.index(config) - 1], "--ro-bind")

    def test_a_settings_file_not_created_yet_becomes_an_empty_json_file_never_a_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            command = self.command(home)
            settings = home / ".gemini" / "antigravity-cli" / "settings.json"
            self.assertTrue(settings.is_file())
            self.assertEqual(settings.read_text().strip(), "{}")
            self.assertIn(str(settings), command)


class KeychainProgramTests(unittest.TestCase):
    """Antigravity saves its sign-in by running /usr/bin/security; in the sandbox that hangs and macOS shows a dialog."""

    class Recorder:
        def __init__(self):
            self.kwargs = None

        def cli_state_paths(self, home, config_dir):
            return []

        def temp_paths(self):
            return []

        def wrap(self, argv, writable, **kwargs):
            self.kwargs = kwargs
            return list(argv)

    def wrapped(self, provider):
        recorder = self.Recorder()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"TMPDIR": ""}):
            agent_confinement.wrap(["true"], [str(Path(tmp) / "p")], store=Path(tmp) / "s", home=Path(tmp) / "h",
                                   implementation=recorder, provider=provider)
        return recorder.kwargs

    def test_only_a_gemini_session_is_refused_the_security_program(self):
        self.assertEqual(self.wrapped("gemini").get("denied_executables"), ["/usr/bin/security"])
        self.assertNotIn("denied_executables", self.wrapped("claude"))
        self.assertNotIn("denied_executables", self.wrapped("codex"))

    def test_the_macos_profile_only_changes_when_asked(self):
        from harness.platform_support import defaults
        implementation = defaults.AGENT_CONFINEMENT
        plain = implementation.profile(["/p"], ["/h/Library/Keychains"])
        denied = implementation.profile(["/p"], ["/h/Library/Keychains"], None, ["/usr/bin/security"])
        self.assertNotIn("process-exec", plain)
        self.assertEqual(denied.replace('(deny process-exec (literal "/usr/bin/security"))\n', ""), plain)

    def test_a_confined_gemini_session_cannot_start_the_security_program(self):
        implementation = platform_support.agent_confinement()
        if not implementation.available() or not Path("/usr/bin/security").exists():
            raise unittest.SkipTest("no macOS Keychain program on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / "home"
            home.mkdir()
            script = "/usr/bin/security help >/dev/null 2>&1; echo CODE=$?"
            command = agent_confinement.wrap(["/bin/sh", "-c", script], [str(Path(tmp).resolve() / "p")],
                                             store=Path(tmp) / "store", home=home, provider="gemini")
            done = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertIn("CODE=126", done.stdout.split(), f"{done.stdout}\n{done.stderr}")


class BoundaryByExecutionTests(unittest.TestCase):
    """The real sandbox of this machine, fake home: sign-in state writable, settings and controls not."""

    def setUp(self):
        if not platform_support.agent_confinement().available():
            raise unittest.SkipTest("no write-confinement primitive on this platform")

    def test_a_confined_gemini_session_writes_its_state_but_not_settings_or_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / "home"
            state, config = home / ".gemini" / "antigravity-cli", home / ".gemini" / "config"
            state.mkdir(parents=True)
            config.mkdir(parents=True)
            (state / "settings.json").write_text('{"owner": true}')
            project = Path(tmp).resolve() / "project"
            script = (
                f"( echo t > '{state}/antigravity-oauth-token' && echo STATE_OK ) 2>/dev/null || echo STATE_DENIED; "
                f"( echo x > '{state}/settings.json' && echo SETTINGS_WROTE ) 2>/dev/null || echo SETTINGS_DENIED; "
                f"( rm -f '{state}/settings.json' && echo SETTINGS_REMOVED ) 2>/dev/null || echo SETTINGS_KEPT; "
                f"( echo x > '{config}/hooks.json' && echo CONFIG_WROTE ) 2>/dev/null || echo CONFIG_DENIED; "
                f"( echo x > '{project}/ok.txt' && echo PROJECT_OK ) 2>/dev/null || echo PROJECT_DENIED"
            )
            command = agent_confinement.wrap(["/bin/sh", "-c", script], [str(project)], store=Path(tmp) / "store",
                                             home=home, provider="gemini")
            done = subprocess.run(command, capture_output=True, text=True, timeout=30)
            words = done.stdout.split()
            for expected in ("STATE_OK", "SETTINGS_DENIED", "SETTINGS_KEPT", "CONFIG_DENIED", "PROJECT_OK"):
                self.assertIn(expected, words, f"{done.stdout}\n{done.stderr}")
            self.assertEqual((state / "settings.json").read_text(), '{"owner": true}')
            self.assertFalse((config / "hooks.json").exists())


def tearDownModule():
    from tests.environment_support import assert_agy_pinned
    assert_agy_pinned()


if __name__ == "__main__":
    unittest.main()

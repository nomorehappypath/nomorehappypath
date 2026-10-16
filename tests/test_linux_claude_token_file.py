# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Linux Claude setup-token survives a reboot in an owner-only file; the Test button uses the agents' sign-in."""
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import claude_auth, global_settings
from harness.platform_support import linux


class LinuxTokenFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "cfg" / "claude-setup-token"
        patcher = mock.patch.object(linux, "claude_token_file", return_value=self.file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.credentials = linux.CLAUDE_CREDENTIALS
        claude_auth._cache.clear()

    def store(self, token="synthetic-setup-token"):
        self.credentials.store_token(token)

    def test_token_file_survives_a_manager_environment_without_the_token(self):
        # Simulated reboot: systemd's in-memory import is gone, the file is not.
        self.store()
        self.assertEqual(self.credentials.read_token({}), "synthetic-setup-token")
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.file.parent.stat().st_mode), 0o700)

    def test_explicit_environment_variable_wins_over_the_file(self):
        self.store()
        self.assertEqual(self.credentials.read_token({"CLAUDE_CODE_OAUTH_TOKEN": "from-env"}), "from-env")

    def test_unsafe_or_invalid_files_are_ignored(self):
        self.store()
        self.file.chmod(0o640)
        self.assertEqual(self.credentials.read_token({}), "")
        self.file.chmod(0o600)
        self.file.write_text("has a space\n")
        self.assertEqual(self.credentials.read_token({}), "")
        real = self.file.parent / "real"
        self.file.rename(real)
        self.file.symlink_to(real)
        self.assertEqual(self.credentials.read_token({}), "")
        self.assertIn("Save it again", self.credentials.read_file()[1])

    def test_missing_file_and_variable_raises_the_card(self):
        with mock.patch.object(claude_auth.platform_support, "claude_credentials", return_value=self.credentials):
            with self.assertRaises(claude_auth.ClaudeAuthRequired):
                claude_auth.resolve_token({})

    def test_store_token_command_hides_input_never_prints_it_and_refuses_junk(self):
        with mock.patch.object(claude_auth.platform_support, "claude_credentials", return_value=self.credentials), \
             mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("getpass.getpass", return_value="synthetic-setup-token") as prompt, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(claude_auth.store_token_command(), 0)
        prompt.assert_called_once()
        self.assertNotIn("synthetic-setup-token", out.getvalue())
        self.assertEqual(self.credentials.read_token({}), "synthetic-setup-token")
        with mock.patch.object(claude_auth.platform_support, "claude_credentials", return_value=self.credentials), \
             mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("getpass.getpass", return_value="  "), mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(claude_auth.store_token_command(), 1)

    def test_a_fifo_or_linked_folder_never_stalls_or_redirects(self):
        self.file.parent.mkdir(mode=0o700, parents=True)
        os.mkfifo(self.file)
        self.assertEqual(self.credentials.read_token({}), "")  # returns at once, no hang
        self.assertIn("plain file", self.credentials.read_file()[1])
        self.file.unlink()
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.file.parent.rmdir()
        self.file.parent.symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            self.credentials.store_token("synthetic-setup-token")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_protection_that_cannot_be_installed_refuses_instead_of_going_unprotected(self):
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            with self.assertRaises(linux.AgentConfinementUnavailable):
                linux._ensure_token_directory(str(self.file.parent))

    def test_resume_waits_as_long_as_the_sign_in_check_can_take(self):
        from harness import control, project_manager
        self.assertGreater(control.claude_launch_budget_seconds(), 2 * 45)
        manager = project_manager.ProjectManager.__new__(project_manager.ProjectManager)
        manager.terminal_launcher = None
        manager._worker_request = lambda *a, **k: object()
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["timeout"] = timeout
            return mock.MagicMock(__enter__=lambda s: mock.Mock(read=lambda: b'{"status":"launch_requested"}'), __exit__=lambda *a: False)
        with mock.patch.object(project_manager, "urlopen", fake_urlopen):
            manager._launch_resumed_terminal(None, {"id": "x"})
        self.assertGreater(seen["timeout"], 2 * 45, "a 6-second Claude check must not be reported as a failed resume")

    def test_store_token_refuses_a_non_terminal(self):
        with mock.patch.object(claude_auth.platform_support, "claude_credentials", return_value=self.credentials), \
             mock.patch("sys.stdin.isatty", return_value=False), mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(claude_auth.store_token_command(), 1)
        self.assertFalse(self.file.exists())

    def test_manager_marks_a_file_token_so_the_worker_rereads_it_after_replacement(self):
        # Manager copies the token into the worker; replacing the file must still reach the already-open worker.
        self.store("old-token")
        with mock.patch.object(claude_auth.platform_support, "claude_credentials", return_value=self.credentials):
            worker = claude_auth.manager_environment({"PATH": "/bin"})
            self.assertEqual(worker["HARNESS_CLAUDE_TOKEN_SOURCE"], "file")
            self.assertEqual(claude_auth.resolve_token(worker), "old-token")
            self.store("new-token")
            self.assertEqual(claude_auth.resolve_token(worker), "new-token")
            # An explicit override stays an override.
            explicit = claude_auth.manager_environment({"CLAUDE_CODE_OAUTH_TOKEN": "explicit"})
            self.assertEqual(claude_auth.resolve_token(explicit), "explicit")

    def test_reviewer_token_fallback_still_works_without_a_file(self):
        with mock.patch.object(claude_auth.platform_support, "claude_credentials", return_value=self.credentials):
            self.assertEqual(claude_auth.resolve_token({claude_auth.REVIEWER_TOKEN_ENV: "reviewer-token"}), "reviewer-token")

    def test_agents_cannot_read_the_saved_token_even_one_saved_after_they_start(self):
        import shutil
        import subprocess
        bubblewrap = linux._BwrapAgentConfinement()
        if not bubblewrap.available():
            self.skipTest("bubblewrap is not installed on this machine")
        protected = bubblewrap.protected_read_paths(Path(self.tmp.name))
        self.assertIn(str(self.file.parent), protected)
        command = bubblewrap.wrap(["/bin/sh", "-c", "sleep 2; cat " + str(self.file)], [self.tmp.name],
                                  store=Path(self.tmp.name) / "store", protected_reads=protected)
        agent = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.store("late-token")  # saved after the agent started
        out, _ = agent.communicate(timeout=30)
        self.assertNotIn("late-token", out)

    def test_card_says_what_survives_a_reboot(self):
        text = self.credentials.instructions()
        self.assertIn("store-token", text)
        self.assertIn("Survives a reboot", text)
        self.assertIn("Does not survive a reboot", text)


class TestButtonUsesAgentSignInTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        cli = base / "claude"
        cli.write_text("#!/bin/sh\necho ok\n")
        cli.chmod(0o755)
        self.cli, self.home = cli, base / "home"
        (base / "ws").mkdir()
        self.ws = base / "ws"

    def run_test(self):
        with mock.patch.dict(os.environ, {"HARNESS_CLAUDE_BIN": str(self.cli)}):
            return global_settings.test_connection(self.home, "claude", "haiku", "low", self.ws)

    def test_no_agent_sign_in_is_not_green(self):
        with mock.patch.object(claude_auth, "resolve_token", side_effect=claude_auth.ClaudeAuthRequired()), \
             mock.patch.object(global_settings.subprocess, "run") as run:
            with self.assertRaises(ValueError) as raised:
                self.run_test()
        run.assert_not_called()
        self.assertIn("Claude agents cannot start", str(raised.exception))
        self.assertIn("setup-token", str(raised.exception))

    def test_rejected_token_is_not_green(self):
        with mock.patch.object(claude_auth, "resolve_token", return_value="tok"), \
             mock.patch.object(claude_auth, "validate_token", side_effect=claude_auth.ClaudeAuthRequired("rejected")):
            with self.assertRaises(ValueError):
                self.run_test()

    def test_green_runs_with_the_agents_token_and_no_other_auth(self):
        seen = {}

        def fake_run(command, **kwargs):
            seen.update(kwargs["env"])
            return mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(claude_auth, "resolve_token", return_value="tok"), \
             mock.patch.object(claude_auth, "validate_token"), \
             mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(global_settings.subprocess, "run", side_effect=fake_run):
            self.assertTrue(self.run_test()["ok"])
        self.assertEqual(global_settings.load(self.home)["connectivity"]["claude"]["method"], "agent-token")
        self.assertEqual(seen["CLAUDE_CODE_OAUTH_TOKEN"], "tok")
        self.assertNotIn("ANTHROPIC_API_KEY", seen)


if __name__ == "__main__":
    unittest.main()

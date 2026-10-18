# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Clean-install findings (2026-10-17): `install.sh --check` names what a FULL run still needs; a token typed as an argument is never echoed."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import claude_auth, global_settings, preflight

ROOT = Path(__file__).resolve().parents[1]
SECRET = "sk-ant-" + "oat01-" + "SECRETVALUEDONOTPRINT"   # built at run time: the release gate refuses secret-shaped literals


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_an_unaccepted_bypass_warning_is_named_with_the_one_time_command(self):
        problem = preflight.claude_bypass_consent_problem(True, self.home, {})
        self.assertIn("claude --dangerously-skip-permissions", problem)
        self.assertIn("Yes, I accept", problem)

    def test_an_accepted_warning_passes_in_any_place_claude_records_it(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"skipDangerousModePermissionPrompt": True}), encoding="utf-8")
        self.assertEqual(preflight.claude_bypass_consent_problem(True, self.home, {}), "")

    def test_the_older_state_file_and_a_custom_config_dir_count(self):
        (self.home / ".claude.json").write_text(json.dumps({"bypassPermissionsModeAccepted": True}), encoding="utf-8")
        self.assertEqual(preflight.claude_bypass_consent_problem(True, self.home, {}), "")
        custom = self.home / "elsewhere"
        custom.mkdir()
        (custom / "settings.json").write_text(json.dumps({"skipDangerousModePermissionPrompt": True}), encoding="utf-8")
        other = self.home / "other-home"
        other.mkdir()
        self.assertEqual(preflight.claude_bypass_consent_problem(True, other, {"CLAUDE_CONFIG_DIR": str(custom)}), "")

    def test_a_false_value_or_unreadable_file_is_not_acceptance_and_no_claude_means_no_message(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
        self.assertNotEqual(preflight.claude_bypass_consent_problem(True, self.home, {}), "")
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"skipDangerousModePermissionPrompt": False}), encoding="utf-8")
        self.assertNotEqual(preflight.claude_bypass_consent_problem(True, self.home, {}), "")
        self.assertEqual(preflight.claude_bypass_consent_problem(False, self.home, {}), "")


class EffectiveConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def test_acceptance_in_the_home_folder_does_not_count_when_another_configuration_is_selected(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"skipDangerousModePermissionPrompt": True}), encoding="utf-8")
        elsewhere = Path(self.tmp.name) / "selected"
        elsewhere.mkdir()
        problem = preflight.claude_bypass_consent_problem(True, self.home, {"CLAUDE_CONFIG_DIR": str(elsewhere)}, "/opt/claude/bin/claude")
        self.assertIn(f"CLAUDE_CONFIG_DIR={elsewhere} /opt/claude/bin/claude --dangerously-skip-permissions", problem)

    def test_the_command_names_the_selected_executable(self):
        problem = preflight.claude_bypass_consent_problem(True, self.home, {}, "/home/u/.local/bin/claude")
        self.assertIn("`/home/u/.local/bin/claude --dangerously-skip-permissions`", problem)

    def test_a_settings_file_that_cannot_be_read_is_reported_as_that_not_as_a_missing_acceptance(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text("{broken", encoding="utf-8")
        problem = preflight.claude_bypass_consent_problem(True, self.home, {})
        self.assertIn("cannot be read", problem)
        self.assertIn("settings.json", problem)

    def test_the_launch_gate_raises_a_consent_card_with_the_command_and_no_token_advice(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False), \
                mock.patch("harness.global_settings.provider_executable", return_value="/x/claude"):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            with mock.patch("pathlib.Path.home", return_value=self.home):
                with self.assertRaises(claude_auth.ClaudeAuthRequired) as caught:
                    claude_auth._require_bypass_consent()
        card = claude_auth.action(caught.exception.reason, caught.exception.detail)
        self.assertEqual(caught.exception.reason, "consent")
        self.assertIn("/x/claude --dangerously-skip-permissions", card["why"])
        self.assertIn("No new token is needed", card["instructions"])

    def test_a_relative_configuration_path_is_refused_with_a_plain_reason(self):
        problem = preflight.claude_bypass_consent_problem(True, self.home, {"CLAUDE_CONFIG_DIR": "relative/dir"})
        self.assertIn("relative path", problem)
        self.assertIn("absolute path", problem)

    def test_the_repair_text_protects_existing_settings_and_says_the_session_is_not_confined(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text("{broken", encoding="utf-8")
        self.assertIn("do not delete other permissions", preflight.claude_bypass_consent_problem(True, self.home, {}))
        self.assertIn("not confined like the agents", preflight.claude_bypass_consent_problem(True, Path(self.tmp.name) / "empty", {}))

    def test_the_shipped_defaults_are_reported_without_changing_them(self):
        text = preflight.role_defaults_text()
        self.assertIn("Reviewer", text)
        self.assertIn("opus (max)", text)


class BrowserTests(unittest.TestCase):
    def missing(self):
        raise FileNotFoundError("none")

    def test_linux_without_a_native_browser_gets_the_install_line_for_its_architecture(self):
        arm = preflight.native_browser_problem("Linux", "aarch64", self.missing)
        self.assertIn("google-chrome-stable_current_arm64.deb", arm)
        self.assertIn("sudo apt install", arm)
        self.assertIn("HARNESS_BROWSER_BIN", arm)
        self.assertIn("google-chrome-stable_current_amd64.deb", preflight.native_browser_problem("Linux", "x86_64", self.missing))

    def test_a_found_browser_or_another_platform_says_nothing(self):
        self.assertEqual(preflight.native_browser_problem("Linux", "x86_64", lambda: "/usr/bin/google-chrome"), "")
        self.assertEqual(preflight.native_browser_problem("Darwin", "arm64", lambda: "/opt/chrome-headless-shell"), "")

    def test_macos_without_a_headless_browser_gets_accurate_wording_and_other_work_is_not_blocked(self):
        text = preflight.native_browser_problem("Darwin", "arm64", self.missing)
        self.assertIn("rendered-page proof", text)
        self.assertIn("other work runs without it", text)
        self.assertNotIn("sudo apt", text)

    def test_an_unknown_processor_gets_no_invented_download_and_the_exact_export_is_named_after_install(self):
        unknown = preflight.native_browser_problem("Linux", "riscv64", self.missing)
        self.assertNotIn(".deb", unknown)
        self.assertIn("HARNESS_BROWSER_BIN", unknown)
        known = preflight.native_browser_problem("Linux", "x86_64", self.missing)
        self.assertIn("HARNESS_BROWSER_BIN=/usr/bin/google-chrome", known)
        self.assertIn("service", known)

    def test_a_configured_but_unusable_browser_is_reported(self):
        def bad():
            raise ValueError("not executable")
        self.assertIn("HARNESS_BROWSER_BIN", preflight.native_browser_problem("Linux", "x86_64", bad))


class TokenArgumentTests(unittest.TestCase):
    def test_a_token_after_store_token_is_refused_without_being_printed(self):
        message = claude_auth.token_argument_problem(["store-token", SECRET])
        self.assertIn("looks like a token", message)
        self.assertIn("python3 -m harness.claude_auth store-token", message)
        self.assertNotIn(SECRET, message)

    def test_any_other_argument_and_a_bare_token_are_refused_and_plain_store_token_is_not(self):
        self.assertIn("takes no arguments", claude_auth.token_argument_problem(["store-token", "--help"]))
        self.assertIn("looks like a token", claude_auth.token_argument_problem([SECRET]))
        self.assertEqual(claude_auth.token_argument_problem(["store-token"]), "")
        self.assertEqual(claude_auth.token_argument_problem([]), "")

    def test_the_real_command_exits_two_and_never_prints_the_value(self):
        completed = subprocess.run(
            [sys.executable, "-m", "harness.claude_auth", "store-token", SECRET],
            cwd=ROOT, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn(SECRET, completed.stdout + completed.stderr)
        self.assertIn("hidden prompt", completed.stderr)


class TestButtonTests(unittest.TestCase):
    def run_button(self, consent_text):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake = base / "claude"
            fake.write_text("#!/bin/sh\necho OK\n", encoding="utf-8")
            fake.chmod(0o755)
            workspace = base / "workspace"
            workspace.mkdir()
            with mock.patch.dict(os.environ, {"HARNESS_CLAUDE_BIN": str(fake)}), \
                    mock.patch("harness.claude_auth.resolve_token", return_value="tok"), \
                    mock.patch("harness.claude_auth.validate_token"), \
                    mock.patch("harness.preflight.claude_bypass_consent_problem", return_value=consent_text):
                return global_settings.test_connection(base / "home", "claude", "haiku", "low", workspace)

    def test_a_green_claude_test_still_says_when_the_bypass_warning_is_not_accepted(self):
        result = self.run_button("CONSENT TEXT")
        self.assertTrue(result["ok"])
        self.assertTrue(result["bypass_consent_missing"])
        self.assertIn("BUT: CONSENT TEXT", result["message"])

    def test_an_accepted_warning_leaves_the_green_result_untouched(self):
        result = self.run_button("")
        self.assertNotIn("bypass_consent_missing", result)
        self.assertNotIn("BUT:", result["message"])


class InstallCheckOutputTests(unittest.TestCase):
    def test_check_names_the_full_run_gaps_with_a_fake_home_and_a_fake_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".local" / "bin").mkdir(parents=True)
            fake = home / ".local" / "bin" / "claude"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            environment = {
                **os.environ, "HOME": str(home), "HARNESS_CLAUDE_BIN": str(fake),
                "HARNESS_BROWSER_BIN": "", "CHROME_BIN": "",
                # Nothing on this machine's PATH (nvm, a test toolchain) may supply a browser or a CLI.
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            }
            environment.pop("CLAUDE_CONFIG_DIR", None)
            completed = subprocess.run(["bash", str(ROOT / "install.sh"), "--check"], cwd=ROOT, capture_output=True, text=True, env=environment)
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("FULL run", output)
        self.assertIn("claude --dangerously-skip-permissions", output)
        self.assertIn("Open a NEW terminal", output)
        if sys.platform.startswith("linux"):
            self.assertIn("HARNESS_BROWSER_BIN", output)


if __name__ == "__main__":
    unittest.main()

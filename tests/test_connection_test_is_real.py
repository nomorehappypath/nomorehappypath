# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Test connection must run the model, not merely parse its flags.

Field defect: `<cli> --model X --help` exits 0 for ANY model name, so an older
Codex CLI passed the Settings test and then failed every real task with
HTTP 400 "requires a newer version of Codex". These tests pin the real
round-trip and the plain-language diagnosis.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import global_settings


def fake_cli(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


class RealConnectionTestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.home = self.base / "home"
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()

    def run_test(self, body: str, provider: str = "codex", model: str = "gpt-5.6-terra"):
        fake_cli(self.bin, provider, body)
        env = {control_env: str(self.bin / provider)}
        with mock.patch.dict(os.environ, env):
            return global_settings.test_connection(
                self.home, provider, model, "high", self.workspace,
            )

    def test_a_model_the_cli_cannot_run_fails_with_plain_guidance(self):
        body = (
            'cat <<EOF\n'
            '{"type":"error","status":400,"error":{"message":'
            '"The gpt-5.6-sol model requires a newer version of Codex."}}\n'
            'EOF\n'
            "exit 0\n"   # the CLI exits 0 - only the payload reveals the failure
        )
        with self.assertRaises(ValueError) as raised:
            self.run_test(body, model="gpt-5.6-sol")
        message = str(raised.exception)
        self.assertIn("could not run gpt-5.6-sol", message)
        self.assertIn("older than the model", message)
        self.assertIn("choose a different", message)

    def test_a_working_model_passes_and_says_it_was_a_real_billed_request(self):
        result = self.run_test("echo ready\nexit 0\n")
        self.assertTrue(result["ok"])
        self.assertIn("real request on your own account", result["message"])

    def test_the_probe_is_a_real_run_not_a_help_flag(self):
        recorder = self.base / "argv.txt"
        body = f'printf "%s\\n" "$@" > {recorder}\necho ready\nexit 0\n'
        self.run_test(body)
        argv = recorder.read_text(encoding="utf-8")
        self.assertIn("exec", argv, "codex must be invoked in exec mode")
        self.assertNotIn("--help", argv, "a --help probe proves nothing about the model")
        self.assertIn(global_settings.CONNECTION_TEST_PROMPT.split()[0], argv)

    def test_a_nonzero_exit_still_fails(self):
        with self.assertRaises(ValueError):
            self.run_test("echo 'boom' >&2\nexit 3\n")


control_env = "HARNESS_CODEX_BIN"


if __name__ == "__main__":
    unittest.main()


class CreditExhaustionTests(RealConnectionTestTests):
    def test_out_of_credit_names_the_account_not_the_app(self):
        body = (
            'cat <<EOF\n'
            '{"type":"error","error":{"type":"insufficient_quota","message":'
            '"You exceeded your current quota, please check your plan and billing"}}\n'
            'EOF\n'
            "exit 1\n"
        )
        with self.assertRaises(ValueError) as raised:
            self.run_test(body)
        message = str(raised.exception)
        self.assertIn("out of credit or over its usage limit", message)
        self.assertIn("OpenAI", message)
        self.assertIn("The app itself is fine", message)

    def test_anthropic_credit_error_names_anthropic(self):
        body = 'echo "Your credit balance is too low to access the Anthropic API" >&2\nexit 1\n'
        fake_cli(self.bin, "claude", body)
        with mock.patch.dict(os.environ, {"HARNESS_CLAUDE_BIN": str(self.bin / "claude")}):
            with self.assertRaises(ValueError) as raised:
                global_settings.test_connection(
                    self.home, "claude", "opus", "high", self.workspace,
                )
        self.assertIn("Anthropic", str(raised.exception))
        self.assertIn("out of credit", str(raised.exception))


class SignalFalsePositiveTests(RealConnectionTestTests):
    def test_a_clean_answer_mentioning_quota_words_is_not_a_failure(self):
        # Review finding: exit-0 output CONTAINING a signal phrase was
        # misclassified. Signals count only inside an error context.
        body = 'echo "ready - note: docs mention insufficient_quota handling"\nexit 0\n'
        result = self.run_test(body)
        self.assertTrue(result["ok"])

    def test_an_exit_zero_error_envelope_still_fails(self):
        body = (
            'cat <<EOF\n'
            '{"type":"error","error":{"type":"insufficient_quota","message":"exceeded your current quota"}}\n'
            'EOF\n'
            "exit 0\n"
        )
        with self.assertRaises(ValueError) as raised:
            self.run_test(body)
        self.assertIn("out of credit", str(raised.exception))

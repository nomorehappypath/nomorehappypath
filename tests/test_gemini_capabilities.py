# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Gemini (Antigravity CLI), T0: the executable is `agy`, and the static probe reads its `--help`.

Spec board/tasks/2026-10-07_gemini-provider_SPEC.md M-1, M-9, §5 T0. These tests
use a stub `agy`; the real CLI is never started by the suite.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import cli_capabilities, control, global_settings

FAKE_AGY = r'''#!/bin/sh
echo "HOME=$HOME $@" >> "{calls}"
case "$1" in
  --version) echo "1.3.1" ;;
  --help) printf 'Usage of agy:\n{flags}\nAvailable subcommands:\n  mcp             Manage MCP servers\n  models          List available models\n' ;;
esac
'''
ALL_FLAGS = ("  --add-dir  Add a directory\n  --conversation  Resume by ID\n  --dangerously-skip-permissions  Auto-approve\n"
             "  --effort  Reasoning effort\n  --model  Model\n  --output-format  json|stream-json\n  -p  Short alias for --print\n")


def write_agy(path: Path, flags: str = ALL_FLAGS) -> Path:
    path.write_text(FAKE_AGY.format(calls=str(path) + ".calls", flags=flags), encoding="utf-8")
    path.chmod(0o755)
    return path


class ExecutableDiscoveryTests(unittest.TestCase):
    """The provider id is `gemini`; the program is `agy` (spec C-10)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_the_provider_runs_agy_not_gemini(self):
        self.assertEqual(control.provider_executable_name("gemini"), "agy")
        self.assertEqual(control.provider_executable_name("claude"), "claude")
        self.assertEqual(control.PROVIDERS["gemini"]["binary_env"], "HARNESS_AGY_BIN")

    def test_found_on_the_path_as_agy_and_a_binary_named_gemini_is_never_used(self):
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        agy = write_agy(bin_dir / "agy")
        write_agy(bin_dir / "gemini")        # the excluded open-source CLI
        # Only the fake folders: the machine's own agy (which self-updates) must not take part.
        with mock.patch.object(global_settings, "PROVIDER_SEARCH_DIRECTORIES", ()):
            found = global_settings.provider_executable("gemini", source_environment={"PATH": str(bin_dir)})
        self.assertEqual(found, str(agy))

    def test_unset_and_absent_resolve_to_nothing(self):
        empty = self.base / "empty"
        empty.mkdir()
        write_agy(empty / "gemini")
        with mock.patch.object(global_settings, "PROVIDER_SEARCH_DIRECTORIES", ()):
            self.assertEqual(global_settings.provider_executable("gemini", source_environment={"PATH": str(empty)}), "")

    def test_an_explicit_absolute_path_wins_and_must_exist(self):
        agy = write_agy(self.base / "my-agy")
        env = {"HARNESS_AGY_BIN": str(agy), "PATH": ""}
        self.assertEqual(global_settings.provider_executable("gemini", source_environment=env), str(agy))
        env["HARNESS_AGY_BIN"] = str(self.base / "missing")
        self.assertEqual(global_settings.provider_executable("gemini", source_environment=env), "")

    def test_two_installed_copies_the_newest_version_wins(self):
        old, new = self.base / "old", self.base / "new"
        old.mkdir(), new.mkdir()
        for directory, version in ((old, "1.2.9"), (new, "1.3.1")):
            script = write_agy(directory / "agy")
            script.write_text(script.read_text().replace('echo "1.3.1"', f'echo "{version}"'))
        env = {"PATH": f"{old}{os.pathsep}{new}"}
        with mock.patch.object(global_settings, "PROVIDER_SEARCH_DIRECTORIES", ()):
            self.assertEqual(global_settings.provider_executable("gemini", source_environment=env), str(new / "agy"))


class GeminiModelRuleTests(unittest.TestCase):
    def test_a_gemini_session_runs_only_google_models(self):
        self.assertEqual(control.normalize_provider_model("gemini", "gemini-3.8-flash-low"), "gemini-3.8-flash-low")
        self.assertEqual(control.normalize_provider_model("gemini", ""), control.PROVIDER_DEFAULT_MODELS["gemini"])
        for foreign in ("claude-opus-4-6-thinking", "gpt-oss-120b-medium", "opus"):
            with self.assertRaisesRegex(ValueError, "only Google's models"):
                control.normalize_provider_model("gemini", foreign)

    def test_the_other_providers_are_unchanged(self):
        self.assertEqual(control.normalize_provider_model("claude", "opus"), "opus")
        self.assertEqual(control.normalize_provider_model("codex", "gpt-5.6-sol"), "gpt-5.6-sol")

    def test_every_provider_has_every_table(self):
        for provider in control.PROVIDERS:
            self.assertIn(provider, control.PROVIDER_EFFORTS)
            self.assertIn(provider, control.PROVIDER_MODELS)
            self.assertIn(provider, control.PROVIDER_MODEL_DESCRIPTIONS)
            self.assertIn(provider, control.PROVIDER_DEFAULT_MODELS)
            self.assertTrue(control.PROVIDERS[provider]["vendor"])


class GeminiProbeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def probe(self, agy: Path) -> dict:
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)}):
            return cli_capabilities.capabilities(self.base / "home", "gemini")

    def test_the_static_items_come_from_the_binarys_help_and_are_cached(self):
        agy = write_agy(self.base / "agy")
        record = self.probe(agy)
        self.assertEqual(set(record["static"]), set(cli_capabilities.STATIC_ITEMS["gemini"]))
        self.assertTrue(all(record["static"].values()), record["static"])
        self.assertEqual(record["identity"]["version"], "1.3.1")
        calls = Path(str(agy) + ".calls").read_text().count("\n")
        self.probe(agy)
        self.assertEqual(Path(str(agy) + ".calls").read_text().count("\n"), calls, "second read comes from the cache")

    def test_the_probe_never_touches_the_owners_home(self):
        agy = write_agy(self.base / "agy")
        self.probe(agy)
        lines = Path(str(agy) + ".calls").read_text().splitlines()
        help_calls = [line for line in lines if line.endswith("--help")]
        self.assertEqual(len(help_calls), 1)
        self.assertNotIn(f"HOME={Path.home()} ", help_calls[0], "the CLI keeps its state in $HOME/.gemini: scratch home only")
        text = "\n".join(lines)
        self.assertNotIn(" -p ", text, "the static probe never makes a model turn")

    def test_a_missing_flag_is_false_and_unproven_live_items_gate_like_false(self):
        agy = write_agy(self.base / "agy", flags="  --model  Model\n")
        agy.write_text(agy.read_text().replace('echo "1.3.1"', 'echo "2.0.0"'))      # no development proof for 2.x
        record = self.probe(agy)
        self.assertFalse(record["static"]["gemini.add_dir"])
        self.assertTrue(record["static"]["gemini.model_flag"])
        self.assertFalse(cli_capabilities.capability(record, "gemini.mcp_reaches_board"), "UNPROVEN gates like false")

    def test_the_probe_ignores_an_api_key_in_the_environment(self):
        agy = write_agy(self.base / "agy")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "must-not-reach-the-probe"}):
            self.probe(agy)
        self.assertNotIn("must-not-reach", Path(str(agy) + ".calls").read_text())

    def test_stage_requirements_name_gemini_so_a_missing_capability_is_a_logged_fallback(self):
        for stage in ("stage1_hooks", "stage2_board_mcp", "stage4_system_layer_directives"):
            self.assertIn("gemini", cli_capabilities.STAGE_REQUIREMENTS[stage], stage)
        record = {"identity": {}, "static": {name: True for name in cli_capabilities.STATIC_ITEMS["gemini"]}, "live": {}}
        flags = global_settings.default_plumbing()
        flags["stage1_hooks_enabled"] = True
        status = cli_capabilities.stage_status(Path(self.base), "stage1_hooks", "gemini",
                                               settings={**global_settings._default(), "plumbing": flags},
                                               records={"gemini": record})
        self.assertEqual(status["reason"], "PLUMBING FALLBACK stage1_hooks gemini.session_scoped_controls=UNPROVEN")

    def test_a_live_proof_is_recorded_with_its_evidence_and_carried_within_a_major_version(self):
        agy = write_agy(self.base / "agy")
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)}):
            cli_capabilities.record_live_item(self.base / "home", "gemini", "gemini.session_scoped_controls", True,
                                              "hook fired from an --add-dir runtime directory (macOS and Linux)")
            record = cli_capabilities.capabilities(self.base / "home", "gemini")
        self.assertTrue(cli_capabilities.capability(record, "gemini.session_scoped_controls"))
        with self.assertRaises(ValueError):
            cli_capabilities.record_live_item(self.base / "home", "gemini", "gemini.busy_delivery_receipt", True, "")


class AvailabilityAndBaselineTests(unittest.TestCase):
    """Settings offers Gemini only where it is installed and proven for this version (spec C-39)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def availability(self, agy: Path | None) -> dict:
        path = str(agy) if agy else str(self.base / "absent")
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": path}):
            return cli_capabilities.provider_availability(self.base / "home", "gemini")

    def make(self, version: str, flags: str = ALL_FLAGS) -> Path:
        agy = write_agy(self.base / "agy", flags=flags)
        agy.write_text(agy.read_text().replace('echo "1.3.1"', f'echo "{version}"'))
        return agy

    def test_claude_and_codex_are_always_offered(self):
        for provider in ("claude", "codex"):
            offered = cli_capabilities.provider_availability(self.base, provider)
            self.assertEqual((offered["available"], offered["reason"]), (True, ""))
            self.assertEqual(set(offered), {"available", "reason", "executable"})     # the resolved CLI, for the history comparison

    def test_not_installed_is_listed_disabled_with_the_reason(self):
        result = self.availability(None)
        self.assertFalse(result["available"])
        self.assertTrue(result["reason"].startswith("Gemini (Antigravity CLI) — not yet available on this machine: "))
        self.assertIn("not installed", result["reason"])

    def test_the_proven_version_and_its_later_minor_versions_are_offered(self):
        self.assertTrue(self.availability(self.make("1.3.1"))["available"])
        self.assertTrue(self.availability(self.make("1.4.0"))["available"])

    def test_an_unproven_major_version_or_an_older_one_is_not_offered_and_says_why(self):
        for version in ("2.0.0", "1.2.9"):
            result = self.availability(self.make(version))
            self.assertFalse(result["available"], version)
            self.assertIn("has not been proven", result["reason"])

    def test_a_version_missing_a_flag_the_harness_needs_is_not_offered(self):
        result = self.availability(self.make("1.3.1", flags="  --model  Model\n"))
        self.assertFalse(result["available"])
        self.assertIn("gemini.add_dir", result["reason"])

    def test_the_baseline_is_carried_with_its_origin_and_an_explicit_false_wins(self):
        agy = self.make("1.3.1")
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)}):
            record = cli_capabilities.capabilities(self.base / "home", "gemini")
            self.assertIn("development proof", record["carried"]["gemini.busy_delivery_receipt"])
            cli_capabilities.record_live_item(self.base / "home", "gemini", "gemini.busy_delivery_receipt", False, "broke on this machine")
            record = cli_capabilities.capabilities(self.base / "home", "gemini")
        self.assertFalse(cli_capabilities.capability(record, "gemini.busy_delivery_receipt"))

    def test_settings_refuse_a_gemini_role_while_it_is_unavailable(self):
        from harness import global_settings
        home = self.base / "settings-home"
        entry = {"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "high"}
        chosen = {"delivery": entry, "cto": {"provider": "claude", "model": "opus", "effort": "high"},
                  "reviewer": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"}}
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(self.base / "absent")}):
            with self.assertRaisesRegex(ValueError, "not yet available on this machine"):
                global_settings.update_agent_settings(home, chosen)
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(self.make("1.3.1"))}):
            saved = global_settings.update_agent_settings(home, chosen)
        self.assertEqual(saved["agent_settings"]["delivery"]["provider"], "gemini")


def tearDownModule():
    from tests.environment_support import assert_agy_pinned
    assert_agy_pinned()


if __name__ == "__main__":
    unittest.main()

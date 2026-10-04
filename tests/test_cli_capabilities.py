# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Stage 0 of the plumbing program: the capability gate and the stage switches.

A plumbing stage runs for a session only when its switch is on, every stage it
depends on is on, and every capability it needs is PROVEN for the installed
CLI. UNPROVEN gates exactly like false. With every switch off - the default -
nothing changes.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import cli_capabilities, global_settings

FAKE_CODEX = r'''#!/bin/sh
echo "$@" >> "{calls}"
case "$1" in
  --help) printf 'Commands:\n  exec    Run\n  mcp     Manage\n  app-server  Run\nOptions:\n  {remote}\n  --dangerously-bypass-hook-trust\n' ;;
  --version) echo "codex-cli 9.9.9" ;;
  features) printf 'hooks                                    stable             {hooks}\n' ;;
  app-server)
    out="$4"; mkdir -p "$out"
    printf '%s' '{{"oneOf":[{{"properties":{{"method":{{"enum":["initialize"]}}}}}},{{"properties":{{"method":{{"enum":["thread/start"]}}}}}},{{"properties":{{"method":{{"enum":["turn/start"]}}}}}}]}}' > "$out/ClientRequest.json"
    printf '%s' '{{"definitions":{{"TurnStartParams":{{"properties":{{"clientUserMessageId":{{}}}}}},"ThreadStartParams":{{"properties":{{"developerInstructions":{{}}}}}},"ThreadItem":{{"oneOf":[{{"properties":{{"type":{{"enum":["userMessage"]}},"clientId":{{}}}}}}]}},"SandboxPolicy":{{"oneOf":[{{"properties":{{"writableRoots":{{}},"excludeSlashTmp":{{}},"excludeTmpdirEnvVar":{{}}}}}}]}}}}}}' > "$out/codex_app_server_protocol.v2.schemas.json" ;;
  sandbox) exit 0 ;;
esac
'''


def write_fake_codex(path: Path, *, remote: bool = True, hooks: bool = True) -> Path:
    path.write_text(FAKE_CODEX.format(calls=str(path) + ".calls", remote="--remote <ADDR>" if remote else "",
                                      hooks="true" if hooks else "false"), encoding="utf-8")
    path.chmod(0o755)
    return path


def all_on(*stages: str) -> dict:
    plumbing = global_settings.default_plumbing()
    for stage in stages:
        plumbing[f"{stage}_enabled"] = True
    return {**global_settings._default(), "plumbing": plumbing}


class SettingsTests(unittest.TestCase):
    def test_every_stage_is_off_by_default_and_an_old_document_loads_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "settings.json").write_text(json.dumps({"version": global_settings.SETTINGS_VERSION,
                                                            "agent_settings": None, "connectivity": {}}), encoding="utf-8")
            value = global_settings.load(home)
        self.assertEqual(value["plumbing"], global_settings.default_plumbing())
        self.assertFalse(any(value["plumbing"][f"{stage}_enabled"] for stage in global_settings.PLUMBING_STAGES))
        status = global_settings.plumbing_status(home, settings=value)
        self.assertFalse(any(item["enabled"] for item in status.values()))

    def test_invalid_plumbing_settings_are_refused_with_the_reason(self):
        for bad, message in (({"stage9_enabled": True}, "unknown plumbing settings"),
                             ({"stage1_hooks_enabled": "yes"}, "true or false"),
                             ({"hook_gate_timeout_seconds": 0}, "whole number from 1 to 60"),
                             ({"delivery_receipt_timeout_seconds": True}, "whole number")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, message):
                global_settings._validated_plumbing(bad)

    def test_a_stage_whose_dependency_is_off_is_refused_and_behaves_as_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = global_settings.plumbing_status(Path(tmp), settings=all_on("stage3_codex_app_server"))
            self.assertFalse(status["stage3_codex_app_server"]["enabled"])
            self.assertIn("needs stage1_hooks switched on first", status["stage3_codex_app_server"]["reason"])
            status = global_settings.plumbing_status(Path(tmp), settings=all_on("stage3_codex_app_server", "stage1_hooks"))
            self.assertTrue(status["stage3_codex_app_server"]["enabled"])


class StageGateTests(unittest.TestCase):
    def record(self, static: dict, live: dict) -> dict:
        return {"identity": {}, "static": static, "live": live}

    def test_unproven_gates_exactly_like_false_and_says_which(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = all_on("stage1_hooks")
            records = {"codex": self.record({"codex.hooks_session_flags": True, "codex.bypass_hook_trust_flag": True},
                                            {"codex.hooks_loaded_once": cli_capabilities.UNPROVEN})}
            status = cli_capabilities.stage_status(Path(tmp), "stage1_hooks", "codex", settings=settings, records=records)
            self.assertFalse(status["enabled"])
            self.assertEqual(status["reason"], "PLUMBING FALLBACK stage1_hooks codex.hooks_loaded_once=UNPROVEN")
            records["codex"]["live"]["codex.hooks_loaded_once"] = False
            status = cli_capabilities.stage_status(Path(tmp), "stage1_hooks", "codex", settings=settings, records=records)
            self.assertIn("codex.hooks_loaded_once=false", status["reason"])
            records["codex"]["live"]["codex.hooks_loaded_once"] = True
            self.assertTrue(cli_capabilities.stage_status(Path(tmp), "stage1_hooks", "codex",
                                                          settings=settings, records=records)["enabled"])

    def test_a_static_capability_missing_from_the_binary_is_false_not_unproven(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = {"claude": self.record({"claude.settings_flag": True, "claude.inline_settings_hooks": False}, {})}
            status = cli_capabilities.stage_status(Path(tmp), "stage1_hooks", "claude",
                                                   settings=all_on("stage1_hooks"), records=records)
            self.assertEqual(status["reason"], "PLUMBING FALLBACK stage1_hooks claude.inline_settings_hooks=false")

    def test_a_stage_half_for_the_other_vendor_does_not_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = cli_capabilities.stage_status(
                Path(tmp), "stage3_codex_app_server", "claude",
                settings=all_on("stage1_hooks", "stage3_codex_app_server"), records={"claude": self.record({}, {})})
            self.assertFalse(status["enabled"])
            self.assertIn("does not apply to claude", status["reason"])

    def test_a_switched_off_stage_never_consults_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cli_capabilities, "capabilities") as probe:
            status = cli_capabilities.stage_status(Path(tmp), "stage1_hooks", "codex", settings=all_on())
            probe.assert_not_called()
            self.assertEqual(status["reason"], "stage1_hooks is switched off")

    def test_a_cache_from_an_older_harness_missing_a_static_item_is_probed_again(self):
        identity = {"provider": "claude", "path": "/x/claude", "version": "1", "sha256": "ab"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cli_capabilities, "binary_identity", return_value=identity):
            home = Path(tmp)
            path = cli_capabilities.cache_path(home, identity)
            path.parent.mkdir(parents=True)
            old = {name: True for name in cli_capabilities.STATIC_ITEMS["claude"] if name != "claude.inbox_relay_admission"}
            path.write_text(json.dumps({"identity": identity, "static": old, "probed_at": "old"}))
            fresh = {name: True for name in cli_capabilities.STATIC_ITEMS["claude"]}
            with mock.patch.dict(cli_capabilities.PROBES, {"claude": lambda executable, scratch: fresh}):
                record = cli_capabilities.capabilities(home, "claude")
                self.assertEqual(record["static"], fresh, "a missing item is probed, not read as unproven")
                self.assertNotEqual(record["probed_at"], "old")
                again = cli_capabilities.capabilities(home, "claude")
                self.assertEqual(again["probed_at"], record["probed_at"], "a complete cache is used as is")

    def test_live_results_accept_only_true_false_or_unproven(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                cli_capabilities, "binary_identity",
                return_value={"provider": "codex", "path": "/x/codex", "version": "1", "sha256": "ab"}):
            with self.assertRaisesRegex(ValueError, "must be true, false or UNPROVEN"):
                cli_capabilities.record_live(Path(tmp), "codex", {"codex.turn_receipt": "yes"}, auth_mode="none")
            path = cli_capabilities.record_live(Path(tmp), "codex", {"codex.turn_receipt": True}, auth_mode="api-key")
            self.assertEqual(json.loads(path.read_text())["auth_mode"], "api-key")


class ProbeTests(unittest.TestCase):
    """The static probe, against a stub CLI: what the binary offers, cached per binary identity."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def probe(self, fake: Path) -> dict:
        with mock.patch.dict(os.environ, {"HARNESS_CODEX_BIN": str(fake)}):
            return cli_capabilities.capabilities(self.base / "home", "codex")

    def test_the_probe_reads_capabilities_from_the_binary_and_caches_them(self):
        fake = write_fake_codex(self.base / "codex")
        record = self.probe(fake)
        static = record["static"]
        for name in ("codex.hooks_session_flags", "codex.bypass_hook_trust_flag", "codex.remote_tui",
                     "codex.app_server_stdio", "codex.turn_client_message_id", "codex.exclude_tmp_keys",
                     "codex.developer_instructions", "codex.permission_profiles", "codex.mcp_command"):
            self.assertTrue(static[name], name)
        calls = Path(str(fake) + ".calls").read_text().count("\n")
        self.probe(fake)
        self.assertEqual(Path(str(fake) + ".calls").read_text().count("\n"), calls, "second read comes from the cache")
        # Scratch homes only: the probe never ran with the owner's CODEX_HOME.
        self.assertNotIn(str(Path.home() / ".codex"), Path(str(fake) + ".calls").read_text())

    def test_a_missing_capability_is_detected_and_an_upgrade_reprobes(self):
        fake = write_fake_codex(self.base / "codex", remote=False, hooks=False)
        static = self.probe(fake)["static"]
        self.assertFalse(static["codex.remote_tui"])
        self.assertFalse(static["codex.hooks_session_flags"])
        write_fake_codex(fake, remote=True, hooks=True)   # the binary changed: a new identity
        static = self.probe(fake)["static"]
        self.assertTrue(static["codex.remote_tui"])
        self.assertTrue(static["codex.hooks_session_flags"])

    def test_a_named_binary_keys_the_same_record_as_the_discovered_one(self):
        """Found live (dev copy, Stage 2): the runner names the CLI via HARNESS_*_BIN, the
        spike discovers it on PATH. The identity must not depend on which - so the version
        is always the binary's own answer, never '' for a named binary."""
        fake = write_fake_codex(self.base / "codex")
        with mock.patch.dict(os.environ, {"HARNESS_CODEX_BIN": str(fake)}):
            named = cli_capabilities.binary_identity("codex")
        self.assertEqual(named["version"], "9.9.9")
        with mock.patch.object(cli_capabilities.global_settings, "resolved_cli",
                               return_value={"path": str(fake), "version": "9.9.9", "source": "discovered"}):
            discovered = cli_capabilities.binary_identity("codex")
        self.assertEqual(cli_capabilities.cache_path(self.base, named), cli_capabilities.cache_path(self.base, discovered))

    def test_a_missing_cli_is_reported_not_probed(self):
        with mock.patch.dict(os.environ, {"HARNESS_CODEX_BIN": str(self.base / "absent")}):
            record = cli_capabilities.capabilities(self.base / "home", "codex")
        self.assertTrue(record.get("missing"))


if __name__ == "__main__":
    unittest.main()

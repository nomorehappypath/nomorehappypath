# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""What passed on one version of a CLI stays passed on its later versions.

Owner's policy, 2026-10-04: "once something has passed on one version of Codex
or Claude, we move on. We don't re-review it every time a CLI updates." The
CLIs update almost daily; before this, a proven stage switched itself off on
every update until someone proved it again.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import cli_capabilities, global_settings


def identity(version: str, sha: str) -> dict[str, str]:
    return {"provider": "claude", "path": f"/cli/versions/{version}", "version": version, "sha256": sha}


class CarryForwardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def write_static(self, ident: dict[str, str]) -> None:
        static = {name: True for name in cli_capabilities.STATIC_ITEMS["claude"]}
        path = cli_capabilities.cache_path(self.home, ident)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"identity": ident, "static": static, "probed_at": "t",
                                    "probe_version": cli_capabilities.PROBE_VERSION}), encoding="utf-8")

    def write_live(self, ident: dict[str, str], results: dict) -> None:
        path = cli_capabilities.cache_path(self.home, ident, live=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"identity": ident, "results": results, "evidence": {}}), encoding="utf-8")

    def record_for(self, ident: dict[str, str]) -> dict:
        self.write_static(ident)
        with mock.patch.object(cli_capabilities, "binary_identity", return_value=ident):
            return cli_capabilities.capabilities(self.home, "claude")

    def stage4(self, ident: dict[str, str]) -> dict:
        settings = global_settings.load(self.home)
        settings["plumbing"]["stage4_system_layer_directives_enabled"] = True
        self.write_static(ident)
        with mock.patch.object(cli_capabilities, "binary_identity", return_value=ident):
            return cli_capabilities.stage_status(self.home, "stage4_system_layer_directives", "claude", settings=settings)

    def test_a_proof_on_one_version_keeps_the_stage_active_after_the_cli_updates(self):
        self.write_live(identity("2.1.289", "a"), {"claude.append_prompt_on_resume": True})
        newer = identity("2.1.290", "b")
        self.assertEqual(self.stage4(newer), {"enabled": True, "reason": ""})
        self.assertEqual(self.record_for(newer)["carried"], {"claude.append_prompt_on_resume": "2.1.289"})

    def test_this_binarys_own_false_wins_over_a_carried_proof(self):
        self.write_live(identity("2.1.289", "a"), {"claude.append_prompt_on_resume": True})
        newer = identity("2.1.290", "b")
        self.write_live(newer, {"claude.append_prompt_on_resume": False})
        self.assertFalse(self.stage4(newer)["enabled"])

    def test_an_unproven_or_false_older_result_is_never_carried(self):
        self.write_live(identity("2.1.289", "a"), {"claude.append_prompt_on_resume": "UNPROVEN", "claude.inbox_receipt": False})
        live = self.record_for(identity("2.1.290", "b"))["live"]
        self.assertNotIn("claude.inbox_receipt", live)
        self.assertNotEqual(live.get("claude.append_prompt_on_resume"), True)

    def test_a_proof_never_flows_backwards_or_across_a_major_version(self):
        self.write_live(identity("2.1.300", "a"), {"claude.append_prompt_on_resume": True})
        self.write_live(identity("1.9.0", "c"), {"claude.inbox_receipt": True})
        older = self.record_for(identity("2.1.290", "b"))
        self.assertNotEqual(older["live"].get("claude.append_prompt_on_resume"), True, "not from a newer version")
        self.write_live(identity("2.1.289", "d"), {})
        major = self.record_for(identity("3.0.0", "e"))
        self.assertNotIn("carried", major, "not across a major version")

    def test_the_static_probe_still_decides_for_the_new_binary(self):
        self.write_live(identity("2.1.289", "a"), {"claude.append_prompt_on_resume": True})
        newer = identity("2.1.290", "b")
        static = {name: True for name in cli_capabilities.STATIC_ITEMS["claude"]}
        static["claude.append_system_prompt"] = False          # the new binary lost the flag
        path = cli_capabilities.cache_path(self.home, newer)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"identity": newer, "static": static, "probed_at": "t",
                                    "probe_version": cli_capabilities.PROBE_VERSION}), encoding="utf-8")
        settings = global_settings.load(self.home)
        settings["plumbing"]["stage4_system_layer_directives_enabled"] = True
        with mock.patch.object(cli_capabilities, "binary_identity", return_value=newer):
            status = cli_capabilities.stage_status(self.home, "stage4_system_layer_directives", "claude", settings=settings)
        self.assertFalse(status["enabled"])
        self.assertIn("claude.append_system_prompt=false", status["reason"])

    def test_a_wrapper_cli_whose_launcher_did_not_change_still_carries_its_proofs(self):
        # Review r1: Codex is identified by its launcher (codex.js), which an
        # update can reinstall byte-identically while the version moves on.
        older = {"provider": "codex", "path": "/cli/codex.js", "version": "0.160.0", "sha256": "same"}
        newer = {"provider": "codex", "path": "/cli/codex.js", "version": "0.161.0", "sha256": "same"}
        self.write_live(older, {"codex.developer_instructions_on_resume": True})
        static = {name: True for name in cli_capabilities.STATIC_ITEMS["codex"]}
        path = cli_capabilities.cache_path(self.home, newer)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"identity": newer, "static": static, "probed_at": "t",
                                    "probe_version": cli_capabilities.PROBE_VERSION}), encoding="utf-8")
        settings = global_settings.load(self.home)
        settings["plumbing"]["stage4_system_layer_directives_enabled"] = True
        with mock.patch.object(cli_capabilities, "binary_identity", return_value=newer):
            record = cli_capabilities.capabilities(self.home, "codex")
            status = cli_capabilities.stage_status(self.home, "stage4_system_layer_directives", "codex", settings=settings)
        self.assertEqual(record["carried"], {"codex.developer_instructions_on_resume": "0.160.0"})
        self.assertEqual(status, {"enabled": True, "reason": ""})
        self.write_live(newer, {"codex.developer_instructions_on_resume": False})
        with mock.patch.object(cli_capabilities, "binary_identity", return_value=newer):
            self.assertFalse(cli_capabilities.stage_status(self.home, "stage4_system_layer_directives", "codex", settings=settings)["enabled"],
                             "this binary's own false still wins")

    def test_a_newer_explicit_false_stops_an_older_proof_from_being_carried(self):
        self.write_live(identity("2.1.288", "a"), {"claude.append_prompt_on_resume": True})
        self.write_live(identity("2.1.289", "b"), {"claude.append_prompt_on_resume": False})
        record = self.record_for(identity("2.1.290", "c"))
        self.assertNotEqual(record["live"].get("claude.append_prompt_on_resume"), True)
        self.assertNotIn("carried", record)


if __name__ == "__main__":
    unittest.main()

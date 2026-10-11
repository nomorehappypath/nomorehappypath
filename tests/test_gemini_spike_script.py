# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""scripts/agy_spike.sh against a stub `agy`: the gates it reports and how it records them.

The real CLI is never started. The stub signs in (or not) and answers the two
headless calls; it plays no TUI, so the delivery gate is expected to report false.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import agent_confinement, cli_capabilities, platform_support

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "agy_spike.sh"
STUB = r'''#!/bin/sh
case "$1" in
  --version) echo 1.3.1 ;;
  models) {models} ;;
  -p) {headless} ;;
  *) sleep 1 ;;
esac
'''


def stub(path: Path, *, signed_in: bool) -> Path:
    models = "echo gemini-3.8-flash-low" if signed_in else "echo 'You are not logged into Antigravity.' >&2; exit 1"
    headless = 'echo \'{"status": "SUCCESS", "response": "OK"}\'' if signed_in else "exit 1"
    path.write_text(STUB.format(models=models, headless=headless), encoding="utf-8")
    path.chmod(0o755)
    return path


class SpikeScriptTests(unittest.TestCase):
    def setUp(self):
        try:
            platform_support.agent_confinement()
        except Exception:                                  # pragma: no cover - platform without a box
            self.skipTest("no confinement primitive on this machine")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()

    def run_spike(self, agy: Path, *extra: str) -> dict[str, tuple[str, str]]:
        home = self.base / "home"
        home.mkdir(exist_ok=True)
        environment = {**os.environ, "HARNESS_AGY_BIN": str(agy), "HOME": str(home), "AGY_SPIKE_BOOT": "1",
                       "AGY_SPIKE_TURN": "1", "TMPDIR": str(self.base)}
        done = subprocess.run(["bash", str(SCRIPT), "--work", str(self.base / "work"), *extra], env=environment,
                              capture_output=True, text=True, timeout=120)
        gates = {}
        for line in done.stdout.splitlines():
            if line.startswith("GATE "):
                _, name, value, evidence = (line.split(" ", 3) + [""])[:4]
                gates[name] = (value, evidence)
        self.assertTrue(gates, f"no GATE lines:\n{done.stdout}\n{done.stderr}")
        return gates

    def test_a_signed_out_cli_records_g_a_false_with_the_reason_and_the_work_dir_is_removed(self):
        gates = self.run_spike(stub(self.base / "agy", signed_in=False))
        value, evidence = gates["gemini.auth_under_confinement"]
        self.assertEqual(value, "false")
        self.assertIn("not logged into Antigravity", evidence)
        self.assertFalse((self.base / "work").exists(), "the spike leaves nothing behind")

    def test_a_signed_in_cli_proves_g_a_inside_the_box_and_the_other_gates_report_their_own_answer(self):
        gates = self.run_spike(stub(self.base / "agy", signed_in=True))
        self.assertEqual(gates["gemini.auth_under_confinement"][0], "true")
        self.assertEqual(gates["gemini.session_scoped_controls"][0], "false", "the stub fires no hook")
        self.assertEqual(gates["gemini.busy_delivery_receipt"][0], "false", "the stub writes no transcript")
        self.assertEqual(gates["gemini.process_group_cleanup"][0], "true")

    def test_record_stores_each_gate_with_its_evidence_for_the_binary(self):
        agy = stub(self.base / "agy", signed_in=True)
        manager_home = self.base / "manager"
        self.run_spike(agy, "--record", str(manager_home))
        # Restored, not removed: the suite pins HARNESS_AGY_BIN to "no agy" so no test can ever find the owner's real one.
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)}):
            record = cli_capabilities.capabilities(manager_home, "gemini")
        self.assertIs(record["live"]["gemini.auth_under_confinement"], True)
        self.assertIs(record["live"]["gemini.session_scoped_controls"], False)
        live = json.loads(cli_capabilities.cache_path(manager_home, record["identity"], live=True).read_text())
        self.assertIn("SUCCESS", live["evidence"]["gemini.auth_under_confinement"])

    def test_a_missing_cli_stops_at_once(self):
        environment = {**os.environ, "HARNESS_AGY_BIN": str(self.base / "absent"), "PATH": "/usr/bin:/bin"}
        done = subprocess.run(["bash", str(SCRIPT)], env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 1)
        self.assertIn("GATE agy_missing false", done.stdout)


def tearDownModule():
    from tests.environment_support import assert_agy_pinned
    assert_agy_pinned()


if __name__ == "__main__":
    unittest.main()

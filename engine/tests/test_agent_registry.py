#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Regression tests for the agent registry (engine/scripts/agent_registry.py). Stdlib unittest.

Run via: bash engine/scripts/run_tests.sh   (or: python3 -m unittest discover -s engine/tests)
"""
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_registry as reg  # noqa: E402


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        (Path(self._tmp.name) / ".agents").mkdir()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _backdate_heartbeat(self, signature, seconds_ago):
        p = reg._file_for(signature)
        rec = json.loads(p.read_text())
        rec["heartbeat_at"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
        p.write_text(json.dumps(rec))

    def test_register_unique_and_format(self):  # AC1
        a = reg.register("Claude (Anthropic)", "implementer")
        b = reg.register("Claude (Anthropic)", "implementer")
        self.assertRegex(a["signature"], r"^claude-implementer-[0-9a-f]{8}$")  # slug keeps the word whole
        self.assertNotEqual(a["signature"], b["signature"])
        self.assertEqual(a["generation"], 1)
        self.assertEqual(a["status"], "active")
        self.assertTrue(reg._file_for(a["signature"]).exists())

    def test_register_never_collides_even_with_duplicate_uuid(self):  # AC1 root cause
        """Force uuid4 to return the SAME value twice (Codex's reproduction): register must
        still produce two distinct signatures and must NOT overwrite the first record."""
        class _FixedUUID:
            hex = "abcd1234ffff0000"
        with mock.patch.object(reg.uuid, "uuid4", return_value=_FixedUUID()):
            a = reg.register("VendorX", "implementer")
            b = reg.register("VendorX", "implementer")
        self.assertNotEqual(a["signature"], b["signature"])
        self.assertTrue(reg._file_for(a["signature"]).exists())
        self.assertTrue(reg._file_for(b["signature"]).exists())
        self.assertEqual(len(reg.list_agents()), 2)  # both records preserved, no overwrite

    def test_heartbeat_advances_and_active(self):  # AC2
        a = reg.register("VendorX", "worker")
        self._backdate_heartbeat(a["signature"], 60)
        reg.heartbeat(a["signature"])
        listed = {r["signature"]: r for r in reg.list_agents(stale_seconds=120)}
        self.assertEqual(listed[a["signature"]]["liveness"], "active")
        self.assertLess(listed[a["signature"]]["heartbeat_age_seconds"], 5)

    def test_stale_detection_deadmans_switch(self):  # AC3
        fresh = reg.register("VendorX", "worker")
        dead = reg.register("VendorX", "worker")
        self._backdate_heartbeat(dead["signature"], 3600)
        listed = {r["signature"]: r for r in reg.list_agents(stale_seconds=120)}
        self.assertEqual(listed[fresh["signature"]]["liveness"], "active")
        self.assertEqual(listed[dead["signature"]]["liveness"], "stale")

    def test_recycle_lineage(self):  # AC4
        a = reg.register("Claude (Anthropic)", "implementer", task="T1")
        b = reg.recycle(a["signature"])
        self.assertEqual(b["signature"], a["base"] + "#2")
        self.assertEqual(b["generation"], 2)
        self.assertEqual(b["recycled_from"], a["signature"])
        self.assertEqual(b["task"], "T1")
        self.assertEqual(b["base"], a["base"])
        old = reg._load(a["signature"])
        self.assertEqual(old["status"], "retired")
        self.assertEqual(old["recycled_to"], b["signature"])
        # old excluded from active; new is active
        live = {r["signature"]: r["liveness"] for r in reg.list_agents()}
        self.assertEqual(live[a["signature"]], "retired")
        self.assertEqual(live[b["signature"]], "active")

    def test_retire_excludes_from_active(self):  # AC5
        a = reg.register("VendorX", "worker")
        reg.retire(a["signature"])
        live = {r["signature"]: r["liveness"] for r in reg.list_agents()}
        self.assertEqual(live[a["signature"]], "retired")

    def test_cli_smoke(self):  # AC6
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(reg.main(["register", "--vendor", "Claude (Anthropic)", "--role", "reviewer"]), 0)
        sig = buf.getvalue().strip()
        self.assertRegex(sig, r"^claude-reviewer-[0-9a-f]{8}$")
        for argv in (["heartbeat", "--signature", sig], ["recycle", "--signature", sig], ["list"]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(reg.main(argv), 0)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Regression tests for the live dashboard (engine/scripts/dashboard.py). Stdlib unittest.

Run via: bash engine/scripts/run_tests.sh
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_registry as reg  # noqa: E402
import claim  # noqa: E402
import dashboard  # noqa: E402


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        (Path(self._tmp.name) / ".agents").mkdir()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _backdate(self, signature, seconds_ago):
        p = reg._file_for(signature)
        rec = json.loads(p.read_text())
        rec["heartbeat_at"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
        p.write_text(json.dumps(rec))

    def test_snapshot_nodes_and_claimed_edge(self):  # AC1
        a = reg.register("VendorX", "worker")
        item = claim.post("T1", "worker")
        claim.claim(item["item_id"], a["signature"])
        snap = dashboard.snapshot()
        ids = {n["id"] for n in snap["nodes"]}
        self.assertIn(a["signature"], ids)
        self.assertIn(item["item_id"], ids)
        self.assertIn(
            {"from": a["signature"], "to": item["item_id"], "kind": "claimed"},
            snap["edges"],
        )

    def test_snapshot_recycle_edge(self):  # AC1
        a = reg.register("VendorX", "worker")
        b = reg.recycle(a["signature"])
        snap = dashboard.snapshot()
        self.assertIn(
            {"from": a["signature"], "to": b["signature"], "kind": "recycled"},
            snap["edges"],
        )

    def test_stale_liveness_deadmans_switch(self):  # AC2
        a = reg.register("VendorX", "worker")
        self._backdate(a["signature"], 3600)
        snap = dashboard.snapshot(stale_seconds=120)
        node = next(n for n in snap["nodes"] if n["id"] == a["signature"])
        self.assertEqual(node["liveness"], "stale")

    def test_render_html_cards_arrows_and_stale_color(self):  # AC3
        a = reg.register("VendorX", "worker")
        item = claim.post("T1", "worker")
        claim.claim(item["item_id"], a["signature"])
        self._backdate(a["signature"], 3600)
        snap = dashboard.snapshot(stale_seconds=120)
        html_doc = dashboard.render_html(snap)
        self.assertIn(a["signature"], html_doc)             # agent card
        self.assertIn(item["item_id"], html_doc)            # item card
        self.assertIn("<marker", html_doc)                  # arrowhead def
        self.assertIn("liveness-stale", html_doc)           # dead-man's-switch class
        self.assertIn("#e3342f", html_doc)                  # red
        # one SVG arrow <path> per edge that has both endpoints
        # (match '<path class="edge' so the '<svg class="edges">' wrapper isn't counted)
        drawable = [e for e in snap["edges"]]
        self.assertEqual(html_doc.count('<path class="edge'), len(drawable))

    def test_render_text(self):  # AC4
        a = reg.register("VendorX", "worker")
        item = claim.post("T1", "worker")
        claim.claim(item["item_id"], a["signature"])
        text = dashboard.render_text(dashboard.snapshot())
        self.assertIn("Agents:", text)
        self.assertIn("Links", text)
        self.assertIn("--claimed-->", text)
        self.assertIn(a["signature"], text)

    def test_cli_smoke(self):  # AC5
        reg.register("VendorX", "worker")
        claim.post("T1", "worker")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(dashboard.main(["snapshot"]), 0)
            self.assertEqual(dashboard.main(["show"]), 0)
            self.assertEqual(dashboard.main(["html", "--out", "build/dash.html"]), 0)
        self.assertTrue(Path("build/dash.html").exists())


if __name__ == "__main__":
    unittest.main()

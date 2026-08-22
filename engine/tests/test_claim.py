#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Regression tests for the claim/assignment protocol (engine/scripts/claim.py). Stdlib unittest.

Run via: bash engine/scripts/run_tests.sh
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_registry as reg  # noqa: E402
import claim  # noqa: E402


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        (Path(self._tmp.name) / ".agents").mkdir()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_post_creates_open_item(self):  # AC1
        item = claim.post("T1", "implementer")
        self.assertEqual(item["status"], "open")
        self.assertIsNone(item["claimed_by"])
        self.assertTrue(claim._item_path(item["item_id"]).exists())

    def test_claim_atomic_no_double_claim(self):  # AC2
        a = reg.register("VendorX", "implementer")
        item = claim.post("T1", "implementer")
        claimed = claim.claim(item["item_id"], a["signature"])
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["claimed_by"], a["signature"])
        b = reg.register("VendorY", "implementer")
        with self.assertRaises(claim.ClaimError):
            claim.claim(item["item_id"], b["signature"])  # already claimed

    def test_cross_vendor_rejected_diff_vendor_ok(self):  # AC3
        impl = reg.register("Claude (Anthropic)", "implementer")
        rev_same = reg.register("Claude (Anthropic)", "reviewer")
        rev_diff = reg.register("Codex (OpenAI)", "reviewer")
        item = claim.post("T1", "reviewer", forbid_vendor="Claude (Anthropic)")
        with self.assertRaises(claim.ClaimError):
            claim.claim(item["item_id"], rev_same["signature"])   # same vendor forbidden
        claimed = claim.claim(item["item_id"], rev_diff["signature"])  # different vendor OK
        self.assertEqual(claimed["claimed_by"], rev_diff["signature"])
        self.assertNotEqual(impl["vendor"], rev_diff["vendor"])

    def test_unregistered_signature_rejected(self):  # AC4
        item = claim.post("T1", "implementer")
        with self.assertRaises(claim.ClaimError):
            claim.claim(item["item_id"], "ghost-implementer-0000")

    def test_complete_and_release(self):  # AC5
        a = reg.register("VendorX", "worker")
        item = claim.post("T1", "worker")
        claim.claim(item["item_id"], a["signature"])
        done = claim.complete(item["item_id"])
        self.assertEqual(done["status"], "done")
        # a fresh item: claim, release back to open, re-claim
        item2 = claim.post("T2", "worker")
        claim.claim(item2["item_id"], a["signature"])
        rel = claim.release(item2["item_id"])
        self.assertEqual(rel["status"], "open")
        self.assertIsNone(rel["claimed_by"])
        self.assertFalse(claim._lock_path(item2["item_id"]).exists())
        reclaim = claim.claim(item2["item_id"], a["signature"])
        self.assertEqual(reclaim["status"], "claimed")

    def test_cli_smoke(self):  # AC6
        a = reg.register("VendorX", "implementer")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(claim.main(["post", "--task", "T9", "--role", "implementer"]), 0)
        item_id = buf.getvalue().strip()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(claim.main(["claim", "--item", item_id, "--signature", a["signature"]]), 0)
            self.assertEqual(claim.main(["list"]), 0)
            self.assertEqual(claim.main(["complete", "--item", item_id]), 0)


if __name__ == "__main__":
    unittest.main()

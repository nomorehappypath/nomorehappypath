#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Regression tests for the durable scheduler (engine/scripts/scheduler.py). Stdlib unittest.

The runner seam keeps real agent execution out of the tests (no live LLM calls).
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

import claim  # noqa: E402
import scheduler  # noqa: E402

POOLS = {"implementer_vendor": "Claude (Anthropic)", "reviewer_vendor": "Codex (OpenAI)"}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        (Path(self._tmp.name) / ".agents").mkdir()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_tick_dispatches_open_item(self):  # AC1
        item = claim.post("T1", "implementer")
        res = scheduler.tick(POOLS)
        self.assertEqual(len(res["dispatched"]), 1)
        d = res["dispatched"][0]
        self.assertEqual(d["item_id"], item["item_id"])
        self.assertIn(d["vendor"], POOLS.values())
        # item is now claimed (no longer open)
        self.assertEqual(claim._load(item["item_id"])["status"], "claimed")

    def test_tick_respects_cross_vendor(self):  # AC2
        claim.post("T1", "reviewer", forbid_vendor="Claude (Anthropic)")
        res = scheduler.tick(POOLS)
        self.assertEqual(len(res["dispatched"]), 1)
        self.assertEqual(res["dispatched"][0]["vendor"], "Codex (OpenAI)")  # not the forbidden one

    def test_tick_skips_when_all_vendors_forbidden(self):  # AC2 (skip path)
        item = claim.post("T1", "reviewer", forbid_vendor="Solo")
        res = scheduler.tick({"implementer_vendor": "Solo", "reviewer_vendor": "Solo"})
        self.assertEqual(res["dispatched"], [])
        self.assertEqual(len(res["skipped"]), 1)
        self.assertEqual(claim._load(item["item_id"])["status"], "open")  # left open

    def test_tick_idempotent(self):  # AC3
        claim.post("T1", "implementer")
        first = scheduler.tick(POOLS)
        second = scheduler.tick(POOLS)
        self.assertEqual(len(first["dispatched"]), 1)
        self.assertEqual(len(second["dispatched"]), 0)  # already claimed; not re-dispatched

    def test_runner_seam_invoked(self):  # AC4
        claim.post("T1", "implementer")
        calls = []

        def fake_runner(item, agent):
            calls.append((item["item_id"], agent["signature"], agent["vendor"]))
            return {"item_id": item["item_id"], "command": "fake", "spawned": False}

        res = scheduler.tick(POOLS, runner=fake_runner)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(res["dispatched"]), 1)

    def test_default_runner_does_not_spawn(self):  # AC4
        item = {"item_id": "x", "task_id": "T", "role": "implementer"}
        agent = {"signature": "v-impl-0000", "vendor": "V", "role": "implementer"}
        out = scheduler.default_runner(item, agent)
        self.assertFalse(out["spawned"])
        self.assertIn("run V as implementer", out["command"])

    def test_cli_tick_smoke(self):  # AC5
        (Path("profile")).mkdir()
        Path("profile/profile.config").write_text(
            'implementer_vendor: "Claude (Anthropic)"\nreviewer_vendor: "Codex (OpenAI)"\n',
            encoding="utf-8",
        )
        claim.post("T9", "implementer")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scheduler.main(["tick"]), 0)


if __name__ == "__main__":
    unittest.main()

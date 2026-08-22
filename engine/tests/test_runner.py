#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Regression tests for the spawn-runner (engine/scripts/runner.py). Stdlib unittest.

Real spawning is exercised with a HARMLESS stub command (writes a marker) — never a live LLM.
Run via: bash engine/scripts/run_tests.sh
"""
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_registry as reg  # noqa: E402
import claim  # noqa: E402
import runner  # noqa: E402
import scheduler  # noqa: E402

# a stub "agent" that just writes a marker file — stands in for a real vendor CLI
STUB = "python3 -c \"open('SPAWNED_MARKER','w').write('ok')\""


def _wait_for(path, timeout=5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if Path(path).exists():
            return True
        time.sleep(0.05)
    return False


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        (Path(self._tmp.name) / ".agents").mkdir()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_dry_run_when_no_command(self):  # AC2
        a = reg.register("VendorX", "implementer")
        item = claim.post("T1", "implementer")
        rec = runner.spawn_runner(item, a, {})  # no spawn_command
        self.assertFalse(rec["spawned"])
        self.assertIsNone(rec["command"])
        self.assertIn("opt-in", rec["reason"])
        self.assertEqual(list(Path(".agents/dispatches").glob("*.json")) != [], True)  # record written

    def test_real_spawn_runs_stub_command(self):  # AC1
        a = reg.register("VendorX", "implementer")
        item = claim.post("T1", "implementer")
        rec = runner.spawn_runner(item, a, {"spawn_command": STUB})
        self.assertTrue(rec["spawned"])
        self.assertIn("pid", rec)
        self.assertTrue(_wait_for("SPAWNED_MARKER"), "stub agent process did not run")
        for _ in range(20):
            if runner.reap_children():
                break
            time.sleep(0.01)
        self.assertFalse(runner._CHILDREN)

    def test_command_for_substitution(self):  # AC1
        a = {"vendor": "V", "role": "reviewer", "signature": "v-reviewer-1234"}
        item = {"item_id": "i-1", "task_id": "T9"}
        cmd = runner.command_for(item, a, {"spawn_command": "go {role} {signature} {task} {item}"})
        self.assertEqual(cmd, "go reviewer v-reviewer-1234 T9 i-1")

    def test_make_spawn_runner_closure(self):  # AC1
        a = reg.register("VendorX", "implementer")
        item = claim.post("T1", "implementer")
        r = runner.make_spawn_runner({})  # dry-run closure
        rec = r(item, a)
        self.assertFalse(rec["spawned"])

    def test_scheduler_tick_with_spawn(self):  # AC3
        claim.post("T1", "implementer")
        profile = {"implementer_vendor": "Claude (Anthropic)", "reviewer_vendor": "Codex (OpenAI)",
                   "spawn_command": STUB}
        res = scheduler.tick(profile, runner.make_spawn_runner(profile))
        self.assertEqual(len(res["dispatched"]), 1)
        self.assertTrue(res["dispatched"][0]["spawned"])
        self.assertTrue(_wait_for("SPAWNED_MARKER"), "scheduler --spawn did not launch the agent")
        for _ in range(20):
            if runner.reap_children():
                break
            time.sleep(0.01)
        self.assertFalse(runner._CHILDREN)

    def test_cli_dispatch_dry_run(self):  # AC5
        a = reg.register("VendorX", "implementer")
        item = claim.post("T1", "implementer")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["dispatch", "--item", item["item_id"], "--signature", a["signature"]]), 0)


if __name__ == "__main__":
    unittest.main()

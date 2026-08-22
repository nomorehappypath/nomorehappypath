#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Regression tests for the OS-timer generator (engine/scripts/timer.py). Stdlib unittest.

Verifies the generated unit content; it never loads a real system timer (install is manual).
Run via: bash engine/scripts/run_tests.sh
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import timer  # noqa: E402


class TimerTests(unittest.TestCase):
    def test_render_launchd(self):  # AC4
        plist = timer.render_launchd(90, "/repo/dev_harness")
        self.assertIn("<key>StartInterval</key><integer>90</integer>", plist)
        self.assertIn("scheduler.sh", plist)
        self.assertIn("<string>tick</string>", plist)
        self.assertIn(timer.LABEL, plist)
        self.assertIn("/repo/dev_harness", plist)

    def test_render_cron(self):  # AC4
        line = timer.render_cron(120, "/repo/dev_harness")
        self.assertIn("* * * * *", line)
        self.assertIn("scheduler.sh tick", line)
        self.assertIn("/repo/dev_harness", line)
        self.assertIn("120", line)

    def test_cli_gen_writes_file_and_does_not_install(self):  # AC4 + AC5
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "agent.plist"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = timer.main(["gen", "--kind", "launchd", "--interval", "75",
                                 "--repo", "/repo/x", "--out", str(out)])
            self.assertEqual(rc, 0)
            self.assertIn("<integer>75</integer>", out.read_text())
            # the only file written is the one we asked for — no system timer was loaded
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["agent.plist"])

    def test_cli_gen_cron_stdout(self):  # AC5
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            rc = timer.main(["gen", "--kind", "cron", "--interval", "60", "--repo", "/repo/x"])
        self.assertEqual(rc, 0)
        self.assertIn("* * * * *", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

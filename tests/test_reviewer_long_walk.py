# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Reviewer's commands may run as long as the end-to-end walk needs.

Owner, 2026-10-07: the Reviewer split a 20-minute whole-task walk into pieces because its tools allow 10 minutes. Claude Code
caps each Bash command at BASH_MAX_TIMEOUT_MS (default 10 minutes); the launcher raises it for the Reviewer only.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from harness import control
from tests.test_stage4_system_layer import Stage4RunnerTests


class ReviewerLongWalkTests(unittest.TestCase):
    served = Stage4RunnerTests.served
    bootstrap_served = Stage4RunnerTests.bootstrap_served
    setUp = Stage4RunnerTests.setUp
    switch = Stage4RunnerTests.switch
    launch = Stage4RunnerTests.launch
    require_confinement = Stage4RunnerTests.require_confinement

    def fake(self, name: str) -> Path:
        path = self.base / name
        path.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                        f"open({str(self.capture)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                        "if sys.argv[1:] != ['--version']:\n"
                        f"    open({str(self.capture) + '.env'!r}, 'a').write(os.environ.get('BASH_MAX_TIMEOUT_MS', '<unset>') + '\\n')\n",
                        encoding="utf-8")
        path.chmod(0o755)
        return path

    def limit(self) -> str:
        record = Path(str(self.capture) + ".env")
        value = record.read_text().strip()
        record.unlink()
        return value

    def test_only_the_reviewer_gets_a_long_command_limit_and_it_comes_from_a_setting(self):
        self.require_confinement()
        self.switch("claude", stage4=False)
        self.launch(control.create(self.context, "claude_reviewer"), "claude_reviewer")
        self.assertEqual(self.limit(), "7200000", "the Reviewer's commands were capped at 10 minutes")
        self.launch(control.create(self.context, "claude_cto"), "claude_cto")
        self.assertEqual(self.limit(), "<unset>", "the CTO keeps Claude Code's default")
        self.environment["HARNESS_REVIEWER_BASH_MAX_TIMEOUT_MS"] = "3600000"
        self.launch(control.create(self.context, "claude_reviewer"), "claude_reviewer")
        self.assertEqual(self.limit(), "3600000", "the limit is a setting, not a constant")


if __name__ == "__main__":
    unittest.main()

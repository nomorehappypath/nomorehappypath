# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The CTO and Reviewer (Claude) windows let the owner select and copy text.

Owner, 2026-10-07: "the dev cli I can select and copy, the cto and reviewer no". The Delivery agent is Codex; the
CTO and Reviewer are Claude Code, whose full-screen mode captures the mouse. CLAUDE_CODE_DISABLE_MOUSE=1 is the
documented switch that gives the mouse back to the terminal. The launch script must set it for every Claude agent and
leave Codex alone.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from harness import control
from tests.test_stage4_system_layer import Stage4RunnerTests


class ClaudeWindowSelectCopyTests(unittest.TestCase):
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
                        f"    open({str(self.capture) + '.env'!r}, 'a').write(os.environ.get('CLAUDE_CODE_DISABLE_MOUSE', '<unset>') + '\\n')\n",
                        encoding="utf-8")
        path.chmod(0o755)
        return path

    def mouse_values(self) -> list[str]:
        record = Path(str(self.capture) + ".env")
        values = record.read_text().split()
        record.unlink()
        return values

    def test_claude_reviewer_and_cto_windows_give_the_mouse_to_the_terminal(self):
        self.require_confinement()
        self.switch("claude", stage4=False)
        for kind in ("claude_reviewer", "claude_cto"):
            with self.subTest(kind=kind):
                self.launch(control.create(self.context, kind), kind)
                self.assertEqual(self.mouse_values(), ["1"], f"{kind}: the owner could not select or copy text")

    def test_the_codex_delivery_window_is_left_alone(self):
        self.require_confinement()
        self.switch("codex", stage4=False)
        self.launch(control.create(self.context, "codex_delivery"), "codex_delivery")
        self.assertNotIn("1", self.mouse_values())


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner can close one open control-plane hold by its exact source; agents cannot."""
from __future__ import annotations

import contextlib
import io
import json
import unittest

from harness import board, board_surface
from harness.project_context import context_cli_arguments
from tests import test_scaffold_wedge

_fixture = vars(test_scaffold_wedge.ScaffoldFixture)


class OwnerClearHoldTests(unittest.TestCase):
    setUp = _fixture["setUp"]
    begin_task = _fixture["begin_task"]

    def run_cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = board.main([*context_cli_arguments(self.context), "clear-hold", *arguments])
        return code, out.getvalue(), err.getvalue()

    def status(self, task):
        return board.snapshot(self.context)["control_plane_holds"][task]["status"]

    def test_the_owner_clears_the_named_hold_and_nothing_else(self):
        self.begin_task("HELD-TASK")
        board.record_control_plane_hold(self.context, "HELD-TASK", "repeated_refusal:task-brief", "old false alarm")
        code, out, err = self.run_cli("--task", "HELD-TASK", "--source", "repeated_refusal:task-brief")
        self.assertEqual(code, 0, err)
        self.assertIn("Certified results were not changed", json.loads(out)["message"])
        self.assertEqual(self.status("HELD-TASK"), "resolved")

    def test_a_different_source_or_a_missing_hold_is_refused_and_changes_nothing(self):
        self.begin_task("HELD-TASK")
        board.record_control_plane_hold(self.context, "HELD-TASK", "repeated_refusal:start-subtask", "real wedge")
        code, _, err = self.run_cli("--task", "HELD-TASK", "--source", "repeated_refusal:task-brief")
        self.assertNotEqual(code, 0)
        self.assertIn("not 'repeated_refusal:task-brief'", err)
        self.assertEqual(self.status("HELD-TASK"), "open")
        code, _, err = self.run_cli("--task", "OTHER", "--source", "repeated_refusal:task-brief")
        self.assertNotEqual(code, 0)
        self.assertIn("no open hold", err)

    def test_no_agent_role_may_call_clear_hold(self):
        self.assertEqual(board_surface.AUTHORIZATION_MATRIX["clear-hold"], frozenset())


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""F-1 follow-up: a finished task can still reach "finished" and history.

Found by the owner in the test copy (2026-10-02): every task that passed its
final review stayed at "FINAL RELEASE CHECKS" for ever. The CTO's release step
(`harness.cto release-check --record-ready`) wrote the board state from inside
the CTO's sandbox, and F-1 protects that storage, so the CTO was refused ("the
CTO's release-check fails there on the board lock file"). The release is now
recorded by the board through the authenticated surface (`record-release`).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import agent_confinement, board, board_viewer, control, platform_support
from harness.board_client import ENDPOINT_ENV, PROTOCOL_ENV, TOKEN_ENV
from harness.board_surface import PROTOCOL_VERSION
from harness.project_context import ProjectContext
from tests import test_board_surface as surface
from tests.environment_support import require_loopback

ROOT = Path(__file__).resolve().parents[1]
GREEN = {key: True for key in board.RELEASE_REQUIRED_CHECKS | board.BROKER_RELEASE_REQUIRED_CHECKS}


class _Fixture(unittest.TestCase):
    served = surface.BoardSurfaceCommandTests.served
    session = surface.BoardSurfaceCommandTests.session

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        # A scaffolded layout: the board store sits INSIDE the checkout (the hard F-1 case).
        self.context = ProjectContext(code, code / ".harness", base / "workspaces")
        control.initialize(self.context)

    def passed_final_review(self, task: str) -> None:
        with board.locked_state(self.context) as state:
            state["qa_requests"][f"final-{task}"] = {
                "id": f"final-{task}", "task": task, "status": "passed", "phase": "final_acceptance",
                "stage": "independent_review", "cycle": 1, "structure_revision": 0, "subtask": "", "chunk": "",
                "developer_id": "dev", "claimed_by": "qa", "requested_at": board.now(), "review_wait_started_at": "",
                "reviewed_commit": "0" * 40, "ledger": "",
            }

    def board_call(self, token, endpoint, *arguments, wrap=None):
        environment = {**os.environ, TOKEN_ENV: token, ENDPOINT_ENV: endpoint, PROTOCOL_ENV: PROTOCOL_VERSION}
        command = [os.path.realpath(os.sys.executable), "-E", str(ROOT / "harness" / "board.py"),
                   "--root", str(self.context.code_root), *arguments]
        if wrap:
            command = wrap(command)
        return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=60)


class RecordReleaseTests(_Fixture):
    def test_the_cto_records_the_release_through_the_board(self):
        _, cto, authority, token, _ = self.session("claude_cto", "cto", "GLOBAL_MONITOR")
        self.passed_final_review("greeter")
        with mock.patch("harness.cto.release_check", return_value=dict(GREEN)), self.served(authority) as endpoint:
            completed = self.board_call(token, endpoint, "record-release", "--agent", cto["id"], "--task", "greeter")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        release = board.snapshot(self.context)["releases"]["greeter"]
        self.assertEqual(release["status"], "VISUAL_TEST_REQUIRED")

    def test_only_the_cto_may_record_and_a_cancelled_task_never_releases(self):
        _, delivery, authority, token, _ = self.session()
        self.passed_final_review("greeter")
        with self.served(authority) as endpoint:
            refused = self.board_call(token, endpoint, "record-release", "--agent", delivery["id"], "--task", "greeter")
        self.assertNotEqual(refused.returncode, 0)
        _, cto, authority, token, _ = self.session("claude_cto", "cto", "GLOBAL_MONITOR")
        with board.locked_state(self.context) as state:
            state.setdefault("cancelled_tasks", {})["greeter"] = {"cancelled_at": board.now()}
        with mock.patch("harness.cto.release_check", return_value=dict(GREEN)), self.served(authority) as endpoint:
            cancelled = self.board_call(token, endpoint, "record-release", "--agent", cto["id"], "--task", "greeter")
        self.assertNotEqual(cancelled.returncode, 0)
        self.assertIn("cancelled", cancelled.stderr)
        self.assertNotIn("greeter", board.snapshot(self.context).get("releases", {}))

    def test_a_failed_gate_still_refuses_the_release(self):
        _, cto, authority, token, _ = self.session("claude_cto", "cto", "GLOBAL_MONITOR")
        self.passed_final_review("greeter")
        failing = dict(GREEN, main_health_verified=False)
        with mock.patch("harness.cto.release_check", return_value=failing), self.served(authority) as endpoint:
            refused = self.board_call(token, endpoint, "record-release", "--agent", cto["id"], "--task", "greeter")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("main_health_verified", refused.stderr)


class InsideTheSandboxTests(_Fixture):
    """The incident, executed: the CTO under the real agent confinement."""

    def setUp(self):
        super().setUp()
        if not platform_support.agent_confinement().available():
            self.skipTest("no write-confinement primitive on this platform")

    def wrap(self, command):
        return agent_confinement.wrap(command, [str(self.context.code_root)], store=Path(self._tmp.name) / "store",
                                      home=Path.home(), protected_writes=[str(self.context.data_root)])

    def test_the_old_direct_command_names_the_remedy_and_the_board_command_records(self):
        _, cto, authority, token, _ = self.session("claude_cto", "cto", "GLOBAL_MONITOR")
        self.passed_final_review("greeter")
        environment = {**os.environ, TOKEN_ENV: token, ENDPOINT_ENV: "http://127.0.0.1:9", PROTOCOL_ENV: PROTOCOL_VERSION}
        old = subprocess.run(self.wrap([
            os.path.realpath(os.sys.executable), "-E", "-m", "harness.cto", "--root", str(self.context.code_root),
            "--data-root", str(self.context.data_root), "--workspace-root", str(self.context.workspace_root),
            "release-check", "--task", "greeter", "--ledger", "x.md", "--execute-health", "--record-ready",
            "--agent", cto["id"]]), cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60)
        self.assertEqual(old.returncode, 2, old.stdout + old.stderr)
        self.assertIn("record-release", old.stderr, "the refusal names the remedy")
        with mock.patch("harness.cto.release_check", return_value=dict(GREEN)), self.served(authority) as endpoint:
            new = self.board_call(token, endpoint, "record-release", "--agent", cto["id"], "--task", "greeter",
                                  wrap=self.wrap)
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertEqual(board.snapshot(self.context)["releases"]["greeter"]["status"], "VISUAL_TEST_REQUIRED")


class CancelTellsTheCtoTests(unittest.TestCase):
    def test_a_running_cto_is_told_directly_when_the_owner_cancels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cto = control.create(root, "claude_cto")
            control.attach(root, cto["id"], os.getpid())
            reviewer = control.create(root, "claude_reviewer")
            control.attach(root, reviewer["id"], os.getpid())
            told = board_viewer.notify_cto_of_cancel(root, "greeter")
            self.assertEqual(told, [cto["id"]])
            messages = control.take_instructions(root, cto["id"])
            self.assertEqual(len(messages), 1)
            self.assertIn("TASK CANCELLED BY THE OWNER: greeter", messages[0]["text"])
            self.assertEqual(control.take_instructions(root, reviewer["id"]), [], "only the CTO is told this way")


class DirectiveTests(unittest.TestCase):
    def test_no_agent_rulebook_points_at_a_direct_write_of_harness_storage(self):
        cto = (ROOT / "directives" / "CTO.md").read_text(encoding="utf-8")
        self.assertIn("record-release --task", cto)
        self.assertIn("Only a hold RECORDED ON THE BOARD may delay a release", cto)
        coordinator = (ROOT / "harness" / "release_coordinator.py").read_text(encoding="utf-8")
        self.assertNotIn("Call release-check", coordinator)


if __name__ == "__main__":
    unittest.main()

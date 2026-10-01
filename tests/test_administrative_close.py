# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Batch 2 item A2: close a task the owner shipped by hand, through a governed operation.

2026-10-01: the owner merged headless-browser-maya-mac into main himself
(0c48126 contains the task head d88fa78). Owner Accept needs an unchanged main
and a final review, so the task could never leave the open list.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, board_surface, control, project_manager
from tests import test_git_model_board

_fixture = vars(test_git_model_board.GitModelBoardIntegrationTests)  # helpers only
REASON = "Owner shipped it by hand: main has the task head plus the Help commit, pushed; Maya server on 2026.10.01.1519."


class AdministrativeCloseTests(unittest.TestCase):
    setUp = _fixture["setUp"]
    tearDown = _fixture["tearDown"]
    git = _fixture["git"]

    def shipped_by_hand(self):
        """Task work committed on its branch, then merged into main outside the board, plus a Help commit."""
        workspace = Path(self.begun["task_workspace"])
        (workspace / "product.txt").write_text("shipped\n", encoding="utf-8")
        committed = board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "task work")
        self.git("merge", "--ff-only", committed["commit"])
        (self.root / "help.md").write_text("help\n", encoding="utf-8")
        self.git("add", "help.md")
        self.git("commit", "-m", "Help commit")
        return committed["commit"], self.git("rev-parse", "HEAD").strip()

    def cto(self):
        session = control.create(self.root, "claude_cto")
        return board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])

    def open_count(self):
        directory = Path(tempfile.mkdtemp(dir=self.root.parent))
        (directory / "data" / "board").mkdir(parents=True)
        (directory / "data" / "board" / "state.json").write_text(json.dumps(board.snapshot(self.root)), encoding="utf-8")
        row = project_manager.derive_status({"data_root": str(directory / "data"), "code_root": str(directory), "workspace_root": str(directory / "ws")})
        return row["task_counts"]

    def test_cto_closes_a_task_shipped_by_hand_and_it_leaves_the_open_list(self):
        task_head, shipped = self.shipped_by_hand()
        with self.assertRaisesRegex(ValueError, "owner responses are available only for a released task"):
            board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        before = self.open_count()
        cto = self.cto()["id"]
        closed = board.close_released_administratively(self.root, cto, "GIT-MODEL", shipped[:7], REASON)
        self.assertEqual((closed["released_commit"], closed["task_head"], closed["by_role"]), (shipped, task_head, "cto"))
        state = board.snapshot(self.root)
        self.assertEqual(state["releases"]["GIT-MODEL"]["status"], board.RELEASE_ACCEPTED)
        self.assertEqual(state["releases"]["GIT-MODEL"]["head_commit"], shipped)
        self.assertEqual(state["release_decisions"]["GIT-MODEL"]["decision"], "accepted")
        self.assertTrue(state["release_decisions"]["GIT-MODEL"]["administrative"])
        self.assertEqual(state["git_acceptances"]["GIT-MODEL"]["commit"], shipped)
        self.assertTrue(board._task_finished(state, "GIT-MODEL"))
        event = [e for e in state["events"] if e["kind"] == "task_closed_administratively"][-1]
        self.assertIn(f"The CTO recorded GIT-MODEL as released and accepted at {shipped[:12]}", event["message"])
        after = self.open_count()
        self.assertEqual(before["open"], 1)
        self.assertEqual(after["open"], 0, "the task left the open list")
        # A2 follow-up: the same command again is an idempotent residue run; another commit still refuses.
        self.assertTrue(board.close_released_administratively(self.root, cto, "GIT-MODEL", shipped, REASON)["already_closed"])
        with self.assertRaisesRegex(ValueError, "already released or accepted"):
            board.close_released_administratively(self.root, cto, "GIT-MODEL", task_head, REASON)

    def test_the_owner_can_close_it_at_the_local_cli(self):
        _, shipped = self.shipped_by_hand()
        result = subprocess.run(
            [sys.executable, str(Path(board.__file__)), "--root", str(self.root), "close-released",
             "--task", "GIT-MODEL", "--commit", shipped, "--reason", REASON],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(board.snapshot(self.root)["releases"]["GIT-MODEL"]["administrative_close"]["by_role"], "owner")

    def test_unproven_closes_are_refused_and_change_nothing(self):
        task_head, shipped = self.shipped_by_hand()
        cto = self.cto()["id"]

        def refused(pattern, *, agent=cto, task="GIT-MODEL", commit=shipped, reason=REASON):
            before = json.dumps(board.snapshot(self.root).get("releases", {}), sort_keys=True)
            with self.assertRaisesRegex(ValueError, pattern):
                board.close_released_administratively(self.root, agent, task, commit, reason)
            self.assertEqual(json.dumps(board.snapshot(self.root).get("releases", {}), sort_keys=True), before)

        refused("plain-language reason", reason="")
        refused("commit id", commit="not-a-sha")
        refused("does not exist", commit="0" * 40)
        # A commit off main: a side branch carrying the task head.
        self.git("checkout", "-q", "-b", "side", task_head)
        (self.root / "side.txt").write_text("side\n", encoding="utf-8")
        self.git("add", "side.txt"); self.git("commit", "-m", "side")
        side = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-q", "main")
        refused("is not on main", commit=side)
        # A main commit that predates the task's work.
        refused("does not contain the task's own work", commit=self.base_commit)
        refused("only the CTO or the owner", agent=self.delivery["id"])
        reviewer_session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=reviewer_session["id"])
        refused("only the CTO or the owner", agent=reviewer["id"])
        refused("no governed repository", task="NO-SUCH-TASK")
        with board.locked_state(self.root) as state:
            state["task_repositories"]["GIT-MODEL"] = str(self.root / "moved-away")
        refused("not reachable")

    def residue(self):
        """What studio's maya still had after the first close: findings and a card."""
        with board.locked_state(self.root) as state:
            findings = state.setdefault("deferred_findings", {})
            for index, (status, task, classification) in enumerate((
                ("in_scope", "GIT-MODEL", "impacts_current_task"),
                ("in_scope", "GIT-MODEL", "impacts_current_task"),
                ("needs_triage", "GIT-MODEL", "unrelated_to_current_task"),
                ("deferred", "GIT-MODEL", "unrelated_to_current_task"),
                ("in_scope", "OTHER-TASK", "impacts_current_task"),
            )):
                findings[f"F-{index}"] = {
                    "id": f"F-{index}", "fingerprint": f"F-{index}", "task": task, "title": "x", "description": "x",
                    "evidence": "x", "status": status, "classification": classification, "decision": None,
                    "created_at": board.now(), "decided_at": None, "next_action": "x",
                }
        return board.record_owner_action(
            self.root, self.cto_id, "Ask harness dev to close the task (complete) now", task="GIT-MODEL",
        )

    def assert_finished_everywhere(self):
        state = board.snapshot(self.root)
        working = [a for a in state["agents"].values() if a.get("task") == "GIT-MODEL" and a.get("active")]
        self.assertEqual(working, [], "no agent stays working on the closed task")
        # A done Delivery agent on an accepted task moves to the board's cold
        # store, the same end state a normal complete + Accept leaves.
        self.assertNotIn(self.delivery["id"], state["agents"])
        retired = [e for e in state["events"] if e["kind"] == "administrative_close_residue_cleared"][-1]
        self.assertIn(self.delivery["id"], retired["retired_agents"])
        findings = state["deferred_findings"]
        self.assertEqual([findings[f"F-{i}"]["status"] for i in range(5)],
                         ["resolved", "resolved", "needs_triage", "deferred", "in_scope"])
        self.assertIn("Superseded: Task closed as released", findings["F-0"]["resolution_evidence"])
        cards = [c for c in state["owner_actions"].values() if c.get("task") == "GIT-MODEL" and c.get("status") == "open"]
        self.assertEqual(cards, [], "the task's owner cards clear")
        return state

    def test_closing_finishes_the_task_everywhere(self):
        _, shipped = self.shipped_by_hand()
        self.cto_id = self.cto()["id"]
        self.residue()
        with patch("harness.control.enqueue_instruction") as told:
            closed = board.close_released_administratively(self.root, self.cto_id, "GIT-MODEL", shipped, REASON)
        self.assertEqual(closed["retired_agents"], [self.delivery["id"]])
        self.assertEqual(closed["superseded_findings"], ["F-0", "F-1"])
        self.assertEqual(told.call_count, 1)
        self.assertIn("TASK CLOSED: GIT-MODEL", told.call_args[0][2])
        self.assert_finished_everywhere()

    def test_rerunning_the_same_close_finishes_the_residue_and_nothing_else(self):
        """Studio's maya: closed by the first A2, then its Delivery agent resumed and its findings stayed open."""
        _, shipped = self.shipped_by_hand()
        self.cto_id = self.cto()["id"]
        with patch("harness.control.enqueue_instruction"):
            board.close_released_administratively(self.root, self.cto_id, "GIT-MODEL", shipped, REASON)
        resumed = "engineering-0033-resumed"
        with board.locked_state(self.root) as state:   # studio's resume brought a Delivery agent back onto the task
            state["agents"][resumed] = {
                "id": resumed, "role": "engineering", "task": "GIT-MODEL", "active": True, "status": "working",
                "write_authority": True, "session_id": "codex_delivery-resumed", "poll_counter": 0,
                "status_note": "recovery accepted; preserved task and next action resumed", "last_status_at": board.now(),
            }
        self.delivery = {"id": resumed}
        self.residue()
        before = board.snapshot(self.root)
        records = {key: json.dumps(before[key]["GIT-MODEL"], sort_keys=True) for key in ("releases", "release_decisions", "git_acceptances")}
        with patch("harness.control.enqueue_instruction"):
            again = board.close_released_administratively(self.root, None, "GIT-MODEL", shipped[:7], REASON)
        self.assertTrue(again["already_closed"])
        self.assertEqual(again["retired_agents"], [self.delivery["id"]])
        state = self.assert_finished_everywhere()
        self.assertEqual({key: json.dumps(state[key]["GIT-MODEL"], sort_keys=True) for key in records}, records,
                         "the release record is not rewritten")
        events = len(state["events"])
        with patch("harness.control.enqueue_instruction") as told:
            third = board.close_released_administratively(self.root, None, "GIT-MODEL", shipped, REASON)
        self.assertEqual((third["retired_agents"], third["superseded_findings"]), ([], []))
        self.assertEqual(len(board.snapshot(self.root)["events"]), events, "a second re-run changes nothing")
        told.assert_not_called()
        with self.assertRaisesRegex(ValueError, "already released or accepted"):
            board.close_released_administratively(self.root, None, "GIT-MODEL", self.base_commit, REASON)

    def test_the_command_surface_allows_only_the_cto(self):
        self.assertEqual(board_surface.AUTHORIZATION_MATRIX["close-released"], frozenset({"cto"}))
        self.assertIn("close-released", board_surface.AGENT_ARGUMENT_OPERATIONS, "an agent session always acts as itself")


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Backlog #11 (2026-09-27): control-plane holds end with their task, and the card names the held task.

The studio card said "Needs repair" for four days because a hold recorded on
2026-09-23 outlived its cancelled task, and it printed that hold's reason
under a different task (latest_task). Now cancelling, accepting or releasing a
task closes its open hold; the Projects card ignores a hold whose task has
already ended; and the "Needs repair" line names the held task.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness import board, project_manager
from tests import test_board as _board_tests
from tests import test_reviewer_release as _release_tests

REASON = "The same operation was refused 5 times: status note is required and must be 240 characters or fewer"


class _Fixture(unittest.TestCase):
    setUp = _board_tests.BoardTests.setUp
    tearDown = _board_tests.BoardTests.tearDown
    delivery = _board_tests.BoardTests.delivery
    accept = _release_tests.AcceptanceTests.accept

    def hold(self, task: str) -> None:
        board.record_control_plane_hold(self.root, task, "repeated_refusal:status", REASON)

    def holds(self) -> dict:
        return board.snapshot(self.root).get("control_plane_holds", {})

    def cancel(self, task: str, dev: dict) -> None:
        # Backlog #10 moves cancelling from Stop to Cancel task; either one
        # emits task_cancelled, which is what closes the hold.
        if hasattr(board, "cancel_task"):
            board.cancel_task(self.root, task)
        else:
            board.cancel_session_work(self.root, dev["session_id"])


class HoldsEndWithTheirTaskTests(_Fixture):
    """Criterion 1."""

    def assertClosed(self, task: str, outcome: str):
        hold = self.holds()[task]
        self.assertEqual((hold["status"], hold.get("outcome")), ("resolved", outcome), json.dumps(hold, indent=2))
        events = [e for e in board.snapshot(self.root)["events"] if e["kind"] == "control_plane_hold_resolved" and e["task"] == task]
        self.assertEqual([e["outcome"] for e in events], [outcome])

    def test_cancelling_the_task_closes_its_hold_and_leaves_another(self):
        dev = self.delivery("FILM")
        self.delivery("OTHER")
        self.hold("FILM")
        self.hold("OTHER")
        self.cancel("FILM", dev)
        self.assertClosed("FILM", "Resolved: task was cancelled")
        self.assertEqual(self.holds()["OTHER"]["status"], "open")

    def test_accepting_the_task_closes_its_hold(self):
        self.delivery("FILM")
        self.hold("FILM")
        self.accept("FILM")
        self.assertClosed("FILM", "Resolved: task was accepted")

    def test_releasing_the_task_closes_its_hold(self):
        self.delivery("FILM")
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        self.hold("FILM")
        board.record_release_ready(self.root, cto["id"], "FILM", {key: True for key in board.RELEASE_REQUIRED_CHECKS})
        self.assertClosed("FILM", "Resolved: task was released")

    def test_a_rejected_release_leaves_the_hold_open(self):
        self.delivery("FILM")
        self.hold("FILM")
        with board.locked_state(self.root) as state:
            board._event(state, "owner_release_decision_recorded", None, {"task": "FILM", "decision": "not_accepted"})
        self.assertEqual(self.holds()["FILM"]["status"], "open")

    def test_an_explicitly_cleared_hold_is_not_touched_again(self):
        dev = self.delivery("FILM")
        self.hold("FILM")
        board.clear_control_plane_hold(self.root, "FILM", "release_coordinator")
        self.cancel("FILM", dev)
        self.assertEqual(self.holds()["FILM"]["resolved_by"], "release_coordinator")


def _entry(base: Path, state: dict) -> dict:
    entry = {"data_root": str(base / "data"), "code_root": str(base), "workspace_root": str(base / "workspaces")}
    directory = Path(entry["data_root"]) / "board"
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps({"agents": {}, "events": [], **state}))
    return entry


def studio_state(held: str, *, cancelled: bool = False, accepted: bool = False, released: bool = False) -> dict:
    """The studio's shape: the hold is on one task, the newest brief on another."""
    return {
        "task_owner_directions": {held: "film", "content-planning": "plan"},
        "task_briefs": {
            held: {"update": "Old work.", "updated_at": "2026-09-23T10:00:00+00:00"},
            "content-planning": {"update": "Half way.", "updated_at": "2026-09-27T10:00:00+00:00"},
        },
        "control_plane_holds": {held: {"task": held, "status": "open", "reason": REASON, "recorded_at": "2026-09-23T19:31:53+00:00"}},
        "cancelled_tasks": {held: {"cancelled_at": "2026-09-24T00:00:00+00:00"}} if cancelled else {},
        "release_decisions": {held: {"decision": "accepted"}} if accepted else {},
        "releases": {held: {"task": held, "status": "VISUAL_TEST_REQUIRED"}} if released else {},
    }


class CardSummaryTests(unittest.TestCase):
    """Criteria 2 and 3, on the manager's project summary."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def summary(self, name: str, state: dict) -> dict:
        return project_manager.derive_status(_entry(self.base / name, state))

    def test_the_card_names_the_held_task_not_the_latest_one(self):
        row = self.summary("open", studio_state("creative-film"))
        self.assertEqual(row["latest_task"], "content-planning")
        self.assertEqual(row["control_plane_hold_task"], "creative-film")
        self.assertEqual(row["control_plane_hold"], REASON)

    def test_a_hold_on_a_task_that_already_ended_is_not_a_repair(self):
        for ended in ("cancelled", "accepted", "released"):
            with self.subTest(ended=ended):
                row = self.summary(ended, studio_state("creative-film", **{ended: True}))
                self.assertEqual((row["control_plane_hold"], row["control_plane_hold_task"]), ("", ""))


if __name__ == "__main__":
    unittest.main()

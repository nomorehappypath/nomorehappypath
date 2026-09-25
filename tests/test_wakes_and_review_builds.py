# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Wakes after a refusal or a cancelled review; build dependencies in review checkouts.

Defects #4, #10 and #11 of the 2026-09-25 marketing_agency run.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness import board, control


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class RefusalWakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        with board.locked_state(self.root) as state:
            state["agents"][self.delivery["id"]].update({
                "task": "WORK", "active": True, "last_poll_at": _ago(900), "last_progress_at": _ago(900), "spawned_at": _ago(900),
                "broker_refusal": {"operation": "git-commit", "reason": "replay refused: nonce 7 was already consumed", "kind": "transaction", "route": "run recover-git", "at": _ago(800)},
            })

    def test_a_transaction_refusal_is_routed_once_per_retry_window_with_its_reason(self):
        with patch("harness.control.enqueue_instruction", return_value={"id": "w-1", "source": "automatic-recovery"}) as enqueue:
            board.mark_stalled(self.root)
            self.assertEqual(enqueue.call_count, 1, "a refused Delivery must be woken")
            text = enqueue.call_args.args[2]
            self.assertIn("refused by the Git broker", text)
            self.assertIn("nonce 7 was already consumed", text)
            self.assertIn("recover-git", text)
            board.mark_stalled(self.root)
            self.assertEqual(enqueue.call_count, 1, "inside the retry window nothing more is routed")
        agent = board.snapshot(self.root)["agents"][self.delivery["id"]]
        self.assertEqual(agent["recovery_state"], "automatic_requested")
        self.assertIn("broker_refusal", agent)

    def test_an_unanswered_refusal_wake_stays_blocked_and_is_repeated_only_after_the_retry_window(self):
        """Reviewer finding, round 1: the generic timeout used to mark a refused agent stalled."""
        with patch("harness.control.enqueue_instruction", return_value={"id": "w-1", "source": "automatic-recovery"}) as enqueue:
            board.mark_stalled(self.root)
            self.assertEqual(enqueue.call_count, 1)
            # The wake goes unanswered past the grace period.
            with board.locked_state(self.root) as state:
                state["agents"][self.delivery["id"]]["automatic_recovery_requested_at"] = _ago(board.AUTO_RECOVERY_GRACE_SECONDS + 10)
            board.mark_stalled(self.root)
            agent = board.snapshot(self.root)["agents"][self.delivery["id"]]
            self.assertNotIn(agent.get("liveness"), {"stalled", "recovering"}, "a refused agent is blocked, never stalled")
            self.assertEqual(agent["recovery_state"], "blocked_wake_unanswered")
            self.assertIn("broker_refusal", agent)
            kinds = [event["kind"] for event in board.snapshot(self.root)["events"]]
            self.assertNotIn("agent_stalled", kinds)
            self.assertEqual(kinds.count("broker_refusal_wake_unanswered"), 1)
            self.assertEqual(enqueue.call_count, 1, "inside the retry window the wake is not repeated")
            # Past the retry window the refusal is routed again, once.
            with board.locked_state(self.root) as state:
                state["agents"][self.delivery["id"]]["automatic_recovery_requested_at"] = _ago(board.AUTO_RECOVERY_RETRY_SECONDS + 10)
            board.mark_stalled(self.root)
            self.assertEqual(enqueue.call_count, 2)
            agent = board.snapshot(self.root)["agents"][self.delivery["id"]]
            self.assertEqual(agent["recovery_state"], "automatic_requested")
            self.assertNotIn(agent.get("liveness"), {"stalled", "recovering"})
        board.poll(self.root, self.delivery["id"])
        agent = board.snapshot(self.root)["agents"][self.delivery["id"]]
        self.assertEqual(agent["recovery_state"], "resumed")


class ReviewWakeWithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.session = control.create(self.root, "claude_reviewer")
        self.reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=self.session["id"])

    def inbox(self) -> list[dict]:
        with control.locked_state(self.root) as state:
            return list((state.get("inbox") or {}).get(self.session["id"]) or [])

    def test_a_still_queued_wake_is_withdrawn_before_the_terminal_sees_it(self):
        queued = control.enqueue_instruction(self.root, self.session["id"], "REVIEW ACTION DUE: claim review-X", "review-assignment")
        outcome = board._withdraw_reviewer_wake(self.root, {
            "request_id": "review-X", "task": "WORK", "instruction_id": queued["id"],
            "session_id": self.session["id"], "reviewer_id": self.reviewer["id"], "reason": "unit tests failed",
        })
        self.assertEqual(outcome["action"], "withdrawn")
        self.assertEqual(self.inbox(), [])
        self.assertEqual(control.instruction_receipt(self.root, queued["id"])["status"], "withdrawn")
        events = [e for e in board.snapshot(self.root)["events"] if e["kind"] == "review_wake_withdrawn"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "withdrawn")

    def test_a_wake_already_taken_gets_a_one_line_cancellation_notice(self):
        queued = control.enqueue_instruction(self.root, self.session["id"], "REVIEW ACTION DUE: claim review-Y", "review-assignment")
        taken = control.take_instructions(self.root, self.session["id"])
        self.assertEqual([item["id"] for item in taken], [queued["id"]])
        outcome = board._withdraw_reviewer_wake(self.root, {
            "request_id": "review-Y", "task": "WORK", "instruction_id": queued["id"],
            "session_id": self.session["id"], "reviewer_id": self.reviewer["id"], "reason": "unit tests failed",
        })
        self.assertEqual(outcome["action"], "notified")
        notices = self.inbox()
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0]["text"].startswith("REVIEW CANCELLED: request review-Y for WORK was withdrawn"))
        self.assertIn("unit tests failed", notices[0]["text"])
        self.assertEqual(notices[0]["source"], "review-cancelled")
        self.assertEqual(control.instruction_receipt(self.root, queued["id"])["status"], "taken")


class LinkedBuildDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.workspace / ".gitignore").write_text("node_modules/\n.harness/\n", encoding="utf-8")
        (self.workspace / "frontend").mkdir()
        (self.workspace / "frontend" / "package.json").write_text('{"name": "app", "scripts": {"build": "vite build"}}\n', encoding="utf-8")
        (self.workspace / "ledger.md").write_text("| ID |\n|---|\n| S-1 |\n", encoding="utf-8")
        self.git("add", ".gitignore", "frontend/package.json", "ledger.md")
        self.git("commit", "-q", "-m", "app")
        self.commit = self.git("rev-parse", "HEAD").strip()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", *arguments], cwd=self.workspace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def checkout(self):
        state = {"task_workspaces": {"WORK": str(self.workspace)}, "subtask_workspaces": {}}
        request = {"task": "WORK", "subtask": "", "reviewed_commit": self.commit}
        return board._review_candidate_checkout(self.root, state, request, self.workspace / "ledger.md")

    def test_the_workspace_node_modules_is_linked_into_the_archive_checkout(self):
        modules = self.workspace / "frontend" / "node_modules"
        (modules / ".bin").mkdir(parents=True)
        (modules / ".bin" / "vite").write_text("#!/bin/sh\necho vite\n", encoding="utf-8")
        with self.checkout() as (checkout, ledger):
            linked = checkout / "frontend" / "node_modules"
            self.assertTrue(linked.is_symlink(), "node_modules was not linked")
            self.assertEqual(linked.resolve(), modules.resolve())
            self.assertTrue((linked / ".bin" / "vite").is_file())
            note = (checkout / ".harness-linked-dependencies").read_text(encoding="utf-8")
            self.assertIn("frontend/node_modules -> ", note)
            self.assertTrue(ledger.is_file())
            self.assertFalse((checkout / ".git").exists())

    def test_nothing_is_linked_when_the_workspace_has_no_node_modules(self):
        with self.checkout() as (checkout, _):
            self.assertFalse((checkout / "frontend" / "node_modules").exists())
            self.assertFalse((checkout / ".harness-linked-dependencies").exists())


if __name__ == "__main__":
    unittest.main()

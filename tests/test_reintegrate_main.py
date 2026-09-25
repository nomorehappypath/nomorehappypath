# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Re-integration when main moves (defects #17, #18, #19 of 2026-09-25).

A task branched from main, another task was accepted (main moved), and the
first task's final review passed on the stale base before anyone noticed.
The coordinator then logged an incident every cycle, the finished Delivery
was never woken, and no governed command could merge main into the branch.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness import board, contract, control, git_broker, release_coordinator
from harness.git_broker import AuthorizationError, BrokerError
from tests import test_git_broker, test_git_model_board
from tests.requirements_support import agreed_requirements


def _own_tests_only(subclass: type, parent: type) -> type:
    for name in dir(parent):
        if name.startswith("test") and name not in vars(subclass):
            setattr(subclass, name, None)
    return subclass


def _commit_on_main(git, repository: Path, path: str, text: str, message: str) -> str:
    (repository / path).write_text(text, encoding="utf-8")
    git(repository, "add", path)
    git(repository, "-c", "user.name=Owner", "-c", "user.email=owner@example.invalid", "commit", "-m", message)
    return git(repository, "rev-parse", "HEAD").strip()


class BrokerReintegrateMainTests(test_git_broker.GitBrokerTests):
    """The broker operation itself: clean merge, conflicts, finish, refusals."""

    def start_task_branch(self):
        created, workspace = self.create_task_workspace()
        # The board records the branch when it begins a task; the raw fixture does not.
        self.state.setdefault("task_branches", {})["TASK"] = dict(created)
        (workspace / "product.txt").write_text("task work\n", encoding="utf-8")
        committed = self.broker.stage_commit("delivery", 2, ["product.txt"], "task work")
        return workspace, committed

    def test_a_clean_merge_is_one_full_merge_commit_and_the_worktree_stays_clean(self):
        workspace, committed = self.start_task_branch()
        main = _commit_on_main(self._git, self.repository, "other.txt", "accepted elsewhere\n", "another task accepted")
        result = self.broker.reintegrate_main("delivery", 3)
        self.assertEqual(result["status"], "merged")
        self.assertEqual(result["main"], main)
        parents = self._git(workspace, "show", "-s", "--format=%P", result["commit"]).split()
        self.assertEqual(parents, [committed["commit"], main])
        self.assertEqual(self._git(workspace, "status", "--porcelain"), "")
        self.assertEqual((workspace / "other.txt").read_text(encoding="utf-8"), "accepted elsewhere\n")
        self.assertEqual((workspace / "product.txt").read_text(encoding="utf-8"), "task work\n")
        self._git(self.repository, "merge-base", "--is-ancestor", main, result["commit"])
        # main itself is untouched by the re-integration
        self.assertEqual(self._git(self.repository, "rev-parse", "main").strip(), main)

    def test_conflicts_are_left_in_the_worktree_and_finished_with_a_full_commit(self):
        workspace, committed = self.start_task_branch()
        main = _commit_on_main(self._git, self.repository, "product.txt", "main changed the same line\n", "conflicting main")
        result = self.broker.reintegrate_main("delivery", 3)
        self.assertEqual(result["status"], "conflicts")
        self.assertEqual(result["conflicts"], ["product.txt"])
        self.assertIn("<<<<<<<", (workspace / "product.txt").read_text(encoding="utf-8"))
        self.assertEqual(self._git(workspace, "rev-parse", "-q", "--verify", "MERGE_HEAD").strip(), main)
        with self.assertRaisesRegex(BrokerError, "already in progress"):
            self.broker.reintegrate_main("delivery", 4)
        with self.assertRaisesRegex(BrokerError, "conflict markers are still present in: product.txt"):
            self.broker.reintegrate_main("delivery", 5, finish=True)
        # Delivery resolves the file in the worktree; it cannot touch the index.
        (workspace / "product.txt").write_text("resolved by delivery\n", encoding="utf-8")
        finished = self.broker.reintegrate_main("delivery", 6, finish=True)
        self.assertEqual(finished["status"], "merged")
        parents = self._git(workspace, "show", "-s", "--format=%P", finished["commit"]).split()
        self.assertEqual(parents, [committed["commit"], main])
        self.assertEqual(self._git(workspace, "status", "--porcelain"), "")
        self.assertEqual(self._git(workspace, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).strip(), "")
        self.assertEqual(self._git(workspace, "show", "HEAD:product.txt"), "resolved by delivery\n")

    def test_up_to_date_branch_needs_no_merge_and_finish_without_a_merge_is_refused(self):
        workspace, committed = self.start_task_branch()
        result = self.broker.reintegrate_main("delivery", 3)
        self.assertEqual(result["status"], "up_to_date")
        self.assertEqual(result["commit"], committed["commit"])
        with self.assertRaisesRegex(BrokerError, "no re-integration merge is in progress"):
            self.broker.reintegrate_main("delivery", 4, finish=True)

    def test_a_dirty_worktree_and_a_non_delivery_role_are_refused(self):
        workspace, _ = self.start_task_branch()
        _commit_on_main(self._git, self.repository, "other.txt", "x\n", "main moved")
        (workspace / "scratch.txt").write_text("unsaved\n", encoding="utf-8")
        with self.assertRaisesRegex(git_broker.RecoveryHoldError, "must be clean"):
            self.broker.reintegrate_main("delivery", 3)
        (workspace / "scratch.txt").unlink()
        with self.assertRaises(AuthorizationError):
            self.broker.reintegrate_main("cto", 4)


_own_tests_only(BrokerReintegrateMainTests, test_git_broker.GitBrokerTests)


class BoardReintegrationTests(test_git_model_board.GitModelBoardIntegrationTests):
    """Accept marks the other task, final review is refused, Delivery merges through the board."""

    def second_task(self, name: str, kind: str = "codex_delivery"):
        session = control.create(self.root, kind)
        delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], f"Implement {name} in the same repository.")
        begun = board.begin_task(self.root, delivery["id"], name)
        contract.create_contract(self.root, name, f"Implement {name} in the same repository.", ["governed change"])
        agreed_requirements(self.root, delivery["id"], f"Implement and verify {name}.")
        board.define_delivery_plan(self.root, delivery["id"], "atomic", f"{name} fixture")
        return delivery, session, begun

    def queued_instructions(self, session_id: str) -> list[dict]:
        with control.locked_state(self.root) as state:
            return list((state.get("inbox") or {}).get(session_id) or [])

    def test_accepting_one_task_marks_the_other_open_task_and_wakes_its_delivery(self):
        second, second_session, second_begun = self.second_task("SECOND")
        committed = self.certified_candidate()
        response = board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertEqual(response["git_acceptance"]["commit"], committed["commit"])
        state = board.snapshot(self.root)
        marker = state["git_reintegration_required"]["SECOND"]
        self.assertIn("main advanced", marker["reason"])
        self.assertIn("A newer version of main was accepted", marker["owner_line"])
        self.assertEqual(marker["delivery"], "active")
        self.assertTrue(marker["delivery_notified_at"])
        self.assertNotIn("GIT-MODEL", state["git_reintegration_required"])
        kinds = [event["kind"] for event in state["events"] if event.get("task") == "SECOND"]
        self.assertEqual(kinds.count("git_reintegration_required"), 1)
        queued = self.queued_instructions(second_session["id"])
        self.assertTrue(any("reintegrate-main" in item["text"] for item in queued), queued)

        # A task begun after main moved already contains main and is left alone
        # (the accepted task's terminal is stopped first: two Codex seats only).
        control.stop(self.root, str(self.delivery["session_id"]))
        third, _, _ = self.second_task("THIRD")
        state = board.snapshot(self.root)
        self.assertNotIn("THIRD", state.get("git_reintegration_required", {}))

        # Final review on the stale base is refused before any evidence work.
        with self.assertRaisesRegex(ValueError, "reintegrate-main"):
            board.request_review(
                self.root, second["id"], "ledger.md", "final", phase="final_acceptance",
                test_command="python3 -m unittest",
            )
        state = board.snapshot(self.root)
        self.assertEqual([r for r in state["qa_requests"].values() if r.get("task") == "SECOND"], [])

        # Delivery merges main in through the board; the marker clears.
        merged = board.broker_reintegrate_main(self.root, second["id"])
        self.assertEqual(merged["status"], "merged")
        state = board.snapshot(self.root)
        self.assertNotIn("SECOND", state["git_reintegration_required"])
        self.assertEqual(state["task_branches"]["SECOND"]["head"], merged["commit"])
        kinds = [event["kind"] for event in state["events"] if event.get("task") == "SECOND"]
        self.assertIn("git_reintegration_completed", kinds)
        workspace = Path(second_begun["task_workspace"])
        self.assertEqual((workspace / "product.txt").read_text(encoding="utf-8"), "accepted\n")
        self.assertEqual(self.git("status", "--porcelain", cwd=workspace), "")
        # The stale-base refusal is gone; the next refusal is the ordinary ledger check.
        with self.assertRaises(ValueError) as caught:
            board.request_review(
                self.root, second["id"], "ledger.md", "final", phase="final_acceptance",
                test_command="python3 -m unittest",
            )
        self.assertNotIn("reintegrate-main", str(caught.exception))

    def test_an_unmarked_stale_base_is_still_refused_at_final_review_and_marked(self):
        second, _, _ = self.second_task("SECOND")
        self.git("checkout", "-q", "main")
        (self.root / "moved.txt").write_text("main moved by hand\n", encoding="utf-8")
        self.git("add", "moved.txt")
        self.git("commit", "-q", "-m", "main moved outside the harness")
        with self.assertRaisesRegex(ValueError, "reintegrate-main"):
            board.request_review(
                self.root, second["id"], "ledger.md", "final", phase="final_acceptance",
                test_command="python3 -m unittest",
            )
        state = board.snapshot(self.root)
        self.assertEqual(state["git_reintegration_required"]["SECOND"]["source"], "request-review")


_own_tests_only(BoardReintegrationTests, test_git_model_board.GitModelBoardIntegrationTests)


class RouteReintegrationTests(unittest.TestCase):
    """The wake path: active, reactivated, or an owner line to relaunch."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        with board.locked_state(self.root) as state:
            state["agents"]["dev"] = {
                "id": "dev", "role": "engineering", "task": "STALE", "active": False,
                "status": "done", "session_id": "s-dev", "liveness": "healthy", "write_authority": True,
                "poll_counter": 0, "status_note": "done", "last_status_at": board.now(),
            }

    def events(self, kind: str) -> int:
        return sum(1 for event in board.snapshot(self.root)["events"] if event["kind"] == kind)

    def test_a_finished_delivery_with_a_live_terminal_is_reactivated_once(self):
        with patch.object(board, "_managed_session_is_live", return_value=True), \
                patch("harness.control.enqueue_instruction", return_value={"id": "wake-1"}) as enqueue:
            marker = board.route_reintegration(self.root, "STALE", "main moved", source="release-coordinator")
            again = board.route_reintegration(self.root, "STALE", "main moved", source="release-coordinator")
        self.assertEqual(marker["delivery"], "reactivated")
        self.assertEqual(marker["instruction_id"], "wake-1")
        self.assertEqual(again["instruction_id"], "wake-1")
        self.assertEqual(enqueue.call_count, 1)
        self.assertIn("reintegrate-main", enqueue.call_args.args[2])
        state = board.snapshot(self.root)
        self.assertTrue(state["agents"]["dev"]["active"])
        self.assertEqual(self.events("git_reintegration_required"), 1)
        self.assertEqual(self.events("agent_reactivated_for_reintegration"), 1)

    def test_a_dead_delivery_terminal_yields_one_owner_line_and_no_wake(self):
        with patch.object(board, "_managed_session_is_live", return_value=False), \
                patch("harness.control.enqueue_instruction") as enqueue:
            marker = board.route_reintegration(self.root, "STALE", "main moved", source="release-coordinator")
            board.route_reintegration(self.root, "STALE", "main moved", source="release-coordinator")
        self.assertEqual(marker["delivery"], "relaunch_required")
        self.assertIn("no longer running", marker["owner_line"])
        self.assertIn("resume this task", marker["owner_line"])
        enqueue.assert_not_called()
        self.assertFalse(board.snapshot(self.root)["agents"]["dev"]["active"])
        self.assertEqual(self.events("git_reintegration_required"), 1)


class CoordinatorStaleBaseTests(unittest.TestCase):
    """A stale base after development_complete is routed once, never an incident per cycle."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "board-root"
        self.root.mkdir()
        self.repo = self.base / "product"
        self.repo.mkdir()
        for arguments in (["init", "-q", "-b", "main"], ["config", "user.email", "h@example.invalid"], ["config", "user.name", "H"]):
            subprocess.run(["git", *arguments], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "base.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.reviewed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        with board.locked_state(self.root) as state:
            state["qa_requests"]["final-STALE"] = {
                "id": "final-STALE", "task": "STALE", "status": "passed",
                "phase": "final_acceptance", "stage": "independent_review",
                "cycle": 1, "structure_revision": 0, "subtask": "", "chunk": "",
                "developer_id": "dev", "claimed_by": "qa", "mirror_ref": "refs/harness/STALE/reviewed-1",
                "review_wait_started_at": "", "requested_at": board.now(),
                "reviewed_commit": self.reviewed,
            }
            state["task_workspaces"]["STALE"] = str(self.repo)
            state.setdefault("task_repositories", {})["STALE"] = str(self.repo)
            state["agents"]["dev"] = {
                "id": "dev", "role": "engineering", "task": "STALE", "active": False,
                "status": "done", "session_id": "s-dead", "liveness": "healthy", "write_authority": True,
                "poll_counter": 0, "status_note": "done", "last_status_at": board.now(),
            }
            board._event(state, "development_complete", state["agents"]["dev"], {"task": "STALE", "message": "done"})

    def test_stale_base_is_marked_once_and_incidents_are_not_repeated(self):
        checks = {key: True for key in board.BROKER_RELEASE_REQUIRED_CHECKS}
        checks["main_fast_forward_safe"] = False
        checks["main_unchanged_before_accept"] = False
        with patch.object(release_coordinator.cto, "release_check", return_value=checks), \
                patch.object(board, "_managed_session_is_live", return_value=False):
            first = release_coordinator.coordinate(self.root)
            second = release_coordinator.coordinate(self.root)
        self.assertEqual(first[0]["status"], "reintegration_required")
        self.assertEqual(second[0]["status"], "unchanged_checks_failed")
        state = board.snapshot(self.root)
        marker = state["git_reintegration_required"]["STALE"]
        self.assertEqual(marker["source"], "release-coordinator")
        self.assertEqual(marker["delivery"], "relaunch_required")
        kinds = [event["kind"] for event in state["events"]]
        self.assertEqual(kinds.count("git_reintegration_required"), 1)
        self.assertNotIn("control_plane_incident", kinds)
        self.assertNotIn("STALE", state.get("control_plane_holds", {}))


if __name__ == "__main__":
    unittest.main()

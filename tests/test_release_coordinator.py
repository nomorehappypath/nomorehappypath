# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Release-coordinator sims (efficiency item 2; owner bar: no happy path).

The two recorded deadlocks are replayed as fixtures: a task with a PASSED
final acceptance whose delivery concluded with commits unpushed used to
strand forever (task_b 04:03Z, TASK_C 16:43Z, both 2026-08-16). The
coordinator must resolve both with zero rescue routing, be idempotent under
rerun, refuse divergence, bound its retries, and classify every failure as a
control-plane incident — never a product repair.

Run:  PYTHONPATH=. python3 -m unittest tests.test_release_coordinator -v
"""
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, release_coordinator


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()


class ReleaseCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "board-root"
        self.root.mkdir()
        # A governed repository with a bare origin, one reviewed commit ahead.
        self.repo = self.base / "product"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "h@example.invalid")
        _git(self.repo, "config", "user.name", "H")
        (self.repo / "base.txt").write_text("base\n")
        _git(self.repo, "add", "base.txt")
        _git(self.repo, "commit", "-qm", "base")
        self.origin = self.base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        _git(self.repo, "push", "-q", "origin", "main")
        (self.repo / "delivered.txt").write_text("reviewed work\n")
        _git(self.repo, "add", "delivered.txt")
        _git(self.repo, "commit", "-qm", "reviewed work")
        self.reviewed = _git(self.repo, "rev-parse", "HEAD")

    def _strand(self, task: str) -> None:
        """Reproduce the recorded deadlock shape on the board."""
        with board.locked_state(self.root) as state:
            state["qa_requests"][f"final-{task}"] = {
                "id": f"final-{task}", "task": task, "status": "passed",
                "phase": "final_acceptance", "stage": "independent_review",
                "cycle": 1, "structure_revision": 0, "subtask": "", "chunk": "",
                "developer_id": "dev", "claimed_by": "qa",
                "review_wait_started_at": "", "requested_at": board.now(),
                "reviewed_commit": self.reviewed,
            }
            state["task_workspaces"][task] = str(self.repo)
            # Delivery concluded and inactive — the exact stranding condition.
            state["agents"]["dev"] = {
                "id": "dev", "role": "engineering", "task": task,
                "active": False, "status": "done", "session_id": "s-dead",
                "poll_counter": 0, "status_note": "done with commits unpushed",
                "last_status_at": board.now(),
            }
            board._event(state, "development_complete", state["agents"]["dev"],
                         {"task": task, "message": "done with commits unpushed"})

    def test_prepared_release_routes_once_per_unchanged_cto_session(self):
        state = board._initial_state()
        state["agents"]["cto"] = {
            "id": "cto", "role": "cto", "task": "GLOBAL_MONITOR",
            "active": True, "session_id": "cto-session-a",
        }
        checks_path = self.base / "checks.json"
        checks_path.write_text("{}\n")
        with patch("harness.control.enqueue_instruction") as enqueue:
            enqueue.side_effect = [{"id": "route-a"}, {"id": "route-b"}]
            self.assertTrue(release_coordinator._route_prepared(
                self.root, state, "TASK-ROUTE", self.reviewed,
                checks_path, "coordination-key",
            ))
            self.assertTrue(release_coordinator._route_prepared(
                self.root, state, "TASK-ROUTE", self.reviewed,
                checks_path, "coordination-key",
            ))
            self.assertEqual(enqueue.call_count, 1)
            instruction = enqueue.call_args.args[2]
            self.assertIn("omit --health-command", instruction)
            self.assertIn("Do not rerun certified product tests", instruction)

            state["agents"]["cto"]["session_id"] = "cto-session-b"
            self.assertTrue(release_coordinator._route_prepared(
                self.root, state, "TASK-ROUTE", self.reviewed,
                checks_path, "coordination-key",
            ))
            self.assertEqual(enqueue.call_count, 2)

    # ---- S-COORD-001: release work advances without an automatic remote mutation ----
    def test_task_c_deadlock_replay_routes_release_without_remote_push(self):
        self._strand("TASK_C_REPLAY")
        remote_before = _git(self.origin, "rev-parse", "main")
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        with patch.object(release_coordinator.cto, "release_check", return_value=checks):
            outcomes = release_coordinator.coordinate(self.root)
        self.assertEqual(_git(self.origin, "rev-parse", "main"), remote_before)
        journal = release_coordinator._journal_records(self.root, "TASK_C_REPLAY", self.reviewed)
        self.assertTrue(any(r.get("step") == "push_authorization" and r.get("status") == "owner_authorization_required" for r in journal))
        self.assertEqual(outcomes[0]["status"], "prepared")

    # ---- S-COORD-002: the task_b variant — machine slept, coordinator finds it late ----
    def test_task_b_deadlock_replay_after_delay_still_resolves(self):
        self._strand("TASK_B_REPLAY")
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        before = _git(self.origin, "rev-parse", "main")
        with patch.object(release_coordinator.cto, "release_check", return_value=checks):
            release_coordinator.coordinate(self.root)
            outcomes = release_coordinator.coordinate(self.root)
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertEqual(outcomes[0]["status"], "already_prepared")

    # ---- S-COORD-003: divergence is refused and classified, never overridden ----
    def test_remote_divergence_is_never_contacted_or_overridden(self):
        self._strand("TASK_DIVERGED")
        # An external commit lands on origin that local main does not contain.
        clone = self.base / "external"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(clone)], check=True)
        _git(clone, "config", "user.email", "x@example.invalid")
        _git(clone, "config", "user.name", "X")
        (clone / "external.txt").write_text("external\n")
        _git(clone, "add", "external.txt")
        _git(clone, "commit", "-qm", "external commit")
        _git(clone, "push", "-q", "origin", "main")
        external_tip = _git(self.origin, "rev-parse", "main")
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        with patch.object(release_coordinator.cto, "release_check", return_value=checks):
            release_coordinator.coordinate(self.root)
        self.assertEqual(_git(self.origin, "rev-parse", "main"), external_tip,
                         "an external commit is NEVER overridden")

    # ---- S-COORD-004: an unreachable remote is not contacted automatically ----
    def test_unreachable_remote_is_not_contacted(self):
        self._strand("TASK_PUSHFAIL")
        # Make origin unreachable by pointing the remote at a void path.
        _git(self.repo, "remote", "set-url", "origin", str(self.base / "missing.git"))
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        with patch.object(release_coordinator.cto, "release_check", return_value=checks):
            outcomes = release_coordinator.coordinate(self.root)
        journal = release_coordinator._journal_records(self.root, "TASK_PUSHFAIL", self.reviewed)
        self.assertTrue(any(r.get("step") == "push_authorization" for r in journal))
        self.assertEqual(outcomes[0]["status"], "prepared")

    # ---- S-COORD-005: a cancelled task is never coordinated ----
    def test_cancelled_task_is_ignored(self):
        self._strand("TASK_CANCELLED")
        with board.locked_state(self.root) as state:
            state.setdefault("cancelled_tasks", {})["TASK_CANCELLED"] = {
                "task": "TASK_CANCELLED", "cancelled_at": board.now(), "reason": "owner"}
        before = _git(self.origin, "rev-parse", "main")
        outcomes = release_coordinator.coordinate(self.root)
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertEqual(outcomes, [])

    # ---- S-COORD-006: a recorded release is never re-coordinated ----
    def test_recorded_release_is_left_alone(self):
        self._strand("TASK_RELEASED")
        with board.locked_state(self.root) as state:
            state["releases"]["TASK_RELEASED"] = {
                "task": "TASK_RELEASED", "status": "VISUAL_TEST_REQUIRED",
                "head_commit": self.reviewed, "cto_id": "cto", "recorded_at": board.now()}
        outcomes = release_coordinator.coordinate(self.root)
        self.assertEqual(outcomes, [])

    # ---- S-COORD-007: mechanical check failure is an incident, CTO is not engaged ----
    def test_checks_failure_is_classified_and_stops(self):
        self._strand("TASK_BADCHECKS")
        outcomes = release_coordinator.coordinate(self.root)
        # This fixture has no contract/ledger/confirmation, so mechanical
        # checks legitimately fail — the coordinator must classify and stop,
        # never enqueue the CTO or mark prepared.
        self.assertEqual(outcomes[0]["status"], "checks_failed")
        journal = release_coordinator._journal_records(self.root, "TASK_BADCHECKS", self.reviewed)
        self.assertFalse(any(r.get("step") == "prepared" for r in journal))
        state = board.snapshot(self.root)
        self.assertTrue(any(e.get("kind") == "control_plane_incident"
                            for e in state.get("events", [])))

    def test_broker_governed_candidate_never_contacts_remote(self):
        task = "TASK_BROKER_NO_PUSH"
        self._strand(task)
        with board.locked_state(self.root) as state:
            request = state["qa_requests"][f"final-{task}"]
            request["mirror_ref"] = f"refs/harness/{task}/reviewed-1"
            state.setdefault("task_repositories", {})[task] = str(self.repo)
        checks = {key: True for key in board.BROKER_RELEASE_REQUIRED_CHECKS}
        with patch.object(release_coordinator.cto, "release_check", return_value=checks):
            outcomes = release_coordinator.coordinate(self.root)
        self.assertEqual(outcomes[0]["status"], "prepared")
        journal = release_coordinator._journal_records(self.root, task, self.reviewed)
        self.assertTrue(any(
            row.get("step") == "push_authorization"
            and row.get("status") == "owner_authorization_required"
            for row in journal
        ))

    def test_unchanged_failed_checks_do_not_repeat_expensive_release_health(self):
        task = "TASK-BOUNDED-FAILURE"
        self._strand(task)
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        checks["main_health_verified"] = False
        with patch.object(
            release_coordinator.cto, "release_check", return_value=checks,
        ) as release_check:
            first = release_coordinator.coordinate(self.root)
            second = release_coordinator.coordinate(self.root)
        self.assertEqual(first[0]["status"], "checks_failed")
        self.assertEqual(second[0]["status"], "unchanged_checks_failed")
        release_check.assert_called_once()
        final = board.snapshot(self.root)["qa_requests"][f"final-{task}"]
        self.assertEqual(final["status"], "passed")

    def test_concurrent_controller_ticks_are_single_flight(self):
        task = "TASK-SINGLE-FLIGHT-COORDINATOR"
        self._strand(task)
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        outcomes = []

        def slow_check(*_args, **_kwargs):
            time.sleep(0.1)
            return checks

        with patch.object(
            release_coordinator.cto, "release_check", side_effect=slow_check,
        ) as release_check:
            threads = [threading.Thread(
                target=lambda: outcomes.append(release_coordinator.coordinate(self.root))
            ) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
        self.assertEqual(release_check.call_count, 1)
        self.assertEqual(
            sorted(result[0]["status"] for result in outcomes),
            ["already_prepared", "prepared"],
        )


if __name__ == "__main__":
    unittest.main()


class CoordinatorKillMatrixTests(ReleaseCoordinatorTests):
    """Owner bar: killed at every step boundary and mid-push, a rerun completes
    or cleanly reports — with zero duplicated side effects."""

    def _origin_main(self):
        return _git(self.origin, "rev-parse", "main")

    def _run_killed(self, task, **kill):
        with self.assertRaises(release_coordinator.InjectedCrash):
            release_coordinator.coordinate(self.root, **kill)

    def _push_count(self, task):
        journal = release_coordinator._journal_records(self.root, task, self.reviewed)
        return sum(1 for r in journal if r.get("step") == "push" and r.get("status") == "pushed")

    # ---- S-COORD-010: killed after begin — rerun completes fully ----
    def test_killed_after_begin_rerun_completes(self):
        self._strand("KILL_BEGIN")
        self._run_killed("KILL_BEGIN", crash_after="begin")
        release_coordinator.coordinate(self.root)
        self.assertNotEqual(self._origin_main(), self.reviewed)
        self.assertEqual(self._push_count("KILL_BEGIN"), 0)

    # ---- S-COORD-011: killed MID-PUSH (pushed, journal lost) — rerun re-verifies, no double act ----
    def test_killed_mid_push_rerun_verifies_reality(self):
        self._strand("KILL_MIDPUSH")
        self._run_killed("KILL_MIDPUSH", crash_before_journal="push")
        remote_before = self._origin_main()
        release_coordinator.coordinate(self.root)
        self.assertEqual(self._origin_main(), remote_before)
        journal = release_coordinator._journal_records(self.root, "KILL_MIDPUSH", self.reviewed)
        outcomes = [r.get("status") for r in journal if r.get("step") == "push_authorization"]
        self.assertIn("owner_authorization_required", outcomes)

    # ---- S-COORD-012: killed after push — rerun does not re-push ----
    def test_killed_after_push_rerun_is_idempotent(self):
        self._strand("KILL_PUSH")
        self._run_killed("KILL_PUSH", crash_after="push")
        release_coordinator.coordinate(self.root)
        self.assertEqual(self._push_count("KILL_PUSH"), 0)
        self.assertNotEqual(self._origin_main(), self.reviewed)

    # ---- S-COORD-013: killed after checks — rerun reaches a terminal state once ----
    def test_killed_after_checks_rerun_reaches_terminal_state(self):
        self._strand("KILL_CHECKS")
        self._run_killed("KILL_CHECKS", crash_after="checks")
        release_coordinator.coordinate(self.root)
        journal = release_coordinator._journal_records(self.root, "KILL_CHECKS", self.reviewed)
        checks_steps = [r for r in journal if r.get("step") == "checks"]
        self.assertGreaterEqual(len(checks_steps), 1)
        # This fixture legitimately fails mechanical checks; terminal state is
        # the classified incident, recorded exactly — not a prepared release.
        state = board.snapshot(self.root)
        self.assertTrue(any(e.get("kind") == "control_plane_incident" for e in state.get("events", [])))
        self.assertFalse(any(r.get("step") == "prepared" for r in journal))

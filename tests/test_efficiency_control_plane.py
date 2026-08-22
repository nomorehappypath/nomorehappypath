# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Adversarial tests for the quality-preserving control-plane efficiency fixes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from harness import board, child_process, control, control_plane, cto, repair_package
from tests.environment_support import require_sandbox_exec


class DeterministicEnvironmentTests(unittest.TestCase):
    def test_secrets_and_shell_routing_cannot_enter_governed_environment(self):
        hostile = {
            "HOME": "/tmp/harness-home", "USER": "owner", "LANG": "en_US.UTF-8",
            "OPENAI_API_KEY": "must-not-cross", "GIT_DIR": "/tmp/foreign.git",
            "PYTHONPATH": "/tmp/inject", "NODE_OPTIONS": "--require=/tmp/inject.js",
            "BASH_ENV": "/tmp/startup", "PATH": "/tmp/hostile-bin",
        }
        environment = child_process.execution_environment(hostile)
        for forbidden in (
            "OPENAI_API_KEY", "GIT_DIR", "PYTHONPATH", "NODE_OPTIONS", "BASH_ENV",
        ):
            self.assertNotIn(forbidden, environment)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(environment["PYTHONHASHSEED"], "0")
        self.assertNotIn("/tmp/hostile-bin", environment["PATH"])

    def test_irrelevant_ambient_changes_do_not_change_execution_environment(self):
        first = child_process.execution_environment({
            "HOME": "/tmp/home", "USER": "owner", "OPENAI_API_KEY": "one",
        })
        second = child_process.execution_environment({
            "HOME": "/tmp/home", "USER": "owner", "OPENAI_API_KEY": "two",
            "RANDOM_AGENT_SETTING": "different",
        })
        self.assertEqual(first, second)


class ExternalReleaseVerificationTests(unittest.TestCase):
    def setUp(self):
        require_sandbox_exec()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "external-product"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Harness"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=self.repo, check=True)
        (self.repo / "test_git_health.py").write_text(
            "import subprocess, unittest\n\n"
            "class GitHealth(unittest.TestCase):\n"
            "    def test_checkout_has_exact_git_metadata(self):\n"
            "        value = subprocess.check_output(['git', 'rev-parse', '--is-inside-work-tree'], text=True).strip()\n"
            "        self.assertEqual(value, 'true')\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "test_git_health.py"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "git health"], cwd=self.repo, check=True)
        self.commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True,
        ).strip()

    def test_health_runs_in_exact_git_checkout_not_metadata_free_archive(self):
        result = cto._task_artifact_gate(
            self.root, "EXTERNAL", self.repo,
            {"reviewed_commit": self.commit, "reviewed_files": ["test_git_health.py"]},
            True, "python3 -m unittest test_git_health", check_remote=False,
        )
        self.assertTrue(result["artifact_archive_verified"], result)
        self.assertTrue(result["artifact_health_verified"], result)
        self.assertTrue(result["task_artifact_release_verified"], result)

    def test_external_broker_runtime_never_probes_control_plane_server(self):
        policy = cto._runtime_verification_policy(
            broker_governed=True, runtime_candidate_changed=True,
        )
        self.assertFalse(policy["runtime_gate_required"])
        self.assertTrue(policy["runtime_verification_deferred_to_target_acceptance"])
        self.assertTrue(policy["runtime_verification_scope_correct"])
        legacy = cto._runtime_verification_policy(
            broker_governed=False, runtime_candidate_changed=True,
        )
        self.assertTrue(legacy["runtime_gate_required"])


class AtomicRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @staticmethod
    def _agent(agent_id: str, role: str, task: str, session_id: str, **values):
        timestamp = board.now()
        agent = {
            "id": agent_id, "role": role, "task": task,
            "display_name": agent_id, "vendor": "test",
            "spawned_at": timestamp, "last_poll_at": timestamp,
            "last_progress_at": timestamp, "poll_counter": 1,
            "last_status_at": timestamp, "status": "working",
            "status_note": "test fixture", "cursor": 0,
            "active": True, "liveness": "healthy",
            "liveness_note": "test fixture", "session_id": session_id,
        }
        agent.update(values)
        return agent

    def _stale_delivery_state(self) -> str:
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        source = {
            "id": "review-task-final-01", "task": "TASK", "phase": "final_acceptance",
            "subtask": "", "chunk": "", "reviewed_files": ["product.py"],
            "reviewed_commit": "a" * 40, "reviewed_tree_hash": "b" * 40,
        }
        package = repair_package.build("TASK", source, "A real review failure", [{
            "id": "FAIL", "summary": "The recovery boundary failed under interruption.",
            "category": "recovery", "affected_paths": ["product.py"],
            "surface": "final acceptance", "regression_check": "Repeat the crash boundary check.",
        }])
        repair_package.resolve(package, [{
            "id": "FAIL", "resolution": "Repaired the atomic state transition.",
            "regression_check": "The crash boundary now returns to review safely.",
        }], "delivery")
        package.update({
            "status": "under_review", "review_request_id": source["id"],
            "review_started_at": old,
        })
        repair_package.refresh_digest(package)
        with board.locked_state(self.root) as state:
            state["agents"]["delivery"] = self._agent(
                "delivery", "development", "TASK", "delivery-session",
                review_execution={
                    "active": True, "request_id": source["id"],
                    "last_heartbeat_at": old,
                },
            )
            state["agents"]["reviewer"] = self._agent(
                "reviewer", "qa", "TASK", "review-session",
            )
            state["repair_packages"][package["id"]] = package
            state["qa_requests"][source["id"]] = {
                **source, "cycle": 1, "stage": board.INDEPENDENT_REVIEW,
                "status": "reserved", "delivery_state": "executing",
                "developer_id": "delivery", "reserved_by": "reviewer",
                "claimed_by": None, "requested_at": old,
                "review_wait_started_at": old,
                "repair_package_id": package["id"],
            }
        return package["id"]

    def test_duplicate_recovery_is_single_effect_and_restores_repair_package(self):
        package_id = self._stale_delivery_state()
        results: list[list[dict]] = []
        threads = [
            threading.Thread(
                target=lambda: results.append(board.recover_interrupted_executions(self.root))
            ) for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        state = board.snapshot(self.root)
        self.assertNotIn("review-task-final-01", state["qa_requests"])
        self.assertEqual(len(state["delivery_attempt_failures"]), 1)
        package = state["repair_packages"][package_id]
        self.assertEqual(package["status"], "ready_for_review")
        self.assertNotIn("review_request_id", package)
        self.assertEqual(sum(len(value) for value in results), 1)

    def test_recent_delivery_command_exit_is_not_recovered_during_certification(self):
        self._stale_delivery_state()
        with board.locked_state(self.root) as state:
            state["agents"]["delivery"]["review_execution"].update({
                "active": False, "finished_at": board.now(), "result": "completed",
            })
        self.assertEqual(board.recover_interrupted_executions(self.root), [])
        self.assertIn("review-task-final-01", board.snapshot(self.root)["qa_requests"])

    def test_dead_reviewer_execution_reopens_without_transferring_its_ledger(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"]["reviewer"] = self._agent(
                "reviewer", "qa", "TASK", "dead",
                # The durable board can still look healthy after a terminal dies.
                # Recovery must consult the managed-session authority as well.
                active=True, liveness="healthy", status="qa_testing",
                review_execution={
                    "active": True, "request_id": "review-final-01",
                    "last_heartbeat_at": old,
                },
            )
            state["qa_requests"]["review-final-01"] = {
                "id": "review-final-01", "task": "TASK", "cycle": 1,
                "status": "claimed", "stage": board.INDEPENDENT_REVIEW,
                "phase": "final_acceptance", "developer_id": "delivery",
                "claimed_by": "reviewer", "reserved_by": "reviewer",
                "requested_at": old, "review_wait_started_at": old,
                "claimed_at": old, "challenge_ledger": "/tmp/reviewer-ledger.md",
                "challenge_ledger_sha256": "a" * 64,
                "challenge_execution_authorization": {"sha256": "b" * 64},
            }
        first = board.recover_interrupted_executions(self.root)
        second = board.recover_interrupted_executions(self.root)
        request = board.snapshot(self.root)["qa_requests"]["review-final-01"]
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        # A dead terminal with a durable reviewer record RESUMES: the claim,
        # ledger, and authorization stay with the same reviewer so completed
        # certified scenarios keep their identity. Nothing transfers.
        self.assertEqual(request["status"], "claimed")
        self.assertEqual(request["claimed_by"], "reviewer")
        self.assertEqual(request["challenge_ledger"], "/tmp/reviewer-ledger.md")
        self.assertEqual(request["route_state"], "challenge_interrupted_retry_required")
        self.assertNotIn("abandoned_challenge_ledgers", request)
        # A RETIRED reviewer still releases the request without transferring
        # its ledger to anyone.
        with board.locked_state(self.root) as state:
            state["agents"]["reviewer"]["active"] = False
            state["qa_requests"]["review-final-01"]["route_state"] = ""
            state["agents"]["reviewer"]["review_execution"]["active"] = True
            state["agents"]["reviewer"]["review_execution"]["last_heartbeat_at"] = old
        board.recover_interrupted_executions(self.root)
        request = board.snapshot(self.root)["qa_requests"]["review-final-01"]
        self.assertEqual(request["status"], "open")
        self.assertIsNone(request["challenge_ledger"])
        self.assertEqual(len(request["abandoned_challenge_ledgers"]), 1)
        self.assertNotIn("challenge_execution_authorization", request)

    def test_recent_reviewer_command_exit_is_not_reopened_during_certification(self):
        current = board.now()
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"]["reviewer"] = self._agent(
                "reviewer", "qa", "TASK", "",
                review_execution={
                    "active": False, "request_id": "review-final-settling",
                    "last_heartbeat_at": old, "finished_at": current,
                    "result": "completed",
                },
            )
            state["qa_requests"]["review-final-settling"] = {
                "id": "review-final-settling", "task": "TASK", "cycle": 1,
                "status": "claimed", "stage": board.INDEPENDENT_REVIEW,
                "phase": "final_acceptance", "developer_id": "delivery",
                "claimed_by": "reviewer", "reserved_by": "reviewer",
                "requested_at": old, "review_wait_started_at": old,
                "claimed_at": old, "challenge_ledger": "/tmp/reviewer-ledger.md",
                "challenge_ledger_sha256": "a" * 64,
            }
        self.assertEqual(board.recover_interrupted_executions(self.root), [])
        request = board.snapshot(self.root)["qa_requests"]["review-final-settling"]
        self.assertEqual(request["status"], "claimed")

    def test_prior_review_marker_cannot_interrupt_new_claim_and_recovery_is_idempotent(self):
        current = board.now()
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"]["reviewer"] = self._agent(
                "reviewer", "qa", "TASK", "",
                review_execution={
                    "active": False, "request_id": "review-final-prior",
                    "last_heartbeat_at": old, "finished_at": old,
                    "result": "completed",
                },
            )
            state["qa_requests"]["review-final-current"] = {
                "id": "review-final-current", "task": "TASK", "cycle": 2,
                "status": "claimed", "stage": board.INDEPENDENT_REVIEW,
                "phase": "final_acceptance", "developer_id": "delivery",
                "claimed_by": "reviewer", "reserved_by": "reviewer",
                "requested_at": old, "review_wait_started_at": old,
                "claimed_at": current, "challenge_ledger": "/tmp/current.md",
                "challenge_ledger_sha256": "c" * 64,
                "route_state": "executing_review",
            }

        self.assertEqual(board.recover_interrupted_executions(self.root), [])
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-final-current"]["claimed_at"] = old
        first = board.recover_interrupted_executions(self.root)
        second = board.recover_interrupted_executions(self.root)
        state = board.snapshot(self.root)
        request = state["qa_requests"]["review-final-current"]
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(request["route_state"], "challenge_interrupted_retry_required")
        self.assertEqual(len(request["challenge_execution_attempts"]), 1)
        self.assertEqual(
            state["agents"]["reviewer"]["review_execution"]["request_id"],
            "review-final-prior",
        )

    def test_corrupt_reservation_timestamp_reopens_once_without_crashing(self):
        with board.locked_state(self.root) as state:
            state["agents"]["reviewer"] = self._agent(
                "reviewer", "qa", "TASK", "missing-session",
                last_poll_at="not-a-time", last_progress_at="not-a-time",
            )
            state["qa_requests"]["review-final-corrupt"] = {
                "id": "review-final-corrupt", "task": "TASK", "cycle": 1,
                "status": "reserved", "stage": board.INDEPENDENT_REVIEW,
                "phase": "final_acceptance", "developer_id": "delivery",
                "claimed_by": None, "reserved_by": "reviewer",
                "requested_at": "not-a-time", "reserved_at": "not-a-time",
                "authoring_last_activity_at": "also-not-a-time",
                "review_wait_started_at": "not-a-time",
            }
        first = board.release_expired_review_reservations(self.root, 1)
        second = board.release_expired_review_reservations(self.root, 1)
        request = board.snapshot(self.root)["qa_requests"]["review-final-corrupt"]
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(request["status"], "open")
        self.assertIsNone(request["reserved_by"])

    def test_corrupt_agent_heartbeat_fails_stale_without_crashing_controller(self):
        with board.locked_state(self.root) as state:
            state["agents"]["delivery"] = self._agent(
                "delivery", "development", "TASK", "",
                spawned_at="bad", last_poll_at="bad", last_progress_at="bad",
                last_status_at="bad",
            )
        events = board.mark_stalled(self.root, 1)
        agent = board.snapshot(self.root)["agents"]["delivery"]
        self.assertTrue(events)
        self.assertEqual(agent["liveness"], "stalled")
        self.assertEqual(agent["recovery_state"], "automatic_failed")

    def test_control_plane_tick_owns_coordination_and_skips_everything_when_paused(self):
        with mock.patch.object(board, "recover_interrupted_executions", return_value=[]) as recover, \
                mock.patch.object(board, "route_open_reviews", return_value=[]) as reviews, \
                mock.patch.object(control_plane.release_coordinator, "coordinate", return_value=[]) as release, \
                mock.patch.object(board, "mark_stalled", return_value=[]) as stalled:
            report = control_plane.tick(self.root, 123)
            self.assertEqual(report["status"], "active")
            recover.assert_called_once_with(self.root)
            reviews.assert_called_once_with(self.root)
            release.assert_called_once_with(self.root)
            stalled.assert_called_once_with(self.root, 123)
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        with mock.patch.object(board, "recover_interrupted_executions") as recover:
            report = control_plane.tick(self.root)
            self.assertEqual(report["status"], "paused")
            recover.assert_not_called()


class PassPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        timestamp = board.now()
        with board.locked_state(self.root) as state:
            state["qa_requests"]["final-pass"] = {
                "id": "final-pass", "task": "TASK", "status": "passed",
                "result": "passed", "phase": "final_acceptance", "cycle": 1,
                "developer_id": "delivery", "review_wait_started_at": "",
                "requested_at": timestamp, "claimed_by": "reviewer",
            }

    def test_valid_pass_cannot_be_reopened_for_control_plane_reason(self):
        with mock.patch.object(board, "_request_integrity", return_value=(True, [])):
            with self.assertRaisesRegex(ValueError, "control-plane defect"):
                board.reopen_integrity_requests(
                    self.root, ["final-pass"], "release routing failed",
                )
        self.assertEqual(
            board.snapshot(self.root)["qa_requests"]["final-pass"]["status"], "passed",
        )

    def test_control_plane_hold_records_and_clears_without_mutating_pass(self):
        hold = board.record_control_plane_hold(
            self.root, "TASK", "release_coordinator:test", "target routing is unavailable",
        )
        self.assertEqual(hold["preserved_pass_request_ids"], ["final-pass"])
        board.clear_control_plane_hold(self.root, "TASK", "test")
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"]["final-pass"]["status"], "passed")
        self.assertEqual(state["control_plane_holds"]["TASK"]["status"], "resolved")

    def test_final_pass_routes_immediately_once_without_mutating_pass(self):
        session = control.create(self.root, "codex_delivery")
        with board.locked_state(self.root) as state:
            state["agents"]["delivery"] = AtomicRecoveryTests._agent(
                "delivery", "engineering", "TASK", session["id"],
            )
        first = control_plane._route_final_pass_completion(self.root)
        second = control_plane._route_final_pass_completion(self.root)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        queued = control.take_instructions(self.root, session["id"])
        self.assertEqual(len(queued), 1)
        self.assertIn("Do not rerun the final tests", queued[0]["text"])
        self.assertEqual(
            board.snapshot(self.root)["qa_requests"]["final-pass"]["status"],
            "passed",
        )


if __name__ == "__main__":
    unittest.main()

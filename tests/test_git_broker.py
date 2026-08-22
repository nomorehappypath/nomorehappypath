# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Executable contract tests for the trusted Git broker (spec §10)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from harness.git_broker import (
    AuthorizationError,
    BrokerError,
    FilterRequiredError,
    GitBroker,
    InjectedCrash,
    MainMovedError,
    RecoveryHoldError,
    ReplayError,
    ZERO_OID,
)
from harness.project_context import ProjectContext


class GitBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repository = self.base / "code"
        self.data = self.base / "data"
        self.workspaces = self.base / "workspaces"
        self.repository.mkdir()
        self._git(self.repository, "init", "-b", "main")
        (self.repository / "product.txt").write_text("base\n", encoding="utf-8")
        self._git(self.repository, "add", "product.txt")
        self._git(
            self.repository, "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "commit", "-m", "base",
        )
        self.base_commit = self._git(self.repository, "rev-parse", "HEAD").strip()
        self.base_tree = self._git(self.repository, "rev-parse", "HEAD^{tree}").strip()
        self.state = {
            "agents": {
                "delivery": {
                    "id": "delivery", "role": "engineering", "task": "TASK",
                    "active": True, "session_id": "session-delivery",
                    "write_authority": True,
                },
                "cto": {
                    "id": "cto", "role": "cto", "task": "GLOBAL_MONITOR",
                    "active": True, "session_id": "session-cto",
                },
            },
            "delivery_plans": {
                "TASK": {
                    "mode": "application",
                    "subtasks": {"alpha": {"status": "open"}, "beta": {"status": "open"}},
                },
            },
            "task_repositories": {"TASK": str(self.repository)},
            "task_workspaces": {},
            "subtask_workspaces": {},
            "task_baselines": {"TASK": {"head": self.base_commit, "tree": self.base_tree}},
            "qa_requests": {},
            "release_decisions": {},
            "git_acceptances": {},
            "remote_push_instructions": {},
            "approved_remotes": {},
        }
        self.context = ProjectContext(self.repository, self.data, self.workspaces)
        self.broker = GitBroker(self.context, state_loader=lambda: self.state)

    def tearDown(self):
        self.temporary.cleanup()

    def _git(self, cwd: Path, *arguments: str, check: bool = True) -> str:
        import subprocess
        result = subprocess.run(
            ["/usr/bin/git", *arguments], cwd=cwd, capture_output=True, text=True,
        )
        if check and result.returncode:
            self.fail(result.stderr or result.stdout)
        return result.stdout

    def isolated_fixture(self, name: str):
        repository = self.base / name / "code"
        repository.mkdir(parents=True)
        self._git(repository, "init", "-b", "main")
        (repository / "product.txt").write_text("base\n", encoding="utf-8")
        self._git(repository, "add", "product.txt")
        self._git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "base")
        base_commit = self._git(repository, "rev-parse", "HEAD").strip()
        state = {
            "agents": {"delivery": {"id": "delivery", "role": "engineering", "task": "TASK", "active": True, "session_id": f"session-{name}", "write_authority": True}},
            "delivery_plans": {"TASK": {"mode": "atomic", "subtasks": {}}},
            "task_repositories": {"TASK": str(repository)}, "task_workspaces": {},
            "subtask_workspaces": {}, "task_baselines": {"TASK": {"head": base_commit}},
            "qa_requests": {}, "release_decisions": {}, "git_acceptances": {},
            "remote_push_instructions": {}, "approved_remotes": {},
        }
        context = ProjectContext(repository, self.base / name / "data", self.base / name / "workspaces")
        broker = GitBroker(context, state_loader=lambda: state)
        created = broker.branch_create("delivery", 1)
        state["task_workspaces"]["TASK"] = created["workspace"]
        workspace = Path(created["workspace"])
        (workspace / "product.txt").write_text("candidate\n", encoding="utf-8")
        committed = broker.stage_commit("delivery", 2, ["product.txt"], "candidate")
        request = {"id": f"review-{name}", "task": "TASK", "reviewed_commit": committed["commit"], "reviewed_tree_hash": committed["tree"]}
        state["qa_requests"][request["id"]] = request
        pinned = broker.create_review_ref(request, review_number=1, board_mutation=lambda record: request.update(record))
        state["release_decisions"]["TASK"] = {"decision": "accepted"}
        candidate = {"recorded_base": base_commit, "commit": committed["commit"], "tree": committed["tree"], "manifest": committed["manifest"], "mirror_ref": pinned["ref"]}
        return repository, state, broker, base_commit, committed, candidate

    def create_task_workspace(self):
        created = self.broker.branch_create("delivery", 1)
        self.state["task_workspaces"]["TASK"] = created["workspace"]
        return created, Path(created["workspace"])

    def create_candidate(self):
        created, workspace = self.create_task_workspace()
        (workspace / "product.txt").write_text("candidate\n", encoding="utf-8")
        committed = self.broker.stage_commit("delivery", 2, ["product.txt"], "candidate")
        return created, workspace, committed

    def pin_candidate(self, committed, request_id="review-TASK-final-01", review_number=1):
        request = {
            "id": request_id, "task": "TASK", "reviewed_commit": committed["commit"],
            "reviewed_tree_hash": committed["tree"], "reviewed_files": committed["manifest"],
        }
        self.state["qa_requests"][request_id] = request
        records = []
        pinned = self.broker.create_review_ref(
            request, review_number=review_number, board_mutation=records.append,
        )
        request["mirror_ref"] = pinned["ref"]
        request["mirror_transaction_id"] = pinned["transaction_id"]
        return request, pinned, records

    def test_branch_and_stage_commit_are_board_derived_and_nonce_guarded(self):
        created, workspace = self.create_task_workspace()
        self.assertTrue(Path(created["workspace"]).resolve().is_relative_to(self.workspaces.resolve()))
        self.assertEqual(created["branch"], "refs/heads/harness/tasks/TASK/task")
        self.assertEqual(self._git(self.repository, "rev-parse", "main").strip(), self.base_commit)
        (workspace / "product.txt").write_text("changed\n", encoding="utf-8")
        committed = self.broker.stage_commit("delivery", 2, ["product.txt"], "change")
        self.assertEqual(committed["manifest"], ["product.txt"])
        self.assertNotEqual(committed["commit"], self.base_commit)
        self.assertEqual(self._git(self.repository, "rev-parse", "main").strip(), self.base_commit)
        with self.assertRaises(ReplayError):
            self.broker.stage_commit("delivery", 2, ["product.txt"], "replay")

    def test_subtasks_receive_distinct_branches_and_worktrees(self):
        task = self.broker.branch_create("delivery", 1)
        self.state["task_workspaces"]["TASK"] = task["workspace"]
        alpha = self.broker.branch_create("delivery", 2, subtask="alpha")
        beta = self.broker.branch_create("delivery", 3, subtask="beta")
        self.assertNotEqual(alpha["workspace"], beta["workspace"])
        self.assertNotEqual(alpha["branch"], beta["branch"])
        self.assertEqual(self._git(Path(alpha["workspace"]), "status", "--porcelain"), "")
        self.assertEqual(self._git(Path(beta["workspace"]), "status", "--porcelain"), "")

    def test_stage_input_hardening_refuses_every_escape_shape(self):
        _, workspace = self.create_task_workspace()
        outside = self.base / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (workspace / "escape").symlink_to(outside)
        (workspace / "directory").mkdir()
        cases = [
            [str(outside)], ["../outside.txt"], ["escape"], ["--all"], ["nested/-option"],
            [""], [" "], ["\t"], ["\n"], ["."], ["./"], [".//"], ["././"],
            ["directory"],
        ]
        nonce = 2
        for paths in cases:
            with self.subTest(paths=paths), self.assertRaises(BrokerError):
                self.broker.stage_commit("delivery", nonce, paths, "refused")
            nonce += 1
        self.assertEqual(outside.read_text(encoding="utf-8"), "secret")
        self.assertEqual(self._git(workspace, "status", "--porcelain"), "?? escape\n")

    def test_wrong_role_wrong_task_and_cross_project_state_are_refused(self):
        self.state["agents"]["reviewer"] = {
            "id": "reviewer", "role": "qa", "task": "TASK", "active": True,
            "session_id": "reviewer-session",
        }
        with self.assertRaises(AuthorizationError):
            self.broker.branch_create("reviewer", 1)
        self.state["agents"]["delivery"]["task"] = "FOREIGN"
        with self.assertRaises(BrokerError):
            self.broker.branch_create("delivery", 1)
        self.state["agents"]["delivery"]["task"] = "TASK"
        foreign = self.base / "foreign"
        foreign.mkdir()
        self._git(foreign, "init", "-b", "main")
        self.state["task_repositories"]["TASK"] = str(foreign / "missing")
        with self.assertRaises(BrokerError):
            self.broker.branch_create("delivery", 1)

    def test_single_flight_lock_fails_fast_instead_of_interleaving(self):
        with self.broker.project_lock():
            with self.assertRaisesRegex(BrokerError, "already in flight"):
                with self.broker.project_lock(fail_fast=True):
                    self.fail("nested broker operation must not enter")

    def test_hooks_helpers_and_required_filters_cannot_execute(self):
        _, workspace = self.create_task_workspace()
        canary = self.base / "hook-canary"
        hooks = self.repository / ".git" / "hooks"
        hook = hooks / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch '{canary}'\nexit 77\n", encoding="utf-8")
        hook.chmod(0o755)
        self._git(self.repository, "config", "commit.gpgSign", "true")
        self._git(self.repository, "config", "gpg.program", str(self.base / "malicious-gpg"))
        (workspace / "product.txt").write_text("safe\n", encoding="utf-8")
        committed = self.broker.stage_commit("delivery", 2, ["product.txt"], "hooks disabled")
        self.assertTrue(committed["commit"])
        self.assertFalse(canary.exists())

        (workspace / ".gitattributes").write_text("filtered.txt filter=hostile\n", encoding="utf-8")
        (workspace / "filtered.txt").write_text("content\n", encoding="utf-8")
        with self.assertRaises(FilterRequiredError):
            self.broker.stage_commit("delivery", 3, ["filtered.txt"], "filter refused")
        holds = self.broker.holds_path.read_text(encoding="utf-8")
        self.assertIn("attribute-driven filter required", holds)

    def test_branch_materialization_refuses_required_filter_before_execution(self):
        canary = self.repository / "filter-canary"
        (self.repository / ".gitattributes").write_text("filtered.txt filter=hostile\n", encoding="utf-8")
        (self.repository / "filtered.txt").write_text("content\n", encoding="utf-8")
        self._git(self.repository, "add", ".gitattributes", "filtered.txt")
        self._git(
            self.repository, "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "commit", "-m", "filtered base",
        )
        self._git(self.repository, "config", "filter.hostile.required", "true")
        self._git(self.repository, "config", "filter.hostile.smudge", f"touch {canary}")
        with self.assertRaises(FilterRequiredError):
            self.broker.branch_create("delivery", 1)
        self.assertFalse(canary.exists())
        self.assertIn("attribute-driven filter required", self.broker.holds_path.read_text(encoding="utf-8"))

    def test_mirror_ref_is_create_only_and_object_correspondence_is_verified(self):
        _, _, committed = self.create_candidate()
        request, pinned, records = self.pin_candidate(committed)
        self.assertEqual(records[0]["commit"], committed["commit"])
        self.assertEqual(self._git(self.broker.mirror_root, "rev-parse", pinned["ref"]).strip(), committed["commit"])
        self.assertEqual(self._git(self.broker.mirror_root, "rev-parse", pinned["ref"] + "^{tree}").strip(), committed["tree"])
        with self.assertRaisesRegex(BrokerError, "already exists"):
            self.broker.create_review_ref(request, review_number=1, board_mutation=lambda value: None)

        other = self._git(self.repository, "rev-parse", "main").strip()
        self._git(self.broker.mirror_root, "update-ref", "refs/harness/TASK/reviewed-2", other, ZERO_OID)
        second = dict(request, id="review-TASK-final-02")
        self.state["qa_requests"][second["id"]] = second
        with self.assertRaises(RecoveryHoldError):
            self.broker.create_review_ref(second, review_number=2, board_mutation=lambda value: None)
        self.assertEqual(self._git(self.broker.mirror_root, "rev-parse", "refs/harness/TASK/reviewed-2").strip(), other)

    def test_mirror_crash_after_mutation_recovers_as_idempotent_completion(self):
        _, _, committed = self.create_candidate()
        request = {
            "id": "review-TASK-final-01", "task": "TASK",
            "reviewed_commit": committed["commit"], "reviewed_tree_hash": committed["tree"],
        }
        self.state["qa_requests"][request["id"]] = request
        with self.assertRaises(InjectedCrash):
            self.broker.create_review_ref(
                request, review_number=1, board_mutation=lambda value: None,
                crash_after="git_mutation",
            )
        recovered = []
        outcomes = self.broker.recover(recovered.append)
        self.assertEqual(outcomes[-1]["status"], "completed_idempotently")
        self.assertEqual(recovered[-1]["commit"], committed["commit"])

    def test_mirror_crash_matrix_converges_at_every_journal_boundary(self):
        for crash_point in ("intent", "git_mutation", "board_mutation"):
            with self.subTest(crash_point=crash_point):
                _, state, broker, _, committed, _ = self.isolated_fixture("mirror-" + crash_point)
                request = {
                    "id": "review-mirror-crash", "task": "TASK",
                    "reviewed_commit": committed["commit"],
                    "reviewed_tree_hash": committed["tree"],
                }
                state["qa_requests"][request["id"]] = request
                board_records = []
                with self.assertRaises(InjectedCrash):
                    broker.create_review_ref(
                        request, review_number=2,
                        board_mutation=board_records.append,
                        crash_after=crash_point,
                    )
                outcomes = broker.recover(board_records.append)
                if crash_point == "intent":
                    self.assertEqual(outcomes[-1]["status"], "not_applied")
                    self.assertEqual(
                        self._git(broker.mirror_root, "show-ref", "--verify", "refs/harness/TASK/reviewed-2", check=False),
                        "",
                    )
                else:
                    self.assertEqual(outcomes[-1]["status"], "completed_idempotently")
                    self.assertEqual(
                        self._git(broker.mirror_root, "rev-parse", "refs/harness/TASK/reviewed-2").strip(),
                        committed["commit"],
                    )
                    self.assertTrue(any(row.get("commit") == committed["commit"] for row in board_records))

    def test_mirror_recovery_mismatch_and_missing_certified_ref_open_holds(self):
        _, state, broker, base, committed, _ = self.isolated_fixture("mirror-holds")
        request = {
            "id": "review-mirror-mismatch", "task": "TASK",
            "reviewed_commit": committed["commit"],
            "reviewed_tree_hash": committed["tree"],
        }
        state["qa_requests"][request["id"]] = request
        with self.assertRaises(InjectedCrash):
            broker.create_review_ref(
                request, review_number=2, board_mutation=lambda value: None,
                crash_after="intent",
            )
        self._git(
            broker.mirror_root, "update-ref", "refs/harness/TASK/reviewed-2",
            base, ZERO_OID,
        )
        self.assertEqual(broker.recover()[-1]["status"], "CTO_RECOVERY_HOLD")

        certified = state["qa_requests"]["review-mirror-holds"]
        certified.update({
            "mirror_ref": "refs/harness/TASK/reviewed-1",
            "mirror_commit": committed["commit"],
            "mirror_tree_hash": committed["tree"],
        })
        self._git(broker.mirror_root, "update-ref", "-d", certified["mirror_ref"])
        holds = broker.audit_mirror_records()
        self.assertTrue(any("missing or mismatched" in row["reason"] for row in holds))

    def test_orphaned_mirror_ref_waits_for_deterministic_review_retry(self):
        _, _, committed = self.create_candidate()
        request = {
            "id": "review-TASK-final-01", "task": "TASK",
            "reviewed_commit": committed["commit"],
            "reviewed_tree_hash": committed["tree"],
        }
        self.state["qa_requests"][request["id"]] = request
        with self.assertRaises(InjectedCrash):
            self.broker.create_review_ref(
                request, review_number=1, board_mutation=lambda value: None,
                crash_after="git_mutation",
            )

        def missing_board_request(_record):
            raise BrokerError("request was not durable at worker crash")

        outcome = self.broker.recover(missing_board_request)[-1]
        self.assertEqual(outcome["status"], "awaiting_board_request_retry")
        transaction = outcome["transaction_id"]
        self.assertFalse(any(
            row.get("transaction_id") == transaction and row.get("step") == "done"
            for row in self.broker.transaction_records()
        ))
        retried = self.broker.create_review_ref(
            request, review_number=1, board_mutation=lambda record: request.update(record),
        )
        self.assertEqual(retried["commit"], committed["commit"])

    def test_ff_acceptance_updates_main_only_after_owner_accept_and_mirror_verification(self):
        _, _, committed = self.create_candidate()
        request, pinned, _ = self.pin_candidate(committed)
        candidate = {
            "recorded_base": self.base_commit,
            "commit": committed["commit"], "tree": committed["tree"],
            "manifest": committed["manifest"], "mirror_ref": pinned["ref"],
        }
        with self.assertRaises(AuthorizationError):
            self.broker.accept_merge("TASK", candidate, board_mutation=lambda value: None)
        self.state["release_decisions"]["TASK"] = {"decision": "accepted"}
        recorded = []
        accepted = self.broker.accept_merge("TASK", candidate, board_mutation=recorded.append)
        self.assertEqual(self._git(self.repository, "rev-parse", "main").strip(), committed["commit"])
        self.assertEqual((self.repository / "product.txt").read_text(encoding="utf-8"), "candidate\n")
        self.assertEqual(recorded[0]["tree"], committed["tree"])
        self.assertEqual(accepted["mirror_ref"], request["mirror_ref"])

    def test_moved_main_refuses_acceptance_without_absorbing_commit(self):
        _, _, committed = self.create_candidate()
        _, pinned, _ = self.pin_candidate(committed)
        self.state["release_decisions"]["TASK"] = {"decision": "accepted"}
        (self.repository / "external.txt").write_text("external\n", encoding="utf-8")
        self._git(self.repository, "add", "external.txt")
        self._git(self.repository, "-c", "user.name=External", "-c", "user.email=external@example.invalid", "commit", "-m", "external")
        external = self._git(self.repository, "rev-parse", "main").strip()
        with self.assertRaises(MainMovedError):
            self.broker.accept_merge("TASK", {
                "recorded_base": self.base_commit, "commit": committed["commit"],
                "tree": committed["tree"], "manifest": committed["manifest"],
                "mirror_ref": pinned["ref"],
            }, board_mutation=lambda value: None)
        self.assertEqual(self._git(self.repository, "rev-parse", "main").strip(), external)

    def test_main_advanced_before_readiness_is_accepted_when_candidate_contains_it(self):
        _, workspace, _ = self.create_candidate()
        (self.repository / "concurrent.txt").write_text("preserve\n", encoding="utf-8")
        self._git(self.repository, "add", "concurrent.txt")
        self._git(
            self.repository, "-c", "user.name=Concurrent", "-c",
            "user.email=concurrent@example.invalid", "commit", "-m", "concurrent main",
        )
        observed_main = self._git(self.repository, "rev-parse", "main").strip()
        self._git(
            workspace, "-c", "user.name=Delivery", "-c",
            "user.email=delivery@example.invalid", "merge", "--no-edit", "main",
        )
        reviewed = self._git(workspace, "rev-parse", "HEAD").strip()
        tree = self._git(workspace, "rev-parse", "HEAD^{tree}").strip()
        committed = {"commit": reviewed, "tree": tree, "manifest": ["product.txt"]}
        _, pinned, _ = self.pin_candidate(committed)
        self.state["release_decisions"]["TASK"] = {"decision": "accepted"}
        accepted = self.broker.accept_merge("TASK", {
            "recorded_base": observed_main, "commit": reviewed, "tree": tree,
            "manifest": ["product.txt"], "mirror_ref": pinned["ref"],
        }, board_mutation=lambda value: None)
        self.assertEqual(accepted["commit"], reviewed)
        self.assertEqual(self._git(self.repository, "rev-parse", "main").strip(), reviewed)
        self.assertEqual((self.repository / "concurrent.txt").read_text(), "preserve\n")

    def test_post_mutation_crash_with_external_commit_opens_hold_and_preserves_all_commits(self):
        _, _, committed = self.create_candidate()
        _, pinned, _ = self.pin_candidate(committed)
        self.state["release_decisions"]["TASK"] = {"decision": "accepted"}
        candidate = {
            "recorded_base": self.base_commit, "commit": committed["commit"],
            "tree": committed["tree"], "manifest": committed["manifest"],
            "mirror_ref": pinned["ref"],
        }
        with self.assertRaises(InjectedCrash):
            self.broker.accept_merge("TASK", candidate, board_mutation=lambda value: None, crash_after="git_mutation")
        self._git(self.repository, "checkout", "-B", "main", committed["commit"])
        (self.repository / "external.txt").write_text("external\n", encoding="utf-8")
        self._git(self.repository, "add", "external.txt")
        self._git(self.repository, "-c", "user.name=External", "-c", "user.email=external@example.invalid", "commit", "-m", "external")
        external = self._git(self.repository, "rev-parse", "main").strip()
        outcomes = self.broker.recover()
        self.assertEqual(outcomes[-1]["status"], "CTO_RECOVERY_HOLD")
        self.assertEqual(outcomes[-1]["observed_main"], external)
        self.assertEqual(self._git(self.repository, "rev-parse", "main").strip(), external)
        self.assertEqual(self._git(self.repository, "cat-file", "-t", committed["commit"]).strip(), "commit")

    def test_acceptance_crash_matrix_converges_at_every_journal_boundary(self):
        for crash_point in ("intent", "git_mutation", "board_mutation"):
            with self.subTest(crash_point=crash_point):
                repository, _, broker, base, committed, candidate = self.isolated_fixture("accept-" + crash_point)
                with self.assertRaises(InjectedCrash):
                    broker.accept_merge("TASK", candidate, board_mutation=lambda record: None, crash_after=crash_point)
                outcomes = broker.recover()
                if crash_point == "intent":
                    self.assertEqual(outcomes[-1]["status"], "not_applied")
                    self.assertEqual(self._git(repository, "rev-parse", "main").strip(), base)
                else:
                    self.assertEqual(outcomes[-1]["status"], "completed_idempotently")
                    self.assertEqual(self._git(repository, "rev-parse", "main").strip(), committed["commit"])
                    self.assertEqual((repository / "product.txt").read_text(encoding="utf-8"), "candidate\n")

    def test_external_commit_at_every_post_mutation_crash_point_opens_hold(self):
        for crash_point in ("git_mutation", "board_mutation"):
            with self.subTest(crash_point=crash_point):
                repository, _, broker, _, committed, candidate = self.isolated_fixture("external-" + crash_point)
                with self.assertRaises(InjectedCrash):
                    broker.accept_merge("TASK", candidate, board_mutation=lambda record: None, crash_after=crash_point)
                self._git(repository, "reset", "--hard", committed["commit"])
                (repository / "external.txt").write_text("preserve\n", encoding="utf-8")
                self._git(repository, "add", "external.txt")
                self._git(repository, "-c", "user.name=External", "-c", "user.email=external@example.invalid", "commit", "-m", "external")
                external = self._git(repository, "rev-parse", "main").strip()
                outcomes = broker.recover()
                self.assertEqual(outcomes[-1]["status"], "CTO_RECOVERY_HOLD")
                self.assertEqual(outcomes[-1]["observed_main"], external)
                self.assertEqual(self._git(repository, "rev-parse", "main").strip(), external)

    def test_guarded_push_uses_exact_accepted_commit_and_aborts_on_drift(self):
        _, _, committed = self.create_candidate()
        _, pinned, _ = self.pin_candidate(committed)
        self.state["release_decisions"]["TASK"] = {"decision": "accepted"}
        accepted = self.broker.accept_merge("TASK", {
            "recorded_base": self.base_commit, "commit": committed["commit"],
            "tree": committed["tree"], "manifest": committed["manifest"],
            "mirror_ref": pinned["ref"],
        }, board_mutation=lambda value: None)
        self.state["git_acceptances"]["TASK"] = accepted
        remote = self.base / "remote.git"
        remote.mkdir()
        self._git(remote, "init", "--bare")
        self._git(self.repository, "remote", "add", "approved", str(remote))
        self.state["approved_remotes"]["TASK"] = {
            "name": "approved", "url": str(remote), "branch": "main",
        }
        self.state["remote_push_instructions"]["TASK"] = {
            "owner_instructed_at": "now", "confirmed_at": "now", "remote": "approved",
            "branch": "main", "expected_remote_tip": ZERO_OID,
        }
        outcomes = []
        pushed = self.broker.remote_push("TASK", board_mutation=outcomes.append)
        self.assertEqual(pushed["outcome"], "pushed")
        self.assertEqual(self._git(remote, "rev-parse", "refs/heads/main").strip(), committed["commit"])

        self.state["remote_push_instructions"]["TASK"] = {
            "owner_instructed_at": "later", "confirmed_at": "later", "remote": "approved",
            "branch": "main", "expected_remote_tip": ZERO_OID,
        }
        with self.assertRaisesRegex(BrokerError, "drifted"):
            self.broker.remote_push("TASK", board_mutation=lambda value: None)
        self.assertEqual(self.state["git_acceptances"]["TASK"]["commit"], committed["commit"])


if __name__ == "__main__":
    unittest.main()

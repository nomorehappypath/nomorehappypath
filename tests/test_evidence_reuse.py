# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Mechanical and auditable saved-PASS validity simulations."""
from __future__ import annotations

import json
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, contract, project_manager
from harness import project_registry as registry


class _Worker:
    pid = 88231

    def poll(self):
        return None

    def terminate(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


class EvidenceReuseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self._case_number = 0

    def _case(self):
        self._case_number += 1
        base = self.base / f"case-{self._case_number}"
        home, code = base / "home", base / "code"
        code.mkdir(parents=True)
        (code / "product.txt").write_text("reviewed product bytes\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=code, check=True)
        subprocess.run(["git", "config", "user.email", "qa@example.invalid"], cwd=code, check=True)
        subprocess.run(["git", "config", "user.name", "QA Fixture"], cwd=code, check=True)
        subprocess.run(["git", "add", "product.txt"], cwd=code, check=True)
        subprocess.run(["git", "commit", "-qm", "reviewed candidate"], cwd=code, check=True)

        entry = registry.register(
            home, f"reuse-{self._case_number}", code, kind="adopted",
            data_root=base / "managed-data",
            workspace_root=base / "managed-workspace",
        )
        root = registry.context_for_entry(entry)
        contract.create_contract(
            root, "T-REUSE", "Keep saved PASS evidence honest.",
            ["Mechanically validate every reviewed identity"],
        )
        agent = board.register(
            root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI",
        )
        evidence_dir = root.storage_path("evidence", "reuse-fixture")
        evidence_dir.mkdir(parents=True)
        sources = {
            "delivery_ledger": evidence_dir / "delivery-ledger.md",
            "challenge_ledger": evidence_dir / "challenge-ledger.md",
            "result_evidence": evidence_dir / "review-evidence.txt",
            "delivery_evidence": evidence_dir / "delivery-evidence.txt",
        }
        for name, path in sources.items():
            path.write_text(f"immutable reviewed {name}\n", encoding="utf-8")
        artifacts = {
            name: board._certify_file(root, path)
            for name, path in sources.items()
        }
        git_identity = board._git_review_artifact(code)
        request = {
            "id": "review-T-REUSE-valid-01",
            "task": "T-REUSE",
            "phase": "subtask_acceptance",
            "subtask": "reuse",
            "chunk": "subtask-final",
            "cycle": 1,
            "structure_revision": 1,
            "stage": board.INDEPENDENT_REVIEW,
            "status": "passed",
            "result": "passed",
            "result_summary": "Fixture PASS",
            "requested_at": board.now(),
            "completed_at": board.now(),
            "review_wait_started_at": board.now(),
            "review_wait_stopped_at": board.now(),
            "claimed_by": "qa-fixture",
            "developer_id": agent["id"],
            "ledger": str(sources["delivery_ledger"]),
            "challenge_ledger": str(sources["challenge_ledger"]),
            "evidence": str(sources["result_evidence"]),
            "ledger_sha256": artifacts["delivery_ledger"]["sha256"],
            "challenge_ledger_sha256": artifacts["challenge_ledger"]["sha256"],
            "certified_artifacts": artifacts,
            "reviewed_commit": git_identity["commit"],
            "reviewed_base_commit": git_identity["base_commit"],
            "reviewed_tree_hash": git_identity["tree_hash"],
            "reviewed_worktree_digest": git_identity["working_tree_digest"],
            "contract_revision": board._contract_revision(root, "T-REUSE"),
            "environment_identity": board._environment_identity(),
        }
        request["evidence_reuse_identity"] = board._pass_reuse_identity(request)
        with board.locked_state(root) as state:
            state["task_workspaces"]["T-REUSE"] = str(code)
            state["delivery_plans"]["T-REUSE"] = {
                "task": "T-REUSE", "mode": "application", "structure_revision": 1,
                "subtasks": {
                    "reuse": {"id": "reuse", "status": "passed", "dependencies": [], "chunks": {}},
                },
            }
            state["agents"][agent["id"]].update({"task": "T-REUSE", "status": "working"})
            state["qa_requests"][request["id"]] = request
        return {
            "home": home, "code": code, "entry": entry, "root": root,
            "agent": agent, "request": request, "sources": sources,
            "git_identity": git_identity,
        }

    def _assert_invalidated(self, case, expected_identity: str):
        manager = project_manager.ProjectManager(case["home"], board_port=0, terminal_launcher=lambda *_: None)
        manager.pause_project(case["entry"]["id"], drain_seconds=0, stop_timeout=0)
        manager.worker = _Worker()
        manager.worker_project = case["entry"]["id"]
        resumed = manager.resume_project(case["entry"]["id"])
        result = resumed["evidence_reuse"]
        self.assertEqual(result["reused"], [])
        self.assertEqual(len(result["invalidated"]), 1)
        state = board.snapshot(case["root"])
        request = state["qa_requests"][case["request"]["id"]]
        self.assertEqual(request["status"], "failed")
        self.assertEqual(state["delivery_plans"]["T-REUSE"]["subtasks"]["reuse"]["status"], "open")
        event = next(value for value in reversed(state["events"]) if value["kind"] == "qa_pass_invalidated")
        self.assertIn(expected_identity, event["diverged_identities"])
        self.assertIn(
            "identity_incomplete" if expected_identity == "identity_incomplete" else f"{expected_identity} diverged",
            event["message"],
        )

    def test_valid_pass_is_reused_once_through_real_resume(self):
        case = self._case()
        manager = project_manager.ProjectManager(case["home"], board_port=0, terminal_launcher=lambda *_: None)
        manager.pause_project(case["entry"]["id"], drain_seconds=0, stop_timeout=0)
        manager.worker = _Worker()
        manager.worker_project = case["entry"]["id"]
        first = manager.resume_project(case["entry"]["id"])
        second = manager.resume_project(case["entry"]["id"])

        self.assertEqual(len(first["evidence_reuse"]["reused"]), 1)
        self.assertEqual(first["evidence_reuse"]["invalidated"], [])
        self.assertEqual(second["resume"]["resume_id"], first["resume"]["resume_id"])
        state = board.snapshot(case["root"])
        self.assertEqual(state["qa_requests"][case["request"]["id"]]["status"], "passed")
        reuse_events = [value for value in state["events"] if value["kind"] == "qa_pass_reused"]
        self.assertEqual(len(reuse_events), 1, "double Resume must not duplicate reuse audit")
        identities = {
            item["identity"]
            for item in state["qa_requests"][case["request"]["id"]]["evidence_reuse_validation"]["checks"]
        }
        self.assertTrue({
            "commit hash", "tree hash", "challenge-ledger hash", "evidence file hash",
            "contract revision", "environment identity",
        }.issubset(identities))

    def test_commit_hash_change_invalidates_saved_pass(self):
        case = self._case()
        changed = dict(case["git_identity"], base_commit="different-commit")
        with patch("harness.board._git_review_artifact", return_value=changed):
            self._assert_invalidated(case, "commit hash")

    def test_tree_hash_change_invalidates_saved_pass(self):
        case = self._case()
        changed = dict(case["git_identity"], tree_hash="different-tree")
        with patch("harness.board._git_review_artifact", return_value=changed):
            self._assert_invalidated(case, "tree hash")

    def test_working_tree_change_invalidates_saved_pass(self):
        case = self._case()
        (case["code"] / "product.txt").write_text("changed after review\n", encoding="utf-8")
        self._assert_invalidated(case, "working-tree hash")

    def test_challenge_ledger_change_invalidates_saved_pass(self):
        case = self._case()
        case["sources"]["challenge_ledger"].write_text("changed challenge\n", encoding="utf-8")
        self._assert_invalidated(case, "challenge-ledger hash")

    def test_evidence_file_change_invalidates_saved_pass(self):
        case = self._case()
        case["sources"]["result_evidence"].write_text("changed evidence\n", encoding="utf-8")
        self._assert_invalidated(case, "evidence file hash")

    def test_required_manifest_without_sha256_refuses_reuse_fail_closed(self):
        case = self._case()
        with board.locked_state(case["root"]) as state:
            request = state["qa_requests"][case["request"]["id"]]
            request["certified_artifacts"]["challenge_ledger"].pop("sha256")

        self._assert_invalidated(case, "identity_incomplete")

        state = board.snapshot(case["root"])
        validation = state["qa_requests"][case["request"]["id"]]["evidence_reuse_validation"]
        incomplete = [item for item in validation["checks"] if item["identity"] == "identity_incomplete"]
        self.assertEqual(len(incomplete), 1)
        self.assertEqual(incomplete[0]["artifact"], "challenge-ledger hash")
        self.assertIn("lacks sha256", incomplete[0]["message"])

    def test_contract_scope_change_invalidates_and_routes_saved_agent(self):
        case = self._case()
        path = Path(case["request"]["contract_revision"]["path"])
        value = json.loads(path.read_text(encoding="utf-8"))
        scope = value.get("immutable_scope") or {}
        scope["objective"] = "A genuinely different reviewed objective."
        value["immutable_scope"] = scope
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        self._assert_invalidated(case, "contract revision")
        agent = board.snapshot(case["root"])["agents"][case["agent"]["id"]]
        self.assertEqual(agent["status"], "independent_review_failed")
        self.assertIn("fresh review required", agent["status_note"])

    def test_post_pass_evidence_linking_never_voids_the_pass(self):
        # The 2026-08-21 ghost-relaunch bug: linking evidence and marking
        # deliverables verified AFTER the pass is bookkeeping, not a change
        # to the reviewed scope. The PASS must reuse.
        case = self._case()
        path = Path(case["request"]["contract_revision"]["path"])
        value = json.loads(path.read_text(encoding="utf-8"))
        value["updated_at"] = "2099-01-01T00:00:00+00:00"
        for deliverable in value.get("deliverables") or []:
            deliverable["verified"] = True
            deliverable["evidence"] = [{"path": "/tmp/e.txt", "sha256": "a" * 64}]
        value["remaining_work"] = []
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        result = board.reconcile_evidence_reuse(case["root"], "resume-evidence-append")
        self.assertEqual(result["invalidated"], [])
        self.assertEqual(len(result["reused"]), 1)

    def test_legacy_whole_file_pass_identity_still_reconciles(self):
        case = self._case()
        with board.locked_state(case["root"]) as state:
            request = state["qa_requests"][case["request"]["id"]]
            revision = request["contract_revision"]
            # A PASS saved by a pre-scope harness recorded the whole-file sha.
            revision["sha256"] = revision["legacy_sha256"]
            revision["revision"] = "legacy"
        result = board.reconcile_evidence_reuse(case["root"], "resume-legacy")
        self.assertEqual(result["invalidated"], [])
        self.assertEqual(len(result["reused"]), 1)

    def test_environment_change_invalidates_saved_pass(self):
        case = self._case()
        with patch("harness.board._environment_identity", return_value={"fields": {}, "sha256": "different-environment"}):
            self._assert_invalidated(case, "environment identity")

    def test_finalization_diff_and_classification_are_immutable_reuse_inputs(self):
        case = self._case()
        payload = {
            "version": 1,
            "baseline_commit": "base",
            "final_commit": "final",
            "paths": ["release.py"],
            "classification_required": True,
            "classification": "pending_independent_review",
        }
        payload["sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with board.locked_state(case["root"]) as state:
            request = state["qa_requests"][case["request"]["id"]]
            request["finalization_diff"] = payload
            request["finalization_classification"] = {
                "decision": "accepted", "diff_sha256": payload["sha256"],
            }
            request["evidence_reuse_identity"] = board._pass_reuse_identity(request)
            valid, problems = board._request_integrity(case["root"], request)
            self.assertTrue(valid, problems)
            request["finalization_diff"]["paths"] = ["silently-changed.py"]
            valid, problems = board._request_integrity(case["root"], request)
            self.assertFalse(valid)
            self.assertIn("finalization diff digest is missing or mismatched", problems)
            checks = board._evidence_reuse_checks(case["root"], state, request)
        matched = {item["identity"]: item["matched"] for item in checks}
        self.assertTrue(matched["finalization diff"])
        self.assertTrue(matched["finalization classification"])
        self.assertTrue(matched["finalization classification diff"])

    def test_resume_invalidates_before_restoring_the_saved_agent_gate(self):
        case = self._case()
        manager = project_manager.ProjectManager(case["home"], board_port=0, terminal_launcher=lambda *_: None)
        manager.pause_project(case["entry"]["id"], drain_seconds=0, stop_timeout=0)
        path = Path(case["request"]["contract_revision"]["path"])
        value = json.loads(path.read_text(encoding="utf-8"))
        scope = value.get("immutable_scope") or {}
        scope["objective"] = "The reviewed objective genuinely changed."
        value["immutable_scope"] = scope
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        manager.worker = _Worker()
        manager.worker_project = case["entry"]["id"]

        result = manager.resume_project(case["entry"]["id"])

        self.assertEqual(len(result["evidence_reuse"]["invalidated"]), 1)
        state = board.snapshot(case["root"])
        agent = state["agents"][case["agent"]["id"]]
        self.assertEqual(agent["status"], "independent_review_failed")
        self.assertIn("contract revision diverged", agent["status_note"])
        checkpoint = state["project_pause"]["last_resume"]["checkpoints"]["evidence_reconciled"]
        self.assertEqual(len(checkpoint["details"]["invalidated"]), 1)

    def test_superseded_historical_pass_cannot_reopen_the_current_scope(self):
        case = self._case()
        case["sources"]["challenge_ledger"].write_text("changed historical challenge\n", encoding="utf-8")
        with board.locked_state(case["root"]) as state:
            latest = dict(case["request"])
            latest.update({
                "id": "review-T-REUSE-valid-02", "cycle": 2,
                "status": "failed", "result": "failed",
            })
            state["qa_requests"][latest["id"]] = latest
            state["delivery_plans"]["T-REUSE"]["subtasks"]["reuse"]["status"] = "open"

        result = board.reconcile_evidence_reuse(case["root"], "resume-latest-failed")

        self.assertEqual(result, {"resume_id": "resume-latest-failed", "reused": [], "invalidated": []})
        state = board.snapshot(case["root"])
        self.assertEqual(state["qa_requests"][case["request"]["id"]]["status"], "passed")
        self.assertEqual(state["delivery_plans"]["T-REUSE"]["subtasks"]["reuse"]["status"], "open")


if __name__ == "__main__":
    unittest.main(verbosity=2)

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""P5 dependency, ownership, workspace, and reviewer-concurrency proofs."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, contract, control, execution_identity as xid, git_broker


class SubtaskPipeliningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "code"
        self.root.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Fixture")
        self._git("config", "user.email", "fixture@example.invalid")
        (self.root / ".gitignore").write_text(".harness/\n", encoding="utf-8")
        (self.root / "test_smoke.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n"
            "    def test_passes(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self._git("add", ".gitignore", "test_smoke.py")
        self._git("commit", "-q", "-m", "baseline")
        session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(
            self.root, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Build the pipelined application")
        board.begin_task(self.root, self.delivery["id"], "PIPELINE")
        contract.create_contract(
            self.root, "PIPELINE", "Build the pipelined application", ["delivery"],
        )
        board.record_requirement_confirmation(
            self.root, self.delivery["id"],
            "Build and independently verify every declared application subtask.",
        )
        board.define_delivery_plan(
            self.root, self.delivery["id"], "application",
            "Independent product capabilities can use isolated worktrees.",
        )

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def declare(
        self, alpha_path: str = "alpha", beta_path: str = "beta", *,
        beta_dependencies=None, alpha_surface: str = "api:alpha",
        beta_surface: str = "api:beta",
    ):
        return board.declare_subtasks(self.root, self.delivery["id"], [
            {
                "id": "alpha", "title": "Alpha", "acceptance_proof": "Alpha works",
                "dependencies": [], "owned_paths": [alpha_path],
                "owned_surfaces": [alpha_surface],
            },
            {
                "id": "beta", "title": "Beta", "acceptance_proof": "Beta works",
                "dependencies": beta_dependencies or [], "owned_paths": [beta_path],
                "owned_surfaces": [beta_surface],
            },
        ])

    def workspace(self, subtask: str) -> Path:
        state = board.snapshot(self.root)
        return Path(state["subtask_workspaces"]["PIPELINE"][subtask])

    def commit(self, subtask: str, path: str) -> dict:
        destination = self.workspace(subtask) / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"{subtask} candidate\n", encoding="utf-8")
        return board.broker_stage_commit(
            self.root, self.delivery["id"], [path],
            f"implement {subtask}", subtask=subtask,
        )

    def ledger(self, name: str, *, reviewer: bool = False) -> str:
        path = self.root / "ledgers" / f"{name}.md"
        path.parent.mkdir(exist_ok=True)
        command = (
            "python3 -m unittest test_smoke -k passes"
            if reviewer else "python3 -m unittest test_smoke"
        )
        scenario = "reviewer challenge" if reviewer else "delivery acceptance"
        path.write_text(
            "| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|---|\n"
            f"| S-{name} | The {name} capability remains isolated and reaches its own independently verified outcome. | {scenario} | `{command}` | Scope passes independently | PASS: executable scope passed | PASS |\n",
            encoding="utf-8",
        )
        return str(path.relative_to(self.root))

    def evidence(self, name: str) -> str:
        path = self.root / "evidence" / f"{name}.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text("command: python3 -m unittest\nresult: PASS\n", encoding="utf-8")
        return str(path)

    def request(self, subtask: str) -> dict:
        return board.request_review(
            self.root, self.delivery["id"], self.ledger(f"delivery-{subtask}"),
            f"review {subtask}", phase="subtask_acceptance", subtask=subtask,
            test_command="python3 -m unittest test_smoke",
        )

    def reviewer(self):
        session = control.create(self.root, "claude_reviewer")
        return board.register(
            self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic",
            session_id=session["id"],
        )

    def pass_request(self, reviewer: dict, request: dict, name: str) -> dict:
        board.reserve_qa(self.root, reviewer["id"], request["id"])
        board.attach_challenge_ledger(
            self.root, reviewer["id"], request["id"],
            self.ledger(f"challenge-{name}", reviewer=True),
        )
        board.execute_challenge(self.root, reviewer["id"], request["id"])
        return board.qa_result(
            self.root, reviewer["id"], request["id"], "passed",
            f"{name} passed", self.evidence(name),
        )

    def assert_fold_crash_recovers(self, crash_point: str) -> None:
        self.declare()
        reviewer = self.reviewer()
        board.start_subtask(self.root, self.delivery["id"], "alpha")
        candidate = self.commit("alpha", "alpha/result.txt")
        request = self.request("alpha")
        original = git_broker.GitBroker.integrate_subtask

        def crash(broker, value, *, board_mutation, crash_after=""):
            return original(
                broker, value, board_mutation=board_mutation,
                crash_after=crash_point,
            )

        with patch.object(git_broker.GitBroker, "integrate_subtask", new=crash):
            with self.assertRaises(git_broker.InjectedCrash):
                self.pass_request(reviewer, request, f"alpha-{crash_point}")

        recovery = board.recover_git_transactions(self.root)
        self.assertFalse(recovery["holds"])
        passed = board.qa_result(
            self.root, reviewer["id"], request["id"], "passed",
            f"alpha recovered after {crash_point}", self.evidence(f"retry-{crash_point}"),
        )
        task_workspace = Path(board.snapshot(self.root)["task_workspaces"]["PIPELINE"])
        self.assertEqual(
            (task_workspace / "alpha" / "result.txt").read_text(),
            "alpha candidate\n",
        )
        self.assertEqual(self._git("-C", str(task_workspace), "status", "--porcelain"), "")
        self.assertEqual(
            self._git(
                "-C", str(task_workspace), "merge-base", "--is-ancestor",
                candidate["commit"], passed["integrated_commit"],
            ),
            "",
        )
        records = [
            json.loads(line)
            for line in (
                board.board_dir(self.root).parent / "broker-journal" / "transactions.jsonl"
            ).read_text().splitlines()
        ]
        completed = [
            row for row in records
            if row.get("operation") == "subtask-fold"
            and row.get("request_id") == request["id"]
            and row.get("step") == "done"
        ]
        self.assertGreaterEqual(len(completed), 1)

    def test_forced_path_overlap_serializes_while_first_scope_is_under_review(self):
        self.declare("shared", "shared/component")
        board.start_subtask(self.root, self.delivery["id"], "alpha")
        self.commit("alpha", "shared/alpha.txt")
        alpha = self.request("alpha")
        self.assertEqual(alpha["status"], "open")
        with self.assertRaisesRegex(ValueError, "ownership overlaps.*alpha"):
            board.start_subtask(self.root, self.delivery["id"], "beta")
        beta = board.snapshot(self.root)["delivery_plans"]["PIPELINE"]["subtasks"]["beta"]
        self.assertEqual(beta["pipeline_status"], "pending")

    def test_unmet_dependency_blocks_before_implementation(self):
        self.declare(beta_dependencies=["alpha"])
        with self.assertRaisesRegex(ValueError, "unmet dependencies: alpha"):
            board.start_subtask(self.root, self.delivery["id"], "beta")
        self.assertEqual(
            board.snapshot(self.root)["delivery_plans"]["PIPELINE"]["subtasks"]["beta"]["pipeline_status"],
            "pending",
        )

    def test_dependency_starts_from_its_integrated_predecessor(self):
        self.declare(beta_dependencies=["alpha"])
        reviewer = self.reviewer()
        board.start_subtask(self.root, self.delivery["id"], "alpha")
        self.commit("alpha", "alpha/result.txt")
        passed = self.pass_request(reviewer, self.request("alpha"), "alpha-dependency")

        beta = board.start_subtask(self.root, self.delivery["id"], "beta")

        self.assertEqual(beta["base_commit"], passed["integrated_commit"])
        self.assertEqual(
            (self.workspace("beta") / "alpha" / "result.txt").read_text(),
            "alpha candidate\n",
        )
        self.assertEqual(self._git("-C", str(self.workspace("beta")), "status", "--porcelain"), "")

    def test_forced_logical_surface_overlap_serializes_disjoint_paths(self):
        self.declare(alpha_surface="database:users", beta_surface="database:users")
        board.start_subtask(self.root, self.delivery["id"], "alpha")
        with self.assertRaisesRegex(ValueError, "ownership overlaps.*alpha"):
            board.start_subtask(self.root, self.delivery["id"], "beta")

    def test_disjoint_subtasks_pipeline_to_two_reviewers_one_request_each(self):
        self.declare()
        first, second = self.reviewer(), self.reviewer()
        board.start_subtask(self.root, self.delivery["id"], "alpha")
        self.commit("alpha", "alpha/result.txt")
        alpha = self.request("alpha")
        board.start_subtask(self.root, self.delivery["id"], "beta")
        self.commit("beta", "beta/result.txt")
        beta = self.request("beta")
        self.assertNotEqual(alpha["routed_to"], beta["routed_to"])
        routed = {alpha["routed_to"]: alpha, beta["routed_to"]: beta}
        first_request = routed[first["id"]]
        second_request = routed[second["id"]]
        board.reserve_qa(self.root, first["id"], first_request["id"])
        with self.assertRaisesRegex(ValueError, "already holds active request"):
            board.reserve_qa(self.root, first["id"], second_request["id"])
        board.reserve_qa(self.root, second["id"], second_request["id"])
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"][first_request["id"]]["status"], "reserved")
        self.assertEqual(state["qa_requests"][second_request["id"]]["status"], "reserved")

    def test_disjoint_reviewed_subtasks_both_fold_into_one_task_history(self):
        self.declare()
        reviewers = [self.reviewer(), self.reviewer()]
        board.start_subtask(self.root, self.delivery["id"], "alpha")
        alpha_candidate = self.commit("alpha", "alpha/result.txt")
        alpha = self.request("alpha")
        board.start_subtask(self.root, self.delivery["id"], "beta")
        beta_candidate = self.commit("beta", "beta/result.txt")
        beta = self.request("beta")
        by_id = {reviewer["id"]: reviewer for reviewer in reviewers}

        alpha_passed = self.pass_request(by_id[alpha["routed_to"]], alpha, "parallel-alpha")
        beta_passed = self.pass_request(by_id[beta["routed_to"]], beta, "parallel-beta")

        task_workspace = Path(board.snapshot(self.root)["task_workspaces"]["PIPELINE"])
        self.assertEqual((task_workspace / "alpha" / "result.txt").read_text(), "alpha candidate\n")
        self.assertEqual((task_workspace / "beta" / "result.txt").read_text(), "beta candidate\n")
        for candidate in (alpha_candidate, beta_candidate):
            self._git(
                "-C", str(task_workspace), "merge-base", "--is-ancestor",
                candidate["commit"], beta_passed["integrated_commit"],
            )
        self.assertNotEqual(alpha_passed["integrated_commit"], beta_passed["integrated_commit"])
        self.assertEqual(self._git("-C", str(task_workspace), "status", "--porcelain"), "")

    def test_final_review_classifies_board_computed_diff_after_all_subtasks_pass(self):
        self.declare()
        reviewers = [self.reviewer(), self.reviewer()]
        by_id = {reviewer["id"]: reviewer for reviewer in reviewers}
        for subtask in ("alpha", "beta"):
            board.start_subtask(self.root, self.delivery["id"], subtask)
            self.commit(subtask, f"{subtask}/result.txt")
            request = self.request(subtask)
            self.pass_request(by_id[request["routed_to"]], request, subtask)

        final = board.request_review(
            self.root, self.delivery["id"], self.ledger("delivery-final"),
            "review the integrated application", phase="final_acceptance",
            test_command="python3 -m unittest test_smoke",
        )
        self.assertEqual(final["finalization_diff"]["paths"], [])
        self.assertEqual(len(final["finalization_diff"]["accepted_manifests"]), 2)
        self.assertEqual(
            final["review_brief"]["finalization_diff"]["sha256"],
            final["finalization_diff"]["sha256"],
        )
        reviewer = by_id[final["routed_to"]]
        board.reserve_qa(self.root, reviewer["id"], final["id"])
        board.attach_challenge_ledger(
            self.root, reviewer["id"], final["id"],
            self.ledger("challenge-final", reviewer=True),
        )
        board.execute_challenge(self.root, reviewer["id"], final["id"])
        with self.assertRaisesRegex(ValueError, "explicit accepted or rejected"):
            board.qa_result(
                self.root, reviewer["id"], final["id"], "passed",
                "final application passed", self.evidence("final-missing-classification"),
            )
        passed = board.qa_result(
            self.root, reviewer["id"], final["id"], "passed",
            "The empty finalization diff adds no new product capability.",
            self.evidence("final-classified"), "accepted",
        )
        self.assertEqual(passed["finalization_classification"]["decision"], "accepted")

    def test_execution_identity_from_alpha_cannot_certify_beta_scope(self):
        self.declare()
        reviewer = self.reviewer()
        board.start_subtask(self.root, self.delivery["id"], "alpha")
        self.commit("alpha", "alpha/result.txt")
        alpha = self.request("alpha")
        board.reserve_qa(self.root, reviewer["id"], alpha["id"])
        board.attach_challenge_ledger(
            self.root, reviewer["id"], alpha["id"],
            self.ledger("challenge-shared", reviewer=True),
        )
        board.execute_challenge(self.root, reviewer["id"], alpha["id"])
        stamped = board.snapshot(self.root)["qa_requests"][alpha["id"]]["challenge_execution"]
        candidate_a = stamped["candidate_identity"]
        fields = candidate_a["fields"]
        beta_scope = {
            "task": "PIPELINE", "structure_revision": alpha["structure_revision"],
            "phase": "subtask_acceptance", "subtask": "beta", "chunk": "subtask-final",
        }
        beta_scope_sha = hashlib.sha256(
            json.dumps(beta_scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        artifacts = dict(fields["artifacts"])
        artifacts["review_scope"] = beta_scope_sha
        candidate_b = xid.candidate_evidence_identity(
            fields["commit"], fields["tree"], fields["contract_revision"], artifacts,
        )
        store_rows = [
            json.loads(line)
            for line in (board.board_dir(self.root) / xid.STORE_NAME).read_text().splitlines()
        ]
        run_a = next(row for row in store_rows if row["identity_kind"] == "command_run")
        run_b = xid.command_run_identity(
            candidate_b, run_a["identity_fields"]["argv"], run_a["identity_fields"]["cwd"],
            run_a["identity_fields"]["environment_sha256"], run_a["identity_fields"]["lockfiles"],
            runtime=run_a["identity_fields"]["runtime"],
        )
        decision = xid.lookup(self.root, run_b)
        self.assertEqual(decision["status"], "miss")
        self.assertIn("candidate_sha256", decision["diverged_fields"])

    def test_final_acceptance_refuses_any_outstanding_subtask_verdict(self):
        self.declare()
        board.start_subtask(self.root, self.delivery["id"], "alpha")
        self.commit("alpha", "alpha/result.txt")
        self.request("alpha")
        with patch.object(
            board, "_execute_internal_qa",
            side_effect=AssertionError("final suite must not start"),
        ) as execute:
            with self.assertRaisesRegex(ValueError, "another review is active"):
                board.request_review(
                    self.root, self.delivery["id"], self.ledger("premature-final"),
                    "premature final", phase="final_acceptance",
                    test_command="python3 -m unittest test_smoke",
                )
        execute.assert_not_called()

    def test_parallel_start_refuses_an_unpinned_active_review(self):
        self.declare()
        board.start_subtask(self.root, self.delivery["id"], "alpha")
        self.commit("alpha", "alpha/result.txt")
        alpha = self.request("alpha")
        with board.locked_state(self.root) as state:
            state["qa_requests"][alpha["id"]]["reviewed_commit"] = ""
            state["qa_requests"][alpha["id"]]["reviewed_tree_hash"] = ""
        with self.assertRaisesRegex(ValueError, "immutable commit/tree.*alpha"):
            board.start_subtask(self.root, self.delivery["id"], "beta")

    def test_crash_after_concurrent_start_preserves_exact_pipeline_state(self):
        self.declare()
        board.start_subtask(self.root, self.delivery["id"], "alpha")
        self.commit("alpha", "alpha/result.txt")
        self.request("alpha")
        script = (
            "import os,sys; from pathlib import Path; from harness import board; "
            "board.start_subtask(Path(sys.argv[1]), sys.argv[2], 'beta'); os._exit(73)"
        )
        crashed = subprocess.run(
            [sys.executable, "-c", script, str(self.root), self.delivery["id"]],
            cwd=Path(board.__file__).resolve().parents[1],
        )
        self.assertEqual(crashed.returncode, 73)
        restarted = subprocess.run(
            [sys.executable, str(Path(board.__file__)), "--root", str(self.root), "snapshot"],
            capture_output=True, text=True,
        )
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        state = json.loads(restarted.stdout)
        subtasks = state["delivery_plans"]["PIPELINE"]["subtasks"]
        self.assertEqual(set(subtasks), {"alpha", "beta"})
        self.assertEqual(subtasks["alpha"]["pipeline_status"], "in_review")
        self.assertEqual(subtasks["beta"]["pipeline_status"], "in_progress")
        board.start_subtask(self.root, self.delivery["id"], "beta")
        beta_starts = [
            event for event in board.snapshot(self.root)["events"]
            if event.get("kind") == "subtask_started" and event.get("subtask") == "beta"
        ]
        self.assertEqual(len(beta_starts), 1)

    def test_broker_commit_cannot_cross_declared_path_ownership(self):
        self.declare()
        board.start_subtask(self.root, self.delivery["id"], "beta")
        outside = self.workspace("beta") / "alpha" / "escape.txt"
        outside.parent.mkdir(parents=True)
        outside.write_text("escape\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ownership boundary"):
            board.broker_stage_commit(
                self.root, self.delivery["id"], ["alpha/escape.txt"],
                "escape beta ownership", subtask="beta",
            )

    def test_passed_subtask_is_folded_exactly_into_the_task_branch(self):
        self.declare()
        reviewer = self.reviewer()
        started = board.start_subtask(self.root, self.delivery["id"], "alpha")
        candidate = self.commit("alpha", "alpha/result.txt")
        request = self.request("alpha")

        passed = self.pass_request(reviewer, request, "alpha")

        state = board.snapshot(self.root)
        task_workspace = Path(state["task_workspaces"]["PIPELINE"])
        self.assertEqual(
            (task_workspace / "alpha" / "result.txt").read_text(),
            "alpha candidate\n",
        )
        self.assertTrue(passed["integrated_commit"])
        self.assertNotEqual(passed["integrated_commit"], candidate["commit"])
        parents = self._git(
            "-C", str(task_workspace), "show", "-s", "--format=%P",
            passed["integrated_commit"],
        ).split()
        self.assertEqual(parents, [started["base_commit"], candidate["commit"]])
        self.assertEqual(self._git("-C", str(task_workspace), "status", "--porcelain"), "")
        self.assertEqual(
            state["delivery_plans"]["PIPELINE"]["subtasks"]["alpha"]["integrated_commit"],
            passed["integrated_commit"],
        )

    def test_fold_crash_after_intent_recovers_without_losing_the_verdict(self):
        self.assert_fold_crash_recovers("intent")

    def test_fold_crash_after_git_mutation_recovers_without_duplicate_commit(self):
        self.assert_fold_crash_recovers("git_mutation")

    def test_fold_crash_after_board_mutation_recovers_without_duplicate_commit(self):
        self.assert_fold_crash_recovers("board_mutation")


if __name__ == "__main__":
    unittest.main()

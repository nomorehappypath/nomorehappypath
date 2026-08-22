# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import hashlib
import json
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from harness import board, board_viewer, contract, control
from tests.requirements_support import agreed_requirements


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "test_smoke.py").write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(True)\n")

    def tearDown(self):
        self.tmp.cleanup()

    def evidence(self, name: str) -> str:
        path = self.root / "evidence" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("command: python3 -m unittest\nresult: PASS\n")
        return str(path.relative_to(self.root))

    def ledger(self, name: str, result: str = "PASS") -> str:
        path = self.root / "docs" / name
        path.parent.mkdir(exist_ok=True)
        command = "python3 -m unittest test_smoke -k passes" if ("challenge" in name or "review" in name) else "python3 -m unittest test_smoke"
        observed = "PASS: targeted smoke simulation ran and the expected harness response was observed" if result == "PASS" else "Not executed"
        path.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            f"| S-001 | The {name} workflow preserves its expected behavior without side effects. | `{command}` | The targeted harness behavior is observed without side effects | {observed} | {result} |\n"
        )
        return str(path.relative_to(self.root))

    def qa_command(self) -> str:
        return "python3 -m unittest test_smoke"

    def declare_chunks(self, agent_id: str, chunks: list[tuple[str, str]]):
        board.define_delivery_plan(
            self.root, agent_id, "chunked",
            "Test fixture uses bounded reviewable outcomes",
        )
        return board.declare_chunks(self.root, agent_id, chunks)

    def atomic_plan(self, agent_id: str):
        return board.define_delivery_plan(
            self.root, agent_id, "atomic",
            "Test fixture is one cohesive compatibility task",
        )

    def delivery(self, task: str, vendor: str = "OpenAI", create_contract: bool = True):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor=vendor, session_id=session["id"])
        objective = f"OWNER DIRECTION — {task}"
        board.record_owner_direction(self.root, session["id"], objective)
        board.begin_task(self.root, agent["id"], task)
        agreed_requirements(self.root, agent["id"], f"Final agreed requirements for {task}: preserve the requested delivery and verify it end to end.")
        if create_contract:
            contract.create_contract(self.root, task, objective, ["delivery"])
        return agent

    def test_registration_poll_is_role_labelled_and_counted(self):
        dev = self.delivery("TASK-1")
        self.assertRegex(dev["id"], r"^development-0001-[0-9a-f]{6}$")
        first = board.poll(self.root, dev["id"])
        second = board.poll(self.root, dev["id"])
        self.assertEqual(first["role"], "development")
        self.assertEqual(first["poll_counter"], 1)
        self.assertEqual(second["poll_counter"], 2)

    def test_viewer_owner_direction_and_working_task_clarification_are_atomic_with_attachments(self):
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        direction = board.record_owner_message(
            self.root, dev["id"], "Review the harness from the viewer and preserve the full directive.", "direction",
            [{"filename": "requirements.png", "content_type": "image/png", "data": b"PNG-DIRECTION"}],
        )
        self.assertEqual(direction["message"]["type"], "direction")
        self.assertEqual(direction["message"]["task"], "")
        self.assertEqual(board.snapshot(self.root)["owner_directions"][session["id"]]["text"], "Review the harness from the viewer and preserve the full directive.")
        board.begin_task(self.root, dev["id"], "VIEWER-DIRECTION-TASK")
        contract.create_contract(self.root, "VIEWER-DIRECTION-TASK", "Review the harness from the viewer and preserve the full directive.", ["delivery"])
        agreed_requirements(self.root, dev["id"], "Final agreed requirements: preserve the directive, attachments, and review evidence.")
        clarification = board.record_owner_message(
            self.root, dev["id"], "Also verify the provider setting with a non-happy-path test.", "clarification",
            [{"filename": "notes.docx", "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "data": b"DOCX-CLARIFICATION"}],
        )
        state = board.snapshot(self.root)
        self.assertEqual(state["owner_clarifications"]["VIEWER-DIRECTION-TASK"][0]["text"], "Also verify the provider setting with a non-happy-path test.")
        self.assertEqual(len(clarification["message"]["attachments"]), 1)
        stored = self.root / clarification["message"]["attachments"][0]["stored_path"]
        self.assertEqual(stored.read_bytes(), b"DOCX-CLARIFICATION")
        queued = control.take_instructions(self.root, session["id"])
        self.assertEqual(len(queued), 2)
        self.assertIn("OWNER DIRECTION", queued[0]["text"])
        self.assertIn("OWNER CLARIFICATION", queued[1]["text"])

    def test_stopped_delivery_task_resumes_in_a_replacement_session_without_owner_reentry(self):
        source = self.delivery("TASK-RECOVERY")
        board.task_brief(self.root, source["id"], "Keep the recovered task alive and continue its review flow.", "Poll the board, retain the task, and resume the next review action.")
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-in-flight"] = {
                "id": "review-in-flight", "task": "TASK-RECOVERY", "developer_id": source["id"],
                "status": "open", "requested_at": board.now(), "cycle": 1,
                "phase": "final_acceptance", "subtask": "", "chunk": "final",
                "claimed_by": None, "review_wait_started_at": board.now(),
            }
        original_baseline = board.snapshot(self.root)["task_baselines"]["TASK-RECOVERY"]
        board.offline(self.root, source["id"], "visible CLI terminal ended", transport_ended=True)
        replacement_session = control.create(self.root, "codex_delivery")
        replacement = board.register(
            self.root,
            "engineering",
            board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI",
            session_id=replacement_session["id"],
        )
        event = board.resume_task(self.root, replacement["id"], source["id"], "TASK-RECOVERY")
        snapshot = board.snapshot(self.root)
        resumed = snapshot["agents"][replacement["id"]]
        self.assertEqual(event["kind"], "task_resumed")
        self.assertEqual(resumed["task"], "TASK-RECOVERY")
        self.assertEqual(resumed["recovery_context"]["owner_direction"], "OWNER DIRECTION — TASK-RECOVERY")
        self.assertEqual(resumed["recovery_context"]["next_action"], "Poll the board, retain the task, and resume the next review action.")
        self.assertEqual(snapshot["task_baselines"]["TASK-RECOVERY"], original_baseline)
        self.assertEqual(snapshot["qa_requests"]["review-in-flight"]["developer_id"], replacement["id"])
        self.assertEqual(snapshot["qa_requests"]["review-in-flight"]["developer_lineage"][0]["source_agent_id"], source["id"])
        self.assertIn("owner action is not required", event["message"])
        routed = control.take_instructions(self.root, replacement_session["id"])
        self.assertEqual(len(routed), 1)
        self.assertIn("TASK-RECOVERY", routed[0]["text"])
        self.assertIn("next review action", routed[0]["text"])
        for _ in range(3):
            board.poll(self.root, replacement["id"])
        live = board.snapshot(self.root)["agents"][replacement["id"]]
        self.assertTrue(live["active"])
        self.assertEqual(live["task"], "TASK-RECOVERY")
        self.assertEqual(live["recovery_context"]["next_action"], "Poll the board, retain the task, and resume the next review action.")
        predecessor = snapshot["agents"][source["id"]]
        self.assertFalse(predecessor["write_authority"])
        self.assertEqual(predecessor["superseded_by_agent_id"], replacement["id"])
        with self.assertRaisesRegex(ValueError, "superseded Delivery Agent is read-only"):
            board.status(self.root, source["id"], "late predecessor update")
        with self.assertRaisesRegex(ValueError, "superseded Delivery Agent is read-only"):
            board.request_qa(self.root, source["id"], self.ledger("late-source.md"), "late review request")
        source_control = next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == source["session_id"])
        self.assertTrue(source_control["read_only"])
        self.assertEqual(source_control["superseded_by_session_id"], replacement_session["id"])
        self.assertEqual(control.take_instructions(self.root, source["session_id"]), [])

    def test_failed_task_recovery_is_transactional_and_leaves_predecessor_unchanged(self):
        source = self.delivery("TASK-RECOVERY-ROLLBACK")
        board.offline(self.root, source["id"], "visible CLI terminal ended", transport_ended=True)
        replacement_session = control.create(self.root, "codex_delivery")
        replacement = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=replacement_session["id"])
        before = board.snapshot(self.root)
        with patch.object(board, "_event", side_effect=RuntimeError("injected durable recovery failure")):
            with self.assertRaisesRegex(RuntimeError, "injected durable recovery failure"):
                board.resume_task(self.root, replacement["id"], source["id"], "TASK-RECOVERY-ROLLBACK")
        after = board.snapshot(self.root)
        self.assertEqual(after["agents"][source["id"]], before["agents"][source["id"]])
        self.assertEqual(after["agents"][replacement["id"]]["task"], board.AWAITING_OWNER_DIRECTION)
        self.assertNotIn("TASK-RECOVERY-ROLLBACK", after.get("task_lineage", {}))

    def test_retained_task_workspace_attaches_through_board_operation(self):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Harness"], cwd=self.root, check=True)
        (self.root / ".gitignore").write_text(".harness/\n")
        (self.root / "tracked.txt").write_text("baseline\n")
        subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.root, check=True)
        dev = self.delivery("TASK-WORKSPACE-ATTACH")
        workspace = Path(board.snapshot(self.root)["task_workspaces"]["TASK-WORKSPACE-ATTACH"])
        state_path = self.root / ".harness" / "board" / "state.json"
        state = json.loads(state_path.read_text())
        state["task_workspaces"].pop("TASK-WORKSPACE-ATTACH")
        state_path.write_text(json.dumps(state))
        attached = board.attach_task_workspace(self.root, dev["id"], str(workspace))
        self.assertEqual(attached["kind"], "task_workspace_attached")
        self.assertEqual(board.snapshot(self.root)["task_workspaces"]["TASK-WORKSPACE-ATTACH"], str(workspace.resolve()))
        self.assertIn("review and QA will execute there", attached["message"])

    def test_owner_named_external_repository_binds_review_to_the_product_commit(self):
        repository = self.root / "product-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Harness"], cwd=repository, check=True)
        (repository / "test_external.py").write_text(
            "import unittest\n\nclass External(unittest.TestCase):\n    def test_product(self): self.assertTrue(True)\n"
        )
        (repository / "product.txt").write_text("baseline\n")
        subprocess.run(["git", "add", "test_external.py", "product.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repository, check=True)
        baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
        (repository / "product.txt").write_text("candidate\n")
        unrelated = repository / "owner-unrelated-secret.txt"
        unrelated.write_text("must not be opened by task intake or review\n")
        unrelated.chmod(0)
        self.addCleanup(lambda: unrelated.exists() and unrelated.chmod(0o600))

        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        objective = f"Verify the product candidate.\nWORK IN THIS FOLDER\n{repository}\nUse baseline HEAD {baseline}."
        board.record_owner_direction(self.root, session["id"], objective)
        begun = board.begin_task(self.root, dev["id"], "EXTERNAL-PRODUCT")
        task_workspace = Path(begun["task_workspace"])
        self.assertNotEqual(task_workspace, repository.resolve())
        self.assertTrue(task_workspace.resolve().is_relative_to((self.root.parent / ".harness-task-workspaces").resolve()))
        state = board.snapshot(self.root)
        self.assertEqual(Path(state["task_repositories"]["EXTERNAL-PRODUCT"]), repository.resolve())
        self.assertEqual(Path(state["task_workspaces"]["EXTERNAL-PRODUCT"]), task_workspace.resolve())
        self.assertEqual(state["task_baselines"]["EXTERNAL-PRODUCT"]["head"], baseline)
        self.assertIn("product.txt", state["task_baselines"]["EXTERNAL-PRODUCT"]["dirty_files"])

        contract.create_contract(self.root, "EXTERNAL-PRODUCT", objective, ["verify product candidate"])
        agreed_requirements(self.root, dev["id"], "Final agreed requirements: verify the external product candidate end to end.")
        board.define_delivery_plan(self.root, dev["id"], "atomic", "One cohesive external candidate verification")
        committed = board.broker_stage_commit(
            self.root, dev["id"], ["product.txt"], "candidate",
        )
        candidate = committed["commit"]
        ledger = self.root / "docs" / "external-ledger.md"
        ledger.parent.mkdir(exist_ok=True)
        ledger.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-EXT-1 | The external product behaves correctly in its exact target repository. | `python3 -m unittest test_external` | Product test passes in target repository | PASS: target repository test executed | PASS |\n"
        )
        request = board.request_review(
            self.root, dev["id"], str(ledger), "review exact external product commit",
            phase="final_acceptance", test_command="python3 -m unittest test_external",
        )
        self.assertEqual(request["reviewed_commit"], candidate)
        self.assertEqual(request["reviewed_files"], ["product.txt"])
        self.assertTrue(request["reviewed_worktree_digest"])
        self.assertTrue(request["mirror_ref"].startswith("refs/harness/EXTERNAL-PRODUCT/reviewed-"))
        self.assertFalse((repository / ".harness").exists())
        self.assertFalse((repository / "git-mirror").exists())
        self.assertFalse((repository / "broker-journal").exists())
        self.assertTrue((self.root / ".harness" / "git-mirror").is_dir())
        self.assertTrue((self.root / ".harness" / "broker-journal").is_dir())
        artifact = board._git_review_artifact(repository, baseline)
        self.assertIn("owner-unrelated-secret.txt", artifact["shared_worktree_dirty_files"])

    def test_pre_task_clarification_can_bind_the_owner_named_repository(self):
        repository = self.root / "clarified-product"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Harness"], cwd=repository, check=True)
        (repository / "product.txt").write_text("baseline\n")
        subprocess.run(["git", "add", "product.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repository, check=True)

        session = control.create(self.root, "codex_delivery")
        dev = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        original = "Implement the accepted project chat directive end to end."
        board.record_owner_message(self.root, dev["id"], original, "direction")
        clarification = (
            f"The exact implementation repository is {repository}. "
            f"The board root {self.root} is only the control plane. Go ahead."
        )
        board.record_owner_message(self.root, dev["id"], clarification, "clarification")

        begun = board.begin_task(self.root, dev["id"], "CLARIFIED-EXTERNAL")
        state = board.snapshot(self.root)
        workspace = Path(begun["task_workspace"])
        self.assertEqual(Path(state["task_repositories"]["CLARIFIED-EXTERNAL"]), repository.resolve())
        self.assertEqual(Path(state["task_workspaces"]["CLARIFIED-EXTERNAL"]), workspace.resolve())
        self.assertNotEqual(workspace.resolve(), repository.resolve())
        self.assertTrue(workspace.is_dir())
        self.assertEqual(state["task_owner_directions"]["CLARIFIED-EXTERNAL"], original)
        self.assertEqual(
            state["owner_clarifications"]["CLARIFIED-EXTERNAL"][0]["text"],
            clarification,
        )
        self.assertNotIn(session["id"], state["pending_owner_clarifications"])

    def test_ambiguous_pre_task_repository_clarification_does_not_choose_one(self):
        repositories = []
        for name in ("product-a", "product-b"):
            repository = self.root / name
            repository.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
            repositories.append(repository)

        session = control.create(self.root, "codex_delivery")
        dev = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_message(self.root, dev["id"], "Implement the requested change.", "direction")
        board.record_owner_message(
            self.root, dev["id"],
            f"Use either {repositories[0]} or {repositories[1]}.",
            "clarification",
        )

        begun = board.begin_task(self.root, dev["id"], "AMBIGUOUS-EXTERNAL")
        state = board.snapshot(self.root)
        self.assertNotIn("AMBIGUOUS-EXTERNAL", state.get("task_repositories", {}))
        self.assertEqual(begun["task_workspace"], "")
        self.assertEqual(begun["repository_binding"], "isolated broker task worktree")

    def test_legacy_task_can_bind_external_repository_before_review(self):
        repository = self.root / "legacy-product"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Harness"], cwd=repository, check=True)
        (repository / "product.txt").write_text("baseline\n")
        subprocess.run(["git", "add", "product.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repository, check=True)
        baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
        (repository / "product.txt").write_text("candidate\n")
        subprocess.run(["git", "add", "product.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "candidate"], cwd=repository, check=True)

        dev = self.delivery("LEGACY-EXTERNAL-BIND")
        event = board.bind_task_repository(self.root, dev["id"], str(repository), baseline)
        state = board.snapshot(self.root)
        self.assertEqual(event["kind"], "task_repository_bound")
        workspace = Path(state["task_workspaces"]["LEGACY-EXTERNAL-BIND"])
        self.assertNotEqual(workspace, repository.resolve())
        self.assertTrue(workspace.is_dir())
        self.assertEqual(event["branch"], "refs/heads/harness/tasks/LEGACY-EXTERNAL-BIND/task")
        self.assertEqual(state["task_baselines"]["LEGACY-EXTERNAL-BIND"]["head"], baseline)
        self.assertTrue(state["task_baselines"]["LEGACY-EXTERNAL-BIND"]["declared_baseline_verified"])

    def test_task_recovery_refuses_to_create_two_active_delivery_owners(self):
        source = self.delivery("TASK-NO-DUPLICATE")
        replacement_session = control.create(self.root, "codex_delivery")
        replacement = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=replacement_session["id"])
        with self.assertRaisesRegex(ValueError, "still active"):
            board.resume_task(self.root, replacement["id"], source["id"], "TASK-NO-DUPLICATE")

    def test_owner_stop_cancels_unfinished_delivery_without_leaving_a_phantom_task(self):
        dev = self.delivery("TASK-OWNER-CANCEL")
        self.atomic_plan(dev["id"])
        finding = board.record_finding(self.root, "TASK-OWNER-CANCEL", "Cancelled task finding", "This belongs only to the cancelled task", False)
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-cancelled"] = {
                "id": "review-cancelled", "task": "TASK-OWNER-CANCEL", "developer_id": dev["id"],
                "status": "open", "requested_at": board.now(), "cycle": 1,
                "phase": "final_acceptance", "subtask": "", "chunk": "final",
                "claimed_by": None, "review_wait_started_at": board.now(),
            }
        result = board.cancel_session_work(self.root, dev["session_id"])
        state = board.snapshot(self.root)
        self.assertEqual(result["cancelled_tasks"], ["TASK-OWNER-CANCEL"])
        self.assertEqual(state["agents"][dev["id"]]["status"], "cancelled")
        self.assertFalse(state["agents"][dev["id"]]["write_authority"])
        self.assertNotIn("review-cancelled", state["qa_requests"])
        self.assertNotIn("TASK-OWNER-CANCEL", state["delivery_plans"])
        self.assertNotIn(finding["id"], state["deferred_findings"])
        self.assertFalse((self.root / ".harness" / "tasks" / "TASK-OWNER-CANCEL.json").exists())
        self.assertEqual(board_viewer.dashboard_payload(self.root)["live_tasks"], [])
        self.assertEqual(board_viewer.history_payload(self.root)["task_history"], [])
        with self.assertRaisesRegex(ValueError, "superseded Delivery Agent is read-only"):
            board.status(self.root, dev["id"], "late mutation")

    def test_stopping_reviewer_reopens_claim_without_cancelling_delivery_task(self):
        dev = self.delivery("TASK-REVIEWER-STOP")
        reviewer_session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=reviewer_session["id"])
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-reopen"] = {
                "id": "review-reopen", "task": "TASK-REVIEWER-STOP", "developer_id": dev["id"],
                "status": "claimed", "requested_at": board.now(), "cycle": 1,
                "phase": "final_acceptance", "subtask": "", "chunk": "final",
                "claimed_by": reviewer["id"], "claimed_at": board.now(),
                "reserved_by": reviewer["id"], "reserved_at": board.now(),
                "review_wait_started_at": board.now(), "challenge_ledger": "challenge.md",
            }
        result = board.cancel_session_work(self.root, reviewer_session["id"])
        state = board.snapshot(self.root)
        self.assertEqual(result["cancelled_tasks"], [])
        self.assertEqual(state["qa_requests"]["review-reopen"]["status"], "open")
        self.assertIsNone(state["qa_requests"]["review-reopen"]["claimed_by"])
        self.assertIsNone(state["qa_requests"]["review-reopen"]["challenge_ledger"])
        self.assertNotIn("TASK-REVIEWER-STOP", state["cancelled_tasks"])
        self.assertTrue((self.root / ".harness" / "tasks" / "TASK-REVIEWER-STOP.json").is_file())

    def test_only_cto_can_record_release_and_only_after_every_gate_passes(self):
        dev = self.delivery("TASK-RELEASE")
        cto_agent = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        with self.assertRaisesRegex(ValueError, "only the CTO"):
            board.record_release_ready(self.root, dev["id"], "TASK-RELEASE", checks)
        checks["git_clean"] = False
        with self.assertRaisesRegex(ValueError, "git_clean"):
            board.record_release_ready(self.root, cto_agent["id"], "TASK-RELEASE", checks)
        checks["git_clean"] = True
        recorded = board.record_release_ready(self.root, cto_agent["id"], "TASK-RELEASE", checks)
        self.assertEqual(recorded["status"], "VISUAL_TEST_REQUIRED")
        self.assertEqual(board.snapshot(self.root)["releases"]["TASK-RELEASE"]["cto_id"], cto_agent["id"])

    def test_cto_can_repin_passed_final_review_only_to_an_identical_git_tree(self):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Harness"], cwd=self.root, check=True)
        (self.root / ".gitignore").write_text(".harness/\n")
        (self.root / "app.txt").write_text("reviewed bytes\n")
        subprocess.run(["git", "add", ".gitignore", "app.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "reviewed candidate"], cwd=self.root, check=True)
        reviewed = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        subprocess.run(["git", "commit", "--allow-empty", "-qm", "integration metadata"], cwd=self.root, check=True)
        identical = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        cto_agent = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        dev = self.delivery("TASK-REPIN")
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-TASK-REPIN-final-01"] = {
                "id": "review-TASK-REPIN-final-01", "task": "TASK-REPIN", "cycle": 1,
                "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance",
                "status": "passed", "reviewed_commit": reviewed,
                "developer_id": dev["id"], "structure_revision": 0,
                "requested_at": board.now(), "claimed_by": "qa-reviewed",
                "review_wait_started_at": board.now(), "chunk": "final", "subtask": "",
            }
        result = board.repin_final_review(self.root, cto_agent["id"], "TASK-REPIN", identical)
        request = board.snapshot(self.root)["qa_requests"][result["request_id"]]
        self.assertEqual(request["reviewed_commit"], identical)
        self.assertEqual(result["verification"]["from_commit"], reviewed)
        self.assertTrue(result["verification"]["board_verified"])
        self.assertEqual(
            subprocess.check_output(["git", "rev-parse", f"{reviewed}^{{tree}}"], cwd=self.root, text=True).strip(),
            result["verification"]["tree_hash"],
        )

        (self.root / "app.txt").write_text("one changed byte\n")
        subprocess.run(["git", "add", "app.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "changed candidate"], cwd=self.root, check=True)
        changed = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        with self.assertRaisesRegex(ValueError, "tree differs"):
            board.repin_final_review(self.root, cto_agent["id"], "TASK-REPIN", changed)
        with self.assertRaisesRegex(ValueError, "only the CTO"):
            board.repin_final_review(self.root, dev["id"], "TASK-REPIN", identical)

    def test_missing_heartbeat_is_visible_as_stalled_and_a_poll_recovers_it(self):
        dev = self.delivery("TASK-LIVE")
        state_path = self.root / ".harness" / "board" / "state.json"
        state = json.loads(state_path.read_text())
        state["agents"][dev["id"]]["spawned_at"] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        state["agents"][dev["id"]]["last_progress_at"] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        state_path.write_text(json.dumps(state))
        self.assertEqual(board.mark_stalled(self.root, stale_seconds=90), [])
        recovering = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertEqual(recovering["liveness"], "recovering")
        self.assertEqual(recovering["recovery_state"], "automatic_requested")
        routed = control.take_instructions(self.root, dev["session_id"])
        self.assertEqual(len(routed), 1)
        self.assertIn("TASK ACTION DUE", routed[0]["text"])
        with board.locked_state(self.root) as state:
            recovery_requested_at = (
                datetime.now(timezone.utc)
                - timedelta(seconds=board.AUTO_RECOVERY_GRACE_SECONDS + 1)
            )
            old_heartbeat = recovery_requested_at - timedelta(seconds=1)
            state["agents"][dev["id"]].update({
                "automatic_recovery_requested_at": recovery_requested_at.isoformat(),
                "spawned_at": old_heartbeat.isoformat(),
                "last_progress_at": old_heartbeat.isoformat(),
                "last_poll_at": old_heartbeat.isoformat(),
            })
        stalled = board.mark_stalled(self.root, stale_seconds=90)
        self.assertEqual(stalled[0]["kind"], "agent_stalled")
        self.assertEqual(board.snapshot(self.root)["agents"][dev["id"]]["liveness"], "stalled")
        board.poll(self.root, dev["id"])
        snapshot = board.snapshot(self.root)
        self.assertEqual(snapshot["agents"][dev["id"]]["liveness"], "healthy")
        self.assertTrue(any(event["kind"] == "agent_recovered" for event in snapshot["events"]))

    def test_observed_93_to_99_second_cadence_never_flaps_over_ten_minutes(self):
        dev = self.delivery("TASK-NATURAL-CADENCE")
        observed_gaps = [93, 99, 96, 94, 98, 95, 97]
        for elapsed in range(0, 601, board.WATCHDOG_INTERVAL_SECONDS):
            gap = observed_gaps[(elapsed // board.WATCHDOG_INTERVAL_SECONDS) % len(observed_gaps)]
            with board.locked_state(self.root) as state:
                heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=gap)).isoformat()
                state["agents"][dev["id"]].update({
                    "spawned_at": heartbeat, "last_poll_at": heartbeat,
                    "last_progress_at": heartbeat, "liveness": "healthy",
                })
            self.assertEqual(board.mark_stalled(self.root), [], f"false stall at replay second {elapsed}")
        events = board.snapshot(self.root)["events"]
        self.assertFalse(any(event["kind"] == "agent_stalled" for event in events))

    def test_five_minute_silence_stalls_once_and_recovers_once(self):
        dev = self.delivery("TASK-DEAD-CADENCE")
        old = (datetime.now(timezone.utc) - timedelta(minutes=5, seconds=1)).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"][dev["id"]].update({
                "spawned_at": old, "last_poll_at": old, "last_progress_at": old,
            })
        self.assertEqual(board.mark_stalled(self.root), [])
        with board.locked_state(self.root) as state:
            state["agents"][dev["id"]]["automatic_recovery_requested_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=board.AUTO_RECOVERY_GRACE_SECONDS + 1)
            ).isoformat()
        self.assertEqual(len(board.mark_stalled(self.root)), 1)
        self.assertEqual(board.mark_stalled(self.root), [])
        board.poll(self.root, dev["id"])
        events = board.snapshot(self.root)["events"]
        self.assertEqual(sum(event["kind"] == "agent_stalled" for event in events), 1)
        self.assertEqual(sum(event["kind"] == "agent_recovered" for event in events), 1)

    def test_idle_reviewer_and_delivery_intake_are_healthy_standby(self):
        delivery_session = control.create(self.root, "codex_delivery")
        delivery = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id=delivery_session["id"])
        reviewer_session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", session_id=reviewer_session["id"])
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with board.locked_state(self.root) as state:
            for agent_id in (delivery["id"], reviewer["id"]):
                state["agents"][agent_id].update({
                    "spawned_at": old, "last_poll_at": old,
                    "last_progress_at": old, "liveness": "stalled",
                })
        self.assertEqual(board.mark_stalled(self.root), [])
        state = board.snapshot(self.root)
        self.assertEqual(state["agents"][delivery["id"]]["liveness"], "healthy")
        self.assertEqual(state["agents"][reviewer["id"]]["liveness"], "healthy")
        self.assertEqual(control.take_instructions(self.root, delivery_session["id"]), [])
        self.assertEqual(control.take_instructions(self.root, reviewer_session["id"]), [])

    def test_cto_wakes_for_active_delivery_on_bounded_monitoring_lease(self):
        """A healthy active task is CTO work, but only after the lease expires."""
        self.delivery("TASK-CTO-WAKE")
        cto_session = control.create(self.root, "claude_cto")
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", session_id=cto_session["id"])
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"][cto["id"]].update({
                "spawned_at": old, "last_poll_at": old, "last_progress_at": old,
            })
        self.assertEqual(board.mark_stalled(self.root), [])
        current = board.snapshot(self.root)["agents"][cto["id"]]
        self.assertEqual(current["liveness"], "recovering")
        routed = control.take_instructions(self.root, cto_session["id"])
        self.assertEqual(len(routed), 1)
        self.assertIn("MONITORING CYCLE DUE", routed[0]["text"])
        self.assertEqual(routed[0]["source"], "cto-monitoring-lease")

    def test_delivery_waiting_on_routed_review_is_healthy_standby(self):
        dev = self.delivery("TASK-REVIEW-STANDBY")
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-wait"] = {
                "id": "review-wait", "task": "TASK-REVIEW-STANDBY",
                "developer_id": dev["id"], "status": "claimed",
                "claimed_by": "qa-agent", "requested_at": board.now(),
                "phase": "final_acceptance", "cycle": 1,
                "review_wait_started_at": board.now(),
            }
            old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            state["agents"][dev["id"]].update({
                "spawned_at": old, "last_poll_at": old,
                "last_progress_at": old, "liveness": "stalled",
            })
        self.assertEqual(board.mark_stalled(self.root), [])
        self.assertEqual(board.snapshot(self.root)["agents"][dev["id"]]["liveness"], "healthy")
        self.assertEqual(control.take_instructions(self.root, dev["session_id"]), [])

    def test_recent_terminal_output_does_not_replace_board_heartbeat(self):
        dev = self.delivery("TASK-OUTPUT-CADENCE")
        old = (datetime.now(timezone.utc) - timedelta(minutes=5, seconds=1)).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"][dev["id"]].update({
                "spawned_at": old, "last_poll_at": old, "last_progress_at": old,
            })
        with control.locked_state(self.root) as state:
            state["sessions"][dev["session_id"]]["last_output_at"] = datetime.now(timezone.utc).isoformat()
        self.assertEqual(board.mark_stalled(self.root), [])
        current = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertEqual(current["liveness"], "recovering")
        self.assertEqual(current["recovery_state"], "automatic_requested")

    def test_recent_recorded_progress_prevents_false_stall_without_a_fresh_poll(self):
        dev = self.delivery("TASK-PROGRESS-LEASE")
        state_path = self.root / ".harness" / "board" / "state.json"
        state = json.loads(state_path.read_text())
        old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        state["agents"][dev["id"]].update({"spawned_at": old, "last_poll_at": old, "last_progress_at": datetime.now(timezone.utc).isoformat()})
        state_path.write_text(json.dumps(state))
        self.assertEqual(board.mark_stalled(self.root, stale_seconds=90), [])

    def test_delivery_agent_can_hold_a_work_execution_lease(self):
        dev = self.delivery("TASK-WORK-LEASE")
        started = board.review_execution_start(self.root, dev["id"], "delivery-check-1", "python3 -m unittest test_smoke")
        self.assertIn("executing a long check", started["message"])
        self.assertEqual(board.mark_stalled(self.root, stale_seconds=90), [])
        self.assertTrue(board.review_execution_finish(self.root, dev["id"], "delivery-check-1"))

    def test_blocker_reset_preserves_memory_routes_terminal_and_recovers_on_poll(self):
        dev = self.delivery("TASK-BLOCKER-RESET")
        board.status(self.root, dev["id"], "Review failed; repair the timeout path", "blocked")
        event = board.request_recovery(self.root, dev["id"])
        recovering = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertEqual(event["kind"], "agent_recovery_requested")
        self.assertEqual(recovering["task"], "TASK-BLOCKER-RESET")
        self.assertEqual(recovering["recovery_state"], "reset_requested")
        self.assertEqual(recovering["recovery_context"]["previous_status_note"], "Review failed; repair the timeout path")
        routed = control.take_instructions(self.root, dev["session_id"])
        self.assertEqual(len(routed), 1)
        self.assertIn("TASK-BLOCKER-RESET", routed[0]["text"])
        board.poll(self.root, dev["id"])
        resumed = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertEqual(resumed["recovery_state"], "resumed")
        self.assertEqual(resumed["liveness"], "healthy")
        self.assertEqual(resumed["status"], "working")

    def test_recovery_against_dead_managed_session_keeps_blocker_and_control_visible(self):
        session = control.create(self.root, "codex_delivery")
        control.attach(self.root, session["id"], 99999999)
        dev = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "Recover the blocked delivery without losing its task memory.")
        board.begin_task(self.root, dev["id"], "TASK-DEAD-RECOVERY")
        board.status(self.root, dev["id"], "Review failed; the managed terminal has exited", "blocked")

        with self.assertRaisesRegex(ValueError, "inactive managed session"):
            board.request_recovery(self.root, dev["id"])

        current = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertEqual(current["task"], "TASK-DEAD-RECOVERY")
        self.assertEqual(current["status"], "blocked")
        self.assertNotEqual(current.get("liveness"), "recovering")
        self.assertNotIn("recovery_state", current)
        self.assertTrue(any(event["kind"] == "agent_recovery_failed" for event in board.snapshot(self.root)["events"]))

    def test_poll_from_exited_managed_session_is_refused_without_heartbeat(self):
        session = control.create(self.root, "codex_delivery")
        control.attach(self.root, session["id"], 99999999)
        dev = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "Do not let an exited session keep polling the board.")
        board.begin_task(self.root, dev["id"], "TASK-GHOST-POLLER")

        with self.assertRaisesRegex(ValueError, "managed session is inactive"):
            board.poll(self.root, dev["id"])

        current = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertFalse(current["active"])
        self.assertEqual(current["status"], "offline")
        self.assertEqual(current["poll_counter"], 0)
        self.assertTrue(any(event["kind"] == "board_poll_refused" for event in board.snapshot(self.root)["events"]))

    def test_dirty_candidate_review_executes_attached_workspace_not_board_root(self):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Harness"], cwd=self.root, check=True)
        (self.root / "tracked.txt").write_text("baseline\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.root, check=True)
        workspace = self.root.parent / f"candidate-{self.root.name}"
        subprocess.run(["git", "worktree", "add", "--detach", "-q", str(workspace), "HEAD"], cwd=self.root, check=True)
        try:
            (workspace / "candidate-only.txt").write_text("dirty candidate\n")
            source = workspace / "docs" / "challenge.md"
            source.parent.mkdir(parents=True)
            source.write_text("candidate challenge\n")
            artifact = board._git_review_artifact(workspace)
            self.assertEqual(set(artifact["files"]), {"candidate-only.txt", "docs/challenge.md"})
            self.assertNotIn("tracked.txt", artifact["files"])
            request = {
                "task": "TASK-DIRTY-CANDIDATE",
                "stage": board.INDEPENDENT_REVIEW,
                "reviewed_commit": "",
                "reviewed_worktree_digest": artifact["working_tree_digest"],
            }
            state = {"task_workspaces": {"TASK-DIRTY-CANDIDATE": str(workspace)}}
            with board._review_candidate_checkout(self.root, state, request, source) as (candidate_root, candidate_ledger):
                self.assertEqual(candidate_root.resolve(), workspace.resolve())
                self.assertEqual(candidate_ledger.resolve(), source.resolve())
                self.assertNotEqual(candidate_root.resolve(), self.root.resolve())
            missing_digest = dict(request)
            missing_digest.pop("reviewed_worktree_digest")
            with self.assertRaisesRegex(ValueError, "reviewed_worktree_digest"):
                with board._review_candidate_checkout(self.root, state, missing_digest, source):
                    pass
            development_qa = {"task": "TASK-DIRTY-CANDIDATE", "stage": board.DEVELOPMENT_QA, "reviewed_commit": ""}
            with board._review_candidate_checkout(self.root, state, development_qa, source) as (candidate_root, candidate_ledger):
                self.assertEqual(candidate_root.resolve(), workspace.resolve())
                self.assertEqual(candidate_ledger.resolve(), source.resolve())
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(workspace)], cwd=self.root, check=False, capture_output=True, text=True)

    def test_internal_qa_does_not_block_another_agent_heartbeat(self):
        dev = self.delivery("TASK-CONCURRENT")
        observer = board.register(self.root, "qa", "QA-QUEUE", vendor="Anthropic")
        self.declare_chunks(dev["id"], [("core", "focused behavior")])
        ledger = self.ledger("concurrent.md")
        (self.root / "test_slow_qa.py").write_text(
            "import time\n"
            "import unittest\n"
            "from pathlib import Path\n\n"
            "class SlowQA(unittest.TestCase):\n"
            "    def test_marker_and_sleep(self):\n"
            "        Path('qa-started').write_text('1')\n"
            "        time.sleep(1)\n"
        )
        command = "python3 -m unittest test_slow_qa"
        failure = []
        worker = threading.Thread(target=lambda: self._request_review(failure, dev["id"], ledger, command))
        worker.start()
        for _ in range(20):
            if (self.root / "qa-started").exists():
                break
            time.sleep(.05)
        else:
            self.fail("internal QA command did not start")
        started = time.monotonic()
        board.poll(self.root, observer["id"])
        self.assertLess(time.monotonic() - started, .5, "board polling was blocked by a running QA suite")
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failure, [])

    def _request_review(self, failure: list[Exception], agent_id: str, ledger: str, command: str) -> None:
        try:
            board.request_review(self.root, agent_id, ledger, "concurrent review", chunk="core", test_command=command)
        except Exception as error:  # surfaced back to the test thread
            failure.append(error)

    def test_standing_by_delivery_agent_cannot_do_work_until_product_management_begins_task(self):
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, "Delivery Agent", session_id=session["id"])
        with self.assertRaises(ValueError):
            self.declare_chunks(dev["id"], [("guessed", "must not invent work")])
        board.record_owner_direction(self.root, session["id"], "OWNER DIRECTION — designed work")
        begun = board.begin_task(self.root, dev["id"], "OWNER-DIRECTED-TASK")
        self.assertEqual(begun["task"], "OWNER-DIRECTED-TASK")
        contract.create_contract(self.root, "OWNER-DIRECTED-TASK", "OWNER DIRECTION — designed work", ["delivery"])
        agreed_requirements(self.root, dev["id"], "Final agreed requirements: implement the designed owner-directed work and verify it.")
        self.declare_chunks(dev["id"], [("designed", "owner-directed work")])

    def test_delivery_can_declare_a_newly_discovered_chunk_without_rewriting_existing_state(self):
        dev = self.delivery("TASK-ADDITIONAL-CHUNK")
        self.declare_chunks(dev["id"], [("first", "initial focused outcome")])
        state_path = self.root / ".harness" / "board" / "state.json"
        state = json.loads(state_path.read_text())
        state["task_chunks"]["TASK-ADDITIONAL-CHUNK"]["first"]["status"] = "passed"
        state_path.write_text(json.dumps(state))
        chunks = board.declare_chunks(
            self.root, dev["id"], [("discovered", "later review-blocking outcome")],
            reason="A newly discovered review-blocking outcome requires its own check.",
        )
        self.assertEqual(chunks["first"]["status"], "passed")
        self.assertEqual(chunks["discovered"]["status"], "open")
        with self.assertRaisesRegex(ValueError, "already declared: discovered"):
            board.declare_chunks(
                self.root, dev["id"], [("discovered", "must not overwrite")],
                reason="A duplicate declaration must still be rejected.",
            )

    def test_managed_delivery_requires_recorded_owner_terminal_direction(self):
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        with self.assertRaisesRegex(ValueError, "owner terminal direction"):
            board.begin_task(self.root, dev["id"], "INVENTED-TASK")
        event = board.record_owner_direction(self.root, session["id"], "Make the viewer clearly show stalled delivery")
        self.assertEqual(event["kind"], "owner_direction_received")
        board.begin_task(self.root, dev["id"], "OWNER-DIRECTED-TASK")
        with self.assertRaisesRegex(ValueError, "already has an active task"):
            board.record_owner_direction(self.root, session["id"], "second direction")

    def test_later_terminal_approval_cannot_replace_mission_control_direction(self):
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        original = "Build provider settings and preserve every existing directive."
        board.record_owner_message(self.root, dev["id"], original)
        with self.assertRaisesRegex(ValueError, "already has an owner direction"):
            board.record_owner_direction(self.root, session["id"], "go ahead")
        begun = board.begin_task(self.root, dev["id"], "PROVIDER-SETTINGS")
        self.assertEqual(begun["owner_direction"], original)
        self.assertEqual(board.owner_direction_for_task(board.snapshot(self.root), dev["id"], "PROVIDER-SETTINGS"), original)

    def test_unconsumed_direction_moves_to_replacement_delivery_terminal(self):
        source_session = control.create(self.root, "codex_delivery")
        source = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=source_session["id"])
        original = "Preserve this complete request even if this terminal exits before task creation."
        board.record_owner_message(self.root, source["id"], original)
        board.offline(self.root, source["id"], "terminal exited before Product Management began the task", transport_ended=True)

        replacement_session = control.create(self.root, "codex_delivery")
        replacement = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=replacement_session["id"],
        )
        state = board.snapshot(self.root)
        recovered = state["owner_directions"][replacement_session["id"]]
        self.assertEqual(recovered["text"], original)
        self.assertEqual(recovered["recovered_from_agent_id"], source["id"])
        self.assertEqual(replacement["status"], "direction_recovered")
        self.assertEqual(
            state["owner_directions"][source_session["id"]]["transferred_to_agent_id"],
            replacement["id"],
        )
        routed = control.take_instructions(self.root, replacement_session["id"])
        self.assertEqual(len(routed), 1)
        self.assertIn(original, routed[0]["text"])
        begun = board.begin_task(self.root, replacement["id"], "RECOVERED-PRE-TASK-DIRECTION")
        self.assertEqual(begun["owner_direction"], original)
        self.assertEqual(
            board.owner_direction_for_task(board.snapshot(self.root), replacement["id"], "RECOVERED-PRE-TASK-DIRECTION"),
            original,
        )

    def test_old_unconsumed_direction_is_not_attached_to_unrelated_fresh_session(self):
        source_session = control.create(self.root, "codex_delivery")
        source = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=source_session["id"])
        board.record_owner_direction(self.root, source_session["id"], "An old request that was abandoned hours ago.")
        board.offline(self.root, source["id"], "old terminal ended", transport_ended=True)
        with board.locked_state(self.root) as state:
            state["agents"][source["id"]]["last_status_at"] = "2000-01-01T00:00:00+00:00"

        replacement_session = control.create(self.root, "codex_delivery")
        replacement = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=replacement_session["id"],
        )
        state = board.snapshot(self.root)
        self.assertNotIn(replacement_session["id"], state["owner_directions"])
        self.assertEqual(replacement["status"], "spawned")
        self.assertEqual(control.take_instructions(self.root, replacement_session["id"]), [])

    def test_live_managed_terminal_cannot_hide_itself_by_declaring_offline(self):
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        with self.assertRaisesRegex(ValueError, "live managed terminal cannot declare itself offline"):
            board.offline(self.root, dev["id"], "No new owner direction; going offline.")
        current = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertTrue(current["active"])
        self.assertNotEqual(current["status"], "offline")

    def test_task_start_captures_dirty_baseline_without_owner_action(self):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Harness"], cwd=self.root, check=True)
        (self.root / ".gitignore").write_text(".harness/\ntest_smoke.py\n")
        inherited = self.root / "inherited.txt"
        inherited.write_text("committed\n")
        subprocess.run(["git", "add", ".gitignore", "inherited.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.root, check=True)
        inherited.write_text("pre-existing dirty work\n")
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "Continue without asking me to classify Git changes")
        event = board.begin_task(self.root, dev["id"], "TASK-INHERITED")
        baseline = board.snapshot(self.root)["task_baselines"]["TASK-INHERITED"]
        self.assertEqual(baseline["dirty_files"], ["inherited.txt"])
        self.assertEqual(inherited.read_text(), "pre-existing dirty work\n")
        self.assertFalse(baseline["requires_owner_action"])
        self.assertIn("owner action is not required", event["message"])
        (self.root / "after-task.txt").write_text("new task work\n")
        self.assertEqual(board.snapshot(self.root)["task_baselines"]["TASK-INHERITED"], baseline)

    def test_recovered_baseline_is_one_time_and_never_requests_owner_classification(self):
        dev = self.delivery("TASK-LEGACY")
        state_path = self.root / ".harness" / "board" / "state.json"
        state = json.loads(state_path.read_text())
        state.setdefault("task_baselines", {}).pop("TASK-LEGACY", None)
        state_path.write_text(json.dumps(state))
        recovered = board.reconcile_inherited_baseline(self.root, dev["id"])
        self.assertIn("owner action is not required", recovered["message"])
        with self.assertRaisesRegex(ValueError, "immutable"):
            board.reconcile_inherited_baseline(self.root, dev["id"])

    def test_delivery_publishes_a_short_human_facing_plan_and_update(self):
        dev = self.delivery("TASK-BRIEF")
        brief = board.task_brief(self.root, dev["id"], "I will harden task gating, then test the full review flow.", "Inspecting the current safeguards before changing them.")
        self.assertEqual(brief["plan"], "I will harden task gating, then test the full review flow.")
        self.assertEqual(board.snapshot(self.root)["task_briefs"]["TASK-BRIEF"]["update"], "Inspecting the current safeguards before changing them.")
        reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        with self.assertRaisesRegex(ValueError, "Delivery Agent"):
            board.task_brief(self.root, reviewer["id"], "not allowed", "not allowed")

    def test_failed_qa_is_routed_back_to_the_managed_delivery_terminal(self):
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "OWNER DIRECTION — TASK-ROUTE")
        board.begin_task(self.root, dev["id"], "TASK-ROUTE")
        contract.create_contract(self.root, "TASK-ROUTE", "OWNER DIRECTION — TASK-ROUTE", ["delivery"])
        agreed_requirements(self.root, dev["id"], "Final agreed requirements: route the QA result and repair the requested task.")
        self.atomic_plan(dev["id"])
        qa = board.register(self.root, "qa", "QA-QUEUE", vendor="Anthropic")
        request = board.request_qa(self.root, dev["id"], self.ledger("route.md"), "run QA")
        board.claim_qa(self.root, qa["id"], request["id"])
        board.qa_result(self.root, qa["id"], request["id"], "failed", "S-1 failed", self.evidence("route.txt"))
        routed = control.take_instructions(self.root, session["id"])
        self.assertEqual(len(routed), 1)
        self.assertIn("FAILED", routed[0]["text"])

    def test_board_is_directly_executable_without_pythonpath_setup(self):
        script = Path(__file__).resolve().parents[1] / "harness" / "board.py"
        result = subprocess.run(["python3", str(script), "--root", str(self.root), "view"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Live Harness Board", result.stdout)

    def test_developer_qa_clock_and_retest_cycle(self):
        dev = self.delivery("TASK-2")
        self.atomic_plan(dev["id"])
        qa = board.register(self.root, "qa", "QA-QUEUE")
        ledger = self.ledger("ledger.md")
        first = board.request_qa(self.root, dev["id"], ledger, "first QA request")
        self.assertEqual(first["cycle"], 1)
        self.assertIsNone(first["review_wait_stopped_at"])
        claimed = board.claim_qa(self.root, qa["id"])
        failed = board.qa_result(self.root, qa["id"], claimed["id"], "failed", "S-004 fails after timeout", self.evidence("qa-cycle-1.txt"))
        self.assertEqual(failed["review_wait_stopped_at"], failed["completed_at"])
        events = board.poll(self.root, dev["id"])["events"]
        self.assertTrue(any(event["kind"] == "qa_result" and event["result"] == "failed" for event in events))
        with self.assertRaises(ValueError):
            board.request_qa(self.root, dev["id"], ledger, "missing change summary")
        second = board.request_qa(self.root, dev["id"], ledger, "fixed timeout; request retest", changes="bounded retry added")
        self.assertEqual(second["cycle"], 2)
        self.assertEqual(second["status"], "open")

    def test_only_developer_can_mark_implementation_complete(self):
        contract.create_contract(self.root, "TASK-2B", "OWNER DIRECTION — TASK-2B", ["delivery"])
        proof = self.root / "delivery-proof.txt"
        proof.write_text("all deliverables proven\n")
        contract.add_evidence(self.root, "TASK-2B", "delivery", [proof])
        dev = self.delivery("TASK-2B", create_contract=False)
        qa = board.register(self.root, "qa", "QA-QUEUE", vendor="Anthropic")
        self.declare_chunks(dev["id"], [("delivery", "focused delivery")])
        ledger = self.ledger("ledger.md")
        chunk = board.request_review(self.root, dev["id"], ledger, "chunk review", chunk="delivery", test_command=self.qa_command())
        board.claim_qa(self.root, qa["id"], chunk["id"], self.ledger("chunk-challenge.md"))
        board.execute_challenge(self.root, qa["id"], chunk["id"])
        board.qa_result(self.root, qa["id"], chunk["id"], "passed", "chunk passed", self.evidence("chunk.txt"))
        final = board.request_review(self.root, dev["id"], ledger, "final review", phase="final_acceptance", test_command=self.qa_command())
        board.claim_qa(self.root, qa["id"], final["id"], self.ledger("final-challenge.md"))
        board.execute_challenge(self.root, qa["id"], final["id"])
        board.qa_result(self.root, qa["id"], final["id"], "passed", "final passed", self.evidence("final.txt"))
        board.complete(self.root, dev["id"], "merged candidate and evidence ready")
        self.assertEqual(board.snapshot(self.root)["agents"][dev["id"]]["status"], "done")
        with self.assertRaises(ValueError):
            board.complete(self.root, qa["id"], "not allowed")

    def test_only_qa_can_claim_and_report_qa(self):
        dev = self.delivery("TASK-3")
        self.atomic_plan(dev["id"])
        request = board.request_qa(self.root, dev["id"], self.ledger("ledger.md"), "ready")
        with self.assertRaises(ValueError):
            board.claim_qa(self.root, dev["id"], request["id"])
        qa = board.register(self.root, "qa", "QA-QUEUE")
        board.claim_qa(self.root, qa["id"], request["id"])
        with self.assertRaises(ValueError):
            board.qa_result(self.root, dev["id"], request["id"], "passed", "not allowed", self.evidence("forbidden.txt"))

    def test_open_review_actively_wakes_one_eligible_managed_reviewer(self):
        dev = self.delivery("TASK-REVIEW-WAKE")
        self.declare_chunks(dev["id"], [("wake", "wake a waiting reviewer without an agent polling loop")])
        reviewer_session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(
            self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic",
            session_id=reviewer_session["id"],
        )

        request = board.request_review(
            self.root, dev["id"], self.ledger("wake.md"), "review the wake path",
            chunk="wake", test_command=self.qa_command(),
        )

        self.assertEqual(request["status"], "open")
        self.assertEqual(request["routed_to"], reviewer["id"])
        self.assertEqual(request["route_state"], "routed")
        self.assertEqual(request["route_attempts"], 1)
        self.assertEqual(request["route_transport_state"], "instruction_queued")
        self.assertTrue(request["route_instruction_id"])
        self.assertEqual(
            control.instruction_receipt(self.root, request["route_instruction_id"])["status"],
            "queued",
        )
        inbox = control.take_instructions(self.root, reviewer_session["id"])
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["source"], "review-assignment")
        self.assertIn(request["id"], inbox[0]["text"])
        self.assertIn("one bounded board poll", inbox[0]["text"])
        self.assertIn("Do not start or continue any polling", inbox[0]["text"])

    def test_review_route_retries_without_duplicate_prompt_before_deadline(self):
        dev = self.delivery("TASK-REVIEW-RETRY")
        self.declare_chunks(dev["id"], [("retry", "retry an unclaimed routed review")])
        reviewer_session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=reviewer_session["id"])
        request = board.request_review(
            self.root, dev["id"], self.ledger("retry.md"), "review retry routing",
            chunk="retry", test_command=self.qa_command(),
        )
        control.take_instructions(self.root, reviewer_session["id"])

        self.assertEqual(board.route_open_reviews(self.root, retry_seconds=90), [])
        self.assertEqual(control.take_instructions(self.root, reviewer_session["id"]), [])

        state_path = self.root / ".harness" / "board" / "state.json"
        state = json.loads(state_path.read_text())
        overdue = (datetime.now(timezone.utc) - timedelta(seconds=91)).isoformat()
        state["qa_requests"][request["id"]]["routed_at"] = overdue
        state["agents"][reviewer["id"]]["last_status_at"] = overdue
        state_path.write_text(json.dumps(state))
        retried = board.route_open_reviews(self.root, retry_seconds=90)

        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]["agent_id"], reviewer["id"])
        self.assertEqual(retried[0]["route_attempt"], 2)
        self.assertEqual(len(control.take_instructions(self.root, reviewer_session["id"])), 1)

    def test_reviewer_progress_update_extends_the_unclaimed_route_deadline(self):
        dev = self.delivery("TASK-REVIEW-PROGRESS")
        self.declare_chunks(dev["id"], [("progress", "do not interrupt active Challenge Ledger preparation")])
        reviewer_session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=reviewer_session["id"])
        request = board.request_review(
            self.root, dev["id"], self.ledger("progress.md"), "review progress routing",
            chunk="progress", test_command=self.qa_command(),
        )
        control.take_instructions(self.root, reviewer_session["id"])
        board.status(
            self.root, reviewer["id"],
            f"Authoring the distinct Challenge Ledger for {request['id']} before claiming.",
            "review_routed",
        )

        state_path = self.root / ".harness" / "board" / "state.json"
        state = json.loads(state_path.read_text())
        state["qa_requests"][request["id"]]["routed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=91)).isoformat()
        state_path.write_text(json.dumps(state))

        self.assertEqual(board.route_open_reviews(self.root, retry_seconds=90), [])
        self.assertEqual(control.take_instructions(self.root, reviewer_session["id"]), [])
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["route_attempts"], 1)

    def test_routed_review_cannot_be_claimed_by_a_second_reviewer(self):
        dev = self.delivery("TASK-REVIEW-SINGLE")
        self.declare_chunks(dev["id"], [("single", "route to only one reviewer")])
        first_session = control.create(self.root, "claude_reviewer")
        first = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=first_session["id"])
        second_session = control.create(self.root, "claude_reviewer")
        second = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=second_session["id"])
        request = board.request_review(
            self.root, dev["id"], self.ledger("single.md"), "review once",
            chunk="single", test_command=self.qa_command(),
        )

        self.assertEqual(request["routed_to"], first["id"])
        with self.assertRaisesRegex(ValueError, "already routed"):
            board.claim_qa(self.root, second["id"], request["id"], self.ledger("single-challenge.md"))
        claimed = board.claim_qa(self.root, first["id"], request["id"], self.ledger("single-first-challenge.md"))
        self.assertEqual(claimed["route_state"], "executing_review")

    def test_review_waiting_before_reviewer_launch_is_routed_when_controller_checks(self):
        dev = self.delivery("TASK-LATE-REVIEWER")
        self.declare_chunks(dev["id"], [("late", "route after reviewer registration")])
        request = board.request_review(
            self.root, dev["id"], self.ledger("late.md"), "wait for reviewer",
            chunk="late", test_command=self.qa_command(),
        )
        self.assertEqual(request.get("route_state"), "waiting_for_eligible_reviewer")

        reviewer_session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=reviewer_session["id"])
        routed = board.snapshot(self.root)["qa_requests"][request["id"]]
        self.assertEqual(routed["routed_to"], reviewer["id"])
        self.assertEqual(len(control.take_instructions(self.root, reviewer_session["id"])), 1)

    def test_watch_requests_short_status_updates_and_cleanup_archives(self):
        dev = self.delivery("TASK-4")
        self.atomic_plan(dev["id"])
        state_path = self.root / ".harness" / "board" / "state.json"
        state = json.loads(state_path.read_text())
        state["agents"][dev["id"]]["last_status_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        state_path.write_text(json.dumps(state))
        due = board.watch(self.root, status_interval_seconds=60, stale_seconds=120)
        self.assertTrue(any(item["agent_id"] == dev["id"] and item["kind"] == "stale" for item in due))
        qa = board.register(self.root, "qa", "QA-QUEUE")
        ledger = self.ledger("ledger.md")
        request = board.request_qa(self.root, dev["id"], ledger, "ready")
        board.claim_qa(self.root, qa["id"], request["id"])
        board.qa_result(self.root, qa["id"], request["id"], "passed", "all scenarios pass", self.evidence("qa-pass.txt"))
        cleaned = board.cleanup(self.root)
        self.assertEqual(cleaned["archived_qa_requests"], 1)
        self.assertEqual(cleaned["active_qa_requests"], 0)
        with self.assertRaises(ValueError):
            board.request_qa(self.root, dev["id"], ledger, "must start another task")

    def test_cleanup_preserves_failed_cycle_counter_for_retest(self):
        dev = self.delivery("TASK-5")
        self.atomic_plan(dev["id"])
        qa = board.register(self.root, "qa", "QA-QUEUE")
        ledger = self.ledger("ledger.md")
        first = board.request_qa(self.root, dev["id"], ledger, "ready")
        board.claim_qa(self.root, qa["id"], first["id"])
        board.qa_result(self.root, qa["id"], first["id"], "failed", "S-1 fails", self.evidence("qa-fail.txt"))
        board.cleanup(self.root)
        retest = board.request_qa(self.root, dev["id"], ledger, "retest", changes="fixed S-1")
        self.assertEqual(retest["cycle"], 2)
        self.assertEqual(retest["id"], "qa-TASK-5-dev-02")

    def test_independent_review_requires_different_vendor_and_challenge_ledger(self):
        dev = self.delivery("TASK-6")
        self.atomic_plan(dev["id"])
        development_qa = board.register(self.root, "qa", "QA-QUEUE", vendor="Anthropic")
        ledger = self.ledger("spec-ledger.md")
        request = board.request_qa(self.root, dev["id"], ledger, "development QA")
        board.claim_qa(self.root, development_qa["id"], request["id"])
        board.qa_result(self.root, development_qa["id"], request["id"], "passed", "spec ledger executed", self.evidence("dev-qa.txt"))
        review = board.request_independent_review(self.root, dev["id"], "challenge the development assumptions")
        same_vendor = board.register(self.root, "qa", "QA-QUEUE", vendor="OpenAI")
        with self.assertRaises(ValueError):
            board.claim_qa(self.root, same_vendor["id"], review["id"], self.ledger("review-ledger.md"))
        reviewer = board.register(self.root, "qa", "QA-QUEUE", vendor="Anthropic")
        reservation = board.claim_qa(self.root, reviewer["id"], review["id"])
        self.assertEqual(reservation["status"], "reserved")
        board.claim_qa(self.root, reviewer["id"], review["id"], self.ledger("review-ledger.md"))
        board.execute_challenge(self.root, reviewer["id"], review["id"])
        passed = board.qa_result(self.root, reviewer["id"], review["id"], "passed", "independent scenarios executed", self.evidence("reviewer.txt"))
        self.assertEqual(passed["stage"], board.INDEPENDENT_REVIEW)
        self.assertEqual(passed["challenge_ledger"], "docs/review-ledger.md")

    def test_two_phase_review_reservation_is_visible_distinct_and_expires_once(self):
        dev = self.delivery("TASK-TWO-PHASE")
        self.declare_chunks(dev["id"], [("core", "focused two-phase behavior")])
        first = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        second = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        delivery_ledger = self.ledger("two-phase-delivery.md")
        request = board.request_review(
            self.root, dev["id"], delivery_ledger, "review two phase",
            chunk="core", test_command=self.qa_command(),
        )
        reserved = board.reserve_qa(self.root, first["id"], request["id"])
        self.assertEqual(reserved["status"], "reserved")
        self.assertEqual(reserved["route_state"], "preparing_challenge_ledger")
        self.assertEqual(reserved["reserved_by"], first["id"])
        with self.assertRaisesRegex(ValueError, "no open|already reserved|already routed"):
            board.reserve_qa(self.root, second["id"], request["id"])
        with self.assertRaisesRegex(ValueError, "distinct"):
            board.attach_challenge_ledger(self.root, first["id"], request["id"], delivery_ledger)
        still_reserved = board.snapshot(self.root)["qa_requests"][request["id"]]
        self.assertEqual(still_reserved["status"], "reserved")
        with self.assertRaisesRegex(ValueError, "claimed"):
            board.qa_result(self.root, first["id"], request["id"], "passed", "not executed", self.evidence("reserved-pass.txt"))

        with board.locked_state(self.root) as state:
            old = (datetime.now(timezone.utc) - timedelta(
                seconds=board.REVIEW_RESERVATION_SECONDS + 1
            )).isoformat()
            state["qa_requests"][request["id"]]["reserved_at"] = old
            state["qa_requests"][request["id"]]["authoring_last_activity_at"] = old
            state["agents"][first["id"]]["last_poll_at"] = old
            state["agents"][first["id"]]["last_progress_at"] = old
        expired = board.release_expired_review_reservations(self.root)
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["kind"], "qa_reservation_expired")
        self.assertEqual(board.release_expired_review_reservations(self.root), [])
        reopened = board.snapshot(self.root)["qa_requests"][request["id"]]
        self.assertEqual(reopened["status"], "open")

        board.reserve_qa(self.root, second["id"], request["id"])
        claimed = board.attach_challenge_ledger(self.root, second["id"], request["id"], self.ledger("two-phase-challenge.md"))
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["route_state"], "executing_review")

    def test_active_reviewer_heartbeat_prevents_reservation_theft(self):
        dev = self.delivery("TASK-AUTHORING-HEARTBEAT")
        self.declare_chunks(dev["id"], [("core", "reviewer authoring lease")])
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        request = board.request_review(
            self.root, dev["id"], self.ledger("authoring-delivery.md"),
            "authoring heartbeat", chunk="core", test_command=self.qa_command(),
        )
        board.reserve_qa(self.root, reviewer["id"], request["id"])
        old = (datetime.now(timezone.utc) - timedelta(
            seconds=board.REVIEW_RESERVATION_SECONDS + 1
        )).isoformat()
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]]["reserved_at"] = old
            state["qa_requests"][request["id"]]["authoring_last_activity_at"] = old
            state["agents"][reviewer["id"]]["last_poll_at"] = board.now()
        self.assertEqual(board.release_expired_review_reservations(self.root), [])
        self.assertEqual(
            board.snapshot(self.root)["qa_requests"][request["id"]]["reserved_by"],
            reviewer["id"],
        )

    def test_challenge_execution_requires_exact_attach_authorization(self):
        dev = self.delivery("TASK-ATTACH-AUTHORIZATION")
        self.declare_chunks(dev["id"], [("core", "ledger authorization")])
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        request = board.request_review(
            self.root, dev["id"], self.ledger("attach-delivery.md"),
            "attachment authorization", chunk="core", test_command=self.qa_command(),
        )
        board.reserve_qa(self.root, reviewer["id"], request["id"])
        with self.assertRaisesRegex(ValueError, "claimed|attached"):
            board.execute_challenge(self.root, reviewer["id"], request["id"])
        board.attach_challenge_ledger(
            self.root, reviewer["id"], request["id"], self.ledger("attach-challenge.md"),
        )
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]]["challenge_execution_authorization"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "exact authorization"):
            board.execute_challenge(self.root, reviewer["id"], request["id"])

    def test_failed_challenge_cannot_retry_silently(self):
        dev = self.delivery("TASK-EXPLICIT-RETRY")
        self.declare_chunks(dev["id"], [("core", "failed retry disclosure")])
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        request = board.request_review(
            self.root, dev["id"], self.ledger("retry-delivery.md"),
            "retry disclosure", chunk="core", test_command=self.qa_command(),
        )
        (self.root / "test_retry_failure.py").write_text(
            "import unittest\n\nclass Retry(unittest.TestCase):\n"
            "    def test_failure(self): self.fail('expected failure')\n"
        )
        challenge = self.root / "docs" / "retry-challenge.md"
        challenge.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-RETRY | A failed certified check cannot be repeated without a recorded reason. | `python3 -m unittest test_retry_failure` | The retry is disclosed | PASS: pending governed execution | PASS |\n"
        )
        board.claim_qa(
            self.root, reviewer["id"], request["id"], str(challenge.relative_to(self.root)),
        )
        with self.assertRaisesRegex(ValueError, "S-RETRY"):
            board.execute_challenge(self.root, reviewer["id"], request["id"])
        with self.assertRaisesRegex(ValueError, "non-empty repair reason"):
            board.execute_challenge(self.root, reviewer["id"], request["id"])
        with self.assertRaisesRegex(ValueError, "S-RETRY"):
            board.execute_challenge(
                self.root, reviewer["id"], request["id"],
                "The test dependency was repaired and the failure must be rechecked.",
            )
        attempts = board.snapshot(self.root)["qa_requests"][request["id"]]["challenge_execution_attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertTrue(attempts[-1]["retry_reason"])

    def test_reviewer_cannot_pass_a_copied_or_incomplete_challenge_ledger(self):
        dev = self.delivery("TASK-6B")
        reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        self.declare_chunks(dev["id"], [("core", "focused behavior")])
        ledger = self.ledger("delivery.md")
        request = board.request_review(self.root, dev["id"], ledger, "review the core", chunk="core", test_command=self.qa_command())
        with self.assertRaisesRegex(ValueError, "distinct"):
            board.claim_qa(self.root, reviewer["id"], request["id"], ledger)
        challenge = self.ledger("challenge.md", "OPEN")
        board.claim_qa(self.root, reviewer["id"], request["id"], challenge)
        with self.assertRaisesRegex(ValueError, "Challenge Ledger is incomplete"):
            board.qa_result(self.root, reviewer["id"], request["id"], "passed", "claimed pass without proof", self.evidence("bad-pass.txt"))

    def test_reviewer_cannot_claim_with_a_copied_ledger_or_unexecuted_internal_qa(self):
        dev = self.delivery("TASK-6C")
        reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        self.declare_chunks(dev["id"], [("core", "focused behavior")])
        ledger = self.ledger("delivery.md")
        with self.assertRaisesRegex(ValueError, "test-command"):
            board.request_review(self.root, dev["id"], ledger, "review the core", chunk="core")
        with self.assertRaisesRegex(ValueError, "recognized test runner"):
            board.request_review(self.root, dev["id"], ledger, "review the core", chunk="core", test_command="true")
        with self.assertRaisesRegex(ValueError, "zero executed tests"):
            board.request_review(self.root, dev["id"], ledger, "review the core", chunk="core", test_command="python3 -m unittest -k definitely_missing")
        request = board.request_review(self.root, dev["id"], ledger, "review the core", chunk="core", test_command=self.qa_command())
        copied = self.root / "docs" / "copied.md"
        copied.write_text((self.root / ledger).read_text())
        with self.assertRaisesRegex(ValueError, "introduce at least one scenario"):
            board.claim_qa(self.root, reviewer["id"], request["id"], str(copied.relative_to(self.root)))

    def test_internal_qa_rejects_background_and_pipeline_failure_masks(self):
        fixtures = self.root / "ampproof"
        fixtures.mkdir()
        (fixtures / "test_amp_failure.py").write_text(
            "import unittest\n\n"
            "class AmpFailure(unittest.TestCase):\n"
            "    def test_failure(self): self.fail('intentional failure')\n"
        )
        commands = (
            "python3 -m unittest discover -s ampproof -p 'test_amp_*.py' &",
            "python3 -m unittest discover -s ampproof -p 'test_amp_*.py' | true",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "shell control operators"):
                    board._execute_internal_qa(command, self.root)

    def test_unspaced_delivery_ledger_cannot_bypass_copied_challenge_guard(self):
        dev = self.delivery("TASK-6D")
        reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        self.declare_chunks(dev["id"], [("core", "focused behavior")])
        docs = self.root / "docs"
        docs.mkdir(exist_ok=True)
        delivery = docs / "unspaced-delivery.md"
        delivery.write_text(
            "|ID|What was tested|Simulation command|Expected system response|Observed system response|QA result|\n"
            "|---|---|---|---|---|---|\n"
            "|S-001|A dependency failure is detected while the saved state remains safe.|`python3 -m unittest test_smoke`|Failure is detected and state stays safe|PASS: targeted simulation observed safe state|PASS|\n"
        )
        request = board.request_review(
            self.root, dev["id"], str(delivery.relative_to(self.root)),
            "review unspaced scenarios", chunk="core", test_command=self.qa_command(),
        )
        copied = docs / "respaced-copy.md"
        copied.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-001 | A dependency failure is detected while the saved state remains safe. | `python3 -m unittest test_smoke` | Failure is detected and state stays safe | PASS: targeted simulation observed safe state | PASS |\n"
        )
        with self.assertRaisesRegex(ValueError, "introduce at least one scenario"):
            board.claim_qa(self.root, reviewer["id"], request["id"], str(copied.relative_to(self.root)))

    def test_delivery_review_executes_every_scenario_simulation(self):
        dev = self.delivery("TASK-SIM-FAIL")
        self.declare_chunks(dev["id"], [("core", "execute every scenario")])
        (self.root / "test_scenario_one.py").write_text(
            "import unittest\nfrom pathlib import Path\n\nclass ScenarioOne(unittest.TestCase):\n    def test_runs(self):\n        Path('scenario-one-ran').write_text('yes')\n        self.assertTrue(True)\n"
        )
        (self.root / "test_scenario_two.py").write_text(
            "import unittest\nfrom pathlib import Path\n\nclass ScenarioTwo(unittest.TestCase):\n    def test_runs(self):\n        Path('scenario-two-ran').write_text('yes')\n        self.fail('simulated unsafe response')\n"
        )
        ledger = self.root / "docs" / "delivery-sim-fail.md"
        ledger.parent.mkdir(exist_ok=True)
        ledger.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-001 | The first condition is handled without leaving unsafe state behind. | `python3 -m unittest test_scenario_one` | First condition is handled | PASS: targeted simulation observed safe handling | PASS |\n"
            "| S-002 | An unsafe response is rejected instead of being treated as successful. | `python3 -m unittest test_scenario_two` | Unsafe response is rejected | PASS: self-reported observation must not be trusted | PASS |\n"
        )
        with self.assertRaisesRegex(ValueError, "S-002.*simulations? failed|simulations failed.*S-002"):
            board.request_review(self.root, dev["id"], str(ledger.relative_to(self.root)), "must reject false PASS", chunk="core", test_command=self.qa_command())
        self.assertTrue((self.root / "scenario-one-ran").is_file())
        self.assertTrue((self.root / "scenario-two-ran").is_file())
        self.assertEqual(board.snapshot(self.root)["qa_requests"], {})

    def test_delivery_review_persists_scenario_linked_simulation_evidence(self):
        dev = self.delivery("TASK-SIM-EVIDENCE")
        self.declare_chunks(dev["id"], [("core", "capture every scenario")])
        ledger = self.root / "docs" / "delivery-sim-pass.md"
        ledger.parent.mkdir(exist_ok=True)
        ledger.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-001 | The first condition is handled without leaving unsafe state behind. | `python3 -m unittest test_smoke` | First condition is handled | PASS: targeted smoke simulation observed safe handling | PASS |\n"
            "| S-002 | Recovery preserves the saved state after the same behavior is retried. | `python3 -m unittest test_smoke` | Recovery preserves state | PASS: targeted smoke simulation observed preserved state | PASS |\n"
        )
        request = board.request_review(
            self.root, dev["id"], str(ledger.relative_to(self.root)),
            "capture scenario evidence", chunk="core", test_command=self.qa_command(),
        )
        simulations = request["delivery_simulations"]
        self.assertEqual(simulations["scenario_ids"], ["S-001", "S-002"])
        self.assertEqual(simulations["executed_count"], 2)
        evidence = Path(simulations["evidence"])
        self.assertEqual(hashlib.sha256(evidence.read_bytes()).hexdigest(), simulations["evidence_sha256"])
        text = evidence.read_text()
        self.assertIn("scenario: S-001", text)
        self.assertIn("scenario: S-002", text)
        self.assertGreaterEqual(text.count("Ran 1 test"), 2)

    def test_duplicate_scenario_commands_execute_once_and_remain_attributed(self):
        ledger = self.root / "docs" / "duplicate-commands.md"
        ledger.parent.mkdir(exist_ok=True)
        ledger.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-001 | The normal flow produces the safe result expected by the owner. | `python3 -m unittest test_smoke` | safe result | PASS: normal result observed | PASS |\n"
            "| S-002 | The recovery flow restores safe state after the same command runs. | `python3 -m unittest test_smoke` | state recovers | PASS: recovery result observed | PASS |\n"
        )
        with patch.object(board, "_execute_internal_qa", return_value="Ran 1 test in 0.001s\nOK") as execute:
            results = board._execute_scenario_simulations(self.root, ledger)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(results[0]["outcome"], "passed")
        self.assertEqual(results[1]["deduplicated_from"], "S-001")

    def test_delivery_review_rejects_ledger_changed_during_simulation(self):
        dev = self.delivery("TASK-SIM-RACE")
        self.declare_chunks(dev["id"], [("core", "bind evidence to ledger content")])
        ledger = self.root / "docs" / "mutable.md"
        ledger.parent.mkdir(exist_ok=True)
        ledger.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-001 | Evidence changed during execution is rejected instead of being certified. | `python3 -m unittest test_mutating_scenario` | Changed evidence is rejected | PASS: original observation before execution | PASS |\n"
        )
        (self.root / "test_mutating_scenario.py").write_text(
            "import unittest\nfrom pathlib import Path\n\nclass Mutate(unittest.TestCase):\n    def test_changes_ledger(self):\n        path = Path('docs/mutable.md')\n        path.write_text(path.read_text().replace('original observation', 'changed observation'))\n        self.assertTrue(True)\n"
        )
        with self.assertRaisesRegex(ValueError, "changed while its simulations were executing"):
            board.request_review(
                self.root, dev["id"], str(ledger.relative_to(self.root)),
                "reject changed ledger", chunk="core", test_command=self.qa_command(),
            )
        self.assertEqual(board.snapshot(self.root)["qa_requests"], {})

    def test_reviewer_claim_rejects_description_only_challenge_ledger(self):
        dev = self.delivery("TASK-REVIEW-SCHEMA")
        reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        self.declare_chunks(dev["id"], [("core", "challenge schema")])
        request = board.request_review(
            self.root, dev["id"], self.ledger("delivery-valid.md"),
            "review challenge schema", chunk="core", test_command=self.qa_command(),
        )
        challenge = self.root / "docs" / "description-only.md"
        challenge.write_text("| ID | Scenario | QA result |\n|---|---|---|\n| S-101 | reviewer says failure is handled | PASS |\n")
        with self.assertRaisesRegex(ValueError, "Simulation command"):
            board.claim_qa(self.root, reviewer["id"], request["id"], str(challenge.relative_to(self.root)))

    def test_reviewer_pass_executes_every_challenge_simulation(self):
        dev = self.delivery("TASK-REVIEW-FAIL")
        reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        self.declare_chunks(dev["id"], [("core", "execute reviewer challenges")])
        request = board.request_review(
            self.root, dev["id"], self.ledger("delivery-for-review.md"),
            "execute challenges", chunk="core", test_command=self.qa_command(),
        )
        (self.root / "test_review_one.py").write_text(
            "import unittest\nfrom pathlib import Path\n\nclass ReviewOne(unittest.TestCase):\n    def test_runs(self):\n        Path('review-one-ran').write_text('yes')\n        self.assertTrue(True)\n"
        )
        (self.root / "test_review_two.py").write_text(
            "import unittest\nfrom pathlib import Path\n\nclass ReviewTwo(unittest.TestCase):\n    def test_runs(self):\n        Path('review-two-ran').write_text('yes')\n        self.fail('reviewer simulation exposed unsafe behavior')\n"
        )
        challenge = self.root / "docs" / "review-fail.md"
        challenge.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-101 | The first independent condition is handled without unsafe side effects. | `python3 -m unittest test_review_one` | First challenge is handled | PASS: reviewer claims safe handling | PASS |\n"
            "| S-102 | Unsafe behavior blocks approval even when the ledger claims success. | `python3 -m unittest test_review_two` | Unsafe behavior blocks PASS | PASS: self-report must not override execution | PASS |\n"
        )
        board.claim_qa(self.root, reviewer["id"], request["id"], str(challenge.relative_to(self.root)))
        with self.assertRaisesRegex(ValueError, "S-102.*simulations? failed|simulations failed.*S-102"):
            board.execute_challenge(self.root, reviewer["id"], request["id"])
        self.assertTrue((self.root / "review-one-ran").is_file())
        self.assertTrue((self.root / "review-two-ran").is_file())
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["status"], "claimed")

    def test_reviewer_pass_persists_challenge_simulation_evidence(self):
        dev = self.delivery("TASK-REVIEW-EVIDENCE")
        reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        self.declare_chunks(dev["id"], [("core", "capture reviewer challenges")])
        request = board.request_review(
            self.root, dev["id"], self.ledger("delivery-evidence.md"),
            "capture challenge evidence", chunk="core", test_command=self.qa_command(),
        )
        challenge = self.root / "docs" / "review-pass.md"
        challenge.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-101 | The first independent condition is handled without unsafe side effects. | `python3 -m unittest test_smoke -k passes` | First challenge is handled | PASS: targeted reviewer simulation observed handling | PASS |\n"
            "| S-102 | Recovery remains safe when the independent check repeats the behavior. | `python3 -m unittest test_smoke -k passes` | Recovery stays safe | PASS: targeted reviewer simulation observed recovery | PASS |\n"
        )
        board.claim_qa(self.root, reviewer["id"], request["id"], str(challenge.relative_to(self.root)))
        board.execute_challenge(self.root, reviewer["id"], request["id"])
        passed = board.qa_result(self.root, reviewer["id"], request["id"], "passed", "challenge simulations pass", self.evidence("review-pass.txt"))
        simulations = passed["reviewer_simulations"]
        self.assertEqual(simulations["scenario_ids"], ["S-101", "S-102"])
        self.assertEqual(simulations["executed_count"], 2)
        evidence = Path(simulations["evidence"])
        self.assertEqual(hashlib.sha256(evidence.read_bytes()).hexdigest(), simulations["evidence_sha256"])
        reuse = passed["evidence_reuse_identity"]
        self.assertEqual(reuse["version"], 3)
        self.assertFalse(reuse["finalization"]["applicable"])
        self.assertEqual(reuse["review_scope"]["fields"]["chunk"], "core")
        self.assertEqual(reuse["contract_revision"]["sha256"], passed["contract_revision"]["sha256"])
        self.assertEqual(reuse["environment_identity"]["sha256"], passed["environment_identity"]["sha256"])
        self.assertEqual(
            reuse["source_artifacts"]["challenge_ledger"]["sha256"],
            passed["challenge_ledger_sha256"],
        )
        self.assertEqual(
            reuse["source_artifacts"]["result_evidence"]["path"],
            str(Path(passed["evidence"]).resolve()),
        )

    def test_final_pass_immediately_routes_delivery_and_cto(self):
        dev = self.delivery("TASK-IMMEDIATE-RELEASE")
        self.atomic_plan(dev["id"])
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        cto_session = control.create(self.root, "claude_cto")
        board.register(
            self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic",
            session_id=cto_session["id"],
        )
        control.take_instructions(self.root, dev["session_id"])
        request = board.request_review(
            self.root, dev["id"], self.ledger("immediate-delivery.md"),
            "final candidate", phase="final_acceptance", test_command=self.qa_command(),
        )
        board.claim_qa(
            self.root, reviewer["id"], request["id"],
            self.ledger("immediate-challenge.md"),
        )
        board.execute_challenge(self.root, reviewer["id"], request["id"])
        board.qa_result(
            self.root, reviewer["id"], request["id"], "passed",
            "independent final checks passed", self.evidence("immediate-final.txt"),
        )
        delivery_routes = control.take_instructions(self.root, dev["session_id"])
        cto_routes = control.take_instructions(self.root, cto_session["id"])
        self.assertTrue(any("FINAL REVIEW" in row["text"] for row in delivery_routes))
        self.assertTrue(any("FINAL PASS RECORDED" in row["text"] for row in cto_routes))
        state = board.snapshot(self.root)
        self.assertTrue(any(
            event["kind"] == "release_routing_requested"
            and event["task"] == "TASK-IMMEDIATE-RELEASE"
            for event in state["events"]
        ))
        self.assertEqual(
            state["release_lifecycle"]["TASK-IMMEDIATE-RELEASE"]["final_pass_at"],
            state["qa_requests"][request["id"]]["completed_at"],
        )

    def test_review_pass_rejects_approved_exception_instead_of_skipping_simulation(self):
        dev = self.delivery("TASK-REVIEW-EXCEPTION")
        reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        self.declare_chunks(dev["id"], [("core", "execute every challenge")])
        request = board.request_review(
            self.root, dev["id"], self.ledger("delivery-exception.md"),
            "reject skipped challenges", chunk="core", test_command=self.qa_command(),
        )
        challenge = self.root / "docs" / "review-exception.md"
        challenge.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-101 | The independent challenge runs and verifies the expected owner behavior. | `python3 -m unittest test_smoke -k passes` | Real challenge runs | PASS: targeted reviewer simulation ran | PASS |\n"
            "| S-102 | An unavailable external dependency does not silently become a passed check. | N/A | N/A | N/A | N/A: CTO - temporary external dependency outage |\n"
        )
        board.claim_qa(self.root, reviewer["id"], request["id"], str(challenge.relative_to(self.root)))
        with self.assertRaisesRegex(ValueError, "S-102.*approved exception"):
            board.execute_challenge(self.root, reviewer["id"], request["id"])

    def test_repaired_scenario_simulation_requires_a_new_review_cycle(self):
        dev = self.delivery("TASK-REVIEW-REPAIR")
        reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        self.declare_chunks(dev["id"], [("core", "repair failed reviewer simulation")])
        delivery_ledger = self.ledger("delivery-repair.md")
        first = board.request_review(
            self.root, dev["id"], delivery_ledger,
            "first review", chunk="core", test_command=self.qa_command(),
        )
        scenario_test = self.root / "test_review_repair.py"
        scenario_test.write_text("import unittest\n\nclass Repair(unittest.TestCase):\n    def test_behavior(self): self.fail('unsafe before repair')\n")
        challenge = self.root / "docs" / "review-repair.md"
        challenge.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-101 | The repaired behavior is independently checked and remains safe after retry. | `python3 -m unittest test_review_repair` | Repaired behavior is safe | PASS: reviewer expectation pending actual execution | PASS |\n"
        )
        board.claim_qa(self.root, reviewer["id"], first["id"], str(challenge.relative_to(self.root)))
        with self.assertRaisesRegex(ValueError, "S-101"):
            board.execute_challenge(self.root, reviewer["id"], first["id"])
        failed = board.qa_result(self.root, reviewer["id"], first["id"], "failed", "simulation exposed unsafe behavior", self.evidence("repair-failed.txt"))
        self.assertEqual(failed["status"], "failed")
        scenario_test.write_text("import unittest\n\nclass Repair(unittest.TestCase):\n    def test_behavior(self): self.assertTrue(True)\n")
        second = board.request_review(
            self.root, dev["id"], delivery_ledger,
            "retest repaired behavior", chunk="core", changes="fixed unsafe reviewer scenario", test_command=self.qa_command(),
        )
        self.assertEqual(second["cycle"], 2)
        board.claim_qa(self.root, reviewer["id"], second["id"], str(challenge.relative_to(self.root)))
        board.execute_challenge(self.root, reviewer["id"], second["id"])
        passed = board.qa_result(self.root, reviewer["id"], second["id"], "passed", "repaired simulation passed", self.evidence("repair-passed.txt"))
        self.assertEqual(passed["status"], "passed")
        self.assertEqual(passed["reviewer_simulations"]["scenario_ids"], ["S-101"])

    def test_chunks_must_pass_before_final_acceptance_review_and_board_is_visible(self):
        dev = self.delivery("TASK-7")
        reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        self.declare_chunks(dev["id"], [("api", "bounded API change"), ("ui", "focused UI change")])
        with self.assertRaises(ValueError):
            board.request_review(self.root, dev["id"], self.ledger("ledger.md"), "final too early", phase="final_acceptance", test_command=self.qa_command())
        ledger = self.ledger("ledger.md")
        for chunk in ("api", "ui"):
            request = board.request_review(self.root, dev["id"], ledger, f"review {chunk}", chunk=chunk, test_command=self.qa_command())
            board.claim_qa(self.root, reviewer["id"], request["id"], self.ledger(f"{chunk}-challenge.md"))
            board.execute_challenge(self.root, reviewer["id"], request["id"])
            board.qa_result(self.root, reviewer["id"], request["id"], "passed", f"{chunk} passed", self.evidence(f"{chunk}.txt"))
        final = board.request_review(self.root, dev["id"], ledger, "full objective final acceptance", phase="final_acceptance", test_command=self.qa_command())
        self.assertEqual(final["phase"], "final_acceptance")
        self.assertEqual(final["chunk"], "final")
        board_file = (self.root / ".harness" / "board" / "BOARD.md").read_text()
        events = (self.root / ".harness" / "board" / "events.jsonl").read_text()
        self.assertIn("Delivery chunks", board_file)
        self.assertIn("final_acceptance", board_file)
        self.assertIn("chunks_declared", events)

    def test_chunk_review_requires_completed_ledger_and_internal_qa_evidence(self):
        dev = self.delivery("TASK-8")
        self.declare_chunks(dev["id"], [("core", "focused core behavior")])
        open_ledger = self.ledger("open.md", "OPEN")
        with self.assertRaisesRegex(ValueError, "Scenario Ledger is incomplete"):
            board.request_review(self.root, dev["id"], open_ledger, "review core", chunk="core", test_command=self.qa_command())
        passed_ledger = self.ledger("passed.md")
        with self.assertRaisesRegex(ValueError, "test-command"):
            board.request_review(self.root, dev["id"], passed_ledger, "review core", chunk="core")


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Real board-path integration for Task C's Git model."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from harness import board, board_viewer, contract, control
from tests.environment_support import require_loopback
from tests.requirements_support import agreed_requirements


class GitModelBoardIntegrationTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / ".gitignore").write_text(".harness/\n", encoding="utf-8")
        (self.root / "product.txt").write_text("base\n", encoding="utf-8")
        self.git("add", ".gitignore", "product.txt")
        self.git("commit", "-m", "base")
        self.base_commit = self.git("rev-parse", "HEAD").strip()
        session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Implement one governed Git change in this repository.")
        self.begun = board.begin_task(self.root, self.delivery["id"], "GIT-MODEL")
        contract.create_contract(self.root, "GIT-MODEL", "Implement one governed Git change in this repository.", ["governed change"])
        agreed_requirements(self.root, self.delivery["id"], "Implement and verify the governed change.")
        board.define_delivery_plan(self.root, self.delivery["id"], "atomic", "One cohesive Git-model integration fixture")

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments, cwd=None):
        result = subprocess.run(
            ["/usr/bin/git", *arguments], cwd=cwd or self.root,
            capture_output=True, text=True,
        )
        if result.returncode:
            self.fail(result.stderr or result.stdout)
        return result.stdout

    def certified_candidate(self):
        workspace = Path(self.begun["task_workspace"])
        (workspace / "product.txt").write_text("accepted\n", encoding="utf-8")
        committed = board.broker_stage_commit(
            self.root, self.delivery["id"], ["product.txt"], "governed candidate",
        )
        with board.locked_state(self.root) as state:
            request = {
                "id": "review-GIT-MODEL-final-01", "task": "GIT-MODEL", "cycle": 1,
                "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance",
                "subtask": "", "chunk": "final", "status": "passed",
                "requested_at": board.now(), "developer_id": self.delivery["id"],
                "claimed_by": "qa-fixture", "review_wait_started_at": board.now(),
                "structure_revision": state["delivery_plans"]["GIT-MODEL"]["structure_revision"],
                "reviewed_commit": committed["commit"],
                "reviewed_tree_hash": committed["tree"],
                "reviewed_files": committed["manifest"],
            }
            state["qa_requests"][request["id"]] = request
            broker = board._broker_for_state(self.root, state, "GIT-MODEL")
            mirror = broker.create_review_ref(
                request, review_number=1,
                board_mutation=lambda record: request.update({
                    "mirror_ref": record["ref"], "mirror_commit": record["commit"],
                    "mirror_tree_hash": record["tree"],
                    "mirror_transaction_id": record["transaction_id"],
                }),
            )
            request["mirror_ref"] = mirror["ref"]
            state.setdefault("releases", {})["GIT-MODEL"] = {
                "task": "GIT-MODEL", "status": "VISUAL_TEST_REQUIRED",
                "head_commit": committed["commit"], "git_broker_governed": True,
                "acceptance_base_commit": self.base_commit,
                "acceptance_manifest": committed["manifest"],
                "cto_id": "cto-fixture", "recorded_at": board.now(),
            }
        return committed

    def test_rolled_back_board_nonce_does_not_deadlock_the_next_commit(self):
        """A board rollback after the broker journal commits must self-heal.

        The board counter lives inside the ``locked_state`` transaction and is
        discarded when that block raises, but ``consume_nonce`` has already
        written the broker journal by then. The two stores drift apart by one,
        and allocating from the board alone replays a spent nonce, so every
        later Delivery write is refused as a replay forever.
        """
        workspace = Path(self.begun["task_workspace"])
        (workspace / "product.txt").write_text("first\n", encoding="utf-8")
        board.broker_stage_commit(
            self.root, self.delivery["id"], ["product.txt"], "first governed commit",
        )

        session_key = self.delivery.get("session_id") or self.delivery["id"]
        with board.locked_state(self.root) as state:
            nonces_path = board._broker_for_state(self.root, state, "GIT-MODEL").nonces_path
        accepted = int(json.loads(nonces_path.read_text(encoding="utf-8"))[session_key])

        # Reproduce the rollback: the board forgets an allocation the broker
        # has durably accepted.
        with board.locked_state(self.root) as state:
            state["broker_nonces"][session_key] = accepted - 1

        (workspace / "product.txt").write_text("second\n", encoding="utf-8")
        committed = board.broker_stage_commit(
            self.root, self.delivery["id"], ["product.txt"], "commit after board rollback",
        )
        self.assertTrue(committed["commit"])

        with board.locked_state(self.root) as state:
            self.assertGreater(int(state["broker_nonces"][session_key]), accepted)

    def _accepted_nonce(self):
        session_key = self.delivery.get("session_id") or self.delivery["id"]
        with board.locked_state(self.root) as state:
            nonces_path = board._broker_for_state(self.root, state, "GIT-MODEL").nonces_path
        return session_key, int(json.loads(nonces_path.read_text(encoding="utf-8"))[session_key])

    def test_recover_git_reports_and_reconciles_nonce_drift_and_is_quiet_without_it(self):
        """The 2026-09-21 incident answered `{"holds": [], "recovered": []}` to this drift."""
        workspace = Path(self.begun["task_workspace"])
        (workspace / "product.txt").write_text("first\n", encoding="utf-8")
        board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "first governed commit")
        session_key, accepted = self._accepted_nonce()

        quiet = board.recover_git_transactions(self.root)
        self.assertEqual(quiet["nonce_drift"], [], "no drift must be reported as no drift")

        with board.locked_state(self.root) as state:
            state["broker_nonces"][session_key] = accepted - 1

        report = board.recover_git_transactions(self.root)
        self.assertEqual(report["nonce_drift"], [{
            "task": "GIT-MODEL", "session": session_key,
            "board": accepted - 1, "journal": accepted, "reconciled_to": accepted,
        }])
        with board.locked_state(self.root) as state:
            self.assertEqual(int(state["broker_nonces"][session_key]), accepted)
            kinds = [event["kind"] for event in state["events"]]
        self.assertIn("broker_nonce_drift_reconciled", kinds)
        self.assertEqual(board.recover_git_transactions(self.root)["nonce_drift"], [])

    def test_a_refused_write_is_a_visible_blocked_state_not_a_stall_and_clears_on_success(self):
        """The agent kept polling for 21 minutes while every write bounced; the board said 'stalled'."""
        from unittest import mock
        import time
        from harness import git_broker

        workspace = Path(self.begun["task_workspace"])
        (workspace / "product.txt").write_text("first\n", encoding="utf-8")
        board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "first governed commit")

        (workspace / "product.txt").write_text("second\n", encoding="utf-8")
        with mock.patch.object(board, "_next_broker_nonce", return_value=1):
            with self.assertRaises(git_broker.ReplayError) as caught:
                board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "replayed nonce")
        self.assertIn("replay refused", str(caught.exception))

        with board.locked_state(self.root) as state:
            agent = state["agents"][self.delivery["id"]]
            self.assertEqual(agent["status"], "blocked")
            self.assertEqual(agent["broker_refusal"]["operation"], "git-commit")
            self.assertIn("replay refused", agent["broker_refusal"]["reason"])
            self.assertIn("refused", agent["status_note"])
            self.assertEqual(state["events"][-1]["kind"], "broker_write_refused")
            self.assertNotEqual(agent.get("liveness"), "stalled")
            # Age the heartbeat so the watchdog would otherwise nudge this agent.
            agent["last_poll_at"] = "2020-01-01T00:00:00+00:00"
            agent["last_progress_at"] = "2020-01-01T00:00:00+00:00"

        time.sleep(1.1)
        board.mark_stalled(self.root, stale_seconds=1)
        with board.locked_state(self.root) as state:
            agent = state["agents"][self.delivery["id"]]
            self.assertNotIn(agent.get("liveness"), {"stalled", "recovering"},
                             "a refused write must not be treated as a stall")
            # 2026-09-25 defect #4: the refusal IS the saved next action. It is
            # routed once, naming the reason and the recovery step - never the
            # generic "resume your saved work" wake.
            self.assertTrue(agent.get("automatic_recovery_requested_at"),
                            "a refused Delivery must be woken with its refusal")
            self.assertIn("refused Git write", agent.get("liveness_note", ""))
        with control.locked_state(self.root) as control_state:
            queued = [item["text"] for item in (control_state.get("inbox") or {}).get(self.delivery["session_id"], [])]
        self.assertEqual(len(queued), 1, queued)
        self.assertIn("refused by the Git broker", queued[0])
        self.assertIn("replay refused", queued[0])
        self.assertIn("recover-git", queued[0])
        self.assertNotIn("continue GIT-MODEL from its saved next gate", queued[0])

        committed = board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "after the refusal")
        self.assertTrue(committed["commit"])
        with board.locked_state(self.root) as state:
            agent = state["agents"][self.delivery["id"]]
            self.assertNotIn("broker_refusal", agent)
            self.assertEqual(agent["status"], "working")
            self.assertIn("accepted", agent["status_note"])
            kinds = [event["kind"] for event in state["events"]]
        self.assertIn("broker_write_accepted_after_refusal", kinds)

    def test_owner_accept_advances_local_main_to_exact_mirror_candidate_without_push(self):
        committed = self.certified_candidate()
        response = board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertEqual(response["git_acceptance"]["commit"], committed["commit"])
        self.assertEqual(self.git("rev-parse", "main").strip(), committed["commit"])
        self.assertEqual((self.root / "product.txt").read_text(encoding="utf-8"), "accepted\n")
        state = board.snapshot(self.root)
        self.assertEqual(state["git_acceptances"]["GIT-MODEL"]["tree"], committed["tree"])
        self.assertNotIn("GIT-MODEL", state.get("remote_push_outcomes", {}))

    def test_failed_fast_forward_is_recorded_in_plain_words_and_accept_can_be_retried(self):
        """2026-09-25 defects #2/#21: three Accepts saved the decision, then the
        broker refused the fast-forward, the raw error reached the page, and a
        second Accept was refused as "already recorded"."""
        committed = self.certified_candidate()
        with board.locked_state(self.root) as state:
            state["releases"]["GIT-MODEL"]["acceptance_manifest"] = []  # the release check paired a rename
        response = board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertEqual(response["decision"], "accepted")
        self.assertEqual(response["git_acceptance"]["status"], "failed")
        self.assertIn("press Accept again", response["git_acceptance"]["reason"])
        self.assertNotIn("manifest", response["git_acceptance"]["reason"])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base_commit)
        state = board.snapshot(self.root)
        self.assertNotIn("GIT-MODEL", state.get("git_acceptances", {}))
        self.assertEqual(state["release_decisions"]["GIT-MODEL"]["git_acceptance"]["attempts"], 1)
        kinds = [event["kind"] for event in state["events"]]
        self.assertIn("git_acceptance_failed", kinds)
        self.assertNotIn("git_acceptance_completed", kinds)
        with self.assertRaisesRegex(ValueError, "already been recorded"):
            board.record_release_decision(self.root, "GIT-MODEL", "not_accepted", "changed my mind")

        with board.locked_state(self.root) as state:
            state["releases"]["GIT-MODEL"]["acceptance_manifest"] = committed["manifest"]
        retried = board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertEqual(retried["git_acceptance"]["commit"], committed["commit"])
        self.assertEqual(self.git("rev-parse", "main").strip(), committed["commit"])
        state = board.snapshot(self.root)
        self.assertEqual(state["git_acceptances"]["GIT-MODEL"]["tree"], committed["tree"])
        self.assertNotIn("git_acceptance", state["release_decisions"]["GIT-MODEL"])
        kinds = [event["kind"] for event in state["events"]]
        self.assertIn("owner_release_acceptance_retried", kinds)
        self.assertIn("git_acceptance_completed", kinds)
        with self.assertRaisesRegex(ValueError, "already been recorded"):
            board.record_release_decision(self.root, "GIT-MODEL", "accepted")

    def test_uncommitted_files_in_main_are_named_and_accept_succeeds_once_restored(self):
        committed = self.certified_candidate()
        (self.root / "product.txt").write_text("edited in main by hand\n", encoding="utf-8")
        response = board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertEqual(response["git_acceptance"]["status"], "failed")
        self.assertIn("product.txt", response["git_acceptance"]["reason"])
        self.assertIn("unsaved changes", response["git_acceptance"]["reason"])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base_commit)
        self.git("checkout", "--", "product.txt")
        retried = board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertEqual(retried["git_acceptance"]["commit"], committed["commit"])
        self.assertEqual(self.git("rev-parse", "main").strip(), committed["commit"])
        self.assertEqual((self.root / "product.txt").read_text(encoding="utf-8"), "accepted\n")

    def test_moved_main_routes_reintegration_and_preserves_external_commit(self):
        committed = self.certified_candidate()
        (self.root / "external.txt").write_text("external\n", encoding="utf-8")
        self.git("add", "external.txt")
        self.git("commit", "-m", "external main movement")
        external = self.git("rev-parse", "main").strip()
        response = board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertEqual(response["git_acceptance"]["status"], "reintegration_required")
        self.assertEqual(self.git("rev-parse", "main").strip(), external)
        self.assertNotEqual(external, committed["commit"])
        self.assertIn("GIT-MODEL", board.snapshot(self.root)["git_reintegration_required"])

    def test_remote_push_requires_separate_instruction_and_immediate_confirmation(self):
        committed = self.certified_candidate()
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        remote = self.root.parent / (self.root.name + "-remote.git")
        remote.mkdir()
        self.addCleanup(shutil.rmtree, remote, ignore_errors=True)
        self.git("init", "--bare", cwd=remote)
        self.git("remote", "add", "approved", str(remote))
        instruction = board.record_remote_push_instruction(
            self.root, "GIT-MODEL", "approved", "main",
        )
        self.assertFalse(instruction["confirmed_at"])
        pushed = board.confirm_remote_push(self.root, "GIT-MODEL", instruction["id"])
        self.assertEqual(pushed["commit"], committed["commit"])
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=remote).strip(), committed["commit"])
        with self.assertRaisesRegex(ValueError, "unused instruction"):
            board.confirm_remote_push(self.root, "GIT-MODEL", instruction["id"])

    def test_push_instruction_is_durable_without_network_contact(self):
        self.certified_candidate()
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.git("remote", "add", "offline", "ssh://127.0.0.1:1/unreachable.git")
        instruction = board.record_remote_push_instruction(
            self.root, "GIT-MODEL", "offline", "main",
            expected_remote_tip="1" * 40,
        )
        self.assertEqual(instruction["expected_remote_tip"], "1" * 40)
        self.assertFalse(instruction["confirmed_at"])
        self.assertNotIn("GIT-MODEL", board.snapshot(self.root).get("remote_push_outcomes", {}))

    def test_push_failure_preserves_local_acceptance(self):
        committed = self.certified_candidate()
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        remote = self.root.parent / (self.root.name + "-vanishing.git")
        remote.mkdir()
        self.git("init", "--bare", cwd=remote)
        self.git("remote", "add", "vanishing", str(remote))
        instruction = board.record_remote_push_instruction(
            self.root, "GIT-MODEL", "vanishing", "main",
        )
        shutil.rmtree(remote)
        with self.assertRaisesRegex(ValueError, "push failed|tip could not"):
            board.confirm_remote_push(self.root, "GIT-MODEL", instruction["id"])
        state = board.snapshot(self.root)
        self.assertEqual(state["git_acceptances"]["GIT-MODEL"]["commit"], committed["commit"])
        self.assertEqual(state["remote_push_outcomes"]["GIT-MODEL"]["outcome"], "failed")

    def test_owner_ui_exposes_two_distinct_push_steps_without_browser_confirm(self):
        page = board_viewer.PAGE
        self.assertIn("Push accepted commit", page)
        self.assertIn("Record push instruction", page)
        self.assertIn("Confirm push now", page)
        self.assertIn("/push-instruction", page)
        self.assertIn("/push-confirm", page)
        push_flow = page.split("function openPushDialog", 1)[1].split("async function submitRejection", 1)[0]
        self.assertNotIn("window.confirm", push_flow)

    def test_real_viewer_accept_endpoint_executes_transactional_local_acceptance(self):
        committed = self.certified_candidate()
        server = board_viewer.ThreadingHTTPServer(
            ("127.0.0.1", 0), board_viewer.make_handler(self.root),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: thread.join(timeout=3))
        self.addCleanup(server.shutdown)
        request = Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/releases/GIT-MODEL/decision",
            data=json.dumps({"decision": "accepted"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        self.assertEqual(response.status, 201)
        self.assertEqual(payload["decision"]["git_acceptance"]["commit"], committed["commit"])
        self.assertEqual(self.git("rev-parse", "main").strip(), committed["commit"])
        self.assertNotIn("GIT-MODEL", board.snapshot(self.root).get("remote_push_outcomes", {}))


if __name__ == "__main__":
    unittest.main()

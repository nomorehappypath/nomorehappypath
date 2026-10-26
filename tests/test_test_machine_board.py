# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The board side of the test machine: commands, roles, stamped evidence, stale refusals, captures, recovery.

Run:  PYTHONPATH=. python3 -m unittest tests.test_test_machine_board -v
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest import mock

from harness import board, board_surface, cto, certified_execution, control, execution_identity, test_machine, test_machine_followup
from harness.board_surface import PROTOCOL_VERSION, SurfaceAuthorizationError, SurfaceProtocolError
from tests.test_screen_check_command import ScreenCheckFixture
from tests.test_test_machine import connected_descriptor

REPO = Path(__file__).resolve().parents[1]


class MachineBoardFixture(ScreenCheckFixture):
    def op(self, token, operation, *arguments):
        return self.gateway.execute(token, {
            "protocol": PROTOCOL_VERSION, "nonce": time.time_ns(), "operation": operation,
            "arguments": [operation, "--agent", self.agent_ids[token], *arguments],
        })["result"]

    def write_machine_test(self, name="test_smoke.py", body="self.assertTrue(True)"):
        path = Path(self.context.code_root) / name
        path.write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        " + body + "\n")
        return name

    def run_command(self, command, *, role="delivery", environment=None):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO))
        env.update(environment or {})
        candidate = execution_identity.candidate_evidence_identity("c" * 40, "t" * 40, "contract-1", {"ledger": "l" * 64})
        return certified_execution.run(
            self.context, self.context.code_root, command, candidate=candidate, environment_sha256="e" * 64,
            environment=env, lockfile_digests={}, role=role, gate="final_acceptance")


class RoleAndCommandTests(MachineBoardFixture):
    def test_roles_for_the_three_new_commands(self):
        matrix = board_surface.AUTHORIZATION_MATRIX
        self.assertEqual(matrix["need-test-machine"], frozenset({"engineering", "qa"}))
        self.assertEqual(matrix["machine-baseline"], frozenset({"engineering", "qa"}))
        self.assertEqual(matrix["machine-status"], frozenset({"engineering", "qa", "cto"}))
        self.assertIn("machine-status", board_surface.SHORT_BOARD_OPERATIONS)

    def test_delivery_and_reviewer_raise_the_card_without_a_cto_and_the_cto_cannot(self):
        _d, delivery = self.agent("engineering")
        _r, reviewer = self.agent("qa")
        _c, cto = self.agent("cto")
        result = self.op(delivery, "need-test-machine", "--reason", "run the Windows build")
        self.assertEqual(result["state"], "not_connected")
        self.assertIn("Mission Control", result["card"])
        value = test_machine.read(self.context)
        self.assertEqual(value["request"]["reason"], "run the Windows build")
        self.op(reviewer, "need-test-machine", "--reason", "rerun the Windows tests")
        self.assertEqual(len(test_machine.read(self.context)["waiters"]), 2)
        with self.assertRaises(SurfaceAuthorizationError):
            self.op(cto, "need-test-machine", "--reason", "x")
        with self.assertRaises(SurfaceAuthorizationError):
            self.op(cto, "machine-baseline", "--begin")

    def test_machine_status_for_every_role_prints_the_rule_and_refuses_a_stale_revision(self):
        connected_descriptor(self.context)
        for role in ("engineering", "qa", "cto"):
            _a, token = self.agent(role)
            report = self.op(token, "machine-status")
            self.assertTrue(report["usable"])
            self.assertEqual(report["revision"], 1)
            self.assertIn("machine-status", report["rule"])
        _a, token = self.agent("engineering")
        with self.assertRaisesRegex(SurfaceProtocolError, "machine changed"):
            self.op(token, "machine-status", "--expect-revision", "0")
        self.assertEqual(self.op(token, "machine-status", "--expect-revision", "1")["revision"], 1)

    def test_machine_status_still_answers_when_the_password_is_required(self):
        connected_descriptor(self.context)
        test_machine.mark_reauth(self.context, "project_stopped")
        _a, token = self.agent("engineering")
        report = self.op(token, "machine-status")
        self.assertFalse(report["usable"])
        self.assertEqual(report["state"], "reauth_required")
        self.assertTrue(report["reauth_required"])

    def test_the_machine_commands_keep_working_while_the_project_is_paused(self):
        """Found by the owner-like walk: Pause requires the password again, and a standalone machine-status must still answer then
        (the authenticated agent surface is gone during a pause, so this is the board function the CLI calls)."""
        connected_descriptor(self.context)
        agent, _token = self.agent("engineering")
        board.begin_project_pause(self.context, 0)
        board.finish_project_pause(self.context)
        test_machine.mark_reauth(self.context, "project_stopped")
        self.assertEqual(board.test_machine_status(self.context, agent["id"])["state"], "reauth_required")
        self.assertIn("Mission Control", board.request_test_machine(self.context, agent["id"], "after the pause")["card"])

    def test_baseline_begin_and_done_through_the_surface(self):
        connected_descriptor(self.context)
        _a, token = self.agent("engineering")
        begun = self.op(token, "machine-baseline", "--begin")
        self.assertEqual(begun["baseline"], "incomplete")
        self.assertFalse(self.op(token, "machine-status")["usable"])
        with self.assertRaises(SurfaceProtocolError):
            self.op(token, "machine-baseline", "--done", "--note", "x")
        with self.assertRaisesRegex(SurfaceProtocolError, "exactly one of --begin"):
            self.op(token, "machine-baseline", "--begin", "--done", "--note", "x")
        with self.assertRaisesRegex(SurfaceProtocolError, "exactly one of --begin"):
            self.op(token, "machine-baseline")
        done = self.op(token, "machine-baseline", "--done", "--note", "rustc 1.80.0 and cargo 1.80.0 installed under the machine lock")
        self.assertEqual(done["baseline"], "complete")
        self.assertTrue(self.op(token, "machine-status")["ready"])

    def test_cli_parses_the_new_commands(self):
        import io
        from contextlib import redirect_stdout
        connected_descriptor(self.context)
        agent, _token = self.agent("engineering")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = board.main(["--root", str(self.context.code_root), "--data-root", str(self.context.data_root),
                               "--workspace-root", str(self.context.workspace_root), "machine-status", "--agent", agent["id"]])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(buffer.getvalue())["usable"])


class CertifiedCommandGateTests(MachineBoardFixture):
    def test_a_machine_command_with_no_machine_is_refused_with_the_card_and_the_action(self):
        name = self.write_machine_test()
        with self.assertRaisesRegex(test_machine.MachineError, "Mission Control"):
            self.run_command(f"TEST_MACHINE_GEN=1 python3 -m unittest {name[:-3]}")
        self.assertEqual(test_machine.read(self.context)["request"]["by"], "delivery")

    def test_a_local_command_runs_with_no_machine_and_while_the_password_is_required(self):
        name = self.write_machine_test()
        result = self.run_command(f"python3 -m unittest {name[:-3]}")
        self.assertNotIn("machine", result["measurement"])
        connected_descriptor(self.context)
        test_machine.mark_reauth(self.context, "project_stopped")
        again = self.run_command(f"python3 -m unittest {name[:-3]}")
        self.assertEqual(again["measurement"]["cache_decision"], "exact_success_reused")

    def test_machine_result_is_stamped_reused_only_for_the_same_stamp_and_refused_after_a_stop(self):
        name = self.write_machine_test()
        command = f"TEST_MACHINE_GEN=1 python3 -m unittest {name[:-3]}"
        connected_descriptor(self.context)
        first = self.run_command(command)
        stamp = first["measurement"]["machine"]
        self.assertEqual(first["measurement"]["cache_decision"], "executed_and_certified")
        self.assertEqual(stamp["revision"], 1)
        second = self.run_command(command)
        self.assertEqual(second["measurement"]["cache_decision"], "exact_success_reused")
        self.assertEqual(second["measurement"]["machine"], stamp)
        # a stop: the cached success is NOT returned, the freshness check comes first
        test_machine.mark_reauth(self.context, "project_stopped")
        with self.assertRaisesRegex(test_machine.MachineError, "password again"):
            self.run_command(command)
        # reconnect (new revision and epoch): the old pass is a miss and the command really runs again
        def reconnect(value):
            value.update({"reauth_required": False, "reauth_cause": "", "revision": value["revision"] + 1,
                          "auth_epoch": value["auth_epoch"] + 1, "authorized_by": test_machine.authorization_record(test_machine.manager_identity())})
        test_machine.update(self.context, reconnect)
        third = self.run_command(command)
        self.assertEqual(third["measurement"]["cache_decision"], "executed_and_certified")
        self.assertEqual(third["measurement"]["machine"]["revision"], 2)

    def test_a_tool_change_marker_makes_earlier_passes_miss_and_blocks_certification_until_done(self):
        name = self.write_machine_test()
        command = f"TEST_MACHINE_GEN=1 python3 -m unittest {name[:-3]}"
        connected_descriptor(self.context)
        self.run_command(command)
        test_machine.baseline_begin(self.context, "Delivery")
        with self.assertRaisesRegex(test_machine.MachineError, "machine-baseline --done"):
            self.run_command(command)
        test_machine.baseline_done(self.context, "Delivery", "python 3.12 and make installed on the machine")
        again = self.run_command(command)
        self.assertEqual(again["measurement"]["cache_decision"], "executed_and_certified", "the tools changed: an old pass is not reused")

    def test_a_stop_during_the_run_discards_the_result(self):
        connected_descriptor(self.context)
        name = "test_midrun.py"
        (Path(self.context.code_root) / name).write_text(textwrap.dedent(f'''
            import unittest
            from harness import test_machine
            from harness.project_context import ProjectContext
            class T(unittest.TestCase):
                def test_stop_in_the_middle(self):
                    ctx = ProjectContext({str(self.context.code_root)!r}, {str(self.context.data_root)!r}, {str(self.context.workspace_root)!r})
                    test_machine.mark_reauth(ctx, "project_stopped")
        '''))
        with self.assertRaisesRegex(ValueError, "result was discarded"):
            self.run_command("TEST_MACHINE_GEN=1 python3 -m unittest test_midrun")
        store = Path(board.board_dir(self.context)) / execution_identity.STORE_NAME
        self.assertFalse(store.exists() and "test_midrun" in store.read_text(), "nothing from the interrupted run was certified")

    def test_the_cto_comparison_follows_the_current_stamp(self):
        argv = ["TEST_MACHINE_GEN=1", "make", "test"]
        self.assertIsNone(test_machine.stamp_for_command_or_none(self.context, ["make", "test"]))
        self.assertEqual(test_machine.stamp_for_command_or_none(self.context, argv), {"unusable": "not_connected"})
        connected_descriptor(self.context)
        current = test_machine.stamp_for_command_or_none(self.context, argv)
        self.assertEqual(current["revision"], 1)
        test_machine.mark_reauth(self.context, "project_stopped")
        self.assertEqual(test_machine.stamp_for_command_or_none(self.context, argv), {"unusable": "reauth_required"})

    def test_the_command_identity_of_a_local_command_is_unchanged_by_this_feature(self):
        candidate = execution_identity.candidate_evidence_identity("c", "t", "k", {})
        plain = execution_identity.command_run_identity(candidate, ["make", "test"], ".", "e")
        self.assertNotIn("machine", plain["fields"])
        stamped = execution_identity.command_run_identity(candidate, ["make", "test"], ".", "e", machine={"revision": 1})
        self.assertNotEqual(plain["sha256"], stamped["sha256"])


class EvidenceFreshnessTests(MachineBoardFixture):
    def stale_request(self, stamp):
        return {"task": "T1", "status": "claimed", "developer_id": "dev",
                "command_executions": [{"kind": "unit_test", "command": "TEST_MACHINE_GEN=1 make test", "machine": stamp}]}

    def test_a_verdict_is_refused_when_machine_evidence_went_stale(self):
        connected_descriptor(self.context)
        stamp = test_machine.require_usable(self.context)
        request = self.stale_request(stamp)
        board._require_fresh_machine_evidence(self.context, request)
        test_machine.mark_reauth(self.context, "project_stopped")
        with self.assertRaisesRegex(ValueError, "stale"):
            board._require_fresh_machine_evidence(self.context, request)
        local = {"command_executions": [{"kind": "unit_test", "command": "make test"}]}
        board._require_fresh_machine_evidence(self.context, local)   # local-only evidence is never touched

    def test_resume_reconciliation_does_not_reuse_a_pass_whose_machine_evidence_is_stale(self):
        connected_descriptor(self.context)
        stamp = test_machine.require_usable(self.context)
        request = {"id": "r1", "task": "T1", "evidence_reuse_identity": {"version": 3}, "command_executions": [
            {"kind": "unit_test", "command": "TEST_MACHINE_GEN=1 make test", "machine": stamp}]}
        with mock.patch.object(board, "_git_review_artifact", return_value={}), mock.patch.object(board, "_contract_revision", return_value={}):
            def machine_check():
                checks = board._evidence_reuse_checks(self.context, {"task_workspaces": {}}, request)
                return [c for c in checks if c["identity"] == "test machine evidence"]
            self.assertTrue(machine_check()[0]["matched"])
            test_machine.mark_reauth(self.context, "project_stopped")
            stale = machine_check()[0]
            self.assertFalse(stale["matched"])
            self.assertIn("stale", stale["actual"])
            request["command_executions"] = [{"kind": "unit_test", "command": "make test"}]
            self.assertEqual(machine_check(), [], "a request with no machine evidence gets no machine check")

    def test_the_reviewer_challenge_shortcut_is_not_taken_with_a_stale_machine_stamp(self):
        connected_descriptor(self.context)
        stamp = test_machine.require_usable(self.context)
        request = {"challenge_execution": {"machine_stamps": [stamp]}}
        self.assertEqual(board._stale_machine_evidence(self.context, request), [])
        test_machine.mark_reauth(self.context, "project_stopped")
        self.assertEqual(board._stale_machine_evidence(self.context, request), ["the reviewer's challenge execution"])

    def test_a_capture_with_the_machine_flag_is_stamped_cited_and_then_refused_when_stale(self):
        connected_descriptor(self.context)
        _a, token = self.agent("engineering")
        result = self.gateway.execute(token, {
            "protocol": PROTOCOL_VERSION, "nonce": time.time_ns(), "operation": "screen-check",
            "arguments": ["screen-check", "--agent", self.agent_ids[token], "--url", self.url, "--expect", "deliverables ready", "--machine"],
        })["result"]
        self.assertEqual(result["machine"]["revision"], 1)
        record, problem = board._screen_check_problems(self.context, board.snapshot(self.context)["agents"][self.agent_ids[token]]["task"], result["id"])
        self.assertEqual(problem, "")
        self.assertEqual(record["machine"], result["machine"])
        test_machine.mark_reauth(self.context, "project_stopped")
        _record, problem = board._screen_check_problems(self.context, board.snapshot(self.context)["agents"][self.agent_ids[token]]["task"], result["id"])
        self.assertIn("captured from the owner's test machine", problem)
        self.assertIn("--machine", problem)

    def test_a_capture_with_the_machine_flag_is_refused_when_no_machine_is_usable(self):
        _a, token = self.agent("engineering")
        with self.assertRaises(SurfaceProtocolError) as caught:
            self.gateway.execute(token, {
                "protocol": PROTOCOL_VERSION, "nonce": time.time_ns(), "operation": "screen-check",
                "arguments": ["screen-check", "--agent", self.agent_ids[token], "--url", self.url, "--machine"],
            })
        self.assertIn("Mission Control", str(caught.exception))

    def test_a_capture_without_the_flag_is_a_plain_local_capture(self):
        _a, token = self.agent("engineering")
        result = self.gateway.execute(token, {
            "protocol": PROTOCOL_VERSION, "nonce": time.time_ns(), "operation": "screen-check",
            "arguments": ["screen-check", "--agent", self.agent_ids[token], "--url", self.url, "--expect", "deliverables ready"],
        })["result"]
        self.assertNotIn("machine", result)


class AuthorizationLivenessTests(MachineBoardFixture):
    def test_kill_nine_of_the_manager_voids_authorization_before_any_relaunch(self):
        helper = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f'''
                import sys, time
                sys.path.insert(0, {str(REPO)!r})
                from harness import test_machine
                from harness.project_context import ProjectContext
                ctx = ProjectContext({str(self.context.code_root)!r}, {str(self.context.data_root)!r}, {str(self.context.workspace_root)!r})
                identity = test_machine.manager_identity()
                def change(v):
                    v.update({{"connected": True, "address": "127.0.0.1", "user": "u", "port": 22, "revision": 1, "auth_epoch": 1,
                              "reauth_required": False, "host_key": {{"fingerprint": "SHA256:x"}}, "authorized_by": test_machine.authorization_record(identity)}})
                test_machine.update(ctx, change)
                print("ready", flush=True)
                time.sleep(120)
            ''')], stdout=subprocess.PIPE, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.addCleanup(lambda: helper.poll() is None and helper.kill())
        self.assertEqual(helper.stdout.readline().strip(), "ready")
        self.assertTrue(test_machine.usability(self.context)[0], "authorized while the manager lives")
        os.kill(helper.pid, signal.SIGKILL)            # the helper we started, by the pid its Popen returned
        helper.wait(timeout=10)
        helper.stdout.close()
        usable, code, message = test_machine.usability(self.context)
        self.assertFalse(usable)
        self.assertEqual(code, "manager_down")
        _a, token = self.agent("engineering")
        with self.assertRaises(test_machine.MachineError):
            self.run_command("TEST_MACHINE_GEN=1 python3 -m unittest test_nothing")

    def test_a_different_boot_voids_authorization(self):
        connected_descriptor(self.context)
        def other_boot(value):
            value["authorized_by"]["boot_id"] = "a-previous-boot"
        test_machine.update(self.context, other_boot)
        self.assertEqual(test_machine.usability(self.context)[1], "manager_down")


class RecoveryObligationTests(MachineBoardFixture):
    def make_stale_request(self, task="TASK-1"):
        agent, _token = self.agent("engineering")
        connected_descriptor(self.context)
        old = test_machine.require_usable(self.context)
        with board.locked_state(self.context) as state:
            state["agents"][agent["id"]]["task"] = task
            state.setdefault("qa_requests", {})["req-old"] = {
                "id": "req-old", "task": task, "status": "claimed", "developer_id": agent["id"], "requested_at": "2026-10-20T10:00:00+00:00", "phase": "p", "subtask": "s", "chunk": "c", "cycle": 1, "claimed_by": "", "review_wait_started_at": "",
                "command_executions": [{"kind": "unit_test", "command": "TEST_MACHINE_GEN=1 make test", "machine": old}]}
        return agent

    def reconnect(self):
        def change(value):
            value.update({"revision": value["revision"] + 1, "auth_epoch": value["auth_epoch"] + 1, "reauth_required": False})
        test_machine.update(self.context, change)

    def test_connect_hands_every_stale_request_to_its_delivery_at_once_and_it_stays_visible(self):
        agent = self.make_stale_request()
        self.reconnect()
        result = test_machine_followup.after_connect(self.context)
        self.assertEqual([item["request_id"] for item in result["obligations"]], ["req-old"])
        obligation = result["obligations"][0]
        self.assertEqual((obligation["owner_role"], obligation["state"]), ("Delivery", "sent"))
        with control.locked_state(self.context) as control_state:
            texts = [item["text"] for item in control_state.get("instruction_receipts", {}).values()]
        self.assertTrue(any("req-old" in text and "resubmit" in text for text in texts), texts)
        self.assertTrue(any("test-machine" in text and "machine-baseline" in text for text in texts), "the rule reaches the running agent")
        self.assertEqual(test_machine.public_status(self.context)["recovering"][0]["request_id"], "req-old")

    def test_without_a_delivery_the_cto_owns_it_and_without_anyone_it_waits_visibly(self):
        agent = self.make_stale_request()
        with board.locked_state(self.context) as state:
            state["agents"][agent["id"]]["active"] = False
        self.reconnect()
        none = test_machine_followup.after_connect(self.context)["obligations"][0]
        self.assertEqual((none["owner_role"], none["state"]), ("nobody", "waiting_for_an_agent"))
        cto, _t = self.agent("cto")
        again = test_machine_followup.after_connect(self.context)["obligations"][0]
        self.assertEqual((again["owner_role"], again["state"]), ("CTO", "sent"))

    def test_obligation_clears_when_the_task_has_a_current_request_and_never_revives_failed_work(self):
        self.make_stale_request()
        self.reconnect()
        test_machine_followup.after_connect(self.context)
        self.assertEqual(len(test_machine_followup.refresh_recovering(self.context)), 1)
        current = test_machine.require_usable(self.context)
        with board.locked_state(self.context) as state:
            state["qa_requests"]["req-new"] = {"id": "req-new", "task": "TASK-1", "status": "claimed", "developer_id": "x", "requested_at": "2026-10-20T11:00:00+00:00", "phase": "p", "subtask": "s", "chunk": "c", "cycle": 1, "claimed_by": "", "review_wait_started_at": "",
                                               "command_executions": [{"kind": "unit_test", "command": "TEST_MACHINE_GEN=1 make test", "machine": current}]}
        self.assertEqual(test_machine_followup.refresh_recovering(self.context), [])
        self.reconnect()
        with board.locked_state(self.context) as state:
            state["qa_requests"]["req-old"]["status"] = "failed"
            state["qa_requests"]["req-new"]["status"] = "failed"
        self.assertEqual(test_machine_followup.stale_requests(self.context), [], "failed work is never revived")


class LaunchTextTests(MachineBoardFixture):
    def test_the_launch_script_gives_every_launch_the_machine_note_and_nothing_for_other_projects(self):
        script = (REPO / "scripts" / "run_managed_agent.sh").read_text()
        self.assertIn("machine_note()", script)
        self.assertIn('machine_text="$(machine_note', script)
        connected_descriptor(self.context)
        out = subprocess.run(
            [sys.executable, "-E", "-c", "import sys; sys.path.insert(0, sys.argv[1]);"
             "from harness import test_machine; from harness.project_context import context_from_roots;"
             "print(test_machine.launch_note(context_from_roots(sys.argv[2], sys.argv[3], sys.argv[4])))",
             str(REPO), str(self.context.code_root), str(self.context.data_root), str(self.context.workspace_root)],
            capture_output=True, text=True, check=True).stdout
        self.assertIn("machine-status", out)
        self.assertNotIn("password:", out.lower().replace("password again", ""))


class CtoReleaseHealthTests(MachineBoardFixture):
    """The CTO's reuse of the final Delivery certification follows the current machine stamp."""

    def certified_request(self, command, stamp):
        ledger = Path(self.context.code_root) / "ledger.md"
        ledger.write_text("| ID | What |\n|---|---|\n| S-001 | release health |\n")
        ledger_sha256 = hashlib.sha256(ledger.read_bytes()).hexdigest()
        request = {
            "id": "review-final-01", "task": "TASK-MACHINE", "cycle": 1, "phase": "final_acceptance", "subtask": "", "chunk": "",
            "structure_revision": 0, "test_scope": "full", "delivery_state": "passed", "status": "passed",
            "reviewed_commit": "a" * 40, "reviewed_tree_hash": "b" * 40, "ledger_sha256": ledger_sha256,
            "contract_revision": {"sha256": "c" * 64}, "environment_identity": {"sha256": "d" * 64},
            "unit_test_command": command,
            "certified_artifacts": {"delivery_ledger": {"path": str(ledger), "sha256": ledger_sha256}}, "command_executions": [],
        }
        candidate = execution_identity.run_candidate_identity(request["reviewed_commit"], request["reviewed_tree_hash"])
        run = execution_identity.command_run_identity(
            candidate, command.split(), ".", "d" * 64, {}, role="delivery", gate="final_acceptance::", machine=stamp)
        audit = {"problems": [], "timed_out": False, "forbidden_owner_browser_descendants": {},
                 "new_keychain_or_permission_prompts": {}, "default_handlers_unchanged": True}
        payload = (json.dumps(audit, sort_keys=True) + "\n").encode()
        digest = hashlib.sha256(payload).hexdigest()
        path = board.board_dir(self.context) / "execution-audits" / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        output = "Ran 17 tests in 0.100s\n\nOK\n"
        certified = execution_identity.certify(
            self.context, run, exit_code=0, output_sha256=hashlib.sha256(output.encode()).hexdigest(), duration_seconds=0.1,
            output=output, metadata={"process_audit": {"path": str(path), "sha256": digest}})["entry"]
        request["command_executions"] = [{"kind": "unit_test", "exit_code": 0, "command": command,
                                          "execution_identity": run["sha256"], "execution_record_id": certified["record_id"]}]
        return request

    def test_release_health_reuse_requires_the_current_machine_stamp_for_a_machine_command(self):
        connected_descriptor(self.context)
        command = "TEST_MACHINE_GEN=1 python3 -m unittest discover -s tests"
        request = self.certified_request(command, test_machine.require_usable(self.context))
        self.assertTrue(cto._certified_delivery_health(self.context, request)["verified"])
        test_machine.mark_reauth(self.context, "project_stopped")
        self.assertFalse(cto._certified_delivery_health(self.context, request)["verified"], "a stop makes the old certification unusable")

    def test_a_machine_command_certified_without_any_stamp_is_never_reused(self):
        connected_descriptor(self.context)
        request = self.certified_request("TEST_MACHINE_GEN=1 python3 -m unittest discover -s tests", None)
        self.assertFalse(cto._certified_delivery_health(self.context, request)["verified"])

    def test_a_local_command_is_reused_whatever_the_machine_is_doing(self):
        request = self.certified_request("python3 -m unittest discover -s tests", None)
        self.assertTrue(cto._certified_delivery_health(self.context, request)["verified"])
        connected_descriptor(self.context)
        test_machine.mark_reauth(self.context, "project_stopped")
        self.assertTrue(cto._certified_delivery_health(self.context, request)["verified"])

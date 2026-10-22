# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A terminal that Resume relaunches registers while the resume is still finishing (batch review F1, 2026-10-20).

After Pause -> app restart -> Resume, the worker is brand new and refused EVERY board command until the manager had finished the
resume, so a relaunched agent that registered first exited with code 2 (a real Claude reviewer on the Mac, twice). The board layer
already admits the registration of a session the resume saved; the worker hook now lets `register` through while the project is
"resuming" (not "paused"), everything else stays refused, and the manager tells every live session once the board is open again.

Run:  PYTHONPATH=. python3 -m unittest tests.test_resume_registration -v
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, control, project_manager, project_registry as registry, project_worker
from harness.board_surface import PROTOCOL_VERSION, CommandGateway, SessionTokenAuthority
from harness.project_context import ProjectContext
from tests import test_stage4_system_layer as stage4
from tests.environment_support import require_loopback
from tests.test_conversation_memory import stage_relaunch

RESUMING_SENTENCE = "project resume must complete before board commands are accepted"


class WorkerFixture(unittest.TestCase):
    """The real worker handler, authority and gateway, with the real pre-command hook and a FRESH startup maintenance."""

    def setUp(self):
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        code = base / "code"
        code.mkdir()
        self.context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(self.context)
        self.authority = SessionTokenAuthority(self.context)
        self.gateway = CommandGateway(self.context, self.authority)
        self.maintenance = project_worker.StartupMaintenance(self.context)
        self.hook = project_worker.make_before_command(self.authority, self.maintenance)
        endpoint = {"value": ""}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), project_worker.make_handler(
            self.context, authority=self.authority, gateway=self.gateway, endpoint=lambda: endpoint["value"],
            before_command=self.hook))
        endpoint["value"] = self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.nonce = int(time.time_ns())

    def agent(self, kind="claude_reviewer", role="qa"):
        session = control.create(self.context, kind)
        self.authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())
        token = self.authority.claim(session["id"], os.getpid())
        return session, token

    def post(self, token, operation, *arguments, body=None):
        self.nonce += 1
        payload = body or {"protocol": PROTOCOL_VERSION, "nonce": self.nonce, "operation": operation, "arguments": [operation, *arguments]}
        request = Request(self.url + "/api/board/command", data=json.dumps(payload).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}", "Origin": self.url})
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def register_args(self, session):
        return ("--role", "qa", "--task", "REVIEW_QUEUE", "--name", "Independent Reviewer", "--vendor", "Anthropic", "--session-id", session["id"])

    def saved_reviewer(self):
        """A reviewer registered while the project was active, then Pause: the saved session of the next resume."""
        session, token = self.agent()
        agent = board.register(self.context, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        board.begin_project_pause(self.context, 0)
        board.finish_project_pause(self.context)
        return session, token, agent

    def begin_resume(self):
        return board.begin_project_resume(self.context)


class FreshWorkerTests(WorkerFixture):
    def test_a_saved_session_registers_while_the_resume_is_finishing_and_everything_else_is_refused(self):
        session, token, agent = self.saved_reviewer()
        status, value = self.post(token, "register")
        self.assertEqual(status, 403, "while the project is PAUSED (not resuming) even a saved session is refused")
        self.assertIn(RESUMING_SENTENCE, value["error"])
        transaction = self.begin_resume()
        self.assertEqual(board.pause_state(self.context)["status"], "resuming")
        status, value = self.post(token, "register")
        self.assertEqual(status, 200, value)
        self.assertEqual(value["result"]["id"], agent["id"], "it re-attached as the SAME agent")
        status, value = self.post(token, "poll", "--agent", agent["id"])
        self.assertEqual(status, 403, "every other operation is still refused until the resume finishes")
        self.assertIn(RESUMING_SENTENCE, value["error"])
        self.assertIn("press Resume again", value["error"], "an interrupted Resume has a plain way out")
        # the resume finishes: the maintenance runs ONCE, at the next command, and the agent works
        with mock.patch.object(board, "recover_git_transactions") as recovered, mock.patch.object(project_worker.release_coordinator, "coordinate") as coordinated:
            board.finish_project_resume(self.context, transaction["resume_id"])
            status, value = self.post(token, "poll", "--agent", agent["id"])
            self.assertEqual(status, 200, value)
            self.post(token, "poll", "--agent", agent["id"])
        self.assertEqual((recovered.call_count, coordinated.call_count), (1, 1))

    def test_a_session_the_resume_did_not_save_cannot_register_and_creates_no_agent(self):
        _saved, _token, _agent = self.saved_reviewer()
        self.begin_resume()
        stranger, stranger_token = self.agent()
        before = set(board.snapshot(self.context)["agents"])
        status, value = self.post(stranger_token, "register", *self.register_args(stranger))
        self.assertIn(status, {400, 403}, value)
        self.assertEqual(set(board.snapshot(self.context)["agents"]), before, "no agent was created for an unsaved session")

    def test_a_supplied_session_id_cannot_select_another_session(self):
        saved, token, agent = self.saved_reviewer()
        self.begin_resume()
        stranger, _stranger_token = self.agent()
        status, value = self.post(token, "register", "--role", "qa", "--task", "REVIEW_QUEUE", "--name", "x", "--vendor", "x", "--session-id", stranger["id"])
        self.assertEqual(status, 200, value)
        self.assertEqual(value["result"]["id"], agent["id"], "the authenticated session decides, not the argument")

    def test_a_mismatched_operation_and_arguments_is_refused_and_a_bad_token_never_reaches_maintenance(self):
        saved, token, _agent = self.saved_reviewer()
        self.begin_resume()
        status, value = self.post(token, "register", body={"protocol": PROTOCOL_VERSION, "nonce": 7, "operation": "register", "arguments": ["poll"]})
        self.assertEqual(status, 400, value)
        status, value = self.post("not-a-real-token", "register", *self.register_args(saved))
        self.assertEqual(status, 401)
        self.assertFalse(self.maintenance.complete)

    def test_an_unreadable_request_is_a_plain_400_not_a_crash(self):
        saved, token, _agent = self.saved_reviewer()
        self.begin_resume()
        request = Request(self.url + "/api/board/command", data=b"{not json", method="POST",
                          headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}", "Origin": self.url})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 400)


class MaintainedWorkerTests(WorkerFixture):
    def test_a_long_lived_worker_behaves_the_same_through_two_pause_and_resume_cycles(self):
        session, token = self.agent()
        agent = board.register(self.context, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        with mock.patch.object(board, "recover_git_transactions") as recovered, mock.patch.object(project_worker.release_coordinator, "coordinate"):
            self.maintenance.run()            # the worker was opened while the project was active
            self.assertTrue(self.maintenance.complete)
            for cycle in (1, 2):
                board.begin_project_pause(self.context, 0)
                board.finish_project_pause(self.context)
                transaction = self.begin_resume()
                status, value = self.post(token, "register", *self.register_args(session))
                self.assertEqual(status, 200, (cycle, value))
                board.finish_project_resume(self.context, transaction["resume_id"])
                self.assertEqual(self.post(token, "poll", "--agent", agent["id"])[0], 200)
        self.assertEqual(recovered.call_count, 1, "the maintenance never repeats")


class OperationParsingTests(unittest.TestCase):
    def test_the_operation_is_only_read_while_a_resume_is_finishing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        context = ProjectContext(base / "code", base / "data", base / "workspaces")
        (base / "code").mkdir()
        maintenance = project_worker.StartupMaintenance(context)
        asked = []

        def operation():
            asked.append(1)
            return "register"

        with mock.patch.object(board, "recover_git_transactions"), mock.patch.object(project_worker.release_coordinator, "coordinate"):
            maintenance.run(operation)                      # active: the maintenance runs, nothing is parsed
            self.assertEqual(asked, [])
            self.assertTrue(maintenance.complete)


class ManagerWakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        code = self.base / "code"
        code.mkdir()
        self.entry = registry.register(self.home, "resume-project", code)
        self.context = registry.context_for_entry(self.entry)

    def receipts(self, source):
        with control.locked_state(self.context) as state:
            return [r for r in state.get("instruction_receipts", {}).values() if r.get("source") == source]

    def resume(self, manager):
        class Worker:
            pid = 88123
            def poll(self): return None
            def terminate(self): return None
            def wait(self, timeout=None): return 0
            def kill(self): return None
        with mock.patch("harness.project_manager.subprocess.Popen", return_value=Worker()):
            return manager.resume_project(self.entry["id"])

    def test_every_live_session_is_told_once_after_the_resume_finishes_and_none_before(self):
        sessions = [control.create(self.context, kind) for kind in ("codex_delivery", "claude_reviewer", "claude_cto")]
        for session, role, task, vendor in zip(sessions, ("engineering", "qa", "cto"), (board.AWAITING_OWNER_DIRECTION, "REVIEW_QUEUE", "GLOBAL_MONITOR"), ("OpenAI", "Anthropic", "Anthropic")):
            board.register(self.context, role, task, vendor=vendor, session_id=session["id"])
        launched = []
        manager = project_manager.ProjectManager(self.home, board_port=0, terminal_launcher=lambda _root, s: launched.append(s["id"]))
        manager.pause_project(self.entry["id"], drain_seconds=0, stop_timeout=0)
        self.assertEqual(self.receipts(control.RESUME_COMPLETE_SOURCE), [], "nothing before the resume")
        result = self.resume(manager)
        self.assertEqual(sorted(result["woken"]), sorted(s["id"] for s in sessions))
        receipts = self.receipts(control.RESUME_COMPLETE_SOURCE)
        self.assertEqual(sorted(r["session_id"] for r in receipts), sorted(s["id"] for s in sessions))
        self.assertTrue(all("Poll the board now" in r["text"] and "USER ACTION: None" in r["text"] for r in receipts))
        # a repeated press relaunches nothing but wakes the live sessions again (it also closes a crash between the finish and the wake)
        again = self.resume(manager)
        self.assertEqual(again["relaunched"], [])
        self.assertEqual(sorted(again["woken"]), sorted(s["id"] for s in sessions))
        self.assertEqual(len(self.receipts(control.RESUME_COMPLETE_SOURCE)), 6)

    def test_a_retried_resume_wakes_a_survivor_that_an_interrupted_one_left_running(self):
        session = control.create(self.context, "claude_reviewer")
        board.register(self.context, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        manager = project_manager.ProjectManager(self.home, board_port=0, terminal_launcher=lambda _root, s: None)
        manager.pause_project(self.entry["id"], drain_seconds=0, stop_timeout=0)
        board.begin_project_resume(self.context)              # an earlier Resume began and the app died before it finished
        survivor = control.snapshot(self.context)["sessions"][0]
        self.assertEqual(board.pause_state(self.context)["status"], "resuming")
        result = self.resume(manager)                          # the owner presses Resume again
        self.assertEqual(board.pause_state(self.context)["status"], "active")
        self.assertIn(survivor["id"], result["woken"], "the retried Resume wakes every live session, relaunched or not")

    def test_a_session_that_ended_while_the_resume_finished_is_skipped_quietly(self):
        session = control.create(self.context, "claude_reviewer")
        board.register(self.context, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        manager = project_manager.ProjectManager(self.home, board_port=0, terminal_launcher=lambda _root, s: None)
        manager.pause_project(self.entry["id"], drain_seconds=0, stop_timeout=0)
        with mock.patch.object(control, "enqueue_instruction", side_effect=ValueError("cannot route to an inactive managed session")):
            result = self.resume(manager)
        self.assertEqual(result["woken"], [])
        self.assertEqual(board.pause_state(self.context)["status"], "active")


class RunnerAgainstAResumingWorkerTests(unittest.TestCase):
    """The REAL runner script, relaunched while the project is still resuming, for each kind; the resume finishes a moment later."""
    served = stage4.Stage4RunnerTests.served
    bootstrap_served = stage4.Stage4RunnerTests.bootstrap_served
    setUp = stage4.Stage4RunnerTests.setUp
    fake = stage4.Stage4RunnerTests.fake
    switch = stage4.Stage4RunnerTests.switch
    require_confinement = stage4.Stage4RunnerTests.require_confinement

    def launch_during_resume(self, kind, provider, *, finish_after=2.5):
        self.switch(provider, stage4=False)
        session = control.create(self.context, kind)
        authority = SessionTokenAuthority(self.context)

        def run_runner():
            if self.capture.exists():
                self.capture.unlink()
            authority.prepare(session["id"])
            # every launch gets a FRESH worker (new startup maintenance), as after an app restart
            hook = project_worker.make_before_command(authority, project_worker.StartupMaintenance(self.context))
            with self.served_with_hook(authority, hook) as endpoint, self.bootstrap_served(authority, endpoint) as bootstrap:
                import subprocess, sys
                from tests.claude_auth_support import auth_arguments
                root = Path(__file__).resolve().parents[1]
                return subprocess.run([
                    "/bin/bash", str(root / "scripts" / "run_managed_agent.sh"), "--root", str(self.context.code_root),
                    "--data-root", str(self.context.data_root), "--workspace-root", str(self.context.workspace_root),
                    "--python", os.path.realpath(sys.executable), "--session-id", session["id"], "--kind", kind,
                    "--manager-home", str(self.home), "--board-bootstrap", bootstrap, *auth_arguments(self.context, session),
                ], cwd=self.context.code_root, env={**os.environ, "HARNESS_EXECUTION_ROOT": str(self.context.code_root), **self.environment},
                    capture_output=True, text=True, timeout=120)

        first = run_runner()                                  # the original launch: registers the agent
        self.assertEqual(first.returncode, 0, first.stderr)
        board.begin_project_pause(self.context, 0)
        board.finish_project_pause(self.context)
        transaction = board.begin_project_resume(self.context)
        control.prepare_resume_sessions(self.context, [session["id"]])
        control.mark_resume_launch_requested(self.context, session["id"])
        timer = threading.Timer(finish_after, lambda: board.finish_project_resume(self.context, transaction["resume_id"]))
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        second = run_runner()
        timer.join()
        return second, time.monotonic() - started, session

    @contextmanager
    def served_with_hook(self, authority, hook):
        endpoint = {"value": ""}
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_worker.make_handler(
            self.context, authority=authority, endpoint=lambda: endpoint["value"], before_command=hook))
        endpoint["value"] = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield endpoint["value"]
        finally:
            server.shutdown()
            server.server_close()

    def test_a_relaunched_codex_delivery_registers_and_starts_its_cli_while_the_resume_finishes(self):
        completed, seconds, session = self.launch_during_resume("codex_delivery", "codex")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(self.capture.exists(), "the CLI was started")
        self.assertEqual(board.pause_state(self.context)["status"], "active")

    def test_a_relaunched_claude_reviewer_registers_and_starts_its_cli_while_the_resume_finishes(self):
        self.require_confinement()
        completed, seconds, session = self.launch_during_resume("claude_reviewer", "claude")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(self.capture.exists(), "the CLI was started")


if __name__ == "__main__":
    unittest.main()

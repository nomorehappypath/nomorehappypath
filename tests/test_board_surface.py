# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Session authentication simulations for the Projects board surface."""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, control, project_worker
from harness.board_surface import (
    ALL_BOARD_OPERATIONS,
    AUTHORIZATION_MATRIX,
    ENDPOINT_ENV,
    PROTOCOL_ENV,
    PROTOCOL_VERSION,
    TOKEN_ENV,
    UPLOAD_ARGUMENTS,
    CommandGateway,
    SessionIdentity,
    SessionTokenAuthority,
    SurfaceAuthenticationError,
    SurfaceAuthorizationError,
    SurfaceProtocolError,
    SurfaceReplayError,
    session_environment,
)
from harness.project_context import ProjectContext
from tests.environment_support import require_loopback


class BoardSurfaceAuthenticationTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(self.context)

    def session(self, kind="codex_delivery", role="engineering", task=board.AWAITING_OWNER_DIRECTION):
        session = control.create(self.context, kind)
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())
        agent = board.register(self.context, role, task, session_id=session["id"])
        token = authority.claim(session["id"], os.getpid())
        return session, agent, authority, token

    @contextmanager
    def served(self, authority):
        endpoint_box = {"value": ""}
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            project_worker.make_handler(
                self.context,
                authority=authority,
                endpoint=lambda: endpoint_box["value"],
            ),
        )
        endpoint_box["value"] = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield endpoint_box["value"]
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    @contextmanager
    def bootstrap_served(self, authority, endpoint="http://127.0.0.1:8742"):
        directory = tempfile.TemporaryDirectory(dir="/tmp", prefix="harness-bootstrap-test-")
        path = str(Path(directory.name) / "claim.sock")
        server = project_worker.make_bootstrap_server(path, authority, lambda: endpoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield path
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            directory.cleanup()

    @staticmethod
    def bootstrap_request(path, session_id, protocol=PROTOCOL_VERSION):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        connection.connect(path)
        connection.sendall(json.dumps({
            "session_id": session_id, "protocol": protocol,
        }, separators=(",", ":")).encode() + b"\n")
        response = json.loads(connection.makefile("rb").readline())
        connection.close()
        return response

    def test_token_resolves_identity_only_from_control_and_board_state(self):
        session, agent, authority, token = self.session()
        identity = authority.authenticate(token)
        self.assertEqual(identity.session_id, session["id"])
        self.assertEqual(identity.agent_id, agent["id"])
        self.assertEqual(identity.role, "engineering")
        self.assertEqual(identity.task, board.AWAITING_OWNER_DIRECTION)
        self.assertEqual(identity.project_id, authority.project_id)

    def test_raw_token_is_memory_only_and_authentication_errors_are_sanitized(self):
        _, _, authority, token = self.session()
        stored = authority.path.read_text(encoding="utf-8")
        self.assertNotIn(token, stored)
        self.assertNotIn(TOKEN_ENV, stored)
        with self.assertRaises(SurfaceAuthenticationError) as caught:
            authority.authenticate(token + "forged")
        self.assertNotIn(token, str(caught.exception))
        self.assertEqual(str(caught.exception), "session authentication failed")

    def test_wrong_project_and_cross_session_tokens_are_refused(self):
        first, _, authority, token = self.session()
        second = control.create(self.context, "claude_cto")
        authority.prepare(second["id"])
        control.attach(self.context, second["id"], os.getpid())
        board.register(self.context, "cto", "GLOBAL_MONITOR", session_id=second["id"])
        second_token = authority.claim(second["id"], os.getpid())
        self.assertEqual(authority.authenticate(token).session_id, first["id"])
        self.assertEqual(authority.authenticate(second_token).session_id, second["id"])

        other_base = Path(self._tmp.name) / "other"
        other_code = other_base / "code"; other_code.mkdir(parents=True)
        other = ProjectContext(other_code, other_base / "data", other_base / "workspaces")
        control.initialize(other)
        with self.assertRaisesRegex(SurfaceAuthenticationError, "session authentication failed"):
            SessionTokenAuthority(other).authenticate(token)

    def test_pause_epoch_revokes_and_resume_requires_a_new_token(self):
        session, _, authority, token = self.session()
        with control.locked_state(self.context) as state:
            stored = state["sessions"][session["id"]]
            stored["status"] = "paused"
            stored["auth_epoch"] += 1
            stored["pid"] = None
        with self.assertRaisesRegex(SurfaceAuthenticationError, "session authentication failed"):
            authority.authenticate(token)

        with control.locked_state(self.context) as state:
            stored = state["sessions"][session["id"]]
            stored["status"] = "launching"
        replacement_authority = SessionTokenAuthority(self.context)
        replacement_authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())
        replacement = replacement_authority.claim(session["id"], os.getpid())
        self.assertNotEqual(token, replacement)
        self.assertEqual(replacement_authority.authenticate(replacement).session_id, session["id"])
        with self.assertRaises(SurfaceAuthenticationError):
            replacement_authority.authenticate(token)

    def test_worker_restart_before_claim_replaces_unrecoverable_raw_token(self):
        session = control.create(self.context, "codex_delivery")
        first_worker = SessionTokenAuthority(self.context)
        first_worker.prepare(session["id"])
        original = first_worker._pending[session["id"]]
        control.attach(self.context, session["id"], os.getpid())
        board.register(self.context, "engineering", board.AWAITING_OWNER_DIRECTION, session_id=session["id"])

        restarted_worker = SessionTokenAuthority(self.context)
        replacement = restarted_worker.claim(session["id"], os.getpid())
        self.assertNotEqual(original, replacement)
        self.assertEqual(restarted_worker.authenticate(replacement).session_id, session["id"])
        with self.assertRaises(SurfaceAuthenticationError):
            restarted_worker.authenticate(original)

    def test_bootstrap_claim_is_one_time_and_returns_exact_environment(self):
        session = control.create(self.context, "codex_delivery")
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())

        endpoint = "http://127.0.0.1:8742"
        with self.bootstrap_served(authority, endpoint) as bootstrap:
            environment = self.bootstrap_request(bootstrap, session["id"])["environment"]
            repeated = self.bootstrap_request(bootstrap, session["id"])
        self.assertEqual(set(environment), {TOKEN_ENV, ENDPOINT_ENV, PROTOCOL_ENV})
        self.assertEqual(environment[ENDPOINT_ENV], endpoint)
        self.assertEqual(environment[PROTOCOL_ENV], PROTOCOL_VERSION)
        self.assertEqual(authority.authenticate(environment[TOKEN_ENV]).session_id, session["id"])
        self.assertIn("error", repeated)
        self.assertNotIn(environment[TOKEN_ENV], json.dumps(repeated))

    def test_unrelated_local_process_cannot_race_the_attached_session_bootstrap(self):
        session = control.create(self.context, "codex_delivery")
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())
        program = r'''import json,os,socket,sys,time
if os.fork(): os._exit(0)
os.setsid(); time.sleep(0.1)
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect(sys.argv[1])
s.sendall(json.dumps({"session_id":sys.argv[2],"protocol":"1"}).encode()+b"\n")
print(s.makefile("rb").readline().decode(),end="")
'''
        with self.bootstrap_served(authority) as bootstrap:
            raced = subprocess.run(
                [os.path.realpath(os.sys.executable), "-c", program, bootstrap, session["id"]],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(raced.returncode, 0, raced.stderr)
            self.assertIn("session authentication failed", json.loads(raced.stdout)["error"])
            legitimate = self.bootstrap_request(bootstrap, session["id"])
        self.assertIn("environment", legitimate)
        self.assertEqual(
            authority.authenticate(legitimate["environment"][TOKEN_ENV]).session_id,
            session["id"],
        )

    def test_protocol_mismatch_and_non_loopback_bind_fail_closed(self):
        session = control.create(self.context, "codex_delivery")
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())
        with self.bootstrap_served(authority) as bootstrap:
            mismatch = self.bootstrap_request(bootstrap, session["id"], "2")
            self.assertIn("protocol version is incompatible", mismatch["error"])
            environment = self.bootstrap_request(bootstrap, session["id"])["environment"]
            self.assertEqual(authority.authenticate(environment[TOKEN_ENV]).session_id, session["id"])
        with self.served(authority) as endpoint:
            with self.assertRaises(HTTPError) as refused:
                urlopen(Request(
                    endpoint + "/api/session/bootstrap", data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST",
                ), timeout=5)
            self.assertEqual(refused.exception.code, 410)
        with self.assertRaisesRegex(ValueError, "loopback"):
            project_worker.serve(self.context, host="0.0.0.0", port=0)
        for endpoint in (
            "http://127.0.0.1:8742@evil.example",
            "http://127.0.0.1.evil.example:8742",
            "https://127.0.0.1:8742",
            "http://127.0.0.1:8742/api/board",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(ValueError, "loopback"):
                session_environment(endpoint, "t" * 64)

    def test_rejected_resume_launch_cannot_revoke_a_live_credential(self):
        session, _, authority, token = self.session()
        with self.served(authority) as endpoint:
            request = Request(
                f"{endpoint}/api/sessions/{session['id']}/resume-launch",
                data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
            )
            with self.assertRaises(HTTPError) as refused:
                urlopen(request, timeout=5)
            self.assertEqual(refused.exception.code, 400)
            self.assertIn("not staged", refused.exception.read().decode())

        self.assertEqual(authority.authenticate(token).session_id, session["id"])
        restarted = SessionTokenAuthority(self.context)
        self.assertEqual(restarted.authenticate(token).session_id, session["id"])
        record = json.loads(restarted.path.read_text(encoding="utf-8"))["sessions"][session["id"]]
        self.assertEqual(record["revoked_at"], "")

    def test_real_managed_runner_bootstraps_token_into_provider_environment(self):
        session = control.create(self.context, "codex_delivery")
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        with self.served(authority) as endpoint, self.bootstrap_served(authority, endpoint) as bootstrap:
            fake = Path(self._tmp.name) / "fake-codex"
            fake.write_text(
                "#!/bin/sh\n"
                "test -n \"$HARNESS_BOARD_TOKEN\" || exit 21\n"
                "test \"$HARNESS_BOARD_PROTOCOL\" = 1 || exit 22\n"
                "printf 'endpoint=%s\\ntoken_present=yes\\n' \"$HARNESS_BOARD_ENDPOINT\"\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runner = Path(__file__).resolve().parents[1] / "scripts" / "run_managed_agent.sh"
            command = [
                "/bin/bash", str(runner),
                "--root", str(self.context.code_root),
                "--data-root", str(self.context.data_root),
                "--workspace-root", str(self.context.workspace_root),
                "--python", os.path.realpath(os.sys.executable),
                "--session-id", session["id"],
                "--kind", session["kind"],
                "--board-bootstrap", bootstrap,
            ]
            environment = dict(os.environ)
            environment.update({
                "HARNESS_CODEX_BIN": str(fake),
                "HARNESS_EXECUTION_ROOT": str(self.context.code_root),
            })
            result = subprocess.run(
                command, cwd=self.context.code_root, env=environment,
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            f"endpoint={endpoint}\ntoken_present=yes\n",
        )
        agents = list(board.snapshot(self.context)["agents"].values())
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["session_id"], session["id"])
        self.assertEqual(agents[0]["role"], "engineering")
        self.assertNotIn("HARNESS_BOARD_TOKEN", authority.path.read_text(encoding="utf-8"))


class BoardSurfaceCommandTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, base / "private-data", base / "workspaces")
        control.initialize(self.context)

    def session(self, kind="codex_delivery", role="engineering", task=board.AWAITING_OWNER_DIRECTION):
        session = control.create(self.context, kind)
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())
        agent = board.register(self.context, role, task, session_id=session["id"])
        token = authority.claim(session["id"], os.getpid())
        return session, agent, authority, token, CommandGateway(self.context, authority)

    def test_worker_defers_startup_maintenance_until_resume_is_active(self):
        board.begin_project_pause(self.context, drain_seconds=0)
        board.finish_project_pause(self.context)
        transaction = board.begin_project_resume(self.context)
        maintenance = project_worker.StartupMaintenance(self.context)
        with mock.patch.object(board, "recover_git_transactions") as recover, mock.patch.object(
            project_worker.release_coordinator, "coordinate",
        ) as coordinate:
            with self.assertRaisesRegex(SurfaceAuthorizationError, "resume must complete"):
                maintenance.run()
            recover.assert_not_called()
            coordinate.assert_not_called()

            board.finish_project_resume(self.context, transaction["resume_id"])
            maintenance.run()
            maintenance.run()
            recover.assert_called_once_with(self.context)
            coordinate.assert_called_once_with(self.context)

    def test_project_watchdog_ticks_without_spawning_and_skips_paused_project(self):
        watchdog = project_worker.ProjectWatchdog(self.context)
        with mock.patch.object(board, "mark_stalled", return_value=[]) as mark, \
                mock.patch.object(project_worker, "launch_terminal") as launch:
            self.assertEqual(watchdog.tick(), [])
            mark.assert_called_once_with(self.context, board.AGENT_STALE_SECONDS)
            launch.assert_not_called()

            board.begin_project_pause(self.context, drain_seconds=0)
            board.finish_project_pause(self.context)
            mark.reset_mock()
            self.assertEqual(watchdog.tick(), [])
            mark.assert_not_called()
            launch.assert_not_called()

    def test_project_watchdog_thread_starts_once_and_stops_cleanly(self):
        watchdog = project_worker.ProjectWatchdog(self.context, interval_seconds=0.01)
        ticked = threading.Event()
        with mock.patch.object(watchdog, "tick", side_effect=lambda: ticked.set() or []):
            watchdog.start()
            self.assertTrue(ticked.wait(1), "worker-owned watchdog never ticked")
            with self.assertRaisesRegex(ValueError, "already started"):
                watchdog.start()
            watchdog.shutdown()
        self.assertIsNotNone(watchdog.thread)
        self.assertFalse(watchdog.thread.is_alive())

    @staticmethod
    def request(operation, arguments, nonce=1):
        return {
            "protocol": PROTOCOL_VERSION, "nonce": nonce,
            "operation": operation, "arguments": [operation, *arguments],
        }

    @contextmanager
    def served(self, authority):
        endpoint_box = {"value": ""}
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_worker.make_handler(
            self.context, authority=authority, endpoint=lambda: endpoint_box["value"],
        ))
        endpoint_box["value"] = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield endpoint_box["value"]
        finally:
            server.shutdown(); thread.join(timeout=3); server.server_close()

    def test_matrix_is_total_and_roles_have_explicit_allowed_and_forbidden_operations(self):
        self.assertEqual(set(AUTHORIZATION_MATRIX), ALL_BOARD_OPERATIONS)
        source = (Path(__file__).resolve().parents[1] / "harness" / "board.py").read_text(encoding="utf-8")
        exposed = set(re.findall(r'add_parser\("([a-z-]+)"\)', source))
        self.assertEqual(ALL_BOARD_OPERATIONS, exposed)
        self.assertTrue(all(isinstance(roles, frozenset) for roles in AUTHORIZATION_MATRIX.values()))
        cases = {
            "engineering": ({"poll", "git-commit", "request-review"}, {"reserve-qa", "push-confirm", "owner-direction"}),
            "qa": ({"poll", "reserve-qa", "qa-result"}, {"git-commit", "complete", "push-confirm"}),
            "cto": ({"poll", "push-confirm", "recover-git"}, {"git-commit", "request-review", "qa-result"}),
        }
        for role, (allowed, forbidden) in cases.items():
            with self.subTest(role=role):
                self.assertTrue(all(role in AUTHORIZATION_MATRIX[item] for item in allowed))
                self.assertTrue(all(role not in AUTHORIZATION_MATRIX[item] for item in forbidden))

    def test_gateway_rejects_forged_agent_task_session_role_and_unknown_operation(self):
        session, agent, _, token, gateway = self.session()
        for arguments in (
            ["--agent", "engineering-forged"],
            ["--agent", agent["id"], "--session-id", "session-forged"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(SurfaceAuthorizationError):
                gateway.execute(token, self.request("poll", arguments, nonce=time.time_ns()))
        with board.locked_state(self.context) as state:
            state["agents"][agent["id"]]["task"] = "owned-task"
        with self.assertRaises(SurfaceAuthorizationError):
            gateway.execute(token, self.request("record-finding", [
                "--task", "other-task", "--title", "x", "--description", "x",
            ], nonce=time.time_ns()))
        with self.assertRaises(SurfaceAuthorizationError):
            gateway.execute(token, self.request("reserve-qa", ["--agent", agent["id"]], nonce=time.time_ns()))
        unknown = self.request("unknown-operation", [], nonce=time.time_ns())
        with self.assertRaises(SurfaceAuthorizationError):
            gateway.execute(token, unknown)
        with self.assertRaisesRegex(SurfaceProtocolError, "only once"):
            gateway.execute(token, self.request("poll", [
                "--agent", agent["id"], "--agent", "engineering-forged",
            ], nonce=time.time_ns()))
        with self.assertRaisesRegex(SurfaceAuthorizationError, "credentials"):
            gateway.execute(token, self.request("status", [
                "--agent", agent["id"], "--note", token,
            ], nonce=time.time_ns()))
        saved = AUTHORIZATION_MATRIX.pop("poll")
        try:
            with self.assertRaises(SurfaceAuthorizationError):
                gateway.execute(token, self.request(
                    "poll", ["--agent", agent["id"]], nonce=time.time_ns(),
                ))
        finally:
            AUTHORIZATION_MATRIX["poll"] = saved
        self.assertEqual(session["id"], gateway.authority.authenticate(token).session_id)

    def test_abbreviated_protected_arguments_cannot_cross_identity_or_path_guards(self):
        _, victim, _, _, _ = self.session()
        reviewer = self.session("claude_reviewer", "qa", "REVIEW_QUEUE")
        reviewer_agent, reviewer_token, reviewer_gateway = reviewer[1], reviewer[3], reviewer[4]
        victim_before = json.dumps(
            board.snapshot(self.context)["agents"][victim["id"]], sort_keys=True,
        )

        for nonce, spelling in enumerate(("--age", "--agen", "--ag"), start=600):
            with self.subTest(spelling=spelling), self.assertRaisesRegex(
                SurfaceAuthorizationError, "abbreviated protected"
            ):
                reviewer_gateway.execute(reviewer_token, self.request("status", [
                    spelling, victim["id"], "--note", "forged through abbreviation",
                ], nonce=nonce))
        with self.assertRaises(SurfaceAuthorizationError):
            reviewer_gateway.execute(reviewer_token, self.request(
                "poll", ["--age", victim["id"]], nonce=610,
            ))

        hostile_spelling_requests = (
            ("poll", ["--sess", "other-session", "--agent", reviewer_agent["id"]]),
            ("status", ["--roo", "/tmp/other", "--agent", reviewer_agent["id"], "--note", "x"]),
            ("claim-qa", ["--chall", "/tmp/ledger.md", "--agent", reviewer_agent["id"]]),
            ("repin-final-review", ["--rep", "/tmp/repo", "--agent", reviewer_agent["id"], "--task", "x"]),
            ("record-finding", ["--tas", "other", "--title", "x", "--description", "x"]),
        )
        for nonce, (operation, arguments) in enumerate(hostile_spelling_requests, start=620):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                SurfaceAuthorizationError, "abbreviated protected"
            ):
                reviewer_gateway.execute(
                    reviewer_token, self.request(operation, arguments, nonce=nonce),
                )

        self.assertEqual(
            json.dumps(board.snapshot(self.context)["agents"][victim["id"]], sort_keys=True),
            victim_before,
        )

        script = Path(__file__).resolve().parents[1] / "harness" / "board.py"
        legacy = subprocess.run([
            os.path.realpath(os.sys.executable), str(script),
            "--root", str(self.context.code_root),
            "--data-root", str(self.context.data_root),
            "--workspace-root", str(self.context.workspace_root),
            "status", "--ag", victim["id"], "--note", "legacy abbreviation",
        ], capture_output=True, text=True, timeout=5)
        self.assertEqual(legacy.returncode, 2)
        self.assertIn("required: --agent", legacy.stderr)
        self.assertEqual(
            json.dumps(board.snapshot(self.context)["agents"][victim["id"]], sort_keys=True),
            victim_before,
        )

    def test_matching_identity_is_stripped_and_reinjected_canonically(self):
        _, agent, _, token, gateway = self.session()
        result = gateway.execute(token, self.request("status", [
            "--note", "canonical identity", "--agent=" + agent["id"],
        ], nonce=700))["result"]
        self.assertEqual(result["agent_id"], agent["id"])
        self.assertEqual(
            board.snapshot(self.context)["agents"][agent["id"]]["status_note"],
            "canonical identity",
        )

    def test_register_ignores_claimed_role_task_vendor_name_and_session(self):
        session = control.create(self.context, "claude_reviewer")
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())
        token = authority.claim(session["id"], os.getpid())
        gateway = CommandGateway(self.context, authority)
        output = gateway.execute(token, self.request("register", [
            "--role", "engineering", "--task", "stolen-task", "--name", "Delivery",
            "--vendor", "OpenAI", "--session-id", "stolen-session",
        ], nonce=1))["result"]
        self.assertEqual(output["role"], "qa")
        self.assertEqual(output["task"], "REVIEW_QUEUE")
        self.assertEqual(output["display_name"], "Independent Reviewer")
        self.assertEqual(output["vendor"], "Anthropic")
        self.assertEqual(output["session_id"], session["id"])

    def test_nonce_is_durable_and_duplicate_or_out_of_order_is_refused_after_restart(self):
        _, agent, authority, token, gateway = self.session()
        first = gateway.execute(token, self.request("poll", ["--agent", agent["id"]], nonce=100))
        self.assertEqual(first["result"]["poll_counter"], 1)
        for candidate in (100, 99):
            restarted = CommandGateway(self.context, SessionTokenAuthority(self.context))
            with self.subTest(candidate=candidate), self.assertRaises(SurfaceReplayError):
                restarted.execute(token, self.request("poll", ["--agent", agent["id"]], nonce=candidate))
        restarted = CommandGateway(self.context, SessionTokenAuthority(self.context))
        second = restarted.execute(token, self.request("poll", ["--agent", agent["id"]], nonce=101))
        self.assertEqual(second["result"]["poll_counter"], 2)

    def test_http_surface_maps_auth_protocol_replay_and_size_failures_without_secrets(self):
        _, agent, authority, token, _ = self.session()
        valid = self.request("poll", ["--agent", agent["id"]], nonce=500)

        def post(endpoint, payload, bearer=token, content_length=None):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + bearer}
            if content_length is not None:
                headers["Content-Length"] = str(content_length)
            return urlopen(Request(
                endpoint + "/api/board/command", data=body, method="POST",
                headers=headers,
            ), timeout=5)

        with self.served(authority) as endpoint:
            with self.assertRaises(HTTPError) as unauthenticated:
                post(endpoint, valid, "forged-token-value-that-is-long-enough")
            self.assertEqual(unauthenticated.exception.code, 401)
            self.assertNotIn(token, unauthenticated.exception.read().decode())

            with self.assertRaises(HTTPError) as malformed:
                post(endpoint, {"protocol": PROTOCOL_VERSION})
            self.assertEqual(malformed.exception.code, 400)

            with self.assertRaises(HTTPError) as oversized:
                post(endpoint, b"{}", content_length=project_worker.COMMAND_LIMIT + 1)
            self.assertEqual(oversized.exception.code, 400)

            with post(endpoint, valid) as response:
                self.assertEqual(json.loads(response.read())["result"]["poll_counter"], 1)
            with self.assertRaises(HTTPError) as replayed:
                post(endpoint, valid)
            self.assertEqual(replayed.exception.code, 409)
            self.assertNotIn(token, replayed.exception.read().decode())

    def test_project_mutations_are_single_flight_under_concurrency(self):
        _, agent, _, token, gateway = self.session()
        entered = threading.Event()
        release = threading.Event()
        active = 0
        peak = 0
        active_lock = threading.Lock()

        def fake_main(_arguments):
            nonlocal active, peak
            with active_lock:
                active += 1
                peak = max(peak, active)
            entered.set()
            release.wait(3)
            print(json.dumps({"ok": True}))
            with active_lock:
                active -= 1
            return 0

        results = []
        with mock.patch.object(board, "main", side_effect=fake_main):
            # status now rides the short-command fast lane by design; the
            # single-flight guarantee under test belongs to real serialized
            # mutations such as task-brief.
            first = threading.Thread(target=lambda: results.append(gateway.execute(
                token, self.request("task-brief", [
                    "--agent", agent["id"], "--update", "first serialized mutation",
                ], nonce=200))))
            second = threading.Thread(target=lambda: results.append(gateway.execute(
                token, self.request("task-brief", [
                    "--agent", agent["id"], "--update", "second serialized mutation",
                ], nonce=201))))
            first.start(); self.assertTrue(entered.wait(2)); second.start()
            time.sleep(0.1)
            self.assertEqual(peak, 1)
            release.set(); first.join(3); second.join(3)
        self.assertEqual(len(results), 2)
        self.assertEqual(peak, 1)

    def test_path_ingestion_operations_require_authenticated_bytes(self):
        _, agent, _, token, gateway = self.session()
        reviewer = self.session("claude_reviewer", "qa", "REVIEW_QUEUE")
        identities = {
            "request-qa": (agent, token, gateway, "--ledger", "ledger"),
            "request-review": (agent, token, gateway, "--ledger", "ledger"),
            "attach-challenge-ledger": (
                reviewer[1], reviewer[3], reviewer[4], "--challenge-ledger", "challenge_ledger",
            ),
            "qa-result": (reviewer[1], reviewer[3], reviewer[4], "--evidence", "evidence"),
        }
        required_operations = {
            operation for operation, (_option, _field, required) in UPLOAD_ARGUMENTS.items()
            if required
        }
        for index, operation in enumerate(sorted(required_operations), start=300):
            operation_agent, operation_token, operation_gateway, option, field = identities[operation]
            with self.subTest(operation=operation), self.assertRaisesRegex(
                SurfaceAuthorizationError, "agent paths"
            ):
                operation_gateway.execute(operation_token, self.request(
                    operation, [
                        "--agent", operation_agent["id"], option, "/tmp/agent-artifact",
                    ], nonce=index,
                ))
            with self.subTest(operation=operation, missing_bytes=True), self.assertRaisesRegex(
                SurfaceProtocolError, "artifact field"
            ):
                operation_gateway.execute(operation_token, self.request(
                    operation, [
                        "--agent", operation_agent["id"], option, f"upload:{field}",
                    ], nonce=index + 100,
                ))
        with self.assertRaisesRegex(SurfaceAuthorizationError, "agent paths"):
            reviewer[4].execute(reviewer[3], self.request("claim-qa", [
                "--agent", reviewer[1]["id"], "--challenge-ledger", "/tmp/agent-ledger.md",
            ], nonce=400))
        with self.assertRaisesRegex(SurfaceProtocolError, "artifact field"):
            reviewer[4].execute(reviewer[3], self.request("claim-qa", [
                "--agent", reviewer[1]["id"], "--challenge-ledger=upload:challenge_ledger",
            ], nonce=401))

    def test_thin_client_worker_loss_and_incomplete_environment_never_fall_back(self):
        _, agent, _, token, _ = self.session()
        script = Path(__file__).resolve().parents[1] / "harness" / "board.py"
        environment = dict(os.environ)
        environment.update({
            TOKEN_ENV: token, ENDPOINT_ENV: "http://127.0.0.1:1", PROTOCOL_ENV: PROTOCOL_VERSION,
        })
        before = board.snapshot(self.context)["agents"][agent["id"]]["status_note"]
        result = subprocess.run([
            os.path.realpath(os.sys.executable), str(script), "--root", str(self.context.code_root),
            "--data-root", str(self.context.data_root), "--workspace-root", str(self.context.workspace_root),
            "status", "--agent", agent["id"], "--note", "must-not-run",
        ], env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("authenticated board worker is unavailable", result.stderr)
        self.assertNotIn(str(self.context.data_root), result.stderr + result.stdout)
        self.assertEqual(board.snapshot(self.context)["agents"][agent["id"]]["status_note"], before)

        environment.pop(PROTOCOL_ENV)
        partial = subprocess.run([
            os.path.realpath(os.sys.executable), str(script), "poll", "--agent", agent["id"],
        ], env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(partial.returncode, 2)
        self.assertIn("environment is incomplete", partial.stderr)

        environment[PROTOCOL_ENV] = PROTOCOL_VERSION
        environment[ENDPOINT_ENV] = "https://example.invalid"
        external = subprocess.run([
            os.path.realpath(os.sys.executable), str(script), "poll", "--agent", agent["id"],
        ], env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(external.returncode, 2)
        self.assertIn("worker response is invalid or incompatible", external.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

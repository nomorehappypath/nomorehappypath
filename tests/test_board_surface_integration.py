# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Byte-ingestion adversarial tests for the authenticated board surface."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from harness import board, board_client, control, lifecycle_metrics, project_worker
from harness.board_surface import (
    ENDPOINT_ENV,
    MAX_ARTIFACT_BYTES,
    PROTOCOL_ENV,
    PROTOCOL_VERSION,
    TOKEN_ENV,
    CommandGateway,
    SessionTokenAuthority,
    SurfaceAuthorizationError,
    SurfaceProtocolError,
)
from harness.project_context import ProjectContext
from tests.environment_support import require_loopback


class BoardArtifactIngestionTests(unittest.TestCase):
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

    @staticmethod
    def artifact(payload: bytes, field="ledger"):
        return {field: {
            "content": base64.b64encode(payload).decode("ascii"),
            "length": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "media_type": "text/markdown" if "ledger" in field else "text/plain",
        }}

    @staticmethod
    def request(agent_id, nonce, artifacts, ledger="upload:ledger"):
        return {
            "protocol": PROTOCOL_VERSION,
            "nonce": nonce,
            "operation": "request-review",
            "arguments": [
                "request-review", "--agent", agent_id, "--ledger", ledger,
                "--summary", "uploaded", "--unit-test-command", "python3 -m unittest -v",
            ],
            "artifacts": artifacts,
        }

    def test_worker_stores_only_verified_bytes_with_session_manifest_and_logical_response(self):
        session, agent, _, token, gateway = self.session()
        payload = b"# ledger\n\nUTF-8 bytes stay exact.\n"
        captured = {}

        def fake_main(arguments):
            path = Path(arguments[arguments.index("--ledger") + 1])
            captured["path"] = path
            captured["payload"] = path.read_bytes()
            print(json.dumps({"accepted": True, "ledger": str(path)}))
            return 0

        with mock.patch.object(board, "main", side_effect=fake_main):
            output = gateway.execute(token, self.request(agent["id"], 10, self.artifact(payload)))
        stored = captured["path"]
        self.assertEqual(captured["payload"], payload)
        self.assertTrue(stored.is_relative_to(self.context.data_root / "evidence" / "uploads"))
        self.assertNotIn(session["id"], str(stored.parent))
        self.assertNotIn(str(self.context.data_root), json.dumps(output["result"]))
        artifact = output["artifacts"]["ledger"]
        self.assertEqual(artifact["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(artifact["byte_count"], len(payload))
        self.assertNotIn("path", artifact)
        manifest = json.loads(stored.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["session_id"], session["id"])
        self.assertEqual(manifest["agent_id"], agent["id"])
        self.assertEqual(manifest["sha256"], artifact["sha256"])
        self.assertEqual(manifest["byte_count"], len(payload))

    def test_authenticated_begin_task_accepts_explicit_project_context(self):
        session, agent, _, token, gateway = self.session()
        board.record_owner_direction(
            self.context,
            session["id"],
            "Implement the approved change in this project.",
        )
        output = gateway.execute(token, {
            "protocol": PROTOCOL_VERSION,
            "nonce": 11,
            "operation": "begin-task",
            "arguments": [
                "begin-task", "--agent", agent["id"], "--task", "CONTEXT-TASK",
            ],
            "artifacts": {},
        })
        self.assertEqual(output["result"]["task"], "CONTEXT-TASK")
        self.assertEqual(
            board.snapshot(self.context)["agents"][agent["id"]]["task"],
            "CONTEXT-TASK",
        )

    def test_raw_paths_traversal_missing_bytes_and_cross_session_reuse_are_refused(self):
        _, agent, _, token, gateway = self.session()
        payload = b"# ledger\n"
        for nonce, path in enumerate(("/tmp/absolute.md", "../traversal.md", "other/session.md"), start=20):
            with self.subTest(path=path), self.assertRaisesRegex(
                SurfaceAuthorizationError, "agent paths"
            ):
                gateway.execute(token, self.request(agent["id"], nonce, self.artifact(payload), path))
        with self.assertRaisesRegex(SurfaceProtocolError, "artifact field"):
            gateway.execute(token, self.request(agent["id"], 30, {}, "upload:ledger"))
        with self.assertRaisesRegex(SurfaceProtocolError, "artifact field"):
            gateway.execute(token, self.request(
                agent["id"], 31, {"evidence": self.artifact(payload, "evidence")["evidence"]},
            ))
        upload_root = self.context.data_root / "evidence" / "uploads"
        self.assertFalse(upload_root.exists())

    def test_hash_length_encoding_media_and_size_mismatch_leave_no_artifact(self):
        _, agent, _, token, gateway = self.session()
        base = self.artifact(b"valid utf8")["ledger"]
        variants = []
        for key, value in (
            ("sha256", "0" * 64),
            ("length", len(b"valid utf8") + 1),
            ("content", "%%%not-base64%%%"),
            ("media_type", "text/plain"),
            ("length", MAX_ARTIFACT_BYTES + 1),
        ):
            variant = dict(base); variant[key] = value; variants.append(variant)
        invalid_utf8 = dict(base)
        invalid_utf8.update({
            "content": base64.b64encode(b"\xff").decode("ascii"),
            "length": 1,
            "sha256": hashlib.sha256(b"\xff").hexdigest(),
        })
        variants.append(invalid_utf8)
        for nonce, metadata in enumerate(variants, start=40):
            with self.subTest(nonce=nonce), self.assertRaises(SurfaceProtocolError):
                gateway.execute(token, self.request(agent["id"], nonce, {"ledger": metadata}))
        self.assertFalse((self.context.data_root / "evidence" / "uploads").exists())

    def test_poll_bypasses_long_command_but_mutations_remain_serialized(self):
        _, delivery, _, delivery_token, delivery_gateway = self.session(
            "codex_delivery", "engineering", board.AWAITING_OWNER_DIRECTION,
        )
        _, reviewer, _, reviewer_token, reviewer_gateway = self.session(
            "claude_reviewer", "qa", "REVIEW_QUEUE",
        )
        _, second_delivery, _, second_token, second_gateway = self.session(
            "codex_delivery", "engineering", board.AWAITING_OWNER_DIRECTION,
        )
        requested_at = board.now()
        with board.locked_state(self.context) as state:
            state["agents"][delivery["id"]].update({
                "task": "STAGED", "vendor": "OpenAI",
            })
            state["agents"][reviewer["id"]]["vendor"] = "Anthropic"
            state["requirement_confirmations"]["STAGED"] = {
                "text": "The Reviewer can author independently while Delivery evidence executes.",
                "confirmed_at": requested_at,
            }
            state["qa_requests"]["review-staged"] = {
                "id": "review-staged", "task": "STAGED", "cycle": 1,
                "status": "authoring", "stage": board.INDEPENDENT_REVIEW,
                "phase": "final_acceptance", "developer_id": delivery["id"],
                "claimed_by": None, "reserved_by": None,
                "requested_at": requested_at, "review_wait_started_at": requested_at,
                "delivery_state": "executing", "test_scope": "full",
                "lifecycle": {"review_queue": {"started_at": requested_at}},
            }
        payload = b"# ledger\n"
        artifact = {"ledger": {
            "content": base64.b64encode(payload).decode("ascii"),
            "length": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "media_type": "text/markdown",
        }}
        started = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()

        def fake_main(arguments):
            if "request-review" in arguments:
                started.set()
                self.assertTrue(release.wait(timeout=5))
                operation = "request-review"
            else:
                second_entered.set()
                operation = "begin-task"
            print(json.dumps({"operation": operation}))
            return 0

        long_result = []
        second_result = []
        with mock.patch.object(board, "main", side_effect=fake_main):
            long_thread = threading.Thread(target=lambda: long_result.append(
                delivery_gateway.execute(delivery_token, {
                    "protocol": PROTOCOL_VERSION, "nonce": 101,
                    "operation": "request-review",
                    "arguments": [
                        "request-review", "--agent", delivery["id"],
                        "--ledger", "upload:ledger", "--summary", "long",
                        "--unit-test-command", "python3 -m unittest",
                    ],
                    "artifacts": artifact,
                })
            ))
            long_thread.start()
            self.assertTrue(started.wait(timeout=2))

            # Poll is a board-owned heartbeat and must not wait behind the suite.
            polled = reviewer_gateway.execute(reviewer_token, {
                "protocol": PROTOCOL_VERSION, "nonce": 201,
                "operation": "poll",
                "arguments": ["poll", "--agent", reviewer["id"]],
                "artifacts": {},
            })
            self.assertEqual(polled["result"]["agent_id"], reviewer["id"])

            # The real staged-authoring calls must also bypass the long suite.
            reserved = reviewer_gateway.execute(reviewer_token, {
                "protocol": PROTOCOL_VERSION, "nonce": 202,
                "operation": "reserve-qa",
                "arguments": [
                    "reserve-qa", "--agent", reviewer["id"],
                    "--request", "review-staged",
                ],
                "artifacts": {},
            })
            self.assertEqual(reserved["result"]["status"], "reserved")
            intents = reviewer_gateway.execute(reviewer_token, {
                "protocol": PROTOCOL_VERSION, "nonce": 203,
                "operation": "review-intents",
                "arguments": [
                    "review-intents", "--agent", reviewer["id"],
                    "--request", "review-staged", "--intent",
                    "Independently force the overlap boundary and require both commands to retain their exact task scope.",
                ],
                "artifacts": {},
            })
            self.assertEqual(len(intents["result"]["reviewer_initial_intents"]), 1)

            second_thread = threading.Thread(target=lambda: second_result.append(
                second_gateway.execute(second_token, {
                    "protocol": PROTOCOL_VERSION, "nonce": 301,
                    "operation": "begin-task",
                    "arguments": [
                        "begin-task", "--agent", second_delivery["id"],
                        "--task", "SERIALIZED",
                    ],
                    "artifacts": {},
                })
            ))
            second_thread.start()
            self.assertFalse(second_entered.wait(timeout=0.15))
            release.set()
            long_thread.join(timeout=3)
            second_thread.join(timeout=3)

        self.assertFalse(long_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(long_result[0]["result"]["operation"], "request-review")
        self.assertEqual(second_result[0]["result"]["operation"], "begin-task")

    def test_poll_transport_retries_are_bounded_and_not_called_incompatible(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return json.dumps({"result": {"ok": True}}).encode()

        environment = {
            TOKEN_ENV: "token", ENDPOINT_ENV: "http://127.0.0.1:9876",
            PROTOCOL_ENV: PROTOCOL_VERSION,
        }
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=False), \
                mock.patch.object(board_client, "_nonce", side_effect=[1, 2]), \
                mock.patch.object(board_client, "urlopen", side_effect=[URLError("busy"), Response()]) as opened, \
                mock.patch.object(board_client.time, "sleep") as slept, \
                redirect_stdout(output), redirect_stderr(errors):
            result = board_client.invoke(["poll", "--agent", "reviewer"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), {"ok": True})
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(opened.call_count, 2)
        slept.assert_called_once_with(board_client.POLL_RETRY_DELAYS[0])

    def test_concurrent_surface_rejects_parser_bypasses_without_board_mutation(self):
        _, reviewer, _, token, gateway = self.session(
            "claude_reviewer", "qa", "REVIEW_QUEUE",
        )
        before = board.snapshot(self.context)["agents"][reviewer["id"]]["poll_counter"]
        invalid = (
            (401, "poll", ["poll", "--agent", reviewer["id"], "--request", "cross-task"]),
            (402, "review-brief", ["review-brief", "--agent", reviewer["id"]]),
            (403, "review-intents", [
                "review-intents", "--agent", reviewer["id"], "--request", "request",
            ]),
            (404, "reserve-qa", [
                "reserve-qa", "--agent", reviewer["id"], "--unexpected", "value",
            ]),
        )
        for nonce, operation, arguments in invalid:
            with self.subTest(operation=operation, nonce=nonce), self.assertRaises(
                SurfaceProtocolError,
            ):
                gateway.execute(token, {
                    "protocol": PROTOCOL_VERSION, "nonce": nonce,
                    "operation": operation, "arguments": arguments,
                    "artifacts": {},
                })
        self.assertEqual(
            board.snapshot(self.context)["agents"][reviewer["id"]]["poll_counter"],
            before,
        )

    def test_every_artifact_command_replaces_the_marker_with_worker_owned_bytes(self):
        delivery = self.session()
        reviewer = self.session("claude_reviewer", "qa", "REVIEW_QUEUE")
        cases = (
            (delivery, "request-qa", "--ledger", "ledger", 100),
            (delivery, "request-review", "--ledger", "ledger", 101),
            (reviewer, "attach-challenge-ledger", "--challenge-ledger", "challenge_ledger", 100),
            (reviewer, "claim-qa", "--challenge-ledger", "challenge_ledger", 101),
            (reviewer, "qa-result", "--evidence", "evidence", 102),
        )
        captured = []

        def fake_main(arguments):
            operation = next(case[1] for case in cases if case[1] in arguments)
            option = next(option for option in ("--ledger", "--challenge-ledger", "--evidence") if option in arguments)
            path = Path(arguments[arguments.index(option) + 1])
            captured.append((operation, option, path, path.read_bytes()))
            print(json.dumps({"accepted": True}))
            return 0

        with mock.patch.object(board, "main", side_effect=fake_main):
            for session, operation, option, field, nonce in cases:
                payload = f"{operation} verified bytes\n".encode("utf-8")
                request = {
                    "protocol": PROTOCOL_VERSION,
                    "nonce": nonce,
                    "operation": operation,
                    "arguments": [operation, "--agent", session[1]["id"], option, f"upload:{field}"],
                    "artifacts": self.artifact(payload, field),
                }
                result = session[4].execute(session[3], request)
                self.assertEqual(result["artifacts"][field]["byte_count"], len(payload))

        self.assertEqual([item[0] for item in captured], [case[1] for case in cases])
        for operation, _option, path, payload in captured:
            self.assertTrue(path.is_relative_to(self.context.data_root / "evidence" / "uploads"))
            self.assertEqual(payload, f"{operation} verified bytes\n".encode("utf-8"))

    def test_exact_size_limit_is_accepted(self):
        _, agent, _, token, gateway = self.session()
        payload = b"x" * MAX_ARTIFACT_BYTES
        captured = {}

        def fake_main(arguments):
            path = Path(arguments[arguments.index("--ledger") + 1])
            captured["size"] = path.stat().st_size
            print(json.dumps({"accepted": True}))
            return 0

        with mock.patch.object(board, "main", side_effect=fake_main):
            result = gateway.execute(token, self.request(agent["id"], 110, self.artifact(payload)))
        self.assertEqual(captured["size"], MAX_ARTIFACT_BYTES)
        self.assertEqual(result["artifacts"]["ledger"]["byte_count"], MAX_ARTIFACT_BYTES)

    def test_governance_failure_removes_payload_manifest_and_temporary_files(self):
        _, agent, _, token, gateway = self.session()
        captured = {}

        def failed_main(arguments):
            captured["path"] = Path(arguments[arguments.index("--ledger") + 1])
            print("error: governance refused the review", file=sys.stderr)
            return 2

        with mock.patch.object(board, "main", side_effect=failed_main), self.assertRaisesRegex(
            SurfaceProtocolError, "governance refused"
        ):
            gateway.execute(token, self.request(agent["id"], 60, self.artifact(b"# valid\n")))
        self.assertFalse(captured["path"].exists())
        self.assertFalse(captured["path"].with_suffix(".json").exists())
        files = list((self.context.data_root / "evidence" / "uploads").rglob("*"))
        self.assertFalse([path for path in files if path.is_file()])

    def test_destination_collisions_preserve_every_preexisting_byte(self):
        session, agent, _, token, gateway = self.session()

        def destinations(nonce, payload):
            digest = hashlib.sha256(payload).hexdigest()
            session_label = hashlib.sha256(session["id"].encode("utf-8")).hexdigest()[:20]
            directory = self.context.data_root / "evidence" / "uploads" / session_label
            stem = f"{nonce}-ledger-{digest}"
            return directory / f"{stem}.md", directory / f"{stem}.json"

        first_payload = b"# attempted replacement\n"
        first_path, first_manifest = destinations(120, first_payload)
        first_path.parent.mkdir(parents=True)
        first_path.write_bytes(b"prior payload bytes")
        first_manifest.write_bytes(b"prior manifest bytes")
        with mock.patch.object(board, "main") as board_main, self.assertRaisesRegex(
            SurfaceProtocolError, "destination already exists"
        ):
            gateway.execute(token, self.request(agent["id"], 120, self.artifact(first_payload)))
        board_main.assert_not_called()
        self.assertEqual(first_path.read_bytes(), b"prior payload bytes")
        self.assertEqual(first_manifest.read_bytes(), b"prior manifest bytes")

        second_payload = b"# manifest collision\n"
        second_path, second_manifest = destinations(121, second_payload)
        second_manifest.write_bytes(b"prior manifest only")
        with mock.patch.object(board, "main") as board_main, self.assertRaisesRegex(
            SurfaceProtocolError, "destination already exists"
        ):
            gateway.execute(token, self.request(agent["id"], 121, self.artifact(second_payload)))
        board_main.assert_not_called()
        self.assertFalse(second_path.exists())
        self.assertEqual(second_manifest.read_bytes(), b"prior manifest only")

    def test_same_bytes_from_two_sessions_have_distinct_owned_destinations(self):
        first = self.session()
        second = self.session()
        paths = []

        def fake_main(arguments):
            path = Path(arguments[arguments.index("--ledger") + 1])
            paths.append(path)
            print(json.dumps({"ok": True}))
            return 0

        payload = self.artifact(b"# shared bytes\n")
        with mock.patch.object(board, "main", side_effect=fake_main):
            first[4].execute(first[3], self.request(first[1]["id"], 70, payload))
            second[4].execute(second[3], self.request(second[1]["id"], 70, payload))
        self.assertEqual(len(paths), 2)
        self.assertNotEqual(paths[0], paths[1])
        self.assertNotEqual(paths[0].parent, paths[1].parent)
        self.assertEqual(paths[0].read_bytes(), paths[1].read_bytes())

    def test_client_reads_regular_single_link_file_and_rejects_special_or_raced_inputs(self):
        source = self.context.code_root / "ledger.md"
        source.write_text("# local ledger\n", encoding="utf-8")
        arguments, artifacts = board_client._prepare_artifacts(
            "request-review", ["request-review", "--ledger", str(source)],
        )
        self.assertEqual(arguments[-2:], ["--ledger", "upload:ledger"])
        self.assertNotIn(str(source), arguments)
        self.assertEqual(
            base64.b64decode(artifacts["ledger"]["content"]), source.read_bytes(),
        )

        symlink = self.context.code_root / "symlink.md"; symlink.symlink_to(source)
        hardlink = self.context.code_root / "hardlink.md"; os.link(source, hardlink)
        fifo = self.context.code_root / "fifo"; os.mkfifo(fifo)
        invalid = self.context.code_root / "invalid.txt"; invalid.write_bytes(b"\xff")
        oversized = self.context.code_root / "oversized.txt"; oversized.write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
        for path in (source, symlink, hardlink, fifo, invalid, oversized):
            if path == source:
                # Creating the hardlink makes both names multi-link and therefore unsafe.
                expected = "single-link"
            else:
                expected = "artifact"
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, expected):
                board_client._read_artifact(str(path), "ledger")

        raced = self.context.code_root / "raced.md"; raced.write_text("before", encoding="utf-8")
        original_open = os.open

        def swapped_open(path, flags, *args):
            raced.unlink(); raced.write_text("after", encoding="utf-8")
            return original_open(path, flags, *args)

        with mock.patch.object(board_client.os, "open", side_effect=swapped_open), self.assertRaisesRegex(
            ValueError, "changed while it was being opened"
        ):
            board_client._read_artifact(str(raced), "ledger")

        modified = self.context.code_root / "modified.md"
        modified.write_text("before", encoding="utf-8")
        original_read = os.read
        changed = False

        def modified_read(descriptor, count):
            nonlocal changed
            if not changed:
                changed = True
                modified.write_text("AFTER!", encoding="utf-8")
            return original_read(descriptor, count)

        with mock.patch.object(board_client.os, "read", side_effect=modified_read), self.assertRaisesRegex(
            ValueError, "changed while it was being read"
        ):
            board_client._read_artifact(str(modified), "ledger")

    def test_real_thin_client_uploads_bytes_over_http_without_sending_its_path(self):
        _, agent, authority, token, gateway = self.session()
        source = self.context.code_root / "delivery-ledger.md"
        source.write_text("# uploaded through client\n", encoding="utf-8")
        endpoint_box = {"value": ""}
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_worker.make_handler(
            self.context, authority=authority, gateway=gateway,
            endpoint=lambda: endpoint_box["value"],
        ))
        endpoint_box["value"] = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        captured = {}

        def fake_main(arguments):
            path = Path(arguments[arguments.index("--ledger") + 1])
            captured["path"] = path
            captured["payload"] = path.read_bytes()
            print(json.dumps({"accepted": True}))
            return 0

        environment = {
            TOKEN_ENV: token, ENDPOINT_ENV: endpoint_box["value"], PROTOCOL_ENV: PROTOCOL_VERSION,
        }
        try:
            output = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                board, "main", side_effect=fake_main,
            ), redirect_stdout(output):
                result = board_client.invoke([
                    "request-review", "--agent", agent["id"], "--ledger", str(source),
                    "--summary", "real client", "--unit-test-command", "python3 -m unittest -v",
                ])
        finally:
            server.shutdown(); thread.join(timeout=3); server.server_close()
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), {"accepted": True})
        self.assertEqual(captured["payload"], source.read_bytes())
        self.assertNotEqual(captured["path"], source)
        self.assertTrue(captured["path"].is_relative_to(self.context.data_root))

    def test_partial_http_upload_is_refused_before_gateway_or_storage(self):
        _, agent, authority, token, gateway = self.session()
        endpoint_box = {"value": ""}
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_worker.make_handler(
            self.context, authority=authority, gateway=gateway,
            endpoint=lambda: endpoint_box["value"],
        ))
        endpoint_box["value"] = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        before = board.snapshot(self.context)["agents"][agent["id"]]["poll_counter"]
        client = socket.create_connection(server.server_address, timeout=5)
        try:
            fragment = b'{"protocol":"1"'
            headers = (
                b"POST /api/board/command HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                + f"Authorization: Bearer {token}\r\n".encode("ascii")
                + b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(fragment) + 100}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
            )
            client.sendall(headers + fragment)
            client.shutdown(socket.SHUT_WR)
            response = b""
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
        finally:
            client.close()
            server.shutdown(); thread.join(timeout=3); server.server_close()
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])
        self.assertEqual(board.snapshot(self.context)["agents"][agent["id"]]["poll_counter"], before)
        self.assertFalse((self.context.data_root / "evidence" / "uploads").exists())


class FieldTrialMetricsTests(unittest.TestCase):
    def test_field_trial_projection_separates_measured_values_from_missing_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_evidence = root / "first-evidence.txt"
            second_evidence = root / "second-evidence.txt"
            first_evidence.write_text("command: python3 -m unittest shared\n", encoding="utf-8")
            second_evidence.write_text("command: python3 -m unittest shared\n", encoding="utf-8")
            with board.locked_state(root) as state:
                def event(kind, seconds, task="FIELD", agent=None):
                    board._event(state, kind, agent, {
                        "task": task,
                        "at": f"2026-08-17T12:00:{seconds:02d}+00:00",
                    })

                event("task_begun", 0)
                event("requirements_confirmed", 5)
                event("review_execution_started", 15)
                state["events"][-1]["request_id"] = "r1"
                event("review_execution_finished", 18)
                state["events"][-1]["request_id"] = "r1"
                event("review_execution_started", 21)
                state["events"][-1]["request_id"] = "r1"
                event("review_execution_finished", 24)
                state["events"][-1]["request_id"] = "r1"
                event("qa_reserved", 12, agent={"id": "qa-one", "role": "qa"})
                event("qa_reserved", 42, agent={"id": "qa-two", "role": "qa"})
                event("qa_result", 50)
                common = {
                    "task": "FIELD", "phase": "chunk", "stage": "independent_review",
                    "subtask": "secure", "chunk": "transport", "developer_id": "dev",
                    "claimed_by": "qa", "structure_revision": 1,
                }
                state["qa_requests"].update({
                    "r1": {
                        **common, "id": "r1", "cycle": 1, "status": "failed",
                        "requested_at": "2026-08-17T12:00:10+00:00",
                        "review_wait_started_at": "2026-08-17T12:00:10+00:00",
                        "reserved_at": "2026-08-17T12:00:12+00:00",
                        "challenge_ledger_attached_at": "2026-08-17T12:00:14+00:00",
                        "completed_at": "2026-08-17T12:00:30+00:00",
                        "lifecycle": {"implementation": {
                            "started_at": "2026-08-17T12:00:05+00:00",
                            "finished_at": "2026-08-17T12:00:10+00:00",
                            "duration_seconds": 5.0,
                        }},
                        "evidence": str(first_evidence),
                        "command_executions": [{
                            "command": "python3 -m unittest", "duration_seconds": 3.0,
                            "cache_decision": "executed",
                            "finished_at": "2026-08-17T12:00:18+00:00",
                        }],
                    },
                    "r2": {
                        **common, "id": "r2", "cycle": 2, "status": "passed",
                        "requested_at": "2026-08-17T12:00:40+00:00",
                        "review_wait_started_at": "2026-08-17T12:00:40+00:00",
                        "reserved_at": "2026-08-17T12:00:42+00:00",
                        "challenge_ledger_attached_at": "2026-08-17T12:00:44+00:00",
                        "completed_at": "2026-08-17T12:00:50+00:00",
                        "evidence": str(second_evidence),
                        "command_executions": [],
                    },
                })

            metrics = lifecycle_metrics.field_trial_metrics(root, "FIELD")
            pinned = lifecycle_metrics.field_trial_metrics(
                root, "FIELD", measurement_cutoff_at="2026-08-17T12:00:35+00:00",
            )

        self.assertEqual(metrics["wall_clock_to_cutoff_seconds"], 50.0)
        self.assertEqual(metrics["definition_seconds"], 5.0)
        self.assertEqual(metrics["phase_totals_seconds"]["queue_wait"], 4.0)
        self.assertEqual(metrics["phase_totals_seconds"]["challenge_authoring"], 4.0)
        self.assertEqual(metrics["phase_totals_seconds"]["challenge_execution"], 6.0)
        self.assertEqual(metrics["phase_totals_seconds"]["verdict"], 6.0)
        self.assertEqual(metrics["recorded_command_count"], 1)
        self.assertEqual(metrics["recorded_command_seconds"], 3.0)
        self.assertEqual(metrics["repair_turnarounds"][0]["duration_seconds"], 10.0)
        self.assertEqual(metrics["reviews"][0]["challenge_execution_interval_count"], 2)
        self.assertIsNone(metrics["context_rotations"]["count"])
        self.assertEqual(metrics["context_rotations"]["status"], "instrumentation_gap")
        self.assertEqual(metrics["context_rotations"]["observed_reviewer_agents"], ["qa-one", "qa-two"])
        self.assertEqual(metrics["context_rotations"]["observed_reviewer_transition_count"], 1)
        self.assertEqual(metrics["missing_records"]["commands"], ["r2"])
        self.assertEqual(metrics["missing_records"]["challenge_execution"], ["r2"])
        self.assertIsNone(metrics["reviews"][1]["recorded_command_seconds"])
        self.assertIsNone(metrics["release_tail_seconds"])
        self.assertTrue(metrics["unavailable"]["release_tail_seconds"])
        self.assertEqual(metrics["duplicates"]["duplicate_executions_upper_bound"], 1)
        self.assertEqual(pinned["measurement_cutoff_at"], "2026-08-17T12:00:35+00:00")
        self.assertEqual(pinned["wall_clock_to_cutoff_seconds"], 35.0)
        self.assertEqual(pinned["review_count"], 1)
        self.assertEqual(pinned["duplicates"]["duplicate_executions_upper_bound"], 0)
        self.assertEqual(pinned["context_rotations"]["count"], 0)
        self.assertEqual(pinned["context_rotations"]["observed_reviewer_agents"], ["qa-one"])
        with self.assertRaisesRegex(ValueError, "ISO timestamp"):
            lifecycle_metrics.field_trial_metrics(root, "FIELD", measurement_cutoff_at="invalid")


if __name__ == "__main__":
    unittest.main(verbosity=2)

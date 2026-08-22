# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""HTTP and real-browser acceptance for the project-specific chat surface."""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import (
    board, browser_acceptance, control, global_settings, project_chat, project_manager, project_memory,
    project_registry as registry, project_worker,
)
from harness.board_surface import SessionTokenAuthority
from harness.project_context import ProjectContext
from tests.chat_key_support import configure_verified_key
from tests.environment_support import require_loopback


def response(answer="Current project fact."):
    return {
        "answer": answer,
        "source_ids": ["board:opaque"],
        "snapshot": {"at": "2026-08-17T00:00:00+00:00", "board_sequence": 7, "digest": "a" * 64},
        "unknown": answer == project_chat.UNKNOWN_ANSWER,
    }


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def action_token(origin):
    page = urlopen(origin + "/", timeout=5).read().decode()
    match = re.search(r'id="project-chat" data-action-token="([^"]+)"', page)
    if not match:
        raise AssertionError("managed project page did not expose its ephemeral chat action token")
    return match.group(1), page


def post_managed_chat(origin, token, request_id, question):
    request = Request(
        origin + "/api/project-chat",
        data=json.dumps({"request_id": request_id, "question": question}).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json", "Origin": origin,
            "Sec-Fetch-Site": "same-origin", "X-Harness-Chat-Action": token,
        },
    )
    try:
        with urlopen(request, timeout=30) as opened:
            return opened.status, json.loads(opened.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


class FakeOpenAI:
    def __init__(self, *, delay=0.0):
        self.delay = delay
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                payload = json.loads(raw)
                owner.requests.append(payload)
                if owner.delay:
                    time.sleep(owner.delay)
                prompt = json.loads(payload["input"].rsplit("\n", 1)[1])
                package = prompt["fact_package"]
                question = str(prompt.get("question", "")).casefold()
                off_topic = any(marker in question for marker in ("france", "capital", "weather"))
                preferred = [
                    fact_id for fact_id in ("project_about", "current_status", "task_list")
                    if fact_id in package["facts"]
                ]
                answer = json.dumps({
                    "in_scope": not off_topic,
                    "action_oriented": False,
                    "claims": [] if off_topic else preferred[:1],
                })
                body = json.dumps({
                    "status": "completed",
                    "output": [{"type": "message", "content": [{
                        "type": "output_text", "text": answer,
                    }]}],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1/responses"

    def environment(self):
        return {
            "OPENAI_API_KEY": "sk-test-managed-chat-1234567890",
            "HARNESS_OPENAI_TESTING": "1",
            "HARNESS_OPENAI_TEST_ENDPOINT": self.endpoint,
        }

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()


def managed_project(base, name, description):
    home = base / "manager"
    global_settings.initialize(home)
    configure_verified_key(home)
    code = base / "adopted-code"
    code.mkdir(parents=True)
    (code / "owner.txt").write_text(f"{name} owner bytes\n", encoding="utf-8")
    entry = registry.register(home, name, code, kind="adopted", description=description)
    manager = project_manager.ProjectManager(home, board_port=free_port(), worker_start_timeout=8)
    opened = manager.open_project(entry["id"])
    token, page = action_token(opened["board_url"].rstrip("/"))
    return manager, entry, opened["board_url"].rstrip("/"), token, page


class ServedChat:
    def __init__(self, context, settings_home, answerer, *, token="chat-action-token", project_id="project-one"):
        self.context = context
        self.token = token
        self.service = project_worker.ProjectChatService(
            context, settings_home, project_id, answerer=answerer,
        )
        endpoint = {"value": ""}
        handler = project_worker.make_handler(
            context, authority=SessionTokenAuthority(context),
            endpoint=lambda: endpoint["value"], project_name="Project One",
            project_description="Verified project facts.", project_id=project_id,
            settings_home=settings_home, chat_service=self.service,
            chat_action_token=token,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        endpoint["value"] = self.origin
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.service.shutdown()
        self.server.shutdown(); self.thread.join(timeout=3); self.server.server_close()

    def post(self, path, value, *, token=None, origin=None, content_type="application/json", raw=None):
        body = raw if raw is not None else json.dumps(value).encode("utf-8")
        request = Request(
            self.origin + path, data=body, method="POST",
            headers={
                "Content-Type": content_type,
                "Origin": self.origin if origin is None else origin,
                "X-Harness-Chat-Action": self.token if token is None else token,
                "Sec-Fetch-Site": "same-origin",
            },
        )
        try:
            with urlopen(request, timeout=8) as opened:
                return opened.status, json.loads(opened.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())


class ProjectChatHTTPTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "adopted-code"; code.mkdir()
        (code / "owner.txt").write_text("owner bytes\n", encoding="utf-8")
        self.context = ProjectContext(code, base / "private-data", base / "workspaces")
        control.initialize(self.context)
        board.snapshot(self.context)
        project_memory.initialize(
            self.context, project_name="Project One", description="Verified project facts.",
        )
        self.settings_home = base / "manager"
        global_settings.initialize(self.settings_home)
        configure_verified_key(self.settings_home)
        self.calls = []

        def answerer(root, question, **kwargs):
            self.calls.append((root, question, kwargs.get("project_id")))
            return response(f"Answer for {kwargs.get('project_id')}.")

        self.chat = ServedChat(self.context, self.settings_home, answerer)
        self.addCleanup(self.chat.close)

    def test_page_places_chat_only_inside_managed_project_and_separates_action_token(self):
        page = urlopen(self.chat.origin + "/", timeout=3).read().decode()
        self.assertIn('id="project-chat"', page)
        self.assertIn('id="project-chat-input"', page)
        self.assertIn('id="project-chat-cancel"', page)
        self.assertIn('data-action-token="chat-action-token"', page)
        self.assertNotIn(str(self.context.code_root), page)
        self.assertNotIn(str(self.context.data_root), page)
        dashboard = json.loads(urlopen(self.chat.origin + "/api/dashboard", timeout=3).read())
        self.assertNotIn("path", dashboard)
        self.assertNotIn(str(self.context.code_root), json.dumps(dashboard))
        self.assertNotIn(str(self.context.data_root), json.dumps(dashboard))
        from harness import board_viewer
        self.assertNotIn('id="project-chat"', board_viewer.rendered_page())

    def test_success_is_worker_scoped_and_request_cannot_select_foreign_context(self):
        status, value = self.chat.post(
            "/api/project-chat", {"request_id": "request-0001", "question": "What is left?"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(value["answer"], "Answer for project-one.")
        self.assertEqual(self.calls[0][2], "project-one")
        for field, foreign in (
            ("project_id", "project-two"), ("code_root", "/foreign"),
            ("data_root", "/foreign-private"), ("task", "OTHER"),
            ("memory_path", "../other/memory"), ("session", "foreign-session"),
        ):
            request = {"request_id": f"foreign-{field.replace('_', '-')}", "question": "status", field: foreign}
            refused, error = self.chat.post("/api/project-chat", request)
            self.assertEqual(refused, 400)
            self.assertEqual(error["code"], "invalid_request")
        self.assertEqual(len(self.calls), 1)

    def test_malformed_oversized_cross_site_form_and_stale_action_are_refused(self):
        cases = (
            ({"request_id": "malformed-1", "question": "status"}, {}, "application/json", b"{"),
            ({}, {}, "application/json", b"x" * (project_worker.CHAT_BODY_LIMIT + 1)),
            ({"request_id": 42, "question": "status"}, {}, "application/json", None),
            ({"request_id": "bad-question-1", "question": {"status": True}}, {}, "application/json", None),
            ({"request_id": "empty-question-1", "question": "  "}, {}, "application/json", None),
            ({"request_id": "crosssite-1", "question": "status"}, {"origin": "http://evil.example"}, "application/json", None),
            ({"request_id": "formbody-1", "question": "status"}, {}, "application/x-www-form-urlencoded", None),
            ({"request_id": "stale-token-1", "question": "status"}, {"token": "old-worker-token"}, "application/json", None),
        )
        expected = (400, 400, 400, 400, 400, 403, 400, 401)
        for index, (value, options, content_type, raw) in enumerate(cases):
            with self.subTest(index=index):
                status, _error = self.chat.post(
                    "/api/project-chat", value, content_type=content_type, raw=raw, **options,
                )
                self.assertEqual(status, expected[index])
        self.assertEqual(self.calls, [])

    def test_duplicate_receipt_is_bounded_and_does_not_execute_provider_twice(self):
        request = {"request_id": "duplicate-0001", "question": "What is left?"}
        first = self.chat.post("/api/project-chat", request)
        second = self.chat.post("/api/project-chat", request)
        self.assertEqual(first[0], second[0], 200)
        self.assertFalse(first[1]["duplicate"])
        self.assertTrue(second[1]["duplicate"])
        self.assertEqual(len(self.calls), 1)
        changed = self.chat.post("/api/project-chat", {
            "request_id": "duplicate-0001", "question": "What is this project about?",
        })
        self.assertEqual(changed[0], 409)
        self.assertEqual(len(self.calls), 1)
        for index in range(project_worker.CHAT_RECEIPT_LIMIT + 4):
            self.chat.post("/api/project-chat", {
                "request_id": f"receipt-{index:08d}", "question": "status",
            })
            if index and index % 10 == 0:
                # Directly expire the bounded timing window; production uses
                # monotonic time and never sleeps or loops in the worker.
                self.chat.service.started.clear()
        self.assertLessEqual(len(self.chat.service.receipts), project_worker.CHAT_RECEIPT_LIMIT)

    def test_concurrency_duplicate_and_cancellation_leave_no_partial_receipt(self):
        started = threading.Event()

        def blocking(_root, _question, **kwargs):
            started.set()
            cancel = kwargs["cancel_event"]
            cancel.wait(5)
            if cancel.is_set():
                raise project_chat.ChatCancelled("The chat request was cancelled")
            return response()

        chat = ServedChat(self.context, self.settings_home, blocking, token="cancel-token")
        self.addCleanup(chat.close)
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault(
            "first", chat.post("/api/project-chat", {
                "request_id": "cancel-request-1", "question": "status",
            })
        ))
        thread.start(); self.assertTrue(started.wait(2))
        duplicate = chat.post("/api/project-chat", {
            "request_id": "cancel-request-1", "question": "status",
        })
        concurrent = chat.post("/api/project-chat", {
            "request_id": "cancel-request-2", "question": "status",
        })
        cancelled = chat.post("/api/project-chat/cancel", {"request_id": "cancel-request-1"})
        thread.join(timeout=5)
        self.assertEqual(duplicate[0], 409)
        self.assertEqual(concurrent[0], 429)
        self.assertEqual(cancelled, (200, {"request_id": "cancel-request-1", "cancelled": True}))
        self.assertEqual(result["first"][0], 409)
        self.assertNotIn("cancel-request-1", chat.service.receipts)

    def test_worker_shutdown_cancels_and_drains_inflight_provider(self):
        started = threading.Event()

        def blocking(_root, _question, **kwargs):
            started.set()
            kwargs["cancel_event"].wait(5)
            raise project_chat.ChatCancelled("The chat request was cancelled")

        service = project_worker.ProjectChatService(
            self.context, self.settings_home, "shutdown-project", answerer=blocking,
        )
        result = {}

        def submit():
            try:
                service.submit("shutdown-request", "status")
            except Exception as error:  # capture the expected operational result
                result["error"] = error

        thread = threading.Thread(target=submit)
        thread.start(); self.assertTrue(started.wait(2))
        service.shutdown(); thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result.get("error"), project_chat.ChatCancelled)
        self.assertFalse(service.active)
        self.assertFalse(service.receipts)

    def test_rate_limit_and_provider_operational_errors_are_explicit(self):
        for index in range(project_worker.CHAT_RATE_LIMIT):
            status, _ = self.chat.post("/api/project-chat", {
                "request_id": f"rate-{index:08d}", "question": "status",
            })
            self.assertEqual(status, 200)
        limited = self.chat.post("/api/project-chat", {
            "request_id": "rate-overflow", "question": "status",
        })
        self.assertEqual(limited[0], 429)

        errors = (
            (project_chat.ProviderTimeout("provider timed out"), 504, "provider_timeout"),
            (project_chat.ProviderMalformedOutput("provider malformed"), 502, "provider_malformed_output"),
            (project_chat.AnswerValidationError("provider rejected"), 502, "answer_validation_failed"),
        )
        for index, (failure, expected_status, code) in enumerate(errors):
            failed = ServedChat(
                self.context, self.settings_home,
                lambda *_args, failure=failure, **_kwargs: (_ for _ in ()).throw(failure),
                token=f"error-token-{index}", project_id=f"error-project-{index}",
            )
            try:
                status, value = failed.post("/api/project-chat", {
                    "request_id": f"error-request-{index}", "question": "status",
                })
                self.assertEqual(status, expected_status)
                self.assertEqual(value["code"], code)
                self.assertNotEqual(value["error"], project_chat.UNKNOWN_ANSWER)
            finally:
                failed.close()

    def test_paused_project_refuses_chat_without_calling_provider(self):
        board.begin_project_pause(self.context, drain_seconds=0)
        board.finish_project_pause(self.context)
        status, value = self.chat.post("/api/project-chat", {
            "request_id": "paused-request-1", "question": "status",
        })
        self.assertEqual(status, 403)
        self.assertEqual(value["code"], "forbidden")
        self.assertIn("paused", value["error"])
        self.assertEqual(self.calls, [])

    def test_chat_does_not_write_governance_memory_git_or_adopted_repository(self):
        def digest_tree(path: Path) -> str:
            digest = hashlib.sha256()
            if not path.exists():
                return digest.hexdigest()
            for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
                digest.update(str(item.relative_to(path)).encode()); digest.update(b"\0")
                digest.update(item.read_bytes()); digest.update(b"\0")
            return digest.hexdigest()

        protected = [
            self.context.code_root,
            self.context.data_root / "board",
            self.context.data_root / "tasks",
            self.context.data_root / "evidence",
            self.context.data_root / "memory",
        ]
        before = [digest_tree(path) for path in protected]
        question = "SECRET-PROMPT-9911 What is this project about?"
        status, value = self.chat.post("/api/project-chat", {
            "request_id": "readonly-0001", "question": question,
        })
        self.assertEqual(status, 200)
        self.assertEqual([digest_tree(path) for path in protected], before)
        persisted = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in self.context.data_root.rglob("*") if path.is_file()
        )
        self.assertNotIn(question, persisted)
        self.assertNotIn(value["answer"], persisted)

    def test_worker_restart_rotates_browser_action_credential(self):
        old_token = self.chat.token
        self.chat.close()
        restarted = ServedChat(
            self.context, self.settings_home,
            lambda *_args, **_kwargs: response(), token="new-worker-token",
        )
        self.addCleanup(restarted.close)
        status, value = restarted.post(
            "/api/project-chat", {"request_id": "restart-0001", "question": "status"},
            token=old_token,
        )
        self.assertEqual(status, 401)
        self.assertEqual(value["code"], "stale_action")


class ProjectChatBrowserTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(self.context)
        project_memory.initialize(self.context, project_name="Browser chat", description="Browser facts.")
        self.settings_home = base / "manager"; global_settings.initialize(self.settings_home)
        configure_verified_key(self.settings_home)
        self.questions = []

        def answerer(_root, question, **_kwargs):
            self.questions.append(question)
            return response("Direct browser answer.")

        self.chat = ServedChat(self.context, self.settings_home, answerer, token="browser-token")
        self.addCleanup(self.chat.close)

    def render(self, width, height):
        sink = {}
        origin = self.chat.origin
        script = r"""
<script>
(async()=>{
  const pause=delay=>new Promise(resolve=>setTimeout(resolve,delay));
  for(let attempt=0;attempt<80&&!document.querySelector('#project-chat');attempt++)await pause(50);
  const input=document.querySelector('#project-chat-input');input.value='What is this project about?\nWhat is left?';
  document.querySelector('#project-chat-form').requestSubmit();
  for(let attempt=0;attempt<100&&!document.querySelector('.chat-answer');attempt++)await pause(50);
  const chat=document.querySelector('#project-chat').getBoundingClientRect(),progress=document.querySelector('.delivery-progress-panel').getBoundingClientRect(),doc=document.scrollingElement;
  const answer=document.querySelector('.chat-answer')?.textContent||'',status=document.querySelector('#project-chat-status').textContent;
  document.querySelector('#project-chat-clear').click();
  await fetch('/__layout_result__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answer,status,chatBottom:chat.bottom,progressTop:progress.top,scrollWidth:doc.scrollWidth,clientWidth:doc.clientWidth,historyAfterClear:document.querySelectorAll('.chat-turn').length,inputFocused:document.activeElement===input,storedChatKeys:Object.keys(localStorage).filter(key=>key.includes('chat'))})});
})();
</script>
"""

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *_): return
            def reply(self, status, body, content_type="application/json"):
                self.send_response(status); self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                try:
                    with urlopen(origin + self.path, timeout=10) as opened:
                        body = opened.read(); content_type = opened.headers.get("Content-Type", "application/json")
                except HTTPError as error:
                    self.reply(error.code, error.read(), error.headers.get("Content-Type", "application/json")); return
                if self.path == "/":
                    body = body.decode().replace("</body>", script + "</body>", 1).encode()
                self.reply(200, body, content_type)
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0")); body = self.rfile.read(length)
                if self.path == "/__layout_result__":
                    sink["value"] = json.loads(body); self.reply(200, b"{}"); return
                request = Request(origin + self.path, data=body, method="POST", headers={
                    "Content-Type": self.headers.get("Content-Type", "application/json"),
                    "X-Harness-Chat-Action": self.headers.get("X-Harness-Chat-Action", ""),
                    "Origin": origin, "Sec-Fetch-Site": "same-origin",
                })
                try:
                    with urlopen(request, timeout=10) as opened:
                        self.reply(opened.status, opened.read(), opened.headers.get("Content-Type", "application/json"))
                except HTTPError as error:
                    self.reply(error.code, error.read())

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True); thread.start()
        profile = tempfile.TemporaryDirectory()
        process = browser_acceptance.launch(
            f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name),
            width=width, height=height,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.05)
            self.assertIn("value", sink, "Chrome did not report project chat state")
            return sink["value"]
        finally:
            process.close()
            profile.cleanup(); proxy.shutdown(); thread.join(timeout=3); proxy.server_close()

    def test_real_browser_multiline_send_clear_focus_and_responsive_non_overlap(self):
        for width, height in ((1000, 622), (390, 700)):
            with self.subTest(viewport=(width, height)):
                value = self.render(width, height)
                self.assertIn("Direct browser answer.", value["answer"])
                self.assertIn("Answered from", value["status"])
                self.assertLessEqual(value["chatBottom"], value["progressTop"] + 1)
                self.assertLessEqual(value["scrollWidth"], value["clientWidth"])
                self.assertEqual(value["historyAfterClear"], 0)
                self.assertTrue(value["inputFocused"])
                self.assertEqual(value["storedChatKeys"], [])
        self.assertEqual(self.questions, [
            "What is this project about?\nWhat is left?",
            "What is this project about?\nWhat is left?",
        ])


class ProjectChatManagedLifecycleTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.api = FakeOpenAI(delay=0.6)
        self.addCleanup(self.api.close)

    def test_two_real_managed_workers_keep_simultaneous_api_answers_project_scoped(self):
        managers = []
        with patch.dict(os.environ, self.api.environment()):
            try:
                first = managed_project(
                    self.base / "one", "Project One", "Purpose unique to managed project one.",
                )
                second = managed_project(
                    self.base / "two", "Project Two", "Purpose unique to managed project two.",
                )
                managers.extend(((first[0], first[1]), (second[0], second[1])))
                before = {
                    entry["id"]: hashlib.sha256((Path(entry["code_root"]) / "owner.txt").read_bytes()).hexdigest()
                    for _manager, entry in managers
                }
                values = {}
                threads = [
                    threading.Thread(
                        target=lambda label=label, item=item: values.setdefault(
                            label, post_managed_chat(
                                item[2], item[3], f"simultaneous-{label}-0001",
                                "What is this project about?",
                            ),
                        )
                    )
                    for label, item in (("one", first), ("two", second))
                ]
                started = time.monotonic()
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                elapsed = time.monotonic() - started
                self.assertEqual(values["one"][0], 200)
                self.assertEqual(values["two"][0], 200)
                self.assertEqual(values["one"][1]["answer"], "Purpose unique to managed project one.")
                self.assertEqual(values["two"][1]["answer"], "Purpose unique to managed project two.")
                self.assertNotIn("project two", values["one"][1]["answer"].casefold())
                self.assertNotIn("project one", values["two"][1]["answer"].casefold())
                self.assertLess(elapsed, 1.8, "independent project workers must not serialize provider waits")
                for _manager, entry in managers:
                    self.assertEqual(
                        hashlib.sha256((Path(entry["code_root"]) / "owner.txt").read_bytes()).hexdigest(),
                        before[entry["id"]],
                    )
                    self.assertFalse((Path(entry["code_root"]) / ".harness").exists())
            finally:
                for manager, entry in reversed(managers):
                    manager.close_project(entry["id"])

    def test_real_manager_pause_resume_close_reopen_and_stale_token(self):
        manager = entry = None
        with patch.dict(os.environ, self.api.environment()):
            try:
                manager, entry, origin, token, page = managed_project(
                    self.base / "lifecycle", "Lifecycle project", "Durable lifecycle purpose.",
                )
                self.assertNotIn(str(Path(entry["code_root"])), page)
                self.assertNotIn(str(Path(entry["data_root"])), page)
                self.assertEqual(
                    post_managed_chat(origin, token, "lifecycle-active-1", "What is its current status?")[0],
                    200,
                )
                manager.pause_project(entry["id"], drain_seconds=0, stop_timeout=0)
                paused = post_managed_chat(origin, token, "lifecycle-paused-1", "What is its current status?")
                self.assertEqual(paused[0], 403)
                self.assertEqual(paused[1]["code"], "forbidden")
                manager.resume_project(entry["id"])
                resumed = post_managed_chat(origin, token, "lifecycle-resumed-1", "What is this project about?")
                self.assertEqual(resumed[0], 200)
                self.assertEqual(resumed[1]["answer"], "Durable lifecycle purpose.")

                manager.close_project(entry["id"])
                reopened = manager.open_project(entry["id"])
                reopened_origin = reopened["board_url"].rstrip("/")
                new_token, _ = action_token(reopened_origin)
                self.assertNotEqual(token, new_token)
                stale = post_managed_chat(
                    reopened_origin, token, "lifecycle-stale-1", "What is this project about?",
                )
                self.assertEqual(stale[0], 401)
                self.assertEqual(stale[1]["code"], "stale_action")
                fresh = post_managed_chat(
                    reopened_origin, new_token, "lifecycle-reopen-1", "What is this project about?",
                )
                self.assertEqual(fresh[0], 200)
                self.assertEqual(fresh[1]["answer"], "Durable lifecycle purpose.")
            finally:
                if manager is not None and entry is not None:
                    manager.close_project(entry["id"])


@unittest.skipUnless(
    os.environ.get("HARNESS_RUN_LIVE_PROVIDER") == "1",
    "set HARNESS_RUN_LIVE_PROVIDER=1 for the configured-provider field trial",
)
class ProjectChatLiveProviderTests(unittest.TestCase):
    def test_real_manager_worker_browser_and_openai_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = entry = None
            try:
                manager, entry, origin, token, page = managed_project(
                    base, "Live provider project", "Live provider proves exact grounded project chat.",
                )
                self.assertIn('id="project-chat"', page)
                dashboard = json.loads(urlopen(origin + "/api/dashboard", timeout=5).read())
                self.assertNotIn("path", dashboard)
                self.assertNotIn(str(Path(entry["code_root"])), json.dumps(dashboard))
                self.assertNotIn(str(Path(entry["data_root"])), json.dumps(dashboard))
                status, value = post_managed_chat(
                    origin, token, "live-provider-request-0001", "What is this project about?",
                )
                self.assertEqual(status, 200, value)
                self.assertEqual(value["answer"], "Live provider proves exact grounded project chat.")
                self.assertTrue(value["source_ids"])
                self.assertNotIn(str(Path(entry["code_root"])), json.dumps(value))
                self.assertNotIn(str(Path(entry["data_root"])), json.dumps(value))

                # The real rendered-browser behavior is independently exercised
                # by ProjectChatBrowserTests against this identical worker page;
                # this live case proves the manager-launched worker reaches the
                # direct OpenAI API rather than a CLI or mock.
                with tempfile.TemporaryDirectory() as browser_runtime:
                    dom = browser_acceptance.dump_dom(origin + "/", Path(browser_runtime), timeout=30)
                self.assertIn('id="project-chat"', dom)
                self.assertNotIn(str(Path(entry["code_root"])), dom)
                self.assertNotIn(str(Path(entry["data_root"])), dom)
            finally:
                if manager is not None and entry is not None:
                    manager.close_project(entry["id"])


if __name__ == "__main__":
    unittest.main()

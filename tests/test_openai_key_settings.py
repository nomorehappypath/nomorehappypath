# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The owner's OpenAI API key lives in Settings and gates project chat.

Project chat calls OpenAI directly with the owner's key.  Until this change the
key could only arrive as an environment variable, the composer offered itself
whether or not a key existed, and a stray ``OPENAI_API_KEY`` silently outranked
anything the owner saved.  These tests hold the three guarantees the owner
asked for: the key is entered and verified in Settings, chat stays switched off
until that key connects, and the key bytes never leave the restricted file.

Run: PYTHONPATH=. python3 -m unittest tests.test_openai_key_settings -v
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import (
    board, board_viewer, browser_acceptance, control, global_settings, project_chat,
    project_manager, project_memory, project_worker,
)
from harness.project_context import ProjectContext
from tests.chat_key_support import configure_verified_key
from tests.environment_support import require_loopback

GOOD_KEY = "sk-" + "g" * 32
OTHER_KEY = "sk-" + "o" * 32
ENV_KEY = "sk-" + "e" * 32


class FakeOpenAI:
    """A loopback stand-in that accepts exactly one key, like the real API."""

    def __init__(self, accepted: str = GOOD_KEY):
        self.accepted = accepted
        self.requests: list[dict] = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args):
                return

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                authorization = self.headers.get("Authorization", "")
                test.requests.append({
                    "authorization": authorization,
                    "path": self.path,
                    "payload": json.loads(body or b"{}"),
                })
                accepted = authorization == f"Bearer {test.accepted}"
                payload = json.dumps({
                    "status": "completed",
                    "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": "ok"},
                    ]}],
                } if accepted else {"error": {"message": "invalid api key"}}).encode()
                self.send_response(200 if accepted else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def environment(self) -> dict[str, str]:
        port = self.server.server_address[1]
        return {
            "HARNESS_OPENAI_TESTING": "1",
            "HARNESS_OPENAI_TEST_ENDPOINT": f"http://127.0.0.1:{port}/v1/responses",
        }

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()


class KeyStorageTests(unittest.TestCase):
    """The stored key is the key that is used, and it stays unreadable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        global_settings.initialize(self.home)

    def test_saved_key_outranks_a_key_in_the_process_environment(self):
        global_settings.store_openai_api_key(self.home, GOOD_KEY)
        self.assertEqual(
            global_settings.openai_api_key(
                self.home, source_environment={"OPENAI_API_KEY": ENV_KEY},
            ),
            GOOD_KEY,
        )

    def test_environment_key_is_used_only_when_settings_holds_none(self):
        self.assertEqual(
            global_settings.openai_api_key(
                self.home, source_environment={"OPENAI_API_KEY": ENV_KEY},
            ),
            ENV_KEY,
        )
        status = global_settings.openai_status(
            self.home, source_environment={"OPENAI_API_KEY": ENV_KEY},
        )
        self.assertEqual(status["source"], "environment")
        self.assertTrue(status["configured"])
        self.assertFalse(status["connected"])

    def test_no_key_reports_a_plain_reason_and_no_key_bytes(self):
        status = global_settings.openai_status(self.home, source_environment={})
        self.assertFalse(status["configured"])
        self.assertFalse(status["connected"])
        self.assertEqual(status["masked"], "")

    def test_status_masks_the_key_and_never_returns_its_bytes(self):
        configure_verified_key(self.home, GOOD_KEY)
        status = global_settings.openai_status(self.home)
        self.assertTrue(status["connected"])
        self.assertEqual(status["masked"], f"sk-…{GOOD_KEY[-4:]}")
        self.assertNotIn(GOOD_KEY, json.dumps(status))
        self.assertNotIn(GOOD_KEY, (self.home / "settings.json").read_text(encoding="utf-8"))

    def test_replacing_the_key_drops_the_previous_keys_verified_state(self):
        configure_verified_key(self.home, GOOD_KEY)
        global_settings.store_openai_api_key(self.home, OTHER_KEY)
        status = global_settings.openai_status(self.home)
        self.assertTrue(status["configured"])
        self.assertFalse(status["connected"])

    def test_secret_file_and_directory_are_readable_only_by_this_owner(self):
        global_settings.store_openai_api_key(self.home, GOOD_KEY)
        path = global_settings.openai_api_key_path(self.home)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_uid, os.geteuid())

    def test_removing_the_key_switches_chat_off_again(self):
        configure_verified_key(self.home, GOOD_KEY)
        self.assertTrue(global_settings.chat_availability(self.home)["available"])
        global_settings.remove_openai_api_key(self.home)
        self.assertFalse(global_settings.openai_api_key_path(self.home).exists())
        availability = global_settings.chat_availability(self.home)
        self.assertFalse(availability["available"])
        self.assertIn("Settings", availability["reason"])

    def test_an_unusable_key_is_refused_before_any_network_call(self):
        for value in ("", "not-a-key", "sk-short", "sk-" + "x" * 4096):
            with self.assertRaises(ValueError):
                global_settings.validate_openai_api_key(value)


class UntrustworthyStoredKeyTests(unittest.TestCase):
    """A key that cannot be trusted fails closed; it never falls back.

    Falling back to ``OPENAI_API_KEY`` when the stored secret exists but fails
    its security checks would let anyone who can loosen the file's mode - or
    anything that can set an environment variable on the launchd job - decide
    which key the owner's project chat spends money with.  Absent is the only
    condition the environment may answer.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        global_settings.initialize(self.home)
        self.path = global_settings.openai_api_key_path(self.home)
        self.environment = {"OPENAI_API_KEY": ENV_KEY}

    def store(self) -> Path:
        global_settings.store_openai_api_key(self.home, GOOD_KEY)
        return self.path

    def assert_fails_closed(self, expected: str):
        with self.assertRaises(ValueError) as raised:
            global_settings.openai_api_key(self.home, source_environment=self.environment)
        self.assertNotIsInstance(raised.exception, global_settings.OpenAIKeyMissing)
        self.assertIn(expected, str(raised.exception))
        status = global_settings.openai_status(
            self.home, source_environment=self.environment,
        )
        self.assertTrue(status["unusable"])
        self.assertFalse(status["configured"])
        self.assertFalse(status["connected"])
        self.assertEqual(status["masked"], "")
        availability = global_settings.chat_availability(self.home)
        self.assertFalse(availability["available"])
        self.assertIn(expected, availability["reason"])

    def test_a_world_readable_key_file_never_falls_back_to_the_environment(self):
        self.store().chmod(0o644)
        self.assert_fails_closed("mode 0600")

    def test_a_group_writable_key_file_fails_closed(self):
        self.store().chmod(0o620)
        self.assert_fails_closed("mode 0600")

    def test_a_symlinked_key_file_fails_closed(self):
        self.store()
        planted = Path(self._tmp.name) / "planted"
        planted.write_text(OTHER_KEY + "\n", encoding="utf-8")
        planted.chmod(0o600)
        self.path.unlink()
        self.path.symlink_to(planted)
        self.assert_fails_closed("regular file")

    def test_a_writable_secrets_directory_fails_closed(self):
        self.store()
        self.path.parent.chmod(0o777)
        self.assert_fails_closed("writable only by you")

    def test_unreadable_key_bytes_fail_closed(self):
        self.store()
        self.path.write_text("not-a-key-at-all-but-long-enough\n", encoding="utf-8")
        self.path.chmod(0o600)
        self.assert_fails_closed("invalid")

    def test_an_empty_key_file_fails_closed(self):
        self.store()
        self.path.write_text("\n", encoding="utf-8")
        self.path.chmod(0o600)
        self.assert_fails_closed("size")

    def test_a_directory_in_place_of_the_key_file_fails_closed(self):
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.path.mkdir()
        self.assert_fails_closed("regular file")

    def test_only_an_absent_key_lets_the_environment_answer(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(
            global_settings.openai_api_key(self.home, source_environment=self.environment),
            ENV_KEY,
        )
        with self.assertRaises(global_settings.OpenAIKeyMissing):
            global_settings.openai_api_key(self.home, source_environment={})

    def test_an_unusable_environment_key_is_reported_not_ignored(self):
        status = global_settings.openai_status(
            self.home, source_environment={"OPENAI_API_KEY": "junk"},
        )
        self.assertTrue(status["unusable"])
        self.assertIn("environment", status["message"])

    def test_a_chat_request_is_refused_while_the_stored_key_is_unsafe(self):
        base = Path(self._tmp.name)
        code = base / "code"
        code.mkdir()
        context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(context)
        board.snapshot(context)
        project_memory.initialize(context, project_name="Unsafe", description="Facts.")
        configure_verified_key(self.home, GOOD_KEY)
        self.path.chmod(0o644)
        service = project_worker.ProjectChatService(
            context, self.home, "unsafe-project",
            answerer=lambda *args, **kwargs: {"answer": "never reached", "source_ids": []},
        )
        with self.assertRaises(project_chat.ProviderFailure) as raised:
            service.submit("33333333-3333-4333-8333-333333333333", "What is left?")
        self.assertIn("mode 0600", str(raised.exception))

    def test_saving_a_fresh_key_repairs_an_unsafe_file(self):
        self.store().chmod(0o644)
        global_settings.store_openai_api_key(self.home, OTHER_KEY)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(global_settings.openai_api_key(self.home), OTHER_KEY)


class SettingsApiTests(unittest.TestCase):
    """Saving a key verifies it first, and a failed save changes nothing."""

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.openai = FakeOpenAI()
        self.addCleanup(self.openai.close)
        patcher = mock.patch.dict(os.environ, self.openai.environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = project_manager.ProjectManager(self.home, board_port=0)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), project_manager.make_handler(self.manager),
        )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, path: str, method: str = "POST", body: dict | None = None):
        request = Request(
            self.url + path, method=method,
            data=json.dumps(body or {}).encode() if method != "DELETE" else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b"{}")
        except HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_settings_reports_no_key_before_the_owner_saves_one(self):
        with urlopen(self.url + "/api/settings", timeout=15) as response:
            payload = json.loads(response.read())
        self.assertFalse(payload["openai"]["configured"])
        self.assertFalse(payload["openai"]["connected"])
        self.assertTrue(payload["chat_model"])

    def test_saving_a_working_key_verifies_it_then_stores_it(self):
        status, payload = self.call("/api/settings/openai-key", body={"key": GOOD_KEY})
        self.assertEqual(status, 200)
        self.assertTrue(payload["openai"]["connected"])
        self.assertEqual(payload["openai"]["source"], "manager_secret")
        self.assertEqual(payload["openai"]["masked"], f"sk-…{GOOD_KEY[-4:]}")
        self.assertNotIn(GOOD_KEY, json.dumps(payload))
        self.assertEqual(
            self.openai.requests[-1]["authorization"], f"Bearer {GOOD_KEY}",
        )
        self.assertEqual(self.openai.requests[-1]["path"], "/v1/responses")
        self.assertTrue(global_settings.chat_availability(self.home)["available"])

    def test_a_key_openai_rejects_is_never_stored(self):
        status, payload = self.call("/api/settings/openai-key", body={"key": OTHER_KEY})
        self.assertEqual(status, 400)
        self.assertIn("did not accept this API key", payload["error"])
        self.assertFalse(global_settings.openai_api_key_path(self.home).exists())
        self.assertFalse(global_settings.chat_availability(self.home)["available"])

    def test_a_rejected_replacement_leaves_the_working_key_connected(self):
        self.call("/api/settings/openai-key", body={"key": GOOD_KEY})
        status, _ = self.call("/api/settings/openai-key", body={"key": OTHER_KEY})
        self.assertEqual(status, 400)
        self.assertEqual(global_settings.openai_api_key(self.home), GOOD_KEY)
        self.assertTrue(global_settings.chat_availability(self.home)["available"])

    def test_testing_the_saved_key_records_a_fresh_result(self):
        self.call("/api/settings/openai-key", body={"key": GOOD_KEY})
        before = len(self.openai.requests)
        status, payload = self.call("/api/settings/openai-key/test")
        self.assertEqual(status, 200)
        self.assertTrue(payload["openai"]["connected"])
        self.assertEqual(len(self.openai.requests), before + 1)

    def test_a_revoked_key_stops_reporting_connected(self):
        self.call("/api/settings/openai-key", body={"key": GOOD_KEY})
        self.openai.accepted = "sk-" + "z" * 32
        status, payload = self.call("/api/settings/openai-key/test")
        self.assertEqual(status, 400)
        self.assertFalse(global_settings.openai_status(self.home)["connected"])
        self.assertFalse(global_settings.chat_availability(self.home)["available"])

    def test_removing_the_key_from_settings_clears_it(self):
        self.call("/api/settings/openai-key", body={"key": GOOD_KEY})
        status, payload = self.call("/api/settings/openai-key", method="DELETE")
        self.assertEqual(status, 200)
        self.assertFalse(payload["openai"]["configured"])
        self.assertFalse(global_settings.openai_api_key_path(self.home).exists())

    def test_saving_an_unusable_key_explains_itself_without_calling_openai(self):
        before = len(self.openai.requests)
        status, payload = self.call("/api/settings/openai-key", body={"key": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("invalid", payload["error"])
        self.assertEqual(len(self.openai.requests), before)


class ChatGateTests(unittest.TestCase):
    """The board refuses chat, not just the button, until the key connects."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"
        code.mkdir()
        self.context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(self.context)
        board.snapshot(self.context)
        project_memory.initialize(
            self.context, project_name="Gate", description="Gate facts.",
        )
        self.home = base / "home"
        global_settings.initialize(self.home)

    def service(self):
        return project_worker.ProjectChatService(
            self.context, self.home, "gate-project",
            answerer=lambda *args, **kwargs: {"answer": "never reached", "source_ids": []},
        )

    def test_a_question_is_refused_while_no_key_is_connected(self):
        with self.assertRaises(project_chat.ProviderFailure) as raised:
            self.service().submit("11111111-1111-4111-8111-111111111111", "What is left?")
        self.assertIn("Settings", str(raised.exception))

    def test_a_question_runs_once_the_key_is_connected(self):
        configure_verified_key(self.home, GOOD_KEY)
        answer = self.service().submit(
            "22222222-2222-4222-8222-222222222222", "What is left?",
        )
        self.assertEqual(answer["answer"], "never reached")

    def test_the_served_composer_is_disabled_until_the_key_connects(self):
        page = board_viewer.rendered_page(
            "Gate", "", "http://127.0.0.1:1/", "gate-project", "", "token", None, "",
            global_settings.chat_availability(self.home),
        )
        self.assertIn('id="project-chat-input"', page)
        self.assertIn('aria-describedby="project-chat-status" disabled', page)
        self.assertIn('id="project-chat-send" disabled', page)
        self.assertIn("Add it in Settings", page)

        configure_verified_key(self.home, GOOD_KEY)
        page = board_viewer.rendered_page(
            "Gate", "", "http://127.0.0.1:1/", "gate-project", "", "token", None, "",
            global_settings.chat_availability(self.home),
        )
        self.assertIn('aria-describedby="project-chat-status"></textarea>', page)
        self.assertNotIn('id="project-chat-send" disabled', page)

    def test_the_board_api_carries_the_reason_the_page_shows(self):
        require_loopback()
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.context, project_name="Gate", settings_home=self.home,
            project_id="gate-project", chat_action_token="token",
        ))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/api/control"
        with urlopen(url, timeout=10) as response:
            value = json.loads(response.read())
        self.assertFalse(value["project_chat"]["available"])
        self.assertIn("Settings", value["project_chat"]["reason"])

        configure_verified_key(self.home, GOOD_KEY)
        with urlopen(url, timeout=10) as response:
            value = json.loads(response.read())
        self.assertTrue(value["project_chat"]["available"])
        self.assertNotIn(GOOD_KEY, json.dumps(value))


CHAT_PROBE = r"""
<script>
(async () => {
  const read = () => {
    const input = document.querySelector('#project-chat-input');
    const send = document.querySelector('#project-chat-send');
    const locked = document.querySelector('#project-chat-locked');
    if (!input) return null;
    const style = getComputedStyle(input);
    return {
      inputDisabled: input.disabled,
      sendDisabled: send ? send.disabled : null,
      noticeVisible: locked ? !locked.hidden && locked.getBoundingClientRect().height > 0 : false,
      noticeText: locked ? locked.textContent.trim() : '',
      opacity: Number(style.opacity),
      panelHeight: document.querySelector('#project-chat').getBoundingClientRect().height,
    };
  };
  const first = read();
  let enabled = null;
  for (let attempt = 0; attempt < 60; attempt++) {
    const now = read();
    if (now && !now.inputDisabled) { enabled = now; break; }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({first, enabled}),
  });
})();
</script>
"""


def probe_proxy(target: str, sink: dict, script: str):
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args):
            return

        def reply(self, status, payload, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            try:
                with urlopen(target.rstrip("/") + self.path, timeout=15) as response:
                    payload, status = response.read(), response.status
                    content_type = response.headers.get("Content-Type", "application/json")
            except HTTPError as error:
                self.reply(error.code, error.read(), "application/json")
                return
            if content_type.startswith("text/html"):
                payload = payload.decode().replace("</body>", script + "</body>", 1).encode()
            self.reply(status, payload, content_type)

        def do_POST(self):
            data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/__probe__":
                sink["value"] = json.loads(data or b"{}")
                self.reply(200, b"{}", "application/json")
                return
            request = Request(
                target.rstrip("/") + self.path, data=data, method="POST",
                headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
            )
            try:
                with urlopen(request, timeout=15) as response:
                    self.reply(
                        response.status, response.read(),
                        response.headers.get("Content-Type", "application/json"),
                    )
            except HTTPError as error:
                self.reply(error.code, error.read(), "application/json")

    return Proxy


class RenderedChatGateTests(unittest.TestCase):
    """What the owner actually sees: a greyed composer that comes alive."""

    def setUp(self):
        require_loopback()
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"
        code.mkdir()
        self.context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(self.context)
        board.snapshot(self.context)
        project_memory.initialize(
            self.context, project_name="Rendered gate", description="Rendered facts.",
        )
        self.home = base / "home"
        global_settings.initialize(self.home)

    def test_composer_is_greyed_without_a_key_and_enables_when_one_connects(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.context, project_name="Rendered gate", settings_home=self.home,
            project_id="rendered-gate", chat_action_token="rendered-token",
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(
            f"http://127.0.0.1:{server.server_address[1]}", sink, CHAT_PROBE,
        ))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)

        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(
            f"http://127.0.0.1:{proxy.server_address[1]}/",
            Path(profile.name), width=1200, height=900,
        )
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and "first" not in sink.get("value", {}):
                time.sleep(0.1)
            # The owner saves a key in Settings while the board stays open.
            configure_verified_key(self.home, GOOD_KEY)
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if sink.get("value", {}).get("enabled"):
                    break
                time.sleep(0.1)
            reading = sink.get("value") or {}
        finally:
            process.close()

        self.assertTrue(reading, "Chrome did not report the chat composer state")
        first = reading["first"]
        self.assertIsNotNone(first, "the chat panel did not render")
        self.assertTrue(first["inputDisabled"], "composer was usable without a key")
        self.assertTrue(first["sendDisabled"], "Send was usable without a key")
        self.assertTrue(first["noticeVisible"], "no visible reason chat is switched off")
        self.assertIn("Settings", first["noticeText"])
        self.assertLess(first["opacity"], 1.0, "the disabled composer was not greyed")
        self.assertGreater(first["panelHeight"], 100, "the chat panel rendered blank")

        enabled = reading["enabled"]
        self.assertIsNotNone(enabled, "the composer never enabled after the key connected")
        self.assertFalse(enabled["sendDisabled"])
        self.assertFalse(enabled["noticeVisible"])


SETTINGS_PROBE = r"""
<script>
(async () => {
  const wait = async (test) => {
    for (let attempt = 0; attempt < 80; attempt++) {
      if (test()) return true;
      await new Promise(resolve => setTimeout(resolve, 150));
    }
    return false;
  };
  const card = () => document.querySelector('#openai-card');
  await wait(() => card() && document.querySelector('#openai-badge').textContent.trim() !== 'Checking');
  const state = () => {
    const badge = document.querySelector('#openai-badge');
    const box = card().getBoundingClientRect();
    return {
      badge: badge.textContent.trim(),
      badgeClass: badge.className,
      detail: document.querySelector('#openai-detail').textContent.trim(),
      result: document.querySelector('#openai-result').textContent.trim(),
      height: +box.height.toFixed(1),
      width: +box.width.toFixed(1),
      visible: box.height > 0 && box.width > 0,
      keyValue: document.querySelector('#openai-key').value,
      testHidden: document.querySelector('#openai-test').hidden,
      removeHidden: document.querySelector('#openai-remove').hidden,
    };
  };
  const before = state();
  document.querySelector('#openai-key').value = '__KEY__';
  document.querySelector('#openai-save').click();
  const connected = await wait(() => document.querySelector('#openai-badge').textContent.trim() === 'Connected');
  const after = state();
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({before, after, connected, html: document.querySelector('#settings-page').innerHTML}),
  });
})();
</script>
"""


UNSAFE_PROBE = SETTINGS_PROBE.split("  const before = state();", 1)[0] + r"""
  const before = state();
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(before),
  });
})();
</script>
"""


class RenderedSettingsCardTests(unittest.TestCase):
    """The owner can see and use the key card on the real Settings page."""

    def setUp(self):
        require_loopback()
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.openai = FakeOpenAI()
        self.addCleanup(self.openai.close)
        patcher = mock.patch.dict(os.environ, self.openai.environment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_settings_card_saves_a_key_and_reports_connected(self):
        manager = project_manager.ProjectManager(self.home, board_port=0)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), project_manager.make_handler(manager),
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(
            f"http://127.0.0.1:{server.server_address[1]}", sink,
            SETTINGS_PROBE.replace("__KEY__", GOOD_KEY),
        ))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)

        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(
            f"http://127.0.0.1:{proxy.server_address[1]}/?page=settings",
            Path(profile.name), width=1280, height=900,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
            reading = sink.get("value") or {}
        finally:
            process.close()

        self.assertTrue(reading, "Chrome did not report the Settings card")
        before, after = reading["before"], reading["after"]
        self.assertTrue(before["visible"], "the key card did not render")
        self.assertGreater(before["height"], 150, "the key card rendered collapsed")
        self.assertEqual(before["badge"], "Not connected")
        self.assertTrue(before["testHidden"] and before["removeHidden"])
        self.assertIn("No key saved yet", before["detail"])

        self.assertTrue(reading["connected"], f"the card never reported Connected: {after}")
        self.assertEqual(after["badge"], "Connected")
        self.assertIn(f"sk-…{GOOD_KEY[-4:]}", after["detail"])
        self.assertEqual(after["keyValue"], "", "the key stayed in the form field")
        self.assertFalse(after["testHidden"] or after["removeHidden"])
        self.assertNotIn(GOOD_KEY, reading["html"], "the page held the key bytes")
        self.assertTrue(global_settings.openai_status(self.home)["connected"])


class RenderedUnsafeKeyTests(unittest.TestCase):
    """The card names the real problem instead of pretending no key exists."""

    def setUp(self):
        require_loopback()
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        configure_verified_key(self.home, GOOD_KEY)
        global_settings.openai_api_key_path(self.home).chmod(0o644)

    def test_card_reports_an_unsafe_key_file_and_offers_to_replace_it(self):
        manager = project_manager.ProjectManager(self.home, board_port=0)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), project_manager.make_handler(manager),
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(
            f"http://127.0.0.1:{server.server_address[1]}", sink, UNSAFE_PROBE,
        ))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(
            f"http://127.0.0.1:{proxy.server_address[1]}/?page=settings",
            Path(profile.name), width=1280, height=900,
        )
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
            reading = sink.get("value") or {}
        finally:
            process.close()
        self.assertTrue(reading, "Chrome did not report the Settings card")
        self.assertEqual(reading["badge"], "Not connected")
        self.assertIn("cannot be used", reading["detail"])
        self.assertIn("mode 0600", reading["result"])
        self.assertFalse(reading["removeHidden"], "no way to clear the unsafe key")
        self.assertTrue(reading["visible"] and reading["height"] > 150)


if __name__ == "__main__":
    unittest.main()

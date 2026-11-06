# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The warning email: the owner is emailed once when a project is blocked, paused and waiting for a human.

Covers the saved settings and the password file, the sender (a mocked mail library for the happy path and a handwritten
fake mail server on 127.0.0.1 for the failure paths), the worker hook on a real temporary board, and the Settings routes.
No real email is ever sent.

Run:  PYTHONPATH=. python3 -m unittest tests.test_warning_email -v
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import stat
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, global_settings, project_manager, warning_email
from tests.environment_support import require_loopback

PASSWORD = "s3cret-mail-pass-XYZ"
GOOD = {"to": "owner@example.com", "host": "smtp.example.com", "port": 587, "user": "owner@example.com"}


class SettingsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"


class SavedSettings(SettingsCase):
    def test_round_trip_and_password_file(self):
        status = global_settings.save_warning_email(self.home, GOOD, PASSWORD)
        self.assertEqual({k: status[k] for k in GOOD}, GOOD)
        self.assertTrue(status["on"] and status["password_saved"])
        self.assertEqual(global_settings.load(self.home)["warning_email"], GOOD)
        path = global_settings.warning_email_password_path(self.home)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(global_settings.warning_email_password(self.home), PASSWORD)
        self.assertNotIn(PASSWORD, (self.home / "settings.json").read_text())
        self.assertNotIn(PASSWORD, json.dumps(status))

    def test_an_old_settings_file_without_the_key_still_loads(self):
        global_settings.initialize(self.home)
        path = self.home / "settings.json"
        document = json.loads(path.read_text())
        document.pop("warning_email")
        path.write_text(json.dumps(document))
        loaded = global_settings.load(self.home)
        self.assertNotIn("error", loaded)
        self.assertEqual(loaded["warning_email"], global_settings.default_warning_email())
        self.assertFalse(global_settings.warning_email_status(self.home)["on"])

    def test_bad_fields_are_refused_in_plain_words(self):
        bad = [
            {**GOOD, "to": "a b@example.com"}, {**GOOD, "to": "a@b.com, c@d.com"}, {**GOOD, "to": "no-at-sign"},
            {**GOOD, "to": "a@b.com\nBcc: x@y.com"}, {**GOOD, "host": "https://smtp.example.com"},
            {**GOOD, "host": "smtp example.com"}, {**GOOD, "host": "smtp.example.com\r\nRCPT"}, {**GOOD, "port": 0},
            {**GOOD, "port": 70000}, {**GOOD, "port": "abc"}, {**GOOD, "user": "me\r\nBcc: x@y.com"},
            {**GOOD, "to": "a@" + "b" * 300 + ".com"},
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    global_settings.save_warning_email(self.home, value, "")
                self.assertNotIn("Traceback", str(caught.exception))
        with self.assertRaises(ValueError):
            global_settings.save_warning_email(self.home, GOOD, "pass\nword")
        self.assertFalse(global_settings.warning_email_status(self.home)["on"], "a refused save changes nothing")

    def test_an_empty_password_keeps_the_saved_one(self):
        global_settings.save_warning_email(self.home, GOOD, PASSWORD)
        status = global_settings.save_warning_email(self.home, {**GOOD, "port": 465}, "")
        self.assertEqual(status["port"], 465)
        self.assertEqual(global_settings.warning_email_password(self.home), PASSWORD)

    def test_clearing_address_and_server_switches_it_off_and_removes_the_password(self):
        global_settings.save_warning_email(self.home, GOOD, PASSWORD)
        status = global_settings.save_warning_email(self.home, {"to": "", "host": "", "port": 587, "user": ""}, "")
        self.assertFalse(status["on"] or status["password_saved"])
        self.assertFalse(global_settings.warning_email_password_path(self.home).exists())

    def test_a_password_file_anyone_can_read_is_not_used(self):
        global_settings.save_warning_email(self.home, GOOD, PASSWORD)
        os.chmod(global_settings.warning_email_password_path(self.home), 0o644)
        with self.assertRaises(ValueError) as caught:
            global_settings.warning_email_password(self.home)
        self.assertNotIn(PASSWORD, str(caught.exception))


class FakeSmtp:
    """A tiny hand-written mail server on 127.0.0.1 that does not offer STARTTLS and records every byte it is sent."""

    def __init__(self):
        self.received: list[str] = []
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"220 fake ready\r\n")
                for raw in self.rfile:
                    line = raw.decode("utf-8", "replace")
                    outer.received.append(line)
                    command = line.strip().upper()
                    if command.startswith("EHLO") or command.startswith("HELO"):
                        self.wfile.write(b"250-fake\r\n250 8BITMIME\r\n")        # no STARTTLS on purpose
                    elif command == "QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        return
                    else:
                        self.wfile.write(b"250 ok\r\n")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class RecordingSmtp:
    """Stands in for smtplib.SMTP: records what the sender does and offers STARTTLS."""
    instances: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list = []
        self.sent = None
        RecordingSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def has_extn(self, name):
        return name == "starttls"

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        self.sent = message


class Sending(unittest.TestCase):
    def setUp(self):
        RecordingSmtp.instances = []

    def test_a_message_has_the_right_headers_and_is_sent_after_starttls_and_login(self):
        with mock.patch.object(warning_email.smtplib, "SMTP", RecordingSmtp):
            result = warning_email.send_message(GOOD, PASSWORD, "Hello  \n there", "Body text")
        self.assertEqual(result, {"sent": True, "error": ""})
        smtp = RecordingSmtp.instances[0]
        self.assertEqual((smtp.host, smtp.port, smtp.timeout), ("smtp.example.com", 587, warning_email.SMTP_TIMEOUT_SECONDS))
        self.assertLess(smtp.calls.index("starttls"), smtp.calls.index(("login", "owner@example.com", PASSWORD)))
        self.assertEqual(smtp.sent["To"], "owner@example.com")
        self.assertEqual(smtp.sent["From"], "owner@example.com")
        self.assertEqual(smtp.sent["Subject"], "Hello there")
        self.assertIn("Body text", smtp.sent.get_content())

    def test_from_falls_back_to_the_address_when_the_user_name_is_not_one(self):
        with mock.patch.object(warning_email.smtplib, "SMTP", RecordingSmtp):
            warning_email.send_message({**GOOD, "user": "apikey"}, PASSWORD, "s", "b")
        self.assertEqual(RecordingSmtp.instances[0].sent["From"], "owner@example.com")

    def test_port_465_uses_the_secure_connection_from_the_start(self):
        with mock.patch.object(warning_email.smtplib, "SMTP_SSL", RecordingSmtp) as secure, \
                mock.patch.object(warning_email.smtplib, "SMTP", side_effect=AssertionError("plain SMTP used")):
            result = warning_email.send_message({**GOOD, "port": 465}, PASSWORD, "s", "b")
        self.assertTrue(result["sent"])
        self.assertNotIn("starttls", RecordingSmtp.instances[0].calls)

    def test_blocked_message_wording(self):
        card = {"title": "Blocked, needs you: Delivery is stuck", "why": "Problem: X\nWhat to do: Y"}
        subject, body = warning_email.blocked_message("Weather App", card, "http://127.0.0.1:8741/")
        self.assertEqual(subject, "NoMoreHappyPath: Weather App is blocked and needs you")
        self.assertTrue(body.startswith("Blocked, needs you: Delivery is stuck\n\nProblem: X\nWhat to do: Y"))
        self.assertIn("The project is paused; nothing more is spent until you press Resume.", body)
        self.assertIn("http://127.0.0.1:8741/", body)
        self.assertNotIn("Mission Control", warning_email.blocked_message("P", card)[1], "no address is invented")

    def test_a_server_without_starttls_gets_nothing_and_never_the_password(self):
        require_loopback()
        server = FakeSmtp()
        self.addCleanup(server.close)
        result = warning_email.send_message({**GOOD, "host": "127.0.0.1", "port": server.port}, PASSWORD, "s", "b")
        self.assertFalse(result["sent"])
        self.assertEqual(result["error"], warning_email.NO_SECURE_CONNECTION)
        everything = "".join(server.received)
        self.assertNotIn(PASSWORD, everything)
        self.assertNotIn("AUTH", everything.upper())
        self.assertNotIn("DATA", everything.upper())

    def test_a_connection_error_is_one_plain_sentence(self):
        require_loopback()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]            # nothing listens here once the probe closes
        result = warning_email.send_message({**GOOD, "host": "127.0.0.1", "port": port}, PASSWORD, "s", "b")
        self.assertFalse(result["sent"])
        self.assertEqual(result["error"], f"Could not reach the mail server 127.0.0.1 on port {port}.")
        self.assertNotIn(PASSWORD, json.dumps(result))
        self.assertNotIn("Traceback", result["error"])

    def test_a_refused_login_says_so_without_the_password(self):
        class Refusing(RecordingSmtp):
            def login(self, user, password):
                raise warning_email.smtplib.SMTPAuthenticationError(535, ("bad " + password).encode())
        with mock.patch.object(warning_email.smtplib, "SMTP", Refusing):
            result = warning_email.send_message(GOOD, PASSWORD, "s", "b")
        self.assertEqual(result, {"sent": False, "error": warning_email.BAD_LOGIN})
        self.assertNotIn(PASSWORD, json.dumps(result))

    def test_off_means_nothing_is_attempted(self):
        with mock.patch.object(warning_email.smtplib, "SMTP", side_effect=AssertionError("connected")):
            result = warning_email.send_message({**GOOD, "host": ""}, PASSWORD, "s", "b")
        self.assertEqual(result, {"sent": False, "error": warning_email.NOT_SET_UP})


class WorkerHook(SettingsCase):
    """notify_blocked_cards on a real temporary board."""

    def setUp(self):
        super().setUp()
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        self.agent = board.register(self.root, "cto", "GLOBAL_MONITOR")
        self.sent: list = []

    def card(self, *, blocked: bool = True, title: str = "Blocked, needs you: Delivery is stuck and the project is paused") -> str:
        with board.locked_state(self.root, allow_paused=True, operation="test card") as state:
            card = board._record_owner_action(state, self.agent, title, "", board.blocked_card_text("It cannot sign in.", "Fix the sign-in."),
                                              "TASK-ONE", kind="agent", for_agent=self.agent["id"])
            if blocked:
                card["blocked"] = True
            return card["id"]

    def run_hook(self, outcome=None):
        outcome = outcome or {"sent": True, "error": ""}

        def fake(settings, password, subject, body):
            self.sent.append((settings, password, subject, body))
            return outcome
        with mock.patch.object(warning_email, "send_message", fake):
            return warning_email.notify_blocked_cards(self.root, self.home, "Weather App", "http://127.0.0.1:8741/")

    def stored(self, card_id):
        return board.snapshot(self.root)["owner_actions"][card_id]

    def events(self):
        return [e for e in board.snapshot(self.root)["events"] if e["kind"].startswith("warning_email")]

    def test_one_blocked_card_sends_exactly_once(self):
        global_settings.save_warning_email(self.home, GOOD, PASSWORD)
        card_id = self.card()
        self.assertEqual(len(self.run_hook()), 1)
        self.assertEqual(len(self.sent), 1)
        settings, password, subject, body = self.sent[0]
        self.assertEqual((password, subject), (PASSWORD, "NoMoreHappyPath: Weather App is blocked and needs you"))
        self.assertIn("Problem: It cannot sign in.\nWhat to do: Fix the sign-in.", body)
        card = self.stored(card_id)
        self.assertTrue(card["warning_email_at"])
        self.assertEqual(card["warning_email_result"], "sent")
        events = self.events()
        self.assertEqual([e["kind"] for e in events], ["warning_email_sent"])
        self.assertNotIn(PASSWORD, json.dumps(board.snapshot(self.root)))
        self.assertEqual(self.run_hook(), [])
        self.assertEqual(len(self.sent), 1, "a second pass sends nothing")
        self.assertEqual(len(self.events()), 1)

    def test_a_failed_send_is_recorded_and_never_retried(self):
        global_settings.save_warning_email(self.home, GOOD, PASSWORD)
        card_id = self.card()
        self.run_hook({"sent": False, "error": warning_email.UNREACHABLE.format(host="h", port=1)})
        self.assertEqual(self.stored(card_id)["warning_email_result"], "Could not reach the mail server h on port 1.")
        self.assertEqual([e["kind"] for e in self.events()], ["warning_email_failed"])
        self.run_hook()
        self.assertEqual(len(self.sent), 1)

    def test_feature_off_sends_and_stamps_nothing(self):
        card_id = self.card()
        self.assertEqual(self.run_hook(), [])
        global_settings.save_warning_email(self.home, {**GOOD, "host": ""}, "")
        self.assertEqual(self.run_hook(), [])
        self.assertEqual(self.sent, [])
        self.assertNotIn("warning_email_at", self.stored(card_id))
        self.assertEqual(self.events(), [])
        self.assertEqual(warning_email.notify_blocked_cards(self.root, None), [])

    def test_a_card_that_is_not_blocked_is_ignored(self):
        global_settings.save_warning_email(self.home, GOOD, PASSWORD)
        card_id = self.card(blocked=False, title="Run this command")
        self.assertEqual(self.run_hook(), [])
        self.assertNotIn("warning_email_at", self.stored(card_id))

    def test_it_works_while_the_project_is_paused(self):
        global_settings.save_warning_email(self.home, GOOD, PASSWORD)
        card_id = self.card()
        with board.locked_state(self.root, allow_paused=True, operation="test pause") as state:
            state["project_pause"] = {"status": "paused", "pause_id": "p1"}
        self.assertEqual(len(self.run_hook()), 1)
        self.assertEqual(board.pause_state(self.root)["status"], "paused")
        self.assertEqual(self.stored(card_id)["warning_email_result"], "sent")
        self.assertEqual([e["kind"] for e in self.events()], ["warning_email_sent"])

    def test_the_board_lock_is_not_held_while_sending(self):
        global_settings.save_warning_email(self.home, GOOD, PASSWORD)
        self.card()
        seen = []

        def fake(settings, password, subject, body):
            with board.locked_state(self.root, allow_paused=True, operation="probe"):
                seen.append("lock was free")
            return {"sent": True, "error": ""}
        with mock.patch.object(warning_email, "send_message", fake):
            warning_email.notify_blocked_cards(self.root, self.home)
        self.assertEqual(seen, ["lock was free"])

    def test_the_watchdog_calls_the_hook_after_self_heal(self):
        from harness import project_worker
        order = []
        watchdog = project_worker.ProjectWatchdog(self.root)
        watchdog.self_heal = lambda: order.append("heal") or []
        watchdog.warn = lambda: order.append("warn")
        with mock.patch.object(project_worker.control_plane, "tick", return_value={"status": "active"}), \
                mock.patch.object(project_worker.cost_center, "collect_project"):
            watchdog.tick()
        self.assertEqual(order, ["heal", "warn"])


class SettingsRoutes(SettingsCase):
    def setUp(self):
        super().setUp()
        require_loopback()
        self.manager = project_manager.ProjectManager(self.home, board_port=0)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(self.manager))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def post(self, path, body, headers=None):
        request = Request(self.url + path, method="POST", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b"{}")
        except HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_save_then_read_back_without_the_password(self):
        status, payload = self.post("/api/settings/warning-email", {**GOOD, "password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(payload["warning_email"]["to"], GOOD["to"])
        self.assertTrue(payload["warning_email"]["password_saved"])
        with urlopen(self.url + "/api/settings", timeout=15) as response:
            raw = response.read().decode()
        self.assertNotIn(PASSWORD, raw)
        self.assertNotIn(PASSWORD, status and json.dumps(payload))
        self.assertTrue(json.loads(raw)["warning_email"]["password_saved"])

    def test_a_bad_field_is_a_plain_400(self):
        status, payload = self.post("/api/settings/warning-email", {**GOOD, "to": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("email address", payload["error"])

    def test_the_test_route_reports_the_plain_result(self):
        self.post("/api/settings/warning-email", {**GOOD, "password": PASSWORD})
        with mock.patch.object(warning_email, "send_message", return_value={"sent": True, "error": ""}) as send:
            status, payload = self.post("/api/settings/warning-email/test", {})
        self.assertEqual((status, payload), (200, {"sent": True, "error": ""}))
        self.assertIn("test from NoMoreHappyPath", send.call_args[0][3])

    def test_both_routes_refuse_a_foreign_request_like_the_other_settings_routes(self):
        for headers in ({"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            with mock.patch.object(warning_email.smtplib, "SMTP", side_effect=AssertionError("connected")):
                status, _ = self.post("/api/settings/warning-email", {**GOOD, "password": PASSWORD}, headers)
                self.assertEqual(status, 403)
                status, _ = self.post("/api/settings/warning-email/test", {}, headers)
                self.assertEqual(status, 403)
        request = Request(self.url + "/api/settings/warning-email", method="POST", data=b"to=a",
                          headers={"Content-Type": "text/plain"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=15)
        self.assertEqual(caught.exception.code, 403)
        self.assertFalse(global_settings.warning_email_status(self.home)["on"], "nothing was saved")


if __name__ == "__main__":
    unittest.main()


class ThePausedLineIsOnlySaidWhenTrue(unittest.TestCase):
    def test_a_card_raised_without_a_pause_does_not_claim_the_project_is_paused(self):
        card = {"title": "Blocked, needs you: the CTO cannot solve this on its own", "why": "Problem: x\nWhat to do: y"}
        self.assertIn(warning_email.PAUSED_LINE, warning_email.blocked_message("Weather", card)[1])
        self.assertNotIn(warning_email.PAUSED_LINE, warning_email.blocked_message("Weather", card, paused=False)[1])

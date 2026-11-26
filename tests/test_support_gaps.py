# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner 2026-11-25: an error that returns after Deploy is told, the version is real, an app that is down is noticed."""
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import support_mode as sm
from tests.environment_support import require_loopback
from tests.test_support_mode import _error_event

ROOT = Path(__file__).resolve().parents[1]


class Mail:
    """Records the messages Support would send; answer is what the mail server says."""

    def __init__(self):
        """Start with no message and a mail server that accepts."""
        self.subjects, self.answer = [], {"sent": True}

    def __call__(self, settings, password, subject, body):
        """Takes one message; returns the mail server's answer."""
        if self.answer["sent"]:
            self.subjects.append(subject)
        return dict(self.answer)


class SupportGaps(unittest.TestCase):
    def setUp(self):
        """A private store with a notice address and a recorded mail server."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.store = sm.CaseStore(self.home)
        self.addCleanup(lambda: self.store.close())
        self.store.save_settings(notice_email="owner@example.com")
        self.mail = Mail()
        patch = mock.patch.object(sm.warning_email, "send_message", self.mail)
        patch.start()
        self.addCleanup(patch.stop)

    def report(self, number, version):
        """Takes the report's number and version; sends it; returns the case."""
        sm.ingest(self.store, _error_event(id=f"e{number}", version=version))
        return self.store.get_case("cafebabe00000001")

    def test_an_error_that_returns_after_deploy_is_told_once(self):
        self.report(1, "v1")
        self.store.set_deployed("cafebabe00000001")
        self.assertEqual(self.report(2, "v1")["status"], "deployed", "the old copy may still run for a while")
        case = self.report(3, "v2")
        self.assertEqual((case["status"], case["version"], case["count"]), ("came_back", "v2", 3))
        self.report(4, "v2")
        self.assertEqual(sum("Came back after Deploy" in subject for subject in self.mail.subjects), 1)
        self.assertIn("Came back after Deploy</strong>, on version v2", sm._render_case_card(case, "t", True))

    def test_the_same_version_an_hour_after_deploy_is_a_return(self):
        self.report(1, "1.0")
        self.store.set_deployed("cafebabe00000001")
        with mock.patch.object(sm, "DEPLOY_SETTLE_SECONDS", -5):
            self.assertEqual(self.report(2, "1.0")["status"], "came_back")

    def test_a_case_that_was_not_deployed_only_counts(self):
        self.report(1, "v1")
        status = self.store.get_case("cafebabe00000001")["status"]
        case = self.report(2, "v2")
        self.assertEqual((case["status"], case["version"], case["count"]), (status, "v1", 2))

    def test_down_and_back_are_told_once_each_and_survive_a_restart(self):
        self.store.save_settings(notice_email="owner@example.com", app_address="http://app.test")
        self.assertIn("not been checked", sm._app_watch_line(self.store))
        answer = {"why": ""}
        check = lambda: sm.check_app(self.store, lambda address: answer["why"])
        check()
        self.assertIn("is answering (noticed", sm._app_watch_line(self.store))
        answer["why"] = "connection refused"
        check()
        self.assertEqual(self.mail.subjects, [], "one lost answer is not down")
        check()
        check()
        self.assertIn("NOT ANSWERING", sm._app_watch_line(self.store))
        self.store.close()
        self.store = sm.CaseStore(self.home)
        check()
        self.assertEqual(len(self.mail.subjects), 1)
        answer["why"] = ""
        check()
        check()
        self.assertEqual([("not answering" in s, "answering again" in s) for s in self.mail.subjects], [(True, False), (False, True)])

    def test_a_message_that_could_not_be_sent_is_tried_again(self):
        self.store.save_settings(notice_email="owner@example.com", app_address="http://app.test")
        self.mail.answer = {"sent": False}
        for _ in range(3):
            sm.check_app(self.store, lambda address: "no answer")
        self.mail.answer = {"sent": True}
        sm.check_app(self.store, lambda address: "no answer")
        self.assertEqual(len(self.mail.subjects), 1)

    def test_a_way_back_message_that_could_not_be_sent_is_tried_again_after_a_restart(self):
        self.store.save_settings(notice_email="owner@example.com", app_address="http://app.test")
        for _ in range(2):
            sm.check_app(self.store, lambda address: "no answer")
        self.mail.answer = {"sent": False}
        sm.check_app(self.store, lambda address: "")
        self.store.close()
        self.store = sm.CaseStore(self.home)
        self.mail.answer = {"sent": True}
        for _ in range(3):
            sm.check_app(self.store, lambda address: "")
        self.assertEqual([("not answering" in s, "answering again" in s) for s in self.mail.subjects], [(True, False), (False, True)])

    def test_no_address_means_no_check(self):
        sm.check_app(self.store, lambda address: self.fail("nothing may be asked"))
        self.assertIn("No app address", sm._app_watch_line(self.store))

    def test_a_real_server_answering_failing_and_stopped(self):
        require_loopback()
        state = {"code": 200}

        class App(BaseHTTPRequestHandler):
            def do_GET(self):
                """Answer with the code the test chose."""
                self.send_response(state["code"])
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                """Say nothing."""

        server = ThreadingHTTPServer(("127.0.0.1", 0), App)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        address = f"http://127.0.0.1:{server.server_address[1]}/"
        self.assertEqual(sm.ask_app(address), "")
        state["code"] = 401
        self.assertEqual(sm.ask_app(address), "", "a sign-in page is an answer")
        state["code"] = 503
        self.assertEqual(sm.ask_app(address), "it answered 503")
        server.shutdown()
        server.server_close()
        self.assertTrue(sm.ask_app(address))

    def test_a_store_from_the_release_before_keeps_its_rows(self):
        home = self.home / "old"
        sm.CaseStore(home).close()
        with sqlite3.connect(home / sm.DB_FILENAME) as connection:
            connection.execute("ALTER TABLE cases DROP COLUMN deployed_at") if sqlite3.sqlite_version_info >= (3, 35) else self.skipTest("this SQLite cannot drop a column")
            connection.execute("ALTER TABLE settings DROP COLUMN app_address")
            connection.execute("DROP TABLE app_watch")
            connection.execute("INSERT INTO settings (id, app_repo) VALUES (1, 'owner/app')")
        store = sm.CaseStore(home)
        self.addCleanup(store.close)
        self.assertEqual((store.settings()["app_repo"], store.settings()["app_address"]), ("owner/app", ""))

    def test_the_rules_ask_for_the_real_version(self):
        contract = (ROOT / "directives" / "ERROR_SINK_CONTRACT.md").read_text(encoding="utf-8")
        self.assertIn("never a number typed in the code", contract)
        self.assertIn("is the commit it runs from", contract)
        self.assertIn("never typed in the", (ROOT / "directives" / "AGENT.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

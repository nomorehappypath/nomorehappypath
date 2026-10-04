# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Only this app's own pages may act on it, and no page may be framed.

Security scan 2026-10-04 (OpenAI Codex Security on v0.7.2):
- finding 3: the board worker accepted cross-origin loopback mutations with no
  Origin, Host or media-type check (any web page could post to it);
- finding 4: no page refused to be framed;
- finding 5: View app's window kept a live link (opener) to Mission Control.
"""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board_viewer, control, project_manager, web_guard
from tests.environment_support import require_loopback


class RuleTests(unittest.TestCase):
    def refusal(self, method, **headers):
        defaults = {"Host": "127.0.0.1:8741"}
        defaults.update({key.replace("_", "-"): value for key, value in headers.items()})
        return web_guard.request_refusal(method, defaults)

    def test_loopback_names_are_accepted_and_any_other_name_is_refused(self):
        for host in ("127.0.0.1:8741", "localhost:8741", "[::1]:8741"):
            self.assertEqual(self.refusal("GET", Host=host), "", host)
        self.assertIn("own address", self.refusal("GET", Host="attacker.example:8741"),
                      "a page that re-points its own name at 127.0.0.1 is refused")

    def test_a_server_bound_to_every_address_accepts_ip_literals_but_never_names(self):
        self.assertEqual(web_guard.request_refusal("GET", {"Host": "192.168.1.20:8741"}, "0.0.0.0"), "")
        self.assertNotEqual(web_guard.request_refusal("GET", {"Host": "attacker.example:8741"}, "0.0.0.0"), "")

    def test_a_browser_mutation_must_be_same_origin(self):
        self.assertEqual(self.refusal("POST", Origin="http://127.0.0.1:8741", Content_Type="application/json", Content_Length="2"), "")
        self.assertIn("same-origin", self.refusal("POST", Origin="http://attacker.example", Content_Type="application/json", Content_Length="2"))
        self.assertIn("same-origin", self.refusal("POST", Sec_Fetch_Site="cross-site", Content_Type="application/json", Content_Length="2"))

    def test_a_body_must_be_json_or_a_form_upload(self):
        self.assertIn("JSON", self.refusal("POST", Content_Type="text/plain", Content_Length="2"))
        self.assertEqual(self.refusal("POST", Content_Type="multipart/form-data; boundary=x", Content_Length="2"), "")

    def test_a_local_tool_that_sends_no_origin_is_unaffected(self):
        self.assertEqual(self.refusal("POST", Content_Type="application/json", Content_Length="2"), "")


class ServedTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        control.initialize(self.root)

    def serve(self, handler) -> int:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def request(self, port, method, path, *, headers=None, body=b""):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.putrequest(method, path, skip_host=True)
        for name, value in {"Host": f"127.0.0.1:{port}", **(headers or {})}.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def test_mission_control_refuses_a_cross_origin_post_and_changes_nothing(self):
        port = self.serve(board_viewer.make_handler(self.root))
        session = control.create(self.root, "claude_reviewer")
        status, _, body = self.request(port, "POST", "/api/sessions/stop-all",
                                       headers={"Origin": "http://attacker.example", "Content-Type": "text/plain"}, body=b"{}")
        self.assertEqual(status, 403, body)
        self.assertEqual(control.snapshot(self.root)["sessions"][0]["id"], session["id"])
        self.assertNotEqual(control.snapshot(self.root)["sessions"][0]["status"], "stopped", "nothing was stopped")
        status, _, _ = self.request(port, "POST", "/api/sessions/stop-all",
                                    headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}, body=b"{}")
        self.assertEqual(status, 200, "the page's own request still works")

    def test_mission_control_refuses_a_rebound_host_even_for_reading(self):
        port = self.serve(board_viewer.make_handler(self.root))
        status, _, _ = self.request(port, "GET", "/api/board", headers={"Host": f"attacker.example:{port}"})
        self.assertEqual(status, 403)
        self.assertEqual(self.request(port, "GET", "/api/board")[0], 200)

    def test_every_page_refuses_to_be_framed(self):
        viewer = self.serve(board_viewer.make_handler(self.root))
        manager = self.serve(project_manager.make_handler(project_manager.ProjectManager(self.root / "home", board_port=0)))
        for port, path in ((viewer, "/"), (viewer, "/api/board"), (manager, "/"), (manager, "/api/projects")):
            status, headers, _ = self.request(port, "GET", path)
            self.assertEqual(headers.get("X-Frame-Options"), "DENY", (port, path, status))
            self.assertIn("frame-ancestors 'none'", headers.get("Content-Security-Policy", ""), (port, path))

    def test_the_manager_refuses_a_rebound_host_and_a_cross_origin_mutation(self):
        port = self.serve(project_manager.make_handler(project_manager.ProjectManager(self.root / "home", board_port=0)))
        self.assertEqual(self.request(port, "GET", "/api/projects", headers={"Host": f"attacker.example:{port}"})[0], 403)
        status, _, _ = self.request(port, "POST", "/api/projects", headers={"Origin": "http://attacker.example",
                                    "Content-Type": "application/json"}, body=json.dumps({"name": "x"}).encode())
        self.assertEqual(status, 403)


class ViewAppTests(unittest.TestCase):
    def test_the_view_app_window_is_cut_off_from_mission_control(self):
        page = board_viewer.INDEX_HTML if hasattr(board_viewer, "INDEX_HTML") else Path(board_viewer.__file__).read_text()
        self.assertIn("tab.opener=null", page)
        self.assertIn("window.open(outcome.url,'_blank','noopener')", page)


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The harness renders an agent's own local page (owner 2026-09-29, option 1).

Agents' browser tools refuse localhost inside their sandbox. The worker runs
outside it, so an agent asks the board: `screen-check --url ... --expect ...`.
These tests drive that through the real authenticated command surface and the
real harness browser, against a page whose text only exists after its script
runs.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import board, browser_acceptance, control
from harness.board_surface import (
    PROTOCOL_VERSION, CommandGateway, SessionTokenAuthority, SurfaceAuthorizationError, SurfaceProtocolError,
)
from harness.project_context import ProjectContext
from tests.environment_support import require_loopback

APP_PAGE = b"""<!doctype html><html><head><title>Studio</title></head><body>
<h1>Studio</h1><div style="display:contents"><div id="out">loading</div></div>
<button hidden>Accept</button><div style="display:none">Secret panel</div><p style="visibility:hidden">Ghost note</p>
<span style="opacity:0">Faded tip</span><span style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">Reader only</span><div style="position:absolute;left:-9999px">Parked away</div>
<script>setTimeout(()=>{document.querySelector('#out').textContent='Deliverables ready: 3 films';},300)</script>
</body></html>"""
# Review r1: a page whose only text is its title or hidden markup renders blank.
BLANK_PAGE = b"""<!doctype html><html><head><title>Only a title</title></head><body><button hidden>Accept</button></body></html>"""


class ScreenCheckCommandTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(self.context)
        self.authority = SessionTokenAuthority(self.context)
        self.gateway = CommandGateway(self.context, self.authority)

        class App(BaseHTTPRequestHandler):
            def log_message(self, *_): return
            def do_GET(self):
                page = BLANK_PAGE if self.path.startswith("/blank") else APP_PAGE
                self.send_response(200); self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page)

        self.app = ThreadingHTTPServer(("127.0.0.1", 0), App)
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.addCleanup(self.app.server_close)
        self.addCleanup(self.app.shutdown)
        self.url = f"http://127.0.0.1:{self.app.server_address[1]}/deliverables"

    def agent(self, role):
        kind = {"engineering": "codex_delivery", "qa": "claude_reviewer", "cto": "claude_cto"}[role]
        session = control.create(self.context, kind)
        self.authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())
        agent = board.register(self.context, role, board.AWAITING_OWNER_DIRECTION, session_id=session["id"])
        self.agent_ids = getattr(self, "agent_ids", {})
        token = self.authority.claim(session["id"], os.getpid())
        self.agent_ids[token] = agent["id"]
        return agent, token

    def ask(self, token, *arguments):
        return self.gateway.execute(token, {
            "protocol": PROTOCOL_VERSION, "nonce": time.time_ns(),
            "operation": "screen-check",
            "arguments": ["screen-check", "--agent", self.agent_ids[token], *arguments],
        })

    def test_agent_gets_the_rendered_page_and_the_reviewer_gets_saved_evidence(self):
        for role in ("engineering", "qa"):
            with self.subTest(role=role):
                agent, token = self.agent(role)
                result = self.ask(token, "--url", self.url, "--expect", "deliverables ready")["result"]
                self.assertIn("Deliverables ready: 3 films", result["visible_text"], "scripts ran before the capture")
                self.assertNotIn("loading", result["visible_text"])
                self.assertTrue(result["expect_found"])
                self.assertFalse(result["blank"])
                event = [e for e in board.snapshot(self.context)["events"] if e.get("kind") == "screen_check"][-1]
                self.assertEqual((event["agent_id"], event["check_id"]), (agent["id"], result["id"]))
                for kind, item in event["evidence"].items():
                    data = Path(item["path"]).read_bytes()
                    self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"], kind)
                self.assertTrue(Path(event["evidence"]["screenshot"]["path"]).read_bytes().startswith(b"\x89PNG"))
        # Review r1: only what a person sees counts - hidden, invisible and head text never do.
        for hidden in ("Accept", "Secret panel", "Ghost note", "Studio Studio", "Faded tip", "Reader only", "Parked away"):
            self.assertNotIn(hidden, self.ask(token, "--url", self.url)["result"]["visible_text"])
        self.assertFalse(self.ask(token, "--url", self.url, "--expect", "Accept")["result"]["expect_found"])
        blank = self.ask(token, "--url", self.url.replace("/deliverables", "/blank"), "--expect", "Accept")["result"]
        self.assertEqual((blank["blank"], blank["expect_found"], blank["visible_text"]), (True, False, ""))

    def test_refusals_happen_before_any_browser_starts(self):
        _agent, token = self.agent("engineering")
        self.gateway.served_ports.add(self.app.server_address[1])
        with mock.patch.object(browser_acceptance, "launch", side_effect=AssertionError("browser started")):
            with self.assertRaisesRegex(SurfaceAuthorizationError, "own board"):
                self.ask(token, "--url", self.url)
            for url in (
                "https://example.com/", "http://192.168.1.10:8900/", "file:///etc/passwd",
                "http://127.0.0.1:8740/", "http://127.0.0.1:8741/", "http://127.0.0.1:8742/",
            ):
                with self.subTest(url=url), self.assertRaises(SurfaceProtocolError):
                    self.ask(token, "--url", url)
            _cto, cto_token = self.agent("cto")
            with self.assertRaises(SurfaceAuthorizationError):
                self.ask(cto_token, "--url", self.url)

    def test_a_browser_failure_is_a_plain_error(self):
        _agent, token = self.agent("engineering")
        with mock.patch.object(browser_acceptance, "render_page", side_effect=RuntimeError("browser capture failed: crashed")):
            with self.assertRaisesRegex(SurfaceProtocolError, "harness browser could not render .*crashed"):
                self.ask(token, "--url", self.url)


if __name__ == "__main__":
    unittest.main()

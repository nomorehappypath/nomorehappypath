# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Single-origin Projects app routing and isolation acceptance tests."""
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
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from harness import project_chat, project_manager, project_registry as registry
from tests.chat_key_support import configure_verified_key
from tests.test_project_chat_ui import FakeOpenAI
from tests.test_project_manager_rendered import chrome_binary
from tests.environment_support import require_loopback


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ProjectManagerSingleOriginTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def test_landing_open_view_and_chat_stay_on_next_app_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "manager"
            code = base / "adopted"
            code.mkdir()
            configure_verified_key(home)
            entry = registry.register(
                home, "Single origin", code, kind="adopted",
                description="Single-origin project facts.",
            )
            manager = project_manager.ProjectManager(home, board_port=free_port())
            server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            manager.manager_url = origin + "/"
            manager.public_board_url = origin + project_manager.PROJECT_ROUTE
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            api = FakeOpenAI()
            try:
                with patch.dict("os.environ", api.environment()):
                    request = Request(
                        origin + f"/api/projects/{entry['id']}/open",
                        data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
                    )
                    opened = json.loads(urlopen(request, timeout=10).read())
                self.assertEqual(opened["board_url"], origin + project_manager.PROJECT_ROUTE)
                self.assertEqual(urlparse(opened["board_url"]).port, server.server_address[1])

                page = urlopen(opened["board_url"], timeout=5).read().decode()
                self.assertIn('const apiPrefix="/project";', page)
                self.assertIn('id="project-chat"', page)
                self.assertIn('id="project-navigation"', page)
                self.assertIn(f'id="project-nav-projects" href="{origin}/"', page)
                self.assertIn(f'href="{origin}/project/" aria-current="page">Mission Control</a>', page)
                self.assertIn(f'href="{origin}/?page=settings">Settings</a>', page)
                self.assertIn(f'href="{origin}/?page=help">Help</a>', page)
                self.assertNotIn("&larr; All projects", page)
                self.assertNotIn(f"127.0.0.1:{manager.board_port}", page)
                self.assertNotIn(manager.worker_proxy_token, page)
                with self.assertRaises(HTTPError) as direct:
                    urlopen(f"http://127.0.0.1:{manager.board_port}/", timeout=3)
                self.assertEqual(direct.exception.code, 403)
                direct_command = Request(
                    f"http://127.0.0.1:{manager.board_port}/api/board/command",
                    data=b"{}", headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as unauthenticated_command:
                    urlopen(direct_command, timeout=3)
                self.assertEqual(unauthenticated_command.exception.code, 401)
                token = re.search(r'id="project-chat" data-action-token="([^"]+)"', page).group(1)

                dashboard = json.loads(urlopen(
                    origin + project_manager.PROJECT_ROUTE + "api/dashboard", timeout=5,
                ).read())
                self.assertNotIn(str(code), json.dumps(dashboard))
                projects = json.loads(urlopen(origin + "/api/projects", timeout=5).read())
                self.assertNotIn("board_port", projects)
                active = next(row for row in projects["projects"] if row["id"] == entry["id"])
                self.assertEqual(active["board_url"], opened["board_url"])

                def chat(question, request_id, request_origin=origin):
                    chat_request = Request(
                        origin + project_manager.PROJECT_ROUTE + "api/project-chat",
                        data=json.dumps({"request_id": request_id, "question": question}).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "Origin": request_origin,
                            "Sec-Fetch-Site": "same-origin",
                            "X-Harness-Chat-Action": token,
                        },
                        method="POST",
                    )
                    try:
                        with urlopen(chat_request, timeout=10) as response:
                            return response.status, json.loads(response.read())
                    except HTTPError as error:
                        return error.code, json.loads(error.read())

                status, answer = chat(
                    "What is this project about?", "single-origin-answer-0001",
                )
                self.assertEqual(status, 200, answer)
                self.assertEqual(answer["answer"], "Single-origin project facts.")
                # Key project questions answer deterministically: no provider call.
                self.assertEqual(len(api.requests), 0)
                status, refused = chat(
                    "What is the capital of France?", "single-origin-unknown-0001",
                )
                self.assertEqual(status, 200, refused)
                self.assertEqual(refused["answer"], project_chat.REFUSAL_ANSWER)
                # The scope classifier is one bounded provider call by design.
                self.assertEqual(len(api.requests), 1)

                refused, error = chat(
                    "What is this project about?", "single-origin-cross-site-1",
                    "http://evil.example",
                )
                self.assertEqual(refused, 403)
                self.assertIn("same-origin", error["error"])
            finally:
                manager.close_project(entry["id"])
                api.close()
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()

    def test_real_browser_card_click_navigates_to_next_app_project_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "manager"
            code = base / "adopted"
            code.mkdir()
            configure_verified_key(home)
            entry = registry.register(
                home, "Browser route", code, kind="adopted",
                description="Browser navigation proof.",
            )
            manager = project_manager.ProjectManager(home, board_port=free_port())
            server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            manager.manager_url = origin + "/"
            manager.public_board_url = origin + project_manager.PROJECT_ROUTE
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            api = FakeOpenAI()
            debug_port = free_port()
            auto_click = """
<script>
(async()=>{
  const pause=delay=>new Promise(resolve=>setTimeout(resolve,delay));
  for(let attempt=0;attempt<500;attempt++){
    const open=document.querySelector('[data-act=open]');
    if(open){open.click();return;}
    await pause(50);
  }
})();
</script>
"""
            page = project_manager.PAGE.replace("</body>", auto_click + "</body>", 1)
            original_projects_payload = manager.projects_payload
            projects_payload_calls = 0

            def delayed_projects_payload():
                nonlocal projects_payload_calls
                projects_payload_calls += 1
                if projects_payload_calls == 1:
                    # The governed runner is measurably slower than a direct
                    # shell. Keep this beyond the former five-second click
                    # window so the regression proves the browser waits for
                    # readiness instead of sampling the landing URL early.
                    time.sleep(5.2)
                return original_projects_payload()

            profile = tempfile.TemporaryDirectory()
            process = None
            try:
                with (
                    patch.object(project_manager, "PAGE", page),
                    patch.object(manager, "projects_payload", side_effect=delayed_projects_payload),
                    patch.dict(os.environ, api.environment(), clear=False),
                ):
                    process = subprocess.Popen(
                        [
                            chrome_binary(), "--headless=new", "--disable-gpu",
                            "--no-first-run", "--no-default-browser-check",
                            "--disable-extensions", f"--user-data-dir={profile.name}",
                            f"--remote-debugging-port={debug_port}", origin + "/",
                        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    observed = ""
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        try:
                            tabs = json.loads(urlopen(
                                f"http://127.0.0.1:{debug_port}/json", timeout=0.3,
                            ).read())
                            observed = next(
                                (tab.get("url", "") for tab in tabs if tab.get("type") == "page"),
                                "",
                            )
                            if observed == origin + project_manager.PROJECT_ROUTE:
                                break
                        except (OSError, ValueError, TypeError):
                            pass
                        time.sleep(0.1)
                    self.assertEqual(
                        observed,
                        origin + project_manager.PROJECT_ROUTE,
                        "the project card never became ready or its click did not navigate",
                    )
                    project_page = urlopen(observed, timeout=3).read().decode()
                    self.assertIn('id="project-chat"', project_page)
                    self.assertIn('const apiPrefix="/project";', project_page)
                    self.assertNotIn(f"127.0.0.1:{manager.board_port}", observed)
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                profile.cleanup()
                manager.close_project(entry["id"])
                api.close()
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()


if __name__ == "__main__":
    unittest.main()

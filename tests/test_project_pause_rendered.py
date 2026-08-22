# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Real-browser acceptance for paused project and board truth."""
from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, browser_acceptance, control, project_manager
from harness import project_registry as registry
from tests.test_project_manager_rendered import proxy_handler
from tests.environment_support import require_loopback


PAUSED_CARD_SCRIPT = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (document.querySelector('#projects .project')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const card = document.querySelector('#projects .project');
  await fetch('/__layout_result__', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      cardClass: card.className,
      badges: Array.from(card.querySelectorAll('.badge')).map(node => node.textContent.trim()),
      progress: card.querySelector('.progress').textContent.trim(),
      actions: card.querySelector('.actions').textContent.trim(),
    }),
  });
})();
</script>
"""


PAUSED_BOARD_SCRIPT = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    const banner = document.querySelector('#paused-banner');
    if (banner && !banner.hidden) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const banner = document.querySelector('#paused-banner');
  const disabled = Array.from(document.querySelectorAll('button:disabled'));
  const enabled = Array.from(document.querySelectorAll('button:not(:disabled)'));
  await fetch('/__layout_result__', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      bannerHidden: banner.hidden,
      bannerText: banner.textContent.trim(),
      resumeText: document.querySelector('#resume-project').textContent.trim(),
      resumeHref: document.querySelector('#resume-project').href,
      bodyClass: document.body.className,
      disabledCount: disabled.length,
      disabledWithoutReason: disabled.filter(button => !/paused/i.test(button.title)).map(button => button.textContent.trim()),
      enabledButtons: enabled.map(button => button.textContent.trim()),
      agentsText: document.querySelector('#agents').textContent.trim(),
      queueText: document.querySelector('#queue').textContent.trim(),
    }),
  });
})();
</script>
"""


PAUSE_ACTION_SCRIPT = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (document.querySelector('[data-act="pause"]')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const pause = document.querySelector('[data-act="pause"]');
  pause.click();
  for (let attempt = 0; attempt < 80; attempt++) {
    if (Array.from(document.querySelectorAll('.badge')).some(node => node.textContent.trim() === 'Paused')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  await fetch('/__layout_result__', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      badgeText: Array.from(document.querySelectorAll('.badge')).map(node => node.textContent.trim()),
      notice: document.querySelector('#status').textContent.trim(),
      actions: document.querySelector('.actions').textContent.trim(),
    }),
  });
})();
</script>
"""


RESUME_ACTION_SCRIPT = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (document.querySelector('[data-act="open"]')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  document.querySelector('[data-act="open"]').click();
  for (let attempt = 0; attempt < 120; attempt++) {
    if (/Project ready|Project resumed/.test(document.querySelector('#status').textContent)) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  await fetch('/__layout_result__', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({notice: document.querySelector('#status').textContent.trim()}),
  });
})();
</script>
"""


class RenderedPauseTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _render(self, origin: str, script: str) -> dict:
        sink: dict = {}
        proxy = ThreadingHTTPServer(
            ("127.0.0.1", 0), proxy_handler(origin, sink, script),
        )
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        profile = tempfile.TemporaryDirectory()
        process = browser_acceptance.launch(
            f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name),
            width=1400, height=1000,
        )
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
            self.assertIn("value", sink, "Chrome did not report paused UI state")
            return sink["value"]
        finally:
            process.close()
            profile.cleanup()
            proxy.shutdown()
            thread.join(timeout=3)
            proxy.server_close()

    def test_projects_page_plainly_renders_paused_row(self):
        home = self.base / "manager-home"
        code = self.base / "code"
        code.mkdir()
        entry = registry.register(home, "Paused Project", code)
        context = registry.context_for_entry(entry)
        board.begin_project_pause(context, drain_seconds=0)
        board.finish_project_pause(context)
        manager = project_manager.ProjectManager(home, board_port=0)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), project_manager.make_handler(manager),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            rendered = self._render(
                f"http://127.0.0.1:{server.server_address[1]}", PAUSED_CARD_SCRIPT,
            )
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

        self.assertIn("paused", rendered["cardClass"].split())
        self.assertEqual(rendered["badges"].count("Paused"), 1)
        self.assertIn("Paused safely", rendered["progress"])
        self.assertIn("Open project", rendered["actions"])
        self.assertNotIn("View paused board", rendered["actions"])

    def test_projects_pause_button_executes_real_pause_and_renders_result(self):
        home = self.base / "pause-action-home"
        code = self.base / "pause-action-code"
        code.mkdir()
        entry = registry.register(home, "Active Project", code)
        context = registry.context_for_entry(entry)
        registry.activate(home, entry["id"])
        manager = project_manager.ProjectManager(home, board_port=0)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), project_manager.make_handler(manager),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            rendered = self._render(
                f"http://127.0.0.1:{server.server_address[1]}", PAUSE_ACTION_SCRIPT,
            )
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

        self.assertEqual(rendered["badgeText"].count("Paused"), 1, rendered)
        self.assertIn("paused safely", rendered["notice"].casefold())
        self.assertIn("Open project", rendered["actions"])
        self.assertNotIn("Pause project", rendered["actions"])
        self.assertEqual(board.pause_state(context)["status"], "paused")

    def test_projects_resume_button_opens_a_real_verified_board_worker(self):
        home = self.base / "resume-action-home"
        code = self.base / "resume-action-code"
        code.mkdir()
        entry = registry.register(home, "Resume Project", code)
        context = registry.context_for_entry(entry)
        board.begin_project_pause(context, drain_seconds=0)
        board.finish_project_pause(context)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            board_port = probe.getsockname()[1]
        manager = project_manager.ProjectManager(
            home, board_port=board_port, worker_start_timeout=5,
        )
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), project_manager.make_handler(manager),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            rendered = self._render(
                f"http://127.0.0.1:{server.server_address[1]}", RESUME_ACTION_SCRIPT,
            )
            with urlopen(f"http://127.0.0.1:{board_port}/api/ready", timeout=3) as response:
                readiness = json.loads(response.read())
        finally:
            manager.close_project(entry["id"])
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

        self.assertIn("Project ready", rendered["notice"])
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["project_name"], "Resume Project")
        self.assertEqual(board.pause_state(context)["status"], "active")

    def test_paused_board_renders_banner_resume_and_only_read_only_controls(self):
        root = self.base / "paused-board"
        root.mkdir()
        session = control.create(root, "claude_reviewer")
        reviewer = board.register(
            root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"],
        )
        with board.locked_state(root) as state:
            state["qa_requests"]["review-paused-ui"] = {
                "id": "review-paused-ui", "task": "T", "phase": "subtask_acceptance",
                "subtask": "paused-ux", "chunk": "subtask-final", "cycle": 1,
                "status": "claimed", "result": None, "developer_id": "delivery",
                "claimed_by": reviewer["id"], "reserved_by": reviewer["id"],
                "requested_at": board.now(), "review_wait_started_at": board.now(),
                "challenge_ledger": "/durable/challenge.md", "route_state": "review_executing",
            }
        board.begin_project_pause(root, drain_seconds=0)
        board.finish_project_pause(root)
        control.pause_sessions(root, [session["id"]], timeout=0)
        manager_url = "http://127.0.0.1:8740/"
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            board_viewer.make_handler(root, project_name="Paused Project", manager_url=manager_url),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            rendered = self._render(
                f"http://127.0.0.1:{server.server_address[1]}", PAUSED_BOARD_SCRIPT,
            )
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

        self.assertFalse(rendered["bannerHidden"])
        self.assertIn("Project paused", rendered["bannerText"])
        self.assertIn("read-only", rendered["bannerText"])
        self.assertEqual(rendered["resumeText"], "Resume project")
        self.assertEqual(rendered["resumeHref"], manager_url)
        self.assertIn("project-paused", rendered["bodyClass"].split())
        self.assertGreater(rendered["disabledCount"], 0)
        self.assertEqual(rendered["disabledWithoutReason"], [])
        self.assertEqual(rendered["enabledButtons"], [])
        self.assertIn("PAUSED", rendered["agentsText"])
        self.assertIn("intentionally paused", rendered["agentsText"])
        self.assertIn("PAUSED — REVIEWER PRESERVED", rendered["queueText"])
        refusal_events = [
            event for event in board.snapshot(root)["events"]
            if event["kind"] == "project_paused_write_refused"
        ]
        self.assertEqual(refusal_events, [], "read-only refreshes must not attempt hidden writes")

    def test_paused_board_http_refuses_control_mutation_with_plain_reason(self):
        root = self.base / "paused-api"
        root.mkdir()
        board.begin_project_pause(root, drain_seconds=0)
        board.finish_project_pause(root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            request = Request(
                base + "/api/sessions", method="POST",
                data=json.dumps({"kind": "codex_delivery"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 409)
            message = json.loads(raised.exception.read())["error"]
            self.assertIn("paused", message.casefold())
            self.assertIn("Resume", message)
            self.assertEqual(control.snapshot(root)["sessions"], [])
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Backlog #4 (2026-09-27): a requirements proposal waiting for the owner's
Go ahead / Modify was counted nowhere and flagged nowhere; the owner, told to
click Modify, could not find the button. It is now counted on the project
list and named in a top-level "Your decision needed" banner on the board.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, control, project_manager, project_memory
from harness import project_registry as registry

PROPOSAL = "Build the content plan with three pillars and a publishing calendar."


def _state_with(proposal_status: str | None, confirmed: bool = False) -> dict:
    state = {
        "task_owner_directions": {"CONTENT-PLAN": "Plan the content."},
        "agents": {"engineering-1": {"id": "engineering-1", "role": "engineering", "active": True,
                                      "task": "CONTENT-PLAN", "session_id": "s1"}},
        "release_decisions": {}, "task_briefs": {}, "events": [],
    }
    if proposal_status:
        state["requirement_proposals"] = {"CONTENT-PLAN": {"task": "CONTENT-PLAN", "status": proposal_status, "text": PROPOSAL}}
    if confirmed:
        state["requirement_confirmations"] = {"CONTENT-PLAN": {"text": PROPOSAL}}
    return state


class ProjectListCountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def status(self, state: dict) -> dict:
        entry = {"data_root": str(self.base / "data"), "code_root": str(self.base), "workspace_root": str(self.base / "ws")}
        board_dir = Path(entry["data_root"]) / "board"
        board_dir.mkdir(parents=True, exist_ok=True)
        (board_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        return project_manager.derive_status(entry)

    def test_a_pending_proposal_is_counted_and_named_as_the_owners_decision(self):
        row = self.status(_state_with("awaiting_owner"))
        self.assertEqual(row["task_counts"]["awaiting_owner"], 1)
        self.assertEqual(row["latest_task"], "CONTENT-PLAN")
        self.assertTrue(row["latest_progress"].startswith("Your decision needed"), row["latest_progress"])
        self.assertIn("Go ahead", row["latest_progress"])

    def test_a_decided_or_confirmed_proposal_is_not_waiting(self):
        for state in (_state_with("accepted"), _state_with("modify_requested"), _state_with("awaiting_owner", confirmed=True), _state_with(None)):
            row = self.status(state)
            self.assertEqual(row["task_counts"]["awaiting_owner"], 0, state.get("requirement_proposals"))
            self.assertFalse(row["latest_progress"].startswith("Your decision needed"))

    def test_a_cancelled_task_with_a_pending_proposal_is_not_waiting(self):
        state = _state_with("awaiting_owner")
        state["cancelled_tasks"] = {"CONTENT-PLAN": {"at": "2026-09-27T10:00:00Z"}}
        row = self.status(state)
        self.assertEqual(row["task_counts"]["awaiting_owner"], 0)
        self.assertFalse(row["latest_progress"].startswith("Your decision needed"), row["latest_progress"])


BOARD_PROBE = r"""
<script>
(async () => {
  const banner = () => document.querySelector('#decision-banner');
  for (let attempt = 0; attempt < 150 && !(banner() && !banner().hidden); attempt++) await new Promise(r => setTimeout(r, 100));
  const section = banner();
  const reading = {bannerVisible: Boolean(section && !section.hidden && section.getBoundingClientRect().height > 0),
                   bannerText: section ? section.textContent.trim() : ''};
  const button = section ? section.querySelector('[data-show-decision]') : null;
  // The board refreshes every 2 s; an unchanged banner must not be rebuilt
  // (a rebuilt live region re-announces itself and drops keyboard focus).
  await new Promise(r => setTimeout(r, 4500));
  reading.bannerKeptAcrossRefreshes = Boolean(button && button.isConnected && section.querySelector('[data-show-decision]') === button);
  const target = () => document.querySelector('.requirements-proposal');
  window.scrollTo(0, 0);
  const scrollers = Array.from(document.querySelectorAll('*')).filter(n => n.scrollTop > 0);
  scrollers.forEach(n => n.scrollTop = 0);
  const before = target() ? target().getBoundingClientRect() : {top: -1};
  reading.initiallyInView = Boolean(target()) && before.top >= 0 && before.top < window.innerHeight;
  if (button) button.click();
  await new Promise(r => setTimeout(r, 900));
  const block = target();
  const rect = block ? block.getBoundingClientRect() : {top: -1, bottom: -1};
  reading.blockInView = Boolean(block) && rect.top >= 0 && rect.top < window.innerHeight;
  reading.blockHasButtons = Boolean(block && block.querySelector('[data-req-go]') && block.querySelector('[data-req-modify]'));
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(reading)});
})();
</script>
"""

CARD_PROBE = r"""
<script>
(async () => {
  const card = () => document.querySelector('.project .metric.waiting-you');
  for (let attempt = 0; attempt < 150 && !card(); attempt++) await new Promise(r => setTimeout(r, 100));
  const node = card();
  const progress = document.querySelector('.project .progress');
  await fetch('/__layout_result__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({waiting: node ? node.textContent.trim() : '', visible: Boolean(node && node.getBoundingClientRect().width > 0),
                          progress: progress ? progress.textContent.trim() : ''})});
})();
</script>
"""


class RenderedDecisionTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        from tests.environment_support import require_loopback
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def browse(self, url: str, sink: dict) -> None:
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(url, Path(profile.name), width=1280, height=900)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()

    def serve(self, handler) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_the_board_names_the_waiting_decision_and_its_button_brings_the_block_into_view(self):
        from tests.test_branding_rendered import probe_proxy
        project_memory.initialize(self.root, project_name="Decision proof", description="Facts.")
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "Plan the content for the spring launch.")
        board.begin_task(self.root, agent["id"], "CONTENT-PLAN")
        board.record_requirement_proposal(self.root, agent["id"], PROPOSAL)
        viewer = self.serve(board_viewer.make_handler(
            self.root, project_name="Decision proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="decision-proof", chat_action_token="owner-token",
        ))
        sink: dict = {}
        self.browse(self.serve(probe_proxy(viewer, sink, BOARD_PROBE)) + "/", sink)
        reading = sink.get("value")
        self.assertIsNotNone(reading, "Chrome reported nothing")
        self.assertTrue(reading["bannerVisible"], json.dumps(reading))
        self.assertIn("Your decision needed", reading["bannerText"])
        self.assertIn("Go ahead", reading["bannerText"])
        self.assertTrue(reading["blockHasButtons"], json.dumps(reading))
        self.assertTrue(reading["bannerKeptAcrossRefreshes"], "the banner was rebuilt on a refresh with nothing changed")
        self.assertFalse(reading["initiallyInView"], "the proof needs the block to start off screen: " + json.dumps(reading))
        self.assertTrue(reading["blockInView"], "the button must bring the Go ahead / Modify block into view")

    def test_the_project_card_shows_what_is_waiting_for_the_owner(self):
        from tests.test_project_manager_rendered import proxy_handler
        home = self.root / "home"; home.mkdir()
        code = self.root / "code"; code.mkdir()
        entry = registry.register(home, "Studio", code, kind="scaffold", description="A studio.")
        board_dir = Path(entry["data_root"]) / "board"; board_dir.mkdir(parents=True, exist_ok=True)
        (board_dir / "state.json").write_text(json.dumps(_state_with("awaiting_owner")), encoding="utf-8")
        manager = project_manager.ProjectManager(home, board_port=0)
        manager_url = self.serve(project_manager.make_handler(manager))
        sink: dict = {}
        self.browse(self.serve(proxy_handler(manager_url, sink, CARD_PROBE)) + "/", sink)
        reading = sink.get("value")
        self.assertIsNotNone(reading, "Chrome reported nothing")
        self.assertEqual(reading["waiting"], "1 waiting for you", json.dumps(reading))
        self.assertTrue(reading["visible"])
        self.assertIn("Your decision needed", reading["progress"])


if __name__ == "__main__":
    unittest.main()

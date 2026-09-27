# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Backlog #3 (2026-09-27): an accepted release kept VISUAL_TEST_REQUIRED for
ever. It now reads ACCEPTED; old boards are upgraded on load; every reader
that means "released" still treats it as released.
(The real Accept flow is proven in tests/test_git_model_board.py.)
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, control, project_memory

TASK = "BRAND-UPGRADE"


def _legacy_accepted(state: dict) -> None:
    """The shape the studio's board has today: accepted, in main, status never moved on."""
    state.setdefault("releases", {})[TASK] = {"task": TASK, "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto-fixture", "recorded_at": board.now()}
    state.setdefault("release_decisions", {})[TASK] = {"task": TASK, "decision": "accepted", "recorded_at": board.now()}
    state.setdefault("git_acceptances", {})[TASK] = {"task": TASK, "commit": "a" * 40, "tree": "b" * 40, "accepted_at": "2026-09-26T21:10:00+00:00"}


class _Board(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        with board.locked_state(self.root):
            pass  # a write creates the board file


class LegacyUpgradeTests(_Board):
    def test_an_old_accepted_release_reads_accepted_after_load_and_is_saved_that_way(self):
        path = board.board_dir(self.root) / "state.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        _legacy_accepted(raw)
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(board.snapshot(self.root)["releases"][TASK]["status"], "ACCEPTED", "readers see ACCEPTED")
        with board.locked_state(self.root) as state:
            self.assertEqual(state["releases"][TASK]["status"], "ACCEPTED")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["releases"][TASK]["status"], "ACCEPTED", "the next write saves the upgrade")
        self.assertEqual(saved["releases"][TASK]["accepted_at"], "2026-09-26T21:10:00+00:00")

    def test_only_accepted_and_fast_forwarded_releases_are_upgraded(self):
        with board.locked_state(self.root) as state:
            _legacy_accepted(state)
            for task, decision, acceptance in (
                ("WAITING", None, None),                                   # awaiting the owner's test
                ("REJECTED", {"decision": "not_accepted"}, None),          # sent back
                ("FAILED-FF", {"decision": "accepted", "git_acceptance": {"status": "failed"}}, None),  # Accept saved, not in main
                ("NO-GIT", {"decision": "accepted"}, None),                # no git acceptance recorded
            ):
                state["releases"][task] = {"task": task, "status": "VISUAL_TEST_REQUIRED", "cto_id": "c", "recorded_at": board.now()}
                if decision:
                    state["release_decisions"][task] = {"task": task, "recorded_at": board.now(), **decision}
        releases = board.snapshot(self.root)["releases"]
        self.assertEqual(releases[TASK]["status"], "ACCEPTED")
        for task in ("WAITING", "REJECTED", "FAILED-FF", "NO-GIT"):
            self.assertEqual(releases[task]["status"], "VISUAL_TEST_REQUIRED", task)


class ReleasedReaderTests(unittest.TestCase):
    def state(self, release_status: str | None) -> dict:
        state = {"agents": {}, "cancelled_tasks": {}, "release_repairs": {}, "deferred_findings": {},
                 "qa_requests": {"final": {"id": "final", "task": TASK, "phase": "final_acceptance", "status": "passed"}},
                 "releases": {}}
        if release_status:
            state["releases"][TASK] = {"task": TASK, "status": release_status}
        return state

    def test_the_cto_is_not_woken_for_an_accepted_task_whose_final_review_passed(self):
        cto = {"id": "cto-1", "role": "cto", "active": True}
        self.assertTrue(board._agent_has_actionable_work(self.state(None), cto),
                        "a passed final review with no release recorded is CTO work (the rule under test)")
        self.assertFalse(board._agent_has_actionable_work(self.state("VISUAL_TEST_REQUIRED"), cto))
        self.assertFalse(board._agent_has_actionable_work(self.state("ACCEPTED"), cto),
                         "an ACCEPTED release is a recorded release; nothing is left for the CTO")


PROBE = r"""
<script>
(async () => {
  const read = () => ({cards: Array.from(document.querySelectorAll('#tasks .task')).map(n => n.dataset.task),
                       history: (document.querySelector('#history-list')?.textContent || '')});
  for (let attempt = 0; attempt < 100 && !document.querySelector('#tasks'); attempt++) await new Promise(r => setTimeout(r, 100));
  await new Promise(r => setTimeout(r, 1500));
  const history = document.querySelector('#history');
  if (history) history.open = true;  // what the owner does: open Task history, which loads it
  for (let attempt = 0; attempt < 100 && !/Brand Upgrade/.test(read().history); attempt++) await new Promise(r => setTimeout(r, 100));
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(read())});
})();
</script>
"""


class RenderedAcceptedTests(_Board):
    def setUp(self):
        super().setUp()
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        from tests.environment_support import require_loopback
        require_loopback()

    def test_an_accepted_task_is_in_task_history_as_accepted_and_not_a_live_card(self):
        from tests.test_branding_rendered import probe_proxy
        project_memory.initialize(self.root, project_name="Accepted proof", description="Facts.")
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "Upgrade the brand experts.")
        board.begin_task(self.root, agent["id"], TASK)
        with board.locked_state(self.root) as state:
            _legacy_accepted(state)
            state["agents"][agent["id"]].update({"active": False, "status": "stopped"})
        self.assertEqual(board.snapshot(self.root)["releases"][TASK]["status"], "ACCEPTED")
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Accepted proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="accepted-proof", chat_action_token="owner-token",
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=1000)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        reading = sink.get("value")
        self.assertIsNotNone(reading, "Chrome reported nothing")
        self.assertNotIn(TASK, reading["cards"], json.dumps(reading)[:400])
        self.assertIn("Brand Upgrade", reading["history"], json.dumps(reading)[:600])
        self.assertIn("Accepted", reading["history"], "Task history shows it as accepted")
        self.assertNotIn("READY FOR YOUR TEST", reading["history"].upper())


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, both platforms) for the frozen review scope (task 2026-10-25_frozen-review-scope).

The owner reads two things and is never asked anything: the end-of-task information (what the checks judged minor and left as notes, and the
plain outcome when the harness stopped repairing a part) and, in Settings, the plain field "If repairs keep failing, retry once with".
Machine inputs removed: nothing is read from the machine; the Settings home is a temporary folder.

Run:  PYTHONPATH=. python3 -m unittest tests.test_frozen_review_scope_rendered -v
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, control, global_settings, project_manager
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_requirements_contract import context, delivery_with_direction

HISTORY_PROBE = r"""
<script>
(async () => {
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const history = document.querySelector('#history');
  if (history && !history.open) history.open = true;
  for (let attempt = 0; attempt < 120; attempt++) {
    if (document.querySelector('.history-item [data-review-notes]')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  document.querySelectorAll('details.history-date-group').forEach(group => { group.open = true; });
  await new Promise(resolve => setTimeout(resolve, 400));
  const blocks = Array.from(document.querySelectorAll('.history-item [data-review-notes]'));
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    count: blocks.length, visible: blocks.every(visible), text: blocks.map(node => node.innerText).join(' || '),
    fontSize: blocks[0] ? getComputedStyle(blocks[0]).fontSize : '',
    cards: Array.from(document.querySelectorAll('#owner-actions .owner-action, #owner-actions article')).map(node => node.innerText),
    pageText: document.body.innerText,
  })});
})();
</script>
"""

SETTINGS_PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 600; attempt++) {
    if (document.querySelector('[data-recovery-model="codex"]')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const card = document.querySelector('[data-recovery-card]');
  const select = document.querySelector('[data-recovery-model="codex"]');
  const box = card ? card.getBoundingClientRect() : {width: 0, height: 0};
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    text: card ? card.innerText : '', visible: box.width > 0 && box.height > 0,
    options: select ? Array.from(select.options).map(option => option.textContent) : [],
    vendors: Array.from(document.querySelectorAll('[data-recovery-model]')).map(node => node.dataset.recoveryModel),
  })});
})();
</script>
"""


def _render(testcase: unittest.TestCase, handler, probe: str, path: str = "/", timeout: int = 120) -> dict:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    testcase.addCleanup(server.server_close)
    testcase.addCleanup(server.shutdown)
    sink: dict = {}
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, probe))
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    testcase.addCleanup(proxy.server_close)
    testcase.addCleanup(proxy.shutdown)
    profile = tempfile.TemporaryDirectory()
    testcase.addCleanup(profile.cleanup)
    process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}{path}", Path(profile.name), width=1300, height=1000)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and "value" not in sink:
            time.sleep(0.1)
    finally:
        process.close()
    testcase.assertIn("value", sink, "Chrome reported no probe; a blank render is not a pass")
    return sink["value"]


class RenderedFrozenScopeTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def test_the_end_of_task_information_lists_the_notes_and_the_outcome_and_asks_nothing(self):
        root = context(Path(self.temporary.name))
        session = control.create(root, "claude_cto")
        cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
        for task in ("TASK-NOTES", "TASK-STOPPED"):
            delivery_with_direction(root, task)
        with board.locked_state(root) as state:
            for task in ("TASK-NOTES", "TASK-STOPPED"):
                state["releases"][task] = {"task": task, "status": "RELEASE_ACCEPTED", "cto_id": cto["id"], "recorded_at": board.now(), "head_commit": "c0ffee"}
                state["release_decisions"][task] = {"task": task, "decision": "accepted", "recorded_at": board.now()}
            state["qa_requests"]["review-TASK-NOTES-final-02"] = {
                "id": "review-TASK-NOTES-final-02", "task": "TASK-NOTES", "phase": "final_acceptance", "stage": "independent_review", "status": "passed", "cycle": 2,
                "structure_revision": 1, "subtask": "", "chunk": "", "requested_at": board.now(), "claimed_by": "qa-1", "developer_id": "d-1", "review_wait_started_at": board.now(),
                "board_decision": {"round": 2, "effective_result": "passed", "reclassified": True, "blocking": [], "baseline_request": "x", "message": "m",
                                   "notes": [{"id": "R2-1", "summary": "The footer sits two pixels low on the report page."}]}}
            state["review_escalations"] = {"k": {"task": "TASK-STOPPED", "status": "could_not_finish", "failed_judgments": 4, "open_ids": ["B2"],
                                                 "outcome": "Could not finish: after 4 failed reviews of this part and one fresh attempt, B2 still failed. Nothing from this part was released and the harness will not retry it by itself. Other work continues."}}
        reading = _render(self, board_viewer.make_handler(root, project_name="Notes proof"), HISTORY_PROBE)
        self.assertGreaterEqual(reading["count"], 2, reading)
        self.assertTrue(reading["visible"], reading)
        self.assertIn("Left as notes (judged minor; nothing for you to do):", reading["text"])
        self.assertIn("The footer sits two pixels low on the report page.", reading["text"])
        self.assertIn("Could not finish: after 4 failed reviews of this part and one fresh attempt", reading["text"])
        self.assertGreaterEqual(float(reading["fontSize"].rstrip("px") or 0), 13.0, "readable at the size it ships")
        self.assertEqual(reading["cards"], [], "no card asks the owner anything")
        self.assertNotIn("Your decision needed", reading["pageText"])

    def test_settings_shows_the_plain_retry_field_per_vendor_with_the_default_and_saves_nothing_by_itself(self):
        home = Path(self.temporary.name) / "home"
        manager = project_manager.ProjectManager(home, board_port=0)
        reading = _render(self, project_manager.make_handler(manager), SETTINGS_PROBE, path="/?page=settings", timeout=180)
        self.assertTrue(reading["visible"], reading)
        self.assertIn("If repairs keep failing", reading["text"])
        self.assertIn("retry once with", reading["text"])
        self.assertIn("never asks you at that moment", reading["text"])
        self.assertEqual(sorted(reading["vendors"]), ["claude", "codex", "gemini"])
        self.assertIn(f"Default: {control.STRONGEST_MODELS['codex']}", reading["options"])
        self.assertEqual(global_settings.load(home)["recovery_models"], {"claude": "", "codex": "", "gemini": ""}, "viewing the page chose nothing")


if __name__ == "__main__":
    unittest.main()

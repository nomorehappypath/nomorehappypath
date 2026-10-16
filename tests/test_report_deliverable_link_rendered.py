# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, both platforms): the owner sees the delivered document in one click.

Owner 2026-10-15: on the task card and on the release card a marked document
shows as "Read what was delivered" with an Open report link that opens the text;
View app is still there; the CTO's card carries the link too; History keeps it.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, control
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_report_deliverable_link import REPORT, REPORT_TEXT, STUDIO_TITLE, STUDIO_WHY
from tests.test_requirements_contract import context, delivery_with_direction

PROBE = r"""
<script>
(async () => {
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  for (let attempt = 0; attempt < 120; attempt++) {
    if (document.querySelector('.release-response [data-delivered-documents]') && document.querySelector('#owner-actions [data-card-link]')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const release = document.querySelector('.release-response');
  const releaseBlock = release ? release.querySelector('[data-delivered-documents]') : null;
  const releaseLink = releaseBlock ? releaseBlock.querySelector('a[data-deliverable-link]') : null;
  const taskBlocks = Array.from(document.querySelectorAll('.task [data-delivered-documents]'));
  const viewApp = release ? release.querySelector('button.view-app') : null;
  const cardLink = document.querySelector('#owner-actions [data-card-link]');
  const cardHrefs = Array.from(document.querySelectorAll('#owner-actions [data-card-link]')).map(node => node.getAttribute('href'));
  const cardLabels = Array.from(document.querySelectorAll('#owner-actions [data-card-link]')).map(node => node.innerText);
  let opened = '';
  if (releaseLink) { try { opened = await (await fetch(releaseLink.getAttribute('href'), {cache: 'no-store'})).text(); } catch (error) { opened = 'ERROR ' + error.message; } }
  const history = document.querySelector('#history');
  if (history && !history.open) history.open = true;
  let historyText = '';
  for (let attempt = 0; attempt < 80; attempt++) {
    const block = document.querySelector('.history-item [data-delivered-documents]');
    if (block) { historyText = block.textContent; break; }
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    releaseVisible: visible(releaseBlock), releaseText: releaseBlock ? releaseBlock.innerText : '',
    releaseLinkText: releaseLink ? releaseLink.innerText : '', releaseFontSize: releaseLink ? getComputedStyle(releaseLink).fontSize : '',
    releaseBeforeViewApp: Boolean(releaseBlock && viewApp) && (releaseBlock.compareDocumentPosition(viewApp) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
    viewAppVisible: visible(viewApp), viewAppText: viewApp ? viewApp.innerText : '',
    taskBlockCount: taskBlocks.length, taskText: taskBlocks.map(node => node.innerText).join(' | '),
    cardLinkVisible: visible(cardLink), cardLinkText: cardLink ? cardLink.innerText : '', cardHrefs, cardLabels,
    cardNote: cardLink ? (cardLink.parentElement.querySelector('small')?.innerText || '') : '',
    opened, historyText,
    target: releaseLink ? releaseLink.getAttribute('target') : '',
  })});
})();
</script>
"""


class RenderedReportLinkTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = context(Path(self.temporary.name))
        self.delivery = delivery_with_direction(self.root, "TASK-DOC")
        session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
        board.create_contract(self.root, self.delivery["id"], "Diagnose the existing deck", [REPORT])
        uploads = self.root / ".harness" / "evidence" / "uploads" / "session"
        uploads.mkdir(parents=True)
        self.report = uploads / "1792029081869284000-evidence-a947.txt"
        self.report.write_text(REPORT_TEXT, encoding="utf-8")
        board.attach_contract_evidence(self.root, self.delivery["id"], REPORT, str(self.report), True, "Report")
        self.plan = uploads / "plan.txt"
        self.plan.write_text("# Implementation plan\n\nNext steps\n", encoding="utf-8")
        board.attach_contract_evidence(self.root, self.delivery["id"], REPORT, str(self.plan), True, "Plan")
        with board.locked_state(self.root) as state:
            # the task is released for the owner's test: the release card renders
            state["releases"]["TASK-DOC"] = {"task": "TASK-DOC", "status": "VISUAL_TEST_REQUIRED", "cto_id": self.cto["id"],
                                             "recorded_at": board.now(), "head_commit": "c0ffee", "acceptance_base_commit": "base",
                                             "acceptance_manifest": [], "owner_test_steps": [f"Verify {REPORT}."], "preview": {}}
            # a second, accepted task with the same kind of document sits in History
            state["releases"]["TASK-OLD"] = {"task": "TASK-OLD", "status": "RELEASE_ACCEPTED", "cto_id": self.cto["id"], "recorded_at": board.now(), "head_commit": "c0ffee"}
            state["release_decisions"]["TASK-OLD"] = {"task": "TASK-OLD", "decision": "accepted", "recorded_at": board.now()}
        old = delivery_with_direction(self.root, "TASK-OLD")
        board.create_contract(self.root, old["id"], "An older diagnosis", ["An older report"])
        older = uploads / "older.txt"; older.write_text("# Older diagnosis\n", encoding="utf-8")
        board.attach_contract_evidence(self.root, old["id"], "An older report", str(older), True)
        board.record_owner_action(self.root, self.cto["id"], STUDIO_TITLE, why=STUDIO_WHY, task="TASK-DOC")
        # a second card of another kind (the same kind would refresh the first in place)
        board.record_owner_action(self.root, self.cto["id"], "Read the report and review the plan, then Accept or Reject",
                                  why="Both are delivered documents.", task="TASK-DOC", kind="other")

    def render(self) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root, project_name="Report link proof"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close); self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory(); self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1300, height=1000)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe; a blank render is not a pass")
        return sink["value"]

    def test_the_owner_sees_the_report_link_on_the_release_card_the_task_card_the_cto_card_and_in_history(self):
        reading = self.render()
        self.assertTrue(reading["releaseVisible"], reading)
        self.assertIn("Read what was delivered", reading["releaseText"])
        self.assertIn("JLS Brand Strategy quality diagnostic — content-focused update", reading["releaseText"])
        self.assertIn("View app starts the app itself; it does not show the document", reading["releaseText"])
        self.assertEqual(reading["releaseLinkText"], "Open report")
        self.assertEqual(reading["target"], "_blank")
        self.assertGreaterEqual(float(reading["releaseFontSize"].rstrip("px") or 0), 13.0, "readable at the size it ships")
        self.assertTrue(reading["viewAppVisible"], "View app is never hidden")
        self.assertEqual(reading["viewAppText"], "View app")
        self.assertTrue(reading["releaseBeforeViewApp"], "the document comes before the View app box")
        self.assertGreaterEqual(reading["taskBlockCount"], 1, "the task card carries the block too")
        self.assertIn("Open report", reading["taskText"])
        self.assertTrue(reading["cardLinkVisible"], "the CTO's card carries the link")
        self.assertIn(reading["cardLinkText"], {"Open report: Plan", "Open report: Report"}, "a task with two documents names them")
        self.assertEqual(len(set(reading["cardHrefs"])), 2, f"the cards link two distinct documents, never duplicate links to one: {reading['cardHrefs']}")
        self.assertEqual(sorted(reading["cardLabels"]), ["Open report: Plan", "Open report: Plan", "Open report: Report", "Open report: Report"])
        self.assertIn("Implementation plan", reading["releaseText"], "both current documents are on the release card")
        self.assertIn("View app starts the app and does not show a document", reading["cardNote"])
        self.assertIn(reading["opened"], {REPORT_TEXT, "# Implementation plan\n\nNext steps\n"}, reading["opened"][:120])
        self.assertIn("Older diagnosis", reading["historyText"], "the accepted task's report stays one click away in History")


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof for the two Mission Control screen problems of the risk-report task (Stage 6).

6a. Accepted button: pressing it shows at once that it worked, the card then closes, a second press does nothing and
    says so, and a task the board already accepted never offers Accepted or Not accepted.
6b. Analyze code: the card offers "View FINDINGS.md", which opens the report from the delivered work, and never
    starts an app preview. Every other kind keeps "View app".

The real board page, the real worker handler and real headless Chrome are used. The decision is slowed to 2.5 seconds
(as the real merge makes it) so the moments between the press and the answer can be looked at.

Run:  PYTHONPATH=. python3 -m unittest tests.test_stage6_mission_control_rendered -v
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from harness import board, board_viewer, browser_acceptance, release_preview
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_release_preview import _git

REPORT = "# Risk report\n\n## P1 - an example finding\nThe delivered analysis text.\n"

PREAMBLE = r"""
const out = {};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (test, tries = 300) => { for (let i = 0; i < tries && !test(); i++) await sleep(100); return Boolean(test()); };
const releaseButtons = () => Array.from(document.querySelectorAll('.release-response button')).map(b => b.textContent.trim());
const decisionButtons = () => releaseButtons().filter(text => text === 'Accepted' || text === 'Not accepted');
const cardText = () => Array.from(document.querySelectorAll('.release-response')).map(n => n.textContent.replace(/\s+/g, ' ').trim()).join(' | ');
"""


def script(body: str) -> str:
    return ("<script>(async () => {" + PREAMBLE + "try {" + body +
            "} catch (error) { out.error = String(error && error.stack || error); }"
            " await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)}); })();</script>")


class Stage6Case(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.root = base / "project"
        self.root.mkdir()
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        _git(self.workspace, "init", "-q")
        (self.workspace / "FINDINGS.md").write_text(REPORT)
        (self.workspace / "app.py").write_text("print('hello')\n")
        _git(self.workspace, "add", "FINDINGS.md", "app.py")
        _git(self.workspace, "commit", "-qm", "delivered work")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")

    def board_state(self, *, kind: str = "", decision: bool = False, accepted_in_main: bool = False) -> None:
        with board.locked_state(self.root) as state:
            state.setdefault("releases", {})["TASK"] = {
                "task": "TASK", "status": "VISUAL_TEST_REQUIRED", "head_commit": self.commit,
                "cto_id": "cto-0001-test", "recorded_at": board.now(), "owner_test_steps": ["Open the result"],
            }
            state.setdefault("task_workspaces", {})["TASK"] = str(self.workspace)
            if kind:
                state.setdefault("task_kinds", {})["TASK"] = kind
            if decision:
                state.setdefault("release_decisions", {})["TASK"] = {"task": "TASK", "decision": "accepted", "recorded_at": board.now()}
            if accepted_in_main:
                state.setdefault("git_acceptances", {})["TASK"] = {"task": "TASK", "commit": self.commit, "recorded_at": board.now()}

    def probe(self, body: str, *, slow_accept: bool = False, wait: int = 120) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Stage 6 proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="stage6-proof", chat_action_token="stage6-token",
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, script(body)))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)

        def slow_decision(root, task, decision, reason=""):
            time.sleep(2.5)                         # the real answer waits for the merge into main
            record = {"task": task, "decision": decision, "recorded_at": board.now()}
            with board.locked_state(root) as state:
                state.setdefault("release_decisions", {})[task] = record
            return record

        patcher = patch.object(board, "record_release_decision", slow_decision) if slow_accept else patch.object(board, "now", board.now)
        with patcher:
            process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=900)
            try:
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline and "value" not in sink:
                    time.sleep(0.1)
            finally:
                process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        self.assertNotIn("error", sink["value"], sink["value"].get("error"))
        return sink["value"]


class AcceptedButtonTests(Stage6Case):
    def test_pressing_accepted_confirms_at_once_closes_the_card_and_ignores_a_second_press(self):
        self.board_state()
        value = self.probe(r"""
            out.appeared = await until(() => decisionButtons().length === 2);
            out.beforeButtons = releaseButtons();
            let posts = 0;
            const realFetch = window.fetch;
            window.fetch = (url, options) => { if (String(url).includes('/decision') && options && options.method === 'POST') posts += 1; return realFetch(url, options); };
            Array.from(document.querySelectorAll('.release-response button')).find(b => b.textContent.trim() === 'Accepted').click();
            await sleep(250);                                      // the answer takes 2.5 s: this is the moment the owner pressed again
            out.soon = {card: cardText(), buttons: releaseButtons(), notice: document.querySelector('#notice').textContent};
            await submitAccepted('TASK'); await submitAccepted('TASK');
            out.second = {notice: document.querySelector('#notice').textContent, posts};
            const worst = [];
            for (let i = 0; i < 80; i++) { await sleep(100); worst.push(decisionButtons().length); }   // 8 s through the answer and the refresh
            out.maxDecisionButtonsAfterPress = Math.max(...worst);
            out.closed = await until(() => document.querySelectorAll('.release-response').length === 0 && document.querySelectorAll('.accepted-thanks').length === 0, 100);
            out.finalPosts = posts;
            out.tasksLeft = document.querySelectorAll('#tasks .task').length;
        """, slow_accept=True)
        self.assertTrue(value["appeared"], "the Accepted card never rendered: " + json.dumps(value))
        self.assertIn("Accepted", value["beforeButtons"])
        self.assertIn("Not accepted", value["beforeButtons"])
        self.assertIn("Accepted, thank you", value["soon"]["card"], "no confirmation on the card right after the press: " + json.dumps(value["soon"]))
        self.assertEqual([b for b in value["soon"]["buttons"] if b in ("Accepted", "Not accepted")], [], "the buttons were still offered")
        self.assertIn("already accepted", value["second"]["notice"].lower())
        self.assertEqual(value["second"]["posts"], 1, "a second press must not ask the board again")
        self.assertEqual(value["maxDecisionButtonsAfterPress"], 0, "Accepted or Not accepted came back after the press")
        self.assertTrue(value["closed"], "the card stayed on screen: " + json.dumps(value))
        self.assertEqual(value["finalPosts"], 1)

    def test_a_task_the_board_already_accepted_never_offers_accepted_or_not_accepted(self):
        for name, arguments in (("decision recorded", {"decision": True}), ("work already in main", {"accepted_in_main": True})):
            with self.subTest(name):
                self.board_state(**arguments)
                value = self.probe(r"""
                    await sleep(4000);                              // long enough for several page refreshes
                    out.buttons = releaseButtons();
                    out.decisionButtons = decisionButtons();
                    out.pageLoaded = document.querySelector('#tasks') !== null;
                """)
                self.assertTrue(value["pageLoaded"])
                self.assertEqual(value["decisionButtons"], [], f"{name}: " + json.dumps(value["buttons"]))


class AnalysisReportTests(Stage6Case):
    FIND = r"""
        out.appeared = await until(() => document.querySelector('.release-response .release-preview'));
        out.buttons = releaseButtons();
        out.previewText = (document.querySelector('.release-response .release-preview')?.textContent || '').replace(/\s+/g, ' ').trim();
        let opened = '';
        window.open = url => { opened = String(url); return null; };
        const open = Array.from(document.querySelectorAll('.release-response button')).find(b => /View FINDINGS\.md/.test(b.textContent));
        if (open) { open.click(); await sleep(200); }
        out.opened = opened;
        out.report = opened ? await (await fetch(opened)).text() : '';
        await sleep(1500);
    """

    def test_an_analysis_card_offers_the_report_and_never_an_app(self):
        self.board_state(kind="analysis")
        supervisor = release_preview.ReleasePreviewSupervisor(self.root, tick_seconds=0.2)
        supervisor.start()
        self.addCleanup(supervisor.shutdown)
        value = self.probe(self.FIND)
        self.assertTrue(value["appeared"], json.dumps(value))
        self.assertIn("View FINDINGS.md", value["buttons"])
        self.assertNotIn("View app", value["buttons"])
        self.assertNotIn("View app", value["previewText"])
        self.assertTrue(value["opened"].endswith("/api/releases/TASK/findings"), value["opened"])
        self.assertEqual(value["report"], REPORT, "the button must open the delivered FINDINGS.md")
        release = board.snapshot(self.root)["releases"]["TASK"]
        self.assertFalse(release.get("preview"), "no preview may be recorded for an analysis: " + json.dumps(release.get("preview")))
        with self.assertRaisesRegex(ValueError, "analysis"):
            release_preview.request_view(self.root, "TASK")
        self.assertFalse((release_preview.preview_root(self.root) / "TASK").exists(), "no preview folder may be made for an analysis")

    def test_the_report_comes_from_the_delivered_commit_and_only_for_an_analysis(self):
        self.board_state(kind="analysis")
        (self.workspace / "FINDINGS.md").write_text("edited after delivery, never committed\n")
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="p", manager_url="http://127.0.0.1:1/", settings_home=self.root / ".h", project_id="p", chat_action_token="t"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/releases/TASK/findings", timeout=15) as response:
            self.assertEqual(response.read().decode("utf-8"), REPORT)
        self.board_state(kind="development")
        status, _body, _media = board_viewer.serve_findings_report(self.root, "/api/releases/TASK/findings")
        self.assertEqual(status, 404, "a development task has no analysis report")

    def test_a_development_card_keeps_view_app_unchanged(self):
        self.board_state()
        value = self.probe(r"""
            out.appeared = await until(() => document.querySelector('.release-response .view-app'));
            out.buttons = releaseButtons();
        """)
        self.assertTrue(value["appeared"], json.dumps(value))
        self.assertIn("View app", value["buttons"])
        self.assertNotIn("View FINDINGS.md", value["buttons"])


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Test machine door in the project's own Mission Control header (windows-walk item 5), seen in a real browser.

Machine inputs removed: the owner's HOME, ~/.ssh and any ssh config (no ssh runs here; the descriptor is written as Connect leaves it).

Run:  PYTHONPATH=. python3 -m unittest tests.test_project_header_machine_rendered -v
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board_viewer, browser_acceptance, control, project_manager, project_registry as registry, screen_script, test_machine
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_test_machine import connected_descriptor

LONG_USER = "an-unusually-long-service-account-name"
LONG_ADDRESS = "windows-test-machine-in-the-lab-with-a-very-long-dns-name.example.internal"

PROBE = r"""
<script>
(async () => {
  const wait = async (test, tries = 200) => { for (let i = 0; i < tries && !test(); i++) await new Promise(r => setTimeout(r, 100)); return test(); };
  const q = s => document.querySelector(s);
  const result = {};
  try {
    await wait(() => q('#project-test-machine') && /Updated/.test(document.body.textContent) && q('#project-test-machine').textContent !== 'Test machine');
    const box = node => { if (!node) return null; const b = node.getBoundingClientRect(); return {left: b.left, right: b.right, top: b.top, width: b.width, height: b.height}; };
    const link = q('#project-test-machine');
    result.text = link ? link.textContent : null;
    result.href = link ? link.getAttribute('href') : null;
    result.box = box(link);
    result.viewport = innerWidth;
    result.overflowX = document.scrollingElement.scrollWidth > document.scrollingElement.clientWidth;
    result.close = box(q('#close-project'));
    if (link) { link.focus(); result.focused = document.activeElement === link; result.outline = getComputedStyle(link).outlineStyle + ' ' + getComputedStyle(link).outlineWidth; }
    result.cardHidden = q('#machine-card').hidden;
    // the label mapping, fed directly
    const feed = m => { renderMachineLink(m); return link.textContent; };
    result.map = {
      unknown: feed({state: 'unknown'}), none: feed(null),
      usable: feed({state: 'ready', usable: true, connected: true, user: 'u', address: 'a'}),
      setup: feed({state: 'baseline_incomplete', usable: false, connected: true}),
      needs: feed({state: 'reauth_required', usable: false, connected: true}),
      asked: feed({state: 'not_connected', usable: false, connected: false, request: {reason: 'build'}}),
      notConnected: feed({state: 'not_connected', usable: false, connected: false}),
    };
  } catch (error) { result.error = String(error && error.stack || error); }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(result)});
})();
</script>
"""


class HeaderMachineButtonTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.entries = {}
        for name in ("alpha", "beta"):
            code = self.base / name
            code.mkdir()
            self.entries[name] = registry.register(self.home, name, code)

    def context(self, name):
        entry = self.entries[name]
        return type(registry.context_for_entry(entry))(*[getattr(registry.context_for_entry(entry), field) for field in ("code_root", "data_root", "workspace_root")])

    def serve_manager(self):
        manager = project_manager.ProjectManager(self.home, board_port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", manager

    def serve_board(self, name, manager_url="http://127.0.0.1:1", with_project=True):
        context = self.context(name)
        control.initialize(context)
        kwargs = dict(project_name=name, settings_home=self.home)
        if with_project:
            kwargs.update(manager_url=manager_url, project_id=self.entries[name]["id"], chat_action_token="board-token", ready_token="ready-token")
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(context.code_root, **kwargs))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def read(self, board_url, width):
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(board_url, sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        with tempfile.TemporaryDirectory() as profile:
            process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile), width=width, height=1000)
            try:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline and "value" not in sink:
                    time.sleep(0.1)
            finally:
                process.close()
        self.assertIn("value", sink, "Chrome reported nothing: a blank render is not a pass")
        reading = sink["value"]
        self.assertNotIn("error", reading, json.dumps(reading, indent=2))
        return reading

    def walk(self, url, steps):
        runtime = Path(tempfile.mkdtemp(prefix="header-machine-"))
        self.addCleanup(shutil.rmtree, runtime, True)
        run = screen_script.run_script(url, screen_script.parse_steps(json.dumps(steps)), runtime, timeout=80)
        self.assertEqual(run["error"], "", run)
        failed = [item for item in run["steps"] if not item["ok"]]
        self.assertFalse(failed, f"{failed} | {run['final_text'][:500]}")
        self.assertTrue(any(run["pictures"]), "a blank render is a finding, not a pass")
        return run

    def inside(self, reading):
        dump = json.dumps(reading, indent=2)
        self.assertFalse(reading["overflowX"], dump)
        self.assertGreaterEqual(reading["box"]["left"], 0, dump)
        self.assertLessEqual(reading["box"]["right"], reading["viewport"] + 1, dump)
        self.assertGreater(reading["box"]["height"], 20, dump)
        close = reading["close"]
        self.assertIsNotNone(close, "the Close project button sits beside the door: " + dump)
        self.assertLessEqual(close["right"], reading["viewport"] + 1, dump)

    def test_the_door_is_in_the_header_on_a_desktop_and_a_phone_and_says_the_state(self):
        manager_url, _manager = self.serve_manager()
        board_url = self.serve_board("alpha", manager_url)
        for width in (1400, 390):
            reading = self.read(board_url, width)
            dump = json.dumps(reading, indent=2)
            self.assertEqual(reading["text"], "Test machine: not connected", dump)
            self.assertEqual(reading["href"], f"{manager_url}/?machine={self.entries['alpha']['id']}", dump)
            self.assertTrue(reading["focused"], dump)
            self.assertFalse(reading["outline"].startswith("none"), "a focused door shows a ring: " + dump)
            self.inside(reading)
            self.assertEqual(reading["map"], {
                "unknown": "Test machine", "none": "Test machine", "usable": "Test machine: connected u@a",
                "setup": "Test machine: agents are setting it up", "needs": "Test machine: needs you",
                "asked": "Test machine: an agent needs it", "notConnected": "Test machine: not connected"}, dump)

    def test_a_long_identity_does_not_push_the_page_wider_than_a_phone(self):
        manager_url, manager = self.serve_manager()
        context = self.context("alpha")
        connected_descriptor(context, identity=manager.machine_identity, user=LONG_USER, address=LONG_ADDRESS, baseline_state="complete")
        board_url = self.serve_board("alpha", manager_url)
        for width in (1400, 390):
            reading = self.read(board_url, width)
            dump = json.dumps(reading, indent=2)
            self.assertEqual(reading["text"], f"Test machine: connected {LONG_USER}@{LONG_ADDRESS}", dump)
            self.inside(reading)
            self.assertTrue(reading["cardHidden"], "a healthy machine raises no card: " + dump)

    def test_the_state_changes_with_the_machine(self):
        manager_url, manager = self.serve_manager()
        context = self.context("alpha")
        connected_descriptor(context, identity=manager.machine_identity)
        board_url = self.serve_board("alpha", manager_url)
        self.assertEqual(self.read(board_url, 1400)["text"], "Test machine: connected u@127.0.0.1")
        test_machine.mark_reauth(context, "project_stopped")
        reading = self.read(board_url, 1400)
        self.assertEqual(reading["text"], "Test machine: needs you", json.dumps(reading))
        self.assertFalse(reading["cardHidden"], "the existing card still appears when the machine needs the owner")

    def test_each_project_leads_to_its_own_form(self):
        manager_url, _manager = self.serve_manager()
        for name in ("alpha", "beta"):
            board_url = self.serve_board(name, manager_url)
            reading = self.read(board_url, 1400)
            self.assertEqual(reading["href"], f"{manager_url}/?machine={self.entries[name]['id']}")
            run = self.walk(reading["href"], [{"expect": f"Test machine — {name}"}, {"expect": "No machine connected yet."}])
            self.assertNotIn("Test machine — " + ("beta" if name == "alpha" else "alpha"), run["final_text"])

    def test_a_standalone_page_has_no_door_and_the_page_text_never_shows_a_broken_link(self):
        page = board_viewer.rendered_page()
        self.assertNotIn('id="project-test-machine"', page)
        self.assertNotIn('class="project-controls"', page)
        with_manager = board_viewer.rendered_page("alpha", "d", "http://127.0.0.1:8740", "pid-1", "")
        self.assertIn('id="project-test-machine"', with_manager)
        self.assertNotIn('id="close-project"', with_manager, "no close token: the door still shows, the Close button does not")
        both = board_viewer.rendered_page("alpha", "d", "http://127.0.0.1:8740", "pid-1", "token")
        self.assertIn('id="project-test-machine"', both)
        self.assertIn('id="close-project"', both)
        self.assertIn("?machine=pid-1", both)
        tricky = board_viewer.rendered_page("a", "d", "http://127.0.0.1:8740", 'x"><b>', "")
        self.assertNotIn('"><b>', tricky.split("<script")[0].split('id="project-test-machine"')[1][:200], "the id is quoted into the link")

    def test_standalone_board_in_a_browser_shows_no_door(self):
        board_url = self.serve_board("beta", with_project=False)
        runtime = Path(tempfile.mkdtemp(prefix="header-machine-"))
        self.addCleanup(shutil.rmtree, runtime, True)
        run = screen_script.run_script(board_url + "/", screen_script.parse_steps(json.dumps([{"expect": "Updated"}])), runtime, timeout=80)
        self.assertEqual(run["error"], "", run)
        self.assertNotIn("Test machine", run["final_text"])


if __name__ == "__main__":
    unittest.main()

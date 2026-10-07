# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Real-page adoption with no native picker and service cwd above the target."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from harness import browser_acceptance, project_manager, project_registry as registry
from tests.environment_support import require_loopback


class FolderBrowserRenderedTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        browser_acceptance.resolve_binary()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / '.harness-home'
        self.work = self.base / 'work'
        self.work.mkdir()
        self.target = self.work / 'test'
        self.target.mkdir()
        (self.target / 'source.txt').write_text('owner source remains untouched')
        existing = self.work / 'existing'
        existing.mkdir()
        registry.register(self.home, 'Existing', existing, kind='adopted')
        self.addCleanup(patch.stopall)
        patch.object(project_manager, 'native_folder_selection_available', return_value=False).start()
        # Match the Linux service's home cwd without changing the browser host.
        patch.object(os, 'getcwd', return_value=str(self.base)).start()
        self.manager = project_manager.ProjectManager(self.home, board_port=0)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), project_manager.make_handler(self.manager))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.session = browser_acceptance.launch(
            f'http://127.0.0.1:{self.server.server_address[1]}/', self.base / 'browser',
            width=1280, height=900)
        self.addCleanup(self.session.close)
        port, path = browser_acceptance._devtools(self.base / 'browser', time.monotonic() + 15)
        self.browser = browser_acceptance._DevToolsSocket(port, path, timeout=15)
        self.addCleanup(self.browser.close)
        self.wait("document.querySelectorAll('.project').length > 0")

    def wait(self, expression):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.browser.evaluate(expression):
                return
            time.sleep(.05)
        self.fail(f'Browser condition not met: {expression}; page: ' + str(
            self.browser.evaluate('document.body.innerText')))

    def click(self, selector):
        box = self.browser.evaluate("(() => {const n=document.querySelector(" + json.dumps(selector)
            + "); n.scrollIntoView({block:'center'}); const r=n.getBoundingClientRect();"
              "return {x:r.x+r.width/2,y:r.y+r.height/2,w:r.width,h:r.height};})()")
        self.assertGreater(box['w'], 0)
        self.assertGreater(box['h'], 0)
        for kind in ('mousePressed', 'mouseReleased'):
            self.browser.call('Input.dispatchMouseEvent', {
                'type': kind, 'x': box['x'], 'y': box['y'], 'button': 'left', 'clickCount': 1})

    def enter(self, selector):
        self.browser.evaluate('document.querySelector(' + json.dumps(selector) + ').focus()')
        for kind in ('keyDown', 'keyUp'):
            self.browser.call('Input.dispatchKeyEvent', {
                'type': kind, 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13, 'text': '\r' if kind == 'keyDown' else ''})

    def type_path(self, value):
        self.click('#project-folder-typed')
        self.browser.evaluate("document.querySelector('#project-folder-typed').value = ''")
        self.browser.call('Input.insertText', {'text': str(value)})

    def screenshot(self, name):
        folder = os.environ.get('HARNESS_FOLDER_BROWSER_EVIDENCE')
        if folder:
            target = Path(folder)
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_bytes(base64.b64decode(
                self.browser.call('Page.captureScreenshot', {'format': 'png'})['data']))

    def test_linux_typed_adoption_replaces_the_custom_browser(self):
        self.click('#adopt-btn')
        self.assertIsNone(self.browser.evaluate("document.querySelector('#folder-browser')"))
        self.assertIsNone(self.browser.evaluate("document.querySelector('#project-folder-browser')"))
        self.type_path(self.target / 'missing')
        self.enter('#project-folder-browse')
        self.wait("document.querySelector('#create-error').textContent.includes('no folder')")
        self.type_path(self.target)
        self.enter('#project-folder-browse')
        self.wait("document.querySelector('#project-folder-path').textContent === " + json.dumps(str(self.target)))
        self.enter('#create-save')
        self.wait("document.querySelectorAll('.project').length === 2 && !document.querySelector('#create-dialog').open")
        self.assertEqual(list(self.target.iterdir()), [self.target / 'source.txt'])
        self.assertEqual((self.target / 'source.txt').read_text(), 'owner source remains untouched')
        self.screenshot('linux-typed-adoption.png')

    def test_new_project_and_repair_keep_typed_paths(self):
        self.click('#new-btn')
        self.click('#project-name')
        self.browser.call('Input.insertText', {'text': 'New build'})
        self.type_path(self.work)
        self.enter('#project-folder-browse')
        self.wait("document.querySelector('#project-folder-preview').textContent.includes('/new-build')")
        self.click('#create-save')
        self.wait("document.querySelectorAll('.project').length === 2 && !document.querySelector('#create-dialog').open")
        self.assertTrue((self.work / 'new-build' / '.harness').is_dir())
        existing = next(e for e in registry.entries(self.home) if e['name'] == 'Existing')
        moved = self.work / 'relocated'
        (self.work / 'existing').rename(moved)
        self.browser.evaluate('refresh()')
        self.browser.evaluate('openRepair(projectsById.get(' + json.dumps(existing['id']) + '))')
        self.click('#repair-folder-typed')
        self.browser.call('Input.insertText', {'text': str(moved)})
        self.enter('#repair-folder-browse')
        self.wait("document.querySelector('#repair-folder-path').textContent === " + json.dumps(str(moved)))
        self.enter('#repair-save')
        self.wait("!document.querySelector('#repair-dialog').open")
        updated = next(e for e in registry.entries(self.home) if e['id'] == existing['id'])
        self.assertEqual(updated['code_root'], str(moved))
        self.assertEqual(updated['data_root'], existing['data_root'])


if __name__ == '__main__':
    unittest.main()

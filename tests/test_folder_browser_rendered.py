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

    def test_linux_browser_and_typed_path_adopt_unrelated_projects(self):
        self.click('#adopt-btn')
        self.wait("document.querySelector('#create-dialog').open")
        self.assertFalse(self.browser.evaluate("document.querySelector('#project-folder-typed').hidden"))
        self.assertEqual(self.browser.evaluate("document.querySelector('#project-folder-browse').textContent"),
                         'Use this folder')
        self.click('#project-folder-browser')
        self.wait("document.querySelector('#folder-browser-path').textContent === " + json.dumps(str(self.base)))
        # Cancel via a real key event, then reopen with pointer input.
        self.browser.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'Escape', 'code': 'Escape', 'windowsVirtualKeyCode': 27})
        self.browser.call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': 'Escape', 'code': 'Escape', 'windowsVirtualKeyCode': 27})
        self.wait("!document.querySelector('#folder-browser').open")
        self.assertEqual(self.browser.evaluate("document.querySelector('#project-folder-path').textContent"), 'No folder selected')
        self.click('#project-folder-browser')
        self.wait("document.querySelector('#folder-browser-select').disabled === false")
        self.screenshot('folder-browser.png')
        self.browser.evaluate("Array.from(document.querySelectorAll('#folder-browser-list button')).find(b=>b.textContent==='work').id='work-folder'")
        self.enter('#work-folder')
        self.wait("document.querySelector('#folder-browser-path').textContent === " + json.dumps(str(self.work)))
        self.click('#folder-browser-up')
        self.wait("document.querySelector('#folder-browser-path').textContent === " + json.dumps(str(self.base)))
        self.browser.evaluate("Array.from(document.querySelectorAll('#folder-browser-list button')).find(b=>b.textContent==='work').id='work-folder'")
        self.click('#work-folder')
        self.wait("document.querySelector('#folder-browser-path').textContent === " + json.dumps(str(self.work)))
        self.browser.evaluate("Array.from(document.querySelectorAll('#folder-browser-list button')).find(b=>b.textContent==='test').id='test-folder'")
        self.click('#test-folder')
        self.wait("document.querySelector('#folder-browser-status').textContent.includes('No subfolders')")
        self.enter('#folder-browser-select')
        self.wait("document.querySelector('#project-folder-path').textContent === " + json.dumps(str(self.target)))
        self.click('#create-save')
        self.wait("!document.querySelector('#create-dialog').open && document.body.innerText.includes('Existing project adopted')")
        self.wait("document.querySelectorAll('.project').length === 2")
        self.screenshot('adopted-project.png')
        self.assertEqual((self.target / 'source.txt').read_text(), 'owner source remains untouched')
        self.assertEqual(list(self.target.iterdir()), [self.target / 'source.txt'])
        # The typed/pasted alternative still validates and adopts independently.
        typed_target = self.work / 'pasted'
        typed_target.mkdir()
        self.click('#adopt-btn')
        self.type_path(typed_target / 'missing')
        self.enter('#project-folder-browse')
        self.wait("document.querySelector('#create-error').textContent.includes('no folder')")
        self.type_path(typed_target)
        self.enter('#project-folder-browse')
        self.wait("document.querySelector('#project-folder-path').textContent === " + json.dumps(str(typed_target)))
        self.enter('#create-save')
        self.wait("document.querySelectorAll('.project').length === 3 && !document.querySelector('#create-dialog').open")
        self.assertEqual(list(typed_target.iterdir()), [])
        self.click('[data-page="help"]')
        self.wait("!document.querySelector('#help-page').hidden")
        help_text = self.browser.evaluate("document.querySelector('#help-setup-title').parentElement.innerText")
        for phrase in ('Linux, including a VM on Windows', 'Codex CLI', 'Claude Code CLI',
                       'PATH', 'all shells', 'non-interactive shells', 'systemd service',
                       'codex --version', 'claude --version', 'Browse folders', 'Use this folder'):
            self.assertIn(phrase, help_text)
        self.browser.evaluate("document.querySelector('#help-setup-title').scrollIntoView({block:'start'})")
        self.assertGreater(self.browser.evaluate("document.querySelector('#help-setup-title').getBoundingClientRect().height"), 0)
        self.screenshot('linux-help.png')
        self.assertEqual({e['code_root'] for e in registry.entries(self.home)},
                         {str(self.work / 'existing'), str(self.target), str(typed_target)})


    def test_queued_close_event_does_not_cancel_a_reopened_browser(self):
        self.click('#adopt-btn')
        self.click('#project-folder-browser')
        self.wait("document.querySelector('#folder-browser-select').disabled === false")
        self.browser.call('Network.enable')
        self.browser.call('Network.emulateNetworkConditions', {
            'offline': False, 'latency': 100, 'downloadThroughput': -1, 'uploadThroughput': -1})
        self.browser.evaluate("document.querySelector('#folder-browser-cancel').click(); document.querySelector('#project-folder-browser').click()")
        self.wait("document.querySelector('#folder-browser-select').disabled === false")
        self.assertTrue(self.browser.evaluate("document.querySelector('#folder-browser').open"))

    def test_new_project_and_repair_share_the_linux_browser(self):
        self.click('#new-btn')
        self.click('#project-name')
        self.browser.call('Input.insertText', {'text': 'New build'})
        parent = self.work
        self.type_path(parent)
        self.click('#project-folder-browser')
        self.wait("document.querySelector('#folder-browser-select').disabled === false")
        self.click('#folder-browser-select')
        self.wait("document.querySelector('#project-folder-preview').textContent.includes('/new-build')")
        self.click('#create-save')
        self.wait("document.querySelectorAll('.project').length === 2 && !document.querySelector('#create-dialog').open")
        self.assertTrue((parent / 'new-build' / '.harness').is_dir())
        existing = next(e for e in registry.entries(self.home) if e['name'] == 'Existing')
        moved = self.work / 'relocated'
        (self.work / 'existing').rename(moved)
        self.browser.evaluate('refresh()')
        self.browser.evaluate('openRepair(projectsById.get(' + json.dumps(existing['id']) + '))')
        self.click('#repair-folder-typed')
        self.browser.call('Input.insertText', {'text': str(moved)})
        self.click('#repair-folder-browser')
        self.wait("document.querySelector('#folder-browser-select').disabled === false")
        self.enter('#folder-browser-select')
        self.wait("document.querySelector('#repair-folder-path').textContent === " + json.dumps(str(moved)))
        self.enter('#repair-save')
        self.wait("!document.querySelector('#repair-dialog').open")
        updated = next(e for e in registry.entries(self.home) if e['id'] == existing['id'])
        self.assertEqual(updated['code_root'], str(moved))
        self.assertEqual(updated['data_root'], existing['data_root'])


if __name__ == '__main__':
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Real-page folder dialog where there is no native picker (Linux): places, drives, folders, New Folder, keyboard."""
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

    def key(self, name, code, number, text=''):
        for kind in ('keyDown', 'keyUp'):
            self.browser.call('Input.dispatchKeyEvent', {
                'type': kind, 'key': name, 'code': code, 'windowsVirtualKeyCode': number, 'text': text if kind == 'keyDown' else ''})

    def click_twice(self, selector):
        box = self.browser.evaluate("(() => {const n=document.querySelector(" + json.dumps(selector)
            + "); n.scrollIntoView({block:'nearest'}); const r=n.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
        for count in (1, 2):
            for kind in ('mousePressed', 'mouseReleased'):
                self.browser.call('Input.dispatchMouseEvent', {
                    'type': kind, 'x': box['x'], 'y': box['y'], 'button': 'left', 'clickCount': count})

    @staticmethod
    def item(path):
        return '.fb-item[data-path=' + json.dumps(str(path)) + ']'

    def open_dialog(self, button):
        self.click(button)
        self.wait("document.querySelector('#folder-browser').open && document.querySelector('#folder-browser-path').value !== ''"
                  " && !document.querySelector('#folder-browser-status').textContent.startsWith('Loading')")

    def go(self, path):
        """Type a path into the dialog's path bar and press Enter."""
        self.click('#folder-browser-path')
        self.browser.evaluate("document.querySelector('#folder-browser-path').select()")
        self.browser.call('Input.insertText', {'text': str(path)})
        self.key('Enter', 'Enter', 13, '\r')

    def at(self, path):
        self.wait("document.querySelector('#folder-browser-path').value === " + json.dumps(str(path))
                  + " && !document.querySelector('#folder-browser-status').textContent.startsWith('Loading')")

    def places(self):
        return self.browser.evaluate("Array.from(document.querySelectorAll('#folder-browser-places .fb-place')).map(n => n.textContent)")

    def screenshot(self, name):
        folder = os.environ.get('HARNESS_FOLDER_BROWSER_EVIDENCE')
        if folder:
            target = Path(folder)
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_bytes(base64.b64decode(
                self.browser.call('Page.captureScreenshot', {'format': 'png'})['data']))

    def test_linux_adoption_uses_a_folder_dialog_like_the_macos_one(self):
        self.click('#adopt-btn')
        self.assertTrue(self.browser.evaluate("document.querySelector('#project-folder-typed').hidden"))
        self.open_dialog('#project-folder-browse')
        self.assertEqual(self.browser.evaluate("document.querySelector('#folder-browser-prompt').textContent"),
                         'Choose the existing project folder to adopt')
        places = self.places()
        self.assertEqual([places[0], places[-1] if 'Computer' in places else None][0], 'Home')
        self.assertIn('Computer', places)
        self.screenshot('folder-dialog-open.png')
        self.go(self.target / 'missing')
        self.wait("document.querySelector('#folder-browser-error').textContent.includes('no folder')")
        self.assertTrue(self.browser.evaluate("document.querySelector('#folder-browser').open"))
        self.go(self.work)
        self.at(self.work)
        self.assertEqual(self.browser.evaluate("document.querySelector('#folder-browser-error').textContent"), '')
        self.click(self.item(self.target))
        self.wait("document.querySelector(" + json.dumps(self.item(self.target)) + ").getAttribute('aria-selected') === 'true'")
        self.screenshot('folder-dialog-selected.png')
        self.click('#folder-browser-select')
        self.wait("!document.querySelector('#folder-browser').open")
        self.wait("document.querySelector('#project-folder-path').textContent === " + json.dumps(str(self.target)))
        self.enter('#create-save')
        self.wait("document.querySelectorAll('.project').length === 2 && !document.querySelector('#create-dialog').open")
        self.assertEqual(list(self.target.iterdir()), [self.target / 'source.txt'])
        self.assertEqual((self.target / 'source.txt').read_text(), 'owner source remains untouched')

    def test_new_project_and_repair_use_the_same_dialog(self):
        self.click('#new-btn')
        self.click('#project-name')
        self.browser.call('Input.insertText', {'text': 'New build'})
        self.open_dialog('#project-folder-browse')
        self.go(self.work)
        self.at(self.work)
        self.click('#folder-browser-select')      # nothing highlighted: Choose takes the folder being shown
        self.wait("!document.querySelector('#folder-browser').open")
        self.wait("document.querySelector('#project-folder-preview').textContent.includes('/new-build')")
        self.click('#create-save')
        self.wait("document.querySelectorAll('.project').length === 2 && !document.querySelector('#create-dialog').open")
        self.assertTrue((self.work / 'new-build' / '.harness').is_dir())
        existing = next(e for e in registry.entries(self.home) if e['name'] == 'Existing')
        moved = self.work / 'relocated'
        (self.work / 'existing').rename(moved)
        self.browser.evaluate('refresh()')
        self.browser.evaluate('openRepair(projectsById.get(' + json.dumps(existing['id']) + '))')
        self.open_dialog('#repair-folder-browse')
        self.assertEqual(self.browser.evaluate("document.querySelector('#folder-browser-prompt').textContent"),
                         "Choose the project's current folder")
        self.go(self.work)
        self.at(self.work)
        self.click(self.item(moved))
        self.click('#folder-browser-select')
        self.wait("document.querySelector('#repair-folder-path').textContent === " + json.dumps(str(moved)))
        self.enter('#repair-save')
        self.wait("!document.querySelector('#repair-dialog').open")
        updated = next(e for e in registry.entries(self.home) if e['id'] == existing['id'])
        self.assertEqual(updated['code_root'], str(moved))
        self.assertEqual(updated['data_root'], existing['data_root'])

    def test_another_drive_is_a_place_the_owner_can_switch_to(self):
        drive = self.base / 'USB DISK'
        (drive / 'photos').mkdir(parents=True)
        mounts = self.base / 'mounts'
        mounts.write_text('/dev/sda1 / ext4 rw 0 0\n/dev/sdb1 ' + str(drive).replace(' ', '\\040') + ' vfat rw 0 0\n'
                          '/dev/loop3 /snap/core/1 squashfs ro 0 0\ntmpfs /run tmpfs rw 0 0\n')
        patch.object(project_manager, 'PROC_MOUNTS', str(mounts)).start()
        patch.object(project_manager, '_SYSTEM_MOUNTS', ()).start()
        self.click('#new-btn')
        self.open_dialog('#project-folder-browse')
        self.assertIn('\U0001F4BF USB DISK', self.places())
        self.assertFalse([name for name in self.places() if 'snap' in name or 'loop' in name])
        self.click_twice('#folder-browser-places .fb-place[title=' + json.dumps(str(drive)) + ']') if False else None
        self.click('#folder-browser-places .fb-place[title=' + json.dumps(str(drive)) + ']')
        self.at(drive)
        self.assertTrue(self.browser.evaluate("document.querySelector('#folder-browser-places .fb-place[aria-current=true]').title === " + json.dumps(str(drive))))
        self.wait("document.querySelector(" + json.dumps(self.item(drive / 'photos')) + ") !== null")
        self.click_twice(self.item(drive / 'photos'))
        self.at(drive / 'photos')
        self.click('#folder-browser-select')
        self.wait("document.querySelector('#project-folder-path').textContent === " + json.dumps(str(drive / 'photos')))
        self.screenshot('folder-dialog-drive.png')

    def test_new_folder_and_keyboard_navigation(self):
        for name in ('alpha', 'beta', '.hidden'):
            (self.work / name).mkdir()
        self.click('#new-btn')
        self.open_dialog('#project-folder-browse')
        self.go(self.work)
        self.at(self.work)
        self.assertIsNone(self.browser.evaluate("document.querySelector(" + json.dumps(self.item(self.work / '.hidden')) + ")"))   # hidden folders are not listed, as on macOS
        self.click('#folder-browser-new')
        self.wait("document.activeElement.id === 'folder-browser-newname'")
        self.browser.call('Input.insertText', {'text': 'beta'})
        self.key('Enter', 'Enter', 13, '\r')
        self.wait("document.querySelector('#folder-browser-error').textContent.includes('already exists')")
        self.assertTrue(self.browser.evaluate("document.querySelector('#folder-browser').open && !!document.querySelector('#folder-browser-newname')"), 'Enter in the New Folder box closed the dialog')
        self.browser.evaluate("document.querySelector('#folder-browser-newname').select()")
        self.browser.call('Input.insertText', {'text': 'fresh'})
        self.key('Enter', 'Enter', 13, '\r')
        self.wait("document.querySelector(" + json.dumps(self.item(self.work / 'fresh')) + ") !== null && !document.querySelector('#folder-browser-newrow')")
        self.assertTrue((self.work / 'fresh').is_dir())
        self.assertEqual(self.browser.evaluate("document.querySelector(" + json.dumps(self.item(self.work / 'fresh')) + ").getAttribute('aria-selected')"), 'true')
        # Keyboard only: arrows move the highlight, Enter opens it, Backspace goes up.
        self.browser.evaluate("document.querySelector('#folder-browser-list').focus()")
        self.key('Home', 'Home', 36)
        selected = lambda: self.browser.evaluate("(document.querySelector('.fb-item[aria-selected=true]') || {dataset: {}}).dataset.path")
        self.assertEqual(selected(), str(self.work / 'alpha'))
        self.key('ArrowDown', 'ArrowDown', 40)
        self.assertEqual(selected(), str(self.work / 'beta'))
        self.key('Enter', 'Enter', 13, '\r')
        self.at(self.work / 'beta')
        self.key('Backspace', 'Backspace', 8)
        self.at(self.work)
        # Escape cancels: the form is exactly as it was.
        self.key('Escape', 'Escape', 27)
        self.wait("!document.querySelector('#folder-browser').open")
        self.assertEqual(self.browser.evaluate("document.querySelector('#project-folder-path').textContent"), 'No folder selected')

    def test_a_mistyped_path_is_never_replaced_by_the_previous_folder_on_choose(self):
        """Review round 1: typing a wrong path then Choose used to take the folder shown before, silently."""
        self.click('#new-btn')
        self.open_dialog('#project-folder-browse')
        self.go(self.work)
        self.at(self.work)
        self.go(self.work / 'mistyped destination')
        self.wait("document.querySelector('#folder-browser-error').textContent.includes('no folder')")
        self.click('#folder-browser-select')
        self.wait("document.querySelector('#folder-browser-error').textContent.includes('there is no folder')")
        self.assertTrue(self.browser.evaluate("document.querySelector('#folder-browser').open"))
        self.assertEqual(self.browser.evaluate("document.querySelector('#project-folder-path').textContent"), 'No folder selected')
        # The same for a folder that cannot be opened: the refusal stays, nothing else is chosen.
        locked = self.work / 'locked folder'
        locked.mkdir()
        locked.chmod(0o000)
        self.addCleanup(lambda: locked.chmod(0o700))
        if os.geteuid() != 0:
            self.go(self.work)
            self.at(self.work)
            self.click_twice(self.item(locked))
            self.wait("document.querySelector('#folder-browser-error').textContent !== ''")
            self.click('#folder-browser-select')
            self.wait("document.querySelector('#folder-browser-error').textContent !== ''")
            self.assertTrue(self.browser.evaluate("document.querySelector('#folder-browser').open"))
            self.assertEqual(self.browser.evaluate("document.querySelector('#project-folder-path').textContent"), 'No folder selected')

    def test_a_path_typed_without_pressing_enter_is_what_choose_takes(self):
        self.click('#new-btn')
        self.open_dialog('#project-folder-browse')
        self.click(self.item(self.work) if False else '#folder-browser-path')
        self.browser.evaluate("document.querySelector('#folder-browser-path').select()")
        self.browser.call('Input.insertText', {'text': str(self.work)})
        self.click('#folder-browser-select')
        self.wait("!document.querySelector('#folder-browser').open")
        self.wait("document.querySelector('#project-folder-path').textContent === " + json.dumps(str(self.work)))

    def test_a_folder_that_is_already_a_project_is_refused_inside_the_dialog(self):
        self.click('#adopt-btn')
        self.open_dialog('#project-folder-browse')
        self.go(self.work)
        self.at(self.work)
        self.click(self.item(self.work / 'existing'))
        self.click('#folder-browser-select')
        self.wait("document.querySelector('#folder-browser-error').textContent.includes('already a project')")
        self.assertTrue(self.browser.evaluate("document.querySelector('#folder-browser').open"))
        self.assertEqual(self.browser.evaluate("document.querySelector('#project-folder-path').textContent"), 'No folder selected')


if __name__ == '__main__':
    unittest.main()

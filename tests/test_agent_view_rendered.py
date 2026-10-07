# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Real Linux tmux sessions, HTTP routes and browser keyboard interaction."""
from __future__ import annotations

import os
import base64
import json
import signal
import contextlib
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from harness import board_viewer, browser_acceptance, control, project_manager, project_registry
from harness.platform_support import linux
from harness.project_context import project_context
from tests import test_folder_browser_rendered as rendered
from tests.environment_support import require_loopback


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('tmux'), 'requires real Linux and tmux')
class AgentViewRenderedTests(unittest.TestCase):
    wait = rendered.FolderBrowserRenderedTests.wait
    click = rendered.FolderBrowserRenderedTests.click

    def wait_terminal_text(self, text):
        self.wait("agentTerminal && Array.from({length:agentTerminal.buffer.active.length}, (_,i)=>agentTerminal.buffer.active.getLine(i).translateToString()).join('\\n').includes(" + json.dumps(text) + ")")

    def capture_view(self, name):
        self.assertTrue(self.browser.evaluate("document.querySelector('.xterm-screen').getBoundingClientRect().height > 0"))
        folder = os.environ.get('HARNESS_AGENT_VIEW_EVIDENCE')
        if folder:
            Path(folder).mkdir(parents=True, exist_ok=True)
            (Path(folder) / name).write_bytes(base64.b64decode(
                self.browser.call('Page.captureScreenshot', {'format': 'png'})['data']))

    def setUp(self):
        require_loopback()
        browser_acceptance.resolve_binary()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'project'
        self.root.mkdir()
        manager_mode = os.environ.get('HARNESS_AGENT_VIEW_MANAGER') == '1'
        self.real_clis = os.environ.get('HARNESS_AGENT_VIEW_REAL_CLIS') == '1' or manager_mode
        isolated_env = {'TMUX': ''}
        if self.real_clis:
            home = self.base / 'home'
            (home / '.codex').mkdir(parents=True)
            (home / '.claude').mkdir()
            isolated_env.update({
                'HOME': str(home), 'CODEX_HOME': str(home / '.codex'),
                'CLAUDE_CONFIG_DIR': str(home / '.claude'),
                'HARNESS_CODEX_BIN': shutil.which('codex') or '',
                'HARNESS_CLAUDE_BIN': shutil.which('claude') or '',
                'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            })
        socket_dir = self.base / 'tmux'
        socket_dir.mkdir()
        isolated_env['TMUX_TMPDIR'] = str(socket_dir)
        env_patch = patch.dict(os.environ, isolated_env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.addCleanup(lambda: subprocess.run(['tmux', 'kill-server'], capture_output=True))
        self.manager_home = None
        self.manager = None
        if manager_mode:
            self.manager_home = self.base / 'manager'
            entry = project_registry.register(self.manager_home, 'Agent view proof', self.root, kind='adopted')
            self.root = project_registry.context_for_entry(entry)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                worker_port = probe.getsockname()[1]
            self.manager = project_manager.ProjectManager(self.manager_home, board_port=worker_port)
            self.addCleanup(self.manager.shutdown)
            self.server = ThreadingHTTPServer(('127.0.0.1', 0), project_manager.make_handler(self.manager))
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            self.addCleanup(self.server.server_close)
            self.addCleanup(self.server.shutdown)
            origin = f'http://127.0.0.1:{self.server.server_address[1]}'
            self.manager.manager_url = origin
            self.manager.public_board_url = origin + '/project/'
            self.manager.open_project(entry['id'])
        self.owned = []
        for kind in (() if manager_mode else ('codex_delivery', 'claude_reviewer', 'claude_cto')):
            session = control.create(self.root, kind)
            if self.real_clis:
                board_viewer.launch_terminal(self.root, session, manager_home=self.manager_home)
            else:
                linux.TERMINAL_HOST.open_session(session['id'], [
                    '/bin/bash', '--noprofile', '--norc', '-c',
                    'printf "LIVE_SESSION_READY\\n"; while IFS= read -r line; do printf "RECEIVED:%s\\n" "$line"; done',
                ], color_rgb=(0, 0, 0))
                pid = subprocess.check_output(['tmux', 'display-message', '-p', '-t',
                    linux.TERMINAL_HOST._pane(session['id']), '#{pane_pid}'], text=True)
                control.attach(self.root, session['id'], int(pid.strip()))
            self.owned.append(session)
        if not manager_mode:
            self.server = ThreadingHTTPServer(('127.0.0.1', 0), board_viewer.make_handler(self.root))
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            self.addCleanup(self.server.server_close)
            self.addCleanup(self.server.shutdown)
        url = f'http://127.0.0.1:{self.server.server_address[1]}/' + ('project/' if manager_mode else '')
        self.session = browser_acceptance.launch(
            url, self.base / 'browser', width=1280, height=900)
        self.addCleanup(self.session.close)
        port, path = browser_acceptance._devtools(self.base / 'browser', time.monotonic() + 15)
        self.browser = browser_acceptance._DevToolsSocket(port, path, timeout=15)
        self.addCleanup(self.browser.close)
        if manager_mode:
            self.wait("document.querySelector('#codex') !== null")
            for index, button in enumerate(('#codex', '#claude', '#cto')):
                self.click(button)
                self.wait("document.querySelector('#color-dialog').open")
                self.click('#color-cancel')
                self.wait("!document.querySelector('#color-dialog').open")
                self.wait("document.querySelectorAll('#agents .agent-row').length === " + str(index + 1))
            sessions = control.snapshot(self.root)['sessions']
            self.owned = [next(s for s in sessions if s['kind'] == kind)
                          for kind in ('codex_delivery', 'claude_reviewer', 'claude_cto')]
        self.wait("document.querySelectorAll('#agents .agent-row').length === 3")

    def test_each_role_view_interacts_closes_and_reopens_without_new_session(self):
        original_ids = {s['id'] for s in control.snapshot(self.root)['sessions']}
        for index, session in enumerate(self.owned):
            selector = '#agents [data-session-id="' + session['id'] + '"] .actions button:first-child'
            self.click(selector)
            self.wait("document.querySelector('#agent-view-dialog').open && document.querySelector('.xterm-screen') !== null")
            ready = ('Sign in with ChatGPT' if index == 0 else "Let's get started.") if self.real_clis else 'LIVE_SESSION_READY'
            self.wait_terminal_text(ready)
            if self.real_clis:
                before = linux.TERMINAL_HOST.capture_session(session['id'])['screen']
                self.browser.call('Input.dispatchKeyEvent', {
                    'type': 'keyDown', 'key': 'ArrowDown', 'code': 'ArrowDown', 'windowsVirtualKeyCode': 40})
                deadline = time.monotonic() + 15
                while linux.TERMINAL_HOST.capture_session(session['id'])['screen'] == before and time.monotonic() < deadline:
                    time.sleep(.1)
                self.assertNotEqual(linux.TERMINAL_HOST.capture_session(session['id'])['screen'], before)
                self.wait_terminal_text('> 2.' if index == 0 else '❯ 3.')
                self.capture_view('real-role-' + str(index) + '-interacted.png')
            else:
            # Input goes through xterm's real keyboard event and the HTTP input route.
                for character in 'role-' + str(index):
                    self.browser.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': character, 'text': character})
                    self.browser.call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': character})
                self.browser.call('Input.dispatchKeyEvent', {
                    'type': 'keyDown', 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13, 'text': '\r'})
                self.wait_terminal_text('RECEIVED:role-' + str(index))
                self.capture_view('role-' + str(index) + '-interacted.png')
            self.click('#agent-view-close')
            self.wait("!document.querySelector('#agent-view-dialog').open")
            self.click(selector)
            self.wait("document.querySelector('#agent-view-dialog').open && document.querySelector('.xterm-screen') !== null")
            self.wait_terminal_text(ready if self.real_clis else 'RECEIVED:role-' + str(index))
            self.assertEqual({s['id'] for s in control.snapshot(self.root)['sessions']}, original_ids)
            self.assertTrue(all(s['status'] == 'running' for s in control.snapshot(self.root)['sessions']))
            self.click('#agent-view-close')

    def test_missing_profile_shows_guidance_without_starting_another_terminal(self):
        # A copied executable has no path-matched AppArmor profile. Loaded
        # system policy is untouched, and no restriction is switched off.
        binary = linux.AGENT_CONFINEMENT.binary()
        copied = self.base / 'bwrap-no-profile'
        shutil.copy2(binary, copied)
        result = subprocess.run([str(copied), '--die-with-parent', '--ro-bind', '/', '/',
            '--dev-bind', '/dev', '/dev', '--proc', '/proc', '--', '/bin/true'], capture_output=True, text=True)
        if 'setting up uid map: Permission denied' not in result.stderr:
            self.skipTest('host does not restrict the unprofiled executable through AppArmor')
        before = subprocess.check_output(['tmux', 'list-sessions', '-F', '#{session_name}'], text=True)
        with patch.dict(os.environ, {'HARNESS_BWRAP_BIN': str(copied)}):
            self.click('#codex')
            self.wait("document.querySelector('#color-dialog').open")
            self.click('#color-cancel')
            self.wait("document.querySelector('#notice').textContent.includes('apparmor-profiles')")
            message = self.browser.evaluate("document.querySelector('#notice').textContent")
            self.assertIn('bwrap-userns-restrict', message)
            self.assertIn('apparmor_parser -r', message)
            self.assertIn('Permission denied', message)
        after = subprocess.check_output(['tmux', 'list-sessions', '-F', '#{session_name}'], text=True)
        self.assertEqual(after, before)


@unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('HARNESS_TEST_NATIVE_AGENT_VIEW') == '1',
                     'explicit macOS native Terminal proof')
class NativeAgentViewRenderedTests(unittest.TestCase):
    wait = rendered.FolderBrowserRenderedTests.wait
    click = rendered.FolderBrowserRenderedTests.click

    def test_each_native_button_focuses_existing_owned_terminal_without_duplicate(self):
        require_loopback()
        browser_binary = browser_acceptance.resolve_binary()
        from harness.platform_support import defaults
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        with contextlib.nullcontext(temporary_directory.name) as temporary:
            base = Path(temporary)
            root = base / 'project'
            root.mkdir()
            manager_mode = os.environ.get('HARNESS_AGENT_VIEW_MANAGER') == '1'
            real_clis = os.environ.get('HARNESS_AGENT_VIEW_REAL_CLIS') == '1'
            if manager_mode:
                (base / 'home' / '.codex').mkdir(parents=True)
                (base / 'home' / '.claude').mkdir()
                env_patch = patch.dict(os.environ, {
                    'HOME': str(base / 'home'), 'CODEX_HOME': str(base / 'home' / '.codex'),
                    'CLAUDE_CONFIG_DIR': str(base / 'home' / '.claude'),
                    'HARNESS_BROWSER_BIN': browser_binary,
                    'HARNESS_CODEX_BIN': shutil.which('codex') or '',
                    'HARNESS_CLAUDE_BIN': shutil.which('claude') or '',
                    'OPENAI_API_KEY': '', 'ANTHROPIC_API_KEY': '',
                })
                env_patch.start()
                self.addCleanup(env_patch.stop)
                entry = project_registry.register(base / 'manager', 'Native agent view proof', root, kind='adopted')
                root = project_registry.context_for_entry(entry)
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    worker_port = probe.getsockname()[1]
                manager = project_manager.ProjectManager(base / 'manager', board_port=worker_port)
                self.addCleanup(manager.shutdown)
                server = ThreadingHTTPServer(('127.0.0.1', 0), project_manager.make_handler(manager))
                origin = f'http://127.0.0.1:{server.server_address[1]}'
                manager.manager_url = origin
                manager.public_board_url = origin + '/project/'
                manager.open_project(entry['id'])
            child = base / 'native_session.py'
            child.write_text(
                'import os,sys,json\nfrom pathlib import Path\n'
                'sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[1])) + ')\n'
                'from harness import control\n'
                'from harness.project_context import ProjectContext\n'
                'control.attach(ProjectContext(*map(Path,json.loads(sys.argv[1]))),sys.argv[2],os.getpid())\n'
                'print("NATIVE_SESSION_READY",flush=True)\n'
                'for line in sys.stdin:\n'
                '    with open(sys.argv[3],"a") as output: output.write(line)\n'
                '    print("RECEIVED:"+line.strip(),flush=True)\n')
            owned = []
            try:
                for kind in ('codex_delivery', 'claude_reviewer', 'claude_cto'):
                    session = control.create(root, kind)
                    owned.append(session)
                    if real_clis:
                        board_viewer.launch_terminal(root, session, manager_home=base / 'manager')
                    else:
                        defaults.TERMINAL_HOST.open_session(session['id'], [
                        '/usr/bin/env', '-i', 'HOME=' + str(base), 'PATH=/usr/bin:/bin',
                        sys.executable, str(child), json.dumps([str(getattr(project_context(root), name)) for name in ('code_root', 'data_root', 'workspace_root')]), session['id'], str(base / (session['id'] + '.input')),
                        '--data-root', str(project_context(root).data_root),
                    ], color_rgb=(0, 0, 0))
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    running = control.snapshot(root)['sessions']
                    if len(running) == 3 and all(s['status'] == 'running' for s in running):
                        break
                    time.sleep(.1)
                self.assertTrue(all(s['status'] == 'running' for s in running))
                if not manager_mode:
                    server = ThreadingHTTPServer(('127.0.0.1', 0), board_viewer.make_handler(root))
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
                browser_session = browser_acceptance.launch(
                    f'http://127.0.0.1:{server.server_address[1]}/' + ('project/' if manager_mode else ''), base / 'browser', width=1280, height=900)
                self.addCleanup(browser_session.close)
                port, path = browser_acceptance._devtools(base / 'browser', time.monotonic() + 15)
                self.browser = browser_acceptance._DevToolsSocket(port, path, timeout=15)
                self.addCleanup(self.browser.close)
                self.wait("document.querySelectorAll('#agents .agent-row').length === 3")
                ids = {s['id'] for s in running}
                for session in running:
                    selector = '#agents [data-session-id="' + session['id'] + '"] .actions button:first-child'
                    self.browser.evaluate("document.querySelector('#notice').textContent='' ")
                    self.click(selector)
                    self.wait("document.querySelector('#notice').textContent.includes('existing agent session is open')")
                    tty = subprocess.check_output(['/usr/bin/osascript', '-e',
                        'tell application "Terminal" to get tty of selected tab of front window'], text=True).strip()
                    original_pid = session['pid']
                    # Send input only to the exact owned tab; no new CLI or shell is created.
                    script = '''on run argv
tell application "Terminal"
repeat with w in windows
repeat with t in tabs of w
if tty of t is item 1 of argv then
do script "native-input" in t
return "sent"
end if
end repeat
end repeat
end tell
end run'''
                    output = base / (session['id'] + '.input')
                    def interact(target_tty, count):
                        if real_clis:
                            capture_script = script.replace('do script "native-input" in t\nreturn "sent"', 'set selected tab of w to t\nreturn contents of selected tab of w')
                            def contents():
                                return subprocess.check_output(['/usr/bin/osascript', '-e', capture_script, target_tty], text=True)
                            ready = 'Sign in with ChatGPT' if session['kind'] == 'codex_delivery' else "Let's get started."
                            deadline = time.monotonic() + 15
                            before = contents()
                            while ready not in before and time.monotonic() < deadline:
                                time.sleep(.1)
                                before = contents()
                            self.assertIn(ready, before)
                            directory = project_context(root).storage_path('control', 'terminal-sockets')
                            subprocess.run(['/usr/bin/screen', '-c', '/dev/null', '-S', defaults.TERMINAL_HOST._screen_name(session['id']), '-X', 'stuff', '\x1b[B'],
                                           env={**os.environ, 'SCREENDIR': str(directory)}, check=True, capture_output=True)
                            deadline = time.monotonic() + 5
                            while contents() == before and time.monotonic() < deadline:
                                time.sleep(.1)
                            self.assertNotEqual(contents(), before)
                        else:
                            subprocess.run(['/usr/bin/osascript', '-e', script, target_tty], check=True, capture_output=True)
                            deadline = time.monotonic() + 5
                            while (not output.exists() or output.read_text().count('native-input') < count) and time.monotonic() < deadline:
                                time.sleep(.1)
                            self.assertEqual(output.read_text().count('native-input'), count)
                    interact(tty, 1)
                    # Close only this owned view. Screen must retain the live CLI.
                    subprocess.run(['/usr/bin/osascript', '-e', '''on run argv
tell application "Terminal"
repeat with w in windows
if (count of tabs of w) is 1 and tty of tab 1 of w is item 1 of argv then
close w saving no
return
end if
end repeat
end tell
end run''', tty], check=True, capture_output=True)
                    self.assertEqual(next(s for s in control.snapshot(root)['sessions'] if s['id'] == session['id'])['pid'], original_pid)
                    self.browser.evaluate("document.querySelector('#notice').textContent='' ")
                    self.click(selector)
                    self.wait("document.querySelector('#notice').textContent.includes('existing agent session is open')")
                    reopened_tty = subprocess.check_output(['/usr/bin/osascript', '-e',
                        'tell application "Terminal" to get tty of selected tab of front window'], text=True).strip()
                    interact(reopened_tty, 2)
                    self.assertTrue(all(s['status'] == 'running' for s in control.snapshot(root)['sessions']))
                    self.assertEqual({s['id'] for s in control.snapshot(root)['sessions']}, ids)
            finally:
                for session in control.snapshot(root)['sessions']:
                    if session.get('pid'):
                        try:
                            os.kill(session['pid'], signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    title = defaults.role_title(session['id']) + ' [' + session['id'] + ']'
                    if title:
                        # Close only a single-tab test window matching our owned TTY.
                        script = '''on run argv
tell application "Terminal"
repeat with w in windows
if (count of tabs of w) is 1 then
if custom title of tab 1 of w is item 1 of argv then
if not busy of tab 1 of w then close w
return
end if
end if
end repeat
end tell
end run'''
                        subprocess.run(['/usr/bin/osascript', '-e', script, title], capture_output=True, timeout=5)

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Opt-in real manager, managed CTO/Reviewer, real OAuth auth and model turns."""
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from harness import browser_acceptance, control, project_manager, project_registry
from tests import test_folder_browser_rendered as rendered
from tests.environment_support import require_loopback


@unittest.skipUnless(os.environ.get('HARNESS_TEST_REAL_CLAUDE_TOKEN')=='1', 'requires explicitly supplied setup-token')
class ClaudeAuthE2ETests(unittest.TestCase):
    wait=rendered.FolderBrowserRenderedTests.wait
    click=rendered.FolderBrowserRenderedTests.click
    enter=rendered.FolderBrowserRenderedTests.enter

    def test_cto_and_reviewer_use_oauth_token_and_complete_real_turns(self):
        require_loopback()
        browser_binary = browser_acceptance.resolve_binary()
        real_cli=shutil.which('claude')
        self.assertTrue(real_cli)
        self.assertTrue(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'))
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);home=base/'home';home.mkdir();code=base/'code';code.mkdir()
            wrapper=code/'proof-claude'
            wrapper.write_text('''#!/usr/bin/env python3
import json,os,subprocess,sys,time
real=os.environ['HARNESS_TEST_REAL_CLAUDE']
if '--version' in sys.argv or '-p' in sys.argv or (len(sys.argv)>1 and sys.argv[1]=='auth'):
    os.execvpe(real,[real,*sys.argv[1:]],os.environ)
status=subprocess.run([real,'auth','status'],capture_output=True,text=True)
try: method=json.loads(status.stdout).get('authMethod')
except ValueError: method='unknown'
if status.returncode or method!='oauth_token':
    print('AUTH_PROOF_FAILED',flush=True);sys.exit(2)
print('AUTH_METHOD=oauth_token',flush=True)
turn=subprocess.run([real,'-p','Reply with exactly OK.','--model','haiku','--output-format','json','--no-session-persistence','--max-turns','1','--tools','','--setting-sources',''],capture_output=True,text=True)
try: result=json.loads(turn.stdout)
except ValueError: result={'is_error':True}
if turn.returncode or result.get('is_error'):
    print('TURN_PROOF_FAILED',flush=True);sys.exit(2)
print('REAL_TURN_OK',flush=True)
while True: time.sleep(.2)
''');wrapper.chmod(0o755)
            socketdir=base/'tmux';socketdir.mkdir()
            with patch.dict(os.environ,{'HARNESS_BROWSER_BIN':str(browser_binary),'HOME':str(home),'CLAUDE_CONFIG_DIR':str(home/'.claude'),'CODEX_HOME':str(home/'.codex'),'HARNESS_CLAUDE_BIN':str(wrapper),'HARNESS_TEST_REAL_CLAUDE':real_cli,'TMUX':'','TMUX_TMPDIR':str(socketdir),'HARNESS_CLAUDE_TOKEN_SOURCE':'environment'}):
                entry=project_registry.register(base/'manager','Claude authentication proof',code,kind='adopted')
                context=project_registry.context_for_entry(entry)
                with socket.socket() as sock:sock.bind(('127.0.0.1',0));worker_port=sock.getsockname()[1]
                manager=project_manager.ProjectManager(base/'manager',board_port=worker_port)
                server=ThreadingHTTPServer(('127.0.0.1',0),project_manager.make_handler(manager))
                origin=f'http://127.0.0.1:{server.server_address[1]}'
                manager.manager_url=origin;manager.public_board_url=origin+'/project/'
                threading.Thread(target=server.serve_forever,daemon=True).start()
                browser_session=None;self.browser=None
                try:
                    manager.open_project(entry['id'])
                    browser_session=browser_acceptance.launch(origin+'/project/',base/'browser',width=1280,height=1000)
                    port,path=browser_acceptance._devtools(base/'browser',time.monotonic()+15)
                    self.browser=browser_acceptance._DevToolsSocket(port,path,timeout=60)
                    self.wait("document.readyState==='complete' && document.querySelector('#cto')?.textContent.includes('opus') && !document.querySelector('#cto').disabled")
                    for kind,button in (('claude_cto','#cto'),('claude_reviewer','#claude')):
                        self.enter(button);self.wait("document.querySelector('#color-dialog').open");self.click('#color-cancel')
                        deadline=time.monotonic()+90
                        session=None
                        while time.monotonic()<deadline:
                            session=next((item for item in control._read_state(context)['sessions'].values() if item['kind']==kind),None)
                            if session:
                                transcript=context.data_root/'control'/'transcripts'/(session['id']+'.log')
                                if transcript.exists() and 'REAL_TURN_OK' in transcript.read_text():break
                            time.sleep(.2)
                        self.assertIsNotNone(session)
                        text=transcript.read_text() if transcript.exists() else ''
                        self.assertIn('AUTH_METHOD=oauth_token',text)
                        self.assertIn('REAL_TURN_OK',text)
                        self.assertNotIn('login expired',text.lower());self.assertNotIn('keychain is locked',text.lower())
                        self.assertTrue(os.environ['CLAUDE_CODE_OAUTH_TOKEN'] not in text, 'token persisted in transcript')
                        print(kind+': HARNESS_OAUTH_TOKEN_REAL_TURN_OK',flush=True)
                    self.wait("document.querySelectorAll('#agents .agent-row').length===2")
                    evidence=os.environ.get('HARNESS_CLAUDE_AUTH_EVIDENCE')
                    if evidence:
                        Path(evidence).mkdir(parents=True,exist_ok=True)
                        (Path(evidence)/('real-claude-'+sys.platform+'.png')).write_bytes(base64.b64decode(self.browser.call('Page.captureScreenshot',{'format':'png'})['data']))
                    for item in [*context.data_root.rglob('*'), *home.rglob('*')]:
                        if item.is_file(): self.assertTrue(os.environ['CLAUDE_CODE_OAUTH_TOKEN'].encode() not in item.read_bytes(),'token persisted in project or CLI storage')
                finally:
                    for session in control._read_state(context).get('sessions',{}).values():
                        try:control.stop(context,session['id'])
                        except (OSError,ValueError):pass
                    if self.browser:self.browser.close()
                    if browser_session:browser_session.close()
                    manager.shutdown();server.shutdown();server.server_close()
                    if sys.platform.startswith('linux'):subprocess.run(['tmux','kill-server'],capture_output=True)
                    else:
                        # Only exact owned native titles; never touch another window.
                        for session in control._read_state(context).get('sessions',{}).values():
                            from harness.platform_support.defaults import role_title
                            subprocess.run(['/usr/bin/osascript','-e','on run argv','-e','tell application "Terminal"','-e','repeat with w in windows','-e','repeat with t in tabs of w','-e','if custom title of t is item 1 of argv then close w saving no','-e','end repeat','-e','end repeat','-e','end tell','-e','end run',role_title(session['id'])],capture_output=True)

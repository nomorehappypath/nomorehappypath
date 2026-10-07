# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Actual missing/rejected launch requests produce a readable owner-action card."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from harness import board, board_viewer, claude_auth, control, project_worker, project_memory
from harness.board_surface import SessionTokenAuthority
from tests.environment_support import require_loopback

PROBE = r'''<script>
(async()=>{
 for(let i=0;i<150&&!document.querySelector('#claude-auth-actions .owner-action');i++)await new Promise(r=>setTimeout(r,100));
 const section=document.querySelector('#claude-auth-actions');
 const card=section?.querySelector('.owner-action');
 await fetch('/__probe__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rendered:!!document.querySelector('#agents'),visible:!!card&&card.getBoundingClientRect().height>0&&!section.hidden,text:section?.innerText||''})});
})();</script>'''


class ClaudeAuthRenderedTests(unittest.TestCase):
    def test_missing_and_rejected_tokens_show_setup_card_without_terminal(self):
        from tests.test_branding_rendered import probe_proxy
        from harness import browser_acceptance
        import time
        require_loopback()
        with tempfile.TemporaryDirectory() as tmp:
            self.root = Path(tmp)
            project_memory.initialize(self.root, project_name='Claude setup proof', description='Token-only agent authentication')
            authority = SessionTokenAuthority(self.root)
            worker = ThreadingHTTPServer(('127.0.0.1', 0), project_worker.make_handler(self.root, authority=authority, endpoint=lambda:'http://127.0.0.1:1', bootstrap_socket=lambda:'/tmp/unused.sock'))
            threading.Thread(target=worker.serve_forever, daemon=True).start()
            try:
                for reason in ('missing','rejected'):
                    with patch.object(claude_auth, 'resolve_token', side_effect=claude_auth.ClaudeAuthRequired(reason)), patch('harness.platform_support.terminal_host') as host:
                        request = Request(f'http://127.0.0.1:{worker.server_address[1]}/api/sessions', data=b'{"kind":"claude_cto"}', headers={'Content-Type':'application/json'}, method='POST')
                        with self.assertRaises(HTTPError) as caught: urlopen(request)
                        response = json.load(caught.exception)
                        self.assertIn('setup-token', response['error'])
                        host.assert_not_called()
                    sink = {}
                    proxy = ThreadingHTTPServer(('127.0.0.1',0), probe_proxy(f'http://127.0.0.1:{worker.server_address[1]}',sink,PROBE))
                    threading.Thread(target=proxy.serve_forever,daemon=True).start()
                    try:
                        with tempfile.TemporaryDirectory() as profile:
                            session = browser_acceptance.launch(f'http://127.0.0.1:{proxy.server_address[1]}/',Path(profile),width=1280,height=1000)
                            try:
                                deadline=time.monotonic()+30
                                while 'value' not in sink and time.monotonic()<deadline: time.sleep(.1)
                            finally: session.close()
                        self.assertIn('value',sink,'Chrome never reported the rendered page')
                        self.assertTrue(sink['value']['rendered'])
                        self.assertTrue(sink['value']['visible'])
                        self.assertIn('Set up Claude agent authentication',sink['value']['text'])
                        self.assertIn('claude setup-token',sink['value']['text'])
                        self.assertIn('retry',sink['value']['text'])
                        if reason=='rejected': self.assertIn('rejected',sink['value']['text'])
                    finally: proxy.shutdown();proxy.server_close()
            finally: worker.shutdown();worker.server_close()

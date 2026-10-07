# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A failed status response must not erase the last visible agents."""
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, control
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy


class AgentStatusFailureRenderedTests(unittest.TestCase):
    def test_failed_dashboard_and_control_keep_last_known_agents_and_show_error(self):
        require_loopback()
        browser_acceptance.resolve_binary()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for kind, role, vendor in (
                ('codex_delivery', 'engineering', 'OpenAI'),
                ('claude_reviewer', 'qa', 'Anthropic'),
                ('claude_reviewer', 'qa', 'Anthropic'),
                ('claude_cto', 'cto', 'Anthropic'),
            ):
                session = control.create(root, kind)
                control.attach(root, session['id'], os.getpid())
                board.register(root, role, board.AWAITING_OWNER_DIRECTION,
                               vendor=vendor, session_id=session['id'])
            base = board_viewer.make_handler(root)
            broken = {'path': ''}

            class Handler(base):
                def do_GET(self):
                    if self.path.startswith('/__break__/'):
                        broken['path'] = '/api/' + self.path.rsplit('/', 1)[1]
                        return self.send_json(200, {})
                    if self.path == broken['path']:
                        return self.send_json(502, {'error': 'temporary status failure'})
                    return super().do_GET()

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                for endpoint in ('control', 'dashboard'):
                    with self.subTest(endpoint=endpoint):
                        broken['path'] = ''
                        sink = {}
                        probe = r'''<script>
(async()=>{
 for(let i=0;i<200&&document.querySelector('#active').textContent!=='4';i++)await new Promise(r=>setTimeout(r,50));
 while(refreshing)await new Promise(r=>setTimeout(r,20));
 await fetch('/__break__/ENDPOINT');
 for(let i=0;i<3;i++){while(refreshing)await new Promise(r=>setTimeout(r,20));await refresh();}
 const failed={
  active:document.querySelector('#active').textContent,
  rows:document.querySelectorAll('#agents .agent-row').length,
  notice:document.querySelector('#notice').textContent,
  connection:document.querySelector('.top .live').textContent,
  overlay:document.querySelector('#board-offline').innerText,
  visible:document.querySelector('#board-offline').getBoundingClientRect().height>0
 };
 await fetch('/__break__/restored');
 while(refreshing)await new Promise(r=>setTimeout(r,20));
 await refresh();
 failed.recovered={notice:document.querySelector('#notice').textContent,
  connection:document.querySelector('.top .live').textContent,
  overlay:document.querySelector('#board-offline').getBoundingClientRect().height>0};
 await fetch('/__probe__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(failed)});
})();</script>'''.replace('ENDPOINT', endpoint)
                        proxy = ThreadingHTTPServer(('127.0.0.1', 0), probe_proxy(
                            f'http://127.0.0.1:{server.server_address[1]}', sink, probe))
                        threading.Thread(target=proxy.serve_forever, daemon=True).start()
                        try:
                            with tempfile.TemporaryDirectory() as profile:
                                browser = browser_acceptance.launch(
                                    f'http://127.0.0.1:{proxy.server_address[1]}/', Path(profile))
                                try:
                                    deadline = time.monotonic() + 20
                                    while 'value' not in sink and time.monotonic() < deadline:
                                        time.sleep(.1)
                                finally:
                                    browser.close()
                            self.assertIn('value', sink)
                            value = sink['value']
                            self.assertEqual(value['active'], '4', value)
                            self.assertEqual(value['rows'], 4, value)
                            self.assertIn('temporary status failure', value['notice'])
                            self.assertIn('Status unavailable', value['connection'])
                            self.assertTrue(value['visible'])
                            self.assertIn('connection unavailable', value['overlay'])
                            self.assertNotIn('This project is closed', value['overlay'])
                            self.assertEqual(value['recovered']['notice'], '')
                            self.assertIn('Board connected', value['recovered']['connection'])
                            self.assertFalse(value['recovered']['overlay'])
                        finally:
                            proxy.shutdown(); proxy.server_close()
            finally:
                server.shutdown(); server.server_close()

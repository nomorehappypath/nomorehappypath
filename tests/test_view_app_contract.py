"""Black-box checks for the delivered task card and View app owner path."""
import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

from harness import board, board_viewer, browser_acceptance, control, release_preview, workspace_settings
from tests.test_branding_rendered import probe_proxy
from tests.environment_support import require_loopback

PROBE = r'''<script>
(async () => {
  for (let n=0; n<150 && !document.querySelector('.release-response'); n++) await new Promise(r=>setTimeout(r,100));
  const card=document.querySelector('.release-response');
  const button=card?.querySelector('.view-app');
  const before={text:card?.innerText||'', button:button?.innerText||'', buttons:Array.from(card?.querySelectorAll('button')||[]).map(x=>x.innerText.trim())};
  let opened='';
  window.open=()=>({closed:false,document:{title:'',body:{}},location:{set href(v){opened=v;}}});
  if (button && location.search.includes('click=1')) {
    button.click();
    for(let n=0;n<600 && !opened;n++) await new Promise(r=>setTimeout(r,100));
  }
  await fetch('/__probe__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({before,opened})});
})();
</script>'''

APP = '''import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body=json.dumps({"version":"reviewed-web-app"}).encode()
        self.send_response(200); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): pass
ThreadingHTTPServer(("127.0.0.1",int(os.environ["PORT"])),H).serve_forever()
'''

class ViewAppContract(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        base=Path(self.tmp.name)
        self.root=base/'project'; self.root.mkdir()
        control.initialize(self.root); board.snapshot(self.root)
        fixture=mock.patch.object(board,'RELEASE_FIXTURE_EXEMPT',True)
        fixture.start(); self.addCleanup(fixture.stop)
        self.workspace=base/'delivered'; self.workspace.mkdir()
        self.cto=board.register(self.root,'cto','GLOBAL_MONITOR',vendor='Anthropic')

    def deliver(self):
        checks={key:True for key in board.RELEASE_REQUIRED_CHECKS}
        checks['head_commit']='c'*40
        with board.locked_state(self.root) as state:
            state.setdefault('task_workspaces',{})['TASK']=str(self.workspace)
        board.record_release_ready(self.root,self.cto['id'],'TASK',checks)
        return board.snapshot(self.root)['releases']['TASK']

    def render(self, click=False):
        server=ThreadingHTTPServer(('127.0.0.1',0),board_viewer.make_handler(self.root,project_name='View app contract'))
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        sink={}
        proxy=ThreadingHTTPServer(('127.0.0.1',0),probe_proxy(f'http://127.0.0.1:{server.server_address[1]}',sink,PROBE))
        threading.Thread(target=proxy.serve_forever,daemon=True).start()
        self.addCleanup(proxy.server_close); self.addCleanup(proxy.shutdown)
        profile=tempfile.TemporaryDirectory(); self.addCleanup(profile.cleanup)
        url=f'http://127.0.0.1:{proxy.server_address[1]}/'+('?click=1' if click else '')
        process=browser_acceptance.launch(url,Path(profile.name),width=1280,height=900)
        try:
            end=time.monotonic()+90
            while 'value' not in sink and time.monotonic()<end: time.sleep(.1)
        finally: process.close()
        self.assertIn('value',sink,'The owner card did not render')
        return sink['value']

    def test_ordinary_web_app_keeps_view_app_and_opens(self):
        (self.workspace/'app.py').write_text(APP)
        self.deliver()
        def isolated_copy(_supervisor,preview,workspace):
            preview.directory.mkdir(parents=True,exist_ok=True)
            preview.state_dir.mkdir(parents=True,exist_ok=True)
            shutil.copytree(workspace,preview.source,dirs_exist_ok=True)
        with mock.patch.object(release_preview.ReleasePreviewSupervisor,'_materialize',isolated_copy):
            supervisor=release_preview.ReleasePreviewSupervisor(self.root,tick_seconds=3600)
            supervisor.start(); self.addCleanup(supervisor.shutdown)
            seen=self.render(click=True)
            self.assertEqual(seen['before']['button'],'View app')
            self.assertTrue(seen['opened'].startswith('http://127.0.0.1:'),seen)
            with urlopen(seen['opened'],timeout=5) as answer:
                self.assertEqual(json.load(answer)['version'],'reviewed-web-app')
        (self.workspace/'start.sh').write_text('#!/bin/sh\nexec python3 app.py\n')
        self.assertEqual(release_preview.detect_start(self.workspace)['command'],'sh start.sh')
        rules=Path('directives/AGENT.md').read_text()
        self.assertIn('start.sh',rules)
        self.assertIn('PORT',rules)

    def test_delivered_non_app_has_plain_note_without_button_or_download_advice(self):
        (self.workspace/'README.md').write_text('A delivered code change with no app entry point.\n')
        self.deliver()
        release_preview.ReleasePreviewSupervisor(self.root).tick()
        seen=self.render()
        text=seen['before']['text']
        self.assertIn('This work is not an app to open.',text)
        self.assertIn('Where the delivered work is',text)
        self.assertNotIn('View app',seen['before']['buttons'])
        self.assertNotIn('No download is available',text)
        self.assertNotIn('publish it for download',text)
        (self.workspace/'app.py').write_text(APP)
        again=self.render()
        self.assertNotIn('View app',again['before']['buttons'],'The delivery decision must not be recomputed after release')

    def test_a_saved_preview_command_keeps_the_release_openable(self):
        # Review r1 B1: a saved start command does not live in the workspace's own files, so a
        # file-only look (detect_start) alone would wrongly call this release not openable.
        (self.workspace/'README.md').write_text('No ordinary entry file, but the owner saved a start command.\n')
        workspace_settings.update_preview(self.root,{'command':'./run.sh'})
        release=self.deliver()
        self.assertTrue(release['app_openable'])

    def test_a_built_app_bundle_keeps_the_release_openable(self):
        # Review r1 B1: a packaged .app bundle is found by find_app_bundle, not detect_start.
        bundle=self.workspace/'Example.app'/'Contents'/'MacOS'
        bundle.mkdir(parents=True)
        (bundle/'Example').write_text('#!/bin/sh\necho hi\n')
        release=self.deliver()
        self.assertTrue(release['app_openable'])

if __name__=='__main__': unittest.main()

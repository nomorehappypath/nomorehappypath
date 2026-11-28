"""Owner 2026-11-28 ("Open on iPhone"): a delivered Flutter phone app opens on the iPhone simulator from its test card."""
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import board, board_viewer, browser_acceptance, control, release_preview
from tests.test_branding_rendered import probe_proxy
from tests.environment_support import require_loopback

PROBE = r'''<script>
(async () => {
  for (let n=0; n<150 && !document.querySelector('.release-response'); n++) await new Promise(r=>setTimeout(r,100));
  const card=document.querySelector('.release-response');
  const button=card?.querySelector('.view-app');
  const before={text:card?.innerText||'', button:button?.innerText||''};
  let tabs=0;
  window.open=()=>{tabs++;return {closed:false,document:{title:'',body:{}},location:{}};};
  let after='';
  if (button && location.search.includes('click=1')) {
    button.click();
    for(let n=0;n<300;n++){
      await new Promise(r=>setTimeout(r,100));
      after=document.querySelector('.release-response')?.innerText||'';
      if(/Simulator window|could not start/.test(after))break;
    }
  }
  await fetch('/__probe__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({before,after,tabs})});
})();
</script>'''


@unittest.skipUnless(sys.platform == "darwin", "the iPhone simulator exists only on a Mac")
class OpenOnIphone(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / 'project'; self.root.mkdir()
        control.initialize(self.root); board.snapshot(self.root)
        fixture = mock.patch.object(board, 'RELEASE_FIXTURE_EXEMPT', True)
        fixture.start(); self.addCleanup(fixture.stop)
        self.workspace = base / 'delivered'; self.workspace.mkdir()
        (self.workspace / 'pubspec.yaml').write_text('name: hello\ndependencies:\n  flutter:\n    sdk: flutter\n')
        (self.workspace / 'ios').mkdir()
        self.cto = board.register(self.root, 'cto', 'GLOBAL_MONITOR', vendor='Anthropic')

    def deliver(self):
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        checks['head_commit'] = 'c' * 40
        with board.locked_state(self.root) as state:
            state.setdefault('task_workspaces', {})['TASK'] = str(self.workspace)
        board.record_release_ready(self.root, self.cto['id'], 'TASK', checks)
        return board.snapshot(self.root)['releases']['TASK']

    def render(self, click=False):
        server = ThreadingHTTPServer(('127.0.0.1', 0), board_viewer.make_handler(self.root, project_name='Open on iPhone'))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        sink = {}
        proxy = ThreadingHTTPServer(('127.0.0.1', 0), probe_proxy(f'http://127.0.0.1:{server.server_address[1]}', sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close); self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory(); self.addCleanup(profile.cleanup)
        url = f'http://127.0.0.1:{proxy.server_address[1]}/' + ('?click=1' if click else '')
        process = browser_acceptance.launch(url, Path(profile.name), width=1280, height=900)
        try:
            end = time.monotonic() + 90
            while 'value' not in sink and time.monotonic() < end:
                time.sleep(.1)
        finally:
            process.close()
        self.assertIn('value', sink, 'The owner card did not render')
        return sink['value']

    def supervisor_copying_the_workspace(self):
        import shutil

        def isolated_copy(_supervisor, preview, workspace):
            preview.directory.mkdir(parents=True, exist_ok=True)
            preview.state_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(workspace, preview.source, dirs_exist_ok=True)
        patch = mock.patch.object(release_preview.ReleasePreviewSupervisor, '_materialize', isolated_copy)
        patch.start(); self.addCleanup(patch.stop)
        supervisor = release_preview.ReleasePreviewSupervisor(self.root, tick_seconds=3600)
        supervisor.start(); self.addCleanup(supervisor.shutdown)
        return supervisor

    def test_a_phone_app_is_openable_and_its_card_says_open_on_iphone(self):
        self.assertTrue(self.deliver()['app_openable'])
        release_preview.ReleasePreviewSupervisor(self.root).tick()
        preview = board.snapshot(self.root)['releases']['TASK']['preview']
        self.assertEqual((preview['status'], preview['kind']), ('phone', 'phone'))
        seen = self.render()
        self.assertEqual(seen['before']['button'], 'Open on iPhone')
        self.assertIn('iPhone simulator', seen['before']['text'])

    def test_a_release_recorded_before_phone_apps_were_known_gets_the_button_too(self):
        # test567's release was recorded "not an app to open" by the older version.
        self.deliver()
        with board.locked_state(self.root) as state:
            state['releases']['TASK']['app_openable'] = False
        release_preview.ReleasePreviewSupervisor(self.root).tick()
        seen = self.render()
        self.assertEqual(seen['before']['button'], 'Open on iPhone')
        self.assertNotIn('not an app to open', seen['before']['text'])

    def test_the_click_opens_it_on_the_simulator_without_a_browser_tab(self):
        self.deliver()
        opened, homes = [], []
        release_preview.ReleasePreviewSupervisor(self.root).tick()
        with mock.patch.object(release_preview, 'open_on_iphone', lambda source, env, log: homes.append(env.get('HOME')) or opened.append(source) or 'iPhone 17 Pro'):
            self.supervisor_copying_the_workspace()
            seen = self.render(click=True)
        self.assertEqual(len(opened), 1)
        self.assertTrue(homes[0], 'Flutter needs the owner home folder to build')
        self.assertTrue((opened[0] / 'pubspec.yaml').is_file(), 'the reviewed copy, not the workspace, is opened')
        self.assertNotEqual(opened[0], self.workspace)
        self.assertIn('open on the iPhone 17 Pro simulator', seen['after'])
        self.assertEqual(seen['tabs'], 0)
        preview = board.snapshot(self.root)['releases']['TASK']['preview']
        self.assertEqual((preview['status'], preview['device']), ('phone_open', 'iPhone 17 Pro'))
        release_preview.ReleasePreviewSupervisor(self.root).tick()
        self.assertEqual(board.snapshot(self.root)['releases']['TASK']['preview']['status'], 'phone_open',
                         'a later pass keeps the opened state')

    def test_without_flutter_the_card_says_why_and_how_to_run_it_yourself(self):
        self.deliver()
        empty = Path(self.tmp.name) / 'empty-bin'; empty.mkdir()
        real = release_preview.preview_environment
        with mock.patch.object(release_preview, 'preview_environment',
                               lambda *a, **k: {**real(*a, **k), 'PATH': str(empty)}):
            self.supervisor_copying_the_workspace()
            seen = self.render(click=True)
        preview = board.snapshot(self.root)['releases']['TASK']['preview']
        self.assertEqual(preview['status'], 'failed')
        self.assertIn('Flutter is not installed', preview['error'])
        self.assertIn('open -a Simulator', preview['run_yourself'])
        self.assertIn('Flutter is not installed', seen['after'])
        self.assertEqual(preview['kind'], 'phone')

    def test_a_web_app_is_not_a_phone_app(self):
        (self.workspace / 'pubspec.yaml').unlink()
        (self.workspace / 'app.py').write_text('print(1)\n')
        self.assertFalse(release_preview.phone_project(self.workspace))
        self.deliver()
        release_preview.ReleasePreviewSupervisor(self.root).tick()
        self.assertNotEqual(board.snapshot(self.root)['releases']['TASK']['preview'].get('kind'), 'phone')


if __name__ == '__main__':
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations
import subprocess
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock
from harness import agent_view, control
from harness.platform_support import linux, defaults

class SandboxPreflightTests(unittest.TestCase):
    def test_all_roles_keep_complete_setup_guidance_after_failed_launch(self):
        guidance = 'Linux refused the required sandbox. ' + ('Details. ' * 30) + 'sudo apt install apparmor-profiles; sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for kind in ('codex_delivery', 'claude_reviewer', 'claude_cto'):
                session = control.create(root, kind)
                failed = control.fail_launch(root, session['id'], guidance)
                self.assertEqual(failed['reason'], guidance)
                saved = next(s for s in control.snapshot(root)['sessions'] if s['id'] == session['id'])
                self.assertEqual(saved['reason'], guidance)
    def test_installed_but_denied_probe_has_distro_guidance_and_refuses_launch(self):
        result=subprocess.CompletedProcess([],1,'','bwrap: setting up uid map: Permission denied')
        with patch.object(linux.shutil,'which',return_value='/usr/bin/tool'), patch.object(linux.AGENT_CONFINEMENT,'binary',return_value='/usr/bin/bwrap'), patch.object(linux.subprocess,'run',return_value=result) as run:
            with self.assertRaisesRegex(linux.UnsupportedPlatformOperation,'apparmor-profiles'):
                linux.TERMINAL_HOST.open_session('owned',['/bin/true'],color_rgb=(0,0,0))
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.args[0][-2:],['--','/bin/true'])
            problem=linux.launch_problem()
            self.assertIn('bwrap-userns-restrict',problem)
            self.assertIn('apparmor_parser -r',problem)
            self.assertNotIn('sysctl -w',problem)
    def test_working_sandbox_probe_is_required_before_launch(self):
        with patch.object(linux.shutil,'which',return_value='/usr/bin/tool'), patch.object(linux.AGENT_CONFINEMENT,'binary',return_value='/usr/bin/bwrap'), patch.object(linux.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            linux.TERMINAL_HOST.open_session('owned',['/bin/true'],color_rgb=(0,0,0))
            self.assertEqual(run.call_args_list[0].args[0][0],'/usr/bin/bwrap')
            self.assertEqual(run.call_args_list[1].args[0][:2],['tmux','new-session'])
            self.assertIn('HARNESS_BWRAP_BIN=/usr/bin/bwrap', run.call_args_list[1].args[0])

class AgentViewTests(unittest.TestCase):
    def test_native_managed_launch_is_detached_and_reopening_only_attaches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / '.harness'
            session = {'id': 'codex_delivery-owned', 'status': 'running', 'color': 'black'}
            with patch.object(defaults.sys, 'platform', 'darwin'), patch.object(defaults.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'not found\n', '')) as run:
                defaults.TERMINAL_HOST.open_session(session['id'], ['/bin/true', '--data-root', str(data)], color_rgb=(0, 0, 0))
                self.assertIn('-dmS', run.call_args_list[0].args[0])
                directory = data / 'control' / 'terminal-sockets'
                (directory / ('123.nmhp-' + session['id'])).touch()
                run.reset_mock()
                defaults.TERMINAL_HOST.view_session(session, root=root)
                self.assertEqual(run.call_count, 2)
                self.assertIn('screen', run.call_args.args[0][-1])
                self.assertIn(' -r ', run.call_args.args[0][-1])
                self.assertNotIn('-dmS', run.call_args.args[0][-1])

    def test_view_and_input_only_address_existing_registered_session(self):
        host=Mock();host.capture_session.return_value={'screen':'live'}
        with patch.object(agent_view.control,'snapshot',return_value={'sessions':[{'id':'owned','status':'running'}]}), patch.object(agent_view.platform_support,'terminal_host',return_value=host):
            self.assertEqual(agent_view.open_view(None,'owned')['mode'],'browser')
            agent_view.send_input(None,'owned','hello\r')
            host.input_session.assert_called_once_with('owned',b'hello\r')
            host.open_session.assert_not_called()
            with self.assertRaisesRegex(ValueError,'not running'):agent_view.open_view(None,'other-project')
    def test_tmux_input_is_literal_hex_including_controls(self):
        with patch.object(linux.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            linux.TERMINAL_HOST.input_session('owned',b'\x03\x00\x1b[A')
            argv=run.call_args.args[0]
            self.assertIn('-H',argv)
            self.assertEqual(argv[-5:],['03','00','1b','5b','41'])
    def test_native_view_focuses_matching_tty_without_do_script(self):
        with patch.object(defaults.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'focused\n','')) as run:
            self.assertEqual(defaults.TERMINAL_HOST.view_session({'id':'owned','terminal_tty':'/dev/ttys999'})['mode'],'native')
            script=run.call_args.args[0][2]
            self.assertIn('selected tab',script)
            self.assertNotIn('do script',script)

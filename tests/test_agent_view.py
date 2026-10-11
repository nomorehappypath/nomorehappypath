# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations
import os
import subprocess
import time
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

    def test_resume_replaces_the_held_window_a_paused_agent_left_behind(self):
        duplicate = subprocess.CalledProcessError(1, 'tmux', '', 'duplicate session: nmhp-owned')
        seen = []
        def fake(command, **_):
            seen.append(command)
            if command[:2] == ['tmux', 'new-session'] and sum(1 for c in seen if c[:2] == ['tmux', 'new-session']) == 1:
                raise duplicate
            return subprocess.CompletedProcess([], 0, '', '')
        with patch.object(linux.shutil, 'which', return_value='/usr/bin/tool'), patch.object(linux.AGENT_CONFINEMENT, 'binary', return_value='/usr/bin/bwrap'), patch.object(linux.subprocess, 'run', side_effect=fake):
            linux.TERMINAL_HOST.open_session('owned', ['/bin/true'], color_rgb=(0, 0, 0))
        verbs = [c[1] for c in seen if c[0] == 'tmux']
        self.assertEqual(verbs[:3], ['new-session', 'kill-session', 'new-session'])
    def test_other_tmux_failures_are_still_reported(self):
        failure = subprocess.CalledProcessError(1, 'tmux', '', 'no server running')
        def fake(command, **_):
            if command[0] == 'tmux':
                raise failure
            return subprocess.CompletedProcess([], 0, '', '')
        with patch.object(linux.shutil, 'which', return_value='/usr/bin/tool'), patch.object(linux.AGENT_CONFINEMENT, 'binary', return_value='/usr/bin/bwrap'), patch.object(linux.subprocess, 'run', side_effect=fake):
            with self.assertRaises(subprocess.CalledProcessError):
                linux.TERMINAL_HOST.open_session('owned', ['/bin/true'], color_rgb=(0, 0, 0))

class MacosHomeSurvivesScreenTests(unittest.TestCase):
    """GNU screen resets HOME; an app started with another HOME must still launch its agents there."""

    def launch(self, home):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / '.harness'
            env = {'HOME': home} if home is not None else {}
            with patch.dict(defaults.os.environ, env, clear=home is None), patch.object(defaults.sys, 'platform', 'darwin'), \
                    patch.object(defaults.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
                defaults.TERMINAL_HOST.open_session('codex_delivery-owned', ['/bin/true', '--data-root', str(data)], color_rgb=(0, 0, 0))
            return run.call_args_list[0]

    def test_the_supplied_home_travels_to_the_runner_and_the_command_is_unchanged(self):
        call = self.launch('/private/tmp/some-private-home')
        command = call.args[0]
        self.assertEqual(command[command.index('-dmS') + 2], '/bin/true')
        self.assertEqual(call.kwargs['env']['HARNESS_AGENT_HOME'], '/private/tmp/some-private-home')

    def test_without_a_home_nothing_is_carried(self):
        self.assertNotIn('HARNESS_AGENT_HOME', self.launch(None).kwargs['env'])

    def test_the_runner_puts_the_carried_home_back_before_anything_reads_it(self):
        import re
        text = (Path(__file__).resolve().parents[1] / 'scripts' / 'run_managed_agent.sh').read_text(encoding='utf-8')
        restore = text.index('export HOME="$HARNESS_AGENT_HOME"')
        self.assertLess(restore, text.index('target_root=""'))
        later_uses = [m.start() for m in re.finditer(r'\$\{?HOME\b', text)]
        self.assertTrue(not later_uses or min(later_uses) > restore, 'HOME is read before it is restored')

    def test_a_real_screen_and_the_runner_snippet_keep_the_supplied_home(self):
        if sys_platform() != 'darwin' or not Path('/usr/bin/screen').exists():
            raise unittest.SkipTest('GNU screen of macOS only')
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
            temporary = Path(temporary)
            sockets = temporary / 's'
            sockets.mkdir(mode=0o700)
            out = temporary / 'home.txt'
            home = temporary / 'private-home'
            env = {**os.environ, 'HOME': str(home), 'HARNESS_AGENT_HOME': str(home), 'SCREENDIR': str(sockets)}
            snippet = 'if [[ -n "${HARNESS_AGENT_HOME:-}" ]]; then export HOME="$HARNESS_AGENT_HOME"; fi; echo "$HOME" > ' + str(out)
            subprocess.run(['/usr/bin/screen', '-c', '/dev/null', '-dmS', 'homecheck', '/bin/bash', '--noprofile', '--norc', '-c', snippet],
                           env=env, check=True, capture_output=True, timeout=20)
            for _ in range(40):
                if out.exists() and out.read_text().strip():
                    break
                time.sleep(0.1)
            subprocess.run(['/usr/bin/screen', '-wipe'], env=env, capture_output=True, timeout=20)
            self.assertEqual(out.read_text().strip(), str(home))


def sys_platform():
    import sys
    return sys.platform


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

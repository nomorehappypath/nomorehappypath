# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Service launches must use setup-tokens, never a shared refresh credential."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from harness import claude_auth, control, project_worker, board_viewer, agent_confinement, platform_support


class ClaudeAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        claude_auth._cache.clear()

    def test_service_reads_named_keychain_item_without_secret_in_argv(self):
        result = subprocess.CompletedProcess([], 0, 'synthetic-setup-token\n', 'private diagnostic')
        with patch.object(claude_auth.sys, 'platform', 'darwin'), patch.object(claude_auth.subprocess, 'run', return_value=result) as run:
            env = claude_auth.manager_environment({'PATH': '/bin'})
        self.assertEqual(env[claude_auth.TOKEN_ENV], 'synthetic-setup-token')
        self.assertEqual(env['HARNESS_CLAUDE_TOKEN_SOURCE'], 'keychain')
        self.assertIn('claude-cli-oauth', run.call_args.args[0])
        self.assertNotIn('synthetic-setup-token', repr(run.call_args))

    def test_disabled_keychain_source_does_not_spawn_a_process(self):
        with patch.object(claude_auth.sys, 'platform', 'darwin'), patch.object(claude_auth.subprocess, 'Popen') as process:
            environment = claude_auth.manager_environment({'HARNESS_CLAUDE_KEYCHAIN_SERVICE': ''})
            self.assertNotIn(claude_auth.TOKEN_ENV, environment)
            process.assert_not_called()

    def test_missing_linux_token_never_tries_keychain(self):
        with patch.object(claude_auth.sys, 'platform', 'linux'), patch.object(claude_auth.subprocess, 'run') as run:
            with self.assertRaises(claude_auth.ClaudeAuthRequired):
                claude_auth.resolve_token({})
            run.assert_not_called()

    def test_each_claude_role_and_delivery_override_refuses_before_terminal(self):
        for kind in ('claude_cto', 'claude_reviewer', 'codex_delivery'):
            settings = control.default_agent_settings()
            if kind == 'codex_delivery':
                settings['delivery'] = {'provider': 'claude', 'model': 'opus', 'effort': 'high'}
                settings['reviewer'] = {'provider': 'codex', 'model': 'gpt-5.6-sol', 'effort': 'high'}
            session = control.create(self.root, kind, settings_override=settings)
            with patch.object(claude_auth, 'resolve_token', side_effect=claude_auth.ClaudeAuthRequired()), patch.object(platform_support, 'terminal_host') as host:
                with self.assertRaises(claude_auth.ClaudeAuthRequired):
                    project_worker.launch_terminal(self.root, session, '/tmp/board.sock')
                host.assert_not_called()
            stored = control._read_state(self.root)['sessions'][session['id']]
            self.assertIn('setup-token', stored['claude_auth_action']['why'])
            control.fail_launch(self.root, session['id'], 'missing dedicated token')

    def test_standalone_launch_also_refuses_missing_token(self):
        session = control.create(self.root, 'claude_cto')
        with patch.object(claude_auth, 'resolve_token', side_effect=claude_auth.ClaudeAuthRequired()), patch.object(platform_support, 'terminal_host') as host:
            with self.assertRaises(claude_auth.ClaudeAuthRequired):
                board_viewer.launch_terminal(self.root, session)
            host.assert_not_called()

    def test_codex_does_not_resolve_a_claude_token(self):
        session = control.create(self.root, 'codex_delivery')
        with patch.object(claude_auth, 'resolve_token') as resolve:
            self.assertEqual(claude_auth.prepare_launch(self.root, session), '')
            resolve.assert_not_called()

    def test_rejected_real_turn_is_not_mistaken_for_auth_status_success(self):
        statuses = [subprocess.CompletedProcess([], 0, '{"authMethod":"oauth_token"}', ''),
                    subprocess.CompletedProcess([], 1, '{"is_error":true,"result":"401 invalid token synthetic-secret"}', 'synthetic-secret')]
        with patch.object(claude_auth.global_settings, 'resolved_cli', return_value={'path': '/fake/claude'}), patch.object(claude_auth, '_probe_prefix', return_value=[]), patch.object(claude_auth.subprocess, 'run', side_effect=statuses) as run:
            with self.assertRaises(claude_auth.ClaudeAuthRequired) as caught:
                claude_auth.validate_token('synthetic-secret')
        self.assertEqual(caught.exception.reason, 'rejected')
        self.assertNotIn('synthetic-secret', str(caught.exception))
        for call in run.call_args_list:
            self.assertNotIn('synthetic-secret', repr(call.args[0]))
            self.assertEqual(call.kwargs['env'][claude_auth.TOKEN_ENV], 'synthetic-secret')

    def test_handoff_only_serves_registered_pid_once_without_persistence(self):
        session = control.create(self.root, 'claude_cto')
        path = claude_auth._handoff(self.root, session['id'], 'synthetic-secret')
        def claim(identifier):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(path)
                connection.sendall((identifier + '\n').encode())
                return json.loads(connection.makefile('rb').readline())
        self.assertIn('error', claim(session['id']))  # Not attached yet.
        control.attach(self.root, session['id'], os.getpid())
        self.assertIn('error', claim('wrong-session'))
        self.assertEqual(claim(session['id'])[claude_auth.TOKEN_ENV], 'synthetic-secret')
        deadline = time.monotonic() + 2
        while Path(path).exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(Path(path).exists())
        self.assertNotIn('synthetic-secret', json.dumps(control._read_state(self.root)))
        for item in self.root.rglob('*'):
            if item.is_file():
                self.assertNotIn(b'synthetic-secret', item.read_bytes())

    def test_confined_process_cannot_read_or_write_shared_login(self):
        home = Path(self.tmp.name) / 'home'; home.mkdir()
        credentials = home / '.claude' / '.credentials.json'
        credentials.parent.mkdir(); credentials.write_text('shared-login')
        command = [sys.executable, '-c', 'from pathlib import Path; p=Path(__import__("sys").argv[1]);\ntry: print("LEAK" if "shared-login" in p.read_text() else "MASKED")\nexcept OSError: print("DENIED_READ")\ntry: p.write_text("replaced"); print("WROTE")\nexcept OSError: print("DENIED_WRITE")', str(credentials)]
        wrapped = agent_confinement.wrap(command, [str(home)], store=home / 'guard', home=home)
        result = subprocess.run(wrapped, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('LEAK', result.stdout)
        self.assertNotIn('WROTE', result.stdout)
        self.assertEqual(credentials.read_text(), 'shared-login')

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS Keychain sandbox')
    def test_confined_security_tool_cannot_access_keychain_service(self):
        home = Path(self.tmp.name) / 'home'; home.mkdir()
        wrapped = agent_confinement.wrap(['/usr/bin/security', 'find-generic-password', '-s', 'nmhp-nonexistent-probe', '-w'], [str(home)], store=home / 'guard', home=home)
        result = subprocess.run(wrapped, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        profile = Path(wrapped[2]).read_text()
        self.assertIn('(deny mach-lookup', profile)
        self.assertIn('/Library/Keychains', profile)

    def test_transcripts_redact_a_token_split_across_output_chunks(self):
        from harness import conversation
        path = self.root / 'transcript.log'
        with patch.dict(os.environ, {claude_auth.TOKEN_ENV: 'synthetic-secret'}):
            transcript = conversation.Transcript(path)
            transcript.agent_bytes(b'synthetic-')
            transcript.agent_bytes(b'secret\n')
            transcript.owner('synthetic-secret')
            transcript.close()
        text = path.read_text()
        self.assertNotIn('synthetic-secret', text)
        self.assertIn('[REDACTED CLAUDE TOKEN]', text)

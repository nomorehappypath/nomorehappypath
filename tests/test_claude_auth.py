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

    def _validate(self, runs=None, **patches):
        ok = subprocess.CompletedProcess([], 0, '{"authMethod":"oauth_token"}', '')
        with patch.object(claude_auth.global_settings, 'resolved_cli', return_value={'path': '/fake/claude'}), \
                patch.object(claude_auth, '_probe_prefix', **(patches or {'return_value': []})), \
                patch.object(claude_auth.subprocess, 'run', side_effect=runs or [ok]):
            with self.assertRaises(claude_auth.ClaudeAuthRequired) as caught:
                claude_auth.validate_token('synthetic-secret')
        return caught.exception

    def test_attach_deadline_covers_the_slow_sign_in_check_then_restarts_at_thirty_seconds(self):
        from datetime import datetime, timezone
        session = control.create(self.root, 'claude_cto')
        seen = {}
        def slow_check(token):
            stored = control._read_state(self.root)['sessions'][session['id']]
            seen['during'] = (datetime.fromisoformat(stored['launch_deadline']) - datetime.now(timezone.utc)).total_seconds()
        with patch.object(claude_auth, 'resolve_token', return_value='synthetic-secret'), patch.object(claude_auth, 'validate_token', side_effect=slow_check), \
                patch.dict(os.environ, {'HARNESS_CLAUDE_AUTH_TIMEOUT_SECONDS': '45'}):
            claude_auth.prepare_launch(self.root, session)
        self.assertGreater(seen['during'], 2 * 45, 'a 45-second probe must not be cut off by the 30-second attach deadline')
        after = control._read_state(self.root)['sessions'][session['id']]
        self.assertLessEqual((datetime.fromisoformat(after['launch_deadline']) - datetime.now(timezone.utc)).total_seconds(), 30)
        control.fail_launch(self.root, session['id'], 'test over')

    def test_a_failed_sign_in_check_does_not_leave_a_long_deadline_and_never_revives_a_failed_session(self):
        from datetime import datetime, timezone
        session = control.create(self.root, 'claude_cto')
        with patch.object(claude_auth, 'resolve_token', side_effect=claude_auth.ClaudeAuthRequired()):
            with self.assertRaises(claude_auth.ClaudeAuthRequired):
                claude_auth.prepare_launch(self.root, session)
        stored = control._read_state(self.root)['sessions'][session['id']]
        self.assertLessEqual((datetime.fromisoformat(stored['launch_deadline']) - datetime.now(timezone.utc)).total_seconds(), 30)
        control.fail_launch(self.root, session['id'], 'test over')
        claude_auth._set_launch_deadline(self.root, session, 999)
        self.assertEqual(control._read_state(self.root)['sessions'][session['id']]['status'], 'failed')

    def test_every_staged_resume_role_waits_for_its_turn_and_gets_thirty_seconds_when_claimed(self):
        from datetime import datetime, timezone
        claude, codex = control.create(self.root, 'claude_cto'), control.create(self.root, 'codex_delivery')
        for session in (claude, codex):
            control.prepare_resume_sessions(self.root, [session['id']])
        left = lambda item: (datetime.fromisoformat(item['launch_deadline']) - datetime.now(timezone.utc)).total_seconds()
        stored = control._read_state(self.root)['sessions']
        for session in (claude, codex):
            self.assertGreater(left(stored[session['id']]), 2 * 45, 'a role behind a slow Claude check must not expire while it waits')
        control.mark_resume_launch_requested(self.root, codex['id'])
        self.assertLessEqual(left(control._read_state(self.root)['sessions'][codex['id']]), 30)
        for session in (claude, codex):
            control.fail_launch(self.root, session['id'], 'test over')

    def test_missing_claude_program_is_a_cli_problem_not_a_token_problem(self):
        with patch.object(claude_auth.global_settings, 'resolved_cli', return_value={'path': ''}):
            with self.assertRaises(claude_auth.ClaudeAuthRequired) as caught:
                claude_auth.validate_token('synthetic-secret')
        self.assertEqual(caught.exception.reason, 'cli')

    def test_failures_that_are_not_the_token_say_so_and_ask_for_no_new_token(self):
        ok = subprocess.CompletedProcess([], 0, '{"authMethod":"oauth_token"}', '')
        def turn(text):
            return subprocess.CompletedProcess([], 1, json.dumps({'is_error': True, 'result': text}), '')
        cases = [
            ('confinement', self._validate(side_effect=agent_confinement.ConfinementUnavailable('no bubblewrap'))),
            ('cli', self._validate(runs=[subprocess.CompletedProcess([], 2, '', 'error: unknown command auth')], return_value=[])),
            ('quota', self._validate(runs=[ok, turn('Claude usage limit reached')], return_value=[])),
            ('model', self._validate(runs=[ok, turn('model haiku not found for this account')], return_value=[])),
            ('cli', self._validate(runs=[FileNotFoundError('claude')], return_value=[])),
            ('confinement', self._validate(runs=[subprocess.CompletedProcess([], 1, '', 'bwrap: Creating new namespace failed: Operation not permitted')], return_value=[])),
            ('confinement', self._validate(runs=[ok, subprocess.CompletedProcess([], 1, '', 'bwrap: Creating new namespace failed')], return_value=[])),
            ('rejected', self._validate(runs=[ok, turn('401 invalid token')], return_value=[])),
        ]
        for reason, error in cases:
            self.assertEqual(error.reason, reason)
            card = claude_auth.action(reason)
            if reason != 'rejected':
                self.assertEqual(card['title'], 'Claude agents cannot start')
                self.assertIn('not rejected', card['why'])
                self.assertNotIn('setup-token', card['instructions'])
            else:
                self.assertIn('Generate a replacement', card['why'])

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

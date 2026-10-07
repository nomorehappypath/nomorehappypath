# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner access to an existing managed session. Never creates an agent."""
from __future__ import annotations

import os
from harness import control, platform_support

MAX_INPUT_BYTES = 4096


def session_for_view(root, session_id):
    session = next((s for s in control.snapshot(root)['sessions'] if s['id'] == session_id), None)
    if not session or session['status'] not in {'launching', 'running'} or session.get('read_only'):
        raise ValueError('This agent is not running. Its saved work remains on the board.')
    return session


def open_view(root, session_id):
    session = session_for_view(root, session_id)
    host = platform_support.terminal_host()
    if hasattr(host, 'capture_session'):
        host.capture_session(session_id)
        return {'mode': 'browser', 'session_id': session_id, 'poll_ms': max(250, min(5000, int(os.environ.get('HARNESS_AGENT_VIEW_POLL_MS', '500'))))}
    return host.view_session(session, root=root)


def capture(root, session_id):
    session_for_view(root, session_id)
    return platform_support.terminal_host().capture_session(session_id)


def send_input(root, session_id, text):
    session_for_view(root, session_id)
    if not isinstance(text, str) or not text or len(text.encode('utf-8')) > MAX_INPUT_BYTES:
        raise ValueError('Terminal input must contain between 1 and 4096 UTF-8 bytes.')
    platform_support.terminal_host().input_session(session_id, text.encode('utf-8'))
    return {'sent': True}

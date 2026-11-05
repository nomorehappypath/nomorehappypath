# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner access to an existing managed session. Never creates an agent."""
from __future__ import annotations

import os
from harness import control, platform_support

MAX_INPUT_BYTES = 4096


def _call(host, method, root, *arguments):
    """Invoke method on host, passing root when the host is project-scoped.

    Args:
        host: The terminal host object that should expose method.
        method: Name of the host method to call.
        root: Project root, forwarded as a keyword when the host needs it.
        arguments: Positional arguments passed through to the host method.

    Returns:
        The result produced by calling the host method.
    """
    # A host that cannot do this says so in plain words (a 400 the screen shows), never a 500.
    if not hasattr(host, method):
        raise ValueError("This computer cannot send input to the agent from here. Use the agent's own window.")
    extra = {'root': root} if getattr(host, 'session_scoped_to_project', False) is True else {}
    return getattr(host, method)(*arguments, **extra)


def session_for_view(root, session_id):
    """Look up the running session eligible for a live view.

    Args:
        root: Project root whose session snapshot is consulted.
        session_id: Identifier of the session to find.

    Returns:
        The matching session dict, or raises ValueError when none is running.
    """
    session = next((s for s in control.snapshot(root)['sessions'] if s['id'] == session_id), None)
    if not session or session['status'] not in {'launching', 'running'} or session.get('read_only'):
        raise ValueError('This agent is not running. Its saved work remains on the board.')
    return session


def open_view(root, session_id):
    """Open a live view of the session, in the browser or the host's own window.

    Args:
        root: Project root the session belongs to.
        session_id: Identifier of the session to view.

    Returns:
        A dict describing a browser-polling view, or the host's own view result.
    """
    session = session_for_view(root, session_id)
    host = platform_support.terminal_host()
    if hasattr(host, 'capture_session') and getattr(host, 'uses_browser_view', lambda: True)():
        _call(host, 'capture_session', root, session_id)
        return {'mode': 'browser', 'session_id': session_id, 'poll_ms': max(250, min(5000, int(os.environ.get('HARNESS_AGENT_VIEW_POLL_MS', '500'))))}
    return host.view_session(session, root=root)


def capture(root, session_id):
    """Capture the current terminal contents of a running session.

    Args:
        root: Project root the session belongs to.
        session_id: Identifier of the session to capture.

    Returns:
        The captured terminal output from the host.
    """
    session_for_view(root, session_id)
    return _call(platform_support.terminal_host(), 'capture_session', root, session_id)


def send_input(root, session_id, text):
    """Send text input to a running session's terminal.

    Args:
        root: Project root the session belongs to.
        session_id: Identifier of the session to send input to.
        text: UTF-8 text to send; must be 1 to 4096 bytes once encoded.

    Return:
        A dict confirming the input was sent.
    """
    session_for_view(root, session_id)
    if not isinstance(text, str) or not text or len(text.encode('utf-8')) > MAX_INPUT_BYTES:
        raise ValueError('Terminal input must contain between 1 and 4096 UTF-8 bytes.')
    _call(platform_support.terminal_host(), 'input_session', root, session_id, text.encode('utf-8'))
    return {'sent': True}

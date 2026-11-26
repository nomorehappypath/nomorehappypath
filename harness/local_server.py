#!/usr/bin/env python3
"""The HTTP server class every NMHP server uses (Mission Control, the project worker, the board viewer, Support)."""
from __future__ import annotations

import http.server


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """The standard threading server with a longer queue of waiting connections.

    2026-11-26: the standard queue holds 5. On macOS 27.2.1 a sixth connection that arrives at the same moment is reset
    ("Connection reset by peer"), so a page loading several parts at once, or several agents calling the board together,
    could fail. 128 is the macOS default limit (kern.ipc.somaxconn)."""

    request_queue_size = 128

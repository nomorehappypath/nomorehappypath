# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Skip loudly where a sandboxed environment denies test prerequisites.

Review shells can forbid loopback sockets or nested sandbox-exec. A raw
PermissionError there reads as a product failure; an explicit SkipTest with
the reason is honest, visible, and is not a pass. Capable environments are
unaffected - the assertions execute in full.
"""
from __future__ import annotations

import socket
import subprocess
import unittest


def require_loopback() -> None:
    """SkipTest where binding 127.0.0.1 is denied by the environment."""
    try:
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", 0))
        finally:
            probe.close()
    except OSError as error:
        raise unittest.SkipTest(f"environment forbids loopback binding: {error}")


def require_sandbox_exec() -> None:
    """SkipTest where macOS sandbox-exec (used by the git broker) is denied.

    Nested sandboxing is refused inside many review shells; the broker's
    certified execution cannot run there and the test must say so.
    """
    try:
        completed = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
            capture_output=True, timeout=10,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or b"").decode("utf-8", "replace").strip()
            raise unittest.SkipTest(f"environment denies sandbox-exec: {detail or completed.returncode}")
    except (OSError, subprocess.SubprocessError) as error:
        raise unittest.SkipTest(f"environment denies sandbox-exec: {error}")

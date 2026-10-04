# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
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


def require_process_table() -> None:
    """SkipTest where the environment forbids reading the OS process table.

    Certified execution proves which processes it owned by reading ``ps``.
    Review shells that deny process execution make that evidence impossible to
    collect, and a test cannot pass without it - so it says so out loud instead
    of failing as though the product were broken. Capable environments run the
    assertions in full.
    """
    from harness import browser_acceptance

    try:
        browser_acceptance._process_table()
    except browser_acceptance.ProcessTableUnavailable as error:
        raise unittest.SkipTest(str(error)) from error


def home_outside_temp_space(case: unittest.TestCase, prefix: str) -> "Path":
    """A manager home OUTSIDE every agent's temp grant and OUTSIDE this tree.

    Stage 3's runtime directory is refused inside an agent's write grant, and
    temp space is in every agent's grant, so a test home must live elsewhere.
    Not inside the tree either: the release gate assembles `tests/` and
    refuses any personal path it finds there (it did, 2026-10-03). Beside the
    checkout is used; where the checkout itself sits in temp space (the
    release gate's own assembled tree) no such place exists, and the test is
    skipped with that reason rather than weakened.
    """
    import os
    import shutil
    import tempfile
    from pathlib import Path
    from harness import platform_support
    parent = Path(os.path.realpath(Path(__file__).resolve().parents[2]))
    temp = [os.path.realpath(path) for path in platform_support.agent_confinement().temp_paths()]
    if os.environ.get("TMPDIR"):
        temp.append(os.path.realpath(os.environ["TMPDIR"]))
    if any(str(parent) == root or str(parent).startswith(root.rstrip("/") + "/") for root in temp):
        raise unittest.SkipTest("this checkout lives in temp space; a runtime directory there is refused by design")
    home = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
    case.addCleanup(shutil.rmtree, home, True)
    return home

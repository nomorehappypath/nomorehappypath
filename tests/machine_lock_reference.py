# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A reference of the version 2 machine lock rule for macOS and Linux (directives/AGENT.md, windows-walk item 1).

The rule: an exclusive lock file in a `<lock folder>-v2` folder, held non-inheritably for the whole run by the lock script; the
operating system frees it when the script dies; `holder.txt` is published by temp file and rename; only contention is retried, for
a bounded time, then the failure names the holder; any other error fails at once. The tests run this with real processes. The
Windows form (`[System.IO.File]::Open(...,'None')`) is NOT exercised here: neither test machine has PowerShell.
"""
from __future__ import annotations

import errno
import fcntl
import os
import time
from pathlib import Path

HOLDER = "holder.txt"
LOCK_FILE = "lock"


class Held(Exception):
    """Another live run holds the lock; str() names it."""


def _read_holder(folder: Path) -> str:
    try:
        return (folder / HOLDER).read_text(encoding="utf-8").strip()
    except OSError:
        return "(holder note not published yet)"


def acquire(folder: Path, agent: str, role: str, wait: float = 0.5, start: str = "now") -> int:
    """Returns the lock descriptor to keep open until the run ends. Raises Held after `wait` seconds of contention."""
    folder.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(folder / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)   # os.open: not inherited by exec'd children (PEP 446)
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as error:
            if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                os.close(descriptor)
                raise                      # anything but contention fails at once with its real cause
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise Held(_read_holder(folder)) from None
            time.sleep(0.05)
    temporary = folder / f".{HOLDER}.{os.getpid()}"
    temporary.write_text(f"v2 {agent} {role} pid={os.getpid()} start={start} since={time.strftime('%Y-%m-%dT%H:%M:%S')}\n", encoding="utf-8")
    os.replace(temporary, folder / HOLDER)
    return descriptor

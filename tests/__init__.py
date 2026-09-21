# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Test-run isolation: no test may ever touch the owner's real files.

Two incidents shaped this module.

2026-08-21: every manager test that opened a project wrote a codex trust entry
into the owner's real ~/.codex/config.toml - one hundred garbage entries across
three suite runs. CODEX_HOME is pinned to a throwaway directory before any test
code runs.

2026-09-21: the production box serving two live products ran out of inodes.
A compatibility project deliberately keeps its board backups, memory backups
and task workspaces BESIDE the project, so a crash inside the project cannot
take them with it. Tests build their projects directly under the system temp
directory, so those siblings landed in the shared /tmp, outside the directory
each test tears down - 175,089 leftover entries and 24 GB after three days of
review runs, plus the board-client nonce journal, the pinned CODEX_HOME dirs
and every project root of a run that was killed part-way.

The fix is one private root per suite process. TMPDIR, tempfile.tempdir and
CODEX_HOME all point inside it, so every tempfile call, every subprocess (the
child environment forwards TMPDIR) and every sibling directory resolves under
it, and removing that one root at exit removes everything. A run that is
killed leaves exactly one directory, named after its pid, and the next run
sweeps it when that pid is dead.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

RUN_ROOT_PREFIX = "hnt-"
CODEX_HOME_NAME = "harness-tests-codex-home-pinned"


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else: never ours to remove
    return True


def _sweep_dead_run_roots(system_temp: Path) -> list[Path]:
    """Remove the private roots of suite processes that no longer exist."""
    removed: list[Path] = []
    try:
        candidates = list(system_temp.iterdir())
    except OSError:
        return removed
    for candidate in candidates:
        name = candidate.name
        if not name.startswith(RUN_ROOT_PREFIX) or not candidate.is_dir():
            continue
        try:
            pid = int(name[len(RUN_ROOT_PREFIX):])
        except ValueError:
            continue
        if _pid_is_alive(pid):
            continue
        shutil.rmtree(candidate, ignore_errors=True)
        if not candidate.exists():
            removed.append(candidate)
    return removed


def _run_root_base() -> Path:
    """The directory the private run root is created under.

    Unix sockets are created under the temp directory (the project worker's
    bootstrap `claim.sock`), and macOS limits a socket path to 104 bytes. The
    default macOS temp directory is already ~50 bytes deep, so nesting one
    more level under it pushed the worker over the limit ("AF_UNIX path too
    long") and every real-worker test failed. /tmp is 12 bytes resolved and
    exists on both platforms; it is used whenever it is writable.
    """
    short = Path("/tmp")
    if short.is_dir() and os.access(short, os.W_OK):
        return short.resolve()
    return Path(tempfile.gettempdir()).resolve()


def _pin_run_root() -> Path:
    system_temp = _run_root_base()
    _sweep_dead_run_roots(system_temp)
    run_root = system_temp / f"{RUN_ROOT_PREFIX}{os.getpid()}"
    run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(run_root)
    tempfile.tempdir = str(run_root)
    codex_home = run_root / CODEX_HOME_NAME
    codex_home.mkdir(mode=0o700, exist_ok=True)
    os.environ["CODEX_HOME"] = str(codex_home)
    atexit.register(shutil.rmtree, run_root, True)
    return run_root


if not os.environ.get("HARNESS_TESTS_RUN_ROOT"):
    SYSTEM_TEMP = _run_root_base()
    RUN_ROOT = _pin_run_root()
    os.environ["HARNESS_TESTS_RUN_ROOT"] = str(RUN_ROOT)
    os.environ["HARNESS_TESTS_SYSTEM_TEMP"] = str(SYSTEM_TEMP)
    os.environ["HARNESS_TESTS_CODEX_HOME_PINNED"] = "1"
else:
    # A child process of a pinned run (a subprocess that imports the tests
    # package) inherits the parent's root rather than nesting a new one.
    RUN_ROOT = Path(os.environ["HARNESS_TESTS_RUN_ROOT"])
    SYSTEM_TEMP = Path(os.environ["HARNESS_TESTS_SYSTEM_TEMP"])

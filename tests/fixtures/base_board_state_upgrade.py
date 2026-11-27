# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Frozen fixture for tests.test_mobile_direction's saved-data round-trip test (CTO finding, owner
release repair 2026-11-27, round 2 of B3: the prior version of this test read the base commit's own
board.py through `git show`, which skips on a tar-only checkout - the Reviewer's own checkout among
them - so the round trip never ran there).

`board_dir`, `_state_path` and the atomic-write tail of `locked_state` below are copied VERBATIM from
harness/board.py at commit 0003ae6 (the task's base commit, before project_mobile/task_mobile existed).
Never edit these to match current board.py: their only job is to behave exactly as that old version did.
Deliberately NOT copied: 0003ae6's history-migration, task-baseline compaction and project-memory sync
calls inside the real locked_state - they exist to repair or summarize OLDER history, never to drop a
key the loaded JSON already has, so they are orthogonal to the one property this fixture proves: that
an old version's own load-mutate-save cycle (whole dict in, whole dict back out, unknown keys and all)
never drops a key it does not recognize. `project_context` is imported live (not frozen): it is a
generic storage-path resolver every harness module depends on and has not changed what `storage_path`
returns for "board" since before this feature existed.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

from harness.project_context import ProjectRoot, project_context

SOURCE_COMMIT = "0003ae6"


def board_dir(root: ProjectRoot) -> Path:
    """Return the board storage directory path for the given project root. (verbatim, 0003ae6)"""
    return project_context(root).storage_path("board")


def _state_path(root: Path) -> Path:
    """Return the path to the board's state.json file under root. (verbatim, 0003ae6)"""
    return board_dir(root) / "state.json"


def resave_untouched(root: Path) -> None:
    """Load root's state.json and atomically write it straight back, exactly as 0003ae6's own
    `locked_state` did around the caller's mutation (lock, load the whole dict, yield, re-serialize the
    whole dict, atomic temp-file rename) - with no mutation at all, so this is the old version simply
    opening a project and closing it again. Proves unknown keys (project_mobile/task_mobile, which
    0003ae6 never heard of) survive that cycle, because the cycle carries the WHOLE loaded dict, not a
    fixed schema."""
    directory = board_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            path = _state_path(root)
            state: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            # (the caller's mutation would happen here, between load and save, in the real locked_state)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            os.replace(temp, path)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

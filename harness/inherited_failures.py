"""A test that already failed before the task began is not the task's fault (owner, 2026-11-11).

When a unittest run fails, each failing test is re-run alone on the commit the task started from. If every failure also fails
there, none was caused by the task: the run counts as passed, with a plain note naming them. One failure that passes at the start
(or does not exist there) means the task broke it, and the run stays failed. Anything this module cannot read with certainty
counts as "not inherited": it never makes a run pass by guessing."""
from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

_FAILED = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)\s*$", re.M)
_SUMMARY = re.compile(r"FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?(?:, )?(?:skipped=\d+)?(?:, )?(?:expected failures=\d+)?(?:, )?(?:unexpected successes=\d+)?\)")
MAX_LISTED = 40


def failed_ids(output: str) -> list[str] | None:
    """The dotted ids of the failing tests, or None when the output is not one plain unittest failure list (import errors,
    set-up failures and unreadable output are never inherited)."""
    ids = []
    for name, where in _FAILED.findall(output):
        if name.startswith("setUp") or name.startswith("tearDown") or "_FailedTest" in where:
            return None
        ids.append(where if where.endswith("." + name) else f"{where}.{name}")
    summary = _SUMMARY.search(output)
    if not ids or not summary:
        return None
    if sum(int(group or 0) for group in summary.groups()[:2]) != len(ids):
        return None
    return sorted(set(ids))


def _is_unittest(command: str) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return "unittest" in argv and "-m" in argv


def judge(command: str, output: str, execution_root: Path, baseline_commit: str, *, workdir: str = ".",
          environment: Mapping[str, str] | None = None, timeout_seconds: int = 900) -> dict[str, Any] | None:
    """None when the failures are the task's own (or unknown); else {"ids": [...], "commit": ..., "note": ...} when all of them
    also fail on `baseline_commit`."""
    if not baseline_commit or not _is_unittest(command):
        return None
    ids = failed_ids(output)
    if not ids:
        return None
    temp = tempfile.mkdtemp(prefix="nmhp-baseline-")
    folder = Path(temp) / "base"
    added = False
    try:
        made = subprocess.run(["git", "worktree", "add", "--detach", str(folder), baseline_commit], cwd=execution_root,
                              capture_output=True, text=True, timeout=120)
        if made.returncode != 0:
            return None
        added = True
        ran = subprocess.run(["python3", "-m", "unittest", *ids], cwd=folder / (workdir if workdir and workdir != "." else "."),
                             capture_output=True, text=True, timeout=timeout_seconds, env=dict(environment) if environment else None)
        again = failed_ids(ran.stdout + ran.stderr)
        if not again or not set(ids) <= set(again):
            return None         # at least one passes (or is missing) at the start: the task caused it
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if added:
            subprocess.run(["git", "worktree", "remove", "--force", str(folder)], cwd=execution_root, capture_output=True, text=True)
        subprocess.run(["rm", "-rf", temp], capture_output=True)
    listed = "\n".join(f"  - {item}" for item in ids[:MAX_LISTED]) + (f"\n  ... and {len(ids) - MAX_LISTED} more" if len(ids) > MAX_LISTED else "")
    note = (f"INHERITED FAILURES: {len(ids)} test(s) failed in this run and fail the same way at the commit this task started from "
            f"({baseline_commit[:12]}), so this task did not cause them. They do not count against it and are not repaired in it:\n{listed}")
    return {"ids": ids, "commit": baseline_commit, "note": note}

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Review cost controls (task 2026-10-25_review-cost-controls): information only, nothing is ever cut off.

1. A very large change is given to the Reviewer as a git summary plus the largest changes in full, with one plain line saying what was
   reduced and that the Challenge Ledger covers the rest. The size limits are the owner's settings (global_settings, plumbing section).
2. The owner sees one plain line per task: how many agent turns and how many minutes of review it has used so far. Both are counted from
   the board's own records. A "turn" is one recorded Delivery or Reviewer step on the task (the board's progress events), shown to the owner as a step. Nothing here is a
   provider bill and no line claims one.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness import accepted_bytes, git_process

# Defaults and bounds of the two Settings fields (global_settings.PLUMBING_LIMITS routes them; nothing else holds a number).
MAX_FILES_DEFAULT, MAX_FILES_LOW, MAX_FILES_HIGH = 60, 5, 5000
MAX_BYTES_DEFAULT, MAX_BYTES_LOW, MAX_BYTES_HIGH = 400_000, 10_000, 50_000_000
MEASURE_SECONDS_DEFAULT, MEASURE_SECONDS_LOW, MEASURE_SECONDS_HIGH = 20, 1, 300
LISTED_PATHS = 40
NOT_A_TASK_PREFIXES = ("REVIEW_", "AWAITING_")

_configured: dict[str, int] = {}


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def configure(plumbing: Any) -> None:
    """Install the owner's limits (the project worker passes the settings once at start); anything unusable keeps the default."""
    _configured.clear()
    if not isinstance(plumbing, dict):
        return
    for key, name, low, high in (
        ("review_input_max_files", "files", MAX_FILES_LOW, MAX_FILES_HIGH),
        ("review_input_max_bytes", "bytes", MAX_BYTES_LOW, MAX_BYTES_HIGH),
        ("review_input_measure_seconds", "seconds", MEASURE_SECONDS_LOW, MEASURE_SECONDS_HIGH),
    ):
        value = plumbing.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            _configured[name] = value


def limits() -> tuple[int, int]:
    return _configured.get("files", MAX_FILES_DEFAULT), _configured.get("bytes", MAX_BYTES_DEFAULT)


def _size_text(count: int) -> str:
    return f"{count / 1_000_000:.1f} MB" if count >= 1_000_000 else f"{max(1, round(count / 1000))} KB"


def measure_seconds() -> int:
    return _configured.get("seconds", MEASURE_SECONDS_DEFAULT)


def _measure(code_root: Path, base: str, commit: str) -> list[tuple[str, int, int]] | None:
    """[(path, changed lines, size of the file at the candidate in bytes)] from git, or None when git cannot tell.

    Bounded: three small git calls through the sanitized boundary with the owner's time limit, no external diff or text conversion, and
    no patch text is ever produced (sizes come from the object database)."""
    if not base or not commit:
        return None
    wait = measure_seconds()
    try:
        stat = git_process.run(["git", *accepted_bytes.numstat_arguments(base, commit)],
                               cwd=code_root, capture_output=True, text=True, timeout=wait)
        if stat.returncode != 0:
            return None
        changed: list[tuple[str, int]] = []
        for entry in (stat.stdout or "").split("\0"):
            parts = entry.strip("\n").split("\t", 2)
            if len(parts) == 3 and parts[2]:
                added, removed, path = parts
                changed.append((path, int(added) + int(removed) if added.isdigit() and removed.isdigit() else 0))
        sizes: dict[str, int] = {}
        if changed:
            query = git_process.run(["git", "cat-file", "--batch-check"], cwd=code_root, capture_output=True, text=True, timeout=wait,
                                    input="".join(f"{commit}:{path}\n" for path, _lines in changed if "\n" not in path))
            if query.returncode != 0:
                return None
            names = [path for path, _lines in changed if "\n" not in path]
            for path, line in zip(names, (query.stdout or "").splitlines()):
                fields = line.split()
                sizes[path] = int(fields[2]) if len(fields) == 3 and fields[1] == "blob" and fields[2].isdigit() else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return [(path, lines, sizes.get(path, 0)) for path, lines in changed]


def reduce_candidate(code_root: Path, request: dict[str, Any], paths: list[str], list_path: Path | None = None) -> dict[str, Any] | None:
    """None for an ordinary change (the brief is untouched). For one above either limit, the deterministic reading list and the plain line.

    Largest change first (ties by path). A file is read in full only while the file limit AND the byte limit still hold; a file that on its
    own is larger than what is left of the byte limit is never read in full. Every changed file ends up in exactly one of the two lists, so
    nothing is dropped. The brief spells out the first LISTED_PATHS names of each list and the exact counts; the COMPLETE partition and the full
    git summary are written to `list_path` (named in the brief with its sha256) so the brief stays small and no changed file is unfindable.
    If that file cannot be written, the complete lists go into the brief instead."""
    max_files, max_bytes = limits()
    rows = _measure(Path(code_root), str(request.get("reviewed_base_commit") or ""), str(request.get("reviewed_commit") or ""))
    measured = bool(rows)
    if measured:
        total_files, total_bytes = len(rows), sum(size for _p, _l, size in rows)
    else:
        total_files, total_bytes = len(paths), None
    if total_files <= max_files and (total_bytes is None or total_bytes <= max_bytes):
        return None
    ordered = sorted(rows, key=lambda row: (-row[1], row[0])) if measured else [(path, 0, 0) for path in paths]
    full: list[str] = []
    rest: list[str] = []
    budget = max_bytes
    for path, _lines, size in ordered:
        if len(full) < max_files and (not measured or size <= budget):
            full.append(path)
            budget -= size if measured else 0
        else:
            rest.append(path)
    size_words = f" and {_size_text(total_bytes)} of changed files" if total_bytes is not None else " (their size could not be measured)"
    summary_rows = [{"path": path, "changed_lines": lines, "bytes": size} for path, lines, size in ordered] if measured else []
    complete = {"read_in_full": full, "not_read_in_full": rest, "diff_summary": summary_rows}
    stored: dict[str, Any] = {}
    if list_path is not None:
        try:
            payload = json.dumps(complete, sort_keys=True, indent=1).encode("utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            if not (list_path.is_file() and hashlib.sha256(list_path.read_bytes()).hexdigest() == digest):
                list_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = list_path.with_name(list_path.name + f".{os.getpid()}.tmp")
                temporary.write_bytes(payload)
                os.replace(temporary, list_path)
            stored = {"complete_lists_file": {"path": str(list_path), "sha256": digest}}
        except OSError:
            stored = {}
    spelled = stored or {"complete_lists_in_brief": True}
    return {
        **spelled,
        "plain_line": (
            f"This change is large ({total_files} files{size_words}), above the limit set in Settings. You are given the git summary and "
            f"{len(full)} files to read in full; the other {len(rest)} files are not read line by line, and you check them with Challenge Ledger rows."
        ),
        "instruction": (
            "Read in full only the files under read_in_full. For every other changed file use diff_summary and git for that one path; add a "
            "Challenge Ledger row for any of them that touches permissions, stored data, security or something the owner sees, and name in "
            "your ledger the omitted areas you judged immaterial. The complete lists of both groups and the whole git summary are in complete_lists_file "
            "(or in this block when that file is absent). The owner documents and pinned proof files are still all required reading."
        ),
        "size_measured": measured,
        "total_files": total_files, "total_bytes": total_bytes,
        "limit_files": max_files, "limit_bytes": max_bytes,
        "diff_summary": summary_rows[:LISTED_PATHS] if stored else summary_rows,
        "read_in_full": full[:LISTED_PATHS] if stored else full, "read_in_full_count": len(full),
        "not_read_in_full": rest[:LISTED_PATHS] if stored else rest, "not_read_in_full_count": len(rest),
    }


# ---- what a task has used so far -------------------------------------------------------------------------------------------

def count_turn(state: dict[str, Any], agent: dict[str, Any] | None, payload: dict[str, Any]) -> None:
    """One recorded agent step on a task. Kept in the board's own state, so it survives a restart. The CTO and the harness never count."""
    if not agent or agent.get("role") in {"cto", "system"}:
        return
    task = str(payload.get("task") or agent.get("task") or "")
    if not task or task.startswith(NOT_A_TASK_PREFIXES):
        return
    turns = state.setdefault("task_turns", {})
    turns[task] = int(turns.get(task, 0) or 0) + 1


def _moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _running_since(request: dict[str, Any]) -> datetime | None:
    """When the review's current stretch of active work began; None when no stretch is open (not started, paused, or between attempts)."""
    marker = request.get("review_active_since")
    if marker:
        return _moment(marker)
    if request.get("status") in {"reserved", "claimed"} or request.get("completed_at"):
        starts = [moment for moment in (_moment(request.get("reserved_at")), _moment(request.get("claimed_at"))) if moment]
        return min(starts) if starts else None
    return None


def close_stretch(request: dict[str, Any], at: datetime | None = None) -> None:
    """The review stops working for now (paused, or its attempt was abandoned): bank the stretch so far, so nothing is lost or double counted."""
    start = _running_since(request)
    if start is not None:
        request["review_active_seconds"] = float(request.get("review_active_seconds", 0.0) or 0.0) + max(0.0, ((at or _clock()) - start).total_seconds())
    request["review_active_since"] = ""


def open_stretch(request: dict[str, Any], at: datetime | None = None) -> None:
    """The review works again (resumed after a pause)."""
    request["review_active_since"] = (at or _clock()).isoformat()


def _active_seconds(request: dict[str, Any], end: datetime) -> float:
    start = _running_since(request)
    return float(request.get("review_active_seconds", 0.0) or 0.0) + (max(0.0, (end - start).total_seconds()) if start else 0.0)


def settle_review(state: dict[str, Any], request_id: Any) -> None:
    """A review reached its verdict: add its active time to the task's running total, kept in the board state.

    The total therefore survives a restart AND the archiving of the finished request, and an earlier attempt that was abandoned or paused
    keeps the time it had banked. Reviews that overlap are each counted (it is a total of review work, not the task's wall clock)."""
    request = (state.get("qa_requests") or {}).get(str(request_id or ""))
    if not request or not request.get("task") or request.get("review_time_settled"):
        return
    end = _moment(request.get("completed_at"))
    if end is None:
        return
    request["review_time_settled"] = True
    seconds = state.setdefault("task_review_seconds", {})
    seconds[request["task"]] = float(seconds.get(request["task"], 0.0) or 0.0) + _active_seconds(request, end)


def review_minutes(state: dict[str, Any], task: str, at: datetime | None = None) -> int:
    """Whole minutes of review: the settled total plus every unfinished review's active time (a paused one keeps what it banked)."""
    current = at or _clock()
    seconds = float((state.get("task_review_seconds") or {}).get(task, 0.0) or 0.0)
    for request in (state.get("qa_requests") or {}).values():
        if request.get("task") == task and not request.get("review_time_settled") and not request.get("completed_at"):
            seconds += _active_seconds(request, current)
    return int(seconds // 60)


def usage(state: dict[str, Any], task: str, at: datetime | None = None) -> dict[str, Any]:
    turns = int((state.get("task_turns") or {}).get(task, 0) or 0)
    minutes = review_minutes(state, task, at)
    return {
        "agent_turns": turns, "review_minutes": minutes,
        "line": (
            f"Counted so far: {turns} recorded Delivery and Reviewer {'step' if turns == 1 else 'steps'} and {minutes} "
            f"{'minute' if minutes == 1 else 'minutes'} of review. This is the board's own count, not a provider bill; it leaves out the "
            "CTO and any work from before counting began."
        ),
    }

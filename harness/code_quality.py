# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Code-quality notes (task 2026-10-26_code-quality-and-nfr): information only, never a blocker.

The Reviewer may add short observations on how the code is built (duplication, oversized files or functions, tangled structure,
hard-coded values, missing seams for extension), one plain line each with a file reference, on its round-1 `qa-result`
(`--code-quality`, repeatable). The board keeps them with the task. They are not findings: the frozen-scope judgment never reads them,
so they cannot block, fail, re-run or reclassify any review (DEV_DIRECTIVE 2026-10-26 (ab)). A missing or empty note stores nothing and
refuses nothing; an overlong line is trimmed, never refused. The owner's history shows a plain "Code quality" summary.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

MAX_NOTES = 10
MAX_CHARS = 240


def clean(raw: Any) -> list[str]:
    """One trimmed line per non-empty note, duplicates dropped, at most MAX_NOTES; accepts a list, a single string or nothing."""
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[str] = []
    for item in items:
        line = " ".join(str(item or "").split())
        if not line:
            continue
        line = line if len(line) <= MAX_CHARS else line[: MAX_CHARS - 1].rstrip() + "…"
        if line not in out:
            out.append(line)
    return out[:MAX_NOTES]


def record(state: dict[str, Any], task: str, step: str, raw: Any) -> bool:
    """Keep the notes on the task (newest review's notes replace older ones, so a re-review does not pile up). False when there is nothing to keep."""
    lines = clean(raw)
    if not lines or not task:
        return False
    state.setdefault("code_quality_notes", {})[task] = {"step": step, "at": datetime.now(timezone.utc).isoformat(), "notes": lines}
    return True


def notes(state: dict[str, Any], task: str) -> list[str]:
    entry = (state.get("code_quality_notes") or {}).get(task)
    return clean(entry.get("notes")) if isinstance(entry, dict) else []

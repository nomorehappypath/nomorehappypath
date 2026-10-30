# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Synthetic cost ledgers for the Costs page tests: a ledger written directly, priced from the shipped price card (claude-sonnet-5-5: $2 in, $10 out per million tokens)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import cost_center

MODEL = "claude-sonnet-5-5"
NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)


def write_ledger(board: Path, *, buckets: dict[tuple[str, str], list[int]], active: dict[str, float] | None = None, gemini: dict[str, float] | None = None,
                 limits: dict[str, tuple[float, float]] | None = None, partial: dict[str, str] | None = None) -> None:
    """buckets: (role, task) -> [input, output, cache read, cache write]; active: task -> days ago it last ran; gemini: task -> days ago;
    limits: task -> (percent at start, percent at end) of the 5-hour Codex window; partial: task -> why its reading is incomplete."""
    ledger = cost_center._empty()
    for (role, task), counts in buckets.items():
        ledger["buckets"][f"{role}\t{task}\t{MODEL}"] = counts
    for number, (task, days) in enumerate((active or {}).items()):
        key = f"claude|store|session-{number:03d}-aaaa"
        ledger["sessions"][key] = {"provider": "claude", "role": "delivery", "status": "counted"}
        ledger["windows"][key] = [{"task": task, "role": "delivery", "from": (NOW - timedelta(days=days)).isoformat()}]
    for number, (task, days) in enumerate((gemini or {}).items()):
        key = f"gemini|store|gemini-{number:03d}-bbbb"
        ledger["sessions"][key] = {"provider": "gemini", "role": "delivery"}
        ledger["windows"][key] = [{"task": task, "role": "delivery", "from": (NOW - timedelta(days=days)).isoformat()}]
    for number, (task, why) in enumerate((partial or {}).items()):
        key = f"codex|store|codex-{number:03d}-cccc"
        ledger["sessions"][key] = {"provider": "codex", "role": "delivery", "status": "partial", "reason": why}
        ledger["windows"][key] = [{"task": task, "role": "delivery", "from": (NOW - timedelta(days=1)).isoformat()}]
    for task, (start, end) in (limits or {}).items():
        ledger["limits"][task] = {"first": {"pct": start, "at": 1.0, "minutes": 300, "resets": 5}, "last": {"pct": end, "at": 2.0, "minutes": 300, "resets": 5}, "reset": False}
    cost_center._save(board, ledger)
    cost_center._memo.clear()

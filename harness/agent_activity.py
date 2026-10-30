# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Is a live agent terminal working, even though it has not touched the board? (task 2026-10-27_audit-run-defects)

Two signals, both read-only and both from the agent's own CLI, never from what the agent says:
- its terminal printed something recently (the supervisor records every chunk), and
- its own session file (the transcript the CLI appends to for every message and tool call) grew recently.
An agent reading a large codebase can go minutes without printing anything new; its session file still moves.

Used by BOTH places that could call an agent silent: the board watchdog (first wake-up) and the stall ladder
(self_heal). Using one answer in both is the point: a busy agent must never be woken, warned about, restarted or replaced.
The exemption is bounded (BUSY_MAX_SECONDS of board silence), so a CLI that only animates a spinner cannot hide a real stall.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUTPUT_SECONDS = 120
TRANSCRIPT_SECONDS = 300
BUSY_MAX_SECONDS = 30 * 60

_FILES: dict[tuple[str, str, str], Path] = {}


def _parsed(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def transcript_age(session: dict[str, Any], current: datetime) -> float | None:
    """Seconds since the CLI's own session file last grew, or None when it cannot be found."""
    from harness import cost_center
    provider = str(session.get("cli_session_provider") or session.get("provider") or "")
    vendor_id = str(session.get("cli_session_id") or "")
    store = str(session.get("cli_session_store") or "") or cost_center.default_store(provider)
    if not (provider and vendor_id and store):
        return None
    key = (provider, store, vendor_id)
    path = _FILES.get(key)
    try:
        if path is None or not path.is_file():
            path = cost_center.find_file(provider, store, vendor_id)
            if path is None:
                return None
            _FILES[key] = path
        return current.timestamp() - os.stat(path).st_mtime
    except OSError:
        return None


def seconds_since_activity(session: dict[str, Any] | None, current: datetime, output_counts: bool = True) -> float | None:
    """Seconds since the freshest sign of work from this session (the smaller of the two measured ages), or None when there is none."""
    if not session:
        return None
    ages = []
    printed = _parsed(session.get("last_output_at")) if output_counts else None
    if printed is not None and (current - printed).total_seconds() < OUTPUT_SECONDS:
        ages.append((current - printed).total_seconds())
    moved = transcript_age(session, current)
    if moved is not None and moved < TRANSCRIPT_SECONDS:
        ages.append(moved)
    return min(ages) if ages else None


def is_working(session: dict[str, Any] | None, current: datetime, board_silence: float, woken: bool = False) -> bool:
    """True when the terminal shows recent work and the board silence is still inside the bounded exemption.

    Once a wake-up has gone unanswered, printing alone proves nothing (a frozen CLI can still animate a spinner): then only the
    session file growing counts, because that needs real messages or tool calls.
    """
    return board_silence < BUSY_MAX_SECONDS and seconds_since_activity(session, current, output_counts=not woken) is not None

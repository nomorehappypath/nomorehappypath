# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Difficulty note (task 2026-10-26_difficulty-note): information only, never paperwork.

Delivery (on request-review) and the Reviewer (on qa-result) may add one short line saying what slowed them down. The board keeps it
with the task, the role, the step (the review request) and the time. A missing or empty note stores nothing and refuses nothing; an
overlong note is trimmed, never refused. The owner's task card and history show the latest note per role as one plain line.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

MAX_CHARS_DEFAULT, MAX_CHARS_LOW, MAX_CHARS_HIGH = 200, 40, 1000
ROLE_LABELS = {"builder": "builder", "reviewer": "reviewer"}
_ROLE_OF_AGENT = {"engineering": "builder", "development": "builder", "qa": "reviewer"}
_configured: dict[str, int] = {}


def configure(plumbing: Any) -> None:
    """Install the owner's length limit (the project worker passes the settings once at start); anything unusable keeps the default."""
    _configured.clear()
    value = plumbing.get("difficulty_note_max_chars") if isinstance(plumbing, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and MAX_CHARS_LOW <= value <= MAX_CHARS_HIGH:
        _configured["chars"] = value


def max_chars() -> int:
    return _configured.get("chars", MAX_CHARS_DEFAULT)


def clean(text: Any) -> str:
    """One line, trimmed to the limit; empty when there is nothing to keep."""
    line = " ".join(str(text or "").split())
    limit = max_chars()
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def record(state: dict[str, Any], agent: dict[str, Any] | None, task: str, step: str, text: Any) -> bool:
    """Keep the note on the task. Returns False (and changes nothing) when there is no note, no task or no known role."""
    line = clean(text)
    role = _ROLE_OF_AGENT.get(str((agent or {}).get("role") or ""))
    if not line or not task or not role:
        return False
    notes = state.setdefault("difficulty_notes", {}).setdefault(task, [])
    notes.append({"role": role, "step": step, "at": datetime.now(timezone.utc).isoformat(), "text": line})
    del notes[:-20]
    return True


def latest(state: dict[str, Any], task: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for note in (state.get("difficulty_notes") or {}).get(task) or []:
        if isinstance(note, dict) and note.get("role") in ROLE_LABELS and str(note.get("text") or "").strip():
            out[note["role"]] = note
    return out


def line(state: dict[str, Any], task: str) -> str:
    """The owner's one plain line (empty when nobody left a note). The page escapes it when it draws it."""
    notes = latest(state, task)
    return " · ".join(f"What slowed the {role}: {notes[role]['text']}" for role in ("builder", "reviewer") if role in notes)

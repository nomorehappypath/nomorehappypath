# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Measured lifecycle phases and owner-readable critical-path summaries.

The board remains the durable source of truth.  This module adds timestamps to
records that are already persisted and derives summaries without creating a
second telemetry database or inferring timing from status prose.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
from typing import Any, Iterable


DEFAULT_TOLERANCE_SECONDS = 0.01

# When phases overlap, the more specific phase owns that wall-clock segment.
# This makes the exclusive totals reconcile exactly while retaining the raw
# phase intervals for audit.
PHASE_PRIORITY = (
    "health",
    "push",
    "release_checks",
    "completion",
    "release_ready",
    "verdict",
    "formal_review",
    "challenge_authoring",
    "review_queue",
    "scenario_execution",
    "unit_execution",
    "repair",
    "implementation",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def duration_seconds(started_at: str, finished_at: str) -> float | None:
    started = _datetime(started_at)
    finished = _datetime(finished_at)
    if started is None or finished is None or finished < started:
        return None
    return round((finished - started).total_seconds(), 6)


def phase(started_at: str, finished_at: str) -> dict[str, Any]:
    duration = duration_seconds(started_at, finished_at)
    if duration is None:
        return {}
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration,
    }


def command_measurement(
    command: str,
    started_at: str,
    finished_at: str,
    *,
    exit_code: int,
    cache_decision: str = "executed_no_cache_store",
) -> dict[str, Any]:
    measured = phase(started_at, finished_at)
    measured.update({
        "command": command,
        "command_fingerprint": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "exit_code": int(exit_code),
        "cache_decision": cache_decision,
    })
    return measured


def _requests(state: dict[str, Any], task: str) -> list[dict[str, Any]]:
    values = [
        request for request in state.get("qa_requests", {}).values()
        if request.get("task") == task
    ]
    values.extend(
        entry.get("value", {}) for entry in state.get("archive", [])
        if entry.get("kind") == "qa_request"
        and entry.get("value", {}).get("task") == task
    )
    by_id = {str(value.get("id", index)): value for index, value in enumerate(values)}
    return list(by_id.values())


def _task_events(state: dict[str, Any], task: str) -> list[dict[str, Any]]:
    return sorted(
        (event for event in state.get("events", []) if event.get("task") == task),
        key=lambda event: (str(event.get("at", "")), int(event.get("sequence", 0))),
    )


def _event_time(events: Iterable[dict[str, Any]], *kinds: str, last: bool = False) -> str:
    matches = [str(event.get("at", "")) for event in events if event.get("kind") in kinds and _datetime(str(event.get("at", "")))]
    if not matches:
        return ""
    return matches[-1] if last else matches[0]


def _fallback_review_phases(request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Reconstruct historical phases only where durable boundaries exist."""
    phases: dict[str, dict[str, Any]] = {}
    candidates = {
        "review_queue": (request.get("review_wait_started_at") or request.get("requested_at"), request.get("reserved_at") or request.get("claimed_at")),
        "challenge_authoring": (request.get("reserved_at"), request.get("challenge_ledger_attached_at") or request.get("claimed_at")),
        "formal_review": (request.get("challenge_ledger_attached_at") or request.get("claimed_at"), request.get("verdict_started_at") or request.get("completed_at")),
        "verdict": (request.get("verdict_started_at"), request.get("completed_at")),
    }
    for name, (started, finished) in candidates.items():
        measured = phase(str(started or ""), str(finished or ""))
        if measured:
            phases[name] = measured
    return phases


def _intervals(state: dict[str, Any], task: str) -> tuple[list[tuple[str, datetime, datetime]], list[dict[str, Any]]]:
    intervals: list[tuple[str, datetime, datetime]] = []
    commands: list[dict[str, Any]] = []
    for request in _requests(state, task):
        lifecycle = request.get("lifecycle", {})
        phases = lifecycle if isinstance(lifecycle, dict) else {}
        fallback = _fallback_review_phases(request)
        for name, value in {**fallback, **phases}.items():
            if name == "commands" or not isinstance(value, dict):
                continue
            started = _datetime(str(value.get("started_at", "")))
            finished = _datetime(str(value.get("finished_at", "")))
            if started is not None and finished is not None and finished >= started:
                intervals.append((name, started, finished))
        commands.extend(
            value for value in request.get("command_executions", [])
            if isinstance(value, dict)
        )
    release = state.get("releases", {}).get(task, {})
    for name, value in release.get("lifecycle", {}).items() if isinstance(release.get("lifecycle"), dict) else []:
        if not isinstance(value, dict):
            continue
        started = _datetime(str(value.get("started_at", "")))
        finished = _datetime(str(value.get("finished_at", "")))
        if started is not None and finished is not None and finished >= started:
            intervals.append((name, started, finished))
    return intervals, commands


def task_summary(
    state: dict[str, Any], task: str, *, tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    events = _task_events(state, task)
    release = state.get("releases", {}).get(task, {})
    started_at = _event_time(events, "task_begun")
    if not started_at:
        requests = _requests(state, task)
        candidates = [str(value.get("requested_at", "")) for value in requests if _datetime(str(value.get("requested_at", "")))]
        started_at = min(candidates, default="")
    ended_at = str(release.get("recorded_at", "")) or _event_time(
        events, "visual_test_required", "development_complete", last=True,
    )
    if not ended_at and events:
        ended_at = str(events[-1].get("at", ""))
    start = _datetime(started_at)
    end = _datetime(ended_at)
    intervals, commands = _intervals(state, task)
    if start is None or end is None or end < start:
        return {
            "task": task,
            "status": "insufficient_history",
            "started_at": started_at,
            "ended_at": ended_at,
            "wall_clock_seconds": None,
            "phase_totals_seconds": {},
            "command_execution_count": sum(value.get("cache_decision") != "same_request_deduplicated" for value in commands),
            "cache_decision_counts": dict(Counter(str(value.get("cache_decision", "unrecorded")) for value in commands)),
            "reconciliation": {"within_tolerance": False, "reason": "task boundaries unavailable"},
        }

    clipped = [
        (name, max(start, left), min(end, right))
        for name, left, right in intervals
        if min(end, right) >= max(start, left)
    ]
    boundaries = sorted({start, end, *(left for _, left, _ in clipped), *(right for _, _, right in clipped)})
    priority = {name: index for index, name in enumerate(PHASE_PRIORITY)}
    exclusive: Counter[str] = Counter()
    for left, right in zip(boundaries, boundaries[1:]):
        if right <= left:
            continue
        active = [name for name, item_start, item_end in clipped if item_start <= left and item_end >= right]
        selected = min(active, key=lambda name: priority.get(name, len(priority))) if active else "unattributed"
        exclusive[selected] += (right - left).total_seconds()
    totals = {name: round(value, 6) for name, value in sorted(exclusive.items())}
    wall = round((end - start).total_seconds(), 6)
    reconciled = round(sum(totals.values()), 6)
    difference = round(abs(wall - reconciled), 6)
    raw_totals: Counter[str] = Counter()
    for name, left, right in clipped:
        raw_totals[name] += (right - left).total_seconds()
    cache_counts = Counter(str(value.get("cache_decision", "unrecorded")) for value in commands)
    executed_commands = sum(
        value.get("cache_decision") not in {"same_request_deduplicated", "cache_hit"}
        for value in commands
    )
    return {
        "task": task,
        "status": "measured",
        "started_at": started_at,
        "ended_at": ended_at,
        "wall_clock_seconds": wall,
        "phase_totals_seconds": totals,
        "raw_phase_totals_seconds": {name: round(value, 6) for name, value in sorted(raw_totals.items())},
        "command_execution_count": executed_commands,
        "cache_decision_counts": dict(sorted(cache_counts.items())),
        "reconciliation": {
            "exclusive_total_seconds": reconciled,
            "difference_seconds": difference,
            "tolerance_seconds": tolerance_seconds,
            "within_tolerance": difference <= tolerance_seconds,
        },
    }


def summaries(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tasks = {
        str(event.get("task")) for event in state.get("events", []) if event.get("task")
    }
    tasks.update(
        str(request.get("task")) for request in state.get("qa_requests", {}).values() if request.get("task")
    )
    tasks.update(str(task) for task in state.get("releases", {}))
    return {task: task_summary(state, task) for task in sorted(tasks)}

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""P0 lifecycle instrumentation: mechanically derived task timing (item 2).

Every number is a projection over durable board records — qa_request
timestamps and the append-only event log. Nothing here writes anything, so
instrumentation overhead on the write path is zero by construction; the
projection itself is bounded by one pass over the task's records.

Outputs per task:
  - phases: definition, implementation, review cycles (queue wait / authoring
    + execution split per request), repair turnarounds, mechanical tail
    (final PASS -> completion -> release -> owner decision);
  - critical_path: the ordered spans with durations, summing (within recorded
    gaps) to the task's wall clock;
  - duplicate_executions: upper-bound count of identical (command, scenario)
    evidence lines appearing in more than one review request — the number
    that bounds any execute-once savings claim. Labeled an upper bound: some
    repeats were legitimately forced by candidate changes.
"""
from __future__ import annotations

import json
import fcntl
import os
import re
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from harness import board, control
from harness.project_context import ProjectRoot, project_context


CHAT_METRICS_VERSION = 1
CHAT_OUTCOMES = {"answered", "unknown", "rejected", "cancelled", "failure", "refused"}
CHAT_PHASES = {"selection_ms", "provider_ms", "validation_ms", "total_ms"}


def chat_metrics_path(root: ProjectRoot) -> Path:
    return project_context(root).storage_path("control", "chat-metrics.json")


def record_chat_measurement(root: ProjectRoot, *, outcome: str, **phases: float) -> dict[str, Any]:
    """Update bounded, non-secret chat aggregates.

    The document never contains prompts, answers, paths, credentials, source
    values, or per-request receipts.  Counts plus aggregate/max/last timings
    preserve operational observability without unbounded growth.
    """
    if outcome not in CHAT_OUTCOMES:
        raise ValueError("unsupported chat measurement outcome")
    unknown = set(phases) - CHAT_PHASES
    if unknown:
        raise ValueError("unsupported chat measurement phases: " + ", ".join(sorted(unknown)))
    normalized = {
        name: max(0.0, round(float(phases.get(name, 0.0)), 3))
        for name in sorted(CHAT_PHASES)
    }
    path = chat_metrics_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                current = {
                    "version": CHAT_METRICS_VERSION,
                    "request_count": 0,
                    "outcomes": {name: 0 for name in sorted(CHAT_OUTCOMES)},
                    "phases": {
                        name: {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "last_ms": 0.0}
                        for name in sorted(CHAT_PHASES)
                    },
                }
            if int(current.get("version", 0)) != CHAT_METRICS_VERSION:
                raise ValueError("chat metrics version is unsupported")
            current["request_count"] = int(current.get("request_count", 0)) + 1
            outcomes = current.setdefault("outcomes", {})
            for name in CHAT_OUTCOMES:
                outcomes[name] = int(outcomes.get(name, 0))
            outcomes[outcome] += 1
            phase_values = current.setdefault("phases", {})
            for name, milliseconds in normalized.items():
                row = phase_values.setdefault(name, {})
                row["count"] = int(row.get("count", 0)) + 1
                row["total_ms"] = round(float(row.get("total_ms", 0.0)) + milliseconds, 3)
                row["max_ms"] = max(float(row.get("max_ms", 0.0)), milliseconds)
                row["last_ms"] = milliseconds
            current["updated_at"] = datetime.now().astimezone().isoformat()
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            try:
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            return json.loads(json.dumps(current))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _parse(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def _seconds(start: str, end: str) -> float | None:
    a, b = _parse(start), _parse(end)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def _task_events(root: Path, task: str) -> list[dict[str, Any]]:
    """All durable events for a task, oldest first (survives hot-state caps)."""
    path = board.board_dir(root) / "events.jsonl"
    events: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if value.get("task") == task:
                events.append(value)
    if not events:
        events = [e for e in board.snapshot(root).get("events", []) if e.get("task") == task]
    return events


def _all_events(root: Path) -> list[dict[str, Any]]:
    """Return every parseable durable event, including global rotation events."""
    path = board.board_dir(root) / "events.jsonl"
    events: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events or list(board.snapshot(root).get("events", []))


def _first(events: list[dict[str, Any]], kind: str) -> str:
    return next((e.get("at", "") for e in events if e.get("kind") == kind), "")


def _last(events: list[dict[str, Any]], kind: str) -> str:
    return next((e.get("at", "") for e in reversed(events) if e.get("kind") == kind), "")


def _task_requests(root: Path, task: str) -> list[dict[str, Any]]:
    state = board.historical_snapshot(root) if hasattr(board, "historical_snapshot") else board.snapshot(root)
    active = list((state.get("qa_requests") or {}).values())
    archived = [entry.get("value", {}) for entry in state.get("archive", [])
                if entry.get("kind") == "qa_request"]
    seen: dict[str, dict[str, Any]] = {}
    for request in active + archived:
        if request.get("task") == task and request.get("id"):
            seen.setdefault(request["id"], request)
    return sorted(seen.values(), key=lambda r: str(r.get("requested_at", "")))


def task_metrics(root: Path, task: str) -> dict[str, Any]:
    events = _task_events(root, task)
    requests = _task_requests(root, task)
    begun = _first(events, "task_begun")
    confirmed = _first(events, "requirements_confirmed")
    completed = _last(events, "development_complete")
    released = _last(events, "visual_test_required")
    decided = _last(events, "owner_release_decision_recorded")
    first_request = requests[0].get("requested_at", "") if requests else ""

    reviews = []
    for request in requests:
        queue_wait = _seconds(request.get("requested_at", ""), request.get("claimed_at", ""))
        execution = _seconds(request.get("claimed_at", ""), request.get("completed_at", ""))
        reviews.append({
            "id": request.get("id", ""),
            "phase": request.get("phase", ""),
            "cycle": request.get("cycle", 0),
            "result": request.get("status", ""),
            "queue_wait_seconds": queue_wait,
            "review_execution_seconds": execution,
        })
    repair_turnarounds = []
    for index, request in enumerate(requests[:-1]):
        if request.get("status") == "failed":
            turnaround = _seconds(request.get("completed_at", ""),
                                  requests[index + 1].get("requested_at", ""))
            if turnaround is not None:
                repair_turnarounds.append(round(turnaround, 1))

    phases = {
        "definition_seconds": _seconds(begun, confirmed),
        "implementation_seconds": _seconds(confirmed, first_request),
        "review_queue_wait_seconds": round(sum(r["queue_wait_seconds"] or 0 for r in reviews), 1),
        "review_execution_seconds": round(sum(r["review_execution_seconds"] or 0 for r in reviews), 1),
        "repair_turnaround_seconds": round(sum(repair_turnarounds), 1),
        "mechanical_tail_seconds": _seconds(completed or released, released) if released else None,
        "owner_wait_seconds": _seconds(released, decided) if decided else None,
    }
    total = _seconds(begun, decided or released or (requests[-1].get("completed_at", "") if requests else ""))
    return {
        "task": task,
        "started_at": begun,
        "concluded_at": decided or released,
        "total_seconds": round(total, 1) if total is not None else None,
        "phases": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in phases.items()},
        "reviews": reviews,
        "repair_turnarounds": repair_turnarounds,
        "review_count": len(reviews),
        "failed_cycles": sum(1 for r in reviews if r["result"] == "failed"),
    }


_COMMAND_LINE = re.compile(r"^\s*command:\s*(.+)$", re.I | re.M)
_SCENARIO_LINE = re.compile(r"^\s*scenario:\s*(\S+)", re.I | re.M)


def duplicate_execution_count(
    root: ProjectRoot, task: str, *, measurement_cutoff_at: str = "",
) -> dict[str, Any]:
    """Upper bound of identical (command) evidence lines across a task's requests.

    This is the number that bounds P1's savings claim (Codex round-2 item 2):
    only exact repeats across DIFFERENT requests count, and the figure is an
    upper bound because some repeats were forced by candidate changes.
    """
    cutoff = _parse(measurement_cutoff_at) if measurement_cutoff_at else None
    if measurement_cutoff_at and cutoff is None:
        raise ValueError("measurement cutoff must be an ISO timestamp")
    requests = [
        request for request in _task_requests(root, task)
        if cutoff is None
        or (_parse(str(request.get("requested_at", ""))) is not None
            and _parse(str(request.get("requested_at", ""))) <= cutoff)
    ]
    code_root = project_context(root).code_root
    per_request_commands: list[set[str]] = []
    for request in requests:
        commands: set[str] = set()
        for key in ("evidence", "ledger", "challenge_ledger"):
            value = str(request.get(key) or "")
            path = Path(value)
            if not path.is_absolute():
                path = code_root / value
            if value and path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                commands.update(m.strip() for m in _COMMAND_LINE.findall(text))
        artifacts = request.get("certified_artifacts") or {}
        for manifest in artifacts.values():
            source = str((manifest or {}).get("path") or (manifest or {}).get("source_path") or "")
            path = Path(source)
            if source and path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                commands.update(m.strip() for m in _COMMAND_LINE.findall(text))
        if commands:
            per_request_commands.append(commands)
    counts: dict[str, int] = {}
    for commands in per_request_commands:
        for command in commands:
            counts[command] = counts.get(command, 0) + 1
    duplicates = {cmd: n for cmd, n in counts.items() if n > 1}
    return {
        "task": task,
        "requests_with_evidence": len(per_request_commands),
        "distinct_commands": len(counts),
        "duplicated_commands": len(duplicates),
        "duplicate_executions_upper_bound": sum(n - 1 for n in duplicates.values()),
        "note": "upper bound: repeats forced by candidate changes are included",
    }


def field_trial_metrics(
    root: Path, task: str, *, measurement_cutoff_at: str = "",
) -> dict[str, Any]:
    """Project every Task D field-trial measure without inventing missing data.

    Older live requests may predate one or more P0 fields.  Such gaps are
    returned as request IDs in ``missing_records``; their durations remain
    ``None`` instead of being included as zero in a total.
    """
    cutoff_limit = _parse(measurement_cutoff_at) if measurement_cutoff_at else None
    if measurement_cutoff_at and cutoff_limit is None:
        raise ValueError("measurement cutoff must be an ISO timestamp")
    all_events = [
        event for event in _all_events(root)
        if cutoff_limit is None
        or (_parse(str(event.get("at", ""))) is not None
            and _parse(str(event.get("at", ""))) <= cutoff_limit)
    ]
    events = [event for event in all_events if event.get("task") == task]
    requests = [
        request for request in _task_requests(root, task)
        if cutoff_limit is None
        or (_parse(str(request.get("requested_at", ""))) is not None
            and _parse(str(request.get("requested_at", ""))) <= cutoff_limit)
    ]
    started_at = _first(events, "task_begun")
    timestamp_candidates = [str(event.get("at", "")) for event in events]
    for request in requests:
        timestamp_candidates.extend(str(request.get(key, "") or "") for key in (
            "requested_at", "reserved_at", "challenge_ledger_attached_at",
            "claimed_at", "completed_at",
        ))
    parsed_candidates = [(value, _parse(value)) for value in timestamp_candidates]
    cutoff_at = measurement_cutoff_at or max(
        ((value, parsed) for value, parsed in parsed_candidates if parsed is not None),
        key=lambda item: item[1], default=("", None),
    )[0]

    def measured_seconds(left: str, right: str) -> float | None:
        if cutoff_limit is not None:
            end = _parse(right)
            if end is None or end > cutoff_limit:
                return None
        return _seconds(left, right)

    execution_starts: dict[str, list[str]] = {}
    execution_intervals: dict[str, list[tuple[str, str]]] = {}
    for event in events:
        request_id = str(event.get("request_id", ""))
        kind = str(event.get("kind", ""))
        if not request_id:
            continue
        if kind == "review_execution_started":
            execution_starts.setdefault(request_id, []).append(str(event.get("at", "")))
        elif kind == "review_execution_finished" and execution_starts.get(request_id):
            started = execution_starts[request_id].pop(0)
            execution_intervals.setdefault(request_id, []).append(
                (started, str(event.get("at", "")))
            )

    review_rows = []
    missing: dict[str, list[str]] = {
        "queue_wait": [], "challenge_authoring": [], "challenge_execution": [],
        "verdict": [], "commands": [], "implementation": [],
    }
    phase_values: dict[str, list[float]] = {
        "queue_wait": [], "challenge_authoring": [], "challenge_execution": [],
        "verdict": [], "implementation": [],
    }
    commands: list[dict[str, Any]] = []
    for request in requests:
        request_id = str(request.get("id", ""))
        intervals = execution_intervals.get(request_id, [])
        interval_durations = [
            measured_seconds(started, finished) for started, finished in intervals
        ]
        complete_interval_durations = [
            value for value in interval_durations if value is not None
        ]
        last_execution_finished = intervals[-1][1] if intervals else ""
        values = {
            "queue_wait": measured_seconds(
                str(request.get("review_wait_started_at") or request.get("requested_at") or ""),
                str(request.get("reserved_at") or request.get("claimed_at") or ""),
            ),
            "challenge_authoring": measured_seconds(
                str(request.get("reserved_at") or ""),
                str(request.get("challenge_ledger_attached_at") or request.get("claimed_at") or ""),
            ),
            "challenge_execution": (
                sum(complete_interval_durations)
                if intervals and len(complete_interval_durations) == len(intervals) else None
            ),
            "verdict": measured_seconds(
                last_execution_finished,
                str(request.get("completed_at") or ""),
            ),
        }
        implementation = (request.get("lifecycle") or {}).get("implementation", {})
        values["implementation"] = (
            measured_seconds(
                str(implementation.get("started_at", "")),
                str(implementation.get("finished_at", "")),
            ) if isinstance(implementation, dict) else None
        )
        request_commands = [
            value for value in request.get("command_executions", [])
            if isinstance(value, dict)
            and (cutoff_limit is None
                 or (_parse(str(value.get("finished_at", ""))) is not None
                     and _parse(str(value.get("finished_at", ""))) <= cutoff_limit))
        ]
        commands.extend(request_commands)
        for name, value in values.items():
            if value is None:
                missing[name].append(request_id)
            else:
                phase_values[name].append(value)
        if not request_commands:
            missing["commands"].append(request_id)
        review_rows.append({
            "id": request_id,
            "phase": request.get("phase", ""),
            "subtask": request.get("subtask", ""),
            "chunk": request.get("chunk", ""),
            "cycle": request.get("cycle", 0),
            "result": request.get("status", ""),
            **{f"{name}_seconds": round(value, 6) if value is not None else None
               for name, value in values.items()},
            "recorded_command_count": len(request_commands),
            "challenge_execution_interval_count": len(intervals),
            "recorded_command_seconds": (
                round(sum(
                    float(value.get("duration_seconds", 0)) for value in request_commands
                    if isinstance(value.get("duration_seconds"), (int, float))
                ), 6) if request_commands else None
            ),
        })

    repair_turnarounds = []
    for failed in (request for request in requests if request.get("status") == "failed"):
        next_cycles = [
            candidate for candidate in requests
            if candidate.get("phase") == failed.get("phase")
            and candidate.get("subtask") == failed.get("subtask")
            and candidate.get("chunk") == failed.get("chunk")
            and int(candidate.get("cycle", 0)) > int(failed.get("cycle", 0))
            and _parse(str(candidate.get("requested_at", ""))) is not None
        ]
        next_cycle = min(next_cycles, key=lambda value: str(value.get("requested_at", "")), default=None)
        duration = measured_seconds(
            str(failed.get("completed_at", "")),
            str((next_cycle or {}).get("requested_at", "")),
        )
        repair_turnarounds.append({
            "failed_request": failed.get("id", ""),
            "next_request": (next_cycle or {}).get("id", ""),
            "duration_seconds": round(duration, 6) if duration is not None else None,
        })

    start = _parse(started_at)
    cutoff = _parse(cutoff_at)
    rotation_kinds = {"reviewer_rotated", "cto_rotated"}
    rotations = [
        event for event in all_events
        if event.get("kind") in rotation_kinds
        and start is not None and cutoff is not None
        and (_parse(str(event.get("at", ""))) or start) >= start
        and (_parse(str(event.get("at", ""))) or cutoff) <= cutoff
    ]
    reviewer_agents: list[str] = []
    for event in events:
        agent_id = str(event.get("agent_id", ""))
        if event.get("role") == "qa" and agent_id and agent_id != "system":
            if not reviewer_agents or reviewer_agents[-1] != agent_id:
                reviewer_agents.append(agent_id)
    reviewer_transitions = max(0, len(reviewer_agents) - 1)
    explicit_reviewer_rotations = sum(
        event.get("kind") == "reviewer_rotated" for event in rotations
    )
    rotation_gap = reviewer_transitions > explicit_reviewer_rotations

    final_passes = [
        request for request in requests
        if request.get("phase") == "final_acceptance" and request.get("status") == "passed"
    ]
    final_pass = max(final_passes, key=lambda value: str(value.get("completed_at", "")), default={})
    release_at = _last(events, "visual_test_required")
    owner_decision_at = _last(events, "owner_release_decision_recorded")
    release_tail = measured_seconds(str(final_pass.get("completed_at", "")), release_at)
    owner_wait = measured_seconds(release_at, owner_decision_at)
    cache_counts = Counter(str(value.get("cache_decision", "unrecorded")) for value in commands)
    recorded_command_seconds = round(sum(
        float(value.get("duration_seconds", 0)) for value in commands
        if isinstance(value.get("duration_seconds"), (int, float))
    ), 6)
    # Route delivery is an explicit P0 boundary: the board records which
    # instruction belongs to this task and the control plane independently
    # records when that exact instruction reached its managed terminal.  Keep
    # missing/expired receipts visible rather than treating them as zero.
    route_rows = []
    route_values = []
    missing_routes = []
    for event in events:
        if event.get("kind") != "instruction_route_queued" or not event.get("instruction_id"):
            continue
        instruction_id = str(event["instruction_id"])
        try:
            receipt = control.instruction_receipt(root, instruction_id)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            receipt = {}
        queued_at = str(receipt.get("queued_at") or event.get("at") or "")
        delivered_at = str(receipt.get("delivered_at") or "")
        duration = measured_seconds(queued_at, delivered_at)
        if duration is None:
            missing_routes.append(instruction_id)
        else:
            route_values.append(duration)
        route_rows.append({
            "instruction_id": instruction_id,
            "source": event.get("source", receipt.get("source", "")),
            "request_id": event.get("request_id", ""),
            "status": receipt.get("status", "missing"),
            "delivery_seconds": round(duration, 6) if duration is not None else None,
        })
    return {
        "task": task,
        "measurement_cutoff_at": cutoff_at,
        "wall_clock_to_cutoff_seconds": (
            round(measured_seconds(started_at, cutoff_at), 6)
            if measured_seconds(started_at, cutoff_at) is not None else None
        ),
        "definition_seconds": measured_seconds(started_at, _first(events, "requirements_confirmed")),
        "phase_totals_seconds": {
            name: round(sum(values), 6) if values else None
            for name, values in phase_values.items()
        },
        "reviews": review_rows,
        "review_count": len(requests),
        "failed_cycles": sum(request.get("status") == "failed" for request in requests),
        "repair_turnarounds": repair_turnarounds,
        "recorded_command_count": len(commands),
        "recorded_command_seconds": recorded_command_seconds,
        "route_delivery_seconds": (
            round(sum(route_values), 6) if route_values else None
        ),
        "route_deliveries": route_rows,
        "cache_decision_counts": dict(sorted(cache_counts.items())),
        "duplicates": duplicate_execution_count(
            root, task, measurement_cutoff_at=measurement_cutoff_at,
        ),
        "context_rotations": {
            "count": None if rotation_gap else len(rotations),
            "status": "instrumentation_gap" if rotation_gap else "measured",
            "explicit_event_count": len(rotations),
            "event_sequences": [event.get("sequence") for event in rotations],
            "observed_reviewer_agents": reviewer_agents,
            "observed_reviewer_transition_count": reviewer_transitions,
            "note": (
                "reviewer identity changed without a matching reviewer_rotated event; "
                "the context-rotation total is unavailable, not zero"
                if rotation_gap else "rotation total is derived from durable rotation events"
            ),
        },
        "parallel_subtask_overlap_events": [
            {"sequence": event.get("sequence"), "subtask": event.get("subtask", ""),
             "concurrent_with": event.get("concurrent_with", [])}
            for event in events
            if event.get("kind") == "subtask_started" and event.get("concurrent_with")
        ],
        "release_tail_seconds": release_tail,
        "owner_wait_seconds": owner_wait,
        "unavailable": {
            "release_tail_seconds": (
                "final acceptance PASS or VISUAL_TEST_REQUIRED is not yet recorded"
                if release_tail is None else ""
            ),
            "owner_wait_seconds": (
                "VISUAL_TEST_REQUIRED or owner decision is not yet recorded"
                if owner_wait is None else ""
            ),
            "route_delivery_seconds": (
                "one or more routed instructions lack a durable delivered receipt"
                if missing_routes else ""
            ),
        },
        "missing_records": {
            **{name: ids for name, ids in missing.items() if ids},
            **({"route_delivery": missing_routes} if missing_routes else {}),
        },
        "note": "Totals include only mechanically recorded intervals; missing intervals are never treated as zero.",
    }

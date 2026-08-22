# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Deterministic, board-owned lifecycle reports.

Reports are projections of durable evidence. They never modify the candidate
repository and never turn missing or inconsistent measurements into zeroes.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

from harness import board, lifecycle_metrics


def _strict_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ValueError(f"durable event log is unreadable: {error}") from error
    if payload and not payload.endswith(b"\n"):
        raise ValueError("durable event log ends with a torn partial record")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise ValueError("durable event log is not valid UTF-8") from error
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"durable event record {line_number} is corrupt") from error
        if not isinstance(value, dict) or not value.get("kind") or not value.get("at"):
            raise ValueError(f"durable event record {line_number} is incomplete")
        records.append(value)
    return records


def _exact_duplicate_executions(requests: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    unavailable = 0
    for request in requests:
        seen: set[str] = set()
        executions = request.get("command_executions") or []
        for execution in executions:
            identity = str((execution or {}).get("execution_identity") or "")
            if identity:
                seen.add(identity)
            else:
                unavailable += 1
        for identity in seen:
            counts[identity] = counts.get(identity, 0) + 1
    repeated = {identity: count for identity, count in counts.items() if count > 1}
    return {
        "distinct_execution_identities": len(counts),
        "duplicate_exact_success_references": sum(count - 1 for count in repeated.values()),
        "records_without_execution_identity": unavailable,
        "status": "Unavailable" if unavailable else "Measured",
    }


def _time_anomalies(requests: list[dict[str, Any]]) -> list[dict[str, str]]:
    ordered_fields = (
        "requested_at", "reserved_at", "challenge_ledger_attached_at",
        "claimed_at", "completed_at",
    )
    anomalies: list[dict[str, str]] = []
    for request in requests:
        present = [
            (name, lifecycle_metrics._parse(str(request.get(name) or "")))
            for name in ordered_fields
        ]
        present = [(name, value) for name, value in present if value is not None]
        for (left_name, left), (right_name, right) in zip(present, present[1:]):
            if left is not None and right is not None and right < left:
                anomalies.append({
                    "request": str(request.get("id") or "Unavailable"),
                    "earlier_field": left_name,
                    "earlier_value": left.isoformat(),
                    "later_field": right_name,
                    "later_value": right.isoformat(),
                })
    return anomalies


def build(root: Path, task: str, *, measurement_cutoff_at: str = "") -> dict[str, Any]:
    event_path = board.board_dir(root) / "events.jsonl"
    events = _strict_jsonl(event_path)
    task_events = [event for event in events if event.get("task") == task]
    requests = lifecycle_metrics._task_requests(root, task)
    source = {
        "task_events": task_events,
        "requests": requests,
        "measurement_cutoff_at": measurement_cutoff_at,
    }
    if hasattr(lifecycle_metrics, "field_trial_metrics"):
        metrics = lifecycle_metrics.field_trial_metrics(
            root, task, measurement_cutoff_at=measurement_cutoff_at,
        )
    else:
        legacy = lifecycle_metrics.task_metrics(root, task)
        commands = [
            execution
            for request in requests
            for execution in (request.get("command_executions") or [])
            if isinstance(execution, dict)
        ]
        metrics = {
            "wall_clock_to_cutoff_seconds": legacy.get("total_seconds"),
            "recorded_command_seconds": (
                round(sum(float(value.get("duration_seconds", 0)) for value in commands), 6)
                if commands else None
            ),
            "review_count": legacy.get("review_count"),
            "failed_cycles": legacy.get("failed_cycles"),
            "missing_records": ({"commands": [
                str(request.get("id") or "Unavailable") for request in requests
                if not request.get("command_executions")
            ]} if any(not request.get("command_executions") for request in requests) else {}),
        }
    return {
        "version": 1,
        "task": task,
        "source_sha256": hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "metrics": metrics,
        "exact_execution_duplicates": _exact_duplicate_executions(requests),
        "time_anomalies": _time_anomalies(requests),
    }


def _display(value: Any) -> str:
    return "Unavailable" if value is None or value == "" else str(value)


def render(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        f"# Efficiency report: {report['task']}", "",
        f"Evidence identity: `{report['source_sha256']}`", "",
        "## Measured duration", "",
        f"- Wall clock: {_display(metrics.get('wall_clock_to_cutoff_seconds'))} seconds",
        f"- Recorded command time: {_display(metrics.get('recorded_command_seconds'))} seconds",
        f"- Review attempts: {_display(metrics.get('review_count'))}",
        f"- Failed review attempts: {_display(metrics.get('failed_cycles'))}", "",
        "## Execution reuse", "",
        f"- Exact repeated certified references: {_display(report['exact_execution_duplicates'].get('duplicate_exact_success_references'))}",
        f"- Records lacking an exact execution identity: {_display(report['exact_execution_duplicates'].get('records_without_execution_identity'))}", "",
        "## Integrity", "",
    ]
    anomalies = report.get("time_anomalies") or []
    if anomalies:
        for anomaly in anomalies:
            lines.append(
                "- Unavailable: request " + anomaly["request"] + " records "
                + anomaly["later_field"] + " (" + anomaly["later_value"] + ") before "
                + anomaly["earlier_field"] + " (" + anomaly["earlier_value"] + ")."
            )
    else:
        lines.append("- No backwards request intervals were found.")
    missing = metrics.get("missing_records") or {}
    if missing:
        lines.extend(("", "## Unavailable measurements", ""))
        for name in sorted(missing):
            lines.append(f"- {name}: {', '.join(str(value) for value in missing[name])}")
    lines.extend(("", "Generated deterministically from board-owned durable records.", ""))
    return "\n".join(lines)


def generate(root: Path, task: str, *, measurement_cutoff_at: str = "") -> dict[str, Any]:
    report = build(root, task, measurement_cutoff_at=measurement_cutoff_at)
    payload = render(report).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    directory = board.board_dir(root) / "generated-reports"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest}.md"
    if destination.exists():
        if destination.read_bytes() != payload:
            raise ValueError("content-addressed report conflicts with its digest")
    else:
        temporary = directory / f".{digest}.{secrets.token_hex(6)}.tmp"
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    return {"sha256": digest, "path": str(destination), "report": report}

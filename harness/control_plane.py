# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Bounded deterministic orchestration owned by the Python project server."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from datetime import datetime, timezone

from harness import board, release_coordinator


def _wake_recovered(root: Path, recovered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from harness import control

    routed = []
    seen: set[str] = set()
    for event in recovered:
        session_id = str(event.get("wake_session") or "")
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        try:
            instruction = control.enqueue_instruction(
                root, session_id,
                "CONTROL-PLANE RECOVERY: a stale execution lease was repaired "
                "atomically. Poll the board once and continue its recorded next "
                "action. Do not repeat any certified PASS. USER ACTION: None.",
                source="python-control-plane-recovery",
            )
            routed.append(instruction)
        except (OSError, ValueError):
            continue
    return routed


def _route_final_pass_completion(root: Path) -> list[dict[str, Any]]:
    """Keep a final PASS moving even when its first event wake was lost."""
    from harness import control

    routes: list[dict[str, str]] = []
    current = datetime.now(timezone.utc)
    with board.locked_state(root) as state:
        releases = state.get("releases", {})
        for request in state.get("qa_requests", {}).values():
            task = str(request.get("task") or "")
            if (
                request.get("phase") != "final_acceptance"
                or request.get("status") != "passed" or not task
                or task in releases
            ):
                continue
            developer = state.get("agents", {}).get(request.get("developer_id", ""), {})
            if not developer.get("active") or not developer.get("session_id"):
                continue
            lifecycle = state.setdefault("release_lifecycle", {}).setdefault(task, {"phases": {}})
            last = str(lifecycle.get("completion_route_last_at") or "")
            try:
                age = (current - datetime.fromisoformat(last)).total_seconds()
            except (TypeError, ValueError):
                age = board.REVIEW_ROUTE_RETRY_SECONDS
            if age < board.REVIEW_ROUTE_RETRY_SECONDS:
                continue
            lifecycle["completion_route_last_at"] = board.now()
            routes.append({
                "task": task, "session_id": str(developer["session_id"]),
                "request_id": str(request.get("id") or ""),
            })
    routed = []
    for route in routes:
        try:
            routed.append(control.enqueue_instruction(
                root, route["session_id"],
                f"FINAL PASS is certified for {route['task']}. Complete the saved "
                "Completion Contract now so the Python coordinator can prepare "
                "release. Do not rerun the final tests. USER ACTION: None.",
                source="python-final-pass-routing",
            ))
        except (OSError, ValueError):
            continue
    return routed


def tick(root: Path, stale_after: int = board.AGENT_STALE_SECONDS) -> dict[str, Any]:
    """Run one idempotent server-owned coordination pass without spawning agents."""
    if board.pause_state(root).get("status") != "active":
        return {
            "status": "paused", "recovered": [], "recovery_wakes": [],
            "final_pass_routes": [], "review_routes": [],
            "release_outcomes": [], "stalled": [],
        }
    recovered = board.recover_interrupted_executions(root)
    recovery_wakes = _wake_recovered(root, recovered)
    final_pass_routes = _route_final_pass_completion(root)
    review_routes = board.route_open_reviews(root)
    release_outcomes = release_coordinator.coordinate(root)
    stalled = board.mark_stalled(root, stale_after)
    return {
        "status": "active", "recovered": recovered,
        "recovery_wakes": recovery_wakes, "final_pass_routes": final_pass_routes,
        "review_routes": review_routes,
        "release_outcomes": release_outcomes, "stalled": stalled,
    }

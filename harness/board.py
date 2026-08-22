#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Durable multi-agent board with role-labelled events and QA cycles.

The board is intentionally a small stdlib-only service. Agents never invent a
status from chat: they register, poll with a persisted cursor, post concise
status, and exchange QA requests/results through this state file. Managed
reviewers are actively woken when work opens; agents use bounded polls on each
turn and never need a blocking polling helper.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

# Support the no-configuration form used by pasted directives:
# ``python3 /path/to/dev_harness/harness/board.py ...``.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import accepted_bytes, certified_execution, child_process, contract, execution_identity, execution_preflight, git_broker, git_process, lifecycle, project_memory, repair_package as repair_package_model, review_brief as review_brief_projection
from harness.project_context import ProjectContext, ProjectRoot, add_context_arguments, context_from_args, project_context


ROLES = {
    "development", "product_management", "qa", "cto", "engineering",
    "dev_ops", "ux_design",
}
DEVELOPER_ROLES = {"development", "engineering"}
TERMINAL_QA = {"passed", "failed", "cancelled"}
DEVELOPMENT_QA = "development_qa"
INDEPENDENT_REVIEW = "independent_review"
AWAITING_OWNER_DIRECTION = "AWAITING_OWNER_DIRECTION"
REVIEW_EXECUTION_STALE_SECONDS = 90
REVIEW_EXECUTION_HEARTBEAT_SECONDS = 5
REVIEW_ROUTE_RETRY_SECONDS = 90
REVIEW_RESERVATION_SECONDS = 600
# Normal LLM turns and focused test runs on the observed board regularly take
# 93-99 seconds. Four minutes keeps those healthy turns green. Only durable board
# polls, progress records, and exact execution leases count as activity.
AGENT_STALE_SECONDS = 240
WATCHDOG_INTERVAL_SECONDS = 15
# A live project task requires a real CTO board poll at least every five minutes.
# Terminal output and status posts are deliberately not substitutes for that
# supervisory poll. Route one watchdog tick before the deadline so scheduler
# granularity does not make the real poll late.
CTO_MONITOR_INTERVAL_SECONDS = 300
CTO_MONITOR_ROUTE_SECONDS = CTO_MONITOR_INTERVAL_SECONDS - WATCHDOG_INTERVAL_SECONDS
# A routed CLI turn has the same observed latency budget as other agent work.
# Keep it visibly recovering (never healthy) during that bounded response
# window. Dead managed sessions are still detected immediately above this
# state machine, so a longer grace does not hide a terminated terminal.
AUTO_RECOVERY_GRACE_SECONDS = AGENT_STALE_SECONDS
# After an automatic wake-up fails, the agent stays visibly stalled and is retried
# at most once per this window — never re-nudged every cycle (that is stall
# flapping: liveness oscillating stalled<->recovering with repeated wake-ups).
AUTO_RECOVERY_RETRY_SECONDS = 300
HOT_EVENT_WINDOW = 500
HOT_EVENT_BYTES = 90_000
# Board durability: periodic backups of the durable board files, stored OUTSIDE
# .harness so deleting .harness/board cannot also destroy the backups. Recovery
# on load restores from these instead of silently reinitializing over history.
BOARD_BACKUP_EVERY_EVENTS = 50
BOARD_BACKUP_KEEP = 8
# Board events that prove an agent is alive and advancing work, so recording one
# clears a stalled/recovering liveness. These MUST be the kinds actually emitted by
# _event — "status_update" (a technical status note) and "task_brief_updated" (the
# plain-language plan/update) are the two most common liveness proofs, so a mismatch
# here silently keeps a reporting agent shown as stalled and feeds stall flapping.
PROGRESS_EVENTS = {
    "status_update", "task_brief_updated", "chunks_declared", "subtask_chunks_declared",
    "independent_review_requested", "qa_claimed", "qa_result", "finding_classified",
    "finding_resolved", "task_begun", "subtask_started", "requirements_confirmed",
    "task_repository_bound", "qa_reserved", "reviewer_intents_recorded",
    "qa_challenge_ledger_attached", "qa_challenge_ledger_corrected",
}
MAX_REASON_LENGTH = 20_000
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_DEFERRED_FINDINGS_VISIBLE = 5
MAX_FINDING_TEXT_LENGTH = 20_000
ALLOWED_ATTACHMENT_TYPES = {
    "application/pdf": ".pdf",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "application/rtf": ".rtf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


class ProjectPausedError(ValueError):
    """A board mutation arrived after the durable pause gate closed."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def board_dir(root: ProjectRoot) -> Path:
    return project_context(root).storage_path("board")


def _state_path(root: Path) -> Path:
    return board_dir(root) / "state.json"


def _board_backup_root(root: ProjectRoot) -> Path:
    """Durable backup location OUTSIDE .harness (and outside the board's repo),
    so deleting .harness/board — e.g. `git clean -fdx` in that repo — cannot also
    destroy the backups."""
    return project_context(root).board_backup_root


def _log_board_recovery(root: Path, message: str) -> None:
    """Loudly record a board data-loss / recovery event — to stderr and to a
    durable marker outside .harness — so a wipe is never silent again."""
    code_root = project_context(root).code_root
    try:
        backups = _board_backup_root(root)
        backups.mkdir(parents=True, exist_ok=True)
        with (backups / "RECOVERY.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{now()} | {code_root} | {message}\n")
    except OSError:
        pass
    print(f"HARNESS BOARD RECOVERY | {code_root} | {message}", file=sys.stderr, flush=True)


def _continue_from_log(events: Path) -> dict[str, Any]:
    """state.json is gone but the append-only event log survived. Start a state
    that continues PAST the log's last sequence, so the durable log is never
    overwritten and its sequence numbers are never reused."""
    state = _initial_state()
    try:
        last_seq = 0
        for line in events.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                seq = int(json.loads(line).get("sequence", 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            last_seq = max(last_seq, seq)
        if last_seq:
            state["next_event"] = last_seq + 1
            state["last_event_exported"] = last_seq
            state["event_window_start"] = last_seq + 1
    except OSError:
        pass
    return state


def _recover_from_backup(root: Path, allow_restore: bool) -> dict[str, Any] | None:
    """Return the newest usable backup state, restoring the durable files to disk
    when holding the writer lock. None if no backup exists."""
    backups = _board_backup_root(root)
    if not backups.is_dir():
        return None
    snaps = sorted(p for p in backups.iterdir() if p.is_dir() and (p / "state.json").is_file())
    for snap in reversed(snaps):
        try:
            recovered = json.loads((snap / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if allow_restore:
            target = board_dir(root)
            target.mkdir(parents=True, exist_ok=True)
            for name in ("events.jsonl", "state.json"):
                source = snap / name
                if source.is_file() and not (target / name).exists():
                    try:
                        shutil.copy2(source, target / name)
                    except OSError:
                        pass
        _log_board_recovery(root, f"restored board from backup {snap.name}")
        return recovered
    return None


def _load_or_recover_state(root: Path, allow_restore: bool) -> dict[str, Any]:
    """Load board state, RECOVERING instead of silently reinitializing when the
    state file is missing or corrupt while prior history exists. This is the guard
    against silent board wipes: a fresh empty board is created ONLY when there is
    genuinely no prior history anywhere (backup or durable log)."""
    path = _state_path(root)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _log_board_recovery(root, "state.json unreadable/corrupt; attempting recovery")
            recovered = _recover_from_backup(root, allow_restore)
            if recovered is not None:
                return recovered
            events = board_dir(root) / "events.jsonl"
            if events.is_file() and events.stat().st_size > 0:
                return _continue_from_log(events)
            return _initial_state()
    recovered = _recover_from_backup(root, allow_restore)
    if recovered is not None:
        return recovered
    events = board_dir(root) / "events.jsonl"
    if events.is_file() and events.stat().st_size > 0:
        _log_board_recovery(root, "state.json absent with no backup; continuing past the durable event log")
        return _continue_from_log(events)
    return _initial_state()


def _snapshot_board(root: Path, state: dict[str, Any]) -> None:
    """Best-effort periodic backup of the durable board files, throttled by event
    count and stored outside .harness so a deletion there cannot take the backups
    too. Sets `_last_backup_event` before the caller persists state so the
    throttle survives restarts."""
    try:
        next_event = int(state.get("next_event", 1))
        if next_event - int(state.get("_last_backup_event", 0)) < BOARD_BACKUP_EVERY_EVENTS:
            return
        state["_last_backup_event"] = next_event
        source = board_dir(root)
        events = source / "events.jsonl"
        if not events.is_file():
            return
        backups = _board_backup_root(root)
        dest = backups / f"e{next_event:09d}"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(events, dest / "events.jsonl")
        (dest / "state.json").write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        for old in sorted(p for p in backups.iterdir() if p.is_dir())[:-BOARD_BACKUP_KEEP]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass


def _initial_state() -> dict[str, Any]:
    return {"next_event": 1, "role_counters": {}, "agents": {}, "qa_requests": {}, "qa_request_index": {}, "repair_packages": {}, "control_plane_holds": {}, "delivery_attempt_failures": [], "task_chunks": {}, "delivery_plans": {}, "task_briefs": {}, "task_baselines": {}, "task_workspaces": {}, "task_owner_directions": {}, "task_lineage": {}, "owner_directions": {}, "owner_messages": [], "owner_clarifications": {}, "pending_owner_clarifications": {}, "requirement_confirmations": {}, "releases": {}, "release_lifecycle": {}, "release_attempts": [], "release_decisions": {}, "release_repairs": {}, "deferred_findings": {}, "cancelled_tasks": {}, "reviewer_needed": None, "project_pause": {"status": "active"}, "events": [], "archive": [], "last_event_exported": 0}


def _cold_dir(root: Path) -> Path:
    return board_dir(root) / "archive"


def _cold_path(root: Path, kind: str) -> Path:
    return _cold_dir(root) / f"{kind}.jsonl"


def _cold_identity(kind: str, value: dict[str, Any]) -> str:
    if kind in {"qa_request", "qa_requests"}:
        return str(value.get("id", ""))
    if kind == "agent":
        return str(value.get("id", ""))
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _read_cold(root: Path, kind: str) -> list[dict[str, Any]]:
    path = _cold_path(root, kind)
    if not path.is_file():
        return []
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _append_cold(root: Path, kind: str, values: list[dict[str, Any]]) -> None:
    """Idempotently append cold records before removing them from hot state."""
    if not values:
        return
    directory = _cold_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    existing = {_cold_identity(kind, value) for value in _read_cold(root, kind)}
    path = _cold_path(root, kind)
    with path.open("a", encoding="utf-8") as stream:
        for value in values:
            identity = _cold_identity(kind, value)
            if not identity or identity in existing:
                continue
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            existing.add(identity)


def _rewrite_cold(root: Path, kind: str, values: list[dict[str, Any]]) -> None:
    """Replace one cold index after intentional owner cancellation."""
    path = _cold_path(root, kind)
    if not values:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        "".join(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n" for value in values),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _extract_cold_state(root: Path, state: dict[str, Any]) -> None:
    # A stopped Delivery terminal with unconsumed owner input is not terminal
    # history: it is the durable recovery source for the next Delivery launch.
    for cold_agent in _read_cold(root, "agents"):
        session_id = str(cold_agent.get("session_id", ""))
        direction = state.get("owner_directions", {}).get(session_id, {})
        if (
            cold_agent.get("role") in DEVELOPER_ROLES
            and cold_agent.get("task") == AWAITING_OWNER_DIRECTION
            and direction.get("text")
            and not direction.get("consumed")
            and not direction.get("transferred_to_session_id")
        ):
            state.setdefault("agents", {}).setdefault(cold_agent.get("id", ""), cold_agent)
    directions = state.setdefault("task_owner_directions", {})
    lineage = state.setdefault("task_lineage", {})
    for event in state.get("events", []):
        if event.get("kind") == "task_begun" and event.get("task") and event.get("owner_direction"):
            directions.setdefault(event["task"], contract.normalize_owner_direction(event["owner_direction"]))
        if event.get("kind") == "task_resumed" and event.get("task") and event.get("source_agent_id") and event.get("agent_id"):
            edge = {"source_agent_id": event["source_agent_id"], "agent_id": event["agent_id"], "at": event.get("at", "")}
            if edge not in lineage.setdefault(event["task"], []):
                lineage[event["task"]].append(edge)
        if event.get("kind") == "task_cancelled" and event.get("task"):
            state.setdefault("cancelled_tasks", {}).setdefault(event["task"], {
                "cancelled_at": event.get("at", ""),
                "reason": event.get("message", "owner intentionally stopped the Delivery task"),
            })
    accepted = {
        task for task, decision in state.get("release_decisions", {}).items()
        if decision.get("decision") == "accepted"
    }
    archived_requests = [
        entry.get("value", {}) for entry in state.get("archive", [])
        if entry.get("kind") == "qa_request" and isinstance(entry.get("value"), dict)
    ]
    archived_requests.extend(
        request for request in state.get("qa_requests", {}).values()
        if request.get("status") in TERMINAL_QA and request.get("task") in accepted
    )
    _append_cold(root, "qa_requests", archived_requests)
    index = state.setdefault("qa_request_index", {})
    index_fields = {
        "id", "task", "cycle", "stage", "phase", "subtask", "chunk",
        "status", "result", "developer_id", "requested_at", "completed_at",
        "review_wait_started_at", "review_wait_stopped_at", "reviewed_commit",
        "reviewed_tree_hash", "reviewed_files", "reviewed_base_commit",
        "mirror_ref", "mirror_commit", "mirror_tree_hash", "mirror_transaction_id",
        "structure_revision",
    }
    for request in archived_requests:
        if request.get("id"):
            index[request["id"]] = {key: value for key, value in request.items() if key in index_fields}
            state.get("qa_requests", {}).pop(request["id"], None)
    state["archive"] = []

    inactive = [
        agent for agent in state.get("agents", {}).values()
        if not agent.get("active") and agent.get("status") != "paused" and (
            agent.get("role") not in DEVELOPER_ROLES
            or agent.get("task") in accepted
            or (
                agent.get("task") == AWAITING_OWNER_DIRECTION
                and not (
                    state.get("owner_directions", {}).get(str(agent.get("session_id", "")), {}).get("text")
                    and not state.get("owner_directions", {}).get(str(agent.get("session_id", "")), {}).get("consumed")
                    and not state.get("owner_directions", {}).get(str(agent.get("session_id", "")), {}).get("transferred_to_session_id")
                )
            )
        )
    ]
    _append_cold(root, "agents", inactive)
    for agent in inactive:
        state["agents"].pop(agent.get("id", ""), None)


def _render_board(state: dict[str, Any]) -> str:
    """Render the live board humans can read while watching the CLI agents."""
    lines = ["# Live Harness Board", "", f"Updated: {now()}", "", "## Agents", "", "| ID | Role | Task | Poll | Status | Last update |", "|---|---|---|---:|---|---|"]
    for agent in sorted(state.get("agents", {}).values(), key=lambda value: value["id"]):
        lines.append("| {id} | {role} | {task} | {poll_counter} | {status}: {status_note} | {last_status_at} |".format(**agent))
    lines += ["", "## Delivery chunks", "", "| Task | Chunk | Status | Description |", "|---|---|---|---|"]
    for task, chunks in sorted(state.get("task_chunks", {}).items()):
        for chunk, value in sorted(chunks.items()):
            lines.append(f"| {task} | {chunk} | {value['status']} | {value['description']} |")
    lines += ["", "## Product plans", "", "| Objective | Structure | Subtask | Acceptance | Pipeline | Dependencies | Ownership |", "|---|---|---|---|---|---|---|"]
    for task, plan in sorted(state.get("delivery_plans", {}).items()):
        subtasks = plan.get("subtasks", {})
        if not subtasks:
            lines.append(f"| {task} | {plan.get('mode', '')} | — | — | — | — | — |")
        for name, subtask in sorted(subtasks.items()):
            path_scope = ", ".join(subtask.get("owned_paths", []))
            surface_scope = ", ".join(subtask.get("owned_surfaces", []))
            ownership = "; ".join(
                value for value in (
                    f"paths: {path_scope}" if path_scope else "",
                    f"surfaces: {surface_scope}" if surface_scope else "",
                ) if value
            ) or "—"
            lines.append(f"| {task} | {plan.get('mode', '')} | {name}: {subtask.get('title', '')} | {subtask.get('status', '')} | {subtask.get('pipeline_status', 'pending')} | {', '.join(subtask.get('dependencies', [])) or '—'} | {ownership} |")
    lines += ["", "## Owner messages", "", "| Type | Task | Agent | Attachments | Time | Text |", "|---|---|---|---:|---|---|"]
    for message in state.get("owner_messages", []):
        message_text = str(message.get("text", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {message.get('type', '')} | {message.get('task', '') or 'new direction'} | {message.get('agent_id', '')} | {len(message.get('attachments', []))} | {message.get('created_at', '')} | {message_text} |")
    lines += ["", "## Review queue", "", "| Request | Task | Phase | Subtask | Chunk | Cycle | Status | Developer | Reviewer | Wait started |", "|---|---|---|---|---|---:|---|---|---|---|"]
    requests = list(state.get("qa_requests", {}).values())
    for request in sorted(requests, key=lambda value: value["requested_at"]):
        row = {"phase": request.get("phase", "legacy"), "subtask": request.get("subtask", ""), "chunk": request.get("chunk", ""), **request}
        lines.append("| {id} | {task} | {phase} | {subtask} | {chunk} | {cycle} | {status} | {developer_id} | {claimed_by} | {review_wait_started_at} |".format(**row))
    lines += ["", "## CTO release gates", "", "| Task | Status | CTO | Recorded |", "|---|---|---|---|"]
    for task, release in sorted(state.get("releases", {}).items()):
        lines.append(f"| {task} | {release['status']} | {release['cto_id']} | {release['recorded_at']} |")
    lines += ["", "## Owner release responses", "", "| Task | Response | Recorded |", "|---|---|---|"]
    for task, response in sorted(state.get("release_decisions", {}).items()):
        lines.append(f"| {task} | {response.get('decision', '')} | {response.get('recorded_at', '')} |")
    lines += ["", "## Owner repair routes", "", "| Task | Status | Next action |", "|---|---|---|"]
    for task, repair in sorted(state.get("release_repairs", {}).items()):
        lines.append(f"| {task} | {repair.get('status', '')} | {repair.get('next_action', '')} |")
    lines += ["", "## Findings outside the current task", "", "| ID | Found while working on | Classification | Status | Next action |", "|---|---|---|---|---|"]
    for finding in sorted(state.get("deferred_findings", {}).values(), key=lambda value: value.get("created_at", "")):
        lines.append("| {id} | {task} | {classification} | {status} | {next_action} |".format(**finding))
    lines += ["", "## Recent events", ""]
    for event in state.get("events", [])[-20:]:
        lines.append(f"- {event['at']} | {event['kind']} | {event['agent_id']} ({event['role']}) | {event.get('task', '')} | {event.get('message', event.get('result', ''))}")
    return "\n".join(lines) + "\n"


def _persist_visible_board(root: Path, state: dict[str, Any]) -> None:
    directory = board_dir(root)
    (directory / "BOARD.md").write_text(_render_board(state), encoding="utf-8")
    last_exported = int(state.get("last_event_exported", 0))
    new_events = [event for event in state.get("events", []) if int(event["sequence"]) > last_exported]
    if new_events:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            for event in new_events:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
        state["last_event_exported"] = new_events[-1]["sequence"]
    _extract_cold_state(root, state)
    hot = state.get("events", [])[-HOT_EVENT_WINDOW:]
    while len(hot) > 1 and len(json.dumps(hot, separators=(",", ":"))) > HOT_EVENT_BYTES:
        hot = hot[1:]
    state["events"] = hot
    state["event_window_start"] = state["events"][0]["sequence"] if state.get("events") else state.get("next_event", 1)
    _snapshot_board(root, state)


@contextmanager
def locked_state(
    root: Path, *, allow_paused: bool = False, operation: str = "board mutation",
    resume_session_id: str = "",
) -> Iterator[dict[str, Any]]:
    """Lock, load, mutate, and atomically persist board state."""
    directory = board_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".lock"
    memory_state: dict[str, Any] | None = None
    memory_events: list[dict[str, Any]] = []
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        path = _state_path(root)
        try:
            state = _load_or_recover_state(root, allow_restore=True)
            pause = state.setdefault("project_pause", {"status": "active"})
            saved_resume_session = bool(
                pause.get("status") == "resuming"
                and resume_session_id
                and any(
                    saved.get("session_id") == resume_session_id
                    for saved in pause.get("agents", {}).values()
                )
            )
            if (
                pause.get("status") in {"paused", "resuming"}
                and not allow_paused and not saved_resume_session
            ):
                _event(state, "project_paused_write_refused", None, {
                    "task": "",
                    "pause_id": pause.get("pause_id", ""),
                    "operation": str(operation)[:120],
                    "message": (
                        f"Late {str(operation)[:120]} refused because the project "
                        "is paused; no requested board state changed"
                    ),
                })
                _persist_visible_board(root, state)
                temp = path.with_suffix(".tmp")
                temp.write_text(
                    json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                os.replace(temp, path)
                raise ProjectPausedError(
                    "project is paused; resume it before writing to the board"
                )
            first_new_sequence = int(state.get("next_event", 1))
            before = json.dumps(state, sort_keys=True, separators=(",", ":"))
            yield state
            memory_events = [
                dict(event) for event in state.get("events", [])
                if int(event.get("sequence", 0)) >= first_new_sequence
                and event.get("kind") not in project_memory.NON_MATERIAL_EVENTS
            ]
            serialized = json.dumps(state, sort_keys=True, separators=(",", ":"))
            # P9: a locked read that mutated nothing writes nothing — UNLESS
            # persistence-side maintenance is owed (durable export, cold
            # split) or the on-disk state differs from what was loaded.
            maintenance_due = (
                any(int(e["sequence"]) > int(state.get("last_event_exported", 0))
                    for e in state.get("events", []))
                or len(state.get("events", [])) > HOT_EVENT_WINDOW
                or not (board_dir(root) / "BOARD.md").is_file()
            )
            disk_matches = False
            if serialized == before and not maintenance_due and path.exists():
                try:
                    disk_matches = path.read_text(encoding="utf-8").strip() == serialized
                except OSError:
                    disk_matches = False
            if not disk_matches:
                _persist_visible_board(root, state)
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
                os.replace(temp, path)
            memory_state = state
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    if memory_state is not None and memory_events:
        try:
            project_memory.sync_board_state(root, memory_state, memory_events)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            # Memory is derived narrative.  Its failure is loud and retryable,
            # but can never roll back or contradict the board commit above.
            project_memory.log_sync_failure(root, error)


def _read_state(root: Path) -> dict[str, Any]:
    """Read a consistent hot snapshot without taking the writer lock."""
    directory = board_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            path = _state_path(root)
            return _load_or_recover_state(root, allow_restore=False)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def historical_snapshot(root: Path) -> dict[str, Any]:
    """Load cold history only for explicit history/reconstruction requests."""
    state = _read_state(root)
    state["archive"] = [
        {"kind": "qa_request", "archived_at": value.get("completed_at", ""), "value": value}
        for value in _read_cold(root, "qa_requests")
    ]
    for agent in _read_cold(root, "agents"):
        state.setdefault("agents", {}).setdefault(agent.get("id", ""), agent)
    event_path = board_dir(root) / "events.jsonl"
    if event_path.is_file():
        events = []
        for line in event_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        state["events"] = events
    state["critical_path_summaries"] = lifecycle.summaries(state)
    return state


def _event(state: dict[str, Any], kind: str, agent: dict[str, Any] | None, payload: dict[str, Any]) -> dict[str, Any]:
    sequence = state["next_event"]
    state["next_event"] += 1
    event = {
        "sequence": sequence,
        "at": now(),
        "kind": kind,
        "agent_id": agent["id"] if agent else "system",
        "role": agent["role"] if agent else "system",
        **payload,
    }
    state["events"].append(event)
    if agent is not None and kind in PROGRESS_EVENTS:
        agent["last_progress_at"] = event["at"]
        if agent.get("liveness") in {"stalled", "recovering"}:
            agent.update({"liveness": "healthy", "liveness_note": "board-recorded progress is current"})
            # Progress after an automatic wake-up means recovery succeeded: clear the
            # automatic-recovery bookkeeping (as poll() does) so a FUTURE stall is
            # handled fresh instead of being skipped with a stale request timestamp.
            # Owner-initiated reset_requested is left untouched — it clears only on poll.
            if agent.get("recovery_state") in {"automatic_requested", "automatic_failed"}:
                agent["recovery_state"] = "resumed"
                agent.pop("automatic_recovery_requested_at", None)
    return event


def pause_state(root: ProjectRoot) -> dict[str, Any]:
    """Return the durable project pause record without mutating the board."""
    value = snapshot(root).get("project_pause", {"status": "active"})
    return json.loads(json.dumps(value))


def begin_project_pause(root: ProjectRoot, drain_seconds: float = 1.0) -> dict[str, Any]:
    """Record quiesce intent and a bounded deadline before late writes close."""
    seconds = max(0.0, min(float(drain_seconds), 30.0))
    with locked_state(root, allow_paused=True, operation="begin project pause") as state:
        current = state.setdefault("project_pause", {"status": "active"})
        if current.get("status") == "resuming":
            raise ValueError("project resume is in progress; wait for reconciliation to finish")
        if current.get("status") in {"draining", "paused"}:
            _event(state, "project_pause_reused", None, {
                "task": "", "pause_id": current.get("pause_id", ""),
                "status": current.get("status", ""),
                "message": f"Pause request reused existing {current.get('status')} state",
            })
            return json.loads(json.dumps(current))
        requested_at = datetime.now(timezone.utc)
        pause_id = secrets.token_hex(8)
        current = {
            "status": "draining",
            "pause_id": pause_id,
            "requested_at": requested_at.isoformat(),
            "drain_seconds": seconds,
            "drain_deadline": (requested_at + timedelta(seconds=seconds)).isoformat(),
            "paused_at": None,
            "agents": {},
            "reviews": {},
        }
        state["project_pause"] = current
        _event(state, "project_pause_requested", None, {
            "task": "", "pause_id": pause_id, "drain_seconds": seconds,
            "message": f"Project pause signalled with a bounded {seconds:g}-second drain window",
        })
        return json.loads(json.dumps(current))


def finish_project_pause(root: ProjectRoot) -> dict[str, Any]:
    """Close the write gate while preserving every unfinished owner and pointer."""
    with locked_state(root, allow_paused=True, operation="finish project pause") as state:
        pause = state.setdefault("project_pause", {"status": "active"})
        if pause.get("status") == "paused":
            _event(state, "project_pause_reused", None, {
                "task": "", "pause_id": pause.get("pause_id", ""),
                "status": "paused", "message": "Project is already paused; no state was reset",
            })
            return json.loads(json.dumps(pause))
        if pause.get("status") != "draining":
            raise ValueError("project pause must begin with a drain window")

        saved_agents: dict[str, Any] = {}
        for agent_id, agent in state.get("agents", {}).items():
            if not agent.get("active"):
                continue
            saved_agents[agent_id] = {
                "session_id": agent.get("session_id", ""),
                "task": agent.get("task", ""),
                "status": agent.get("status", ""),
                "status_note": agent.get("status_note", ""),
                "poll_counter": agent.get("poll_counter", 0),
                "write_authority": agent.get("write_authority", True),
                "next_action": agent.get("status_note", "") or "Poll the board and continue the saved gate.",
            }
            agent.update({
                "active": False,
                "write_authority": False,
                "status": "paused",
                "status_note": "project intentionally paused; saved next action will resume",
                "liveness": "paused",
                "liveness_note": "intentional project pause",
                "last_status_at": now(),
            })

        saved_reviews: dict[str, Any] = {}
        for request_id, request in state.get("qa_requests", {}).items():
            if request.get("status") not in {"reserved", "claimed"}:
                continue
            saved_reviews[request_id] = {
                "status": request.get("status"),
                "reserved_by": request.get("reserved_by"),
                "claimed_by": request.get("claimed_by"),
                "challenge_ledger": request.get("challenge_ledger"),
                "route_state": request.get("route_state"),
            }
            request["paused_from_status"] = request["status"]
            request["status"] = "suspended"
            request["suspended_at"] = now()
            request["route_state"] = "suspended_with_owner"

        pause.update({
            "status": "paused",
            "paused_at": now(),
            "agents": saved_agents,
            "reviews": saved_reviews,
        })
        _event(state, "project_paused", None, {
            "task": "", "pause_id": pause.get("pause_id", ""),
            "agent_count": len(saved_agents), "review_count": len(saved_reviews),
            "message": (
                f"Project paused non-destructively; preserved {len(saved_agents)} "
                f"agent pointers and {len(saved_reviews)} in-flight review owners"
            ),
        })
        return json.loads(json.dumps(pause))


def begin_project_resume(root: ProjectRoot) -> dict[str, Any]:
    """Start or recover one durable resume transaction without opening writes."""
    with locked_state(root, allow_paused=True, operation="begin project resume") as state:
        pause = state.setdefault("project_pause", {"status": "active"})
        if pause.get("status") == "active":
            completed = pause.get("last_resume")
            if not isinstance(completed, dict) or not completed.get("resume_id"):
                raise ValueError("project is not paused")
            _event(state, "project_resume_reused", None, {
                "task": "", "resume_id": completed["resume_id"],
                "message": "Completed project resume reused without repeating work",
            })
            return json.loads(json.dumps(pause))
        if pause.get("status") == "resuming":
            _event(state, "project_resume_reused", None, {
                "task": "", "resume_id": pause.get("resume_id", ""),
                "message": "Interrupted project resume recovered from its saved stage",
            })
            return json.loads(json.dumps(pause))
        if pause.get("status") != "paused":
            raise ValueError("project must finish pausing before it can resume")
        pause.update({
            "status": "resuming",
            "resume_id": secrets.token_hex(8),
            "resume_requested_at": now(),
            "resume_stage": "reconciling_board_authority",
            "resume_checkpoints": {},
        })
        _event(state, "project_resume_requested", None, {
            "task": "", "pause_id": pause.get("pause_id", ""),
            "resume_id": pause["resume_id"],
            "message": "Project resume started; board remains read-only during reconciliation",
        })
        return json.loads(json.dumps(pause))


def record_project_resume_checkpoint(
    root: ProjectRoot, resume_id: str, checkpoint: str, details: dict[str, Any],
) -> dict[str, Any]:
    """Persist an idempotent resume stage so process death cannot reset progress."""
    checkpoint = str(checkpoint).strip()
    if not checkpoint:
        raise ValueError("resume checkpoint is required")
    with locked_state(root, allow_paused=True, operation="record project resume checkpoint") as state:
        pause = state.setdefault("project_pause", {"status": "active"})
        if pause.get("status") != "resuming" or pause.get("resume_id") != resume_id:
            raise ValueError("resume checkpoint does not match the active transaction")
        value = {
            "checkpoint": checkpoint,
            "recorded_at": now(),
            "details": json.loads(json.dumps(details)),
        }
        pause.setdefault("resume_checkpoints", {})[checkpoint] = value
        pause["resume_stage"] = checkpoint
        _event(state, "project_resume_checkpoint", None, {
            "task": "", "resume_id": resume_id, "checkpoint": checkpoint,
            "message": str(details.get("message", f"Resume checkpoint recorded: {checkpoint}"))[:500],
        })
        return json.loads(json.dumps(value))


def stage_required_delivery_resumes(
    root: ProjectRoot, resume_id: str,
) -> dict[str, Any]:
    """Preserve Delivery ownership when reconciliation leaves actionable work.

    A Delivery agent can be inactive legitimately after final acceptance.  A
    resumed project must not revive it merely because the task has not yet been
    released.  It must, however, restore Delivery when the newest review was
    reopened/failed, an in-scope repair exists, or development never reached
    its completion gate.
    """
    staged: list[dict[str, Any]] = []
    with locked_state(
        root, allow_paused=True, operation="stage required delivery resumes",
    ) as state:
        pause = state.setdefault("project_pause", {"status": "active"})
        if pause.get("status") != "resuming" or pause.get("resume_id") != resume_id:
            raise ValueError("delivery resume staging does not match the active transaction")

        cancelled = set((state.get("cancelled_tasks") or {}).keys())
        accepted = {
            str(task) for task, decision in (state.get("release_decisions") or {}).items()
            if decision.get("decision") == "accepted"
        }
        unfinished = {
            str(task) for task in (state.get("task_owner_directions") or {})
            if str(task) not in cancelled and str(task) not in accepted
        }
        development_complete = {
            str(task) for task, record in (state.get("release_lifecycle") or {}).items()
            if record.get("development_completed_at")
        }
        # Legacy boards may predate the durable lifecycle record. The event is
        # a compatibility fallback only; current decisions use lifecycle state.
        development_complete.update(
            str(event.get("task") or "") for event in state.get("events", [])
            if event.get("kind") == "development_complete"
        )
        required_tasks = {
            task for task in unfinished if task not in development_complete
        }

        latest_by_scope: dict[tuple[str, str, str, str, int], dict[str, Any]] = {}
        for request in (state.get("qa_requests") or {}).values():
            task = str(request.get("task") or "")
            if task not in unfinished:
                continue
            key = (
                task, str(request.get("phase") or "legacy"),
                str(request.get("subtask") or ""), str(request.get("chunk") or ""),
                int(request.get("structure_revision", 0)),
            )
            previous = latest_by_scope.get(key)
            if previous is None or int(request.get("cycle", 0)) > int(previous.get("cycle", 0)):
                latest_by_scope[key] = request
        for request in latest_by_scope.values():
            # An open or active review is Reviewer work. Reviving an inactive
            # Delivery process for it wastes a terminal and does not advance the
            # gate. A failed review, however, requires Delivery repair.
            if request.get("status") == "failed":
                required_tasks.add(str(request.get("task") or ""))

        required_tasks.update(
            str(task) for task, repair in (state.get("release_repairs") or {}).items()
            if task in unfinished and repair.get("status") in {
                "OWNER_REJECTED_REPAIR_REQUIRED", "DELIVERY_REPAIR_IN_PROGRESS",
            }
        )
        required_tasks.update(
            str(finding.get("task") or "")
            for finding in (state.get("deferred_findings") or {}).values()
            if finding.get("status") == "in_scope"
            and str(finding.get("task") or "") in unfinished
        )

        saved_agents = pause.setdefault("agents", {})
        for task in sorted(required_tasks):
            if any(
                saved.get("task") == task
                and state.get("agents", {}).get(agent_id, {}).get("role") in DEVELOPER_ROLES
                for agent_id, saved in saved_agents.items()
            ):
                continue
            candidates = [
                agent for agent in state.get("agents", {}).values()
                if agent.get("role") in DEVELOPER_ROLES
                and agent.get("task") == task
                and not agent.get("superseded_by_agent_id")
                and agent.get("status") not in {"superseded", "cancelled"}
            ]
            if not candidates:
                continue
            source = max(
                candidates,
                key=lambda agent: (
                    bool(agent.get("active")), str(agent.get("spawned_at") or ""),
                    str(agent.get("id") or ""),
                ),
            )
            next_action = (
                (state.get("task_briefs") or {}).get(task, {}).get("update")
                or source.get("status_note")
                or "Poll the board once and continue the saved Delivery gate."
            )
            saved_agents[source["id"]] = {
                "session_id": str(source.get("session_id") or ""),
                "task": task,
                "status": str(source.get("status") or "working"),
                "status_note": str(next_action),
                "poll_counter": int(source.get("poll_counter", 0)),
                "write_authority": source.get("write_authority", True) is not False,
                "next_action": str(next_action),
            }
            event = _event(state, "project_resume_delivery_staged", source, {
                "task": task, "resume_id": resume_id,
                "message": (
                    "Resume restored the saved Delivery owner because unfinished "
                    "work requires a Delivery action"
                ),
            })
            staged.append({
                "agent_id": source["id"], "task": task,
                "session_id": str(source.get("session_id") or ""),
                "event": event,
            })
    return {"resume_id": resume_id, "staged": staged}


def replace_project_resume_session(
    root: ProjectRoot, resume_id: str, agent_id: str, session_id: str,
) -> dict[str, Any]:
    """Bind a replacement transport to one staged agent before writes reopen."""
    session_id = str(session_id or "").strip()
    if not session_id:
        raise ValueError("replacement resume session is required")
    with locked_state(
        root, allow_paused=True, operation="replace project resume session",
    ) as state:
        pause = state.setdefault("project_pause", {"status": "active"})
        if pause.get("status") != "resuming" or pause.get("resume_id") != resume_id:
            raise ValueError("resume session replacement does not match the active transaction")
        saved = pause.setdefault("agents", {}).get(agent_id)
        agent = state.get("agents", {}).get(agent_id)
        if not saved or not agent:
            raise ValueError("resume session replacement requires a staged agent")
        old_session_id = str(saved.get("session_id") or agent.get("session_id") or "")
        saved["session_id"] = session_id
        agent["session_id"] = session_id
        event = _event(state, "project_resume_session_replaced", agent, {
            "task": saved.get("task", agent.get("task", "")),
            "resume_id": resume_id,
            "old_session_id": old_session_id,
            "session_id": session_id,
            "message": "Missing terminal transport was recreated without changing board ownership",
        })
        return {"agent_id": agent_id, "session_id": session_id, "event": event}


def finish_project_resume(root: ProjectRoot, resume_id: str) -> dict[str, Any]:
    """Restore saved owners and pointers, then atomically reopen board writes."""
    with locked_state(root, allow_paused=True, operation="finish project resume") as state:
        pause = state.setdefault("project_pause", {"status": "active"})
        if pause.get("status") == "active":
            completed = pause.get("last_resume")
            if isinstance(completed, dict) and completed.get("resume_id") == resume_id:
                _event(state, "project_resume_reused", None, {
                    "task": "", "resume_id": resume_id,
                    "message": "Project resume already completed; no state was repeated",
                })
                return json.loads(json.dumps(completed))
            raise ValueError("project resume is not active")
        if pause.get("status") != "resuming" or pause.get("resume_id") != resume_id:
            raise ValueError("resume completion does not match the active transaction")

        restored_agents = 0
        for agent_id, saved in pause.get("agents", {}).items():
            agent = state.get("agents", {}).get(agent_id)
            if not agent:
                continue
            agent.update({
                "active": True,
                "write_authority": saved.get("write_authority", True),
                "status": saved.get("status", "working"),
                "status_note": saved.get("status_note", saved.get("next_action", "")),
                "liveness": "healthy",
                "liveness_note": "project resumed at the saved next action",
                "last_status_at": now(),
            })
            restored_agents += 1

        restored_reviews = 0
        for request_id, saved in pause.get("reviews", {}).items():
            request = state.get("qa_requests", {}).get(request_id)
            if not request or request.get("status") != "suspended":
                continue
            request.update({
                "status": request.get("paused_from_status") or saved.get("status", "claimed"),
                "reserved_by": saved.get("reserved_by"),
                "claimed_by": saved.get("claimed_by"),
                "challenge_ledger": saved.get("challenge_ledger"),
                "route_state": saved.get("route_state"),
                "resumed_at": now(),
            })
            request.pop("suspended_at", None)
            restored_reviews += 1

        completed = {
            "resume_id": resume_id,
            "pause_id": pause.get("pause_id", ""),
            "completed_at": now(),
            "restored_agents": restored_agents,
            "restored_reviews": restored_reviews,
            "checkpoints": json.loads(json.dumps(pause.get("resume_checkpoints", {}))),
        }
        pause.update({
            "status": "active", "resumed_at": completed["completed_at"],
            "resume_stage": "complete", "last_resume": completed,
        })
        _event(state, "project_resumed", None, {
            "task": "", "pause_id": pause.get("pause_id", ""),
            "resume_id": resume_id, "agent_count": restored_agents,
            "review_count": restored_reviews,
            "message": (
                f"Project resumed at saved gates; restored {restored_agents} agents "
                f"and {restored_reviews} in-flight review owners"
            ),
        })
        return json.loads(json.dumps(completed))


def _require_agent(state: dict[str, Any], agent_id: str) -> dict[str, Any]:
    agent = state["agents"].get(agent_id)
    if not agent:
        raise ValueError(f"unknown agent: {agent_id}")
    return agent


def _require_writable_agent(state: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Return an agent only while it still owns authority to mutate work."""
    agent = _require_agent(state, agent_id)
    if agent.get("write_authority") is False or agent.get("superseded_by_agent_id"):
        replacement = agent.get("superseded_by_agent_id") or "the replacement Delivery Agent"
        raise ValueError(f"superseded Delivery Agent is read-only; current owner is {replacement}")
    return agent


def _finding_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"finding {label} is required")
    if len(text) > MAX_FINDING_TEXT_LENGTH:
        raise ValueError(f"finding {label} must be {MAX_FINDING_TEXT_LENGTH} characters or fewer")
    return text


def _finding_fingerprint(title: str, description: str) -> str:
    normalized = " ".join(f"{title} {description}".casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_FINDING_STOPWORDS = frozenset(
    "the a an and or of to in on for with its it is are was be can that this "
    "not no never every ever still one has have had from by as at so we into "
    "their them they when while after before which whose what who how".split()
)
# Measured on real board data: a reworded duplicate scored 0.42 while the
# closest genuinely-distinct pair scored 0.32 — too narrow for an automatic
# merge. The score therefore only nominates candidates for CTO judgment.
FINDING_REPEAT_CANDIDATE_SCORE = 0.2
FINDING_REPEAT_CANDIDATE_LIMIT = 3


def _finding_tokens(title: str, description: str) -> set[str]:
    words = re.findall(r"[a-z0-9_]+", f"{title} {description}".casefold())
    return {w for w in words if len(w) > 2 and w not in _FINDING_STOPWORDS}


def _finding_repeat_candidates(
    state: dict[str, Any], title: str, description: str,
) -> list[dict[str, Any]]:
    """Nominate earlier findings the new one may repeat, for CTO triage.

    Resolved findings are deliberately excluded: a recurrence of a fixed
    defect is a regression and must surface as a new finding, never be merged
    away into closed history.
    """
    tokens = _finding_tokens(title, description)
    if not tokens:
        return []
    scored = []
    for existing in (state.get("deferred_findings") or {}).values():
        if existing.get("status") in {"resolved", "merged"}:
            continue
        other = _finding_tokens(str(existing.get("title", "")), str(existing.get("description", "")))
        if not other:
            continue
        score = len(tokens & other) / min(len(tokens), len(other))
        if score >= FINDING_REPEAT_CANDIDATE_SCORE:
            scored.append((score, existing))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"id": existing.get("id", ""), "title": existing.get("title", ""),
         "status": existing.get("status", ""), "score": round(score, 3)}
        for score, existing in scored[:FINDING_REPEAT_CANDIDATE_LIMIT]
    ]


def record_finding(
    root: Path,
    task: str,
    title: str,
    description: str,
    affects_current_task: bool = False,
    evidence: str = "",
) -> dict[str, Any]:
    """Classify a newly discovered issue without silently changing task scope.

    Findings that affect the requested task are routed into the active work and
    must be repaired before completion. Unrelated findings are durable, deferred
    records; they never create work or block the current task until the owner
    explicitly chooses ``Fix``.
    """
    task = _finding_text(task, "task")
    title = _finding_text(title, "title")
    description = _finding_text(description, "description")
    evidence = str(evidence or "").strip()
    if len(evidence) > MAX_FINDING_TEXT_LENGTH:
        raise ValueError(f"finding evidence must be {MAX_FINDING_TEXT_LENGTH} characters or fewer")
    fingerprint = _finding_fingerprint(title, description)
    created_at = now()
    in_scope = bool(affects_current_task)
    # Unrelated findings are never born as decision requests. They start in
    # needs_triage, where the CTO judges whether they repeat an earlier
    # finding (whose decision then stands) before anything may enter the
    # decision queue. This is what stops the same defect, reworded, from
    # asking the owner again at every acceptance.
    finding = {
        "fingerprint": fingerprint,
        "task": task,
        "title": title,
        "description": description,
        "evidence": evidence,
        "classification": "impacts_current_task" if in_scope else "unrelated_to_current_task",
        "status": "in_scope" if in_scope else "needs_triage",
        "decision": None,
        "created_at": created_at,
        "decided_at": None,
        "next_action": (
            "Fix this before the current task can be completed, then re-test it."
            if in_scope
            else "CTO triage due: judge by behavior, not wording — repeat of an "
                 "earlier finding (its decision stands) or genuinely new."
        ),
    }
    with locked_state(root) as state:
        for existing in state.setdefault("deferred_findings", {}).values():
            existing_fingerprint = existing.get("fingerprint") or _finding_fingerprint(
                str(existing.get("title", "")), str(existing.get("description", "")),
            )
            if existing_fingerprint != fingerprint:
                continue
            existing["fingerprint"] = existing_fingerprint
            observed = existing.setdefault("observed_in_tasks", [existing.get("task", "")])
            if task not in observed:
                observed.append(task)
            # Identical evidence for an already decided finding is historical
            # repetition, not another owner decision card. A materially
            # different recurrence must be recorded with a description that
            # explains what changed.
            return dict(existing)
        finding_id = f"finding-{secrets.token_hex(8)}"
        finding["id"] = finding_id
        finding["observed_in_tasks"] = [task]
        if not in_scope:
            finding["repeat_candidates"] = _finding_repeat_candidates(state, title, description)
        state.setdefault("deferred_findings", {})[finding_id] = finding
        _event(state, "finding_classified", None, {
            "task": task,
            "finding_id": finding_id,
            "classification": finding["classification"],
            "status": finding["status"],
            "message": finding["next_action"],
        })
        return dict(finding)


def triage_finding(
    root: Path,
    finding_id: str,
    verdict: str,
    target_id: str = "",
    note: str = "",
    recommend: str = "",
    recommend_reason: str = "",
) -> dict[str, Any]:
    """CTO judgment on a needs_triage finding: repeat, new, or already cleared.

    ``repeat`` merges the observation into the earlier finding — whose existing
    decision stands, so a dismissed defect can never ask the owner again.
    ``distinct`` promotes the finding into the normal deferred queue, where it
    will be decided exactly once. ``cleared`` closes an observation that later
    board evidence proves was transient or false, without manufacturing an
    owner decision. A repeat of a *resolved* finding is refused: that is a
    regression and must be handled as a new finding.
    """
    finding_id = _finding_text(finding_id, "id")
    verdict = str(verdict or "").strip().lower()
    if verdict not in {"repeat", "distinct", "cleared"}:
        raise ValueError("triage verdict must be repeat, distinct, or cleared")
    note = str(note or "").strip()
    if len(note) > MAX_FINDING_TEXT_LENGTH:
        raise ValueError(f"triage note must be {MAX_FINDING_TEXT_LENGTH} characters or fewer")
    recommend = str(recommend or "").strip().lower().replace(" ", "_")
    if recommend and recommend not in {"fix", "do_not_fix"}:
        raise ValueError("triage recommendation must be fix or do_not_fix")
    recommend_reason = str(recommend_reason or "").strip()
    if len(recommend_reason) > MAX_FINDING_TEXT_LENGTH:
        raise ValueError(f"recommendation reason must be {MAX_FINDING_TEXT_LENGTH} characters or fewer")
    with locked_state(root) as state:
        findings = state.setdefault("deferred_findings", {})
        finding = findings.get(finding_id)
        if not finding:
            raise ValueError(f"unknown finding: {finding_id}")
        if finding.get("status") != "needs_triage":
            raise ValueError("only a needs_triage finding can be triaged")
        triaged_at = now()
        if verdict == "cleared":
            if not note:
                raise ValueError("cleared triage requires evidence explaining why the observation no longer exists")
            if target_id or recommend or recommend_reason:
                raise ValueError("cleared triage accepts only an evidence note")
            finding.update({
                "status": "cleared",
                "decision": "system_cleared",
                "triaged_at": triaged_at,
                "cleared_at": triaged_at,
                "clearance_evidence": note,
                "next_action": "Later board evidence disproved or superseded this observation; no owner decision or follow-up work is due.",
            })
            _event(state, "finding_triaged", None, {
                "task": finding.get("task", ""),
                "finding_id": finding_id,
                "verdict": "cleared",
                "message": f"Cleared without owner action because later board evidence disproved or superseded the observation. Evidence: {note}",
            })
            return dict(finding)
        if verdict == "repeat":
            target = findings.get(str(target_id or "").strip())
            if not target or target is finding:
                raise ValueError("repeat verdict requires --target: the id of the earlier finding it repeats")
            if target.get("status") in {"resolved", "merged"}:
                raise ValueError(
                    "refusing to merge into a closed finding: a recurrence of a "
                    "resolved defect is a regression — triage it as distinct"
                )
            observed = target.setdefault("observed_in_tasks", [target.get("task", "")])
            if finding.get("task") and finding["task"] not in observed:
                observed.append(finding["task"])
            target.setdefault("repeat_observations", []).append({
                "task": finding.get("task", ""),
                "title": finding.get("title", ""),
                "merged_finding_id": finding_id,
                "note": note,
                "at": triaged_at,
            })
            finding.update({
                "status": "merged",
                "merged_into": target.get("id", ""),
                "triaged_at": triaged_at,
                "next_action": "Merged as a repeat; the earlier finding's decision stands.",
            })
            _event(state, "finding_triaged", None, {
                "task": finding.get("task", ""),
                "finding_id": finding_id,
                "merged_into": target.get("id", ""),
                "message": f"Repeat of \"{str(target.get('title', ''))[:80]}\" — the earlier decision stands; nobody is asked again.",
            })
            return dict(finding)
        finding.update({
            "status": "deferred",
            "triaged_at": triaged_at,
            "next_action": "Genuinely new. Held for one Fix / Do-not-fix decision; it will never be asked twice.",
        })
        # The CTO already understands the finding at triage time, so it leaves
        # a ready-made disposition. The queue therefore needs no standing
        # watcher: whoever looks next — owner or proxy, whenever — accepts or
        # overrides a prepared recommendation instead of analyzing from zero.
        if recommend:
            finding["recommendation"] = {"decision": recommend, "reason": recommend_reason, "at": triaged_at}
        _event(state, "finding_triaged", None, {
            "task": finding.get("task", ""),
            "finding_id": finding_id,
            "message": "Triaged as genuinely new; entered the decision queue once."
                       + (f" Recommends {recommend.replace('_', ' ')}: {recommend_reason}" if recommend else "")
                       + (f" Note: {note}" if note else ""),
        })
        return dict(finding)


def list_findings(root: Path, include_resolved: bool = True) -> list[dict[str, Any]]:
    """Return findings in creation order for API and viewer consumers."""
    state = snapshot(root)
    values = list((state.get("deferred_findings") or {}).values())
    if not include_resolved:
        values = [value for value in values if value.get("status") == "deferred"]
    return sorted(values, key=lambda value: (value.get("created_at", ""), value.get("id", "")))


def record_finding_decision(root: Path, finding_id: str, decision: str) -> dict[str, Any]:
    """Record the owner's independent decision for one deferred finding."""
    finding_id = _finding_text(finding_id, "id")
    decision = str(decision or "").strip().lower().replace(" ", "_")
    if decision not in {"fix", "do_not_fix"}:
        raise ValueError("finding decision must be fix or do_not_fix")
    with locked_state(root) as state:
        finding = (state.setdefault("deferred_findings", {})).get(finding_id)
        if not finding:
            raise ValueError(f"unknown finding: {finding_id}")
        if finding.get("status") == "needs_triage":
            raise ValueError("this finding has not been triaged yet: CTO must rule repeat or distinct first")
        if finding.get("status") != "deferred":
            raise ValueError("this finding already has an owner decision")
        decided_at = now()
        queue_position = None
        if decision == "fix":
            queue_position = 1 + sum(
                1 for value in state["deferred_findings"].values()
                if value.get("status") in {"fix_requested", "fix_in_progress"}
            )
        finding.update({
            "decision": decision,
            "decided_at": decided_at,
            "queue_position": queue_position,
            "status": "fix_requested" if decision == "fix" else "dismissed",
            "next_action": (
                f"Queued as follow-up item {queue_position}; create it only after earlier approved follow-ups are handled."
                if decision == "fix"
                else "No work will be created for this finding unless the owner requests it again."
            ),
        })
        _event(state, "finding_decision_recorded", None, {
            "task": finding.get("task", ""),
            "finding_id": finding_id,
            "decision": decision,
            "message": finding["next_action"],
        })
        return dict(finding)


def dispatch_approved_finding(root: Path, agent_id: str) -> dict[str, Any]:
    """Route only the first approved follow-up to one waiting Delivery agent."""
    instruction = ""
    session_id = ""
    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        if agent.get("role") not in DEVELOPER_ROLES or not agent.get("active"):
            raise ValueError("an active Delivery Agent is required for approved follow-up work")
        if agent.get("task") != AWAITING_OWNER_DIRECTION:
            raise ValueError("approved follow-up work requires a Delivery Agent waiting for a task")
        if any(value.get("status") == "fix_in_progress" for value in state.get("deferred_findings", {}).values()):
            raise ValueError("another approved finding is already in progress")
        queued = sorted(
            (value for value in state.get("deferred_findings", {}).values() if value.get("status") == "fix_requested"),
            key=lambda value: (int(value.get("queue_position") or 0), value.get("decided_at", ""), value.get("id", "")),
        )
        if not queued:
            raise ValueError("no approved finding is waiting")
        finding = queued[0]
        session_id = str(agent.get("session_id") or "")
        if not session_id:
            raise ValueError("approved follow-up work requires a managed Delivery terminal")
        if state.setdefault("owner_directions", {}).get(session_id):
            raise ValueError("the waiting Delivery session already has an owner direction")
        instruction = (
            "OWNER-APPROVED FOLLOW-UP — treat this as the exact owner direction.\n\n"
            f"Fix: {finding['title']}\n\n"
            f"Problem: {finding['description']}"
        )
        if finding.get("evidence"):
            instruction += f"\n\nExisting evidence: {finding['evidence']}"
        instruction += (
            f"\n\nThis work came from approved finding {finding['id']}. Keep it as one separate task, "
            "test and independently review it normally, and do not pull later queued findings into its scope."
        )
        state["owner_directions"][session_id] = {
            "session_id": session_id,
            "text": instruction,
            "received_at": now(),
            "consumed": False,
        }
        finding.update({
            "status": "fix_in_progress",
            "dispatched_at": now(),
            "assigned_agent_id": agent_id,
            "next_action": "Delivery is defining and implementing this approved follow-up. Later queued findings remain paused.",
        })
        _event(state, "owner_direction_received", agent, {
            "task": agent["task"],
            "message": "owner-approved finding routed as the next separate Delivery task",
            "owner_direction": instruction,
            "finding_id": finding["id"],
        })
        event = _event(state, "finding_dispatched", agent, {
            "task": finding.get("task", ""),
            "finding_id": finding["id"],
            "message": finding["next_action"],
        })
        result = {"finding": dict(finding), "event": event}
    try:
        from harness import control
        control.enqueue_instruction(root, session_id, instruction, source="owner-approved-finding")
    except ValueError:
        # The board direction remains durable if the terminal closes during
        # dispatch; a replacement can recover it without owner re-entry.
        pass
    return result


def resolve_finding(root: Path, finding_id: str, evidence: str = "") -> dict[str, Any]:
    """Mark an approved or in-scope finding fixed after it has been re-tested."""
    finding_id = _finding_text(finding_id, "id")
    evidence = str(evidence or "").strip()
    if len(evidence) > MAX_FINDING_TEXT_LENGTH:
        raise ValueError(f"finding evidence must be {MAX_FINDING_TEXT_LENGTH} characters or fewer")
    with locked_state(root) as state:
        finding = (state.setdefault("deferred_findings", {})).get(finding_id)
        if not finding:
            raise ValueError(f"unknown finding: {finding_id}")
        if finding.get("status") not in {"in_scope", "fix_requested", "fix_in_progress"}:
            raise ValueError("only an in-scope or owner-approved finding can be resolved")
        finding.update({"status": "resolved", "resolved_at": now(), "resolution_evidence": evidence, "next_action": "Resolved and included in the current task evidence."})
        _event(state, "finding_resolved", None, {"task": finding.get("task", ""), "finding_id": finding_id, "message": finding["next_action"]})
        return dict(finding)


def _resolve_findings_certified_by_final_review(
    state: dict[str, Any], request: dict[str, Any], completed_at: str,
) -> list[str]:
    """Close only findings explicitly covered by an intact final review brief."""
    if not (
        request.get("stage") == INDEPENDENT_REVIEW
        and request.get("phase") == "final_acceptance"
        and request.get("status") == "passed"
    ):
        return []
    brief = request.get("review_brief")
    execution = request.get("challenge_execution") or {}
    evidence_sha256 = str((execution.get("bundle") or {}).get("evidence_sha256") or "")
    if not isinstance(brief, dict) or len(evidence_sha256) != 64:
        return []
    brief_sha256 = str(brief.get("sha256") or "")
    unsigned = {key: value for key, value in brief.items() if key != "sha256"}
    actual_sha256 = hashlib.sha256(json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if (
        brief_sha256 != actual_sha256
        or brief.get("task") != request.get("task")
        or brief.get("request_id") != request.get("id")
    ):
        return []
    covered = {
        (str(value.get("title") or ""), str(value.get("description") or ""))
        for value in (brief.get("risk_and_scope") or {}).get("unresolved_findings", [])
        if isinstance(value, dict)
    }
    resolved: list[str] = []
    for finding in state.get("deferred_findings", {}).values():
        if not (
            finding.get("task") == request.get("task")
            and finding.get("status") == "in_scope"
            and (str(finding.get("title") or ""), str(finding.get("description") or ""))
            in covered
        ):
            continue
        finding.update({
            "status": "resolved",
            "resolved_at": completed_at,
            "resolution_evidence": (
                f"Final independent review {request['id']} explicitly covered this "
                f"finding and passed with certified challenge evidence {evidence_sha256}."
            ),
            "resolution_source": {
                "request_id": request["id"],
                "review_brief_sha256": brief_sha256,
                "challenge_evidence_sha256": evidence_sha256,
            },
            "next_action": "Resolved by the certified final review; no separate Delivery action remains.",
        })
        resolved.append(str(finding.get("id") or ""))
        _event(state, "finding_resolved", None, {
            "task": request["task"], "finding_id": finding.get("id", ""),
            "request_id": request["id"],
            "message": finding["next_action"],
        })
    return resolved


def _review_execution_is_current(agent: dict[str, Any], stale_seconds: int = REVIEW_EXECUTION_STALE_SECONDS) -> bool:
    """Return true only for a recent, durable executable-review heartbeat."""
    execution = agent.get("review_execution")
    if not isinstance(execution, dict) or not execution.get("active"):
        return False
    heartbeat = execution.get("last_heartbeat_at")
    if not heartbeat:
        return False
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds()
    except (TypeError, ValueError):
        return False
    return age < stale_seconds


def _review_execution_is_running_or_settling(
    agent: dict[str, Any], stale_seconds: int = REVIEW_EXECUTION_STALE_SECONDS,
) -> bool:
    """Cover the short atomic handoff from command exit to evidence persistence."""
    if _review_execution_is_current(agent, stale_seconds):
        return True
    execution = agent.get("review_execution")
    if not isinstance(execution, dict) or execution.get("active"):
        return False
    finished_at = _parsed_timestamp(execution.get("finished_at"))
    return bool(
        finished_at is not None
        and (datetime.now(timezone.utc) - finished_at).total_seconds() < stale_seconds
    )


def _parsed_timestamp(value: Any) -> datetime | None:
    """Parse one persisted UTC timestamp without letting corrupt state kill a tick."""
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def review_execution_start(root: Path, agent_id: str, request_id: str, command: str) -> dict[str, Any]:
    """Persist evidence that a reviewer is executing a long board check."""
    request_id, command = request_id.strip(), command.strip()
    if not request_id or not command:
        raise ValueError("review execution requires a request ID and command")
    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        if not agent.get("active") or agent.get("status") == "offline" or agent.get("liveness") == "offline":
            raise ValueError("inactive/offline agent cannot start work execution")
        started = now()
        execution = {
            "active": True,
            "request_id": request_id,
            "command": command,
            "started_at": started,
            "last_heartbeat_at": started,
            "finished_at": None,
            "result": None,
        }
        agent["review_execution"] = execution
        agent.update({
            "liveness": "healthy",
            "liveness_note": "executable review heartbeat is current; board polling may be temporarily deferred",
        })
        return _event(state, "review_execution_started", agent, {
            "task": agent["task"], "request_id": request_id,
            "message": f"{agent['role']} is executing a long check; execution heartbeat is current",
        })


def review_execution_heartbeat(root: Path, agent_id: str, request_id: str) -> bool:
    """Refresh review execution evidence without pretending it is a board poll."""
    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        execution = agent.get("review_execution")
        if (
            not agent.get("active")
            or agent.get("status") == "offline"
            or agent.get("liveness") == "offline"
            or not isinstance(execution, dict)
            or not execution.get("active")
            or execution.get("request_id") != request_id
        ):
            return False
        execution["last_heartbeat_at"] = now()
        return True


def review_execution_finish(root: Path, agent_id: str, request_id: str, result: str = "completed") -> bool:
    """Close a review execution marker without changing board-poll liveness."""
    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        execution = agent.get("review_execution")
        if not isinstance(execution, dict) or execution.get("request_id") != request_id:
            return False
        finished_at = now()
        measured = lifecycle.phase(str(execution.get("started_at", "")), finished_at)
        execution.update({"active": False, "finished_at": finished_at, "result": result, "duration_seconds": measured.get("duration_seconds")})
        _event(state, "review_execution_finished", agent, {
            "task": agent["task"], "request_id": request_id,
            "lifecycle": measured,
            "message": f"executable review check {result}",
        })
        return True


def _reset_interrupted_repair_package(
    state: dict[str, Any], request: dict[str, Any], reason: str,
) -> None:
    package_id = str(request.get("repair_package_id") or "")
    package = state.get("repair_packages", {}).get(package_id)
    if not package or package.get("review_request_id") != request.get("id"):
        return
    resolved = bool(package.get("members")) and all(
        member.get("status") == "resolved" for member in package.get("members", [])
    )
    package.update({
        "status": "ready_for_review" if resolved else "open",
        "interrupted_review_request_id": request.get("id", ""),
        "interrupted_at": now(),
        "interruption_reason": reason[:500],
    })
    for field in ("review_request_id", "review_started_at", "settled_request_id", "settled_at"):
        package.pop(field, None)
    repair_package_model.refresh_digest(package)


def recover_interrupted_executions(root: ProjectRoot) -> list[dict[str, Any]]:
    """Atomically release work whose exact board execution lease expired.

    The Python project worker calls this on its bounded coordination tick. No
    agent poll is needed to discover or repair the stranded state.
    """
    current = datetime.now(timezone.utc)
    recovered: list[dict[str, Any]] = []
    try:
        from harness import control
        live_session_ids: set[str] | None = {
            str(item.get("id") or "") for item in control.snapshot(root).get("sessions", [])
            if item.get("status") in control.ACTIVE_STATUSES
        }
    except (OSError, ValueError):
        live_session_ids = None
    with locked_state(root) as state:
        for request_id, request in list(state.get("qa_requests", {}).items()):
            if (
                request.get("delivery_state") != "executing"
                or request.get("status") not in {"authoring", "open", "reserved"}
            ):
                continue
            developer = state.get("agents", {}).get(request.get("developer_id", ""), {})
            execution = developer.get("review_execution") or {}
            exact_lease = bool(
                execution.get("request_id") == request_id
                and _review_execution_is_running_or_settling(developer)
            )
            try:
                age = (current - datetime.fromisoformat(
                    str(request.get("requested_at") or "")
                )).total_seconds()
            except (TypeError, ValueError):
                age = REVIEW_EXECUTION_STALE_SECONDS
            if exact_lease or age < REVIEW_EXECUTION_STALE_SECONDS:
                continue
            reason = (
                "Delivery evidence execution was interrupted and its exact board "
                "heartbeat expired; the same scope must be rerun."
            )
            failed = json.loads(json.dumps(request))
            failed.update({
                "status": "cancelled", "delivery_state": "interrupted",
                "result": "failed", "result_summary": reason,
                "completed_at": now(),
                "route_state": "delivery_interrupted_recovered",
            })
            failures = state.setdefault("delivery_attempt_failures", [])
            failures.append(failed)
            del failures[:-200]
            _reset_interrupted_repair_package(state, request, reason)
            state["qa_requests"].pop(request_id, None)
            reviewer_id = str(request.get("reserved_by") or request.get("claimed_by") or "")
            reviewer = state.get("agents", {}).get(reviewer_id)
            if reviewer:
                reviewer.update({
                    "task": "REVIEW_QUEUE", "status": "waiting",
                    "status_note": "Interrupted Delivery evidence was cleared; waiting for a replacement request.",
                    "last_status_at": now(),
                })
            if developer:
                developer.update({
                    "status": "working",
                    "status_note": "Interrupted Delivery evidence was recovered; rerun the same review scope.",
                    "last_status_at": now(),
                })
            event = _event(state, "staged_review_interruption_recovered", developer or None, {
                "task": request.get("task", ""), "request_id": request_id,
                "wake_session": str(developer.get("session_id") or ""),
                "message": reason,
            })
            recovered.append(event)

        for request_id, request in list(state.get("qa_requests", {}).items()):
            if request.get("status") != "claimed" or request.get("challenge_execution"):
                continue
            if request.get("route_state") == "challenge_interrupted_retry_required":
                # Recovery already produced one explicit, auditable retry gate.
                # Only execute-challenge with a retry reason may advance it.
                continue
            reviewer = state.get("agents", {}).get(request.get("claimed_by", ""), {})
            execution = reviewer.get("review_execution") or {}
            execution_matches = execution.get("request_id") == request_id
            try:
                age = (current - datetime.fromisoformat(str(
                    (
                        execution.get("last_heartbeat_at")
                        if execution_matches else request.get("claimed_at")
                    ) or ""
                ))).total_seconds()
            except (TypeError, ValueError):
                age = REVIEW_EXECUTION_STALE_SECONDS
            exact_lease = bool(
                execution_matches
                and _review_execution_is_running_or_settling(reviewer)
            )
            if exact_lease or age < REVIEW_EXECUTION_STALE_SECONDS:
                continue
            if execution_matches:
                execution.update({
                    "active": False, "finished_at": now(), "result": "interrupted",
                })
            attempt = {
                "status": "interrupted", "recorded_at": now(),
                "ledger_sha256": request.get("challenge_ledger_sha256", ""),
                "cause": "system_interruption",
                "reason": "Reviewer execution heartbeat expired before certification completed.",
            }
            request.setdefault("challenge_execution_attempts", []).append(attempt)
            # A terminal that is merely dead or relaunching does not lose the
            # claim: the agent record is durable, the attached ledger's digest
            # anchors every certified scenario, and abandoning here forces a
            # re-authored ledger that invalidates all of them. Only a retired
            # reviewer (agent record inactive) releases the request.
            reviewer_live = bool(reviewer and reviewer.get("active"))
            if reviewer_live:
                request["route_state"] = "challenge_interrupted_retry_required"
                reviewer.update({
                    "status": "qa_testing",
                    "status_note": (
                        "Certified challenge execution was interrupted by a system event; "
                        "completed scenarios stay certified. Rerun execute-challenge to "
                        "resume the remainder."
                    ),
                    "last_status_at": now(),
                })
            else:
                request.setdefault("abandoned_challenge_ledgers", []).append({
                    "reviewer_id": request.get("claimed_by", ""),
                    "path": request.get("challenge_ledger", ""),
                    "sha256": request.get("challenge_ledger_sha256", ""),
                    "reason": attempt["reason"], "abandoned_at": now(),
                })
                _abandon_review_intents(
                    request, str(request.get("claimed_by") or ""), attempt["reason"],
                )
                request.update({
                    "status": "open", "claimed_by": None, "claimed_at": None,
                    "reserved_by": None, "reserved_at": None,
                    "challenge_ledger": None, "route_state": "interrupted_reopened",
                })
                for field in (
                    "challenge_ledger_sha256", "challenge_ledger_attached_at",
                    "challenge_execution_authorization",
                ):
                    request.pop(field, None)
            event = _event(state, "challenge_execution_interruption_recovered", reviewer or None, {
                "task": request.get("task", ""), "request_id": request_id,
                "wake_session": str(reviewer.get("session_id") or "") if reviewer_live else "",
                "reopened": not reviewer_live,
                "message": (
                    "interrupted Reviewer execution resumes on retry; completed certified scenarios are retained"
                    if reviewer_live else
                    "interrupted Reviewer execution was reopened for independent reassignment"
                ),
            })
            recovered.append(event)
    return recovered


@contextmanager
def review_execution_lease(root: Path, agent_id: str, request_id: str, command: str) -> Iterator[None]:
    """Keep durable review execution evidence alive while a check runs."""
    review_execution_start(root, agent_id, request_id, command)
    stop = threading.Event()

    def heartbeat_loop() -> None:
        while not stop.wait(REVIEW_EXECUTION_HEARTBEAT_SECONDS):
            try:
                if not review_execution_heartbeat(root, agent_id, request_id):
                    return
            except (OSError, ValueError):
                return

    thread = threading.Thread(target=heartbeat_loop, name=f"review-heartbeat-{request_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)
        review_execution_finish(root, agent_id, request_id)


def _require_contract_preflight(root: Path, task: str) -> None:
    valid, problems = contract.contract_preflight(root, task)
    if not valid:
        raise ValueError("delivery work requires a valid Completion Contract: " + "; ".join(problems))


def _next_broker_nonce(state: dict[str, Any], identity: str) -> int:
    """Allocate one worker-side monotonic nonce without trusting the caller."""
    nonces = state.setdefault("broker_nonces", {})
    value = int(nonces.get(identity, 0)) + 1
    nonces[identity] = value
    return value


def _broker_for_state(root: ProjectRoot, state: dict[str, Any], task: str) -> git_broker.GitBroker:
    """Build the trusted broker from board-derived project paths only."""
    control = project_context(root)
    repository_value = state.get("task_repositories", {}).get(task, "")
    repository = Path(repository_value).resolve() if repository_value else control.code_root
    context = git_broker.context_for_repository(control, repository)
    return git_broker.GitBroker(context, state_loader=lambda: state)


def _git_task_baseline(root: ProjectRoot, mode: str = "task_start") -> dict[str, Any]:
    """Capture inherited Git state so later work never becomes an owner choice."""
    root = project_context(root).code_root
    status = git_process.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root, capture_output=True, text=True,
    )
    if status.returncode != 0:
        return {"available": False, "mode": mode, "captured_at": now(), "requires_owner_action": False, "dirty_files": []}
    head = git_process.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    branch = git_process.run(["git", "branch", "--show-current"], cwd=root, capture_output=True, text=True)
    status_lines = [line for line in status.stdout.splitlines() if line]
    # Intake is attribution, not review: record paths and Git state without
    # opening unrelated untracked files that the owner explicitly excluded.
    # Candidate bytes are bound later by the reviewed commit or dirty-worktree
    # review digest.
    fingerprint = hashlib.sha256(status.stdout.encode("utf-8"))
    return {
        "available": True,
        "mode": mode,
        "captured_at": now(),
        "head": head.stdout.strip() if head.returncode == 0 else "",
        "branch": branch.stdout.strip() if branch.returncode == 0 else "",
        "dirty_files": [line[3:] for line in status_lines],
        "status_lines": status_lines,
        "fingerprint": fingerprint.hexdigest(),
        "requires_owner_action": False,
        "policy": "inherited changes are attributed, tested, and reviewed internally; they never require owner classification",
    }


def _git_review_artifact(root: ProjectRoot, baseline_commit: str = "") -> dict[str, Any]:
    """Capture exactly what an independent review examined.

    A dirty tree is never represented by its parent HEAD. Chunk reviews may
    bind themselves to the immutable digest below; final acceptance requires
    the clean-commit form.
    """
    root = project_context(root).code_root
    head = git_process.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    branch = git_process.run(["git", "branch", "--show-current"], cwd=root, capture_output=True, text=True)
    if head.returncode != 0:
        return {"available": False, "commit": "", "branch": "", "files": [], "working_tree_digest": "", "working_tree_files": [], "immutable_clean": False}
    status = git_process.run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root, capture_output=True, text=True)
    status_lines = [line for line in status.stdout.splitlines() if line] if status.returncode == 0 else []
    manifest_command = ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", head.stdout.strip()]
    if baseline_commit and baseline_commit != head.stdout.strip():
        manifest_command = ["git", "diff", "--name-only", f"{baseline_commit}..{head.stdout.strip()}"]
    files = git_process.run(
        manifest_command,
        cwd=root, capture_output=True, text=True,
    )
    committed_manifest = [path for path in files.stdout.splitlines() if path.strip()] if files.returncode == 0 else []
    dirty_paths = [line[3:].split(" -> ", 1)[-1] for line in status_lines if len(line) >= 4]
    # A committed candidate remains immutable even when a shared product
    # checkout contains unrelated inherited files. Review materializes HEAD in
    # a disposable archive; only dirt overlapping the candidate manifest can
    # change the artifact under review.
    committed_candidate = bool(
        status.returncode == 0
        and files.returncode == 0
        and baseline_commit
        and baseline_commit != head.stdout.strip()
        and not set(dirty_paths).intersection(committed_manifest)
    )
    clean = status.returncode == 0 and not status_lines
    immutable_candidate = clean or committed_candidate
    digest = hashlib.sha256(status.stdout.encode("utf-8"))
    if not immutable_candidate:
        diff = git_process.run(["git", "diff", "--binary", "HEAD"], cwd=root, capture_output=True)
        digest.update(b"\0" + diff.stdout)
        for line in status_lines:
            if not line.startswith("?? "):
                continue
            candidate = root / line[3:]
            if candidate.is_file():
                try:
                    payload = candidate.read_bytes()
                except OSError:
                    # Intake and shared-checkout diagnostics must never fail by
                    # opening an unrelated owner file. Governed reviews now
                    # require clean commits, so an unreadable dirty artifact
                    # is represented as non-certifiable metadata only.
                    payload = b"<unreadable-untracked-file>"
                digest.update(b"\0" + line[3:].encode("utf-8", errors="replace") + b"\0" + payload)
    if not immutable_candidate and status.returncode == 0:
        # A dirty review has no truthful commit manifest.  Bind the visible
        # scope to the exact candidate working tree instead of reporting the
        # unrelated files from its parent task commit or baseline.
        manifest_files = dirty_paths
    else:
        manifest_files = committed_manifest
    tree = git_process.run(
        ["git", "rev-parse", f"{head.stdout.strip()}^{{tree}}"],
        cwd=root, capture_output=True, text=True,
    )
    return {
        "available": files.returncode == 0,
        "commit": head.stdout.strip() if immutable_candidate and files.returncode == 0 else "",
        # Dirty chunk/subtask reviews still have a precise base commit/tree;
        # their additional bytes are independently bound by
        # ``working_tree_digest``.  Keeping the base tree lets resume enforce
        # the required commit + tree identities for both candidate modes.
        "tree_hash": tree.stdout.strip() if tree.returncode == 0 else "",
        "base_commit": head.stdout.strip(),
        "branch": branch.stdout.strip() if branch.returncode == 0 else "",
        "files": sorted(set(manifest_files)),
        "working_tree_digest": digest.hexdigest() if status.returncode == 0 else "",
        "working_tree_files": [line[3:].split(" -> ", 1)[-1] for line in status_lines if len(line) >= 4],
        "immutable_clean": immutable_candidate,
        "shared_worktree_dirty_files": dirty_paths,
        "captured_at": now(),
    }


def _git_commit_and_tree(repo: Path, revision: str) -> tuple[str, str]:
    """Resolve a commit and its tree without trusting caller-supplied hashes."""
    commit = git_process.run(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
        cwd=repo, capture_output=True, text=True,
    )
    if commit.returncode != 0 or not commit.stdout.strip():
        raise ValueError(f"Git commit could not be verified: {revision}")
    canonical = commit.stdout.strip()
    tree = git_process.run(
        ["git", "rev-parse", "--verify", f"{canonical}^{{tree}}"],
        cwd=repo, capture_output=True, text=True,
    )
    if tree.returncode != 0 or not tree.stdout.strip():
        raise ValueError(f"Git tree could not be verified for commit: {canonical}")
    return canonical, tree.stdout.strip()


def _initialize_project_repository(root: ProjectRoot) -> Path | None:
    """Turn a not-yet-versioned project folder into the task's Git repository.

    A scaffold project starts as an empty folder. Without this, begin-task
    finds no repository, records no broker workspace, and every later step
    is refused until someone runs the owner-only bind operation — the
    2026-08-21 overnight wedge. The harness owns its own zero-setup promise:
    initialize the folder and proceed down the normal broker path.
    """
    context = project_context(root)
    code_root = context.code_root
    if context.is_compatibility or not code_root.is_dir():
        # Legacy single-root setups keep their historical no-repository
        # behavior; only registered projects-layer scaffolds are initialized.
        return None
    initialized = git_process.run(
        ["git", "init", "-q", "-b", "main"], cwd=code_root,
        capture_output=True, text=True,
    )
    if initialized.returncode != 0:
        return None
    committed = git_process.run(
        ["git", "-c", "user.name=Harness", "-c", "user.email=harness@local.invalid",
         "commit", "--allow-empty", "-q", "-m",
         "Initialize project repository for governed delivery"],
        cwd=code_root, capture_output=True, text=True,
    )
    probe = git_process.run(
        ["git", "rev-parse", "HEAD"], cwd=code_root, capture_output=True, text=True,
    )
    if committed.returncode != 0 and probe.returncode != 0:
        return None
    return code_root.resolve() if probe.returncode == 0 else None


def _git_repository(value: Path) -> Path | None:
    """Return the canonical Git top level containing an existing path."""
    candidate = value.expanduser()
    if not candidate.exists():
        return None
    cwd = candidate if candidate.is_dir() else candidate.parent
    probe = git_process.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd, capture_output=True, text=True,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return None
    repository = Path(probe.stdout.strip()).resolve()
    return repository if repository.is_dir() else None


def _harness_source_root() -> Path:
    """Directory that contains this running harness package (…/dev_harness)."""
    return Path(__file__).resolve().parents[1]


def _is_harness_self_target(workspace: Path | None) -> bool:
    """True when a task's execution workspace is the harness's own repository.

    The board's bookkeeping may live in the harness repository while a task
    legitimately targets a different owner-named repository — that is safe and
    is the normal Mission Control workflow. The hazard is only when the code
    that would be edited belongs to the same working tree as the running
    control plane, because then governed delivery rewrites the harness while it
    is orchestrating work. Comparison is on the Git working-tree top level.

    Note: once the harness is installed as a pinned, non-Git copy, its source
    has no repository and this guard is naturally inert — which is the intended
    end state.
    """
    if workspace is None:
        return False
    harness_repo = _git_repository(_harness_source_root())
    if harness_repo is None:
        return False
    return harness_repo == _git_repository(workspace)


def _owner_named_repository(root: ProjectRoot, direction: str) -> Path | None:
    """Find one unambiguous external Git repository named by the owner.

    Mission Control starts agents from a broad workspace, while an owner can
    direct one task to a nested project. Binding that repository at task start
    keeps QA, independent review, and release evidence on the same artifact.
    Multiple repositories are deliberately treated as ambiguous and require
    the Delivery Agent to bind the intended one explicitly.
    """
    candidates: set[Path] = set()
    for match in re.finditer(r"(?<!\S)(/[^\s,;)\]}>]+)", direction or ""):
        raw = match.group(1).rstrip(".:")
        repository = _git_repository(Path(raw))
        if repository:
            candidates.add(repository)
    board_repository = _git_repository(project_context(root).code_root)
    external = {item for item in candidates if item != board_repository}
    return next(iter(external)) if len(external) == 1 else None


def _ensure_task_workspace(root: ProjectRoot, task: str) -> Path | None:
    """Return no workspace when the project has no Git repository.

    Git-backed tasks are handled earlier by :class:`GitBroker`.  Keeping a
    second worktree creator here would violate the broker's sole-writer
    contract; non-Git projects simply continue without a Git workspace.
    """
    return None


def task_workspace(root: ProjectRoot, task: str) -> Path:
    """Return the repository/worktree that contains the task candidate."""
    state = snapshot(root)
    value = state.get("task_workspaces", {}).get(task)
    code_root = project_context(root).code_root
    path = Path(value) if value else code_root
    return path if path.is_dir() else code_root


def task_path(root: Path, task: str, value: str) -> Path:
    """Resolve a task artifact in its isolated workspace when available."""
    state = snapshot(root)
    return _task_path_from_state(root, state, task, value)


def _task_path_from_state(root: Path, state: dict[str, Any], task: str, value: str) -> Path:
    """Resolve a task path without re-entering the board lock."""
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    code_root = project_context(root).code_root
    workspace_value = state.get("task_workspaces", {}).get(task, "")
    workspace = Path(workspace_value) if workspace_value and Path(workspace_value).is_dir() else code_root
    isolated = (workspace / path).resolve()
    if isolated.exists():
        return isolated
    return (code_root / path).resolve()


def owner_direction_for_task(state: dict[str, Any], agent_id: str, task: str) -> str:
    """Recover the immutable owner input that authorized this task.

    The interactive supervisor may receive a multi-line paste one terminal line
    at a time. Events are append-only, so concatenate that pre-task input
    rather than trusting the mutable session slot.
    """
    preserved = state.get("task_owner_directions", {}).get(task)
    if preserved:
        return contract.normalize_owner_direction(preserved)
    events = state.get("events", [])
    agents = state.get("agents", {})

    def resolve(current_id: str, current_task: str, visited: set[str]) -> str:
        if current_id in visited:
            return ""
        visited.add(current_id)
        # Mission Control owner messages are append-only and therefore more
        # authoritative than the mutable terminal-intake slot.  A later
        # approval such as "go ahead" must never replace the actual request.
        original = next(
            (
                message.get("text", "")
                for message in state.get("owner_messages", [])
                if message.get("type") == "direction"
                and message.get("agent_id") == current_id
                and message.get("text")
            ),
            "",
        )
        if original:
            return contract.normalize_owner_direction(original)
        begun = next(
            (event for event in events
             if event.get("kind") == "task_begun"
             and event.get("agent_id") == current_id
             and event.get("task") == current_task),
            None,
        )
        if begun:
            parts = [
                event.get("owner_direction", "") for event in events
                if event.get("kind") in {"owner_direction_received", "owner_direction_transferred"}
                and event.get("agent_id") == current_id
                and event.get("sequence", 0) < begun.get("sequence", 0)
            ]
            return contract.normalize_owner_direction("\n".join(part for part in parts if part))
        resumed = next(
            (event for event in reversed(events)
             if event.get("kind") == "task_resumed"
             and event.get("agent_id") == current_id
             and event.get("task") == current_task),
            None,
        )
        if not resumed:
            return ""
        source_id = str(resumed.get("source_agent_id", ""))
        if not source_id or source_id == current_id:
            return ""
        return resolve(source_id, current_task, visited)

    return resolve(agent_id, task, set())


def register(root: Path, role: str, task: str, display_name: str = "", vendor: str = "", session_id: str = "") -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError(f"invalid role {role!r}; choose one of: {', '.join(sorted(ROLES))}")
    if not task.strip():
        raise ValueError("task is required")
    if role in DEVELOPER_ROLES and task != AWAITING_OWNER_DIRECTION:
        raise ValueError("Delivery Agents must register in AWAITING_OWNER_DIRECTION and begin only after recorded owner direction")
    recovered_direction = ""
    with locked_state(
        root, operation="register agent", resume_session_id=session_id,
    ) as state:
        existing = next((
            agent for agent in state.get("agents", {}).values()
            if session_id and agent.get("session_id") == session_id
        ), None)
        pause = state.get("project_pause", {})
        saved_for_session = any(
            saved.get("session_id") == session_id
            for saved in pause.get("agents", {}).values()
        )
        if existing and existing.get("role") == role and (
            (pause.get("status") == "resuming" and saved_for_session)
            or (pause.get("status") == "active" and existing.get("active"))
        ):
            # Reattachment proves only that a fresh transport is starting. It
            # does not fabricate a poll or progress event, but the watchdog must
            # not charge owner-paused or restart time against this new process.
            attached_at = now()
            saved_agent = (pause.get("agents") or {}).get(existing.get("id"), {})
            if existing.get("status") == "offline":
                existing.update({
                    "status": saved_agent.get("status") or "working",
                    "status_note": (
                        saved_agent.get("status_note")
                        or "resumed terminal is awaiting its first board heartbeat"
                    ),
                    "last_status_at": attached_at,
                })
            existing.update({
                "active": True,
                "liveness": "recovering",
                "liveness_note": "resumed terminal attached; awaiting its first board heartbeat",
                "session_reattached_at": attached_at,
            })
            _event(state, "agent_resume_session_attached", existing, {
                "task": existing.get("task", task),
                "session_id": session_id,
                "message": "Resumed terminal reattached to its preserved board agent",
            })
            return dict(existing)
        sequence = int(state["role_counters"].get(role, 0)) + 1
        state["role_counters"][role] = sequence
        agent_id = f"{role}-{sequence:04d}-{secrets.token_hex(3)}"
        agent = {
            "id": agent_id, "role": role, "role_counter": sequence,
            "task": task, "display_name": display_name or role, "vendor": vendor.strip(),
            "spawned_at": now(), "last_poll_at": None, "last_progress_at": None, "poll_counter": 0,
            "last_status_at": now(), "status": "spawned", "status_note": "registered",
            # New agents begin at the current edge; historical work belongs in
            # events.jsonl, not in the first live poll.
            "cursor": int(state.get("next_event", 1)) - 1, "active": True, "liveness": "healthy", "liveness_note": "awaiting first board heartbeat",
            "session_id": session_id.strip(),
        }
        state["agents"][agent_id] = agent
        _event(state, "agent_registered", agent, {"task": task, "message": "agent registered"})
        if role in DEVELOPER_ROLES and task == AWAITING_OWNER_DIRECTION and session_id:
            orphaned = []
            for source in state["agents"].values():
                source_session = str(source.get("session_id", ""))
                direction = state.get("owner_directions", {}).get(source_session, {})
                if (
                    source.get("id") != agent_id
                    and source.get("role") in DEVELOPER_ROLES
                    and source.get("task") == AWAITING_OWNER_DIRECTION
                    and not source.get("active")
                    and source_session
                    and direction.get("text")
                    and not direction.get("consumed")
                    and not direction.get("transferred_to_session_id")
                    and _direction_is_recently_stranded(source, direction)
                ):
                    orphaned.append((direction.get("received_at", ""), source, source_session, direction))
            if orphaned:
                _, source, source_session, direction = min(orphaned, key=lambda item: (item[0], item[1]["id"]))
                recovered_direction = contract.normalize_owner_direction(direction["text"])
                transferred_at = now()
                direction.update({
                    "transferred_to_session_id": session_id,
                    "transferred_to_agent_id": agent_id,
                    "transferred_at": transferred_at,
                })
                state.setdefault("owner_directions", {})[session_id] = {
                    **direction,
                    "session_id": session_id,
                    "consumed": False,
                    "recovered_from_session_id": source_session,
                    "recovered_from_agent_id": source["id"],
                    "transferred_at": transferred_at,
                }
                agent.update({
                    "status": "direction_recovered",
                    "status_note": "saved owner direction recovered from the ended Delivery terminal",
                    "last_status_at": transferred_at,
                })
                _event(state, "owner_direction_transferred", agent, {
                    "task": task,
                    "source_agent_id": source["id"],
                    "owner_direction": recovered_direction,
                    "message": "unconsumed owner direction transferred to the replacement Delivery terminal",
                })
        result = dict(agent)
    if recovered_direction:
        try:
            from harness import control
            control.enqueue_instruction(
                root,
                session_id,
                "RECOVERED OWNER DIRECTION FROM ENDED DELIVERY TERMINAL:\n"
                f"{recovered_direction}\n\n"
                "Continue the Product Management clarification from this saved direction. "
                "Do not ask the owner to repeat it.",
                source="owner-direction-recovery",
            )
        except (ValueError, OSError):
            # The board copy remains authoritative if terminal routing races.
            pass
    if role == "qa":
        # Registration is the event that can satisfy a durable reviewer-needed
        # signal. Routing here keeps dashboard reads free of controller work.
        route_open_reviews(root)
    elif role in DEVELOPER_ROLES:
        # A replacement Delivery registration can satisfy a saved owner-repair
        # route. This is a material lifecycle event, not a dashboard poll.
        route_owner_repairs(root)
    return result


# Foreign-task conclusion signals every agent may see; everything else from
# another task is context flood (P8: a task poll contains no unrelated task
# events, poll heartbeats, or foreign verdict prose).
_FOREIGN_EVENT_ALLOWLIST = {"task_cancelled", "project_paused", "project_resumed"}
_CROSS_TASK_DESIGNATIONS = {"GLOBAL_MONITOR", "REVIEW_QUEUE", AWAITING_OWNER_DIRECTION}


def _scoped_events(agent: dict[str, Any], unseen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scope an agent's event feed to its task (P8 context hygiene)."""
    task = str(agent.get("task") or "")
    scoped: list[dict[str, Any]] = []
    cross_task = task in _CROSS_TASK_DESIGNATIONS
    for event in unseen:
        if event.get("kind") == "board_polled":
            continue
        event_task = str(event.get("task") or "")
        if cross_task or not event_task or event_task == task:
            scoped.append(event)
        elif event.get("kind") in _FOREIGN_EVENT_ALLOWLIST:
            scoped.append(event)
    return scoped


def _context_task_names(state: dict[str, Any], agent: dict[str, Any]) -> list[str]:
    """Return the tasks this role must reconstruct without rotating sessions."""
    task = str(agent.get("task") or "")
    if task not in _CROSS_TASK_DESIGNATIONS:
        return [task] if task else []
    names = {
        str(value.get("task") or "")
        for value in state.get("agents", {}).values()
        if value.get("active") and value.get("task") not in _CROSS_TASK_DESIGNATIONS
    }
    names.update(
        str(value.get("task") or "")
        for value in state.get("qa_requests", {}).values()
        if value.get("status") in {"authoring", "open", "reserved", "claimed"}
    )
    names.update(
        str(task_name)
        for task_name, release in state.get("releases", {}).items()
        if release.get("status") == "VISUAL_TEST_REQUIRED"
        and not state.get("release_decisions", {}).get(task_name)
    )
    names.update(str(task_name) for task_name in state.get("release_repairs", {}))
    names.update(
        str(finding.get("task") or "")
        for finding in state.get("deferred_findings", {}).values()
        if finding.get("status") == "in_scope"
    )
    return sorted(name for name in names if name)


def _task_context_bundle(state: dict[str, Any], task: str) -> dict[str, Any]:
    """Compact, board-authoritative reconstruction state for one task.

    Verbose command output stays in certified evidence. The full owner
    direction and the current governance facts remain inline so provider-side
    context compaction cannot turn into forgotten requirements.
    """
    requests = [
        value for value in state.get("qa_requests", {}).values()
        if value.get("task") == task
    ]
    latest = max(
        requests,
        key=lambda value: (
            str(value.get("completed_at") or value.get("claimed_at")
                or value.get("reserved_at") or value.get("requested_at") or ""),
            int(value.get("cycle", 0)),
        ),
        default={},
    )
    review = {
        key: latest.get(key)
        for key in (
            "id", "status", "phase", "subtask", "chunk", "cycle",
            "reviewed_commit", "reviewed_tree_hash", "test_scope",
            "scope_reason", "result", "result_summary", "evidence",
            "challenge_ledger_sha256", "repair_context",
        )
        if latest.get(key) not in (None, "", [], {})
    }
    release = state.get("releases", {}).get(task, {})
    decision = state.get("release_decisions", {}).get(task, {})
    repair = state.get("release_repairs", {}).get(task, {})
    in_scope_findings = [
        finding for finding in state.get("deferred_findings", {}).values()
        if finding.get("task") == task and finding.get("status") == "in_scope"
    ]
    if in_scope_findings:
        next_action = "Delivery repairs the recorded in-scope finding and submits the affected scope for re-review."
    elif release.get("status") == "VISUAL_TEST_REQUIRED" and not decision:
        next_action = "Owner visual acceptance is pending; agents remain in healthy standby."
    elif latest.get("status") == "failed":
        next_action = "Delivery repairs the recorded blocking result and submits the affected scope for re-review."
    elif latest.get("phase") == "final_acceptance" and latest.get("status") == "passed" and not release:
        next_action = "CTO completes claim-scope and release verification for the exact final PASS candidate."
    elif latest.get("status") in {"authoring", "open", "reserved", "claimed"}:
        next_action = "Independent Review completes the currently owned review request."
    elif repair:
        next_action = str(repair.get("next_action") or "Delivery completes the owner-requested repair.")
    else:
        next_action = "Delivery advances the next declared implementation or acceptance gate."
    return {
        "task": task,
        "owner_direction": str(state.get("task_owner_directions", {}).get(task) or ""),
        "requirements_confirmation": json.loads(json.dumps(
            state.get("requirement_confirmations", {}).get(task, {}))),
        "delivery_plan": json.loads(json.dumps(state.get("delivery_plans", {}).get(task, {}))),
        "task_brief": json.loads(json.dumps(state.get("task_briefs", {}).get(task, {}))),
        "latest_review": review,
        "release": json.loads(json.dumps(release)),
        "owner_decision": json.loads(json.dumps(decision)),
        "next_action": next_action,
    }


def _agent_context_bundle(state: dict[str, Any], agent: dict[str, Any]) -> dict[str, Any]:
    tasks = _context_task_names(state, agent)
    return {
        "scope": "global" if agent.get("role") == "cto" else "task",
        "authority": "board",
        "generated_at": now(),
        "tasks": [_task_context_bundle(state, task) for task in tasks],
        "untriaged_findings": [
            {
                key: finding.get(key)
                for key in ("id", "task", "title", "status", "next_action")
                if finding.get(key) not in (None, "")
            }
            for finding in state.get("deferred_findings", {}).values()
            if finding.get("status") in {"in_scope", "fix_requested", "fix_in_progress"}
        ],
    }


def poll(root: Path, agent_id: str) -> dict[str, Any]:
    rejection = ""
    with locked_state(root) as state:
        agent = _require_writable_agent(state, agent_id)
        if not agent.get("active") or agent.get("status") == "offline" or agent.get("liveness") == "offline":
            raise ValueError("inactive/offline agent cannot poll or regain healthy liveness")
        session_id = str(agent.get("session_id", ""))
        if session_id:
            from harness import control
            managed = next((item for item in control.snapshot(root)["sessions"] if item.get("id") == session_id), None)
            if managed and managed.get("status") not in control.ACTIVE_STATUSES:
                agent.update({
                    "active": False,
                    "status": "offline",
                    "liveness": "offline",
                    "liveness_note": "managed terminal ended; board heartbeat refused",
                    "status_note": "managed terminal is no longer running; board heartbeat refused",
                    "last_status_at": now(),
                })
                _event(state, "board_poll_refused", agent, {
                    "task": agent["task"],
                    "session_id": session_id,
                    "message": "poll refused because the managed session registry marks this terminal exited",
                })
                rejection = "managed session is inactive; board heartbeat refused"
        if not rejection:
            raw_unseen = [e for e in state["events"] if e["sequence"] > agent["cursor"]]
            unseen = _scoped_events(agent, raw_unseen)
            first_available = int(state.get("event_window_start") or (state["events"][0]["sequence"] if state.get("events") else state.get("next_event", 1)))
            history_truncated = int(agent.get("cursor", 0)) < first_available - 1
            if history_truncated:
                unseen.insert(0, {
                    "sequence": first_available - 1,
                    "at": now(),
                    "kind": "history_truncated",
                    "agent_id": "system",
                    "role": "system",
                    "task": agent.get("task", ""),
                    "requested_from_sequence": int(agent.get("cursor", 0)) + 1,
                    "available_from_sequence": first_available,
                    "durable_log": str(board_dir(root) / "events.jsonl"),
                    "message": "Earlier board events were moved out of hot state; read the durable event log before acting so failures are not silently skipped",
                })
            recovered = agent.get("liveness") in {"stalled", "recovering"} or agent.get("recovery_state") in {"reset_requested", "automatic_requested", "automatic_failed"}
            agent["poll_counter"] += 1
            agent["last_poll_at"] = now()
            agent.update({"liveness": "healthy", "liveness_note": "board heartbeat is current"})
            if agent.get("recovery_state") in {"reset_requested", "automatic_requested", "automatic_failed"}:
                agent.update({"recovery_state": "resumed", "status": "working", "status_note": "recovery accepted; preserved task and next action resumed", "last_status_at": now()})
                agent.pop("automatic_recovery_requested_at", None)
            if raw_unseen:
                # Cursor advances over everything SCANNED, not only delivered.
                agent["cursor"] = raw_unseen[-1]["sequence"]
            if recovered:
                _event(state, "agent_recovered", agent, {"task": agent["task"], "message": "board heartbeat resumed; agent must process outstanding work"})
            _event(state, "board_polled", agent, {"task": agent["task"], "poll_counter": agent["poll_counter"], "unseen": len(unseen)})
            result = {
                "agent_id": agent_id,
                "role": agent["role"],
                "poll_counter": agent["poll_counter"],
                "events": unseen,
                "history_truncated": history_truncated,
                "context_bundle": _agent_context_bundle(state, agent),
            }
        else:
            result = None
    if rejection:
        raise ValueError(rejection)
    return result


def request_recovery(root: Path, agent_id: str) -> dict[str, Any]:
    """Reset a visible blocker while preserving and routing the agent's task memory."""
    session_id = ""
    task = ""
    note = ""
    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        if not agent.get("active"):
            raise ValueError("inactive agent requires a replacement session, not blocker reset")
        session_id = agent.get("session_id", "")
        if not session_id:
            raise ValueError("agent has no managed terminal for recovery routing")
        task = agent["task"]
        note = agent.get("status_note", "")
    from harness import control
    try:
        control.enqueue_instruction(root, session_id, f"RECOVERY REQUEST: Poll the board now and resume {task}. The prior status was: {note}. Task memory and the next action are preserved. Do not ask the owner to repeat the task.", source="mission-control-recovery")
    except ValueError as error:
        with locked_state(root) as state:
            agent = _require_agent(state, agent_id)
            agent.pop("recovery_state", None)
            agent.pop("recovery_context", None)
            agent.update({
                "status_note": f"Recovery route unavailable; blocker remains visible: {error}",
                "last_status_at": now(),
            })
            _event(state, "agent_recovery_failed", agent, {"task": task, "message": "recovery could not reach the managed terminal; blocker and Recover control remain visible", "error": str(error)})
        raise
    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        agent["recovery_context"] = {
            "task": task,
            "previous_status": agent.get("status", ""),
            "previous_status_note": note,
            "requested_at": now(),
            "next_action": "poll the board and resume the preserved task from its latest review or release gate",
        }
        agent.update({
            "recovery_state": "reset_requested",
            "liveness": "recovering",
            "liveness_note": "recovery was requested from Mission Control",
            "status": "recovery_requested",
            "status_note": "Mission Control reset the blocker; saved task memory is being resumed",
            "last_status_at": now(),
        })
        event = _event(state, "agent_recovery_requested", agent, {"task": task, "message": "blocker reset requested; task and next action preserved; owner action is not required"})
    return event


def _agent_has_actionable_work(state: dict[str, Any], agent: dict[str, Any]) -> bool:
    """Return whether silence means this role is failing to advance work."""
    role = agent.get("role")
    if role == "qa":
        return any(
            request.get("status") in {"authoring", "open", "reserved", "claimed"}
            and (
                request.get("routed_to") == agent.get("id")
                or request.get("claimed_by") == agent.get("id")
            )
            for request in state.get("qa_requests", {}).values()
        )
    if role == "cto":
        # The CTO is the global monitor across all projects. While any task is
        # active, a bounded five-minute poll is actionable supervisory work;
        # this does not rotate the CTO or create a new terminal.
        cancelled = state.get("cancelled_tasks", {})
        if any(
            candidate.get("active")
            and candidate.get("role") in DEVELOPER_ROLES
            and candidate.get("task") != AWAITING_OWNER_DIRECTION
            and candidate.get("task") not in cancelled
            for candidate in state.get("agents", {}).values()
        ):
            return True
        if any(
            request.get("status") in {"authoring", "open", "reserved", "claimed"}
            and request.get("task") not in cancelled
            for request in state.get("qa_requests", {}).values()
        ):
            return True
        if any(
            candidate.get("active")
            and candidate.get("liveness") in {"stalled", "offline"}
            for candidate in state.get("agents", {}).values()
        ):
            return True
        if any(
            repair.get("status") in {"OWNER_REJECTED_REPAIR_REQUIRED"}
            and not any(
                agent.get("active") and agent.get("role") in DEVELOPER_ROLES
                and agent.get("task") == task
                for agent in state.get("agents", {}).values()
            )
            for task, repair in (state.get("release_repairs") or {}).items()
        ):
            return True
        if state.get("git_recovery_holds"):
            return True

        if any(
            finding.get("status") == "in_scope"
            and not any(
                candidate.get("active")
                and candidate.get("role") in DEVELOPER_ROLES
                and candidate.get("task") == finding.get("task")
                for candidate in state.get("agents", {}).values()
            )
            for finding in state.get("deferred_findings", {}).values()
        ):
            return True
        # A PASSED final acceptance without a recorded release is CTO work even
        # when delivery has already gone quiet. Without this, the CTO drops to
        # standby (never nudged) and a finished task waits overnight for its
        # release checks. Self-clearing: recording the release ends it.
        releases = state.get("releases", {})
        return any(
            request.get("phase") == "final_acceptance"
            and request.get("status") == "passed"
            and request.get("task") not in cancelled
            and releases.get(request.get("task"), {}).get("status") != "VISUAL_TEST_REQUIRED"
            for request in state.get("qa_requests", {}).values()
        )
    if role in DEVELOPER_ROLES:
        if agent.get("task") == AWAITING_OWNER_DIRECTION:
            return False
        plan = state.get("delivery_plans", {}).get(agent.get("task"), {})
        if any(
            _effective_subtask_pipeline_status(
                state, str(agent.get("task") or ""), name, item,
            ) == "in_progress"
            for name, item in plan.get("subtasks", {}).items()
        ):
            return True
        active_reviews = [
            request for request in state.get("qa_requests", {}).values()
            if request.get("task") == agent.get("task")
            and request.get("status") in {"authoring", "open", "reserved", "claimed"}
        ]
        if any(
            request.get("developer_id") == agent.get("id")
            and request.get("delivery_state") == "executing"
            and not (
                (agent.get("review_execution") or {}).get("request_id") == request.get("id")
                and _review_execution_is_current(agent)
            )
            for request in active_reviews
        ):
            return True
        # An open review belongs to the Independent Reviewer. Delivery is
        # truthfully waiting and will be routed automatically on FAIL/PASS.
        return not active_reviews
    return False


def _review_authoring_lease_current(
    state: dict[str, Any], agent: dict[str, Any], current: datetime,
) -> bool:
    """Trust only durable Reviewer board activity for the authoring lease."""
    if agent.get("role") != "qa":
        return False
    request = next((
        candidate for candidate in state.get("qa_requests", {}).values()
        if candidate.get("status") == "reserved"
        and candidate.get("reserved_by") == agent.get("id")
    ), None)
    if not request:
        return False
    activities = [
        parsed for value in (
            request.get("authoring_last_activity_at"), request.get("reserved_at"),
            agent.get("last_poll_at"),
        ) if (parsed := _parsed_timestamp(value)) is not None
    ]
    activity = max(activities) if activities else None
    return bool(
        activity is not None
        and (current - activity).total_seconds() < REVIEW_RESERVATION_SECONDS
    )


def _automatic_recovery_instruction(agent: dict[str, Any]) -> str:
    role = agent.get("role")
    task = agent.get("task", "current work")
    if role == "cto":
        return "MONITORING CYCLE DUE: Poll the board once now, process material changes, route the next concrete action, and post a short human-readable status. Never wait on the product owner: owner touchpoints are asynchronous board surfaces you post and continue past, and an owner rejection is routed to a repair cycle automatically. When only an owner decision is outstanding, record 'awaiting owner decision' and stand by healthy — never hold the cycle for a reply. USER ACTION: None."
    if role == "qa":
        return f"REVIEW ACTION DUE: Poll the board once now and continue the routed review for {task}. Preserve its existing evidence and task memory. USER ACTION: None."
    return f"TASK ACTION DUE: Poll the board once now and continue {task} from its saved next gate. Preserve the owner direction, requirements, evidence, and task memory. USER ACTION: None."


def mark_stalled(root: Path, stale_seconds: int = AGENT_STALE_SECONDS) -> list[dict[str, Any]]:
    """Record missing board heartbeats without changing the agent's workflow state.

    A terminal PID proves only that a program still exists.  The watchdog owns
    this separate liveness signal so the board and viewer never describe an
    unresponsive agent as working.
    """
    if stale_seconds < 1:
        raise ValueError("stale_seconds must be positive")
    current = datetime.now(timezone.utc)
    stalled: list[dict[str, Any]] = []
    recovery_routes: list[dict[str, str]] = []
    try:
        from harness import control
        sessions_snapshot = control.snapshot(root).get("sessions", [])
        live_session_ids = {
            item.get("id") for item in sessions_snapshot
            if item.get("status") in control.ACTIVE_STATUSES
        }
    except (OSError, ValueError):
        live_session_ids = None
    with locked_state(root) as state:
        for agent in state["agents"].values():
            if not agent.get("active"):
                continue
            if _review_execution_is_current(agent):
                continue
            # A dead terminal is never "healthy standby" (issue row 9): standby
            # only describes an agent whose managed session is actually alive.
            # When control state is unreadable, judge nothing rather than lie.
            if (
                live_session_ids is not None
                and agent.get("session_id")
                and agent["session_id"] not in live_session_ids
            ):
                if agent.get("liveness") != "offline":
                    agent.update({
                        "liveness": "offline",
                        "liveness_note": "managed terminal is no longer running",
                    })
                    _event(state, "agent_offline", agent, {
                        "task": agent["task"],
                        "message": "managed terminal is no longer running; standby would be false health",
                    })
                continue
            if not _agent_has_actionable_work(state, agent):
                if agent.get("liveness") in {"stalled", "recovering"}:
                    agent.update({
                        "liveness": "healthy",
                        "liveness_note": "standing by with no assigned action",
                        "recovery_state": "standby",
                    })
                    _event(state, "agent_standby", agent, {
                        "task": agent["task"],
                        "message": "role is healthy and standing by; no recovery is required",
                    })
                continue
            # Reserving a review, recording intentions, and attaching its ledger
            # are material board heartbeats. A real Reviewer poll also starts the
            # same bounded authoring lease; terminal output and status prose do
            # not. Reservation expiry recovers a genuinely abandoned review.
            if _review_authoring_lease_current(state, agent, current):
                continue
            if agent.get("role") == "cto":
                # Process output proves liveness, not that the global monitor
                # inspected the board. Only a poll renews this lease.
                heartbeats = [agent.get("last_poll_at") or agent.get("spawned_at")]
                effective_stale_seconds = (
                    CTO_MONITOR_ROUTE_SECONDS
                    if stale_seconds == AGENT_STALE_SECONDS
                    else stale_seconds
                )
            else:
                heartbeats = [
                    agent.get("last_poll_at"), agent.get("last_progress_at"),
                    agent.get("spawned_at"),
                ]
                effective_stale_seconds = stale_seconds
            reattached_at = _parsed_timestamp(agent.get("session_reattached_at"))
            if (
                reattached_at is not None
                and (current - reattached_at).total_seconds() < effective_stale_seconds
            ):
                # A relaunched process gets one normal heartbeat interval to
                # poll. This is transport grace, not evidence that it progressed.
                continue
            parsed_heartbeats = [
                parsed for value in heartbeats
                if (parsed := _parsed_timestamp(value)) is not None
            ]
            fallback = _parsed_timestamp(agent.get("last_status_at"))
            heartbeat_at = max(parsed_heartbeats) if parsed_heartbeats else fallback
            # Malformed persisted heartbeat state cannot make an agent healthy
            # and cannot crash the continuously running controller.
            age = (
                (current - heartbeat_at).total_seconds()
                if heartbeat_at is not None else float(effective_stale_seconds)
            )
            recovery_requested_at = agent.get("automatic_recovery_requested_at", "")
            if agent.get("recovery_state") == "automatic_requested" and recovery_requested_at:
                recovery_at = _parsed_timestamp(recovery_requested_at)
                request_age = (
                    (current - recovery_at).total_seconds()
                    if recovery_at is not None else float(AUTO_RECOVERY_GRACE_SECONDS)
                )
                heartbeat_after_request = bool(
                    recovery_at is not None and heartbeat_at is not None
                    and heartbeat_at > recovery_at
                )
                if not heartbeat_after_request and request_age >= AUTO_RECOVERY_GRACE_SECONDS:
                    note = f"Automatic wake-up received no board heartbeat for {int(request_age)}s"
                    agent.update({"liveness": "stalled", "liveness_note": note, "recovery_state": "automatic_failed"})
                    event = _event(state, "agent_stalled", agent, {"task": agent["task"], "age_seconds": int(age), "message": note})
                    stalled.append(event)
                continue
            if agent.get("recovery_state") == "automatic_failed" and recovery_requested_at:
                # Automatic recovery already failed for this agent. Do NOT re-request a
                # wake-up every cycle — that oscillation (stalled -> recovering ->
                # stalled ...) with repeated nudges is the flapping. Hold it visibly
                # stalled and retry at most once per AUTO_RECOVERY_RETRY_SECONDS. A real
                # board heartbeat (poll or progress) clears this state immediately.
                recovery_at = _parsed_timestamp(recovery_requested_at)
                since_last_attempt = (
                    (current - recovery_at).total_seconds()
                    if recovery_at is not None else float(AUTO_RECOVERY_RETRY_SECONDS)
                )
                if since_last_attempt < AUTO_RECOVERY_RETRY_SECONDS:
                    continue
            if age < effective_stale_seconds:
                continue
            # A blocked agent is not a stalled agent. While its task carries an
            # open control-plane hold, wake-up nudges cannot help and only burn
            # tokens (171 of them on 2026-08-21). Route at most one recovery per
            # AUTO_RECOVERY_RETRY_SECONDS under a hold; the red card owns
            # visibility.
            open_hold = (state.get("control_plane_holds") or {}).get(agent.get("task", ""))
            if (
                isinstance(open_hold, dict) and open_hold.get("status") == "open"
                and agent.get("role") != "cto"
            ):
                held_recovery_at = _parsed_timestamp(agent.get("automatic_recovery_requested_at"))
                if (
                    held_recovery_at is not None
                    and (current - held_recovery_at).total_seconds() < AUTO_RECOVERY_RETRY_SECONDS * 4
                ):
                    continue
            requested_at = now()
            # The CTO's monitoring loop is DRIVEN by these nudges (its cycle is
            # longer than the stale threshold), so for the CTO this is routine
            # scheduling, not failure recovery — label it truthfully (row 4).
            routine_cycle = agent.get("role") == "cto"
            agent.update({
                "liveness": "recovering",
                "liveness_note": (
                    "scheduled monitoring cycle nudge; owner action is not required"
                    if routine_cycle
                    else "automatic wake-up routed; owner action is not required"
                ),
                "recovery_state": "automatic_requested",
                "automatic_recovery_requested_at": requested_at,
            })
            _event(state, "agent_automatic_recovery_routed", agent, {
                "task": agent["task"],
                "age_seconds": int(age),
                "routine_cycle": routine_cycle,
                "message": (
                    "scheduled CTO monitoring cycle (not a failure recovery); owner action is not required"
                    if routine_cycle
                    else "the controller routed the saved next action automatically; owner action is not required"
                ),
            })
            recovery_routes.append({
                "agent_id": agent["id"],
                "session_id": str(agent.get("session_id", "")),
                "requested_at": requested_at,
                "instruction": _automatic_recovery_instruction(agent),
            })
    for route in recovery_routes:
        try:
            queued = control.enqueue_instruction(
                root, route["session_id"], route["instruction"],
                source=(
                    "cto-monitoring-lease"
                    if route["instruction"].startswith("MONITORING CYCLE DUE")
                    else "automatic-recovery"
                ),
            )
            with locked_state(root) as state:
                agent = state.get("agents", {}).get(route["agent_id"])
                if not agent or agent.get("automatic_recovery_requested_at") != route["requested_at"]:
                    continue
                agent["automatic_recovery_instruction_id"] = queued["id"]
                _event(state, "instruction_route_queued", agent, {
                    "task": agent["task"],
                    "instruction_id": queued["id"],
                    "source": queued["source"],
                    "message": "scheduled monitoring instruction durably queued",
                })
        except (OSError, ValueError) as error:
            with locked_state(root) as state:
                agent = state.get("agents", {}).get(route["agent_id"])
                if not agent or agent.get("automatic_recovery_requested_at") != route["requested_at"]:
                    continue
                note = f"Automatic wake-up could not reach the managed terminal: {error}"
                agent.update({"liveness": "stalled", "liveness_note": note, "recovery_state": "automatic_failed"})
                stalled.append(_event(state, "agent_stalled", agent, {
                    "task": agent["task"], "message": note,
                }))
    return stalled


def record_owner_direction(root: Path, session_id: str, text: str) -> dict[str, Any]:
    """Record the owner's actual terminal input for a managed Delivery session."""
    session_id = session_id.strip()
    text = contract.normalize_owner_direction(text)
    if not session_id or not text:
        raise ValueError("session_id and owner direction are required")
    # A bare CLI control command (/model, /help, …) typed at the terminal is
    # input for the CLI, never the owner's objective (issue row 1). A real
    # direction that begins with a filesystem path contains a second slash and
    # is unaffected.
    if re.fullmatch(r"/[A-Za-z0-9_-]+", text):
        raise ValueError(
            f"'{text}' is a CLI control command, not an owner direction; "
            "type the actual task objective"
        )
    with locked_state(root) as state:
        agent = next((value for value in state["agents"].values() if value.get("session_id") == session_id), None)
        if not agent or agent["role"] not in DEVELOPER_ROLES:
            raise ValueError("owner direction is accepted only for a managed Delivery session")
        if agent["task"] != AWAITING_OWNER_DIRECTION:
            raise ValueError("this Delivery session already has an active task")
        if state.get("owner_directions", {}).get(session_id):
            raise ValueError("this Delivery session already has an owner direction")
        direction = {"session_id": session_id, "text": text, "received_at": now(), "consumed": False}
        state.setdefault("owner_directions", {})[session_id] = direction
        return _event(state, "owner_direction_received", agent, {"task": agent["task"], "message": "owner direction recorded for Product Management", "owner_direction": text})


def _prepare_owner_message_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(attachments) > MAX_ATTACHMENTS:
        raise ValueError(f"you can attach up to {MAX_ATTACHMENTS} files")
    prepared: list[dict[str, Any]] = []
    total = 0
    for attachment in attachments:
        content_type = str(attachment.get("content_type", "")).split(";", 1)[0].strip().lower()
        data = bytes(attachment.get("data", b""))
        if content_type not in ALLOWED_ATTACHMENT_TYPES:
            raise ValueError("only documents, PDFs, and screenshots can be attached")
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError("each attachment must be larger than zero and no bigger than 10 MB")
        total += len(data)
        prepared.append({
            "display_name": _safe_attachment_name(str(attachment.get("filename", ""))),
            "content_type": content_type,
            "data": data,
            "extension": ALLOWED_ATTACHMENT_TYPES[content_type],
        })
    if total > MAX_TOTAL_ATTACHMENT_BYTES:
        raise ValueError("the attachments are too large")
    return prepared


def _store_owner_message_attachments(root: Path, message_id: str, prepared: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not prepared:
        return []
    storage_dir = board_dir(root) / "owner-messages" / message_id
    written: list[Path] = []
    metadata: list[dict[str, Any]] = []
    try:
        for item in prepared:
            stored_name = f"{secrets.token_hex(16)}{item['extension']}"
            stored_path = storage_dir / stored_name
            _write_attachment(stored_path, item["data"])
            written.append(stored_path)
            metadata.append({
                "attachment_id": secrets.token_hex(12),
                "display_name": item["display_name"],
                "content_type": item["content_type"],
                "size": len(item["data"]),
                "stored_name": stored_name,
                "stored_path": _display_storage_path(root, stored_path),
                "sha256": hashlib.sha256(item["data"]).hexdigest(),
            })
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return metadata


def _display_storage_path(root: ProjectRoot, path: Path) -> str:
    """Keep compatibility metadata relative; explicit external data stays absolute."""
    code_root = project_context(root).code_root
    try:
        return str(path.resolve().relative_to(code_root))
    except ValueError:
        return str(path.resolve())


def record_owner_message(root: Path, agent_id: str, text: str, message_type: str = "direction", attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Record one atomic owner message and route it to a Delivery terminal.

    Initial direction messages authorize a waiting Delivery Agent. Clarifications
    are append-only task messages and never rewrite the original direction.
    Attachments are stored under generated names and included only as metadata in
    the board event and routed terminal message.
    """
    text = contract.normalize_owner_direction(text)
    message_type = str(message_type or "").strip().lower()
    if not text:
        raise ValueError("a direction or clarification is required")
    if len(text) > MAX_REASON_LENGTH:
        raise ValueError(f"the message must be {MAX_REASON_LENGTH} characters or fewer")
    if message_type not in {"direction", "clarification"}:
        raise ValueError("message type must be direction or clarification")
    prepared = _prepare_owner_message_attachments(attachments or [])
    session_id = ""
    message: dict[str, Any]
    with locked_state(root) as state:
        agent = _require_writable_agent(state, agent_id)
        if agent["role"] not in DEVELOPER_ROLES or not agent.get("active"):
            raise ValueError("only an active Delivery Agent can receive owner messages")
        session_id = str(agent.get("session_id", "")).strip()
        if not session_id:
            raise ValueError("the Delivery Agent has no managed CLI session")
        task = str(agent.get("task", ""))
        if message_type == "direction":
            if task != AWAITING_OWNER_DIRECTION:
                raise ValueError("this Delivery Agent already has a task; use Send clarification")
            if state.get("owner_directions", {}).get(session_id):
                raise ValueError("an owner direction is already recorded for this waiting session")
        elif task == AWAITING_OWNER_DIRECTION and not state.get("owner_directions", {}).get(session_id):
            raise ValueError("a clarification requires an initial owner direction")
        message_id = secrets.token_hex(12)
        metadata = _store_owner_message_attachments(root, message_id, prepared)
        created_at = now()
        message = {
            "id": message_id, "type": message_type, "agent_id": agent_id,
            "session_id": session_id, "task": task if task != AWAITING_OWNER_DIRECTION else "",
            "text": text, "attachments": metadata, "created_at": created_at,
            "status": "queued",
        }
        state.setdefault("owner_messages", []).append(message)
        if message_type == "direction":
            state.setdefault("owner_directions", {})[session_id] = {
                "session_id": session_id, "text": text, "received_at": created_at,
                "consumed": False, "message_id": message_id, "attachments": metadata,
            }
            event = _event(state, "owner_direction_received", agent, {
                "task": agent["task"], "message": "owner direction recorded from Mission Control",
                "owner_direction": text, "owner_message_id": message_id, "attachments": metadata,
            })
        else:
            if task == AWAITING_OWNER_DIRECTION:
                state.setdefault("pending_owner_clarifications", {}).setdefault(session_id, []).append(message)
            else:
                state.setdefault("owner_clarifications", {}).setdefault(task, []).append(message)
            event = _event(state, "owner_clarification_received", agent, {
                "task": task, "message": "owner clarification recorded from Mission Control",
                "clarification": text, "owner_message_id": message_id, "attachments": metadata,
            })
    attachment_lines = "".join(f"\n- {item['display_name']} ({item['stored_path']})" for item in message["attachments"])
    label = "OWNER DIRECTION" if message_type == "direction" else f"OWNER CLARIFICATION for {message['task']}"
    routed = f"{label}:\n{text}"
    if attachment_lines:
        routed += "\nAttachments saved by Mission Control:" + attachment_lines
    try:
        from harness import control
        control.enqueue_instruction(root, session_id, routed, source=f"owner-{message_type}")
    except (ValueError, OSError):
        # The board record is durable; a recovered Delivery session can receive
        # the saved message from task history when it returns.
        pass
    return {"message": message, "event": event}


def record_requirement_confirmation(root: Path, agent_id: str, text: str) -> dict[str, Any]:
    """Persist the final, clarified requirements after the owner says go ahead.

    The original owner direction remains immutable. This is an append-only
    confirmation record: later scope changes require a new owner direction/task
    rather than silently rewriting the original request.
    """
    text = contract.normalize_owner_direction(text)
    if not text or len(text) > 12000:
        raise ValueError("final requirements confirmation is required and must be 12000 characters or fewer")
    with locked_state(root) as state:
        agent = _require_writable_agent(state, agent_id)
        if agent["role"] not in DEVELOPER_ROLES or agent["task"] == AWAITING_OWNER_DIRECTION or not agent.get("active"):
            raise ValueError("only an active Delivery Agent with a task may confirm requirements")
        task = agent["task"]
        if state.get("delivery_plans", {}).get(task) or _task_requests(state, task):
            raise ValueError("final requirements must be confirmed before planning or review work starts")
        direction = owner_direction_for_task(state, agent_id, task)
        if not direction:
            raise ValueError("cannot confirm requirements without the preserved original owner direction")
        existing = state.setdefault("requirement_confirmations", {}).get(task)
        if existing:
            raise ValueError("final requirements are already confirmed for this task; start a new owner-directed task for scope changes")
        confirmation = {
            "task": task, "agent_id": agent_id, "text": text,
            "owner_direction": direction, "version": 1,
            "confirmed_at": now(), "status": "confirmed",
        }
        state["requirement_confirmations"][task] = confirmation
        agent.update({"status": "requirements_confirmed", "status_note": "final requirements confirmed; Product Management may plan delivery", "last_status_at": now()})
        return _event(state, "requirements_confirmed", agent, {
            "task": task, "message": "final clarified requirements confirmed after owner direction; delivery may now begin",
            "owner_direction": direction, "requirements_confirmation": text,
        })


def status(root: Path, agent_id: str, note: str, state_name: str = "working") -> dict[str, Any]:
    note = note.strip()
    if not note or len(note) > 240:
        raise ValueError("status note is required and must be 240 characters or fewer")
    with locked_state(root) as state:
        agent = _require_writable_agent(state, agent_id)
        if not agent.get("active") or agent.get("status") == "offline" or agent.get("liveness") == "offline":
            raise ValueError("inactive/offline agent cannot post status updates")
        agent.update({"status": state_name, "status_note": note, "last_status_at": now()})
        return _event(state, "status_update", agent, {"task": agent["task"], "state": state_name, "message": note})


def _direction_is_recently_stranded(agent: dict[str, Any], direction: dict[str, Any], recovery_seconds: int = 600) -> bool:
    """Avoid silently attaching an old abandoned request to a fresh session."""
    timestamp = agent.get("last_status_at") or direction.get("received_at")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(timestamp))).total_seconds() <= recovery_seconds
    except (TypeError, ValueError):
        return False


def offline(root: Path, agent_id: str, note: str = "managed CLI session ended", transport_ended: bool = False) -> dict[str, Any]:
    """Close an agent's board presence when its visible terminal ends."""
    note = note.strip()
    if not note or len(note) > 240:
        raise ValueError("offline note is required and must be 240 characters or fewer")
    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        if not agent.get("active"):
            return {"agent_id": agent_id, "already_offline": True}
        session_id = str(agent.get("session_id", ""))
        if session_id and not transport_ended:
            from harness import control
            managed = next(
                (item for item in control.snapshot(root).get("sessions", []) if item.get("id") == session_id),
                None,
            )
            if managed and managed.get("status") in control.ACTIVE_STATUSES:
                raise ValueError("a live managed terminal cannot declare itself offline; stop the terminal instead")
        agent.update({"active": False, "status": "offline", "status_note": note, "last_status_at": now(), "liveness": "offline", "liveness_note": note})
        return _event(state, "agent_offline", agent, {"task": agent["task"], "message": note})


def task_brief(root: Path, agent_id: str, plan: str, update: str) -> dict[str, Any]:
    """Publish the Delivery Agent's two-line human-facing explanation.

    This is deliberately separate from technical status notes: Mission Control
    must tell the owner what is being built and what is happening now without
    asking them to decode board IDs or raw events.
    """
    plan, update = plan.strip(), update.strip()
    if not plan or not update or len(plan) > 420 or len(update) > 240:
        raise ValueError("task brief needs a plan (1-420 chars) and update (1-240 chars)")
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        if developer["role"] not in DEVELOPER_ROLES or developer["task"] == AWAITING_OWNER_DIRECTION:
            raise ValueError("only an active Delivery Agent may publish a task brief")
        value = {"plan": plan, "update": update, "updated_at": now(), "agent_id": agent_id}
        state.setdefault("task_briefs", {})[developer["task"]] = value
        _event(state, "task_brief_updated", developer, {"task": developer["task"], "message": update})
        return json.loads(json.dumps(value))


def expand_contract(root: Path, agent_id: str, additions: list[tuple[str, str]]) -> dict[str, Any]:
    """Append Product-Management-discovered deliverables with a board audit."""
    with locked_state(root) as state:
        agent = _require_writable_agent(state, agent_id)
        if agent["role"] not in DEVELOPER_ROLES or agent["task"] == AWAITING_OWNER_DIRECTION:
            raise ValueError("only an active Delivery Agent may expand its Completion Contract")
        task = agent["task"]
    result = contract.expand_contract(root, task, additions, actor="Product Management")
    with locked_state(root) as state:
        agent = _require_writable_agent(state, agent_id)
        event = _event(state, "completion_contract_expanded", agent, {
            "task": task, "message": f"append-only Completion Contract expansion added {len(result['added'])} necessary deliverables",
            "deliverables": [item["name"] for item in result["added"]],
        })
        result["audit_event"] = event
        return result


def migrate_contract_scope(root: Path, agent_id: str) -> dict[str, Any]:
    """Record the one-time immutable definition for an older contract."""
    with locked_state(root) as state:
        agent = _require_writable_agent(state, agent_id)
        if agent["role"] not in DEVELOPER_ROLES or agent["task"] == AWAITING_OWNER_DIRECTION:
            raise ValueError("only an active Delivery Agent may migrate its Completion Contract")
        task = agent["task"]
    value = contract.migrate_legacy_scope(root, task)
    with locked_state(root) as state:
        agent = _require_writable_agent(state, agent_id)
        event = _event(state, "completion_contract_scope_migrated", agent, {"task": task, "message": "legacy Completion Contract immutable scope was recorded for append-only governance"})
        return {"contract": value, "audit_event": event}


def begin_task(root: Path, agent_id: str, task: str) -> dict[str, Any]:
    """Turn a standing-by Delivery Agent into a real owner-directed task."""
    task = task.strip()
    if not task or task == AWAITING_OWNER_DIRECTION:
        raise ValueError("a real internal task identifier is required")
    with locked_state(root) as state:
        agent = _require_writable_agent(state, agent_id)
        if agent["role"] not in DEVELOPER_ROLES:
            raise ValueError("only development or engineering agents may begin a task")
        if agent["task"] != AWAITING_OWNER_DIRECTION:
            raise ValueError("agent already has a task")
        if agent.get("session_id"):
            direction = state.get("owner_directions", {}).get(agent["session_id"])
            if not direction or direction.get("consumed"):
                raise ValueError("managed Delivery Agent cannot begin a task until owner terminal direction is recorded")
        else:
            raise ValueError("Delivery Agents require a managed session so the owner direction is recorded verbatim")
        if task in state.get("task_workspaces", {}) or any(value.get("task") == task for value in state.get("agents", {}).values()):
            raise ValueError("task already has an owner or isolated workspace")
        direction_text = direction["text"] if agent.get("session_id") else ""
        pending_clarifications = list(
            state.get("pending_owner_clarifications", {}).get(agent.get("session_id", ""), [])
        )
        repository_direction = "\n".join(
            [direction_text]
            + [str(item.get("text") or "") for item in pending_clarifications]
        )
        # Authorization is complete before any external Git artifact is made.
        # A single Git repository explicitly named by the owner is the task's
        # execution/review root. Otherwise retain the established private
        # worktree behavior for work in this board's own repository.
        owner_repository = _owner_named_repository(root, repository_direction)
        # SELF-DEVELOPMENT FREEZE: refuse to begin a governed delivery task whose
        # execution workspace is the harness's own repository. The control plane
        # must be repaired directly, outside a running harness — never by the
        # agents it is orchestrating (that is what corrupts commits and mixes
        # versions). Targeting a different owner-named repository is unaffected.
        # Checked before any worktree is created, so no side effect precedes the
        # refusal.
        prospective_workspace = (
            owner_repository
            if owner_repository is not None
            else project_context(root).code_root
        )
        if _is_harness_self_target(prospective_workspace) and os.environ.get("HARNESS_ALLOW_SELF_DEVELOPMENT") != "1":
            raise ValueError(
                "refusing to begin a governed task inside the harness's own "
                "repository: fix the harness directly, outside the harness, or "
                "target a different repository. Set HARNESS_ALLOW_SELF_DEVELOPMENT=1 "
                "only if you accept the control plane editing itself while it runs."
            )
        repository = owner_repository or _git_repository(project_context(root).code_root)
        if repository is None:
            repository = _initialize_project_repository(root)
        baseline = _git_task_baseline(repository or root)
        agent.update({"task": task, "status": "task_defined", "status_note": "owner direction translated into internal task", "last_status_at": now()})
        workspace: Path | None = None
        branch_record: dict[str, Any] = {}
        if repository is not None:
            state.setdefault("task_repositories", {})[task] = str(repository)
            broker = _broker_for_state(root, state, task)
            branch_record = broker.branch_create(
                agent_id, _next_broker_nonce(state, str(agent.get("session_id") or agent_id)),
            )
            workspace = Path(branch_record["workspace"])
            state.setdefault("task_branches", {})[task] = branch_record
        else:
            workspace = _ensure_task_workspace(root, task)
        if agent.get("session_id"):
            direction["consumed"] = True
        for finding in state.get("deferred_findings", {}).values():
            if finding.get("status") == "fix_in_progress" and finding.get("assigned_agent_id") == agent_id and not finding.get("follow_up_task"):
                finding["follow_up_task"] = task
                finding["next_action"] = "The approved follow-up is active in Delivery and will use the normal QA, independent-review, and release gates."
                break
        pending_clarifications = state.setdefault("pending_owner_clarifications", {}).pop(agent.get("session_id", ""), [])
        if pending_clarifications:
            for clarification in pending_clarifications:
                clarification["task"] = task
            state.setdefault("owner_clarifications", {}).setdefault(task, []).extend(pending_clarifications)
        state.setdefault("task_baselines", {})[task] = baseline
        state.setdefault("task_owner_directions", {})[task] = contract.normalize_owner_direction(direction_text)
        if workspace:
            state.setdefault("task_workspaces", {})[task] = str(workspace)
        inherited_count = len(baseline.get("dirty_files", []))
        binding = "owner-named Git repository on an isolated broker branch" if owner_repository else "isolated broker task worktree"
        message = f"Product Management defined the internal task in its {binding}; captured {inherited_count} inherited dirty files for internal attribution; owner action is not required"
        return _event(state, "task_begun", agent, {"task": task, "message": message, "owner_direction": direction_text, "inherited_dirty_files": inherited_count, "task_workspace": str(workspace) if workspace else "", "task_branch": branch_record.get("branch", ""), "repository": str(repository) if repository else "", "repository_binding": binding})


def resume_task(root: Path, agent_id: str, source_agent_id: str, task: str) -> dict[str, Any]:
    """Attach a replacement visible Delivery session to preserved task memory."""
    task = task.strip()
    recovery_instruction = ""
    session_id = ""
    source_session_id = ""
    with locked_state(root) as state:
        agent = _require_writable_agent(state, agent_id)
        source = _require_agent(state, source_agent_id)
        if agent["role"] not in DEVELOPER_ROLES or agent["task"] != AWAITING_OWNER_DIRECTION:
            raise ValueError("replacement must be a standing-by Delivery Agent")
        if source["role"] not in DEVELOPER_ROLES or source["task"] != task:
            raise ValueError("source Delivery Agent does not own the requested task")
        if source.get("active"):
            raise ValueError("source Delivery Agent is still active; recovery would create two task owners")
        direction = owner_direction_for_task(state, source_agent_id, task)
        if not direction:
            raise ValueError("cannot resume a task without its preserved owner direction")
        brief = state.get("task_briefs", {}).get(task, {})
        next_action = brief.get("update") or "Poll the board and resume the preserved task from its latest review or release gate."
        resumed_at = now()
        agent.update({
            "task": task,
            "status": "recovered",
            "status_note": "replacement session resumed preserved task memory",
            "last_status_at": now(),
            "recovery_context": {
                "source_agent_id": source_agent_id,
                "owner_direction": direction,
                "previous_status": source.get("status", "offline"),
                "previous_status_note": source.get("status_note", ""),
                "next_action": next_action,
                "resumed_at": resumed_at,
            },
            "write_authority": True,
        })
        source.update({
            "write_authority": False,
            "superseded_by_agent_id": agent_id,
            "superseded_by_session_id": str(agent.get("session_id") or ""),
            "superseded_at": resumed_at,
            "status": "superseded",
            "status_note": f"read-only predecessor; superseded by {agent_id}",
            "liveness": "offline",
            "liveness_note": f"superseded by {agent_id}; predecessor terminal is stopping",
            "active": False,
        })
        for request in state.get("qa_requests", {}).values():
            if (
                request.get("task") == task
                and request.get("developer_id") == source_agent_id
                and request.get("status") not in TERMINAL_QA
            ):
                request.setdefault("developer_lineage", []).append({
                    "source_agent_id": source_agent_id,
                    "agent_id": agent_id,
                    "at": resumed_at,
                })
                request["developer_id"] = agent_id
        state.setdefault("task_lineage", {}).setdefault(task, []).append({
            "source_agent_id": source_agent_id,
            "agent_id": agent_id,
            "at": resumed_at,
        })
        session_id = str(agent.get("session_id") or "")
        source_session_id = str(source.get("session_id") or "")
        recovery_instruction = f"Resume {task}. Preserved next action: {next_action} Owner action is not required."
        event = _event(state, "task_resumed", agent, {"task": task, "source_agent_id": source_agent_id, "message": "replacement Delivery Agent resumed preserved task memory; owner action is not required", "next_action": next_action})
    if session_id:
        try:
            from harness import control
            control.enqueue_instruction(root, session_id, recovery_instruction, source="owner-authorized-recovery")
        except ValueError:
            # The durable recovery context remains authoritative if a visible
            # replacement exits between registration and task resumption.
            pass
    if source_session_id and session_id:
        try:
            from harness import control
            control.supersede(root, source_session_id, session_id, agent_id, task)
        except (ValueError, OSError):
            # Task authority was already transferred durably. Even if the
            # predecessor process exits between those steps, its board record
            # remains read-only and cannot mutate the recovered task.
            pass
    return event


def reconcile_inherited_baseline(root: Path, agent_id: str) -> dict[str, Any]:
    """Recover attribution for a task begun before baseline capture existed."""
    baseline = _git_task_baseline(root, mode="recovered_after_task_start")
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        if developer["role"] not in DEVELOPER_ROLES or developer["task"] == AWAITING_OWNER_DIRECTION:
            raise ValueError("only an active Delivery task may reconcile an inherited baseline")
        if state.setdefault("task_baselines", {}).get(developer["task"]):
            raise ValueError("task baseline is already recorded and immutable")
        state["task_baselines"][developer["task"]] = baseline
        count = len(baseline.get("dirty_files", []))
        return _event(state, "inherited_baseline_reconciled", developer, {
            "task": developer["task"],
            "message": f"recovered {count} inherited dirty files for internal attribution and review; owner action is not required",
            "inherited_dirty_files": count,
        })


def _task_reserves_repository(state: dict[str, Any], task: str) -> bool:
    """Whether a task's recorded workspace still counts as an active reservation.

    Bindings are never deleted — history and evidence projections must keep
    resolving them — but a concluded task (owner-accepted release, or
    cancelled) no longer reserves its repository against new work. Without
    this, every finished task blocked the next task on the same repository
    forever and the CTO had to release bindings by hand.
    """
    decision = (state.get("release_decisions", {}).get(task) or {}).get("decision", "")
    if decision == "accepted":
        return False
    if task in (state.get("cancelled_tasks") or {}):
        return False
    return True


def attach_task_workspace(root: Path, agent_id: str, workspace_value: str) -> dict[str, Any]:
    """Attach an already-retained task worktree through a durable board event."""
    if not workspace_value.strip():
        raise ValueError("workspace path is required")
    context = project_context(root)
    code_root = context.code_root
    workspace = Path(workspace_value).expanduser().resolve()
    with locked_state(root) as state:
        agent = _require_writable_agent(state, agent_id)
        if agent["role"] not in DEVELOPER_ROLES or agent["task"] == AWAITING_OWNER_DIRECTION:
            raise ValueError("only an active Delivery Agent may attach a task workspace")
        task = agent["task"]
        if not workspace.is_dir():
            raise ValueError(f"task workspace does not exist: {workspace}")
        allowed_parent = context.workspace_root
        if allowed_parent not in workspace.parents:
            raise ValueError("task workspace must be inside the retained .harness-task-workspaces directory")
        probe = git_process.run(["git", "rev-parse", "--show-toplevel"], cwd=workspace, capture_output=True, text=True)
        common = git_process.run(["git", "rev-parse", "--git-common-dir"], cwd=workspace, capture_output=True, text=True)
        root_common = git_process.run(["git", "rev-parse", "--git-common-dir"], cwd=code_root, capture_output=True, text=True)
        if probe.returncode != 0 or common.returncode != 0 or root_common.returncode != 0:
            raise ValueError("task workspace must be a Git worktree")

        def resolved_common(repo: Path, value: str) -> Path:
            path = Path(value.strip())
            return (path if path.is_absolute() else repo / path).resolve()

        if resolved_common(workspace, common.stdout) != resolved_common(code_root, root_common.stdout):
            raise ValueError("task workspace belongs to a different Git repository")
        existing = state.setdefault("task_workspaces", {}).get(task, "")
        if existing and Path(existing).resolve() != workspace:
            raise ValueError("task already has a different isolated workspace attached")
        for other_task, other_workspace in state.setdefault("task_workspaces", {}).items():
            if (other_task != task and other_workspace
                    and _task_reserves_repository(state, other_task)
                    and Path(other_workspace).resolve() == workspace):
                raise ValueError(f"workspace is already attached to task {other_task}")
        state["task_workspaces"][task] = str(workspace)
        return _event(state, "task_workspace_attached", agent, {
            "task": task,
            "workspace": str(workspace),
            "message": "existing retained task workspace attached through the board; review and QA will execute there",
        })


def bind_task_repository(root: Path, agent_id: str, repository_value: str, baseline_revision: str = "") -> dict[str, Any]:
    """Bind an active task to the exact external Git repository it delivers.

    This is the explicit recovery path when an owner names more than one
    repository or a legacy task began before automatic repository discovery.
    It is permitted only before any review request exists, so already-certified
    evidence can never be silently rebound to another artifact.
    """
    context = project_context(root)
    if repository_value.strip():
        repository = _git_repository(Path(repository_value).expanduser())
    else:
        # The authenticated surface strips client paths: a Delivery Agent
        # binding without an argument gets exactly the project's own folder,
        # initialized if needed. Containment holds - no arbitrary path ever
        # crosses this boundary from an agent.
        repository = _git_repository(context.code_root) or _initialize_project_repository(root)
    if not repository:
        raise ValueError("task repository must be an existing Git worktree")
    # SELF-DEVELOPMENT FREEZE applies to re-binding exactly as to begin_task:
    # without this, a task begun against a safe repository could be re-pointed
    # at the harness's own repository, bypassing the begin-time guard (row 2).
    if _is_harness_self_target(repository) and os.environ.get("HARNESS_ALLOW_SELF_DEVELOPMENT") != "1":
        raise ValueError(
            "refusing to bind a governed task to the harness's own repository: "
            "fix the harness directly, outside the harness, or target a different "
            "repository. Set HARNESS_ALLOW_SELF_DEVELOPMENT=1 only if you accept "
            "the control plane editing itself while it runs."
        )
    baseline_revision = baseline_revision.strip()
    baseline_commit = ""
    if baseline_revision:
        baseline_commit, _ = _git_commit_and_tree(repository, baseline_revision)
        head_commit, _ = _git_commit_and_tree(repository, "HEAD")
        ancestor = git_process.run(
            ["git", "merge-base", "--is-ancestor", baseline_commit, head_commit],
            cwd=repository, capture_output=True, text=True,
        )
        if ancestor.returncode != 0:
            raise ValueError("declared baseline is not an ancestor of the task repository HEAD")
    baseline = _git_task_baseline(repository, mode="task_repository_bound")
    if baseline_commit:
        baseline["candidate_head_at_binding"] = baseline.get("head", "")
        baseline["head"] = baseline_commit
        baseline["declared_baseline_verified"] = True
    with locked_state(root) as state:
        agent = _require_writable_agent(state, agent_id)
        if agent["role"] not in DEVELOPER_ROLES or agent["task"] == AWAITING_OWNER_DIRECTION:
            raise ValueError("only an active Delivery Agent may bind its task repository")
        task = agent["task"]
        if _task_requests(state, task):
            raise ValueError("task repository cannot change after a review request exists")
        if any(
            other_task != task and other_repository
            and _task_reserves_repository(state, other_task)
            and Path(other_repository).resolve() == repository
            for other_task, other_repository in state.setdefault("task_repositories", {}).items()
        ):
            raise ValueError("task repository is already attached to another active task")
        previous_workspace = str(state.setdefault("task_workspaces", {}).get(task, ""))
        state.setdefault("task_repositories", {})[task] = str(repository)
        state.setdefault("task_baselines", {})[task] = baseline
        broker = _broker_for_state(root, state, task)
        branch_record = broker.branch_create(
            agent_id, _next_broker_nonce(state, str(agent.get("session_id") or agent_id)),
        )
        state["task_workspaces"][task] = branch_record["workspace"]
        state.setdefault("task_branches", {})[task] = branch_record
        event = _event(state, "task_repository_bound", agent, {
            "task": task,
            "repository": str(repository),
            "workspace": branch_record["workspace"],
            "branch": branch_record["branch"],
            "previous_workspace_preserved": previous_workspace,
            "baseline_commit": baseline.get("head", ""),
            "message": "task QA, independent review, and release are bound to an isolated broker branch in the exact target Git repository",
        })
    return event


def _task_requests(state: dict[str, Any], task: str) -> list[dict[str, Any]]:
    archived = [
        entry["value"] for entry in state.get("archive", [])
        if entry.get("kind") == "qa_request" and entry.get("value", {}).get("task") == task
    ]
    current = list(state.get("qa_requests", {}).values())
    current_ids = {request.get("id") for request in current}
    archived_ids = {request.get("id") for request in archived}
    indexed = [
        request for request in state.get("qa_request_index", {}).values()
        if request.get("id") not in current_ids | archived_ids
    ]
    return [request for request in current + archived + indexed if request.get("task") == task]


def _abandon_review_intents(
    request: dict[str, Any], reviewer_id: str, reason: str,
) -> None:
    """Keep abandoned authorship auditable without assigning it to a replacement."""
    intents = list(request.get("reviewer_initial_intents") or [])
    amendments = list(request.get("reviewer_intent_amendments") or [])
    if intents or amendments:
        request.setdefault("abandoned_reviewer_intents", []).append({
            "reviewer_id": reviewer_id,
            "reason": reason,
            "abandoned_at": now(),
            "initial": json.loads(json.dumps(intents)),
            "amendments": json.loads(json.dumps(amendments)),
        })
    for field in (
        "reviewer_initial_intents", "reviewer_intents_recorded_at",
        "reviewer_intent_amendments", "reviewer_authoring_overlap_started",
    ):
        request.pop(field, None)


def release_expired_review_reservations(root: Path, timeout_seconds: int = REVIEW_RESERVATION_SECONDS) -> list[dict[str, Any]]:
    """Re-open an abandoned ledger-preparation reservation exactly once."""
    if timeout_seconds < 1:
        raise ValueError("reservation timeout must be positive")
    current = datetime.now(timezone.utc)
    released = []
    with locked_state(root) as state:
        for request in state.get("qa_requests", {}).values():
            if request.get("status") != "reserved" or not request.get("reserved_at"):
                continue
            reviewer = state.get("agents", {}).get(request.get("reserved_by", ""))
            # The authenticated board poll is the Reviewer liveness proof. Plain
            # terminal output and status prose do not renew authoring ownership.
            activity = [
                request.get("reserved_at"), request.get("authoring_last_activity_at"),
                (reviewer or {}).get("last_poll_at"),
            ]
            parsed_activity = [
                parsed for value in activity
                if (parsed := _parsed_timestamp(value)) is not None
            ]
            # Corrupt reservation timing is not a perpetual lock. Fail closed to
            # expiry so another independent reviewer can recover the request.
            last_activity = max(parsed_activity) if parsed_activity else None
            age = (
                (current - last_activity).total_seconds()
                if last_activity is not None else float(timeout_seconds)
            )
            if age < timeout_seconds:
                continue
            previous_reviewer = request.get("reserved_by", "")
            _abandon_review_intents(request, str(previous_reviewer or ""), "reservation expired")
            request.update({
                "status": "authoring" if request.get("delivery_state") == "executing" else "open",
                "reserved_by": None, "reserved_at": None,
                "authoring_last_activity_at": None,
                "routed_to": None, "routed_session_id": "", "routed_at": None,
                "route_state": "reservation_expired_reopened",
            })
            if reviewer and reviewer.get("active"):
                reviewer.update({"task": "REVIEW_QUEUE", "status": "review_queue", "status_note": "review reservation expired before a valid Challenge Ledger was attached", "last_status_at": now()})
            event = _event(state, "qa_reservation_expired", reviewer, {
                "task": request["task"], "request_id": request["id"],
                "previous_reviewer": previous_reviewer, "age_seconds": int(age),
                "message": "review reservation expired without a valid Challenge Ledger and was reopened for another reviewer",
            })
            released.append(event)
    return released


def route_open_reviews(root: Path, retry_seconds: int = REVIEW_ROUTE_RETRY_SECONDS) -> list[dict[str, Any]]:
    """Wake exactly one eligible managed reviewer for each open request.

    Review intake must not depend on an AI holding an infinite polling tool
    call open. Review creation and reviewer registration call it immediately.
    A durable ``routed_to`` assignment prevents
    duplicate reviewers; an overdue route is re-sent to the same live terminal.
    """
    from harness import control

    release_expired_review_reservations(root)

    managed = control.snapshot(root)
    active_sessions = {
        session["id"]: session
        for session in managed.get("sessions", [])
        if session.get("status") in control.ACTIVE_STATUSES
    }
    current = datetime.now(timezone.utc)
    deliveries: list[dict[str, Any]] = []
    with locked_state(root) as state:
        open_requests = sorted(
            (request for request in state.get("qa_requests", {}).values() if request.get("status") in {"authoring", "open"}),
            key=lambda request: (request.get("requested_at", ""), request.get("id", "")),
        )
        reserved = {
            request.get("routed_to")
            for request in open_requests
            if request.get("routed_to")
        }
        held_reviewers = {
            reviewer_id
            for held in state.get("qa_requests", {}).values()
            if held.get("status") in {"reserved", "claimed"}
            for reviewer_id in (held.get("reserved_by"), held.get("claimed_by"))
            if reviewer_id
        }
        reserved.update(held_reviewers)
        for request in open_requests:
            developer = state.get("agents", {}).get(request.get("developer_id"), {})
            developer_vendor = developer.get("vendor", "")
            routed_id = request.get("routed_to", "")
            routed = state.get("agents", {}).get(routed_id) if routed_id else None
            routed_session = active_sessions.get(routed.get("session_id", "")) if routed else None
            routed_is_eligible = bool(
                routed
                and routed.get("active")
                and routed.get("role") == "qa"
                and routed_session
                and routed.get("id") not in held_reviewers
                and routed.get("task") in {"REVIEW_QUEUE", request.get("task")}
                and (
                    request.get("stage") != INDEPENDENT_REVIEW
                    or (routed.get("vendor") and routed.get("vendor") != developer_vendor)
                )
            )
            routed_at = request.get("routed_at")
            reviewer_progress_at = routed.get("last_status_at") if routed_is_eligible else None
            route_clock_values = [value for value in (routed_at, reviewer_progress_at) if value]
            route_clock = max(route_clock_values, key=lambda value: datetime.fromisoformat(value)) if route_clock_values else None
            route_age = (
                (current - datetime.fromisoformat(route_clock)).total_seconds()
                if route_clock else float("inf")
            )
            if routed_is_eligible and route_age < retry_seconds:
                continue

            reviewer = routed if routed_is_eligible else None
            if reviewer is None:
                candidates = []
                for candidate in state.get("agents", {}).values():
                    session_id = candidate.get("session_id", "")
                    if not candidate.get("active") or candidate.get("role") != "qa" or session_id not in active_sessions:
                        continue
                    if candidate.get("task") not in {"REVIEW_QUEUE", request.get("task")}:
                        continue
                    if request.get("stage") == INDEPENDENT_REVIEW and (
                        not candidate.get("vendor") or candidate.get("vendor") == developer_vendor
                    ):
                        continue
                    if candidate.get("id") in reserved:
                        continue
                    if any(
                        other.get("status") == "claimed" and other.get("claimed_by") == candidate.get("id")
                        for other in state.get("qa_requests", {}).values()
                    ):
                        continue
                    candidates.append(candidate)
                if not candidates:
                    request["route_state"] = "waiting_for_eligible_reviewer"
                    if not state.get("reviewer_needed"):
                        state["reviewer_needed"] = {
                            "requested_at": now(),
                            "request_id": request.get("id", ""),
                        }
                        _event(state, "reviewer_needed", None, {
                            "task": request.get("task", ""),
                            "request_id": request.get("id", ""),
                            "message": "open review queue has no live eligible reviewer; start one from Mission Control",
                        })
                    continue
                reviewer = min(candidates, key=lambda item: (item.get("spawned_at", ""), item.get("id", "")))
                reserved.add(reviewer["id"])

            routed_time = now()
            request.update({
                "routed_to": reviewer["id"],
                "routed_session_id": reviewer.get("session_id", ""),
                "routed_at": routed_time,
                "route_attempts": int(request.get("route_attempts", 0)) + 1,
                "route_state": "routed",
            })
            state["reviewer_needed"] = None
            if reviewer.get("status") in {"spawned", "waiting", "review_routed"}:
                reviewer.update({
                    "task": request["task"],
                    "status": "review_routed",
                    "status_note": f"review request {request['id']} routed; claim and execute it",
                    "last_status_at": routed_time,
                })
            _event(state, "review_routed", reviewer, {
                "task": request["task"],
                "request_id": request["id"],
                "message": "eligible reviewer was actively notified; one bounded poll, claim, execution, and verdict are required",
                "route_attempt": request["route_attempts"],
            })
            deliveries.append({
                "agent_id": reviewer["id"],
                "session_id": reviewer.get("session_id", ""),
                "request_id": request["id"],
                "task": request["task"],
                "reviewed_commit": request.get("reviewed_commit", ""),
                "delivery_state": request.get("delivery_state", "passed"),
                "route_attempt": request["route_attempts"],
            })

    for delivery in deliveries:
        if delivery["delivery_state"] == "executing":
            instruction = (
                f"REVIEW AUTHORING ROUTED: {delivery['request_id']} for task {delivery['task']}. "
                f"Candidate: {delivery['reviewed_commit'] or 'recorded board candidate'}. "
                "Run one bounded board poll, reserve this exact request, read its bounded authoring brief, "
                "and record your independent review intentions before Delivery evidence is revealed. You may "
                "author the distinct Challenge Ledger now, but do not attach it, execute review commands, or "
                "post a verdict until the board records Delivery success. Do not start or continue any polling, "
                "watch, sleep, caffeinate, or background helper. USER ACTION: None."
            )
        else:
            instruction = (
                f"REVIEW REQUEST ROUTED: {delivery['request_id']} for task {delivery['task']}. "
                f"Candidate: {delivery['reviewed_commit'] or 'recorded board candidate'}. "
                "Do not start or continue any polling, watch, sleep, caffeinate, or background helper. "
                "Run one bounded board poll now, author the distinct Challenge Ledger, claim this exact request, "
                "execute the simulations, and post the formal PASS or FAIL result. If ledger preparation exceeds "
                "one minute, post a short board status update before continuing. USER ACTION: None."
            )
        try:
            queued = control.enqueue_instruction(
                root, delivery["session_id"], instruction, source="review-assignment",
            )
            delivery["delivered"] = True
            delivery["instruction_id"] = queued["id"]
        except (ValueError, OSError):
            # The durable route remains overdue and will be retried or assigned
            # to another eligible live reviewer on the next controller pass.
            delivery["delivered"] = False
    if deliveries:
        with locked_state(root) as state:
            for delivery in deliveries:
                request = state.get("qa_requests", {}).get(delivery["request_id"])
                reviewer = state.get("agents", {}).get(delivery["agent_id"])
                if not request:
                    continue
                request["route_transport_state"] = (
                    "instruction_queued" if delivery["delivered"] else "instruction_failed"
                )
                request["route_instruction_id"] = delivery.get("instruction_id", "")
                _event(state, "instruction_route_queued" if delivery["delivered"] else "instruction_route_failed", reviewer, {
                    "task": delivery["task"],
                    "request_id": delivery["request_id"],
                    "instruction_id": delivery.get("instruction_id", ""),
                    "message": (
                        "review wake is durably queued for supervisor delivery"
                        if delivery["delivered"] else
                        "review wake could not be queued; durable route remains eligible for recovery"
                    ),
                })
    return deliveries


def _ledger_path(root: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else project_context(root).code_root / path).resolve()


def _require_ledger_scenarios(
    root: Path, value: str, label: str, *, owner_readable: bool = False,
) -> Path:
    path = _ledger_path(root, value)
    validator = contract.scenario_submission_exists if owner_readable else contract.scenario_ledger_exists
    valid, problems = validator(path)
    if not valid:
        raise ValueError(f"{label} must exist and contain concrete scenarios: " + "; ".join(problems))
    return path


def _require_completed_ledger(
    root: Path, value: str, label: str, *, owner_readable: bool = False,
) -> None:
    validator = contract.scenario_submission_complete if owner_readable else contract.scenario_ledger_complete
    valid, problems = validator(_ledger_path(root, value))
    if not valid:
        raise ValueError(f"cannot record PASS: {label} is incomplete: " + "; ".join(problems))


def _require_evidence_file(root: Path, value: str, label: str) -> str:
    path = _ledger_path(root, value)
    if not value.strip() or not path.is_file():
        raise ValueError(f"{label} file is missing: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    command_line = re.compile(r"^\s*(?:command:\s*)?(?:\$\s*)?(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:python(?:3)?\s+-m\s+|pytest\b|npm\s+(?:test|run)\b|curl\b|git\s+|test\b)", re.I)
    result_line = re.compile(r"^\s*(?:(?:result|status|exit(?:\s+code)?)\s*:\s*(?:pass(?:ed)?|ok|success|fail(?:ed)?|0)\b|(?:ok|failed)\b|ran\s+\d+\s+tests?\b)", re.I)
    has_command = any(command_line.match(line) for line in lines)
    has_result = any(result_line.match(line) for line in lines)
    if len(text.strip()) < 20 or not has_command or not has_result:
        raise ValueError(f"{label} must record a non-empty executed command and result")
    return str(path)


def _execute_internal_qa(
    command: str, root: Path, *, measurement: dict[str, Any] | None = None,
    certification: dict[str, Any] | None = None,
) -> str:
    """Run Delivery's declared test command outside the board lock."""
    if not command.strip():
        raise ValueError("an internal-QA --test-command is required")
    if contract.SHELL_CONTROL.search(command):
        raise ValueError("internal-QA test command must not contain shell control operators")
    if not re.search(r"(?:^|\s)(?:python(?:3)?\s+-m\s+(?:unittest|pytest)|pytest\b|npm\s+(?:test|run\s+test)|go\s+test|cargo\s+test|make\s+test|gradle\s+test)\b", command):
        raise ValueError("internal-QA test command must invoke a recognized test runner; shell placeholders are not evidence")
    execution_root = project_context(root).code_root
    execution_env = child_process.execution_environment()
    execution_preflight.validate_commands(
        execution_root, [command], environment=execution_env,
    )
    if certification is not None:
        result = certified_execution.run(
            certification["board_root"], root, command,
            candidate=certification["candidate"],
            environment_sha256=str(certification["environment_sha256"]),
            environment=execution_env,
            lockfile_digests=dict(certification.get("lockfile_digests") or {}),
            role=str(certification["role"]), gate=str(certification["gate"]),
            retry_reason=str(certification.get("retry_reason") or ""),
            browser=certification.get("browser"),
        )
        if measurement is not None:
            measurement.update(result["measurement"])
        return str(result["output"])
    started_at = lifecycle.now()
    try:
        completed = subprocess.run(command, cwd=execution_root, shell=True, capture_output=True, text=True, timeout=300, env=execution_env)
    except subprocess.TimeoutExpired as error:
        if measurement is not None:
            measurement.update(lifecycle.command_measurement(
                command, started_at, lifecycle.now(), exit_code=-1,
                cache_decision="executed_no_cache_store",
            ))
        raise ValueError(f"internal-QA test command timed out after 300 seconds: {error.cmd}") from error
    if measurement is not None:
        measurement.update(lifecycle.command_measurement(
            command, started_at, lifecycle.now(), exit_code=completed.returncode,
            cache_decision="executed_no_cache_store",
        ))
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        raise ValueError(f"internal-QA test command failed with exit code {completed.returncode}: {output[-500:]}")
    counts = [int(value) for pair in re.findall(r"\bRan\s+(\d+)\s+tests?\b|\b(\d+)\s+passed\b", output, re.I) for value in pair if value]
    if counts and max(counts) == 0:
        raise ValueError("internal-QA test command reported zero executed tests")
    if not counts:
        raise ValueError("internal-QA output must report a positive executed-test count")
    return output


def _execute_scenario_simulations(
    root: Path, ledger: Path, *, certification: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute distinct commands and attribute results to every scenario row."""
    valid, problems, scenarios = contract.scenario_simulations(ledger)
    if not valid:
        raise ValueError("Scenario Ledger simulation plan is invalid: " + "; ".join(problems))
    execution_preflight.validate_commands(
        project_context(root).code_root,
        [
            scenario["command"] for scenario in scenarios
            if not scenario["result"].upper().startswith(("N/A:", "DEFERRED:"))
        ], environment=child_process.execution_environment(),
    )
    results = []
    failures = []
    command_results: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        result = scenario["result"].upper()
        if result.startswith(("N/A:", "DEFERRED:")):
            failures.append(f"{scenario['id']}: an approved exception cannot replace an executed review simulation")
            results.append({
                "id": scenario["id"], "command": scenario["command"],
                "expected_response": scenario["expected_response"],
                "what_was_tested": scenario.get("what_was_tested", ""),
                "outcome": "failed", "output": result,
            })
            continue
        prior = command_results.get(scenario["command"])
        if prior is not None:
            deduplicated_at = lifecycle.now()
            results.append({
                "id": scenario["id"], "command": scenario["command"],
                "expected_response": scenario["expected_response"],
                "what_was_tested": scenario.get("what_was_tested", ""),
                "outcome": prior["outcome"], "output": prior["output"],
                "deduplicated_from": prior["id"],
                "command_fingerprint": prior.get("command_fingerprint", ""),
                "started_at": deduplicated_at,
                "finished_at": deduplicated_at,
                "duration_seconds": 0.0,
                "exit_code": prior.get("exit_code", 0),
                "cache_decision": "same_request_deduplicated",
            })
            if prior["outcome"] == "failed":
                failures.append(f"{scenario['id']}: shared command failed (see {prior['id']})")
            continue
        measurement: dict[str, Any] = {}
        try:
            output = _execute_internal_qa(
                scenario["command"], root, measurement=measurement,
                certification=certification,
            )
        except ValueError as error:
            output = str(error)
            failures.append(f"{scenario['id']}: {error}")
        row = {
            "id": scenario["id"], "command": scenario["command"],
            "expected_response": scenario["expected_response"],
            "what_was_tested": scenario.get("what_was_tested", ""),
            "outcome": "failed" if failures and failures[-1].startswith(f"{scenario['id']}:") else "passed",
            "output": output,
            **measurement,
        }
        command_results[scenario["command"]] = row
        results.append(row)
    if failures:
        raise ValueError("Scenario Ledger simulations failed: " + " | ".join(failures))
    return results


@contextmanager
def _review_candidate_checkout(root: Path, state: dict[str, Any], request: dict[str, Any], source_path: Path) -> Iterator[tuple[Path, Path]]:
    """Run independent scenarios against the immutable reviewed commit.

    Reviewer ledgers remain durable authoring evidence under the board, so they
    are copied into the disposable checkout without becoming candidate files.
    """
    code_root = project_context(root).code_root
    task = request.get("task", "")
    subtask = request.get("subtask", "")
    workspace_value = (
        state.get("subtask_workspaces", {}).get(task, {}).get(subtask, "")
        if subtask else ""
    ) or state.get("task_workspaces", {}).get(task, "")
    workspace = Path(workspace_value) if workspace_value and Path(workspace_value).is_dir() else code_root
    commit = str(request.get("reviewed_commit", ""))
    if not commit:
        if workspace != code_root and request.get("stage") == INDEPENDENT_REVIEW:
            expected_digest = str(request.get("reviewed_worktree_digest", ""))
            current = _git_review_artifact(workspace)
            if not expected_digest:
                raise ValueError("dirty task workspace review is missing its reviewed_worktree_digest")
            if current.get("working_tree_digest") != expected_digest:
                raise ValueError("dirty task workspace changed after review request; request a fresh review cycle")
        yield workspace, source_path
        return
    probe = git_process.run(["git", "rev-parse", "--show-toplevel"], cwd=workspace, capture_output=True, text=True)
    if probe.returncode != 0:
        raise ValueError("reviewed task workspace is not a valid Git worktree")
    with tempfile.TemporaryDirectory(prefix="harness-review-candidate-") as temporary:
        temporary_root = Path(temporary)
        archive_path = temporary_root / "candidate.tar"
        archived = git_process.run(["git", "archive", "--format=tar", "--output", str(archive_path), commit], cwd=workspace, capture_output=True, text=True)
        if archived.returncode != 0:
            raise ValueError("reviewed candidate commit could not be materialized: " + (archived.stderr.strip() or archived.stdout.strip()))
        checkout = temporary_root / "checkout"
        checkout.mkdir()
        with tarfile.open(archive_path, "r") as stream:
            stream.extractall(checkout)
        try:
            relative = source_path.resolve().relative_to(workspace.resolve())
        except ValueError:
            try:
                relative = source_path.resolve().relative_to(code_root)
            except ValueError:
                relative = Path(source_path.name)
        candidate_ledger = checkout / relative
        candidate_ledger.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, candidate_ledger)
        yield checkout, candidate_ledger


def _store_internal_qa_evidence(root: Path, command: str, output: str, simulations: list[dict[str, Any]] | None = None) -> str:
    """Persist aggregate QA plus already-executed scenario results after acceptance."""
    directory = board_dir(root) / "evidence"
    directory.mkdir(parents=True, exist_ok=True)
    name = f"internal-qa-{secrets.token_hex(6)}.txt"
    path = directory / name
    sections = [f"command: {command}\nexit code: 0\nresult: PASS\n\n{output}\n"]
    for simulation in simulations or []:
        dedup_note = f"deduplicated from scenario: {simulation['deduplicated_from']}\n" if simulation.get("deduplicated_from") else ""
        sections.append(
            "\n"
            f"scenario: {simulation['id']}\n"
            f"what was tested: {simulation.get('what_was_tested', '')}\n"
            f"command: {simulation['command']}\n"
            f"expected system response: {simulation['expected_response']}\n"
            f"result: {simulation['outcome'].upper()}\n\n"
            f"{dedup_note}"
            f"{simulation['output']}\n"
        )
    path.write_text("".join(sections), encoding="utf-8")
    return str(path)


def _simulation_bundle(root: Path, label: str, simulations: list[dict[str, Any]]) -> dict[str, Any]:
    evidence = _store_internal_qa_evidence(
        root, f"{label} per-scenario commands",
        f"Executed {len(simulations)} scenario simulations.", simulations,
    )
    return {
        "scenario_ids": [item["id"] for item in simulations],
        "executed_count": sum(item["outcome"] == "passed" for item in simulations),
        "approved_exception_ids": [],
        "evidence": evidence,
        "evidence_sha256": hashlib.sha256(Path(evidence).read_bytes()).hexdigest(),
    }


def _certify_payload(root: Path, payload: bytes, source_path: str) -> dict[str, str]:
    """Store already-verified bytes in immutable content-addressed storage."""
    digest = hashlib.sha256(payload).hexdigest()
    destination = board_dir(root) / "certified" / digest
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes() != payload:
        raise ValueError(f"certified evidence hash collision or tampering: {destination}")
    if not destination.exists():
        destination.write_bytes(payload)
    return {"path": str(destination), "sha256": digest, "source_path": source_path}


def _certify_file(root: Path, source: Path) -> dict[str, str]:
    """Copy verified bytes to immutable content-addressed board storage."""
    source = source.resolve()
    if not source.is_file():
        raise ValueError(f"cannot certify missing evidence: {source}")
    return _certify_payload(root, source.read_bytes(), str(source))


def _certify_request_artifacts(root: Path, request: dict[str, Any], result_evidence: Path, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Certify every ledger/evidence byte that supports a settled result."""
    artifacts: dict[str, Any] = {}
    task = request.get("task", "")
    code_root = project_context(root).code_root
    workspace_value = (state or {}).get("task_workspaces", {}).get(task, "")
    workspace = Path(workspace_value) if workspace_value and Path(workspace_value).is_dir() else code_root
    ledger_value = Path(str(request.get("ledger", "")))
    delivery_ledger = ledger_value if ledger_value.is_absolute() else ((workspace / ledger_value) if (workspace / ledger_value).exists() else (code_root / ledger_value))
    artifacts["delivery_ledger"] = _certify_file(root, delivery_ledger)
    challenge_value = str(request.get("challenge_ledger", ""))
    if challenge_value:
        challenge_path = Path(challenge_value)
        if not challenge_path.is_absolute():
            challenge_path = (workspace / challenge_path) if (workspace / challenge_path).exists() else (code_root / challenge_path)
        artifacts["challenge_ledger"] = _certify_file(root, challenge_path)
    artifacts["result_evidence"] = _certify_file(root, result_evidence)
    if request.get("delivery_evidence"):
        artifacts["delivery_evidence"] = _certify_file(root, Path(request["delivery_evidence"]))
    for field, bundle in (("delivery_simulations", request.get("delivery_simulations")), ("qa_simulations", request.get("qa_simulations")), ("reviewer_simulations", request.get("reviewer_simulations"))):
        if isinstance(bundle, dict) and bundle.get("evidence"):
            certified = _certify_file(root, Path(str(bundle["evidence"])))
            bundle["certified_evidence"] = certified["path"]
            bundle["certified_evidence_sha256"] = certified["sha256"]
            artifacts[f"{field}_evidence"] = certified
    return artifacts


def _certify_failed_request_artifacts(
    root: Path,
    request: dict[str, Any],
    result_evidence: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Freeze exact failed-attempt bytes without promoting its verdict."""
    delivery = _task_path_from_state(
        root, state, request.get("task", ""), str(request.get("ledger", "")),
    )
    delivery_digest = hashlib.sha256(delivery.read_bytes()).hexdigest()
    expected_delivery = str(request.get("ledger_sha256") or "")
    if expected_delivery and delivery_digest != expected_delivery:
        raise ValueError(
            "Delivery Scenario Ledger changed before the failed verdict; "
            "the failed attempt cannot be recorded against different bytes"
        )
    request["ledger_sha256"] = delivery_digest

    challenge_value = str(request.get("challenge_ledger") or "")
    if challenge_value:
        challenge = _task_path_from_state(
            root, state, request.get("task", ""), challenge_value,
        )
        challenge_digest = hashlib.sha256(challenge.read_bytes()).hexdigest()
        expected_challenge = str(request.get("challenge_ledger_sha256") or "")
        if expected_challenge and challenge_digest != expected_challenge:
            raise ValueError(
                "Reviewer Challenge Ledger changed before the failed verdict; "
                "the failed attempt cannot be recorded against different bytes"
            )
        request["challenge_ledger_sha256"] = challenge_digest
        execution = request.get("challenge_execution") or {}
        if execution.get("ledger_sha256") == challenge_digest and isinstance(
            execution.get("bundle"), dict,
        ):
            request["reviewer_simulations"] = execution["bundle"]

    return _certify_request_artifacts(root, request, result_evidence, state)


def backfill_legacy_integrity(root: Path) -> dict[str, Any]:
    """Certify intact legacy PASSes before the new integrity invalidation rule."""
    backfilled = []
    restored = []
    with locked_state(root) as state:
        if state.get("integrity_migration_complete"):
            return {"backfilled": [], "restored": []}
        for request in list(state.get("qa_requests", {}).values()) + [entry["value"] for entry in state.get("archive", []) if entry.get("kind") == "qa_request"]:
            if request.get("integrity_reconciliation_hold"):
                continue
            legacy_failed = request.get("status") == "failed" and request.get("integrity_invalidated")
            if request.get("status") != "passed" and not legacy_failed:
                continue
            if request.get("status") == "passed" and request.get("certified_artifacts"):
                continue
            try:
                delivery = _task_path_from_state(root, state, request.get("task", ""), str(request.get("ledger", "")))
                if not delivery.is_file() or (request.get("ledger_sha256") and hashlib.sha256(delivery.read_bytes()).hexdigest() != request["ledger_sha256"]):
                    continue
                challenge_value = request.get("challenge_ledger", "")
                if challenge_value:
                    challenge = _task_path_from_state(root, state, request.get("task", ""), str(challenge_value))
                    if not challenge.is_file() or (request.get("challenge_ledger_sha256") and hashlib.sha256(challenge.read_bytes()).hexdigest() != request["challenge_ledger_sha256"]):
                        continue
                result_evidence = Path(str(request.get("evidence", "")))
                if not result_evidence.is_file():
                    continue
                existing = request.get("certified_artifacts")
                if existing:
                    # A prior safety-run may have copied intact legacy bytes before
                    # discovering that old records did not carry a challenge digest.
                    # Reuse those immutable bytes only after checking every source
                    # still matches its certified manifest.
                    for source, key in ((delivery, "delivery_ledger"), (result_evidence, "result_evidence")):
                        manifest = existing.get(key, {})
                        if not manifest.get("path") or not Path(manifest["path"]).is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != manifest.get("sha256"):
                            raise ValueError(f"legacy {key} source no longer matches certified bytes")
                    if challenge_value:
                        challenge_manifest = existing.get("challenge_ledger", {})
                        if not challenge_manifest.get("path") or not Path(challenge_manifest["path"]).is_file() or hashlib.sha256(challenge.read_bytes()).hexdigest() != challenge_manifest.get("sha256"):
                            raise ValueError("legacy challenge ledger no longer matches certified bytes")
                    request["certified_artifacts"] = existing
                else:
                    request["certified_artifacts"] = _certify_request_artifacts(root, request, result_evidence, state)
                request["ledger_sha256"] = request.get("ledger_sha256") or request["certified_artifacts"]["delivery_ledger"]["sha256"]
                if request.get("challenge_ledger"):
                    request["challenge_ledger_sha256"] = request.get("challenge_ledger_sha256") or request["certified_artifacts"]["challenge_ledger"]["sha256"]
                if legacy_failed:
                    valid, _ = _request_integrity(root, request)
                    if not valid:
                        continue
                    request.update({"status": "passed", "result": "passed", "integrity_invalidated": False, "result_summary": "Legacy PASS restored after byte-matched certification"})
                    _set_review_scope_status(state, request, "passed")
                    restored.append(request["id"])
                backfilled.append(request["id"])
            except (OSError, ValueError):
                continue
        state["integrity_migration_complete"] = True
        state["integrity_migration_version"] = 1
        if backfilled:
            _event(state, "legacy_passes_certified", None, {"message": f"backfilled {len(backfilled)} intact historical PASS records into certified storage", "request_ids": backfilled})
        if restored:
            _event(state, "legacy_passes_restored", None, {"message": f"restored {len(restored)} legacy PASS records after byte-matched certification", "request_ids": restored})
    return {"backfilled": backfilled, "restored": restored}


def _legacy_ledger_payload(
    root: Path,
    state: dict[str, Any],
    request: dict[str, Any],
    field: str,
    digest_field: str,
) -> tuple[bytes, str]:
    """Recover one legacy ledger only from bytes tied to its reviewed digest."""
    expected = str(request.get(digest_field, "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("reviewed ledger digest is missing or malformed")

    ledger_value = str(request.get(field, "")).strip()
    if not ledger_value:
        raise ValueError("reviewed ledger path is missing")
    current = _task_path_from_state(root, state, str(request.get("task", "")), ledger_value)
    try:
        payload = current.read_bytes()
    except OSError:
        payload = b""
    if payload and hashlib.sha256(payload).hexdigest() == expected:
        return payload, str(current)

    commit = str(request.get("reviewed_commit", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        raise ValueError("exact reviewed commit is missing or malformed")

    context = project_context(root)
    code_root = context.code_root.resolve(strict=False)
    workspace_value = str(state.get("task_workspaces", {}).get(str(request.get("task", "")), ""))
    workspace = Path(workspace_value).resolve(strict=False) if workspace_value else code_root
    raw_path = Path(ledger_value)
    relative_paths: list[PurePosixPath] = []
    if raw_path.is_absolute():
        for base in (workspace, code_root):
            try:
                relative_paths.append(PurePosixPath(raw_path.resolve(strict=False).relative_to(base)))
            except (OSError, ValueError):
                continue
    else:
        relative_paths.append(PurePosixPath(ledger_value))
    safe_paths = []
    for relative in relative_paths:
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or any(part in {".git", ".harness"} for part in relative.parts)
            or any(ord(character) < 32 for character in str(relative))
        ):
            continue
        if relative not in safe_paths:
            safe_paths.append(relative)
    if not safe_paths:
        raise ValueError("reviewed ledger path is outside the governed repository")

    repositories = []
    for candidate in (workspace, code_root):
        if candidate.is_dir() and candidate not in repositories:
            repositories.append(candidate)
    for repository in repositories:
        for relative in safe_paths:
            result = git_process.run(
                ["git", "show", f"{commit}:{relative.as_posix()}"],
                cwd=repository,
                capture_output=True,
            )
            if result.returncode != 0:
                continue
            recovered = bytes(result.stdout)
            if hashlib.sha256(recovered).hexdigest() == expected:
                return recovered, f"git:{commit}:{relative.as_posix()}"
    raise ValueError("exact reviewed ledger bytes are unavailable or mismatched")


def certify_legacy_review_ledgers(root: Path) -> dict[str, Any]:
    """One-time upgrade for legacy history, deliberately outside read paths.

    New review requests certify these bytes when their verdict is recorded. This
    migration covers older failed and superseded attempts too, because owners
    must still be able to read those attempts after the working ledger changes.
    It is called before the viewer starts; dashboard refresh never invokes Git.
    """
    version = 1
    migrated: list[str] = []
    unavailable: list[str] = []
    with locked_state(root) as state:
        prior = state.get("legacy_review_ledger_migration", {})
        if int(prior.get("version", 0)) >= version and prior.get("complete"):
            return {**json.loads(json.dumps(prior)), "already_complete": True}

        cold_requests = _read_cold(root, "qa_requests")
        references: dict[str, list[dict[str, Any]]] = {}
        requests = list(state.get("qa_requests", {}).values())
        requests.extend(
            entry.get("value", {}) for entry in state.get("archive", [])
            if entry.get("kind") == "qa_request" and isinstance(entry.get("value"), dict)
        )
        requests.extend(cold_requests)
        for index, request in enumerate(requests):
            if not isinstance(request, dict):
                continue
            identity = str(request.get("id", "")).strip() or f"legacy-record-{index}"
            references.setdefault(identity, []).append(request)

        for request_id, copies in references.items():
            request = copies[0]
            for artifact, field, digest_field in (
                ("delivery_ledger", "ledger", "ledger_sha256"),
                ("challenge_ledger", "challenge_ledger", "challenge_ledger_sha256"),
            ):
                if not request.get(field):
                    continue
                expected = str(request.get(digest_field, "")).strip().lower()
                existing = request.get("certified_artifacts", {}).get(artifact, {})
                if existing:
                    try:
                        certified_path = Path(str(existing.get("path", "")))
                        intact = (
                            expected
                            and existing.get("sha256") == expected
                            and certified_path.is_file()
                            and hashlib.sha256(certified_path.read_bytes()).hexdigest() == expected
                        )
                    except OSError:
                        intact = False
                    if intact:
                        for copy in copies:
                            copy.setdefault("certified_artifacts", {})[artifact] = json.loads(json.dumps(existing))
                        continue
                    unavailable.append(f"{request_id}:{artifact}:existing certified artifact is invalid")
                    continue
                try:
                    payload, source_path = _legacy_ledger_payload(root, state, request, field, digest_field)
                    manifest = _certify_payload(root, payload, source_path)
                except (OSError, ValueError) as error:
                    unavailable.append(f"{request_id}:{artifact}:{error}")
                    continue
                for copy in copies:
                    copy.setdefault("certified_artifacts", {})[artifact] = json.loads(json.dumps(manifest))
                migrated.append(f"{request_id}:{artifact}")

        _rewrite_cold(root, "qa_requests", cold_requests)
        summary = {
            "version": version,
            "complete": True,
            "completed_at": now(),
            "request_count": len(references),
            "migrated_count": len(migrated),
            "unavailable_count": len(unavailable),
            "migrated": migrated[:100],
            "unavailable": unavailable[:100],
        }
        state["legacy_review_ledger_migration"] = summary
        _event(state, "legacy_review_ledgers_certified", None, {
            "message": (
                f"one-time legacy review-ledger migration certified {len(migrated)} artifacts; "
                f"{len(unavailable)} remained unavailable and fail closed"
            ),
            **summary,
        })
        return json.loads(json.dumps(summary))


def _request_integrity(root: Path, request: dict[str, Any]) -> tuple[bool, list[str]]:
    """Check a passed request only against its certified immutable bytes."""
    problems: list[str] = []
    artifacts = request.get("certified_artifacts")
    if not isinstance(artifacts, dict):
        return False, ["certified evidence snapshot missing"]
    for name, value in artifacts.items():
        if not isinstance(value, dict) or not value.get("path") or not value.get("sha256"):
            problems.append(f"{name} certified manifest is incomplete")
            continue
        path = Path(value["path"])
        if not path.is_file():
            problems.append(f"{name} certified copy is missing")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != value["sha256"]:
            problems.append(f"{name} certified copy was tampered")
    delivery = artifacts.get("delivery_ledger", {})
    if delivery.get("sha256") != request.get("ledger_sha256"):
        problems.append("delivery ledger certified digest does not match the reviewed digest")
    if request.get("challenge_ledger"):
        challenge = artifacts.get("challenge_ledger", {})
        if challenge.get("sha256") != request.get("challenge_ledger_sha256"):
            problems.append("challenge ledger certified digest does not match the reviewed digest")
    for field in ("delivery_simulations", "qa_simulations", "reviewer_simulations"):
        bundle = request.get(field)
        if isinstance(bundle, dict):
            certified = bundle.get("certified_evidence")
            manifest = artifacts.get(f"{field}_evidence", {})
            if not certified or certified != manifest.get("path") or bundle.get("certified_evidence_sha256") != manifest.get("sha256"):
                problems.append(f"{field} certified evidence is missing")
    finalization = request.get("finalization_diff")
    if isinstance(finalization, dict):
        expected = str(finalization.get("sha256") or "")
        payload = {key: value for key, value in finalization.items() if key != "sha256"}
        actual = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if not expected or expected != actual:
            problems.append("finalization diff digest is missing or mismatched")
        classification = request.get("finalization_classification")
        if not isinstance(classification, dict):
            problems.append("finalization classification is missing")
        else:
            if classification.get("decision") != "accepted":
                problems.append("finalization classification is not accepted")
            if classification.get("diff_sha256") != expected:
                problems.append("finalization classification does not match the reviewed diff")
    return not problems, problems


def _environment_identity() -> dict[str, Any]:
    """Return a stable, non-secret identity for the executable QA environment."""
    uname = os.uname()
    execution_environment = child_process.execution_environment()
    relevant_environment = {
        key: execution_environment[key]
        for key in (
            "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
            "PYTHONHASHSEED", "VIRTUAL_ENV", "CONDA_PREFIX", "CI",
        )
        if key in execution_environment
    }
    fields = {
        "system": uname.sysname,
        "release": uname.release,
        "machine": uname.machine,
        "python_implementation": sys.implementation.name,
        "python_version": ".".join(str(value) for value in sys.version_info[:3]),
        "sanitized_environment_sha256": hashlib.sha256(
            json.dumps(relevant_environment, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "environment_policy": "child_process.execution.v2",
    }
    return {
        "fields": fields,
        "sha256": hashlib.sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _execution_lockfile_digests(root: Path) -> dict[str, str]:
    """Hash dependency manifests that can change command behavior."""
    names = (
        "requirements.txt", "requirements-dev.txt", "constraints.txt",
        "poetry.lock", "uv.lock", "Pipfile.lock", "package-lock.json",
        "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "go.sum",
        "gradle.lockfile",
    )
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in names
        if (root / name).is_file()
    }


def _contract_revision(root: ProjectRoot, task: str) -> dict[str, str]:
    """Identify the reviewed Completion Contract SCOPE for one PASS.

    A review certifies the contract's immutable scope - objective,
    deliverables, exclusions. Linking evidence files and marking
    deliverables verified AFTER the pass is bookkeeping about that same
    review, not a change to what was reviewed; it must never void the PASS
    (2026-08-21: a resume did exactly that and relaunched ghost agents).
    The whole-file digest is kept as legacy_sha256 so PASSes saved by
    earlier versions still reconcile.
    """
    path = project_context(root).storage_path("tasks", f"{task}.json")
    if not path.is_file():
        return {"path": str(path), "sha256": "", "legacy_sha256": "", "revision": "missing"}
    payload = path.read_bytes()
    legacy = hashlib.sha256(payload).hexdigest()
    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return {"path": str(path), "sha256": legacy, "legacy_sha256": legacy, "revision": "unreadable"}
    scope = value.get("immutable_scope") if isinstance(value, dict) else None
    if isinstance(scope, dict) and scope:
        canonical = json.dumps(scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {
            "path": str(path),
            "sha256": hashlib.sha256(canonical).hexdigest(),
            "legacy_sha256": legacy,
            "revision": "immutable-scope",
        }
    revision = str(value.get("updated_at", "")) if isinstance(value, dict) else ""
    return {"path": str(path), "sha256": legacy, "legacy_sha256": legacy, "revision": revision}


def _required_reuse_artifacts(request: dict[str, Any]) -> list[str]:
    """List every certified artifact the shape of this PASS promises."""
    required = ["delivery_ledger", "result_evidence"]
    if request.get("challenge_ledger"):
        required.append("challenge_ledger")
    if request.get("delivery_evidence"):
        required.append("delivery_evidence")
    for field in ("delivery_simulations", "qa_simulations", "reviewer_simulations"):
        if isinstance(request.get(field), dict):
            required.append(f"{field}_evidence")
    return list(dict.fromkeys(required))


def _pass_reuse_identity(request: dict[str, Any]) -> dict[str, Any]:
    """Freeze every identity that must still match before a PASS is reused."""
    artifacts = request.get("certified_artifacts", {})
    source_artifacts = {}
    for name in _required_reuse_artifacts(request):
        manifest = artifacts.get(name)
        source_artifacts[name] = {
            "path": str(manifest.get("source_path", "")) if isinstance(manifest, dict) else "",
            "sha256": str(manifest.get("sha256", "")) if isinstance(manifest, dict) else "",
            "complete": bool(
                isinstance(manifest, dict)
                and manifest.get("source_path")
                and manifest.get("sha256")
            ),
        }
    return {
        "version": 3,
        "review_scope": _review_scope_identity(request),
        "candidate": {
            "commit": str(request.get("reviewed_commit") or request.get("reviewed_base_commit", "")),
            "tree_hash": str(request.get("reviewed_tree_hash", "")),
            "working_tree_digest": str(request.get("reviewed_worktree_digest", "")),
            "mode": "commit" if request.get("reviewed_commit") else "working_tree",
        },
        "source_artifacts": source_artifacts,
        "contract_revision": json.loads(json.dumps(request.get("contract_revision", {}))),
        "environment_identity": json.loads(json.dumps(request.get("environment_identity", {}))),
        "finalization": {
            "applicable": bool(request.get("finalization_diff")),
            "diff_sha256": str((request.get("finalization_diff") or {}).get("sha256") or ""),
            "classification": str((request.get("finalization_classification") or {}).get("decision") or ""),
            "classification_diff_sha256": str(
                (request.get("finalization_classification") or {}).get("diff_sha256") or ""
            ),
        },
        "recorded_at": now(),
    }


def _review_scope_identity(request: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "task": str(request.get("task", "")),
        "structure_revision": int(request.get("structure_revision", 0)),
        "phase": str(request.get("phase", "")),
        "subtask": str(request.get("subtask", "")),
        "chunk": str(request.get("chunk", "")),
    }
    return {
        "fields": fields,
        "sha256": hashlib.sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _reuse_check(name: str, expected: str, actual: str) -> dict[str, Any]:
    matched = bool(expected) and expected == actual
    return {
        "identity": name,
        "matched": matched,
        "expected": expected,
        "actual": actual,
        "message": (
            f"{name} matched reviewed value"
            if matched else f"{name} diverged from reviewed value"
        ),
    }


def _incomplete_identity_check(label: str, problem: str) -> dict[str, Any]:
    return {
        "identity": "identity_incomplete",
        "artifact": label,
        "matched": False,
        "expected": "required manifest with source path and sha256",
        "actual": problem,
        "message": f"identity_incomplete: required {label} {problem}",
    }


def _evidence_reuse_checks(
    root: ProjectRoot, state: dict[str, Any], request: dict[str, Any],
) -> list[dict[str, Any]]:
    """Mechanically compare one saved PASS with the identities it reviewed."""
    identity = request.get("evidence_reuse_identity")
    if not isinstance(identity, dict) or int(identity.get("version", 0)) not in {1, 2, 3}:
        return [_reuse_check("evidence-reuse identity", "recorded", "missing")]

    task = str(request.get("task", ""))
    code_root = project_context(root).code_root
    workspace_value = state.get("task_workspaces", {}).get(task, "")
    workspace = Path(workspace_value) if workspace_value and Path(workspace_value).is_dir() else code_root
    current_git = _git_review_artifact(workspace)
    candidate = identity.get("candidate", {})
    checks = [
        _reuse_check(
            "commit hash", str(candidate.get("commit", "")),
            str(current_git.get("base_commit", "")),
        ),
        _reuse_check(
            "tree hash", str(candidate.get("tree_hash", "")),
            str(current_git.get("tree_hash", "")),
        ),
        _reuse_check(
            "working-tree hash", str(candidate.get("working_tree_digest", "")),
            str(current_git.get("working_tree_digest", "")),
        ),
    ]
    if int(identity.get("version", 0)) >= 2:
        checks.append(_reuse_check(
            "review scope",
            str(identity.get("review_scope", {}).get("sha256", "")),
            str(_review_scope_identity(request).get("sha256", "")),
        ))
    if int(identity.get("version", 0)) >= 3:
        expected_finalization = identity.get("finalization", {})
        if expected_finalization.get("applicable"):
            current_finalization = request.get("finalization_diff") or {}
            current_classification = request.get("finalization_classification") or {}
            checks.extend([
                _reuse_check(
                    "finalization diff",
                    str(expected_finalization.get("diff_sha256", "")),
                    str(current_finalization.get("sha256", "")),
                ),
                _reuse_check(
                    "finalization classification",
                    str(expected_finalization.get("classification", "")),
                    str(current_classification.get("decision", "")),
                ),
                _reuse_check(
                    "finalization classification diff",
                    str(expected_finalization.get("classification_diff_sha256", "")),
                    str(current_classification.get("diff_sha256", "")),
                ),
            ])

    labels = {
        "delivery_ledger": "delivery-ledger hash",
        "challenge_ledger": "challenge-ledger hash",
        "result_evidence": "evidence file hash",
        "delivery_evidence": "Delivery evidence file hash",
        "delivery_simulations_evidence": "Delivery simulation evidence hash",
        "qa_simulations_evidence": "QA simulation evidence hash",
        "reviewer_simulations_evidence": "reviewer simulation evidence hash",
    }
    source_artifacts = identity.get("source_artifacts", {})
    certified_artifacts = request.get("certified_artifacts", {})
    for name in _required_reuse_artifacts(request):
        label = labels.get(name, f"{name} hash")
        certified = certified_artifacts.get(name)
        if not isinstance(certified, dict):
            checks.append(_incomplete_identity_check(label, "manifest is missing"))
            continue
        if not certified.get("source_path"):
            checks.append(_incomplete_identity_check(label, "manifest lacks source path"))
            continue
        if not certified.get("sha256"):
            checks.append(_incomplete_identity_check(label, "manifest lacks sha256"))
            continue
        manifest = source_artifacts.get(name) if isinstance(source_artifacts, dict) else None
        if not isinstance(manifest, dict):
            checks.append(_incomplete_identity_check(label, "reuse identity entry is missing"))
            continue
        if not manifest.get("path"):
            checks.append(_incomplete_identity_check(label, "reuse identity lacks source path"))
            continue
        if not manifest.get("sha256"):
            checks.append(_incomplete_identity_check(label, "reuse identity lacks sha256"))
            continue
        path = Path(str(manifest.get("path", "")))
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        checks.append(_reuse_check(
            label, str(manifest.get("sha256", "")), actual,
        ))

    expected_contract = identity.get("contract_revision", {})
    actual_contract = _contract_revision(root, task)
    expected_sha = str(expected_contract.get("sha256", ""))
    scope_sha = str(actual_contract.get("sha256", ""))
    legacy_sha = str(actual_contract.get("legacy_sha256", ""))
    # A PASS recorded before the scope-pinned identity carries the whole-file
    # digest; accept either form so upgrading never voids certified work.
    contract_matched = bool(expected_sha) and expected_sha in {scope_sha, legacy_sha}
    checks.append({
        "identity": "contract revision",
        "matched": contract_matched,
        "expected": expected_sha,
        "actual": scope_sha if expected_sha == scope_sha or not legacy_sha else legacy_sha,
        "message": (
            "contract revision matched reviewed value"
            if contract_matched else "contract revision diverged from reviewed value"
        ),
    })
    checks.append(_reuse_check(
        "environment identity",
        str(identity.get("environment_identity", {}).get("sha256", "")),
        str(_environment_identity().get("sha256", "")),
    ))
    return checks


def reconcile_evidence_reuse(root: ProjectRoot, resume_id: str = "") -> dict[str, Any]:
    """Reuse intact PASSes and reopen any gate whose reviewed identity changed."""
    reused: list[dict[str, Any]] = []
    invalidated: list[dict[str, Any]] = []
    with locked_state(root, allow_paused=True, operation="reconcile saved PASS evidence") as state:
        latest_by_scope: dict[tuple[str, str, str, str, int], dict[str, Any]] = {}
        for request in state.get("qa_requests", {}).values():
            task = str(request.get("task", ""))
            plan = state.get("delivery_plans", {}).get(task, {})
            current_revision = int(plan.get("structure_revision", 0))
            request_revision = int(request.get("structure_revision", 0))
            if plan and request_revision != current_revision:
                continue
            key = (
                task, str(request.get("phase", "legacy")),
                str(request.get("subtask", "")), str(request.get("chunk", "")),
                request_revision,
            )
            previous = latest_by_scope.get(key)
            if previous is None or int(request.get("cycle", 0)) > int(previous.get("cycle", 0)):
                latest_by_scope[key] = request
        for request in latest_by_scope.values():
            if request.get("status") != "passed":
                continue
            prior = request.get("evidence_reuse_validation", {})
            if resume_id and prior.get("resume_id") == resume_id:
                target = reused if prior.get("status") == "reused" else invalidated
                target.append({
                    "request_id": request.get("id", ""),
                    "checks": json.loads(json.dumps(prior.get("checks", []))),
                    "status": prior.get("status", ""),
                })
                continue
            checks = _evidence_reuse_checks(root, state, request)
            failures = [item for item in checks if not item["matched"]]
            validation = {
                "resume_id": resume_id,
                "checked_at": now(),
                "status": "invalidated" if failures else "reused",
                "checks": checks,
            }
            request["evidence_reuse_validation"] = validation
            if not failures:
                event = _event(state, "qa_pass_reused", None, {
                    "task": request.get("task", ""),
                    "request_id": request.get("id", ""),
                    "message": (
                        f"Saved PASS reused for {request.get('id', '')}: all "
                        f"{len(checks)} reviewed identities matched"
                    ),
                    "checks": checks,
                })
                reused.append({"request_id": request.get("id", ""), "status": "reused", "checks": checks, "event": event})
                continue

            one_liner = "; ".join(item["message"] for item in failures)
            request.update({
                "status": "failed", "result": "failed", "integrity_invalidated": True,
                "result_summary": "Saved PASS invalidated: " + one_liner,
                "completed_at": now(), "review_wait_stopped_at": now(),
            })
            _set_review_scope_status(state, request, "open")
            developer = state.get("agents", {}).get(request.get("developer_id"))
            if developer:
                note = "Saved PASS invalidated; fresh review required: " + one_liner
                developer.update({
                    "status": "independent_review_failed", "status_note": note,
                    "last_status_at": now(),
                })
                saved = state.get("project_pause", {}).get("agents", {}).get(developer.get("id"))
                if isinstance(saved, dict):
                    saved.update({"status": "independent_review_failed", "status_note": note, "next_action": note})
            event = _event(state, "qa_pass_invalidated", developer, {
                "task": request.get("task", ""),
                "request_id": request.get("id", ""),
                "message": "Saved PASS invalidated; fresh review required: " + one_liner,
                "checks": checks,
                "diverged_identities": [item["identity"] for item in failures],
            })
            invalidated.append({"request_id": request.get("id", ""), "status": "invalidated", "checks": checks, "event": event})
    return {"resume_id": resume_id, "reused": reused, "invalidated": invalidated}


def _set_review_scope_status(state: dict[str, Any], request: dict[str, Any], status: str) -> None:
    """Apply a review result to its root chunk, nested chunk, or subtask."""
    pipeline_item = None
    if request.get("subtask"):
        pipeline_item = (
            state.get("delivery_plans", {})
            .get(request.get("task"), {})
            .get("subtasks", {})
            .get(request["subtask"])
        )
    if request.get("phase") == "chunk" and request.get("subtask"):
        nested = (pipeline_item or {}).get("chunks", {}).get(request.get("chunk", ""))
        if nested:
            nested["status"] = status
    elif request.get("phase") == "chunk":
        chunk = state.get("task_chunks", {}).get(request.get("task"), {}).get(request.get("chunk", ""))
        if chunk:
            chunk["status"] = status
    elif request.get("phase") == "subtask_acceptance" and request.get("subtask"):
        if pipeline_item:
            pipeline_item["status"] = status
            if status == "passed" and request.get("integrated_commit"):
                pipeline_item.update({
                    "integrated_commit": request["integrated_commit"],
                    "integrated_tree_hash": request.get("integrated_tree_hash", ""),
                    "integration_transaction_id": request.get("integration_transaction_id", ""),
                })
    if pipeline_item:
        pipeline_item.pop("active_review_request", None)
        pipeline_item["pipeline_status"] = (
            "passed"
            if request.get("phase") == "subtask_acceptance" and status == "passed"
            else "in_progress"
        )
        pipeline_item["pipeline_updated_at"] = now()


def reconcile_integrity(root: Path) -> dict[str, Any]:
    """Invalidate PASSes whose immutable certified evidence is absent or changed."""
    invalidated = []
    with locked_state(root) as state:
        if not state.get("integrity_migration_complete"):
            return {"invalidated": []}
        for request in list(state.get("qa_requests", {}).values()):
            if request.get("status") != "passed":
                continue
            valid, problems = _request_integrity(root, request)
            if valid:
                continue
            request.update({
                "status": "failed", "result": "failed", "integrity_invalidated": True,
                "result_summary": "PASS invalidated by certified evidence integrity: " + "; ".join(problems),
                "completed_at": now(), "review_wait_stopped_at": now(),
            })
            _set_review_scope_status(state, request, "open")
            developer = state.get("agents", {}).get(request.get("developer_id"))
            if developer:
                developer.update({"status": "independent_review_failed", "status_note": "PASS invalidated; fresh review cycle required", "last_status_at": now()})
            event = _event(state, "qa_pass_invalidated", developer, {
                "task": request.get("task", ""), "request_id": request["id"],
                "message": "Certified review evidence is missing or tampered; chunk reopened and fresh review is required",
                "problems": problems,
            })
            invalidated.append(event)
    return {"invalidated": invalidated}


def migrate_integrity(root: Path) -> dict[str, Any]:
    """Run the one-time legacy backfill and audited PASS reconciliation."""
    # Preserve the full-artifact PASS migration first. The history migration is
    # intentionally narrower and must not leave an otherwise backfillable PASS
    # with only a ledger manifest before reconciliation runs.
    backfill = backfill_legacy_integrity(root)
    ledgers = certify_legacy_review_ledgers(root)
    reconciled = reconcile_integrity(root)
    return {
        "migration_version": 2,
        "legacy_review_ledgers": ledgers,
        "backfill": backfill,
        "reconciliation": reconciled,
    }


def reopen_integrity_requests(root: Path, request_ids: list[str], reason: str) -> dict[str, Any]:
    """Auditedly reopen specifically rejected PASS evidence for a fresh review."""
    if not request_ids:
        raise ValueError("at least one request ID is required")
    reason = reason.strip()
    if len(reason) < 8:
        raise ValueError("an integrity reopening reason is required")
    reopened = []
    with locked_state(root) as state:
        for request_id in request_ids:
            request = state.get("qa_requests", {}).get(request_id)
            if not request:
                raise ValueError(f"unknown active QA request: {request_id}")
            if request.get("status") != "passed":
                raise ValueError(f"QA request is not a certified PASS: {request_id}")
            valid, problems = _request_integrity(root, request)
            if valid:
                raise ValueError(
                    "certified PASS evidence is intact; a control-plane defect must "
                    "be held and repaired without reopening product review"
                )
            _reset_interrupted_repair_package(
                state, request, "certified evidence integrity failed: " + "; ".join(problems),
            )
            request.update({
                "status": "failed", "result": "failed", "integrity_invalidated": True,
                "integrity_reconciliation_hold": True,
                "result_summary": "PASS reopened by audited integrity reconciliation: " + reason,
                "completed_at": now(), "review_wait_stopped_at": now(),
            })
            _set_review_scope_status(state, request, "open")
            developer = state.get("agents", {}).get(request.get("developer_id"))
            if developer:
                developer.update({"status": "independent_review_failed", "status_note": "PASS reopened; fresh review cycle required", "last_status_at": now()})
            event = _event(state, "qa_pass_invalidated", developer, {
                "task": request.get("task", ""), "request_id": request_id,
                "message": "PASS reopened by explicit certified-evidence reconciliation; fresh review is required",
                "reason": reason,
            })
            reopened.append(event)
    return {"reopened": reopened}


def record_control_plane_hold(
    root: ProjectRoot, task: str, source: str, reason: str,
) -> dict[str, Any]:
    """Hold orchestration without changing any certified product verdict."""
    task, source, reason = task.strip(), source.strip(), reason.strip()
    if not task or not source or len(reason) < 8:
        raise ValueError("control-plane hold requires task, source, and concrete reason")
    with locked_state(root) as state:
        passed_before = sorted(
            request.get("id", "") for request in state.get("qa_requests", {}).values()
            if request.get("task") == task and request.get("status") == "passed"
        )
        hold = {
            "task": task, "source": source, "reason": reason[:2000],
            "status": "open", "recorded_at": now(),
            "preserved_pass_request_ids": passed_before,
        }
        state.setdefault("control_plane_holds", {})[task] = hold
        _event(state, "control_plane_hold_recorded", None, {
            "task": task, "source": source,
            "preserved_pass_request_ids": passed_before,
            "message": "control-plane repair is required; certified product PASS remains unchanged",
        })
        return json.loads(json.dumps(hold))


def clear_control_plane_hold(root: ProjectRoot, task: str, source: str) -> dict[str, Any] | None:
    """Clear a repaired orchestration hold without touching product evidence."""
    with locked_state(root) as state:
        hold = state.setdefault("control_plane_holds", {}).get(task)
        if not hold:
            return None
        hold.update({"status": "resolved", "resolved_at": now(), "resolved_by": source})
        _event(state, "control_plane_hold_resolved", None, {
            "task": task, "source": source,
            "message": "control-plane repair verified; preserved product PASS remains current",
        })
        return json.loads(json.dumps(hold))


def simulation_evidence_complete(root: Path, request: dict[str, Any], field: str, ledger_field: str, state: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    """Verify recorded simulation evidence still matches its ledger and file."""
    problems = []
    bundle = request.get(field)
    if not isinstance(bundle, dict):
        return False, [f"{field} missing"]
    scenario_ids = bundle.get("scenario_ids")
    if not isinstance(scenario_ids, list) or not scenario_ids:
        problems.append(f"{field} has no scenario IDs")
    if bundle.get("approved_exception_ids"):
        problems.append(f"{field} contains unexecuted approved exceptions")
    if bundle.get("executed_count") != len(scenario_ids or []):
        problems.append(f"{field} did not execute every scenario")
    certified_mode = request.get("status") == "passed"
    evidence = Path(str(bundle.get("certified_evidence" if certified_mode else "evidence", "")))
    if not evidence.is_absolute():
        evidence = project_context(root).code_root / evidence
    if not evidence.is_file():
        problems.append(f"{field} evidence file is missing")
    elif hashlib.sha256(evidence.read_bytes()).hexdigest() != bundle.get("certified_evidence_sha256" if certified_mode else "evidence_sha256"):
        problems.append(f"{field} evidence digest does not match")
    if certified_mode:
        key = "challenge_ledger" if ledger_field == "challenge_ledger" else "delivery_ledger"
        ledger = Path(str(request.get("certified_artifacts", {}).get(key, {}).get("path", "")))
    else:
        board_state = state if state is not None else snapshot(root)
        ledger = _task_path_from_state(root, board_state, request.get("task", ""), str(request.get(ledger_field, "")))
    digest_field = "challenge_ledger_sha256" if ledger_field == "challenge_ledger" else "ledger_sha256"
    if not ledger.is_file():
        problems.append(f"{ledger_field} is missing")
    elif hashlib.sha256(ledger.read_bytes()).hexdigest() != request.get(digest_field):
        problems.append(f"{ledger_field} changed after simulation execution")
    return not problems, problems


DELIVERY_MODES = {"atomic", "chunked", "application"}


def define_delivery_plan(root: Path, agent_id: str, mode: str, rationale: str) -> dict[str, Any]:
    """Persist Product Management's proportional decomposition decision."""
    mode, rationale = str(mode or "").strip().lower(), str(rationale or "").strip()
    if mode not in DELIVERY_MODES:
        raise ValueError("delivery plan mode must be atomic, chunked, or application")
    if not rationale:
        raise ValueError("delivery plan requires a concise Product Management rationale")
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        if developer["role"] not in DEVELOPER_ROLES or developer["task"] == AWAITING_OWNER_DIRECTION or not developer.get("active"):
            raise ValueError("only an active Delivery Agent with owner direction may define the delivery plan")
        _require_contract_preflight(root, developer["task"])
        if not state.get("requirement_confirmations", {}).get(developer["task"]):
            raise ValueError("final requirements confirmation is required before defining the delivery plan")
        existing = state.setdefault("delivery_plans", {}).get(developer["task"])
        if existing:
            if existing.get("mode") != mode:
                raise ValueError("delivery plan mode cannot change after it is recorded")
            return json.loads(json.dumps(existing))
        if state.get("task_chunks", {}).get(developer["task"]) or _task_requests(state, developer["task"]):
            raise ValueError("define the delivery plan before declaring chunks or requesting review")
        plan = {
            "task": developer["task"], "mode": mode, "rationale": rationale,
            "subtasks": {}, "structure_revision": 1, "structure_changes": [],
            "created_at": now(), "updated_at": now(),
        }
        state["delivery_plans"][developer["task"]] = plan
        _event(state, "delivery_plan_defined", developer, {
            "task": developer["task"], "mode": mode,
            "message": f"Product Management classified this objective as {mode}: {rationale}",
        })
        return json.loads(json.dumps(plan))


def _validate_subtask_graph(subtasks: dict[str, dict[str, Any]]) -> None:
    names = set(subtasks)
    for name, subtask in subtasks.items():
        dependencies = subtask.get("dependencies", [])
        unknown = sorted(set(dependencies) - names)
        if unknown:
            raise ValueError(f"subtask {name} has unknown dependencies: " + ", ".join(unknown))
        if name in dependencies:
            raise ValueError(f"subtask {name} cannot depend on itself")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError("subtask dependencies must not contain a cycle")
        if name in visited:
            return
        visiting.add(name)
        for dependency in subtasks[name].get("dependencies", []):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in names:
        visit(name)


def _normalize_owned_paths(values: Any) -> list[str]:
    """Normalize project-relative ownership scopes; ``*`` is global scope."""
    if isinstance(values, str):
        values = [values]
    normalized: list[str] = []
    for raw in values or []:
        value = str(raw or "").strip().replace("\\", "/")
        if not value:
            continue
        if value == "*":
            normalized.append(value)
            continue
        path = PurePosixPath(value)
        if (
            path.is_absolute() or ".." in path.parts or "|" in value
            or any(part in {".git", ".harness"} for part in path.parts)
        ):
            raise ValueError(
                "subtask owned paths must be safe project-relative paths outside .git and .harness"
            )
        clean = str(path)
        if clean in {"", "."}:
            raise ValueError("subtask owned paths must identify a file or directory")
        normalized.append(clean.removeprefix("./"))
    return sorted(set(normalized))


def _normalize_owned_surfaces(values: Any) -> list[str]:
    """Normalize logical ownership IDs used for non-file coupling."""
    if isinstance(values, str):
        values = [values]
    normalized = []
    for raw in values or []:
        value = str(raw or "").strip()
        if not value:
            continue
        if value != "*" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", value):
            raise ValueError(
                "subtask owned surfaces may contain only letters, numbers, '.', '_', ':', '/', and '-'"
            )
        normalized.append(value)
    return sorted(set(normalized))


def _path_scopes_overlap(left: str, right: str) -> bool:
    if "*" in {left, right}:
        return True
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    width = min(len(left_parts), len(right_parts))
    return left_parts[:width] == right_parts[:width]


def _subtask_ownership_overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_paths = left.get("owned_paths") or ["*"]
    right_paths = right.get("owned_paths") or ["*"]
    if any(_path_scopes_overlap(a, b) for a in left_paths for b in right_paths):
        return True
    left_surfaces = set(left.get("owned_surfaces") or [])
    right_surfaces = set(right.get("owned_surfaces") or [])
    return bool(
        "*" in left_surfaces or "*" in right_surfaces
        or left_surfaces.intersection(right_surfaces)
    )


def _effective_subtask_pipeline_status(
    state: dict[str, Any], task: str, name: str, item: dict[str, Any],
) -> str:
    """Derive a safe status for pre-P5 records as well as current records."""
    if item.get("status") == "passed":
        return "passed"
    if any(
        request.get("task") == task
        and request.get("subtask") == name
        and request.get("status") not in TERMINAL_QA
        for request in state.get("qa_requests", {}).values()
    ):
        return "in_review"
    return str(item.get("pipeline_status") or "pending")


def _path_is_owned(path: str, owned_paths: list[str]) -> bool:
    normalized = _normalize_owned_paths([path])
    if not normalized:
        return False
    candidate = normalized[0]
    return any(
        scope == "*" or _path_scopes_overlap(scope, candidate)
        and len(PurePosixPath(scope).parts) <= len(PurePosixPath(candidate).parts)
        for scope in owned_paths
    )


def _require_owned_files(item: dict[str, Any], paths: list[str], label: str) -> None:
    owned_paths = list(item.get("owned_paths") or ["*"])
    outside = sorted(path for path in paths if not _path_is_owned(path, owned_paths))
    if outside:
        raise ValueError(
            f"{label} crosses the subtask ownership boundary: " + ", ".join(outside)
        )


def declare_subtasks(
    root: Path, agent_id: str, subtasks: list[dict[str, Any]], reason: str = "",
) -> dict[str, Any]:
    """Declare product capabilities beneath a full-application objective."""
    if not subtasks:
        raise ValueError("an application plan requires at least one product subtask")
    prepared: dict[str, dict[str, Any]] = {}
    for raw in subtasks:
        name = str(raw.get("id", "")).strip()
        title = str(raw.get("title", "")).strip()
        proof = str(raw.get("acceptance_proof", "")).strip()
        description = str(raw.get("description", title)).strip()
        dependencies = [str(value).strip() for value in raw.get("dependencies", []) if str(value).strip()]
        owned_paths = _normalize_owned_paths(raw.get("owned_paths", []))
        owned_surfaces = _normalize_owned_surfaces(raw.get("owned_surfaces", []))
        if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise ValueError("every subtask requires an ID containing only letters, numbers, hyphens, or underscores")
        if not title or not proof:
            raise ValueError(f"subtask {name} requires a title and acceptance proof")
        if name in prepared:
            raise ValueError(f"duplicate subtask: {name}")
        prepared[name] = {
            "id": name, "title": title, "description": description,
            "acceptance_proof": proof, "dependencies": dependencies,
            # Legacy declarations without ownership remain valid but global:
            # they can never be pipelined unsafely with another active scope.
            "owned_paths": owned_paths or ["*"],
            "owned_surfaces": owned_surfaces or (["*"] if not owned_paths else []),
            "status": "open", "pipeline_status": "pending",
            "chunks": {}, "created_at": now(),
        }
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        if developer["role"] not in DEVELOPER_ROLES or developer["task"] == AWAITING_OWNER_DIRECTION or not developer.get("active"):
            raise ValueError("only an active Delivery Agent may declare product subtasks")
        _require_contract_preflight(root, developer["task"])
        plan = state.setdefault("delivery_plans", {}).get(developer["task"])
        if not plan or plan.get("mode") != "application":
            raise ValueError("product subtasks require an application delivery plan")
        existing = plan.setdefault("subtasks", {})
        expansion = bool(existing)
        reason = str(reason or "").strip()
        if expansion and not reason:
            raise ValueError("adding product subtasks requires a plain-language scope expansion reason")
        duplicates = sorted(set(existing).intersection(prepared))
        if duplicates:
            raise ValueError("product subtasks already declared: " + ", ".join(duplicates))
        combined = {**existing, **prepared}
        _validate_subtask_graph(combined)
        existing.update(prepared)
        if state.get("task_repositories", {}).get(developer["task"]):
            broker = _broker_for_state(root, state, developer["task"])
            session_key = str(developer.get("session_id") or developer["id"])
            workspaces = state.setdefault("subtask_workspaces", {}).setdefault(developer["task"], {})
            branches = state.setdefault("subtask_branches", {}).setdefault(developer["task"], {})
            for name in sorted(prepared):
                branch = broker.branch_create(
                    developer["id"], _next_broker_nonce(state, session_key), subtask=name,
                )
                workspaces[name] = branch["workspace"]
                branches[name] = branch
                existing[name].update({
                    "workspace": branch["workspace"],
                    "branch": branch["branch"],
                    "base_commit": branch["base_commit"],
                    "base_tree": branch["base_tree"],
                })
        changed_at = now()
        plan["structure_revision"] = int(plan.get("structure_revision", 1)) + 1
        plan["updated_at"] = changed_at
        if expansion:
            plan.setdefault("structure_changes", []).append({
                "revision": plan["structure_revision"], "kind": "product subtasks added",
                "added": sorted(prepared), "reason": reason, "at": changed_at,
            })
        _event(state, "subtasks_declared", developer, {
            "task": developer["task"], "subtasks": sorted(prepared),
            "scope_expansion_reason": reason if expansion else "",
            "message": (
                f"Product Management added {len(prepared)} required product subtasks: {reason}"
                if expansion else f"Product Management declared {len(prepared)} required product subtasks"
            ),
        })
        return json.loads(json.dumps(existing))


def start_subtask(root: ProjectRoot, agent_id: str, subtask: str) -> dict[str, Any]:
    """Atomically admit one application subtask into the active pipeline."""
    subtask = str(subtask or "").strip()
    if not subtask:
        raise ValueError("subtask is required")
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        if developer.get("role") not in DEVELOPER_ROLES or not developer.get("active"):
            raise ValueError("only the active Delivery task owner may start a subtask")
        task = str(developer.get("task") or "")
        plan = state.get("delivery_plans", {}).get(task, {})
        item = plan.get("subtasks", {}).get(subtask)
        if plan.get("mode") != "application" or not item:
            raise ValueError("start-subtask requires a declared application subtask")
        current = _effective_subtask_pipeline_status(state, task, subtask, item)
        if current == "in_progress":
            return json.loads(json.dumps(item))
        if current == "in_review":
            raise ValueError("subtask is already under review")
        if current == "passed":
            raise ValueError("subtask acceptance already passed")
        pending_dependencies = [
            name for name in item.get("dependencies", [])
            if plan["subtasks"][name].get("status") != "passed"
        ]
        if pending_dependencies:
            raise ValueError(
                "subtask cannot start with unmet dependencies: "
                + ", ".join(pending_dependencies)
            )
        if state.get("task_repositories", {}).get(task):
            broker = _broker_for_state(root, state, task)
            refreshed = broker.refresh_subtask_base(task, subtask)
            state.setdefault("subtask_branches", {}).setdefault(task, {})[subtask] = refreshed
            state.setdefault("subtask_workspaces", {}).setdefault(task, {})[subtask] = refreshed["workspace"]
            item.update({
                "workspace": refreshed["workspace"],
                "branch": refreshed["branch"],
                "base_commit": refreshed["base_commit"],
                "base_tree": refreshed["base_tree"],
            })
        active = [
            (name, candidate)
            for name, candidate in plan.get("subtasks", {}).items()
            if name != subtask
            and _effective_subtask_pipeline_status(state, task, name, candidate)
            in {"in_progress", "in_review"}
        ]
        workspaces = state.get("subtask_workspaces", {}).get(task, {})
        conflicts = [
            name for name, candidate in active
            if _subtask_ownership_overlaps(item, candidate)
        ]
        if conflicts:
            raise ValueError(
                "subtask ownership overlaps active scope; serialize after: "
                + ", ".join(conflicts)
            )
        unisolated = [
            name for name, _ in active
            if not workspaces.get(name) or not workspaces.get(subtask)
            or not Path(workspaces[name]).is_dir()
            or not Path(workspaces[subtask]).is_dir()
            or Path(workspaces[name]).resolve() == Path(workspaces[subtask]).resolve()
        ]
        if unisolated:
            raise ValueError(
                "parallel subtasks require distinct broker-created workspaces; "
                "serialize after: " + ", ".join(unisolated)
            )
        unpinned_reviews = [
            name for name, _ in active
            if _effective_subtask_pipeline_status(
                state, task, name, plan["subtasks"][name],
            ) == "in_review"
            and not any(
                request.get("task") == task
                and request.get("subtask") == name
                and request.get("status") not in TERMINAL_QA
                and request.get("reviewed_commit")
                and request.get("reviewed_tree_hash")
                for request in state.get("qa_requests", {}).values()
            )
        ]
        if unpinned_reviews:
            raise ValueError(
                "parallel work requires every active review to be pinned to an immutable commit/tree: "
                + ", ".join(unpinned_reviews)
            )
        started_at = now()
        item.update({
            "pipeline_status": "in_progress",
            "started_at": item.get("started_at") or started_at,
            "pipeline_updated_at": started_at,
        })
        developer.update({
            "status": "implementing_subtask",
            "status_note": f"implementing application subtask {subtask}",
            "last_status_at": started_at,
        })
        _event(state, "subtask_started", developer, {
            "task": task,
            "subtask": subtask,
            "owned_paths": item.get("owned_paths", []),
            "owned_surfaces": item.get("owned_surfaces", []),
            "workspace": item.get("workspace", ""),
            "concurrent_with": [name for name, _ in active],
            "message": f"subtask {subtask} admitted to the dependency and ownership pipeline",
        })
        return json.loads(json.dumps(item))


def declare_subtask_chunks(
    root: Path, agent_id: str, subtask: str, chunks: list[tuple[str, str]], reason: str = "",
) -> dict[str, Any]:
    """Optionally split one large application subtask into reviewable slices."""
    subtask = str(subtask or "").strip()
    if not chunks or any(not name.strip() or not description.strip() for name, description in chunks):
        raise ValueError("at least one named chunk and description are required")
    names = [name.strip() for name, _ in chunks]
    if len(set(names)) != len(names):
        raise ValueError("chunk names must be unique")
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        plan = state.get("delivery_plans", {}).get(developer.get("task", ""), {})
        item = plan.get("subtasks", {}).get(subtask)
        if developer["role"] not in DEVELOPER_ROLES or not developer.get("active") or plan.get("mode") != "application" or not item:
            raise ValueError("subtask chunks require a declared application subtask")
        existing = item.setdefault("chunks", {})
        expansion = bool(existing)
        reason = str(reason or "").strip()
        if expansion and not reason:
            raise ValueError("adding subtask chunks requires a plain-language scope expansion reason")
        duplicates = sorted(set(names).intersection(existing))
        if duplicates:
            raise ValueError("subtask chunks already declared: " + ", ".join(duplicates))
        existing.update({name.strip(): {"description": description.strip(), "status": "open"} for name, description in chunks})
        item["status"] = "open"
        item["pipeline_status"] = "pending"
        item.pop("active_review_request", None)
        changed_at = now()
        plan["structure_revision"] = int(plan.get("structure_revision", 1)) + 1
        plan["updated_at"] = changed_at
        if expansion:
            plan.setdefault("structure_changes", []).append({
                "revision": plan["structure_revision"], "kind": f"work added within {subtask}",
                "added": sorted(names), "reason": reason, "at": changed_at,
            })
        _event(state, "subtask_chunks_declared", developer, {
            "task": developer["task"], "subtask": subtask,
            "scope_expansion_reason": reason if expansion else "",
            "message": (
                f"added {len(chunks)} reviewable chunks inside product subtask {subtask}: {reason}"
                if expansion else f"declared {len(chunks)} reviewable chunks inside product subtask {subtask}"
            ),
        })
        return json.loads(json.dumps(existing))


def _candidate_freeze_reason(state: dict[str, Any], task: str) -> str:
    """Explain why this task's candidate is frozen, or return an empty string.

    A candidate with a standing final-acceptance PASS is frozen: release
    environment failures must never grow it. The freeze lifts only when the
    product itself is shown defective (a genuinely failed product review or
    an owner rejection) or when the CTO records an explicit scope reopen.
    An invalidated PASS keeps the freeze: invalidation reopens the review of
    the same bytes, and re-review needs no new commit.
    """
    finals = [
        request for request in (state.get("qa_requests") or {}).values()
        if isinstance(request, dict)
        and request.get("task") == task
        and request.get("phase") == "final_acceptance"
        and request.get("status") in {"passed", "failed"}
    ]
    if not finals:
        return ""
    newest = max(finals, key=lambda request: (
        str(request.get("completed_at") or request.get("requested_at") or ""),
        str(request.get("id") or ""),
    ))
    passed = newest.get("status") == "passed"
    invalidated = bool(newest.get("integrity_invalidated"))
    if not passed and not invalidated:
        return ""
    repair = (state.get("release_repairs") or {}).get(task) or {}
    if repair.get("status") in {
        "OWNER_REJECTED_REPAIR_REQUIRED", "DELIVERY_REPAIR_IN_PROGRESS", "repairing",
    }:
        return ""
    reopen = (state.get("candidate_scope_reopens") or {}).get(task) or {}
    reference = str(newest.get("completed_at") or "")
    if reopen and str(reopen.get("recorded_at") or "") > reference:
        return ""
    return (
        "This candidate passed final acceptance and is frozen. Environment or "
        "release-infrastructure repairs must not modify it; open a follow-up "
        "task instead. The freeze lifts on a failed product review, an owner "
        "rejection, or a CTO reopen-candidate-scope record."
        if passed else
        "This candidate's certified PASS was invalidated by an identity check, "
        "not by a product defect. Re-request the review of the same commit; "
        "new commits require a failed product review, an owner rejection, or "
        "a CTO reopen-candidate-scope record."
    )


def reopen_candidate_scope(root: Path, agent_id: str, task: str, reason: str) -> dict[str, Any]:
    """CTO-only: lift the candidate freeze for one recorded, auditable reason."""
    task = str(task or "").strip()
    reason = str(reason or "").strip()
    if not task:
        raise ValueError("reopen-candidate-scope requires a task")
    if len(reason) < 20:
        raise ValueError("reopen-candidate-scope requires a plain-language reason of at least 20 characters")
    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        if agent.get("role") != "cto":
            raise ValueError("only the CTO may reopen a frozen candidate's scope")
        if not _candidate_freeze_reason(state, task):
            raise ValueError("this task's candidate is not frozen; no reopen is needed")
        record = {
            "task": task, "reason": reason,
            "cto_id": agent["id"], "recorded_at": now(),
        }
        state.setdefault("candidate_scope_reopens", {})[task] = record
        _event(state, "candidate_scope_reopened", agent, {
            "task": task,
            "message": f"CTO reopened the frozen candidate scope: {reason[:300]}",
        })
        return dict(record)


def broker_stage_commit(
    root: ProjectRoot,
    agent_id: str,
    paths: list[str],
    message: str,
    subtask: str = "",
) -> dict[str, Any]:
    """Stage explicit task paths and commit them through the trusted broker."""
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        if developer.get("role") not in DEVELOPER_ROLES or not developer.get("active"):
            raise ValueError("only the active Delivery task owner may create a governed commit")
        task = str(developer.get("task") or "")
        _require_contract_preflight(root, task)
        if not state.get("task_repositories", {}).get(task):
            raise ValueError("task has no broker-governed Git repository")
        freeze_reason = _candidate_freeze_reason(state, task)
        if freeze_reason:
            raise ValueError(freeze_reason)
        plan = state.get("delivery_plans", {}).get(task, {})
        item = plan.get("subtasks", {}).get(subtask) if subtask else None
        if subtask and not item:
            raise ValueError("governed commit names an undeclared subtask")
        if item:
            if _effective_subtask_pipeline_status(state, task, subtask, item) != "in_progress":
                raise ValueError("governed subtask commit requires start-subtask and no active review")
            _require_owned_files(item, paths, "governed commit manifest")
        elif plan.get("mode") == "application" and any(
            value.get("status") != "passed"
            for value in plan.get("subtasks", {}).values()
        ):
            raise ValueError("integrated application commits require every subtask acceptance to pass")
        broker = _broker_for_state(root, state, task)
        result = broker.stage_commit(
            agent_id,
            _next_broker_nonce(state, str(developer.get("session_id") or developer["id"])),
            paths,
            message,
            subtask=subtask,
        )
        if item:
            _require_owned_files(item, list(result.get("manifest") or []), "broker commit result")
        _event(state, "broker_commit_created", developer, {
            "task": task,
            "subtask": subtask,
            "commit": result["commit"],
            "tree": result["tree"],
            "manifest": result["manifest"],
            "message": "trusted Git broker committed the explicit reviewed manifest",
        })
        return result


def declare_chunks(
    root: Path, agent_id: str, chunks: list[tuple[str, str]], reason: str = "",
) -> dict[str, Any]:
    """Declare the delivery slices before implementation; owner input is never needed."""
    if not chunks or any(not name.strip() or not description.strip() for name, description in chunks):
        raise ValueError("at least one named chunk and description are required")
    names = [name.strip() for name, _ in chunks]
    if len(set(names)) != len(names):
        raise ValueError("chunk names must be unique")
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        if developer["role"] not in DEVELOPER_ROLES:
            raise ValueError("only development or engineering agents may declare delivery chunks")
        if not developer.get("active"):
            raise ValueError("inactive Delivery Agent cannot change the delivery structure")
        if developer["task"] == AWAITING_OWNER_DIRECTION:
            raise ValueError("delivery agent must await owner direction before creating work")
        _require_contract_preflight(root, developer["task"])
        plan = state.setdefault("delivery_plans", {}).get(developer["task"])
        if not plan:
            raise ValueError("Product Management must define a chunked delivery plan before declaring root chunks")
        if plan.get("mode") != "chunked":
            raise ValueError("root delivery chunks are valid only for a chunked task; application chunks belong under subtasks")
        existing = state["task_chunks"].setdefault(developer["task"], {})
        expansion = bool(existing)
        reason = str(reason or "").strip()
        if expansion and not reason:
            raise ValueError("adding delivery chunks requires a plain-language scope expansion reason")
        duplicates = sorted(set(names).intersection(existing))
        if duplicates:
            raise ValueError("delivery chunks already declared: " + ", ".join(duplicates))
        declared = {name.strip(): {"description": description.strip(), "status": "open"} for name, description in chunks}
        existing.update(declared)
        changed_at = now()
        plan["structure_revision"] = int(plan.get("structure_revision", 1)) + 1
        plan["updated_at"] = changed_at
        if expansion:
            plan.setdefault("structure_changes", []).append({
                "revision": plan["structure_revision"], "kind": "delivery work added",
                "added": sorted(names), "reason": reason, "at": changed_at,
            })
        qualifier = "additional " if len(existing) > len(declared) else ""
        _event(state, "chunks_declared", developer, {
            "task": developer["task"], "scope_expansion_reason": reason if expansion else "",
            "message": (
                f"declared {len(declared)} {qualifier}reviewable chunks: {reason}"
                if expansion else f"declared {len(declared)} {qualifier}reviewable chunks"
            ),
        })
        return json.loads(json.dumps(existing))


def request_qa(root: Path, agent_id: str, ledger: str, summary: str, changes: str = "") -> dict[str, Any]:
    if not ledger.strip() or not summary.strip():
        raise ValueError("ledger and summary are required")
    _require_ledger_scenarios(root, ledger, "Scenario Ledger", owner_readable=True)
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        if developer["role"] not in DEVELOPER_ROLES:
            raise ValueError("only development or engineering agents may request QA")
        if developer["task"] == AWAITING_OWNER_DIRECTION:
            raise ValueError("delivery agent must await owner direction before requesting QA")
        _require_contract_preflight(root, developer["task"])
        plan = state.get("delivery_plans", {}).get(developer["task"])
        if not plan:
            raise ValueError("Product Management must define a delivery plan before requesting QA")
        if plan.get("mode") != "atomic":
            raise ValueError("legacy request-qa is valid only for an atomic plan; use request-review for chunked or application work")
        all_prior = _task_requests(state, developer["task"])
        if any(r["status"] not in TERMINAL_QA for r in all_prior):
            raise ValueError("an active QA request already exists for this task")
        prior = [r for r in all_prior if r.get("stage", DEVELOPMENT_QA) == DEVELOPMENT_QA]
        reviews = [r for r in all_prior if r.get("stage") == INDEPENDENT_REVIEW]
        cycle = max((int(r["cycle"]) for r in prior), default=0) + 1
        latest = max(prior, key=lambda r: int(r["cycle"]), default=None)
        latest_review = max(reviews, key=lambda r: int(r["cycle"]), default=None)
        if latest_review and latest_review["status"] == "passed":
            raise ValueError("independent review already passed for this task; begin a new task instead")
        if latest and latest["status"] == "passed" and not (latest_review and latest_review["status"] == "failed"):
            raise ValueError("development QA passed; request independent review instead of reopening QA")
        if cycle > 1 and not changes.strip():
            raise ValueError("a re-test cycle requires a concise --changes summary")
        request_id = f"qa-{developer['task']}-dev-{cycle:02d}"
        request = {
            "id": request_id, "task": developer["task"], "cycle": cycle,
            "stage": DEVELOPMENT_QA,
            "developer_id": developer["id"], "ledger": ledger, "summary": summary,
            "changes_summary": changes,
            "status": "open", "requested_at": now(), "claimed_by": None,
            "claimed_at": None, "result": None, "result_summary": None,
            "completed_at": None, "review_wait_started_at": now(), "review_wait_stopped_at": None,
        }
        state["qa_requests"][request_id] = request
        developer.update({"status": "awaiting_qa", "status_note": f"QA cycle {cycle} requested", "last_status_at": now()})
        _event(state, "qa_requested", developer, {"task": developer["task"], "request_id": request_id, "stage": DEVELOPMENT_QA, "cycle": cycle, "ledger": ledger, "message": summary})
        created = dict(request)
    route_open_reviews(root)
    return dict(snapshot(root)["qa_requests"].get(request_id, created))


def request_independent_review(root: Path, agent_id: str, summary: str) -> dict[str, Any]:
    """Queue a second, independent execution review after development QA passes."""
    if not summary.strip():
        raise ValueError("review summary is required")
    review_artifact: dict[str, Any] = {}
    contract_revision: dict[str, str] = {}
    environment_identity: dict[str, Any] = {}
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        if developer["role"] not in DEVELOPER_ROLES or not developer.get("vendor"):
            raise ValueError("a development/engineering agent with a declared vendor must request independent review")
        if developer["task"] == AWAITING_OWNER_DIRECTION:
            raise ValueError("delivery agent must await owner direction before requesting review")
        _require_contract_preflight(root, developer["task"])
        plan = state.get("delivery_plans", {}).get(developer["task"])
        if not plan:
            raise ValueError("Product Management must define a delivery plan before requesting independent review")
        if plan.get("mode") != "atomic":
            raise ValueError("legacy independent review is valid only for an atomic plan; use scoped request-review for chunked or application work")
        code_root = project_context(root).code_root
        review_root = Path(state.get("task_workspaces", {}).get(developer["task"], "")) if state.get("task_workspaces", {}).get(developer["task"]) else code_root
        if not review_root.is_dir():
            review_root = code_root
        baseline_commit = state.get("task_baselines", {}).get(developer["task"], {}).get("head", "")
        review_artifact = _git_review_artifact(review_root, baseline_commit)
        contract_revision = _contract_revision(root, developer["task"])
        environment_identity = _environment_identity()
        if review_artifact.get("available") and not review_artifact.get("immutable_clean"):
            raise ValueError("final independent review requires an immutable clean commit/tree")
        prior = _task_requests(state, developer["task"])
        if any(r["status"] not in TERMINAL_QA for r in prior):
            raise ValueError("an active QA or review request already exists for this task")
        dev_runs = [r for r in prior if r.get("stage", DEVELOPMENT_QA) == DEVELOPMENT_QA]
        reviews = [r for r in prior if r.get("stage") == INDEPENDENT_REVIEW]
        latest_dev = max(dev_runs, key=lambda r: int(r["cycle"]), default=None)
        latest_review = max(reviews, key=lambda r: int(r["cycle"]), default=None)
        if not latest_dev or latest_dev["status"] != "passed":
            raise ValueError("independent review requires a passed development-QA cycle")
        simulation_valid, simulation_problems = simulation_evidence_complete(root, latest_dev, "qa_simulations", "ledger", state)
        if not simulation_valid:
            raise ValueError("independent review requires executed development-QA scenarios: " + "; ".join(simulation_problems))
        if latest_review and latest_review["status"] == "passed":
            raise ValueError("independent review already passed for this task")
        cycle = max((int(r["cycle"]) for r in reviews), default=0) + 1
        request_id = f"review-{developer['task']}-{cycle:02d}"
        request = {
            "id": request_id, "task": developer["task"], "cycle": cycle,
            "stage": INDEPENDENT_REVIEW, "phase": "final_acceptance",
            "subtask": "", "chunk": "final", "delivery_mode": "atomic",
            "structure_revision": int(plan.get("structure_revision", 0)),
            "developer_id": developer["id"],
            "ledger": latest_dev["ledger"], "challenge_ledger": None, "summary": summary,
            "ledger_sha256": latest_dev.get("ledger_sha256"),
            "delivery_simulations": latest_dev.get("qa_simulations"),
            "reviewed_commit": review_artifact.get("commit", ""),
            "reviewed_base_commit": review_artifact.get("base_commit", ""),
            "reviewed_tree_hash": review_artifact.get("tree_hash", ""),
            "reviewed_files": review_artifact.get("files", []),
            "reviewed_worktree_digest": review_artifact.get("working_tree_digest", ""),
            "contract_revision": contract_revision,
            "environment_identity": environment_identity,
            "changes_summary": "", "status": "open", "requested_at": now(), "claimed_by": None,
            "claimed_at": None, "result": None, "result_summary": None, "evidence": None,
            "completed_at": None, "review_wait_started_at": now(), "review_wait_stopped_at": None,
        }
        state["qa_requests"][request_id] = request
        developer.update({"status": "awaiting_independent_review", "status_note": f"independent review {cycle} requested", "last_status_at": now()})
        _event(state, "independent_review_requested", developer, {"task": developer["task"], "request_id": request_id, "stage": INDEPENDENT_REVIEW, "cycle": cycle, "ledger": latest_dev["ledger"], "message": summary})
        created = dict(request)
    route_open_reviews(root)
    return dict(snapshot(root)["qa_requests"].get(request_id, created))


def _validate_review_scope(state: dict[str, Any], task: str, phase: str, subtask: str, chunk: str) -> tuple[str, str, str]:
    """Validate and normalize one adaptive delivery-review scope."""
    plan = state.get("delivery_plans", {}).get(task, {})
    root_chunks = state.get("task_chunks", {}).get(task, {})
    if not plan:
        raise ValueError("Product Management must define an atomic, chunked, or application delivery plan before review")
    mode = plan.get("mode")
    subtask, chunk = subtask.strip(), chunk.strip()
    if phase == "chunk":
        if mode == "application":
            item = plan.get("subtasks", {}).get(subtask)
            if not item:
                raise ValueError("application chunk review requires a declared --subtask")
            if not chunk or chunk not in item.get("chunks", {}):
                raise ValueError("application chunk review requires a declared --chunk within that subtask")
            if item["chunks"][chunk].get("status") == "passed":
                raise ValueError("chunk already passed; review the next chunk or request subtask acceptance")
        elif mode == "chunked":
            if subtask:
                raise ValueError("a chunked task does not use --subtask")
            if not chunk or chunk not in root_chunks:
                raise ValueError("chunk review requires a declared --chunk")
            if root_chunks[chunk].get("status") == "passed":
                raise ValueError("chunk already passed; review the next chunk or request final acceptance")
        else:
            raise ValueError("an atomic task has no chunk review; request final acceptance directly")
    elif phase == "subtask_acceptance":
        if mode != "application":
            raise ValueError("subtask acceptance is valid only for an application plan")
        item = plan.get("subtasks", {}).get(subtask)
        if not item:
            raise ValueError("subtask acceptance requires a declared --subtask")
        pending_dependencies = [name for name in item.get("dependencies", []) if plan["subtasks"][name].get("status") != "passed"]
        if pending_dependencies:
            raise ValueError("subtask acceptance requires passed dependencies first: " + ", ".join(pending_dependencies))
        pending_chunks = [name for name, value in item.get("chunks", {}).items() if value.get("status") != "passed"]
        if pending_chunks:
            raise ValueError("subtask acceptance requires every declared subtask chunk to pass first: " + ", ".join(pending_chunks))
        if item.get("status") == "passed":
            raise ValueError("subtask acceptance already passed")
        chunk = "subtask-final"
    else:
        subtask, chunk = "", "final"
        if mode == "chunked":
            if not root_chunks:
                raise ValueError("chunked final acceptance requires declared delivery chunks")
            pending = [name for name, value in root_chunks.items() if value.get("status") != "passed"]
            if pending:
                raise ValueError("final acceptance requires every chunk to pass first: " + ", ".join(pending))
        elif mode == "application":
            subtasks = plan.get("subtasks", {})
            if not subtasks:
                raise ValueError("application final acceptance requires declared product subtasks")
            hold = state.get("finalization_holds", {}).get(task, {})
            if hold and int(plan.get("structure_revision", 0)) <= int(hold.get("structure_revision", 0)):
                raise ValueError(
                    "finalization classification was rejected; declare and independently accept the new product scope before retrying final acceptance"
                )
            pending = [name for name, value in subtasks.items() if value.get("status") != "passed"]
            if pending:
                raise ValueError("application final acceptance requires every subtask acceptance to pass first: " + ", ".join(pending))
    return mode, subtask, chunk


def _implementation_scope_start(
    state: dict[str, Any], task: str, phase: str, subtask: str, chunk: str,
) -> tuple[str, str]:
    """Return durable implementation and optional repair boundaries.

    No prose is inspected.  A first cycle begins when Product Management made
    that scope actionable; a repair begins at the preceding failed verdict.
    """
    prior = [
        request for request in _task_requests(state, task)
        if request.get("stage") == INDEPENDENT_REVIEW
        and request.get("phase", "legacy") == phase
        and request.get("subtask", "") == subtask
        and request.get("chunk", "") == chunk
    ]
    latest = max(prior, key=lambda request: int(request.get("cycle", 0)), default=None)
    if latest and latest.get("status") == "failed" and latest.get("completed_at"):
        return str(latest["completed_at"]), str(latest["completed_at"])
    plan = state.get("delivery_plans", {}).get(task, {})
    if subtask:
        item = plan.get("subtasks", {}).get(subtask, {})
        if item.get("created_at"):
            return str(item["created_at"]), ""
    if chunk:
        item = state.get("task_chunks", {}).get(task, {}).get(chunk, {})
        if item.get("created_at"):
            return str(item["created_at"]), ""
    if phase == "final_acceptance":
        passed = [str(request.get("completed_at", "")) for request in _task_requests(state, task) if request.get("status") == "passed" and request.get("completed_at")]
        if passed:
            return max(passed), ""
    if plan.get("created_at"):
        return str(plan["created_at"]), ""
    begun = [str(event.get("at", "")) for event in state.get("events", []) if event.get("task") == task and event.get("kind") == "task_begun"]
    return (min(begun) if begun else now()), ""


def _application_finalization_diff(
    root: ProjectRoot, state: dict[str, Any], workspace: Path,
    task: str, final_commit: str,
) -> dict[str, Any] | None:
    """Compute final bytes not covered by exact accepted subtask manifests."""
    plan = state.get("delivery_plans", {}).get(task, {})
    if plan.get("mode") != "application" or not state.get("task_repositories", {}).get(task):
        return None
    baseline = str(state.get("task_baselines", {}).get(task, {}).get("head") or "")
    if not baseline or not final_commit:
        raise ValueError("application finalization diff requires exact baseline and final commits")
    accepted_paths: dict[str, dict[str, Any]] = {}
    accepted_manifests = []
    task_requests = _task_requests(state, task)
    for subtask, item in sorted((plan.get("subtasks") or {}).items()):
        candidates = [
            request for request in task_requests
            if request.get("phase") == "subtask_acceptance"
            and request.get("subtask") == subtask
            and request.get("status") == "passed"
        ]
        accepted = max(
            candidates,
            key=lambda request: (
                str(request.get("completed_at") or ""), int(request.get("cycle", 0)),
            ),
            default=None,
        )
        if not accepted:
            raise ValueError(f"finalization diff lacks passed subtask acceptance for {subtask}")
        manifest = accepted.get("accepted_byte_manifest")
        if not isinstance(manifest, dict):
            raise ValueError(f"finalization diff lacks accepted-byte manifest for {subtask}")
        accepted_bytes.verify_manifest(workspace, manifest)
        verification = accepted_bytes.verify_entries(workspace, final_commit, manifest)
        integrated_commit = str(item.get("integrated_commit") or accepted.get("integrated_commit") or "")
        if not integrated_commit:
            raise ValueError(f"finalization diff lacks integrated commit for {subtask}")
        for entry in manifest["entries"]:
            path = str(entry.get("path") or "")
            if path in accepted_paths:
                raise ValueError(f"accepted-byte manifests overlap at {path}")
            accepted_paths[path] = json.loads(json.dumps(entry))
        accepted_manifests.append({
            "subtask": subtask,
            "request_id": accepted.get("id", ""),
            "integrated_commit": integrated_commit,
            "manifest_sha256": manifest.get("sha256", ""),
            "verification": verification,
        })
    delta = accepted_bytes.tree_delta(workspace, baseline, final_commit)
    finalization_paths = sorted(set(delta["paths"]) - set(accepted_paths))
    entries_by_path = {entry["path"]: entry for entry in delta["entries"]}
    result: dict[str, Any] = {
        "version": 1,
        "baseline_commit": baseline,
        "final_commit": final_commit,
        "final_tree": delta["reviewed_tree"],
        "accepted_manifests": accepted_manifests,
        "accepted_paths": sorted(accepted_paths),
        "paths": finalization_paths,
        "entries": [entries_by_path[path] for path in finalization_paths],
        "classification_required": True,
        "classification": "pending_independent_review",
    }
    result["sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result


def _repair_packages_for_scope(
    state: dict[str, Any], task: str, phase: str, subtask: str, chunk: str,
) -> list[dict[str, Any]]:
    return sorted([
        package for package in state.get("repair_packages", {}).values()
        if package.get("task") == task
        and package.get("phase") == phase
        and package.get("subtask", "") == subtask
        and package.get("chunk", "") == chunk
        and package.get("status") in {"open", "ready_for_review", "under_review"}
    ], key=lambda package: (str(package.get("created_at") or ""), str(package.get("id") or "")))


def resolve_repair_package(
    root: ProjectRoot, agent_id: str, package_id: str,
    resolutions: list[dict[str, str]],
) -> dict[str, Any]:
    """Require Delivery to close every grouped failure before review."""
    with locked_state(root) as state:
        delivery = _require_writable_agent(state, agent_id)
        package = state.get("repair_packages", {}).get(package_id)
        if delivery.get("role") not in DEVELOPER_ROLES or not package:
            raise ValueError("only Delivery may resolve an existing repair package")
        if package.get("task") != delivery.get("task"):
            raise ValueError("repair package belongs to a different task")
        repair_package_model.resolve(package, resolutions, delivery["id"])
        _event(state, "repair_package_resolved", delivery, {
            "task": package["task"], "package_id": package_id,
            "member_count": len(package.get("members") or []),
            "required_test_scope": package.get("required_test_scope"),
            "message": "Every grouped failure has a recorded repair and regression check",
        })
        return json.loads(json.dumps(package))


def split_repair_package(
    root: ProjectRoot, agent_id: str, package_id: str,
    groups: list[list[str]], reason: str,
) -> list[dict[str, Any]]:
    """Allow the source Reviewer to split real ownership/security boundaries."""
    with locked_state(root) as state:
        reviewer = _require_agent(state, agent_id)
        package = state.get("repair_packages", {}).get(package_id)
        if reviewer.get("role") != "qa" or not package:
            raise ValueError("only the source Reviewer may split an existing repair package")
        if package.get("source_reviewer_id") != reviewer.get("id"):
            raise ValueError("repair package split belongs to its source Reviewer")
        children = repair_package_model.split(package, groups, reviewer["id"], reason)
        for child in children:
            state["repair_packages"][child["id"]] = child
        _event(state, "repair_package_split", reviewer, {
            "task": package["task"], "package_id": package_id,
            "child_package_ids": [child["id"] for child in children],
            "message": "Reviewer split mechanically independent repair boundaries; review depth was recomputed for each",
        })
        return json.loads(json.dumps(children))


def _repair_context(
    root: Path, state: dict[str, Any], workspace: Path | None,
    scope_reviews: list[dict[str, Any]], new_commit: str,
) -> dict[str, Any] | None:
    """P4 delta context for cycle 2+: what failed, what changed, what was run.

    Mechanically attached, honestly degraded: a diff that cannot be computed
    is marked unavailable rather than blocking the resubmission. Execution
    reuse stays fail-closed regardless — a changed candidate misses the
    item-4 store by identity, so an impacted scenario CANNOT ride a stale
    certification; this context saves authoring and focus, never proof.
    """
    failed = [r for r in scope_reviews if r.get("status") == "failed"]
    if not failed:
        return None
    prior = max(failed, key=lambda r: int(r.get("cycle", 0)))
    context: dict[str, Any] = {
        "prior_request_id": prior.get("id", ""),
        "prior_cycle": prior.get("cycle", 0),
        "prior_blocking_summary": str(prior.get("result_summary") or "")[:2000],
        "prior_challenge_ledger": prior.get("challenge_ledger") or "",
        "prior_challenge_ledger_sha256": prior.get("challenge_ledger_sha256") or "",
        "prior_execution_identities": list(
            (prior.get("challenge_execution") or {}).get("identities", [])),
        "prior_reviewed_commit": prior.get("reviewed_commit") or "",
        "prior_reviewer_id": prior.get("claimed_by") or prior.get("reserved_by") or "",
    }
    packages = [
        package for package in state.get("repair_packages", {}).values()
        if package.get("source_request_id") == prior.get("id")
        and package.get("status") != "split"
    ]
    context["repair_packages"] = json.loads(json.dumps(packages))
    diff_files: list[str] | None = None
    prior_commit = str(prior.get("reviewed_commit") or "")
    if workspace and prior_commit and new_commit:
        changed = git_process.run(
            ["git", "diff", "--name-only", f"{prior_commit}..{new_commit}"],
            cwd=workspace, capture_output=True, text=True)
        if changed.returncode == 0:
            diff_files = sorted(line for line in changed.stdout.splitlines() if line)
    context["diff_files"] = diff_files
    context["diff_available"] = diff_files is not None
    challenge_rows: list[dict[str, Any]] = []
    challenge_problems: list[str] = []
    challenge_digest = str(prior.get("challenge_ledger_sha256") or "")
    challenge_value = str(prior.get("challenge_ledger") or "")
    if challenge_value:
        try:
            challenge_path = _task_path_from_state(root, state, prior.get("task", ""), challenge_value)
            challenge_digest = challenge_digest or hashlib.sha256(challenge_path.read_bytes()).hexdigest()
            valid, problems, rows = contract.scenario_simulations(challenge_path)
            challenge_problems = list(problems)
            if valid:
                identities = (prior.get("challenge_execution") or {}).get("scenario_identities") or {}
                for row in rows:
                    mechanical = {
                        "prior_scenario_id": row["id"],
                        "simulation_command": row["command"],
                        "prior_execution_identity": (
                            identities.get(row["id"], "")
                            if isinstance(identities, dict) else ""
                        ),
                        "rerun_required": bool(
                            new_commit != prior_commit or diff_files is None or diff_files
                        ),
                    }
                    challenge_rows.append(mechanical)
        except (OSError, ValueError) as error:
            challenge_problems = [str(error)]
    context["challenge_prefill"] = {
        "version": 1,
        "source_request_id": prior.get("id", ""),
        "source_ledger_sha256": challenge_digest,
        "mechanical_rows": challenge_rows,
        "unavailable_reasons": challenge_problems,
        "reviewer_must_author": [
            "risk statement", "scenario description", "expected system response",
            "new or escalated commands", "observed system response", "semantic verdict",
        ],
        "note": (
            "Only prior command identity and candidate-delta facts are prefilled. "
            "They are not a Challenge Ledger and cannot satisfy independent authorship."
        ),
    }
    return context


TEST_SCOPES = {"focused", "affected", "integration", "full", "health"}


def _completion_gate_problems(
    root: Path, state: dict[str, Any], task: str, developer_id: str,
) -> list[str]:
    """Return the deterministic gates that used to run only after final PASS."""
    problems: list[str] = []
    complete_contract, contract_problems, contract_value = contract.contract_complete(root, task)
    if not complete_contract:
        problems.append("Completion Contract is incomplete: " + "; ".join(contract_problems))
    owner_direction = owner_direction_for_task(state, developer_id, task)
    if not owner_direction:
        problems.append("recorded owner direction is missing")
    if complete_contract and owner_direction:
        objective_matches = (
            contract.normalize_owner_direction(contract_value["objective"])
            == owner_direction
        )
        confirmation_recorded = bool(state.get("requirement_confirmations", {}).get(task))
        if not objective_matches and not confirmation_recorded:
            problems.append(
                "Completion Contract objective differs from the owner direction and no "
                "requirements confirmation records the agreed translation"
            )
    return problems


def request_review(root: Path, agent_id: str, ledger: str, summary: str, phase: str = "chunk", chunk: str = "", changes: str = "", evidence: str = "", test_command: str = "", subtask: str = "", test_scope: str = "full", scope_reason: str = "", repair_package_id: str = "") -> dict[str, Any]:
    """Queue a direct independent review for the multi-hat delivery-agent flow.

    The delivery agent performs its own QA; the standing reviewer then claims
    this request. A failure reopens work and requires a concise fix summary.
    """
    if phase not in {"chunk", "subtask_acceptance", "final_acceptance"}:
        raise ValueError("phase must be chunk, subtask_acceptance, or final_acceptance")
    test_scope = (test_scope or "full").strip().lower()
    if test_scope not in TEST_SCOPES:
        raise ValueError(f"test scope must be one of: {', '.join(sorted(TEST_SCOPES))}")
    if phase == "final_acceptance" and test_scope != "full":
        raise ValueError("final acceptance always runs the FULL suite; a narrowed scope is refused")
    if test_scope != "full" and not scope_reason.strip():
        raise ValueError(f"a '{test_scope}' scope requires a recorded --scope-reason naming the risk basis")
    if not ledger.strip() or not summary.strip():
        raise ValueError("ledger and summary are required")
    ledger_path: Path | None = None
    ledger_sha256 = ""
    review_artifact: dict[str, Any] = {}
    accepted_byte_manifest: dict[str, Any] = {}
    contract_revision: dict[str, str] = {}
    environment_identity: dict[str, Any] = {}
    implementation_started_at = ""
    repair_started_at = ""
    task_name = ""
    structure_revision = 0
    request_state: dict[str, Any] = {}
    finalization_diff: dict[str, Any] | None = None
    selected_repair_package: dict[str, Any] | None = None
    execution_root = project_context(root).code_root
    # Authenticate and reject obviously invalid requests before invoking a
    # command.  The command itself is deliberately outside the state lock: a
    # slow test suite must never block board polling or the watchdog.
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        if developer["role"] not in DEVELOPER_ROLES or not developer.get("vendor"):
            raise ValueError("a development/engineering agent with a declared vendor must request independent review")
        if developer["task"] == AWAITING_OWNER_DIRECTION:
            raise ValueError("delivery agent must await owner direction before requesting review")
        _require_contract_preflight(root, developer["task"])
        if phase == "final_acceptance" and any(
            request.get("task") == developer["task"]
            and request.get("status") not in TERMINAL_QA
            for request in state.get("qa_requests", {}).values()
        ):
            raise ValueError("final acceptance cannot start while another review is active")
        mode, subtask, chunk = _validate_review_scope(state, developer["task"], phase, subtask, chunk)
        packages = _repair_packages_for_scope(
            state, developer["task"], phase, subtask, chunk,
        )
        if repair_package_id:
            packages = [package for package in packages if package.get("id") == repair_package_id]
            if not packages:
                raise ValueError("the requested repair package is not open for this exact review scope")
        if len(packages) > 1:
            raise ValueError("multiple repair packages are open for this scope; select one exact --repair-package")
        if packages:
            package = packages[0]
            if package.get("status") == "under_review":
                raise ValueError("repair package is already under independent review")
            if package.get("status") == "open":
                if package.get("requires_explicit_resolution"):
                    raise ValueError("every explicit repair-package member must be resolved before review")
                if len(changes.strip()) < 8:
                    raise ValueError("repair review requires a concrete changes summary")
                umbrella = (package.get("members") or [{}])[0]
                repair_package_model.resolve(package, [{
                    "id": str(umbrella.get("id") or "F-001"),
                    "resolution": changes.strip(),
                    "regression_check": "The new Delivery ledger re-executes the complete affected review scope.",
                }], developer["id"])
            if package.get("required_test_scope") == "full" and test_scope != "full":
                raise ValueError("repair package contains a strict-risk member and requires full review scope")
            selected_repair_package = json.loads(json.dumps(package))
        if subtask:
            item = state["delivery_plans"][developer["task"]]["subtasks"][subtask]
            if _effective_subtask_pipeline_status(
                state, developer["task"], subtask, item,
            ) != "in_progress":
                raise ValueError(
                    "application subtask review requires start-subtask and an active implementation scope"
                )
        implementation_started_at, repair_started_at = _implementation_scope_start(
            state, developer["task"], phase, subtask, chunk,
        )
        code_root = project_context(root).code_root
        scoped_workspace = state.get("subtask_workspaces", {}).get(developer["task"], {}).get(subtask, "") if subtask else ""
        workspace_value = scoped_workspace or state.get("task_workspaces", {}).get(developer["task"], "")
        execution_root = Path(workspace_value) if workspace_value else code_root
        if not execution_root.is_dir():
            execution_root = code_root
        ledger_path = _require_ledger_scenarios(
            root, str(_task_path_from_state(root, state, developer["task"], ledger)),
            "Scenario Ledger", owner_readable=True,
        )
        _require_completed_ledger(root, str(ledger_path), "Scenario Ledger", owner_readable=True)
        baseline_commit = state.get("task_baselines", {}).get(developer["task"], {}).get("head", "")
        scope_baseline_commit = (
            state.get("subtask_branches", {}).get(developer["task"], {})
            .get(subtask, {}).get("base_commit", "")
            if subtask else baseline_commit
        )
        review_artifact = _git_review_artifact(execution_root, scope_baseline_commit)
        if subtask:
            _require_owned_files(
                item, list(review_artifact.get("files") or []),
                "reviewed candidate manifest",
            )
        contract_revision = _contract_revision(root, developer["task"])
        environment_identity = _environment_identity()
        task_name = str(developer["task"])
        structure_revision = int(
            state.get("delivery_plans", {}).get(task_name, {}).get("structure_revision", 0)
        )
        request_state = json.loads(json.dumps(state))
    if review_artifact.get("available") and not review_artifact.get("immutable_clean"):
        raise ValueError("every governed review requires an immutable clean commit/tree; use broker stage+commit before review")
    if subtask and review_artifact.get("available"):
        accepted_byte_manifest = accepted_bytes.build_manifest(
            execution_root, scope_baseline_commit,
            str(review_artifact.get("commit") or ""), review_artifact.get("files") or [],
        )
    if phase == "final_acceptance" and review_artifact.get("available"):
        finalization_diff = _application_finalization_diff(
            root, request_state, execution_root, task_name,
            str(review_artifact.get("commit") or ""),
        )
    assert ledger_path is not None
    ledger_sha256 = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    simulation_valid, simulation_problems, planned_scenarios = contract.scenario_simulations(ledger_path)
    if not simulation_valid:
        raise ValueError("Scenario Ledger simulation plan is invalid: " + "; ".join(simulation_problems))
    execution_preflight.validate_commands(
        execution_root,
        [test_command, *[scenario["command"] for scenario in planned_scenarios]],
        environment=child_process.execution_environment(),
    )
    delivery_scope = {
        "task": task_name, "structure_revision": structure_revision,
        "phase": phase, "subtask": subtask, "chunk": chunk,
    }
    delivery_candidate = execution_identity.candidate_evidence_identity(
        str(review_artifact.get("commit") or ""),
        str(review_artifact.get("tree_hash") or review_artifact.get("working_tree_digest") or ""),
        str(contract_revision.get("sha256") or ""),
        {"delivery_ledger": ledger_sha256,
         "review_scope": _review_scope_identity(delivery_scope)["sha256"]},
    )
    delivery_certification = {
        "board_root": root, "candidate": delivery_candidate,
        "environment_sha256": environment_identity["sha256"],
        "lockfile_digests": _execution_lockfile_digests(execution_root),
        "role": "delivery", "gate": f"{phase}:{subtask}:{chunk}",
        "retry_reason": changes.strip(),
    }
    # Freeze and route the review before Delivery executes. The Reviewer may
    # author independent intentions against this exact candidate in parallel,
    # but execution and evidence remain unavailable until Delivery succeeds.
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        prior = _task_requests(state, developer["task"])
        active = [request for request in prior if request.get("status") not in TERMINAL_QA]
        if phase == "final_acceptance" and active:
            raise ValueError("final acceptance cannot start while another review is active")
        if any(
            request.get("phase") == phase
            and request.get("subtask", "") == subtask
            and request.get("chunk", "") == chunk
            for request in active
        ):
            raise ValueError("an active review already exists for this delivery scope")
        scope_reviews = [
            request for request in prior
            if request.get("stage") == INDEPENDENT_REVIEW
            and request.get("phase", "legacy") == phase
            and request.get("subtask", "") == subtask
            and request.get("chunk", "") == chunk
        ]
        current_revision = int(
            state.get("delivery_plans", {}).get(developer["task"], {}).get("structure_revision", 0)
        )
        current_reviews = [
            request for request in scope_reviews
            if int(request.get("structure_revision", 0)) == current_revision
        ]
        latest = max(current_reviews, key=lambda request: int(request["cycle"]), default=None)
        if latest and latest["status"] == "passed" and latest.get("reviewed_commit") == review_artifact.get("commit"):
            raise ValueError("independent review already passed for this exact commit; begin a new task or submit a changed clean candidate")
        cycle = max((int(request["cycle"]) for request in scope_reviews), default=0) + 1
        if cycle > 1 and not changes.strip():
            raise ValueError("a re-review cycle requires a concise --changes summary")
        scope_name = f"{subtask}-{chunk}" if subtask else chunk
        safe_chunk = "".join(character if character.isalnum() or character in "-_" else "-" for character in scope_name)
        request_id = f"review-{developer['task']}-{safe_chunk}-{cycle:02d}"
        requested_at = now()
        staged_request = {
            "id": request_id, "task": developer["task"], "cycle": cycle,
            "test_scope": test_scope, "scope_reason": scope_reason.strip(),
            "stage": INDEPENDENT_REVIEW, "phase": phase, "subtask": subtask,
            "chunk": chunk, "delivery_mode": mode, "structure_revision": current_revision,
            "developer_id": developer["id"], "ledger": ledger,
            "ledger_sha256": ledger_sha256, "challenge_ledger": None,
            "summary": summary, "reviewed_commit": review_artifact.get("commit", ""),
            "reviewed_base_commit": review_artifact.get("base_commit", ""),
            "reviewed_tree_hash": review_artifact.get("tree_hash", ""),
            "reviewed_files": review_artifact.get("files", []),
            "reviewed_worktree_digest": review_artifact.get("working_tree_digest", ""),
            "accepted_byte_manifest": accepted_byte_manifest,
            "finalization_diff": finalization_diff,
            "repair_package": selected_repair_package,
            "repair_package_id": (selected_repair_package or {}).get("id", ""),
            "contract_revision": contract_revision, "environment_identity": environment_identity,
            "changes_summary": changes, "status": "authoring", "delivery_state": "executing",
            "staged_authoring": True, "requested_at": requested_at,
            "review_wait_started_at": requested_at, "review_wait_stopped_at": None,
            "claimed_by": None, "claimed_at": None, "result": None,
            "result_summary": None, "evidence": None, "completed_at": None,
            "lifecycle": {"review_queue": {"started_at": requested_at}},
        }
        staged_request["review_brief"] = review_brief_projection.build(
            root, state, staged_request, delivery_scenarios=planned_scenarios,
            include_delivery_evidence=False,
        )
        state["qa_requests"][request_id] = staged_request
        developer.update({
            "status": "executing_delivery_evidence",
            "status_note": f"Delivery evidence is running while Reviewer authors intentions for {request_id}",
            "last_status_at": requested_at,
        })
        _event(state, "review_authoring_opened", developer, {
            "task": developer["task"], "request_id": request_id, "phase": phase,
            "subtask": subtask, "chunk": chunk, "cycle": cycle,
            "message": "Frozen candidate opened for independent intention authoring while Delivery evidence executes",
        })
    route_open_reviews(root)
    # The board executes the evidence outside its state lock. Keep a durable
    # activity lease for the Delivery Agent while that work runs so the
    # watchdog does not report a false stall during a legitimate long check.
    # Bind the lease to the exact staged request so crash recovery cannot
    # confuse another command from the same Delivery session with this gate.
    execution_id = request_id
    unit_measurement: dict[str, Any] = {}
    unit_started_at = lifecycle.now()
    try:
        with review_execution_lease(root, agent_id, execution_id, f"delivery QA: {test_command}"):
            output = _execute_internal_qa(
                test_command, execution_root, measurement=unit_measurement,
                certification=delivery_certification,
            )
            scenario_started_at = lifecycle.now()
            simulation_results = _execute_scenario_simulations(
                execution_root, ledger_path, certification=delivery_certification,
            )
            scenario_finished_at = lifecycle.now()
        if not ledger_path.is_file():
            raise ValueError("Scenario Ledger disappeared while its simulations were executing")
        _require_completed_ledger(root, str(ledger_path), "Scenario Ledger", owner_readable=True)
        if hashlib.sha256(ledger_path.read_bytes()).hexdigest() != ledger_sha256:
            raise ValueError("Scenario Ledger changed while its simulations were executing; rerun internal QA")
    except Exception as error:
        with locked_state(root) as state:
            staged = state.get("qa_requests", {}).get(request_id)
            if staged and staged.get("status") in {"authoring", "open", "reserved"}:
                staged.update({
                    "status": "cancelled", "delivery_state": "failed",
                    "result": "failed", "result_summary": str(error)[:1000],
                    "completed_at": now(), "route_state": "delivery_failed_cancelled",
                })
                reviewer = state.get("agents", {}).get(staged.get("reserved_by", ""))
                if reviewer:
                    reviewer.update({
                        "status": "review_cancelled", "status_note": "Delivery evidence failed before Reviewer execution",
                        "last_status_at": now(),
                    })
                _event(state, "staged_review_cancelled", developer, {
                    "task": staged["task"], "request_id": request_id,
                    "message": "Delivery evidence failed; staged Reviewer execution and verdict remain blocked",
                })
                failures = state.setdefault("delivery_attempt_failures", [])
                failures.append(json.loads(json.dumps(staged)))
                del failures[:-200]
                state["qa_requests"].pop(request_id, None)
        raise
    with locked_state(root) as state:
        developer = _require_writable_agent(state, agent_id)
        if developer["role"] not in DEVELOPER_ROLES or not developer.get("vendor"):
            raise ValueError("a development/engineering agent with a declared vendor must request independent review")
        if developer["task"] == AWAITING_OWNER_DIRECTION:
            raise ValueError("delivery agent must await owner direction before requesting review")
        _require_contract_preflight(root, developer["task"])
        staged_existing = state.get("qa_requests", {}).get(request_id)
        if not staged_existing or staged_existing.get("status") not in {"authoring", "open", "reserved"}:
            raise ValueError("staged review changed while Delivery evidence executed")
        prior = [
            request for request in _task_requests(state, developer["task"])
            if request.get("id") != request_id
        ]
        mode, subtask, chunk = _validate_review_scope(state, developer["task"], phase, subtask, chunk)
        pipeline_item = (
            state["delivery_plans"][developer["task"]]["subtasks"][subtask]
            if subtask else None
        )
        if pipeline_item and _effective_subtask_pipeline_status(
            state, developer["task"], subtask, pipeline_item,
        ) not in {"in_progress", "in_review"}:
            raise ValueError("subtask pipeline changed while Delivery evidence executed; retry from its current state")
        active = [request for request in prior if request["status"] not in TERMINAL_QA]
        if phase == "final_acceptance" and active:
            raise ValueError("final acceptance cannot start while another review is active")
        if any(request.get("phase") == phase and request.get("subtask", "") == subtask and request.get("chunk", "") == chunk for request in active):
            raise ValueError("an active review already exists for this delivery scope")
        scope_reviews = [request for request in prior if request.get("stage") == INDEPENDENT_REVIEW and request.get("phase", "legacy") == phase and request.get("subtask", "") == subtask and request.get("chunk", "") == chunk]
        current_revision = int(state.get("delivery_plans", {}).get(developer["task"], {}).get("structure_revision", 0))
        current_reviews = [request for request in scope_reviews if int(request.get("structure_revision", 0)) == current_revision]
        latest = max(current_reviews, key=lambda request: int(request["cycle"]), default=None)
        if latest and latest["status"] == "passed" and latest.get("reviewed_commit") == review_artifact.get("commit"):
            raise ValueError("independent review already passed for this exact commit; begin a new task or submit a changed clean candidate")
        cycle = max((int(request["cycle"]) for request in scope_reviews), default=0) + 1
        if cycle > 1 and not changes.strip():
            raise ValueError("a re-review cycle requires a concise --changes summary")
        delivery_evidence = _store_internal_qa_evidence(root, test_command, output, simulation_results)
        evidence_sha256 = hashlib.sha256(Path(delivery_evidence).read_bytes()).hexdigest()
        request_lifecycle = dict(staged_existing.get("lifecycle") or {})
        request_lifecycle.update({
            "implementation": lifecycle.phase(implementation_started_at, unit_started_at),
            "unit_execution": lifecycle.phase(
                str(unit_measurement.get("started_at", unit_started_at)),
                str(unit_measurement.get("finished_at", unit_started_at)),
            ),
            "scenario_execution": lifecycle.phase(scenario_started_at, scenario_finished_at),
        })
        request_lifecycle.setdefault("review_queue", {"started_at": requested_at})
        if repair_started_at:
            request_lifecycle["repair"] = lifecycle.phase(repair_started_at, requested_at)
        command_executions = [{"kind": "unit_test", **unit_measurement}]
        for simulation in simulation_results:
            command_executions.append({
                key: simulation[key]
                for key in (
                    "id", "command", "command_fingerprint", "started_at", "finished_at", "duration_seconds",
                    "exit_code", "cache_decision", "deduplicated_from",
                )
                if key in simulation
            } | {"kind": "delivery_scenario"})
        repair_context = _repair_context(
            root, state, execution_root,
            scope_reviews, str(review_artifact.get("commit") or ""))
        request = {
            "id": request_id, "task": developer["task"], "cycle": cycle,
            "repair_context": repair_context,
            "test_scope": test_scope, "scope_reason": scope_reason.strip(),
            "stage": INDEPENDENT_REVIEW, "phase": phase, "subtask": subtask, "chunk": chunk, "delivery_mode": mode,
            "structure_revision": int(state.get("delivery_plans", {}).get(developer["task"], {}).get("structure_revision", 0)),
            "developer_id": developer["id"],
            "ledger": ledger, "challenge_ledger": None, "summary": summary,
            "delivery_evidence": delivery_evidence,
            "unit_test_command": test_command,
            "unit_test_evidence": delivery_evidence,
            "ledger_sha256": ledger_sha256,
            "delivery_simulations": {
                "scenario_ids": [item["id"] for item in simulation_results],
                "executed_count": sum(item["outcome"] == "passed" for item in simulation_results),
                "approved_exception_ids": [item["id"] for item in simulation_results if item["outcome"] == "approved_exception"],
                "evidence": delivery_evidence,
                "evidence_sha256": evidence_sha256,
            },
            "reviewed_commit": review_artifact.get("commit", ""),
            "reviewed_base_commit": review_artifact.get("base_commit", ""),
            "reviewed_tree_hash": review_artifact.get("tree_hash", ""),
            "reviewed_files": review_artifact.get("files", []),
            "reviewed_worktree_digest": review_artifact.get("working_tree_digest", ""),
            "accepted_byte_manifest": accepted_byte_manifest,
            "finalization_diff": finalization_diff,
            "repair_package": selected_repair_package,
            "repair_package_id": (selected_repair_package or {}).get("id", ""),
            "subtask_base_commit": (
                state.get("subtask_branches", {}).get(developer["task"], {})
                .get(subtask, {}).get("base_commit", "")
                if subtask else ""
            ),
            "contract_revision": contract_revision,
            "environment_identity": environment_identity,
            "changes_summary": changes,
            "status": "reserved" if staged_existing.get("status") == "reserved" else "open",
            "delivery_state": "passed", "staged_authoring": True,
            "requested_at": requested_at,
            "claimed_by": None, "claimed_at": None, "result": None,
            "result_summary": None, "evidence": None, "completed_at": None,
            "review_wait_started_at": requested_at, "review_wait_stopped_at": None,
            "lifecycle": request_lifecycle,
            "command_executions": command_executions,
        }
        for key in (
            "reserved_by", "reserved_at", "routed_to", "routed_session_id",
            "routed_at", "route_attempts", "route_state",
            "route_transport_state", "route_instruction_id",
            "reviewer_initial_intents", "reviewer_intents_recorded_at",
            "reviewer_intent_amendments", "reviewer_authoring_overlap_started",
        ):
            if key in staged_existing:
                request[key] = json.loads(json.dumps(staged_existing[key]))
        request["review_brief"] = review_brief_projection.build(
            root, state, request, delivery_scenarios=planned_scenarios,
            include_delivery_evidence=(
                not request.get("reviewer_authoring_overlap_started")
                or bool(request.get("reviewer_initial_intents"))
            ),
        )
        state["qa_requests"][request_id] = request
        if selected_repair_package:
            current_package = state.get("repair_packages", {}).get(selected_repair_package["id"])
            if (
                not current_package
                or current_package.get("status") != "ready_for_review"
                or current_package.get("sha256") != selected_repair_package.get("sha256")
            ):
                raise ValueError("repair package changed while Delivery evidence executed")
            current_package.update({
                "status": "under_review", "review_request_id": request_id,
                "review_started_at": now(),
            })
            repair_package_model.refresh_digest(current_package)
        if state.get("task_repositories", {}).get(developer["task"]):
            if not request.get("reviewed_commit") or not request.get("reviewed_tree_hash"):
                raise ValueError("governed review could not resolve an immutable commit and tree")
            review_number = sum(1 for item in _task_requests(state, developer["task"]) if item.get("id") != request_id) + 1
            broker = _broker_for_state(root, state, developer["task"])
            mirror = broker.create_review_ref(
                request,
                review_number=review_number,
                board_mutation=lambda record: request.update({
                    "mirror_ref": record["ref"],
                    "mirror_commit": record["commit"],
                    "mirror_tree_hash": record["tree"],
                    "mirror_transaction_id": record["transaction_id"],
                }),
            )
            request["mirror_ref"] = mirror["ref"]
        if pipeline_item:
            pipeline_item.update({
                "pipeline_status": "in_review",
                "active_review_request": request_id,
                "pipeline_updated_at": requested_at,
            })
        another_active_subtask = any(
            name != subtask
            and _effective_subtask_pipeline_status(
                state, developer["task"], name, value,
            ) == "in_progress"
            for name, value in state.get("delivery_plans", {}).get(
                developer["task"], {},
            ).get("subtasks", {}).items()
        )
        developer.update({
            "status": "implementing_subtask" if another_active_subtask else "awaiting_independent_review",
            "status_note": (
                f"review cycle {cycle} requested; another independent subtask remains in progress"
                if another_active_subtask else f"review cycle {cycle} requested"
            ),
            "last_status_at": now(),
        })
        _event(state, "independent_review_requested", developer, {"task": developer["task"], "request_id": request_id, "stage": INDEPENDENT_REVIEW, "phase": phase, "subtask": subtask, "chunk": chunk, "cycle": cycle, "ledger": ledger, "evidence": delivery_evidence, "scenario_count": len(simulation_results), "lifecycle": request_lifecycle, "command_execution_count": len([item for item in request["command_executions"] if item.get("cache_decision") != "same_request_deduplicated"]), "message": summary})
        created = dict(request)
    route_open_reviews(root)
    return dict(snapshot(root)["qa_requests"].get(request_id, created))


def review_brief(
    root: ProjectRoot, request_id: str, agent_id: str = "",
) -> dict[str, Any]:
    """Rebuild one bounded brief from the exact project board."""
    with locked_state(root) as state:
        request = state.get("qa_requests", {}).get(request_id)
        if not request:
            raise ValueError("review request does not exist in this project")
        if agent_id:
            reviewer = _require_agent(state, agent_id)
            assigned = (
                reviewer.get("role") == "qa"
                and reviewer.get("task") in {"REVIEW_QUEUE", request.get("task")}
                and reviewer.get("id") in {
                    request.get("routed_to"), request.get("reserved_by"), request.get("claimed_by"),
                }
            )
            if not assigned:
                raise ValueError("review brief is available only to the Reviewer assigned this exact request")
        ledger = _task_path_from_state(root, state, request["task"], str(request.get("ledger") or ""))
        valid, problems, scenarios = contract.scenario_submission_simulations(ledger)
        if not valid:
            raise ValueError("review brief requires an intact Delivery ledger: " + "; ".join(problems))
        include_delivery = (
            request.get("delivery_state") == "passed"
            and (
                not request.get("reviewer_authoring_overlap_started")
                or bool(request.get("reviewer_initial_intents"))
            )
        )
        result = review_brief_projection.build(
            root, state, request, delivery_scenarios=scenarios,
            include_delivery_evidence=include_delivery,
        )
        request["review_brief"] = result
        return json.loads(json.dumps(result))


def reserve_qa(root: Path, agent_id: str, request_id: str = "") -> dict[str, Any]:
    """Immediately reserve an independent review before ledger authoring."""
    with locked_state(root) as state:
        qa = _require_agent(state, agent_id)
        if qa["role"] != "qa":
            raise ValueError("only QA agents may reserve QA requests")
        held = next((
            item for item in state.get("qa_requests", {}).values()
            if item.get("status") in {"reserved", "claimed"}
            and (
                item.get("reserved_by") == qa["id"]
                or item.get("claimed_by") == qa["id"]
            )
        ), None)
        if held:
            raise ValueError(
                f"reviewer already holds active request {held.get('id')}; "
                "submit its verdict or release it before reserving another"
            )
        candidates = [r for r in state["qa_requests"].values() if r["status"] in {"authoring", "open"}]
        if request_id:
            candidates = [r for r in candidates if r["id"] == request_id]
        if not candidates:
            raise ValueError("no open QA request available")
        request = sorted(candidates, key=lambda r: r["requested_at"])[0]
        routed_to = request.get("routed_to")
        if routed_to and routed_to != qa["id"]:
            raise ValueError(f"review request is already routed to {routed_to}")
        developer = _require_agent(state, request["developer_id"])
        if request.get("stage") == INDEPENDENT_REVIEW:
            if not qa.get("vendor") or qa.get("vendor") == developer.get("vendor"):
                raise ValueError("independent review requires a QA agent from a declared different vendor")
            if request.get("delivery_state") != "executing":
                delivery_valid, delivery_problems = simulation_evidence_complete(root, request, "delivery_simulations", "ledger", state)
                if not delivery_valid:
                    raise ValueError("independent review requires intact Delivery scenario evidence: " + "; ".join(delivery_problems))
            reserved_at = now()
            request_lifecycle = request.setdefault("lifecycle", {})
            queue_started = str(request_lifecycle.get("review_queue", {}).get("started_at") or request.get("requested_at", ""))
            request_lifecycle["review_queue"] = lifecycle.phase(queue_started, reserved_at)
            request_lifecycle["challenge_authoring"] = {"started_at": reserved_at}
            request.update({
                "status": "reserved", "reserved_by": qa["id"], "reserved_at": reserved_at,
                "authoring_last_activity_at": reserved_at,
                "routed_to": qa["id"], "routed_session_id": qa.get("session_id", ""),
                "route_state": "preparing_challenge_ledger",
            })
            if request.get("delivery_state") == "executing":
                request["reviewer_authoring_overlap_started"] = True
            qa.update({"task": request["task"], "status": "qa_preparing", "status_note": f"preparing a distinct Challenge Ledger for {request['id']}", "last_status_at": reserved_at})
            _event(state, "qa_reserved", qa, {"task": request["task"], "request_id": request["id"], "stage": request.get("stage"), "cycle": request["cycle"], "message": "reviewer reserved the request and is preparing a distinct Challenge Ledger"})
            return dict(request)
        claimed_at = now()
        request_lifecycle = request.setdefault("lifecycle", {})
        queue_started = str(request_lifecycle.get("review_queue", {}).get("started_at") or request.get("requested_at", ""))
        request_lifecycle["review_queue"] = lifecycle.phase(queue_started, claimed_at)
        request_lifecycle["formal_review"] = {"started_at": claimed_at}
        request.update({
            "status": "claimed", "claimed_by": qa["id"], "claimed_at": claimed_at,
            "routed_to": qa["id"], "routed_session_id": qa.get("session_id", ""),
            "route_state": "claimed",
        })
        qa.update({"task": request["task"], "status": "qa_testing", "status_note": f"claimed {request['id']}", "last_status_at": now()})
        _event(state, "qa_claimed", qa, {"task": request["task"], "request_id": request["id"], "stage": request.get("stage", DEVELOPMENT_QA), "cycle": request["cycle"], "ledger": request["ledger"], "challenge_ledger": None})
        return dict(request)


def record_review_intents(
    root: ProjectRoot, agent_id: str, request_id: str,
    intents: list[str], *, amendment: bool = False,
) -> dict[str, Any]:
    """Freeze Reviewer-authored scenario intentions before evidence is revealed."""
    prepared = [str(value or "").strip() for value in intents]
    if not prepared or len(prepared) > 20:
        raise ValueError("record between 1 and 20 independent review intents")
    if any(len(value) < 20 or len(value) > 1000 for value in prepared):
        raise ValueError("each review intent must be 20 to 1000 characters")
    if any(re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value) for value in prepared):
        raise ValueError("review intents must not contain control characters")
    with locked_state(root) as state:
        qa = _require_agent(state, agent_id)
        request = state.get("qa_requests", {}).get(request_id)
        if (
            qa.get("role") != "qa" or not request
            or request.get("status") != "reserved"
            or request.get("reserved_by") != qa.get("id")
        ):
            raise ValueError("only the reviewer holding this reservation may record intents")
        field = "reviewer_intent_amendments" if amendment else "reviewer_initial_intents"
        if amendment and not request.get("reviewer_initial_intents"):
            raise ValueError("record initial review intents before recording an amendment")
        if not amendment and request.get(field):
            if [str(item.get("text") or "") for item in request[field]] == prepared:
                return dict(request)
            raise ValueError("initial review intents are immutable; record an amendment instead")
        records = [{"text": value, "at": now(), "reviewer_id": qa["id"]} for value in prepared]
        request["authoring_last_activity_at"] = records[-1]["at"]
        if amendment:
            request.setdefault(field, []).extend(records)
        else:
            request[field] = records
            request["reviewer_intents_recorded_at"] = records[0]["at"]
        request["review_brief"] = review_brief_projection.build(
            root, state, request,
            include_delivery_evidence=(request.get("delivery_state") == "passed"),
        )
        _event(state, "reviewer_intents_recorded", qa, {
            "task": request["task"], "request_id": request_id,
            "count": len(prepared), "amendment": amendment,
            "message": "Reviewer recorded independent scenario intentions before execution",
        })
        return json.loads(json.dumps(request))


def attach_challenge_ledger(
    root: Path, agent_id: str, request_id: str, challenge_ledger: str,
    correction_reason: str = "",
) -> dict[str, Any]:
    """Validate an initial ledger or explicitly correct one after failed execution."""
    if not challenge_ledger.strip():
        raise ValueError("reviewer Challenge Ledger path is required")
    with locked_state(root) as state:
        qa = _require_agent(state, agent_id)
        request = state.get("qa_requests", {}).get(request_id)
        initial_attach = bool(
            qa.get("role") == "qa" and request
            and request.get("status") == "reserved"
            and request.get("reserved_by") == qa.get("id")
        )
        attempts = list((request or {}).get("challenge_execution_attempts") or [])
        correction = bool(
            qa.get("role") == "qa" and request
            and request.get("status") == "claimed"
            and request.get("claimed_by") == qa.get("id")
            and not request.get("challenge_execution")
            and attempts and attempts[-1].get("status") in {"failed", "interrupted"}
        )
        if not initial_attach and not correction:
            raise ValueError("only the reviewer that reserved this request may attach its Challenge Ledger")
        correction_reason = str(correction_reason or "").strip()
        if correction and len(correction_reason) < 8:
            raise ValueError("correcting a failed Challenge Ledger requires an explicit reason")
        if initial_attach and correction_reason:
            raise ValueError("a correction reason is valid only after a failed certified execution")
        developer = _require_agent(state, request["developer_id"])
        if request.get("stage") != INDEPENDENT_REVIEW or not qa.get("vendor") or qa.get("vendor") == developer.get("vendor"):
            raise ValueError("a cross-vendor independent review reservation is required")
        if request.get("delivery_state") == "executing":
            raise ValueError("Reviewer execution is blocked until Delivery evidence succeeds")
        if request.get("delivery_state") == "failed":
            raise ValueError("Delivery evidence failed; this staged review is cancelled")
        if (
            request.get("reviewer_authoring_overlap_started")
            and not request.get("reviewer_initial_intents")
        ):
            raise ValueError("record independent review intents before attaching the Challenge Ledger")
        delivery_valid, delivery_problems = simulation_evidence_complete(root, request, "delivery_simulations", "ledger", state)
        if not delivery_valid:
            raise ValueError("independent review requires intact Delivery scenario evidence: " + "; ".join(delivery_problems))
        source = _task_path_from_state(root, state, request["task"], request["ledger"])
        challenge = _require_ledger_scenarios(
            root, str(_task_path_from_state(root, state, request["task"], challenge_ledger)),
            "reviewer Challenge Ledger", owner_readable=True,
        )
        if challenge == source:
            raise ValueError("reviewer Challenge Ledger must be distinct from the delivery Scenario Ledger")
        source_scenarios = contract.scenario_fingerprints(source)
        challenge_scenarios = contract.scenario_fingerprints(challenge)
        if not challenge_scenarios - source_scenarios:
            raise ValueError("reviewer Challenge Ledger must introduce at least one scenario command beyond the delivery ledger")
        attached_at = now()
        challenge_digest = hashlib.sha256(challenge.read_bytes()).hexdigest()
        if correction:
            request.setdefault("challenge_ledger_revisions", []).append({
                "challenge_ledger": request.get("challenge_ledger", ""),
                "challenge_ledger_sha256": request.get("challenge_ledger_sha256", ""),
                "authorization": json.loads(json.dumps(
                    request.get("challenge_execution_authorization") or {}
                )),
                "failed_attempt": json.loads(json.dumps(attempts[-1])),
                "correction_reason": correction_reason,
                "replaced_at": now(),
                "reviewer_id": qa["id"],
            })
        authorization_fields = {
            "request_id": request["id"], "reviewer_id": qa["id"],
            "reviewed_commit": request.get("reviewed_commit", ""),
            "challenge_ledger_sha256": challenge_digest,
        }
        authorization = {
            **authorization_fields,
            "sha256": hashlib.sha256(json.dumps(
                authorization_fields, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest(),
            "authorized_at": attached_at,
        }
        request_lifecycle = request.setdefault("lifecycle", {})
        challenge_started = str(request_lifecycle.get("challenge_authoring", {}).get("started_at") or request.get("reserved_at", ""))
        request_lifecycle["challenge_authoring"] = lifecycle.phase(challenge_started, attached_at)
        request_lifecycle["formal_review"] = {"started_at": attached_at}
        request.update({
            "challenge_ledger": challenge_ledger,
            "challenge_ledger_sha256": challenge_digest,
            "challenge_execution_authorization": authorization,
            "challenge_ledger_attached_at": attached_at,
            "challenge_ledger_correction_reason": correction_reason,
            "status": "claimed", "claimed_by": qa["id"], "claimed_at": attached_at,
            "route_state": "executing_review",
        })
        qa.update({"task": request["task"], "status": "qa_testing", "status_note": f"executing {request['id']} with a validated distinct Challenge Ledger", "last_status_at": attached_at})
        _event(state, "qa_challenge_ledger_corrected" if correction else "qa_challenge_ledger_attached", qa, {
            "task": request["task"], "request_id": request["id"],
            "cycle": request["cycle"], "challenge_ledger": challenge_ledger,
            "correction_reason": correction_reason,
            "message": (
                "failed Challenge Ledger explicitly corrected; prior attempt remains auditable"
                if correction else
                "distinct Challenge Ledger validated; independent review execution may begin"
            ),
        })
        if not correction:
            _event(state, "qa_claimed", qa, {"task": request["task"], "request_id": request["id"], "stage": request.get("stage", DEVELOPMENT_QA), "cycle": request["cycle"], "ledger": request["ledger"], "challenge_ledger": challenge_ledger})
        return dict(request)


def claim_qa(root: Path, agent_id: str, request_id: str = "", challenge_ledger: str = "") -> dict[str, Any]:
    """Compatibility command: reserve, then attach when a ledger is supplied."""
    current = snapshot(root).get("qa_requests", {})
    request = current.get(request_id) if request_id else next((
        value for value in sorted(current.values(), key=lambda value: value.get("requested_at", ""))
        if value.get("status") in {"authoring", "open", "reserved"}
    ), None)
    if request and request.get("status") == "reserved":
        if request.get("reserved_by") != agent_id:
            raise ValueError(f"review request is already reserved by {request.get('reserved_by')}")
        if not challenge_ledger.strip():
            return dict(request)
        return attach_challenge_ledger(root, agent_id, request["id"], challenge_ledger)
    reserved = reserve_qa(root, agent_id, request_id)
    if reserved.get("stage") != INDEPENDENT_REVIEW or not challenge_ledger.strip():
        return reserved
    return attach_challenge_ledger(root, agent_id, reserved["id"], challenge_ledger)


def migrate_reviewer_ledgers(root: Path) -> dict[str, Any]:
    """Move legacy reviewer ledgers into ignored durable board evidence paths."""
    context = project_context(root)
    code_root = context.code_root
    migrated = []
    with locked_state(root) as state:
        requests = list(state.get("qa_requests", {}).values()) + [
            entry["value"] for entry in state.get("archive", []) if entry.get("kind") == "qa_request"
        ]
        for request in requests:
            original = str(request.get("challenge_ledger", ""))
            if not original.startswith("docs/") or "reviewer-challenge" not in original:
                continue
            relative = Path(original)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"review ledger path traversal is not allowed: {original}")
            tracked = git_process.run(["git", "ls-files", "--error-unmatch", original], cwd=code_root, capture_output=True, text=True)
            if tracked.returncode == 0:
                # Historical committed evidence is immutable and remains at its
                # recorded path; only untracked reviewer artifacts are relocated.
                continue
            source = (code_root / relative).resolve()
            if not source.is_relative_to(code_root):
                raise ValueError(f"review ledger path escapes project root: {original}")
            destination = context.storage_path("reviews", source.name)
            if source.is_file() and not destination.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
            if not destination.is_file():
                continue
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            prior_digest = request.get("challenge_ledger_sha256")
            if prior_digest and prior_digest != digest:
                raise ValueError(f"review ledger digest changed during migration: {original}")
            request["challenge_ledger_legacy_path"] = original
            request["challenge_ledger"] = str(destination.relative_to(code_root)) if destination.is_relative_to(code_root) else str(destination)
            request["challenge_ledger_sha256"] = prior_digest or digest
            migrated.append({"request_id": request["id"], "legacy_path": original, "path": request["challenge_ledger"], "sha256": request["challenge_ledger_sha256"]})
            if source.is_file() and source != destination:
                source.unlink()
        if migrated:
            _event(state, "review_ledgers_migrated", None, {"message": f"migrated {len(migrated)} reviewer ledgers into durable board evidence", "ledgers": migrated})
    return {"migrated": migrated}


@contextmanager
def _challenge_execution_lock(root: Path, request_id: str) -> Iterator[None]:
    """Serialize one review request across threads and harness processes."""
    lock_dir = board_dir(root) / "challenge-execution-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(request_id.encode("utf-8")).hexdigest() + ".lock"
    with (lock_dir / lock_name).open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _challenge_retry_reason(prior_attempts: list[dict[str, Any]], retry_reason: str) -> str:
    """Resolve the recorded reason a certified challenge may run again.

    A genuine command failure still demands the reviewer's own written reason.
    A recorded system interruption is its own auditable reason: completed
    scenarios reuse their certified results and only the remainder executes,
    so no model-authored justification turn is required to resume.
    """
    retry_reason = str(retry_reason or "").strip()
    if not prior_attempts or prior_attempts[-1].get("status") not in {"failed", "interrupted"}:
        return retry_reason
    if retry_reason:
        return retry_reason
    last_attempt = prior_attempts[-1]
    if last_attempt.get("status") == "interrupted":
        return (
            f"resumed after recorded system interruption at "
            f"{last_attempt.get('recorded_at', '')}: "
            f"{last_attempt.get('reason', 'execution lease expired')}"
        )[:300]
    raise ValueError(
        "retrying a failed certified challenge requires a non-empty repair reason"
    )


def _execute_challenge_locked(
    root: Path, agent_id: str, request_id: str, retry_reason: str = "",
) -> dict[str, Any]:
    """P1 step 1: board-execute the claimed Challenge Ledger exactly once.

    Certifies every scenario into the execution store and stamps the request,
    so the verdict step never re-executes. The reviewer reads the certified
    outputs instead of running commands itself — independent authorship stays;
    the forced duplicate run dies.
    """
    import shlex
    from harness import execution_identity as _xid
    with locked_state(root) as state:
        qa = _require_agent(state, agent_id)
        request = state["qa_requests"].get(request_id)
        if qa["role"] != "qa" or not request or request.get("claimed_by") != qa["id"] or request.get("status") != "claimed":
            raise ValueError("only the QA agent that claimed this request may execute its challenge")
        ledger_value = request.get("challenge_ledger") or ""
        if not ledger_value:
            raise ValueError("execute-challenge requires an attached Challenge Ledger")
        simulation_path = _task_path_from_state(root, state, request["task"], ledger_value)
        review_state = json.loads(json.dumps(state))
        review_request = json.loads(json.dumps(request))
        simulation_digest = hashlib.sha256(simulation_path.read_bytes()).hexdigest()
        authorization = request.get("challenge_execution_authorization") or {}
        authorization_fields = {
            "request_id": request["id"], "reviewer_id": qa["id"],
            "reviewed_commit": request.get("reviewed_commit", ""),
            "challenge_ledger_sha256": simulation_digest,
        }
        expected_authorization = hashlib.sha256(json.dumps(
            authorization_fields, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        if authorization.get("sha256") != expected_authorization:
            raise ValueError(
                "challenge execution requires the exact authorization created when "
                "this ledger was attached"
            )
        existing_execution = request.get("challenge_execution") or {}
        if existing_execution.get("ledger_sha256") == simulation_digest:
            bundle = existing_execution.get("bundle") or {}
            return {
                "execution_id": existing_execution.get("id", ""),
                "ledger_sha256": simulation_digest,
                "scenarios": len(bundle.get("scenario_ids", [])),
                "evidence": bundle.get("evidence", ""),
                "evidence_sha256": bundle.get("evidence_sha256", ""),
                "status": "already_certified",
            }
        prior_attempts = list(request.get("challenge_execution_attempts") or [])
        retry_reason = _challenge_retry_reason(prior_attempts, retry_reason)
    simulations: list[dict[str, Any]] = []
    candidate: dict[str, Any] = {}
    lockfile_digests: dict[str, str] = {}
    environment_sha256 = str(
        (review_request.get("environment_identity") or {}).get("sha256") or ""
    )
    candidate_artifacts = {
        "challenge_ledger": simulation_digest,
        "review_scope": _review_scope_identity(review_request)["sha256"],
    }
    if not (
        review_request.get("reviewed_commit")
        or review_request.get("reviewed_tree_hash")
        or review_request.get("reviewed_worktree_digest")
    ):
        # Legacy non-Git reviews cannot prove byte identity across requests. Keep
        # retries within one request linked, but never cross-reuse another cycle.
        candidate_artifacts["legacy_review_request"] = request_id
    candidate = _xid.candidate_evidence_identity(
        str(review_request.get("reviewed_commit") or ""),
        str(review_request.get("reviewed_tree_hash") or review_request.get("reviewed_worktree_digest") or ""),
        str((review_request.get("contract_revision") or {}).get("sha256") or ""),
        candidate_artifacts,
    )
    try:
        with review_execution_lease(root, agent_id, request_id, f"challenge execution: {simulation_path}"):
            with _review_candidate_checkout(root, review_state, review_request, simulation_path) as (candidate_root, candidate_ledger):
                lockfile_digests = _execution_lockfile_digests(candidate_root)
                started = datetime.now(timezone.utc)
                simulations = _execute_scenario_simulations(
                    candidate_root, candidate_ledger,
                    certification={
                        "board_root": root, "candidate": candidate,
                        "environment_sha256": environment_sha256,
                        "lockfile_digests": lockfile_digests,
                        "role": "reviewer",
                        "gate": f"{review_request.get('phase', '')}:{review_request.get('subtask', '')}:{review_request.get('chunk', '')}",
                        "retry_reason": retry_reason.strip(),
                    },
                )
                duration = (datetime.now(timezone.utc) - started).total_seconds()
                _require_completed_ledger(
                    candidate_root, str(candidate_ledger), "reviewer Scenario Ledger",
                    owner_readable=True,
                )
                if hashlib.sha256(candidate_ledger.read_bytes()).hexdigest() != simulation_digest:
                    raise ValueError("reviewer Scenario Ledger changed while its simulations were executing; rerun execute-challenge")
            bundle = _simulation_bundle(root, "reviewer Challenge Ledger", simulations)
    except Exception as error:
        with locked_state(root) as state:
            current_request = state.get("qa_requests", {}).get(request_id)
            if current_request and current_request.get("claimed_by") == agent_id:
                current_request.setdefault("challenge_execution_attempts", []).append({
                    "status": "failed", "recorded_at": now(),
                    "ledger_sha256": simulation_digest,
                    "retry_reason": retry_reason.strip(),
                    "reason": str(error)[:1000],
                })
                _event(state, "challenge_execution_failed", state.get("agents", {}).get(agent_id), {
                    "task": current_request.get("task", ""), "request_id": request_id,
                    "message": "certified challenge execution failed; any retry requires a recorded reason",
                })
        raise
    identities = []
    scenario_identities = []
    for simulation in simulations:
        try:
            argv = shlex.split(str(simulation.get("command") or ""))
        except ValueError:
            argv = [str(simulation.get("command") or "")]
        run = _xid.command_run_identity(
            candidate, argv, ".", environment_sha256, lockfile_digests,
            role="reviewer",
            gate=f"{review_request.get('phase', '')}:{review_request.get('subtask', '')}:{review_request.get('chunk', '')}",
        )
        simulation_output = str(simulation.get("output", ""))
        simulation_output_sha256 = hashlib.sha256(simulation_output.encode()).hexdigest()
        certification = _xid.scenario_certification(run, simulation_digest, str(simulation.get("id") or ""))
        _xid.certify(root, run,
                     exit_code=0 if str(simulation.get("outcome", "")).lower() in {"pass", "passed"} else 1,
                     output_sha256=simulation_output_sha256,
                     duration_seconds=float(simulation.get("duration_seconds", duration / max(1, len(simulations)))),
                     scenario=certification, output=simulation_output)
        _xid.certify(
            root, certification,
            exit_code=0 if str(simulation.get("outcome", "")).lower() in {"pass", "passed"} else 1,
            output_sha256=simulation_output_sha256,
            duration_seconds=float(simulation.get("duration_seconds", duration / max(1, len(simulations)))),
        )
        identities.append(run["sha256"])
        scenario_identities.append(certification["sha256"])
    execution_id = f"challenge-exec-{secrets.token_hex(6)}"
    with locked_state(root) as state:
        request = state["qa_requests"].get(request_id)
        if not request or request.get("claimed_by") != agent_id or request.get("status") != "claimed":
            raise ValueError("request changed while the challenge executed; rerun execute-challenge")
        request["challenge_execution"] = {
            "id": execution_id,
            "ledger_sha256": simulation_digest,
            "bundle": bundle,
            "candidate_identity": candidate,
            "environment_sha256": environment_sha256,
            "lockfile_digests": lockfile_digests,
            "identities": identities,
            "scenario_identities": scenario_identities,
            "executed_at": now(),
            "retry_reason": retry_reason.strip(),
        }
        request.setdefault("challenge_execution_attempts", []).append({
            "status": "passed", "recorded_at": now(),
            "ledger_sha256": simulation_digest,
            "retry_reason": retry_reason.strip(),
            "execution_id": execution_id,
        })
        for simulation in simulations:
            request.setdefault("command_executions", []).append({
                key: simulation[key]
                for key in (
                    "id", "command", "command_fingerprint", "started_at",
                    "finished_at", "duration_seconds", "exit_code",
                    "cache_decision", "deduplicated_from",
                )
                if key in simulation
            } | {"kind": "reviewer_scenario"})
        _event(state, "challenge_executed", state["agents"][agent_id], {
            "task": request["task"],
            "request_id": request_id,
            "execution_id": execution_id,
            "scenario_count": len(simulations),
            "message": f"Challenge Ledger board-executed once and certified ({len(simulations)} scenarios); the verdict step will not re-run it",
        })
        return {
            "execution_id": execution_id,
            "ledger_sha256": simulation_digest,
            "scenarios": len(simulations),
            "evidence": bundle.get("evidence", ""),
            "evidence_sha256": bundle.get("evidence_sha256", ""),
        }


def execute_challenge(
    root: Path, agent_id: str, request_id: str, retry_reason: str = "",
) -> dict[str, Any]:
    """Execute and certify one Challenge Ledger at most once per request."""
    with _challenge_execution_lock(root, request_id):
        return _execute_challenge_locked(root, agent_id, request_id, retry_reason)


def qa_result(
    root: Path, agent_id: str, request_id: str, result: str, summary: str,
    evidence: str, finalization_classification: str = "",
    failure_members: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if result not in {"passed", "failed"}:
        raise ValueError("QA result must be passed or failed")
    if not summary.strip() or not evidence.strip():
        raise ValueError("QA result summary and executable-test evidence are required")
    evidence_path = Path(_require_evidence_file(root, evidence, "QA evidence"))
    route_session = ""
    routed_text = ""
    simulation_bundle = None
    simulation_digest = ""
    simulation_ledger_value = ""
    simulation_field = ""
    simulations: list[dict[str, Any]] = []
    review_state: dict[str, Any] | None = None
    review_request: dict[str, Any] | None = None
    with locked_state(root) as state:
        qa = _require_agent(state, agent_id)
        request = state["qa_requests"].get(request_id)
        if qa["role"] != "qa" or not request or request["claimed_by"] != qa["id"] or request["status"] != "claimed":
            raise ValueError("only the QA agent that claimed this request may submit its result")
        finalization = request.get("finalization_diff")
        classification = str(finalization_classification or "").strip().lower()
        if finalization:
            if classification not in {"accepted", "rejected"}:
                raise ValueError(
                    "final application review requires explicit accepted or rejected finalization classification"
                )
            if result == "passed" and classification != "accepted":
                raise ValueError("a rejected finalization classification cannot receive a PASS verdict")
        elif classification:
            raise ValueError("finalization classification is valid only for a computed application finalization diff")
        if result == "passed":
            _require_completed_ledger(
                root, str(_task_path_from_state(root, state, request["task"], request["ledger"])),
                "delivery Scenario Ledger", owner_readable=True,
            )
            if request.get("stage") == INDEPENDENT_REVIEW:
                delivery_valid, delivery_problems = simulation_evidence_complete(root, request, "delivery_simulations", "ledger", state)
                if not delivery_valid:
                    raise ValueError("cannot record PASS without intact Delivery scenario evidence: " + "; ".join(delivery_problems))
                _require_completed_ledger(
                    root, str(_task_path_from_state(root, state, request["task"], request.get("challenge_ledger", ""))),
                    "reviewer Challenge Ledger", owner_readable=True,
                )
                simulation_ledger_value = request["challenge_ledger"]
                simulation_field = "reviewer_simulations"
            else:
                simulation_ledger_value = request["ledger"]
                simulation_field = "qa_simulations"
            simulation_path = _task_path_from_state(root, state, request["task"], simulation_ledger_value)
            review_state = json.loads(json.dumps(state))
            review_request = json.loads(json.dumps(request))
            simulation_digest = hashlib.sha256(simulation_path.read_bytes()).hexdigest()
    # Reviewer commands run outside the board lock so board polls remain live.
    certified = None
    if result == "passed" and simulation_field == "reviewer_simulations":
        certified = (review_request or {}).get("challenge_execution")
        if certified and certified.get("ledger_sha256") != simulation_digest:
            raise ValueError(
                "Challenge Ledger changed after its certified execution; run "
                "execute-challenge again before submitting a verdict")
    if result == "passed" and certified:
        simulations = []
        simulation_bundle = certified.get("bundle")
    elif result == "passed" and simulation_field == "reviewer_simulations":
        raise ValueError(
            "independent reviewer PASS requires execute-challenge certification; "
            "the verdict step never executes or substitutes the reviewer ledger"
        )
    elif result == "passed":
        execution = (
            review_execution_lease(root, agent_id, request_id, f"scenario simulations: {simulation_path}")
            if simulation_field == "reviewer_simulations" else nullcontext()
        )
        with execution:
            with _review_candidate_checkout(root, review_state or {}, review_request or {}, simulation_path) as (candidate_root, candidate_ledger):
                simulations = _execute_scenario_simulations(candidate_root, candidate_ledger)
                _require_completed_ledger(
                    candidate_root, str(candidate_ledger), "reviewer Scenario Ledger",
                    owner_readable=True,
                )
                if hashlib.sha256(candidate_ledger.read_bytes()).hexdigest() != simulation_digest or hashlib.sha256(simulation_path.read_bytes()).hexdigest() != simulation_digest:
                    raise ValueError("reviewer Scenario Ledger changed while its simulations were executing; rerun review")
            label = "reviewer Challenge Ledger" if simulation_field == "reviewer_simulations" else "development QA Scenario Ledger"
            simulation_bundle = _simulation_bundle(root, label, simulations)
    verdict_started_at = lifecycle.now()
    with locked_state(root) as state:
        qa = _require_agent(state, agent_id)
        request = state["qa_requests"].get(request_id)
        if qa["role"] != "qa" or not request or request["claimed_by"] != qa["id"] or request["status"] != "claimed":
            raise ValueError("only the QA agent that claimed this request may submit its result")
        if result == "passed":
            current_value = request["challenge_ledger"] if simulation_field == "reviewer_simulations" else request["ledger"]
            current_path = _task_path_from_state(root, state, request["task"], current_value)
            if current_value != simulation_ledger_value or hashlib.sha256(current_path.read_bytes()).hexdigest() != simulation_digest:
                raise ValueError("reviewer Scenario Ledger changed after simulation execution; rerun review")
            digest_field = "challenge_ledger_sha256" if simulation_field == "reviewer_simulations" else "ledger_sha256"
            request[digest_field] = simulation_digest
            request[simulation_field] = simulation_bundle
            request["certified_artifacts"] = _certify_request_artifacts(root, request, evidence_path, state)
            if (
                request.get("phase") == "subtask_acceptance"
                and request.get("subtask")
                and state.get("task_repositories", {}).get(request["task"])
            ):
                broker = _broker_for_state(root, state, request["task"])

                def record_integration(record: dict[str, Any]) -> None:
                    request.update({
                        "integrated_commit": record.get("commit", ""),
                        "integrated_tree_hash": record.get("tree", ""),
                        "integration_transaction_id": record.get("transaction_id", ""),
                        "integration_candidate_commit": record.get("candidate_commit", ""),
                        "accepted_byte_verification": record.get("accepted_byte_verification", {}),
                    })

                broker.integrate_subtask(request, board_mutation=record_integration)
        else:
            request["certified_artifacts"] = _certify_failed_request_artifacts(
                root, request, evidence_path, state,
            )
        completed = now()
        prior_package_id = str(request.get("repair_package_id") or "")
        prior_package = state.get("repair_packages", {}).get(prior_package_id)
        if prior_package:
            prior_package.update({
                "status": "accepted" if result == "passed" else "review_failed",
                "settled_request_id": request_id, "settled_at": completed,
            })
            repair_package_model.refresh_digest(prior_package)
        if result == "failed":
            package = repair_package_model.build(
                request["task"], request, summary.strip(), failure_members,
            )
            package["source_reviewer_id"] = qa["id"]
            repair_package_model.refresh_digest(package)
            state.setdefault("repair_packages", {})[package["id"]] = package
            request["created_repair_package_id"] = package["id"]
            request["created_repair_package"] = json.loads(json.dumps(package))
        if request.get("finalization_diff"):
            request["finalization_classification"] = {
                "decision": classification,
                "reviewer_id": qa["id"],
                "diff_sha256": request["finalization_diff"].get("sha256", ""),
                "summary": summary.strip(),
                "recorded_at": completed,
            }
            if classification == "rejected":
                state.setdefault("finalization_holds", {})[request["task"]] = {
                    "request_id": request_id,
                    "structure_revision": int(request.get("structure_revision", 0)),
                    "paths": list(request["finalization_diff"].get("paths") or []),
                    "reason": summary.strip(),
                    "recorded_at": completed,
                }
        if result == "passed":
            request["evidence_reuse_identity"] = _pass_reuse_identity(request)
        request_lifecycle = request.setdefault("lifecycle", {})
        formal_started = str(request_lifecycle.get("formal_review", {}).get("started_at") or request.get("claimed_at", ""))
        request_lifecycle["formal_review"] = lifecycle.phase(formal_started, verdict_started_at)
        request_lifecycle["verdict"] = lifecycle.phase(verdict_started_at, completed)
        for simulation in simulations:
            request.setdefault("command_executions", []).append({
                key: simulation[key]
                for key in (
                    "id", "command", "command_fingerprint", "started_at", "finished_at", "duration_seconds",
                    "exit_code", "cache_decision", "deduplicated_from",
                )
                if key in simulation
            } | {"kind": "reviewer_scenario" if simulation_field == "reviewer_simulations" else "qa_scenario"})
        request.update({"status": result, "result": result, "result_summary": summary, "evidence": str(evidence_path), "completed_at": completed, "review_wait_stopped_at": completed})
        qa.update({"status": "qa_complete", "status_note": f"{request_id}: {result}", "last_status_at": completed})
        developer = _require_agent(state, request["developer_id"])
        if request.get("stage") == INDEPENDENT_REVIEW:
            _set_review_scope_status(state, request, "passed" if result == "passed" else "open")
        passed_state = "independent_review_passed" if request.get("stage") == INDEPENDENT_REVIEW else "development_qa_passed"
        failed_state = "independent_review_failed" if request.get("stage") == INDEPENDENT_REVIEW else "development_qa_failed"
        active_pipeline = [
            name for name, value in state.get("delivery_plans", {}).get(
                request["task"], {},
            ).get("subtasks", {}).items()
            if _effective_subtask_pipeline_status(state, request["task"], name, value)
            == "in_progress"
        ]
        developer.update({
            "status": "implementing_subtask" if active_pipeline else (
                passed_state if result == "passed" else failed_state
            ),
            "status_note": (
                f"{request_id} {result}; active subtask work: {', '.join(active_pipeline)}"
                if active_pipeline
                else f"{request.get('stage', DEVELOPMENT_QA)} cycle {request['cycle']} {result}; poll board"
            ),
            "last_status_at": completed,
        })
        _event(state, "qa_result", qa, {"task": request["task"], "request_id": request_id, "stage": request.get("stage", DEVELOPMENT_QA), "cycle": request["cycle"], "result": result, "ledger": request["ledger"], "challenge_ledger": request.get("challenge_ledger"), "evidence": evidence, "scenario_count": len(simulation_bundle["scenario_ids"]) if simulation_bundle else 0, "repair_package_id": request.get("created_repair_package_id", ""), "lifecycle": request_lifecycle, "command_execution_count": len([item for item in request.get("command_executions", []) if item.get("cache_decision") != "same_request_deduplicated"]), "message": summary})
        if result == "passed":
            request["resolved_in_scope_findings"] = _resolve_findings_certified_by_final_review(
                state, request, completed,
            )
        if result == "failed" and developer.get("session_id"):
            route_session = developer["session_id"]
            routed_text = f"Independent review {request_id} FAILED. Read its board result, fix the root cause, run internal QA, and submit the next review cycle. Do not wait for the owner."
        # RC1 residual (row 10): a PASSED review is also delivery's cue to move
        # NOW — next subtask, integration, or the Completion Contract — instead
        # of waiting for a poll cycle to notice.
        if result == "passed" and developer.get("session_id"):
            route_session = developer["session_id"]
            if request.get("phase") == "final_acceptance":
                routed_text = (
                    f"FINAL REVIEW {request_id} PASSED for {request['task']}. Validate the Completion "
                    "Contract and call complete now. Do not rerun the already-passed final review unless "
                    "its execution identity changes. USER ACTION: None."
                )
            else:
                routed_text = (
                    f"Review {request_id} PASSED for {request['task']}. Continue immediately with the next gate — "
                    "next subtask/chunk, integrated final acceptance, or the Completion Contract and completion. "
                    "Do not wait for a monitoring cycle. USER ACTION: None."
                )
        cto_session, cto_text = "", ""
        if result == "passed" and request.get("phase") == "final_acceptance":
            release_lifecycle = state.setdefault("release_lifecycle", {}).setdefault(
                request["task"], {"phases": {}},
            )
            release_lifecycle["final_pass_at"] = completed
            release_lifecycle["release_route_requested_at"] = completed
            release_lifecycle["completion_route_last_at"] = completed
            cto_agent = next((
                value for value in state.get("agents", {}).values()
                if value.get("role") == "cto" and value.get("active")
                and value.get("session_id")
            ), None)
            if cto_agent:
                cto_session = str(cto_agent["session_id"])
                cto_text = (
                    f"FINAL PASS RECORDED for {request['task']} at "
                    f"{str(request.get('reviewed_commit') or '')[:12]}. The Python "
                    "coordinator now owns release preparation. Poll once for the "
                    "prepared release action; do not ask Delivery or Reviewer to "
                    "repeat certified work. USER ACTION: None."
                )
            _event(state, "release_routing_requested", cto_agent, {
                "task": request["task"], "request_id": request_id,
                "message": "final PASS immediately routed to the Python release coordinator and CTO",
            })
        recorded = dict(request)
    for wake_session, wake_text, wake_source in (
        (route_session, routed_text, "independent-review"),
        (cto_session, cto_text, "final-acceptance-pass"),
    ):
        if not wake_session:
            continue
        instruction_id = ""
        route_error = ""
        try:
            from harness import control
            instruction_id = control.enqueue_instruction(
                root, wake_session, wake_text, source=wake_source,
            )["id"]
        except (ValueError, OSError) as error:
            route_error = str(error)
        with locked_state(root) as state:
            routed_agent = next((
                agent for agent in state.get("agents", {}).values()
                if agent.get("session_id") == wake_session
            ), None)
            _event(state, "instruction_route_queued" if instruction_id else "instruction_route_failed", routed_agent, {
                "task": recorded.get("task", ""),
                "request_id": request_id,
                "instruction_id": instruction_id,
                "source": wake_source,
                "error": route_error,
                "message": (
                    "event-driven wake is durably queued for supervisor delivery"
                    if instruction_id else
                    "event-driven wake could not be queued; board next-action state remains authoritative"
                ),
            })
    route_open_reviews(root)
    return recorded


def complete(root: Path, agent_id: str, note: str) -> dict[str, Any]:
    """Mark delivery work complete only after final independent proof exists."""
    task = ""
    cto_session = ""
    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        if agent.get("status") == "done" and not agent.get("active"):
            prior = next((
                event for event in reversed(state.get("events", []))
                if event.get("kind") == "development_complete"
                and event.get("agent_id") == agent_id
                and event.get("task") == agent.get("task")
            ), None)
            if prior:
                return dict(prior)
        agent = _require_writable_agent(state, agent_id)
        if agent["role"] not in DEVELOPER_ROLES:
            raise ValueError("only development or engineering agents may mark implementation complete")
        if agent["task"] == AWAITING_OWNER_DIRECTION:
            raise ValueError("delivery agent must await owner direction before completion")
        if not note.strip():
            raise ValueError("a concise completion note is required")
        requests = _task_requests(state, agent["task"])
        plan = state.get("delivery_plans", {}).get(agent["task"], {})
        current_revision = int(plan.get("structure_revision", 0))
        final_reviews = [
            request for request in requests
            if request.get("stage") == INDEPENDENT_REVIEW
            and request.get("phase", "legacy") in {"final_acceptance", "legacy"}
            and (not plan or int(request.get("structure_revision", 0)) == current_revision)
        ]
        latest_review = max(final_reviews, key=lambda request: int(request["cycle"]), default=None)
        if not latest_review or latest_review.get("status") != "passed":
            raise ValueError("delivery cannot complete before a passed final independent review")
        completion_problems = _completion_gate_problems(root, state, agent["task"], agent["id"])
        if completion_problems:
            raise ValueError("delivery cannot complete: " + "; ".join(completion_problems))
        completed_at = now()
        release_lifecycle = state.setdefault("release_lifecycle", {}).setdefault(agent["task"], {"phases": {}})
        final_pass_at = str(release_lifecycle.get("final_pass_at", ""))
        completion_phase = lifecycle.phase(final_pass_at, completed_at)
        if completion_phase:
            release_lifecycle.setdefault("phases", {})["completion"] = completion_phase
        release_lifecycle["development_completed_at"] = completed_at
        agent.update({"status": "done", "status_note": note, "last_status_at": completed_at, "active": False})
        task = str(agent["task"])
        event = _event(state, "development_complete", agent, {"task": task, "lifecycle": completion_phase, "message": note})
        cto = next((value for value in state.get("agents", {}).values()
                    if value.get("role") == "cto" and value.get("active") and value.get("session_id")), None)
        if cto:
            cto_session = str(cto["session_id"])
    if cto_session:
        try:
            from harness import control
            control.enqueue_instruction(
                root, cto_session,
                f"DEVELOPMENT COMPLETE for {task}. Run one bounded board poll now. The deterministic "
                "coordinator is executing mechanical release checks; perform only the remaining semantic "
                "claim-scope audit or handle its recorded incident. USER ACTION: None.",
                source="development-complete",
            )
        except (ValueError, OSError):
            pass
    # Development completion is the material event that makes the deterministic
    # release tail eligible. Execute it here, outside the board lock, rather
    # than from a browser refresh. Startup recovery repeats it idempotently.
    from harness import release_coordinator
    release_coordinator.coordinate(root)
    return event


def repin_final_review(root: Path, agent_id: str, task: str, candidate_commit: str = "HEAD", repo: Path | None = None) -> dict[str, Any]:
    """Re-bind a passed final review only when Git proves tree identity.

    Commit metadata or ancestry may change while integration catches up with
    main.  Equal Git tree hashes prove every reviewed byte is identical; any
    tree difference still requires a new independent final-acceptance cycle.
    """
    repository = (repo or root).resolve()
    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        if agent.get("role") != "cto":
            raise ValueError("only the CTO may re-pin a passed final review")
        plan = state.get("delivery_plans", {}).get(task, {})
        revision = int(plan.get("structure_revision", 0))
        finals = [
            request for request in _task_requests(state, task)
            if request.get("stage") == INDEPENDENT_REVIEW
            and request.get("phase", "legacy") in {"final_acceptance", "legacy"}
            and request.get("status") == "passed"
            and (not plan or int(request.get("structure_revision", 0)) == revision)
        ]
        latest = max(finals, key=lambda request: int(request.get("cycle", 0)), default=None)
        if not latest or not latest.get("reviewed_commit"):
            raise ValueError("a passed final review with an immutable commit is required before re-pin")
        request_id = latest["id"]
        source_revision = str(latest["reviewed_commit"])

    source_commit, source_tree = _git_commit_and_tree(repository, source_revision)
    target_commit, target_tree = _git_commit_and_tree(repository, candidate_commit)
    if source_tree != target_tree:
        raise ValueError("candidate Git tree differs from the passed final review; a full new review cycle is required")

    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        if agent.get("role") != "cto":
            raise ValueError("only the CTO may re-pin a passed final review")
        matching = [request for request in _task_requests(state, task) if request.get("id") == request_id]
        if not matching or matching[0].get("status") != "passed" or matching[0].get("reviewed_commit") != source_revision:
            raise ValueError("final review changed while tree identity was being verified; retry against current state")
        request = matching[0]
        record = {
            "from_commit": source_commit,
            "to_commit": target_commit,
            "tree_hash": source_tree,
            "verified_at": now(),
            "verified_by": agent_id,
            "board_verified": True,
        }
        request.setdefault("review_repins", []).append(record)
        request.setdefault("original_reviewed_commit", source_commit)
        request["reviewed_commit"] = target_commit
        request["reviewed_tree_hash"] = target_tree
        event = _event(state, "final_review_repinned", agent, {
            "task": task,
            "request_id": request_id,
            "from_commit": source_commit,
            "to_commit": target_commit,
            "tree_hash": target_tree,
            "message": "Git proved the release candidate tree is byte-identical to the passed final review; no redundant review cycle is required",
        })
        return {"request_id": request_id, "verification": record, "event": event}


RELEASE_REQUIRED_CHECKS = {
    "delivery_plan_recorded", "product_structure_complete",
    "development_qa_passed", "unit_tests_passed", "independent_review_passed",
    "final_acceptance_review_present", "delivery_chunks_complete",
    "scenario_ledger_complete", "reviewer_challenge_ledger_complete",
    "delivery_scenario_simulations_executed",
    "reviewer_scenario_simulations_executed",
    "completion_contract_complete", "owner_direction_recorded",
    "claim_scope_audit_passed", "development_agents_complete",
    "main_branch", "git_clean", "main_pushed", "main_health_verified",
    "task_artifact_release_verified",
    "ready_for_owner_test",
}

BROKER_RELEASE_REQUIRED_CHECKS = {
    "delivery_plan_recorded", "product_structure_complete",
    "development_qa_passed", "unit_tests_passed", "independent_review_passed",
    "final_acceptance_review_present", "delivery_chunks_complete",
    "scenario_ledger_complete", "reviewer_challenge_ledger_complete",
    "delivery_scenario_simulations_executed", "reviewer_scenario_simulations_executed",
    "completion_contract_complete", "owner_direction_recorded", "claim_scope_audit_passed",
    "development_agents_complete", "candidate_branch", "git_clean",
    "candidate_health_verified", "task_artifact_release_verified",
    "main_fast_forward_safe", "mirror_candidate_verified",
    "runtime_verification_scope_correct", "ready_for_owner_test",
}


def record_release_ready(root: Path, agent_id: str, task: str, checks: dict[str, Any]) -> dict[str, Any]:
    """Record the only state that Mission Control may present as complete."""
    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        if agent["role"] != "cto":
            raise ValueError("only the CTO may record VISUAL_TEST_REQUIRED")
        required_checks = BROKER_RELEASE_REQUIRED_CHECKS if checks.get("git_broker_governed") else RELEASE_REQUIRED_CHECKS
        if checks.get("runtime_gate_required"):
            required_checks = set(required_checks) | {
                "deployed_runtime_verified", "deployed_chat_verified",
            }
        failed = sorted(key for key in required_checks if checks.get(key) is not True)
        open_in_scope = [
            finding for finding in state.get("deferred_findings", {}).values()
            if finding.get("task") == task and finding.get("status") == "in_scope"
        ]
        if open_in_scope:
            failed.append("in_scope_findings_resolved")
        if failed:
            raise ValueError("cannot record VISUAL_TEST_REQUIRED; release checks failed: " + ", ".join(failed))
        recorded_at = now()
        lifecycle_record = json.loads(json.dumps(state.setdefault("release_lifecycle", {}).get(task, {"phases": {}})))
        if isinstance(checks.get("lifecycle"), dict):
            lifecycle_record.setdefault("phases", {}).update(json.loads(json.dumps(checks["lifecycle"])))
        final_pass_at = str(lifecycle_record.get("final_pass_at", ""))
        ready_phase = lifecycle.phase(final_pass_at, recorded_at)
        if ready_phase:
            lifecycle_record.setdefault("phases", {})["release_ready"] = ready_phase
        lifecycle_record["release_ready_at"] = recorded_at
        release = {
            "task": task,
            "status": "VISUAL_TEST_REQUIRED",
            "cto_id": agent_id,
            "recorded_at": recorded_at,
            "head_commit": checks.get("head_commit", ""),
            "git_broker_governed": bool(checks.get("git_broker_governed")),
            "acceptance_base_commit": checks.get("acceptance_base_commit", ""),
            "acceptance_manifest": list(checks.get("acceptance_manifest") or []),
            "runtime_verification_deferred_to_target_acceptance": bool(
                checks.get("runtime_verification_deferred_to_target_acceptance")
            ),
            "checks": {key: True for key in sorted(required_checks)},
            "owner_test_steps": contract.owner_test_steps(root, task),
            "lifecycle": lifecycle_record.get("phases", {}),
        }
        state.setdefault("release_lifecycle", {})[task] = lifecycle_record
        state.setdefault("releases", {})[task] = release
        # A NEW release candidate supersedes any earlier owner response. Without
        # this, a task the owner rejected once could never be accepted after its
        # repair: record_release_decision refuses a second response, so the
        # Accept action would fail forever on the repaired candidate.
        #
        # The superseded response is preserved in the DURABLE EVENT LOG rather
        # than state["archive"], because the hot/cold split empties that list of
        # everything except qa_requests on the next persist.
        previous_decision = state.get("release_decisions", {}).pop(task, None)
        previous_repair = state.get("release_repairs", {}).pop(task, None)
        if previous_decision is not None or previous_repair is not None:
            _event(state, "owner_release_response_superseded", agent, {
                "task": task,
                "superseded_by_commit": release["head_commit"],
                "previous_decision": previous_decision,
                "previous_repair_status": (previous_repair or {}).get("status", ""),
                "message": "a repaired release candidate was published; the earlier owner response is preserved in this event so the owner can respond to the new candidate",
            })
        _event(state, "visual_test_required", agent, {
            "task": task,
            "state": "VISUAL_TEST_REQUIRED",
            "message": (
                "Mirror-certified task candidate passed every gate; owner visual test may begin and Accept will perform the local FF-only transaction"
                if checks.get("git_broker_governed")
                else "Release gates passed on clean pushed main; owner visual test may begin"
            ),
        })
        return dict(release)


PREVIEW_STATUSES = {"unconfigured", "starting", "ready", "failed", "app_bundle"}


def record_release_preview(root: Path, task: str, preview: dict[str, Any]) -> dict[str, Any]:
    """Attach the candidate-preview state to a release awaiting the owner."""
    status = str(preview.get("status") or "")
    if status not in PREVIEW_STATUSES:
        raise ValueError("preview status must be one of " + ", ".join(sorted(PREVIEW_STATUSES)))
    with locked_state(root) as state:
        release = state.get("releases", {}).get(task)
        if not release or release.get("status") != "VISUAL_TEST_REQUIRED":
            raise ValueError("a candidate preview may be recorded only for a release awaiting the owner test")
        head_commit = str(preview.get("head_commit") or "")
        if head_commit and str(release.get("head_commit") or "") != head_commit:
            raise ValueError("the preview does not match the current release candidate")
        previous_status = str((release.get("preview") or {}).get("status") or "")
        value = {
            key: preview[key] for key in (
                "status", "url", "command", "pid", "start_token", "head_commit",
                "workspace", "branch", "started_at", "error", "log_tail",
                "app_path", "app_name", "built_at",
            ) if preview.get(key) is not None
        }
        value["recorded_at"] = now()
        release["preview"] = value
        if status in {"ready", "failed"} and status != previous_status:
            _event(state, "release_preview_" + status, None, {
                "task": task,
                "message": (
                    f"candidate preview is running at {value.get('url', '')} for the owner visual test"
                    if status == "ready" else
                    "candidate preview failed to start: " + str(value.get("error") or "unknown error")
                ),
            })
        return dict(value)


def clear_release_preview(root: Path, task: str) -> dict[str, Any]:
    """Drop the recorded preview so the supervisor starts a fresh attempt."""
    with locked_state(root) as state:
        release = state.get("releases", {}).get(task)
        if not release or release.get("status") != "VISUAL_TEST_REQUIRED":
            raise ValueError("a candidate preview exists only for a release awaiting the owner test")
        removed = release.pop("preview", None) or {}
        return dict(removed)


def record_release_decision(root: Path, task: str, decision: str, reason: str = "") -> dict[str, Any]:
    """Persist the owner's response against an already released task."""
    task = task.strip()
    decision = decision.strip().lower()
    reason = reason.strip()
    if not task:
        raise ValueError("release task is required")
    if decision not in {"accepted", "not_accepted"}:
        raise ValueError("decision must be accepted or not_accepted")
    if decision == "not_accepted" and not reason:
        raise ValueError("a reason is required when the release is not accepted")
    if len(reason) > MAX_REASON_LENGTH:
        raise ValueError(f"the reason must be {MAX_REASON_LENGTH} characters or fewer")
    governed_acceptance = False
    with locked_state(root) as state:
        release = state.get("releases", {}).get(task)
        if not release or release.get("status") != "VISUAL_TEST_REQUIRED":
            raise ValueError("owner responses are available only for a released task")
        if state.setdefault("release_decisions", {}).get(task):
            raise ValueError("an owner response has already been recorded for this task")
        response = {
            "task": task,
            "decision": decision,
            "reason": reason if decision == "not_accepted" else "",
            "attachments": [],
            "recorded_at": now(),
        }
        state["release_decisions"][task] = response
        governed_acceptance = bool(
            decision == "accepted"
            and state.get("task_repositories", {}).get(task)
            and any(
                request.get("phase") == "final_acceptance"
                and request.get("status") == "passed"
                and request.get("mirror_ref")
                for request in _task_requests(state, task)
            )
        )
        if decision == "not_accepted":
            release_repairs = state.setdefault("release_repairs", {})
            release_repairs[task] = {
                "task": task,
                "status": "OWNER_REJECTED_REPAIR_REQUIRED",
                "reason": reason,
                "attachments": [],
                "source_release": {
                    "head_commit": release.get("head_commit", ""),
                    "recorded_at": release.get("recorded_at", ""),
                },
                "next_action": "Delivery repairs this release using the saved response, then starts a new review and release cycle.",
                "created_at": response["recorded_at"],
                "updated_at": response["recorded_at"],
            }
        _event(state, "owner_release_decision_recorded", None, {
            "task": task,
            "decision": decision,
            "message": "Owner release response recorded against the released task",
        })
        if decision == "accepted":
            for finding in state.get("deferred_findings", {}).values():
                if finding.get("status") == "fix_in_progress" and finding.get("follow_up_task") == task:
                    finding.update({
                        "status": "resolved",
                        "resolved_at": response["recorded_at"],
                        "resolution_evidence": f"Owner accepted released follow-up {task} at commit {release.get('head_commit', '')}.",
                        "next_action": "Resolved through the independently reviewed and owner-accepted follow-up task.",
                    })
                    _event(state, "finding_resolved", None, {
                        "task": task,
                        "finding_id": finding.get("id", ""),
                        "message": finding["next_action"],
                    })
                    break
        if decision == "not_accepted":
            _event(state, "owner_release_repair_required", None, {
                "task": task,
                "message": "Owner response routed to Delivery for a new repair, review, and release cycle",
            })
        recorded_response = dict(response)
    if governed_acceptance:
        try:
            recorded_response["git_acceptance"] = accept_owner_release(root, task)
        except git_broker.MainMovedError as error:
            with locked_state(root) as state:
                state.setdefault("git_reintegration_required", {})[task] = {
                    "task": task, "reason": str(error), "recorded_at": now(),
                    "next_action": "Re-integrate on the task branch and repeat final QA plus independent review.",
                }
                _event(state, "git_acceptance_reintegration_required", None, {
                    "task": task,
                    "message": "Main moved after final certification; no merge occurred and fresh final review is required",
                })
            recorded_response["git_acceptance"] = {"status": "reintegration_required", "reason": str(error)}
    if decision == "not_accepted":
        route_owner_repairs(root)
    return recorded_response


def accept_owner_release(root: ProjectRoot, task: str) -> dict[str, Any]:
    """Execute the FF-only broker transaction authorized by owner Accept."""
    with locked_state(root) as state:
        if state.get("release_decisions", {}).get(task, {}).get("decision") != "accepted":
            raise ValueError("local acceptance requires the durable owner Accept decision")
        plan = state.get("delivery_plans", {}).get(task, {})
        revision = int(plan.get("structure_revision", 0))
        finals = [
            request for request in _task_requests(state, task)
            if request.get("stage") == INDEPENDENT_REVIEW
            and request.get("phase") == "final_acceptance"
            and request.get("status") == "passed"
            and request.get("mirror_ref")
            and (not plan or int(request.get("structure_revision", 0)) == revision)
        ]
        final = max(finals, key=lambda request: int(request.get("cycle", 0)), default=None)
        if not final:
            raise ValueError("owner acceptance requires a passed mirror-pinned final review")
        release = state.get("releases", {}).get(task, {})
        if release.get("head_commit") and release.get("head_commit") != final.get("reviewed_commit"):
            raise ValueError("owner acceptance candidate differs from the released final review")
        acceptance_base = str(release.get("acceptance_base_commit") or "")
        acceptance_manifest = release.get("acceptance_manifest")
        if not acceptance_base or not isinstance(acceptance_manifest, list):
            raise ValueError("released broker candidate lacks its observed main CAS identity")
        candidate = {
            "recorded_base": acceptance_base,
            "commit": final.get("reviewed_commit", ""),
            "tree": final.get("reviewed_tree_hash", ""),
            "manifest": acceptance_manifest,
            "mirror_ref": final.get("mirror_ref", ""),
        }
        broker = _broker_for_state(root, state, task)
        result = broker.accept_merge(
            task,
            candidate,
            board_mutation=lambda record: state.setdefault("git_acceptances", {}).__setitem__(task, {
                **record,
                "accepted_at": now(),
                "owner_decision_recorded_at": state["release_decisions"][task]["recorded_at"],
            }),
        )
        state.setdefault("git_reintegration_required", {}).pop(task, None)
        _event(state, "git_acceptance_completed", None, {
            "task": task, "commit": result["commit"], "tree": result["tree"],
            "mirror_ref": result["mirror_ref"],
            "message": "Owner Accept advanced local main by verified FF-only broker transaction; no remote push occurred",
        })
        return dict(state["git_acceptances"][task])


def record_remote_push_instruction(
    root: ProjectRoot,
    task: str,
    remote: str,
    branch: str,
    expected_remote_tip: str = "",
) -> dict[str, Any]:
    """Durably record a separate post-acceptance owner push instruction."""
    remote, branch = remote.strip(), branch.strip()
    if not remote or not re.fullmatch(r"[A-Za-z0-9._-]+", remote):
        raise ValueError("push instruction requires an existing remote name")
    if not branch or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith("-"):
        raise ValueError("push instruction requires one branch name")
    if branch.startswith("refs/") and not branch.startswith("refs/heads/"):
        raise ValueError("push instruction may target branches only")
    with locked_state(root) as state:
        acceptance = state.get("git_acceptances", {}).get(task, {})
        if not acceptance.get("commit"):
            raise ValueError("remote push instruction is available only after local owner acceptance")
        if not state.get("task_repositories", {}).get(task):
            raise ValueError("accepted task has no board-derived repository")
        broker = _broker_for_state(root, state, task)
        destination = broker.inspect_push_destination(
            task, remote, branch, expected_remote_tip,
        )
        actual_tip = destination["expected_remote_tip"]
        instruction = {
            "id": f"push-{task}-{secrets.token_hex(6)}",
            "task": task,
            "remote": remote,
            "branch": branch,
            "expected_remote_tip": actual_tip,
            "owner_instructed_at": now(),
            "confirmed_at": "",
            "used_at": "",
        }
        state.setdefault("approved_remotes", {})[task] = {
            "name": remote, "url": destination["url"], "branch": branch,
        }
        state.setdefault("remote_push_instructions", {})[task] = instruction
        _event(state, "remote_push_owner_instruction_recorded", None, {
            "task": task, "instruction_id": instruction["id"],
            "remote": remote, "branch": branch, "expected_remote_tip": actual_tip,
            "message": "Separate owner push instruction recorded; explicit confirmation is still required before remote contact",
        })
        return dict(instruction)


def confirm_remote_push(root: ProjectRoot, task: str, instruction_id: str) -> dict[str, Any]:
    """Record immediate owner confirmation and execute exactly one push."""
    with locked_state(root) as state:
        instruction = state.get("remote_push_instructions", {}).get(task, {})
        if instruction.get("id") != instruction_id or instruction.get("used_at"):
            raise ValueError("push confirmation does not match the current unused instruction")
        instruction["confirmed_at"] = now()
        _event(state, "remote_push_owner_confirmation_recorded", None, {
            "task": task, "instruction_id": instruction_id,
            "message": "Owner confirmed the exact accepted commit immediately before broker push",
        })
        broker = _broker_for_state(root, state, task)

    # Never hold the board lock across remote contact.  Confirmation above is
    # already durable; the callback below commits the broker's outcome in its
    # own atomic board transaction before ``remote_push`` returns or raises.
    def persist_outcome(record: dict[str, Any]) -> None:
        with locked_state(root) as current:
            current_instruction = current.get("remote_push_instructions", {}).get(task, {})
            if current_instruction.get("id") != instruction_id:
                raise ValueError("push instruction changed while the confirmed operation was running")
            current.setdefault("remote_push_outcomes", {})[task] = dict(record)
            current_instruction["used_at"] = now()

    try:
        result = broker.remote_push(task, board_mutation=persist_outcome)
    except git_broker.BrokerError as error:
        with locked_state(root) as state:
            instruction = state.get("remote_push_instructions", {}).get(task, {})
            if instruction.get("id") == instruction_id:
                instruction["used_at"] = instruction.get("used_at") or now()
            state.setdefault("remote_push_outcomes", {}).setdefault(task, {
                "task": task, "outcome": "failed", "reason": str(error), "at": now(),
                "accepted_commit": state.get("git_acceptances", {}).get(task, {}).get("commit", ""),
            })
            _event(state, "remote_push_failed", None, {
                "task": task,
                "message": "Remote push failed or was refused; the accepted local release remains valid",
            })
        raise
    with locked_state(root) as state:
        _event(state, "remote_push_completed", None, {
            "task": task, "commit": result["commit"], "remote": result["remote"],
            "branch": result["branch"], "message": "Exact accepted commit pushed after separate owner confirmation",
        })
    return result


def _safe_attachment_name(value: str) -> str:
    """Keep the original name for display only; never use it as a path."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (name or "attachment")[:120]


def _write_attachment(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def add_release_attachments(root: Path, task: str, attachments: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate and store untrusted owner files under generated names."""
    task = task.strip()
    if len(attachments) > MAX_ATTACHMENTS:
        raise ValueError(f"you can attach up to {MAX_ATTACHMENTS} files")
    prepared = []
    total = 0
    for attachment in attachments:
        content_type = str(attachment.get("content_type", "")).split(";", 1)[0].strip().lower()
        data = bytes(attachment.get("data", b""))
        if content_type not in ALLOWED_ATTACHMENT_TYPES:
            raise ValueError("only documents, PDFs, and screenshots can be attached")
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError("each attachment must be larger than zero and no bigger than 10 MB")
        total += len(data)
        prepared.append({
            "display_name": _safe_attachment_name(str(attachment.get("filename", ""))),
            "content_type": content_type,
            "data": data,
            "extension": ALLOWED_ATTACHMENT_TYPES[content_type],
        })
    if total > MAX_TOTAL_ATTACHMENT_BYTES:
        raise ValueError("the attachments are too large")
    with locked_state(root) as state:
        decision = state.get("release_decisions", {}).get(task)
        if not decision or decision.get("decision") != "not_accepted":
            raise ValueError("attachments require a saved Not accepted response")
        existing = decision.setdefault("attachments", [])
        if len(existing) + len(prepared) > MAX_ATTACHMENTS:
            raise ValueError(f"you can attach up to {MAX_ATTACHMENTS} files in total")
        existing_bytes = sum(int(item.get("size", 0)) for item in existing)
        if existing_bytes + total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("the attachments are too large in total")
        storage_dir = board_dir(root) / "owner-feedback" / hashlib.sha256(task.encode("utf-8")).hexdigest()[:24]
        written: list[Path] = []
        metadata: list[dict[str, Any]] = []
        try:
            for item in prepared:
                stored_name = f"{secrets.token_hex(16)}{item['extension']}"
                stored_path = storage_dir / stored_name
                _write_attachment(stored_path, item["data"])
                written.append(stored_path)
                metadata.append({
                    "attachment_id": secrets.token_hex(12),
                    "display_name": item["display_name"],
                    "content_type": item["content_type"],
                    "size": len(item["data"]),
                    "stored_name": stored_name,
                    "stored_path": _display_storage_path(root, stored_path),
                    "sha256": hashlib.sha256(item["data"]).hexdigest(),
                })
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        existing.extend(metadata)
        repair = state.setdefault("release_repairs", {}).setdefault(task, {})
        repair.setdefault("attachments", []).extend(metadata)
        repair["updated_at"] = now()
        _event(state, "owner_release_attachments_stored", None, {
            "task": task,
            "attachment_count": len(metadata),
            "message": "Owner attachments stored with generated safe names and routed to Delivery repair",
        })
        return dict(decision)


def release_repairs_for_delivery(root: Path, task: str = "") -> list[dict[str, Any]]:
    """Return saved owner repair routes for a Delivery agent to discover."""
    task = task.strip()
    state = snapshot(root)
    repairs = state.get("release_repairs", {})
    selected = [repairs[task]] if task and task in repairs else list(repairs.values()) if not task else []
    return json.loads(json.dumps(selected))


def _managed_session_is_live(root: Path, session_id: str) -> bool:
    """Whether an agent's visible terminal is still running (best effort)."""
    if not session_id:
        return False
    try:
        from harness import control
        return any(
            item.get("id") == session_id and item.get("status") in control.ACTIVE_STATUSES
            for item in control.snapshot(root).get("sessions", [])
        )
    except (OSError, ValueError):
        return False


def claim_release_repair(root: Path, agent_id: str, task: str) -> dict[str, Any]:
    """Give a Delivery agent the saved owner response and start its repair cycle."""
    task = task.strip()
    if not task:
        raise ValueError("repair task is required")
    session_id = ""
    with locked_state(root) as state:
        agent = _require_agent(state, agent_id)
        if agent["role"] not in DEVELOPER_ROLES:
            raise ValueError("only a Delivery agent may claim an owner repair")
        if agent.get("task") != task:
            raise ValueError("Delivery agent task does not match the owner repair")
        repair = state.get("release_repairs", {}).get(task)
        if not repair:
            raise ValueError("no saved owner repair exists for this task")
        if repair.get("status") not in {"OWNER_REJECTED_REPAIR_REQUIRED", "DELIVERY_REPAIR_IN_PROGRESS"}:
            raise ValueError("this owner repair is no longer available")
        started_at = repair.get("repair_cycle", {}).get("started_at") or now()
        cycle = repair.get("repair_cycle", {}).get("number", 0) or 0
        repair["status"] = "DELIVERY_REPAIR_IN_PROGRESS"
        repair["delivery_agent_id"] = agent_id
        repair["repair_cycle"] = {
            "number": cycle + 1,
            "started_at": started_at,
            "status": "repairing",
            "delivery_agent_id": agent_id,
        }
        repair["next_action"] = "Delivery is repairing this release with the saved response, then starts a new review and release cycle."
        repair["updated_at"] = now()
        # Claiming an owner repair reactivates the delivery record, because the
        # agent that shipped the release is normally inactive by the time the
        # owner responds and an inactive agent may not post status or request
        # review — the repair would strand with no automatic recovery (the
        # router skips repairs already marked in progress).
        #
        # It is reactivated ONLY when its managed terminal is still alive and the
        # record has not been superseded. A superseded record must never be
        # resurrected: the board's lineage has already moved this task to a
        # replacement agent, and reviving the old one would put two active
        # delivery agents on a single task.
        revive = (
            agent.get("status") not in {"superseded", "offline"}
            and agent.get("liveness") != "offline"
            and _managed_session_is_live(root, agent.get("session_id", ""))
        )
        if not agent.get("active") and not revive:
            raise ValueError(
                "this delivery record cannot claim the repair: its terminal has ended or it was "
                "superseded — claim the repair with the agent that now owns this task"
            )
        agent.update({
            "active": True,
            "liveness": "healthy",
            "liveness_note": "owner repair claimed; delivery resumed",
            "status": "repairing",
            "status_note": "owner response claimed; repair cycle started",
            "last_status_at": now(),
        })
        session_id = str(agent.get("session_id", ""))
        _event(state, "owner_release_repair_claimed", agent, {
            "task": task,
            "message": "Delivery claimed the saved owner response and started a new repair cycle",
        })
        result = json.loads(json.dumps(repair))
    if session_id:
        try:
            from harness import control
            control.enqueue_instruction(
                root,
                session_id,
                "OWNER REPAIR ROUTED: read the saved reason and attachment metadata from the board, repair this release, then start a new review and release cycle without asking the owner to repeat anything.",
                source="owner-release-decision",
            )
        except ValueError:
            # The durable repair claim remains authoritative if the terminal
            # exits between board claim and controller delivery.
            pass
    return result


def route_owner_repairs(root: Path) -> list[dict[str, Any]]:
    """Attach available Delivery sessions to owner repairs without owner work."""
    from harness import control

    state = snapshot(root)
    sessions = {item["id"]: item for item in control.snapshot(root)["sessions"]}
    actions: list[dict[str, Any]] = []
    for task, repair in state.get("release_repairs", {}).items():
        if repair.get("status") not in {"OWNER_REJECTED_REPAIR_REQUIRED", "DELIVERY_REPAIR_IN_PROGRESS"}:
            continue
        if repair.get("status") == "DELIVERY_REPAIR_IN_PROGRESS":
            continue
        delivery_agents = sorted((
            agent for agent in state.get("agents", {}).values()
            if agent.get("role") in DEVELOPER_ROLES
            and agent.get("task") == task
            and agent.get("status") not in {"superseded", "offline"}
            and agent.get("liveness") != "offline"
            and sessions.get(agent.get("session_id"), {}).get("status") in control.ACTIVE_STATUSES
        ), key=lambda agent: (
            bool(agent.get("active")), agent.get("spawned_at", ""), agent.get("id", ""),
        ), reverse=True)
        if delivery_agents:
            for delivery_agent in delivery_agents:
                try:
                    actions.append(claim_release_repair(root, delivery_agent["id"], task))
                    break
                except ValueError:
                    continue
            if actions and actions[-1].get("task") == task:
                continue
        replacement_agents = [
            agent for agent in state.get("agents", {}).values()
            if agent.get("role") in DEVELOPER_ROLES
            and agent.get("task") == AWAITING_OWNER_DIRECTION
            and agent.get("active")
            and sessions.get(agent.get("session_id"), {}).get("status") in control.ACTIVE_STATUSES
        ]
        sources = [
            agent for agent in state.get("agents", {}).values()
            if agent.get("role") in DEVELOPER_ROLES
            and agent.get("task") == task
            and not agent.get("active")
            and owner_direction_for_task(state, agent["id"], task)
        ]
        if not replacement_agents or not sources:
            continue
        try:
            resume_task(root, replacement_agents[0]["id"], sources[0]["id"], task)
            actions.append(claim_release_repair(root, replacement_agents[0]["id"], task))
        except ValueError:
            # Another board/viewer tick may have claimed this route first.
            continue
    return actions


def snapshot(root: Path) -> dict[str, Any]:
    projected = _read_state(root)
    if projected.get("integrity_migration_complete"):
        for request in projected.get("qa_requests", {}).values():
            if request.get("status") != "passed":
                continue
            valid, problems = _request_integrity(root, request)
            if valid:
                continue
            request.update({"status": "failed", "result": "failed", "integrity_invalidated": True, "result_summary": "PASS invalidated by certified evidence integrity: " + "; ".join(problems)})
            _set_review_scope_status(projected, request, "open")
    projected["critical_path_summaries"] = lifecycle.summaries(projected)
    return projected


def _remove_cancelled_task_files(root: Path, task: str, artifact_values: list[str], workspace_value: str) -> dict[str, Any]:
    """Remove only runtime artifacts proven to belong to one cancelled task."""
    context = project_context(root)
    code_root = context.code_root
    removed_artifacts = 0
    contracts_dir = context.storage_path("tasks")
    if contracts_dir.is_dir():
        for path in contracts_dir.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if value.get("task") == task:
                path.unlink(missing_ok=True)
                removed_artifacts += 1
    reviews_dir = context.storage_path("reviews")
    for value in artifact_values:
        path = Path(str(value or ""))
        if not path.is_absolute():
            path = code_root / path
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if reviews_dir in resolved.parents and resolved.is_file():
            resolved.unlink(missing_ok=True)
            removed_artifacts += 1
    workspace_removed = False
    if workspace_value:
        workspace = Path(workspace_value).resolve()
        allowed_parent = context.workspace_root
        if allowed_parent in workspace.parents:
            # The §10 operation matrix has no worktree-delete operation.  A
            # cancelled candidate is therefore made unreachable from active
            # board state but its Git worktree/ref is preserved for recovery;
            # board cleanup must never become a second Git writer.
            workspace_removed = False
    return {"removed_runtime_artifacts": removed_artifacts, "workspace_removed": workspace_removed}


def cancel_session_work(root: Path, session_id: str) -> dict[str, Any]:
    """Cancel unfinished work when the owner deliberately stops one terminal.

    A transport crash still uses :func:`offline` and preserves recovery memory.
    This operation is only for an explicit Stop action from Mission Control.
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        raise ValueError("managed session ID is required")
    cancelled_tasks: list[str] = []
    related_session_ids: set[str] = {session_id}
    artifact_values: dict[str, list[str]] = {}
    workspace_values: dict[str, str] = {}
    with locked_state(root) as state:
        matched = [agent for agent in state.get("agents", {}).values() if agent.get("session_id") == session_id]
        task_candidates = {
            agent.get("task", "") for agent in matched
            if agent.get("role") in DEVELOPER_ROLES
            and agent.get("task") not in {"", AWAITING_OWNER_DIRECTION}
            and state.get("releases", {}).get(agent.get("task", ""), {}).get("status") != "VISUAL_TEST_REQUIRED"
        }
        for task in sorted(task_candidates):
            requests = [request for request in _task_requests(state, task)]
            artifact_values[task] = [
                str(request.get("challenge_ledger", ""))
                for request in requests if request.get("challenge_ledger")
            ]
            workspace_values[task] = str(state.get("task_workspaces", {}).get(task, ""))
            for key in list(state.get("qa_requests", {})):
                if state["qa_requests"][key].get("task") == task:
                    state["qa_requests"].pop(key, None)
            for key in list(state.get("qa_request_index", {})):
                if state["qa_request_index"][key].get("task") == task:
                    state["qa_request_index"].pop(key, None)
            state["archive"] = [
                entry for entry in state.get("archive", [])
                if entry.get("value", {}).get("task") != task
            ]
            for key in (
                "task_chunks", "delivery_plans", "task_briefs", "task_baselines",
                "task_workspaces", "subtask_workspaces", "subtask_branches",
                "task_owner_directions", "task_lineage",
                "owner_clarifications", "requirement_confirmations", "releases",
                "release_decisions", "release_repairs",
            ):
                state.setdefault(key, {}).pop(task, None)
            state["owner_messages"] = [message for message in state.get("owner_messages", []) if message.get("task") != task]
            for finding_id, finding in list(state.get("deferred_findings", {}).items()):
                if finding.get("task") == task or finding.get("follow_up_task") == task:
                    state["deferred_findings"].pop(finding_id, None)
            for agent in state.get("agents", {}).values():
                if agent.get("task") == task:
                    if agent.get("session_id"):
                        related_session_ids.add(str(agent["session_id"]))
                    agent.update({
                        "active": False, "write_authority": False,
                        "status": "cancelled", "liveness": "offline",
                        "status_note": "owner intentionally stopped this unfinished task; runtime work was removed",
                        "liveness_note": "task cancelled by owner",
                        "last_status_at": now(),
                    })
                    if agent.get("session_id"):
                        state.get("owner_directions", {}).pop(agent["session_id"], None)
                        state.get("pending_owner_clarifications", {}).pop(agent["session_id"], None)
            cancelled_at = now()
            state.setdefault("cancelled_tasks", {})[task] = {
                "cancelled_at": cancelled_at,
                "reason": "owner intentionally stopped the Delivery task; unfinished runtime work was removed",
            }
            _event(state, "task_cancelled", matched[0] if matched else None, {
                "task": task,
                "message": "owner intentionally stopped the Delivery task; unfinished runtime work and workspace were removed",
            })
            cancelled_tasks.append(task)

        # A waiting Delivery terminal may hold an unconsumed direction but no
        # task yet. Explicit Stop discards that intake so it cannot reappear in
        # the next terminal as a phantom project.
        for agent in matched:
            if agent.get("task") == AWAITING_OWNER_DIRECTION:
                state.get("owner_directions", {}).pop(session_id, None)
                state.get("pending_owner_clarifications", {}).pop(session_id, None)
            agent.update({
                "active": False, "write_authority": False,
                "status": "cancelled", "liveness": "offline",
                "status_note": "terminal intentionally stopped by owner",
                "liveness_note": "terminal intentionally stopped by owner",
                "last_status_at": now(),
            })
        # If a reviewer is intentionally stopped, return its unfinished claim
        # to the queue instead of leaving a review owned by a dead agent.
        matched_ids = {agent.get("id") for agent in matched}
        for request in state.get("qa_requests", {}).values():
            # Settled verdicts are immutable history. Only UNFINISHED work returns
            # to the queue; a completed review keeps its verdict, its reviewer
            # attribution, and its challenge ledger. Without this guard, stopping
            # a reviewer erased the ledger reference of every review it had ever
            # completed (owner-reported data loss, 2026-08-15).
            if request.get("status") not in ("authoring", "open", "reserved", "claimed"):
                continue
            if request.get("reserved_by") in matched_ids or request.get("claimed_by") in matched_ids:
                prior_reviewer = str(request.get("reserved_by") or request.get("claimed_by") or "")
                _abandon_review_intents(request, prior_reviewer, "reviewer terminal stopped")
                request.update({
                    "status": "authoring" if request.get("delivery_state") == "executing" else "open",
                    "reserved_by": None, "reserved_at": None,
                    "claimed_by": None, "claimed_at": None, "routed_to": None,
                    "routed_session_id": "", "routed_at": None,
                    "challenge_ledger": None, "route_state": "reviewer_stopped_reopened",
                })

    cancelled = set(cancelled_tasks)
    if cancelled:
        _rewrite_cold(root, "qa_requests", [value for value in _read_cold(root, "qa_requests") if value.get("task") not in cancelled])
        _rewrite_cold(root, "agents", [value for value in _read_cold(root, "agents") if value.get("task") not in cancelled])
    file_results = {
        task: _remove_cancelled_task_files(root, task, artifact_values.get(task, []), workspace_values.get(task, ""))
        for task in cancelled_tasks
    }
    return {
        "session_id": session_id,
        "related_session_ids": sorted(related_session_ids),
        "cancelled_tasks": cancelled_tasks,
        "cleanup": file_results,
    }


def cancel_all_unfinished_work(root: Path) -> dict[str, Any]:
    """Cancel every owner-visible unfinished task after an explicit Stop All."""
    state = snapshot(root)
    session_ids = sorted({
        str(agent.get("session_id")) for agent in state.get("agents", {}).values()
        if agent.get("session_id") and agent.get("active")
    })
    results = [cancel_session_work(root, session_id) for session_id in session_ids]
    # Include an orphaned Delivery task whose terminal already died before the
    # owner pressed Stop All by routing it through its preserved session ID.
    refreshed = snapshot(root)
    orphan_sessions = sorted({
        str(agent.get("session_id")) for agent in refreshed.get("agents", {}).values()
        if agent.get("role") in DEVELOPER_ROLES
        and agent.get("session_id")
        and agent.get("task") not in {"", AWAITING_OWNER_DIRECTION}
        and refreshed.get("releases", {}).get(agent.get("task", ""), {}).get("status") != "VISUAL_TEST_REQUIRED"
        and agent.get("task") not in refreshed.get("cancelled_tasks", {})
    })
    results.extend(cancel_session_work(root, session_id) for session_id in orphan_sessions)
    return {
        "sessions_cleaned": len({value["session_id"] for value in results}),
        "cancelled_tasks": sorted({task for value in results for task in value.get("cancelled_tasks", [])}),
    }


def cleanup(root: Path) -> dict[str, int]:
    """Archive completed QA work so active board views contain only live work."""
    context = project_context(root)
    with locked_state(root) as state:
        old = [key for key, request in state["qa_requests"].items() if request["status"] in TERMINAL_QA]
        for key in old:
            state["archive"].append({"kind": "qa_request", "archived_at": now(), "value": state["qa_requests"].pop(key)})
        removed = 0
        for task, release in state.get("releases", {}).items():
            if release.get("status") != "VISUAL_TEST_REQUIRED":
                continue
            workspace_value = state.get("task_workspaces", {}).get(task)
            if not workspace_value:
                continue
            workspace = Path(workspace_value).resolve()
            allowed_parent = context.workspace_root
            if allowed_parent not in workspace.parents:
                continue
            if any(agent.get("active") and agent.get("task") == task for agent in state.get("agents", {}).values()):
                continue
            # Broker-governed refs/worktrees are immutable audit material.  No
            # cleanup path is authorized to mutate Git metadata.
            continue
        _event(state, "board_cleanup", None, {"archived_qa_requests": len(old), "removed_task_workspaces": removed})
        return {"archived_qa_requests": len(old), "active_qa_requests": len(state["qa_requests"]), "removed_task_workspaces": removed}


def watch(root: Path, status_interval_seconds: int = 300, stale_seconds: int = 900) -> list[dict[str, Any]]:
    """Return short updates due; a scheduler delivers these to the listed agents."""
    route_owner_repairs(root)
    route_open_reviews(root)
    current = datetime.now(timezone.utc)
    due: list[dict[str, Any]] = []
    with locked_state(root) as state:
        for agent in state["agents"].values():
            if not agent.get("active"):
                continue
            at = datetime.fromisoformat(agent["last_status_at"])
            age = (current - at).total_seconds()
            if age >= stale_seconds:
                due.append({"agent_id": agent["id"], "role": agent["role"], "vendor": agent.get("vendor", ""), "task": agent["task"], "kind": "stale", "message": "status is stale; post a short blocker/progress update"})
            elif age >= status_interval_seconds:
                due.append({"agent_id": agent["id"], "role": agent["role"], "vendor": agent.get("vendor", ""), "task": agent["task"], "kind": "update_due", "message": "post a short progress update"})
        for request in state["qa_requests"].values():
            if request["status"] in {"authoring", "open", "reserved", "claimed"}:
                reminder = request.get("last_reminded_at")
                reminder_age = (current - datetime.fromisoformat(reminder)).total_seconds() if reminder else status_interval_seconds
                if reminder_age >= status_interval_seconds:
                    request["last_reminded_at"] = now()
                    owner = request.get("claimed_by") or request.get("reserved_by") or request.get("routed_to") or "qa_queue"
                    due.append({"agent_id": owner, "role": "qa", "kind": "qa_pending", "request_id": request["id"], "message": "QA request remains active; report progress or complete it"})
        return due


def recover_git_transactions(root: ProjectRoot) -> dict[str, Any]:
    """Worker-start reconciliation for incomplete broker transactions."""
    recovered: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    with locked_state(root) as state:
        tasks = sorted(state.get("task_repositories", {}))
        for task in tasks:
            broker = _broker_for_state(root, state, task)

            def record(outcome: dict[str, Any]) -> None:
                if outcome.get("operation") == "accept-merge" and outcome.get("status") == "completed_idempotently":
                    state.setdefault("git_acceptances", {})[task] = {
                        **outcome, "accepted_at": now(), "recovered": True,
                    }
                elif outcome.get("operation") == "subtask-fold":
                    request = state.get("qa_requests", {}).get(outcome.get("request_id"))
                    if not request:
                        raise git_broker.BrokerError(
                            "subtask fold transaction awaits its board review request retry"
                        )
                    request.update({
                        "integrated_commit": outcome.get("commit", ""),
                        "integrated_tree_hash": outcome.get("tree", ""),
                        "integration_transaction_id": outcome.get("transaction_id", ""),
                        "integration_candidate_commit": outcome.get("candidate_commit", ""),
                    })
                elif outcome.get("operation") == "mirror-ref-create":
                    for request in state.get("qa_requests", {}).values():
                        if request.get("task") == task and request.get("reviewed_commit") == outcome.get("commit"):
                            request.update({
                                "mirror_ref": outcome.get("ref", ""),
                                "mirror_commit": outcome.get("commit", ""),
                                "mirror_tree_hash": outcome.get("tree", ""),
                                "mirror_transaction_id": outcome.get("transaction_id", ""),
                            })
                            return
                    # A real worker crash can occur after the create-only ref
                    # mutation but before the enclosing board request commits.
                    # Keep the journal incomplete so the Delivery Agent's
                    # deterministic re-request can attach to the matching ref;
                    # marking it done here would strand the task forever.
                    raise git_broker.BrokerError("mirror transaction awaits its board review request retry")

            task_outcomes = broker.recover(record)
            task_holds = broker.audit_mirror_records()
            recovered.extend(task_outcomes)
            holds.extend(task_holds)
        for outcome in recovered:
            if outcome.get("status") == "CTO_RECOVERY_HOLD":
                holds.append(outcome)
        if holds:
            for hold in holds:
                key = str(hold.get("transaction_id") or secrets.token_hex(8))
                state.setdefault("git_recovery_holds", {})[key] = hold
            _event(state, "git_recovery_hold_opened", None, {
                "message": f"Git broker recovery preserved repository state and opened {len(holds)} CTO hold(s)",
                "holds": [item.get("transaction_id", "") for item in holds],
            })
        elif recovered:
            _event(state, "git_recovery_completed", None, {
                "message": f"Git broker reconciled {len(recovered)} incomplete transaction(s) without data loss",
            })
    return {"recovered": recovered, "holds": holds}


def _root(value: str) -> Path:
    return Path(value).resolve()


def main(argv: list[str] | None = None) -> int:
    from harness import board_client
    client_state = board_client.environment_state()
    if client_state != "legacy":
        return board_client.invoke(list(sys.argv[1:] if argv is None else argv))
    parser = argparse.ArgumentParser(
        description="Dev Harness durable agent board", allow_abbrev=False,
    )
    add_context_arguments(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("register"); p.add_argument("--role", required=True, choices=sorted(ROLES)); p.add_argument("--task", required=True); p.add_argument("--name", default=""); p.add_argument("--vendor", default=""); p.add_argument("--session-id", default="")
    p = sub.add_parser("poll"); p.add_argument("--agent", required=True)
    p = sub.add_parser("recover"); p.add_argument("--agent", required=True)
    p = sub.add_parser("status"); p.add_argument("--agent", required=True); p.add_argument("--note", required=True); p.add_argument("--state", default="working")
    p = sub.add_parser("offline"); p.add_argument("--agent", required=True); p.add_argument("--note", default="managed CLI session ended")
    p = sub.add_parser("task-brief"); p.add_argument("--agent", required=True); p.add_argument("--plan", required=True); p.add_argument("--update", required=True)
    p = sub.add_parser("migrate-contract-scope"); p.add_argument("--agent", required=True)
    p = sub.add_parser("expand-contract"); p.add_argument("--agent", required=True); p.add_argument("--deliverable", action="append", required=True, metavar="NAME|ACCEPTANCE_PROOF")
    p = sub.add_parser("begin-task"); p.add_argument("--agent", required=True); p.add_argument("--task", required=True)
    p = sub.add_parser("resume-task"); p.add_argument("--agent", required=True); p.add_argument("--source-agent", required=True); p.add_argument("--task", required=True)
    p = sub.add_parser("attach-workspace"); p.add_argument("--agent", required=True); p.add_argument("--workspace", required=True)
    p = sub.add_parser("bind-repository"); p.add_argument("--agent", required=True); p.add_argument("--repo", default=""); p.add_argument("--baseline", default="")
    p = sub.add_parser("reconcile-baseline"); p.add_argument("--agent", required=True)
    p = sub.add_parser("owner-direction"); p.add_argument("--session-id", required=True); p.add_argument("--text", required=True)
    p = sub.add_parser("owner-message"); p.add_argument("--agent", required=True); p.add_argument("--text", required=True); p.add_argument("--type", choices=["direction", "clarification"], default="direction")
    p = sub.add_parser("confirm-requirements"); p.add_argument("--agent", required=True); p.add_argument("--text", required=True)
    p = sub.add_parser("record-finding"); p.add_argument("--task", required=True); p.add_argument("--title", required=True); p.add_argument("--description", required=True); p.add_argument("--evidence", default=""); p.add_argument("--affects-current-task", action="store_true")
    p = sub.add_parser("finding-decision"); p.add_argument("--finding", required=True); p.add_argument("--decision", required=True, choices=["fix", "do_not_fix"])
    p = sub.add_parser("execute-challenge"); p.add_argument("--agent", required=True); p.add_argument("--request", required=True); p.add_argument("--retry-reason", default="")
    p = sub.add_parser("finding-triage"); p.add_argument("--finding", required=True); p.add_argument("--verdict", required=True, choices=["repeat", "distinct", "cleared"]); p.add_argument("--target", default=""); p.add_argument("--note", default=""); p.add_argument("--recommend", default="", choices=["", "fix", "do_not_fix"]); p.add_argument("--why", default="")
    p = sub.add_parser("finding-resolved"); p.add_argument("--finding", required=True); p.add_argument("--evidence", default="")
    p = sub.add_parser("findings"); p.add_argument("--include-resolved", action="store_true")
    p = sub.add_parser("request-qa"); p.add_argument("--agent", required=True); p.add_argument("--ledger", required=True); p.add_argument("--summary", required=True)
    p.add_argument("--changes", default="", help="required for QA re-test cycles; state what changed")
    p = sub.add_parser("request-independent-review"); p.add_argument("--agent", required=True); p.add_argument("--summary", required=True)
    p = sub.add_parser("define-plan"); p.add_argument("--agent", required=True); p.add_argument("--mode", required=True, choices=sorted(DELIVERY_MODES)); p.add_argument("--rationale", required=True)
    p = sub.add_parser("declare-subtasks"); p.add_argument("--agent", required=True); p.add_argument("--subtask", action="append", required=True, metavar="ID|TITLE|ACCEPTANCE_PROOF|DEPENDENCIES|OWNED_PATHS|OWNED_SURFACES"); p.add_argument("--reason", default="")
    p = sub.add_parser("start-subtask"); p.add_argument("--agent", required=True); p.add_argument("--subtask", required=True)
    p = sub.add_parser("declare-subtask-chunks"); p.add_argument("--agent", required=True); p.add_argument("--subtask", required=True); p.add_argument("--chunk", action="append", required=True, metavar="NAME:DESCRIPTION"); p.add_argument("--reason", default="")
    p = sub.add_parser("git-commit"); p.add_argument("--agent", required=True); p.add_argument("--path", action="append", required=True); p.add_argument("--message", required=True); p.add_argument("--subtask", default="")
    p = sub.add_parser("reopen-candidate-scope"); p.add_argument("--agent", required=True); p.add_argument("--task", required=True); p.add_argument("--reason", required=True)
    p = sub.add_parser("declare-chunks"); p.add_argument("--agent", required=True); p.add_argument("--chunk", action="append", required=True, metavar="NAME:DESCRIPTION"); p.add_argument("--reason", default="")
    p = sub.add_parser("request-review"); p.add_argument("--agent", required=True); p.add_argument("--ledger", required=True); p.add_argument("--summary", required=True); p.add_argument("--unit-test-command", "--test-command", dest="test_command", required=True, help="unit-test command executed before acceptance scenarios and captured by the board; --test-command remains a compatibility alias"); p.add_argument("--phase", choices=["chunk", "subtask_acceptance", "final_acceptance"], default="chunk"); p.add_argument("--subtask", default=""); p.add_argument("--chunk", default=""); p.add_argument("--changes", default=""); p.add_argument("--test-scope", default="full", choices=sorted(TEST_SCOPES)); p.add_argument("--scope-reason", default=""); p.add_argument("--repair-package", default="")
    p = sub.add_parser("claim-qa"); p.add_argument("--agent", required=True); p.add_argument("--request", default=""); p.add_argument("--challenge-ledger", default="")
    p = sub.add_parser("reserve-qa"); p.add_argument("--agent", required=True); p.add_argument("--request", default="")
    p = sub.add_parser("review-brief"); p.add_argument("--agent", required=True); p.add_argument("--request", required=True)
    p = sub.add_parser("review-intents"); p.add_argument("--agent", required=True); p.add_argument("--request", required=True); p.add_argument("--intent", action="append", required=True); p.add_argument("--amend", action="store_true")
    p = sub.add_parser("attach-challenge-ledger"); p.add_argument("--agent", required=True); p.add_argument("--request", required=True); p.add_argument("--challenge-ledger", required=True); p.add_argument("--correction-reason", default="")
    p = sub.add_parser("qa-result"); p.add_argument("--agent", required=True); p.add_argument("--request", required=True); p.add_argument("--result", required=True, choices=["passed", "failed"]); p.add_argument("--summary", required=True); p.add_argument("--evidence", required=True); p.add_argument("--finalization-classification", default="", choices=["", "accepted", "rejected"]); p.add_argument("--failure", action="append", default=[], metavar="ID|CATEGORY|SUMMARY|PATHS|SURFACE|REGRESSION_CHECK")
    p = sub.add_parser("resolve-repair-package"); p.add_argument("--agent", required=True); p.add_argument("--package", required=True); p.add_argument("--resolution", action="append", required=True, metavar="MEMBER_ID|RESOLUTION|REGRESSION_CHECK")
    p = sub.add_parser("split-repair-package"); p.add_argument("--agent", required=True); p.add_argument("--package", required=True); p.add_argument("--group", action="append", required=True, metavar="MEMBER_ID,MEMBER_ID"); p.add_argument("--reason", required=True)
    p = sub.add_parser("complete"); p.add_argument("--agent", required=True); p.add_argument("--note", required=True)
    p = sub.add_parser("claim-release-repair"); p.add_argument("--agent", required=True); p.add_argument("--task", required=True)
    p = sub.add_parser("repin-final-review"); p.add_argument("--agent", required=True); p.add_argument("--task", required=True); p.add_argument("--commit", default="HEAD"); p.add_argument("--repo", default="")
    p = sub.add_parser("push-instruction"); p.add_argument("--task", required=True); p.add_argument("--remote", required=True); p.add_argument("--branch", required=True); p.add_argument("--expected-tip", default="")
    p = sub.add_parser("push-confirm"); p.add_argument("--task", required=True); p.add_argument("--instruction", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("view")
    sub.add_parser("cleanup")
    sub.add_parser("migrate-review-ledgers")
    sub.add_parser("migrate-integrity")
    sub.add_parser("recover-git")
    p = sub.add_parser("reopen-integrity"); p.add_argument("--request", action="append", required=True); p.add_argument("--reason", required=True)
    p = sub.add_parser("watch"); p.add_argument("--status-interval", type=int, default=300); p.add_argument("--stale-after", type=int, default=900)
    for command_parser in sub.choices.values():
        command_parser.allow_abbrev = False
    args = parser.parse_args(argv)
    root = context_from_args(args)
    try:
        if args.command == "register": out = register(root, args.role, args.task, args.name, args.vendor, args.session_id)
        elif args.command == "poll": out = poll(root, args.agent)
        elif args.command == "recover": out = request_recovery(root, args.agent)
        elif args.command == "status": out = status(root, args.agent, args.note, args.state)
        elif args.command == "offline": out = offline(root, args.agent, args.note)
        elif args.command == "task-brief": out = task_brief(root, args.agent, args.plan, args.update)
        elif args.command == "migrate-contract-scope": out = migrate_contract_scope(root, args.agent)
        elif args.command == "expand-contract": out = expand_contract(root, args.agent, [tuple(raw.split("|", 1)) if "|" in raw else ("", "") for raw in args.deliverable])
        elif args.command == "begin-task": out = begin_task(root, args.agent, args.task)
        elif args.command == "resume-task": out = resume_task(root, args.agent, args.source_agent, args.task)
        elif args.command == "attach-workspace": out = attach_task_workspace(root, args.agent, args.workspace)
        elif args.command == "bind-repository": out = bind_task_repository(root, args.agent, args.repo, args.baseline)
        elif args.command == "reconcile-baseline": out = reconcile_inherited_baseline(root, args.agent)
        elif args.command == "owner-direction": out = record_owner_direction(root, args.session_id, args.text)
        elif args.command == "owner-message": out = record_owner_message(root, args.agent, args.text, args.type)
        elif args.command == "confirm-requirements": out = record_requirement_confirmation(root, args.agent, args.text)
        elif args.command == "record-finding": out = record_finding(root, args.task, args.title, args.description, args.affects_current_task, args.evidence)
        elif args.command == "finding-decision": out = record_finding_decision(root, args.finding, args.decision)
        elif args.command == "execute-challenge": out = execute_challenge(root, args.agent, args.request, args.retry_reason)
        elif args.command == "finding-triage": out = triage_finding(root, args.finding, args.verdict, args.target, args.note, args.recommend, args.why)
        elif args.command == "finding-resolved": out = resolve_finding(root, args.finding, args.evidence)
        elif args.command == "findings": out = {"findings": list_findings(root, args.include_resolved)}
        elif args.command == "request-qa": out = request_qa(root, args.agent, args.ledger, args.summary, args.changes)
        elif args.command == "request-independent-review": out = request_independent_review(root, args.agent, args.summary)
        elif args.command == "define-plan": out = define_delivery_plan(root, args.agent, args.mode, args.rationale)
        elif args.command == "declare-subtasks":
            subtasks = []
            for raw in args.subtask:
                parts = raw.split("|", 5)
                if len(parts) < 3:
                    raise ValueError("subtask format is ID|TITLE|ACCEPTANCE_PROOF|DEPENDENCIES|OWNED_PATHS|OWNED_SURFACES")
                subtasks.append({
                    "id": parts[0], "title": parts[1],
                    "acceptance_proof": parts[2],
                    "dependencies": parts[3].split(",") if len(parts) > 3 and parts[3].strip() else [],
                    "owned_paths": parts[4].split(",") if len(parts) > 4 and parts[4].strip() else [],
                    "owned_surfaces": parts[5].split(",") if len(parts) > 5 and parts[5].strip() else [],
                })
            out = declare_subtasks(root, args.agent, subtasks, args.reason)
        elif args.command == "start-subtask": out = start_subtask(root, args.agent, args.subtask)
        elif args.command == "declare-subtask-chunks":
            chunks = [tuple(raw.split(":", 1)) if ":" in raw else ("", "") for raw in args.chunk]
            out = declare_subtask_chunks(root, args.agent, args.subtask, chunks, args.reason)
        elif args.command == "git-commit": out = broker_stage_commit(root, args.agent, args.path, args.message, args.subtask)
        elif args.command == "reopen-candidate-scope": out = reopen_candidate_scope(root, args.agent, args.task, args.reason)
        elif args.command == "declare-chunks":
            chunks = [tuple(raw.split(":", 1)) if ":" in raw else ("", "") for raw in args.chunk]
            out = declare_chunks(root, args.agent, chunks, args.reason)
        elif args.command == "request-review": out = request_review(root, args.agent, args.ledger, args.summary, args.phase, args.chunk, args.changes, test_command=args.test_command, subtask=args.subtask, test_scope=args.test_scope, scope_reason=args.scope_reason, repair_package_id=args.repair_package)
        elif args.command == "claim-qa": out = claim_qa(root, args.agent, args.request, args.challenge_ledger)
        elif args.command == "reserve-qa": out = reserve_qa(root, args.agent, args.request)
        elif args.command == "review-brief": out = review_brief(root, args.request, args.agent)
        elif args.command == "review-intents": out = record_review_intents(root, args.agent, args.request, args.intent, amendment=args.amend)
        elif args.command == "attach-challenge-ledger": out = attach_challenge_ledger(root, args.agent, args.request, args.challenge_ledger, args.correction_reason)
        elif args.command == "qa-result":
            failure_members = []
            for raw in args.failure:
                parts = raw.split("|", 5)
                if len(parts) != 6:
                    raise ValueError("failure format is ID|CATEGORY|SUMMARY|PATHS|SURFACE|REGRESSION_CHECK")
                failure_members.append({
                    "id": parts[0], "category": parts[1], "summary": parts[2],
                    "affected_paths": [path for path in parts[3].split(",") if path],
                    "surface": parts[4], "regression_check": parts[5],
                })
            out = qa_result(root, args.agent, args.request, args.result, args.summary, args.evidence, args.finalization_classification, failure_members or None)
        elif args.command == "resolve-repair-package":
            resolutions = []
            for raw in args.resolution:
                parts = raw.split("|", 2)
                if len(parts) != 3:
                    raise ValueError("resolution format is MEMBER_ID|RESOLUTION|REGRESSION_CHECK")
                resolutions.append({"id": parts[0], "resolution": parts[1], "regression_check": parts[2]})
            out = resolve_repair_package(root, args.agent, args.package, resolutions)
        elif args.command == "split-repair-package": out = split_repair_package(root, args.agent, args.package, [[member for member in raw.split(",") if member] for raw in args.group], args.reason)
        elif args.command == "complete": out = complete(root, args.agent, args.note)
        elif args.command == "claim-release-repair": out = claim_release_repair(root, args.agent, args.task)
        elif args.command == "repin-final-review": out = repin_final_review(root, args.agent, args.task, args.commit, Path(args.repo).resolve() if args.repo else root)
        elif args.command == "push-instruction": out = record_remote_push_instruction(root, args.task, args.remote, args.branch, args.expected_tip)
        elif args.command == "push-confirm": out = confirm_remote_push(root, args.task, args.instruction)
        elif args.command == "snapshot": out = snapshot(root)
        elif args.command == "view": out = {"board": str(board_dir(root) / "BOARD.md"), "text": _render_board(snapshot(root))}
        elif args.command == "cleanup": out = cleanup(root)
        elif args.command == "migrate-review-ledgers": out = migrate_reviewer_ledgers(root)
        elif args.command == "migrate-integrity": out = migrate_integrity(root)
        elif args.command == "recover-git": out = recover_git_transactions(root)
        elif args.command == "reopen-integrity": out = reopen_integrity_requests(root, args.request, args.reason)
        else: out = watch(root, args.status_interval, args.stale_after)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

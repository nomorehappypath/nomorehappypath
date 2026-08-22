# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Compact, board-derived, per-project memory with external durability.

The board remains the source of truth.  This module stores a small human-
readable index plus immutable detailed records so a resumed session can load
only the references it needs instead of replaying the complete board history.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.project_context import ProjectRoot, project_context

MEMORY_VERSION = 1
RECENT_RECORD_LIMIT = 8
INDEX_START = "<!-- HARNESS_MEMORY_INDEX\n"
INDEX_END = "\n-->"
NON_MATERIAL_EVENTS = {
    "board_polled",
    "review_execution_started",
    "review_execution_finished",
}


class MemoryRecordConflict(ValueError):
    """An existing immutable record disagrees with its replayed board event."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def memory_dir(root: ProjectRoot) -> Path:
    return project_context(root).storage_path("memory")


def records_dir(root: ProjectRoot) -> Path:
    return memory_dir(root) / "records"


def index_path(root: ProjectRoot) -> Path:
    return memory_dir(root) / "index.md"


def memory_lock_path(root: ProjectRoot) -> Path:
    return memory_dir(root).with_name(".memory.lock")


@contextmanager
def _memory_write_lock(root: ProjectRoot):
    """Serialize derived-memory publication independently of the board lock."""
    path = memory_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def external_backup_root(root: ProjectRoot) -> Path:
    context = project_context(root)
    if context.is_compatibility:
        return context.code_root.parent / ".harness-memory-backups" / context.code_root.name
    return context.data_root.parent / f"{context.data_root.name}-memory-backups"


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(value, encoding="utf-8")
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _unknown_answers() -> dict[str, str]:
    return {
        "project_about": "Project purpose is not recorded.",
        "current_status": "No project status is recorded.",
        "last_task_result": "No task or result is recorded.",
        "remaining_work": "Remaining work is not recorded.",
    }


def _safe_project(project: dict[str, Any] | None) -> dict[str, str]:
    value = project or {}
    return {
        "name": str(value.get("name", "")).strip(),
        "description": str(value.get("description", "")).strip(),
    }


def _index_document(
    project: dict[str, str], answers: dict[str, str], board_sequence: int,
    record_refs: list[str],
) -> dict[str, Any]:
    sequence = max(0, int(board_sequence))
    generated_at = _now()
    unknown = _unknown_answers()
    supported_facts = {
        fact_id: {
            "value": str(value),
            "source_ids": (
                ["registry:project-description"] if fact_id == "project_about"
                else [f"board:sequence:{sequence}"]
            ),
            "freshness": {"generated_at": generated_at, "board_sequence": sequence},
        }
        for fact_id, value in answers.items()
        if fact_id in unknown and str(value).strip() and value != unknown[fact_id]
    }
    return {
        "version": MEMORY_VERSION,
        "authority": "board",
        "generated_at": generated_at,
        "board_sequence": sequence,
        "project": _safe_project(project),
        "answers": {**_unknown_answers(), **answers},
        "facts": supported_facts,
        "record_refs": list(record_refs[-RECENT_RECORD_LIMIT:]),
    }


def _render_index(value: dict[str, Any]) -> str:
    project = value["project"]
    answers = value["answers"]
    name = project.get("name") or "Unnamed project"
    detail_refs = value.get("record_refs", [])
    details = "\n".join(f"- `records/{record}`" for record in detail_refs) or "- No detailed records yet."
    embedded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return (
        f"# Project memory — {name}\n\n"
        "> Derived narrative only. The board is authoritative whenever they differ.\n\n"
        f"## What is this project about?\n\n{answers['project_about']}\n\n"
        f"## What is the current status?\n\n{answers['current_status']}\n\n"
        f"## What was the last task and its result?\n\n{answers['last_task_result']}\n\n"
        f"## What is left to do?\n\n{answers['remaining_work']}\n\n"
        f"## Targeted detail records\n\n{details}\n\n"
        f"{INDEX_START}{embedded}{INDEX_END}\n"
    )


def _parse_index_text(text: str) -> dict[str, Any]:
    start = text.rfind(INDEX_START)
    end = text.find(INDEX_END, start + len(INDEX_START)) if start >= 0 else -1
    if start < 0 or end < 0:
        raise ValueError("project memory index has no machine-readable metadata")
    value = json.loads(text[start + len(INDEX_START):end])
    if not isinstance(value, dict) or int(value.get("version", 0)) != MEMORY_VERSION:
        raise ValueError(f"project memory index must use version {MEMORY_VERSION}")
    if value.get("authority") != "board":
        raise ValueError("project memory must declare the board as authority")
    if not isinstance(value.get("answers"), dict) or not isinstance(value.get("record_refs"), list):
        raise ValueError("project memory index has an invalid shape")
    if "facts" in value and not isinstance(value.get("facts"), dict):
        raise ValueError("project memory index facts have an invalid shape")
    return value


def _read_index_at(path: Path) -> dict[str, Any]:
    return _parse_index_text(path.read_text(encoding="utf-8"))


def _write_index(root: ProjectRoot, value: dict[str, Any]) -> dict[str, Any]:
    rendered = _render_index(value)
    if len(rendered.encode("utf-8")) > 32_000:
        raise ValueError("project memory index exceeded the compact 32 KiB limit")
    _atomic_text(index_path(root), rendered)
    return value


def initialize(
    root: ProjectRoot, *, project_name: str = "", description: str = "",
    create_backup: bool = True,
) -> dict[str, Any]:
    with _memory_write_lock(root):
        return _initialize_unlocked(
            root, project_name=project_name, description=description,
            create_backup=create_backup,
        )


def _initialize_unlocked(
    root: ProjectRoot, *, project_name: str = "", description: str = "",
    create_backup: bool = True,
) -> dict[str, Any]:
    """Create or refresh the compact index without changing existing records."""
    records_dir(root).mkdir(parents=True, exist_ok=True)
    path = index_path(root)
    if path.is_file():
        current = load_index(root)
        project = _safe_project(current.get("project"))
        if project_name:
            project["name"] = str(project_name).strip()
        if description:
            project["description"] = str(description).strip()
        answers = dict(current.get("answers") or _unknown_answers())
        if project.get("description"):
            answers["project_about"] = project["description"]
        current = _index_document(
            project, answers, int(current.get("board_sequence", 0)),
            [str(item) for item in current.get("record_refs", [])],
        )
    else:
        project = _safe_project({"name": project_name, "description": description})
        answers = _unknown_answers()
        if project["description"]:
            answers["project_about"] = project["description"]
        current = _index_document(project, answers, 0, [])
    _write_index(root, current)
    if create_backup:
        snapshot(root, int(current.get("board_sequence", 0)))
    return current


def load_index(root: ProjectRoot, *, restore: bool = True) -> dict[str, Any]:
    try:
        return _read_index_at(index_path(root))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        if not restore or not restore_latest(root):
            raise ValueError("project memory index is unavailable and no valid backup can restore it")
        return _read_index_at(index_path(root))


def _record_name(event: dict[str, Any]) -> str:
    sequence = int(event.get("sequence", 0))
    if sequence <= 0:
        raise ValueError("memory records require a positive board sequence")
    kind = re.sub(r"[^a-z0-9._-]+", "-", str(event.get("kind", "event")).casefold()).strip("-.")
    return f"{sequence:09d}-{kind or 'event'}.json"


def append_record(root: ProjectRoot, event: dict[str, Any]) -> str:
    """Create one immutable detailed record, idempotently for replay recovery."""
    directory = records_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    name = _record_name(event)
    path = directory / name
    payload = json.dumps(
        {
            "version": MEMORY_VERSION,
            "recorded_at": _now(),
            "maintained_by": "cto",
            "board_event": event,
        },
        indent=2, sort_keys=True,
    ) + "\n"
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(payload)
    except FileExistsError:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("board_event") != event:
            raise MemoryRecordConflict(
                f"append-only memory record conflicts with board sequence {event.get('sequence')}"
            )
    return name


def _append_replay_batch(root: ProjectRoot, events: list[dict[str, Any]]) -> None:
    """Drain a replay batch before surfacing its first immutable-record conflict.

    The compact index is intentionally published before detail records.  A
    changed replay payload must remain a loud append-only conflict, but raising
    immediately would strand every later record already promised by that
    index.  Continue writing independent records, refresh the durable mirror,
    and then raise the conflict to the caller.
    """
    conflict: MemoryRecordConflict | None = None
    for event in events:
        try:
            append_record(root, event)
        except MemoryRecordConflict as error:
            if conflict is None:
                conflict = error
    if conflict is not None:
        raise conflict


def _known_tasks(state: dict[str, Any]) -> dict[str, Any]:
    value = state.get("task_owner_directions", {})
    return value if isinstance(value, dict) else {}


def answers_from_board(state: dict[str, Any], project: dict[str, Any] | None = None) -> dict[str, str]:
    """Derive the four owner answers from board state without trusting memory."""
    project_value = _safe_project(project)
    answers = _unknown_answers()
    if project_value["description"]:
        answers["project_about"] = project_value["description"]

    directions = _known_tasks(state)
    releases = state.get("release_decisions", {})
    releases = releases if isinstance(releases, dict) else {}
    accepted = {
        str(task) for task, decision in releases.items()
        if str(task) in directions and isinstance(decision, dict)
        and decision.get("decision") == "accepted"
    }
    sentinels = {"AWAITING_OWNER_DIRECTION", "GLOBAL_MONITOR", "REVIEW_QUEUE"}
    agents = state.get("agents", {})
    agents = agents.values() if isinstance(agents, dict) else []
    active = sorted({
        str(agent.get("task")) for agent in agents
        if isinstance(agent, dict) and agent.get("active")
        and agent.get("task") not in sentinels and agent.get("task")
    })
    remaining = sorted(str(task) for task in directions if str(task) not in accepted)
    if active:
        answers["current_status"] = "Work is active on: " + ", ".join(active) + "."
    elif remaining:
        answers["current_status"] = "Recorded work remains for: " + ", ".join(remaining) + "."
    elif directions:
        answers["current_status"] = "All recorded tasks are accepted."

    decision_candidates = [
        (str(task), decision) for task, decision in releases.items()
        if str(task) in directions and isinstance(decision, dict)
    ]
    if decision_candidates:
        task, decision = max(
            decision_candidates,
            key=lambda item: (str(item[1].get("recorded_at", "")), item[0]),
        )
        result = str(decision.get("decision", "")).replace("_", " ") or "No result is recorded"
        answers["last_task_result"] = f"{task}: {result}."
    elif directions:
        event_tasks = [
            (int(event.get("sequence", 0)), str(event.get("task", "")))
            for event in state.get("events", []) if isinstance(event, dict)
            and str(event.get("task", "")) in directions
        ]
        task = max(event_tasks)[1] if event_tasks else sorted(str(task) for task in directions)[-1]
        answers["last_task_result"] = f"{task}: no result is recorded yet."

    if remaining:
        answers["remaining_work"] = "Tasks without owner acceptance: " + ", ".join(remaining) + "."
    elif directions:
        answers["remaining_work"] = "No recorded task remains."
    return answers


def _board_sequence(state: dict[str, Any]) -> int:
    values = [int(state.get("next_event", 1)) - 1]
    values.extend(
        int(event.get("sequence", 0)) for event in state.get("events", [])
        if isinstance(event, dict)
    )
    return max(values, default=0)


def sync_events(
    root: ProjectRoot, state: dict[str, Any], events: list[dict[str, Any]],
    *, project: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append material events and refresh the compact board-derived index."""
    material = [
        event for event in events
        if isinstance(event, dict) and event.get("kind") not in NON_MATERIAL_EVENTS
    ]
    if not material:
        return None
    with _memory_write_lock(root):
        return _sync_events_unlocked(root, state, material, project=project)


def _sync_events_unlocked(
    root: ProjectRoot, state: dict[str, Any], material: list[dict[str, Any]],
    *, project: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        current = load_index(root)
    except ValueError:
        current = _initialize_unlocked(
            root,
            project_name=str((project or {}).get("name", "")),
            description=str((project or {}).get("description", "")),
            create_backup=False,
        )
    project_value = _safe_project(current.get("project"))
    supplied = _safe_project(project)
    project_value["name"] = supplied["name"] or project_value["name"]
    project_value["description"] = supplied["description"] or project_value["description"]
    refs = [str(item) for item in current.get("record_refs", [])]
    ordered = sorted(material, key=lambda item: int(item.get("sequence", 0)))
    incoming_sequence = _board_sequence(state)
    current_sequence = int(current.get("board_sequence", 0))
    if incoming_sequence < current_sequence:
        # Board commits persist before memory synchronization and can finish
        # their post-commit work out of order. Preserve any missing immutable
        # detail, but never let a stale state roll the compact narrative back.
        conflict = None
        try:
            _append_replay_batch(root, ordered)
        except MemoryRecordConflict as error:
            conflict = error
        snapshot(root, current_sequence)
        if conflict is not None:
            raise conflict
        return current
    for event in ordered:
        name = _record_name(event)
        refs = [item for item in refs if item != name] + [name]
    value = _index_document(
        project_value, answers_from_board(state, project_value), incoming_sequence, refs,
    )
    # Publish and validate the bounded index first.  A later record-write
    # interruption leaves an explicit missing-detail marker on resume and can
    # be filled idempotently by retry; a rejected index can never orphan newly
    # appended records that no index references.
    _write_index(root, value)
    conflict = None
    try:
        _append_replay_batch(root, ordered)
    except MemoryRecordConflict as error:
        conflict = error
    snapshot(root, int(value["board_sequence"]))
    if conflict is not None:
        raise conflict
    return value


def sync_board_state(
    root: ProjectRoot, state: dict[str, Any], new_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Sync new board events and retry any index-published missing records."""
    with _memory_write_lock(root):
        candidates = list(new_events)
        try:
            index = load_index(root)
        except ValueError:
            index = _initialize_unlocked(root, create_backup=False)
            # Reconstruct as much recent detail as the hot board state still holds.
            candidates.extend(
                event for event in state.get("events", [])
                if isinstance(event, dict)
            )
        else:
            last_sequence = int(index.get("board_sequence", 0))
            candidates.extend(
                event for event in state.get("events", [])
                if isinstance(event, dict)
                and int(event.get("sequence", 0)) > last_sequence
            )
            by_name = {
                _record_name(event): event for event in state.get("events", [])
                if isinstance(event, dict) and int(event.get("sequence", 0)) > 0
            }
            for reference in index.get("record_refs", []):
                if not (records_dir(root) / str(reference)).is_file() and reference in by_name:
                    candidates.append(by_name[reference])
        deduplicated = {
            int(event.get("sequence", 0)): event for event in candidates
            if isinstance(event, dict) and int(event.get("sequence", 0)) > 0
        }
        material = [
            event for event in deduplicated.values()
            if event.get("kind") not in NON_MATERIAL_EVENTS
        ]
        if not material:
            return None
        return _sync_events_unlocked(root, state, material)


def log_sync_failure(root: ProjectRoot, error: BaseException) -> None:
    """Record derived-memory failures without rolling back authoritative board state."""
    backup = external_backup_root(root)
    try:
        backup.mkdir(parents=True, exist_ok=True)
        with (backup / "MEMORY_RECOVERY.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{_now()} | {type(error).__name__} | {error}\n")
    except OSError:
        pass
    print(
        f"HARNESS PROJECT MEMORY | {project_context(root).code_root} | "
        f"sync failed after board commit: {error}",
        file=__import__("sys").stderr,
        flush=True,
    )


def _read_record(root: ProjectRoot, name: str) -> dict[str, Any]:
    try:
        candidate = Path(name)
        if candidate.is_absolute() or ".." in candidate.parts or candidate.name != str(name):
            raise ValueError("project memory record reference is invalid")
        value = json.loads((records_dir(root) / candidate).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != MEMORY_VERSION:
            raise ValueError(f"project memory record is invalid: {name}")
        return value
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        # Detail loss must never suppress the four compact owner answers.  The
        # marker is deliberately explicit: no missing narrative is guessed.
        return {
            "version": MEMORY_VERSION,
            "record_ref": str(name),
            "detail_status": "unavailable",
            "message": "Targeted detail is unavailable; compact answers remain derived from the board-backed index.",
            "error_type": type(error).__name__,
        }


def resume_context(
    root: ProjectRoot, *, board_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load the compact index and only its explicitly targeted detail records."""
    index = load_index(root)
    answers = dict(index["answers"])
    authority = "memory"
    if board_state is not None:
        # Memory is derived.  Supplying current board state always wins even if
        # a stale or damaged index claims a newer timestamp.
        answers = answers_from_board(board_state, index.get("project"))
        authority = "board"
    records = [_read_record(root, str(name)) for name in index.get("record_refs", [])]
    return {
        "authority": authority,
        "board_sequence": _board_sequence(board_state) if board_state is not None else int(index.get("board_sequence", 0)),
        "project": index.get("project", {}),
        "answers": answers,
        "records": records,
        "loaded_record_refs": list(index.get("record_refs", [])),
    }


def reconcile_from_board(root: ProjectRoot, board_state: dict[str, Any]) -> dict[str, Any]:
    """Mechanically rebuild divergent narrative from the authoritative board."""
    with _memory_write_lock(root):
        divergences: list[str] = []
        try:
            current = _read_index_at(index_path(root))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            divergences.append("memory index was unavailable or invalid")
            records_dir(root).mkdir(parents=True, exist_ok=True)
            current = _index_document(
                _safe_project(), _unknown_answers(), 0, [],
            )

        project_value = _safe_project(current.get("project"))
        expected_sequence = _board_sequence(board_state)
        expected_answers = answers_from_board(board_state, project_value)
        if int(current.get("board_sequence", 0)) != expected_sequence:
            divergences.append(
                f"memory sequence {int(current.get('board_sequence', 0))} "
                f"did not match board sequence {expected_sequence}"
            )
        if current.get("answers") != expected_answers:
            divergences.append("memory answers did not match board-derived answers")

        valid_refs: list[str] = []
        for value in current.get("record_refs", []):
            reference = str(value)
            candidate = Path(reference)
            if (
                not candidate.is_absolute() and ".." not in candidate.parts
                and candidate.name == reference
                and (records_dir(root) / candidate).is_file()
            ):
                valid_refs.append(reference)
            else:
                divergences.append(f"memory detail reference was invalid or missing: {reference}")

        rebuilt = _index_document(
            project_value, expected_answers, expected_sequence, valid_refs,
        )
        # Re-publish even on a match. This makes the board-authoritative read
        # explicit and refreshes the validated external mirror atomically.
        _write_index(root, rebuilt)
        snapshot(root, expected_sequence)
        return {
            "authority": "board",
            "status": "rebuilt" if divergences else "matched",
            "board_sequence": expected_sequence,
            "divergences": divergences,
            "warning": (
                "Project memory diverged and was rebuilt from the board: "
                + "; ".join(divergences)
                if divergences else ""
            ),
            "project": rebuilt["project"],
            "answers": rebuilt["answers"],
            "loaded_record_refs": valid_refs,
        }


def snapshot(root: ProjectRoot, sequence: int) -> Path:
    """Refresh the self-contained external mirror outside ``data_root``.

    Records are append-only, so an incremental mirror preserves the complete
    folder without recopying its growing history on every board mutation.  The
    compact index is published last with atomic replacement, making it the
    mirror's commit point just as it is in live memory.
    """
    source = memory_dir(root)
    if not source.is_dir() or not index_path(root).is_file():
        raise ValueError("project memory must be initialized before backup")
    backup_root = external_backup_root(root)
    backup_root.mkdir(parents=True, exist_ok=True)
    destination = backup_root / "memory-current"
    if not destination.is_dir() or not _valid_snapshot(destination):
        temporary = backup_root / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        shutil.copytree(source, temporary)
        try:
            if destination.exists():
                preserved = backup_root / f"memory-broken-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
                os.replace(destination, preserved)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return destination

    source_records = records_dir(root)
    mirrored_records = destination / "records"
    mirrored_records.mkdir(parents=True, exist_ok=True)
    for record in source_records.iterdir():
        if not record.is_file():
            continue
        mirrored = mirrored_records / record.name
        if mirrored.exists():
            continue
        temporary_record = mirrored.with_name(f".{mirrored.name}.{uuid.uuid4().hex}.tmp")
        shutil.copy2(record, temporary_record)
        os.replace(temporary_record, mirrored)

    temporary_index = destination / f".index.md.{uuid.uuid4().hex}.tmp"
    shutil.copy2(index_path(root), temporary_index)
    os.replace(temporary_index, destination / "index.md")
    return destination


def restore_latest(root: ProjectRoot) -> bool:
    """Restore the newest valid external snapshot without discarding bad data."""
    backup_root = external_backup_root(root)
    if not backup_root.is_dir():
        return False
    current = backup_root / "memory-current"
    legacy = sorted(
        (path for path in backup_root.iterdir() if path.is_dir() and path.name.startswith("e")),
        reverse=True,
    )
    snapshots = ([current] if current.is_dir() else []) + legacy
    source = next((path for path in snapshots if _valid_snapshot(path)), None)
    if source is None:
        return False
    target = memory_dir(root)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore")
    shutil.copytree(source, temporary)
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        preserved = target.with_name(f"{target.name}.broken-{stamp}-{uuid.uuid4().hex[:8]}")
        os.replace(target, preserved)
    os.replace(temporary, target)
    return True


def _valid_snapshot(path: Path) -> bool:
    try:
        _read_index_at(path / "index.md")
        return (path / "records").is_dir()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def memory_digest(root: ProjectRoot) -> str:
    """Return a deterministic digest for backup/restore acceptance evidence."""
    digest = hashlib.sha256()
    base = memory_dir(root)
    for path in sorted(item for item in base.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(base)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()

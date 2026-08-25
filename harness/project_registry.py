# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Project registry, activation lock, and single-root migration (spec §6).

The registry is the manager-owned list of projects.  It stores only declared
fields (schema v1); anything derived — task counts, running flags, health — is
computed at read time so the list stays truthful even when a project was
touched outside the manager.  Writes are atomic (temp + rename) and rotated
into backups so the project list is never silently lost.

Nothing here reads ambient environment state: every function takes the
harness-home directory explicitly, matching the reviewed boundary rules of
``project_context``.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness import project_memory
from harness.project_context import ProjectContext, context_from_roots, project_context

REGISTRY_VERSION = 1
REGISTRY_FILENAME = "registry.json"
LOCK_FILENAME = "registry.lock"
AUDIT_FILENAME = "registry-audit.log"
BACKUP_DIRNAME = "registry-backups"
BACKUP_KEEP = 8
PROJECT_KINDS = {"scaffold", "adopted"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_home() -> Path:
    """Manager entry points only; library callers always pass ``home``."""
    return Path.home() / ".harness-home"


def _registry_path(home: Path) -> Path:
    return Path(home) / REGISTRY_FILENAME


def _same_directory(left: Path, right: Path) -> bool:
    """Directory identity that survives symlinks AND case-insensitive volumes.

    ``Path.resolve`` canonicalizes symlinks but not letter case, so on the
    case-insensitive filesystem macOS ships, ``Project`` and ``project`` are one
    directory with two spellings.  When both paths exist, compare device and
    inode — the filesystem's own identity — and fall back to resolved-path
    equality for paths that do not exist yet.
    """
    left, right = Path(left), Path(right)
    try:
        stat_left, stat_right = left.stat(), right.stat()
        return (stat_left.st_dev, stat_left.st_ino) == (stat_right.st_dev, stat_right.st_ino)
    except OSError:
        return left.resolve(strict=False) == right.resolve(strict=False)


def _is_within(inner: Path, outer: Path) -> bool:
    inner = Path(inner).resolve(strict=False)
    outer = Path(outer).resolve(strict=False)
    return inner == outer or outer in inner.parents


def _audit(home: Path, message: str) -> None:
    path = Path(home) / AUDIT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{_now()} {message}\n")


def load(home: Path) -> dict[str, Any]:
    """Load the registry; a missing file is an empty, valid registry."""
    path = _registry_path(home)
    if not path.is_file():
        return {"version": REGISTRY_VERSION, "projects": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    version = int(value.get("version", 0))
    if version != REGISTRY_VERSION:
        raise ValueError(f"unsupported registry version {version}; this harness reads version {REGISTRY_VERSION}")
    if not isinstance(value.get("projects"), list):
        raise ValueError("registry is corrupt: 'projects' must be a list")
    return value


def save(home: Path, registry: dict[str, Any]) -> None:
    """Atomic write (temp + rename in the same directory) plus rotated backup."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    path = _registry_path(home)
    payload = json.dumps(registry, indent=2, sort_keys=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(payload, encoding="utf-8")
    try:
        if path.is_file():
            backups = home / BACKUP_DIRNAME
            backups.mkdir(exist_ok=True)
            backup = backups / f"registry-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.json"
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            existing = sorted(backups.glob("registry-*.json"))
            for stale in existing[:-BACKUP_KEEP]:
                stale.unlink()
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def entries(home: Path) -> list[dict[str, Any]]:
    return list(load(home)["projects"])


def _find(registry: dict[str, Any], project_id: str) -> dict[str, Any]:
    for entry in registry["projects"]:
        if entry["id"] == project_id:
            return entry
    raise KeyError(f"no project with id {project_id}")


def _reject_overlap(registry: dict[str, Any], context: ProjectContext, *, ignore_id: str = "") -> None:
    for entry in registry["projects"]:
        if entry["id"] == ignore_id:
            continue
        for field in ("code_root", "data_root", "workspace_root"):
            theirs = Path(entry[field])
            for mine in (context.code_root, context.data_root, context.workspace_root):
                if _same_directory(mine, theirs):
                    raise ValueError(
                        f"project '{entry['name']}' already uses {theirs} — duplicate or shared roots are rejected"
                    )
        if _is_within(context.data_root, Path(entry["code_root"])) or _is_within(Path(entry["data_root"]), context.code_root):
            raise ValueError(f"data roots may not nest inside another project's repository ('{entry['name']}')")


def _context_for(kind: str, code_root: Path, data_root: Path | None, workspace_root: Path | None) -> ProjectContext:
    if kind not in PROJECT_KINDS:
        raise ValueError(f"kind must be one of {sorted(PROJECT_KINDS)}")
    if kind == "adopted":
        if data_root is None or workspace_root is None:
            raise ValueError("adopted projects require explicit data_root and workspace_root outside the repository")
        context = context_from_roots(code_root=code_root, data_root=data_root, workspace_root=workspace_root)
        if _is_within(context.data_root, context.code_root) or _is_within(context.workspace_root, context.code_root):
            raise ValueError("adopted repositories must keep data_root and workspace_root OUTSIDE code_root")
        return context
    if data_root is None and workspace_root is None:
        return project_context(code_root)
    return context_from_roots(code_root=code_root, data_root=data_root, workspace_root=workspace_root)


def register(
    home: Path,
    name: str,
    code_root: Path,
    *,
    kind: str = "scaffold",
    description: str = "",
    data_root: Path | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    name = str(name).strip()
    if not name:
        raise ValueError("a project needs a name")
    entry_id = uuid.uuid4().hex
    if kind == "adopted":
        # Adopted repositories stay pristine. When the owner does not choose
        # advanced storage locations, allocate collision-free manager-owned
        # roots outside the repository instead of forcing technical path entry.
        project_home = Path(home) / "projects" / entry_id
        data_root = Path(data_root) if data_root is not None else project_home / "data"
        workspace_root = Path(workspace_root) if workspace_root is not None else project_home / "workspaces"
    if kind == "scaffold" and data_root is None and workspace_root is None:
        # New scaffold projects get a collision-free workspace under harness-home;
        # the parent-level legacy location is reserved for the MIGRATED single-root
        # project, whose byte-identical compatibility paths are passed explicitly
        # by migrate_single_root. Without this, sibling projects would share one
        # workspace container and the overlap guard would (rightly) refuse them.
        data_root = Path(code_root) / ".harness"
        workspace_root = Path(home) / "workspaces" / entry_id
    context = _context_for(kind, Path(code_root), data_root, workspace_root)
    registry = load(home)
    if any(entry["name"] == name for entry in registry["projects"]):
        raise ValueError(f"a project named '{name}' already exists")
    _reject_overlap(registry, context)
    entry = {
        "id": entry_id,
        "name": name,
        "description": str(description),
        "code_root": str(context.code_root),
        "data_root": str(context.data_root),
        "workspace_root": str(context.workspace_root),
        "kind": kind,
        "created_at": _now(),
        "last_active_at": "",
    }
    # Registry operations must never make a missing source tree look healthy.
    # Project memory lives under the declared data root, but scaffold data may
    # be nested below code_root; initialize it only after that source root
    # actually exists.
    if context.code_root.is_dir():
        project_memory.initialize(
            context, project_name=entry["name"], description=entry["description"],
        )
    registry["projects"].append(entry)
    save(home, registry)
    _audit(home, f"registered project '{name}' ({entry['id']}) kind={kind} code_root={context.code_root}")
    return dict(entry)


def update_entry(home: Path, project_id: str, **fields: Any) -> dict[str, Any]:
    """Repair/re-point an entry; path changes revalidate identity and overlap."""
    allowed = {"name", "description", "code_root", "data_root", "workspace_root", "last_active_at"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown registry fields: {sorted(unknown)}")
    registry = load(home)
    entry = _find(registry, project_id)
    candidate = dict(entry)
    candidate.update({key: str(value) for key, value in fields.items()})
    context = _context_for(
        candidate["kind"], Path(candidate["code_root"]),
        Path(candidate["data_root"]), Path(candidate["workspace_root"]),
    )
    _reject_overlap(registry, context, ignore_id=project_id)
    candidate.update({
        "code_root": str(context.code_root),
        "data_root": str(context.data_root),
        "workspace_root": str(context.workspace_root),
    })
    entry.update(candidate)
    if context.code_root.is_dir():
        project_memory.initialize(
            context, project_name=entry["name"], description=entry["description"],
        )
    save(home, registry)
    _audit(home, f"updated project {project_id}: {sorted(fields)}")
    return dict(entry)


def remove(home: Path, project_id: str) -> dict[str, Any]:
    registry = load(home)
    entry = _find(registry, project_id)
    registry["projects"] = [item for item in registry["projects"] if item["id"] != project_id]
    save(home, registry)
    _audit(home, f"removed project '{entry['name']}' ({project_id}); its folders were NOT touched")
    return dict(entry)


def entry_health(entry: dict[str, Any]) -> dict[str, Any]:
    """Derived at read time; an unhealthy entry renders as a repairable row."""
    reasons: list[str] = []
    code_root = Path(entry["code_root"])
    if not code_root.is_dir():
        reasons.append(f"code_root missing or not a directory: {code_root}")
    elif not os.access(code_root, os.R_OK):
        reasons.append(f"code_root not readable: {code_root}")
    for field in ("data_root", "workspace_root"):
        path = Path(entry[field])
        if path.exists() and not path.is_dir():
            reasons.append(f"{field} exists but is not a directory: {path}")
    return {"ok": not reasons, "reasons": reasons}


def context_for_entry(entry: dict[str, Any]) -> ProjectContext:
    return context_from_roots(
        code_root=entry["code_root"], data_root=entry["data_root"], workspace_root=entry["workspace_root"],
    )


# --- activation lock -------------------------------------------------------

def _lock_path(home: Path) -> Path:
    return Path(home) / LOCK_FILENAME


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_lock(path: Path) -> dict[str, Any] | None:
    """The lock's record, or None when it is absent, empty, or unparseable."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def active_project(home: Path) -> dict[str, Any] | None:
    """The live activation, or None. A stale lock (dead pid) reads as inactive."""
    path = _lock_path(home)
    if not path.is_file():
        return None
    value = _read_lock(path)
    if value is None:
        return None
    return value if _pid_alive(int(value.get("pid", 0))) else None


def activate(home: Path, project_id: str, pid: int | None = None) -> dict[str, Any]:
    """Exclusive activation: exactly one project may be active at a time.

    The lock file is published already carrying its record — written to a
    temporary file and hard-linked into place, the same idiom
    ``board_surface._write_new_file`` uses — so a concurrent caller can never
    observe a created-but-empty lock.  A lock that exists but cannot be read is
    therefore never treated as abandoned: only a *readable* record whose pid is
    dead is reclaimed, atomically and with an audit record.  A live lock is
    never overridden.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    registry = load(home)
    entry = _find(registry, project_id)
    record = {"pid": int(pid if pid is not None else os.getpid()), "project_id": project_id,
              "project_name": entry["name"], "acquired_at": _now()}
    payload = json.dumps(record, indent=2, sort_keys=True)
    path = _lock_path(home)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            current = _read_lock(path)
            if current is None:
                raise RuntimeError(
                    "another activation is in progress or the activation lock is unreadable "
                    f"({path}); pause the open project, or remove that file if no project is running"
                ) from None
            if _pid_alive(int(current.get("pid", 0))):
                raise RuntimeError(
                    f"project '{current.get('project_name', current.get('project_id'))}' is already active "
                    f"(pid {current.get('pid')}); pause it before opening another project"
                ) from None
            os.replace(temp, path)
            _audit(home, f"reclaimed stale activation lock (dead pid {current.get('pid')}) for project {project_id}")
        else:
            _audit(home, f"activated project '{entry['name']}' ({project_id}) pid={record['pid']}")
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    update_entry(home, project_id, last_active_at=_now())
    return record


def deactivate(home: Path, project_id: str) -> None:
    """Release the activation lock; only the active project's lock is removed."""
    path = _lock_path(home)
    if not path.is_file():
        return
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    if current.get("project_id") not in ("", None, project_id):
        raise RuntimeError(f"the active project is {current.get('project_id')}, not {project_id}")
    path.unlink()
    _audit(home, f"deactivated project {project_id}")


# --- migration -------------------------------------------------------------

def migrate_single_root(home: Path, root: Path) -> dict[str, Any]:
    """Register the existing single-root harness as the default project.

    Idempotent: if a registered project already IS this root (directory
    identity, not string equality), it is returned unchanged.  The compatibility
    mapping supplies all three context paths, so history, board, and settings
    stay exactly where they are.
    """
    root = Path(root)
    for entry in entries(home):
        if _same_directory(Path(entry["code_root"]), root):
            return entry
    compat = project_context(root)
    return register(
        home, root.name or "default", root,
        kind="scaffold",
        description="Migrated single-root harness (default project)",
        data_root=compat.data_root,
        workspace_root=compat.workspace_root,
    )

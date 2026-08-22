#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Local, loopback-only lifecycle registry for visible CLI agent sessions."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.project_context import ProjectRoot, add_context_arguments, context_from_args, project_context


KINDS = {
    "codex_delivery": {"label": "CODEX CLI", "role": "Delivery Agent", "vendor": "OpenAI"},
    "claude_reviewer": {"label": "CLAUDE CLI", "role": "Independent Reviewer", "vendor": "Anthropic"},
    "claude_cto": {"label": "CTO (CLAUDE)", "role": "CTO", "vendor": "Anthropic"},
}
ROLE_SETTINGS = {
    "delivery": {"label": "Delivery Agent", "default_provider": "codex", "default_model": "gpt-5.6-sol", "default_effort": "high"},
    "cto": {"label": "CTO", "default_provider": "claude", "default_model": "opus", "default_effort": "high"},
    "reviewer": {"label": "Independent Reviewer", "default_provider": "claude", "default_model": "opus", "default_effort": "max"},
}
PROVIDERS = {
    "codex": {"label": "Codex", "binary_env": "HARNESS_CODEX_BIN"},
    "claude": {"label": "Claude", "binary_env": "HARNESS_CLAUDE_BIN"},
}
PROVIDER_EFFORTS = {
    # Codex's reasoning setting uses xhigh for its strongest selectable level.
    "codex": {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high"},
    # Claude Code's CLI exposes max, not Codex's xhigh spelling.
    "claude": {"low": "Low", "medium": "Medium", "high": "High", "max": "Max"},
}
PROVIDER_MODELS = {
    # Suggestions only. The settings UI also accepts a full model ID so the
    # harness does not become stale when a provider releases another model.
    "codex": [
        "gpt-5.6-sol", "gpt-5.6-sol-wm", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark",
        "codex-auto-review",
    ],
    "claude": [
        "claude-fable-5[1m]", "claude-fable-5", "claude-opus-5",
        "claude-sonnet-5", "opus", "sonnet", "haiku",
    ],
}
PROVIDER_DEFAULT_MODELS = {"codex": "gpt-5.6-sol", "claude": "opus"}
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-\[\]]{0,119}$")
# Backward-compatible union for older callers; validation is provider-specific.
EFFORTS = {key: label for values in PROVIDER_EFFORTS.values() for key, label in values.items()}
KIND_TO_ROLE = {
    "codex_delivery": "delivery",
    "claude_reviewer": "reviewer",
    "claude_cto": "cto",
    "delivery": "delivery",
    "reviewer": "reviewer",
    "cto": "cto",
}
ACTIVE_STATUSES = {"launching", "running", "stopping", "pausing"}
# Hard limits for visible work. A stopping process keeps its slot until the
# registry observes it exited, preventing a fast double-click from exceeding
# capacity.
MAX_ACTIVE_SESSIONS = {
    "codex_delivery": 2,
    "claude_reviewer": 2,
    "claude_cto": 1,
}
SESSION_COLORS = {
    "black": {"label": "Standard black", "hex": "#000000", "rgb": (0, 0, 0)},
    "blue": {"label": "Ocean blue", "hex": "#123B5D", "rgb": (0x12, 0x3B, 0x5D)},
    "purple": {"label": "Plum purple", "hex": "#42275A", "rgb": (0x42, 0x27, 0x5A)},
    "green": {"label": "Forest green", "hex": "#164A35", "rgb": (0x16, 0x4A, 0x35)},
    "red": {"label": "Brick red", "hex": "#5A2525", "rgb": (0x5A, 0x25, 0x25)},
    "amber": {"label": "Dark amber", "hex": "#5A4314", "rgb": (0x5A, 0x43, 0x14)},
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_agent_settings() -> dict[str, dict[str, str]]:
    return {
        role: {
            "provider": values["default_provider"],
            "model": values["default_model"],
            "effort": values["default_effort"],
        }
        for role, values in ROLE_SETTINGS.items()
    }


def role_for_kind(kind: str) -> str:
    try:
        return KIND_TO_ROLE[kind]
    except KeyError as error:
        raise ValueError("unknown agent role") from error


def normalize_provider_effort(provider: str, effort: str) -> str:
    provider = str(provider).strip().lower()
    effort = str(effort).strip().lower().replace("extra-high", "xhigh").replace("extra high", "xhigh")
    if provider == "claude" and effort == "xhigh":
        # Existing persisted settings used Codex's spelling for Claude. Accept
        # it once and return the real Claude CLI spelling during migration.
        effort = "max"
    elif provider == "codex" and effort == "max":
        effort = "xhigh"
    return effort


def normalize_provider_model(provider: str, model: str) -> str:
    """Return one explicit CLI model, migrating legacy provider-only settings."""
    provider = str(provider).strip().lower()
    model = str(model or "").strip()
    if not model:
        model = PROVIDER_DEFAULT_MODELS.get(provider, "")
    if not MODEL_PATTERN.fullmatch(model):
        raise ValueError("model must be a provider alias or model ID without spaces")
    return model


def _validated_agent_settings(value: dict | None) -> dict[str, dict[str, str]]:
    candidate = default_agent_settings() if value is None else value
    if not isinstance(candidate, dict):
        raise ValueError("agent settings must be an object")
    result: dict[str, dict[str, str]] = {}
    for role in ROLE_SETTINGS:
        entry = candidate.get(role)
        if not isinstance(entry, dict):
            raise ValueError(f"settings for {role} are required")
        provider = str(entry.get("provider", "")).strip().lower()
        effort = normalize_provider_effort(provider, entry.get("effort", ""))
        if provider not in PROVIDERS:
            raise ValueError(f"unsupported provider for {role}; choose codex or claude")
        model = normalize_provider_model(provider, entry.get("model", ""))
        if effort not in PROVIDER_EFFORTS[provider]:
            choices = ", ".join(PROVIDER_EFFORTS[provider])
            raise ValueError(f"unsupported effort for {role} with {provider}; choose {choices}")
        result[role] = {"provider": provider, "model": model, "effort": effort}
    if result["delivery"]["provider"] == result["reviewer"]["provider"]:
        raise ValueError("Delivery and Independent Reviewer must use different providers so independent review remains claimable")
    return result


def agent_settings(root: Path) -> dict[str, dict[str, str]]:
    with locked_state(root) as state:
        try:
            validated = _validated_agent_settings(state.get("agent_settings"))
        except ValueError:
            # A prior build may have persisted a same-vendor or otherwise
            # malformed block. Keep the dashboard and settings dialog usable;
            # the next explicit POST can replace it with a valid configuration.
            validated = default_agent_settings()
        return json.loads(json.dumps(validated))


def update_agent_settings(root: Path, value: dict) -> dict[str, dict[str, str]]:
    validated = _validated_agent_settings(value)
    with locked_state(root) as state:
        state["agent_settings"] = validated
        return json.loads(json.dumps(validated))


def launch_settings(
    root: Path,
    kind: str,
    *,
    settings_override: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Resolve a legacy launch only from settings supplied by its caller.

    ``root`` remains in the signature for compatibility with callers that
    identify the project being launched.  It is deliberately not used as a
    settings source: in Projects Layer mode that project's control document
    does not own provider/model/effort settings.
    """
    if settings_override is None:
        raise ValueError("legacy launch requires an explicit settings override or managed session")
    role = role_for_kind(kind)
    settings = _validated_agent_settings(settings_override)[role]
    return {"role": role, **settings, "provider_label": PROVIDERS[settings["provider"]]["label"]}


def session_launch_settings(root: Path, session_id: str, kind: str) -> dict[str, str]:
    """Return the immutable provider/model/effort captured at button click."""
    with locked_state(root) as state:
        _reconcile(state)
        session = state.get("sessions", {}).get(session_id)
        if not session or session.get("kind") != kind:
            raise ValueError("managed session does not match the requested agent role")
        provider = str(session.get("provider", ""))
        model = normalize_provider_model(provider, session.get("model", ""))
        effort = normalize_provider_effort(provider, session.get("effort", ""))
        if provider not in PROVIDERS or effort not in PROVIDER_EFFORTS[provider]:
            raise ValueError("managed session has invalid launch settings")
        return {
            "role": role_for_kind(kind),
            "provider": provider,
            "model": model,
            "effort": effort,
            "provider_label": PROVIDERS[provider]["label"],
        }


def control_dir(root: ProjectRoot) -> Path:
    return project_context(root).storage_path("control")


def _state_path(root: Path) -> Path:
    return control_dir(root) / "sessions.json"


def _initial_state() -> dict[str, Any]:
    # Provider/model/effort are manager-global in Projects Layer mode.  The
    # field remains readable when an older standalone harness explicitly saved
    # it, but new project control documents never create a second copy.
    return {"sessions": {}, "inbox": {}, "instruction_receipts": {}}


MAX_INSTRUCTION_RECEIPTS = 1000


def _prune_instruction_receipts(state: dict[str, Any]) -> None:
    """Bound JSON rewrite cost without deleting queued or in-flight receipts."""
    receipts = state.setdefault("instruction_receipts", {})
    overflow = len(receipts) - MAX_INSTRUCTION_RECEIPTS
    if overflow <= 0:
        return
    terminal = sorted(
        (
            receipt for receipt in receipts.values()
            if receipt.get("status") in {"delivered", "discarded"}
        ),
        key=lambda receipt: (receipt.get("delivered_at") or receipt.get("discarded_at")
                             or receipt.get("queued_at") or "", receipt.get("id") or ""),
    )
    for receipt in terminal[:overflow]:
        receipts.pop(receipt["id"], None)


def initialize(root: ProjectRoot) -> dict[str, Any]:
    """Create the project control document without local settings copies."""
    with locked_state(root) as state:
        _reconcile(state)
        return json.loads(json.dumps(state))


@contextmanager
def locked_state(root: Path) -> Iterator[dict[str, Any]]:
    directory = control_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        path = _state_path(root)
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else _initial_state()
        before = json.dumps(state, sort_keys=True)
        try:
            yield state
            serialized = json.dumps(state, indent=2, sort_keys=True) + "\n"
            if json.dumps(state, sort_keys=True) == before and path.exists():
                return
            temp = path.with_suffix(".tmp")
            temp.write_text(serialized, encoding="utf-8")
            os.replace(temp, path)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _pid_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reconcile(state: dict[str, Any]) -> None:
    for session in state["sessions"].values():
        if session["status"] not in ACTIVE_STATUSES:
            continue
        if session["pid"]:
            if _pid_is_alive(session["pid"]):
                continue
            if session.get("pause_requested_at"):
                session["status"] = "paused"
            else:
                session["status"] = "stopped" if session.get("stop_requested_at") else "exited"
            session["ended_at"] = now()
            session["reason"] = (
                "terminal intentionally paused with its saved session pointer"
                if session.get("pause_requested_at") else
                "stopped from control panel" if session.get("stop_requested_at") else
                "terminal session ended outside the viewer"
            )
            continue
        deadline = datetime.fromisoformat(session["launch_deadline"])
        if datetime.now(timezone.utc) >= deadline:
            session["status"] = "failed"
            session["ended_at"] = now()
            session["reason"] = "terminal session did not attach within 30 seconds"


def create(root: Path, kind: str, task: str = "", color: str = "black",
           settings_override: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("unknown session kind")
    task = task.strip()
    color = str(color or "black").strip().lower()
    if task:
        raise ValueError("start an agent role only; give your direction later inside its CLI")
    if color not in SESSION_COLORS:
        raise ValueError("unknown terminal color; choose one of: " + ", ".join(SESSION_COLORS))
    role = role_for_kind(kind)
    with locked_state(root) as state:
        _reconcile(state)
        try:
            selected = _validated_agent_settings(
                settings_override if settings_override is not None else state.get("agent_settings")
            )[role]
        except ValueError:
            selected = default_agent_settings()[role]
        counterpart = {"delivery": "reviewer", "reviewer": "delivery"}.get(role)
        collision = next((
            entry for entry in state["sessions"].values()
            if counterpart
            and entry.get("status") in ACTIVE_STATUSES
            # A damaged or legacy record must never make every valid launch
            # fail. Unknown kinds have no safe counterpart role, so ignore
            # them for this collision check and let normal reconciliation or
            # later repair handle the bad historical record.
            and KIND_TO_ROLE.get(str(entry.get("kind", ""))) == counterpart
            and entry.get("provider") == selected["provider"]
        ), None)
        if collision:
            provider_label = PROVIDERS[selected["provider"]]["label"]
            raise ValueError(
                f"cannot start {ROLE_SETTINGS[role]['label']} with {provider_label} while an active "
                f"{ROLE_SETTINGS[counterpart]['label']} is already using {provider_label}; stop that session "
                "or choose a different provider so independent review remains available"
            )
        active_count = sum(
            entry["kind"] == kind and entry["status"] in ACTIVE_STATUSES
            for entry in state["sessions"].values()
        )
        capacity = MAX_ACTIVE_SESSIONS[kind]
        if active_count >= capacity:
            label = KINDS[kind]["label"]
            if kind == "claude_cto":
                raise ValueError("the CTO session is already active (limit 1)")
            raise ValueError(f"maximum {capacity} active {label} sessions reached; stop one before starting another")
        session_id = f"{kind}-{secrets.token_hex(5)}"
        session = {
            "id": session_id, "kind": kind, "task": task,
            "status": "launching", "pid": None, "created_at": now(),
            # The board-surface verifier is bound to this epoch.  Every
            # terminal-ending lifecycle transition advances it so a stale
            # environment credential can never become valid again on resume.
            "auth_epoch": 1,
            "attached_at": None, "ended_at": None, "stop_requested_at": None,
            "pause_requested_at": None,
            "resume_launch_requested_at": None,
            "reason": "waiting for the Terminal session to attach",
            "color": color,
            "color_hex": SESSION_COLORS[color]["hex"],
            "color_label": SESSION_COLORS[color]["label"],
            "last_output_at": None, "output_bytes": 0, "last_status_request_at": None,
            "launch_deadline": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
            **KINDS[kind],
            "provider": selected["provider"],
            "model": selected["model"],
            "effort": selected["effort"],
            "provider_label": PROVIDERS[selected["provider"]]["label"],
        }
        state["sessions"][session_id] = session
        return dict(session)


def restore_missing_resume_session(
    root: Path, session_id: str, kind: str,
    settings_override: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Recreate one missing control transport named by a paused board owner.

    The board's saved session pointer remains the identity authority.  Reusing
    that exact ID lets the relaunched runner reattach to the existing agent
    rather than registering a second Delivery owner.
    """
    session_id = str(session_id or "").strip()
    if kind not in KINDS or not session_id.startswith(f"{kind}-"):
        raise ValueError("saved resume session identity does not match its role")
    with locked_state(root) as state:
        _reconcile(state)
        existing = state.get("sessions", {}).get(session_id)
        if existing:
            if existing.get("kind") != kind:
                raise ValueError("saved resume session has a conflicting role")
            return dict(existing)
        role = role_for_kind(kind)
        try:
            selected = _validated_agent_settings(
                settings_override if settings_override is not None else state.get("agent_settings")
            )[role]
        except ValueError:
            selected = default_agent_settings()[role]
        active_count = sum(
            entry.get("kind") == kind and entry.get("status") in ACTIVE_STATUSES
            for entry in state.get("sessions", {}).values()
        )
        if active_count >= MAX_ACTIVE_SESSIONS[kind]:
            raise ValueError("cannot restore the saved terminal because its role is at capacity")
        created_at = now()
        session = {
            "id": session_id, "kind": kind, "task": "",
            "status": "launching", "pid": None, "created_at": created_at,
            "auth_epoch": 1,
            "attached_at": None, "ended_at": None, "stop_requested_at": None,
            "pause_requested_at": None, "resume_launch_requested_at": None,
            "reason": "missing saved terminal transport reconstructed for project resume",
            "color": "black", "color_hex": SESSION_COLORS["black"]["hex"],
            "color_label": SESSION_COLORS["black"]["label"],
            "last_output_at": None, "output_bytes": 0, "last_status_request_at": None,
            "launch_deadline": (
                datetime.now(timezone.utc) + timedelta(seconds=30)
            ).isoformat(),
            **KINDS[kind],
            "provider": selected["provider"], "model": selected["model"],
            "effort": selected["effort"],
            "provider_label": PROVIDERS[selected["provider"]]["label"],
        }
        state.setdefault("sessions", {})[session_id] = session
        return dict(session)


def attach(root: Path, session_id: str, pid: int) -> dict[str, Any]:
    if pid <= 0:
        raise ValueError("pid must be positive")
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session:
            raise ValueError("unknown session")
        if session["status"] != "launching":
            raise ValueError("session was cancelled before it attached")
        session.update({"status": "running", "pid": pid, "attached_at": now(), "reason": "interactive CLI session is running"})
        return dict(session)


def enqueue_instruction(root: Path, session_id: str, text: str, source: str = "controller") -> dict[str, Any]:
    """Queue a visible control message for the terminal supervisor to type."""
    text = text.strip()
    if not text:
        raise ValueError("instruction text is required")
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session or session["status"] not in ACTIVE_STATUSES:
            raise ValueError("cannot route to an inactive managed session")
        if session.get("read_only") or session.get("superseded_by_session_id"):
            raise ValueError("cannot route to a superseded read-only managed session")
        entry = {
            "id": secrets.token_hex(6),
            "session_id": session_id,
            "text": text,
            "source": source[:80],
            "queued_at": now(),
        }
        state.setdefault("inbox", {}).setdefault(session_id, []).append(entry)
        state.setdefault("instruction_receipts", {})[entry["id"]] = {
            "id": entry["id"],
            "session_id": session_id,
            "source": entry["source"],
            "status": "queued",
            "queued_at": entry["queued_at"],
            "taken_at": None,
            "delivered_at": None,
        }
        _prune_instruction_receipts(state)
        return dict(entry)


def record_output(root: Path, session_id: str, byte_count: int) -> dict[str, Any]:
    """Record recent child output as activity evidence, never as a board poll."""
    if byte_count <= 0:
        raise ValueError("output byte count must be positive")
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session or session["status"] not in ACTIVE_STATUSES:
            raise ValueError("cannot record output for an inactive managed session")
        session["last_output_at"] = now()
        session["output_bytes"] = int(session.get("output_bytes", 0)) + byte_count
        return {"session_id": session_id, "last_output_at": session["last_output_at"], "output_bytes": session["output_bytes"]}


def request_status_update(root: Path, session_id: str) -> dict[str, Any] | None:
    """Route one concise update request for an active output-producing session."""
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session or session["status"] not in ACTIVE_STATUSES:
            return None
        previous = session.get("last_status_request_at")
        if previous:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(previous)).total_seconds()
            if age < 60:
                return None
        entry = {
            "id": secrets.token_hex(6),
            "session_id": session_id,
            "text": "Post a short board status update now. Your terminal output shows active work, but the board heartbeat is overdue. This is an internal reminder; owner action is not required.",
            "source": "liveness",
            "queued_at": now(),
        }
        state.setdefault("inbox", {}).setdefault(session_id, []).append(entry)
        state.setdefault("instruction_receipts", {})[entry["id"]] = {
            "id": entry["id"],
            "session_id": session_id,
            "source": entry["source"],
            "status": "queued",
            "queued_at": entry["queued_at"],
            "taken_at": None,
            "delivered_at": None,
        }
        _prune_instruction_receipts(state)
        session["last_status_request_at"] = now()
        return dict(entry)


def take_instructions(root: Path, session_id: str) -> list[dict[str, Any]]:
    """Atomically deliver all pending control messages to one terminal."""
    with locked_state(root) as state:
        _reconcile(state)
        session = state.get("sessions", {}).get(session_id, {})
        if session.get("read_only") or session.get("superseded_by_session_id"):
            discarded = state.setdefault("inbox", {}).pop(session_id, [])
            for entry in discarded:
                receipt = state.setdefault("instruction_receipts", {}).get(entry.get("id"), {})
                receipt.update({"status": "discarded", "discarded_at": now()})
            return []
        entries = state.setdefault("inbox", {}).pop(session_id, [])
        taken_at = now()
        for entry in entries:
            receipt = state.setdefault("instruction_receipts", {}).get(entry.get("id"), {})
            receipt.update({"status": "taken", "taken_at": taken_at})
        return json.loads(json.dumps(entries))


def acknowledge_instruction(root: Path, session_id: str, instruction_id: str) -> dict[str, Any]:
    """Record that the supervisor submitted one queued message to the child PTY."""
    with locked_state(root) as state:
        receipt = state.setdefault("instruction_receipts", {}).get(instruction_id)
        if not receipt:
            raise ValueError("unknown instruction receipt")
        if receipt.get("session_id") != session_id:
            raise ValueError("instruction receipt belongs to another managed session")
        if receipt.get("status") == "delivered":
            return dict(receipt)
        if receipt.get("status") != "taken":
            raise ValueError("instruction must be taken by its supervisor before delivery is acknowledged")
        receipt.update({"status": "delivered", "delivered_at": now()})
        _prune_instruction_receipts(state)
        return dict(receipt)


def instruction_receipt(root: Path, instruction_id: str) -> dict[str, Any]:
    """Return the durable transport state for one event-driven instruction."""
    with locked_state(root) as state:
        receipt = state.setdefault("instruction_receipts", {}).get(instruction_id)
        if not receipt:
            raise ValueError("unknown instruction receipt")
        return json.loads(json.dumps(receipt))


def supersede(
    root: Path,
    source_session_id: str,
    replacement_session_id: str,
    replacement_agent_id: str,
    task: str,
) -> dict[str, Any]:
    """Make a predecessor terminal read-only and stop it after durable recovery.

    The board performs the authoritative task transfer first. This operation
    then closes the obsolete transport so late terminal input cannot compete
    with the replacement owner.
    """
    if not source_session_id or not replacement_session_id or source_session_id == replacement_session_id:
        raise ValueError("distinct source and replacement sessions are required")
    with locked_state(root) as state:
        _reconcile(state)
        source = state.get("sessions", {}).get(source_session_id)
        replacement = state.get("sessions", {}).get(replacement_session_id)
        if not source:
            raise ValueError("unknown source session")
        if not replacement or replacement.get("status") not in ACTIVE_STATUSES:
            raise ValueError("replacement session is not active")
        changed_at = now()
        source.update({
            "read_only": True,
            "superseded_by_session_id": replacement_session_id,
            "superseded_by_agent_id": replacement_agent_id,
            "superseded_task": task,
            "superseded_at": changed_at,
            "stop_requested_at": changed_at,
            "reason": f"superseded by {replacement_agent_id}; read-only terminal is stopping",
        })
        state.setdefault("inbox", {}).pop(source_session_id, None)
        if source.get("pid") and _pid_is_alive(source["pid"]):
            os.kill(source["pid"], signal.SIGTERM)
            source["status"] = "stopping"
        else:
            source.update({"status": "stopped", "ended_at": changed_at})
        return dict(source)


def fail_launch(root: Path, session_id: str, reason: str) -> dict[str, Any]:
    with locked_state(root) as state:
        session = state["sessions"].get(session_id)
        if not session:
            raise ValueError("unknown session")
        if session["status"] == "launching":
            session.update({"status": "failed", "ended_at": now(), "reason": reason[:240]})
        return dict(session)


def stop(root: Path, session_id: str) -> dict[str, Any]:
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session:
            raise ValueError("unknown session")
        if session["status"] not in ACTIVE_STATUSES:
            return dict(session)
        session["auth_epoch"] = int(session.get("auth_epoch", 1)) + 1
        session["stop_requested_at"] = now()
        session["reason"] = "stop requested from control panel"
        if session["pid"] and _pid_is_alive(session["pid"]):
            os.kill(session["pid"], signal.SIGTERM)
            session["status"] = "stopping"
        else:
            session.update({"status": "stopped", "ended_at": now(), "reason": "stopped before terminal session attached"})
        return dict(session)


def pause(root: Path, session_id: str) -> dict[str, Any]:
    """Stop one live terminal while durably distinguishing intentional pause."""
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session:
            raise ValueError("unknown session")
        if session.get("status") == "paused":
            return dict(session)
        if session.get("status") not in ACTIVE_STATUSES:
            return dict(session)
        session["auth_epoch"] = int(session.get("auth_epoch", 1)) + 1
        requested_at = now()
        session["pause_requested_at"] = requested_at
        session["reason"] = "project pause requested; preserving saved session pointer"
        if session.get("pid") and _pid_is_alive(session["pid"]):
            try:
                os.kill(session["pid"], signal.SIGTERM)
            except ProcessLookupError:
                session.update({
                    "status": "paused", "ended_at": requested_at,
                    "reason": "terminal exited while the project pause signal was sent",
                })
            else:
                session["status"] = "pausing"
        else:
            session.update({
                "status": "paused", "ended_at": requested_at,
                "reason": "terminal intentionally paused before attachment",
            })
        return dict(session)


def pause_sessions(
    root: Path, session_ids: list[str], *, timeout: float = 3.0,
) -> list[dict[str, Any]]:
    """Pause sessions together, bounding termination and marking every target."""
    unique = list(dict.fromkeys(str(value) for value in session_ids if str(value)))
    for session_id in unique:
        pause(root, session_id)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        with locked_state(root) as state:
            _reconcile(state)
            if all(
                state["sessions"].get(session_id, {}).get("status") == "paused"
                for session_id in unique
            ):
                return [dict(state["sessions"][session_id]) for session_id in unique]
        time.sleep(0.02)
    with locked_state(root) as state:
        _reconcile(state)
        for session_id in unique:
            session = state["sessions"].get(session_id)
            if not session or session.get("status") == "paused":
                continue
            pid = session.get("pid")
            if pid and _pid_is_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            session.update({
                "status": "paused", "ended_at": now(),
                "reason": "terminal force-stopped after the bounded pause timeout",
            })
        return [dict(state["sessions"][session_id]) for session_id in unique]


def prepare_resume_sessions(root: Path, session_ids: list[str]) -> list[dict[str, Any]]:
    """Re-adopt surviving PIDs and stage each dead terminal for one relaunch."""
    unique = list(dict.fromkeys(str(value) for value in session_ids if str(value)))
    prepared: list[dict[str, Any]] = []
    with locked_state(root) as state:
        _reconcile(state)
        for session_id in unique:
            session = state.get("sessions", {}).get(session_id)
            if not session:
                prepared.append({"id": session_id, "action": "missing"})
                continue
            pid = session.get("pid")
            if pid and _pid_is_alive(pid):
                session.pop("resume_offer", None)
                session.update({
                    "status": "running", "ended_at": None,
                    "pause_requested_at": None, "resume_launch_requested_at": None,
                    "reason": "surviving terminal re-adopted during project resume",
                })
                prepared.append({**dict(session), "action": "re_adopted"})
                continue
            if (
                session.get("status") == "launching"
                and session.get("resume_launch_requested_at")
            ):
                prepared.append({**dict(session), "action": "awaiting_attachment"})
                continue
            session.update({
                "status": "launching", "pid": None, "ended_at": None,
                "auth_epoch": int(session.get("auth_epoch", 1)) + 1,
                "pause_requested_at": None, "stop_requested_at": None,
                "resume_launch_requested_at": None,
                # The offer is the owner-consent boundary: only sessions the
                # resume selected for relaunch may be launched by the owner's
                # button; a retired terminal is never offered.
                "resume_offer": "relaunch",
                "launch_deadline": (
                    datetime.now(timezone.utc) + timedelta(seconds=30)
                ).isoformat(),
                "reason": "terminal ready to relaunch from its saved next action",
            })
            prepared.append({**dict(session), "action": "relaunch"})
    return prepared


def mark_resume_launch_requested(root: Path, session_id: str) -> dict[str, Any]:
    """Claim one staged relaunch before opening Terminal, preventing duplicates."""
    with locked_state(root) as state:
        _reconcile(state)
        session = state.get("sessions", {}).get(session_id)
        if not session or session.get("status") != "launching":
            raise ValueError("resume terminal is not staged for launch")
        if session.get("resume_launch_requested_at"):
            return dict(session)
        if session.get("resume_offer") != "relaunch":
            raise ValueError("this terminal was not offered for relaunch")
        session["resume_launch_requested_at"] = now()
        session["reason"] = "resume terminal launch requested; waiting for attachment"
        return dict(session)


def snapshot(root: Path) -> dict[str, Any]:
    with locked_state(root) as state:
        _reconcile(state)
        sessions = sorted(state["sessions"].values(), key=lambda value: value["created_at"], reverse=True)
        active_counts = {
            kind: sum(item["kind"] == kind and item["status"] in ACTIVE_STATUSES for item in sessions)
            for kind in KINDS
        }
        result = {
            "sessions": json.loads(json.dumps(sessions)),
            "active_counts": active_counts,
            "limits": dict(MAX_ACTIVE_SESSIONS),
        }
    result["agent_settings"] = agent_settings(root)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harness local CLI-session lifecycle registry")
    add_context_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)
    attach_parser = subparsers.add_parser("attach")
    attach_parser.add_argument("--id", required=True)
    attach_parser.add_argument("--pid", required=True, type=int)
    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("--kind", required=True)
    resolve_parser.add_argument("--session-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "attach":
        result = attach(context_from_args(args), args.id, args.pid)
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "resolve":
        root = context_from_args(args)
        value = session_launch_settings(root, args.session_id, args.kind)
        print(json.dumps(value, sort_keys=True))
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())

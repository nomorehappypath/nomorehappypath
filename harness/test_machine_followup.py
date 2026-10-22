# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""What happens right after the owner connects the test machine (plan X1, S8, U5, W3).

1. Every running agent is told, once, the current connection and the rule (no manual block; install tools yourself).
2. Every request recorded under an older machine stamp (waiting, in review, or passed) gets a durable replacement
   obligation naming its responsible role (the task's Delivery, or the CTO when Delivery is gone); it is sent at once,
   not at the next pause or resume, and shown on the card as "recovering N" until the task has a current request.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from harness import board, control, test_machine
from harness.project_context import ProjectRoot, project_context

TERMINAL_STATUSES = {"failed", "cancelled", "canceled", "superseded", "withdrawn"}


_HANDOFF_LOCKS: dict[str, threading.RLock] = {}
_HANDOFF_LOCKS_GUARD = threading.Lock()


def handoff_lock(root: ProjectRoot) -> threading.RLock:
    """One lock per project: the announcement after a Connect or a Resume and the recovery refresh never overlap."""
    key = str(project_context(root).storage_path(test_machine.SUBDIR))
    with _HANDOFF_LOCKS_GUARD:
        return _HANDOFF_LOCKS.setdefault(key, threading.RLock())


def handoff_pending(root: ProjectRoot) -> bool:
    """True while an announcement owed for the CURRENT authorization has not completed (set by Connect and by a kept Resume)."""
    try:
        value = test_machine.read(project_context(root)) or {}
    except test_machine.Damaged:
        return False
    return bool(value.get("handoff_pending")) and not value.get("reauth_required") and int(value["handoff_pending"]) == int(value.get("auth_epoch", 0))


def connection_message(root: ProjectRoot) -> str:
    report = test_machine.status_report(root)
    return (
        "[SYSTEM CONTROL — test-machine] The owner connected the test machine: "
        f"{report['user']}@{report['address']}:{report['port']}, host key {report['host_key_fingerprint']}, "
        f"revision {report['revision']}. Key file {report['key_path']}, known hosts {report['known_hosts_path']} "
        "(use them exactly as the test-machine rule says). "
        + report["rule"]
        + " Run machine-status first: if it says the machine is not usable or needs the password, this announcement is obsolete."
    )


def _active_sessions(root: ProjectRoot) -> list[dict[str, Any]]:
    return [value for value in control.snapshot(project_context(root)).get("sessions", []) if value.get("status") in control.ACTIVE_STATUSES]


def stale_requests(root: ProjectRoot, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    state = state if state is not None else board.snapshot(root)
    found = []
    for request_id, request in (state.get("qa_requests") or {}).items():
        if str(request.get("status", "")).lower() in TERMINAL_STATUSES:
            continue
        stale = board._stale_machine_evidence(root, request)
        if stale:
            found.append({"request_id": request_id, "task": str(request.get("task") or ""), "status": request.get("status", ""),
                          "developer_id": request.get("developer_id", ""), "commands": sorted(set(stale))})
    return found


def _responsible(state: dict[str, Any], task: str, developer_id: str) -> tuple[str, str, str]:
    """(agent id, role, session id) of the task's active Delivery, else the active CTO, else ('', 'nobody', '')."""
    agents = state.get("agents") or {}
    candidates = [agents.get(developer_id) or {}] + [value for value in agents.values() if value.get("role") == "engineering" and value.get("task") == task]
    for agent in candidates:
        if agent.get("active") and agent.get("role") == "engineering" and agent.get("session_id"):
            return str(agent.get("id") or developer_id), "Delivery", str(agent["session_id"])
    for agent_id, agent in agents.items():
        if agent.get("active") and agent.get("role") == "cto" and agent.get("session_id"):
            return str(agent_id), "CTO", str(agent["session_id"])
    return "", "nobody", ""


def after_connect(root: ProjectRoot) -> dict[str, Any]:
    """Announce the current connection and persist the stale-evidence obligations. `completed` is True only when the work finished for
    the authorization that stands now; False (nothing recorded) when a stop, release or newer connection overtook it."""
    root = project_context(root)
    with handoff_lock(root):
        try:
            value = test_machine.read(root) or {}
        except test_machine.Damaged:
            return {"completed": False, "sessions_told": [], "obligations": []}
        if not value.get("connected") or value.get("reauth_required"):
            return {"completed": False, "sessions_told": [], "obligations": []}
        epoch = int(value.get("auth_epoch", 0))
        sent: list[str] = []
        announced = int(value.get("announced_epoch", -1)) == epoch
        if not announced:
            text = connection_message(root)
            for session in _active_sessions(root):
                try:
                    control.enqueue_instruction(root, str(session["id"]), text, source="test-machine")
                    sent.append(str(session["id"]))
                    announced = True
                except ValueError:
                    pass   # a session that ended or is paused is told by the Resume handoff or its launch notes
        already_sent = {item.get("request_id"): item for item in value.get("recovering") or []
                        if item.get("epoch") == epoch and item.get("state") == "sent"}
        state = board.snapshot(root)
        obligations = []
        for item in stale_requests(root, state):
            if item["request_id"] in already_sent:
                obligations.append(already_sent[item["request_id"]])
                continue
            agent_id, role, session_id = _responsible(state, item["task"], str(item.get("developer_id") or ""))
            obligation = {**item, "owner_agent_id": agent_id, "owner_role": role, "state": "sent" if session_id else "waiting_for_an_agent",
                          "created_at": test_machine.now(), "epoch": epoch}
            if session_id:
                message = (
                    "[SYSTEM CONTROL — test-machine] The machine was reconnected, so your earlier machine evidence for request "
                    f"{item['request_id']} (task {item['task']}: {'; '.join(item['commands'])[:200]}) is stale. Run machine-status, then "
                    "resubmit the task's review under the current machine revision: Delivery resubmits and a fresh review request is made "
                    "(the Reviewer's rerun on the old request cannot clear its stale records). Do not reuse the old request."
                )
                try:
                    control.enqueue_instruction(root, session_id, message, source="test-machine")
                except ValueError:
                    obligation["state"] = "waiting_for_an_agent"
            obligations.append(obligation)
        done = {"value": False}

        def change(current: dict[str, Any]) -> None:
            if int(current.get("auth_epoch", 0)) != epoch or current.get("reauth_required"):
                return   # a stop, release or newer connection owns the record now
            current["recovering"] = obligations
            current["waiters"] = []
            if announced:
                current["announced_epoch"] = epoch   # nobody told (everything paused): the Resume handoff announces it
            if int(current.get("handoff_pending", 0)) == epoch:
                current["handoff_pending"] = 0
            done["value"] = True

        test_machine.update(root, change, create=False)
        return {"completed": done["value"], "sessions_told": sent, "obligations": obligations}


def refresh_recovering(root: ProjectRoot) -> list[dict[str, Any]]:
    """Drop obligations whose request is no longer stale or no longer open; re-send those with no agent when one exists."""
    root = project_context(root)
    with handoff_lock(root):
        return _refresh_recovering_locked(root)


def _refresh_recovering_locked(root: ProjectRoot) -> list[dict[str, Any]]:
    try:
        record = test_machine.read(root) or {}
    except test_machine.Damaged:
        return []
    current = record.get("recovering") or []
    epoch = int(record.get("auth_epoch", 0))
    if not current:
        return []
    state = board.snapshot(root)
    still = {item["request_id"]: item for item in stale_requests(root, state)}
    kept = []
    for obligation in current:
        task = obligation.get("task", "")
        resubmitted = any(
            other_id != obligation.get("request_id") and str(other.get("task") or "") == task
            and str(other.get("status", "")).lower() not in TERMINAL_STATUSES and other_id not in still
            and any(row.get("machine") for row in other.get("command_executions", []) or [])
            for other_id, other in (state.get("qa_requests") or {}).items()
        )
        if obligation.get("request_id") in still and not resubmitted:
            if obligation.get("state") == "waiting_for_an_agent":
                agent_id, role, session_id = _responsible(state, obligation.get("task", ""), obligation.get("developer_id", ""))
                if session_id:
                    try:
                        control.enqueue_instruction(
                            root, session_id,
                            "[SYSTEM CONTROL — test-machine] Machine evidence for request "
                            f"{obligation['request_id']} is stale after the machine was reconnected: run machine-status and resubmit under the current revision.",
                            source="test-machine")
                        obligation = {**obligation, "owner_agent_id": agent_id, "owner_role": role, "state": "sent"}
                    except ValueError:
                        pass
            kept.append(obligation)
    if kept != current:
        test_machine.update(root, lambda value: value.update({"recovering": kept}) if int(value.get("auth_epoch", 0)) == epoch else None, create=False)
    return kept

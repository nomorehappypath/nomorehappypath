# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The harness restarts stuck agents by itself (backlog #7, owner feature).

Owner, 2026-09-26: "i cannot afford being abscent for hours and come back and
see you guys stuck waiting for me". Three cases, each acted on by the worker's
watchdog, each announced with one plain Mission Control line:

- FROZEN: a live terminal has had a message queued undelivered and has shown
  no output for FROZEN_AFTER_SECONDS. It is stopped WITHOUT cancelling its
  task, and the SAME session is relaunched: the CLI resumes its own
  conversation and the board reattaches the SAME agent (`request_agent_restart`).
- ENDED: a terminal ended by itself (not stopped by the owner, not paused)
  while its agent had unfinished work. It is relaunched the same way.
- NO REVIEWER: an open review has had no live eligible reviewer for
  REVIEWER_WAIT_SECONDS. One Reviewer terminal is started.

Never for a terminal the owner stopped, a paused project, a superseded
terminal, or a screen asking for sign-in (sign-in is the owner's; backlog #8
alerts for it). At most MAX_ACTIONS_PER_HOUR for one agent or role; after that
the harness stops trying and says so in one plain line.

A restart takes two watchdog ticks: stop now, relaunch once the old terminal
has ended. Deciding is separate from acting so both can be tested directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from harness import board, control

FROZEN_AFTER_SECONDS = 15 * 60
REVIEWER_WAIT_SECONDS = 5 * 60
# Only a terminal that ended recently is brought back: an old ended session is
# history, not a stuck agent (the first pass after an upgrade must not reopen
# terminals that closed days ago).
ENDED_RECENT_SECONDS = 30 * 60
MAX_ACTIONS_PER_HOUR = 3
SIGN_IN_WORDS = ("login", "log in", "sign in", "signed out", "logged out", "401")
EVENT_KINDS = ("agent_restart_requested", "agent_restarted_by_harness", "reviewer_started_by_harness", "self_heal_failed", "self_heal_gave_up")
ROLE_BY_KIND = {"codex_delivery": "the Delivery Agent", "claude_reviewer": "the Reviewer", "claude_cto": "the CTO"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parsed(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age(value: Any, current: datetime) -> float | None:
    parsed = _parsed(value)
    return None if parsed is None else (current - parsed).total_seconds()


def _needs_sign_in(session: dict[str, Any]) -> bool:
    reason = str(session.get("attention_reason") or "").lower()
    return any(word in reason for word in SIGN_IN_WORDS)


def _unfinished(state: dict[str, Any], agent: dict[str, Any]) -> bool:
    """Work the agent would lose if nobody brought it back."""
    role, task = agent.get("role"), str(agent.get("task") or "")
    if role == "cto":
        return True
    if role in board.DEVELOPER_ROLES:
        if not task or task == board.AWAITING_OWNER_DIRECTION:
            return False
        decided = (state.get("release_decisions") or {}).get(task, {}).get("decision") == "accepted"
        return not decided and task not in (state.get("cancelled_tasks") or {}) and task not in (state.get("git_acceptances") or {})
    if role == "qa":
        return any(
            request.get("status") in {"reserved", "claimed"}
            and agent.get("id") in {request.get("reserved_by"), request.get("claimed_by")}
            for request in (state.get("qa_requests") or {}).values()
        )
    return False


def _replaced(state: dict[str, Any], control_state: dict[str, Any], agent: dict[str, Any], session: dict[str, Any]) -> bool:
    """Another live agent already carries this work (a replacement, or the one CTO)."""
    live_sessions = {
        str(item.get("id")) for item in control_state.get("sessions", [])
        if item.get("status") in control.ACTIVE_STATUSES
    }
    for other in (state.get("agents") or {}).values():
        if other.get("id") == agent.get("id") or str(other.get("session_id")) not in live_sessions:
            continue
        if agent.get("role") == "cto" and other.get("role") == "cto":
            return True
        if other.get("role") == agent.get("role") and other.get("task") == agent.get("task"):
            return True
    return False


def _ledger(state: dict[str, Any]) -> dict[str, Any]:
    """Persistent attempt ledger (review round 1): the hot event window is
    trimmed on a busy board, so it cannot be what enforces the hourly cap."""
    ledger = state.get("self_heal") or {}
    return {"attempts": dict(ledger.get("attempts") or {}), "gave_up": dict(ledger.get("gave_up") or {})}


def _recent_attempts(state: dict[str, Any], key: str, current: datetime) -> int:
    since = current - timedelta(hours=1)
    return sum(1 for at in _ledger(state)["attempts"].get(key, []) if (_parsed(at) or since) > since)


def _gave_up_recently(state: dict[str, Any], key: str, current: datetime) -> bool:
    at = _parsed(_ledger(state)["gave_up"].get(key))
    return at is not None and at > current - timedelta(hours=1)


def _note_attempt(root: Path, key: str) -> None:
    """Count an attempt BEFORE acting, so a failure can never bypass the cap."""
    current = _now()
    since = current - timedelta(hours=1)
    with board.locked_state(root) as state:
        ledger = state.setdefault("self_heal", {})
        attempts = ledger.setdefault("attempts", {})
        kept = [at for at in attempts.get(key, []) if (_parsed(at) or since) > since]
        attempts[key] = kept + [current.isoformat()]


def plan(root: Path, current: datetime | None = None) -> list[dict[str, Any]]:
    """Decide, without acting, what the harness should do right now."""
    current = current or _now()
    if board.pause_state(root).get("status") != "active":
        return []
    state = board.snapshot(root)
    control_state = control.snapshot(root)
    queued = control.queued_instructions(root)
    inbox, receipts = queued["inbox"], queued["receipts"]
    agents_by_session = {
        str(agent.get("session_id")): agent for agent in (state.get("agents") or {}).values()
        if agent.get("session_id")
    }
    actions: list[dict[str, Any]] = []
    for session in control_state.get("sessions", []):
        if session.get("read_only") or session.get("superseded_by_session_id"):
            continue
        agent = agents_by_session.get(str(session.get("id")))
        if not agent or _needs_sign_in(session):
            continue
        status = session.get("status")
        if agent.get("restart_requested_at") and status not in control.ACTIVE_STATUSES:
            # Phase two of a restart: the old terminal has ended; bring it back.
            # Capped like everything else (review round 1: a failing launcher
            # made this repeat every tick).
            actions.append(_limited(state, f"agent:{agent['id']}", current, {
                "action": "relaunch", "session_id": session["id"], "agent_id": agent["id"],
                "kind": session.get("kind", ""), "reason": agent.get("restart_reason", "")}))
            continue
        if agent.get("restart_requested_at"):
            continue  # stopping; wait for the terminal to end
        key = f"agent:{agent['id']}"
        if status == "running":
            queued_ages = [_age(item.get("queued_at"), current) for item in inbox.get(session["id"], [])]
            queued_ages += [
                _age(item.get("taken_at"), current) for item in receipts.values()
                if item.get("session_id") == session["id"] and item.get("status") == "taken" and not item.get("delivered_at")
            ]
            oldest_queued = max((age for age in queued_ages if age is not None), default=None)
            quiet = _age(session.get("last_output_at") or session.get("attached_at"), current)
            if (oldest_queued is not None and oldest_queued >= FROZEN_AFTER_SECONDS
                    and quiet is not None and quiet >= FROZEN_AFTER_SECONDS):
                actions.append(_limited(state, key, current, {
                    "action": "restart", "session_id": session["id"], "agent_id": agent["id"],
                    "kind": session.get("kind", ""),
                    "reason": f"its terminal showed nothing for {int(quiet // 60)} minutes while messages waited",
                }))
        elif status == "exited" and not session.get("stop_requested_at") and not session.get("pause_requested_at"):
            if agent.get("active") is False and agent.get("status") not in {"offline"}:
                continue  # stopped or cancelled on purpose; never bring it back
            ended = _age(session.get("ended_at"), current)
            if ended is None or ended > ENDED_RECENT_SECONDS:
                continue
            if _replaced(state, control_state, agent, session):
                continue
            if _unfinished(state, agent):
                actions.append(_limited(state, key, current, {
                    "action": "relaunch_ended", "session_id": session["id"], "agent_id": agent["id"],
                    "kind": session.get("kind", ""),
                    "reason": "its terminal closed while it still had work to finish",
                }))
    needed = state.get("reviewer_needed") or {}
    waited = _age(needed.get("requested_at"), current)
    live = [session for session in control_state.get("sessions", []) if session.get("status") in control.ACTIVE_STATUSES]
    reviewer_starting = any(
        session.get("kind") == "claude_reviewer" and not agents_by_session.get(str(session.get("id")))
        for session in live
    )
    # Claude sign-in is shared by every Claude terminal on the machine: a new
    # Reviewer would be signed out too. Backlog #8 tells the owner instead.
    claude_signed_out = any(session.get("kind", "").startswith("claude") and _needs_sign_in(session) for session in live)
    if needed and waited is not None and waited >= REVIEWER_WAIT_SECONDS and not reviewer_starting and not claude_signed_out:
        actions.append(_limited(state, "role:reviewer", current, {
            "action": "start_reviewer", "kind": "claude_reviewer",
            "reason": f"a review has waited {int(waited // 60)} minutes with no reviewer available",
        }))
    return [action for action in actions if action]


def _limited(state: dict[str, Any], key: str, current: datetime, action: dict[str, Any]) -> dict[str, Any]:
    if _recent_attempts(state, key, current) >= MAX_ACTIONS_PER_HOUR:
        if _gave_up_recently(state, key, current):
            return {}
        return {"action": "give_up", "self_heal_key": key, "kind": action.get("kind", ""),
                "agent_id": action.get("agent_id", ""), "reason": action.get("reason", "")}
    return {**action, "self_heal_key": key}


def _record(root: Path, kind: str, key: str, message: str, **payload: Any) -> None:
    with board.locked_state(root) as state:
        agent = (state.get("agents") or {}).get(payload.get("agent_id", ""))
        board._event(state, kind, agent, {"task": (agent or {}).get("task", ""), "self_heal_key": key, "message": message, **payload})


def execute(root: Path, action: dict[str, Any], launch: Callable[[dict[str, Any]], Any],
            create: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Carry out one planned action.

    `launch(session)` opens a terminal for a staged session; `create(kind)`
    makes a new session with the owner's saved model settings (the worker
    passes the same creation the Mission Control button uses).

    Every intervention is counted in the ledger BEFORE it acts (review round
    1): the stop of a frozen terminal, the relaunch of one that ended by
    itself, and the start of a Reviewer. The second step of a restart (the
    relaunch after the stop) is part of the same intervention and counts only
    if it fails, so a failing launcher is capped too. A failure also leaves
    one plain line for the owner.
    """
    kind, key = action.get("kind", ""), action.get("self_heal_key", "")
    who = ROLE_BY_KIND.get(kind, "the agent")
    if action["action"] == "give_up":
        with board.locked_state(root) as state:
            state.setdefault("self_heal", {}).setdefault("gave_up", {})[key] = _now().isoformat()
        _record(root, "self_heal_gave_up", key,
                f"The harness tried to bring back {who} {MAX_ACTIONS_PER_HOUR} times in the last hour and it is still "
                f"stuck ({action['reason']}). Please open its terminal to see what it needs.",
                agent_id=action.get("agent_id", ""))
        return {"action": "give_up", "self_heal_key": key}
    if action["action"] in {"restart", "relaunch_ended", "start_reviewer"}:
        _note_attempt(root, key)
    try:
        if action["action"] == "restart":
            board.request_agent_restart(root, action["agent_id"], action["reason"])
            control.stop(root, action["session_id"])
            return {"action": "restart", "phase": "stopping", "session_id": action["session_id"]}
        if action["action"] == "relaunch_ended":
            board.request_agent_restart(root, action["agent_id"], action["reason"])
            return _relaunch(root, action["session_id"], launch)
        if action["action"] == "relaunch":
            try:
                return _relaunch(root, action["session_id"], launch)
            except Exception:
                _note_attempt(root, key)
                raise
        if action["action"] == "start_reviewer":
            session = (create or (lambda value: control.create(root, value)))("claude_reviewer")
            launch(session)
            _record(root, "reviewer_started_by_harness", key,
                    f"The harness started a Reviewer because {action['reason']}.", session_id=session["id"])
            return {"action": "start_reviewer", "session_id": session["id"]}
    except Exception as error:
        _record(root, "self_heal_failed", key,
                f"The harness tried to {'start a Reviewer' if action['action'] == 'start_reviewer' else f'bring back {who}'} "
                f"but could not: {str(error)[:160]}",
                agent_id=action.get("agent_id", ""))
        raise
    raise ValueError(f"unknown self-heal action {action['action']!r}")


def _relaunch(root: Path, session_id: str, launch: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    staged = control.prepare_resume_sessions(root, [session_id])
    if not staged or staged[0].get("action") not in {"relaunch", "awaiting_attachment"}:
        return {"action": "relaunch", "session_id": session_id, "skipped": (staged or [{}])[0].get("action", "missing")}
    if staged[0].get("action") == "relaunch":
        session = control.mark_resume_launch_requested(root, session_id)
        launch(session)
    return {"action": "relaunch", "session_id": session_id, "phase": "launched"}


def run_once(root: Path, launch: Callable[[dict[str, Any]], Any], current: datetime | None = None,
             create: Callable[[str], dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One self-heal pass: plan, then act on each decision; one failure never blocks the rest."""
    results = []
    for action in plan(root, current):
        try:
            results.append(execute(root, action, launch, create))
        except Exception as error:  # one failure never stops the watchdog; it is already counted and recorded
            results.append({"action": action.get("action"), "error": str(error)[:300]})
    return results

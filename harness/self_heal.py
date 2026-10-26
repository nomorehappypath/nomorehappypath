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

- STALLED (2026-10-15, Studio: Delivery was silent 8.5 minutes and only the
  CTO did something, by doing Delivery's job): ANY role with unfinished work
  that records no board heartbeat for the stall window (board.stall_settings)
  climbs a ladder, whatever else is open on its task (a control-plane hold
  never pauses it): wake it, restart its terminal from its saved conversation,
  replace the terminal by a new one that continues the conversation and the
  task. Every step is a plain board event; only when the ladder is exhausted
  is there exactly ONE plain card for the owner.

A restart takes two watchdog ticks: stop now, relaunch once the old terminal
has ended. Deciding is separate from acting so both can be tested directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from harness import attention, claude_auth, board, control, review_rounds

FROZEN_AFTER_SECONDS = 15 * 60
REVIEWER_WAIT_SECONDS = 5 * 60
# Only a terminal that ended recently is brought back: an old ended session is
# history, not a stuck agent (the first pass after an upgrade must not reopen
# terminals that closed days ago).
ENDED_RECENT_SECONDS = 30 * 60
MAX_ACTIONS_PER_HOUR = 3
SIGN_IN_WORDS = ("login", "log in", "sign in", "signed out", "logged out", "401", "out of quota")
STALL_ROLES = set(board.DEVELOPER_ROLES) | {"qa", "cto"}
# A long executable check keeps its owner exempt from the ladder only this long
# (plan check 2026-10-15 finding 4): a lease heartbeat proves the wrapper lives,
# not that the command advances.
STALL_LEASE_MAX_SECONDS = 30 * 60
RECOVERY_CREATE_GRACE_SECONDS = 120
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
    return attention.needs_consent(session) or any(word in reason for word in SIGN_IN_WORDS)


def _limit_held(session: dict[str, Any], current: datetime | None = None) -> bool:
    """A LIVE terminal that shows its provider's usage-limit message, or that is within the grace after the line cleared.

    The hold belongs to a live terminal: one that ended while held is an ordinary exit (control clears its hold fields), so
    it reaches relaunch and recovery as always.
    """
    if session.get("status") not in control.ACTIVE_STATUSES:
        return False
    return attention.is_limit_reason(session.get("attention_reason")) or attention.in_limit_grace(session, current or _now())


def _unfinished(state: dict[str, Any], agent: dict[str, Any]) -> bool:
    """Work the agent would lose if nobody brought it back."""
    role, task = agent.get("role"), str(agent.get("task") or "")
    if role == "cto":
        return True
    if role in board.DEVELOPER_ROLES:
        if not task or task == board.AWAITING_OWNER_DIRECTION:
            return False
        decided = (state.get("release_decisions") or {}).get(task, {}).get("decision") == "accepted"
        if board.work_left_outside_stopped_scopes(state, task) is False:
            return False   # only parts the harness stopped repairing are left: never brought back; other parts of the task stay eligible
        return not decided and task not in (state.get("cancelled_tasks") or {}) and task not in (state.get("git_acceptances") or {})
    if role == "qa":
        return any(
            request.get("status") in {"reserved", "claimed"}
            and agent.get("id") in {request.get("reserved_by"), request.get("claimed_by")}
            for request in (state.get("qa_requests") or {}).values()
        )
    return False


def _barred(state: dict[str, Any], agent: dict[str, Any]) -> bool:
    """THE terminal-scope check of every restart and relaunch route (pending restart, frozen terminal, natural exit, stall ladder, watchdog):
    a Delivery agent whose task has only scopes left that the harness stopped repairing is never brought back. A task with other unfinished
    work, or a new structure revision, is not barred."""
    return agent.get("role") in board.DEVELOPER_ROLES and board.work_left_outside_stopped_scopes(state, str(agent.get("task") or "")) is False


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


def _stall_exhausted(state: dict[str, Any], agent: dict[str, Any] | None) -> bool:
    """The stall ladder already raised its one owner card for this role and task: no
    other path (frozen, ended, launch failure) restarts or announces until a real
    heartbeat clears it (plan check 2026-10-15 finding 6: one budget, one card)."""
    if not agent:
        return False
    return bool(((state.get("stall_ladder") or {}).get(_stall_key(agent)) or {}).get("card"))


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
    announced = (state.get("self_heal") or {}).get("announced") or {}
    for session in control_state.get("sessions", []):
        # A terminal that stopped before its CLI started says why on the board, once per attempt
        # (a registered agent is not needed: the idle reviewer of 2026-10-10 never got that far).
        failure = session.get("launch_failure")
        if (isinstance(failure, dict) and failure.get("reason") and session.get("status") not in control.ACTIVE_STATUSES
                and not (session.get("read_only") or session.get("superseded_by_session_id")
                         or session.get("stop_requested_at") or session.get("pause_requested_at"))):
            key = f"launch:{session.get('id')}:{session.get('attached_at') or session.get('launch_deadline')}"
            failed_agent = agents_by_session.get(str(session.get("id")))
            if failed_agent:
                # Work was registered here: only a session that carries THAT work replaces it.
                replaced = _replaced(state, control_state, failed_agent, session) or any(
                    other.get("continues_session") == session.get("id") and other.get("status") in control.ACTIVE_STATUSES
                    for other in control_state.get("sessions", []))
            else:
                # Nothing was registered, so nothing can be stranded: a newer live session of the role is the replacement.
                replaced = any(
                    other.get("kind") == session.get("kind") and other.get("id") != session.get("id")
                    and other.get("status") in control.ACTIVE_STATUSES
                    and str(other.get("created_at") or "") > str(session.get("created_at") or "")
                    for other in control_state.get("sessions", []))
            if key not in announced and not replaced and not _stall_exhausted(state, failed_agent):
                actions.append({"action": "announce_launch_failure", "session_id": session["id"],
                                "kind": session.get("kind", ""), "reason": failure["reason"], "self_heal_key": key,
                                "attempt": str(failure.get("at") or ""),
                                "agent_id": (failed_agent or {}).get("id", "")})
        if session.get("read_only") or session.get("superseded_by_session_id"):
            continue
        agent = agents_by_session.get(str(session.get("id")))
        if not agent or _barred(state, agent) or _needs_sign_in(session) or _limit_held(session, current) or _stall_exhausted(state, agent):
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
    actions.extend(_stall_actions(state, control_state, agents_by_session, actions, current))
    actions.extend(_recovery_actions(state, control_state, current))
    needed = state.get("reviewer_needed") or {}
    retry_starting = any(e.get("status") == "recovery" and e.get("who") == "reviewer" and e.get("phase") != "started"
                         for e in (state.get("review_escalations") or {}).values())
    waited = _age(needed.get("requested_at"), current)
    live = [session for session in control_state.get("sessions", []) if session.get("status") in control.ACTIVE_STATUSES]
    reviewer_starting = any(
        session.get("kind") == "claude_reviewer" and not agents_by_session.get(str(session.get("id")))
        for session in live
    )
    # A provider's sign-in is shared by every terminal of that provider on the
    # machine: a new Reviewer of the configured provider would be signed out
    # too. Backlog #8 tells the owner instead. (Claude and Gemini alike.)
    reviewer_provider = str(((control_state.get("agent_settings") or {}).get("reviewer") or {}).get("provider") or "")
    claude_signed_out = any(
        (session.get("provider") == reviewer_provider if reviewer_provider and session.get("provider")
         else session.get("kind", "").startswith("claude")) and _needs_sign_in(session)
        for session in live)
    # A Reviewer of the required provider that is only waiting out its usage limit is present, not missing: another terminal
    # against the same exhausted allowance would only fail too.
    reviewer_limit_held = any(
        control.KIND_TO_ROLE.get(str(session.get("kind", ""))) == "reviewer"
        and (not reviewer_provider or not session.get("provider") or session.get("provider") == reviewer_provider)
        and _limit_held(session, current)
        for session in live)
    if needed and waited is not None and waited >= REVIEWER_WAIT_SECONDS and not reviewer_starting and not claude_signed_out \
            and not reviewer_limit_held and not retry_starting:
        actions.append(_limited(state, "role:reviewer", current, {
            "action": "start_reviewer", "kind": "claude_reviewer",
            "reason": f"a review has waited {int(waited // 60)} minutes with no reviewer available",
        }))
    return [action for action in actions if action]


# ---------------------------------------------------------------------------
# The one fresh Delivery attempt after the round limit (2026-10-25). Phases are recorded in the board's review_escalations entry
# (requested, stopping, creating, started), every step is idempotent, and the owner is never asked anything.
# ---------------------------------------------------------------------------
def _recovery_actions(state: dict[str, Any], control_state: dict[str, Any], current: datetime) -> list[dict[str, Any]]:
    sessions = {str(item.get("id")): item for item in control_state.get("sessions", [])}
    resumed = _parsed((state.get("project_pause") or {}).get("resumed_at"))
    actions: list[dict[str, Any]] = []
    for key, entry in sorted((state.get("review_escalations") or {}).items()):
        if entry.get("status") == "could_not_finish" and entry.get("session_id") and not entry.get("retired_at") and entry.get("who") != "reviewer":
            made = sessions.get(str(entry["session_id"]))
            if made is not None and made.get("status") in control.ACTIVE_STATUSES and board.work_left_outside_stopped_scopes(state, str(entry.get("task") or "")) is False:
                actions.append({"review_key": key, "task": entry["task"], "self_heal_key": f"review-recovery:{key}", "at": current.isoformat(),
                                "action": "review_recovery_retire", "session_id": made["id"]})
            continue
        if entry.get("status") != "recovery":
            continue
        task = str(entry.get("task") or "")
        base = {"review_key": key, "task": task, "self_heal_key": f"review-recovery:{key}", "at": current.isoformat(),
                **({"who": "reviewer"} if entry.get("who") == "reviewer" else {})}
        if task in (state.get("cancelled_tasks") or {}):
            actions.append({**base, "action": "review_recovery_settle", "reason": "the task was cancelled", "closed": True})
            continue
        phase = str(entry.get("phase") or "requested")
        reviewer = entry.get("who") == "reviewer"
        agent = ((state.get("agents") or {}).get(str(entry.get("reviewer_id") or "")) if reviewer else
                 next((a for a in (state.get("agents") or {}).values()
                       if a.get("role") in board.DEVELOPER_ROLES and a.get("task") == task and a.get("active")), None))
        if reviewer and phase == "requested" and agent and any(
                r.get("status") in {"reserved", "claimed"} and agent.get("id") in (r.get("reserved_by"), r.get("claimed_by"))
                for r in (state.get("qa_requests") or {}).values()):
            # The Reviewer that ended the review still holds another review: replacing it now would strand that work. Wait, up to the recovery wait.
            if _anchored_age(entry.get("requested_at"), resumed, current) >= review_rounds.recovery_wait_seconds():
                actions.append({**base, "action": "review_recovery_settle", "reason": "the previous Reviewer stayed busy with another review"})
            continue
        if phase == "requested":
            session = sessions.get(str((agent or {}).get("session_id") or ""))
            if agent and agent.get("active") and session and session.get("status") in control.ACTIVE_STATUSES:
                actions.append({**base, "action": "review_recovery_stop", "agent_id": agent["id"], "session_id": session["id"], "kind": session.get("kind", "")})
            else:
                actions.append({**base, "action": "review_recovery_create"})
        elif phase == "stopping":
            old = sessions.get(str(entry.get("old_session_id") or ""))
            if old is None or old.get("status") not in control.ACTIVE_STATUSES:
                actions.append({**base, "action": "review_recovery_create"})
            elif _anchored_age(entry.get("stopping_at"), resumed, current) >= review_rounds.recovery_wait_seconds():
                actions.append({**base, "action": "review_recovery_settle", "reason": f"the previous {'Reviewer' if reviewer else 'Delivery'} terminal would not close"})
        elif phase == "creating":
            made = sessions.get(str(entry.get("session_id") or ""))
            if made is not None:
                if made.get("status") == "launching" and not made.get("pid") and not made.get("attached_at"):
                    actions.append({**base, "action": "review_recovery_launch", "session_id": made["id"]})
                else:
                    actions.append({**base, "action": "review_recovery_mark_started", "session_id": made["id"]})
            elif _anchored_age(entry.get("creating_at"), resumed, current) >= RECOVERY_CREATE_GRACE_SECONDS:
                actions.append({**base, "action": "review_recovery_settle", "reason": "starting the fresh session was interrupted"})
        elif phase == "started":
            made = sessions.get(str(entry.get("session_id") or ""))
            registered = any(a.get("session_id") == entry.get("session_id") for a in (state.get("agents") or {}).values())
            if (made is None or made.get("status") not in control.ACTIVE_STATUSES) and not registered:
                actions.append({**base, "action": "review_recovery_settle", "reason": f"the fresh {'Reviewer' if reviewer else 'Delivery'} session did not start"})
            elif _anchored_age(entry.get("started_at"), resumed, current) >= review_rounds.recovery_wait_seconds():
                actions.append({**base, "action": "review_recovery_settle", "reason": "it produced no new review in the time allowed"})
    return actions


def _anchored_age(since: Any, resumed: datetime | None, current: datetime) -> float:
    """Seconds since `since`, counted from the project's last resume when later: time spent paused never runs the clock."""
    start = _parsed(since)
    if start is None:
        return 0.0
    if resumed is not None and resumed > start:
        start = resumed
    return max(0.0, (current - start).total_seconds())


def _retire_recovery_session(root: Path, key: str, at: str) -> dict[str, Any]:
    """A stopped recovery leaves no live terminal behind (managed stop, recorded once), unless that session still serves other parts of the task."""
    state = board.snapshot(root)
    entry = (state.get("review_escalations") or {}).get(key) or {}
    session_id = str(entry.get("session_id") or "")
    if entry.get("status") != "could_not_finish" or not session_id or entry.get("retired_at"):
        return {"action": "review_recovery_retire", "skipped": "nothing to retire"}
    if board.work_left_outside_stopped_scopes(state, str(entry.get("task") or "")) is not False:
        return {"action": "review_recovery_retire", "skipped": "the session still serves other parts of the task"}
    live = next((item for item in control.snapshot(root).get("sessions", []) if item.get("id") == session_id), None)
    if live and live.get("status") in control.ACTIVE_STATUSES:
        control.stop(root, session_id)
    board.review_recovery_mark_retired(root, key, at)
    return {"action": "review_recovery_retire", "review_key": key, "session_id": session_id}


def _execute_recovery(root: Path, action: dict[str, Any], launch: Callable[[dict[str, Any]], Any],
                      create: Callable[..., dict[str, Any]] | None) -> dict[str, Any]:
    key, name, at = action["review_key"], action["action"], action.get("at") or _now().isoformat()
    state = board.snapshot(root)
    entry = (state.get("review_escalations") or {}).get(key)
    if name == "review_recovery_retire":
        return _retire_recovery_session(root, key, at)
    if not entry or entry.get("status") != "recovery":
        return {"action": name, "skipped": "no longer in recovery"}
    if board.pause_state(root).get("status") != "active":
        return {"action": name, "skipped": "the project is not active"}
    if name == "review_recovery_settle":
        board.review_recovery_settle(root, key, action["reason"], closed=bool(action.get("closed")))
        _retire_recovery_session(root, key, at)       # the attempt that ran out of time (or never started) is stopped too
        return {"action": name, "review_key": key}
    if name == "review_recovery_stop":
        board.request_agent_replacement(root, action["agent_id"], "the review could not be completed; one fresh Reviewer follows" if entry.get("who") == "reviewer"
                                        else "the same part failed independent review repeatedly; one fresh attempt follows")
        stopped = control.stop(root, action["session_id"])
        board.review_recovery_update(root, key, phase="stopping", old_agent_id=action["agent_id"], old_session_id=action["session_id"],
                                     stopping_at=at, stop_mark=stopped.get("stop_requested_at") or "")
        return {"action": name, "review_key": key, "phase": "stopping"}
    if name == "review_recovery_launch":
        session = next((item for item in control.snapshot(root).get("sessions", []) if item.get("id") == action["session_id"]), None)
        if session and session.get("status") == "launching" and not session.get("pid") and not session.get("attached_at"):
            launch(session)
        board.review_recovery_update(root, key, phase="started", started_at=at)
        return {"action": name, "review_key": key, "phase": "started"}
    if name == "review_recovery_mark_started":
        board.review_recovery_update(root, key, phase="started", started_at=at)
        return {"action": name, "review_key": key, "phase": "started"}
    # review_recovery_create: the one attempt. Counted (phase "creating") BEFORE it acts; any failure settles it as could-not-finish.
    reviewer = entry.get("who") == "reviewer"
    old_agent = str(entry.get("old_agent_id") or "") or next((a["id"] for a in (state.get("agents") or {}).values()
                                                              if a.get("role") in board.DEVELOPER_ROLES and a.get("task") == entry["task"]), "")
    if not old_agent and not reviewer:
        board.review_recovery_settle(root, key, "there is no Delivery agent on this task to replace")
        return {"action": name, "review_key": key, "settled": True}
    board.review_recovery_update(root, key, phase="creating", creating_at=at)
    try:
        session = (create or (lambda value, continues="", recovery=False: control.create(root, value, fresh=recovery)))(
            "claude_reviewer" if reviewer else "codex_delivery", recovery=True)
        board.review_recovery_update(root, key, session_id=session["id"])
        if not reviewer:
            board.bind_replacement(root, session["id"], old_agent, entry["task"])
            try:
                control.enqueue_instruction(root, session["id"], board.request_recovery_wake_text(entry), source="review-recovery")
            except (OSError, ValueError):
                pass       # the brief is also on the board: the new agent's first poll carries it
        launch(session)
        board.review_recovery_update(root, key, phase="started", started_at=_now().isoformat(),
                                     model=f"{session.get('provider', '')}:{session.get('model', '')}:{session.get('effort', '')}")
    except Exception as error:
        board.review_recovery_settle(root, key, f"the fresh session could not be started: {str(error)[:160]}")
        raise
    return {"action": name, "review_key": key, "session_id": session["id"]}


# ---------------------------------------------------------------------------
# The stall ladder (2026-10-15): no stall ends silent, none needs a human.
# ---------------------------------------------------------------------------
def _role_words(agent: dict[str, Any]) -> str:
    role = agent.get("role")
    return "the CTO" if role == "cto" else "the Reviewer" if role == "qa" else "the Delivery Agent"


def _stall_key(agent: dict[str, Any]) -> str:
    return f"{agent.get('role')}:{agent.get('task')}"


def _stall_blocked(state: dict[str, Any], agent: dict[str, Any], session: dict[str, Any] | None, current: datetime) -> bool:
    """Reasons a silent agent is NOT a stall to climb on: the owner's own acts and honest waiting."""
    if session:
        if session.get("read_only") or session.get("superseded_by_session_id") or session.get("pause_requested_at"):
            return True
        if session.get("stop_requested_at") and not (agent.get("restart_requested_at") or agent.get("replacement_requested_at")):
            return True  # the owner stopped it
        if _needs_sign_in(session) or attention.needs_owner(session) or attention.holds_harness_input(session.get("attention_reason")):
            return True  # sign-in and trust menus are the owner's
        if _limit_held(session, current):
            return True  # a usage-limit message, and the grace after it clears: honest waiting, not a stall
        if session.get("launch_failure") and session.get("status") not in control.ACTIVE_STATUSES:
            return True  # the launch-failure card owns this
    if agent.get("liveness") in {board.SIGNED_OUT_LIVENESS, board.USAGE_LIMIT_LIVENESS} or agent.get("task") == board.AWAITING_OWNER_DIRECTION:
        return True
    if str(agent.get("status") or "") in {"awaiting_owner", "awaiting_owner_decision", "waiting_for_owner"}:
        return True
    if any(
        card.get("status") == "open" and card.get("kind") == "decision" and card.get("for_agent") == agent.get("id")
        for card in (state.get("owner_actions") or {}).values()
    ):
        return True
    execution = agent.get("review_execution") if isinstance(agent.get("review_execution"), dict) else {}
    started = _age(execution.get("started_at"), current)
    if board._review_execution_is_current(agent) and (started is None or started < STALL_LEASE_MAX_SECONDS):
        return True
    return board._review_authoring_lease_current(state, agent, current)


def _work_remains(state: dict[str, Any], role: str, task: str, agent_id: str) -> bool:
    """Whether the role still has work, for a ladder whose agent record has already left the hot board
    (a Reviewer or CTO that goes offline is archived at once)."""
    if role == "qa":
        if board._open_review_blocks(state):
            return True   # a blocked review waits on the Reviewer; the request is gone but the work is not
        return any(request.get("status") in {"authoring", "open", "reserved", "claimed"} for request in (state.get("qa_requests") or {}).values())
    return board._agent_has_actionable_work(state, {"role": role, "id": agent_id, "task": task})


def _stall_actions(state: dict[str, Any], control_state: dict[str, Any], agents_by_session: dict[str, dict[str, Any]],
                   planned: list[dict[str, Any]], current: datetime) -> list[dict[str, Any]]:
    """The ladder. Driven by the persistent ledger first (it survives the agent record, the old session and a
    worker restart), then by agents that have just gone silent."""
    window, step_wait = board.stall_settings()
    sessions = {str(item.get("id")): item for item in control_state.get("sessions", [])}
    ledger = state.get("stall_ladder") or {}
    resumed = _parsed((state.get("project_pause") or {}).get("resumed_at"))
    busy = {item.get("agent_id") for item in planned if item}
    by_key: dict[str, list[dict[str, Any]]] = {}
    for agent in (state.get("agents") or {}).values():
        if agent.get("role") in STALL_ROLES and (
                agent.get("active") or agent.get("restart_requested_at") or agent.get("replacement_requested_at")):
            by_key.setdefault(_stall_key(agent), []).append(agent)
    actions: list[dict[str, Any]] = []
    at = current.isoformat()
    for key in sorted(set(by_key) | set(ledger)):
        entry = ledger.get(key)
        candidates = by_key.get(key, [])
        agent = next((item for item in candidates if item.get("active")), None) or (candidates[0] if candidates else None)
        if agent is not None and (agent.get("id") in busy or _barred(state, agent)):
            continue
        role, _, task = key.partition(":")
        session = sessions.get(str((agent or {}).get("session_id") or (entry or {}).get("session_id") or ""))
        kind = str((session or {}).get("kind") or (entry or {}).get("kind") or "")
        base = {"stall_key": key, "self_heal_key": f"stall:{key}", "agent_id": (agent or {}).get("id") or (entry or {}).get("agent_id", ""),
                "kind": kind, "session_id": str((session or {}).get("id") or (entry or {}).get("session_id") or ""),
                "unconfirmed": bool((session or {}).get("exit_unconfirmed")), "at": at}
        if entry is None:
            # a fresh stall: an active agent with actionable work, a live terminal, and silence past the window
            if agent is None or not agent.get("active") or not session or session.get("status") not in control.ACTIVE_STATUSES:
                continue
            if _stall_blocked(state, agent, session, current) or not board._agent_has_actionable_work(state, agent):
                continue
            real = board.real_heartbeat(agent)
            beats = [real, _parsed(agent.get("spawned_at")), _parsed(agent.get("session_reattached_at")), resumed,
                     _parsed((session or {}).get("limit_cleared_at"))]
            beat = max((item for item in beats if item is not None), default=None)
            threshold = window + (board.CTO_MONITOR_INTERVAL_SECONDS if role == "cto" else 0)
            if beat is None or (current - beat).total_seconds() < threshold:
                continue
            woken = _parsed(agent.get("automatic_recovery_requested_at"))
            actions.append({**base, "action": "stall_wake", "silence": int((current - beat).total_seconds()),
                            "since": real.isoformat() if real else "",
                            "already_woken_at": woken.isoformat() if woken and woken > beat else ""})
            continue
        # ---- a ladder is in progress ----
        since = _parsed(entry.get("since"))
        real = board.real_heartbeat(agent) if agent else None
        if real is not None and (since is None or real > since):
            actions.append({"action": "stall_reset", **base})   # verified recovery
            continue
        remains = board._agent_has_actionable_work(state, agent) if agent else _work_remains(state, role, task, base["agent_id"])
        if not remains or task in (state.get("cancelled_tasks") or {}):
            actions.append({"action": "stall_reset", **base})   # nothing left to recover
            continue
        if entry.get("card"):
            continue
        if agent is not None and _stall_blocked(state, agent, session, current):
            continue
        if session is not None and (session.get("pause_requested_at") or (
                session.get("stop_requested_at") and session.get("stop_requested_at") not in (entry.get("stop_marks") or []))):
            continue   # a stop the ladder did not issue is the owner's
        step = int(entry.get("step") or 1)
        active_session = bool(session) and session.get("status") in control.ACTIVE_STATUSES
        # phase two of a rung, which must happen promptly rather than after a wait
        if step == 2 and entry.get("relaunch_pending") and session and not active_session:
            if not (agent is not None and agent.get("restart_requested_at")):
                actions.append(_limited(state, f"relaunch:{key}", current, {**base, "action": "stall_relaunch_resume"}))
            continue   # with the marker still set, the ordinary phase two relaunches it
        if step == 3 and entry.get("replace_pending") and not entry.get("replace_launched") and session and not active_session:
            if not session.get("exit_unconfirmed") and not any(
                    other.get("continues_session") == session.get("id") and other.get("status") in control.ACTIVE_STATUSES
                    for other in control_state.get("sessions", [])):
                actions.append(_limited(state, f"replace:{key}", current, {**base, "action": "stall_replace_launch"}))
                continue
        reference = [_parsed(entry.get("step_at")), _parsed((agent or {}).get("session_reattached_at")),
                     _parsed((agent or {}).get("spawned_at")) if step >= 2 else None, resumed,
                     _parsed((session or {}).get("limit_cleared_at"))]    # time spent held never advances a step
        anchor = max((item for item in reference if item is not None), default=None)
        if anchor is None or (current - anchor).total_seconds() < step_wait:
            continue
        actions.append({**base, "action": ("stall_relaunch", "stall_replace", "stall_card")[min(step, 3) - 1],
                        "silence": int((current - (real or anchor)).total_seconds())})
    return [item for item in actions if item]


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


def _skip_if_held_now(root: Path, action: dict[str, Any]) -> str:
    """Re-read the world right before any destructive or replacement action: a plan made a tick ago must not run against a
    terminal that has just shown its usage-limit message, is in the grace after it cleared, or whose project is no longer active.
    Returns the plain reason to skip, or an empty string."""
    name = str(action.get("action", ""))
    acts_on_terminal = name in {"restart", "relaunch", "relaunch_ended"} or (name.startswith("stall_") and name != "stall_reset")
    # The one fresh Reviewer of a review that could not be completed obeys the same sign-in, consent and usage-limit holds as any Reviewer start.
    starts_reviewer = name == "start_reviewer" or (name == "review_recovery_create" and action.get("who") == "reviewer")
    if not (acts_on_terminal or starts_reviewer):
        return ""
    if acts_on_terminal and action.get("agent_id"):
        # Re-checked immediately before acting: a plan made a tick ago must not bring back what has just been stopped for good.
        current_state = board.snapshot(root)
        target_agent = (current_state.get("agents") or {}).get(action["agent_id"])
        if target_agent and _barred(current_state, target_agent):
            return "skipped: the harness stopped repairing the part this agent was working on, so it is not brought back"
    try:
        if board.pause_state(root).get("status") != "active":
            return "skipped: the project is not active"
    except (OSError, ValueError, KeyError):
        return ""
    snapshot = control.snapshot(root)
    sessions = snapshot.get("sessions", [])
    if acts_on_terminal and action.get("session_id"):
        target = next((item for item in sessions if item.get("id") == action.get("session_id")), None)
        if target is not None and _limit_held(target):
            return "skipped: the terminal shows a usage-limit message, so it is waiting, not stuck"
    if starts_reviewer:
        target_provider = str(((snapshot.get("agent_settings") or {}).get("reviewer") or {}).get("provider") or "")
    elif acts_on_terminal and action.get("session_id"):
        target = next((item for item in sessions if item.get("id") == action.get("session_id")), None) or {}
        target_provider = str(target.get("provider") or ("claude" if str(target.get("kind", action.get("kind", ""))).startswith("claude") else ""))
    else:
        target_provider = ""
    if target_provider == "claude":
        # A Claude launch the owner has not consented to stays refused: another attempt cannot succeed and would only repeat.
        # Other providers are unaffected by a Claude consent card.
        if any((item.get("claude_auth_action") or {}).get("reason") == "consent" for item in sessions):
            return "skipped: Claude agents are waiting for YOU to accept the one-time bypass warning, so another launch would be refused again"
    if starts_reviewer:
        settings = snapshot.get("agent_settings") or {}
        reviewer_provider = str((settings.get("reviewer") or {}).get("provider") or "")
        for item in sessions:
            if control.KIND_TO_ROLE.get(str(item.get("kind", ""))) == "reviewer" and _limit_held(item) and (
                    not reviewer_provider or not item.get("provider") or item.get("provider") == reviewer_provider):
                return "skipped: the Reviewer shows a usage-limit message, so a second one would hit the same limit"
    return ""


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
    if action["action"] == "announce_launch_failure":
        # A plan made before a successful resume must not publish an obsolete failure: look again at the session.
        current = next((item for item in control.snapshot(root).get("sessions", []) if item.get("id") == action.get("session_id")), {})
        if (str((current.get("launch_failure") or {}).get("at") or "") != action.get("attempt")
                or current.get("status") in control.ACTIVE_STATUSES
                or current.get("stop_requested_at") or current.get("pause_requested_at")):
            return {"action": "announce_launch_failure", "skipped": "the session has moved on"}
        # The card and its once-only key are one board transaction: a crash cannot lose or duplicate it.
        with board.locked_state(root) as state:
            seen = state.setdefault("self_heal", {}).setdefault("announced", {})
            if key in seen:
                return {"action": "announce_launch_failure", "duplicate": True}
            seen[key] = _now().isoformat()
            for stale in sorted(seen, key=seen.get)[:-200]:
                del seen[stale]
            agent = (state.get("agents") or {}).get(action.get("agent_id", ""))
            board._event(state, "self_heal_failed", agent, {
                "task": (agent or {}).get("task", ""), "self_heal_key": key,
                "session_id": action.get("session_id", ""), "message": str(action["reason"])[:700],
            })
        return {"action": "announce_launch_failure", "session_id": action.get("session_id", "")}
    if action["action"] == "give_up":
        with board.locked_state(root) as state:
            state.setdefault("self_heal", {}).setdefault("gave_up", {})[key] = _now().isoformat()
        _record(root, "self_heal_gave_up", key,
                f"The harness tried to bring back {who} {MAX_ACTIONS_PER_HOUR} times in the last hour and it is still "
                f"stuck ({action['reason']}). Please open its terminal to see what it needs.",
                agent_id=action.get("agent_id", ""))
        return {"action": "give_up", "self_heal_key": key}
    skipped = _skip_if_held_now(root, action)
    if skipped:
        try:
            _record(root, "self_heal_skipped", key, skipped, agent_id=action.get("agent_id", ""), session_id=action.get("session_id", ""))
        except board.ProjectPausedError:
            pass       # a paused project refuses every write, this note included; nothing was done, which is the point
        return {"action": action["action"], "skipped": skipped}
    if str(action["action"]).startswith("review_recovery_"):
        return _execute_recovery(root, action, launch, create)
    if str(action["action"]).startswith("stall_"):
        return _execute_stall(root, action, launch, create)
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


def _minutes(seconds: Any) -> str:
    minutes = max(int(seconds or 0) // 60, 1)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _ladder_event(root: Path, action: dict[str, Any], step: str, message: str, **extra: Any) -> None:
    _record(root, "agent_stall_step", action["self_heal_key"], message, agent_id=action.get("agent_id", ""),
            step=step, role_kind=action.get("kind", ""), **extra)


def _mark_stop(root: Path, key: str, stopped: dict[str, Any]) -> None:
    """Remember a stop as the ladder's own, so it is never mistaken for the owner stopping the terminal."""
    mark = stopped.get("stop_requested_at")
    if mark:
        with board.locked_state(root) as state:
            entry = (state.get("stall_ladder") or {}).get(key)
            if entry is not None:
                entry["stop_marks"] = list(entry.get("stop_marks") or []) + [mark]


def _set_ladder(root: Path, key: str, **fields: Any) -> dict[str, Any]:
    with board.locked_state(root) as state:
        entry = state.setdefault("stall_ladder", {}).setdefault(key, {})
        entry.update(fields)
        return dict(entry)


def _still_stalled(root: Path, action: dict[str, Any]) -> bool:
    """Look again at execution time (plan check finding 9): the project may have been paused, the owner may
    have stopped the terminal, or the agent may have polled since the plan was made."""
    if board.pause_state(root).get("status") != "active":
        return False
    state = board.snapshot(root)
    agent = (state.get("agents") or {}).get(action.get("agent_id", ""))
    entry = (state.get("stall_ladder") or {}).get(action["stall_key"])
    if not entry or entry.get("card"):
        return False
    if not agent:
        return True   # the record has left the hot board (a Reviewer or CTO that went offline); the ledger carries on
    real, since = board.real_heartbeat(agent), _parsed(entry.get("since"))
    if real is not None and (since is None or real > since):
        return False
    session = next((item for item in control.snapshot(root).get("sessions", []) if item.get("id") == action.get("session_id")), None)
    return not _stall_blocked(state, agent, session, _parsed(action.get("at")) or _now())


def _execute_stall(root: Path, action: dict[str, Any], launch: Callable[[dict[str, Any]], Any],
                   create: Callable[[str], dict[str, Any]] | None) -> dict[str, Any]:
    """One rung of the stall ladder. The rung is written to the ledger BEFORE it acts,
    so a failing rung can never be repeated every tick."""
    kind, name, key = action.get("kind", ""), action["action"], action["stall_key"]
    at = action.get("at") or _now().isoformat()
    who = ROLE_BY_KIND.get(kind, "the agent")
    role_name, _, key_task = key.partition(":")
    with board.locked_state(root) as state:
        agent = (state.get("agents") or {}).get(action.get("agent_id", "")) or {}
        task = str(agent.get("task") or key_task)
        agent_words = _role_words({"role": agent.get("role") or role_name})
    if name == "stall_reset":
        with board.locked_state(root) as state:
            gone = (state.get("stall_ladder") or {}).pop(key, None) or {}
            if gone.get("card"):
                # it answered after the card: the card goes, the agent is no longer flagged
                for item in (state.get("agents") or {}).values():
                    if _stall_key(item) == key and item.pop("stall_exhausted", None):
                        for card in list((state.get("owner_actions") or {}).values()):
                            if card.get("status") == "open" and card.get("for_agent") == item.get("id") \
                                    and "could not be brought back" in str(card.get("title")):
                                board._resolve_owner_action(state, card, "Resolved: the agent answered", "stall_reset")
        return {"action": name, "stall_key": key}
    entry = ((board.snapshot(root).get("stall_ladder") or {}).get(key)) or {}
    if name in {"stall_relaunch", "stall_replace"} and not _still_stalled(root, action):
        return {"action": name, "stall_key": key, "skipped": "it is no longer stalled or no longer ours to touch"}
    if name == "stall_wake":
        silence = int(action.get("silence") or 0)
        tried = "it had already been woken once and did not answer" if action.get("already_woken_at") else "the harness woke it with a plain instruction to poll the board and continue"
        _set_ladder(root, key, since=action.get("since", ""), started_at=at, step=1, agent_id=action["agent_id"], kind=kind,
                    session_id=action.get("session_id", ""), role=role_name, task=task,
                    step_at=action.get("already_woken_at") or at, card=False,
                    steps=["woke it" if not action.get("already_woken_at") else "woke it (it did not answer)"], silence=silence)
        delivered = bool(action.get("already_woken_at"))
        if not delivered:
            with board.locked_state(root) as state:
                woken_agent = dict((state.get("agents") or {}).get(action["agent_id"]) or {})
            text = (board._automatic_recovery_instruction(woken_agent) if woken_agent.get("role") == "cto" else
                    f"STALL RECOVERY: you have not checked the board for {_minutes(silence)}. Stop what you are doing, run your "
                    f"board poll command now, then continue {task} from its saved next gate. Do not wait for the owner. "
                    "USER ACTION: None.")
            try:
                control.enqueue_instruction(root, action["session_id"], text, source="stall-ladder")
            except (OSError, ValueError) as error:
                tried = f"the wake-up could not be delivered ({str(error)[:120]})"
        _ladder_event(root, action, "wake",
                      f"{agent_words[0].upper() + agent_words[1:]} has not checked the board for {_minutes(silence)}. Step 1 of 3: {tried}. "
                      "The CTO does not need to do its work and the owner does not need to act.",
                      task=task, silence_seconds=silence)
        return {"action": name, "stall_key": key, "step": "wake"}
    steps = list(entry.get("steps") or [])
    if name == "stall_relaunch_resume":
        try:
            outcome = _relaunch(root, action["session_id"], launch)
        except Exception as error:
            _note_attempt(root, f"relaunch:{key}")
            _record(root, "self_heal_failed", action["self_heal_key"],
                    f"The harness tried to restart {agent_words} but could not: {str(error)[:160]}", agent_id=action.get("agent_id", ""))
            raise
        _set_ladder(root, key, relaunch_pending=False, step_at=at)
        return outcome
    if name == "stall_relaunch":
        capped = _recent_attempts(board.snapshot(root), f"relaunch:{key}", _now()) >= MAX_ACTIONS_PER_HOUR
        _set_ladder(root, key, step=2, step_at=at, relaunch_pending=not capped,
                    steps=steps + (["relaunch skipped (tried 3 times this hour)"] if capped else ["restarted its terminal"]))
        if capped:
            _ladder_event(root, action, "relaunch", f"Step 2 of 3 skipped for {agent_words}: its terminal was already restarted {MAX_ACTIONS_PER_HOUR} times this hour; going to replacement.", task=task)
            _set_ladder(root, key, step=2, step_at=(_parsed(at) - timedelta(days=1)).isoformat())
            return {"action": name, "stall_key": key, "skipped": "capped"}
        _note_attempt(root, f"relaunch:{key}")
        _note_attempt(root, f"agent:{action['agent_id']}")
        reason = f"it sent no board heartbeat for {_minutes(action.get('silence'))} and did not answer a wake-up"
        _ladder_event(root, action, "relaunch",
                      f"Still no answer from {agent_words}. Step 2 of 3: the harness is restarting its terminal from its saved "
                      "conversation (same model and effort); its task and memory are kept.", task=task)
        try:
            if agent:
                board.request_agent_restart(root, action["agent_id"], reason)
            live = next((item for item in control.snapshot(root).get("sessions", []) if item.get("id") == action["session_id"]), {})
            if live.get("status") in control.ACTIVE_STATUSES:
                _mark_stop(root, key, control.stop(root, action["session_id"]))
                return {"action": name, "stall_key": key, "phase": "stopping"}
            return _relaunch(root, action["session_id"], launch)
        except Exception as error:
            _record(root, "self_heal_failed", action["self_heal_key"],
                    f"The harness tried to restart {agent_words} but could not: {str(error)[:160]}", agent_id=action.get("agent_id", ""))
            raise
    if name == "stall_replace":
        capped = _recent_attempts(board.snapshot(root), f"replace:{key}", _now()) >= MAX_ACTIONS_PER_HOUR
        if capped:
            _set_ladder(root, key, step=3, step_at=(_parsed(at) - timedelta(days=1)).isoformat(),
                        steps=steps + ["replacement skipped (tried 3 times this hour)"])
            return {"action": name, "stall_key": key, "skipped": "capped"}
        _note_attempt(root, f"replace:{key}")
        _set_ladder(root, key, step=3, step_at=at, steps=steps + ["replaced its terminal"], replace_pending=True,
                    relaunch_pending=False, replace_launched=False)
        _ladder_event(root, action, "replace",
                      f"Still no answer from {agent_words} after the restart. Step 3 of 3: the harness is replacing its terminal with a "
                      "new one that continues the saved conversation and carries the same task.", task=task)
        try:
            if agent:
                board.request_agent_replacement(root, action["agent_id"], "it stayed silent after a wake-up and a restart")
            live = next((item for item in control.snapshot(root).get("sessions", []) if item.get("id") == action["session_id"]), {})
            if live.get("status") in control.ACTIVE_STATUSES:
                _mark_stop(root, key, control.stop(root, action["session_id"]))
            return {"action": name, "stall_key": key, "phase": "stopping"}
        except Exception as error:
            _record(root, "self_heal_failed", action["self_heal_key"],
                    f"The harness tried to replace {agent_words} but could not: {str(error)[:160]}", agent_id=action.get("agent_id", ""))
            raise
    if name == "stall_replace_launch":
        try:
            session = (create or (lambda value, continues="": control.create(root, value, continues=continues)))(
                kind, continues=action["session_id"])
            if role_name in board.DEVELOPER_ROLES and task and action.get("agent_id"):
                board.bind_replacement(root, session["id"], action["agent_id"], task)
            launch(session)
        except Exception as error:
            _note_attempt(root, f"replace:{key}")
            _record(root, "self_heal_failed", action["self_heal_key"],
                    f"The harness tried to open a replacement for {agent_words} but could not: {str(error)[:160]}",
                    agent_id=action.get("agent_id", ""))
            raise
        with board.locked_state(root) as state:
            old = (state.get("agents") or {}).get(action["agent_id"])
            if old is not None:
                old["replacement_launched_at"] = at
        _set_ladder(root, key, step=3, step_at=at, replace_launched=True, replace_pending=False)
        kept = session.get("continues_session") == action["session_id"]
        _ladder_event(root, action, "replace",
                      f"A new terminal for {agent_words} was opened; it continues the saved conversation and the same task." if kept else
                      f"A new terminal for {agent_words} was opened with the same task; the provider kept no saved conversation, so it starts "
                      "fresh from the board's saved task memory.", task=task, session_id=session["id"], conversation_kept=kept)
        return {"action": name, "stall_key": key, "session_id": session["id"]}
    if name == "stall_card":
        with board.locked_state(root) as state:
            ledger = state.setdefault("stall_ladder", {})
            current_entry = ledger.get(key) or {}
            if current_entry.get("card"):
                return {"action": name, "duplicate": True}
            current_entry["card"] = True
            ledger[key] = current_entry
            old = (state.get("agents") or {}).get(action.get("agent_id", "")) or (
                {"id": action.get("agent_id") or current_entry.get("agent_id", ""), "role": role_name, "task": task} if role_name else None)
            word = _role_words(old) if old else who
            state.setdefault("self_heal", {}).setdefault("gave_up", {})[action["self_heal_key"]] = at
            tried = ", ".join(current_entry.get("steps") or ["woke it"])
            started = _parsed(current_entry.get("started_at"))
            since_word = f"for about {_minutes((_parsed(at) - started).total_seconds() + int(current_entry.get('silence') or 0))}" if started else "for several minutes"
            tried_words = f"it {tried}"
            if action.get("unconfirmed"):
                tried_words += "; the old terminal could not be confirmed closed, so no replacement was opened (check this machine for a leftover terminal)"
            message = (f"{word[0].upper() + word[1:]} has been silent {since_word} and the harness could not bring it back "
                       f"({tried_words}). Please open its terminal to see what it needs, or tell the CTO what to do. "
                       "Nothing else is stuck behind this card.")
            board._event(state, "self_heal_gave_up", old, {
                "task": (old or {}).get("task", ""), "self_heal_key": action["self_heal_key"], "message": message,
            })
            if old is not None:
                if action.get("agent_id") in (state.get("agents") or {}):
                    old["stall_exhausted"] = True  # the viewer stops calling busy output "owner action none"
                # The persistent surface (plan check finding 5): the event line scrolls away, the card stays
                # until the agent restarts or answers. It names who, since when, what failed, what the owner can do.
                board._record_owner_action(
                    state, old, f"{word[0].upper() + word[1:]} stopped answering and could not be brought back"[:240], "",
                    message[:800], str(old.get("task") or ""), kind="agent", for_agent=str(old.get("id")))
        return {"action": name, "stall_key": key}
    raise ValueError(f"unknown stall action {name!r}")


def _relaunch(root: Path, session_id: str, launch: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    staged = control.prepare_resume_sessions(root, [session_id])
    if not staged or staged[0].get("action") not in {"relaunch", "awaiting_attachment"}:
        return {"action": "relaunch", "session_id": session_id, "skipped": (staged or [{}])[0].get("action", "missing")}
    if staged[0].get("action") == "relaunch":
        session = control.mark_resume_launch_requested(root, session_id)
        launch(session)
    return {"action": "relaunch", "session_id": session_id, "phase": "launched"}


def run_once(root: Path, launch: Callable[[dict[str, Any]], Any], current: datetime | None = None,
             create: Callable[..., dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One self-heal pass: plan, then act on each decision; one failure never blocks the rest."""
    results = []
    try:
        claude_auth.settle_consent_cards(root)
    except Exception:  # a card that cannot be checked stays; it never stops the watchdog
        pass
    for action in plan(root, current):
        try:
            results.append(execute(root, action, launch, create))
        except Exception as error:  # one failure never stops the watchdog; it is already counted and recorded
            results.append({"action": action.get("action"), "error": str(error)[:300]})
    return results

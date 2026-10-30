"""When the CTO is woken, why, and what is still waiting for its judgment (2026-10-26_lighter-cto).

The board used to wake the CTO on a clock (every 120 or 300 seconds while any task was live), and every wake was a paid
agent turn that mostly found nothing. The CTO now stays a standing agent but is woken only for a reason, through ONE
path: every producer (final pass, completion, a prepared release, a finding, a stuck item, a resume or launch, the slow
heartbeat) opens or merges a pending-judgment item and the wake names its reason. Every wake is counted, so before and
after use the same instrument.

Everything here works on the board state passed in (the caller holds the board lock); nothing in this module reads a
file or talks to a terminal. State keys: ``cto_wakes`` (counters) and ``cto_pending`` (the judgment items).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from harness import analysis_pass

# The reasons that open a pending-judgment item, in the order a wake names them.
REASONS = ("finding", "release", "analysis_merge", "owner_summary", "disputed", "stuck", "no_progress", "resume", "heartbeat")
# Counted but not judged: the board's own recovery of a dead or silent CTO, and the owner talking to it.
OTHER_SOURCES = ("recovery", "owner")
OUTCOMES = ("hold", "note", "route", "nothing")
PLAIN = {
    "finding": "a new finding", "release": "a release step", "analysis_merge": "an analysis merge", "owner_summary": "an owner card",
    "disputed": "a disputed finding", "stuck": "a stuck item", "no_progress": "a task with no progress", "resume": "a resume or launch", "heartbeat": "the slow check-in",
}
# Events that open an item by themselves. A Delivery or Reviewer stall is not here: the stall ladder owns it until it gives up.
EVENT_REASONS = {
    "finding_classified": "finding",
    "control_plane_incident": "release",
    "self_heal_gave_up": "stuck", "repeated_refusal_defect": "stuck", "review_needs_environment": "stuck",
    "staged_review_cancelled": "stuck", "challenge_execution_failed": "stuck", "agent_recovery_failed": "stuck",
    "git_acceptance_failed": "stuck", "git_acceptance_reintegration_required": "stuck", "remote_push_failed": "stuck",
    "qa_pass_invalidated": "stuck", "ready_claim_without_review": "stuck",
}
# The slow check-in (Settings, plumbing section: cto_heartbeat_seconds): a backstop under stalls no rule names, never switched off.
# Default 24 hours (2026-10-28, owner: the CTO is not a paid timer); the board's own free checks below do the watching.
HEARTBEAT_DEFAULT_SECONDS, HEARTBEAT_LOW_SECONDS, HEARTBEAT_HIGH_SECONDS = 86_400, 60, 86_400
# A live task whose agents have recorded no progress for this long wakes the CTO once (Settings: cto_no_progress_seconds).
NO_PROGRESS_DEFAULT_SECONDS, NO_PROGRESS_LOW_SECONDS, NO_PROGRESS_HIGH_SECONDS = 1800, 300, 86_400
_configured: dict[str, int] = {}
CLOSED_KEPT = 40
LISTED_ITEMS = 6
DETAIL_CHARS = 140
TEXT_CHARS = 1800


def configure(plumbing: Any) -> None:
    """Install the owner's check-in interval (the project worker passes the settings once at start); anything unusable keeps the default."""
    _configured.clear()
    if not isinstance(plumbing, dict):
        return
    value = plumbing.get("cto_heartbeat_seconds")
    if isinstance(value, int) and not isinstance(value, bool) and HEARTBEAT_LOW_SECONDS <= value <= HEARTBEAT_HIGH_SECONDS:
        _configured["seconds"] = value
    value = plumbing.get("cto_no_progress_seconds")
    if isinstance(value, int) and not isinstance(value, bool) and NO_PROGRESS_LOW_SECONDS <= value <= NO_PROGRESS_HIGH_SECONDS:
        _configured["no_progress"] = value


def heartbeat_seconds() -> int:
    return _configured.get("seconds", HEARTBEAT_DEFAULT_SECONDS)


def no_progress_seconds() -> int:
    return _configured.get("no_progress", NO_PROGRESS_DEFAULT_SECONDS)


def _parsed(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---- counters (the instrument) ------------------------------------------------------------------------------------------

def counters(state: dict[str, Any]) -> dict[str, Any]:
    found = state.setdefault("cto_wakes", {})
    found.setdefault("total", 0)
    found.setdefault("by_reason", {})
    found.setdefault("by_task", {})
    found.setdefault("polls", 0)
    found.setdefault("posts", 0)
    return found


def count_wake(state: dict[str, Any], reason: str, task: str = "") -> None:
    """One agent turn bought: a wake of the CTO, by reason (or by the other source that caused it)."""
    found = counters(state)
    found["total"] = int(found["total"]) + 1
    found["by_reason"][reason] = int(found["by_reason"].get(reason, 0)) + 1
    if task:
        per_task = found["by_task"].setdefault(task, {})
        per_task[reason] = int(per_task.get(reason, 0)) + 1


def count_poll(state: dict[str, Any]) -> None:
    found = counters(state)
    found["polls"] = int(found["polls"]) + 1


def count_post(state: dict[str, Any]) -> None:
    found = counters(state)
    found["posts"] = int(found["posts"]) + 1


def usage_sentence(state: dict[str, Any], task: str) -> str:
    """The plain 'CTO wakes' sentence for a task's usage line; empty when the CTO was never woken for it."""
    per_task = (state.get("cto_wakes") or {}).get("by_task", {}).get(task) or {}
    total = sum(int(v or 0) for v in per_task.values())
    if not total:
        return ""
    detail = ", ".join(f"{name.replace('_', ' ')} {int(count)}" for name, count in sorted(per_task.items()))
    return f" CTO wakes: {total} ({detail})."


# ---- pending judgments -----------------------------------------------------------------------------------------------

def _items(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return state.setdefault("cto_pending", {})


def has_cto(state: dict[str, Any]) -> bool:
    return any(a.get("role") == "cto" and a.get("active") for a in (state.get("agents") or {}).values())


def open_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted((i for i in (state.get("cto_pending") or {}).values() if i.get("status") == "open"),
                  key=lambda i: (str(i.get("opened_at")), str(i.get("id"))))


def urgent_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in open_items(state) if i.get("wake_pending")]


def request(state: dict[str, Any], reason: str, subject: str, task: str = "", detail: str = "",
            pending: bool = True, once: bool = False, at: str = "") -> tuple[dict[str, Any] | None, bool]:
    """Open a judgment item for (reason, subject), or merge into the open one. Returns (item, is_new).

    A repeat of the same reason and subject never opens a second item or a second wake; the next wake names it again.
    ``once`` also refuses to reopen an item the CTO already judged (a resume or launch is one wake)."""
    if reason not in REASONS or not has_cto(state):
        return None, False
    items = _items(state)
    detail = " ".join(str(detail or "").split())[:DETAIL_CHARS]
    for item in items.values():
        if item.get("reason") == reason and item.get("subject") == subject and (item.get("status") == "open" or once):
            if item.get("status") == "open":
                item["repeats"] = int(item.get("repeats", 0)) + 1
                if detail:
                    item["detail"] = detail
            return item, False
    item = {
        "id": f"judge-{secrets.token_hex(4)}", "reason": reason, "subject": subject, "task": task, "detail": detail,
        "opened_at": at or datetime.now(timezone.utc).isoformat(), "status": "open", "wake_pending": bool(pending),
        "wakes": 0, "repeats": 0,
    }
    items[item["id"]] = item
    closed = sorted((i for i in items.values() if i.get("status") != "open"), key=lambda i: str(i.get("closed_at")))
    for old in closed[:max(0, len(closed) - CLOSED_KEPT)]:
        items.pop(old["id"], None)
    return item, True


def from_event(state: dict[str, Any], event: dict[str, Any], agent: dict[str, Any] | None) -> None:
    """The one place an ordinary board event turns into a wake request."""
    kind = str(event.get("kind") or "")
    reason = EVENT_REASONS.get(kind, "")
    task = str(event.get("task") or (agent or {}).get("task") or "")
    if kind == "owner_action_recorded" and (agent or {}).get("role") != "cto":
        # a card the board itself pins for the owner: the CTO words it for the owner (rules 3, 27)
        request(state, "owner_summary", str(event.get("action_id") or event.get("title") or task), task, str(event.get("title") or ""))
        return
    if not reason:
        return
    if kind == "finding_classified":
        finding = (state.get("deferred_findings") or {}).get(str(event.get("finding_id") or "")) or {}
        subject, detail = str(event.get("finding_id") or ""), str(finding.get("title") or event.get("message") or "")
    else:
        subject, detail = f"{kind}:{task}", str(event.get("message") or kind)
    request(state, reason, subject, task, detail)


def review_failed(state: dict[str, Any], request_row: dict[str, Any], escalation: dict[str, Any]) -> None:
    """A Reviewer FAIL is a finding; the same part failing again after Delivery's repair is a disputed finding."""
    task = str(request_row.get("task") or "")
    repeated = int((escalation or {}).get("failed_judgments") or 0) >= 2
    request(state, "disputed" if repeated else "finding", f"review:{request_row.get('id', '')}", task,
            str(request_row.get("result_summary") or "the independent review failed"))


def dispose(state: dict[str, Any], item_id: str, outcome: str, note: str, at: str) -> list[dict[str, Any]]:
    """The CTO's disposition closes an item (or every open one for 'all'). A poll or a status post never does."""
    if outcome not in OUTCOMES:
        raise ValueError("the outcome must be one of: " + ", ".join(OUTCOMES))
    note = " ".join(str(note or "").split())
    if not note or len(note) > 600:
        raise ValueError("a disposition needs a one-line note of at most 600 characters")
    chosen = open_items(state) if item_id == "all" else [i for i in open_items(state) if i["id"] == item_id]
    if not chosen:
        if item_id != "all" and (state.get("cto_pending") or {}).get(item_id, {}).get("status") == "closed":
            return []
        raise ValueError("no open judgment item with that id; see the list in the wake message")
    for item in chosen:
        item.update({"status": "closed", "wake_pending": False, "outcome": outcome, "disposition": note[:600], "closed_at": at})
    return chosen


# ---- the wake ---------------------------------------------------------------------------------------------------------

def _no_progress(state: dict[str, Any], current: datetime) -> None:
    """Open one item per task that has made no progress: free, from board facts only (2026-10-28_cto-pathfinder).

    Progress is a progress record, a status post or a spawn by an agent on the task, never a poll (a polling retry loop is
    exactly what this catches). A paused project, a cancelled, accepted or released task (the owner's visual acceptance is the gate), a task
    waiting for the owner and a task with an open review (its Reviewer is the one working) are not stalled; only Delivery agents count. One item per stall episode: the stamp is part of the subject.

    A Code analysis in its two-pass phases (spec 2026-10-29_code-analysis-two-pass) is counted per side instead: only the side that owes the next step
    (its list not closed, or its cross-check not in) is watched, by its own analysis-list / cross-check actions and status posts; a side that is only
    waiting is left out, so a waiting Delivery's status posts never hide a silent Reviewer. The item is `no_progress:<task>:<side>:<stamp>`."""
    if (state.get("project_pause") or {}).get("status", "active") != "active":
        return
    from harness import board        # local: board imports this module
    reviewing = {r.get("task") for r in (state.get("qa_requests") or {}).values()
                 if r.get("status") in {"authoring", "open", "reserved", "claimed"}}
    waiting = {p.get("task") for p in (state.get("requirement_proposals") or {}).values() if p.get("status") == "awaiting_owner"}
    last: dict[str, datetime] = {}
    for agent in (state.get("agents") or {}).values():
        task = str(agent.get("task") or "")
        if not agent.get("active") or agent.get("role") not in board.DEVELOPER_ROLES or not task or task in board._CROSS_TASK_DESIGNATIONS:
            continue                  # only a Delivery agent on a real task is task work; an idle Reviewer or the CTO is not
        stamps = [m for m in (_parsed(agent.get(k)) for k in ("last_progress_at", "last_status_at", "spawned_at")) if m]
        if stamps:
            last[task] = max([last.get(task), *stamps], key=lambda m: m or datetime.min.replace(tzinfo=timezone.utc))
    passes = state.get("analysis_passes") or {}
    for task, record in sorted(passes.items()):
        if analysis_pass.stage(record) not in {"lists", "crosscheck", "merge"} or board._task_closed_for_checks(state, task):
            continue
        for side in analysis_pass.owing(record):
            who = next((a for a in (state.get("agents") or {}).values() if a.get("active") and (
                (a.get("role") in board.DEVELOPER_ROLES and a.get("task") == task) if side == "delivery" else a.get("id") == record.get("reviewer_id"))), None) or {}
            stamps = [m for m in (_parsed(v) for v in (record["sides"][side]["last_at"], who.get("last_progress_at"), who.get("last_status_at"))) if m]
            moment = max(stamps) if stamps else None
            if moment and (current - moment).total_seconds() >= no_progress_seconds():
                request(state, "no_progress", f"no_progress:{task}:{side}:{moment.isoformat()}", task,
                        f"no progress from the {analysis_pass.SIDE_WORD[side]} on the {task} analysis for {int((current - moment).total_seconds() // 60)} minutes (polling does not count)",
                        at=current.isoformat(), once=True)
    for task, moment in sorted(last.items()):
        if analysis_pass.stage(passes.get(task)) in {"lists", "crosscheck", "merge"}:
            continue                  # counted per side above
        age = (current - moment).total_seconds()
        if board._task_closed_for_checks(state, task) or task in reviewing or task in waiting or age < no_progress_seconds():
            continue
        request(state, "no_progress", f"no_progress:{task}:{moment.isoformat()}", task,
                f"no progress recorded on {task} for {int(age // 60)} minutes (polling does not count)", at=current.isoformat(), once=True)


def plan(state: dict[str, Any], agent: dict[str, Any], current: datetime, actionable: bool) -> list[dict[str, Any]]:
    """The items to wake the CTO for now (opens the resume, launch and heartbeat ones). Empty means stay quiet."""
    found = counters(state)
    for task, record in sorted((state.get("analysis_passes") or {}).items()):
        if analysis_pass.stage(record) == "merge":      # both cross-checks are in and no clean list yet: the merge is owed, whenever a CTO exists
            request(state, "analysis_merge", f"analysis_merge:{task}", task, "both analysis lists are checked: group the duplicates", once=True, at=current.isoformat())
    if actionable:
        pause = state.get("project_pause") or {}
        if pause.get("resumed_at"):
            request(state, "resume", f"resume:{pause['resumed_at']}", "", "the project was resumed: re-triage what was inherited",
                    at=current.isoformat(), once=True)
        request(state, "resume", f"launch:{agent.get('id')}", "",
                "this CTO is new: re-triage inherited owner cards and findings", at=current.isoformat(), once=True)
        _no_progress(state, current)
        last = _parsed(found.get("last_heartbeat_at"))
        if last is None:
            found["last_heartbeat_at"] = current.isoformat()
        elif (current - last).total_seconds() >= heartbeat_seconds():
            found["last_heartbeat_at"] = current.isoformat()
            item, _ = request(state, "heartbeat", "heartbeat", "", "slow check-in: files, deliverables and final-check output change without a board event",
                              at=current.isoformat())
            if item is not None:
                item["wake_pending"] = True
    else:
        found.pop("last_heartbeat_at", None)   # a quiet project starts its clock again when work returns
    return urgent_items(state)


def mark_woken(state: dict[str, Any], items: list[dict[str, Any]], at: str) -> str:
    """Count one wake for these items (named by the highest-priority reason) and clear their pending flag."""
    reasons = [i["reason"] for i in items]
    primary = next((r for r in REASONS if r in reasons), reasons[0] if reasons else "")
    for item in items:
        item["wake_pending"] = False
        item["last_woken_at"] = at
        item["wakes"] = int(item.get("wakes", 0)) + 1
    tasks = sorted({i.get("task") for i in items if i.get("task")})
    count_wake(state, primary, tasks[0] if len(tasks) == 1 else "")
    return primary


def claim_direct(state: dict[str, Any], reason: str, subject: str, task: str, detail: str, at: str) -> tuple[dict[str, Any] | None, bool]:
    """A producer that sends its own wake text (a final pass, a completion, a prepared release): open or merge the item and say
    whether to send. A repeat of an item the CTO was already told about is not sent again; one whose delivery failed is."""
    item, is_new = request(state, reason, subject, task, detail, pending=False, at=at)
    if item is None:
        return None, True
    send = is_new or bool(item.get("wake_pending"))
    if send:
        mark_woken(state, [item], at)
    return item, send


def requeue(state: dict[str, Any], item_ids: list[str]) -> None:
    """A wake that could not be delivered is still owed."""
    for item_id in item_ids:
        item = (state.get("cto_pending") or {}).get(item_id)
        if item and item.get("status") == "open":
            item["wake_pending"] = True


def _line(item: dict[str, Any]) -> str:
    return f"[{item['id']}] {PLAIN.get(item['reason'], item['reason'])}: {item.get('detail') or item.get('subject')}"[:DETAIL_CHARS + 40]


def suffix(state: dict[str, Any], waking: list[dict[str, Any]], agent_id: str) -> str:
    """The sentence every CTO wake ends with: what to judge, how to record it, what is still unjudged."""
    waking_ids = {i["id"] for i in waking}
    others = [i for i in open_items(state) if i["id"] not in waking_ids]
    if not waking and not others:
        return ""
    text = (f" Judge it, then record the decision with `cto-disposition --agent {agent_id} --item <item id> "
            "--outcome hold|note|route|nothing --note \"<one line>\"`; a poll or a status post does not close an item.")
    if others:
        text += " Still unjudged: " + "; ".join(_line(i) for i in others[:LISTED_ITEMS]) + (
            f"; and {len(others) - LISTED_ITEMS} more" if len(others) > LISTED_ITEMS else "") + "."
    return text[:TEXT_CHARS]


def wake_text(state: dict[str, Any] | None, agent: dict[str, Any], items: list[dict[str, Any]]) -> str:
    """The instruction for a wake the board itself routes (no producer text of its own). The caller adds its notes and USER ACTION."""
    poll_command = f"poll --agent {agent.get('id', '')}"
    names = ", ".join(PLAIN[r] for r in REASONS if any(i["reason"] == r for i in items)) or "a check-in"
    listed = "; ".join(_line(i) for i in items[:LISTED_ITEMS]) + (f"; and {len(items) - LISTED_ITEMS} more" if len(items) > LISTED_ITEMS else "")
    base = (f"CTO WAKE ({names}){': ' + listed if listed else ''}. Run the board command `{poll_command}` (your launch board command prefix "
            "followed by it) once, then act on what it shows; a poll and a status post alone are not a judgment. "
            "Never wait on the product owner: when only an owner decision is outstanding, record 'awaiting owner decision' and stand by healthy.")
    return (base + (suffix(state, items, str(agent.get("id", ""))) if state is not None else ""))[:TEXT_CHARS]

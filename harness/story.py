# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The task as a story (task 2026-10-27_mission-control-redesign, slice 1): pure functions, no files, no network, no writes.

Board state goes in; plain words come out: one "Now" sentence, what is next, who has to act, "Step N of 26", the one thing the owner
must do, and the agent strip (builder, reviewer, CTO). The Projects page, the task page and the manager's project cards all read
this ONE computation, so the pages hold no business rules.

The story is a DISPLAY, never a gate (spec 12.1): it reads facts and decides nothing. A fact it cannot find is "not recorded"; it
never blocks, never errors and never claims more than the board knows. Every returned string is plain text; the pages escape it.

`gate()` is the Python port of the viewer's `taskGate` (board_viewer.py). Both stay until slice 3 removes the JavaScript copy; the
shared fixtures in tests/test_story.py pin them together.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from harness import analysis_pass, cost_center

TOTAL_STEPS = 26

# The plain 26-step list (spec section 3): number -> (title, who).
STEPS: dict[int, tuple[str, str]] = {
    1: ("You described it", "You"),
    2: ("Requirements drafted", "Builder"),
    3: ("Reviewer's early look", "Reviewer"),
    4: ("Questions put to you", "Reviewer"),
    5: ("You answered", "You"),
    6: ("Requirements agreed", "You"),
    7: ("Tests written first", "Reviewer"),
    8: ("Every requirement covered", "Board"),
    9: ("Tests locked", "Board"),
    10: ("Every test failed on the empty project", "Board"),
    11: ("A questioned test settled", "Builder, Reviewer"),
    12: ("Plan made", "Builder"),
    13: ("Building", "Builder"),
    14: ("Builder's own testing", "Builder"),
    15: ("Board ran the tests itself", "Board"),
    16: ("Review requested", "Board"),
    17: ("Review round 1", "Reviewer"),
    18: ("Round 1 result", "Reviewer"),
    19: ("Repair", "Builder"),
    20: ("Review round 2", "Reviewer"),
    21: ("Last retry", "Builder, Reviewer"),
    22: ("Final whole-app review", "Reviewer"),
    23: ("Release checks", "Board"),
    24: ("Try-it copy prepared", "Board"),
    25: ("Your review", "You"),
    26: ("Accepted and in main", "Board"),
}

DEVELOPER_ROLES = {"development", "engineering"}
SENTINEL_TASKS = {"AWAITING_OWNER_DIRECTION", "GLOBAL_MONITOR", "REVIEW_QUEUE"}
OPEN_REVIEW = ("authoring", "open", "reserved", "claimed")
COMPANY = {"codex": "OpenAI Codex", "claude": "Anthropic Claude", "gemini": "Google Gemini"}
VENDOR_COMPANY = {"openai": "OpenAI Codex", "anthropic": "Anthropic Claude", "google": "Google Gemini"}
ROLE_LABEL = {"builder": "Builder", "reviewer": "Independent reviewer", "cto": "CTO"}
ROLE_WORD = {"builder": "builder", "reviewer": "reviewer", "cto": "CTO"}
STALE_OUTPUT_SECONDS = 240


def step_title(number: int) -> str:
    return STEPS.get(number, ("", ""))[0]


def step_who(number: int) -> str:
    return STEPS.get(number, ("", ""))[1]


# ---- small helpers ------------------------------------------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parsed(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _title_case(value: Any) -> str:
    text = str(value or "Delivery task").replace("_", " ").replace("-", " ").lower()
    return " ".join(word[:1].upper() + word[1:] for word in text.split(" ") if word) or "Delivery task"


def _short(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def company_of(provider: str = "", vendor: str = "") -> str:
    """'OpenAI Codex', 'Anthropic Claude' or 'Google Gemini' from a provider id or a vendor name; '' when unknown."""
    return COMPANY.get(str(provider or "").lower()) or VENDOR_COMPANY.get(str(vendor or "").lower().split(" ")[0], "")


def elapsed_words(started: Any, ended: Any = None, now: datetime | None = None) -> str:
    """'just started', 'about 30 min so far' or 'about 2 h so far'; '' when the start is unknown."""
    start = _parsed(started)
    if start is None:
        return ""
    end = _parsed(ended) if ended else (now or _now())
    if end is None or end < start:
        return ""
    minutes = int((end - start).total_seconds() // 60)
    tail = "" if ended else " so far"
    if minutes < 2:
        return "just started" if not ended else "under 2 min"
    if minutes < 90:
        return f"about {minutes} min{tail}"
    hours = minutes / 60
    return f"about {hours:.0f} h{tail}" if hours >= 10 else f"about {hours:.1f} h{tail}".replace(".0 h", " h")


# ---- the facts of one task (ports of taskFacts and friends) ------------------------------------------------------------------

def visible_agents(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Agents that are on the page: live or paused ones (the viewer adds connected builder records)."""
    return [a for a in _dict(state.get("agents")).values() if isinstance(a, dict) and (a.get("active") or a.get("status") == "paused")]


def ordered_reviews(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(reviews, key=lambda r: str(r.get("completed_at") or r.get("claimed_at") or r.get("reserved_at") or r.get("requested_at") or ""))


def review_scope(review: dict[str, Any] | None) -> str:
    if not review:
        return "current work"
    if review.get("phase") == "final_acceptance":
        return "the complete task"
    subtask, chunk = review.get("subtask"), review.get("chunk")
    scope = subtask if subtask and subtask != "subtask-final" else chunk if chunk and chunk != "subtask-final" else "current chunk"
    return _title_case(scope)


def task_facts(state: dict[str, Any], task: str, agents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    chunks = list(_dict(_dict(state.get("task_chunks")).get(task)).values())
    plan = _dict(_dict(state.get("delivery_plans")).get(task))
    mode = plan.get("mode") or ("chunked" if chunks else "atomic")
    subtasks = [s for s in _dict(plan.get("subtasks")).values() if isinstance(s, dict) and s.get("status") != "superseded"]
    reviews = [r for r in _dict(state.get("qa_requests")).values() if isinstance(r, dict) and r.get("task") == task]
    pool = agents if agents is not None else visible_agents(state)
    agent = next((a for a in pool if a.get("task") == task and a.get("role") in DEVELOPER_ROLES), None) or next((a for a in pool if a.get("task") == task), None)
    if mode == "application":
        done = 0.0
        for sub in subtasks:
            if sub.get("status") == "passed":
                done += 1
                continue
            leaves = list(_dict(sub.get("chunks")).values())
            done += (0.9 * sum(1 for c in leaves if _dict(c).get("status") == "passed") / len(leaves)) if leaves else 0
        total = len(subtasks)
    else:
        done = float(sum(1 for c in chunks if _dict(c).get("status") == "passed"))
        total = len(chunks)
    passed_subtasks = sum(1 for s in subtasks if s.get("status") == "passed")
    return {"mode": mode, "plan": plan, "reviews": reviews, "agent": agent, "done": done, "total": total,
            "subtasks": subtasks, "passed_subtasks": passed_subtasks}


def stopped_tasks(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Unfinished tasks whose builder the owner stopped and nobody has resumed (port of board_viewer._stopped_tasks)."""
    cancelled = _dict(state.get("cancelled_tasks"))
    agents = [a for a in _dict(state.get("agents")).values() if isinstance(a, dict)]
    owned = {a.get("task") for a in agents if a.get("active") and a.get("role") in DEVELOPER_ROLES}
    out: dict[str, dict[str, Any]] = {}
    for agent in agents:
        task = agent.get("task")
        if (agent.get("role") in DEVELOPER_ROLES and agent.get("stopped_by_owner_at") and not agent.get("active")
                and not agent.get("superseded_by_agent_id") and task and task not in cancelled and task not in owned
                and _dict(_dict(state.get("releases")).get(task)).get("status") not in ("VISUAL_TEST_REQUIRED", "ACCEPTED")):
            out[str(task)] = {"agent_id": agent.get("id"), "stopped_at": agent.get("stopped_by_owner_at")}
    return out


def recent_output_active(agent: dict[str, Any] | None, now: datetime | None = None) -> bool:
    agent = agent or {}
    if agent.get("turn_state") in ("idle_at_prompt", "session_ended"):
        return False
    if agent.get("turn_state") == "working":
        return True
    moment = _parsed(agent.get("recent_output_at"))
    return bool(moment and ((now or _now()) - moment).total_seconds() < STALE_OUTPUT_SECONDS)


def _accepted_real(state: dict[str, Any], task: str) -> bool:
    """The owner's Accept counts only when its merge exists (port of board.owner_accept_is_real, over the same state keys)."""
    decision = _dict(_dict(state.get("release_decisions")).get(task))
    if decision.get("decision") != "accepted":
        return False
    if task in _dict(state.get("git_acceptances")):
        return True
    if _dict(decision.get("git_acceptance")).get("status") == "failed" or task in _dict(state.get("git_reintegration_required")):
        return False
    governed = bool(_dict(state.get("task_repositories")).get(task) and any(
        isinstance(r, dict) and r.get("task") == task and r.get("phase") == "final_acceptance" and r.get("status") == "passed" and r.get("mirror_ref")
        for r in _dict(state.get("qa_requests")).values()))
    return not governed


# ---- the gate: Python port of taskGate -----------------------------------------------------------------------------------------

def gate(state: dict[str, Any], task: str, facts: dict[str, Any], *, runtime_commit: str = "", runtime_managed: bool = False,
         now: datetime | None = None, skip: frozenset[str] = frozenset(), stopped: dict[str, Any] | None = None) -> dict[str, Any]:
    """The task's board status and the board's own plain sentence, exactly as the viewer's taskGate computes them.

    `skip` names checks to leave out ("reintegration", "refresh", "overdue", "stopped", "git") so the caller can learn which step the
    work is really at underneath a note. Returns status, next, tone and the review the status was read from."""
    release = _dict(_dict(state.get("releases")).get(task))
    row = _dict(_dict(state.get("release_decisions")).get(task))
    decision = row.get("decision")
    released = release.get("status") in ("VISUAL_TEST_REQUIRED", "ACCEPTED")
    external = release.get("runtime_verification_deferred_to_target_acceptance") is True
    checks = _dict(release.get("checks"))
    runtime_gated = bool(checks.get("deployed_runtime_verified") or checks.get("deployed_chat_verified"))
    deployed = external or not runtime_gated or not released or not runtime_managed or (bool(runtime_commit) and release.get("head_commit") == runtime_commit)
    agent = facts["agent"]
    ordered = ordered_reviews(facts["reviews"])
    latest = ordered[-1] if ordered else None
    current = latest if latest and latest.get("status") in OPEN_REVIEW else None
    structure = int(_dict(_dict(state.get("delivery_plans")).get(task)).get("structure_revision") or 0)
    final_passed = any(r.get("phase") == "final_acceptance" and r.get("status") == "passed" and (not structure or int(r.get("structure_revision") or 0) == structure) for r in ordered)
    total, done = facts["total"], facts["done"]
    confirmed = bool(_dict(_dict(state.get("requirement_confirmations")).get(task)).get("text"))

    def out(status: str, nxt: str, tone: str = "active", review: dict[str, Any] | None = None, kind: str = "") -> dict[str, Any]:
        return {"status": status, "next": nxt, "tone": tone, "review": review, "kind": kind}

    if decision == "accepted" and _dict(row.get("git_acceptance")).get("status") == "failed" and "git" not in skip:
        return out("ACCEPT SAVED, NOT IN MAIN YET", "Your approval was saved, but the app could not be updated with the approved work, so the task is not finished. Ask the CTO to fix the reason shown below, then press Try Accept again.", "repair")
    if decision == "accepted":
        return out("OWNER ACCEPTED", "You accepted this release. Your visual-test response is closed.", "ready")
    reintegration = _dict(_dict(state.get("git_reintegration_required")).get(task))
    if reintegration and not decision and "reintegration" not in skip:
        return out("RE-INTEGRATION NEEDED", str(reintegration.get("owner_line") or "Other approved work changed the app while this task was underway. Delivery is combining the changes and will test again before the final review. Nothing for you to do."), "repair")
    if decision == "not_accepted":
        return out("OWNER REJECTED / REPAIR REQUIRED", "Your reason and attachments are saved. Delivery will use them in a new repair, review, and release cycle.", "repair")
    if released and not deployed and "refresh" not in skip:
        return out("DEPLOYMENT REFRESH REQUIRED", "Mission Control is running a different version from the one that was reviewed, so Accept is switched off for now. The CTO is loading the reviewed version. Nothing for you to do.", "repair")
    stopped = stopped if stopped is not None else stopped_tasks(state)
    if task in stopped and not released and "stopped" not in skip:
        return out("STOPPED — TASK KEPT", "You stopped the Delivery agent. The task is kept with all its work. Start a new Delivery agent to carry on, or use Cancel task to abandon it.", "repair", kind="stopped")
    if agent and agent.get("task") == task and not confirmed:
        return out("AWAITING FINAL REQUIREMENTS", "Delivery is clarifying the request. No implementation or review may begin until you say go ahead and the final requirements are recorded.")
    if released:
        return out("READY FOR YOUR TEST", "The reviewed version is ready. Try it using the steps below, then choose Accepted or Not accepted.", "ready")
    if latest and latest.get("status") == "failed":
        return out("REPAIR IN PROGRESS", "The independent review found a problem. Delivery is fixing it and will send it for review again. Nothing for you to do.", "repair", kind="review_failed")
    if agent and agent.get("broker_refusal") and "git" not in skip:
        reason = _dict(agent.get("broker_refusal")).get("reason", "")
        return out("BLOCKED — GIT WRITE REFUSED", f"The Delivery agent could not save its latest changes ({reason}). Trying again will not help; the CTO has to repair this. Nothing for you to do.", "repair", kind="git_refused")
    if agent and agent.get("liveness") == "stalled" and recent_output_active(agent, now) and not agent.get("stall_exhausted") and "overdue" not in skip:
        return out("STATUS UPDATE OVERDUE", "The Delivery agent is working but has not posted a progress update recently. The app has asked it for one. Nothing for you to do.", kind="overdue")
    if agent and agent.get("liveness") == "stalled":
        return out("REPAIR IN PROGRESS", "The Delivery agent stopped reporting. The app is restarting it from its saved work. Nothing for you to do.", "repair", kind="stalled")
    if current and current.get("status") == "claimed":
        return out("INDEPENDENT REVIEW IN PROGRESS", f"An Independent Reviewer is testing {review_scope(current)} with its own set of checks, separate from the ones Delivery ran. Nothing for you to do.", review=current)
    if current and current.get("status") == "reserved" and current.get("delivery_state") == "executing":
        return out("DELIVERY TESTING / REVIEWER AUTHORING", f"Delivery is running its own checks while the independent reviewer prepares separate tests for {review_scope(current)}. The reviewer starts testing when Delivery's checks pass. Nothing for you to do.", review=current)
    if current and current.get("status") == "reserved":
        return out("REVIEWER PREPARING CHALLENGE LEDGER", f"The independent reviewer has taken {review_scope(current)} and is preparing its own tests, different from the ones Delivery ran. The review starts when they are ready. Nothing for you to do.", review=current)
    if current and current.get("status") == "authoring":
        return out("DELIVERY TESTING / REVIEW READY", f"Delivery is running its checks for {review_scope(current)}. A copy of the work is set aside so the reviewer can prepare tests now; the review starts after Delivery's checks pass. Nothing for you to do.", review=current)
    if current and current.get("status") == "open":
        return out("INDEPENDENT REVIEW IN PROGRESS", f"The review for {review_scope(current)} is waiting for the independent reviewer to pick it up. Nothing for you to do.", review=current, kind="waiting_pickup")
    if final_passed or (agent and agent.get("status") in ("release_wait", "done", "final_review_passed")):
        return out("FINAL RELEASE CHECKS", "Building and the independent review have passed. The CTO is checking that the version prepared for you is exactly the one that passed. It is not ready for your test yet. Nothing for you to do.")
    if total > 0 and done == total:
        application = facts["plan"].get("mode") == "application"
        return out("INDEPENDENT REVIEW IN PROGRESS", f"The separate {'parts' if application else 'steps'} have each passed. The whole {'app' if application else 'task'} still needs a complete test and an independent review. Nothing for you to do.", kind="whole")
    if agent and agent.get("status") in ("repairing", "CONSOLIDATED_REPAIR"):
        return out("REPAIR IN PROGRESS", "The review found a problem. Delivery is fixing it and will send it for a new independent review. Nothing for you to do.", "repair", kind="repairing")
    mode = facts["mode"]
    nxt = ("Delivery is building and checking the next part of your app. Nothing for you to do." if mode == "application"
           else "Delivery is building your requested change and checking it. An independent reviewer checks the result next. Nothing for you to do." if mode == "atomic"
           else "Delivery is building and checking the next small step. Nothing for you to do.")
    return out("DEVELOPMENT IN PROGRESS", nxt)


# ---- which step is Now ---------------------------------------------------------------------------------------------------------

def review_round(state: dict[str, Any], review: dict[str, Any] | None) -> int:
    """1, 2 or 3+: the round of the scope the review belongs to (round numbers restart per scope lineage, as review_rounds does)."""
    if not review:
        return 1
    from harness import review_rounds
    requests = [r for r in _dict(state.get("qa_requests")).values() if isinstance(r, dict)]
    prior = review_rounds.prior_judgments(requests, review)
    return review_rounds.round_number(prior, review_rounds.baseline(prior))


def _confirmation_check(state: dict[str, Any], task: str) -> dict[str, Any]:
    return _dict(_dict(_dict(state.get("requirement_confirmations")).get(task)).get("contract_check"))


def _early_step(state: dict[str, Any], task: str) -> int:
    """First unmet of steps 7-11 (tests written first); 14 when all are met (the builder's own checks are what is running)."""
    check = _confirmation_check(state, task)
    if not check.get("checks"):
        return 7
    if not check.get("compare") and not check.get("labels"):
        return 8
    if not check.get("checks_sha256"):
        return 9
    if _dict(check.get("red_proof")).get("status") not in ("settled", "done"):
        return 10
    if any(_dict(d).get("status") == "open" for d in check.get("deviations") or []):
        return 11
    return 14


def current_step(state: dict[str, Any], task: str, g: dict[str, Any], facts: dict[str, Any]) -> int:
    status = g["status"]
    proposal = _dict(_dict(state.get("requirement_proposals")).get(task))
    if status == "AWAITING FINAL REQUIREMENTS":
        check = _dict(proposal.get("contract_check"))
        if not proposal or proposal.get("status") != "awaiting_owner":
            return 2        # nothing has been put to the owner yet (or he asked for changes and the builder is rewriting them)
        if check.get("status") in ("reading", "rechecking"):
            return 3
        if check.get("status") == "questions":
            return 5
        return 6
    if status == "DELIVERY TESTING / REVIEWER AUTHORING":
        return _early_step(state, task)
    if status == "DEVELOPMENT IN PROGRESS":
        return 13 if (facts["plan"] or _dict(_dict(state.get("task_chunks")).get(task))) else 12
    if status == "DELIVERY TESTING / REVIEW READY":
        review = g.get("review") or {}
        return 15 if review.get("delivery_state") == "passed" and review.get("delivery_evidence") else 14
    if status == "REVIEWER PREPARING CHALLENGE LEDGER":
        return 16
    if status == "INDEPENDENT REVIEW IN PROGRESS":
        review = g.get("review") or {}
        if g.get("kind") == "waiting_pickup":
            return 16
        if g.get("kind") == "whole" or review.get("phase") == "final_acceptance":
            return 22
        number = review_round(state, review)
        return 17 if number <= 1 else 20 if number == 2 else 21
    if status in ("REPAIR IN PROGRESS", "OWNER REJECTED / REPAIR REQUIRED"):
        if g.get("kind") in ("stalled", "overdue"):
            return 13
        return 19
    if status in ("FINAL RELEASE CHECKS", "DEPLOYMENT REFRESH REQUIRED"):
        return 23
    if status == "READY FOR YOUR TEST":
        preview = _dict(_dict(_dict(state.get("releases")).get(task)).get("preview"))
        return 24 if preview.get("status") == "starting" and preview.get("requested") != "view_app" else 25
    if status in ("ACCEPT SAVED, NOT IN MAIN YET", "OWNER ACCEPTED"):
        return 26
    return 13


def _underlying(state: dict[str, Any], task: str, facts: dict[str, Any], g: dict[str, Any], **kw: Any) -> tuple[dict[str, Any], int]:
    """The gate and step underneath a note that leaves the step where it was (re-integration, refresh text, overdue, stopped, Git refusal)."""
    skip = frozenset({"reintegration", "overdue", "stopped", "git"})
    under = gate(state, task, facts, skip=skip, **kw)
    return under, current_step(state, task, under, facts)


# ---- the sentences ------------------------------------------------------------------------------------------------------------

def _round_line(state: dict[str, Any], task: str) -> str:
    """'Round 1: 2 blockers, 3 notes.' for the newest judged review of the task; '' when nothing was judged or the counts are not recorded."""
    judged = [r for r in _dict(state.get("qa_requests")).values() if isinstance(r, dict) and r.get("task") == task and r.get("status") in ("passed", "failed")]
    if not judged:
        return ""
    last = sorted(judged, key=lambda r: (int(r.get("cycle", 0) or 0), str(r.get("completed_at") or "")))[-1]
    blockers = last.get("structured_findings")
    notes = _dict(last.get("board_decision")).get("notes") if isinstance(last.get("board_decision"), dict) else None
    number = review_round(state, last) if last.get("review_round") else 0
    head = f"Round {number}" if number else "Last review"
    if isinstance(blockers, list):
        text = f"{head}: {_plural(len(blockers), 'blocker')}"
        if isinstance(notes, list):
            text += f", {_plural(len(notes), 'note')}"
        return text + "."
    return f"{head}: {'passed' if last.get('status') == 'passed' else 'found problems'}."


def _blockers(state: dict[str, Any], task: str) -> int:
    failed = [r for r in _dict(state.get("qa_requests")).values() if isinstance(r, dict) and r.get("task") == task and r.get("status") == "failed"]
    if not failed:
        return 0
    last = sorted(failed, key=lambda r: (int(r.get("cycle", 0) or 0), str(r.get("completed_at") or "")))[-1]
    findings = last.get("structured_findings")
    return len(findings) if isinstance(findings, list) else 0


def _area(facts: dict[str, Any]) -> str:
    if facts["mode"] != "application" or not facts["total"]:
        return ""
    return f" (work area {min(facts['total'], facts['passed_subtasks'] + 1)} of {facts['total']})"


def sentences(state: dict[str, Any], task: str, step: int, g: dict[str, Any], facts: dict[str, Any]) -> tuple[str, str]:
    """(Now, Next): one plain sentence each for the ordinary course of work."""
    review = g.get("review") or {}
    scope = review_scope(review) if review else "the work"
    if step == 2:
        return "The builder is writing down what you asked for.", "Next: the reviewer reads it, then you say Go ahead."
    if step == 3:
        return "The reviewer is reading the requirements and planning the tests.", "Next: you read the requirements and say Go ahead."
    if step in (4, 5):
        return "The reviewer has a question for you.", "Next: answer it, then say Go ahead."
    if step == 6:
        return "The requirements are ready. Nothing is built until you say Go ahead.", "Next: the reviewer writes its tests first, then the builder starts."
    if step in (7, 8, 9, 10, 11):
        return "The reviewer is writing its own tests before anything is built.", "Next: the builder plans the work."
    if step == 12:
        return "The builder is planning the work.", "Next: the builder builds it, then tests it."
    if step == 13:
        return f"The builder is building{_area(facts)}.", "Next: the builder tests it, then an independent reviewer checks it."
    if step in (14, 15):
        return "The builder is running its own checks before the review.", "Next: the independent reviewer tests it."
    if step == 16:
        waiting = g.get("kind") == "waiting_pickup"
        return (f"The review for {scope} is waiting for the independent reviewer." if waiting else f"The reviewer is preparing its tests for {scope}."), "Next: the review itself."
    if step in (17, 20):
        return f"The reviewer is testing {scope} (round {1 if step == 17 else 2}).", "Next: its result. If it finds problems the builder fixes them."
    if step == 21:
        return f"The reviewer is testing {scope} on the last retry.", "Next: its result. If it still fails the part is reported as not finished."
    if step == 19:
        if g["status"] == "OWNER REJECTED / REPAIR REQUIRED":
            return "Repairing after your feedback. The builder is using your reason and files.", "Next: a new review, then it is yours to try again."
        count = _blockers(state, task)
        what = f"the {_plural(count, 'problem')} the reviewer found" if count else "what the reviewer found"
        return f"The builder is fixing {what}.", "Next: a new review round, then it is yours to try."
    if step == 22:
        if _dict(state.get("task_kinds")).get(task) == "analysis":
            return "The reviewer is checking the report against the code.", "Next: it is yours to read."
        return "The reviewer is testing the whole app.", "Next: release checks, then it is yours to try."
    if step == 23:
        return "The board is checking that the version prepared for you is the one that passed.", "Next: it is yours to try."
    if step == 24:
        return "The board is preparing your try-it copy.", "Next: you try it and accept."
    if step == 25:
        return "Built, tested and passed by the independent reviewer. It is ready for you.", "Next: you try it and accept."
    if step == 26:
        return "Accepted.", "Nothing more to do on this task."
    return "Work is in progress.", ""


def _accepted_sentence(state: dict[str, Any], task: str) -> str:
    governed = task in _dict(state.get("git_acceptances"))
    return "Accepted. It is on your project's main." if governed else "Accepted."


# ---- exceptions outrank progress (spec 12.6) -----------------------------------------------------------------------------------

def _role_of_agent(agent: dict[str, Any]) -> str:
    role = str(agent.get("role") or "")
    return "builder" if role in DEVELOPER_ROLES else "reviewer" if role == "qa" else "cto" if role == "cto" else "builder"


def _role_of_kind(kind: Any) -> str:
    kind = str(kind or "")
    return "reviewer" if "reviewer" in kind else "cto" if "cto" in kind else "builder"


def exception_for(state: dict[str, Any], task: str, *, agents: list[dict[str, Any]] | None = None, sessions: list[dict[str, Any]] | None = None,
                  stopped: dict[str, Any] | None = None, g: dict[str, Any] | None = None, redact: Callable[[Any], Any] = lambda value: value,
                  accepted: set[str] | None = None) -> dict[str, Any] | None:
    """The first matching exception, in the spec's priority order, or None.

    {"kind", "role", "reason", "action", "needs_owner"}: the reason, the role and the ONE action. A fresh builder note never overrides it."""
    pool = agents if agents is not None else visible_agents(state)
    cancelled = _dict(state.get("cancelled_tasks"))
    accepted = accepted or set()
    live = lambda t: str(t) not in accepted and str(t) not in cancelled   # noqa: E731
    sessions = sessions or []

    needed = _dict(state.get("reviewer_needed"))
    if needed.get("sign_in"):
        how = needed.get("sign_in_how") or "run /login"
        return {"kind": "sign_in", "role": "reviewer", "needs_owner": True,
                "reason": "The independent reviewer is signed out, so nothing can move forward.",
                "action": f"Open the reviewer's terminal window and {how}."}
    for agent in pool:
        note = str(agent.get("liveness_note") or "")
        if agent.get("liveness") == "needs_sign_in" and "Bypass Permissions" not in note and "out of Gemini quota" not in note:
            role = _role_of_agent(agent)
            return {"kind": "sign_in", "role": role, "needs_owner": True,
                    "reason": f"The {ROLE_WORD[role]} is signed out, so it cannot work.",
                    "action": f"Open the {ROLE_WORD[role]}'s terminal window and sign in again."}
    for item in sessions:
        auth = _dict(item.get("claude_auth_action"))
        if auth and auth.get("reason") != "consent":
            role = _role_of_kind(item.get("kind"))
            return {"kind": "sign_in", "role": role, "needs_owner": True,
                    "reason": _short(auth.get("why") or f"The {ROLE_WORD[role]} needs you at its Claude sign-in."),
                    "action": _short(auth.get("title") or f"Open the {ROLE_WORD[role]}'s terminal window and follow the Claude prompt.")}
    for agent in pool:
        if "Bypass Permissions" in str(agent.get("liveness_note") or ""):
            role = _role_of_agent(agent)
            return {"kind": "consent", "role": role, "needs_owner": True,
                    "reason": f"The {ROLE_WORD[role]} is waiting for your OK to run without asking each time.",
                    "action": f"Open the {ROLE_WORD[role]}'s terminal window and accept the Claude permission question."}
    for item in sessions:
        auth = _dict(item.get("claude_auth_action"))
        if auth.get("reason") == "consent":
            role = _role_of_kind(item.get("kind"))
            return {"kind": "consent", "role": role, "needs_owner": True,
                    "reason": _short(auth.get("why") or f"The {ROLE_WORD[role]} is waiting for your OK to run without asking each time."),
                    "action": _short(auth.get("title") or f"Open the {ROLE_WORD[role]}'s terminal window and accept the Claude permission question.")}
    for agent in pool:
        if "out of Gemini quota" in str(agent.get("liveness_note") or ""):
            role = _role_of_agent(agent)
            return {"kind": "quota", "role": role, "needs_owner": False,
                    "reason": f"The {ROLE_WORD[role]} (Gemini) has used up its quota.",
                    "action": "Wait for the quota to reset, or choose another AI company for this role in Settings."}
    for agent in pool:
        if agent.get("role") == "cto" and agent.get("active") and agent.get("recovery_state") == "unresponsive":
            return {"kind": "cto_unresponsive", "role": "cto", "needs_owner": True,
                    "reason": "The CTO is not responding; it may need to sign in again. Its automatic wake-ups are paused.",
                    "action": "Open the CTO's terminal window; if it shows a login prompt, run /login."}
    for escalation in _dict(state.get("review_escalations")).values():
        if isinstance(escalation, dict) and escalation.get("status") == "could_not_finish" and live(escalation.get("task")) and (not task or str(escalation.get("task")) == task):
            return {"kind": "repairs_exhausted", "role": "builder", "needs_owner": False,
                    "reason": _short(redact(escalation.get("outcome") or "The builder could not finish one part after repeated tries; nothing of it was released.")),
                    "action": "Nothing is needed from you now. Send the builder a message if you want it to try another way."}
    for gap in _dict(state.get("verification_gaps")).values():
        if isinstance(gap, dict) and gap.get("state") in ("diagnosing", "owner_decision", "answered") and live(gap.get("task")) and (not task or str(gap.get("task")) == task):
            ask = gap.get("state") == "owner_decision"
            return {"kind": "verification_gap", "role": "reviewer", "needs_owner": ask,
                    "reason": "Not fully tested: " + (_short(redact(gap.get("reason") or "one part could not be checked")) + "."),
                    "action": "Open the project and answer the question about this test." if ask else "Nothing for you to do; the app is settling it by itself."}
    pause = str(_dict(state.get("project_pause")).get("status") or "")
    if pause in ("paused", "resuming"):
        return {"kind": "paused", "role": "", "needs_owner": False,
                "reason": "Resume recovery is in progress. The board stays read-only until it finishes." if pause == "resuming" else "Paused safely. Nothing is lost.",
                "action": "Nothing for you to do." if pause == "resuming" else "Press Resume when you are ready."}
    gate_kind = (g or {}).get("kind", "")
    if gate_kind == "stopped" or (task and task in (stopped if stopped is not None else stopped_tasks(state))):
        return {"kind": "stopped", "role": "builder", "needs_owner": True,
                "reason": "You stopped the builder. The task and all its work are kept.",
                "action": "Start a new builder to carry on, or cancel the task."}
    if gate_kind == "git_refused":
        owner_of = next((a for a in pool if a.get("task") == task), None) or {}
        reason = _dict(owner_of.get("broker_refusal")).get("reason", "")
        return {"kind": "git_refused", "role": "builder", "needs_owner": False,
                "reason": "The builder could not save its latest changes" + (f" ({_short(redact(reason), 120)})" if reason else "") + ".",
                "action": "Nothing for you to do; the CTO has to repair this."}
    for held, hold in sorted(_dict(state.get("control_plane_holds")).items()):
        if isinstance(hold, dict) and hold.get("status") == "open" and live(hold.get("task") or held) and (not task or str(hold.get("task") or held) == task):
            return {"kind": "hold", "role": "cto", "needs_owner": False,
                    "reason": "Needs repair: " + _short(redact(hold.get("reason") or "the app needs a repair before this task can move on"), 300),
                    "action": "Nothing for you to do unless a card above asks for something."}
    return None


# ---- one task -----------------------------------------------------------------------------------------------------------------

def money_line(cost: dict[str, Any] | None, task: str) -> dict[str, Any]:
    """The task's cost for a card: {"text", "usd", "window"}. Unmeasured is said so; it is never $0 and never invented."""
    if not cost or not cost.get("available"):
        return {"text": "Cost: not measured yet", "usd": None, "window": ""}
    row = _dict(_dict(cost.get("tasks")).get(task))
    if not row or not (row.get("tokens") or row.get("usd")):
        gemini = bool(row.get("gemini"))
        return {"text": "Cost: Gemini is not measured yet" if gemini else "Cost: not counted yet", "usd": None, "window": ""}
    text = f"{cost_center.money(row['usd'])} so far" + (" (estimated)" if row.get("estimated") else "")
    limit = _dict(row.get("codex_limit"))
    window = ""
    if limit:
        if limit.get("reset"):
            window = "Codex window reset during this task"
        elif float(limit.get("increase") or 0) > 0:
            window = f"Codex +{float(limit['increase']):g}% of window"
    return {"text": text, "usd": float(row["usd"]), "window": window}


def _task_started(state: dict[str, Any], task: str) -> str:
    begun = [e.get("at") for e in state.get("events") or [] if isinstance(e, dict) and e.get("task") == task and e.get("kind") == "task_begun" and e.get("at")]
    if begun:
        return str(min(begun))
    requested = [str(r.get("requested_at")) for r in _dict(state.get("qa_requests")).values() if isinstance(r, dict) and r.get("task") == task and r.get("requested_at")]
    return min(requested, default="")


def build(state: dict[str, Any], contracts: dict[str, Any] | None, task: str, runtime: dict[str, Any] | None = None, *,
          agents: list[dict[str, Any]] | None = None, sessions: list[dict[str, Any]] | None = None, cost: dict[str, Any] | None = None,
          now: datetime | None = None, redact: Callable[[Any], Any] = lambda value: value, detail: bool = True,
          ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    """The story of one task: step, title, now, next, who, needs_owner (and the ONE action), cost.

    With `detail` (the task page) it also carries `timeline` (the 26 steps, slice 2) and `proof` (the Proof tab); the Projects card leaves them out.

    `runtime` = {"commit", "managed"}: the viewer's loaded commit and managed flag, so Ready/Accept is offered only when the ported gate
    allows it. With no runtime (the manager) the commit check is off and the manager only links to the project (spec 12.2)."""
    runtime = runtime or {}
    commit, managed = str(runtime.get("commit") or ""), bool(runtime.get("managed"))
    facts = task_facts(state, task, agents)
    stopped = stopped_tasks(state)
    g = gate(state, task, facts, runtime_commit=commit, runtime_managed=managed, now=now, stopped=stopped)
    step = current_step(state, task, g, facts)
    note = ""
    if g["status"] in ("RE-INTEGRATION NEEDED",) or g.get("kind") in ("overdue", "stopped", "git_refused"):
        under, step = _underlying(state, task, facts, g, runtime_commit=commit, runtime_managed=managed, now=now, stopped=stopped)
        note = g["next"]
        facts_g = under
    else:
        facts_g = g
    now_text, next_text = sentences(state, task, step, facts_g, facts)
    if facts_g["status"] == "DEVELOPMENT IN PROGRESS":      # a Code analysis in its two passes: say which phase in plain words (spec 2026-10-29)
        now_text, next_text = analysis_pass.story_now(_dict(_dict(state.get("analysis_passes")).get(task))) or (now_text, next_text)
    needs_owner, action, who = False, "", step_who(step)
    status = g["status"]
    if status == "AWAITING FINAL REQUIREMENTS" and step in (5, 6):
        needs_owner = True
        action = "Answer the reviewer's question, then press Go ahead." if step == 5 else "Read the requirements and press Go ahead (or Modify to change them)."
    elif status == "READY FOR YOUR TEST":
        needs_owner, action = True, "Try it, then press Accept or Not accepted."
    elif status == "ACCEPT SAVED, NOT IN MAIN YET":
        now_text, next_text = "Accepted, but it is not in main yet.", "Next: fix the reason shown on the task card, then press Try Accept again."
        needs_owner, action, who = True, "Press Try Accept again after the reason is fixed.", "You"
    elif status == "OWNER ACCEPTED":
        now_text, next_text = _accepted_sentence(state, task), "Nothing more to do on this task."
    elif status == "DEPLOYMENT REFRESH REQUIRED":
        now_text = "Mission Control is loading the version that was reviewed. Accept is off for the moment."
        next_text = "Next: it is yours to try."
    elif status == "RE-INTEGRATION NEEDED":
        who = "Builder"
    problem = exception_for(state, task, agents=agents, sessions=sessions, stopped=stopped, g=g, redact=redact,
                            accepted={str(t) for t in _dict(state.get("release_decisions")) if _accepted_real(state, str(t))})
    if problem and status != "OWNER ACCEPTED":
        now_text, next_text = problem["reason"], "Next: " + problem["action"]
        needs_owner, action = (True, problem["action"]) if problem["needs_owner"] else (False, "")
        who = ROLE_LABEL.get(problem["role"], "") if problem["role"] else "You"
    elif note:
        next_text = next_text + " " + note if next_text else note
    money = money_line(cost, task)
    started = _task_started(state, task)
    release = _dict(_dict(state.get("releases")).get(task))
    # The clock stops when the work reached the owner (its release was recorded): what he sees is how long the work took, not how long it has waited for him.
    ended = (str(_dict(_dict(state.get("release_decisions")).get(task)).get("recorded_at") or "") if status == "OWNER ACCEPTED" else "") or (str(release.get("recorded_at") or "") if release.get("status") in ("VISUAL_TEST_REQUIRED", "ACCEPTED") and status != "OWNER REJECTED / REPAIR REQUIRED" else "")
    brief = _dict(_dict(state.get("task_briefs")).get(task))
    fresh_note = ""
    if brief.get("update") and status not in ("OWNER ACCEPTED",) and not problem:
        failures = _dict(_dict(state.get("failure_words")).get("delivery")).get(task)
        stale = bool(failures and brief.get("updated_at") and (_parsed(_dict(failures).get("latest_at")) or datetime.min.replace(tzinfo=timezone.utc)) > (_parsed(brief.get("updated_at")) or datetime.max.replace(tzinfo=timezone.utc)))
        if not stale:
            fresh_note = _short(brief.get("update"), 400)
            # a builder that kept working after its note says "now adding a test" must not leave that line looking current
            moved_on = [str(e.get("at") or "") for e in (state.get("events") or []) if isinstance(e, dict) and e.get("task") == task
                        and e.get("role") == "engineering" and e.get("kind") in ("broker_commit_created", "completion_contract_evidence", "status_update")]
            note_at = str(brief.get("updated_at") or "")
            if fresh_note and note_at and moved_on and max(moved_on) > note_at:
                fresh_note += " (written earlier; the builder has done more since)"
    result = {
        "task": task, "title": _title_case(task), "step": step, "of": TOTAL_STEPS, "step_title": step_title(step), "who": who,
        "now": now_text, "next": next_text, "needs_owner": needs_owner, "owner_action": action,
        "gate": status, "exception": problem, "builder_note": fresh_note,
        "elapsed": elapsed_words(started, ended or None, now), "started_at": started,
        "cost": money["text"], "usd": money["usd"], "codex_window": money["window"],
        "released": bool(release.get("status") in ("VISUAL_TEST_REQUIRED", "ACCEPTED")),
        "accepted": status == "OWNER ACCEPTED", "progress": round(100 * step / TOTAL_STEPS),
    }
    if detail:
        try:
            result["proof"] = proof(state, task, facts, ledger)
            result["requirements"] = result["proof"].get("requirements_tile", {})
            result["timeline"] = timeline(state, task, result, facts, g, real=_accepted_real(state, task))
        except Exception as error:   # noqa: BLE001 - a display: a story that cannot be drawn never hides the rest of the page
            result["timeline"] = {"error": f"The step list could not be drawn ({type(error).__name__}).", "steps": []}
            result["proof"] = {"error": f"The proof could not be drawn ({type(error).__name__}).", "rows": []}
    return result


# ---- the agent strip -----------------------------------------------------------------------------------------------------------

def _session_for(agent: dict[str, Any] | None, sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not agent:
        return None
    return next((s for s in sessions if s.get("id") == agent.get("session_id")), None)


def _role_cost(cost: dict[str, Any] | None, task: str, role: str) -> str:
    if not cost or not cost.get("available"):
        return "Cost: not measured yet"
    key = {"builder": "delivery", "reviewer": "reviewer", "cto": "cto"}[role]
    tasks = _dict(cost.get("tasks"))
    last_task = False
    if role != "cto" and not (task and task in tasks):
        # No task is on the page (it was accepted): an idle builder or reviewer shows its figure from the latest task, labelled so.
        done = [(str(row.get("last_at") or ""), name) for name, row in tasks.items() if isinstance(row, dict) and _dict(row.get("roles")).get(key)]
        if done:
            task, last_task = max(done)[1], True
    row = _dict(_dict(cost.get("overhead") if role == "cto" else tasks.get(task)).get("roles")).get(key)
    if not row or not (row.get("tokens") or row.get("usd")):
        gemini = key in _dict(_dict(cost.get("tasks")).get(task)).get("gemini", [])
        return "Cost: Gemini is not measured yet" if gemini else "Cost: not counted yet"
    return (f"{cost_center.money(row['usd'])} · {cost_center.tokens_text(int(row['tokens']))} tokens"
            + (" (project overhead)" if role == "cto" else " (last task)" if last_task else ""))


def _reviewer_assignment(state: dict[str, Any], agent: dict[str, Any]) -> dict[str, Any] | None:
    mine = [r for r in _dict(state.get("qa_requests")).values() if isinstance(r, dict) and (
        (r.get("status") in ("claimed", "suspended") and r.get("claimed_by") == agent.get("id"))
        or (r.get("status") == "reserved" and r.get("reserved_by") == agent.get("id"))
        or (r.get("status") in ("authoring", "open") and r.get("routed_to") == agent.get("id")))]
    return ordered_reviews(mine)[-1] if mine else None


def agent_strip(state: dict[str, Any], task: str, *, agents: list[dict[str, Any]] | None = None, sessions: list[dict[str, Any]] | None = None,
                settings: dict[str, Any] | None = None, cost: dict[str, Any] | None = None, story: dict[str, Any] | None = None,
                now: datetime | None = None) -> list[dict[str, Any]]:
    """Builder, independent reviewer and CTO for one task: company, what each is doing now, what slowed it, its cost, and what the
    Message and View-its-screen buttons can do (with the plain fallbacks of spec 12.8). Never invents a row for an agent that is not there."""
    pool = agents if agents is not None else visible_agents(state)
    sessions = sessions or []
    settings = _dict(settings)
    notes = _difficulty_latest(state, task)
    rows: list[dict[str, Any]] = []
    for role in ("builder", "reviewer", "cto"):
        if role == "builder":
            agent = next((a for a in pool if a.get("task") == task and a.get("role") in DEVELOPER_ROLES), None) or next((a for a in pool if a.get("role") in DEVELOPER_ROLES and a.get("active")), None)
        elif role == "reviewer":
            agent = next((a for a in pool if a.get("role") == "qa" and a.get("active") and (_reviewer_assignment(state, a) or {}).get("task") == task), None) or next((a for a in pool if a.get("role") == "qa" and a.get("active")), None)
        else:
            agent = next((a for a in pool if a.get("role") == "cto" and a.get("active")), None)
        session = _session_for(agent, sessions)
        setting = _dict(_dict(settings.get("agent_settings")).get({"builder": "delivery"}.get(role, role)))
        company = company_of(session.get("provider") if session else "", (session or {}).get("vendor") or (agent or {}).get("vendor") or "") or company_of(setting.get("provider"))
        doing, detail = _doing(state, task, role, agent, story, now)
        slowed = ""
        note = notes.get({"builder": "builder", "reviewer": "reviewer"}.get(role, ""), None)
        if note:
            slowed = "What slowed me: " + _short(note.get("text"), 300)
        if role == "cto":
            wakes = sum(int(v or 0) for v in _dict(_dict(_dict(state.get("cto_wakes")).get("by_task")).get(task)).values())
            detail = f"{_plural(wakes, 'wake-up')} this task." if wakes else "Not woken for this task."
        viewable = bool(session and session.get("status") in ("launching", "running") and not session.get("read_only"))
        message = _message(role, agent, session, viewable, state)
        rows.append({
            "role": role, "label": ROLE_LABEL[role], "company": company, "present": bool(agent),
            "agent_id": (agent or {}).get("id", ""), "session_id": (session or {}).get("id", ""),
            "doing": doing, "detail": detail, "slowed": slowed, "cost": _role_cost(cost, task, role),
            "message": message,
            "view": {"enabled": viewable, "session_id": (session or {}).get("id", ""),
                     "note": "" if viewable else _no_view_note(role, agent, session)},
        })
    return rows


def _difficulty_latest(state: dict[str, Any], task: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for note in _dict(state.get("difficulty_notes")).get(task) or []:
        if isinstance(note, dict) and note.get("role") in ("builder", "reviewer") and str(note.get("text") or "").strip():
            out[note["role"]] = note
    return out


def _doing(state: dict[str, Any], task: str, role: str, agent: dict[str, Any] | None, story: dict[str, Any] | None, now: datetime | None) -> tuple[str, str]:
    if not agent:
        return {"builder": "No builder is running.", "reviewer": "No reviewer is running.", "cto": "No CTO is running."}[role], ""
    if role == "builder":
        if agent.get("status") == "paused":
            return "Paused safely.", ""
        if story and story.get("exception") and story["exception"]["role"] == "builder":
            return story["exception"]["reason"], ""
        if agent.get("task") == "AWAITING_OWNER_DIRECTION":
            return "Waiting for your next task.", ""
        note = (story or {}).get("builder_note")
        return (f"Latest note: {note}" if note else ((story or {}).get("now") or "Working.")), ""
    if role == "reviewer":
        if story and story.get("exception") and story["exception"]["role"] == "reviewer":
            return story["exception"]["reason"], ""
        assignment = _reviewer_assignment(state, agent)
        line = _round_line(state, task)
        if assignment and assignment.get("task") == task:
            word = {"claimed": "Testing", "suspended": "Paused on", "reserved": "Preparing its tests for", "authoring": "Ready to start on", "open": "Ready to start on"}.get(str(assignment.get("status")), "Working on")
            return f"{word} {review_scope(assignment)}.", line
        gate_name = (story or {}).get("gate", "")
        if gate_name in ("REPAIR IN PROGRESS", "OWNER REJECTED / REPAIR REQUIRED"):
            return "Waiting for the repair, then the next round.", line
        return "Waiting for the next review.", line
    if story and story.get("exception") and story["exception"]["role"] == "cto":
        return story["exception"]["reason"], ""
    return "Quiet. Wakes only when a judgment is needed.", ""


def _message(role: str, agent: dict[str, Any] | None, session: dict[str, Any] | None, viewable: bool, state: dict[str, Any]) -> dict[str, Any]:
    """What the Message button does. Builder and CTO open the existing composer; the reviewer has no message route, so its button
    is 'Type to the reviewer' and opens its screen (spec 12.8). Each says plainly when it cannot work and what to do instead."""
    if not agent:
        return {"mode": "none", "label": "Message", "note": ""}
    if role == "reviewer":
        if viewable:
            return {"mode": "screen", "label": "Type to the reviewer", "note": ""}
        why = ("The reviewer's window is not available here."
               if session else "The reviewer is not running right now.")
        return {"mode": "none", "label": "Type to the reviewer", "note": why + " Open its terminal window on this computer, or resume the project, then try again."}
    if role == "builder":
        waiting = agent.get("task") == "AWAITING_OWNER_DIRECTION"
        sent = bool(_dict(_dict(state.get("owner_directions")).get(agent.get("session_id"))).get("text"))
        kind = "direction" if waiting and not sent else "clarification"
        return {"mode": "composer", "type": kind, "label": "Message", "note": ""}
    return {"mode": "composer", "type": "cto_message", "label": "Message", "note": ""} if agent.get("active") else {"mode": "none", "label": "Message", "note": ""}


def _no_view_note(role: str, agent: dict[str, Any] | None, session: dict[str, Any] | None) -> str:
    word = ROLE_WORD[role]
    if not agent:
        return f"No {word} is running right now."
    if not session:
        return f"The {word}'s window is not connected. The saved work is kept on the board."
    if session.get("read_only"):
        return f"The {word}'s window is read-only because a replacement has taken over."
    return f"The {word}'s window is not running. Resume the project or start it again from Advanced."


# ---- the whole page ------------------------------------------------------------------------------------------------------------

def _priority(item: dict[str, Any]) -> tuple:
    exc = item.get("exception") or {}
    return (0 if item.get("needs_owner") else 1 if exc else 2 if not item.get("accepted") else 3, -int(item.get("step") or 0))


def _direction_sent(state: dict[str, Any], agents: list[dict[str, Any]] | None) -> bool:
    """The owner has sent a direction and the builder has not turned it into a task yet (a minute or two; 2026-10-28 the page said
    "Nothing is in progress, tell the builder what to build" during it)."""
    directions = _dict(state.get("owner_directions"))
    for agent in agents if agents is not None else list(_dict(state.get("agents")).values()):
        if (agent.get("active") and agent.get("task") == "AWAITING_OWNER_DIRECTION"
                and agent.get("role") in ("engineering", "development", "builder")):
            direction = _dict(directions.get(agent.get("session_id")))
            if direction.get("text") and not direction.get("consumed"):
                return True
    return False


def for_page(state: dict[str, Any], contracts: dict[str, Any] | None, live_tasks: list[str], runtime: dict[str, Any] | None = None, *,
             agents: list[dict[str, Any]] | None = None, sessions: list[dict[str, Any]] | None = None, cost: dict[str, Any] | None = None,
             settings: dict[str, Any] | None = None, now: datetime | None = None, redact: Callable[[Any], Any] = lambda value: value,
             ledgers: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the task page reads: every live task's story, the one to lead with, the project-level exception, and the agent strip."""
    stories = {t: build(state, contracts, t, runtime, agents=agents, sessions=sessions, cost=cost, now=now, redact=redact,
                        ledger=_dict(ledgers).get(t)) for t in live_tasks}
    ranked = sorted(stories.values(), key=_priority)
    lead = ranked[0] if ranked else None
    project_exc = None
    if not lead:
        project_exc = exception_for(state, "", agents=agents, sessions=sessions, redact=redact)
    task = lead["task"] if lead else ""
    return {"current": lead, "direction_sent": not lead and _direction_sent(state, agents), "tasks": stories, "order": [s["task"] for s in ranked], "exception": (lead or {}).get("exception") or project_exc,
            "agents": agent_strip(state, task, agents=agents, sessions=sessions, settings=settings, cost=cost, story=lead, now=now),
            "empty": not stories, "total_steps": TOTAL_STEPS}


# ---- the Projects card ---------------------------------------------------------------------------------------------------------

def project_now(state: dict[str, Any], task: str, *, paused: bool = False, accepted_tasks: set[str] | None = None, awaiting_owner: set[str] | None = None,
                awaiting_requirements: set[str] | None = None, tasks_total: int = 0, owner_cards: list[dict[str, Any]] | None = None,
                sessions: list[dict[str, Any]] | None = None, agents: list[dict[str, Any]] | None = None, cost: dict[str, Any] | None = None,
                runtime: dict[str, Any] | None = None, now: datetime | None = None, redact: Callable[[Any], Any] = lambda value: value) -> dict[str, Any]:
    """The Now card of one project, from the persisted board only (the manager reads closed projects read-only, spec 11).

    The task is the one the manager already picked as the project's latest. {"state": empty|working|needs_you|attention|paused|done,
    "now", "next", "step", "of", "step_title", "elapsed", "cost", "needs_you": {kind, title, detail, action} | None}"""
    accepted_tasks = accepted_tasks or set()
    awaiting_owner = awaiting_owner or set()
    awaiting_requirements = awaiting_requirements or set()
    cards = [c for c in (owner_cards or []) if isinstance(c, dict)]
    open_questions = [q for q in _dict(state.get("agent_questions")).values() if isinstance(q, dict) and q.get("status") == "open"]
    base = {"of": TOTAL_STEPS, "tasks_total": tasks_total, "task": task, "has_task": bool(task), "step": 0, "step_title": "", "elapsed": "",
            "cost": money_line(cost, task)["text"], "usd": None, "codex_window": "",
            "needs_you": None, "exception": None, "progress": 0}
    if not task or task in _dict(state.get("cancelled_tasks")):
        # Nothing has been asked for yet (or the only task was cancelled): one clear way to start.
        if open_questions:
            return {**base, "state": "needs_you", "now": "An agent has a question for you.", "next": "Next: answer it.",
                    "needs_you": {"kind": "question", "title": "An agent has a question for you", "detail": _short(redact(open_questions[0].get("question") or ""), 160), "action": "Open the project and answer it."}}
        exc = exception_for(state, "", agents=agents, sessions=sessions, redact=redact)
        if exc:
            return {**base, "state": "paused" if exc["kind"] == "paused" else "attention", "now": exc["reason"], "next": "Next: " + exc["action"], "exception": exc,
                    "needs_you": _need_from_exception(exc) if exc["needs_owner"] else None, "tell_builder": exc["kind"] != "paused"}
        return {**base, "state": "empty", "now": "No work has been started yet.", "next": "Next: tell the builder what to build.", "tell_builder": True}
    if task in accepted_tasks:
        exc = exception_for(state, "", agents=agents, sessions=sessions, redact=redact, accepted=accepted_tasks) if paused else None
        if exc and exc["kind"] == "paused":
            return {**base, "state": "paused", "now": exc["reason"], "next": "Next: " + exc["action"], "exception": exc}
        return {**base, "state": "done", "now": "Accepted and complete.", "next": "Next: open the project to add new instructions or a new task.",
                "step": TOTAL_STEPS, "step_title": step_title(TOTAL_STEPS), "progress": 100}
    story = build(state, None, task, runtime, agents=agents, sessions=sessions, cost=cost, now=now, redact=redact, detail=False)
    out = {**base, "step": story["step"], "step_title": story["step_title"], "now": story["now"], "next": story["next"], "elapsed": story["elapsed"],
           "cost": story["cost"], "usd": story["usd"], "codex_window": story["codex_window"], "exception": story["exception"], "progress": story["progress"],
           "gate": story["gate"], "state": "working"}
    exc = story["exception"]
    if story["needs_owner"]:
        out["state"] = "needs_you"
        out["needs_you"] = _need_for(story, state, task)
    elif exc:
        out["state"] = "paused" if exc["kind"] == "paused" else "attention"
        if exc["needs_owner"]:
            out["needs_you"] = _need_from_exception(exc)
    elif story["gate"] == "READY FOR YOUR TEST":
        out["state"] = "needs_you"
    if agents is not None and not agents and out["state"] == "working":
        # No agent is running (a closed project): the saved work is kept; opening the project brings the agents back.
        out.update(state="idle", now="Nothing is running right now. Your saved work is kept.", next="Next: open the project; its agents come back with their saved work.")
    if (task in awaiting_requirements or task in awaiting_owner) and not out["needs_you"] and not exc:
        out["state"] = "needs_you"
        out["needs_you"] = _need_for(story, state, task)
    if not out["needs_you"] and open_questions:
        out["needs_you"] = {"kind": "question", "title": "An agent has a question for you", "detail": _short(redact(open_questions[0].get("question") or ""), 160), "action": "Open the project and answer it."}
    if not out["needs_you"] and cards:
        card = cards[0]
        out["needs_you"] = {"kind": "card", "title": _short(redact(card.get("title") or "The CTO needs you to do one thing"), 160), "detail": _short(redact(card.get("why") or ""), 160),
                            "action": "Open the project to see what it needs."}
    return out


def _need_from_exception(exc: dict[str, Any]) -> dict[str, Any]:
    return {"kind": exc["kind"], "title": exc["reason"], "detail": "", "action": exc["action"]}


def _need_for(story: dict[str, Any], state: dict[str, Any], task: str) -> dict[str, Any]:
    if story["gate"] == "READY FOR YOUR TEST":
        return {"kind": "ready", "title": f"{story['title']} is ready for you to try", "detail": ready_summary(state, task, story), "action": "Try it, then press Accept or Not accepted."}
    if story["gate"] == "ACCEPT SAVED, NOT IN MAIN YET":
        return {"kind": "accept_retry", "title": f"{story['title']}: accepted, but not in main yet", "detail": story["next"], "action": story["owner_action"]}
    if story["step"] in (5, 6):
        return {"kind": "go_ahead", "title": f"{story['title']}: your Go ahead is needed", "detail": story["now"], "action": story["owner_action"]}
    return {"kind": "task", "title": f"{story['title']} needs you", "detail": story["now"], "action": story["owner_action"] or "Open the project."}


def ready_summary(state: dict[str, Any], task: str, story: dict[str, Any] | None = None) -> str:
    """'6 requirements had tests written first · passed the independent review by Anthropic Claude · about $4.10 so far'. Only facts the board holds."""
    parts: list[str] = []
    check = _confirmation_check(state, task)
    criteria = {c.get("criterion_index") for c in check.get("checks") or [] if isinstance(c, dict)}
    if criteria:
        parts.append(f"{_plural(len(criteria), 'requirement')} had tests written first")
    final = next((r for r in _dict(state.get("qa_requests")).values() if isinstance(r, dict) and r.get("task") == task and r.get("phase") == "final_acceptance" and r.get("status") == "passed"), None)
    if final:
        agent = _dict(_dict(state.get("agents")).get(str(final.get("claimed_by") or "")))
        company = company_of("", agent.get("vendor") or "")
        parts.append("passed the independent review" + (f" by a reviewer from {company.split(' ')[0]}" if company else ""))
    if story and story.get("usd") is not None:
        parts.append(f"about {cost_center.money(story['usd'])} API-equivalent")
    if story and story.get("elapsed"):
        parts.append(story["elapsed"].replace(" so far", ""))
    return " · ".join(parts)


# ---- the 26 steps as a timeline (slice 2) ---------------------------------------------------------------------------------------
# A DISPLAY (spec 12.1): every row is read from facts the board holds. A step with no matching fact is "Not recorded", never an error and
# never a block. Steps 1-11 and 22-26 are task level; steps 12-21 are one work cycle, repeated for every work area of a larger task
# (spec 12.4). Exceptions are put first by the page from `build()["exception"]`.

PHASES = (
    ("You", "Describe and approve", 1, 6, "Say what you want, answer the reviewer's questions, and approve the requirements in plain words."),
    ("Before any code", "Tests come first", 7, 11, "The reviewer writes the tests. They are locked and must fail on the project as it was."),
    ("Build", "Built and proven", 12, 16, "The builder builds; the board runs the tests itself and records the real results."),
    ("Review", "Reviews that end", 17, 22, "Round 1 lists every real problem; later rounds block only on real ones."),
    ("You", "Try and accept", 23, 26, "Nothing is done until you accept. Not right? Say why and it goes back for repair."),
)

TAGS = {"done": "Done", "now": "Now", "next": "Next", "skipped": "Skipped", "gap": "Not covered", "earlier": "Done for the earlier version",
        "not_recorded": "No separate record"}

# What each step will do, said before it happens (shown on "Next" rows).
WILL: dict[int, str] = {
    1: "You say what you want built.",
    2: "The builder writes your request down as numbered requirements.",
    3: "The reviewer reads the requirements and plans a test for each.",
    4: "The reviewer asks you only what it cannot test without guessing.",
    5: "You pick an answer for each question; a recommended one is already chosen.",
    6: "You read the requirements and press Go ahead. Nothing is built before that.",
    7: "The reviewer writes the tests before any code exists.",
    8: "The board checks that every requirement has a test, including a failure case.",
    9: "The tests are locked so the builder cannot change them.",
    10: "The board runs each test on the project as it was, to see it fail first.",
    11: "If the builder says a test is wrong, the reviewer keeps it or changes it, with a reason.",
    12: "The builder plans the work.",
    13: "The builder builds it and posts short progress notes.",
    14: "The builder runs its own checks.",
    15: "The board runs the locked tests itself and records the real results.",
    16: "The board asks the independent reviewer to look at the work.",
    17: "The reviewer tests the work and lists every real problem at once.",
    18: "The result of round 1: passed, or the problems the builder must fix.",
    19: "The builder fixes what the reviewer found. The tests cannot be changed.",
    20: "A second review. Only real problems can block now; small points become notes.",
    21: "One last retry, with fresh eyes, before the part is reported as not finished.",
    22: "The reviewer tests the whole app once more.",
    23: "The board checks that the version prepared for you is the one that passed, and which changed files a test notices.",
    24: "The board prepares your try-it copy.",
    25: "You try it and press Accept or Not accepted.",
    26: "Your approval is merged into the project's main.",
}


def help_phases() -> list[dict[str, Any]]:
    """The five phases of 'How a task works' with their step titles, for the Help page (one table, no copy of it elsewhere)."""
    return [{"when": when, "title": title, "first": first, "last": last, "text": text,
             "steps": [{"n": n, "title": step_title(n)} for n in range(first, last + 1)]} for when, title, first, last, text in PHASES]


def _ctx(state: dict[str, Any], task: str, facts: dict[str, Any]) -> dict[str, Any]:
    conf = _dict(_dict(state.get("requirement_confirmations")).get(task))
    prop = _dict(_dict(state.get("requirement_proposals")).get(task))
    cc, pcc = _dict(conf.get("contract_check")), _dict(prop.get("contract_check"))
    release = _dict(_dict(state.get("releases")).get(task))
    return {"state": state, "task": task, "facts": facts, "conf": conf, "prop": prop, "cc": cc, "pcc": pcc, "release": release,
            "decision": _dict(_dict(state.get("release_decisions")).get(task)), "reviews": ordered_reviews(facts["reviews"]),
            "plan": facts["plan"], "structure": int(_dict(facts["plan"]).get("structure_revision") or 0)}


def _direction(state: dict[str, Any], task: str) -> str:
    begun = [e for e in state.get("events") or [] if isinstance(e, dict) and e.get("task") == task and e.get("kind") == "task_begun" and e.get("owner_direction")]
    text = begun[0].get("owner_direction") if begun else _dict(state.get("task_owner_directions")).get(task)
    return " ".join(str(text or "").split())


def _company_of_agent(state: dict[str, Any], agent_id: Any) -> str:
    agent = _dict(_dict(state.get("agents")).get(str(agent_id or "")))
    return company_of("", agent.get("vendor") or "")


def _is_final(review: dict[str, Any]) -> bool:
    return review.get("phase") == "final_acceptance"


def _fresh_structure(c: dict[str, Any], review: dict[str, Any]) -> bool:
    return not c["structure"] or int(review.get("structure_revision") or 0) == c["structure"]


def _judged_text(review: dict[str, Any]) -> str:
    """'2 blockers, 3 notes' / 'passed' for one judged review, only from what the review record holds."""
    blockers = review.get("structured_findings")
    decision = review.get("board_decision")
    notes = decision.get("notes") if isinstance(decision, dict) else None
    if isinstance(blockers, list):
        text = _plural(len(blockers), "blocker")
        return text + (f", {_plural(len(notes), 'note')}" if isinstance(notes, list) else "")
    return "passed" if review.get("status") == "passed" else "found problems"


def _early_facts(c: dict[str, Any]) -> dict[int, tuple[str, str, list[str]]]:
    state, task, cc, pcc = c["state"], c["task"], c["cc"], c["pcc"]
    confirmed = bool(c["conf"].get("text"))
    labels = [x for x in cc.get("labels") or [] if isinstance(x, dict)]
    checks = [x for x in cc.get("checks") or [] if isinstance(x, dict)]
    status = str(cc.get("status") or pcc.get("status") or "")
    small = cc.get("lane") == "small" or bool(c["prop"].get("small_change"))
    bypass_reason = (str(cc.get("reason") or "") if cc.get("reason") else "")
    out: dict[int, tuple[str, str, list[str]]] = {}
    said = _direction(state, task)
    out[1] = ("done", f"“{_short(said, 160)}”" if said else "The task was started.",
              ["You described what you want built."] + ([f"In your words: “{_short(said, 400)}”"] if said else []))
    criteria = pcc.get("criteria") if isinstance(pcc.get("criteria"), list) else None
    count = len(labels) or len(criteria or [])
    if c["prop"] or confirmed:
        out[2] = ("done", f"{_plural(count, 'requirement')} written down." if count else "The builder wrote down the requirements.",
                  ["The builder turned your request into numbered requirements in plain words."])
    if _dict(state.get("task_kinds")).get(task) == "analysis":
        why = "a code analysis tests nothing in advance. The builder and the reviewer each read all the code after your Go ahead."
        for n in (3, 4, 5, 7, 8, 9, 10, 11):
            out[n] = ("skipped", "Not needed: " + why, [why[0].upper() + why[1:]])
    elif small:
        why = "small change. Tests are not written in advance; the independent reviewer still reviews the finished work."
        for n in (3, 4, 5, 7, 8, 9, 10, 11):
            out[n] = ("skipped", "Skipped: " + why, [why[0].upper() + why[1:]])
    elif status == "not_checked" or (confirmed and not checks and not labels and not status):
        why = "Skipped: the reviewer did not look at the requirements first" + (f" ({bypass_reason})" if bypass_reason else "") + ". The work is judged after it is built."
        for n in (3, 4, 5, 7, 8, 9, 10, 11):
            out[n] = ("skipped", why, [why])
    elif status in ("ready", "questions", "rechecking") or checks or labels:
        out[3] = ("done", "The reviewer read the requirements first and planned a test for each.", ["The reviewer read the requirements before anything was built."])
    questions = [q for q in (cc.get("questions") or pcc.get("questions") or []) if isinstance(q, dict)]
    answers = [a for a in (cc.get("answers") or pcc.get("answers") or []) if isinstance(a, dict)]
    if 4 not in out and (status or checks or labels):
        if questions or answers:
            n_q = len(questions) or len(answers)
            out[4] = ("done", f"{_plural(n_q, 'question')} for you: " + _short("; ".join(str(q.get("text") or "") for q in (questions or answers)), 200),
                      [f"The reviewer asked {_plural(n_q, 'question')} it could not test without guessing."])
        else:
            out[4] = ("skipped", "Skipped: no questions were needed.", ["The reviewer had nothing to ask you."])
    if 5 not in out and (status or checks or labels):
        if answers:
            out[5] = ("done", f"You answered {_plural(len(answers), 'question')}.",
                      [f"{_short(a.get('text'), 120)} → {_short(a.get('answer'), 120)}" for a in answers])
        elif questions and confirmed:
            out[5] = ("done", "You went ahead with the recommended answers.", ["The recommended answers were used."])
        elif not questions:
            out[5] = ("skipped", "Skipped: there was nothing to answer.", ["No question was put to you."])
    if confirmed:
        when = str(c["conf"].get("confirmed_at") or "")[:16].replace("T", " ")
        out[6] = ("done", f"You said Go ahead{f' on {when}' if when else ''}." + (f" {_plural(count, 'requirement')} agreed." if count else ""),
                  ["The requirements were agreed in your words and recorded. Nothing was built before this."])
    if 7 not in out and checks:
        covered_requirements = {x.get("criterion_index") for x in checks}
        out[7] = ("done", f"{_plural(len(checks), 'test')} written for {_plural(len(covered_requirements), 'requirement')}, before any code.",
                  ["The reviewer, from a different AI company, wrote the tests before the builder started."])
    if 8 not in out and labels:
        covered = [x for x in labels if str(x.get("coverage") or "").startswith("covered")]
        if len(covered) == len(labels):
            out[8] = ("done", f"Every requirement ({len(labels)}) has a test, with a failure case.", ["Each requirement is covered by at least one test that can fail."])
        else:
            missing = [str(x.get("n")) for x in labels if x not in covered]
            out[8] = ("gap", f"Not covered: {_plural(len(missing), 'requirement')} ({', '.join(missing)}). {len(covered)} of {len(labels)} are covered.",
                      ["These requirements have no test with a failure case yet. The reviewer was asked to add them."] +
                      [f"{x.get('n')}. {_short(x.get('plain') or x.get('criterion'), 140)}: {str(x.get('coverage') or '').replace('gap: ', '').replace('not mapped: ', '')}" for x in labels if x not in covered])
    if 9 not in out and cc.get("checks_sha256"):
        out[9] = ("done", "The tests are locked. The builder cannot change them.", ["The board keeps the tests and refuses edits from the builder."])
    red = _dict(cc.get("red_proof"))
    recorded = {n: r for n, r in _dict(red.get("checks")).items() if isinstance(r, dict) and any(str(k.get("n")) == n and k.get("sha256") == r.get("sha256") for k in checks)}
    if 10 not in out and red.get("status") in ("settled", "done"):
        failed = sum(1 for r in recorded.values() if r.get("label") == "fails_before")
        idle = sum(1 for r in recorded.values() if r.get("label") == "passes_on_nothing")
        unclear = sum(1 for r in recorded.values() if r.get("label") == "could_not_compare")
        total = len(recorded)
        if total and failed == total:
            out[10] = ("done", f"Every test failed on the empty project ({failed} of {total}), so none is fake.", ["Each test was run on the project as it was and failed, as a real test should."])
        else:
            parts = [f"{failed} of {total} tests failed first (good)"] + ([f"{idle} did not distinguish"] if idle else []) + ([f"{unclear} could not be compared"] if unclear else [])
            out[10] = ("done", "Tests checked on the empty project: " + ", ".join(parts) + ".", ["Each test was run on the project as it was. A test that passes on nothing proves nothing yet."])
    elif 10 not in out and red.get("status") == "unavailable":
        out[10] = ("not_recorded", "The tests could not be compared with the empty project" + (f": {_short(red.get('reason'), 120)}" if red.get("reason") else "") + ".", ["This says nothing about the work."])
    devs = [d for d in cc.get("deviations") or [] if isinstance(d, dict)]
    if 11 not in out:
        if devs and all(d.get("status") in ("replaced", "kept") for d in devs):
            out[11] = ("done", f"{_plural(len(devs), 'test')} questioned by the builder, now settled.", [_deviation_line(d) for d in devs])
        elif not devs and (status or checks):
            out[11] = ("skipped", "Skipped: the builder did not question any test.", ["Nothing to settle."])
    return out


def _deviation_line(item: dict[str, Any]) -> str:
    answer = "changed the test" if item.get("status") == "replaced" else f"kept it: {item.get('answer_reason') or ''}" if item.get("status") == "kept" else "has not answered yet"
    return f"The builder said test {item.get('check')} did not match reality: {item.get('why')}. The reviewer {answer}."


def _late_facts(c: dict[str, Any], real: bool, rejected: bool) -> dict[int, tuple[str, str, list[str]]]:
    state, task, release, decision = c["state"], c["task"], c["release"], c["decision"]
    out: dict[int, tuple[str, str, list[str]]] = {}
    finals = [r for r in c["reviews"] if _is_final(r)]
    passed = [r for r in finals if r.get("status") == "passed"]
    if passed:
        last = passed[-1]
        company = _company_of_agent(state, last.get("claimed_by"))
        stale = not _fresh_structure(c, last)
        text = ("The reviewer checked the report against the code and it passed" if _dict(state.get("task_kinds")).get(task) == "analysis"
                else "The reviewer tested the whole app and it passed") + (f" (reviewer from {company.split(' ')[0]})" if company else "") + "."
        out[22] = ("earlier" if stale else "done", text if not stale else text + " That was before the work changed shape.",
                   [text] + ([f"Final review: {_plural(len(finals) - 1, 'earlier attempt')} found problems."] if len(finals) > 1 else []))
    release_known = release.get("status") in ("VISUAL_TEST_REQUIRED", "ACCEPTED")
    if release_known:
        proof_row = _dict(_dict(state.get("revert_proofs")).get(task))
        from harness import revert_proof
        revert_line = revert_proof.line(proof_row) if proof_row else ""
        out[23] = ("done", revert_line or "The board checked that the version prepared for you is exactly the one that passed.",
                   ["The board checked that the version prepared for you is the one the reviewer passed."] + ([revert_line] if revert_line else []))
        preview = _dict(release.get("preview"))
        if preview.get("status") == "skipped":
            out[24] = ("skipped", "Skipped: " + (_short(preview.get("reason"), 140) or "no try-it copy was needed") + ".", ["No try-it copy was prepared for this task."])
        elif preview.get("status") == "starting" and preview.get("requested") != "view_app":
            pass
        else:
            out[24] = ("done", "Your try-it copy is ready." if preview.get("status") == "ready" else "Nothing more was needed to prepare it.", ["The board prepared what you try."])
    verdict = decision.get("decision")
    if verdict == "accepted":
        out[25] = ("done", "You accepted it.", ["You tried it and pressed Accept."])
    elif verdict == "not_accepted":
        out[25] = ("done", "You said it is not right: " + (_short(decision.get("reason"), 140) or "your reason is saved") + ".", ["Your reason and files were saved and sent to the builder."])
    if real:
        out[26] = ("done", "Accepted and in main." if task in _dict(state.get("git_acceptances")) else "Accepted.",
                   ["Your approval is recorded" + (" and merged into the project's main." if task in _dict(state.get("git_acceptances")) else ".")])
    if rejected:
        for n in (22, 23, 24, 25):
            if n in out and out[n][0] in ("done", "skipped"):
                out[n] = ("earlier", out[n][1] + " (before your feedback)", out[n][2])
    return out


def _areas(c: dict[str, Any]) -> list[dict[str, Any]]:
    facts, state = c["facts"], c["state"]
    plain = [r for r in c["reviews"] if not _is_final(r)]
    plan, mode = c["plan"], facts["mode"]
    if mode == "application":
        items = [(name, sub) for name, sub in _dict(plan.get("subtasks")).items() if isinstance(sub, dict) and sub.get("status") != "superseded"]
        if items:
            return [{"id": name, "title": _title_case(name), "done": sub.get("status") == "passed", "kind": "work area",
                     "reviews": [r for r in plain if r.get("subtask") == name]} for name, sub in items]
    chunks = [(name, ch) for name, ch in _dict(_dict(state.get("task_chunks")).get(c["task"])).items() if isinstance(ch, dict)]
    if mode == "chunked" and len(chunks) > 1:
        return [{"id": name, "title": _title_case(name), "done": ch.get("status") == "passed", "kind": "part",
                 "reviews": [r for r in plain if r.get("chunk") == name or r.get("subtask") == name]} for name, ch in chunks]
    return [{"id": "", "title": "", "done": False, "kind": "", "reviews": plain}]


def _cycle_facts(c: dict[str, Any], area: dict[str, Any]) -> dict[int, tuple[str, str, list[str]]]:
    state, task = c["state"], c["task"]
    reviews = area["reviews"]
    judged = [r for r in reviews if r.get("status") in ("passed", "failed")]
    by_round: dict[int, dict[str, Any]] = {}
    for position, r in enumerate(judged, 1):
        number = review_round(state, r) if r.get("review_round") else position
        by_round[min(max(number, 1), 3)] = r
    r1, r2, r3 = by_round.get(1), by_round.get(2), by_round.get(3)
    escalated = any(isinstance(e, dict) and e.get("task") == task for e in _dict(state.get("review_escalations")).values())
    scope = area["title"] or "the work"
    out: dict[int, tuple[str, str, list[str]]] = {}
    if c["plan"]:
        mode = c["facts"]["mode"]
        what = (f"The work is split into {_plural(c['facts']['total'], 'work area')}." if mode == "application" and c["facts"]["total"]
                else f"The work is split into {_plural(c['facts']['total'], 'step')}." if mode == "chunked" and c["facts"]["total"] else "The work is one piece.")
        out[12] = ("done", what, ["The builder planned how to build it before starting."])
    elif reviews or area["done"]:
        out[12] = ("skipped", "Skipped: this was one piece of work, so no split plan was recorded.", ["No plan splitting the work was needed or recorded."])
    if reviews or area["done"]:
        out[13] = ("done", f"The builder built {scope if area['title'] else 'it'}.", ["The builder built the work and posted progress notes as it went."])
    if any(r.get("delivery_state") == "passed" or r.get("status") in ("claimed", "passed", "failed") for r in reviews):
        out[14] = ("done", "The builder's own checks passed before the review.", ["The builder ran its own checks and they passed before it asked for a review."])
    if any(r.get("evidence") or r.get("delivery_evidence") or r.get("contract_check_runs") for r in reviews):
        out[15] = ("done", "The board ran the tests itself and recorded the results.", ["The board ran the locked tests on the saved version itself; it does not take the builder's word."])
    if reviews:
        out[16] = ("done", f"A review was requested for {scope}.", ["The board asked the independent reviewer to look at the saved version."])
    if r1:
        out[17] = ("done", f"Round 1: {_judged_text(r1)}.", [f"The reviewer tested {scope} in round 1: {_judged_text(r1)}."])
        if r1.get("status") == "passed":
            out[18] = ("done", "Round 1 passed. Nothing to repair.", ["The reviewer passed it in the first round."])
        else:
            n = len(r1.get("structured_findings") or []) if isinstance(r1.get("structured_findings"), list) else 0
            out[18] = ("done", f"Round 1 found {_plural(n, 'problem')} to fix." if n else "Round 1 found problems to fix.", ["Every real problem was listed at once, with steps to reproduce."])
    if r1 and r1.get("status") == "failed" and (r2 or r3 or any(r.get("status") in ("claimed", "passed") and r is not r1 for r in reviews)):
        out[19] = ("done", "The builder fixed what the reviewer found.", ["The builder repaired the problems. The tests cannot be changed to make a repair pass."])
    elif r1 and r1.get("status") == "passed" and not r2:
        out[19] = ("skipped", "Skipped: round 1 passed, so there was nothing to repair.", ["Nothing to repair."])
    last_pass = bool(judged and judged[-1].get("status") == "passed")
    if r2:
        out[20] = ("done", f"Round 2: {_judged_text(r2)}.", [f"Second review of {scope}: {_judged_text(r2)}."])
    elif r1 and (r1.get("status") == "passed" or last_pass):
        out[20] = ("skipped", "Skipped: no second round was needed.", ["Round 2 was not needed."])
    if r3 or escalated:
        out[21] = ("done", "A last retry was used" + (f": {_judged_text(r3)}." if r3 else "."), ["After repeated failures the part got one last retry with fresh eyes."])
    elif r1 and (r2 or r1.get("status") == "passed") and last_pass:
        out[21] = ("skipped", "Skipped: no last retry was needed.", ["Not needed."])
    return out


def _status_for(n: int, fact: tuple[str, str, list[str]] | None, now_step: int | None, rejected: bool) -> str:
    """The row's status: Now at the current step; otherwise what the facts say; a step with no fact is Next ahead of Now and Not recorded behind it."""
    kind = fact[0] if fact else "unknown"
    if now_step is not None and n == now_step:
        return "now"
    if kind == "earlier":
        return "earlier"
    if kind in ("done", "skipped", "gap"):
        if rejected and now_step is not None and n > now_step and kind == "done" and 20 <= n <= 25:
            return "earlier"
        return kind
    if kind == "not_recorded":
        return "not_recorded"
    return "next" if (now_step is not None and n > now_step) else "not_recorded"


def timeline(state: dict[str, Any], task: str, st: dict[str, Any], facts: dict[str, Any], g: dict[str, Any], *, real: bool) -> dict[str, Any]:
    """The 26 steps as a list of rows in display order (work cycles repeated per work area). Exactly one row is Now unless the task is finished.

    Row: key, n, title, who, status (done/now/next/skipped/gap/earlier/not_recorded), tag, text (one plain line), detail (what happened,
    plain sentences), area (the band it sits in), fold ('' shown, 'skipped' or 'later' collapsed), owner (this Now row needs you)."""
    c = _ctx(state, task, facts)
    cur = int(st["step"])
    status = g["status"]
    rejected = status == "OWNER REJECTED / REPAIR REQUIRED"
    finished = status == "OWNER ACCEPTED" and real
    early, late = _early_facts(c), _late_facts(c, real, rejected)
    areas = _areas(c)
    in_cycle = 12 <= cur <= 21
    current_area = len(areas) - 1
    if in_cycle and len(areas) > 1:
        review = g.get("review") or {}
        located = next((i for i, a in enumerate(areas) if review and (review.get("subtask") == a["id"] or review.get("chunk") == a["id"])), None)
        current_area = located if located is not None else next((i for i, a in enumerate(areas) if not a["done"]), len(areas) - 1)
    elif len(areas) > 1:
        current_area = next((i for i, a in enumerate(areas) if not a["done"]), len(areas) - 1)
    now_step = None if finished else cur
    notes = [st["now"]] + ([st["next"]] if st.get("next") else []) + ([f"Needs you: {st['owner_action']}"] if st.get("needs_owner") and st.get("owner_action") else [])
    if st.get("builder_note"):
        notes.append(f"The builder's latest note: {st['builder_note']}")
    rows: list[dict[str, Any]] = []

    def add(n: int, key: str, fact: tuple[str, str, list[str]] | None, state_now: int | None, area_label: str, title: str | None = None) -> None:
        row_status = _status_for(n, fact, state_now, rejected)
        if row_status == "next" and n == 26 and title == "Accepted":
            text, detail = "Your approval is recorded.", ["Your approval is recorded."]
        elif row_status == "now":
            text, detail = (st["now"], notes + (fact[2] if fact and fact[0] != "unknown" else []))
        elif row_status == "next":
            text, detail = WILL.get(n, ""), [WILL.get(n, "")]
        elif row_status == "not_recorded" and not fact:
            text, detail = ("This step was not tracked on its own for this task.",
                            ["The board keeps no separate record of this step for this task: its work is part of the steps around it, for example a task that goes "
                             "straight to its final review. It does not mean the work was not done or that anything is wrong."])
        else:
            text, detail = (fact[1], fact[2]) if fact else ("", [])
        rows.append({"key": key, "n": n, "title": title or step_title(n), "who": step_who(n), "status": row_status, "tag": TAGS[row_status], "text": text,
                     "detail": [d for d in detail if d], "area": area_label, "fold": "", "owner": bool(row_status == "now" and st.get("needs_owner"))})

    for n in range(1, 12):
        title = None
        if n == 10 and early.get(10) and early[10][1].startswith("Tests checked"):
            title = "Tests checked on the empty project"
        add(n, str(n), early.get(n), now_step, "", title)
    for index, area in enumerate(areas):
        is_current = index == current_area and in_cycle
        area_complete = cur >= 22 or (area["done"] and not is_current)
        label = f"{area['kind'].capitalize()} {index + 1} of {len(areas)}: {area['title']}" if len(areas) > 1 else ""
        facts_here = _cycle_facts(c, area)
        # a finished work area has no Now; the current one has the board's step; one that has not started has only Next (or what the board holds)
        local_now = None if area_complete else cur if is_current else 11
        for n in range(12, 22):
            add(n, f"{index}-{n}", facts_here.get(n), local_now, label)
    for n in range(22, 27):
        governed = bool(_dict(state.get("task_repositories")).get(task)) or task in _dict(state.get("git_acceptances"))
        title = ("Accepted, not in main yet" if status == "ACCEPT SAVED, NOT IN MAIN YET" or (status == "OWNER ACCEPTED" and not real) else None if governed else "Accepted") if n == 26 else None
        add(n, str(n), late.get(n), now_step, "", title)
    # collapse: skipped steps under one line; only the next two steps stay open
    shown_next = 0
    for row in rows:
        if row["status"] == "skipped":
            row["fold"] = "skipped"
        elif row["status"] == "next":
            shown_next += 1
            if shown_next > 2:
                row["fold"] = "later"
    counts = {"done": sum(1 for r in rows if r["status"] in ("done", "earlier")), "skipped": sum(1 for r in rows if r["fold"] == "skipped"),
              "later": sum(1 for r in rows if r["fold"] == "later")}
    return {"steps": rows, "counts": counts, "earlier": rejected, "areas": len(areas) if len(areas) > 1 else 0, "finished": finished,
            "now_step": None if finished else cur}


# ---- the Proof tab (slice 2) ---------------------------------------------------------------------------------------------------

RED_WORDS = {"fails_before": ("Failed before the work (good)", "good"), "passes_on_nothing": ("Did not distinguish: it passed on nothing", "bad"),
             "could_not_compare": ("Could not compare with the project before the work", "none")}


def _candidate(c: dict[str, Any]) -> dict[str, Any] | None:
    saved = [r for r in c["reviews"] if r.get("reviewed_commit")]
    return saved[-1] if saved else None


def proof(state: dict[str, Any], task: str, facts: dict[str, Any], ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    """The Proof tab: per requirement its tests, whether it is covered, what the test did on the empty project, and what it does now.

    'Passing now' is claimed only when the board ran THIS version's tests: the newest saved version's commit and the locked checks' hash must
    equal a recorded run (spec 12.5); otherwise 'Not run on this version'. Nothing here reads a test result from an earlier version as current."""
    from harness import revert_proof
    c = _ctx(state, task, facts)
    cc = c["cc"]
    labels = [x for x in cc.get("labels") or [] if isinstance(x, dict)]
    checks = [x for x in cc.get("checks") or [] if isinstance(x, dict)]
    sha = str(cc.get("checks_sha256") or "")
    cand = _candidate(c)
    rejected = c["decision"].get("decision") == "not_accepted"
    commit = str((cand or {}).get("reviewed_commit") or "")
    after_feedback = bool(rejected and cand and str(cand.get("requested_at") or "") <= str(c["decision"].get("recorded_at") or ""))
    stale_scope = bool(cand and not _fresh_structure(c, cand))
    earlier = after_feedback or stale_scope
    runs = [run for run in (cand or {}).get("contract_check_runs") or [] if isinstance(run, dict)]
    fresh = bool(cand and sha and commit and not earlier and any(run.get("commit") == commit and run.get("checks_sha256") == sha for run in runs))
    results = _dict(cc.get("results"))
    compare = _dict(cc.get("compare"))
    red = _dict(cc.get("red_proof"))
    current_checks = {str(k.get("n")): k.get("sha256") for k in checks}
    recorded = {n: r for n, r in _dict(red.get("checks")).items() if isinstance(r, dict) and current_checks.get(n) == r.get("sha256")}
    rows: list[dict[str, Any]] = []
    passing = 0
    for item in labels:
        number = item.get("n")
        mine = [k for k in checks if k.get("criterion_index") == number]
        coverage = str(item.get("coverage") or "")
        if coverage.startswith("covered"):
            covered = {"text": "Covered", "ok": True}
        elif coverage.startswith("gap"):
            covered = {"text": "Not covered: " + coverage.replace("gap: ", ""), "ok": False}
        else:
            covered = {"text": "Not covered: no test is linked to this requirement yet", "ok": False}
        word, tone = RED_WORDS.get(str(compare.get(str(number)) or ""), ("Not compared", "none"))
        outcome = results.get(str(number))
        if not mine:
            now = {"text": "No test is tied to this requirement" + (f" (skipped: {_short(item.get('note'), 100)})" if item.get("note") else ""), "tone": "none"}
        elif earlier:
            now = {"text": "From the earlier version, not current", "tone": "none"}
        elif outcome == "passed" and fresh:
            now, passing = {"text": "Passing now", "tone": "good"}, passing + 1
        elif outcome == "passed":
            now = {"text": "Not run on this version", "tone": "none"}
        elif outcome == "failed":
            now = {"text": "Failed when the board ran it", "tone": "bad"}
        elif outcome == "did_not_run":
            now = {"text": "The board could not run it (no failure of the work)", "tone": "none"}
        else:
            now = {"text": "Not run yet", "tone": "none"}
        rows.append({"n": number, "requirement": _short(item.get("plain") or item.get("criterion"), 220), "quality": item.get("kind") == "non-functional",
                     "tests": len(mine), "covered": covered, "before": {"text": word, "tone": tone}, "now": now})
    covered_count = sum(1 for r in rows if r["covered"]["ok"])
    failed_first = sum(1 for r in recorded.values() if r.get("label") == "fails_before")
    idle = sum(1 for r in recorded.values() if r.get("label") == "passes_on_nothing")
    unclear = sum(1 for r in recorded.values() if r.get("label") == "could_not_compare")
    total_tests = len(checks)
    tested_rows = sum(1 for r in rows if r["tests"])
    revert = _dict(_dict(state.get("revert_proofs")).get(task))
    files = [f for f in revert.get("files") or [] if isinstance(f, dict)]
    revert_fresh = bool(revert and files and commit and revert.get("commit") == commit and revert.get("checks_sha256") == sha and not earlier)
    noticed = sum(1 for f in files if f.get("outcome") == revert_proof.NOTICED)
    measured = noticed + sum(1 for f in files if f.get("outcome") == revert_proof.NOT_NOTICED)
    unchecked = sum(1 for f in files if f.get("outcome") in (revert_proof.SKIPPED, revert_proof.OVER_LIMIT, revert_proof.COULD_NOT_CHECK))
    if not files:
        revert_tile = {"label": "Changed files a test notices", "value": "Not measured", "note": "The revert check runs at the final review."}
    elif not revert_fresh:
        revert_tile = {"label": "Changed files a test notices", "value": "Not run on this version", "note": "The last revert check was of an earlier version."}
    else:
        revert_tile = {"label": "Changed files a test notices", "value": f"{noticed} of {measured} noticed",
                       "note": (f"{unchecked} not checked. " if unchecked else "") + (revert_proof.line(revert) or "")}
    untied = None
    if isinstance(ledger, dict):
        sections = [s for s in (ledger.get("delivery"), ledger.get("reviewer")) if isinstance(s, dict)] or [ledger]
        scenarios = [x for s in sections for x in (s.get("scenarios") or []) if isinstance(x, dict)]
        if scenarios:
            ok = sum(1 for x in scenarios if x.get("status") == "passed")
            untied = {"title": "Not tied to a requirement",
                      "text": f"{_plural(len(scenarios), 'other check')} in the latest review record (the builder's and the reviewer's own checks) are not linked to a single requirement, so they are not counted above. {ok} of them passed when last run."}
    devs = [d for d in cc.get("deviations") or [] if isinstance(d, dict)]
    from harness import code_quality
    notes = code_quality.notes(state, task)
    if rows:
        header = (f"{covered_count} of {len(rows)} requirements have a test written before any code. "
                  + (f"{failed_first} of {total_tests} tests were seen failing on the project before the work, as they should. " if recorded else "")
                  + (f"On this version {passing} of {tested_rows} tested requirements pass when the board ran the tests itself." if fresh
                     else "The board has not run these tests on this exact version yet." if not earlier else "These results are from the version before your feedback."))
    else:
        why = str(cc.get("reason") or "")
        header = "No tests were written in advance for this task" + (f" ({why})" if why else "") + ". The finished work is judged by the independent reviewer after it is built."
    tiles = [{"label": "Requirements covered", "value": f"{covered_count} of {len(rows)}" if rows else "None", "note": "" if covered_count == len(rows) else "Some have no test with a failure case yet."},
             {"label": "Tests that failed first", "value": f"{failed_first} of {total_tests}" if recorded else "Not recorded",
              "note": ", ".join(([f"{idle} did not distinguish"] if idle else []) + ([f"{unclear} could not be compared"] if unclear else []))},
             {"label": "Passing now", "value": f"{passing} of {tested_rows}" if fresh else "Not run on this version" if rows else "None",
              "note": "requirements, when the board ran this version" if fresh else ""},
             revert_tile]
    requirements_tile = {"text": f"{passing} / {len(rows)} proven" if rows and fresh else "", "passing": passing, "total": len(rows), "fresh": fresh}
    return {"rows": rows, "tiles": tiles, "header": header, "earlier": earlier, "fresh": fresh, "untied": untied, "requirements_tile": requirements_tile,
            "questioned": [_deviation_line(d) for d in devs], "quality": notes, "has_tests": bool(rows),
            "earlier_note": "These results are from the version before your feedback. They are not shown as current. New results appear after the next run." if earlier else ""}

"""Recorded failures in plain words (item 24).

The board records why a test attempt failed as raw technical text (a Python
error, a command's last 500 characters).  The owner is not an engineer.  This
module turns each recorded failure into: a count, ONE plain sentence, and the
full technical text for a Details toggle.

Rules:
- A cause is named (program / harness / test machine) ONLY when the recorded
  text proves it.  Anything else gets an honest generic sentence that quotes
  the first sentence of the recorded reason; nothing is invented.
- A failure older than a later PASS for the same task and scope is history,
  not a current problem, and is not shown.
- Pure functions over the board state; nothing here writes to the board.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

DETAIL_LIMIT = 5          # newest failures whose full text is kept for Details
TEXT_LIMIT = 1200         # per recorded failure, after redaction

# (pattern, blame, builder).  The first match wins.  blame is "program", "harness",
# "machine" only when the text itself proves it, otherwise "".
_KNOWN: list[tuple[re.Pattern[str], str, Callable[[re.Match[str]], str]]] = [
    # Blamed on the harness only when this is the WHOLE recorded reason: an exception raised in the harness's own evidence step.
    # The same words inside a command's output ("failed with exit code 1: ...") came from the program or its tests.
    (re.compile(r"\A\s*(?:\w+Error: )?['\"]?[\w.-]+['\"]? codec can.t (?:de|en)code", re.I), "harness",
     lambda m: "The harness could not read what the test printed (a special character, such as the degree sign on Windows). "
               "That is a fault in the harness, not in your program, and the run could not be certified."),
    (re.compile(r"timed out after (\d+) seconds", re.I), "",
     lambda m: f"The test did not finish within {m.group(1)} seconds and was stopped, so its result was thrown away."),
    (re.compile(r"zero executed tests", re.I), "",
     lambda m: "The test command ran but did not run any test, so it proved nothing."),
    (re.compile(r"test machine changed or needed the owner.s password", re.I), "machine",
     lambda m: "The test machine changed (or asked for your password) while the test was running, so the result was thrown away."),
    (re.compile(r"connection (?:refused|timed out|reset)|no route to host|could not resolve|network is unreachable|"
                r"ssh: connect|did not finish in time|permission denied \(publickey", re.I), "machine",
     lambda m: "The test machine could not be reached while the test was running."),
    (re.compile(r"heartbeat expired|execution was interrupted", re.I), "",
     lambda m: "The test run was cut off before it finished and has to be run again."),
    (re.compile(r"scenario ledger (?:changed|disappeared)", re.I), "",
     lambda m: "A file the test depends on changed while the test was running, so the result was thrown away."),
    (re.compile(r"failed with exit code (-?\d+)", re.I), "",
     lambda m: f"The test ran and did not pass (it ended with exit code {m.group(1)})."),
]

_HARNESS_REASON = re.compile(r"\A\s*(?:\w+Error: )?['\"]?[\w.-]+['\"]? codec can.t (?:de|en)code|\btraceback \(most recent|internal error", re.I)


def first_sentence(text: Any, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    match = re.search(r"(?<=[.!?])\s", text)
    sentence = text[:match.start()] if match else text
    if len(sentence) > limit:
        sentence = sentence[:limit - 1].rstrip() + "…"
    return sentence


def describe(text: Any, redact: Callable[[Any], str] | None = None) -> dict[str, str]:
    """One recorded failure -> {sentence, blame, technical}."""
    clean = (redact or str)(text or "")[:TEXT_LIMIT]
    for pattern, blame, build in _KNOWN:
        found = pattern.search(clean)
        if found:
            return {"sentence": build(found), "blame": blame, "technical": clean}
    quoted = first_sentence(clean)
    sentence = "The test run stopped before finishing" + (f": {quoted}" if quoted else ".")
    if not sentence.endswith((".", "…", "!", "?")):
        sentence += "."
    return {"sentence": sentence, "blame": "", "technical": clean}


def _when(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed


def _scope(request: dict[str, Any]) -> tuple[str, str, str, str]:
    return tuple(str(request.get(key) or "") for key in ("task", "phase", "chunk", "subtask"))  # type: ignore[return-value]


def _passes(state: dict[str, Any], independent: bool) -> dict[tuple[str, str, str, str], datetime]:
    """Latest success time per (task, phase, chunk, subtask).

    independent=True: only an independent reviewer PASS (status passed) counts; it clears reviewer failures.
    independent=False: Delivery's own success also counts (delivery_state passed, dated from when that request was made,
    which is never later than its success); it clears only Delivery's failures. Delivery passing is not a review pass.
    """
    latest: dict[tuple[str, str, str, str], datetime] = {}
    for table in ("qa_requests", "qa_request_index"):
        for request in (state.get(table) or {}).values():
            if not isinstance(request, dict):
                continue
            if request.get("status") == "passed":
                at = _when(request.get("completed_at") or request.get("requested_at"))
            elif not independent and request.get("delivery_state") == "passed":
                at = _when(request.get("requested_at"))
            else:
                continue
            if at is not None and (latest.get(_scope(request)) is None or at > latest[_scope(request)]):
                latest[_scope(request)] = at
    return latest


def _newer_than_pass(scope: tuple[str, str, str, str], at: datetime | None, passes: dict) -> bool:
    """True when no later PASS exists for the scope (a failure with no timestamp stays visible)."""
    passed = passes.get(scope)
    return passed is None or at is None or at >= passed


def summarize(items: list[dict[str, Any]], redact: Callable[[Any], str] | None = None) -> dict[str, Any] | None:
    """items: [{"text", "at"}] oldest first -> the card summary, or None when there is nothing to show."""
    if not items:
        return None
    described = [{**describe(item["text"], redact), "at": str(item.get("at") or "")} for item in items]
    latest = described[-1]
    count = len(described)
    return {
        "count": count,
        "sentence": latest["sentence"],
        "blame": latest["blame"],
        "latest_at": latest["at"],
        "headline": f"Test failed {count} time{'' if count == 1 else 's'}: {latest['sentence']}",
        "details": list(reversed(described[-DETAIL_LIMIT:])),
        "details_note": f"Newest {min(count, DETAIL_LIMIT)} of {count} recorded",
    }


def failures_for_state(
    state: dict[str, Any], tasks: list[str] | set[str], redact: Callable[[Any], str] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Current failure summaries per live task: {"delivery": {task: summary}, "reviewer": {task: summary}}."""
    wanted = set(tasks)
    delivery_passes, review_passes = _passes(state, False), _passes(state, True)
    delivery: dict[str, list[dict[str, Any]]] = {}
    for failure in state.get("delivery_attempt_failures") or []:
        if not isinstance(failure, dict) or str(failure.get("task") or "") not in wanted:
            continue
        at = failure.get("completed_at")
        if not _newer_than_pass(_scope(failure), _when(at), delivery_passes):
            continue
        delivery.setdefault(str(failure["task"]), []).append(
            {"text": failure.get("result_summary") or "", "at": at})
    reviewer: dict[str, list[dict[str, Any]]] = {}
    delivery_ids = {str(f.get("id")) for f in (state.get("delivery_attempt_failures") or []) if isinstance(f, dict)}
    seen: set[str] = set()
    for table in ("qa_requests", "qa_request_index"):
        for key, request in (state.get(table) or {}).items():
            if not isinstance(request, dict) or str(request.get("task") or "") not in wanted or key in seen:
                continue
            seen.add(key)
            task = str(request["task"])
            attempts = [a for a in (request.get("challenge_execution_attempts") or []) if isinstance(a, dict)]
            last_pass = max((_when(a.get("recorded_at")) for a in attempts if a.get("status") == "passed"
                             and _when(a.get("recorded_at"))), default=None)
            for attempt in attempts:
                if attempt.get("status") != "failed":
                    continue
                at = _when(attempt.get("recorded_at"))
                if (last_pass and at and at < last_pass) or not _newer_than_pass(_scope(request), at, review_passes):
                    continue
                reviewer.setdefault(task, []).append(
                    {"text": attempt.get("reason") or "", "at": attempt.get("recorded_at")})
            # a review that failed outright (not one Delivery's own run cancelled: those are Delivery's failures above)
            if (request.get("status") == "failed" or request.get("result") == "failed") \
                    and not str(request.get("route_state") or "").startswith("delivery_") \
                    and str(request.get("id") or key) not in delivery_ids:
                at = request.get("completed_at")
                if _newer_than_pass(_scope(request), _when(at), review_passes):
                    reviewer.setdefault(task, []).append(
                        {"text": request.get("result_summary") or "", "at": at})

    def finish(rows: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
        out = {}
        for task, items in rows.items():
            items.sort(key=lambda row: str(row.get("at") or ""))
            summary = summarize(items, redact)
            if summary:
                out[task] = summary
        return out

    return {"delivery": finish(delivery), "reviewer": finish(reviewer)}


def hold_words(source: Any, reason: Any, redact: Callable[[Any], str] | None = None) -> dict[str, Any]:
    """A control-plane hold -> {sentence, harness_defect}.  The reason is usually 'The same operation was refused N times: <text>'."""
    text = (redact or str)(reason or "")
    inner = re.sub(r"^The same operation was refused \d+ times:\s*", "", text.strip())
    known = describe(inner, redact)
    refused = text.strip().startswith("The same operation was refused")
    defect = bool(_HARNESS_REASON.search(text)) or known["blame"] == "harness"
    if known["blame"] or not known["sentence"].startswith("The test run stopped before finishing"):
        sentence = known["sentence"]
    else:
        quoted = first_sentence(inner)
        sentence = ("The harness stopped this task after the same step was refused several times" if refused
                    else "The harness stopped this task to protect your work")
        sentence += f": {quoted}." if quoted else "."
    return {"sentence": sentence, "harness_defect": defect}


def hold_card(task: str, source: Any, reason: Any, clear_command: str, redact: Callable[[Any], str] | None = None) -> dict[str, str]:
    """The owner card for a control-plane hold: what is stuck, why, what the owner can do (title, why, command)."""
    words = hold_words(source, reason, redact)
    why = (
        f"What is stuck: the task {task}. The harness has put it on hold. {words['sentence']} "
        "Your saved work and every check that already passed are kept. "
        "What you can do: wait (nothing is lost by waiting), or cancel the task from its card in Mission Control, "
        "or clear the hold by running the command shown here in Terminal. A paused project must be resumed first. "
        "Clearing the hold lets the task move again but does not repair its cause, so the same hold can come back."
    )
    if words["harness_defect"]:
        why += " This is a fault in the harness, not in your project, and the harness team is on it (the fix arrives in a harness update)."
    return {"title": f"{task} is stuck and on hold", "why": why, "command": clear_command}

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Review rounds inside a project end: the agreed scope is frozen at round 1 (task 2026-10-25_frozen-review-scope).

Pure helpers over board state and request dicts (the board holds the locks and the writes):
- `judge` decides what a Reviewer's verdict means from round 2 on: a blocker is accepted only as (a) a round-1 finding still
  not fixed, (b) broken by the repair, (c) an agreed requirement materially unmet, (d) safety, data loss or a leak. A malformed
  finding is a correction request (ValueError with the exact fix), never a silent downgrade.
- `record_judgment` keeps one durable escalation record per scope lineage: after the round limit, one fresh Delivery attempt,
  then an honest "could not finish". The owner is never asked.
"""
from __future__ import annotations

import os
import re
from typing import Any

INDEPENDENT = "independent_review"
HARM_TAGS = ("safety", "data-loss", "leak")
LETTERS = ("a", "b", "c", "d", "new")
RECOVERY_PHASES = ("requested", "stopping", "creating", "started")
_SUPPORT_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,79}")
END_TO_END_RULE_FROM = "2026-09-30"   # same date as cto.END_TO_END_RULE_FROM
_DECLARATION = re.compile(r"^\s*TASK DONE:\s*(?P<what>[^\n]*)", re.I | re.M)
FINDING_EXAMPLE = (
    '{"id": "B1", "summary": "what is wrong", "harm": "in normal use the owner would see or lose ...", '
    '"harm_tag": "none", "regression_check": "the command or steps that show it"}'
)
# D-023 (owner 2026-11-21, task 2026-11-21_not-run-goes-to-repair): an agreed requirement the Reviewer could not really run is
# neither a pass nor a block. It is a FAIL whose finding starts "Not run for real:" and passes the (c) checks (a confirmed,
# unwaived requirement and current evidence), with who can make it run (fix_by). A "Process note:" line (a limit of the process
# not tied to an agreed requirement, D-017) is never one of these lines.
NOT_RUN_PREFIX = "Not run for real:"
FIX_BY = ("builder", "cto")
_NOT_RUN_LINE = re.compile(r"^[\s>*+\-•#`_\d.)]*not\s+run\b(?:\s+for\s+real\b|[\s*_`]*:)", re.I)
NOT_RUN_EXAMPLE = ('{"id": "B1", "summary": "Not run for real: <what you tried and what refused>", "criterion": "<requirement number or '
                   'verbatim quote>", "evidence_ref": "<the command and its refusal>", "harm": "the owner gets an app nobody ran for '
                   'this requirement", "regression_check": "<how to reproduce>", "fix_by": "builder|cto", "letter": "c"}')


def not_run_lines(summary: Any) -> list[str]:
    """Every line of a verdict that says a step was not run (`Not run:`, `NOT RUN for real`, `Not run for real:`), whatever its
    case, bullet or indent; `Process note:` lines are not among them."""
    return [line.strip() for line in str(summary or "").splitlines() if _NOT_RUN_LINE.match(line)]


def is_not_run_finding(member: Any) -> bool:
    """The finding is an agreed requirement the Reviewer could not really run (its summary starts "Not run for real")."""
    return isinstance(member, dict) and bool(re.match(r"\s*not\s+run\s+for\s+real\b", str(member.get("summary") or ""), re.I))


SCENARIO_NOT_RUN = re.compile(r"(?im)^[\s>*+\-•#`_\d.)]*not\s+run\s+for\s+real\s*:\s*real\s+scenario\b")
SCENARIO_JUDGED = "Scenario result judged:"


def is_scenario_not_run(member: Any) -> bool:
    """The finding says the owner's real scenario was not run for real: the one not-run line that never becomes a PASS (task
    2026-11-23_real-scenario-run; the app's main AI path is never "not tested for real")."""
    return isinstance(member, dict) and bool(SCENARIO_NOT_RUN.match(str(member.get("summary") or "")))


def _env_number(name: str, default: int, minimum: int) -> int:
    """Read an integer environment variable named name, clamped to at least minimum.

    Return: the parsed value, or default when the variable is unset, unparsable,
    or below minimum.
    """
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def round_limit() -> int:
    """Failed judgments of one scope before the one fresh attempt (default 3; HARNESS_REVIEW_ROUND_LIMIT)."""
    return _env_number("HARNESS_REVIEW_ROUND_LIMIT", 3, 2)


# A Code analysis is a report: the owner expects two review rounds, not three (2026-10-28). Development keeps round_limit().
ANALYSIS_ROUND_LIMIT = 2


def limit_for(kind: str) -> int:
    """Return the round limit that applies for task kind: analysis gets the lower cap."""
    return min(ANALYSIS_ROUND_LIMIT, round_limit()) if kind == "analysis" else round_limit()


def recovery_attempts() -> int:
    """Fresh Delivery attempts allowed per scope (default 1; HARNESS_REVIEW_RECOVERY_ATTEMPTS, 0 switches the attempt off)."""
    return _env_number("HARNESS_REVIEW_RECOVERY_ATTEMPTS", 1, 0)


def reviewer_retry_attempts() -> int:
    """Fresh Reviewer attempts allowed per scope when a review cannot be completed (default 1; HARNESS_REVIEW_REVIEWER_RETRY_ATTEMPTS, 0 switches it off)."""
    return _env_number("HARNESS_REVIEW_REVIEWER_RETRY_ATTEMPTS", 1, 0)


def recovery_wait_seconds() -> int:
    """How long a started fresh attempt may go without any review judgment (paused time excluded)."""
    return _env_number("HARNESS_REVIEW_RECOVERY_WAIT_SECONDS", 7200, 60)


def lineage_key(request: dict[str, Any]) -> str:
    """Return the scope-lineage key for request: its task, phase, subtask, chunk, and structure revision joined."""
    return "|".join(str(request.get(key) or "") for key in ("task", "phase", "subtask", "chunk")) + "|" + str(int(request.get("structure_revision", 0) or 0))


def prior_judgments(requests: list[dict[str, Any]], request: dict[str, Any]) -> list[dict[str, Any]]:
    """Completed reviews of the same scope lineage before this request, since the last PASS, that this code judged.

    Adoption boundary: a request without `review_round` was recorded before the frozen-scope rule and is history; it is
    neither a baseline nor a failure to count."""
    key, cycle = lineage_key(request), int(request.get("cycle", 0) or 0)
    rows = sorted(
        (r for r in requests if r.get("stage") == INDEPENDENT and lineage_key(r) == key and r.get("id") != request.get("id")
         and int(r.get("cycle", 0) or 0) < cycle and r.get("status") in {"passed", "failed"}),
        key=lambda r: int(r.get("cycle", 0) or 0))
    for index in range(len(rows) - 1, -1, -1):
        if rows[index]["status"] == "passed":
            rows = rows[index + 1:]
            break
    return [r for r in rows if r.get("review_round")]


def baseline(prior: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first failed judgment of the lineage that carries structured findings."""
    for position, row in enumerate(prior):
        if row.get("status") == "failed" and row.get("structured_findings"):
            return {"request_id": row.get("id", ""), "position": position,
                    "findings": {str(f["id"]): f for f in row["structured_findings"]}}
    return None


def round_number(prior: list[dict[str, Any]], base: dict[str, Any] | None) -> int:
    """Return the current review round number, counting failures of prior since base.

    When base is falsy, this is round 1; otherwise it is one plus the failed
    judgments in prior from the base's position onward.
    """
    if not base:
        return 1
    return 1 + sum(1 for row in prior[base["position"]:] if row.get("status") == "failed")


def declaration(summary: Any) -> str:
    """Extract the text after 'TASK DONE:' from summary.

    Return: the declared text, or "" when summary has no such declaration.
    """
    found = _DECLARATION.search(str(summary or ""))
    return found.group("what").strip() if found else ""


def is_testing_gap(summary: Any) -> bool:
    """The verdict says the work was NOT TESTED / not reproducible: the review could not be completed, which is not a finding about the work."""
    return declaration(summary).lower().startswith(("not tested", "not reproducible"))


# A testing gap whose cause is the Reviewer's own environment (its isolated runner lacks the project's libraries or its test database), not
# the work: a stronger model cannot fix that, only the project's test environment can (2026-10-28, Acremuse: round 4 ran on a stronger
# model to re-check a race it could not reach the database for).
_ENVIRONMENT_GAP = re.compile(
    r"sandbox|isolated (?:runner|check|environment)|environment limit|reviewer(?:'s)? (?:own )?environment|no module named|not installed|cannot import|"
    r"could not import|missing (?:librar|depend|package)|sqlmodel|test database|database url|db url|cannot connect|could not connect|connection (?:refused|failed)", re.I)


def is_environment_gap(summary: Any) -> bool:
    """Return whether summary is a testing gap caused by the reviewer's own environment."""
    return is_testing_gap(summary) and bool(_ENVIRONMENT_GAP.search(str(summary or "")))


ENVIRONMENT_GAP_MESSAGE = ("{task}: the review could not run one check because the reviewer's own environment lacks what the project's tests need "
                           "(its libraries or its test database). That is not a finding about the work and a stronger model would not help, so none is started. "
                           "Delivery prepares the project's test environment (a virtual environment outside the code, recorded with test-env-set); then it asks for the review again.")


def says_not_done(summary: Any) -> bool:
    """The verdict's own TASK DONE line says the task is not done."""
    return bool(re.match(r"(no|not)\b", declaration(str(summary or "")).lower()))


def _first_line_says_yes(summary: Any) -> bool:
    """Return whether summary's first non-blank line declares TASK DONE: YES."""
    text = str(summary or "").lstrip(" \t\r\n>*-#`\"'")
    return text.upper().startswith("TASK DONE: YES")


def _words(text: Any) -> str:
    """Return text lower-cased with runs of whitespace collapsed to single spaces."""
    return " ".join(str(text or "").lower().split())


def _correction(problem: str, fix: str) -> ValueError:
    """Build a ValueError describing problem and the fix for it.

    Return: the ValueError, left for the caller to raise.
    """
    return ValueError(f"the review verdict was not recorded: {problem}. To fix it: {fix}")


def _tag(raw: dict[str, Any]) -> str:
    """raw's harm_tag ("" for none). A word close to one of the three counts as it: a safety, data-loss or leak finding is never lost to spelling."""
    tag = str(raw.get("harm_tag") or "").strip().lower().replace("_", "-").replace(" ", "-")
    if tag in HARM_TAGS:
        return tag
    return next((known for known, words in (("data-loss", ("data", "loss")), ("leak", ("leak", "secret", "privacy")), ("safety", ("safe", "security")))
                 if any(word in tag for word in words)), "")


def _paired_ok(paired: Any, context: dict[str, Any], request: dict[str, Any]) -> bool:
    """Check that paired names a valid earlier-failing / now-passing evidence pair.

    paired must carry command, earlier_commit, earlier_result, repaired_commit, and
    repaired_result; context supplies the set of known commits and request gives the
    reviewed commit the repair is checked against. Return: whether the pair is valid.
    """
    if not isinstance(paired, dict):
        return False
    need = ("command", "earlier_commit", "earlier_result", "repaired_commit", "repaired_result")
    if any(not str(paired.get(key) or "").strip() for key in need):
        return False
    return (str(paired["earlier_commit"]) in context.get("commits", set())
            and str(paired["repaired_commit"]) == str(request.get("reviewed_commit") or "")
            and "pass" in str(paired["earlier_result"]).lower() and "fail" in str(paired["repaired_result"]).lower())


def _waived(raw: dict[str, Any], text: str, context: dict[str, Any]) -> bool:
    """The finding is about a deliverable the owner waived."""
    quoted = _words(str(raw.get("criterion") or ""))
    return any(_words(w.get("deliverable")) and (_words(w.get("deliverable")) in quoted or _words(w.get("deliverable")) in _words(text))
               for w in context.get("waivers", []))


def _normalize(raw: Any, number: int, base: dict[str, Any] | None, context: dict[str, Any], request: dict[str, Any], summary: str) -> dict[str, Any]:
    """One submitted finding in the form the board keeps. Owner 2026-11-24: its form never stops the verdict; what is missing is said to be
    missing, and a possible bug is never dropped for its wording.

    raw is the finding's JSON (number its position); base is the round-1 baseline, if any; context carries the round number plus the known
    passing checks, criteria, requirements text and waivers; summary is the reviewer's full verdict text. From round 2 the letter decides
    what may block (review rounds end, owner 2026-10-25): a letter that does not hold makes the finding a note (new)."""
    if not isinstance(raw, dict):
        raw = {"summary": str(raw)}
    tag = _tag(raw)
    identifier = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw.get("id") or "")).strip("-._")[:64] or f"B{number}"
    member = dict(raw)
    member["id"] = identifier
    member["harm_tag"] = tag
    if not str(raw.get("category") or "").strip():
        member["category"] = {"safety": "security", "leak": "security", "data-loss": "data_integrity"}.get(tag, "behavior")
    text = " ".join(str(raw.get("summary") or "").split()) or " ".join(summary.split())[:300] or "the reviewer did not describe it"
    member["summary"] = text
    member["harm"] = str(raw.get("harm") or "").strip() or (text if tag else "the reviewer did not say what the owner would see")
    member["regression_check"] = str(raw.get("regression_check") or "").strip() or f"Re-run the reviewer's reproduction: {text}"[:500]
    evidence = str(raw.get("evidence_ref") or "").strip()
    member["evidence_ref"] = evidence or "see the verdict summary"
    if tag:
        member["letter"] = "d" if base else ""       # a safety, data-loss or leak finding always blocks
        return member
    if is_not_run_finding(raw):
        # an agreed requirement nobody could really run: who can make it run defaults to the CTO, which routes it
        fix_by = str(raw.get("fix_by") or "").strip().lower()
        member["fix_by"] = fix_by if fix_by in FIX_BY else "cto"
        member["criterion"] = str(raw.get("criterion") or "").strip() or text
        member["letter"] = ("new" if _waived(raw, text, context) else "a" if identifier in base["findings"] else "c") if base else ""
        return member
    if not base:
        member["letter"] = ""
        return member
    letter = str(raw.get("letter") or "").strip().lower().strip("()")
    if identifier in base["findings"]:
        letter = "a"
    elif letter == "b" or not letter:
        references = str(raw.get("refers_to") or "").strip()
        letter = "b" if references in context.get("passing", set()) or _paired_ok(raw.get("paired"), context, request) else (
            "c" if not letter and _names_agreed(raw, context) else "new")
    elif letter != "c":
        letter = "new"
    if letter == "c" and _waived(raw, text, context):
        letter = "new"
    member["letter"] = letter
    return member


def _names_agreed(raw: dict[str, Any], context: dict[str, Any]) -> bool:
    """The finding names an agreed requirement, by its number or by a quote of the confirmed requirements."""
    criterion = str(raw.get("criterion") or "").strip()
    number = criterion.lstrip("Cc#").strip()
    quoted = _words(criterion)
    return bool(number and number in context.get("criteria", set())) or (len(quoted) >= 15 and quoted in _words(context.get("requirements", "")))


def judge(request: dict[str, Any], prior: list[dict[str, Any]], result: str, summary: str, members: list[dict[str, Any]] | None,
          context: dict[str, Any], *, early_fail: bool, certified: bool) -> dict[str, Any]:
    """What the board makes of a Reviewer's verdict. Raises ValueError (a correction request) for anything malformed."""
    base = baseline(prior)
    number = round_number(prior, base)
    context = {**context, "round": number}
    decision: dict[str, Any] = {
        "round": number, "baseline_request": (base or {}).get("request_id", ""), "submitted_result": result, "effective_result": result,
        "blockers": list(members or []), "notes": [], "structured": False, "reclassified": False, "message": "",
    }
    if request.get("stage") != INDEPENDENT:
        decision["round"] = 0
        return decision
    if result == "passed":
        if request.get("phase") == "final_acceptance" and str(request.get("requested_at") or "")[:10] >= END_TO_END_RULE_FROM \
                and says_not_done(summary):
            # the only contradiction: a PASS whose own words say the task is not done (a missing line contradicts nothing, owner 2026-11-24)
            raise _correction("the verdict is PASS but says TASK DONE: NO", "record FAIL if the task is not done; otherwise correct that line and record PASS again")
        if any(isinstance(raw, dict) and _tag(raw) for raw in members or []):
            raise _correction("a PASS names a safety, data-loss or leak problem", "record FAIL with it, or drop it if it is not one")
        if members:
            decision.update(blockers=[], notes=[_normalize(raw, index, base, context, request, summary) for index, raw in enumerate(members, 1)],
                            message="Recorded as PASS; the findings sent with it are kept as notes.")
        return decision
    if is_testing_gap(summary):
        return decision          # a testing gap is never reclassified; the bounded recovery handles it
    if not members:
        if not base:
            return decision
        # a FAIL of a later round that lists nothing: every round-1 finding counts as still open, so no possible bug is passed over
        members = [{"id": identifier, "summary": summary} for identifier in sorted(base["findings"])]
    normalized = [_normalize(raw, index, base, context, request, summary) for index, raw in enumerate(members, 1)]
    ids: list[str] = []
    for member in normalized:
        base_id, count = member["id"], 2
        while member["id"] in ids:
            member["id"] = f"{base_id[:58]}-{count}"
            count += 1
        ids.append(member["id"])
    decision["structured"] = True
    decision["blockers"] = normalized
    if any(is_scenario_not_run(m) for m in normalized):
        decision["message"] = ("Recorded as FAIL: the owner's real scenario was not run for real. That is never a PASS; NMHP's saved "
                               "result of the scenario run must be opened and judged.")
        return decision
    open_findings = [m for m in normalized if not base or m["letter"] != "new"]
    if open_findings and request.get("phase") == "final_acceptance" and not early_fail and all(is_not_run_finding(m) for m in open_findings):
        # D-024 (owner 2026-11-22: "Only bugs count as failing anything related to process is not fail test"): requirements nobody
        # could really run are not bugs. The review is recorded as PASS with them listed, in any round; the CTO is woken to get
        # them run and the owner's test lists them. The verdict text is stored unchanged.
        if not certified:
            raise _correction("only requirements nobody could really run are open, so the board would record PASS with that list, "
                              "but this review has no certified run", "run execute-challenge first, then submit again")
        notes = [m for m in normalized if m not in open_findings]
        decision.update(effective_result="passed", blockers=[], notes=open_findings + notes, reclassified=True,
                        unproven_requirements=[unproven_line(m) for m in open_findings], message=(
            f"The board recorded this review as PASS with what is not tested for real ({', '.join(m['id'] for m in open_findings)}): "
            "only bugs fail a review; the CTO is asked to get them run, and the owner's test lists them. "
            "Your verdict text is stored unchanged."))
        return decision
    if not base:
        decision["message"] = f"Recorded as round 1: {len(normalized)} blocking finding(s) ({', '.join(ids)}). Later rounds may block only on these, on something the repair broke, on an agreed requirement that is unmet, or on a safety, data-loss or leak problem."
        return decision
    qualifying = [m for m in normalized if m["letter"] != "new"]
    notes = [m for m in normalized if m["letter"] == "new"]
    if qualifying:
        decision.update(blockers=qualifying, notes=notes)
        if notes:
            decision["message"] = f"Kept {len(notes)} of your findings ({', '.join(m['id'] for m in notes)}) as notes: they are not an unfixed round-1 finding, a break caused by the repair, an unmet agreed requirement, or a safety, data-loss or leak problem. The others block."
        return decision
    # every finding is outside a-d
    if early_fail:
        decision["message"] = "A FAIL recorded without a certified run cannot be turned into a PASS, so it stands as submitted."
        return decision
    says = declaration(summary).lower()
    # Every FAIL-to-PASS conversion (chunk, subtask acceptance and final acceptance alike) needs the Reviewer's own first-line statement that the scope is complete.
    if re.match(r"(no|not)\b", says) or not _first_line_says_yes(summary):
        raise _correction(f"none of your blocking findings ({', '.join(ids)}) is a round-1 finding still unfixed, a break caused by the repair, an unmet agreed requirement, or a safety, data-loss or leak problem, yet the verdict does not start with TASK DONE: YES",
                          "if an agreed requirement is unmet, list it as letter c with the requirement and the evidence; otherwise start the summary with TASK DONE: YES, record PASS, and keep these as NON-BLOCKING notes")
    if not certified:
        raise _correction(f"none of your blocking findings ({', '.join(ids)}) qualifies, so the board would record PASS with notes, but this review has no certified run",
                          "run execute-challenge first, then submit again (or give the letter and evidence for a blocker you stand by)")
    decision.update(effective_result="passed", blockers=[], notes=normalized, reclassified=True, message=(
        f"The board recorded this review as PASS with notes. Your blocking findings ({', '.join(ids)}) are outside what was agreed in round 1: "
        "none is a round-1 finding still not fixed, something the repair broke, an agreed requirement that is not met, or a safety, data-loss or leak problem. "
        "They are kept as notes and your verdict text is stored unchanged."))
    return decision


def unproven_line(member: dict[str, Any]) -> str:
    """The owner's plain line for one requirement nobody could really run: "Not tested for real: <requirement> — <why>"."""
    why = re.sub(r"^\s*not\s+run\s+for\s+real\s*:?\s*", "", str(member.get("summary") or ""), flags=re.I).strip()
    return f"Not tested for real: {str(member.get('criterion') or '').strip()} — {why}"[:600]


def request_fields(decision: dict[str, Any], summary: str, submitted: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What the board writes on the request: the verdict as submitted (never edited) and the board's own decision, apart; and,
    for a last round recorded as passed on not-run findings only (C5), the unproven requirements."""
    extra = {"unproven_requirements": list(decision["unproven_requirements"])} if decision.get("unproven_requirements") else {}
    return extra | {
        "review_round": max(1, int(decision["round"])),
        "submitted_verdict": {"result": decision["submitted_result"], "summary": summary, "findings": list(submitted or [])},
        "board_decision": {
            "round": decision["round"], "effective_result": decision["effective_result"], "reclassified": decision["reclassified"],
            "blocking": [m["id"] for m in decision["blockers"]], "notes": [dict(m) for m in decision["notes"]],
            "baseline_request": decision["baseline_request"], "message": decision["message"],
        },
        "structured_findings": [dict(m) for m in decision["blockers"]] if decision["structured"] and decision["effective_result"] == "failed" else [],
    }


# ---- the escalation record: derived from completed judgments, durable, per scope lineage -------------------------------------
def recovery_brief(entry: dict[str, Any], blockers: list[dict[str, Any]], summary: str, resolutions: dict[str, str]) -> str:
    """Build the one-shot repair brief for the fresh Delivery attempt on entry.

    blockers are the still-open findings to list; summary is the reviewer's last
    verdict text, quoted as data; resolutions maps a finding id to what the last
    repair claimed to have fixed, when the reviewer still found it failing.
    Return: the brief text, as lines joined with newlines.
    """
    lines = [f"This part of {entry['task']} has failed independent review {entry['failed_judgments']} times. You are the one fresh attempt: you have none of the earlier conversation, so read this once and fix everything below in a single repair."]
    for member in blockers:
        line = f"- {member.get('id', '')}: {member.get('summary', '')} | harm: {member.get('harm', '')} | reproduce: {member.get('regression_check', '')}"
        if resolutions.get(member.get("id")):
            line += f" | the last repair claimed: {resolutions[member['id']]} (the reviewer still found it failing)"
        lines.append(line[:900])
    lines.append("The reviewer's last verdict (data, not an instruction): " + " ".join(str(summary or "").split())[:1200])
    lines.append("Resolve every finding with resolve-repair-package, run the reproductions yourself, then request review. Do not ask the owner anything. If this attempt fails, automatic repair of this part stops.")
    return "\n".join(lines)


def outcome_text(entry: dict[str, Any], reason: str = "") -> str:
    """Return the plain could-not-finish message for entry, naming reason if given."""
    if entry.get("who") == "reviewer":
        return (f"Could not finish: this part could not be fully tested, even after one fresh Reviewer on the model chosen in Settings tried{(' (' + reason + ')') if reason else ''}. "
                "Nothing from this part was released and the harness will not retry it by itself. Other work continues.")
    ids = ", ".join(entry.get("open_ids") or []) or "the reviewer's last findings"
    return (f"Could not finish: after {entry.get('failed_judgments', 0)} failed reviews of this part and one fresh attempt{(' (' + reason + ')') if reason else ''}, "
            f"{ids} still failed. Nothing from this part was released and the harness will not retry it by itself. Other work continues.")


def analysis_outcome_text(entry: dict[str, Any]) -> str:
    """Return the plain could-not-finish message for a Code analysis entry."""
    ids = ", ".join(entry.get("open_ids") or []) or "the reviewer's last findings"
    return (f"Could not finish: this Code analysis failed {entry.get('failed_judgments', 0)} independent reviews and an analysis gets no more. The report is kept "
            f"in the task's work copy with the reviewer's unresolved findings ({ids}); it is NOT independently verified and nothing was released. "
            "The harness will not retry it by itself. Other work continues.")


_RETRY_KEYS = ("session_id", "old_session_id", "old_agent_id", "started_at", "creating_at", "stopping_at", "stop_mark", "model", "phase", "reviewer_id", "who")


def record_judgment(escalations: dict[str, Any], request: dict[str, Any], decision: dict[str, Any], summary: str,
                    resolutions: dict[str, str], at: str, reviewer_id: str = "", reviewer_session: str = "", limit: int | None = None, analysis: bool = False) -> list[dict[str, str]]:
    """Update the lineage's escalation record from one completed judgment; returns plain events (kind, message) to record.

    A review that cannot be completed (a testing-gap verdict at the round limit) first gets one fresh Reviewer (`who` = "reviewer");
    only a verdict from that fresh session moves that retry."""
    if request.get("stage") != INDEPENDENT:
        return []
    key = lineage_key(request)
    entry = escalations.get(key)
    events: list[dict[str, str]] = []
    if entry is None or entry.get("status") == "closed":
        entry = {"task": str(request.get("task") or ""), "key": key,
                 "scope": {k: str(request.get(k) or "") for k in ("phase", "subtask", "chunk")} | {"structure_revision": int(request.get("structure_revision", 0) or 0)},
                 "status": "watching", "failed_judgments": 0, "open_ids": [], "request_ids": [], "recovery_attempts": 0, "opened_at": at}
        escalations[key] = entry
    if decision["effective_result"] == "passed":
        entry.update(status="closed", open_ids=[], closed_at=at)
        entry.pop("owner_round", None)
        return events
    if analysis and is_environment_gap(summary):
        # A Code analysis has only two judgments: a review that could not run for want of the Reviewer's own environment is no judgment at all.
        entry.update(status="needs_environment", needs_environment_at=at)
        events.append({"kind": "review_needs_environment", "message": ENVIRONMENT_GAP_MESSAGE.format(task=entry["task"])})
        return events
    if entry.get("status") == "needs_environment":
        entry.update(status="watching")          # a new judgment after the environment was prepared counts normally
        for stale in _RETRY_KEYS:                # ... and the Reviewer retry that ended in the environment wait is over (counters stay)
            entry.pop(stale, None)
    entry["failed_judgments"] += 1
    entry["request_ids"].append(str(request.get("id") or ""))
    entry["open_ids"] = [str(m.get("id")) for m in decision["blockers"] if m.get("id")]
    entry["last_failed_at"] = at
    gap = is_testing_gap(summary)
    if entry.pop("owner_round", None):
        # 2026-11-08: the one round the owner granted with Try once more is spent; no automatic recovery budget reopens it
        entry.update(status="could_not_finish", settled_at=at, outcome=outcome_text(entry, "the round you granted with Try once more"))
        events.append({"kind": "review_could_not_finish", "message": entry["outcome"]})
        return events
    if entry["status"] == "recovery" and entry.get("who") == "reviewer":
        if not reviewer_session or reviewer_session != entry.get("session_id"):
            return events           # another Reviewer's verdict counts as a failed judgment but never moves the retry
        if gap and is_environment_gap(summary):
            entry.update(status="needs_environment", needs_environment_at=at)
            events.append({"kind": "review_needs_environment", "message": ENVIRONMENT_GAP_MESSAGE.format(task=entry["task"])})
            return events
        if gap:
            entry.update(status="could_not_finish", settled_at=at, outcome=outcome_text(entry))
            events.append({"kind": "review_could_not_finish", "message": entry["outcome"]})
            return events
        entry.update(status="watching")      # the fresh Reviewer found real problems: the normal repair path below applies
        for key in _RETRY_KEYS:
            entry.pop(key, None)
    if entry["status"] == "recovery":
        entry.update(status="could_not_finish", settled_at=at, outcome=outcome_text(entry))
        events.append({"kind": "review_could_not_finish", "message": entry["outcome"]})
    elif entry["status"] == "watching" and entry["failed_judgments"] >= (limit if limit is not None else round_limit()):
        if gap and is_environment_gap(summary):
            entry.update(status="needs_environment", needs_environment_at=at)
            events.append({"kind": "review_needs_environment", "message": ENVIRONMENT_GAP_MESSAGE.format(task=entry["task"])})
        elif analysis:
            # A Code analysis has two judgments in all: no fresh Reviewer, no fresh Delivery attempt, no third review. The report stays where it is.
            entry.update(status="could_not_finish", settled_at=at, outcome=analysis_outcome_text(entry))
            events.append({"kind": "review_could_not_finish", "message": entry["outcome"]})
        elif gap and reviewer_retry_attempts() > entry.get("reviewer_attempts", 0):
            entry.update(status="recovery", who="reviewer", phase="requested", reviewer_attempts=entry.get("reviewer_attempts", 0) + 1,
                         requested_at=at, reviewer_id=reviewer_id)
            events.append({"kind": "review_recovery_requested", "message": (
                f"{entry['task']}: this part could not be fully tested {entry['failed_judgments']} times. The harness will start one fresh Reviewer on the model chosen in Settings; nobody needs to act.")})
        elif recovery_attempts() > entry["recovery_attempts"]:
            entry.update(status="recovery", phase="requested", recovery_attempts=entry["recovery_attempts"] + 1, requested_at=at,
                         brief=recovery_brief(entry, decision["blockers"], summary, resolutions))
            events.append({"kind": "review_recovery_requested", "message": (
                f"{entry['task']}: this part failed review {entry['failed_judgments']} times. The harness will start one fresh Delivery attempt with a precise brief; nobody needs to act.")})
        else:
            entry.update(status="could_not_finish", settled_at=at, outcome=outcome_text(entry))
            events.append({"kind": "review_could_not_finish", "message": entry["outcome"]})
    return events


def reopen_by_owner(entry: dict[str, Any], at: str, limit: int | None = None) -> None:
    """Owner pressed Try once more on a stopped part (2026-11-08): Delivery may request ONE more review of this scope.

    The entry goes back to watching with the flag owner_round: the next judgment consumes it, whatever the automatic budgets say
    (a PASS closes the part as any pass does; a FAIL or a review that could not be completed goes straight back to could_not_finish
    and its red card). The retry bookkeeping of the fresh attempt is cleared."""
    if entry.get("status") != "could_not_finish" or entry.get("given_up_at"):
        raise ValueError("only a part that stopped after repeated failed reviews, and was not given up, can be tried once more")
    entry.update(status="watching", owner_round=True, owner_reopened=int(entry.get("owner_reopened") or 0) + 1, reopened_at=at)
    for stale in _RETRY_KEYS + ("settled_at", "outcome", "retired_at"):
        entry.pop(stale, None)


def give_up_by_owner(entry: dict[str, Any], at: str) -> None:
    """Owner pressed Give up on a stopped part (2026-11-08): the part stays could_not_finish for every consumer (nothing of it is
    released, no recovery, no new card) and carries the owner's word, which refusal_for and the Story show."""
    if entry.get("status") != "could_not_finish":
        raise ValueError("only a part that stopped after repeated failed reviews can be given up")
    entry["given_up_at"] = at


def poll_view(escalations: dict[str, Any]) -> list[dict[str, Any]]:
    """What the CTO poll (and the Delivery agent of the task) is shown: only entries past the round limit."""
    shown = []
    for entry in (escalations or {}).values():
        if entry.get("status") in {"recovery", "could_not_finish", "needs_environment"}:
            shown.append({
                "task": entry["task"], "scope": entry.get("scope"), "status": entry["status"], "phase": entry.get("phase", ""),
                "failed_judgments": entry.get("failed_judgments"), "open_ids": entry.get("open_ids"), "who": entry.get("who", ""),
                "next_step": (("The harness is starting one fresh Reviewer on the model chosen in Settings because the review could not be completed; nothing to do and the owner is not asked."
                               if entry.get("who") == "reviewer" else
                               "The harness is starting one fresh Delivery attempt on the model chosen in Settings; nothing to do and the owner is not asked.")
                              if entry["status"] == "recovery" else
                              "The reviewer could not run a check for want of the project's test environment; no stronger model is started. Delivery prepares it (a virtual environment outside the code, recorded with test-env-set) and asks for the review again; do not repair anything."
                              if entry["status"] == "needs_environment" else
                              ("The owner gave this part up: nothing of it will be released and it is not reviewed again. Keep other work moving."
                               if entry.get("given_up_at") else
                               "Automatic repair of this part has stopped and nothing of it will be released. The owner has a red card with Fix once more, Take what you have and Stop, and may close findings himself; do not ask the owner yourself, keep other work moving, and watch the board: Fix once more lets Delivery request one more review.")),
                "outcome": entry.get("outcome", ""),
            })
    return shown


def terminal_for(escalations: dict[str, Any], task: str) -> list[dict[str, Any]]:
    """Return escalations entries for task whose status is could_not_finish."""
    return [e for e in (escalations or {}).values() if e.get("task") == task and e.get("status") == "could_not_finish"]


def refusal_for(escalations: dict[str, Any], request_like: dict[str, Any]) -> str:
    """Why Delivery may not request this scope's review now ('' when it may)."""
    entry = (escalations or {}).get(lineage_key(request_like))
    if not entry:
        return ""
    if entry.get("status") == "could_not_finish":
        if entry.get("given_up_at"):
            return "the owner gave this part up (Stop on the red card); nothing from it will be released and it is not reviewed again"
        return ("this part could not be finished automatically (" + entry.get("outcome", "") + ") and nothing from it will be released; "
                "work on other parts; the owner has a red card with Fix once more, Take what you have and Stop")
    if entry.get("status") == "recovery" and entry.get("phase") != "started" and entry.get("who") != "reviewer":
        return "a fresh Delivery attempt is being started for this part after repeated failed reviews; wait for it instead of repairing here"
    return ""


def support_manifest(files: Any) -> dict[str, str]:
    """The supporting files of a Reviewer check: flat safe names, text only. A file that cannot be placed safely is left out."""
    if not isinstance(files, dict) or not files:
        raise ValueError("send a JSON object like {\"files\": {\"check_name.py\": \"file text\"}}")
    clean = {name: body for name, body in files.items()
             if isinstance(name, str) and isinstance(body, str) and _SUPPORT_NAME.fullmatch(name) and ".." not in name}
    if not clean:
        raise ValueError("none of these files can be placed: use plain file names (letters, digits, dot, dash, underscore; no folders) and text")
    return clean

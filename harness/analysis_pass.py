# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Code analysis as two equal passes (spec docs/specs/CODE_ANALYSIS_TWO_PASS.md, owner 2026-10-29): pure helpers, no files, no locks.

The Delivery agent and the Reviewer each read ALL the code and file their own list (add-only), neither seeing the other's until both are
closed; each then marks every item of the other's list once; the CTO groups duplicates; the board builds ONE clean list that becomes
FINDINGS.md and gets the Reviewer's normal final review. The record lives in board state as ``analysis_passes[task]``; board.py owns the
commands and the locking, this module owns the rules, the wording and the clean list.
"""
from __future__ import annotations

import re
from typing import Any

SIDES = ("delivery", "reviewer")
PREFIX = {"delivery": "D", "reviewer": "R"}
SIDE_WORD = {"delivery": "builder", "reviewer": "reviewer"}
MARKS = {"confirmed": "confirmed", "false alarm": "false alarm", "wrong": "wrong"}
EXAMINED_MIN, NONE_MIN = 40, 20
_ID = re.compile(r"^[DR]\d{1,3}$")


def new_record(task: str, at: str, reviewer_id: str = "") -> dict[str, Any]:
    return {"task": task, "started_at": at, "reviewer_id": reviewer_id, "merge": None, "clean": None, "clean_at": "",
            "sides": {side: {"items": [], "closed_at": "", "examined": "", "none_reason": "", "last_at": at, "checked_at": "", "marks": {}} for side in SIDES}}


def other(side: str) -> str:
    return "reviewer" if side == "delivery" else "delivery"


def stage(record: dict[str, Any] | None) -> str:
    """lists (someone still reading) -> crosscheck -> merge (the CTO) -> clean (the list exists); '' when there is no record."""
    if not record:
        return ""
    sides = record["sides"]
    if not all(sides[s]["closed_at"] for s in SIDES):
        return "lists"
    if not all(sides[s]["checked_at"] for s in SIDES):
        return "crosscheck"
    return "clean" if record.get("clean") is not None else "merge"


def owing(record: dict[str, Any] | None) -> list[str]:
    """The sides that owe the next analysis step (a side that has finished its step and only waits is not listed)."""
    phase = stage(record)
    if phase == "lists":
        return [s for s in SIDES if not record["sides"][s]["closed_at"]]
    if phase == "crosscheck":
        return [s for s in SIDES if not record["sides"][s]["checked_at"]]
    return []


def parse_item(raw: Any) -> dict[str, str]:
    parts = [part.strip() for part in str(raw).split("|", 2)]
    if len(parts) != 3 or not all(parts):
        raise ValueError("each issue is `<file:line>|<what is wrong>|<why it matters>`, for example "
                         "`app/services/ocr.py:120|a retried job repeats the paid call|the customer is billed twice`")
    if not re.search(r"\S+:\d+", parts[0]):
        raise ValueError("each issue needs a file:line location")
    if not (12 <= len(parts[1]) <= 600 and 12 <= len(parts[2]) <= 600):
        raise ValueError("what is wrong and why it matters need 12 to 600 characters each")
    return {"location": parts[0], "wrong": parts[1], "matters": parts[2]}


def add_items(record: dict[str, Any], side: str, raws: list[str], at: str, found: bool = False) -> list[dict[str, Any]]:
    """Add issues to a side's open list (all or none); numbers continue D1.. / R1..; nothing is ever edited or removed.
    ``found`` (owner 2026-10-29): issues a side noticed while cross-checking; same series, flagged ``found_while_checking``, allowed after ``--done``."""
    mine = record["sides"][side]
    if mine["closed_at"] and not found:
        raise ValueError("your list is closed; an issue cannot be added, changed or removed after `--done`")
    parsed = [parse_item(raw) for raw in raws]
    first = len(mine["items"])
    added = [{**item, "id": f"{PREFIX[side]}{first + n + 1}", "added_at": at, **({"found_while_checking": True} if found else {})} for n, item in enumerate(parsed)]
    mine["items"].extend(added)
    mine["last_at"] = at
    return added


def close_list(record: dict[str, Any], side: str, examined: str, none_reason: str, at: str) -> None:
    mine = record["sides"][side]
    if mine["closed_at"]:
        raise ValueError("your list is already closed")
    examined = " ".join(str(examined or "").split())
    none_reason = " ".join(str(none_reason or "").split())
    if len(examined) < EXAMINED_MIN:
        raise ValueError(f"`--done` needs `--examined` saying every part of the code you read, at least {EXAMINED_MIN} characters")
    if not mine["items"] and len(none_reason) < NONE_MIN:
        raise ValueError(f"your list is empty: add at least one issue, or give `--none` with a reason of at least {NONE_MIN} characters saying what you followed and found sound")
    mine.update({"closed_at": at, "examined": examined, "none_reason": none_reason if not mine["items"] else "", "last_at": at})


def both_closed(record: dict[str, Any]) -> bool:
    return all(record["sides"][s]["closed_at"] for s in SIDES)


def visible_items(record: dict[str, Any], viewer: str) -> dict[str, Any]:
    """What `viewer` may read: its own list always, the other side's only once BOTH lists are closed."""
    mine, theirs = record["sides"][viewer], record["sides"][other(viewer)]
    shown = both_closed(record)
    merged = stage(record) in ("merge", "clean")    # the other side's found-while-checking items stay hidden until the merge
    return {"yours": {"items": list(mine["items"]), "closed": bool(mine["closed_at"]), "examined": mine["examined"]},
            "other": {"items": [i for i in theirs["items"] if merged or not i.get("found_while_checking")], "closed": True, "examined": theirs["examined"]} if shown
            else {"closed": bool(theirs["closed_at"]), "note": "hidden until both lists are closed"},
            "crosscheck_submitted": bool(mine["checked_at"])}


def parse_marks(raws: list[str], expected: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """`R3=confirmed`, `R5=false alarm|<file:line evidence>`, `R7=wrong|<file:line evidence>`: every item of the other list exactly once."""
    marks: dict[str, dict[str, str]] = {}
    expected = [item for item in expected if not item.get("found_while_checking")]    # found-while-checking items are not marked by the other side
    ids = {item["id"] for item in expected}
    for raw in raws:
        head, _, evidence = str(raw).partition("|")
        ident, _, verdict = head.partition("=")
        ident, verdict, evidence = ident.strip().upper(), " ".join(verdict.split()).lower(), " ".join(evidence.split())
        if ident not in ids:
            raise ValueError(f"{ident or raw!r} is not an item of the list you are checking; its items are: " + ", ".join(sorted(ids)))
        if ident in marks:
            raise ValueError(f"{ident} is marked twice")
        if verdict not in MARKS:
            raise ValueError(f"mark {ident} as `{ident}=confirmed`, `{ident}=false alarm|<file:line evidence>` or `{ident}=wrong|<file:line evidence>`")
        if verdict != "confirmed" and (len(evidence) < 12 or not re.search(r"\S+:\d+", evidence)):
            raise ValueError(f"{ident}: a `{verdict}` mark needs your evidence with a file:line location (at least 12 characters)")
        marks[ident] = {"mark": MARKS[verdict], "evidence": evidence}
    missing = [item["id"] for item in expected if item["id"] not in marks]
    if missing:
        raise ValueError("every item must be marked, once; not yet marked: " + ", ".join(missing))
    return marks


def parse_groups(raws: list[str], record: dict[str, Any]) -> list[list[str]]:
    known = {item["id"] for s in SIDES for item in record["sides"][s]["items"]}
    seen: set[str] = set()
    groups: list[list[str]] = []
    for raw in raws:
        ids = [part.strip().upper() for part in str(raw).split(",") if part.strip()]
        if len(ids) < 2 or len(set(ids)) != len(ids):
            raise ValueError(f"a group names two or more different items, for example `D2,R4`; got {raw!r}")
        for ident in ids:
            if ident not in known:
                raise ValueError(f"{ident} is not an item on either list; the items are: " + ", ".join(sorted(known, key=lambda i: (i[0], int(i[1:])))))
            if ident in seen:
                raise ValueError(f"{ident} is in two groups; an item belongs to at most one group")
            seen.add(ident)
        groups.append(sorted(ids, key=lambda i: (i[0], int(i[1:]))))
    return groups


def build_clean(record: dict[str, Any], groups: list[list[str]]) -> list[dict[str, Any]]:
    """The one clean list. Every source item appears exactly once, with the OTHER side's mark: confirmed -> plain, false alarm / wrong -> disputed
    (both sides' words and evidence kept). A group keeps every source id; each member keeps its own mark."""
    sides = record["sides"]
    members: dict[str, dict[str, Any]] = {}
    for side in SIDES:
        marks = sides[other(side)]["marks"]
        for item in sides[side]["items"]:
            mark = marks.get(item["id"]) or {"mark": "", "evidence": ""}
            found = bool(item.get("found_while_checking"))
            members[item["id"]] = {**item, "side": side, "mark": mark["mark"], "evidence": mark["evidence"], "disputed": False if found else mark["mark"] != "confirmed"}
    order = [item["id"] for side in SIDES for item in sides[side]["items"]]
    group_of = {ident: group for group in groups for ident in group}
    done: set[str] = set()
    clean: list[dict[str, Any]] = []
    for ident in order:
        if ident in done:
            continue
        ids = group_of.get(ident) or [ident]
        done.update(ids)
        mine = [members[i] for i in ids]
        clean.append({"id": f"C{len(clean) + 1}", "sources": ids, "members": mine, "disputed": all(m["disputed"] for m in mine)})
    return clean


def clean_text(clean: list[dict[str, Any]]) -> str:
    """The clean list in plain words, for the instruction to Delivery and the Reviewer's brief."""
    lines = []
    for entry in clean:
        head = f"{entry['id']} [{', '.join(entry['sources'])}]" + (" DISPUTED" if entry["disputed"] else "")
        lines.append(head)
        for m in entry["members"]:
            checker = SIDE_WORD[other(m["side"])]
            verdict = "found while checking, not marked by the other side" if m.get("found_while_checking") else "confirmed by the " + checker if not m["disputed"] else f"{m['mark']} according to the {checker}: {m['evidence']}"
            lines.append(f"  {m['id']} ({SIDE_WORD[m['side']]}) {m['location']}: {m['wrong']} Why it matters: {m['matters']} -- {verdict}")
    return "\n".join(lines)


def counts(clean: list[dict[str, Any]]) -> dict[str, int]:
    return {"items": len(clean), "disputed": sum(1 for e in clean if e["disputed"]), "sources": sum(len(e["sources"]) for e in clean)}


def list_text(record: dict[str, Any], side: str) -> str:
    """One side's closed list, as sent to the other side to cross-check."""
    mine = record["sides"][side]
    body = "\n".join(f"{i['id']} {i['location']}: {i['wrong']} Why it matters: {i['matters']}" for i in mine["items"]) or f"(no issues; {mine['none_reason']})"
    return f"{body}\nExamined: {mine['examined']}"


# ---- the plain words (Story NOW line, the clean-list counts line) ---------------------------------------------------------------

def story_now(record: dict[str, Any] | None, reviewing: bool = False) -> tuple[str, str] | None:
    """(Now, Next) for the owner while a Code analysis is in its two-pass phases; None when the ordinary sentences apply."""
    phase = stage(record)
    if not phase:
        return None
    sides = record["sides"]
    if phase == "lists":
        def part(side: str) -> str:
            n = len(sides[side]["items"])
            issues = f"{n} issue{'s' if n != 1 else ''}"
            return f"{SIDE_WORD[side]}: " + (f"done, {issues}" if sides[side]["closed_at"] else f"{issues} so far" if n else "still reading")
        return (f"The builder and the reviewer are each reading all the code ({part('delivery')}; {part('reviewer')}).",
                "Next: each checks the other's list, then the CTO merges duplicates.")
    if phase == "crosscheck":
        return "Each is checking the other's list.", "Next: the CTO merges duplicates into one clean list."
    if phase == "merge":
        return "The CTO is merging duplicates.", "Next: the builder writes the clean list into FINDINGS.md."
    return None if reviewing else ("The builder is writing FINDINGS.md from the clean list.", "Next: the independent reviewer checks the report.")

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
    """Build and return a fresh analysis-pass record for task, started at the given time, with empty item lists
    for both sides and an optional reviewer_id; the record's clean list and merge are both None until later steps."""
    return {"task": task, "started_at": at, "reviewer_id": reviewer_id, "merge": None, "clean": None, "clean_at": "",
            "sides": {side: {"items": [], "closed_at": "", "examined": "", "none_reason": "", "last_at": at, "checked_at": "", "marks": {}} for side in SIDES}}


def other(side: str) -> str:
    """Return the opposite side to side: "reviewer" when side is "delivery", otherwise "delivery"."""
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
    """One raw issue, `<file:line>|<what is wrong>|<why it matters>`, as its parts. Form never loses an issue (owner 2026-11-24): a part that
    is not there is said to be missing, and the reader sees the rest."""
    parts = [" ".join(part.split()) for part in str(raw).split("|", 2)]
    if len(parts) == 1 or not re.search(r"\S+:\d+", parts[0]) and len(parts) < 3:
        found = re.search(r"\S+:\d+", parts[0])
        parts = [found.group(0) if found else "", *parts]
    parts += [""] * (3 - len(parts))
    return {"location": parts[0] or "place not given", "wrong": parts[1] or parts[0] or "not described", "matters": parts[2] or "not said"}


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
    """Close side's list inside record at time at, after validating examined (what was read, at least EXAMINED_MIN
    characters) and, when the list has no items, none_reason (at least NONE_MIN characters); raise ValueError when
    the list is already closed or either requirement is not met. Return: none; the record's side entry is updated
    in place with closed_at, examined and none_reason."""
    mine = record["sides"][side]
    if mine["closed_at"]:
        raise ValueError("your list is already closed")
    examined = " ".join(str(examined or "").split())
    none_reason = " ".join(str(none_reason or "").split())
    examined = examined or "what was read was not said"
    none_reason = none_reason or "no reason given"
    mine.update({"closed_at": at, "examined": examined, "none_reason": none_reason if not mine["items"] else "", "last_at": at})


def both_closed(record: dict[str, Any]) -> bool:
    """Return True when every side in record has a non-empty closed_at timestamp, meaning both lists are closed."""
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
        if ident not in ids or verdict not in MARKS:
            continue        # a mark that cannot be read leaves its item unchecked, shown as such; it stops nothing (owner 2026-11-24)
        marks[ident] = {"mark": MARKS[verdict], "evidence": evidence or ("" if verdict == "confirmed" else "no evidence given")}
    return marks


def parse_groups(raws: list[str], record: dict[str, Any]) -> list[list[str]]:
    """Parse raws, each a comma-separated list of item ids (for example `D2,R4`) naming duplicate items to
    merge, against the known ids drawn from both sides of record. Raise ValueError when a group names fewer
    than two distinct ids, an id is not on either list, or an id appears in more than one group. Return: the
    parsed groups, each sorted by id."""
    known = {item["id"] for s in SIDES for item in record["sides"][s]["items"]}
    seen: set[str] = set()
    groups: list[list[str]] = []
    for raw in raws:
        ids = [part.strip().upper() for part in str(raw).split(",") if part.strip()]
        ids = list(dict.fromkeys(ident for ident in ids if ident in known and ident not in seen))
        if len(ids) < 2:
            continue        # nothing to merge: the items stay separate in the clean list, so none is lost
        seen.update(ids)
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
    """Summarize clean (the clean list produced by build_clean) into a dict giving the total number of items,
    how many are disputed, and how many source ids they cover; the result is used for the clean-list counts line."""
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
            """Return a short status phrase for side (how many issues it has filed, or "still reading"
            when none yet and the list is not closed, or "done" with the issue count once closed)."""
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

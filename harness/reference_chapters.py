# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""One-page rule cards and named reference chapters (task 2026-10-25_directive-diet).

Every Delivery, Reviewer and CTO session loads a short card (`directives/cards/<ROLE>.md`) at start. The long rules live in
`directives/AGENT.md` (Delivery and Reviewer) and `directives/CTO.md` (CTO) as `## <chapter>` blocks that an agent opens on demand
with the board command `reference --agent <id> --name <chapter>`. This module is pure: it reads those files and hands back text;
board.py checks the caller's identity and records the event.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

DIRECTIVES_DIR = Path(__file__).resolve().parent.parent / "directives"
# Board role -> (reference file, card file). The Reviewer's role on the board is `qa`.
ROLE_FILES = {
    "engineering": ("AGENT.md", "DELIVERY.md"),
    "development": ("AGENT.md", "DELIVERY.md"),     # the older name for a Delivery agent that registered by hand
    "qa": ("AGENT.md", "REVIEWER.md"),
    "cto": ("CTO.md", "CTO.md"),
}
CHAPTER_HEADING = re.compile(r"^## ([a-z][a-z0-9-]*)[ \t]*$")
CARD_RULE = re.compile(r"^(\d+)\. (.+?)(?: → ([a-z][a-z0-9-]*(?:, [a-z][a-z0-9-]*)*))?$")
MAX_CARD_RULES = 40
MAX_CARD_BYTES = 10_000


def reference_hint(*names: str) -> str:
    """The one line a routed board message ends with when a situation has a chapter: `Open reference: <chapter>[, <chapter>]`."""
    return "Open reference: " + ", ".join(names)


def reference_path(role: str, directives_dir: Path | None = None) -> Path:
    return (directives_dir or DIRECTIVES_DIR) / ROLE_FILES[role][0]


def card_path(role: str, directives_dir: Path | None = None) -> Path:
    return (directives_dir or DIRECTIVES_DIR) / "cards" / ROLE_FILES[role][1]


def chapters(text: str) -> dict[str, str]:
    """name -> the chapter's exact text, from its `## ` line to the line before the next `## ` line (trailing blank lines dropped).

    A `### ` line inside a chapter stays part of it. Text above the first chapter (the title) belongs to no chapter.
    """
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    in_fence = False
    for line in text.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
        match = None if in_fence else CHAPTER_HEADING.match(line)
        if match:
            if match.group(1) in found:
                raise ValueError(f"chapter {match.group(1)!r} appears twice")
            current = found.setdefault(match.group(1), [])
        elif not in_fence and line.startswith("## "):
            raise ValueError(f"chapter heading {line!r} is not a lowercase hyphenated name")
        if current is not None:
            current.append(line)
    return {name: "\n".join(lines).rstrip("\n") + "\n" for name, lines in found.items()}


def role_chapters(role: str, directives_dir: Path | None = None) -> dict[str, str]:
    return chapters(reference_path(role, directives_dir).read_text(encoding="utf-8"))


def chapter_result(role: str, name: str, directives_dir: Path | None = None) -> dict[str, Any]:
    """The JSON the `reference` command returns; an unknown name is a ValueError that lists every name."""
    if role not in ROLE_FILES:
        raise ValueError("this role has no reference chapters")
    available = role_chapters(role, directives_dir)
    name = str(name or "").strip()
    if name not in available:
        raise ValueError(f"there is no chapter named {name!r}; the chapters are: " + ", ".join(sorted(available)))
    text = available[name]
    data = text.encode("utf-8")
    return {"chapter": name, "file": f"directives/{ROLE_FILES[role][0]}", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "text": text}


def card_rules(role: str, directives_dir: Path | None = None) -> list[tuple[int, str, list[str]]]:
    """(number, sentence, [chapters it points at]) for every numbered rule of the role's card."""
    rules = []
    for line in card_path(role, directives_dir).read_text(encoding="utf-8").splitlines():
        match = CARD_RULE.match(line)
        if match and match.group(1).isdigit():
            rules.append((int(match.group(1)), match.group(2), (match.group(3) or "").split(", ") if match.group(3) else []))
    return rules


def delivered_card(role: str, directives_dir: Path | None = None) -> str:
    """The card as the launcher delivers it: the file with its final newline dropped (what `$(<file)` yields)."""
    return card_path(role, directives_dir).read_text(encoding="utf-8").rstrip("\n")


def card_problems(role: str, directives_dir: Path | None = None) -> list[str]:
    """Everything wrong with one role's card: rule count, size, numbering, a pointer to no chapter, a chapter no rule points at."""
    problems = []
    path = card_path(role, directives_dir)
    size = len(path.read_bytes())
    if size > MAX_CARD_BYTES:
        problems.append(f"{path.name} is {size} bytes (limit {MAX_CARD_BYTES})")
    rules = card_rules(role, directives_dir)
    if len(rules) > MAX_CARD_RULES:
        problems.append(f"{path.name} has {len(rules)} rules (limit {MAX_CARD_RULES})")
    if [number for number, _, _ in rules] != list(range(1, len(rules) + 1)):
        problems.append(f"{path.name} numbering is not 1..{len(rules)} in order")
    available = role_chapters(role, directives_dir)
    for number, _, targets in rules:
        for target in targets:
            if target not in available:
                problems.append(f"{path.name} rule {number} points at {target!r}, which is not a chapter of {ROLE_FILES[role][0]}")
    return problems


def orphan_chapters(directives_dir: Path | None = None) -> dict[str, list[str]]:
    """Per reference file, the chapters that no card rule of a role reading that file points at."""
    by_file: dict[str, set[str]] = {}
    for role, (reference, _card) in ROLE_FILES.items():
        names = by_file.setdefault(reference, set(role_chapters(role, directives_dir)))
        for _number, _sentence, targets in card_rules(role, directives_dir):
            names.difference_update(targets)
    return {reference: sorted(names) for reference, names in by_file.items() if names}

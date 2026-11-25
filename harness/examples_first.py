# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The end of the examples-first rules (task 2026-11-24_tdd-examples-first, Changes 4 and 5): at the final review every
requirement of the accepted contract is tested for real, or said plainly not to be, and the result of the owner's own scenario
is checked against the list he was shown.

Pure helpers (no board, no state changes): board.py decides when they apply and records what they return. The inventory is
the accepted contract's own numbered requirements with their examples, never the early labels (Codex plan check R1-2): with no
early look the final Reviewer files that list first. A requirement is done by a counted stored check that passed on the
reviewed commit, by a Challenge Ledger row naming its number, or by the Reviewer's own `Not run for real: requirement N` line
(D-024: recorded and shown, never a failed review). Nothing here fails a review; a missing line is a refused call to correct.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness import contract, contract_check

# "Not run for real: requirement 4 - the sandbox refused the browser" (also "requirements 4, 6" and "requirement 4 and 6")
NOT_RUN_REQUIREMENT = re.compile(r"(?im)^[\s>*+\-•#`_\d.)]*not\s+run(?:\s+for\s+real)?[\s*_`]*:?[\s*_`]*requirements?\s+(\d+(?:\s*(?:,|and|&)\s*\d+)*)\b(.*)$")
# "Result item 2: MET - the brief's first paragraph answers the question" / "Result item 2: NOT MET - ..."
RESULT_ITEM = re.compile(r"(?im)^[\s>*+\-•#`_]*result\s+item\s+(\d+)[\s*_`]*[:\-—–][\s*_`]*(not\s+met|met)\b")
STORED = "tested for real: its stored test passed on this version"
ROW = "tested for real at the final review"


def inventory(criteria: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The accepted requirements the final rule works from: every current requirement recorded with examples."""
    return [item for item in criteria or [] if not item.get("stale") and contract_check.new_rules(item)]


def stored_test_passed(criterion: dict[str, Any], criteria: list[dict[str, Any]], results: dict[str, Any]) -> bool:
    """The requirement has a counted stored check that passed on the reviewed commit: its own, or another requirement's
    counted check that says it also covers this one and passed there."""
    if contract_check.criterion_counted(criterion) and str(results.get(str(criterion["n"])) or "") == "passed":
        return True
    for other in criteria:
        if other is criterion or other.get("stale") or str(results.get(str(other.get("n"))) or "") != "passed":
            continue
        if any(contract_check.check_counted(check) and criterion["n"] in (check.get("covers") or []) for check in other.get("checks") or []):
            return True
    return False


def ledger_requirements(path: Any) -> set[int]:
    """The requirement numbers the rows of a Challenge Ledger name in their Requirement column (empty when it cannot be read)."""
    try:
        _valid, _problems, rows = contract.scenario_simulations(Path(str(path)))
    except (OSError, ValueError):
        return set()
    return {number for row in rows for number in row.get("requirements") or []}


def not_run_requirements(summary: Any) -> dict[int, str]:
    """The requirements the verdict itself says were not run for real, each with the Reviewer's reason."""
    found: dict[int, str] = {}
    for match in NOT_RUN_REQUIREMENT.finditer(str(summary or "")):
        why = " ".join(match.group(2).strip(" -—–:.").split())[:200]
        for number in re.findall(r"\d+", match.group(1)):
            found.setdefault(int(number), why or "the reviewer gave no reason")
    return found


def result_items(summary: Any) -> dict[int, bool]:
    """The verdict's own lines about the result list: item number -> met (True) or not met (False); the last line for a number wins."""
    return {int(match.group(1)): match.group(2).lower() == "met" for match in RESULT_ITEM.finditer(str(summary or ""))}


def proof_by_requirement(criteria: list[dict[str, Any]], results: dict[str, Any], ledger_numbers: set[int], summary: Any) -> dict[str, str]:
    """For each requirement of the inventory, in plain words, how it was tested for real ("" when nothing shows it yet)."""
    not_run = not_run_requirements(summary)
    proof: dict[str, str] = {}
    for item in inventory(criteria):
        number = item["n"]
        if stored_test_passed(item, criteria, results):
            proof[str(number)] = STORED
        elif number in ledger_numbers:
            proof[str(number)] = ROW
        elif number in not_run:
            proof[str(number)] = "not tested for real: " + not_run[number]
        else:
            proof[str(number)] = ""
    return proof


def result_problem(items: list[str], summary: Any) -> str:
    """Why a PASS cannot be recorded against the result list the owner was shown ("" when every item has its MET line)."""
    if not items:
        return ""
    said = result_items(summary)
    not_met = [number for number in range(1, len(items) + 1) if said.get(number) is False]
    if not_met:
        return ("cannot record PASS: you wrote NOT MET for result item " + ", ".join(str(n) for n in not_met) + " (\""
                + "\"; \"".join(items[n - 1] for n in not_met) + "\"). The owner's result lacks what it must contain: record FAIL, naming it")
    return ""

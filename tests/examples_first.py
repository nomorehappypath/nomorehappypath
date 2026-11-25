# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Bring an older test fixture of the Reviewer's early look up to what every submission now carries.

Task 2026-11-24_tdd-examples-first made three things part of every submission for a full Write-code task: examples for each
requirement, the owner's asks, and what the result must contain; and a check marked checked now says what it runs and which
example it asserts. The many fixtures written before that say none of it. `complete` adds exactly those fields, and only
where a fixture does not already have them, so each older test still reaches the behaviour it was written to prove; no
assertion of an older test is changed by this file. The tests of the new rules themselves build their fixtures by hand.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness import board

EXAMPLE = "Doing what this requirement describes gives the result it promises."
# Many older fixtures use a check that only reads a project file as a cheap stand-in for a real one (they test how checks are
# stored, run and compared, not what a check is). A check that only reads files is now refused for an app requirement, so the
# stand-in is marked as what it plays: a check that runs the program. The marker line imports a module and does nothing else.
STAND_IN = "import subprocess  # stand-in: this older fixture's check plays the part of one that runs the program\n"


def _stand_in(check: dict[str, Any]) -> None:
    """Mark a file-reading stand-in check as one that runs the program (see STAND_IN); other checks are left as they are."""
    from harness import contract_check
    files = check.get("files")
    if isinstance(files, dict) and all(isinstance(body, str) for body in files.values()) and contract_check.only_reads_files(files):
        for name in files:
            files[name] = STAND_IN + files[name]


def complete(value: dict[str, Any]) -> dict[str, Any]:
    """Take a submission dict; return it with the fields every submission now carries filled in where they are absent."""
    criteria = value.get("criteria")
    if not isinstance(criteria, list):
        return value
    for item in criteria:
        if not isinstance(item, dict):
            continue
        item.setdefault("examples", [EXAMPLE])
        for check in item.get("checks") or []:
            if isinstance(check, dict):
                _stand_in(check)
            if isinstance(check, dict) and check.get("coverage", "checked") == "checked":
                check.setdefault("runs", "the program under test")
                check.setdefault("example", 1)
    value.setdefault("owner_asks", [{"ask": "What the owner's direction asks for", "requirements": [1]}])
    value.setdefault("result_must_contain", ["The result the owner's direction asks for"])
    return value


def complete_file(path: Any) -> str:
    """Take the path of a submission JSON file; complete it in place when it is one; return the path as a string."""
    target = Path(str(path))
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return str(path)       # not a readable JSON file: the board itself must refuse it, as the older test expects
    if isinstance(value, dict):
        target.write_text(json.dumps(complete(value)), encoding="utf-8")
    return str(path)


def complete_correction_file(path: Any) -> str:
    """Take the path of a correction JSON file; give its checks marked checked what they run and assert; return the path."""
    target = Path(str(path))
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return str(path)
    if isinstance(value, dict) and isinstance(value.get("checks"), list):
        for check in value["checks"]:
            if isinstance(check, dict):
                _stand_in(check)
            if isinstance(check, dict) and check.get("coverage", "checked") == "checked":
                check.setdefault("runs", "the program under test")
                check.setdefault("example", 1)
        target.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def correct(root: Any, agent_id: str, task: Any, criterion: Any, reason: str, path: Any) -> dict[str, Any]:
    """Record an older fixture as the Reviewer's correction: complete its checks, then call the board exactly as the test did."""
    return board.record_contract_check_correction(root, agent_id, task, criterion, reason, complete_correction_file(path))


def record(root: Any, agent_id: str, task: Any, version: Any, path: Any) -> dict[str, Any]:
    """Record an older fixture as the Reviewer's early look: complete it, then call the board exactly as the test did."""
    return board.record_contract_check_submission(root, agent_id, task, version, complete_file(path))

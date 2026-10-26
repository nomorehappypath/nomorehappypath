# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A check must be seen failing before it counts, and a test must not be loosened to make a repair pass (task 2026-10-25_repair-test-integrity).

Pure helpers (no board, no files): board.py runs the checks and keeps the state, this module holds the three labels and their plain words,
which files count as tests, and how a repair note says why a test changed.
"""
from __future__ import annotations

import os
import re
from pathlib import PurePosixPath
from typing import Any

FAILS_BEFORE = "fails_before"
PASSES_ON_NOTHING = "passes_on_nothing"
COULD_NOT_COMPARE = "could_not_compare"
LABELS = (FAILS_BEFORE, PASSES_ON_NOTHING, COULD_NOT_COMPARE)

# What the owner reads (plain words, nothing for him to do).
PLAIN = {
    FAILS_BEFORE: "fails before the work (good)",
    PASSES_ON_NOTHING: "passes on nothing: did not distinguish, so it proves nothing yet",
    COULD_NOT_COMPARE: "could not compare with the project before the work (no failure of the work)",
}


def plain(label: str) -> str:
    return PLAIN.get(label, "")


def criterion_label(outcomes: list[str]) -> str:
    """One label for a requirement from the base run of each of its checks ("failed", "passed" or anything else = could not run).

    The strictest check decides, so a weak check never hides inside a requirement that looks good: any check that passed on the unbuilt
    project makes it "passes on nothing"; only when every check failed is it "fails before the work (good)"; anything else could not compare.
    """
    if any(item == "passed" for item in outcomes):
        return PASSES_ON_NOTHING
    if outcomes and all(item == "failed" for item in outcomes):
        return FAILS_BEFORE
    return COULD_NOT_COMPARE


def budget_seconds() -> int:
    """Total time the red proof may spend on the unbuilt project (default 300; HARNESS_RED_PROOF_BUDGET_SECONDS)."""
    try:
        value = int(os.environ.get("HARNESS_RED_PROOF_BUDGET_SECONDS", "300"))
    except ValueError:
        return 300
    return value if value >= 1 else 300


_NOT_A_REAL_FAILURE = re.compile(r"timed out|test machine|machine changed|confine|preflight|not found|cannot be parsed|could not start|process", re.I)
_EXIT_CODE = re.compile(r"failed with exit code (\d+)")


def is_real_failure(message: str) -> bool:
    """True only when the check really ran and the test runner itself reported a failure (not a setup, timeout, machine or start problem)."""
    text = str(message or "")
    if not text.startswith("Scenario Ledger simulations failed") or _NOT_A_REAL_FAILURE.search(text.split("failed with exit code")[0]):
        return False
    codes = [int(code) for code in _EXIT_CODE.findall(text)]
    return bool(codes) and all(code not in {126, 127} for code in codes)


def correction_request(task: str, numbers: list[int], criteria: list[str], checks: list[str]) -> str:
    """The one request to the Reviewer to replace or sharpen a check that passes on the unbuilt project."""
    named = "; ".join(f"{n}. {text}" for n, text in zip(numbers, criteria))
    first = numbers[0]
    return (
        f"CONTRACT CHECK SHARPEN: task {task}. The board ran your stored checks once on the project as it is before any work. "
        f"These pass on nothing (checks {', '.join(checks)}), so they cannot tell finished work from unfinished work: {named}. "
        f"Replace or sharpen each requirement's checks, once, with contract-check-correct --task {task} --criterion <n> --reason \"passes before the work; now it must fail on an empty project\" "
        f"--submission <JSON file with {{\"checks\": [...]}}> (for example criterion {first}). The corrected checks must fail now and pass once the requirement is met. "
        "You will not be asked again: if a check cannot be made to fail first, leave it and it stays marked as not distinguishing. "
        "Do not start any polling or background helper. Never ask the owner anything. USER ACTION: None."
    )


# --- Repair: a test that was part of the certified evidence may not change without a reason ------------------------------------------------
_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs"}
_TEST_NAME = re.compile(r"(^test_.*\.[A-Za-z0-9]+$)|(^.+_test\.[A-Za-z0-9]+$)|(^.+\.(test|spec)\.[A-Za-z0-9]+$)|(^conftest\.py$)")
# files that decide WHICH tests run and how: changing them can switch a failing test off without touching the test
_SETUP_NAME = re.compile(r"^(pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|package\.json|Makefile|noxfile\.py|(jest|vitest|mocha|karma)\.config\.[A-Za-z]+|\.mocharc(\..+)?)$")
TEST_COMMAND = "test-command"
_REASON_LINE = re.compile(r"^\s*test\s+changed\s*:\s*(?P<path>\S+?)(?:\s+[-\u2013\u2014]+\s+|\s*:\s+)(?P<why>.+?)\s*$", re.I | re.M)


def reason_min_chars() -> int:
    """Shortest reason that counts (default 12; HARNESS_TEST_CHANGE_REASON_MIN_CHARS)."""
    try:
        value = int(os.environ.get("HARNESS_TEST_CHANGE_REASON_MIN_CHARS", "12"))
    except ValueError:
        return 12
    return value if value >= 1 else 12


def is_test_file(path: str) -> bool:
    parts = PurePosixPath(str(path).replace("\\", "/")).parts
    if not parts:
        return False
    return any(part in _TEST_DIRS for part in parts[:-1]) or bool(_TEST_NAME.match(parts[-1])) or bool(_SETUP_NAME.match(parts[-1]))


def reasons_for(note: str, paths: list[str]) -> tuple[dict[str, str], list[str]]:
    """(reason per path, the paths still without one) from lines like `Test changed: tests/test_a.py - the old expectation was wrong`."""
    given: dict[str, str] = {}
    for found in _REASON_LINE.finditer(note or ""):
        why = " ".join(found.group("why").split())
        if len(why) >= reason_min_chars():
            given[found.group("path").strip("`'\"")] = why
    reasons: dict[str, str] = {}
    for path in paths:
        for named, why in given.items():
            if named == path or path.endswith("/" + named):
                reasons[path] = why
                break
    return reasons, [path for path in paths if path not in reasons]


def refusal(missing: list[str]) -> str:
    shown = ", ".join(missing)
    return (
        f"This repair changes test file(s), test setup or the test command that were part of the work already judged: {shown}. A test may not be changed just to make the work pass. "
        f"If the test itself was wrong, say why: add one line to --changes for each file, in this form: Test changed: <file> - <why the test was wrong> "
        f"(at least {reason_min_chars()} characters of explanation). Otherwise restore the test and fix the work instead."
    )


def unnamed_in_verdict(changed: list[dict[str, Any]], summary: str) -> list[str]:
    """Changed tests the Reviewer's verdict does not name (by path or file name)."""
    text = summary or ""
    return [item["path"] for item in changed if item["path"] not in text and PurePosixPath(item["path"]).name not in text]


def verdict_refusal(changed: list[dict[str, Any]], missing: list[str]) -> str:
    said = "; ".join(f"{item['path']} (Delivery's reason: {item['reason']})" for item in changed if item["path"] in missing)
    return (
        f"Delivery changed test file(s) during this repair: {said}. Name each one in your verdict and say whether the change is fair "
        "or loosens the test, and whether the original failure is still covered (a loosened test means the work is not proven), then submit again."
    )

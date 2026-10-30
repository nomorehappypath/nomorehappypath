# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Revert proof (task 2026-10-26_revert-proof): cheap mutation testing at final review. Information only, never a block.

For each file the work changed, the board runs the stored checks on a temporary copy of the finished work where ONLY that file is put
back to its pre-work version (a new file: removed). A check that then fails "noticed" the file; if every check still passes, no check
notices that file. The result goes to the final reviewer's brief and to the owner as one plain line. Pure helpers here (no board, no
files); board.py runs the checks and keeps the state. Nothing in this module blocks, asks the owner anything or holds a Go ahead.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from harness import red_proof

# Defaults and bounds of the Settings fields (global_settings.PLUMBING_LIMITS routes them; nothing else holds a number).
MAX_FILES_DEFAULT, MAX_FILES_LOW, MAX_FILES_HIGH = 10, 1, 200
BUDGET_DEFAULT, BUDGET_LOW, BUDGET_HIGH = 900, 30, 7200

NOTICED, NOT_NOTICED, SKIPPED, OVER_LIMIT, COULD_NOT_CHECK = "noticed", "not_noticed", "skipped", "over_limit", "could_not_check"

_configured: dict[str, int] = {}


def configure(plumbing: Any) -> None:
    """Install the owner's limits (the project worker passes the settings once at start); anything unusable keeps the default."""
    _configured.clear()
    if not isinstance(plumbing, dict):
        return
    for key, name, low, high in (
        ("revert_proof_max_files", "files", MAX_FILES_LOW, MAX_FILES_HIGH),
        ("revert_proof_budget_seconds", "seconds", BUDGET_LOW, BUDGET_HIGH),
    ):
        value = plumbing.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            _configured[name] = value


def max_files() -> int:
    return _configured.get("files", MAX_FILES_DEFAULT)


def budget_seconds() -> int:
    return _configured.get("seconds", BUDGET_DEFAULT)


_DOC_SUFFIXES = {".md", ".markdown", ".rst", ".txt", ".adoc", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp",
                 ".lock", ".lockb", ".snap"}
_DOC_NAMES = {"license", "notice", "authors", "changelog", "contributing", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "go.sum",
              "uv.lock", "poetry.lock", "pipfile.lock", "cargo.lock", "composer.lock", "gemfile.lock", ".gitignore", ".gitattributes"}


def skip_reason(path: str) -> str:
    """Why a changed file is not put through the revert proof ("" when it is code worth checking)."""
    pure = PurePosixPath(path)
    if red_proof.is_test_file(path):
        return "a test or test setup"
    if pure.suffix.lower() in _DOC_SUFFIXES or pure.name.lower() in _DOC_NAMES:
        return "a document, image or lockfile"
    return ""


def classify(paths: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(files to check up to the cap, files over the cap, files skipped as not code), each sorted."""
    code = sorted(path for path in paths if not skip_reason(path))
    skipped = sorted(path for path in paths if skip_reason(path))
    cap = max_files()
    return code[:cap], code[cap:], skipped


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


def line(result: dict[str, Any] | None) -> str:
    """The owner's one plain line (empty when nothing was measured). The page escapes it when it draws it."""
    if not isinstance(result, dict):
        return ""
    files = [item for item in result.get("files") or [] if isinstance(item, dict)]
    by = lambda outcome: [item["path"] for item in files if item.get("outcome") == outcome]
    noticed, missed = by(NOTICED), by(NOT_NOTICED)
    measured = len(noticed) + len(missed)
    if not files:
        return ""
    if missed:
        head = f"{len(missed)} of {_count(measured, 'changed file', 'changed files')} {'is' if len(missed) == 1 else 'are'} not noticed by any test: {', '.join(missed)}"
    elif measured:
        head = f"All {_count(measured, 'changed file', 'changed files')} {'is' if measured == 1 else 'are'} noticed by at least one test"
    else:
        head = "No changed file could be put through the revert check"
    notes = []
    if by(SKIPPED):
        notes.append(f"{_count(len(by(SKIPPED)), 'file', 'files')} skipped (documents, images, lockfiles, tests)")
    if by(OVER_LIMIT):
        notes.append(f"{_count(len(by(OVER_LIMIT)), 'file', 'files')} not checked: over the limit")
    if by(COULD_NOT_CHECK):
        notes.append(f"{_count(len(by(COULD_NOT_CHECK)), 'file', 'files')} could not be checked")
    return head + (" (" + "; ".join(notes) + ")" if notes else "") + ". For information only."


def label(item: dict[str, Any]) -> str:
    """One file's plain label for the reviewer."""
    outcome = item.get("outcome")
    if outcome == NOTICED:
        return f"noticed: check {item.get('check', '')} failed"
    if outcome == NOT_NOTICED:
        return "not noticed by any check"
    if outcome == OVER_LIMIT:
        return "not checked: over the limit"
    if outcome == SKIPPED:
        return "skipped: " + str(item.get("reason") or "not code")
    return "could not be checked: " + str(item.get("reason") or "")


def brief(result: dict[str, Any] | None) -> dict[str, Any]:
    """What the final reviewer's brief carries ({} when nothing was measured)."""
    if not isinstance(result, dict) or not result.get("files"):
        return {}
    return {
        "summary": line(result),
        "note": "Information only: each changed file was put back to its pre-work version, one at a time, and the stored checks rerun. Judge the work; this never blocks.",
        "files": [{"path": item["path"], "result": label(item)} for item in result["files"]][:200],
    }


def tree_of(request: dict[str, Any]) -> str:
    return str(request.get("reviewed_tree_hash") or request.get("reviewed_commit") or "")

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Standing checks on every task (owner's orders 2026-10-27): security and code quality are part of how NoMoreHappyPath works.

The rules themselves live in the `standing-checks` chapter of directives/AGENT.md. This module is the one gate behind them: it sends a
piece of work back, in plain words, when a required section is missing. It reads text only and never judges whether the check was good.

  requirements   a "Security:" line next to "Non-functional:", so the owner sees how the task is kept safe
  hand-in        the builder's final request-review summary has a "Security:" and a "Code quality:" line
  verdict        the reviewer's PASS has a "Security check:" line, at least one --code-quality line (or one already stored for the task),
                 and, for a report-only task, a "Not examined:" line (it may say nothing)

HARNESS_STANDING_CHECKS=off switches the gate off (the test suite does, for fixtures that are about something else).
"""
from __future__ import annotations

import os
import re
from typing import Any

MIN_CHARS = 8


def enabled() -> bool:
    return os.environ.get("HARNESS_STANDING_CHECKS", "on").strip().lower() not in {"off", "0", "no", "false"}


def has_line(text: Any, label: str, min_chars: int = MIN_CHARS) -> bool:
    """True when some line starts with `label:` (a leading bullet is fine) and says at least a few words after it."""
    pattern = re.compile(rf"^\s*(?:[-*]\s*)?{re.escape(label)}\s*:\s*(\S.*)$", re.I | re.M)
    return any(len(match.group(1).strip()) >= min_chars for match in pattern.finditer(str(text or "")))


def requirements_problem(text: Any) -> str:
    if enabled() and not has_line(text, "Security"):
        return ("add a \"Security:\" line to the requirements, in plain words next to the \"Non-functional:\" lines: how this task keeps the "
                "owner's data and users safe, or why none of it applies. Open `standing-checks`.")
    return ""


def hand_in_problem(summary: Any) -> str:
    if not enabled():
        return ""
    missing = [label for label in ("Security", "Code quality") if not has_line(summary, label)]
    if not missing:
        return ""
    return ("the hand-in is incomplete: its summary needs " + " and ".join(f"a \"{label}:\" line" for label in missing) +
            ". \"Security:\" says what you checked. "
            "\"Code quality:\" says what you did to keep the code readable and maintainable. Open `standing-checks`.")


_VERIFIED_FINDINGS = re.compile(r"^\s*(?:[-*]\s*)?Findings verified independently\s*:\s*(\d+)\s+of\s+(\d+)\b(.*)$", re.I | re.M)


def verified_findings_problem(summary: Any) -> str:
    """A review of a report (an analysis, an audit) says how many of its findings it checked against the code itself, "" when it does.

    2026-10-28 (Acremuse): the reviewer reran the builder's own verification suite as its proof of accuracy and checked two of eighteen findings
    itself. The count must name every finding the report makes; fewer is allowed only with a reason, and the builder's suite is never the proof.
    """
    text = str(summary or "")
    found = _VERIFIED_FINDINGS.search(text)
    how = ('a "Findings verified independently: N of M" line: open the code at every location the report cites and confirm each finding yourself '
           "(the builder's own verification suite and tests are never proof of accuracy); N is how many you confirmed, M how many the report makes")
    if not found:
        return how
    checked, total, rest = int(found.group(1)), int(found.group(2)), found.group(3)
    if checked > total:
        return how
    if checked < total and len(rest.strip(" .:-;,")) < 12:
        return f"a reason after \"Findings verified independently: {checked} of {total}\" saying why the other {total - checked} were not confirmed"
    return ""


_CLEAN_LIST = re.compile(r"^\s*(?:[-*]\s*)?Clean list items in the report\s*:\s*(\d+)\s+of\s+(\d+)\b", re.I | re.M)


def clean_list_problem(summary: Any, total: int) -> str:
    """"" when an analysis verdict says how many items of the board's clean list (the one list built from both passes) are in FINDINGS.md:
    all of them, none dropped or silently changed (a disputed item a side withdrew is still in the report, marked withdrawn)."""
    ask = (f'a "Clean list items in the report: N of {total}" line: open FINDINGS.md and find every item of the clean list in the brief by its id '
           f"(the board's clean list has {total}); a PASS needs N = {total}, and an item the report dropped or changed is a finding")
    found = _CLEAN_LIST.search(str(summary or ""))
    if not found:
        return ask
    if int(found.group(2)) != total:
        return f"the Clean list line must say M = {total}, the number of items in the board's clean list"
    if int(found.group(1)) != total:
        return f"every clean-list item must be in FINDINGS.md before a PASS (the line says {found.group(1)} of {total}); list the missing ones as findings and FAIL instead"
    return ""


_FINDING_ID = re.compile(r"\b([A-Z]{1,2}\d{1,3})\b")


def disputes_problem(hand_in: Any, verdict: Any) -> str:
    """"" unless every finding Delivery disputed is answered on the verdict's Disputes re-checked line: its id, then withdrawn, or kept with
    the reviewer's own file:line evidence. Delivery's line is `B2: <evidence>; B4: <evidence>` (or none)."""
    disputed = _line_text(hand_in, "Disputes")
    if not disputed or disputed.lower().startswith("none"):
        return ""
    ids = []
    for part in re.split(r";", disputed):
        found = re.match(r"\s*([A-Z]{1,2}\d{1,3})\b", part)
        if found and found.group(1) not in ids:
            ids.append(found.group(1))
    answer = _line_text(verdict, "Disputes re-checked")
    ask = ('a "Disputes re-checked:" line answering each finding Delivery disputed by its id: `B2 withdrawn, <why>; B4 kept, <your own file:line '
           "evidence>`; one adjudication round only")
    if not ids:
        return 'the Disputes line must start each dispute with the finding id, e.g. `B2: <file:line evidence>`, or say `Disputes: none`'
    segments = [part.strip() for part in answer.split(";") if part.strip()]
    unanswered = []
    for ident in ids:
        mine = [part for part in segments if re.search(rf"\b{ident}\b", part)]
        ok = any(re.search(r"\bwithdrawn\b", part, re.I) or (re.search(r"\bkept\b", part, re.I) and re.search(r"\S+:\d+", part)) for part in mine)
        if not ok:
            unanswered.append(ident)
    if unanswered:
        return ask + "; not yet answered: " + ", ".join(unanswered)
    return ""


def verdict_problem(summary: Any, quality_notes_given: bool, quality_notes_stored: bool, report_only: bool, analysis: bool = False,
                    clean_total: int | None = None, hand_in: Any = "") -> str:
    if not enabled():
        return ""
    missing = []
    if not has_line(summary, "Security check"):
        missing.append("a \"Security check:\" line saying what you checked yourself and what you found")
    if not (quality_notes_given or quality_notes_stored):
        missing.append("at least one --code-quality line (\"checked, nothing to note\" is enough when that is true)")
    if report_only and not has_line(summary, "Not examined", 1):
        missing.append("a \"Not examined:\" line saying what you did not examine (it may say nothing)")
    if analysis and verified_findings_problem(summary):
        missing.append(verified_findings_problem(summary))
    if analysis and clean_total is not None and clean_list_problem(summary, clean_total):
        missing.append(clean_list_problem(summary, clean_total))
    if analysis and disputes_problem(hand_in, summary):
        missing.append(disputes_problem(hand_in, summary))
    if not missing:
        return ""
    return "the review is incomplete and cannot be recorded as a PASS: it needs " + "; ".join(missing) + ". Open `standing-checks`."


def report_file_name(text: Any) -> str:
    """The file a report-only deliverable names at the start of its text ("FINDINGS.md: a list of ..." -> "FINDINGS.md"), else ""."""
    head = str(text or "").strip().split(None, 1)[0].rstrip(":,;") if str(text or "").strip() else ""
    if head.startswith(("/", "~", "..")) or not re.fullmatch(r"[\w][\w.\-/]*\.[A-Za-z0-9]{1,8}", head):
        return ""
    return head


_TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.[A-Za-z0-9]+$|\.(test|spec)\.[A-Za-z0-9]+$", re.I)
_TEST_DELIVERABLE = re.compile(r"\btests?\b|\bunittest\b|\bpytest\b|\bsuite\b|\bcoverage\b", re.I)


def is_test_file(path: Any) -> bool:
    return bool(_TEST_FILE.search(str(path).replace("\\", "/")))


_DOCUMENT_EXTENSIONS = {".md", ".txt", ".rst", ".pdf", ".docx", ".html", ".htm"}


def _is_report_document(name: Any) -> bool:
    """A named deliverable that is a document ("FINDINGS.md: ..."), never a source file ("app.py: a working web application")."""
    file = report_file_name(name)
    return bool(file) and os.path.splitext(file)[1].lower() in _DOCUMENT_EXTENSIONS


def analysis_deliverables_only(names: list[Any]) -> bool:
    """True when a contract names only report files and tests (an analysis or audit task on the full process, D-014): at least one report
    file, and every other deliverable is about tests. A build, which has any other deliverable, is never limited."""
    names = [str(name) for name in names or [] if str(name).strip()]
    return bool(names) and any(_is_report_document(name) for name in names) and all(
        _is_report_document(name) or _TEST_DELIVERABLE.search(name) for name in names)


_PROOF_NAMES_TESTS = re.compile(r"(?:\btests?/|\btest_\w+|\w+_test\b|\.(?:test|spec)\.[A-Za-z0-9]+)", re.I)


def _line_text(text: Any, label: str) -> str:
    match = re.search(rf"^\s*(?:[-*]\s*)?{re.escape(label)}\s*:\s*(\S.*)$", str(text or ""), re.I | re.M)
    return match.group(1).strip() if match else ""


def analysis_hand_in_problem(summary: Any, files: list[Any]) -> str:
    """A Code analysis hands in the report plus: what it examined, the flows it followed, its helpers and what it dropped from them, and the
    proof of each issue (a test where a test proves it, otherwise a code-evidence paragraph).

    2026-10-28 (Acremuse): helpers split by area missed the money, identity and retry flows; the builder dropped findings its own helpers had made;
    and "a test per issue" produced ten checks of the report's wording. This is the gate behind the rewritten rule. It reads the file list and
    the summary's lines only; whether the proof proves is the Reviewer's check.
    """
    if not enabled():
        return ""
    missing = []
    proof = _line_text(summary, "Proof") or _line_text(summary, "Proof tests")
    if len(proof) < MIN_CHARS:
        missing.append("a \"Proof:\" line saying, for each issue, the proof test's name or the code-evidence paragraph (file:line, the failing path, the "
                       "observable harm) that proves it")
    elif _PROOF_NAMES_TESTS.search(proof) and not any(is_test_file(name) for name in files or []):
        missing.append("the test file your Proof line names (no test file was added to the project)")
    if not has_line(summary, "Examined"):
        missing.append("an \"Examined:\" line listing every part of the code you read, so a part nobody read is visible")
    if not missing:
        return ""
    return ("this is a Code analysis, so the hand-in is incomplete: it needs " + "; ".join(missing) +
            ". Open `standing-checks`.")


def report_only_extra_files(files: list[Any], allowed_paths: list[str]) -> list[str]:
    """Files a report-only task changed in the project that are not the report itself (harness storage excluded)."""
    allowed = {os.path.normpath(str(path)) for path in allowed_paths if path} | {os.path.basename(str(path)) for path in allowed_paths if path}
    extra = []
    for name in files or []:
        name = str(name)
        if name.startswith(".harness") or os.path.normpath(name) in allowed or os.path.basename(name) in allowed:
            continue
        extra.append(name)
    return extra


def report_only_files_problem(extra: list[str]) -> str:
    if not extra or not enabled():
        return ""
    shown = ", ".join(extra[:6]) + (f" and {len(extra) - 6} more" if len(extra) > 6 else "")
    return ("this task only asks for a report (and new test files that prove it), but the project now also holds or changes: " + shown + ". "
            "An analysis points at issues and never fixes them: put every existing file back exactly as it was, keep other scripts and evidence "
            "in your temporary evidence folder, and correct the REPORT instead. Then ask for review again.")

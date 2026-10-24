# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Reviewer's early look at the requirements: submission format, answers and labels.

Pure helpers (no board, no files): board.py holds the state changes, this module
holds what a submission must look like, how the owner's answers become a
"Decided before coding" section, and the plain-word label of each requirement.
Spec: board/tasks/2026-10-23_contract-first-checks_SPEC.md (v2).
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

# The one wait for the Reviewer's early look (seconds). The owner can go ahead without waiting at any time.
WAIT_DEFAULT_SECONDS = 900
WAIT_MIN_SECONDS = 60
WAIT_MAX_SECONDS = 3600
_configured: dict[str, int] = {}

MAX_QUESTIONS = 4
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_CRITERIA = 30
MAX_CHECKS = 12
MAX_FILES_PER_CHECK = 20
MAX_TOTAL_BYTES = 512 * 1024
QUESTION_CHARS = 200
OPTION_CHARS = 150
OTHER_CHARS = 300
CRITERION_CHARS = 400
REQUIREMENTS_LIMIT = 12000          # the same limit propose-requirements and confirm-requirements apply
DECIDED_HEADING = "Decided before coding:"

NOT_CHECKED = "not checked in advance"
PARTLY_CHECKED = "partly checked"
CHECKED = "checked"


def configure(seconds: Any) -> None:
    """Install the owner's wait (the project worker passes the settings value once at start); None restores the default."""
    _configured.clear()
    if isinstance(seconds, int) and not isinstance(seconds, bool) and WAIT_MIN_SECONDS <= seconds <= WAIT_MAX_SECONDS:
        _configured["seconds"] = seconds


def wait_seconds() -> int:
    return _configured.get("seconds", WAIT_DEFAULT_SECONDS)


def _text(value: Any, label: str, limit: int, *, minimum: int = 1) -> str:
    if not isinstance(value, str) or len(" ".join(value.split())) < minimum:
        raise ValueError(f"{label} is required")
    value = " ".join(value.split())
    if len(value) > limit:
        raise ValueError(f"{label} must be {limit} characters or fewer")
    return value


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or "\\" in value or "\0" in value:
        raise ValueError("a check file needs a plain relative path with forward slashes")
    path = PurePosixPath(value.strip())
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or not path.parts:
        raise ValueError(f"check file path {value!r} must stay inside the check's own folder")
    return str(path)


def parse_checks(checks_raw: Any, label: str) -> tuple[list[dict[str, Any]], int]:
    """Validate a list of checks (a ledger plus the test files that assert the result); returns them and their size in bytes."""
    if not isinstance(checks_raw, list):
        raise ValueError(f"{label} checks must be a list")
    total_bytes = 0
    checks = []
    for position, check in enumerate(checks_raw, 1):
        where = f"{label} check {position}"
        if not isinstance(check, dict):
            raise ValueError(f"{where} must be an object")
        coverage = check.get("coverage", CHECKED)
        if coverage not in {"checked", "partly"}:
            raise ValueError(f"{where}: coverage must be checked or partly")
        ledger = check.get("ledger")
        if not isinstance(ledger, str) or not ledger.strip():
            raise ValueError(f"{where} needs its scenario ledger text")
        files_raw = check.get("files")
        if not isinstance(files_raw, dict) or not files_raw:
            raise ValueError(f"{where} needs at least one test file that asserts the result (files)")
        if len(files_raw) > MAX_FILES_PER_CHECK:
            raise ValueError(f"{where}: at most {MAX_FILES_PER_CHECK} files")
        files: dict[str, str] = {}
        for name, body in files_raw.items():
            relative = _relative_path(name)
            if relative in files:
                raise ValueError(f"{where}: file {relative} is listed twice")
            if not isinstance(body, str):
                raise ValueError(f"{where}: file {relative} must be text")
            files[relative] = body
            total_bytes += len(body.encode("utf-8"))
        total_bytes += len(ledger.encode("utf-8"))
        checks.append({"coverage": coverage, "ledger": ledger, "files": files})
    return checks, total_bytes


def parse_submission(raw: str) -> dict[str, Any]:
    """Validate the JSON a Reviewer uploads; raise ValueError with a plain reason, return the normalised form."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("the submission must be a JSON file") from error
    if not isinstance(data, dict):
        raise ValueError("the submission must be a JSON object with criteria and questions")
    criteria_raw = data.get("criteria")
    if not isinstance(criteria_raw, list) or not criteria_raw:
        raise ValueError("list every acceptance criterion under criteria, each with its checks or the reason it has none")
    if len(criteria_raw) > MAX_CRITERIA:
        raise ValueError(f"at most {MAX_CRITERIA} criteria")
    total_bytes = 0
    total_checks = 0
    criteria = []
    for number, item in enumerate(criteria_raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"criterion {number} must be an object")
        text = _text(item.get("text"), f"criterion {number} text", CRITERION_CHARS, minimum=5)
        sharper = _text(item["sharper"], f"criterion {number} sharper sentence", CRITERION_CHARS) if item.get("sharper") else ""
        checks_raw = item.get("checks") or []
        if not isinstance(checks_raw, list):
            raise ValueError(f"criterion {number} checks must be a list")
        why = ""
        if not checks_raw:
            why = _text(item.get("why_not_checked"), f"criterion {number} needs a check or the reason it has none (why_not_checked)", CRITERION_CHARS, minimum=3)
        checks, check_bytes = parse_checks(checks_raw, f"criterion {number}")
        total_bytes += check_bytes
        total_checks += len(checks)
        criteria.append({"n": number, "text": text, "sharper": sharper, "why_not_checked": why, "checks": checks})
    if total_checks > MAX_CHECKS:
        raise ValueError(f"at most {MAX_CHECKS} checks in one submission")
    if total_bytes > MAX_TOTAL_BYTES:
        raise ValueError(f"the checks are larger than {MAX_TOTAL_BYTES // 1024} KB together")
    questions_raw = data.get("questions") or []
    if not isinstance(questions_raw, list):
        raise ValueError("questions must be a list")
    if len(questions_raw) > MAX_QUESTIONS:
        raise ValueError(f"at most {MAX_QUESTIONS} questions, once; keep only the product choices you cannot write a check without")
    questions = []
    for number, item in enumerate(questions_raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"question {number} must be an object")
        options_raw = item.get("options")
        if not isinstance(options_raw, list) or not MIN_OPTIONS <= len(options_raw) <= MAX_OPTIONS:
            raise ValueError(f"question {number} needs {MIN_OPTIONS} to {MAX_OPTIONS} options")
        options = [_text(option, f"question {number} option", OPTION_CHARS) for option in options_raw]
        recommended = item.get("recommended")
        if isinstance(recommended, bool) or not isinstance(recommended, int) or not 0 <= recommended < len(options):
            raise ValueError(f"question {number} must mark one option as recommended (its position, starting at 0)")
        affects_raw = item.get("affects") or []
        if not isinstance(affects_raw, list) or any(isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= len(criteria) for n in affects_raw):
            raise ValueError(f"question {number} affects must list existing criterion numbers (1 to {len(criteria)})")
        questions.append({
            "n": number, "text": _text(item.get("text"), f"question {number} text", QUESTION_CHARS, minimum=5),
            "options": options, "recommended": recommended, "affects": sorted(set(affects_raw)),
        })
    return {"criteria": criteria, "questions": questions}


def resolve_answers(questions: list[dict[str, Any]], answers: Any) -> tuple[list[dict[str, Any]], bool]:
    """The owner's answers (or the recommended ones) as plain records; True when every answer is the recommended default."""
    given: dict[int, dict[str, Any]] = {}
    if answers:
        if not isinstance(answers, list):
            raise ValueError("answers must be a list")
        for item in answers:
            if not isinstance(item, dict) or isinstance(item.get("question"), bool) or not isinstance(item.get("question"), int):
                raise ValueError("each answer needs the question number")
            if item["question"] in given or not any(q["n"] == item["question"] for q in questions):
                raise ValueError(f"answer for question {item['question']} does not match an open question")
            given[item["question"]] = item
    resolved = []
    for question in questions:
        item = given.get(question["n"], {})
        other = " ".join(str(item.get("other") or "").split())
        if len(other) > OTHER_CHARS:
            raise ValueError(f"an Other answer must be {OTHER_CHARS} characters or fewer")
        choice = item.get("choice")
        if other:
            answer, recommended, is_other = other, False, True
        else:
            if choice is None:
                choice = question["recommended"]
            if isinstance(choice, bool) or not isinstance(choice, int) or not 0 <= choice < len(question["options"]):
                raise ValueError(f"question {question['n']}: choose one of the listed options or write an Other answer")
            answer, recommended, is_other = question["options"][choice], choice == question["recommended"], False
        resolved.append({"question": question["n"], "text": question["text"], "answer": answer, "recommended": recommended, "other": is_other, "affects": list(question["affects"])})
    return resolved, all(item["recommended"] for item in resolved)


def append_decided(text: str, resolved: list[dict[str, Any]]) -> str:
    lines = [f"- {item['text']} -> {item['answer']}{' (recommended)' if item['recommended'] else ''}" for item in resolved]
    return text.rstrip() + "\n\n" + DECIDED_HEADING + "\n" + "\n".join(lines)


def worst_case_decided_length(questions: list[dict[str, Any]]) -> int:
    """The longest "Decided before coding" section any set of answers could add (Other answers at their cap)."""
    longest = max(OTHER_CHARS, *(len(option) for q in questions for option in q["options"])) if questions else 0
    return len(DECIDED_HEADING) + 4 + sum(len(q["text"]) + longest + 18 for q in questions)


def criterion_label(criterion: dict[str, Any]) -> str:
    """checked only when every check of the criterion fully covers it; no check, or a check dropped after an answer, is not checked."""
    checks = criterion.get("checks") or []
    if criterion.get("stale") or not checks:
        return NOT_CHECKED
    return CHECKED if all(check.get("coverage") == "checked" for check in checks) else PARTLY_CHECKED


def labels(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"n": item["n"], "criterion": item["text"], "label": criterion_label(item)} for item in criteria]


def reviewer_instruction(task: str, version: int, requirements: str, direction: str, minutes: int) -> str:
    return (
        f"CONTRACT CHECK ROUTED: task {task}, requirements version {version}. This is advisory preparation, not a review round: "
        "do not reserve, claim or review anything for it, and no code exists yet. Read the owner's original direction and the "
        "requirements below and write your checks from them only (black box: commands and their printed output, HTTP calls, or visible text "
        "on a page, each as a test file that asserts the result). Then submit ONCE with "
        f"contract-check-submit --task {task} --version {version} --submission <your JSON file>; the format is in your directive "
        "(Independent Reviewer workflow, Contract check before coding). Ask the owner at most 4 product questions, only where you cannot write "
        "a check without guessing, each with 2 to 4 options and one recommended. The owner may go ahead without waiting and a submission after "
        f"that, or after about {max(1, minutes)} minutes, is refused. Do not start any polling or background helper. USER ACTION: None.\n"
        f"ORIGINAL OWNER DIRECTION:\n{direction}\nREQUIREMENTS (version {version}):\n{requirements}"
    )


def recheck_instruction(task: str, version: int, answers: list[dict[str, Any]], changed: list[int], criteria: list[dict[str, Any]], minutes: int) -> str:
    said = "; ".join(f"{item['text']} -> {item['answer']}" for item in answers)
    names = "; ".join(f"{item['n']}. {item['text']}" for item in criteria if item["n"] in changed)
    return (
        f"CONTRACT RECHECK ROUTED: task {task}, requirements version {version}. The owner answered your questions: {said}. "
        f"Advisory preparation again, not a review round. The answers changed these requirements, so their old checks were set aside: {names}. "
        f"For each, write the new checks from the answers and submit them with contract-check-correct --task {task} --criterion <n> "
        "--reason \"updated for the owner's answers\" --submission <JSON file with {\"checks\": [...]}>. When nothing more needs changing, finish with "
        f"contract-check-correct --task {task} --criterion 0 --reason \"nothing more to change\" --submission <JSON file with {{\"checks\": []}}>. "
        f"The owner may go ahead without waiting and a submission after that, or after about {max(1, minutes)} minutes, is refused. "
        "Do not start any polling or background helper. Never ask the owner anything in your terminal. USER ACTION: None."
    )

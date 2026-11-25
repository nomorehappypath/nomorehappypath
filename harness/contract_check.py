# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Reviewer's early look at the requirements: submission format, answers and labels.

Pure helpers (no board, no files): board.py holds the state changes, this module
holds what a submission must look like, how the owner's answers become a
"Decided before coding" section, and the plain-word label of each requirement.
Spec: board/tasks/2026-10-23_contract-first-checks_SPEC.md (v2).
"""
from __future__ import annotations

import json
import ast
import re
from pathlib import PurePosixPath
from typing import Any

# The one wait for the Reviewer's early look (seconds). The owner can go ahead without waiting at any time.
WAIT_DEFAULT_SECONDS = 1800      # task 2026-11-24_tdd-examples-first: examples, the asks list and the result list take longer to write; still only a default
WAIT_MIN_SECONDS = 60
WAIT_MAX_SECONDS = 3600
_configured: dict[str, int] = {}

MAX_QUESTIONS = 4
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_CRITERIA = 30
MAX_CHECKS = 60        # task 2026-11-24_tdd-examples-first: one real check per example must fit (was 12; the 512 KB limit below stays)
MAX_FILES_PER_CHECK = 20
# Functions that run a command: a call to one of these whose first argument starts with "git" is a git invocation. Words in
# strings, assertion data or comments ("the README documents git diff --stat") are not (round 1 of 2026-11-04_checks-no-git).
COMMAND_RUNNERS = {"run", "call", "check_call", "check_output", "Popen", "system", "popen", "getoutput", "getstatusoutput",
                   "execv", "execvp", "execve", "execvpe", "execl", "execlp", "spawnv", "spawnvp", "spawnl", "spawnlp"}
SHELL_GIT_LINE = re.compile(r"^\s*(sudo\s+|env\s+\S+=\S+\s+)*git\s")


def _starts_with_git(node: Any) -> bool:
    """The command argument of a runner call begins with git: "git …", ["git", …], ("git", …) or an f-string starting with git."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == "git" or node.value.startswith("git ")
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return _starts_with_git(node.elts[0])
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        return isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.startswith("git ")
    return False


def runs_git(relative: str, body: str) -> bool:
    """True when a check file would run git: a runner call with a git command (Python), or a shell line that starts with git."""
    if relative.endswith(".py"):
        try:
            tree = ast.parse(body)
        except SyntaxError:
            return False      # not this check's concern; the file fails on its own when the board runs it
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in COMMAND_RUNNERS and node.args and _starts_with_git(node.args[0]):
                return True
        return False
    return any(SHELL_GIT_LINE.match(line) for line in body.splitlines())
MAX_TOTAL_BYTES = 512 * 1024
QUESTION_CHARS = 200
OPTION_CHARS = 150
OTHER_CHARS = 300
CRITERION_CHARS = 400
REQUIREMENTS_LIMIT = 12000          # the same limit propose-requirements and confirm-requirements apply
DECIDED_HEADING = "Decided before coding:"

MAX_FAILURE_PATHS = 10
COVERED = "covered"
NOT_MAPPED = "not mapped"
NOT_MAPPED_NEW = "not mapped: the reviewer did not say which test covers this"
GAP_NO_TEST = "gap: no test covers this yet"
GAP_NORMAL_ONLY = "gap: only the normal case is tested, no failure or unusual case"

# --- Examples first, real tests only (task 2026-11-24_tdd-examples-first) ----------------------------------------------------------------
# A criterion that carries "examples" follows the new rules: a stored check is COUNTED as a real test only when the Reviewer
# recorded what it runs and which example it asserts and marked it checked; everything else is stored and run but never
# counted, and its requirement is tested for real at the final review. A criterion without "examples" is an older record and
# keeps the older rules untouched (a task past its contract keeps its rules).
MAX_EXAMPLES = 4
EXAMPLE_MIN = 20
EXAMPLE_MAX = 300
MAX_ASKS = 60
ASK_CHARS = 200
MAX_NOT_READ = 20
MAX_RESULT_ITEMS = 6           # what the owner asked the result to contain, one thing a line; a long expert list made no real result pass (first real run)
RESULT_ITEM_CHARS = 120
RUNS_CHARS = 200
KIND_DOCUMENT = "document"         # a requirement about a delivered document (a licence, a README): checked by reading it
KIND_SOURCE = "source"             # a requirement that changes the source everywhere (a rename, a removal): checked by reading it
READING_KINDS = {KIND_DOCUMENT, KIND_SOURCE}
GAP_NOT_COUNTED = "gap: its checks do not run the program against an example; it is tested for real at the final review"
# What a check may import and still only be reading files: with nothing but these and no command run, it cannot be running the program.
_READING_ONLY_MODULES = {"__future__", "unittest", "pathlib", "os", "re", "json", "sys", "io", "typing", "glob", "fnmatch", "hashlib",
                         "textwrap", "string", "collections", "itertools", "functools", "datetime", "codecs", "difflib"}


_FILE_READS = {"read_text", "read_bytes", "open", "is_file", "exists", "is_dir", "iterdir", "glob", "rglob", "listdir"}


def only_reads_files(files: dict[str, str]) -> bool:
    """True when every file of a check is Python that reads files, runs no command and imports nothing beyond file-reading
    helpers: such a check cannot be running the program. One filter for the obvious case only; passing it proves nothing
    (Codex plan check R1-1)."""
    if not files or not all(name.endswith(".py") for name in files):
        return False
    reads = False
    for body in files.values():
        try:
            tree = ast.parse(body)
        except SyntaxError:
            return False        # the file fails on its own when the board runs it; not this filter's concern
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
                if name in COMMAND_RUNNERS:
                    return False
                if name in _FILE_READS:
                    reads = True
            elif isinstance(node, ast.Import):
                if any(alias.name.split(".")[0] not in _READING_ONLY_MODULES for alias in node.names):
                    return False
            elif isinstance(node, ast.ImportFrom):
                if node.level or (node.module or "").split(".")[0] not in _READING_ONLY_MODULES:
                    return False
    return reads


def check_counted(check: dict[str, Any]) -> bool:
    """A stored check counts as a real test: marked checked, with the program entry point it runs and the example it asserts."""
    return check.get("coverage") == "checked" and bool(check.get("runs")) and bool(check.get("example"))


def new_rules(criterion: dict[str, Any]) -> bool:
    """This requirement was recorded with examples, so the examples-first rules apply to it (older records keep theirs)."""
    return bool(criterion.get("examples"))


def criterion_counted(criterion: dict[str, Any]) -> bool:
    """The requirement has a real stored test: a counted check of its own, or, for a document or source-wide requirement, its reading check."""
    checks = [] if criterion.get("stale") else (criterion.get("checks") or [])
    if criterion.get("kind") in READING_KINDS:
        return bool(checks)
    return any(check_counted(check) for check in checks)


def real_test_line(criteria: list[dict[str, Any]]) -> str:
    """The one honest count for the owner ("" for an older record): how many requirements about what the app does have a real test before the build."""
    rows = [item for item in criteria if new_rules(item) and item.get("kind") not in READING_KINDS]
    if not rows:
        return ""
    also = covered_by_others(criteria)
    counted = sum(1 for item in rows if criterion_counted(item) or also.get(item["n"], 0))
    return f"{counted} of {len(rows)} requirements have a real test before the build; the others are tested for real at the final review."


def parse_examples(item: dict[str, Any], label: str, required: bool) -> list[str]:
    """The requirement's examples: one to four plain sentences, each saying what goes in or what the user does and the exact result."""
    raw = item.get("examples")
    if raw is None and not required:
        return []
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_EXAMPLES:
        raise ValueError(f"{label} needs \"examples\": one to {MAX_EXAMPLES} plain sentences, each saying what goes in or what the user does and the exact "
                         "result (for example: \"Typing Chicago and pressing Check weather shows today's temperature for Chicago.\")")
    examples = []
    for position, value in enumerate(raw, 1):
        problem = plain_problem(value, EXAMPLE_MAX)
        if not problem and len(" ".join(value.split())) < EXAMPLE_MIN:
            problem = f"it is too short to be an example ({EXAMPLE_MIN} characters at least)"
        if problem:
            raise ValueError(f"{label} example {position} is not a plain example the owner can read: {problem}")
        examples.append(" ".join(value.split()))
    return examples


def parse_added_requirement(data: dict[str, Any], number: int) -> dict[str, Any]:
    """A requirement of the accepted text that the early list left out, added by the final Reviewer: its text, its plain line
    for the owner, its examples and its kind. Raises ValueError with the plain reason."""
    text = _text(data.get("text"), f"requirement {number} text", CRITERION_CHARS, minimum=5)
    problem = plain_problem(data.get("plain"))
    if problem:
        raise ValueError(f"requirement {number} needs a `plain` sentence the owner can read (at most {PLAIN_LIMIT} characters): {problem}")
    return {"text": text, "plain": " ".join(data["plain"].split()), "examples": parse_examples(data, f"requirement {number}", True),
            "kind": requirement_kind(data, text)}


def parse_owner_lists(data: dict[str, Any], criteria_count: int, required: bool) -> dict[str, Any]:
    """The three lists the Reviewer writes from the owner's own direction and files: what he asked for and which requirements
    cover each ask, any source it could not read, and what the finished result of his scenario must contain at least."""
    asks_raw = data.get("owner_asks")
    if asks_raw is None and not required:
        asks_raw = []
    if not isinstance(asks_raw, list) or len(asks_raw) > MAX_ASKS or (required and not asks_raw):
        raise ValueError(f"list under \"owner_asks\" every distinct thing the owner asked for in the direction, the clarifications and the attached files "
                         f"(one to {MAX_ASKS} plain lines, related ones combined, none dropped), each as {{\"ask\": \"...\", \"requirements\": [<numbers that cover it>]}}; "
                         "use [] for an ask no requirement covers")
    asks = []
    for position, item in enumerate(asks_raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"owner ask {position} must be an object with ask and requirements")
        numbers = item.get("requirements")
        if not isinstance(numbers, list) or any(isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= criteria_count for n in numbers):
            raise ValueError(f"owner ask {position}: requirements must list existing requirement numbers (1 to {criteria_count}), or [] when none covers it")
        asks.append({"ask": _text(item.get("ask"), f"owner ask {position}", ASK_CHARS, minimum=5), "requirements": sorted(set(numbers))})
    not_read_raw = data.get("not_read") or []
    if not isinstance(not_read_raw, list) or len(not_read_raw) > MAX_NOT_READ:
        raise ValueError(f"not_read must list at most {MAX_NOT_READ} sources you could not read (a file name each)")
    not_read = [_text(value, f"not_read {position}", ASK_CHARS, minimum=2) for position, value in enumerate(not_read_raw, 1)]
    result_raw = data.get("result_must_contain")
    if result_raw is None and not required:
        result_raw = []
    if not isinstance(result_raw, list) or len(result_raw) > MAX_RESULT_ITEMS or (required and not result_raw):
        raise ValueError(f"list under \"result_must_contain\" only what the owner asked the finished result of his main scenario to contain: one to "
                         f"{MAX_RESULT_ITEMS} plain lines, one thing each, in his words; never your own expert answer or its details")
    result = [_text(value, f"result item {position}", RESULT_ITEM_CHARS, minimum=5) for position, value in enumerate(result_raw, 1)]
    return {"owner_asks": asks, "not_read": not_read, "result_must_contain": result}


def asks_not_covered(asks: list[dict[str, Any]]) -> list[str]:
    """The owner's asks that no requirement covers, in the Reviewer's plain words."""
    return [item["ask"] for item in asks or [] if not item.get("requirements")]


def asks_line(asks: list[dict[str, Any]], not_read: list[str]) -> str:
    """The one sentence above Go ahead. "Everything was found" is the Reviewer's finding and is said only when nothing is missing
    and nothing was left unread; it is never inferred from the length of the list (Codex plan check, answer 4)."""
    if not asks:
        return ""
    missing = asks_not_covered(asks)
    if missing:
        return "In your direction, not in the requirements: " + "; ".join(missing)
    if not_read:
        return "The reviewer could not read: " + "; ".join(not_read) + ". What it could read is in the requirements."
    return f"The reviewer found everything you asked for in the requirements ({len(asks)} points)."


def asks_request(task: str, missing: list[str]) -> str:
    """The one message telling the Builder which of the owner's asks the requirements leave out."""
    return (f"CONTRACT CHECK: task {task}. The Reviewer read the owner's direction and files and found these asks in none of your requirements: "
            + "; ".join(missing) + ". File a revised proposal with propose-requirements now (it replaces the waiting one, once) that adds each, or says in one "
            "plain line why it is left out; otherwise do nothing. The owner sees the list on the Go ahead card. USER ACTION: None.")


NOT_CHECKED = "not checked in advance"
PARTLY_CHECKED = "partly checked"
CHECKED = "checked"


# --- Plain words for the owner (owner 2026-10-24: "it is gibberish") ------------------------------------------------------------------------
PLAIN_LIMIT = 140                 # one sentence per requirement in "What will be tested"
PLAIN_MIN_WORDS = 5
SUMMARY_HEADING = "In plain words:"
SUMMARY_MAX_LINES = 3
SUMMARY_LINE_LIMIT = 200
_PLAIN_RULES = (
    (re.compile(r"`"), "a backtick (code)"),
    (re.compile(r"\bsections?\s*\d|§", re.I), "a section number"),
    (re.compile(r"\b[\w./-]+\.(?:py|js|ts|md|json|sh|html|css)\b", re.I), "a file name"),
    (re.compile(r"\b(?:GIVEN|WHEN|THEN)\b"), "GIVEN / WHEN / THEN"),
    (re.compile(r"\b(?:spec|delta)\b", re.I), "the word spec or delta"),
    (re.compile(r"\b\w+\("), "a function call"),
    (re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b"), "a code name with underscores"),
    (re.compile(r"\bD-\d{3}\b"), "a decision number"),
)
_COMMIT_ID = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")


NON_FUNCTIONAL = "non-functional"
_NFR_KINDS = {"non-functional", "nonfunctional", "non functional", "quality", "nfr"}
_NFR_PREFIX = re.compile(r"^\s*(non[- ]?functional|quality requirement)\s*:", re.I)


def requirement_kind(item: dict[str, Any], text: str) -> str:
    """`non-functional` (speed, phone layout, accessibility...) when the requirement says so by `kind` or by a leading
    "Non-functional:" / "Quality requirement:"; anything else, or nothing, is an ordinary requirement. Never refused."""
    kind = " ".join(str(item.get("kind") or "").lower().split())
    if kind in READING_KINDS:
        return kind         # checked by reading the delivered document or source, labelled as such, never counted as an app test
    return NON_FUNCTIONAL if kind in _NFR_KINDS or _NFR_PREFIX.match(text) else ""


_FOR_EACH = re.compile(r"\b(for each[^.\n]{8,200})", re.I)


def no_program_reason(text: Any) -> str:
    """What the owner sees instead of "nothing to check" for a task that makes a document: the document's required content is the contract."""
    found = _FOR_EACH.search(" ".join(str(text or "").split()))
    content = f" Required content: {found.group(1).strip().rstrip(',;:')}." if found else ""
    return ("this task makes a report, not a program, so nothing is tested in advance; the contract is what the report must contain." + content +
            " The independent review reads the report against exactly this.")


def plain_problem(value: Any, limit: int = PLAIN_LIMIT, *, min_words: int = PLAIN_MIN_WORDS) -> str:
    """Why this is not plain words for the owner ("" when it is). Deterministic: the board and the tests use the same rules."""
    if not isinstance(value, str) or not value.strip():
        return "it is empty"
    text = " ".join(value.split())
    if len(text) > limit:
        return f"it is {len(text)} characters; the limit is {limit}"
    if len(text.split()) < min_words:
        return f"it is too short to say anything ({min_words} words at least)"
    for pattern, name in _PLAIN_RULES:
        if pattern.search(text):
            return f"it contains {name}, which the owner cannot read"
    if _COMMIT_ID.search(text):
        return "it contains a commit or code id, which the owner cannot read"
    return ""


PLAIN_SUMMARY_EXAMPLE = (
    "Good: \"In plain words:\nA forecast page will show the next five days.\nYou will see a high and a low for each day.\n"
    "Nothing for you to do.\n\n<the full requirements>\". "
    "Bad: a block that names a spec section, a file, a function or a commit, or no block at all."
)


def split_plain_summary(text: Any) -> tuple[list[str], str]:
    """(plain lines, the whole text) when the text starts with a valid "In plain words:" block, else raises ValueError with the fix.

    The block is one to three short lines, then a blank line, then the full requirements. The whole stored text (block included) is what
    the owner accepts and Delivery confirms verbatim, so the second value is the text unchanged.
    """
    raw = str(text or "").replace("\r\n", "\n").strip()
    head = raw.split("\n", 1)[0]
    if head.lower().startswith(SUMMARY_HEADING.lower()) and head[len(SUMMARY_HEADING):].strip():
        # "In plain words: A page will ..." on one line is the same block (agents write it that way, 2026-10-28)
        raw = SUMMARY_HEADING + "\n" + head[len(SUMMARY_HEADING):].strip() + raw[len(head):]
    lines = raw.split("\n")
    if not lines or lines[0].strip().lower() != SUMMARY_HEADING.lower():
        raise ValueError(f"start the requirements with an \"{SUMMARY_HEADING}\" block for the owner: one to {SUMMARY_MAX_LINES} short lines (what will be built, what changes for them, whether they must do anything), then a blank line, then the full requirements. {PLAIN_SUMMARY_EXAMPLE}")
    block: list[str] = []
    index = 1
    while index < len(lines) and lines[index].strip():
        block.append(lines[index].strip())
        index += 1
    rest = "\n".join(lines[index:]).strip()
    if not 1 <= len(block) <= SUMMARY_MAX_LINES:
        raise ValueError(f"the \"{SUMMARY_HEADING}\" block must have one to {SUMMARY_MAX_LINES} lines, then a blank line. {PLAIN_SUMMARY_EXAMPLE}")
    if not rest:
        raise ValueError(f"after the \"{SUMMARY_HEADING}\" block, a blank line and the full requirements must follow. {PLAIN_SUMMARY_EXAMPLE}")
    for number, line in enumerate(block, 1):
        problem = plain_problem(line, SUMMARY_LINE_LIMIT, min_words=3)
        if problem:
            raise ValueError(f"line {number} of the \"{SUMMARY_HEADING}\" block is not plain words: {problem}. {PLAIN_SUMMARY_EXAMPLE}")
    return block, raw


FIT_HEADING = "Already in the project:"
FIT_EXAMPLE = (
    "Good: \"Already in the project:\nThe sign-in page you accepted last week already exists.\nThis change adds a reset link to it instead of "
    "building a second page.\n\n<the full requirements>\". Say \"Nothing related exists yet.\" when that is true."
)


def split_fit_block(text: Any) -> tuple[list[str], str]:
    """(fit lines, the whole text) when the text, which already starts with a valid plain block, continues with a valid "Already in the project:" block, else raises ValueError with the fix.

    Takes the text split_plain_summary returned. The block follows the plain block and its blank line: one to three short lines under the same
    plain-words rules, then a blank line, then the full requirements. Only the shape is checked; whether the lines are true is the Reviewer's judgment.
    """
    lines = str(text or "").split("\n")
    index = 1
    while index < len(lines) and lines[index].strip():
        index += 1                      # past the plain block
    while index < len(lines) and not lines[index].strip():
        index += 1                      # past the blank line
    if index >= len(lines) or not lines[index].strip().lower().startswith(FIT_HEADING.lower()):
        raise ValueError(f"after the \"{SUMMARY_HEADING}\" block and a blank line, add an \"{FIT_HEADING}\" block: one to {SUMMARY_MAX_LINES} short lines saying what accepted work or existing code this task touches and how the new work fits or extends it, then a blank line, then the full requirements. {FIT_EXAMPLE}")
    same_line = lines[index].strip()[len(FIT_HEADING):].strip()
    if same_line:
        # "Already in the project: The page exists." on one line is the same block (as for the plain block)
        lines[index:index + 1] = [FIT_HEADING, same_line]
    block: list[str] = []
    after = index + 1
    while after < len(lines) and lines[after].strip():
        block.append(lines[after].strip())
        after += 1
    if not 1 <= len(block) <= SUMMARY_MAX_LINES:
        raise ValueError(f"the \"{FIT_HEADING}\" block must have one to {SUMMARY_MAX_LINES} lines, then a blank line. {FIT_EXAMPLE}")
    if not "\n".join(lines[after:]).strip():
        raise ValueError(f"after the \"{FIT_HEADING}\" block, a blank line and the full requirements must follow. {FIT_EXAMPLE}")
    for number, line in enumerate(block, 1):
        problem = plain_problem(line, SUMMARY_LINE_LIMIT, min_words=3)
        if problem:
            raise ValueError(f"line {number} of the \"{FIT_HEADING}\" block is not plain words: {problem}. {FIT_EXAMPLE}")
    return block, "\n".join(lines)


def configure(seconds: Any) -> None:
    """Install the owner's wait (the project worker passes the settings value once at start); None restores the default."""
    _configured.clear()
    if isinstance(seconds, int) and not isinstance(seconds, bool) and WAIT_MIN_SECONDS <= seconds <= WAIT_MAX_SECONDS:
        _configured["seconds"] = seconds


def wait_seconds() -> int:
    """Give the wait the owner configured, or WAIT_DEFAULT_SECONDS when configure() set none. Returns the wait in seconds."""
    return _configured.get("seconds", WAIT_DEFAULT_SECONDS)


def _text(value: Any, label: str, limit: int, *, minimum: int = 1) -> str:
    """Collapse value's whitespace and check it is a string of at least minimum and at most limit characters, raising ValueError naming label if not. Return the cleaned value."""
    if not isinstance(value, str) or len(" ".join(value.split())) < minimum:
        raise ValueError(f"{label} is required")
    value = " ".join(value.split())
    if len(value) > limit:
        raise ValueError(f"{label} must be {limit} characters or fewer")
    return value


def _relative_path(value: Any) -> str:
    """Check that value is a plain forward-slash relative path, with no backslash, null byte, absolute prefix or `.`/`..` segment that could escape the check's own folder. Return the normalised path as a string."""
    if not isinstance(value, str) or not value.strip() or "\\" in value or "\0" in value:
        raise ValueError("a check file needs a plain relative path with forward slashes")
    path = PurePosixPath(value.strip())
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or not path.parts:
        raise ValueError(f"check file path {value!r} must stay inside the check's own folder")
    return str(path)


def parse_checks(checks_raw: Any, label: str, *, examples: int | None = None, reading: bool = False) -> tuple[list[dict[str, Any]], int]:
    """Validate a list of checks (a ledger plus the test files that assert the result); returns them and their size in bytes.

    `examples` is the number of examples of the requirement the checks belong to (None for an older record, which keeps the
    older rules); `reading` says the requirement is a document or source-wide one. Under the examples-first rules a check
    marked checked must record what it runs and which example it asserts, and a check for what the app does that only reads
    files is refused (task 2026-11-24_tdd-examples-first, Change 3)."""
    if not isinstance(checks_raw, list):
        raise ValueError(f"{label} checks must be a list")
    total_bytes = 0
    checks = []
    for position, check in enumerate(checks_raw, 1):
        where = f"{label} check {position}"
        if not isinstance(check, dict):
            raise ValueError(f"{where} must be an object")
        if "walk" in check:
            # a walk (task 2026-11-24_tdd-walks): steps on the app's screens, written as data, stored as this requirement's check
            from harness import walks
            number = check.get("example")
            if examples is None or isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= examples:
                raise ValueError(f"{where}: a walk shows one of the requirement's examples: add \"example\" (1 to {examples or 1})")
            entry = {**walks.parse(check["walk"], where), "example": number}
            total_bytes += sum(len(body.encode("utf-8")) for body in entry["files"].values())
            checks.append(entry)
            continue
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
        # Checks run in a copy of the project that has no git (the disposable checkout), so a check that runs git can never pass,
        # whatever the work is. Refused here, when it is written, not weeks later at final acceptance (2026-11-04).
        for relative, body in files.items():
            if runs_git(relative, body):
                raise ValueError(f"{where}: file {relative} runs git; checks run in a copy of the project with no git. Compare against "
                                 "the stored baseline copy (the files as they were) instead, never through git commands")
        entry = {"coverage": coverage, "ledger": ledger, "files": files}
        if check.get("runs"):
            entry["runs"] = _text(check["runs"], f"{where} runs", RUNS_CHARS, minimum=2)
        if check.get("example") is not None:
            number = check["example"]
            if isinstance(number, bool) or not isinstance(number, int) or number < 1 or (examples is not None and number > examples):
                raise ValueError(f"{where}: example must be the number of one of the requirement's examples (1 to {examples or 1})")
            entry["example"] = number
        if examples is not None and not reading:
            if only_reads_files(files):
                raise ValueError(f"{where} only reads files, so it cannot show what the program does: a check for an app requirement runs the program "
                                 "(a command, an address or its own function) and compares the result with an example. If the requirement really is about a "
                                 "delivered document, or changes the source everywhere, declare it with \"kind\": \"document\" or \"kind\": \"source\"")
            if coverage == "checked" and not check_counted(entry):
                raise ValueError(f"{where} is marked checked but does not say what it runs and which example it asserts: add \"runs\" (the command, "
                                 "address or function of the program it exercises) and \"example\" (1 to " + str(examples) + "), or mark it "
                                 "\"coverage\": \"partly\" (a syntax check, an install, a start with no example asserted is partly: stored and run, never counted)")
        if "covers" in check:
            covers = check["covers"]
            if not isinstance(covers, list) or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in covers):
                raise ValueError(f"{where}: covers must list requirement numbers")
            entry["covers"] = sorted(set(covers))
        checks.append(entry)
    return checks, total_bytes


def parse_failure_paths(item: dict[str, Any], label: str) -> tuple[list[str], str, bool]:
    """(failure paths and edge cases the checks cover, the reason none apply, whether the Reviewer said anything about it)."""
    said = "failure_paths" in item or "no_failure_paths_because" in item
    raw = item.get("failure_paths") or []
    if not isinstance(raw, list) or len(raw) > MAX_FAILURE_PATHS:
        raise ValueError(f"{label} failure_paths must be a list of at most {MAX_FAILURE_PATHS} short sentences")
    paths = [_text(entry, f"{label} failure path", CRITERION_CHARS) for entry in raw]
    none = _text(item["no_failure_paths_because"], f"{label} no_failure_paths_because", CRITERION_CHARS, minimum=3) if item.get("no_failure_paths_because") else ""
    return paths, none, said


def covered_by_others(criteria: list[dict[str, Any]]) -> dict[int, int]:
    """How many current (non-stale) checks of OTHER requirements say they also cover each requirement; always recomputed from the stored checks."""
    counts: dict[int, int] = {}
    for item in criteria:
        if item.get("stale"):
            continue
        for check in item.get("checks") or []:
            if new_rules(item) and not check_counted(check):
                continue        # examples-first: a check that is not itself a counted real test covers nothing else either
            for target in check.get("covers") or []:
                if target != item["n"]:
                    counts[target] = counts.get(target, 0) + 1
    return counts


def coverage_line(criterion: dict[str, Any], extra: int = 0) -> str:
    """Mechanical, no judgment: covered, a gap with a plain reason, or not mapped (an older submission that never said)."""
    if new_rules(criterion) and not (criterion_counted(criterion) or extra):
        # examples-first: only a counted real test covers a requirement (its own, or another requirement's counted check that
        # says it also covers this one); nothing else is rounded up to "covered"
        return GAP_NOT_COUNTED if (criterion.get("checks") and not criterion.get("stale")) else GAP_NO_TEST
    if not (criterion.get("mapped") or extra):
        return NOT_MAPPED_NEW if criterion.get("new") else NOT_MAPPED       # only a submission recorded now is held to the question
    checks = len(criterion.get("checks") or []) + extra
    if criterion.get("stale") or not checks:
        return GAP_NO_TEST
    if not (criterion.get("failure_paths") or criterion.get("no_failure_paths_because")):
        return GAP_NORMAL_ONLY
    return COVERED


def apply_coverage(criteria: list[dict[str, Any]]) -> list[int]:
    """Stamp each requirement with its coverage line from the checks as they are now; returns the numbers that have a gap."""
    counts = covered_by_others(criteria)
    for item in criteria:
        item["also_by"] = counts.get(item["n"], 0)
        item["coverage_line"] = coverage_line(item, item["also_by"])
    return [item["n"] for item in criteria if item["coverage_line"].startswith(("gap", "not mapped:"))]


def coverage_request(task: str, numbers: list[int], criteria: list[dict[str, Any]]) -> str:
    """Build the message asking the Reviewer of task to close coverage gaps, naming each requirement in numbers (matched against criteria) with its current coverage line. Return the instruction text to send."""
    named = "; ".join(f"{item['n']}. {item['text']} ({item['coverage_line']})" for item in criteria if item["n"] in numbers)
    return (
        f"CONTRACT CHECK COVERAGE: task {task}. The board matched your checks to the requirements and found gaps: {named}. "
        f"Once, for each, send contract-check-correct --task {task} --criterion <n> --reason \"closing a coverage gap\" "
        "--submission <JSON file with {\"checks\": [...], \"failure_paths\": [\"<failure or edge case a check covers>\"], "
        "\"no_failure_paths_because\": \"<or why none apply>\"}> (checks may be empty when the existing ones already cover it). "
        "You will not be asked again and the owner is not held. Do not start any polling or background helper. Never ask the owner anything. USER ACTION: None."
    )


def parse_submission(raw: str, *, examples_required: bool = True) -> dict[str, Any]:
    """Validate the JSON a Reviewer uploads; raise ValueError with a plain reason, return the normalised form.

    With `examples_required` (every submission the board records for a full Write-code task) each criterion needs its examples
    and the submission needs the owner's asks and the result list (task 2026-11-24_tdd-examples-first, Changes 1, 2 and 5)."""
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
        plain = item.get("plain")
        problem = plain_problem(plain)
        if problem:
            raise ValueError(f"criterion {number} needs a `plain` sentence the owner can read (what this protects for them, at most {PLAIN_LIMIT} characters): {problem}")
        examples = parse_examples(item, f"criterion {number}", examples_required)
        kind = requirement_kind(item, text)
        checks, check_bytes = parse_checks(checks_raw, f"criterion {number}", examples=len(examples) if examples else None, reading=kind in READING_KINDS)
        paths, none_because, said = parse_failure_paths(item, f"criterion {number}")
        total_bytes += check_bytes
        total_checks += len(checks)
        criteria.append({"n": number, "text": text, "plain": " ".join(plain.split()), "sharper": sharper, "why_not_checked": why, "checks": checks,
                         "failure_paths": paths, "no_failure_paths_because": none_because, "_said": said,
                         "kind": kind, **({"examples": examples} if examples else {})})
    if total_checks > MAX_CHECKS:
        raise ValueError(f"at most {MAX_CHECKS} checks in one submission")
    if total_bytes > MAX_TOTAL_BYTES:
        raise ValueError(f"the checks are larger than {MAX_TOTAL_BYTES // 1024} KB together")
    mapped = any(item.pop("_said") or any("covers" in check for check in item["checks"]) for item in criteria)
    for item in criteria:
        item["mapped"] = mapped
        item["new"] = True
        for check in item["checks"]:
            for target in check.get("covers") or []:
                if target > len(criteria):
                    raise ValueError(f"criterion {item['n']}: covers names requirement {target}, which is not in the list")
    apply_coverage(criteria)
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
    return {"criteria": criteria, "questions": questions, **parse_owner_lists(data, len(criteria), examples_required)}


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
    """Append a "Decided before coding" section to text, listing each resolved answer's question text and chosen answer, marking the recommended ones. Return the combined text with the section appended."""
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
    if new_rules(criterion):
        return CHECKED if criterion_counted(criterion) else PARTLY_CHECKED
    return CHECKED if all(check.get("coverage") == "checked" for check in checks) else PARTLY_CHECKED


def labels(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the owner-facing record for each requirement in criteria: its text, plain sentence, checked/partly/not-checked label, note, reason and coverage line, counting checks other requirements also cover it with. Return the list of per-requirement records."""
    counts = covered_by_others(criteria)
    return [{"n": item["n"], "criterion": item["text"], "plain": item.get("plain", ""), "label": criterion_label(item), "note": item.get("skipped_reason", ""), "reason": item.get("why_not_checked", ""), "kind": item.get("kind", ""),
             "coverage": coverage_line(item, counts.get(item["n"], 0)),
             **({"examples": list(item["examples"]), "counted": criterion_counted(item)} if new_rules(item) else {}),
             **({"walks": [check["walk"] for check in item["checks"] if check.get("walk")]} if any(check.get("walk") for check in item.get("checks") or []) else {})} for item in criteria]


def existing_code_note(repository: str) -> str:
    """When the project already has code, the Reviewer reads it to find what the requirements are about instead of guessing an interface."""
    if not str(repository or "").strip():
        return ""
    return (f" The project already has code, at {repository}. Read it (read only; change nothing there) to find the real commands, functions, files and pages "
            "each requirement is about, and write each check so that it calls them. A check for a defect must fail on that code today. Checks run in a copy "
            "of the project that has no git: compare against the stored baseline copy, never run git commands (the board refuses such a check). Never guess an interface "
            "you can find by reading. Leave a requirement unchecked only when, after reading the code, no test can show it, and say in why_not_checked what you read.")


def owner_sources_note(sources: list[str] | None) -> str:
    """Where the Reviewer reads what the owner gave: the copies in the task's own work folder, never raw board storage."""
    listed = [str(item) for item in sources or [] if str(item).strip()]
    if not listed:
        return (" Read the owner's direction and clarifications below for every distinct thing he asks for; he attached no file to this task.")
    return (" Read the owner's direction and clarifications below AND every file he attached, in full (PDFs, documents and screenshots included), "
            "at these copies in the task's work folder (read only): " + "; ".join(listed) + ". Name any you could not read under \"not_read\".")


def reviewer_instruction(task: str, version: int, requirements: str, direction: str, minutes: int, repository: str = "", brief: str = "", sources: list[str] | None = None) -> str:
    """Build the message routing a Reviewer to write contract checks for task at the given requirements version, carrying the owner's original direction, the requirements text, a repository note when repository is given, the minutes allowed before a late submission is refused, and the Project brief when one is given. Return the instruction text to send."""
    return (
        f"CONTRACT CHECK ROUTED: task {task}, requirements version {version}. This is advisory preparation, not a review round: "
        "do not reserve, claim or review anything for it, and the work for this task is not built yet. Read the owner's original direction and the "
        "requirements below and write your checks from them (black box: commands and their printed output, HTTP calls, or visible text "
        "on a page, each as a test file that asserts the result)." + existing_code_note(repository) + owner_sources_note(sources)
        + " Give every criterion its \"examples\" (what goes in or what the user does, and the exact result), list the owner's asks under "
        "\"owner_asks\" with the requirement numbers that cover each ([] when none does), and list under \"result_must_contain\" what the finished "
        "result of his main scenario must contain at least. A check is marked \"checked\" only when it runs the program and asserts one example's result "
        "(\"runs\" and \"example\"); where the app does not exist yet and you cannot write that, give the examples and no check: the requirement is then "
        "tested for real at the final review. Then submit ONCE with "
        f"contract-check-submit --task {task} --version {version} --submission <your JSON file>; the format is in your reference chapter "
        "`contract-first` (the board command reference --agent <your id> --name contract-first). Ask the owner at most 4 product questions, only where you cannot write "
        "a check without guessing, each with 2 to 4 options and one recommended. The owner may go ahead without waiting and a submission after "
        f"that, or after about {max(1, minutes)} minutes, is refused. Do not start any polling or background helper. USER ACTION: None. Open reference: contract-first\n"
        f"ORIGINAL OWNER DIRECTION:\n{direction}\nREQUIREMENTS (version {version}):\n{requirements}"
        + (f"\nPROJECT BRIEF:\n{brief}" if brief else "")
    )


def recheck_instruction(task: str, version: int, answers: list[dict[str, Any]], changed: list[int], criteria: list[dict[str, Any]], minutes: int, repository: str = "") -> str:
    """Build the message telling a Reviewer of task at the given requirements version which answers the owner gave, restating each of answers, naming the requirements in changed (matched against criteria), noting repository when given, and stating minutes before a late submission is refused. Return the instruction text to send."""
    said = "; ".join(f"{item['text']} -> {item['answer']}" for item in answers)
    names = "; ".join(f"{item['n']}. {item['text']}" for item in criteria if item["n"] in changed)
    return (
        f"CONTRACT RECHECK ROUTED: task {task}, requirements version {version}. The owner answered your questions: {said}. "
        f"Advisory preparation again, not a review round. The answers changed these requirements, so their old checks were set aside: {names}. "
        f"For each, write the new checks from the answers and submit them with contract-check-correct --task {task} --criterion <n> "
        "--reason \"updated for the owner's answers\" --submission <JSON file with {\"checks\": [...]}>. When nothing more needs changing, finish with "
        f"contract-check-correct --task {task} --criterion 0 --reason \"nothing more to change\" --submission <JSON file with {{\"checks\": []}}>. "
        + existing_code_note(repository) + f" The owner may go ahead without waiting and a submission after that, or after about {max(1, minutes)} minutes, is refused. "
        "Do not start any polling or background helper. Never ask the owner anything in your terminal. USER ACTION: None. Open reference: contract-first"
    )


# --- The builder says a locked check does not match reality (task 2026-10-26_check-deviation) --------------------------------------------
DEVIATION_WHY_MIN = 20
DEVIATION_WHY_MAX = 1200


def deviation_request(task: str, check: str, number: int, criterion: str, why: str) -> str:
    """The one message that wakes the Reviewer to answer a builder's deviation, once, through the correction path."""
    return (
        f"CHECK DEVIATION: task {task}, check {check} (requirement {number}: {criterion}). Delivery says this stored check does not match reality: {why} "
        f"Answer ONCE with contract-check-correct --task {task} --criterion {number}: either replace the check "
        "(--reason \"<what was wrong>\" --submission <JSON file with {\"checks\": [...]}>; it is locked again, seen failing first and its coverage recomputed as for any correction), "
        "or keep it (--reason \"the check is right because <why>\" --submission <JSON file with {\"keep\": true, \"checks\": []}>). "
        "A second answer is refused. Nothing is held and Delivery keeps building; if you never answer, the final reviewer sees the deviation and the check stays as it is. "
        "Do not start any polling or background helper. Never ask the owner anything. USER ACTION: None. Open reference: contract-first"
    )


# --- The Small change lane (task 2026-10-26_fast-lane) ---------------------------------------------------------------------------------
SMALL_CHANGE_REASON = "Small change: tests are not written in advance"
ANALYSIS_REASON = "Code analysis: nothing is tested in advance; after Go ahead the builder and the reviewer each read all the code"


def full_lane_request(task: str, version: int, why: str, requirements: str, minutes: int, repository: str = "") -> str:
    """The one message that restarts the contract-check step for a Reviewer who sent a Small change task back to the full lane."""
    return (
        f"FULL LANE: you sent task {task} back from the Small change lane ({why}). Advisory preparation again, not a review round: write your checks from the "
        "confirmed requirements below (black box)." + existing_code_note(repository) + " Then submit ONCE with "
        f"contract-check-submit --task {task} --version {version} --submission <your JSON file>; the format is in your reference chapter `contract-first`. "
        "Submit no questions (the owner is not asked; a submission with questions is refused). The board runs the checks on Delivery's work as in the full lane; "
        "nothing is held and the task is not sent back a second time. "
        f"A submission after about {max(1, minutes)} minutes is refused. Do not start any polling or background helper. USER ACTION: None. Open reference: contract-first\n"
        f"REQUIREMENTS:\n{requirements}"
    )

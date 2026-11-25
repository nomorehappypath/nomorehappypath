# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Reviewer's early look at the requirements: submission format, answers and labels.

Helpers without board state: board.py holds the state changes, this module
holds what a submission must look like, how the owner's answers become a
"Decided before coding" section, and the plain-word label of each requirement.
Spec: board/tasks/2026-10-23_contract-first-checks_SPEC.md (v2).
"""
from __future__ import annotations

import json
import ast
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from harness import contract

# The one wait for the Reviewer's early look (seconds). The owner can go ahead without waiting at any time.
WAIT_DEFAULT_SECONDS = 1800      # task 2026-11-24_tdd-examples-first: examples, the asks list and the result list take longer to write; still only a default
WAIT_OLD_DEFAULT_SECONDS = 900
WAIT_MIN_SECONDS = 60
WAIT_MAX_SECONDS = 3600
_configured: dict[str, int] = {}

MAX_QUESTIONS = 4
MIN_OPTIONS = 2
MAX_OPTIONS = 4
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
DECIDED_HEADING = "Decided before coding:"

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


def _words(value: Any) -> str:
    """value as one line of text ("" when it is not text)."""
    return " ".join(value.split()) if isinstance(value, str) else ""


def parse_examples(item: dict[str, Any], label: str, required: bool, notes: list[str] | None = None) -> list[str]:
    """The requirement's examples, each saying what goes in or what the user does and the exact result. Owner 2026-11-24: the form of an
    example never stops the package; what can be read is kept, the rest is named in notes."""
    notes = notes if notes is not None else []
    raw = item.get("examples")
    if isinstance(raw, str):
        raw = [raw]
    examples = [words for words in map(_words, raw if isinstance(raw, list) else []) if words]
    if required and not examples:
        notes.append(f"{label} has no example, so its tests are not counted and it is tested for real at the final review")
    return examples


def parse_added_requirement(data: dict[str, Any], number: int) -> dict[str, Any]:
    """A requirement of the accepted text that the early list left out, added by the final Reviewer: its text, its plain line
    for the owner, its examples and its kind."""
    text = _words(data.get("text"))
    examples = parse_examples(data, f"requirement {number}", True)
    plain = _words(data.get("plain")) or text
    return {"text": text, "plain": plain, "examples": examples or [plain], "kind": requirement_kind(data, text)}


def _numbers(value: Any, highest: int) -> list[int]:
    """The requirement numbers in value that exist (1 to highest); anything else in it is ignored."""
    values = value if isinstance(value, list) else [value]
    return sorted({n for n in values if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= highest})


def _lines(value: Any) -> list[str]:
    """The readable lines of a list (or of one text)."""
    return [words for words in map(_words, value if isinstance(value, list) else [value]) if words]


def parse_owner_lists(data: dict[str, Any], criteria_count: int, required: bool, notes: list[str] | None = None) -> dict[str, Any]:
    """The three lists the Reviewer writes from the owner's own direction and files: what he asked for and which requirements
    cover each ask, any source it could not read, and what the finished result of his scenario must contain at least."""
    notes = notes if notes is not None else []
    asks = []
    raw = data.get("owner_asks")
    for item in raw if isinstance(raw, list) else [raw] if raw else []:
        words = _words(item.get("ask") or item.get("text")) if isinstance(item, dict) else _words(item)
        if words:
            # an ask whose numbers cannot be read is shown as covered by none: the owner sees it, nothing is rounded up
            asks.append({"ask": words, "requirements": _numbers(item.get("requirements"), criteria_count) if isinstance(item, dict) else []})
    result = _lines(data.get("result_must_contain"))
    if required and not asks:
        notes.append("the reviewer listed none of your asks, so nothing says whether the requirements cover them all")
    if required and not result:
        notes.append("the reviewer did not say what the finished result must contain")
    return {"owner_asks": asks, "not_read": _lines(data.get("not_read")), "result_must_contain": result}


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
SUMMARY_HEADING = "In plain words:"
SUMMARY_LINE_LIMIT = 200
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


def _block(lines: list[str], index: int, heading: str) -> tuple[list[str], int]:
    """(the block's lines, the index after it) when lines[index] starts the block named heading ("heading: words" on one line is the same)."""
    first = lines[index].strip()[len(heading):].strip()
    block = [first] if first else []
    index += 1
    while index < len(lines) and lines[index].strip():
        block.append(lines[index].strip())
        index += 1
    return block, index


def _starts(line: str, heading: str) -> bool:
    return line.strip().lower().startswith(heading.lower())


def has_plain_summary(text: Any) -> bool:
    """The text starts with the builder's own block for the owner."""
    return _starts(str(text or "").strip().split("\n")[0], SUMMARY_HEADING)


def split_plain_summary(text: Any) -> tuple[list[str], str]:
    """(plain lines, the whole text). Owner 2026-11-24: how the builder wrote the block for the owner never stops the contract; when there is
    none, the requirements' own first words become it. The whole returned text is what the owner accepts."""
    raw = str(text or "").replace("\r\n", "\n").strip()
    lines = raw.split("\n")
    if _starts(lines[0], SUMMARY_HEADING):
        block, after = _block(lines, 0, SUMMARY_HEADING)
        rest = "\n".join(lines[after:]).strip()
        if block and rest:
            return block, "\n".join([SUMMARY_HEADING, *block]) + "\n\n" + rest
        raw = rest or "\n".join(block)
    words = next((" ".join(line.lstrip("#-*0123456789. ").split()) for line in raw.split("\n") if len(line.split()) > 3), " ".join(raw.split()))
    block = [words if len(words) <= SUMMARY_LINE_LIMIT else words[:SUMMARY_LINE_LIMIT - 1].rstrip() + "…"]
    return block, "\n".join([SUMMARY_HEADING, *block]) + "\n\n" + raw


FIT_HEADING = "Already in the project:"
FIT_NOT_SAID = "The builder did not say what the project already has."


def split_fit_block(text: Any) -> tuple[list[str], str]:
    """(fit lines, the whole text) for a text that starts with the plain block (split_plain_summary's second value). A missing block says so."""
    lines = str(text or "").split("\n")
    index = 1
    while index < len(lines) and lines[index].strip():
        index += 1                      # past the plain block
    while index < len(lines) and not lines[index].strip():
        index += 1                      # past the blank line
    head = "\n".join(lines[:index]).rstrip()
    if index < len(lines) and _starts(lines[index], FIT_HEADING):
        block, after = _block(lines, index, FIT_HEADING)
        rest = "\n".join(lines[after:]).strip()
        if block and rest:
            return block, head + "\n\n" + "\n".join([FIT_HEADING, *block]) + "\n\n" + rest
        rest = rest or "\n".join(block)
    else:
        rest = "\n".join(lines[index:]).strip()
    return [FIT_NOT_SAID], head + "\n\n" + FIT_HEADING + "\n" + FIT_NOT_SAID + "\n\n" + rest


def configure(seconds: Any) -> None:
    """Install the owner's wait (the project worker passes the settings value once at start); None restores the default."""
    _configured.clear()
    if seconds == WAIT_OLD_DEFAULT_SECONDS:
        return      # the value older versions saved for everybody; nobody chose it (2026-11-24: it took half of the new wait away)
    if isinstance(seconds, int) and not isinstance(seconds, bool) and WAIT_MIN_SECONDS <= seconds <= WAIT_MAX_SECONDS:
        _configured["seconds"] = seconds


def wait_seconds() -> int:
    """Give the wait the owner configured, or WAIT_DEFAULT_SECONDS when configure() set none. Returns the wait in seconds."""
    return _configured.get("seconds", WAIT_DEFAULT_SECONDS)


def _relative_path(value: Any) -> str:
    """Check that value is a plain forward-slash relative path, with no backslash, null byte, absolute prefix or `.`/`..` segment that could escape the check's own folder. Return the normalised path as a string."""
    if not isinstance(value, str) or not value.strip() or "\\" in value or "\0" in value:
        raise ValueError("a check file needs a plain relative path with forward slashes")
    path = PurePosixPath(value.strip())
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or not path.parts:
        raise ValueError(f"check file path {value!r} must stay inside the check's own folder")
    return str(path)


def _own_ledger(files: dict[str, str]) -> str:
    """The ledger NMHP writes itself for a check whose own ledger cannot be run: one row that runs the check's own tests."""
    if not any(name.endswith(".py") and "unittest" in body for name, body in files.items()):
        return ""
    names = sorted(name for name, body in files.items() if name.endswith(".py") and "unittest" in body)
    pattern = "test_*.py" if any(PurePosixPath(name).name.startswith("test_") for name in names) else "*.py"
    return ("| ID | Simulation command | Expected system response | Observed system response | QA result |\n| --- | --- | --- | --- | --- |\n"
            f"| S-001 | python3 -m unittest discover -s {{folder}} -p \"{pattern}\" | the check's own tests pass | not run yet | PASS |\n")


def check_folder(number: int, position: int) -> str:
    """Where the board places a check's files in the copy it runs."""
    return f".contract-checks/c{number}-{position}"


_ESCAPED_QUOTE = re.compile(r'\\(?=["\'])')


def usable_ledger(ledger: str, files: dict[str, str], number: int, position: int, notes: list[str], where: str) -> str:
    """ledger in a form the board can run ("" when it cannot be run at all): rows are numbered, escaped quotes unescaped, and, when no row
    can be run, the check's own tests are run by a row NMHP writes."""
    if _ESCAPED_QUOTE.search(ledger):
        ledger = _ESCAPED_QUOTE.sub("", ledger)
        notes.append(f"{where}: escaped quotes in its command were corrected")
    with tempfile.TemporaryDirectory(prefix="harness-contract-check-") as temporary:
        probe = Path(temporary) / "ledger.md"
        probe.write_text(ledger, encoding="utf-8")
        _rows, problems, said = contract.scenario_plan(probe)
    if not problems:
        notes.extend(f"{where}: {line}" for line in said)
        return ledger
    own = _own_ledger(files)
    if own:
        notes.append(f"{where}: its test list could not be run ({'; '.join(problems)[:160]}), so NMHP runs its test files itself")
        return own.format(folder=check_folder(number, position))
    notes.append(f"{where} cannot be run, so it is not used: {'; '.join(problems)[:240]}")
    return ""


def parse_checks(checks_raw: Any, label: str, *, examples: int | None = None, reading: bool = False,
                 notes: list[str] | None = None, number: int = 0) -> tuple[list[dict[str, Any]], int]:
    """The checks that can be used (a ledger plus the test files that assert the result) and their size in bytes.

    `examples` is the number of examples of the requirement the checks belong to (None for an older record, which keeps the
    older rules); `reading` says the requirement is a document or source-wide one. A check counts as a real test only when it says what it
    runs and which example it asserts (task 2026-11-24_tdd-examples-first, Change 3). Owner 2026-11-24: a check in a bad form never takes
    the package with it: it is corrected, or kept as "partly" (run, never counted), or left out and named in notes."""
    notes = notes if notes is not None else []
    total_bytes = 0
    checks = []
    for position, check in enumerate(checks_raw if isinstance(checks_raw, list) else [checks_raw] if checks_raw else [], 1):
        where = f"{label} check {position}"
        if not isinstance(check, dict):
            notes.append(f"{where} could not be read, so it is not used")
            continue
        example = check.get("example")
        if isinstance(example, bool) or not isinstance(example, int) or example < 1 or (examples is not None and example > examples):
            example = None
        if "walk" in check:
            # a walk (task 2026-11-24_tdd-walks): steps on the app's screens, written as data, stored as this requirement's check
            from harness import walks
            try:
                entry = walks.parse(check["walk"], where)
            except ValueError as error:
                notes.append(f"{error}; this walk is not used")
                continue
            if example is None and examples == 1:
                example = 1
            if example is None:
                entry["coverage"] = "partly"
                notes.append(f"{where}: the walk names no example, so it runs but is not counted")
            else:
                entry["example"] = example
            total_bytes += sum(len(body.encode("utf-8")) for body in entry["files"].values())
            checks.append({**entry, "place": position})
            continue
        files: dict[str, str] = {}
        unsafe = ""
        for name, body in (check.get("files") or {}).items() if isinstance(check.get("files"), dict) else []:
            try:
                relative = _relative_path(name)
            except ValueError as error:
                unsafe = str(error)
                break
            if isinstance(body, str):
                files[relative] = body
        # Checks run in a copy of the project that has no git (the disposable checkout), so a check that runs git can never pass,
        # whatever the work is (2026-11-04).
        git = next((relative for relative, body in files.items() if runs_git(relative, body)), "")
        if unsafe or not files or git:
            notes.append(f"{where} is not used: " + (unsafe or (f"its file {git} runs git, and checks run in a copy with no git" if git else "it has no test file")))
            continue
        ledger = usable_ledger(check.get("ledger") if isinstance(check.get("ledger"), str) else "", files, number, position, notes, where)
        if not ledger:
            continue
        coverage = check.get("coverage", CHECKED)
        if coverage not in {"checked", "partly"}:
            coverage = "partly"
            notes.append(f"{where} does not say whether it tests the whole requirement, so it runs but is not counted")
        # the Reviewer names the check's folder by its place in its own list; the board moves that name when a check before it is left out
        entry = {"coverage": coverage, "ledger": ledger, "files": files, "place": position}
        if _words(check.get("runs")):
            entry["runs"] = _words(check["runs"])
        if example is not None:
            entry["example"] = example
        if examples is not None and not reading and coverage == "checked":
            if only_reads_files(files) or not check_counted(entry):
                entry["coverage"] = "partly"
                notes.append(f"{where} " + ("only reads files" if only_reads_files(files) else "does not say what it runs and which example it asserts")
                             + ", so it runs but is not counted")
        covers = _numbers(check.get("covers"), 10 ** 6)
        if covers:
            entry["covers"] = covers
        total_bytes += len(ledger.encode("utf-8")) + sum(len(body.encode("utf-8")) for body in files.values())
        checks.append(entry)
    return checks, total_bytes


def parse_failure_paths(item: dict[str, Any], label: str) -> tuple[list[str], str, bool]:
    """(failure paths and edge cases the checks cover, the reason none apply, whether the Reviewer said anything about it)."""
    said = "failure_paths" in item or "no_failure_paths_because" in item
    return _lines(item.get("failure_paths")), _words(item.get("no_failure_paths_because")), said


def placed(check: dict[str, Any], number: int, position: int) -> tuple[str, dict[str, str]]:
    """The check's ledger and files with its folder named by the place it is stored at."""
    place = check.get("place", position)
    if place == position or is_walk_files(check):
        return check["ledger"], check["files"]
    old = re.compile(re.escape(check_folder(number, place)) + r"(?!\d)")
    new = check_folder(number, position)
    return old.sub(new, check["ledger"]), {name: old.sub(new, body) for name, body in check["files"].items()}


def is_walk_files(check: dict[str, Any]) -> bool:
    """A walk has no folder of its own."""
    return bool(check.get("walk"))


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
    """The Reviewer's package in the form the board stores, with `notes` naming everything that was corrected or left out.

    Only a file nothing can be read from is refused (owner 2026-11-24: form never stops the work). With `examples_required` (every
    submission the board records for a full Write-code task) a criterion without examples, asks or a result list is noted, and a check that
    names no example is never counted (task 2026-11-24_tdd-examples-first, Changes 1, 2 and 5)."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("the submission must be a JSON file; nothing was recorded, so send it again once it is one") from error
    criteria_raw = data.get("criteria") if isinstance(data, dict) else None
    if not isinstance(criteria_raw, list) or not criteria_raw:
        raise ValueError("list every acceptance criterion under criteria, each with its checks or the reason it has none; nothing was recorded, so send it again")
    notes: list[str] = []
    criteria = []
    for number, item in enumerate(criteria_raw, 1):
        label = f"criterion {number}"
        if not isinstance(item, dict):
            item = {"text": item if isinstance(item, str) else ""}
        text = _words(item.get("text")) or _words(item.get("plain"))
        if not text:
            text = f"Requirement {number}"
            notes.append(f"{label} has no text")
        plain = _words(item.get("plain")) or text
        examples = parse_examples(item, label, examples_required, notes)
        kind = requirement_kind(item, text)
        left_out: list[str] = []
        checks, _bytes = parse_checks(item.get("checks"), label, examples=len(examples) if (examples or examples_required) else None,
                                      reading=kind in READING_KINDS, notes=left_out, number=number)
        notes.extend(left_out)
        why = _words(item.get("why_not_checked"))
        if not checks and not why:
            why = "; ".join(line for line in left_out if "not used" in line) or "the reviewer gave no test and no reason"
        paths, none_because, said = parse_failure_paths(item, label)
        criteria.append({"n": number, "text": text, "plain": plain, "sharper": _words(item.get("sharper")), "why_not_checked": why if not checks else "",
                         "checks": checks, "failure_paths": paths, "no_failure_paths_because": none_because, "_said": said,
                         # with no example of its own the requirement itself is what the final review tests for real (never counted before)
                         "kind": kind, **({"examples": examples or [text]} if (examples or examples_required) else {})})
    mapped = any(item.pop("_said") or any("covers" in check for check in item["checks"]) for item in criteria)
    for item in criteria:
        item["mapped"] = mapped
        item["new"] = True
        for check in item["checks"]:
            if check.get("covers"):
                check["covers"] = _numbers(check["covers"], len(criteria))
    apply_coverage(criteria)
    questions: list[dict[str, Any]] = []
    questions_raw = data.get("questions")
    for item in questions_raw if isinstance(questions_raw, list) else []:
        options = _lines(item.get("options"))[:MAX_OPTIONS] if isinstance(item, dict) and isinstance(item.get("options"), list) else []
        words = _words(item.get("text") or item.get("question")) if isinstance(item, dict) else ""
        if not words or len(options) < MIN_OPTIONS or len(questions) == MAX_QUESTIONS:
            notes.append("a question was not asked: " + ("only the first four are" if len(questions) == MAX_QUESTIONS else "it has no text or fewer than two options"))
            continue
        recommended = item.get("recommended")
        if isinstance(recommended, bool) or not isinstance(recommended, int) or not 0 <= recommended < len(options):
            recommended = 0
        questions.append({"n": len(questions) + 1, "text": words, "options": options, "recommended": recommended,
                          "affects": _numbers(item.get("affects"), len(criteria))})
    return {"criteria": criteria, "questions": questions, **parse_owner_lists(data, len(criteria), examples_required, notes), "notes": notes}


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
        "tested for real at the final review. Then submit with "
        f"contract-check-submit --task {task} --version {version} --submission <your JSON file>; the format is in your reference chapter "
        "`contract-first` (the board command reference --agent <your id> --name contract-first). Ask the owner at most 4 product questions, only where you cannot write "
        "a check without guessing, each with 2 to 4 options and one recommended. The answer lists anything that could not be used: correct those with "
        "contract-check-correct; a submission the board could not read was not recorded, so send it again. The owner may go ahead without waiting; "
        f"aim to finish within about {max(1, minutes)} minutes. Do not start any polling or background helper. USER ACTION: None. Open reference: contract-first\n"
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
        + existing_code_note(repository) + f" The owner may go ahead without waiting; aim to finish within about {max(1, minutes)} minutes. "
        "Do not start any polling or background helper. Never ask the owner anything in your terminal. USER ACTION: None. Open reference: contract-first"
    )


# --- The builder says a locked check does not match reality (task 2026-10-26_check-deviation) --------------------------------------------


def deviation_request(task: str, check: str, number: int, criterion: str, why: str) -> str:
    """The one message that wakes the Reviewer to answer a builder's deviation, once, through the correction path."""
    return (
        f"CHECK DEVIATION: task {task}, check {check} (requirement {number}: {criterion}). Delivery says this stored check does not match reality: {why} "
        f"Answer with contract-check-correct --task {task} --criterion {number}: either replace the check "
        "(--reason \"<what was wrong>\" --submission <JSON file with {\"checks\": [...]}>; it is locked again, seen failing first and its coverage recomputed as for any correction), "
        "or keep it (--reason \"the check is right because <why>\" --submission <JSON file with {\"keep\": true, \"checks\": []}>). "
        "Nothing is held and Delivery keeps building; if you never answer, the final reviewer sees the deviation and the check stays as it is. "
        "Do not start any polling or background helper. Never ask the owner anything. USER ACTION: None. Open reference: contract-first"
    )


# --- The Small change lane (task 2026-10-26_fast-lane) ---------------------------------------------------------------------------------
SMALL_CHANGE_REASON = "Small change: tests are not written in advance"
ANALYSIS_REASON = "Code analysis: nothing is tested in advance; after Go ahead the builder and the reviewer each read all the code"


def full_lane_request(task: str, version: int, why: str, requirements: str, minutes: int, repository: str = "") -> str:
    """The one message that restarts the contract-check step for a Reviewer who sent a Small change task back to the full lane."""
    return (
        f"FULL LANE: you sent task {task} back from the Small change lane ({why}). Advisory preparation again, not a review round: write your checks from the "
        "confirmed requirements below (black box)." + existing_code_note(repository) + " Then submit with "
        f"contract-check-submit --task {task} --version {version} --submission <your JSON file>; the format is in your reference chapter `contract-first`. "
        "Submit no questions (the owner is not asked; questions are left out). The board runs the checks on Delivery's work as in the full lane; "
        "nothing is held and the task is not sent back a second time. "
        f"Aim to finish within about {max(1, minutes)} minutes. Do not start any polling or background helper. USER ACTION: None. Open reference: contract-first\n"
        f"REQUIREMENTS:\n{requirements}"
    )

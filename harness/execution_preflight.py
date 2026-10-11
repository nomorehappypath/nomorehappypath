# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Cheap fail-fast checks before governed test execution begins."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
from typing import Iterable


_INSTALL_ACTIONS = {"ci", "install", "i"}


def _tokens(command: str) -> list[str]:
    try:
        values = shlex.split(command, posix=True)
    except ValueError as error:
        raise ValueError(f"test command cannot be parsed: {error}") from error
    while values and "=" in values[0] and values[0].split("=", 1)[0].isidentifier():
        values.pop(0)
    return values


BOARD_CALL_MARKER = "this command runs the harness board CLI"
BOARD_CALL_REFUSAL = (
    f"{BOARD_CALL_MARKER} (board.py or screen-check), which a test or scenario command may not do: "
    "it runs in the review sandbox, which cannot reach the live board. Keep every simulation command hermetic. "
    "Run screen-check yourself as a board command before you submit, and cite its screen-... evidence id and the text it "
    "returned in the ledger row. A test of the board CLI itself must run it against a disposable fixture board from "
    "inside the test's own code"
)

HARNESS_BOARD = Path(__file__).resolve().parent / "board.py"
_NO_VALUE_FLAGS = {"-E", "-I", "-B", "-u", "-s", "-S", "-O", "-OO", "-q", "-d", "-v", "-x", "-b", "-bb", "-R", "-P"}
_VALUE_FLAGS = {"-W", "-X", "-r", "--require", "--import", "-e", "--eval", "-p", "--print", "--loader", "--experimental-loader"}


def _resolves_to_board(token: str, cwd: Path, search_path: str) -> bool:
    if "/" in token:
        candidate = (cwd / token).resolve()
    else:
        found = shutil.which(token, path=search_path)
        candidate = Path(found).resolve() if found else None
    return candidate is not None and candidate == HARNESS_BOARD


def calls_the_board(values: list[str], *, cwd: Path, search_path: str) -> bool:
    """True when the command EXECUTES the harness board CLI: as the program, as the script an
    interpreter runs, or as `python -m harness.board`. A mention inside a -c string, another
    project's board.py, `pytest board.py` and `npm test -- screen-check` are not execution."""
    values = list(values)
    while values and values[0].rsplit("/", 1)[-1] == "env":
        values.pop(0)
        while values:
            if values[0] in {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"} and len(values) > 1:
                del values[:2]
            elif values[0].startswith("-") or ("=" in values[0] and values[0].split("=", 1)[0].isidentifier()):
                values.pop(0)
            else:
                break
    if not values:
        return False
    program = values[0]
    if _resolves_to_board(program, cwd, search_path):
        return True
    name = program.rsplit("/", 1)[-1]
    if not (name.startswith("python") or name in {"node", "nodejs", "sh", "bash", "zsh", "dash"}):
        return False
    index = 1
    while index < len(values):
        argument = values[index]
        if argument == "-m" and name.startswith("python"):
            return index + 1 < len(values) and values[index + 1] in {"harness.board", "harness.board_client"}
        if argument in {"-c"}:
            return False
        if argument in _VALUE_FLAGS:
            index += 2
            continue
        if argument.startswith("-") and (argument in _NO_VALUE_FLAGS or len(argument) > 1):
            index += 1
            continue
        return _resolves_to_board(argument, cwd, search_path)
    return False


def _npm_root(root: Path, values: list[str]) -> Path:
    prefix = "."
    for index, value in enumerate(values):
        if value == "--prefix" and index + 1 < len(values):
            prefix = values[index + 1]
        elif value.startswith("--prefix="):
            prefix = value.split("=", 1)[1]
    candidate = (root / prefix).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("npm --prefix must stay inside the governed execution root")
    return candidate


def _npm_action(values: list[str]) -> str:
    skip_next = False
    for value in values[1:]:
        if skip_next:
            skip_next = False
            continue
        if value == "--prefix":
            skip_next = True
            continue
        if value.startswith("--prefix=") or value.startswith("-"):
            continue
        return value
    return ""


def _node_dependencies_required(package_root: Path) -> bool:
    manifest = package_root / "package.json"
    if not manifest.is_file():
        return False
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read Node dependency manifest {manifest}: {error}") from error
    return any(value.get(field) for field in ("dependencies", "devDependencies", "optionalDependencies"))


def validate_commands(
    root: Path, commands: Iterable[str], *, environment: dict[str, str] | None = None,
) -> dict[str, object]:
    """Validate all commands before the first expensive command executes.

    The check never installs dependencies or mutates the candidate. It catches
    executable absence and the lockfile/install state that forced Task D's
    complete final gate to run twice.
    """
    execution_root = Path(root).resolve()
    problems: list[str] = []
    executable_path = (environment or {}).get("PATH", os.environ.get("PATH", ""))
    checked = 0
    planned_node_installs: set[Path] = set()
    for raw in commands:
        command = str(raw or "").strip()
        if not command:
            continue
        checked += 1
        try:
            values = _tokens(command)
        except ValueError as error:
            problems.append(str(error))
            continue
        if not values:
            problems.append("test command has no executable")
            continue
        if calls_the_board(values, cwd=execution_root, search_path=executable_path):
            problems.append(BOARD_CALL_REFUSAL)
            continue
        executable = values[0]
        available = (
            (execution_root / executable).resolve().is_file()
            if "/" in executable
            else shutil.which(executable, path=executable_path) is not None
        )
        if not available:
            problems.append(f"required test executable is unavailable: {executable}")
            continue
        if executable not in {"npm", "npm.cmd"}:
            continue
        try:
            package_root = _npm_root(execution_root, values)
            action = _npm_action(values)
        except ValueError as error:
            problems.append(str(error))
            continue
        if action in _INSTALL_ACTIONS:
            planned_node_installs.add(package_root)
            continue
        if (
            _node_dependencies_required(package_root)
            and not (package_root / "node_modules").is_dir()
            and package_root not in planned_node_installs
        ):
            problems.append(
                f"Node dependencies are not installed for {package_root}; "
                "run the lockfile install before the governed test gate"
            )
    if problems:
        raise ValueError(
            "test dependency preflight failed before execution: "
            + "; ".join(dict.fromkeys(problems))
        )
    return {"checked_commands": checked, "status": "passed"}

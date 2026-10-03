# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The prose rules a PreToolUse hook enforces mechanically (spec PLUMBING_MODERNIZATION.md §6, Stage 1).

Each rule restates a directive sentence the model used to have to remember:

- R-2  "never use `git add .`, `git add -A`, or another whole-tree staging
       command in a shared checkout" (directives/AGENT.md, CTO.md)
- R-3  "no `while`/`for`/`watch`/`nohup`/`caffeinate`/sleep loops to poll or
       heartbeat the board"; "do not leave CLI probes running in the background"
- R-4  "the owner's logins are never yours to read or copy"
- R-5  a reviewer does not edit the implementation under review

R-1 (writes to harness storage) is enforced by the operating system since F-1
and needs no hook.

HONEST LIMIT. File-tool paths are matched exactly. A shell command is matched
by pattern, which stops the ordinary, unintended form of each mistake - what
the prose rule addresses today - and not a determined adversary who writes the
same thing through a script. The OS confinement remains the hard boundary.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
FILE_READ_TOOLS = {"Read", "NotebookRead", "Grep", "Glob"}
SHELL_TOOLS = {"Bash"}

WHOLE_TREE_STAGING = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?add\s+(?:[^;&|]*\s)?(?:-A|--all|\.|:/)(?=\s|$|;|&|\|)")
BACKGROUND_HELPERS = re.compile(r"(?:^|[\s;&|(])(?:nohup|disown|caffeinate|setsid)(?=\s|$)")
# A single '&' that sends a job to the background - not '&&', not '>&', not
# '&>', not '|&' - searched after quoted text is removed (a URL's '&' is data).
BACKGROUND_JOB = re.compile(r"(?<![&>|])&(?![&>])")
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
BOARD_POLL_LOOP = re.compile(r"\b(?:while|until|for)\b[\s\S]*\bboard\.py\b[\s\S]*\bpoll\b|\bwatch\b[\s\S]*\bboard\.py\b")
CREDENTIAL_FILES = (".codex/auth.json", ".claude/.credentials.json", "/.credentials.json")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _inside(path: str, roots: list[str]) -> bool:
    if not path:
        return False
    real = os.path.realpath(os.path.expanduser(path))
    for root in roots:
        base = os.path.realpath(root)
        if real == base or real.startswith(base.rstrip("/") + "/"):
            return True
    return False


def evaluate(role: str, tool_name: str, tool_input: dict[str, Any], *, project_roots: list[str]) -> tuple[str, str]:
    """('', '') to allow, or (rule, plain reason) to deny one PreToolUse call."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    command = _text(tool_input.get("command"))
    if tool_name in SHELL_TOOLS:
        if WHOLE_TREE_STAGING.search(command):
            return "R-2", ("Whole-tree staging (git add . / -A / --all) is not allowed in a shared checkout. "
                           "Stage the exact paths you changed, or commit through the board's git-commit.")
        if tool_input.get("run_in_background") is True or BACKGROUND_HELPERS.search(command) or BACKGROUND_JOB.search(QUOTED.sub("", command)):
            return "R-3", ("Background helpers (nohup, disown, caffeinate, '&', background shells) are not allowed: "
                           "they outlive the task. Run the command in the foreground.")
        if BOARD_POLL_LOOP.search(command):
            return "R-3", ("Polling the board in a loop is not allowed. Poll once, do the work, and return to the "
                           "prompt; the harness wakes you when there is more.")
        if any(marker in command for marker in CREDENTIAL_FILES):
            return "R-4", "The owner's login files are never yours to read or copy."
    if tool_name in FILE_READ_TOOLS or tool_name in FILE_WRITE_TOOLS:
        target = _text(tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path"))
        if any(target.endswith(marker) or marker.strip("/") in target for marker in CREDENTIAL_FILES):
            return "R-4", "The owner's login files are never yours to read or copy."
    if role == "qa" and tool_name in FILE_WRITE_TOOLS:
        target = _text(tool_input.get("file_path") or tool_input.get("notebook_path"))
        if _inside(target, project_roots):
            return "R-5", ("A reviewer does not edit the work under review. Write your Challenge Ledger and notes "
                           "in a temporary directory you create; report defects as findings.")
    return "", ""


def project_roots_for(code_root: str | Path, workspace_root: str | Path) -> list[str]:
    return [str(code_root), str(workspace_root)]

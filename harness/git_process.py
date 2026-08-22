# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Run harness-owned Git commands without inherited Git routing state.

Git gives variables such as ``GIT_DIR`` and ``GIT_WORK_TREE`` precedence over
the caller's working directory.  Harness repository identity and evidence must
therefore cross one explicit boundary that removes every ``GIT_*`` variable,
resolves the Git executable once, and retains the selected ``cwd``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from harness import child_process


SAFE_CONFIG_ARGUMENTS = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "commit.gpgSign=false",
    "-c", "tag.gpgSign=false",
    "-c", "core.fsmonitor=false",
    "-c", "credential.helper=",
    "-c", "core.pager=cat",
    "-c", "pager.branch=false",
    "-c", "interactive.diffFilter=",
    "-c", "diff.external=",
    "-c", "merge.tool=",
    "-c", "mergetool.prompt=false",
    "-c", "core.sshCommand=/usr/bin/ssh",
    "-c", "http.proxy=",
    "-c", "protocol.ext.allow=never",
]


def sanitized_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy an environment with Git routing and config sources pinned safe."""
    return child_process.environment(environment, git=True, python=True, shell=True)


def run(command: Sequence[str], *, cwd: str | os.PathLike[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Run one Git command against an explicit working directory.

    The command deliberately retains the familiar ``["git", ...]`` shape at
    call sites, while this boundary replaces the executable with its resolved
    absolute path and strips all inherited Git routing/configuration variables.
    """
    if not command or Path(command[0]).name != "git":
        raise ValueError("git_process.run accepts only a git command")
    base_environment = kwargs.pop("env", None)
    environment = sanitized_environment(base_environment)
    executable = shutil.which("git", path=environment.get("PATH"))
    if not executable:
        raise FileNotFoundError("git executable was not found")
    resolved = str(Path(executable).resolve())
    return subprocess.run(
        [resolved, *SAFE_CONFIG_ARGUMENTS, *command[1:]],
        cwd=Path(cwd), env=environment, **kwargs,
    )

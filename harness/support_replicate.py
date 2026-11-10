# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Replication: try to make a reported case happen again, on the exact
checked-out app version, with fake data only (Task 2 of the Dedicated
Support NMHP). Follows the app's own `SUPPORT_PLAN.md`; never a production
credential or a real external service.

The plan names exactly one command Support runs: the one under
`## Reproduce`, which exits non-zero while the reported problem happens.
That same command is the repair's failing-before / passing-after check
(support_fix). The plan never names a repair or a review command: review
round 3 (finding B1) showed that a scripted plan command standing in for the
repair or for the independent reviewer let a case be called Fixed with no
real fix and no real review, so no such heading is read any more.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPRODUCE_HEADING = "## Reproduce"
REPLICATE_TIMEOUT_SECONDS = 120

# Review r1 B3: a replica must never inherit this Support installation's own environment (its
# mail password, GitHub token, or anything else set in the parent process's env) — only a minimal,
# known-safe base plus whatever fake data the plan explicitly names.
_SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")


@dataclass
class ReplicationResult:
    replicated: bool
    attempts: list[str]


def _command_under_heading(support_plan_text: str, heading: str) -> str:
    """Pull the one fenced shell command SUPPORT_PLAN.md names under one heading.

    Args:
        support_plan_text: the checked-out app's SUPPORT_PLAN.md, already read.
        heading: the exact markdown heading the command's fenced block follows.

    Return: the fenced command, stripped; "" if the heading or its fenced block is missing.
    """
    if heading not in support_plan_text:
        return ""
    after = support_plan_text.split(heading, 1)[1]
    if "```" not in after:
        return ""
    fenced = after.split("```", 2)
    if len(fenced) < 3:
        return ""
    lines = [line for line in fenced[1].strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def reproduce_command(support_plan_text: str) -> str:
    """Pull the one shell command SUPPORT_PLAN.md names under its "## Reproduce" heading.

    Args:
        support_plan_text: the app's SUPPORT_PLAN.md text.

    Return: the command, or "" when the plan names none.
    """
    return _command_under_heading(support_plan_text, REPRODUCE_HEADING)


def minimal_env(extra: dict[str, str] | None) -> dict[str, str]:
    """Build a replica subprocess's environment: a small known-safe base plus only the plan's
    own explicit fake data — never this process's full environment.

    Args:
        extra: variables to add on top of the safe base (the plan's fake data, or a repair
            agent's own sign-in token); None for none.

    Return: the environment dict to run a SUPPORT_PLAN.md command (or a Support agent) with.
    """
    base = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
    return {**base, **(extra or {})}


def run_plan_command(checkout: Path, command: str, *, fake_data_env: dict[str, str] | None = None) -> tuple[int | None, str]:
    """Run one SUPPORT_PLAN.md command in the checkout, with the minimal environment.

    Args:
        checkout: the app's checked-out working copy.
        command: the shell command the plan names.
        fake_data_env: extra environment variables for the command (the plan's fake-data setup).

    Return: (exit status or None on timeout, one plain line saying what was run and how it ended).
    """
    try:
        result = subprocess.run(
            command, shell=True, cwd=checkout, capture_output=True, timeout=REPLICATE_TIMEOUT_SECONDS,
            env=minimal_env(fake_data_env), stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None, f"ran `{command}` — timed out after {REPLICATE_TIMEOUT_SECONDS}s"
    return result.returncode, f"ran `{command}` — exit {result.returncode}"


def replicate(checkout: Path, *, fake_data_env: dict[str, str] | None = None) -> ReplicationResult:
    """Try to reproduce a case on one checked-out app copy, using only fake data.

    Args:
        checkout: the app's checked-out working copy (support_github.checkout_version's result).
        fake_data_env: extra environment variables the reproduce command runs with (the app's
            SUPPORT_PLAN.md fake-data setup); never real credentials.

    Return: a ReplicationResult. `replicated` is True only when SUPPORT_PLAN.md names a reproduce
    command and that command's exit status signals the problem happened (non-zero); `attempts`
    always lists what was tried, so a Not-replicated result still says what was tried.
    """
    plan_path = checkout / "SUPPORT_PLAN.md"
    if not plan_path.is_file():
        return ReplicationResult(False, [f"no SUPPORT_PLAN.md in the checked-out app at {checkout}"])
    command = reproduce_command(plan_path.read_text(encoding="utf-8", errors="replace"))
    if not command:
        return ReplicationResult(False, ["SUPPORT_PLAN.md names no reproduce command to try"])
    exit_status, attempt = run_plan_command(checkout, command, fake_data_env=fake_data_env)
    if exit_status is None:
        return ReplicationResult(False, [attempt + ", treated as not reproduced"])
    return ReplicationResult(exit_status != 0, [attempt])

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Support's own repair pair (review round 3, finding B1): the Delivery agent
that makes one fix on the `support/<case>` branch, and the independent
Reviewer from the OTHER vendor that reads that fix and gives a verdict — the
same two-vendor arrangement every NoMoreHappyPath task gets, run here as two
bounded, unattended command-line sessions inside the scratch checkout.

The repair agent is Claude (Anthropic); the reviewer is Codex (OpenAI). Both
are found the way the main harness finds them (`global_settings.provider_executable`,
honouring HARNESS_CLAUDE_BIN / HARNESS_CODEX_BIN), the repair agent signs in
with the harness's own saved Claude setup-token (`claude_auth`), and both run
with the replica's minimal environment — never this Support process's mail
password or GitHub credential. Nothing here reads the app's SUPPORT_PLAN.md
for a repair or review command: the plan is the owner's, not the agents'.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from harness import claude_auth, control, global_settings, support_replicate

REPAIR_PROVIDER = "claude"
REVIEW_PROVIDER = "codex"
REPAIR_TIMEOUT_SECONDS = 30 * 60
REVIEW_TIMEOUT_SECONDS = 20 * 60
VERDICT_LINE = re.compile(r"^\s*REVIEW:\s*(PASS|FAIL)\b\s*[-—:]?\s*(.*)$", re.IGNORECASE)

# The repair agent may edit and run things only inside the checkout: Claude Code's own sandbox
# confines its writes to the working folder, and edits inside it need no prompt (there is nobody
# to answer one). Network is not cut off by this; the plan forbids real data and services in words.
REPAIR_AGENT_SETTINGS = json.dumps({"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True}})
REPAIR_ALLOWED_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,MultiEdit"

REPAIR_BRIEF = """You are the Support repair agent for one reported case of this app. Work only inside this
folder, which is the app checked out at the exact version that reported the problem, on branch
support/{case_id}.

Case: {description}
Reported version: {version}
Trace excerpt:
{trace}

The reproduce command below exits non-zero while the problem happens and zero once it is fixed.
Right now it fails:
    {reproduce}

Fix the root cause of this problem in the app's own code, and add or extend a regression test
for it. Rules:
- never edit SUPPORT_PLAN.md (it is the owner's plan, not yours; a repair that changes it is refused);
- never use real data, real credentials or real external services; fake data only;
- do not commit, push, tag or change branches: Support commits your working-tree changes itself;
- when you are done, make sure the reproduce command above now exits zero.
"""

REVIEW_BRIEF = """You are the independent reviewer of one Support repair for this app (checked out here on
branch support/{case_id}, at the version that reported the problem). You did not write the fix;
judge it as a different vendor's reviewer would, from the diff and the code in this folder.

Case: {description}
Reported version: {version}
Trace excerpt:
{trace}

Reproduce command (exits non-zero while the problem happens): {reproduce}
Checks already run by Support: {checks}

The repair, as a unified diff against the reported version:
{diff}

Decide whether this fix really repairs the reported problem at its root, carries a regression test,
and does not weaken, remove or sidestep any test, check or protection to get there. Read whatever
code you need. End your answer with exactly one line of the form
REVIEW: PASS — <one sentence why>
or
REVIEW: FAIL — <one sentence why>
"""


@dataclass
class AgentRun:
    started: bool  # the agent process actually ran (False: it could not even be started here)
    ok: bool  # repair: the agent finished normally; review: the verdict line said PASS
    notes: str  # one plain sentence the case card shows


def vendor_pair() -> tuple[str, str]:
    """Name the two vendors Support's repair pair comes from.

    Return: (repair vendor, review vendor) — always two different companies; a same-vendor pair
    is a programming error here, never a configuration the owner can reach.
    """
    repair_vendor = str(control.PROVIDERS[REPAIR_PROVIDER]["vendor"])
    review_vendor = str(control.PROVIDERS[REVIEW_PROVIDER]["vendor"])
    if repair_vendor == review_vendor:
        raise RuntimeError("Support's repair agent and its reviewer must come from different vendors")
    return repair_vendor, review_vendor


def repair_brief(case: dict, reproduce: str) -> str:
    """Write the repair agent's brief for one case.

    Args:
        case: the case row (fingerprint, description, version, trace_excerpt).
        reproduce: the plan's reproduce command at the reported version.

    Return: the brief text passed to the repair agent as its prompt.
    """
    return REPAIR_BRIEF.format(
        case_id=case["fingerprint"], description=case.get("description", ""), version=case.get("version", ""),
        trace=(case.get("trace_excerpt") or "(none)")[:2000], reproduce=reproduce,
    )


def review_brief(case: dict, reproduce: str, checks: str, diff: str) -> str:
    """Write the independent reviewer's brief for one repair.

    Args:
        case: the case row.
        reproduce: the plan's reproduce command at the reported version.
        checks: Support's own failing-before / passing-after line.
        diff: the repair's unified diff against the reported version.

    Return: the brief text passed to the reviewer as its prompt.
    """
    return REVIEW_BRIEF.format(
        case_id=case["fingerprint"], description=case.get("description", ""), version=case.get("version", ""),
        trace=(case.get("trace_excerpt") or "(none)")[:2000], reproduce=reproduce, checks=checks, diff=diff,
    )


def preconditions() -> str:
    """Say what, if anything, stops the repair pair from starting on this Support machine.

    Return: "" when the Claude tool and its saved sign-in and the Codex tool are all present;
    otherwise one plain sentence naming the first missing piece, shown on the card as the reason
    no repair was attempted.
    """
    repair_vendor, review_vendor = vendor_pair()
    if not global_settings.provider_executable(REPAIR_PROVIDER):
        return f"the Claude command-line tool ({repair_vendor}), Support's repair agent, is not installed on this Support machine"
    try:
        claude_auth.resolve_token()
    except claude_auth.ClaudeAuthRequired as error:
        card = claude_auth.action(error.reason, error.detail)
        return f"Support's repair agent (Claude) has no sign-in on this Support machine: {card['why']} {card['instructions']}"
    if not global_settings.provider_executable(REVIEW_PROVIDER):
        return f"the Codex command-line tool ({review_vendor}), Support's independent reviewer, is not installed on this Support machine"
    return ""


def _agent_env(executable: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build an agent's environment: the replica's minimal base, the agent's own folder on PATH
    (Homebrew entry points need their interpreter), plus extra.

    Return: the environment dict.
    """
    env = support_replicate.minimal_env(extra)
    env["PATH"] = os.pathsep.join([str(Path(executable).resolve().parent), env.get("PATH", "")])
    return env


def _last_line(text: str) -> str:
    """The last non-empty line of text, or "" — one plain line for a card, never a wall of output."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def run_repair(checkout: Path, brief: str, budget_usd: float) -> AgentRun:
    """Run the Delivery repair agent once, unattended, inside checkout.

    Args:
        checkout: the app's scratch checkout, on the support/<case> branch.
        brief: repair_brief's text.
        budget_usd: the owner's Support spending ceiling (R9), handed to the agent as the most it
            may spend on this one repair; it stops by itself when that is reached.

    Return: an AgentRun. started is False when the agent could not even be launched here (no tool,
    no sign-in); ok is True when it ran to a normal end. Whether anything was actually fixed is
    judged afterwards by support_fix, from the working tree and the reproduce command — never from
    this return value alone.
    """
    problem = preconditions()
    if problem:
        return AgentRun(False, False, problem)
    executable = global_settings.provider_executable(REPAIR_PROVIDER)
    repair_vendor, _ = vendor_pair()
    env = _agent_env(executable, {claude_auth.TOKEN_ENV: claude_auth.resolve_token()})
    command = [
        executable, "-p", brief, "--output-format", "text", "--permission-mode", "acceptEdits",
        "--allowedTools", REPAIR_ALLOWED_TOOLS, "--max-budget-usd", f"{max(budget_usd, 0.01):.2f}", "--settings", REPAIR_AGENT_SETTINGS,
    ]
    try:
        completed = subprocess.run(
            command, cwd=checkout, capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=REPAIR_TIMEOUT_SECONDS, env=env,
        )
    except subprocess.TimeoutExpired:
        return AgentRun(True, False, f"the repair agent (Claude, {repair_vendor}) ran out of time after {REPAIR_TIMEOUT_SECONDS // 60} minutes")
    except OSError as error:
        return AgentRun(False, False, f"the repair agent (Claude, {repair_vendor}) could not be started: {error}")
    if completed.returncode != 0:
        detail = _last_line(completed.stderr) or _last_line(completed.stdout) or f"exit {completed.returncode}"
        return AgentRun(True, False, f"the repair agent (Claude, {repair_vendor}) stopped with an error: {detail}")
    return AgentRun(True, True, f"repair made by Claude ({repair_vendor})")


def _verdict(text: str) -> tuple[str, str]:
    """Find the reviewer's verdict line in its answer.

    Return: ("PASS" or "FAIL", its one-sentence reason), or ("", "") when no verdict line exists.
    """
    verdict, reason = "", ""
    for line in (text or "").splitlines():
        match = VERDICT_LINE.match(line)
        if match:
            verdict, reason = match.group(1).upper(), match.group(2).strip()
    return verdict, reason


def run_review(checkout: Path, brief: str) -> AgentRun:
    """Run the independent other-vendor reviewer once, read-only, over checkout.

    Args:
        checkout: the app's scratch checkout, on the support/<case> branch with the repair committed.
        brief: review_brief's text.

    Return: an AgentRun whose ok is True only when the reviewer's answer ends with a REVIEW: PASS
    line; a FAIL, a missing verdict, an error or a timeout are all not a pass.
    """
    executable = global_settings.provider_executable(REVIEW_PROVIDER)
    _, review_vendor = vendor_pair()
    if not executable:
        return AgentRun(False, False, f"the Codex command-line tool ({review_vendor}), Support's independent reviewer, is not installed on this Support machine")
    with tempfile.TemporaryDirectory(prefix="support-review-") as folder:
        last_message = Path(folder) / "last-message.txt"
        command = [
            executable, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "-C", str(checkout),
            "--output-last-message", str(last_message), brief,
        ]
        try:
            completed = subprocess.run(
                command, cwd=checkout, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                timeout=REVIEW_TIMEOUT_SECONDS, env=_agent_env(executable),
            )
        except subprocess.TimeoutExpired:
            return AgentRun(True, False, f"the independent reviewer (Codex, {review_vendor}) ran out of time after {REVIEW_TIMEOUT_SECONDS // 60} minutes")
        except OSError as error:
            return AgentRun(False, False, f"the independent reviewer (Codex, {review_vendor}) could not be started: {error}")
        answer = last_message.read_text(encoding="utf-8", errors="replace") if last_message.is_file() else ""
    verdict, reason = _verdict(answer or completed.stdout)
    label = f"independent review by Codex ({review_vendor})"
    if completed.returncode != 0:
        detail = _last_line(completed.stderr) or _last_line(completed.stdout) or f"exit {completed.returncode}"
        return AgentRun(True, False, f"{label} stopped with an error: {detail}")
    if verdict == "PASS":
        return AgentRun(True, True, f"{label}: PASS — {reason or 'no reason given'}")
    if verdict == "FAIL":
        return AgentRun(True, False, f"{label}: FAIL — {reason or 'no reason given'}")
    return AgentRun(True, False, f"{label} gave no PASS or FAIL verdict line, so the fix is not accepted")

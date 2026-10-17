#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Local, loopback-only lifecycle registry for visible CLI agent sessions."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.project_context import ProjectRoot, add_context_arguments, context_from_args, project_context


KINDS = {
    "codex_delivery": {"label": "CODEX CLI", "role": "Delivery Agent", "vendor": "OpenAI"},
    "claude_reviewer": {"label": "CLAUDE CLI", "role": "Independent Reviewer", "vendor": "Anthropic"},
    "claude_cto": {"label": "CTO (CLAUDE)", "role": "CTO", "vendor": "Anthropic"},
}
ROLE_SETTINGS = {
    "delivery": {"label": "Delivery Agent", "default_provider": "codex", "default_model": "gpt-5.6-sol", "default_effort": "high"},
    "cto": {"label": "CTO", "default_provider": "claude", "default_model": "opus", "default_effort": "high"},
    "reviewer": {"label": "Independent Reviewer", "default_provider": "claude", "default_model": "opus", "default_effort": "max"},
}
PROVIDERS = {
    "codex": {"label": "Codex", "short": "Codex", "binary_env": "HARNESS_CODEX_BIN", "vendor": "OpenAI"},
    "claude": {"label": "Claude", "short": "Claude", "binary_env": "HARNESS_CLAUDE_BIN", "vendor": "Anthropic"},
    # Antigravity CLI: the executable is `agy`, not the provider id (spec M-1).
    "gemini": {"label": "Gemini (Antigravity CLI)", "short": "Gemini", "binary_env": "HARNESS_AGY_BIN", "executable": "agy",
               "vendor": "Google"},
}
# Gemini sessions may only run Google's models (spec M-3): the Antigravity CLI
# also lists third-party models on some plans, and a Claude model behind the
# Gemini provider would defeat the cross-vendor review rule.
GEMINI_MODEL_PREFIX = "gemini-"
PROVIDER_EFFORTS = {
    # Verified live 2026-09-24: Codex's API accepts low … xhigh, max; its own
    # model catalog adds "ultra" (maximum reasoning with automatic task
    # delegation) on the Sol and Terra tiers.
    "codex": {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high", "max": "Max",
              "ultra": "Ultra (delegates subtasks)"},
    # Claude Code 2.1.281: --effort low, medium, high, xhigh, max.
    "claude": {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high", "max": "Max"},
    # Antigravity CLI 1.3.1: --effort low|medium|high|xhigh|max.
    "gemini": {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high", "max": "Max"},
}
# What each suggested model is for, in the providers' own words where they
# give them (the CLI pickers, 2026-09-24) — shown next to the ID in Settings
# so nobody has to remember which is which.
PROVIDER_MODEL_DESCRIPTIONS = {
    "codex": {
        "gpt-6-astra": "Frontier intelligence for the most demanding work (Codex default)",
        "gpt-6-sol": "Workhorse model for coding and everyday work",
        "gpt-6-luna": "Fast and affordable model for easier tasks",
        "gpt-5.6-sol": "Older coding model for complex work",
        "gpt-5.6-sol-wm": "Older coding model for complex work, working-memory variant",
        "gpt-5.6-terra": "Older balanced model for straightforward work",
        "gpt-5.6-luna": "Older fast and efficient model",
        "gpt-5.5": "Legacy coding model",
        "gpt-5.4": "Legacy general model, superseded by GPT-5.6",
        "gpt-5.4-mini": "Legacy small model for quick, simple tasks",
        "gpt-5.3-codex-spark": "Legacy fast coding model",
        "codex-auto-review": "Codex's own code-review model",
    },
    "claude": {
        "claude-opus-5-5[1m]": "Opus 5.5 with 1M context — best for everyday, complex tasks (Claude default)",
        "claude-opus-5-5": "Opus 5.5 — best for everyday, complex tasks",
        "opus[1m]": "Latest Opus with 1M context (alias)",
        "claude-fable-5-1[1m]": "Fable 5.1 with 1M context — most capable for the hardest, longest-running tasks",
        "claude-fable-5-1": "Fable 5.1 — most capable for the hardest, longest-running tasks",
        "fable": "Latest Fable (alias)",
        "claude-fable-5[1m]": "Fable 5 with 1M context — previous Fable",
        "claude-fable-5": "Fable 5 — previous Fable",
        "claude-opus-5": "Opus 5 — previous Opus",
        "claude-sonnet-5": "Sonnet 5 — efficient for routine tasks",
        "opus": "Latest Opus (alias)",
        "sonnet": "Latest Sonnet (alias) — efficient for routine tasks",
        "haiku": "Latest Haiku (alias) — fastest for quick answers",
        "claude-haiku-4-5-20251001": "Haiku 4.5 — fastest for quick answers",
    },
    "gemini": {
        "gemini-3.1-pro-high": "Gemini 3.1 Pro, high thinking — strongest, for complex work (Gemini default)",
        "gemini-3.1-pro-low": "Gemini 3.1 Pro, low thinking — strong and quicker",
        "gemini-3.8-flash-high": "Gemini 3.8 Flash, high thinking — fast and capable",
        "gemini-3.8-flash-medium": "Gemini 3.8 Flash, medium thinking — fast everyday model",
        "gemini-3.8-flash-low": "Gemini 3.8 Flash, low thinking — fastest, for simple tasks",
    },
}
PROVIDER_MODELS = {
    # Suggestions only, newest first. The settings UI also accepts a full
    # model ID so the harness does not become stale when a provider releases
    # another model. Nothing is removed from these lists when new entries
    # arrive (owner's rule, 2026-09-24); every entry was accepted by the
    # CLI on the day it was added.
    "codex": [
        "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
        "gpt-5.6-sol", "gpt-5.6-sol-wm", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark",
        "codex-auto-review",
    ],
    "claude": [
        # Claude Code's picker order (owner's paste, 2026-09-24): Opus 5.5
        # (the default, 1M context), Fable 5.1, Fable 5, Sonnet 5, Haiku 4.5.
        "claude-opus-5-5[1m]", "claude-opus-5-5", "opus[1m]",
        "claude-fable-5-1[1m]", "claude-fable-5-1", "fable",
        "claude-fable-5[1m]", "claude-fable-5", "claude-opus-5",
        "claude-sonnet-5", "opus", "sonnet", "haiku", "claude-haiku-4-5-20251001",
    ],
    # `agy models` on 2026-10-07, filtered to Google's own models (GEMINI_MODEL_PREFIX).
    "gemini": [
        "gemini-3.1-pro-high", "gemini-3.1-pro-low",
        "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
    ],
}
PROVIDER_DEFAULT_MODELS = {"codex": "gpt-5.6-sol", "claude": "opus", "gemini": "gemini-3.1-pro-high"}
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-\[\]]{0,119}$")
# Backward-compatible union for older callers; validation is provider-specific.
EFFORTS = {key: label for values in PROVIDER_EFFORTS.values() for key, label in values.items()}
KIND_TO_ROLE = {
    "codex_delivery": "delivery",
    "claude_reviewer": "reviewer",
    "claude_cto": "cto",
    "delivery": "delivery",
    "reviewer": "reviewer",
    "cto": "cto",
}
ACTIVE_STATUSES = {"launching", "running", "stopping", "pausing"}


def _attach_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if 0 < value < 1e7 else default


# How long a terminal may take to attach after it is launched: 30 s for a fresh launch.
# A staged resume relaunch waits in line with no clock (see _reconcile) and gets
# RESUME_ATTACH_SECONDS from the moment it is actually launched
# (mark_resume_launch_requested): Open used to expire staged sessions during a slow
# board start (2026-10-15).
LAUNCH_ATTACH_SECONDS = _attach_seconds("HARNESS_LAUNCH_ATTACH_SECONDS", 30.0)
RESUME_ATTACH_SECONDS = _attach_seconds("HARNESS_RESUME_ATTACH_SECONDS", 120.0)
# Hard limits for visible work. A stopping process keeps its slot until the
# registry observes it exited, preventing a fast double-click from exceeding
# capacity.
MAX_ACTIVE_SESSIONS = {
    "codex_delivery": 2,
    "claude_reviewer": 2,
    "claude_cto": 1,
}
SESSION_COLORS = {
    "black": {"label": "Standard black", "hex": "#000000", "rgb": (0, 0, 0)},
    "blue": {"label": "Ocean blue", "hex": "#123B5D", "rgb": (0x12, 0x3B, 0x5D)},
    "purple": {"label": "Plum purple", "hex": "#42275A", "rgb": (0x42, 0x27, 0x5A)},
    "green": {"label": "Forest green", "hex": "#164A35", "rgb": (0x16, 0x4A, 0x35)},
    "red": {"label": "Brick red", "hex": "#5A2525", "rgb": (0x5A, 0x25, 0x25)},
    "amber": {"label": "Dark amber", "hex": "#5A4314", "rgb": (0x5A, 0x43, 0x14)},
}


def session_identity(kind: str, provider: str) -> dict[str, str]:
    """The label and vendor a session of `kind` running `provider` shows and declares.

    The role kinds (`codex_delivery`, `claude_reviewer`, `claude_cto`) are role
    identities and stay as they are; what the window says and which vendor the
    board records come from the provider that actually runs (a Claude Delivery
    agent is not "CODEX CLI", and a Gemini reviewer is vendor Google).
    """
    short = {"codex": "CODEX", "claude": "CLAUDE", "gemini": "GEMINI"}[provider]
    label = f"CTO ({short})" if kind == "claude_cto" else f"{short} CLI"
    return {"label": label, "vendor": PROVIDERS[provider]["vendor"]}


def provider_executable_name(provider: str) -> str:
    """The program a provider runs as: its own `executable` key, else its id (Gemini's is `agy`)."""
    return str(PROVIDERS[provider].get("executable", provider))


def claude_launch_budget_seconds() -> float:
    """Worst case of a Claude sign-in check before the terminal opens: three roles in turn, two probes each, plus the usual 30 s."""
    try:
        return 6 * float(os.environ.get("HARNESS_CLAUDE_AUTH_TIMEOUT_SECONDS", "45")) + 30
    except ValueError:
        return 6 * 45 + 30


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_agent_settings() -> dict[str, dict[str, str]]:
    return {
        role: {
            "provider": values["default_provider"],
            "model": values["default_model"],
            "effort": values["default_effort"],
        }
        for role, values in ROLE_SETTINGS.items()
    }


def role_for_kind(kind: str) -> str:
    try:
        return KIND_TO_ROLE[kind]
    except KeyError as error:
        raise ValueError("unknown agent role") from error


def normalize_provider_effort(provider: str, effort: str) -> str:
    """The CLI spelling of an effort level.

    Both CLIs now accept both "xhigh" and "max" (Claude Code 2.1.281's
    --effort choices; Codex's API enum), so the levels pass through as
    saved. Only the human spellings are folded.
    """
    effort = str(effort).strip().lower().replace("extra-high", "xhigh").replace("extra high", "xhigh")
    return effort


ROLE_NOTICE_NAMES = {"cto": "CTO", "delivery": "Delivery", "reviewer": "Reviewer"}
GEMINI_LEVEL = re.compile(r"^(gemini-.+)-(low|medium|high)$")


def gemini_pair(model: str, effort: str, *, repair: bool) -> tuple[str, str, str]:
    """One consistent (model, effort) for Gemini, and a plain notice when something was changed.

    Antigravity's model ids already carry the thinking level (gemini-3.8-flash-low|medium|high) and the CLI
    rejects a pair where --model and --effort disagree, so the effort the owner chose selects the level of the
    model (the owner's intent wins). A level missing from the catalog is not refused (the catalog is only
    suggestions): the notice says to confirm it with Test connection. xhigh/max cannot exist for a suffixed
    model: refused when the owner saves it, repaired to the model's own level when it is an older stored value.
    Unsuffixed ids and other providers are untouched.
    """
    match = GEMINI_LEVEL.match(model)
    if not match:
        return model, effort, ""
    family, own = match.groups()
    if effort in {"low", "medium", "high"}:
        target = f"{family}-{effort}"
        if target == model:
            return model, effort, ""
        known = target in PROVIDER_MODELS["gemini"]
        notice = (
            f"Gemini's thinking level is part of the model name, so your {effort} effort selects {target} "
            f"(you had {model})"
            + ("" if known else f"; {target} is not in the list this app last saw, so press Test connection to confirm it exists")
        )
        return target, effort, notice
    if repair:
        return model, own, (
            f"Gemini model {model} carries its own thinking level ({own}); the stored effort {effort} was changed to {own}"
        )
    raise ValueError(
        f"Gemini's thinking level is part of the model name: {model} is already {own}. "
        "Choose low, medium or high effort (xhigh and max do not exist for Gemini models)"
    )


def normalize_provider_model(provider: str, model: str) -> str:
    """Return one explicit CLI model, migrating legacy provider-only settings."""
    provider = str(provider).strip().lower()
    model = str(model or "").strip()
    if not model:
        model = PROVIDER_DEFAULT_MODELS.get(provider, "")
    if not MODEL_PATTERN.fullmatch(model):
        raise ValueError("model must be a provider alias or model ID without spaces")
    if provider == "gemini" and not model.startswith(GEMINI_MODEL_PREFIX):
        raise ValueError(f"Gemini sessions run only Google's models (ids starting {GEMINI_MODEL_PREFIX}); {model} is not one")
    return model


def _validated_agent_settings(value: dict | None, *, notices: list[str] | None = None,
                              repair: bool = False) -> dict[str, dict[str, str]]:
    candidate = default_agent_settings() if value is None else value
    if not isinstance(candidate, dict):
        raise ValueError("agent settings must be an object")
    result: dict[str, dict[str, str]] = {}
    for role in ROLE_SETTINGS:
        entry = candidate.get(role)
        if not isinstance(entry, dict):
            raise ValueError(f"settings for {role} are required")
        provider = str(entry.get("provider", "")).strip().lower()
        effort = normalize_provider_effort(provider, entry.get("effort", ""))
        if provider not in PROVIDERS:
            raise ValueError(f"unsupported provider for {role}; choose codex, claude or gemini")
        model = normalize_provider_model(provider, entry.get("model", ""))
        if effort not in PROVIDER_EFFORTS[provider]:
            choices = ", ".join(PROVIDER_EFFORTS[provider])
            raise ValueError(f"unsupported effort for {role} with {provider}; choose {choices}")
        if provider == "gemini":
            model, effort, note = gemini_pair(model, effort, repair=repair)
            if note and notices is not None:
                notices.append(f"{ROLE_NOTICE_NAMES.get(role, role)}: {note}")
        result[role] = {"provider": provider, "model": model, "effort": effort}
    if result["delivery"]["provider"] == result["reviewer"]["provider"]:
        raise ValueError("Delivery and Independent Reviewer must use different providers so independent review remains claimable")
    return result


def agent_settings(root: Path) -> dict[str, dict[str, str]]:
    with locked_state(root) as state:
        try:
            validated = _validated_agent_settings(state.get("agent_settings"))
        except ValueError:
            # A prior build may have persisted a same-vendor or otherwise
            # malformed block. Keep the dashboard and settings dialog usable;
            # the next explicit POST can replace it with a valid configuration.
            validated = default_agent_settings()
        return json.loads(json.dumps(validated))


def update_agent_settings(root: Path, value: dict) -> dict[str, dict[str, str]]:
    validated = _validated_agent_settings(value)
    with locked_state(root) as state:
        state["agent_settings"] = validated
        return json.loads(json.dumps(validated))


def launch_settings(
    root: Path,
    kind: str,
    *,
    settings_override: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Resolve a legacy launch only from settings supplied by its caller.

    ``root`` remains in the signature for compatibility with callers that
    identify the project being launched.  It is deliberately not used as a
    settings source: in Projects Layer mode that project's control document
    does not own provider/model/effort settings.
    """
    if settings_override is None:
        raise ValueError("legacy launch requires an explicit settings override or managed session")
    role = role_for_kind(kind)
    settings = _validated_agent_settings(settings_override)[role]
    return {"role": role, **settings, "provider_label": PROVIDERS[settings["provider"]]["label"]}


def session_launch_settings(root: Path, session_id: str, kind: str) -> dict[str, str]:
    """Return the immutable provider/model/effort captured at button click."""
    with locked_state(root) as state:
        _reconcile(state)
        session = state.get("sessions", {}).get(session_id)
        if not session or session.get("kind") != kind:
            raise ValueError("managed session does not match the requested agent role")
        provider = str(session.get("provider", ""))
        model = normalize_provider_model(provider, session.get("model", ""))
        effort = normalize_provider_effort(provider, session.get("effort", ""))
        if provider not in PROVIDERS or effort not in PROVIDER_EFFORTS[provider]:
            raise ValueError("managed session has invalid launch settings")
        if provider == "gemini":
            # A session captured before the pair rule existed (or by another build) may still hold
            # flash-low with effort medium; the CLI rejects that pair, so repair it at launch too.
            model, effort, note = gemini_pair(model, effort, repair=True)
            if note and session.get("setting_repaired") != note:
                session.update({"model": model, "effort": effort, "setting_repaired": note})
        return {
            "role": role_for_kind(kind),
            "provider": provider,
            "model": model,
            "effort": effort,
            "provider_label": PROVIDERS[provider]["label"],
        }


def control_dir(root: ProjectRoot) -> Path:
    return project_context(root).storage_path("control")


def _state_path(root: Path) -> Path:
    return control_dir(root) / "sessions.json"


def _initial_state() -> dict[str, Any]:
    # Provider/model/effort are manager-global in Projects Layer mode.  The
    # field remains readable when an older standalone harness explicitly saved
    # it, but new project control documents never create a second copy.
    return {"sessions": {}, "inbox": {}, "instruction_receipts": {}}


MAX_INSTRUCTION_RECEIPTS = 1000


def _prune_instruction_receipts(state: dict[str, Any]) -> None:
    """Bound JSON rewrite cost without deleting queued or in-flight receipts."""
    receipts = state.setdefault("instruction_receipts", {})
    overflow = len(receipts) - MAX_INSTRUCTION_RECEIPTS
    if overflow <= 0:
        return
    terminal = sorted(
        (
            receipt for receipt in receipts.values()
            if receipt.get("status") in {"delivered", "discarded", "withdrawn"}
        ),
        key=lambda receipt: (receipt.get("delivered_at") or receipt.get("discarded_at")
                             or receipt.get("queued_at") or "", receipt.get("id") or ""),
    )
    for receipt in terminal[:overflow]:
        receipts.pop(receipt["id"], None)


def initialize(root: ProjectRoot) -> dict[str, Any]:
    """Create the project control document without local settings copies."""
    with locked_state(root) as state:
        _reconcile(state)
        return json.loads(json.dumps(state))


@contextmanager
def locked_state(root: Path) -> Iterator[dict[str, Any]]:
    directory = control_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        path = _state_path(root)
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else _initial_state()
        before = json.dumps(state, sort_keys=True)
        try:
            yield state
            serialized = json.dumps(state, indent=2, sort_keys=True) + "\n"
            if json.dumps(state, sort_keys=True) == before and path.exists():
                return
            temp = path.with_suffix(".tmp")
            temp.write_text(serialized, encoding="utf-8")
            os.replace(temp, path)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _pid_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


MANAGED_SESSION_ENV = "HARNESS_MANAGED_SESSION"
SESSION_SWEEP_GRACE_SECONDS = 0.5


def _processes_carrying(marker: str) -> list[int]:
    """PIDs whose environment holds exactly `marker`; the platform decides how to look."""
    from harness import platform_support
    return platform_support.process_identity().processes_with_environment_entry(marker)


def _sweep_session_descendants(session_id: str) -> list[int]:
    """Stop every process a managed session left behind; return their PIDs.

    Matching is by the inherited marker only, never by folder or program name,
    so the owner's own CLI sessions in the same project are never touched.
    A platform shell wrapping one of those CLIs exits once the CLI is
    stopped (see `processes_with_environment_entry` for what each platform
    can see).
    """
    marker = f"{MANAGED_SESSION_ENV}={session_id}"
    pids = _processes_carrying(marker)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    if pids:
        time.sleep(SESSION_SWEEP_GRACE_SECONDS)
        for pid in _processes_carrying(marker):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return pids


# 2026-10-01: a supervisor blocked on a CLI stuck in kernel exit kept its
# session "stopping" for ever after Stop all. A stop or pause requested longer
# ago than this is recorded as done; the OS clears the stuck process itself.
STOP_RECONCILE_SECONDS = 30.0
STUCK_STOP_REASON = (
    "stopped from control panel; the agent's process did not finish exiting, "
    "and the operating system will clear it"
)
STUCK_PAUSE_REASON = (
    "terminal intentionally paused with its saved session pointer; the agent's process did not "
    "finish exiting, and the operating system will clear it"
)


def _stop_overdue(session: dict[str, Any]) -> bool:
    requested = session.get("pause_requested_at") or session.get("stop_requested_at")
    if session["status"] not in {"stopping", "pausing"} or not requested:
        return False
    try:
        waited = (datetime.now(timezone.utc) - datetime.fromisoformat(str(requested))).total_seconds()
    except ValueError:
        return False
    return waited >= STOP_RECONCILE_SECONDS


def _reconcile(state: dict[str, Any]) -> None:
    from harness import attention      # not at module level: attention imports conversation, which imports this module
    try:
        _reconcile_sessions(state)
    finally:
        # A hold belongs to a live terminal: one that ended while held (in this pass or an earlier one) must reach relaunch and
        # recovery as an ordinary exit, so the hold ends with it.
        for session in state["sessions"].values():
            if session["status"] not in ACTIVE_STATUSES and (
                    session.get("limit_seen_at") or attention.is_limit_reason(session.get("attention_reason"))):
                session["attention_since"] = None
                session["attention_reason"] = None
                _end_limit_hold(session, session.get("ended_at"))


def _reconcile_sessions(state: dict[str, Any]) -> None:
    for session in state["sessions"].values():
        if session["status"] not in ACTIVE_STATUSES:
            continue
        if session["pid"]:
            alive = _pid_is_alive(session["pid"])
            if alive and _stop_overdue(session):
                paused = bool(session.get("pause_requested_at"))
                session.update({
                    "status": "paused" if paused else "stopped", "ended_at": now(),
                    "reason": STUCK_PAUSE_REASON if paused else STUCK_STOP_REASON,
                    "exit_unconfirmed": True,
                })
                continue
            if alive:
                continue
            swept = _sweep_session_descendants(str(session["id"]))
            if swept:
                session["swept_descendants"] = swept
            if session.get("pause_requested_at"):
                session["status"] = "paused"
            else:
                session["status"] = "stopped" if session.get("stop_requested_at") else "exited"
            session["ended_at"] = now()
            session["reason"] = (
                "terminal intentionally paused with its saved session pointer"
                if session.get("pause_requested_at") else
                "stopped from control panel" if session.get("stop_requested_at") else
                _ended_without_launch_reason(session)
                or "terminal session ended outside the viewer"
            )
            continue
        if session.get("resume_offer") == "relaunch" and not session.get("resume_launch_requested_at") and not session.get("pid"):
            # Staged for a relaunch that has not started: its attachment clock begins at
            # the actual launch (mark_resume_launch_requested), never while it waits in
            # line behind a slow board start or another session's launch.
            continue
        deadline = datetime.fromisoformat(session["launch_deadline"])
        if datetime.now(timezone.utc) >= deadline:
            session["status"] = "failed"
            session["ended_at"] = now()
            reported = session.get("launch_failure")
            if isinstance(reported, dict) and reported.get("reason"):
                session["reason"] = str(reported["reason"])      # the runner said why it never attached
            else:
                session["reason"] = "terminal session did not attach within its launch window"
                role = _launch_role_label(session)
                session["launch_failure"] = {
                    "pid": None, "code": None, "step": "attaching", "diagnostic": "", "signal": "", "setup_problem": False,
                    "reason": (f"The {role} terminal did not attach within its launch window and said nothing. Press Start for the "
                               f"{role} in Mission Control; the new terminal continues the role's latest conversation."),
                    "at": now(), "recorded_by": "reconcile",
                }


LAUNCH_STEP_PLAIN = "starting up"
MAX_LAUNCH_DIAGNOSTIC = 300
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_\-.]{32,}")


def sanitize_launch_diagnostic(text: Any) -> str:
    """The runner's last words, made safe to keep and show: no control characters, no token-like strings, bounded."""
    cleaned = "".join(character if character.isprintable() else " " for character in str(text or ""))
    cleaned = re.sub(r"HARNESS_[A-Z_]*TOKEN\S*", "[redacted]", cleaned)
    cleaned = re.sub(r"(?i)(bearer|authorization:?|token|secret|password|passwd|api[_-]?key|key)([=:\s]+)\S+", r"\1\2[redacted]", cleaned)
    cleaned = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@", r"\1[redacted]@", cleaned)
    cleaned = _TOKEN_SHAPE.sub("[redacted]", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[-MAX_LAUNCH_DIAGNOSTIC:]


def _launch_role_label(session: dict[str, Any]) -> str:
    return str(session.get("role") or KINDS.get(str(session.get("kind")), {}).get("role") or "agent")


def launch_failure_text(session: dict[str, Any], step: str, code: Any, diagnostic: str = "", signal_name: str = "") -> tuple[str, bool]:
    """The plain sentence for a terminal that stopped before its CLI started, and whether it is a setup problem."""
    role = _launch_role_label(session)
    step = " ".join(str(step or "").split()) or LAUNCH_STEP_PLAIN
    setup = "REFUSED" in diagnostic or code == 3
    if signal_name:
        sentence = f"The {role} terminal was closed by {signal_name} while it was {step}, before the CLI started."
    elif code in (126, 127) and step.startswith("starting the CLI"):
        sentence = f"The {role} terminal could not run the CLI program (exit code {code}): it is missing or not executable."
        setup = True
    else:
        sentence = f"The {role} terminal stopped while it was {step} (exit code {code}), before the CLI started."
    if diagnostic:
        sentence += f" The terminal said: {diagnostic}"
    if setup:
        sentence += " This is a setup problem; pressing Start again will not fix it. Check the role's provider, model and sign-in in Settings, then press Start."
    else:
        sentence += f" Press Start for the {role} in Mission Control; the new terminal continues the role's latest conversation."
    return sentence, setup


def _ended_without_launch_reason(session: dict[str, Any]) -> str:
    """A tracked attempt whose runner died before noting the CLI launch, with no report: say so honestly."""
    failure = session.get("launch_failure")
    if isinstance(failure, dict) and failure.get("pid") == session.get("pid") and failure.get("reason"):
        return str(failure["reason"])
    if not session.get("launch_tracked") or session.get("launch_exec_at") or session.get("superseded_by_session_id"):
        return ""
    role = _launch_role_label(session)
    reason = (
        f"The {role} terminal ended before the CLI started and the cause was not recorded "
        f"(it may have been killed or the computer shut down). Press Start for the {role} in Mission Control; "
        "the new terminal continues the role's latest conversation."
    )
    session["launch_failure"] = {
        "pid": session.get("pid"), "code": None, "step": "unknown", "diagnostic": "", "signal": "",
        "setup_problem": False, "reason": reason, "at": now(), "recorded_by": "reconcile",
    }
    return reason


def record_launch_failure(root: Path, session_id: str, pid: int, code: int, step: str,
                          diagnostic: str = "", signal_name: str = "") -> dict[str, Any]:
    """The runner reports that it is ending before it started the CLI.

    Accepted only from the pid recorded for the CURRENT attempt, and ignored when
    the owner stopped or paused the session or it was superseded: those are
    legitimate endings, not failures.
    """
    with locked_state(root) as state:
        session = state.get("sessions", {}).get(session_id)
        if not session:
            raise ValueError(f"unknown managed session: {session_id}")
        before_attach = session.get("pid") is None and session.get("status") == "launching"
        if session.get("pid") != pid and not before_attach:
            return {"recorded": False, "why": "a report from an earlier attempt"}
        if session.get("stop_requested_at") or session.get("pause_requested_at") or session.get("superseded_by_session_id"):
            return {"recorded": False, "why": "the session was stopped, paused or replaced"}
        diagnostic = sanitize_launch_diagnostic(diagnostic)
        reason, setup = launch_failure_text(session, step, code, diagnostic, signal_name)
        session["launch_failure"] = {
            "pid": session.get("pid"), "code": code, "step": step[:120], "diagnostic": diagnostic, "signal": signal_name[:12],
            "setup_problem": setup, "reason": reason, "at": now(), "recorded_by": "runner",
        }
        session["reason"] = reason
        return {"recorded": True, "reason": reason}


def create(root: Path, kind: str, task: str = "", color: str = "black",
           settings_override: dict[str, dict[str, str]] | None = None, continues: str = "") -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("unknown session kind")
    task = task.strip()
    color = str(color or "black").strip().lower()
    if task:
        raise ValueError("start an agent role only; give your direction later inside its CLI")
    if color not in SESSION_COLORS:
        raise ValueError("unknown terminal color; choose one of: " + ", ".join(SESSION_COLORS))
    role = role_for_kind(kind)
    with locked_state(root) as state:
        _reconcile(state)
        try:
            selected = _validated_agent_settings(
                settings_override if settings_override is not None else state.get("agent_settings")
            )[role]
        except ValueError:
            selected = default_agent_settings()[role]
        counterpart = {"delivery": "reviewer", "reviewer": "delivery"}.get(role)
        collision = next((
            entry for entry in state["sessions"].values()
            if counterpart
            and entry.get("status") in ACTIVE_STATUSES
            # A damaged or legacy record must never make every valid launch
            # fail. Unknown kinds have no safe counterpart role, so ignore
            # them for this collision check and let normal reconciliation or
            # later repair handle the bad historical record.
            and KIND_TO_ROLE.get(str(entry.get("kind", ""))) == counterpart
            and entry.get("provider") == selected["provider"]
        ), None)
        if collision:
            provider_label = PROVIDERS[selected["provider"]]["label"]
            raise ValueError(
                f"cannot start {ROLE_SETTINGS[role]['label']} with {provider_label} while an active "
                f"{ROLE_SETTINGS[counterpart]['label']} is already using {provider_label}; stop that session "
                "or choose a different provider so independent review remains available"
            )
        active_count = sum(
            entry["kind"] == kind and entry["status"] in ACTIVE_STATUSES
            for entry in state["sessions"].values()
        )
        capacity = MAX_ACTIVE_SESSIONS[kind]
        if active_count >= capacity:
            label = KINDS[kind]["label"]
            if kind == "claude_cto":
                raise ValueError("the CTO session is already active (limit 1)")
            raise ValueError(f"maximum {capacity} active {label} sessions reached; stop one before starting another")
        session_id = f"{kind}-{secrets.token_hex(5)}"
        session = {
            "id": session_id, "kind": kind, "task": task,
            "status": "launching", "pid": None, "created_at": now(),
            # The board-surface verifier is bound to this epoch.  Every
            # terminal-ending lifecycle transition advances it so a stale
            # environment credential can never become valid again on resume.
            "auth_epoch": 1,
            "attached_at": None, "ended_at": None, "stop_requested_at": None,
            "pause_requested_at": None,
            "resume_launch_requested_at": None,
            "reason": "waiting for the Terminal session to attach",
            "color": color,
            "color_hex": SESSION_COLORS[color]["hex"],
            "color_label": SESSION_COLORS[color]["label"],
            "last_output_at": None, "output_bytes": 0, "last_status_request_at": None,
            "attention_since": None, "attention_reason": None,
            **_cli_memory_fields(),
            "launch_deadline": (datetime.now(timezone.utc) + timedelta(seconds=LAUNCH_ATTACH_SECONDS)).isoformat(),
            **KINDS[kind],
            **session_identity(kind, selected["provider"]),
            "provider": selected["provider"],
            "model": selected["model"],
            "effort": selected["effort"],
            "provider_label": PROVIDERS[selected["provider"]]["label"],
        }
        _inherit_cli_memory(state, session, continues)
        state["sessions"][session_id] = session
        return dict(session)


def _inherit_cli_memory(state: dict[str, Any], session: dict[str, Any], only: str = "") -> None:
    """A new session of a role continues the last conversation of that role.

    Conversation memory used to follow the terminal session only, so stopping
    a Delivery agent and launching the role again started a blank agent while
    the previous afternoon's discussion sat readable in the old session's
    view (2026-09-23). The newest ENDED session of the same kind that carries
    a CLI session id hands it on, with its launch count, so the runner's plan
    resumes it. A session that is still active never hands its conversation
    to a second live terminal: two Delivery agents may run at once, and each
    must own its own CLI session.
    """
    kind = session.get("kind")
    live_ids = {
        str(item.get("cli_session_id"))
        for item in state.get("sessions", {}).values()
        if item.get("kind") == kind and item.get("status") in ACTIVE_STATUSES and item.get("cli_session_id")
    }
    candidates = [
        item for item in state.get("sessions", {}).values()
        if item.get("kind") == kind and item.get("id") != session.get("id")
        and item.get("status") not in ACTIVE_STATUSES
        and item.get("cli_session_id") and str(item.get("cli_session_id")) not in live_ids
        and (not only or item.get("id") == only)
    ]
    if not candidates:
        return
    predecessor = max(candidates, key=lambda item: str(item.get("ended_at") or item.get("created_at") or ""))
    session["cli_session_id"] = predecessor["cli_session_id"]
    session["cli_session_provider"] = predecessor.get("cli_session_provider")
    session["cli_launches"] = max(int(predecessor.get("cli_launches") or 1), 1)
    session["continues_session"] = predecessor["id"]
    # Plumbing Stage 3: messages the predecessor's channel POSTED into its CLI
    # but never saw arrive go to the session that continues it, under the SAME
    # id. Its channel checks the conversation for that id before posting, so a
    # message the old CLI did consume is acknowledged, not sent twice; one it
    # did not is delivered exactly once (review r2 design check).
    # Every message the ended session had not DELIVERED - still queued, taken
    # but unsent, or posted but unseen - moves to the successor (review r2 audit:
    # a supervisor that ended with messages taken or in flight left them
    # "taken" for ever). `carried_from` makes the successor check the
    # conversation before sending, so nothing arrives twice.
    texts = {entry.get("id"): entry for entry in state.get("inbox", {}).pop(predecessor["id"], [])}
    carried: list[dict[str, Any]] = []
    for receipt in state.get("instruction_receipts", {}).values():
        if receipt.get("session_id") != predecessor["id"] or receipt.get("status") not in {"queued", "taken", "posted"}:
            continue
        original = texts.get(receipt.get("id"), {})
        text = receipt.get("text") or original.get("text")
        if not text:
            continue
        receipt.update({"session_id": session["id"], "status": "queued", "carried_from": predecessor["id"],
                        "taken_at": None})
        carried.append({"id": receipt["id"], "session_id": session["id"], "text": text,
                        "source": receipt.get("source") or original.get("source") or "controller",
                        "queued_at": receipt.get("queued_at") or original.get("queued_at") or now(),
                        "carried_from": predecessor["id"]})
    if carried:
        state.setdefault("inbox", {}).setdefault(session["id"], [])[:0] = carried


def _validated_session_capture(capture: dict[str, Any]) -> dict[str, str]:
    """A captured provider/model/effort, valid on its own (not against today's other roles)."""
    provider = str(capture.get("provider", "")).strip().lower()
    if provider not in PROVIDERS:
        raise ValueError("captured provider is not supported")
    effort = normalize_provider_effort(provider, capture.get("effort", ""))
    model = normalize_provider_model(provider, capture.get("model", ""))
    if effort not in PROVIDER_EFFORTS[provider]:
        raise ValueError("captured effort is not supported")
    if provider == "gemini":
        model, effort, _note = gemini_pair(model, effort, repair=False)
    return {"provider": provider, "model": model, "effort": effort}


def _paused_session_capture(root: Path, session_id: str, kind: str) -> dict[str, Any]:
    """The identity the project pause saved for this session, or {} for an older pause."""
    from harness import board
    try:
        agents = board.pause_state(root).get("agents") or {}
    except (OSError, ValueError):
        return {}
    for saved in agents.values():
        if isinstance(saved, dict) and saved.get("session_id") == session_id:
            capture = saved.get("session_capture")
            if isinstance(capture, dict) and capture.get("kind", kind) == kind:
                return dict(capture)
    return {}


def restore_missing_resume_session(
    root: Path, session_id: str, kind: str,
    settings_override: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Recreate one missing control transport named by a paused board owner.

    The board's saved session pointer remains the identity authority.  Reusing
    that exact ID lets the relaunched runner reattach to the existing agent
    rather than registering a second Delivery owner.
    """
    session_id = str(session_id or "").strip()
    if kind not in KINDS or not session_id.startswith(f"{kind}-"):
        raise ValueError("saved resume session identity does not match its role")
    capture = _paused_session_capture(root, session_id, kind)
    with locked_state(root) as state:
        _reconcile(state)
        existing = state.get("sessions", {}).get(session_id)
        if existing:
            if existing.get("kind") != kind:
                raise ValueError("saved resume session has a conflicting role")
            return dict(existing)
        role = role_for_kind(kind)
        try:
            selected = _validated_agent_settings(
                settings_override if settings_override is not None else state.get("agent_settings")
            )[role]
        except ValueError:
            selected = default_agent_settings()[role]
        memory = _cli_memory_fields()
        if capture:
            # The pause recorded what this session ran as; restore that, not
            # whatever the settings say today (P2d). An unusable capture
            # falls back to the settings, as an older pause always did.
            try:
                selected = _validated_session_capture(capture)
                if capture.get("cli_session_id"):
                    memory.update({
                        "cli_session_id": capture["cli_session_id"],
                        "cli_session_provider": capture.get("cli_session_provider") or selected["provider"],
                        "cli_launches": max(int(capture.get("cli_launches") or 0), 1),
                    })
                if capture.get("continues_session"):
                    memory["continues_session"] = str(capture["continues_session"])
            except (ValueError, TypeError):
                memory = _cli_memory_fields()
        active_count = sum(
            entry.get("kind") == kind and entry.get("status") in ACTIVE_STATUSES
            for entry in state.get("sessions", {}).values()
        )
        if active_count >= MAX_ACTIVE_SESSIONS[kind]:
            raise ValueError("cannot restore the saved terminal because its role is at capacity")
        created_at = now()
        session = {
            "id": session_id, "kind": kind, "task": "",
            "status": "launching", "pid": None, "created_at": created_at,
            "auth_epoch": 1,
            "attached_at": None, "ended_at": None, "stop_requested_at": None,
            "pause_requested_at": None, "resume_launch_requested_at": None,
            "reason": "missing saved terminal transport reconstructed for project resume",
            "color": "black", "color_hex": SESSION_COLORS["black"]["hex"],
            "color_label": SESSION_COLORS["black"]["label"],
            "last_output_at": None, "output_bytes": 0, "last_status_request_at": None,
            "attention_since": None, "attention_reason": None,
            **memory,
            "launch_deadline": (
                datetime.now(timezone.utc) + timedelta(seconds=30)
            ).isoformat(),
            **KINDS[kind],
            **session_identity(kind, selected["provider"]),
            "provider": selected["provider"], "model": selected["model"],
            "effort": selected["effort"],
            "provider_label": PROVIDERS[selected["provider"]]["label"],
        }
        state.setdefault("sessions", {})[session_id] = session
        return dict(session)


def _cli_memory_fields() -> dict[str, Any]:
    """The CLI's own conversation identity, kept so a relaunch can resume it.

    `cli_session_id` is the vendor's session id (minted by the harness for
    Claude Code, discovered from the rollout for Codex). `cli_launches` counts
    how many times a CLI was started for this managed session; the first
    launch is fresh, every later one tries to resume. Both survive pause,
    resume and harness restarts because the session record does.
    """
    return {
        "cli_session_id": None, "cli_session_provider": None,
        "cli_launches": 0, "cli_last_launch_at": None, "cli_last_launch_resumed": False,
        "continues_session": None,
    }


def _read_state(root: Path) -> dict[str, Any]:
    """A read-only snapshot of the control document, without the write lock.

    Every write replaces the file atomically (`locked_state`: temp file then
    `os.replace`), so a reader always sees one whole version. A managed agent
    may read here but may not take the lock: the control directory is
    harness-owned storage it cannot write (F-1), and opening `.lock` for
    append is a write.
    """
    path = _state_path(root)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else _initial_state()


def live_session_ids(root: Path) -> set[str]:
    """Sessions whose terminal process is still running - read WITHOUT the lock.

    The board calls this while holding ITS lock (cold-state cleanup), so it
    must not take this module's lock: a lock-order inversion with a control
    writer that touches the board would deadlock. A stale "running" record
    whose process has gone is not live.
    """
    live: set[str] = set()
    for session_id, session in (_read_state(root).get("sessions") or {}).items():
        pid = session.get("pid")
        if session.get("status") in ACTIVE_STATUSES and pid and _pid_is_alive(pid):
            live.add(session_id)
    return live


def cli_session(root: Path, session_id: str) -> dict[str, Any]:
    session = _read_state(root).get("sessions", {}).get(session_id)
    if not session:
        raise ValueError(f"unknown managed session: {session_id}")
    return {
        "session_id": session_id,
        "cli_session_id": session.get("cli_session_id"),
        "cli_session_provider": session.get("cli_session_provider"),
        "cli_launches": int(session.get("cli_launches") or 0),
        "cli_last_launch_at": session.get("cli_last_launch_at"),
        "cli_last_launch_resumed": bool(session.get("cli_last_launch_resumed")),
        "continues_session": session.get("continues_session"),
        "resumed_after_pause_at": session.get("resumed_after_pause_at"),
    }


def record_cli_session(root: Path, session_id: str, cli_session_id: str, provider: str) -> dict[str, Any]:
    cli_session_id = str(cli_session_id or "").strip()
    if not cli_session_id:
        raise ValueError("a CLI session id is required")
    with locked_state(root) as state:
        session = state.get("sessions", {}).get(session_id)
        if not session:
            raise ValueError(f"unknown managed session: {session_id}")
        session["cli_session_id"] = cli_session_id
        session["cli_session_provider"] = str(provider or "")
        return {"session_id": session_id, "cli_session_id": cli_session_id, "provider": session["cli_session_provider"]}


def clear_cli_session(root: Path, session_id: str, reason: str = "") -> dict[str, Any]:
    """Forget a CLI session id that can no longer be resumed.

    A stale id is worse than none: the supervisor only discovers a new Codex
    rollout while the record has NO id, so a fallback that left the old id in
    place made every later relaunch start fresh forever (round-2 finding).
    """
    with locked_state(root) as state:
        session = state.get("sessions", {}).get(session_id)
        if not session:
            raise ValueError(f"unknown managed session: {session_id}")
        previous = session.get("cli_session_id")
        session["cli_session_id"] = None
        session["cli_session_provider"] = None
        session["cli_session_cleared_reason"] = str(reason or "")
        return {"session_id": session_id, "cleared": previous, "reason": session["cli_session_cleared_reason"]}


def note_cli_launch(root: Path, session_id: str, resumed: bool) -> dict[str, Any]:
    with locked_state(root) as state:
        session = state.get("sessions", {}).get(session_id)
        if not session:
            raise ValueError(f"unknown managed session: {session_id}")
        session["cli_launches"] = int(session.get("cli_launches") or 0) + 1
        session["cli_last_launch_at"] = now()
        session["cli_last_launch_resumed"] = bool(resumed)
        # The launch that follows a pause has now been planned with the pause
        # in its prompt; a later relaunch (a crash, say) is an ordinary one.
        # (Not cleared on attach: the runner attaches BEFORE it plans.)
        session["resumed_after_pause_at"] = None
        session["launch_noted_at"] = now()
        return {"session_id": session_id, "cli_launches": session["cli_launches"], "resumed": bool(resumed)}


def note_cli_exec(root: Path, session_id: str) -> dict[str, Any]:
    """The runner is about to replace itself with the CLI: from here a death is the CLI's, not the launch's."""
    with locked_state(root) as state:
        session = state.get("sessions", {}).get(session_id)
        if not session:
            raise ValueError(f"unknown managed session: {session_id}")
        session["launch_exec_at"] = now()
        return {"session_id": session_id, "launch_exec_at": session["launch_exec_at"]}


def attach(root: Path, session_id: str, pid: int) -> dict[str, Any]:
    if pid <= 0:
        raise ValueError("pid must be positive")
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session:
            raise ValueError("unknown session")
        if session["status"] != "launching":
            raise ValueError("session was cancelled before it attached")
        try:
            terminal_tty = os.ttyname(0)
        except OSError:
            terminal_tty = ""
        earlier = session.get("launch_failure")
        if earlier:
            history = list(session.get("launch_failures") or [])
            history.append(earlier)
            session["launch_failures"] = history[-5:]
        session.update({
            "status": "running", "pid": pid, "terminal_tty": terminal_tty, "attached_at": now(),
            "reason": "interactive CLI session is running",
            # A new attempt: the CLI is not launched yet, and no failure is current.
            "launch_tracked": True, "launch_noted_at": None, "launch_exec_at": None, "launch_failure": None,
        })
        return dict(session)


def enqueue_instruction(root: Path, session_id: str, text: str, source: str = "controller") -> dict[str, Any]:
    """Queue a visible control message for the terminal supervisor to type."""
    text = text.strip()
    if not text:
        raise ValueError("instruction text is required")
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session or session["status"] not in ACTIVE_STATUSES:
            raise ValueError("cannot route to an inactive managed session")
        if session.get("read_only") or session.get("superseded_by_session_id"):
            raise ValueError("cannot route to a superseded read-only managed session")
        entry = {
            "id": secrets.token_hex(6),
            "session_id": session_id,
            "text": text,
            "source": source[:80],
            "queued_at": now(),
        }
        # The receipt keeps the message, so a continuing session can be given
        # exactly this message under exactly this id.
        state.setdefault("inbox", {}).setdefault(session_id, []).append(entry)
        state.setdefault("instruction_receipts", {})[entry["id"]] = {
            "id": entry["id"],
            "session_id": session_id,
            "text": entry["text"],
            "source": entry["source"],
            "status": "queued",
            "queued_at": entry["queued_at"],
            "taken_at": None,
            "delivered_at": None,
        }
        _prune_instruction_receipts(state)
        return dict(entry)


def record_output(root: Path, session_id: str, byte_count: int) -> dict[str, Any]:
    """Record recent child output as activity evidence, never as a board poll."""
    if byte_count <= 0:
        raise ValueError("output byte count must be positive")
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session or session["status"] not in ACTIVE_STATUSES:
            raise ValueError("cannot record output for an inactive managed session")
        session["last_output_at"] = now()
        session["output_bytes"] = int(session.get("output_bytes", 0)) + byte_count
        return {"session_id": session_id, "last_output_at": session["last_output_at"], "output_bytes": session["output_bytes"]}


TURN_STATES = {"session_started", "working", "idle_at_prompt", "session_ended"}


def record_turn_state(root: Path, session_id: str, turn_state: str) -> dict[str, Any]:
    """The CLI's own lifecycle signal, delivered by its hooks (plumbing Stage 1).

    `idle_at_prompt` means the CLI reported the turn finished; `working` that a
    prompt was submitted. Unlike byte activity, this is never a guess.
    """
    if turn_state not in TURN_STATES:
        raise ValueError(f"unknown turn state: {turn_state}")
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session:
            raise ValueError("unknown managed session")
        session["turn_state"] = turn_state
        session["turn_state_at"] = now()
        return {"session_id": session_id, "turn_state": turn_state, "turn_state_at": session["turn_state_at"]}


def record_observed_model(root: Path, session_id: str, model: str) -> bool:
    """The model a Gemini turn actually used (its hook reports it); True when it is not a Google model.

    The owner can switch models inside the CLI's own window. A Gemini session on a third-party model is
    no longer vendor Google, so it must not take a review assignment (spec M-3, cross-vendor rule).
    """
    model = str(model or "").strip()
    with locked_state(root) as state:
        session = state["sessions"].get(session_id)
        if not session or session.get("provider") != "gemini" or not model:
            return False
        mismatch = not model.startswith(GEMINI_MODEL_PREFIX)
        changed = bool(session.get("model_mismatch")) != mismatch
        requested = str(session.get("model") or "")
        # Any Google model other than the one the app asked for (another thinking level, another family)
        # is recorded, not only a switch to a non-Google model.
        session["selection_differs"] = (
            f"asked for {requested}, the window reports {model[:120]}"
            if not mismatch and requested.startswith(GEMINI_MODEL_PREFIX) and model.startswith(GEMINI_MODEL_PREFIX) and model != requested
            else ""
        )
        session["observed_model"] = model[:120]
        session["model_mismatch"] = model[:120] if mismatch else ""
        return mismatch and changed


LIMIT_ROLE_LABELS = {"delivery": "Delivery agent", "reviewer": "Reviewer", "cto": "CTO"}
LIMIT_FIELDS = ("limit_seen_at", "limit_resets_at", "limit_kind", "limit_when_text", "limit_nudged_at", "limit_nudge_note")


def _end_limit_hold(session: dict[str, Any], cleared_at: str | None = None) -> None:
    """The usage-limit hold of a session ends: keep what was seen as history (`last_limit_*`), clear what holds the terminal."""
    if session.get("limit_seen_at"):
        session["last_limit_seen_at"] = session.get("limit_seen_at")
        session["last_limit_resets_at"] = session.get("limit_resets_at")
        session["limit_cleared_at"] = cleared_at or now()
    for field in LIMIT_FIELDS:
        session.pop(field, None)


def record_attention(root: Path, session_id: str, reason: str, limit: dict[str, Any] | None = None) -> dict[str, Any]:
    """The terminal stopped to wait for the owner; say so on the session record.

    Set once per wait: a redraw that repeats the same prompt keeps the original
    time, so the banner can say how long the owner has been needed.

    A usage-limit message also carries its `limit` incident (when it was seen, the instant it announced, announced or estimated):
    a new incident resets the nudge bookkeeping, a repeat of the same one changes nothing.
    """
    from harness import attention
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("an attention reason is required")
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session or session["status"] not in ACTIVE_STATUSES:
            raise ValueError("cannot record attention for an inactive managed session")
        if not session.get("attention_since"):
            session["attention_since"] = now()
        session["attention_reason"] = reason[:200]
        if limit:
            if session.get("limit_seen_at") != limit.get("seen_at"):
                for field in ("limit_nudged_at", "limit_nudge_note", "limit_cleared_at"):
                    session.pop(field, None)
            session["limit_seen_at"] = limit.get("seen_at")
            session["limit_resets_at"] = limit.get("resets_at")
            session["limit_kind"] = limit.get("kind")
            session["limit_when_text"] = str(limit.get("when_text") or "")[:80]
        elif not attention.is_limit_reason(reason) and session.get("limit_seen_at"):
            _end_limit_hold(session)    # another prompt replaced the limit line on screen
        return {"session_id": session_id, "attention_since": session["attention_since"], "attention_reason": session["attention_reason"]}


def clear_attention(root: Path, session_id: str) -> dict[str, Any]:
    """The owner answered, or the terminal moved on: nothing is waiting any more."""
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session:
            raise ValueError("unknown managed session")
        session["attention_since"] = None
        session["attention_reason"] = None
        _end_limit_hold(session)
        return {"session_id": session_id, "attention_since": None, "attention_reason": None}


def limit_state(root: Path, session_id: str) -> dict[str, Any]:
    """What the supervisor needs to decide a nudge: the session's role, status and its usage-limit fields."""
    session = _read_state(root).get("sessions", {}).get(session_id)
    if not session:
        raise ValueError(f"unknown managed session: {session_id}")
    return {"role": KIND_TO_ROLE.get(str(session.get("kind", "")), ""), "status": session.get("status"),
            "attention_reason": session.get("attention_reason"),
            **{field: session.get(field) for field in LIMIT_FIELDS}}


def record_limit_nudge(root: Path, session_id: str, *, sent: bool, note: str = "") -> dict[str, Any]:
    """The supervisor's one nudge into a held terminal: `limit_nudged_at` is set only when the write was confirmed."""
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session or session["status"] not in ACTIVE_STATUSES or not session.get("limit_seen_at"):
            raise ValueError("no usage-limit hold on this session")
        if sent:
            session["limit_nudged_at"] = now()
            session.pop("limit_nudge_note", None)
        else:
            session["limit_nudge_note"] = str(note or "not sent")[:120]
        return {"session_id": session_id, "limit_nudged_at": session.get("limit_nudged_at"), "limit_nudge_note": session.get("limit_nudge_note")}


def waiting_sessions(root: Path) -> list[dict[str, Any]]:
    """Active sessions whose terminal is waiting for the owner, oldest wait first."""
    from harness import attention
    waiting = [
        item for item in snapshot(root).get("sessions", [])
        # A terminal waiting out a usage limit does not need the owner (it continues by itself): it is not an alarm.
        if item.get("status") in ACTIVE_STATUSES and item.get("attention_since") and not attention.is_limit_reason(item.get("attention_reason"))
    ]
    return sorted(waiting, key=lambda item: str(item.get("attention_since")))


def limit_notices(root: Path) -> list[dict[str, Any]]:
    """Plain one-line notices for live terminals that show their provider's usage-limit message, oldest first."""
    held = [item for item in snapshot(root).get("sessions", []) if item.get("limit_notice")]
    return [{"id": str(item.get("id", "")), "label": str(item.get("label", "")), "role": str(item.get("role", "")), "notice": item["limit_notice"]}
            for item in sorted(held, key=lambda item: str(item.get("attention_since")))]


def request_status_update(root: Path, session_id: str) -> dict[str, Any] | None:
    """Route one concise update request for an active output-producing session."""
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session or session["status"] not in ACTIVE_STATUSES:
            return None
        previous = session.get("last_status_request_at")
        if previous:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(previous)).total_seconds()
            if age < 60:
                return None
        entry = {
            "id": secrets.token_hex(6),
            "session_id": session_id,
            "text": "Post a short board status update now. Your terminal output shows active work, but the board heartbeat is overdue. This is an internal reminder; owner action is not required.",
            "source": "liveness",
            "queued_at": now(),
        }
        state.setdefault("inbox", {}).setdefault(session_id, []).append(entry)
        state.setdefault("instruction_receipts", {})[entry["id"]] = {
            "id": entry["id"],
            "session_id": session_id,
            "text": entry["text"],
            "source": entry["source"],
            "status": "queued",
            "queued_at": entry["queued_at"],
            "taken_at": None,
            "delivered_at": None,
        }
        _prune_instruction_receipts(state)
        session["last_status_request_at"] = now()
        return dict(entry)


def take_instructions(root: Path, session_id: str) -> list[dict[str, Any]]:
    """Atomically deliver all pending control messages to one terminal."""
    with locked_state(root) as state:
        _reconcile(state)
        session = state.get("sessions", {}).get(session_id, {})
        if session.get("read_only") or session.get("superseded_by_session_id"):
            discarded = state.setdefault("inbox", {}).pop(session_id, [])
            for entry in discarded:
                receipt = state.setdefault("instruction_receipts", {}).get(entry.get("id"), {})
                receipt.update({"status": "discarded", "discarded_at": now()})
            return []
        entries = state.setdefault("inbox", {}).pop(session_id, [])
        taken_at = now()
        for entry in entries:
            receipt = state.setdefault("instruction_receipts", {}).get(entry.get("id"), {})
            receipt.update({"status": "taken", "taken_at": taken_at})
        return json.loads(json.dumps(entries))


def return_instructions(root: Path, session_id: str, entries: list[dict[str, Any]]) -> int:
    """Put taken-but-untyped messages back at the head of a terminal's queue.

    Backlog #8, review round 1: a terminal that needs sign-in must not hold
    messages the board can no longer withdraw. When its supervisor sees the
    sign-in prompt it returns what it had taken but not typed, so a review
    moved to another reviewer takes its old assignment back cleanly.
    Entries that are no longer "taken" (withdrawn, delivered) are dropped.
    """
    returned: list[dict[str, Any]] = []
    with locked_state(root) as state:
        receipts = state.setdefault("instruction_receipts", {})
        for entry in entries:
            receipt = receipts.get(entry.get("id"), {})
            if receipt.get("status") != "taken":
                continue
            receipt.update({"status": "queued", "taken_at": None})
            returned.append(dict(entry))
        if returned:
            inbox = state.setdefault("inbox", {})
            inbox[session_id] = returned + list(inbox.get(session_id, []))
    return len(returned)


def withdraw_instruction(root: Path, instruction_id: str) -> dict[str, Any]:
    """Remove a queued instruction before the supervisor takes it.

    2026-09-25 defect #10: a reviewer wake stayed queued after its review was
    cancelled, so the reviewer woke to "no open QA request". Returns the
    receipt; ``status`` is ``withdrawn`` when the message never reached the
    terminal, otherwise the caller must tell the terminal itself.
    """
    with locked_state(root) as state:
        receipt = state.setdefault("instruction_receipts", {}).get(instruction_id)
        if not receipt:
            raise ValueError("unknown instruction receipt")
        if receipt.get("status") == "queued":
            session_id = str(receipt.get("session_id") or "")
            inbox = state.setdefault("inbox", {})
            inbox[session_id] = [entry for entry in inbox.get(session_id, []) if entry.get("id") != instruction_id]
            if not inbox[session_id]:
                inbox.pop(session_id, None)
            receipt.update({"status": "withdrawn", "withdrawn_at": now()})
        return dict(receipt)


def discard_instruction(root: Path, session_id: str, instruction_id: str, reason: str) -> dict[str, Any]:
    """A message the supervisor holds (taken, or returned to its queue) turned out to be obsolete: it is never typed.

    `withdraw_instruction` can only take back what is still queued; this closes a message that was already taken.
    """
    with locked_state(root) as state:
        receipt = state.setdefault("instruction_receipts", {}).get(instruction_id)
        if not receipt:
            raise ValueError("unknown instruction receipt")
        if receipt.get("status") in {"queued", "taken"}:
            # Only what has not been typed yet: a posted or delivered message is already in the CLI and cannot be recalled here.
            receipt.update({"status": "discarded", "discarded_at": now(), "reason": str(reason)[:160]})
        inbox = state.setdefault("inbox", {})
        if session_id in inbox:
            inbox[session_id] = [entry for entry in inbox[session_id] if entry.get("id") != instruction_id]
            if not inbox[session_id]:
                inbox.pop(session_id, None)
        return dict(receipt)


REVIEW_WAKE_SOURCES = ("review-assignment", "review-delivery-succeeded")


def invalidate_review_wakes(root: Path, session_id: str, request_id: str, reason: str) -> int:
    """Close every review wake about `request_id` that this terminal has not typed yet (queued or taken).

    A review wake announces one request to one reviewer. When that reviewer's reservation expires or the request is routed again,
    every wake about it that is still waiting - the assignment and the "Delivery succeeded" message alike - is obsolete, however
    long its terminal waits (a usage limit, a sign-in) before it would type them.
    """
    closed = 0
    with locked_state(root) as state:
        inbox = state.setdefault("inbox", {})
        for receipt in state.setdefault("instruction_receipts", {}).values():
            if (receipt.get("session_id") == session_id and receipt.get("source") in REVIEW_WAKE_SOURCES
                    and receipt.get("status") in {"queued", "taken"} and request_id and request_id in str(receipt.get("text") or "")):
                receipt.update({"status": "discarded", "discarded_at": now(), "reason": str(reason)[:160]})
                closed += 1
        if closed and session_id in inbox:
            alive = {receipt.get("id") for receipt in state["instruction_receipts"].values() if receipt.get("status") == "queued"}
            inbox[session_id] = [entry for entry in inbox[session_id] if entry.get("id") in alive]
            if not inbox[session_id]:
                inbox.pop(session_id, None)
    return closed


def reclaim_stranded(root: Path, session_id: str) -> int:
    """A relaunched terminal takes back what its previous run left in flight.

    Stage 3 review r3b (2026-10-03): a pause force-stops a terminal after its
    bounded wait, and a killed supervisor never runs its own clean-up - the
    owner's direction, the Go ahead and the pause notice stayed "taken" for
    ever. A terminal starts with nothing in hand, so anything of THIS session
    still "taken" or "posted" belongs to a run that ended. It goes back to the
    head of the queue under the same id, marked `carried_from`, so it is
    checked against the conversation before it is sent (it may have arrived).
    """
    with locked_state(root) as state:
        receipts = state.setdefault("instruction_receipts", {})
        inbox = state.setdefault("inbox", {}).setdefault(session_id, [])
        waiting = {entry.get("id") for entry in inbox}
        reclaimed = []
        for receipt in sorted(receipts.values(), key=lambda value: value.get("queued_at") or ""):
            if receipt.get("session_id") != session_id or receipt.get("status") not in {"taken", "posted"}:
                continue
            if receipt.get("id") in waiting or not receipt.get("text"):
                continue
            receipt.update({"status": "queued", "taken_at": None, "carried_from": session_id})
            reclaimed.append({"id": receipt["id"], "session_id": session_id, "text": receipt["text"],
                              "source": receipt.get("source") or "controller",
                              "queued_at": receipt.get("queued_at") or now(), "carried_from": session_id})
        inbox[:0] = reclaimed
        return len(reclaimed)


def mark_posted(root: Path, session_id: str, instruction_id: str, entry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Plumbing Stage 3: the message is in the CLI's own queue, its arrival not yet seen.

    Never typed or posted again by this session; acknowledged when its
    receipt appears; carried to a continuing session if this one ends first.
    """
    with locked_state(root) as state:
        receipt = state.setdefault("instruction_receipts", {}).get(instruction_id)
        if not receipt:
            raise ValueError("unknown instruction receipt")
        if receipt.get("session_id") != session_id:
            raise ValueError("instruction receipt belongs to another managed session")
        if receipt.get("status") == "taken":
            # The message itself is kept with it, so a continuing session can be
            # given exactly this message under exactly this id.
            receipt.update({"status": "posted", "posted_at": now(),
                            "text": str((entry or {}).get("text") or receipt.get("text") or ""),
                            "source": str((entry or {}).get("source") or receipt.get("source") or "controller")})
        return dict(receipt)


def acknowledge_instruction(root: Path, session_id: str, instruction_id: str) -> dict[str, Any]:
    """Record that the supervisor submitted one queued message to the child PTY."""
    with locked_state(root) as state:
        receipt = state.setdefault("instruction_receipts", {}).get(instruction_id)
        if not receipt:
            raise ValueError("unknown instruction receipt")
        if receipt.get("session_id") != session_id:
            raise ValueError("instruction receipt belongs to another managed session")
        if receipt.get("status") == "delivered":
            return dict(receipt)
        if receipt.get("status") not in {"taken", "posted"}:
            raise ValueError("instruction must be taken by its supervisor before delivery is acknowledged")
        receipt.update({"status": "delivered", "delivered_at": now()})
        _prune_instruction_receipts(state)
        return dict(receipt)


def find_instruction_receipt(root: Path, source: str, marker: str) -> dict[str, Any] | None:
    """The live receipt of an instruction queued with this source whose text carries the marker, if any.

    Used by the board to make a dispatch idempotent across a crash (owner
    defaults, 2026-10-15): a withdrawn or discarded receipt does not count.
    """
    with locked_state(root) as state:
        for receipt in state.setdefault("instruction_receipts", {}).values():
            if (
                receipt.get("source") == source and marker in str(receipt.get("text") or "")
                and receipt.get("status") not in {"withdrawn", "discarded", "failed"}
            ):
                return json.loads(json.dumps(receipt))
    return None


def instruction_receipt(root: Path, instruction_id: str) -> dict[str, Any]:
    """Return the durable transport state for one event-driven instruction."""
    with locked_state(root) as state:
        receipt = state.setdefault("instruction_receipts", {}).get(instruction_id)
        if not receipt:
            raise ValueError("unknown instruction receipt")
        return json.loads(json.dumps(receipt))


def supersede(
    root: Path,
    source_session_id: str,
    replacement_session_id: str,
    replacement_agent_id: str,
    task: str,
) -> dict[str, Any]:
    """Make a predecessor terminal read-only and stop it after durable recovery.

    The board performs the authoritative task transfer first. This operation
    then closes the obsolete transport so late terminal input cannot compete
    with the replacement owner.
    """
    if not source_session_id or not replacement_session_id or source_session_id == replacement_session_id:
        raise ValueError("distinct source and replacement sessions are required")
    with locked_state(root) as state:
        _reconcile(state)
        source = state.get("sessions", {}).get(source_session_id)
        replacement = state.get("sessions", {}).get(replacement_session_id)
        if not source:
            raise ValueError("unknown source session")
        if not replacement or replacement.get("status") not in ACTIVE_STATUSES:
            raise ValueError("replacement session is not active")
        changed_at = now()
        source.update({
            "read_only": True,
            "superseded_by_session_id": replacement_session_id,
            "superseded_by_agent_id": replacement_agent_id,
            "superseded_task": task,
            "superseded_at": changed_at,
            "stop_requested_at": changed_at,
            "reason": f"superseded by {replacement_agent_id}; read-only terminal is stopping",
        })
        state.setdefault("inbox", {}).pop(source_session_id, None)
        if source.get("pid") and _pid_is_alive(source["pid"]):
            os.kill(source["pid"], signal.SIGTERM)
            source["status"] = "stopping"
        else:
            source.update({"status": "stopped", "ended_at": changed_at})
        return dict(source)


def fail_launch(root: Path, session_id: str, reason: str) -> dict[str, Any]:
    with locked_state(root) as state:
        session = state["sessions"].get(session_id)
        if not session:
            raise ValueError("unknown session")
        if session["status"] == "launching":
            # Keep the actionable diagnostic, including sandbox setup commands.
            limit = int(os.environ.get("HARNESS_LAUNCH_ERROR_MAX_CHARS", "4096"))
            session.update({"status": "failed", "ended_at": now(), "reason": reason[:limit]})
        return dict(session)


def stop(root: Path, session_id: str) -> dict[str, Any]:
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session:
            raise ValueError("unknown session")
        if session["status"] not in ACTIVE_STATUSES:
            return dict(session)
        session["auth_epoch"] = int(session.get("auth_epoch", 1)) + 1
        session["stop_requested_at"] = now()
        session["reason"] = "stop requested from control panel"
        if session["pid"] and _pid_is_alive(session["pid"]):
            os.kill(session["pid"], signal.SIGTERM)
            session["status"] = "stopping"
        else:
            session.update({"status": "stopped", "ended_at": now(), "reason": "stopped before terminal session attached"})
        return dict(session)


def pause(root: Path, session_id: str) -> dict[str, Any]:
    """Stop one live terminal while durably distinguishing intentional pause."""
    with locked_state(root) as state:
        _reconcile(state)
        session = state["sessions"].get(session_id)
        if not session:
            raise ValueError("unknown session")
        if session.get("status") == "paused":
            return dict(session)
        if session.get("status") not in ACTIVE_STATUSES:
            return dict(session)
        session["auth_epoch"] = int(session.get("auth_epoch", 1)) + 1
        requested_at = now()
        session["pause_requested_at"] = requested_at
        session["reason"] = "project pause requested; preserving saved session pointer"
        if session.get("pid") and _pid_is_alive(session["pid"]):
            try:
                os.kill(session["pid"], signal.SIGTERM)
            except ProcessLookupError:
                session.update({
                    "status": "paused", "ended_at": requested_at,
                    "reason": "terminal exited while the project pause signal was sent",
                })
            else:
                session["status"] = "pausing"
        else:
            session.update({
                "status": "paused", "ended_at": requested_at,
                "reason": "terminal intentionally paused before attachment",
            })
        return dict(session)


def pause_sessions(
    root: Path, session_ids: list[str], *, timeout: float = 3.0,
) -> list[dict[str, Any]]:
    """Pause sessions together, bounding termination and marking every target."""
    unique = list(dict.fromkeys(str(value) for value in session_ids if str(value)))
    for session_id in unique:
        pause(root, session_id)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        with locked_state(root) as state:
            _reconcile(state)
            if all(
                state["sessions"].get(session_id, {}).get("status") == "paused"
                for session_id in unique
            ):
                return [dict(state["sessions"][session_id]) for session_id in unique]
        time.sleep(0.02)
    with locked_state(root) as state:
        _reconcile(state)
        for session_id in unique:
            session = state["sessions"].get(session_id)
            if not session or session.get("status") == "paused":
                continue
            pid = session.get("pid")
            if pid and _pid_is_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            session.update({
                "status": "paused", "ended_at": now(),
                "reason": "terminal force-stopped after the bounded pause timeout",
            })
        return [dict(state["sessions"][session_id]) for session_id in unique]


PAUSE_NOTICE_SOURCE = "project-pause"


def _withdraw_pause_notices(state: dict[str, Any], session_id: str) -> None:
    """A resumed terminal is never told the project is pausing.

    Stage 3 review r4 audit: the pause notice is often still in flight when the
    pause stops the terminal, and the relaunch takes back what its previous run
    held - so the resumed agent was told "Project pause requested ... the
    terminal will stop" after the owner had resumed. Undelivered pause notices
    end with the pause.
    """
    inbox = state.setdefault("inbox", {})
    withdrawn = set()
    for receipt in state.setdefault("instruction_receipts", {}).values():
        if (receipt.get("session_id") == session_id and receipt.get("source") == PAUSE_NOTICE_SOURCE
                and receipt.get("status") in {"queued", "taken", "posted"}):
            receipt.update({"status": "withdrawn", "withdrawn_at": now(), "reason": "the project was resumed"})
            withdrawn.add(receipt.get("id"))
    if withdrawn and session_id in inbox:
        inbox[session_id] = [entry for entry in inbox[session_id] if entry.get("id") not in withdrawn]
        if not inbox[session_id]:
            inbox.pop(session_id, None)


def prepare_resume_sessions(root: Path, session_ids: list[str]) -> list[dict[str, Any]]:
    """Re-adopt surviving PIDs and stage each dead terminal for one relaunch."""
    unique = list(dict.fromkeys(str(value) for value in session_ids if str(value)))
    prepared: list[dict[str, Any]] = []
    with locked_state(root) as state:
        _reconcile(state)
        # A replacement reviewer continues the conversation of the one it
        # replaced; relaunching both would put two terminals on one
        # conversation (P2e).
        sessions_now = state.get("sessions", {})
        successors = {
            str(entry.get("continues_session"))
            for entry in sessions_now.values()
            if entry.get("continues_session") and entry.get("id") in unique
            and entry.get("status") != "failed"
            and entry.get("id") != entry.get("continues_session")
            and (sessions_now.get(str(entry.get("continues_session"))) or {}).get("kind") == entry.get("kind")
            and (sessions_now.get(str(entry.get("continues_session"))) or {}).get("provider") == entry.get("provider")
        }
        for session_id in unique:
            session = state.get("sessions", {}).get(session_id)
            if not session:
                prepared.append({"id": session_id, "action": "missing"})
                continue
            if session_id in successors:
                prepared.append({**dict(session), "action": "superseded_by_continuation"})
                continue
            _withdraw_pause_notices(state, session_id)
            pid = session.get("pid")
            if pid and _pid_is_alive(pid):
                session.pop("resume_offer", None)
                session.update({
                    "status": "running", "ended_at": None,
                    "pause_requested_at": None, "resume_launch_requested_at": None,
                    "reason": "surviving terminal re-adopted during project resume",
                })
                prepared.append({**dict(session), "action": "re_adopted"})
                continue
            if (
                session.get("status") == "launching"
                and session.get("resume_launch_requested_at")
            ):
                prepared.append({**dict(session), "action": "awaiting_attachment"})
                continue
            was_paused = bool(session.get("pause_requested_at")) or session.get("status") == "paused"
            session.update({
                "status": "launching", "pid": None, "ended_at": None,
                "auth_epoch": int(session.get("auth_epoch", 1)) + 1,
                "pause_requested_at": None, "stop_requested_at": None,
                "resume_launch_requested_at": None,
                # Remembered until the CLI is back: the relaunch tells the
                # agent the owner paused and resumed, and to continue its task.
                "resumed_after_pause_at": now() if was_paused else session.get("resumed_after_pause_at"),
                # The offer is the owner-consent boundary: only sessions the
                # resume selected for relaunch may be launched by the owner's
                # button; a retired terminal is never offered.
                "resume_offer": "relaunch",
                # Roles are launched one after another, and a Claude role's sign-in check can take minutes: every staged role
                # waits that long for its turn; the usual 30 seconds start when its own launch is claimed.
                "launch_deadline": (
                    datetime.now(timezone.utc) + timedelta(seconds=claude_launch_budget_seconds())
                ).isoformat(),
                "reason": "terminal ready to relaunch from its saved next action",
            })
            prepared.append({**dict(session), "action": "relaunch"})
    return prepared


def restage_resume_launch(root: Path, session_id: str) -> dict[str, Any]:
    """A relaunch the resume attempted did not open a Terminal: offer it again.

    The board's relaunch button is the fallback for exactly this session, so
    the request claim is released and the offer restored, with a fresh
    attachment deadline.
    """
    with locked_state(root) as state:
        session = state["sessions"].get(session_id)
        if not session:
            raise ValueError("unknown session")
        if session.get("status") != "launching" or session.get("pid"):
            return dict(session)
        session.update({
            "resume_launch_requested_at": None, "resume_offer": "relaunch",
            "launch_deadline": (datetime.now(timezone.utc) + timedelta(seconds=LAUNCH_ATTACH_SECONDS)).isoformat(),
            "reason": "relaunch attempted by the resume did not open a Terminal; ready to relaunch from the board",
        })
        return dict(session)


def mark_resume_launch_requested(root: Path, session_id: str) -> dict[str, Any]:
    """Claim one staged relaunch before opening Terminal, preventing duplicates."""
    with locked_state(root) as state:
        staged = state.get("sessions", {}).get(session_id)
        if (staged and staged.get("status") == "launching" and not staged.get("pid")
                and staged.get("resume_offer") == "relaunch" and not staged.get("resume_launch_requested_at")):
            # The attachment clock starts at the actual launch, not at staging.
            staged["launch_deadline"] = (
                datetime.now(timezone.utc) + timedelta(seconds=RESUME_ATTACH_SECONDS)).isoformat()
        _reconcile(state)
        session = state.get("sessions", {}).get(session_id)
        if not session or session.get("status") != "launching":
            raise ValueError("resume terminal is not staged for launch")
        if session.get("resume_launch_requested_at"):
            return dict(session)
        if session.get("resume_offer") != "relaunch":
            raise ValueError("this terminal was not offered for relaunch")
        session["resume_launch_requested_at"] = now()
        session["launch_deadline"] = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        session["reason"] = "resume terminal launch requested; waiting for attachment"
        return dict(session)


def queued_instructions(root: Path) -> dict[str, Any]:
    """Read-only copy of undelivered controller messages, for the self-restart (backlog #7)."""
    with locked_state(root) as state:
        _reconcile(state)
        return json.loads(json.dumps({
            "inbox": state.get("inbox") or {},
            "receipts": state.get("instruction_receipts") or {},
        }))


def snapshot(root: Path) -> dict[str, Any]:
    with locked_state(root) as state:
        _reconcile(state)
        sessions = sorted(state["sessions"].values(), key=lambda value: value["created_at"], reverse=True)
        active_counts = {
            kind: sum(item["kind"] == kind and item["status"] in ACTIVE_STATUSES for item in sessions)
            for kind in KINDS
        }
        result = {
            "sessions": json.loads(json.dumps(sessions)),
            "active_counts": active_counts,
            "limits": dict(MAX_ACTIVE_SESSIONS),
        }
    from harness import attention
    for item in result["sessions"]:
        if item.get("status") in ACTIVE_STATUSES and attention.is_limit_reason(item.get("attention_reason")):
            # One wording, in Python: the dashboard shows this line as is (what is observed, the time, the way out).
            item["limit_notice"] = attention.limit_sentence(LIMIT_ROLE_LABELS.get(KIND_TO_ROLE.get(str(item.get("kind", "")), ""), "agent"), item)
    result["agent_settings"] = agent_settings(root)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harness local CLI-session lifecycle registry")
    add_context_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)
    attach_parser = subparsers.add_parser("attach")
    attach_parser.add_argument("--id", required=True)
    attach_parser.add_argument("--pid", required=True, type=int)
    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("--kind", required=True)
    resolve_parser.add_argument("--session-id", required=True)
    plan_parser = subparsers.add_parser("plan-cli-launch")
    plan_parser.add_argument("--session-id", required=True)
    plan_parser.add_argument("--provider", required=True)
    record_parser = subparsers.add_parser("record-cli-session")
    record_parser.add_argument("--session-id", required=True)
    record_parser.add_argument("--cli-session-id", required=True)
    record_parser.add_argument("--provider", required=True)
    launch_parser = subparsers.add_parser("note-cli-launch")
    launch_parser.add_argument("--session-id", required=True)
    launch_parser.add_argument("--resumed", action="store_true")
    exec_marker_parser = subparsers.add_parser("note-cli-exec")
    exec_marker_parser.add_argument("--session-id", required=True)
    failed_parser = subparsers.add_parser("launch-failed")
    failed_parser.add_argument("--session-id", required=True)
    failed_parser.add_argument("--pid", required=True, type=int)
    failed_parser.add_argument("--code", required=True, type=int)
    failed_parser.add_argument("--step", default="")
    failed_parser.add_argument("--signal", default="")
    failed_parser.add_argument("--diagnostic", default="")
    conversation_parser = subparsers.add_parser(
        "conversation", help="print the readable conversation of one managed terminal (any agent may read another's)",
    )
    conversation_parser.add_argument("--session-id", required=True)
    conversation_parser.add_argument("--raw", action="store_true", help="the raw terminal record instead")
    args = parser.parse_args(argv)
    if args.command == "attach":
        result = attach(context_from_args(args), args.id, args.pid)
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "resolve":
        root = context_from_args(args)
        value = session_launch_settings(root, args.session_id, args.kind)
        print(json.dumps(value, sort_keys=True))
        return 0
    if args.command == "plan-cli-launch":
        from harness import conversation
        print(json.dumps(conversation.plan_cli_launch(context_from_args(args), args.session_id, args.provider), sort_keys=True))
        return 0
    if args.command == "record-cli-session":
        print(json.dumps(record_cli_session(context_from_args(args), args.session_id, args.cli_session_id, args.provider), sort_keys=True))
        return 0
    if args.command == "note-cli-launch":
        print(json.dumps(note_cli_launch(context_from_args(args), args.session_id, args.resumed), sort_keys=True))
        return 0
    if args.command == "note-cli-exec":
        print(json.dumps(note_cli_exec(context_from_args(args), args.session_id), sort_keys=True))
        return 0
    if args.command == "launch-failed":
        # The shell is waiting for this report while it closes: never let a stalled lock or disk hold it.
        signal.alarm(8)
        print(json.dumps(record_launch_failure(
            context_from_args(args), args.session_id, args.pid, args.code, args.step, args.diagnostic, args.signal),
            sort_keys=True))
        return 0
    if args.command == "conversation":
        from harness import conversation
        text = conversation.conversation_view(context_from_args(args), args.session_id, raw=args.raw)
        if text is None:
            print(f"no conversation is recorded for managed session {args.session_id}", file=sys.stderr)
            return 1
        sys.stdout.write(text)
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Manager-owned provider, model, effort, and connectivity settings."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from harness import child_process, contract_check, control, cost_center, cto_wakes, difficulty_note, review_cost, revert_proof
from harness import platform_support

SETTINGS_VERSION = 1
SETTINGS_FILENAME = "settings.json"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
PROJECT_CHAT_MODEL_ENV = "HARNESS_PROJECT_CHAT_MODEL"
PROJECT_CHAT_MODEL = "gpt-5.6-luna"
SECRETS_DIRECTORY = "secrets"
OPENAI_API_KEY_FILENAME = "openai_api_key"
OPENAI_PROVIDER = "openai"
SECRET_MAX_BYTES = 512
CONNECTION_TEST_TIMEOUT_SECONDS = 90.0
CONNECTION_TEST_PROMPT = "Reply with the single word: ready"
# Signals a CLI emits when the account or the CLI build cannot run the model.
# Signals that the ACCOUNT is out of money/quota, as opposed to a bad model.
CREDIT_EXHAUSTED_SIGNALS = (
    "insufficient credit",
    "insufficient_quota",
    "exceeded your current quota",
    "billing hard limit",
    "credit balance is too low",
    "payment required",
    "usage limit",
    "rate_limit_exceeded",
    "upgrade your plan",
    "status\":402",
    "http 402",
)
MODEL_UNAVAILABLE_SIGNALS = (
    "requires a newer version",
    "model not found",
    "unknown model",
    "does not have access",
    "invalid_request_error",
    "unsupported model",
    "model_not_found",
)
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,100}$")
# The ordering rationale, and the directories themselves, now live in
# harness/platform_support: they are platform knowledge. The name and the
# value are unchanged, so every caller and every recorded PATH string is
# byte-identical.
PROVIDER_SEARCH_DIRECTORIES = platform_support.discovery().owner_tool_directories()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Agent plumbing modernization (docs/specs/PLUMBING_MODERNIZATION.md §8). Every
# stage is OFF by default; with all of them off, behaviour is today's exactly.
PLUMBING_STAGES = (
    "stage1_hooks", "stage2_board_mcp", "stage3_codex_app_server",
    "stage3_claude_socket_delivery", "stage3_gemini_delivery", "stage4_system_layer_directives", "stage5_structured_verdicts",
)
# A stage needs these stages on as well (spec §5, enforced, never advisory).
PLUMBING_DEPENDENCIES = {
    "stage3_codex_app_server": ("stage1_hooks",),
    "stage3_claude_socket_delivery": ("stage1_hooks",),
    "stage3_gemini_delivery": ("stage1_hooks",),
    "stage5_structured_verdicts": ("stage2_board_mcp",),
}
PLUMBING_LIMITS = {
    "hook_gate_timeout_seconds": (5, 1, 60),
    "hook_gate_latency_budget_ms": (250, 10, 5000),
    "app_server_start_timeout_seconds": (20, 1, 300),
    "delivery_receipt_timeout_seconds": (30, 1, 600),
    # How long a message the CLI has queued may wait while the session is busy
    # (Claude reads its inbox between tool calls and at the end of a turn).
    "delivery_busy_wait_seconds": (900, 30, 7200),
    # How long the supervisor looks for the conversation a Gemini session started (the CLI creates it only
    # when the first turn runs, which can wait on a sign-in or trust answer).
    "gemini_conversation_wait_seconds": (1800, 60, 7200),
    # How long the Reviewer's early look at the requirements may take before the owner's Go ahead is plain (the owner can always go ahead sooner).
    # The largest change the Reviewer reads in full (2026-10-25): above either limit the brief is reduced to the git summary plus the
    # largest changes, and says so. Information only: nothing is ever cut off.
    "review_input_max_files": (review_cost.MAX_FILES_DEFAULT, review_cost.MAX_FILES_LOW, review_cost.MAX_FILES_HIGH),
    "review_input_max_bytes": (review_cost.MAX_BYTES_DEFAULT, review_cost.MAX_BYTES_LOW, review_cost.MAX_BYTES_HIGH),
    "review_input_measure_seconds": (review_cost.MEASURE_SECONDS_DEFAULT, review_cost.MEASURE_SECONDS_LOW, review_cost.MEASURE_SECONDS_HIGH),
    # The longest difficulty note kept (2026-10-26): longer text is trimmed, never refused.
    "difficulty_note_max_chars": (difficulty_note.MAX_CHARS_DEFAULT, difficulty_note.MAX_CHARS_LOW, difficulty_note.MAX_CHARS_HIGH),
    "contract_check_wait_seconds": (contract_check.WAIT_DEFAULT_SECONDS, contract_check.WAIT_MIN_SECONDS, contract_check.WAIT_MAX_SECONDS),
    # Revert proof (2026-10-26): how many changed files the board puts back one at a time at final review, and the longest it spends doing so.
    # Information only: files over the limit are listed as not checked.
    "revert_proof_max_files": (revert_proof.MAX_FILES_DEFAULT, revert_proof.MAX_FILES_LOW, revert_proof.MAX_FILES_HIGH),
    "revert_proof_budget_seconds": (revert_proof.BUDGET_DEFAULT, revert_proof.BUDGET_LOW, revert_proof.BUDGET_HIGH),
    # The CTO's slow check-in (2026-10-26): the board wakes the CTO for a reason, plus once per this many seconds while a task is live.
    "cto_heartbeat_seconds": (cto_wakes.HEARTBEAT_DEFAULT_SECONDS, cto_wakes.HEARTBEAT_LOW_SECONDS, cto_wakes.HEARTBEAT_HIGH_SECONDS),
    "cto_no_progress_seconds": (cto_wakes.NO_PROGRESS_DEFAULT_SECONDS, cto_wakes.NO_PROGRESS_LOW_SECONDS, cto_wakes.NO_PROGRESS_HIGH_SECONDS),
    # Cost center (2026-10-27): the most a single background check reads from the programs' session files, and the longest it takes.
    # Information only: whatever is not reached is read on the next check and labelled partial meanwhile.
    "cost_collect_max_bytes": (cost_center.MAX_BYTES_DEFAULT, cost_center.MAX_BYTES_LOW, cost_center.MAX_BYTES_HIGH),
    "cost_collect_seconds": (cost_center.SECONDS_DEFAULT, cost_center.SECONDS_LOW, cost_center.SECONDS_HIGH),
}
# Plumbing text fields (default, longest allowed). cost_rate_card_path: the owner's own price list (see harness/rate_card.json for the shape).
PLUMBING_TEXT = {cost_center.CARD_PATH_FIELD: ("", 1024)}


# 2026-10-15: a CTO question to the owner carries a default and a deadline
# (board.classify_owner_question); the deadline and the phrase lists are the
# owner's settings, defaults in harness/board.py.
OWNER_QUESTION_LISTS = ("question_markers", "authority_patterns", "what_markers", "owner_only_markers")


def default_owner_questions() -> dict[str, Any]:
    from harness import board
    return {
        "default_minutes": board.OWNER_QUESTION_DEFAULT_MINUTES,
        "question_markers": list(board.OWNER_QUESTION_LEADS),
        "authority_patterns": list(board.OWNER_QUESTION_AUTHORITY),
        "what_markers": list(board.OWNER_QUESTION_WHAT_MARKERS),
        "owner_only_markers": list(board.OWNER_QUESTION_OWNER_ONLY),
    }


def _validated_owner_questions(value: Any) -> dict[str, Any]:
    from harness import board
    if value is None:
        return default_owner_questions()
    if not isinstance(value, dict):
        raise ValueError("owner_questions settings must be an object")
    result = default_owner_questions()
    unknown = set(value) - set(result)
    if unknown:
        raise ValueError(f"unknown owner_questions settings: {', '.join(sorted(unknown))}")
    minutes = value.get("default_minutes", result["default_minutes"])
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= board.OWNER_QUESTION_MAX_MINUTES:
        raise ValueError(f"owner_questions default_minutes must be a whole number from 1 to {board.OWNER_QUESTION_MAX_MINUTES}")
    result["default_minutes"] = minutes
    for name in OWNER_QUESTION_LISTS:
        phrases = value.get(name, result[name])
        if not isinstance(phrases, list) or not phrases or any(not isinstance(item, str) or not item.strip() for item in phrases):
            raise ValueError(f"owner_questions {name} must be a non-empty list of phrases")
        if name == "authority_patterns":
            # regular expressions (the other lists are plain lowercase phrases); a bad one is refused here, never at a board command
            for item in phrases:
                try:
                    re.compile(item)
                except re.error as error:
                    raise ValueError(f"owner_questions authority_patterns entry is not a valid regular expression: {item!r} ({error})") from error
            result[name] = [" ".join(item.split()) for item in phrases]
            continue
        result[name] = [" ".join(item.split()).lower() for item in phrases]
    return result


# Proving mode (the owner's test copy only): a switched-on stage may run while
# its LIVE capabilities are still UNPROVEN, so the visible run that proves them
# can happen at all. A capability proven false, and every static one, still
# blocks; the session log says "PLUMBING PROVING" and names what is unproven.
PROVING_FLAG = "prove_live_items"


def default_plumbing() -> dict[str, Any]:
    value: dict[str, Any] = {f"{stage}_enabled": False for stage in PLUMBING_STAGES}
    value[PROVING_FLAG] = False
    value.update({name: default for name, (default, _low, _high) in PLUMBING_LIMITS.items()})
    value.update({name: default for name, (default, _longest) in PLUMBING_TEXT.items()})
    return value


def _validated_plumbing(value: Any) -> dict[str, Any]:
    if value is None:
        return default_plumbing()
    if not isinstance(value, dict):
        raise ValueError("plumbing settings must be an object")
    unknown = set(value) - set(default_plumbing())
    if unknown:
        raise ValueError(f"unknown plumbing settings: {', '.join(sorted(unknown))}")
    result = default_plumbing()
    for stage in PLUMBING_STAGES:
        flag = value.get(f"{stage}_enabled", False)
        if not isinstance(flag, bool):
            raise ValueError(f"plumbing {stage}_enabled must be true or false")
        result[f"{stage}_enabled"] = flag
    proving = value.get(PROVING_FLAG, False)
    if not isinstance(proving, bool):
        raise ValueError(f"plumbing {PROVING_FLAG} must be true or false")
    result[PROVING_FLAG] = proving
    for name, (default, low, high) in PLUMBING_LIMITS.items():
        number = value.get(name, default)
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise ValueError(f"plumbing {name} must be a whole number from {low} to {high}")
        result[name] = number
    for name, (default, longest) in PLUMBING_TEXT.items():
        text = value.get(name, default)
        if not isinstance(text, str) or len(text) > longest or "\0" in text:
            raise ValueError(f"plumbing {name} must be a short text")
        result[name] = text.strip()
    return result


def plumbing_status(home: Path, *, settings: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Each stage's switch after its dependencies: on only when it AND what it needs are on.

    A refused switch behaves as off and carries the plain reason the Settings
    page and the session log show. Capabilities are checked separately
    (`cli_capabilities.stage_status`), per CLI.
    """
    plumbing = _validated_plumbing((settings if settings is not None else load(home)).get("plumbing"))
    status: dict[str, dict[str, Any]] = {}
    for stage in PLUMBING_STAGES:
        if not plumbing[f"{stage}_enabled"]:
            status[stage] = {"enabled": False, "reason": f"{stage} is switched off"}
            continue
        off = [needed for needed in PLUMBING_DEPENDENCIES.get(stage, ()) if not plumbing[f"{needed}_enabled"]]
        status[stage] = ({"enabled": False, "reason": f"{stage} needs {', '.join(off)} switched on first"}
                         if off else {"enabled": True, "reason": ""})
    return status


def _default() -> dict[str, Any]:
    return {
        "version": SETTINGS_VERSION,
        "agent_settings": control.default_agent_settings(),
        "recovery_models": control.validated_recovery_models(None),
        "reviewer_retry_models": control.validated_recovery_models(None),
        "connectivity": {},
        "plumbing": default_plumbing(),
        "owner_questions": default_owner_questions(),
        "notices": [],
    }


def _tolerant_recovery_models(value: Any) -> dict[str, str]:
    try:
        return control.validated_recovery_models(value)
    except ValueError:
        return control.validated_recovery_models(None)


def _validate_document(value: Any, repairs: list[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or int(value.get("version", 0)) != SETTINGS_VERSION:
        raise ValueError(f"global settings must use version {SETTINGS_VERSION}")
    connectivity = value.get("connectivity", {})
    if not isinstance(connectivity, dict):
        raise ValueError("global connectivity results must be an object")
    found: list[str] = []
    agent_settings = control._validated_agent_settings(value.get("agent_settings"), notices=found, repair=True)
    # A repair is never silent and never replaces the owner's other choices with defaults: the notices stay in the
    # document, and Settings shows them, until the owner's next explicit save.
    notices = [str(item) for item in (value.get("notices") or []) if isinstance(item, str)]
    for item in found:
        if item not in notices:
            notices.append(item)
    if repairs is not None:
        repairs.extend(found)
    return {
        "version": SETTINGS_VERSION,
        "agent_settings": agent_settings,
        # "If repairs keep failing, retry once with": blank = the vendor's default (control.STRONGEST_MODELS). Documents saved before
        # 2026-10-25 have no field and load unchanged. An unusable value falls back to the default rather than refusing the document.
        "recovery_models": _tolerant_recovery_models(value.get("recovery_models")),
        # "If the review can't be completed, retry once with" (2026-10-25): same shape, same default, same tolerance.
        "reviewer_retry_models": _tolerant_recovery_models(value.get("reviewer_retry_models")),
        "connectivity": connectivity,
        "plumbing": _validated_plumbing(value.get("plumbing")),
        "owner_questions": _validated_owner_questions(value.get("owner_questions")),
        "notices": notices,
    }


def _path(home: Path) -> Path:
    return Path(home) / SETTINGS_FILENAME


@contextmanager
def _locked(home: Path) -> Iterator[None]:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    with (home / ".settings.lock").open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write(home: Path, value: dict[str, Any]) -> None:
    path = _path(home)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _preserve_broken(path: Path) -> Path:
    """Copy an unreadable owner document aside before an explicit repair."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = path.with_name(f"{path.name}.broken-{stamp}-{uuid.uuid4().hex[:8]}")
    shutil.copy2(path, destination)
    return destination


def _remove_legacy_project_copies(data_roots: list[Path]) -> None:
    """Remove migrated role settings from project-local session documents.

    The global document is written and validated before this runs.  Each
    project control lock is then used so a live session mutation cannot race
    the one-time cleanup.  Malformed session documents are left untouched for
    their own recovery path; they are not usable configuration copies.
    """
    for data_root in data_roots:
        control_dir = Path(data_root) / "control"
        path = control_dir / "sessions.json"
        if not path.is_file():
            continue
        control_dir.mkdir(parents=True, exist_ok=True)
        with (control_dir / ".lock").open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(value, dict) or "agent_settings" not in value:
                    continue
                value.pop("agent_settings", None)
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                temporary.write_text(
                    json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                )
                try:
                    os.replace(temporary, path)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def exists(home: Path) -> bool:
    """True once the global store has been created (its first creation may migrate one legacy project copy)."""
    return _path(home).exists()


def initialize(home: Path, data_roots: list[Path] | None = None) -> dict[str, Any]:
    """Create the global store, migrating the first valid legacy root once."""
    roots = [Path(root) for root in data_roots or []]
    with _locked(home):
        path = _path(home)
        if path.exists():
            repairs: list[str] = []
            try:
                value = _validate_document(json.loads(path.read_text(encoding="utf-8")), repairs)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                return {**_default(), "error": f"saved global settings need attention: {error}"}
            if repairs:
                _write(home, value)          # what launches must match what Settings shows
            _remove_legacy_project_copies(roots)
            return value
        value = _default()
        for data_root in roots:
            legacy = Path(data_root) / "control" / "sessions.json"
            try:
                old = json.loads(legacy.read_text(encoding="utf-8"))
                value["agent_settings"] = control._validated_agent_settings(old.get("agent_settings"))
                break
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        _write(home, value)
        _remove_legacy_project_copies(roots)
        return json.loads(json.dumps(value))


def load(home: Path) -> dict[str, Any]:
    """Read the global settings WITHOUT writing anything.

    Until 2026-10-17 `load` was `initialize`: every read rewrote the file when a stored value needed repair (an older
    Gemini pair) and created it when missing — and readers are everywhere (Mission Control's refresh poll, the launch
    endpoint, the launch script, the plumbing checks). A per-launch model choice promises that a launch never changes
    Settings, so reading must not either. Repairs are applied IN MEMORY (every reader sees the values the Settings page
    shows); a missing file reads as the defaults; an unreadable file reads as the defaults plus `error`, exactly as
    `initialize` reports it. `initialize` keeps repair-and-create for manager start, the Settings page and the
    connection test (task 2026-10-17_launch-model-choice §3.4).
    """
    path = _path(home)
    if not path.exists():
        return json.loads(json.dumps(_default()))
    try:
        return _validate_document(json.loads(path.read_text(encoding="utf-8")), [])
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return {**_default(), "error": f"saved global settings need attention: {error}"}


def chat_settings(
    home: Path, *, source_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the API-only chat configuration, independent of agent CLIs."""
    load(home)
    environment = os.environ if source_environment is None else source_environment
    model = str(environment.get(PROJECT_CHAT_MODEL_ENV, PROJECT_CHAT_MODEL)).strip()
    if not MODEL_NAME.fullmatch(model):
        raise ValueError("project chat model is invalid")
    return {
        "provider": "openai",
        "model": model,
        "effort": "low",
    }


def _validated_openai_api_key(value: str) -> str:
    key = str(value or "").strip()
    encoded = key.encode("utf-8")
    if (
        not key.startswith("sk-")
        or not 20 <= len(encoded) <= SECRET_MAX_BYTES
        or any(character.isspace() or ord(character) < 33 for character in key)
    ):
        raise ValueError("OpenAI API key is invalid")
    return key


class OpenAIKeyMissing(ValueError):
    """No key is configured at all.

    Distinct from every other failure on the read path: a key that exists but
    cannot be trusted - wrong mode, wrong owner, a symlink, a writable parent
    directory, unreadable bytes - must never be treated as "no key" and quietly
    replaced by whatever the process environment happens to hold.
    """


def validate_openai_api_key(value: str) -> str:
    """Reject an unusable key before any network call is attempted."""
    return _validated_openai_api_key(value)


def openai_api_key_path(home: Path) -> Path:
    return Path(home) / SECRETS_DIRECTORY / OPENAI_API_KEY_FILENAME


def store_openai_api_key(home: Path, value: str) -> dict[str, Any]:
    """Atomically store a manager-owned API key without putting it in settings."""
    key = _validated_openai_api_key(value)
    directory = Path(home) / SECRETS_DIRECTORY
    if directory.is_symlink():
        raise ValueError("Harness secrets directory must not be a symlink")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    path = openai_api_key_path(home)
    if path.is_symlink():
        raise ValueError("OpenAI API key file must not be a symlink")
    temporary = directory / f".{OPENAI_API_KEY_FILENAME}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key.encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"configured": True, "source": "manager_secret"}


def _stored_openai_api_key(home: Path) -> str:
    """Read the manager-owned secret file, or raise a plain-language reason."""
    path = openai_api_key_path(home)
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise OpenAIKeyMissing("OpenAI API key is not configured for project chat") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("OpenAI API key file must be a regular file")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("OpenAI API key file must be owned by this user and mode 0600")
    if metadata.st_size < 20 or metadata.st_size > SECRET_MAX_BYTES + 1:
        raise ValueError("OpenAI API key file has an invalid size")
    directory = path.parent.lstat()
    if (
        stat.S_ISLNK(directory.st_mode)
        or directory.st_uid != os.geteuid()
        or stat.S_IMODE(directory.st_mode) & 0o022
    ):
        raise ValueError(
            "OpenAI API key directory must be owned by this user and writable only by you"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError("OpenAI API key file could not be read") from error
    return _validated_openai_api_key(text)


def openai_api_key(
    home: Path, *, source_environment: dict[str, str] | None = None,
) -> str:
    """Return the key project chat must use.

    The key the owner saved in Settings is the key that is used.  A stray
    ``OPENAI_API_KEY`` in the launchd or shell environment may never silently
    replace it; the environment is read only when Settings holds no key, so an
    existing environment-only deployment keeps working until the owner saves
    one.
    """
    try:
        return _stored_openai_api_key(home)
    except OpenAIKeyMissing:
        environment = os.environ if source_environment is None else source_environment
        if environment.get(OPENAI_API_KEY_ENV):
            return _validated_openai_api_key(str(environment[OPENAI_API_KEY_ENV]))
        raise


def openai_key_fingerprint(key: str) -> str:
    """A non-reversible identifier for one key, safe to write to settings."""
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:32]


def remove_openai_api_key(home: Path) -> dict[str, Any]:
    """Delete the stored key and the connection result that described it."""
    path = openai_api_key_path(home)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    _record_connectivity(home, OPENAI_PROVIDER, {})
    return openai_status(home)


def record_openai_connectivity(home: Path, result: dict[str, Any]) -> bool:
    """Store one OpenAI connection result beside the other provider results."""
    return _record_connectivity(home, OPENAI_PROVIDER, dict(result))


def openai_status(
    home: Path, *, source_environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Describe the key for the UI.  Key bytes never leave this function.

    ``connected`` is true only when a verification succeeded for the key that
    is in force right now: the recorded fingerprint must match.  Replacing the
    key therefore drops the connection state instead of inheriting the previous
    key's verdict.
    """
    status: dict[str, Any] = {
        "configured": False, "source": "", "masked": "", "connected": False,
        "unusable": False, "verified_at": "", "model": "", "message": "",
    }
    # One read decides both the key and where it came from.  Reading twice
    # would let a permission change between the two reads throw out of a page
    # request instead of being reported as an unusable key.
    try:
        key, source = _stored_openai_api_key(home), "manager_secret"
    except OpenAIKeyMissing:
        environment = os.environ if source_environment is None else source_environment
        raw = str(environment.get(OPENAI_API_KEY_ENV, "")).strip()
        if not raw:
            status["message"] = "OpenAI API key is not configured for project chat"
            return status
        try:
            key, source = _validated_openai_api_key(raw), "environment"
        except ValueError as error:
            status["message"] = f"{error} It was read from this computer's environment."
            status["unusable"] = True
            return status
    except ValueError as error:
        # A key is present and cannot be trusted.  Chat stays off and the owner
        # is told exactly what is wrong with the file rather than being told
        # nothing is configured.
        status["message"] = str(error)
        status["unusable"] = True
        return status
    status["configured"] = True
    status["source"] = source
    status["masked"] = f"sk-…{key[-4:]}"
    recorded = load(home)["connectivity"].get(OPENAI_PROVIDER) or {}
    if (
        isinstance(recorded, dict)
        and recorded.get("ok")
        and recorded.get("key_fingerprint") == openai_key_fingerprint(key)
    ):
        status["connected"] = True
        status["verified_at"] = str(recorded.get("tested_at", ""))
        status["model"] = str(recorded.get("model", ""))
        status["message"] = str(recorded.get("message", ""))
    else:
        status["message"] = str(recorded.get("message", "")) if isinstance(recorded, dict) else ""
        if not status["message"]:
            status["message"] = "This key has not been checked against OpenAI yet."
    return status


INSTALLATION_ID_FILENAME = "installation_id"


def installation_id(home: Path) -> str:
    """A stable, non-personal identifier for this installation.

    Written once and reused; it lets a build-attribution stamp tie a produced
    project back to one installation without carrying any personal data.
    """
    home = Path(home)
    path = home / INSTALLATION_ID_FILENAME
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
            return value
    except OSError:
        pass
    value = uuid.uuid4().hex
    home.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(value + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return value


def chat_availability(home: Path | None) -> dict[str, Any]:
    """Whether project chat may run, and the owner-facing reason when it may not.

    One rule, read by the board page, the board API, and the request path, so
    the greyed composer and the refused request can never disagree.
    """
    if home is None:
        return {"available": False, "reason": "Project chat is not configured for this project.", "masked": ""}
    status = openai_status(Path(home))
    if status["connected"]:
        return {"available": True, "reason": "", "masked": status["masked"]}
    if status["unusable"]:
        return {
            "available": False,
            "reason": f"{status['message']} Open Settings and save the key again to switch chat on.",
            "masked": "",
        }
    if not status["configured"]:
        return {
            "available": False,
            "reason": "Project chat needs your OpenAI API key. Add it in Settings to switch chat on.",
            "masked": "",
        }
    return {
        "available": False,
        "reason": "Your OpenAI API key has not connected to OpenAI. Open Settings and check the key to switch chat on.",
        "masked": status["masked"],
    }


def provider_executable(
    provider: str, *, source_environment: dict[str, str] | None = None,
    extra_directories: tuple[str, ...] = (),
) -> str:
    """Resolve a configured CLI without depending on an interactive-shell PATH.

    macOS launchd intentionally supplies a minimal PATH. Homebrew's documented
    executable locations are therefore appended to, never substituted for, the
    inherited path. An explicit provider binary environment variable retains
    precedence and must resolve to an executable file.
    """
    provider = str(provider).strip().lower()
    if provider not in control.PROVIDERS:
        return ""
    environment = os.environ if source_environment is None else source_environment
    configured = str(environment.get(control.PROVIDERS[provider]["binary_env"], control.provider_executable_name(provider)))
    if os.path.isabs(configured):
        path = Path(configured)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else ""
    inherited = str(environment.get("PATH", ""))
    directories = []
    for value in (*inherited.split(os.pathsep), *extra_directories, *PROVIDER_SEARCH_DIRECTORIES):
        if value and value not in directories:
            directories.append(value)
    candidates: list[str] = []
    seen: set[str] = set()
    for directory in directories:
        found = shutil.which(configured, path=directory)
        if found and os.path.realpath(found) not in seen:
            seen.add(os.path.realpath(found))
            candidates.append(found)
    if len(candidates) <= 1:
        return candidates[0] if candidates else ""
    # 2026-09-26: a months-old Homebrew `claude` sat before the current one on
    # PATH and the agents ran it. With several copies the newest version wins;
    # PATH order only breaks a tie. `--version` runs only in this ambiguous case.
    return max(candidates, key=lambda item: (_version_key(cli_version(item)), -candidates.index(item)))


CLI_VERSION_PROBE_SECONDS = 10
_CLI_VERSIONS: dict[tuple[str, float], str] = {}


def _version_key(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".")) if text else (-1,)


def cli_version(executable: str) -> str:
    """`<executable> --version` as "x.y.z", or "" when it cannot be read in time.

    Bounded: a CLI that hangs (one `claude auth status` hung for hours on
    2026-09-26) can never stall a launch or a Settings check.
    """
    try:
        real = os.path.realpath(executable)
        key = (real, os.stat(real).st_mtime)
    except OSError:
        return ""
    if key in _CLI_VERSIONS:
        return _CLI_VERSIONS[key]
    try:
        completed = subprocess.run(
            [executable, "--version"], capture_output=True, text=True,
            timeout=CLI_VERSION_PROBE_SECONDS, env=provider_environment(executable),
        )
        match = re.search(r"\d+(?:\.\d+)+", f"{completed.stdout}\n{completed.stderr}")
        version = match.group(0) if match else ""
    except (OSError, subprocess.TimeoutExpired):
        version = ""
    _CLI_VERSIONS[key] = version
    return version


def resolved_cli(provider: str, *, source_environment: dict[str, str] | None = None) -> dict[str, str]:
    """The CLI a managed agent will run and its version, for the launch line and Settings.

    A binary named by HARNESS_CLAUDE_BIN / HARNESS_CODEX_BIN is the owner's
    explicit choice: it is used as given and never executed just to read a version.
    """
    provider = str(provider).strip().lower()
    if provider not in control.PROVIDERS:
        return {"path": "", "version": "", "source": "unknown"}
    environment = os.environ if source_environment is None else source_environment
    explicit = str(environment.get(control.PROVIDERS[provider]["binary_env"], ""))
    path = provider_executable(provider, source_environment=environment)
    if not path:
        return {"path": "", "version": "", "source": "missing"}
    if explicit and os.path.isabs(explicit):
        return {"path": path, "version": "", "source": "configured"}
    return {"path": path, "version": cli_version(path), "source": "discovered"}


def provider_environment(
    executable: str, *, source_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a provider child environment that can run its own interpreter.

    Homebrew CLI entry points commonly use ``/usr/bin/env node``. Merely
    resolving the entry point to an absolute path is therefore insufficient
    under launchd. Prepend the already-trusted executable directory and the
    deterministic provider search directories without changing global state.
    """
    environment = child_process.environment(source_environment, git=True, shell=True)
    directories = []
    for value in (
        str(Path(executable).resolve().parent),
        str(Path(executable).parent),
        *str(environment.get("PATH", "")).split(os.pathsep),
        *PROVIDER_SEARCH_DIRECTORIES,
    ):
        if value and value not in directories:
            directories.append(value)
    environment["PATH"] = os.pathsep.join(directories)
    return environment


def update_agent_settings(home: Path, value: dict[str, Any], recovery_models: Any = None, reviewer_retry_models: Any = None) -> dict[str, Any]:
    save_notices: list[str] = []
    validated = control._validated_agent_settings(value, notices=save_notices)
    validated_recovery = control.validated_recovery_models(recovery_models) if recovery_models is not None else None
    validated_reviewer_retry = control.validated_recovery_models(reviewer_retry_models) if reviewer_retry_models is not None else None
    from harness import cli_capabilities
    for role, entry in validated.items():
        availability = cli_capabilities.provider_availability(home, entry["provider"])
        if not availability["available"]:
            raise ValueError(availability["reason"])
    with _locked(home):
        current = _default()
        path = _path(home)
        if path.exists():
            try:
                current = _validate_document(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # Saving is an explicit repair action, but it is never consent
                # to destroy bytes written by a newer version or damaged on
                # disk.  Keep a recoverable copy before replacing the active
                # document with the validated settings from the form.
                _preserve_broken(path)
                current = _default()
        current["agent_settings"] = validated
        if validated_recovery is not None:
            current["recovery_models"] = validated_recovery
        if validated_reviewer_retry is not None:
            current["reviewer_retry_models"] = validated_reviewer_retry
        current["notices"] = save_notices      # an explicit save clears older repair notices
        _write(home, current)
        return json.loads(json.dumps(current))


def _record_connectivity(home: Path, provider: str, result: dict[str, Any]) -> bool:
    with _locked(home):
        try:
            current = _validate_document(json.loads(_path(home).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # Connectivity is diagnostic, not authorization to replace settings
            # from a newer or damaged version. Preserve the unreadable file for
            # explicit repair through the settings form.
            return False
        current["connectivity"][provider] = result
        _write(home, current)
        return True


GEMINI_SIGN_IN_SIGNALS = ("not logged into", "authentication failed", "authentication required", "please sign in",
                          "waiting for authentication", "unauthenticated")
GEMINI_QUOTA_SIGNALS = ("quota", "resource_exhausted", "weekly limit", "usage limit", "rate limit", "429")
GEMINI_CAPACITY_SIGNALS = ("capacity", "overloaded", "unavailable", "503")
GEMINI_BILLING_SIGNALS = ("insufficient credit", "credits", "billing", "payment required", "402")


def _gemini_connection_failure(output: str, model: str, effort: str, detail: str) -> str:
    """The plain sentence for a failed Gemini test: signed out, quota, capacity, billing or a refused model/effort."""
    lowered = output.lower()
    if any(signal in lowered for signal in GEMINI_SIGN_IN_SIGNALS):
        return ("Gemini is not signed in where the agents run. Start a Gemini agent from Mission Control and "
                "follow the sign-in prompt in its window once; the agents keep that sign-in. "
                f"The CLI said: {detail}")
    if any(signal in lowered for signal in GEMINI_BILLING_SIGNALS):
        return ("Your Google account needs credits or a paid plan to run this - add them on the Google side "
                f"(one.google.com). The app itself is fine. The CLI said: {detail}")
    if any(signal in lowered for signal in GEMINI_QUOTA_SIGNALS):
        return ("Gemini has used up your Google plan's quota for now - the agents cannot work until it refreshes "
                f"or the plan is upgraded on the Google side. The CLI said: {detail}")
    if any(signal in lowered for signal in GEMINI_CAPACITY_SIGNALS):
        return f"Gemini has no capacity right now. Try again in a few minutes. The CLI said: {detail}"
    return f"Gemini CLI rejected model {model} or effort {effort}: {detail}"


def _test_gemini_connection(home: Path, model: str, effort: str, workspace: Path) -> dict[str, Any]:
    """One bounded REAL request to Gemini, made the way the agents run it: inside the confinement wrapper.

    A pass proves that the model and effort run on the owner's Google subscription sign-in AND that this
    sign-in is reachable from inside the agents' sandbox (on macOS that is a separate, one-time sign-in).
    """
    import tempfile
    from harness import agent_confinement, gemini_auth
    tested_at = _now()

    def fail(message: str) -> ValueError:
        _record_connectivity(home, "gemini", {"ok": False, "model": model, "effort": effort,
                                              "tested_at": tested_at, "message": message})
        return ValueError(message)

    configured = os.environ.get(control.PROVIDERS["gemini"]["binary_env"], "agy")
    executable = provider_executable("gemini")
    if not executable:
        raise fail(f"The Antigravity CLI (agy) was not found ({configured}). Install it from antigravity.google/cli.")
    problem = gemini_auth.api_key_problem()
    if problem:
        raise fail(problem)
    command = [executable, "-p", CONNECTION_TEST_PROMPT, "--model", model, "--effort", effort,
               "--output-format", "json", "--dangerously-skip-permissions"]
    folder = Path(workspace).resolve()
    with tempfile.TemporaryDirectory(prefix="harness-gemini-test-") as store:
        try:
            command = agent_confinement.wrap(command, [str(folder)], store=Path(store), home=Path.home(), provider="gemini")
        except agent_confinement.ConfinementUnavailable as error:
            raise fail(f"This computer cannot confine the Gemini agent, so it cannot run here: {error}") from error
        try:
            completed = subprocess.run(command, cwd=str(folder), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                       timeout=CONNECTION_TEST_TIMEOUT_SECONDS, env=provider_environment(executable))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise fail(f"Gemini CLI could not be started or did not answer in time: {error}") from error
    output = f"{completed.stdout}\n{completed.stderr}"
    try:
        envelope = json.loads(completed.stdout.strip().splitlines()[-1]) if completed.stdout.strip() else {}
    except ValueError:
        envelope = {}
    answered = completed.returncode == 0 and str(envelope.get("status", "")).upper() == "SUCCESS" \
        and str(envelope.get("response", "")).strip()
    if not answered:
        detail = str(envelope.get("error") or completed.stderr or completed.stdout or "the CLI rejected the request").strip()
        detail = detail.splitlines()[-1] if detail.splitlines() else detail
        raise fail(_gemini_connection_failure(output, model, effort, detail))
    version = cli_version(executable)
    result = {
        "ok": True, "provider": "gemini", "model": model, "effort": effort, "tested_at": tested_at,
        "cli_path": executable, "cli_version": version,
        "message": (
            f"Gemini ran a one-word test with model {model} at {effort} effort and answered, from inside the agents' "
            f"sandbox, on your Google account. This was a real request and used a little of your plan's quota. "
            f"The agents use Antigravity CLI {version or '(version not reported)'} at {executable}."
        ),
    }
    if not _record_connectivity(home, "gemini", {key: value for key, value in result.items() if key != "provider"}):
        result["message"] += " Repair the saved global settings before this result can be stored."
    return result


def test_connection(home: Path, provider: str, model: str, effort: str,
                    workspace: Path) -> dict[str, Any]:
    """Verify one local CLI's model/effort flags without launching a task."""
    initialize(home)
    provider = str(provider).strip().lower()
    if provider not in control.PROVIDERS:
        raise ValueError("choose Codex, Claude or Gemini before testing the connection")
    model = control.normalize_provider_model(provider, model)
    effort = control.normalize_provider_effort(provider, effort)
    if effort not in control.PROVIDER_EFFORTS[provider]:
        choices = ", ".join(control.PROVIDER_EFFORTS[provider])
        raise ValueError(f"{provider.title()} does not support '{effort}'. Choose {choices}.")
    if provider == "gemini":
        asked_model = model
        model, effort, notice = control.gemini_pair(model, effort, repair=False)
        result = _test_gemini_connection(home, model, effort, workspace)
        result["effective_model"], result["effective_effort"] = model, effort
        if notice:
            result["notice"] = notice
            result["message"] = f"{notice}. {result.get('message', '')}".strip()
        return result
    configured = os.environ.get(control.PROVIDERS[provider]["binary_env"], control.provider_executable_name(provider))
    executable = provider_executable(provider)
    # A --help probe exits 0 whatever model is named: it proves the flags parse,
    # never that the model RUNS. That false green let an older Codex CLI pass
    # this test and then fail every real task with HTTP 400. The test now makes
    # the smallest possible REAL request through the CLI the agents will use.
    command = (
        [executable, "exec", "--model", model, "-c", f"model_reasoning_effort={effort}",
         "--skip-git-repo-check", CONNECTION_TEST_PROMPT]
        if executable and provider == "codex"
        else [executable, "--model", model, "-p", CONNECTION_TEST_PROMPT]
        if executable else []
    )
    tested_at = _now()
    if not executable:
        message = f"{provider.title()} CLI was not found ({configured})"
        _record_connectivity(home, provider, {
            "ok": False, "model": model, "effort": effort,
            "tested_at": tested_at, "message": message,
        })
        raise ValueError(message)
    child_environment = provider_environment(executable)
    if provider == "claude":
        # Test the sign-in the agents launch with (the dedicated setup-token), never the owner's ordinary login:
        # otherwise this button is green while every agent launch is blocked.
        from harness import claude_auth
        try:
            token = claude_auth.resolve_token()
            claude_auth.validate_token(token)
        except claude_auth.ClaudeAuthRequired as error:
            card = claude_auth.action(error.reason, error.detail)
            message = f"Claude agents cannot start: {card['why']} {card['instructions']}"
            _record_connectivity(home, provider, {
                "ok": False, "model": model, "effort": effort,
                "tested_at": tested_at, "message": message,
            })
            raise ValueError(message) from error
        for name in claude_auth.OTHER_AUTH:
            child_environment.pop(name, None)
        child_environment[claude_auth.TOKEN_ENV] = token
    try:
        completed = subprocess.run(
            command, cwd=str(Path(workspace).resolve()), capture_output=True, text=True,
            timeout=CONNECTION_TEST_TIMEOUT_SECONDS, env=child_environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        message = f"{provider.title()} CLI could not be started: {error}"
        _record_connectivity(home, provider, {
            "ok": False, "model": model, "effort": effort,
            "tested_at": tested_at, "message": message,
        })
        raise ValueError(message) from error
    output = f"{completed.stdout}\n{completed.stderr}"
    lowered = output.lower()
    # Review finding: a clean exit-0 answer that merely MENTIONS a signal
    # phrase must never be classified as a failure. Signals are consulted only
    # in an error context: a nonzero exit, or an explicit error envelope in
    # the payload (the CLI prints its API error JSON and still exits 0).
    error_context = completed.returncode != 0 or '"type":"error"' in lowered.replace(" ", "")
    out_of_credit = error_context and any(signal in lowered for signal in CREDIT_EXHAUSTED_SIGNALS)
    unavailable = error_context and any(signal in lowered for signal in MODEL_UNAVAILABLE_SIGNALS)
    if completed.returncode != 0 or unavailable or out_of_credit:
        detail = (completed.stderr or completed.stdout or "the CLI rejected the request").strip()
        detail = detail.splitlines()[-1] if detail.splitlines() else detail
        if out_of_credit:
            account = "OpenAI" if provider == "codex" else "Anthropic"
            message = (
                f"Your {account} account is out of credit or over its usage limit - "
                f"the agents cannot work until it is topped up or the plan is "
                f"upgraded on the {account} side. The app itself is fine. "
                f"The CLI said: {detail}"
            )
        elif unavailable:
            message = (
                f"{provider.title()} could not run {model}. This usually means the "
                f"installed {provider.title()} CLI is older than the model, or your "
                f"account has no access to it. Update the CLI, or choose a different "
                f"model above. The CLI said: {detail}"
            )
        else:
            message = f"{provider.title()} CLI rejected model {model} or effort {effort}: {detail}"
        _record_connectivity(home, provider, {
            "ok": False, "model": model, "effort": effort,
            "tested_at": tested_at, "message": message,
        })
        raise ValueError(message)
    # An explicitly configured binary (HARNESS_*_BIN) is never executed just to read its version.
    version = "" if os.path.isabs(configured) else cli_version(executable)
    result = {
        "ok": True, "provider": provider, "model": model, "effort": effort,
        "tested_at": tested_at, "cli_path": executable, "cli_version": version,
        **({"method": "agent-token"} if provider == "claude" else {}),
        "message": (
            (f"{provider.title()} ran a one-word test with model {model} and answered. " if provider == "claude"
             else f"{provider.title()} ran a one-word test with model {model} at {effort} effort and answered. ")
            + ("The agents' own Claude sign-in (the setup-token) was checked first and accepted. " if provider == "claude" else "")
            + f"This was a real request on your own account and costs a fraction of a cent. "
            f"The agents use {provider.title()} {version or '(version not reported)'} at {executable}."
        ),
    }
    if provider == "claude":
        # The one-time "Bypass Permissions mode" warning exits every Claude agent at startup, and a one-word -p request
        # never meets it: say so next to the green result instead of letting the owner find it as a dead agent.
        from harness import preflight
        consent_problem = preflight.claude_bypass_consent_problem(True, executable=executable)
        if consent_problem:
            result["bypass_consent_missing"] = True
            result["message"] += " BUT: " + consent_problem
    recorded = _record_connectivity(
        home, provider, {key: value for key, value in result.items() if key != "provider"},
    )
    if not recorded:
        result["message"] += " Repair the saved global settings before this result can be stored."
    return result

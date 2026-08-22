# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
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

from harness import child_process, control

SETTINGS_VERSION = 1
SETTINGS_FILENAME = "settings.json"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
PROJECT_CHAT_MODEL_ENV = "HARNESS_PROJECT_CHAT_MODEL"
PROJECT_CHAT_MODEL = "gpt-5.6-luna"
SECRETS_DIRECTORY = "secrets"
OPENAI_API_KEY_FILENAME = "openai_api_key"
OPENAI_PROVIDER = "openai"
SECRET_MAX_BYTES = 512
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,100}$")
PROVIDER_SEARCH_DIRECTORIES = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default() -> dict[str, Any]:
    return {
        "version": SETTINGS_VERSION,
        "agent_settings": control.default_agent_settings(),
        "connectivity": {},
    }


def _validate_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or int(value.get("version", 0)) != SETTINGS_VERSION:
        raise ValueError(f"global settings must use version {SETTINGS_VERSION}")
    connectivity = value.get("connectivity", {})
    if not isinstance(connectivity, dict):
        raise ValueError("global connectivity results must be an object")
    return {
        "version": SETTINGS_VERSION,
        "agent_settings": control._validated_agent_settings(value.get("agent_settings")),
        "connectivity": connectivity,
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


def initialize(home: Path, data_roots: list[Path] | None = None) -> dict[str, Any]:
    """Create the global store, migrating the first valid legacy root once."""
    roots = [Path(root) for root in data_roots or []]
    with _locked(home):
        path = _path(home)
        if path.exists():
            try:
                value = _validate_document(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                return {**_default(), "error": f"saved global settings need attention: {error}"}
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
    return initialize(home)


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
    configured = str(environment.get(control.PROVIDERS[provider]["binary_env"], provider))
    if os.path.isabs(configured):
        path = Path(configured)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else ""
    inherited = str(environment.get("PATH", ""))
    directories = []
    for value in (*inherited.split(os.pathsep), *extra_directories, *PROVIDER_SEARCH_DIRECTORIES):
        if value and value not in directories:
            directories.append(value)
    return shutil.which(configured, path=os.pathsep.join(directories)) or ""


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


def update_agent_settings(home: Path, value: dict[str, Any]) -> dict[str, Any]:
    validated = control._validated_agent_settings(value)
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


def test_connection(home: Path, provider: str, model: str, effort: str,
                    workspace: Path) -> dict[str, Any]:
    """Verify one local CLI's model/effort flags without launching a task."""
    initialize(home)
    provider = str(provider).strip().lower()
    if provider not in control.PROVIDERS:
        raise ValueError("choose Codex or Claude before testing the connection")
    model = control.normalize_provider_model(provider, model)
    effort = control.normalize_provider_effort(provider, effort)
    if effort not in control.PROVIDER_EFFORTS[provider]:
        choices = ", ".join(control.PROVIDER_EFFORTS[provider])
        raise ValueError(f"{provider.title()} does not support '{effort}'. Choose {choices}.")
    configured = os.environ.get(control.PROVIDERS[provider]["binary_env"], provider)
    executable = provider_executable(provider)
    command = (
        [executable, "--model", model, "-c", f"model_reasoning_effort={effort}", "--help"]
        if executable and provider == "codex"
        else [executable, "--model", model, "--effort", effort, "--help"] if executable else []
    )
    tested_at = _now()
    if not executable:
        message = f"{provider.title()} CLI was not found ({configured})"
        _record_connectivity(home, provider, {
            "ok": False, "model": model, "effort": effort,
            "tested_at": tested_at, "message": message,
        })
        raise ValueError(message)
    try:
        completed = subprocess.run(
            command, cwd=str(Path(workspace).resolve()), capture_output=True, text=True,
            timeout=10, env=provider_environment(executable),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        message = f"{provider.title()} CLI could not be started: {error}"
        _record_connectivity(home, provider, {
            "ok": False, "model": model, "effort": effort,
            "tested_at": tested_at, "message": message,
        })
        raise ValueError(message) from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "the CLI rejected its startup options").strip().splitlines()[-1]
        message = f"{provider.title()} CLI rejected model {model} or effort {effort}: {detail}"
        _record_connectivity(home, provider, {
            "ok": False, "model": model, "effort": effort,
            "tested_at": tested_at, "message": message,
        })
        raise ValueError(message)
    result = {
        "ok": True, "provider": provider, "model": model, "effort": effort,
        "tested_at": tested_at,
        "message": f"{provider.title()} is installed and accepts model {model} with {effort} effort. No task was launched or billed.",
    }
    recorded = _record_connectivity(
        home, provider, {key: value for key, value in result.items() if key != "provider"},
    )
    if not recorded:
        result["message"] += " Repair the saved global settings before this result can be stored."
    return result

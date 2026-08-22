# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Durable workspace and provider-access settings for Mission Control."""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from harness.project_context import ProjectRoot, project_context


PROVIDERS = {"claude", "codex"}
ROLES = ("delivery", "cto", "reviewer")
DEFAULT_DENY = [
    "Bash(rm -rf /*)",
    "Bash(rm -rf ~*)",
    "Bash(rm -rf $HOME*)",
    "Bash(sudo *)",
    "Bash(git push --force*)",
    "Bash(git push -f*)",
]
CLAUDE_RULE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\(.*\))?$")


def settings_path(root: ProjectRoot) -> Path:
    return project_context(root).storage_path("control", "workspace_settings.json")


DEFAULT_PREVIEW = {
    "command": "",
    "url_template": "http://127.0.0.1:{port}/",
    "startup_timeout_seconds": 45,
}


def _default(root: ProjectRoot) -> dict[str, Any]:
    root = project_context(root).code_root
    return {
        "workspace_root": str(root),
        "workspace_confirmed": True,
        "claude": {
            "settings_path": str(root / ".claude" / "settings.local.json"),
            "default_mode": "bypassPermissions",
            "deny": list(DEFAULT_DENY),
        },
        "codex": {
            "config_path": str(
                Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
                / "config.toml"
            ),
            "trust_level": "trusted",
            "scope": "project",
            "approval_policy": "per-launch",
            "sandbox_mode": "per-launch",
        },
        "preview": dict(DEFAULT_PREVIEW),
        "updated_at": None,
    }


def _validated_preview(value: Any) -> dict[str, Any]:
    section = value if isinstance(value, dict) else {}
    command = str(section.get("command") or "").strip()
    if len(command) > 2000:
        raise ValueError("the preview command must be 2000 characters or fewer")
    url_template = str(section.get("url_template") or DEFAULT_PREVIEW["url_template"]).strip()
    if "{port}" not in url_template:
        raise ValueError("the preview URL template must contain {port}")
    parsed = urlparse(url_template.replace("{port}", "65535"))
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("the preview URL must stay on this computer (127.0.0.1 or localhost)")
    try:
        timeout = int(section.get("startup_timeout_seconds") or DEFAULT_PREVIEW["startup_timeout_seconds"])
    except (TypeError, ValueError) as error:
        raise ValueError("the preview startup timeout must be a number of seconds") from error
    if not 5 <= timeout <= 300:
        raise ValueError("the preview startup timeout must be between 5 and 300 seconds")
    return {"command": command, "url_template": url_template, "startup_timeout_seconds": timeout}


def _save(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return value


def load(root: ProjectRoot) -> dict[str, Any]:
    path = settings_path(root)
    if not path.is_file():
        return _default(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default(root)
    if not isinstance(value, dict):
        return _default(root)
    merged = _default(root)
    for key, item in value.items():
        if key in PROVIDERS and not isinstance(item, dict):
            continue
        if key not in PROVIDERS:
            merged[key] = item
    for provider in PROVIDERS:
        section = value.get(provider)
        merged[provider] = {
            **_default(root)[provider],
            **(section if isinstance(section, dict) else {}),
        }
    # Project registration is the sole execution-root authority. Ignore a
    # legacy Mission Control override without deleting the historical file.
    execution_root = project_context(root).code_root
    merged["workspace_root"] = str(execution_root)
    merged["workspace_confirmed"] = True
    merged["claude"]["settings_path"] = str(execution_root / ".claude" / "settings.local.json")
    try:
        merged["preview"] = _validated_preview(value.get("preview"))
    except ValueError:
        merged["preview"] = dict(DEFAULT_PREVIEW)
    return merged


def update_preview(root: ProjectRoot, section: dict[str, Any]) -> dict[str, Any]:
    """Persist the owner's candidate-preview launch settings for this project."""
    validated = _validated_preview(section)
    path = settings_path(root)
    current: dict[str, Any] = {}
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                current = stored
        except (OSError, json.JSONDecodeError):
            current = {}
    current["preview"] = validated
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(path, current)
    return validated


def validate_workspace(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("choose a workspace folder")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("workspace folder must be an absolute path")
    if not path.is_dir():
        raise ValueError("workspace folder does not exist or is not a directory")
    return Path(os.path.abspath(os.fspath(path)))


def update(root: ProjectRoot, workspace_root: str) -> dict[str, Any]:
    raise ValueError("the project folder is managed from Projects and cannot be changed here")


def choose_folder() -> str:
    """Open a native macOS folder chooser; return empty when cancelled."""
    if os.uname().sysname != "Darwin":
        raise ValueError("native folder browsing is available on macOS only")
    script = 'POSIX path of (choose folder with prompt "Choose the agent workspace folder")'
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def apply_provider_files(settings: dict[str, Any], provider: str) -> dict[str, str]:
    """Apply only the explicitly supported access fields, preserving other keys."""
    if provider not in PROVIDERS:
        raise ValueError("provider must be claude or codex")
    workspace = validate_workspace(settings["workspace_root"])
    if provider == "claude":
        path = workspace / ".claude" / "settings.local.json"
        value: dict[str, Any] = {}
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(f"Claude settings are not valid JSON: {error}") from error
        permissions = dict(value.get("permissions") or {})
        permissions["defaultMode"] = "bypassPermissions"
        existing_deny = [rule for rule in permissions.get("deny") or [] if isinstance(rule, str) and CLAUDE_RULE.fullmatch(rule)]
        permissions["deny"] = list(dict.fromkeys(DEFAULT_DENY + existing_deny))
        value["permissions"] = permissions
        _save(path, value)
        return {"provider": provider, "path": str(path), "mode": "bypassPermissions", "deny_count": str(len(permissions["deny"]))}
    path = Path(settings["codex"]["config_path"]).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = original.splitlines()
    first_table = next((index for index, line in enumerate(lines) if line.strip().startswith("[")), len(lines))
    top_level = lines[:first_table]
    tables = lines[first_table:]
    # Approval and sandbox access are PER-LAUNCH flags scoped to one project's
    # sessions; they are never written globally. Remove any global copies a
    # previous harness version left behind - one project's full-access decision
    # must not leak into other projects or the owner's own codex sessions.
    top_level = [line for line in top_level if not re.match(r"^\s*(approval_policy|sandbox_mode)\s*=", line)]
    tables = [line for line in tables if not re.match(r"^\s*(approval_policy|sandbox_mode)\s*=", line)]
    escaped_workspace = str(workspace).replace("\\", "\\\\").replace('"', '\\"')
    section = f'[projects."{escaped_workspace}"]'
    if section not in tables:
        tables.extend(["", section, 'trust_level = "trusted"'])
    else:
        section_index = tables.index(section)
        next_section = next((index for index in range(section_index + 1, len(tables)) if tables[index].strip().startswith("[")), len(tables))
        section_body = tables[section_index + 1:next_section]
        trust_indexes = [index for index, line in enumerate(section_body) if re.match(r"^\s*trust_level\s*=", line)]
        if trust_indexes:
            section_body[trust_indexes[0]] = 'trust_level = "trusted"'
            section_body = [line for index, line in enumerate(section_body) if index == trust_indexes[0] or not re.match(r"^\s*trust_level\s*=", line)]
        else:
            section_body.append('trust_level = "trusted"')
        tables[section_index + 1:next_section] = section_body
    lines = top_level + tables
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {
        "provider": provider, "path": str(path), "trust_level": "trusted",
        "scope": "project", "approval_policy": "per-launch", "sandbox_mode": "per-launch",
    }

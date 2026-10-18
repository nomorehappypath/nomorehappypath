# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""What a FULL run still needs, in plain words, for `bash install.sh --check` (clean-install findings, 2026-10-17).

`install.sh --check` used to say agent sessions can start while two things that stop a real run were never mentioned:
Claude Code's one-time "Bypass Permissions mode" warning (its default answer exits) and the absence of a native
headless browser for rendered-page proof. These functions only LOOK; they never accept a consent for the owner and
never install anything. Each returns "" when there is nothing to say.
"""
from __future__ import annotations

import json
import os
import platform
import shlex
from pathlib import Path
from typing import Mapping


def _claude_config_dir(home: Path, env: Mapping[str, str]) -> tuple[Path, bool]:
    configured = str(env.get("CLAUDE_CONFIG_DIR", "")).strip()
    return (Path(configured).expanduser(), True) if configured else (home / ".claude", False)


def _relative_config_dir(env: Mapping[str, str]) -> bool:
    configured = str(env.get("CLAUDE_CONFIG_DIR", "")).strip()
    return bool(configured) and not Path(configured).expanduser().is_absolute()


def _read_json(path: Path) -> tuple[dict, str]:
    """(value, problem): problem is "" when the file is absent or fine, "unreadable" when it exists but cannot be used."""
    if not path.exists():
        return {}, ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, "unreadable"
    return (value, "") if isinstance(value, dict) else ({}, "unreadable")


def claude_bypass_consent_state(home: Path, env: Mapping[str, str]) -> tuple[str, str]:
    """("accepted" | "missing" | "unreadable", the settings file concerned). Only the configuration the agents actually use counts."""
    folder, explicit = _claude_config_dir(home, env)
    settings, settings_problem = _read_json(folder / "settings.json")
    if settings.get("skipDangerousModePermissionPrompt") is True:
        return "accepted", ""
    # Older versions recorded the acceptance in the state file: inside the configuration folder when one is selected,
    # else in the home folder.
    state_path = (folder / ".claude.json") if explicit else (home / ".claude.json")
    state, state_problem = _read_json(state_path)
    if state.get("bypassPermissionsModeAccepted") is True:
        return "accepted", ""
    if settings_problem:
        return "unreadable", str(folder / "settings.json")
    if state_problem:
        return "unreadable", str(state_path)
    return "missing", ""


def claude_bypass_consent_accepted(home: Path, env: Mapping[str, str]) -> bool:
    return claude_bypass_consent_state(home, env)[0] == "accepted"


def claude_bypass_consent_problem(
    claude_installed: bool, home: Path | None = None, env: Mapping[str, str] | None = None, executable: str = "claude",
) -> str:
    """Empty when Claude Code is absent (its own line already says so) or the owner already accepted the warning."""
    if not claude_installed:
        return ""
    home = Path(home) if home is not None else Path.home()
    env = os.environ if env is None else env
    if _relative_config_dir(env):
        # The agent starts in another folder, so a relative path would point at different settings than this check reads.
        return (
            f"CLAUDE_CONFIG_DIR is the relative path {env.get('CLAUDE_CONFIG_DIR')!r}; the agents start in other folders and would read "
            "different settings. Set it to an absolute path (or unset it), then run `claude --dangerously-skip-permissions` once there "
            "and choose \"Yes, I accept\"."
        )
    state, concerned = claude_bypass_consent_state(home, env)
    if state == "accepted":
        return ""
    configured = str(env.get("CLAUDE_CONFIG_DIR", "")).strip()
    prefix = f"CLAUDE_CONFIG_DIR={shlex.quote(configured)} " if configured else ""
    command = f"{prefix}{shlex.quote(executable or 'claude')} --dangerously-skip-permissions"
    if state == "unreadable":
        return (
            f"Claude Code's settings file {concerned} cannot be read, so it cannot be confirmed that you accepted its first-run "
            f"\"Bypass Permissions mode\" warning. Repair or remove that file, then in your own terminal run `{command}` once "
            "and choose \"Yes, I accept\" (this is your decision; the app never does it for you). Edit only what is broken in that file; "
            "do not delete other permissions or hooks you rely on."
        )
    return (
        "Claude Code has not been told once that agents may run without asking. Its first-run \"Bypass Permissions mode\" "
        "warning defaults to \"No, exit\", so every Claude agent would stop at startup. In your own terminal run "
        f"`{command}` once and choose \"Yes, I accept\" (this is your decision; the app never does it for you), then retry the "
        "agent (or run this check again). Run it in an empty folder and type /exit afterwards: that session is not confined like the agents."
    )


def chrome_install_line(machine: str | None = None) -> str:
    """The Google Chrome .deb for this machine, or "" when no known native package exists for the architecture."""
    machine = (machine or platform.machine()).lower()
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(machine, "")
    if not arch:
        return ""
    return (
        f"curl -fsSLo /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_{arch}.deb "
        "&& sudo apt install /tmp/google-chrome.deb"
    )


def native_browser_problem(system: str | None = None, machine: str | None = None, resolver=None) -> str:
    """Rendered-page proof needs a native headless-capable Chromium. Work that does not render pages is not affected."""
    system = system or platform.system()
    if resolver is None:
        from harness import browser_acceptance
        resolver = browser_acceptance.resolve_binary
    try:
        found = resolver()
    except ValueError:
        # HARNESS_BROWSER_BIN / CHROME_BIN is set but unusable: say so instead of pretending a browser exists.
        return "HARNESS_BROWSER_BIN points at a file that is not a usable browser. Point it at a native Chrome or Chromium executable."
    except FileNotFoundError:
        found = ""
    if found:
        return ""
    effect = "Only rendered-page proof (screen checks and rendered tests) is affected; other work runs without it."
    if system != "Linux":
        return (
            f"No headless Chromium for rendered-page proof was found. {effect} Install chrome-headless-shell or Chromium outside "
            "an application bundle and set HARNESS_BROWSER_BIN to its executable."
        )
    line = chrome_install_line(machine)
    how = (
        f"Install Google Chrome: {line} (this assumes apt; on another package manager install a native Chrome or Chromium instead), "
        "then set HARNESS_BROWSER_BIN=/usr/bin/google-chrome (the app does not look for google-chrome on its own). For the auto-start "
        "service on Linux run `systemctl --user edit com.nomorehappypath.app.service`, add under [Service] the line "
        "Environment=HARNESS_BROWSER_BIN=/usr/bin/google-chrome, then `systemctl --user restart com.nomorehappypath.app.service` "
        "(check the unit name with `systemctl --user list-units | grep -i nomorehappypath`)."
        if line else
        "No known native Chrome package for this processor: install a native (not snap) Chromium and set HARNESS_BROWSER_BIN to its executable."
    )
    return f"No native (non-snap) Chrome or Chromium was found; a snap Chromium is not supported. {effect} {how}"


def role_defaults_text() -> str:
    from harness import control
    parts = []
    for role, entry in control.ROLE_SETTINGS.items():
        parts.append(f"{entry['label']}: {entry['default_provider'].title()} {entry['default_model']} ({entry['default_effort']})")
    return "Shipped defaults — " + "; ".join(parts) + ". Change them in Settings; press Test connection on each signed-in CLI."

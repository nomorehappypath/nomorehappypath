# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Version display, update check, and the consented one-click update.

The check talks only to the installation's OWN git origin (for public
installs, GitHub - the same host the clone already points at; nothing is ever
sent to KpiMinds LLC). Updating never happens without a human click, always
fast-forward-only, and never while a project is open.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from harness import git_process

VERSION_FILENAME = "VERSION"
GIT_TIMEOUT_SECONDS = 20.0
VERSION_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
RELEASE_NOTES_URL = "https://github.com/nomorehappypath/nomorehappypath/releases"


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    return git_process.run(
        ["git", *arguments], cwd=root,
        capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
    )


def installed_version(root: Path) -> str:
    """The version this installation runs: VERSION file, tag, or development."""
    root = Path(root)
    try:
        value = (root / VERSION_FILENAME).read_text(encoding="utf-8").strip()
        if value and len(value) <= 64:
            return value
    except OSError:
        pass
    try:
        described = _run_git(root, "describe", "--tags", "--abbrev=0")
        if described.returncode == 0 and described.stdout.strip():
            return described.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "development"


def _parse(tag: str) -> tuple[int, int, int] | None:
    match = VERSION_TAG.fullmatch(tag.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def latest_remote_version(root: Path) -> str:
    """The newest release tag on this installation's own origin."""
    try:
        listed = _run_git(root, "ls-remote", "--tags", "origin")
    except subprocess.TimeoutExpired as error:
        raise ValueError("The update check timed out. Check this computer's internet connection and try again.") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"The update check could not run git: {error}") from error
    if listed.returncode != 0:
        detail = (listed.stderr or "git could not reach the origin").strip().splitlines()[-1]
        raise ValueError(f"The update check could not reach this installation's origin: {detail}")
    versions = []
    for line in listed.stdout.splitlines():
        parts = line.split("refs/tags/", 1)
        if len(parts) == 2:
            parsed = _parse(parts[1].removesuffix("^{}"))
            if parsed:
                versions.append(parsed)
    if not versions:
        raise ValueError("This installation's origin has no release versions to compare against.")
    best = max(versions)
    return "v{}.{}.{}".format(*best)


def check(root: Path) -> dict[str, Any]:
    """Compare installed vs newest origin release, in owner language."""
    installed = installed_version(root)
    latest = latest_remote_version(root)
    installed_parsed = _parse(installed)
    update_available = installed_parsed is None or installed_parsed < _parse(latest)
    return {
        "installed": installed,
        "latest": latest,
        "update_available": update_available,
        "release_notes_url": RELEASE_NOTES_URL,
        "message": (
            f"{latest} is available (you run {installed})."
            if update_available else f"You are up to date ({installed})."
        ),
    }


def apply_update(root: Path) -> dict[str, Any]:
    """Fast-forward this installation to its origin's main. Consented callers only.

    Refuses - with plain guidance - when the clone has local edits or has
    diverged: --ff-only never overwrites anything the user changed.
    """
    root = Path(root)
    status = _run_git(root, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        raise ValueError("This installation is not a git clone; update manually by downloading the new version.")
    if status.stdout.strip():
        raise ValueError(
            "This installation has local changes, so the update will not overwrite anything. "
            "Commit or discard your changes, or run 'git pull --ff-only' yourself."
        )
    pulled = _run_git(root, "pull", "--ff-only", "origin", "main")
    if pulled.returncode != 0:
        detail = (pulled.stderr or pulled.stdout or "git pull failed").strip().splitlines()[-1]
        raise ValueError(
            f"The update could not be applied automatically: {detail} "
            f"Run 'git pull --ff-only' in the installation folder, or re-clone."
        )
    return {"updated_to": installed_version(root), "message": "Updated. The app is restarting with the new version."}

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
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


def latest_remote_release(root: Path) -> tuple[str, str]:
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
    # Each release tag with the COMMIT it names: an annotated tag's "^{}" line
    # is its commit, a light tag's own line is (security scan 2026-10-04, #6).
    commits: dict[tuple[int, int, int], str] = {}
    peeled: set[tuple[int, int, int]] = set()
    for line in listed.stdout.splitlines():
        object_id, _, reference = line.partition("\t")
        parts = reference.split("refs/tags/", 1)
        if len(parts) != 2:
            continue
        parsed = _parse(parts[1].removesuffix("^{}"))
        if not parsed:
            continue
        if parts[1].endswith("^{}"):
            commits[parsed] = object_id.strip()
            peeled.add(parsed)
        elif parsed not in peeled:
            commits[parsed] = object_id.strip()
    if not commits:
        raise ValueError("This installation's origin has no release versions to compare against.")
    best = max(commits)
    return "v{}.{}.{}".format(*best), commits[best]


def latest_remote_version(root: Path) -> str:
    """The newest release tag on this installation's own origin."""
    return latest_remote_release(root)[0]




def check(root: Path) -> dict[str, Any]:
    """Compare installed vs newest origin release, in owner language."""
    installed = installed_version(root)
    latest, latest_commit = latest_remote_release(root)
    installed_parsed = _parse(installed)
    update_available = installed_parsed is None or installed_parsed < _parse(latest)
    return {
        "installed": installed,
        "latest": latest,
        "latest_commit": latest_commit,
        "update_available": update_available,
        "release_notes_url": RELEASE_NOTES_URL,
        "message": (
            f"{latest} is available (you run {installed})."
            if update_available else f"You are up to date ({installed})."
        ),
    }


def apply_update(root: Path, expected_version: str = "", expected_commit: str = "") -> dict[str, Any]:
    """Install EXACTLY the newest release (its tag's commit), never a moving branch. Consented callers only.

    Security scan 2026-10-04, finding 6: the update used to pull origin/main,
    whatever it was at that moment, not the release the owner was shown. Now:
    the release tag is fetched, its commit must equal the one the origin
    listed (and, when the page passes it, the version the owner approved),
    the clone fast-forwards to that commit only, and the result is checked.
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
    version, commit = latest_remote_release(root)
    if expected_version and expected_version != version:
        raise ValueError(f"A newer release ({version}) appeared since you checked ({expected_version}). Check again, then update.")
    if expected_commit and expected_commit != commit:
        # Review r1: the same version re-pointed to other code after the check.
        raise ValueError(f"{version} changed since you checked it; nothing was changed. Check again, then update.")
    fetched = _run_git(root, "fetch", "--no-tags", "origin", f"+refs/tags/{version}:refs/tags/{version}")
    if fetched.returncode != 0:
        detail = (fetched.stderr or fetched.stdout or "git fetch failed").strip().splitlines()[-1]
        raise ValueError(f"The update could not download {version}: {detail}")
    tagged = _run_git(root, "rev-parse", f"refs/tags/{version}^{{commit}}")
    if tagged.returncode != 0 or tagged.stdout.strip() != commit or (expected_commit and tagged.stdout.strip() != expected_commit):
        raise ValueError(f"The downloaded {version} does not match the release the origin listed; nothing was changed.")
    merged = _run_git(root, "merge", "--ff-only", commit)
    if merged.returncode != 0:
        detail = (merged.stderr or merged.stdout or "git merge failed").strip().splitlines()[-1]
        raise ValueError(
            f"The update could not be applied automatically: {detail} "
            f"Run 'git pull --ff-only' in the installation folder, or re-clone."
        )
    head = _run_git(root, "rev-parse", "HEAD")
    if head.stdout.strip() != commit:
        raise ValueError(f"The update stopped short of {version}; nothing else was changed.")
    return {"updated_to": version, "commit": commit, "message": "Updated. The app is restarting with the new version."}

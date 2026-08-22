# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Immutable identity for the Harness code loaded by a long-running process."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness import git_process


# Build-lineage namespace; do not change or derived identities drift.
_LINEAGE_NS = "8f407d14ed13d0117e7367256ed2a17f"

def _git(root: Path, *args: str) -> str:
    try:
        completed = git_process.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _source_digest(root: Path) -> str:
    """Digest executable Harness sources, independent of mtime and paths."""
    digest = hashlib.sha256()
    source_root = root / "harness"
    for path in sorted(source_root.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def capture(root: Path | None = None) -> dict[str, Any]:
    """Capture once; callers must retain this value for the process lifetime."""
    repository = Path(root or Path(__file__).resolve().parents[1]).resolve()
    commit = _git(repository, "rev-parse", "HEAD")
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    status = _git(repository, "status", "--porcelain=v1", "--untracked-files=no")
    return {
        "version": 1,
        "commit": commit,
        "tree": tree,
        "source_digest": _source_digest(repository),
        "clean": not bool(status),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def public(value: dict[str, Any]) -> dict[str, Any]:
    """Return only non-secret identity fields suitable for loopback responses."""
    return {
        key: value.get(key)
        for key in ("version", "commit", "tree", "source_digest", "clean", "captured_at")
    }


def matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Require the strongest identity both loaded processes can prove.

    Normal repository launches must match commit, tree, and executable source
    bytes.  Release health also executes an immutable ``git archive``, where
    commit and tree metadata do not exist.  Two processes from that same
    archive may match by their exact executable-source digest, but a mixed or
    partial Git identity always fails closed.  The deployed-release gate still
    compares the reported commit with the independently reviewed commit.
    """
    left_source = str(left.get("source_digest") or "")
    right_source = str(right.get("source_digest") or "")
    if not left_source or left_source != right_source:
        return False

    left_commit, left_tree = str(left.get("commit") or ""), str(left.get("tree") or "")
    right_commit, right_tree = str(right.get("commit") or ""), str(right.get("tree") or "")
    if bool(left_commit) != bool(left_tree) or bool(right_commit) != bool(right_tree):
        return False
    if left_commit or right_commit:
        return bool(
            left_commit and right_commit
            and left_commit == right_commit
            and left_tree == right_tree
        )
    return True


def digest(value: dict[str, Any]) -> str:
    fields = {key: value.get(key) for key in ("commit", "tree", "source_digest")}
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# Import-time capture is intentional: later Git movement or source edits must
# never make an already-running process claim it loaded newer bytes.
PROCESS = capture()

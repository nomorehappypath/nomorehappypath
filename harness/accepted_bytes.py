# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Exact Git tree-entry manifests for independently accepted changes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
from typing import Any, Iterable


MANIFEST_VERSION = 1


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    executable = shutil.which("git", path=environment.get("PATH"))
    if not executable:
        raise ValueError("git executable is unavailable")
    result = subprocess.run(
        [str(Path(executable).resolve()), "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo, env=environment, capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(args)} failed: {detail}")
    return result


def _safe_path(value: str) -> str:
    path = PurePosixPath(str(value))
    if not value or path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
        raise ValueError(f"accepted-byte path is unsafe: {value!r}")
    return str(path)


def _tree(repo: Path, revision: str) -> str:
    return _git(repo, "rev-parse", f"{revision}^{{tree}}").stdout.decode().strip()


def _changed_paths(repo: Path, base: str, reviewed: str) -> list[str]:
    output = _git(
        repo, "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z",
        base, reviewed, "--",
    ).stdout
    return sorted({_safe_path(value.decode("utf-8", errors="strict")) for value in output.split(b"\0") if value})


def tree_entry(repo: Path, revision: str, path: str) -> dict[str, str]:
    safe = _safe_path(path)
    output = _git(repo, "ls-tree", "-z", revision, "--", safe).stdout
    rows = [row for row in output.split(b"\0") if row]
    if not rows:
        return {"path": safe, "state": "deleted"}
    if len(rows) != 1:
        raise ValueError(f"Git returned ambiguous tree entries for {safe}")
    metadata, raw_path = rows[0].split(b"\t", 1)
    mode, object_type, oid = metadata.decode("ascii").split()
    actual_path = raw_path.decode("utf-8", errors="strict")
    if actual_path != safe:
        raise ValueError(f"Git tree entry path mismatch for {safe}")
    return {
        "path": safe, "state": "present", "mode": mode,
        "type": object_type, "oid": oid,
    }


def tree_delta(repo: Path, base_commit: str, reviewed_commit: str) -> dict[str, Any]:
    """Return a mode/type/OID-aware changed-path projection between two commits."""
    repository = Path(repo).resolve()
    base = _git(repository, "rev-parse", f"{base_commit}^{{commit}}").stdout.decode().strip()
    reviewed = _git(repository, "rev-parse", f"{reviewed_commit}^{{commit}}").stdout.decode().strip()
    paths = _changed_paths(repository, base, reviewed)
    result: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "base_commit": base,
        "reviewed_commit": reviewed,
        "reviewed_tree": _tree(repository, reviewed),
        "paths": paths,
        "entries": [tree_entry(repository, reviewed, path) for path in paths],
    }
    result["sha256"] = _payload_digest(result)
    return result


def _payload_digest(value: dict[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "sha256"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_manifest(
    repo: Path, base_commit: str, reviewed_commit: str,
    expected_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    repository = Path(repo).resolve()
    base = _git(repository, "rev-parse", f"{base_commit}^{{commit}}").stdout.decode().strip()
    reviewed = _git(repository, "rev-parse", f"{reviewed_commit}^{{commit}}").stdout.decode().strip()
    changed = _changed_paths(repository, base, reviewed)
    if expected_paths is not None:
        expected = sorted({_safe_path(value) for value in expected_paths})
        if expected != changed:
            missing = sorted(set(changed) - set(expected))
            extra = sorted(set(expected) - set(changed))
            raise ValueError(
                "accepted-byte path manifest differs from the reviewed change"
                f"; missing={missing}; unexpected={extra}"
            )
    if not changed:
        raise ValueError("accepted-byte manifest requires at least one reviewed path")
    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "base_commit": base,
        "reviewed_commit": reviewed,
        "reviewed_tree": _tree(repository, reviewed),
        "paths": changed,
        "entries": [tree_entry(repository, reviewed, path) for path in changed],
    }
    manifest["sha256"] = _payload_digest(manifest)
    return manifest


def verify_manifest(repo: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise ValueError("accepted-byte manifest is missing or uses an unsupported version")
    if manifest.get("sha256") != _payload_digest(manifest):
        raise ValueError("accepted-byte manifest digest is missing or corrupt")
    repository = Path(repo).resolve()
    expected_paths = sorted({_safe_path(value) for value in manifest.get("paths", [])})
    entries = manifest.get("entries")
    if not expected_paths or not isinstance(entries, list):
        raise ValueError("accepted-byte manifest has no complete path entries")
    if [entry.get("path") for entry in entries if isinstance(entry, dict)] != expected_paths:
        raise ValueError("accepted-byte entries are incomplete, duplicated, or out of order")
    rebuilt = build_manifest(
        repository, str(manifest.get("base_commit", "")),
        str(manifest.get("reviewed_commit", "")), expected_paths,
    )
    if rebuilt != manifest:
        raise ValueError("accepted-byte manifest differs from the reviewed candidate")
    return rebuilt


def verify_entries(repo: Path, revision: str, manifest: dict[str, Any]) -> dict[str, Any]:
    verify_manifest(repo, manifest)
    repository = Path(repo).resolve()
    mismatches = []
    for expected in manifest["entries"]:
        actual = tree_entry(repository, revision, expected["path"])
        if actual != expected:
            mismatches.append({"path": expected["path"], "expected": expected, "actual": actual})
    if mismatches:
        raise ValueError(
            "integrated tree does not contain every exact accepted entry: "
            + ", ".join(item["path"] for item in mismatches)
        )
    return {
        "status": "verified", "revision": revision,
        "tree": _tree(repository, revision), "manifest_sha256": manifest["sha256"],
        "paths": list(manifest["paths"]),
    }


def verify_planned_tree(repo: Path, revision: str, planned_tree: str) -> str:
    actual = _tree(Path(repo).resolve(), revision)
    if not planned_tree or actual != planned_tree:
        raise ValueError(
            f"integrated full tree differs from the planned tree; expected={planned_tree or 'missing'} actual={actual}"
        )
    return actual

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Deterministic grouped repair records with fail-closed review depth."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any


VERSION = 1
FULL_DEPTH_CATEGORIES = {
    "security", "data_integrity", "concurrency", "architecture", "recovery", "unknown",
}
ALLOWED_CATEGORIES = FULL_DEPTH_CATEGORIES | {"behavior", "compatibility", "ux", "cosmetic"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_path(value: str) -> str:
    path = PurePosixPath(str(value or "").strip())
    if not str(path) or path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise ValueError(f"repair member path is unsafe: {value!r}")
    return str(path)


def _digest(value: dict[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "sha256"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def refresh_digest(package: dict[str, Any]) -> str:
    package["sha256"] = _digest(package)
    return package["sha256"]


def required_scope(members: list[dict[str, Any]]) -> str:
    return "full" if any(member["category"] in FULL_DEPTH_CATEGORIES for member in members) else "affected"


def _member(
    raw: dict[str, Any], index: int, *, fallback_summary: str,
    fallback_paths: list[str], fallback_surface: str,
) -> dict[str, Any]:
    identifier = str(raw.get("id") or f"F-{index:03d}").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", identifier):
        raise ValueError(f"repair member ID is invalid: {identifier!r}")
    summary = str(raw.get("summary") or fallback_summary).strip()
    if len(summary) < 8 or len(summary) > 2000:
        raise ValueError(f"repair member {identifier} requires an 8 to 2000 character summary")
    category = str(raw.get("category") or "unknown").strip().lower()
    if category not in ALLOWED_CATEGORIES:
        raise ValueError(f"repair member {identifier} has unsupported category {category!r}")
    paths = sorted({_safe_path(path) for path in (raw.get("affected_paths") or fallback_paths)})
    surface = str(raw.get("surface") or fallback_surface).strip()
    if not surface:
        raise ValueError(f"repair member {identifier} requires an affected surface")
    regression = str(raw.get("regression_check") or "").strip()
    if raw and len(regression) < 8:
        raise ValueError(f"repair member {identifier} requires a concrete regression check")
    return {
        "id": identifier,
        "summary": summary,
        "category": category,
        "affected_paths": paths,
        "surface": surface[:500],
        "regression_check": regression[:2000],
        "status": "open",
    }


def build(
    task: str, request: dict[str, Any], summary: str,
    members: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    explicit = bool(members)
    raw_members = list(members or [{}])
    if len(raw_members) > 100:
        raise ValueError("a repair package may contain at most 100 failures")
    surface = ":".join(filter(None, (
        str(request.get("phase") or "review"), str(request.get("subtask") or ""),
        str(request.get("chunk") or ""),
    )))
    normalized = [
        _member(
            raw, index, fallback_summary=summary,
            fallback_paths=list(request.get("reviewed_files") or []),
            fallback_surface=surface,
        )
        for index, raw in enumerate(raw_members, 1)
    ]
    if len({member["id"] for member in normalized}) != len(normalized):
        raise ValueError("repair member IDs must be unique")
    identity = {
        "reviewed_commit": str(request.get("reviewed_commit") or ""),
        "reviewed_tree": str(request.get("reviewed_tree_hash") or ""),
        "accepted_manifest_sha256": str(
            (request.get("accepted_byte_manifest") or {}).get("sha256") or ""
        ),
        "review_request_id": str(request.get("id") or ""),
    }
    package_id = "repair-" + hashlib.sha256(
        json.dumps({"task": task, "identity": identity, "members": normalized}, sort_keys=True).encode()
    ).hexdigest()[:16]
    package: dict[str, Any] = {
        "version": VERSION, "id": package_id, "task": task,
        "source_request_id": str(request.get("id") or ""),
        "phase": str(request.get("phase") or ""),
        "subtask": str(request.get("subtask") or ""),
        "chunk": str(request.get("chunk") or ""),
        "prior_accepted_identity": identity,
        "members": normalized,
        "required_test_scope": required_scope(normalized),
        "requires_explicit_resolution": explicit,
        "status": "open", "created_at": _now(),
    }
    package["sha256"] = _digest(package)
    return package


def resolve(
    package: dict[str, Any], resolutions: list[dict[str, str]], delivery_id: str,
) -> dict[str, Any]:
    if package.get("status") not in {"open", "ready_for_review"}:
        raise ValueError("only an open repair package may be resolved")
    by_id = {str(row.get("id") or ""): row for row in resolutions}
    expected = {member["id"] for member in package.get("members") or []}
    if set(by_id) != expected:
        raise ValueError(
            "repair resolutions must cover every package member exactly once; "
            f"missing={sorted(expected - set(by_id))}; unexpected={sorted(set(by_id) - expected)}"
        )
    resolved_at = _now()
    for member in package["members"]:
        row = by_id[member["id"]]
        resolution = str(row.get("resolution") or "").strip()
        regression = str(row.get("regression_check") or "").strip()
        if len(resolution) < 8 or len(regression) < 8:
            raise ValueError(f"repair member {member['id']} requires a resolution and regression check")
        member.update({
            "status": "resolved", "resolution": resolution[:2000],
            "resolved_regression_check": regression[:2000],
            "resolved_by": delivery_id, "resolved_at": resolved_at,
        })
    package.update({
        "status": "ready_for_review", "resolved_at": resolved_at,
        "resolved_by": delivery_id,
    })
    package["sha256"] = _digest(package)
    return package


def split(
    package: dict[str, Any], groups: list[list[str]], reviewer_id: str, reason: str,
) -> list[dict[str, Any]]:
    reason = str(reason or "").strip()
    if len(groups) < 2 or len(reason) < 8:
        raise ValueError("a repair split requires at least two groups and a concrete reason")
    expected = {member["id"] for member in package.get("members") or []}
    flattened = [identifier for group in groups for identifier in group]
    if len(flattened) != len(set(flattened)) or set(flattened) != expected:
        raise ValueError("repair split must assign every member exactly once without duplication")
    source_members = {member["id"]: member for member in package["members"]}
    children = []
    for index, group in enumerate(groups, 1):
        members = [json.loads(json.dumps(source_members[identifier])) for identifier in group]
        child = {
            key: json.loads(json.dumps(value))
            for key, value in package.items()
            if key not in {"id", "members", "required_test_scope", "status", "sha256"}
        }
        child.update({
            "id": f"{package['id']}-part-{index}", "parent_package_id": package["id"],
            "members": members, "required_test_scope": required_scope(members),
            "status": "open", "split_by": reviewer_id, "split_reason": reason,
            "split_at": _now(),
        })
        child["sha256"] = _digest(child)
        children.append(child)
    package.update({
        "status": "split", "split_by": reviewer_id, "split_reason": reason,
        "split_at": _now(), "child_package_ids": [child["id"] for child in children],
    })
    package["sha256"] = _digest(package)
    return children

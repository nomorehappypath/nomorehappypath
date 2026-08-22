# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""P2 execution-identity store (item 4): fail-closed certified-result reuse.

Codex round-2 compositional model, one lineage, no parallel schemas:

  CandidateEvidenceIdentity   what was reviewed: commit/tree (or worktree
                              digest), contract revision, artifact digests
  CommandRunIdentity          candidate + STRUCTURED argv (never a shell
                              string), cwd relative to the execution root,
                              runtime versions, dependency lockfile digests,
                              sanitized environment digest, policy version
  ScenarioCertification       ledger digest + scenario id -> a command run
  (PassReuseIdentity lives with the §8.3 evidence-reuse machinery it extends)

Rules, all fail-closed and all audited:
  - lookup returns a certified result only on an EXACT identity match;
  - any differing field is a miss, and the audit names every diverged field;
  - failed executions are stored but NEVER returned as reusable;
  - semantic verdicts are not representable here at all — the store holds
    command executions, and nothing else, by construction;
  - `recorded_at` never participates in any identity;
  - the store is append-only: an identity, once certified, cannot be
    overwritten (a re-certification attempt with a different result digest is
    itself an auditable conflict).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import platform
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from harness import board

STORE_NAME = "execution-store.jsonl"
POLICY_VERSION = 3


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def candidate_evidence_identity(
    commit: str, tree: str, contract_revision: str,
    artifact_digests: dict[str, str] | None = None,
) -> dict[str, Any]:
    fields = {
        "commit": str(commit), "tree": str(tree),
        "contract_revision": str(contract_revision),
        "artifacts": dict(sorted((artifact_digests or {}).items())),
    }
    return {"kind": "candidate_evidence", "fields": fields, "sha256": _sha(fields)}


def runtime_versions() -> dict[str, str]:
    return {
        "python": ".".join(str(v) for v in sys.version_info[:3]),
        "implementation": sys.implementation.name,
        "system": platform.system(),
        "machine": platform.machine(),
    }


def command_run_identity(
    candidate: dict[str, Any],
    argv: list[str],
    cwd: str,
    environment_sha256: str,
    lockfile_digests: dict[str, str] | None = None,
    runtime: dict[str, str] | None = None,
    *,
    role: str = "unspecified",
    gate: str = "unspecified",
    browser: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(argv, (list, tuple)) or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a structured list of strings, never a shell string")
    fields = {
        "candidate_sha256": str(candidate.get("sha256", "")),
        "argv": list(argv),
        "cwd": str(cwd),
        "environment_sha256": str(environment_sha256),
        "lockfiles": dict(sorted((lockfile_digests or {}).items())),
        "runtime": dict(sorted((runtime or runtime_versions()).items())),
        "role": str(role),
        "gate": str(gate),
        "browser": dict(sorted((browser or {}).items())),
        "policy_version": POLICY_VERSION,
    }
    return {"kind": "command_run", "fields": fields, "sha256": _sha(fields)}


def scenario_certification(
    run: dict[str, Any], ledger_sha256: str, scenario_id: str,
) -> dict[str, Any]:
    fields = {
        "command_run_sha256": str(run.get("sha256", "")),
        "ledger_sha256": str(ledger_sha256),
        "scenario_id": str(scenario_id),
    }
    return {"kind": "scenario_certification", "fields": fields, "sha256": _sha(fields)}


def _store_path(root: Path) -> Path:
    return board.board_dir(root) / STORE_NAME


def _store_output(root: Path, payload: bytes, digest: str) -> str:
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("execution output digest does not match the supplied bytes")
    directory = board.board_dir(root) / "execution-artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / digest
    if destination.exists():
        if destination.read_bytes() != payload:
            raise ValueError("execution output artifact conflicts with its content digest")
    else:
        temporary = directory / f".{digest}.{secrets.token_hex(6)}.tmp"
        temporary.write_bytes(payload)
        temporary.replace(destination)
    return str(destination)


def load_output(entry: dict[str, Any]) -> str:
    path_value = str(entry.get("output_path") or "")
    if not path_value:
        raise ValueError("certified execution has no reusable output artifact")
    path = Path(path_value)
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ValueError("certified execution output is unavailable") from error
    if hashlib.sha256(payload).hexdigest() != entry.get("output_sha256"):
        raise ValueError("certified execution output digest is corrupt")
    try:
        return payload.decode("utf-8")
    except UnicodeError as error:
        raise ValueError("certified execution output is not valid UTF-8") from error


def _read_entries(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ValueError(f"execution store is unreadable: {error}") from error
    if payload and not payload.endswith(b"\n"):
        raise ValueError("execution store ends with a torn partial record")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise ValueError("execution store is not valid UTF-8") from error
    entries = []
    for line_number, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"execution store record {line_number} is corrupt") from error
        if not isinstance(entry, dict) or not entry.get("identity_sha256"):
            raise ValueError(f"execution store record {line_number} is incomplete")
        entries.append(entry)
    return entries


@contextmanager
def _store_lock(root: Path, *, exclusive: bool) -> Iterator[Path]:
    directory = board.board_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".execution-store.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield _store_path(root)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _entries(root: Path) -> list[dict[str, Any]]:
    with _store_lock(root, exclusive=False) as path:
        return _read_entries(path)


def _lookup_entries(
    entries: list[dict[str, Any]], identity: dict[str, Any], *, any_result: bool,
) -> dict[str, Any]:
    target_fields = identity["fields"]
    nearest: tuple[int, dict[str, Any] | None] = (-1, None)
    exact_failure = False
    for entry in entries:
        if entry.get("identity_kind") != identity["kind"]:
            continue
        if entry.get("identity_sha256") == identity["sha256"]:
            if not any_result and int(entry.get("exit_code", 1)) != 0:
                exact_failure = True
                continue
            return {"status": "hit", "entry": entry}
        prior_fields = entry.get("identity_fields") or {}
        matching = sum(1 for key, value in target_fields.items() if prior_fields.get(key) == value)
        if matching > nearest[0]:
            nearest = (matching, entry)
    if exact_failure:
        return {"status": "miss", "reason": "prior_execution_failed",
                "diverged_fields": [],
                "note": "failures are never reused as a PASS"}
    diverged = []
    if nearest[1] is not None:
        prior_fields = nearest[1].get("identity_fields") or {}
        diverged = sorted(
            key for key in set(target_fields) | set(prior_fields)
            if target_fields.get(key) != prior_fields.get(key)
        )
    return {"status": "miss", "reason": "no_exact_match", "diverged_fields": diverged}


def certify(
    root: Path,
    identity: dict[str, Any],
    *,
    exit_code: int,
    output_sha256: str,
    duration_seconds: float,
    scenario: dict[str, Any] | None = None,
    retry_reason: str = "",
    output: str | bytes | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one attempt; exact successful identities remain reusable forever."""
    with _store_lock(root, exclusive=True) as path:
        entries = _read_entries(path)
        exact = [
            entry for entry in entries
            if entry.get("identity_kind") == identity.get("kind")
            and entry.get("identity_sha256") == identity.get("sha256")
        ]
        passed = next((entry for entry in exact if int(entry.get("exit_code", 1)) == 0), None)
        if passed:
            if int(exit_code) == 0:
                return {"status": "already_certified", "entry": passed}
            raise ValueError("append-only conflict: a certified success cannot be replaced by a failure")
        prior_failure = exact[-1] if exact else None
        if prior_failure and not retry_reason.strip():
            raise ValueError("retrying a failed certified execution requires an explicit retry reason")
        payload = output.encode("utf-8") if isinstance(output, str) else output
        output_path = _store_output(root, payload, str(output_sha256)) if payload is not None else ""
        entry = {
            "record_id": f"execution-{secrets.token_hex(10)}",
            "identity_sha256": identity["sha256"],
            "identity_kind": identity["kind"],
            "identity_fields": identity["fields"],
            "scenario": (scenario or {}).get("fields"),
            "exit_code": int(exit_code),
            "output_sha256": str(output_sha256),
            "output_path": output_path,
            "duration_seconds": round(float(duration_seconds), 3),
            "retry_of": str((prior_failure or {}).get("record_id") or ""),
            "retry_reason": retry_reason.strip(),
            "metadata": json.loads(json.dumps(metadata or {})),
            "recorded_at": board.now(),  # provenance only — NEVER part of identity
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
        return {"status": "certified", "entry": entry}


def lookup(root: Path, identity: dict[str, Any], *, _any_result: bool = False) -> dict[str, Any]:
    """Exact-match lookup. Misses name every diverged field; failures never reuse.

    The divergence audit compares against the NEAREST prior entry of the same
    kind (most matching fields) so a miss explains itself instead of being a
    bare 'no'.
    """
    with _store_lock(root, exclusive=False) as path:
        return _lookup_entries(_read_entries(path), identity, any_result=_any_result)


def certified_success(
    root: Path, identity_sha256: str, record_id: str = "",
) -> dict[str, Any]:
    """Load one exact certified success by its durable execution identity.

    Release verification uses this read-only path when a final candidate was
    already tested under the same immutable identity. Recompute the identity
    digest and verify the output artifact so a hand-edited store record cannot
    be promoted into a reusable success.
    """
    identity_sha256 = str(identity_sha256 or "").strip()
    record_id = str(record_id or "").strip()
    if len(identity_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in identity_sha256
    ):
        raise ValueError("certified execution identity is invalid")
    matches = [
        entry for entry in _entries(root)
        if entry.get("identity_kind") == "command_run"
        and entry.get("identity_sha256") == identity_sha256
        and (not record_id or entry.get("record_id") == record_id)
        and int(entry.get("exit_code", 1)) == 0
    ]
    if len(matches) != 1:
        raise ValueError("exact certified execution success is unavailable")
    entry = matches[0]
    fields = entry.get("identity_fields")
    if not isinstance(fields, dict) or _sha(fields) != identity_sha256:
        raise ValueError("certified execution identity fields are corrupt")
    load_output(entry)
    return json.loads(json.dumps(entry))


def audit_decision(root: Path, identity: dict[str, Any], decision: dict[str, Any]) -> None:
    """Every reuse decision is an auditable board event (never silent)."""
    with board.locked_state(root) as state:
        board._event(state, "execution_reuse_decision", None, {
            "task": "",
            "identity_sha256": identity.get("sha256", ""),
            "decision": decision.get("status", ""),
            "diverged_fields": decision.get("diverged_fields", []),
            "message": (
                f"execution cache {decision.get('status')}"
                + (": " + ", ".join(decision.get("diverged_fields", []))
                   if decision.get("diverged_fields") else "")
                + (f" ({decision.get('reason')})" if decision.get("reason") else "")
            ),
        })

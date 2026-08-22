# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Deterministic release coordinator (efficiency item 2, P7 residual).

Owns the mechanical tail between a passed final acceptance and the recorded
release, so a finished task can never again strand unpushed behind an inactive
delivery agent (recorded twice: task_b 2026-08-16 04:03Z, TASK_C 16:43Z).

Responsibilities (agreed Round-2 scope):
  - detect a coordinatable task: passed final acceptance, delivery concluded,
    no release recorded, not cancelled;
  - never contact or mutate a remote; record that a separately authorized push
    is required for legacy tasks and preserve broker-governed push guards;
  - execute the mechanical release checks (the same deterministic computation
    the CTO previously relayed by hand) including the bounded health run;
  - hand the CTO exactly ONE semantic step: the claim-scope audit plus the
    release recording, with every mechanical check precomputed;
  - classify its own failures as control-plane incidents on the board — a
    coordinator failure is never routed to delivery as a product repair.

The coordinator STOPS at preparing VISUAL_TEST_REQUIRED: recording remains the
CTO's act, owner acceptance remains the owner's, and any remote beyond the
governed origin remains separately authorized. Every step is restart-safe:
state lives in an append-only journal keyed by (task, head commit), and every
action re-verifies reality before acting.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from harness import board, cto

JOURNAL_NAME = "coordinator-journal.jsonl"


class InjectedCrash(RuntimeError):
    """Test-only crash injection, mirroring the broker's kill-matrix hook."""


def _journal_path(root: Path) -> Path:
    return board.board_dir(root) / JOURNAL_NAME


def _journal(root: Path, record: dict[str, Any]) -> None:
    path = _journal_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": board.now(), **record}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _journal_records(root: Path, task: str, commit: str) -> list[dict[str, Any]]:
    path = _journal_path(root)
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if value.get("task") == task and value.get("commit") == commit:
            records.append(value)
    return records


def _incident(root: Path, task: str, step: str, error: str) -> None:
    """A coordinator failure is a control-plane incident, never product work."""
    board.record_control_plane_hold(
        root, task, f"release_coordinator:{step}",
        error if len(error.strip()) >= 8 else f"coordinator failure: {error}",
    )
    with board.locked_state(root) as state:
        board._event(state, "control_plane_incident", None, {
            "task": task,
            "step": step,
            "message": f"release coordinator {step} failed: {error[:300]} — "
                       "control-plane incident; NOT a product-code repair",
        })


@contextmanager
def _coordination_lock(root: Path) -> Iterator[None]:
    path = board.board_dir(root) / ".release-coordinator.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _file_digest(value: str) -> str:
    path = Path(str(value or ""))
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    except OSError:
        return ""


def _coordination_identity(
    state: dict[str, Any], task: str, request: dict[str, Any], repo: Path,
) -> str:
    repository_value = str(state.get("task_repositories", {}).get(task) or "")
    repository = Path(repository_value) if repository_value else repo
    fields = {
        "task": task,
        "request": {
            key: request.get(key) for key in (
                "id", "status", "reviewed_commit", "reviewed_tree_hash",
                "ledger_sha256", "challenge_ledger_sha256", "completed_at",
            )
        },
        "ledger_digest": _file_digest(str(request.get("ledger") or "")),
        "developers": sorted(
            (agent.get("id", ""), bool(agent.get("active")), agent.get("status", ""))
            for agent in state.get("agents", {}).values()
            if agent.get("task") == task and agent.get("role") in board.DEVELOPER_ROLES
        ),
        "requirements": state.get("requirement_confirmations", {}).get(task, {}),
        "plan": state.get("delivery_plans", {}).get(task, {}),
        "open_findings": sorted(
            finding.get("id", "") for finding in state.get("deferred_findings", {}).values()
            if finding.get("task") == task and finding.get("status") == "in_scope"
        ),
        "workspace_head": cto._git_output(repo, "rev-parse", "HEAD"),
        "workspace_status": cto._status_paths(repo),
        "repository_main": cto._git_output(repository, "rev-parse", "refs/heads/main"),
        "coordinator_code_sha256": _file_digest(__file__),
        "release_gate_code_sha256": _file_digest(cto.__file__),
    }
    return hashlib.sha256(json.dumps(
        fields, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def _route_prepared(
    root: Path, state: dict[str, Any], task: str, commit: str,
    checks_path: Path, coordination_key: str,
) -> bool:
    cto_agent = next((
        agent for agent in state.get("agents", {}).values()
        if agent.get("role") == "cto" and agent.get("active") and agent.get("session_id")
    ), None)
    if not cto_agent:
        return False
    session_id = str(cto_agent["session_id"])
    records = _journal_records(root, task, commit)
    if any(
        row.get("step") == "cto_routed"
        and row.get("coordination_key") == coordination_key
        and row.get("session_id") == session_id
        for row in records
    ):
        # The control inbox is durable. Repeating the same instruction while
        # the CTO works only adds context churn; terminal recovery owns retry.
        return True
    try:
        from harness import control
        queued = control.enqueue_instruction(
            root, session_id,
            f"RELEASE PREPARED by the Python coordinator for {task} at {commit[:12]}. "
            f"Every mechanical check is recorded at {checks_path}. Poll once, perform "
            "the remaining semantic claim-scope decision, and record release-ready. "
            "Call release-check with --execute-health and --record-ready but omit "
            "--health-command: the gate will mechanically validate and reuse the exact "
            "certified full-suite success. Do not rerun certified product tests. "
            "USER ACTION: None.",
            source="release-coordinator",
        )
    except (ValueError, OSError):
        return False
    _journal(root, {
        "task": task, "commit": commit, "step": "cto_routed",
        "coordination_key": coordination_key, "session_id": session_id,
        "instruction_id": queued.get("id", ""),
    })
    return True


def _coordinatable_tasks(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    cancelled = state.get("cancelled_tasks") or {}
    releases = state.get("releases") or {}
    found: list[tuple[str, dict[str, Any]]] = []
    for request in (state.get("qa_requests") or {}).values():
        task = str(request.get("task") or "")
        if (
            request.get("phase") != "final_acceptance"
            or request.get("status") != "passed"
            or not task or task in cancelled or task in releases
        ):
            continue
        developers_live = any(
            agent.get("active") and agent.get("role") in board.DEVELOPER_ROLES
            and agent.get("task") == task
            for agent in (state.get("agents") or {}).values()
        )
        completed = any(
            event.get("kind") == "development_complete" and event.get("task") == task
            for event in state.get("events", [])
        ) or not developers_live
        if completed:
            found.append((task, request))
    return found


def _coordinate_locked(root: Path, crash_after: str = "", crash_before_journal: str = "") -> list[dict[str, Any]]:
    """One controller pass: prepare every coordinatable task's release.

    Idempotent per (task, reviewed commit): a fully prepared task is skipped
    until its CTO recording lands, and a rerun after any crash re-verifies
    reality (remote tip, checks) instead of trusting the journal.
    """
    outcomes: list[dict[str, Any]] = []
    try:
        state = board.snapshot(root)
    except (OSError, ValueError):
        return outcomes
    for task, request in _coordinatable_tasks(state):
        commit = str(request.get("reviewed_commit") or "")
        try:
            records = _journal_records(root, task, commit)
            workspace = state.get("task_workspaces", {}).get(task, "")
            repo = Path(workspace) if workspace else root
            coordination_key = _coordination_identity(state, task, request, repo)
            matching = [
                row for row in records if row.get("coordination_key") == coordination_key
            ]
            prepared = next((row for row in reversed(matching) if row.get("step") == "prepared"), None)
            if prepared:
                checks_path = Path(str(prepared.get("checks_path") or ""))
                routed = _route_prepared(
                    root, state, task, commit, checks_path, coordination_key,
                )
                outcomes.append({
                    "task": task, "status": "already_prepared",
                    "cto_routed": routed,
                })
                continue
            if any(row.get("step") == "checks_failed" for row in matching):
                outcomes.append({"task": task, "status": "unchanged_checks_failed"})
                continue
            _journal(root, {"task": task, "commit": commit, "step": "begin", "coordination_key": coordination_key})
            if crash_after == "begin":
                raise InjectedCrash("killed after begin")
            broker_governed = bool(
                request.get("mirror_ref")
                and state.get("task_repositories", {}).get(task)
            )
            push_result = {
                "step": "push_authorization",
                "status": "owner_authorization_required",
                "policy": "no automatic remote contact",
            }
            if crash_before_journal == "push":
                raise InjectedCrash("killed before push-authorization journal")
            _journal(root, {"task": task, "commit": commit, "coordination_key": coordination_key, **push_result})
            if crash_after == "push":
                raise InjectedCrash("killed after push-authorization journal")
            ledger = str(request.get("ledger") or "")
            checks = cto.release_check(
                root, task, Path(ledger) if ledger else repo / "MISSING-LEDGER",
                repo, execute_health=True)
            required_checks = (
                board.BROKER_RELEASE_REQUIRED_CHECKS
                if broker_governed else board.RELEASE_REQUIRED_CHECKS
            )
            if checks.get("runtime_gate_required"):
                required_checks = set(required_checks) | {
                    "deployed_runtime_verified", "deployed_chat_verified",
                }
            mechanical = {key: bool(checks.get(key)) for key in required_checks}
            failed = sorted(k for k, v in mechanical.items() if not v
                            and k != "claim_scope_audit_passed")
            checks_path = board.board_dir(root) / "evidence" / f"release-checks-{task}.json"
            checks_path.parent.mkdir(parents=True, exist_ok=True)
            checks_path.write_text(json.dumps(
                {k: v for k, v in checks.items() if isinstance(v, (bool, str, int, list))},
                indent=1, sort_keys=True, default=str))
            _journal(root, {"task": task, "commit": commit, "step": "checks",
                            "coordination_key": coordination_key,
                            "failed": failed, "path": str(checks_path)})
            if crash_after == "checks":
                raise InjectedCrash("killed after checks")
            if failed:
                _journal(root, {"task": task, "commit": commit, "step": "checks_failed",
                                "coordination_key": coordination_key, "failed": failed})
                _incident(root, task, "checks", "mechanical checks failed: " + ", ".join(failed))
                outcomes.append({"task": task, "status": "checks_failed", "failed": failed})
                continue
            _journal(root, {"task": task, "commit": commit, "step": "prepared",
                            "coordination_key": coordination_key,
                            "checks_path": str(checks_path)})
            board.clear_control_plane_hold(root, task, "release_coordinator")
            routed = _route_prepared(
                root, state, task, commit, checks_path, coordination_key,
            )
            outcomes.append({
                "task": task, "status": "prepared", "checks": str(checks_path),
                "cto_routed": routed,
            })
        except InjectedCrash:
            raise
        except Exception as error:  # noqa: BLE001 — every failure is an incident, never silent
            _incident(root, task, "coordinate", repr(error))
            outcomes.append({"task": task, "status": "incident", "error": repr(error)})
    return outcomes


def coordinate(root: Path, crash_after: str = "", crash_before_journal: str = "") -> list[dict[str, Any]]:
    """Single-flight public coordinator entry used by startup, events, and timer."""
    with _coordination_lock(root):
        return _coordinate_locked(root, crash_after, crash_before_journal)

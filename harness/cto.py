#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""CTO checks for board hygiene and user-test readiness.

The CTO does not implement or QA work. It verifies that a task has a passed QA
cycle, a complete Scenario Ledger, a clean main checkout, and an explicit
release candidate before it may be presented to the product owner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (
    board, child_process, contract, execution_identity, git_broker, git_process,
    lifecycle, runtime_probe,
)
from harness.project_context import add_context_arguments, context_from_args, project_context


def _requests_for_task(state: dict[str, Any], task: str) -> list[dict[str, Any]]:
    active = list(state.get("qa_requests", {}).values())
    archived = [entry["value"] for entry in state.get("archive", []) if entry.get("kind") == "qa_request"]
    known = {value.get("id") for value in active + archived}
    indexed = [value for value in state.get("qa_request_index", {}).values() if value.get("id") not in known]
    return sorted([r for r in active + archived + indexed if r.get("task") == task], key=lambda r: (r.get("stage", board.DEVELOPMENT_QA), int(r["cycle"])))


def ledger_complete(path: Path) -> tuple[bool, list[str]]:
    """Use the board's PASS rule at the final CTO gate as well."""
    return contract.scenario_ledger_complete(path)


_SCOPE_STOPWORDS = {
    "about", "after", "also", "been", "before", "being", "from", "have",
    "into", "must", "only", "that", "their", "then", "this", "through",
    "with", "your", "task", "make", "sure", "will", "when", "where",
    "build", "create", "develop", "deliver", "implement", "provide", "ship",
    "support", "ensure", "allow", "need", "needs", "want", "wanted", "please",
    "review", "check", "verify", "test", "testing",
    # Scheduling/filler words describe emphasis or cadence, not a product
    # capability.  Treating them as scope made faithful PM paraphrases fail.
    "time", "times", "every", "each", "always",
}


def _scope_stem(term: str) -> str:
    """Return a conservative comparison stem for owner-scope words.

    This is deliberately not fuzzy matching: only common English plural and
    verb suffixes are normalized.  A terse confirmation such as "approved as
    discussed" may therefore still fail the audit; confirmations must retain
    the owner's actual capabilities.
    """
    value = term.lower()
    if len(value) > 4 and value.endswith("ies"):
        value = value[:-3] + "y"
    elif len(value) > 4 and value.endswith(("sses", "shes", "ches", "xes", "zes")):
        value = value[:-2]
    elif len(value) > 3 and value.endswith("s") and not value.endswith(("ss", "us", "is")):
        value = value[:-1]
    if len(value) > 5 and value.endswith("ing"):
        value = value[:-3]
        if len(value) > 2 and value[-1] == value[-2] and value[-1] not in "aeiou":
            value = value[:-1]
    elif len(value) > 4 and value.endswith("ied"):
        value = value[:-3] + "y"
    elif len(value) > 4 and value.endswith("ed"):
        value = value[:-2]
        if len(value) > 2 and value[-1] == value[-2] and value[-1] not in "aeiou":
            value = value[:-1]
    # Match silent-e verbs deterministically: create/created, use/used.
    if len(value) > 3 and value.endswith("e"):
        value = value[:-1]
    return value


_SCOPE_STOPWORD_STEMS = {_scope_stem(term) for term in _SCOPE_STOPWORDS}


def _scope_terms(text: str) -> set[str]:
    return {
        stem for term in re.findall(r"[a-z0-9]+", contract.normalize_owner_direction(text).lower())
        if len(term) > 3
        for stem in [_scope_stem(term)]
        if len(stem) > 2 and stem not in _SCOPE_STOPWORD_STEMS
    }


def _git_output(repo: Path, *args: str) -> str:
    result = git_process.run(["git", *args], cwd=repo, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def _status_paths(repo: Path) -> list[str]:
    result = git_process.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo, capture_output=True, text=True,
    )
    paths = []
    for line in result.stdout.splitlines() if result.returncode == 0 else []:
        if len(line) < 4:
            continue
        path = line[3:]
        paths.append(path.split(" -> ", 1)[-1])
    return paths


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    if not ancestor or not descendant:
        return False
    result = git_process.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo, capture_output=True, text=True,
    )
    return result.returncode == 0


def _changed_paths(repo: Path, base: str, candidate: str) -> list[str]:
    result = git_process.run(
        ["git", "diff", "--no-ext-diff", "--name-only", f"{base}..{candidate}"],
        cwd=repo, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return sorted(line for line in result.stdout.splitlines() if line)


def _runtime_verification_policy(
    *, broker_governed: bool, runtime_candidate_changed: bool,
) -> dict[str, bool]:
    """Keep candidate and deployed-runtime phases bound to the correct project."""
    gate_required = bool(runtime_candidate_changed and not broker_governed)
    return {
        "runtime_candidate_changed": bool(runtime_candidate_changed),
        "runtime_gate_required": gate_required,
        "runtime_verification_deferred_to_target_acceptance": bool(
            runtime_candidate_changed and broker_governed
        ),
        "runtime_verification_scope_correct": not (
            broker_governed and gate_required
        ),
    }


def _task_lineage(state: dict[str, Any], task: str, developers: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve replacement endpoints without treating historical sources as owners."""
    by_id = {agent["id"]: agent for agent in developers}
    superseded: set[str] = set()
    invalid: list[str] = []
    edges = list(state.get("task_lineage", {}).get(task, []))
    if not edges:
        edges = [event for event in state.get("events", []) if event.get("kind") == "task_resumed" and event.get("task") == task]
    for edge in edges:
        source_id = str(edge.get("source_agent_id", ""))
        target_id = str(edge.get("agent_id", ""))
        if not source_id or source_id not in by_id or target_id not in by_id or source_id == target_id:
            invalid.append(f"invalid replacement lineage {source_id}->{target_id}")
            continue
        superseded.add(source_id)
    endpoints = [agent for agent in developers if agent["id"] not in superseded]
    active = [agent for agent in developers if agent.get("active")]
    live_superseded = [agent["id"] for agent in active if agent["id"] in superseded]
    duplicate_active = len(active) > 1 or bool(live_superseded)
    return {
        "endpoints": endpoints,
        "superseded": sorted(superseded),
        "active": [agent["id"] for agent in active],
        "live_superseded": live_superseded,
        "duplicate_active": duplicate_active,
        "invalid": invalid,
    }


def _task_artifact_gate(root: Path, task: str, repo: Path, latest_review: dict[str, Any] | None, execute_health: bool, health_command: str, *, check_remote: bool = True) -> dict[str, Any]:
    """Verify the independently reviewed commit in a disposable Git checkout.

    The shared checkout may contain another task's work. Only files recorded in
    the reviewed task artifact are release-blocking; the tested artifact itself
    must still be the exact pushed commit and health runs with Git metadata.
    """
    artifact_started_at = lifecycle.now()
    reviewed_commit = str((latest_review or {}).get("reviewed_commit", ""))
    reviewed_files = sorted(set((latest_review or {}).get("reviewed_files", []) or []))
    repins = list((latest_review or {}).get("review_repins", []) or [])
    head = _git_output(repo, "rev-parse", "HEAD")
    branch = _git_output(repo, "branch", "--show-current")
    status_paths = _status_paths(repo)
    remote_line = _git_output(repo, "ls-remote", "origin", "refs/heads/main") if check_remote else ""
    remote_commit = remote_line.split()[0] if remote_line else ""
    exact_commit = bool(reviewed_commit and head and reviewed_commit == head)
    repin_verified = True
    if repins:
        repin = repins[-1]
        try:
            source_commit, source_tree = board._git_commit_and_tree(repo, str(repin.get("from_commit", "")))
            target_commit, target_tree = board._git_commit_and_tree(repo, str(repin.get("to_commit", "")))
            repin_verified = bool(
                repin.get("board_verified") is True
                and repin.get("verified_by")
                and source_commit == repin.get("from_commit")
                and target_commit == reviewed_commit == repin.get("to_commit")
                and source_tree == target_tree == repin.get("tree_hash")
            )
        except ValueError:
            repin_verified = False
    pushed = bool(head and remote_commit and head == remote_commit)
    uncommitted_task_files = sorted(set(status_paths).intersection(reviewed_files))
    archive_verified = False
    health_verified = False
    health_output = ""
    archive_error = ""
    health_measurement: dict[str, Any] = {}
    if exact_commit:
        try:
            with tempfile.TemporaryDirectory(prefix=f"harness-release-{task}-") as temporary:
                checkout = Path(temporary) / "checkout"
                broker = git_broker.GitBroker(
                    git_broker.context_for_repository(root, repo),
                )
                materialized = broker.materialize_readonly_candidate(
                    repo, reviewed_commit, checkout,
                )
                cloned = materialized["clone"]
                detached = materialized["checkout"]
                if not materialized["ok"]:
                    failed = detached or cloned
                    archive_error = failed.stderr.strip()[-1000:]
                else:
                    checkout_head = _git_output(checkout, "rev-parse", "HEAD")
                    checkout_tree = _git_output(checkout, "rev-parse", "HEAD^{tree}")
                    reviewed_tree = _git_output(repo, "rev-parse", f"{reviewed_commit}^{{tree}}")
                    archive_verified = bool(
                        checkout_head == reviewed_commit
                        and checkout_tree and checkout_tree == reviewed_tree
                        and not _status_paths(checkout)
                    )
                    if not archive_verified:
                        archive_error = "disposable Git checkout identity mismatch"
                    if execute_health and health_command:
                        health_started_at = lifecycle.now()
                        health = subprocess.run(
                            health_command, cwd=checkout, shell=True, capture_output=True,
                            text=True, env=child_process.execution_environment(),
                        )
                        health_output = (health.stdout + health.stderr)[-2000:]
                        health_verified = health.returncode == 0
                        health_measurement = lifecycle.command_measurement(
                            health_command, health_started_at, lifecycle.now(),
                            exit_code=health.returncode,
                        )
        except (OSError, git_broker.BrokerError) as error:
            archive_error = str(error)
    artifact_finished_at = lifecycle.now()
    return {
        "reviewed_commit": reviewed_commit,
        "reviewed_files": reviewed_files,
        "head_commit": head,
        "branch": branch,
        "remote_main_commit": remote_commit,
        "artifact_commit_exact": exact_commit,
        "review_repin_verified": repin_verified,
        "artifact_commit_pushed": pushed,
        "artifact_archive_verified": archive_verified,
        "artifact_health_verified": health_verified,
        "artifact_archive_error": archive_error,
        "artifact_health_output": health_output,
        "shared_worktree_dirty_files": status_paths,
        "uncommitted_task_artifact_files": uncommitted_task_files,
        "task_artifact_clean": not uncommitted_task_files,
        "task_artifact_release_verified": bool(exact_commit and repin_verified and (pushed or not check_remote) and archive_verified and not uncommitted_task_files),
        "lifecycle": {
            "artifact_materialization": lifecycle.phase(artifact_started_at, artifact_finished_at),
            **({"health": health_measurement} if health_measurement else {}),
        },
        "command_executions": [health_measurement] if health_measurement else [],
    }


def _clean_process_audit(root: Path, entry: dict[str, Any]) -> bool:
    manifest = (entry.get("metadata") or {}).get("process_audit") or {}
    path_value = str(manifest.get("path") or "")
    expected = str(manifest.get("sha256") or "")
    if not path_value or len(expected) != 64:
        return False
    try:
        path = Path(path_value).resolve()
        audit_root = (board.board_dir(root) / "execution-audits").resolve()
        if not path.is_relative_to(audit_root):
            return False
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected:
            return False
        audit = json.loads(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(audit, dict)
        and not audit.get("problems")
        and not audit.get("timed_out")
        and not audit.get("forbidden_owner_browser_descendants")
        and not audit.get("new_keychain_or_permission_prompts")
        and audit.get("default_handlers_unchanged") is True
    )


def _certified_delivery_health(
    root: Path, request: dict[str, Any] | None,
) -> dict[str, Any]:
    """Verify and reuse the exact final Delivery full-suite certification."""
    failed = {
        "verified": False, "source": "", "output": "", "measurement": {},
    }
    if not request or not (
        request.get("phase") == "final_acceptance"
        and request.get("test_scope") == "full"
        and request.get("delivery_state") == "passed"
        and request.get("status") == "passed"
    ):
        return failed
    command = str(request.get("unit_test_command") or "").strip()
    if not command:
        return failed
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return failed
    artifacts = request.get("certified_artifacts") or {}
    delivery_ledger = artifacts.get("delivery_ledger") or {}
    ledger_sha256 = str(request.get("ledger_sha256") or "")
    contract_sha256 = str((request.get("contract_revision") or {}).get("sha256") or "")
    environment_sha256 = str((request.get("environment_identity") or {}).get("sha256") or "")
    commit = str(request.get("reviewed_commit") or "")
    tree = str(request.get("reviewed_tree_hash") or "")
    try:
        certified_ledger = Path(str(delivery_ledger.get("path") or ""))
        if not (
            commit and tree and contract_sha256 and environment_sha256
            and delivery_ledger.get("sha256") == ledger_sha256
            and certified_ledger.is_file()
            and hashlib.sha256(certified_ledger.read_bytes()).hexdigest() == ledger_sha256
        ):
            return failed
    except OSError:
        return failed
    candidate = execution_identity.candidate_evidence_identity(
        commit, tree, contract_sha256,
        {
            "delivery_ledger": ledger_sha256,
            "review_scope": board._review_scope_identity(request)["sha256"],
        },
    )
    expected_gate = (
        f"{request.get('phase', '')}:{request.get('subtask', '')}:"
        f"{request.get('chunk', '')}"
    )
    unit_rows = [
        row for row in request.get("command_executions", [])
        if row.get("kind") == "unit_test" and int(row.get("exit_code", 1)) == 0
        and row.get("execution_identity") and row.get("execution_record_id")
    ]
    if len(unit_rows) != 1:
        return failed
    measurement = unit_rows[0]
    try:
        entry = execution_identity.certified_success(
            root, str(measurement["execution_identity"]),
            str(measurement["execution_record_id"]),
        )
        fields = entry["identity_fields"]
        if not (
            fields.get("candidate_sha256") == candidate["sha256"]
            and fields.get("argv") == argv
            and fields.get("cwd") == "."
            and fields.get("environment_sha256") == environment_sha256
            and fields.get("role") == "delivery"
            and fields.get("gate") == expected_gate
            and int(fields.get("policy_version", 0)) == execution_identity.POLICY_VERSION
            and _clean_process_audit(root, entry)
        ):
            return failed
        output = execution_identity.load_output(entry)
    except (KeyError, TypeError, ValueError):
        return failed
    counts = [
        int(value)
        for pair in re.findall(
            r"\bRan\s+(\d+)\s+tests?\b|\b(\d+)\s+passed\b", output, re.I,
        )
        for value in pair if value
    ]
    if not counts or max(counts) < 1:
        return failed
    return {
        "verified": True,
        "source": "certified_delivery_full_suite",
        "output": output[-2000:],
        "measurement": {
            **measurement,
            "cache_decision": "exact_success_reused_for_release_health",
            "kind": "release_health_reuse",
        },
    }


def _matching_certified_delivery_health(
    root: Path, request: dict[str, Any] | None, execute_health: bool,
    health_command: str,
) -> dict[str, Any]:
    """Reuse health only when the requested command is the certified command."""
    failed = {
        "verified": False, "source": "", "output": "", "measurement": {},
    }
    if not execute_health or not request:
        return failed
    requested = str(health_command or "").strip()
    certified_command = str(request.get("unit_test_command") or "").strip()
    if requested:
        try:
            if shlex.split(requested, posix=True) != shlex.split(
                certified_command, posix=True,
            ):
                return failed
        except ValueError:
            return failed
    return _certified_delivery_health(root, request)


def release_check(root: Path, task: str, ledger: Path, repo: Path, profile: dict | None = None, execute_health: bool = False, health_command: str = "") -> dict[str, Any]:
    release_check_started_at = lifecycle.now()
    state = board.snapshot(root)
    requests = _requests_for_task(state, task)
    development_qa = [request for request in requests if request.get("stage", board.DEVELOPMENT_QA) == board.DEVELOPMENT_QA]
    reviews = [request for request in requests if request.get("stage") == board.INDEPENDENT_REVIEW]
    latest_qa = max(development_qa, key=lambda request: int(request["cycle"]), default=None)
    task_chunks = state.get("task_chunks", {}).get(task, {})
    task_plan = state.get("delivery_plans", {}).get(task, {})
    requirements_confirmation = state.get("requirement_confirmations", {}).get(task, {})
    current_structure_revision = int(task_plan.get("structure_revision", 0))
    final_reviews = [
        request for request in reviews
        if request.get("phase", "legacy") in {"final_acceptance", "legacy"}
        and (not task_plan or int(request.get("structure_revision", 0)) == current_structure_revision)
    ]
    latest_review = max(final_reviews, key=lambda request: int(request["cycle"]), default=None)
    broker_governed = bool(latest_review and latest_review.get("mirror_ref") and state.get("task_repositories", {}).get(task))
    delivery_mode = task_plan.get("mode") or ("chunked" if task_chunks else "atomic")
    subtasks = task_plan.get("subtasks", {}) if delivery_mode == "application" else {}
    chunks_complete = bool(task_chunks) and all(chunk.get("status") == "passed" for chunk in task_chunks.values())
    subtasks_complete = bool(subtasks) and all(subtask.get("status") == "passed" for subtask in subtasks.values())
    nested_chunks_complete = all(
        all(chunk.get("status") == "passed" for chunk in subtask.get("chunks", {}).values())
        for subtask in subtasks.values()
    )
    structure_complete = (
        True if delivery_mode == "atomic"
        else chunks_complete if delivery_mode == "chunked"
        else subtasks_complete and nested_chunks_complete
    )
    plan_recorded = delivery_mode in board.DELIVERY_MODES and bool(task_plan.get("rationale"))
    chunk_flow = delivery_mode == "chunked"
    chunk_reviews = [request for request in reviews if request.get("phase") == "chunk"]
    latest_chunk_reviews = {
        chunk: max((request for request in chunk_reviews if request.get("chunk") == chunk), key=lambda request: int(request["cycle"]), default=None)
        for chunk in task_chunks
    }
    internal_chunk_proof = chunks_complete and all(
        request and request.get("status") == "passed" and request.get("delivery_evidence")
        for request in latest_chunk_reviews.values()
    )
    internal_final_proof = bool(latest_review and latest_review.get("delivery_evidence"))
    application_scope_reviews: list[dict[str, Any] | None] = []
    if delivery_mode == "application":
        for subtask_name, subtask in subtasks.items():
            for chunk_name in subtask.get("chunks", {}):
                application_scope_reviews.append(max(
                    (request for request in reviews if request.get("phase") == "chunk" and request.get("subtask") == subtask_name and request.get("chunk") == chunk_name),
                    key=lambda request: int(request["cycle"]), default=None,
                ))
            application_scope_reviews.append(max(
                (request for request in reviews if request.get("phase") == "subtask_acceptance" and request.get("subtask") == subtask_name),
                key=lambda request: int(request["cycle"]), default=None,
            ))
    application_internal_proof = bool(application_scope_reviews) and all(
        request and request.get("status") == "passed" and request.get("delivery_evidence")
        for request in application_scope_reviews
    )
    scoped_internal_proof = (
        True if delivery_mode == "atomic"
        else internal_chunk_proof if delivery_mode == "chunked"
        else application_internal_proof
    )
    development_proof = bool(latest_qa and latest_qa.get("status") == "passed" and latest_qa.get("evidence")) or (scoped_internal_proof and internal_final_proof)
    direct_unit_tests = bool(latest_review and latest_review.get("unit_test_command") and latest_review.get("unit_test_evidence")) and all(
        request and request.get("unit_test_command") and request.get("unit_test_evidence")
        for request in (latest_chunk_reviews.values() if delivery_mode == "chunked" else application_scope_reviews)
    )
    unit_tests_passed = direct_unit_tests
    checks: dict[str, Any] = {
        "requirements_confirmation_recorded": bool(requirements_confirmation.get("text")),
        "requirements_confirmation_scope_match": bool(requirements_confirmation.get("owner_direction")) and contract.normalize_owner_direction(requirements_confirmation.get("owner_direction", "")) == contract.normalize_owner_direction(board.owner_direction_for_task(state, requirements_confirmation.get("agent_id", ""), task)),
        "delivery_plan_recorded": plan_recorded,
        "product_structure_complete": structure_complete,
        "development_qa_passed": development_proof,
        "unit_tests_passed": unit_tests_passed,
        "delivery_mode": delivery_mode,
        "product_subtasks_complete": subtasks_complete if delivery_mode == "application" else True,
        "internal_chunk_qa_evidence_complete": internal_chunk_proof if chunk_flow else application_internal_proof if delivery_mode == "application" else internal_final_proof,
        "internal_final_qa_evidence_present": internal_final_proof,
        "latest_development_qa_request": latest_qa.get("id") if latest_qa else None,
        "independent_review_passed": bool(latest_review and latest_review.get("status") == "passed" and latest_review.get("evidence")),
        "latest_independent_review_request": latest_review.get("id") if latest_review else None,
        "final_acceptance_review_present": bool(latest_review),
        "delivery_chunks_complete": structure_complete,
    }
    developers = [
        agent for agent in state.get("agents", {}).values()
        if agent.get("task") == task and agent.get("role") in board.DEVELOPER_ROLES
    ]
    lineage = _task_lineage(state, task, developers)
    checks["delivery_lineage_endpoint_ids"] = [agent["id"] for agent in lineage["endpoints"]]
    checks["delivery_lineage_superseded_ids"] = lineage["superseded"]
    checks["delivery_lineage_invalid"] = lineage["invalid"]
    checks["live_duplicate_delivery_agents"] = lineage["live_superseded"] if lineage["duplicate_active"] else []
    checks["development_agents_complete"] = bool(lineage["endpoints"]) and not lineage["duplicate_active"] and not lineage["invalid"] and all(agent.get("status") == "done" for agent in lineage["endpoints"])
    checks["incomplete_development_agents"] = [agent["id"] for agent in lineage["endpoints"] if agent.get("status") != "done"] + lineage["live_superseded"] + lineage["invalid"]
    certified_delivery = Path(str((latest_review or {}).get("certified_artifacts", {}).get("delivery_ledger", {}).get("path", ""))) if latest_review else None
    ledger_for_gate = certified_delivery if certified_delivery and certified_delivery.is_file() else ledger
    ledger_ok, ledger_problems = ledger_complete(ledger_for_gate)
    checks["scenario_ledger_complete"] = ledger_ok
    checks["scenario_ledger_problems"] = ledger_problems
    challenge = Path(str(latest_review.get("certified_artifacts", {}).get("challenge_ledger", {}).get("path", ""))) if latest_review and latest_review.get("challenge_ledger") else None
    if challenge and not challenge.is_file() and latest_review and latest_review.get("challenge_ledger"):
        challenge = Path(latest_review["challenge_ledger"])
        if not challenge.is_absolute():
            challenge = project_context(root).code_root / challenge
    challenge_ok, challenge_problems = ledger_complete(challenge) if challenge else (False, ["independent reviewer challenge ledger missing"])
    checks["reviewer_challenge_ledger_complete"] = challenge_ok
    checks["reviewer_challenge_ledger_problems"] = challenge_problems
    delivery_simulations_ok, delivery_simulation_problems = (
        board.simulation_evidence_complete(root, latest_review, "delivery_simulations", "ledger")
        if latest_review else (False, ["final Delivery simulation evidence missing"])
    )
    reviewer_simulations_ok, reviewer_simulation_problems = (
        board.simulation_evidence_complete(root, latest_review, "reviewer_simulations", "challenge_ledger")
        if latest_review else (False, ["final reviewer simulation evidence missing"])
    )
    checks["delivery_scenario_simulations_executed"] = delivery_simulations_ok
    checks["delivery_scenario_simulation_problems"] = delivery_simulation_problems
    checks["reviewer_scenario_simulations_executed"] = reviewer_simulations_ok
    checks["reviewer_scenario_simulation_problems"] = reviewer_simulation_problems
    try:
        contract_ok, contract_problems, _ = contract.contract_complete(root, task)
    except (FileNotFoundError, json.JSONDecodeError):
        contract_ok, contract_problems = False, ["Completion Contract missing"]
    checks["completion_contract_complete"] = contract_ok
    checks["completion_contract_problems"] = contract_problems
    owner_directions = [
        board.owner_direction_for_task(state, agent["id"], task)
        for agent in developers
    ]
    owner_directions = [value for value in owner_directions if value]
    checks["owner_direction_recorded"] = bool(owner_directions)
    contract_objective = ""
    contract_value: dict[str, Any] = {}
    if contract_ok:
        try:
            contract_value = contract.contract_complete(root, task)[2]
            contract_objective = contract_value.get("objective", "")
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    confirmation = state.get("requirement_confirmations", {}).get(task, {})
    coverage = " ".join([
        contract_objective,
        str(confirmation.get("text", "")),
        " ".join(str(item.get("name", "")) + " " + str(item.get("acceptance_proof", "")) for item in contract_value.get("deliverables", [])),
    ])
    missing_terms = [_scope_terms(direction) - _scope_terms(coverage) for direction in owner_directions]
    missing = sorted(set().union(*missing_terms)) if missing_terms else []
    # Term overlap is never evidence (CTO directive; finding-57a0fe813e3ee545).
    # Word gaps are ADVISORY input to the CTO's artifact-traced audit — they can
    # neither pass nor block a release on their own. A lexical block has no
    # agent-executable repair (the direction is owner-authored), and it blocks
    # exactly the sanctioned pointer-style directive whose words are a file
    # path. The enforceable mechanical facts are: an owner direction exists,
    # and the final integrated acceptance — whose delivery-mode audit IS the
    # executed claim-scope audit — is present and passed.
    checks["claim_scope_missing_terms"] = missing
    checks["claim_scope_audit_passed"] = bool(owner_directions) and bool(
        checks.get("final_acceptance_review_present")
    )
    # A broker-governed candidate lives on its isolated task branch until the
    # owner accepts it.  The caller's ``repo`` is code_root/main and is
    # intentionally unchanged at this gate, so certify the board-derived task
    # workspace rather than accidentally inspecting main as the candidate.
    artifact_repo = board.task_workspace(root, task) if broker_governed else repo
    discovered_health = health_command or (profile or {}).get("health_command", "")
    certified_health = _matching_certified_delivery_health(
        root, latest_review, execute_health, discovered_health,
    )
    artifact = _task_artifact_gate(
        root, task, artifact_repo, latest_review,
        execute_health and not certified_health["verified"],
        discovered_health, check_remote=not broker_governed,
    )
    if (
        certified_health["verified"]
        and artifact.get("artifact_commit_exact")
        and artifact.get("artifact_archive_verified")
    ):
        artifact["artifact_health_verified"] = True
        artifact["artifact_health_output"] = certified_health["output"]
        artifact["artifact_health_source"] = certified_health["source"]
        artifact["command_executions"] = [certified_health["measurement"]]
    checks.update(artifact)
    runtime_paths = {
        "harness/board_viewer.py", "harness/global_settings.py",
        "harness/project_chat.py", "harness/project_manager.py",
        "harness/project_worker.py", "harness/runtime_identity.py",
        "harness/runtime_probe.py",
    }
    runtime_candidate_changed = bool(
        runtime_paths.intersection(artifact.get("reviewed_files", []))
    )
    # A broker candidate is still isolated from the target's live main at this
    # phase. Comparing it with the control-plane manager (often another repo and
    # port) is both false and impossible to satisfy. Its exact candidate tests
    # remain required; live-runtime identity is a post-acceptance target check.
    checks.update(_runtime_verification_policy(
        broker_governed=broker_governed,
        runtime_candidate_changed=runtime_candidate_changed,
    ))
    if checks["runtime_gate_required"]:
        checks.update(runtime_probe.verify(root, str(artifact.get("reviewed_commit", ""))))
    checks["git_broker_governed"] = broker_governed
    checks["main_branch"] = artifact.get("branch") == "main"
    checks["git_clean"] = artifact["task_artifact_clean"]
    checks["main_pushed"] = artifact["artifact_commit_pushed"] and artifact["artifact_commit_exact"]
    checks["main_health_verified"] = artifact["artifact_health_verified"]
    checks["main_health_output"] = artifact["artifact_health_output"]
    if broker_governed:
        repository = Path(state["task_repositories"][task]).resolve()
        main_commit = _git_output(repository, "rev-parse", "refs/heads/main")
        reviewed_commit = str(artifact.get("reviewed_commit", ""))
        fast_forward_safe = _is_ancestor(repository, main_commit, reviewed_commit)
        checks["candidate_branch"] = str(artifact.get("branch", "")).startswith("harness/tasks/")
        checks["acceptance_base_commit"] = main_commit
        checks["acceptance_manifest"] = (
            _changed_paths(repository, main_commit, reviewed_commit)
            if fast_forward_safe else []
        )
        checks["main_fast_forward_safe"] = fast_forward_safe
        # Compatibility field for old reports. It now reflects the enforceable
        # property rather than the obsolete task-start equality assumption.
        checks["main_unchanged_before_accept"] = fast_forward_safe
        checks["candidate_health_verified"] = artifact["artifact_health_verified"]
        mirror_verified = False
        try:
            mirror = project_context(root).storage_path("git-mirror")
            mirror_commit, mirror_tree = board._git_commit_and_tree(mirror, str(latest_review.get("mirror_ref", "")))
            mirror_verified = bool(
                mirror_commit == latest_review.get("reviewed_commit")
                and mirror_tree == latest_review.get("reviewed_tree_hash")
                and latest_review.get("mirror_commit") == mirror_commit
                and latest_review.get("mirror_tree_hash") == mirror_tree
            )
        except ValueError:
            mirror_verified = False
        checks["mirror_candidate_verified"] = mirror_verified
        required = {
            "requirements_confirmation_recorded", "requirements_confirmation_scope_match",
            "delivery_plan_recorded", "product_structure_complete", "development_qa_passed",
            "unit_tests_passed", "independent_review_passed", "final_acceptance_review_present",
            "delivery_chunks_complete", "scenario_ledger_complete",
            "reviewer_challenge_ledger_complete", "delivery_scenario_simulations_executed",
            "reviewer_scenario_simulations_executed", "completion_contract_complete",
            "owner_direction_recorded", "claim_scope_audit_passed", "development_agents_complete",
            "candidate_branch", "git_clean", "candidate_health_verified",
            "task_artifact_release_verified", "main_fast_forward_safe",
            "mirror_candidate_verified", "runtime_verification_scope_correct",
        }
    else:
        required = {"requirements_confirmation_recorded", "requirements_confirmation_scope_match", "delivery_plan_recorded", "product_structure_complete", "development_qa_passed", "unit_tests_passed", "independent_review_passed", "final_acceptance_review_present", "delivery_chunks_complete", "scenario_ledger_complete", "reviewer_challenge_ledger_complete", "delivery_scenario_simulations_executed", "reviewer_scenario_simulations_executed", "completion_contract_complete", "owner_direction_recorded", "claim_scope_audit_passed", "development_agents_complete", "main_branch", "git_clean", "main_pushed", "main_health_verified", "task_artifact_release_verified"}
    if checks.get("runtime_gate_required"):
        required |= {"deployed_runtime_verified", "deployed_chat_verified"}
    checks["ready_for_owner_test"] = all(checks.get(key) is True for key in required)
    artifact_lifecycle = checks.get("lifecycle", {}) if isinstance(checks.get("lifecycle"), dict) else {}
    checks["lifecycle"] = {
        **artifact_lifecycle,
        "release_checks": lifecycle.phase(release_check_started_at, lifecycle.now()),
    }
    checks["rule"] = "Only ready_for_owner_test=true may be presented for product-owner testing."
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dev Harness CTO checks")
    add_context_arguments(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("board-watch")
    clean = sub.add_parser("board-cleanup")
    clean.add_argument("--confirm", action="store_true", help="required: archives completed QA requests")
    gate = sub.add_parser("release-check")
    gate.add_argument("--task", required=True)
    gate.add_argument("--ledger", required=True)
    gate.add_argument("--repo", default="", help="candidate repository; defaults to the task repository recorded by the board")
    gate.add_argument("--profile")
    gate.add_argument("--health-command", default="", help="agent-discovered main health command; no profile required")
    gate.add_argument("--execute-health", action="store_true")
    gate.add_argument("--record-ready", action="store_true", help="record VISUAL_TEST_REQUIRED when every release check passes")
    gate.add_argument("--agent", default="", help="registered CTO agent ID required with --record-ready")
    args = parser.parse_args(argv)
    root = context_from_args(args)
    if args.command == "board-watch":
        out = {"updates_due": board.watch(root)}
    elif args.command == "board-cleanup":
        if not args.confirm:
            print("error: board cleanup requires --confirm", file=sys.stderr)
            return 2
        out = board.cleanup(root)
    else:
        ledger = Path(args.ledger)
        if not ledger.is_absolute():
            ledger = project_context(root).code_root / ledger
        profile = json.loads(Path(args.profile).read_text(encoding="utf-8")) if args.profile else None
        repository = Path(args.repo).resolve() if args.repo else board.task_workspace(root, args.task)
        out = release_check(root, args.task, ledger, repository, profile, args.execute_health, args.health_command)
        if args.record_ready:
            if not args.agent:
                print("error: --record-ready requires --agent", file=sys.stderr)
                return 2
            out["release_record"] = board.record_release_ready(root, args.agent, args.task, out)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

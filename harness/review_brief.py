# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Bounded, project-scoped independent-review context projection."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from harness.project_context import ProjectRoot, project_context
except ImportError:  # Legacy harness compatibility.
    from typing import Union
    from types import SimpleNamespace
    ProjectRoot = Union[Path, str]

    def project_context(value: ProjectRoot):
        code = Path(value).resolve()
        return SimpleNamespace(
            code_root=code, data_root=code / ".harness",
            workspace_root=code.parent / ".harness-task-workspaces",
        )


MAX_ITEMS = 40
MAX_TEXT = 4000


def _text(value: Any) -> str:
    return str(value or "").strip()[:MAX_TEXT]


def _bounded(values: list[Any]) -> list[Any]:
    return values[-MAX_ITEMS:]


def build(
    root: ProjectRoot, state: dict[str, Any], request: dict[str, Any],
    *, delivery_scenarios: list[dict[str, Any]] | None = None,
    include_delivery_evidence: bool = True,
) -> dict[str, Any]:
    task = _text(request.get("task"))
    if not task:
        raise ValueError("review brief requires an exact task")
    confirmation = (state.get("requirement_confirmations") or {}).get(task)
    if not isinstance(confirmation, dict) or not _text(confirmation.get("text")):
        raise ValueError("review brief requires the confirmed final requirements")
    task_requests = [
        value for value in (state.get("qa_requests") or {}).values()
        if value.get("task") == task and value.get("id") != request.get("id")
    ]
    narrowed = []
    for prior in task_requests + [request]:
        scope = _text(prior.get("test_scope") or "full")
        if scope != "full" and not _text(prior.get("scope_reason")):
            raise ValueError(
                f"review brief cannot certify narrowed scope for {prior.get('id', 'request')}: reason missing"
            )
        if scope != "full":
            narrowed.append({
                "request": _text(prior.get("id")), "scope": scope,
                "risk_reason": _text(prior.get("scope_reason")),
            })
    failures = [{
        "request": _text(value.get("id")), "scope": {
            "phase": _text(value.get("phase")), "subtask": _text(value.get("subtask")),
            "chunk": _text(value.get("chunk")),
        }, "finding": _text(value.get("result_summary")),
        "repair": _text(next((
            later.get("changes_summary") for later in task_requests
            if later.get("phase") == value.get("phase")
            and later.get("subtask", "") == value.get("subtask", "")
            and later.get("chunk", "") == value.get("chunk", "")
            and int(later.get("cycle", 0)) > int(value.get("cycle", 0))
        ), "")),
    } for value in task_requests if value.get("status") == "failed"]
    plan = (state.get("delivery_plans") or {}).get(task) or {}
    integrations = [{
        "request": _text(value.get("id")),
        "transaction": _text(value.get("integration_transaction_id") or value.get("mirror_transaction_id")),
        "accepted_byte_verification": value.get("accepted_byte_verification") or {},
        "accepted_byte_manifest_sha256": _text((value.get("accepted_byte_manifest") or {}).get("sha256")),
    } for value in task_requests if value.get("phase") == "subtask_acceptance" and value.get("status") == "passed"]
    if (state.get("task_repositories") or {}).get(task):
        missing = [row["request"] for row in integrations if not row["accepted_byte_manifest_sha256"]]
        if missing:
            raise ValueError("review brief is missing accepted-byte integration identity: " + ", ".join(missing))
    context = project_context(root)
    repair_context = request.get("repair_context") or {}
    current_reviewer = _text(
        request.get("reserved_by") or request.get("claimed_by") or request.get("routed_to")
    )
    prior_reviewer = _text(repair_context.get("prior_reviewer_id"))
    challenge_prefill = repair_context.get("challenge_prefill") or {}
    repair_authoring = None
    if repair_context:
        repair_authoring = {
            "mode": "repair_delta",
            "prior_request": _text(repair_context.get("prior_request_id")),
            "prior_blocking_summary": _text(repair_context.get("prior_blocking_summary")),
            "changed_paths": _bounded(list(repair_context.get("diff_files") or [])),
            "diff_available": bool(repair_context.get("diff_available")),
            "prior_challenge_ledger": _text(repair_context.get("prior_challenge_ledger")),
            "prior_challenge_ledger_sha256": _text(repair_context.get("prior_challenge_ledger_sha256")),
            "same_reviewer_may_reuse_own_wording": bool(
                current_reviewer and prior_reviewer and current_reviewer == prior_reviewer
            ),
            "prior_command_rows": _bounded(list(challenge_prefill.get("mechanical_rows") or [])),
            "prefill_unavailable_reasons": _bounded(list(challenge_prefill.get("unavailable_reasons") or [])),
            "requirements": [
                "Rerun every retained prior command against the new candidate.",
                "Add or escalate checks for the exact repair and changed paths.",
                "Run the complete suite again for final acceptance.",
                "Form a fresh semantic verdict from the new execution evidence.",
            ],
        }
    scenarios = []
    if include_delivery_evidence:
        outcomes = set((request.get("delivery_simulations") or {}).get("scenario_ids") or [])
        scenarios = [{
            "id": _text(row.get("id")),
            "what_was_tested": _text(row.get("what_was_tested") or row.get("expected_response")),
            "recorded": "executed" if row.get("id") in outcomes else "unavailable",
        } for row in (delivery_scenarios or [])]
    brief = {
        "version": 1, "task": task, "request_id": _text(request.get("id")),
        "requirements": {
            "text": _text(confirmation.get("text")),
            "confirmed_at": _text(confirmation.get("confirmed_at")),
            "version": confirmation.get("version", 1),
            "contract_revision": request.get("contract_revision") or {},
        },
        "risk_and_scope": {
            "required_review_scope": "complete full suite" if request.get("phase") == "final_acceptance" else _text(request.get("test_scope") or "full"),
            "narrowed_scope_decisions": _bounded(narrowed),
            "unresolved_findings": _bounded([{
                "title": _text(value.get("title")), "description": _text(value.get("description")),
            } for value in (state.get("deferred_findings") or {}).values()
                if value.get("task") == task and value.get("status") == "in_scope"]),
        },
        "owner_clarifications": _bounded([{
            "text": _text(value.get("text")), "at": _text(value.get("created_at")),
        } for value in (state.get("owner_clarifications") or {}).get(task, [])]),
        "prior_failures_and_repairs": _bounded(failures),
        "repair_package": request.get("repair_package"),
        "repair_authoring": repair_authoring,
        "product_structure": {
            "mode": _text(plan.get("mode")), "rationale": _text(plan.get("rationale")),
            "revision": int(plan.get("structure_revision", 0)),
            "subtasks": _bounded([{
                "id": _text(name), "title": _text(value.get("title")),
                "status": _text(value.get("status")),
            } for name, value in (plan.get("subtasks") or {}).items()]),
            "scope_changes": _bounded(list(plan.get("structure_changes") or [])),
        },
        "candidate": {
            "commit": _text(request.get("reviewed_commit")),
            "tree": _text(request.get("reviewed_tree_hash")),
            "changed_paths": _bounded(list(request.get("reviewed_files") or [])),
            "accepted_byte_manifest": request.get("accepted_byte_manifest") or {},
        },
        "integration_integrity": _bounded(integrations),
        "finalization_diff": request.get("finalization_diff"),
        "environment": request.get("environment_identity") or {},
        "isolation": {
            "code_root": str(context.code_root), "data_root": str(context.data_root),
            "workspace_root": str(context.workspace_root),
        },
        "reviewer_initial_intents": _bounded(list(request.get("reviewer_initial_intents") or [])),
        "delivery_evidence_withheld": not include_delivery_evidence,
        "delivery_scenarios": _bounded(scenarios),
        "reviewer_responsibility": (
            "Identify missing risks, author distinct adversarial checks, execute them, "
            "and record an independent semantic verdict."
        ),
    }
    encoded = json.dumps(brief, sort_keys=True, separators=(",", ":")).encode("utf-8")
    brief["sha256"] = hashlib.sha256(encoded).hexdigest()
    return brief

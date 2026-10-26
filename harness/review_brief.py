# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Bounded, project-scoped independent-review context projection."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from harness import reference_chapters, review_cost

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
    reduction = review_cost.reduce_candidate(
        context.code_root, request, list(request.get("reviewed_files") or []),
        context.storage_path("reviews", f"{_text(request.get('id')) or 'request'}.reading-list.json"),
    )
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
            # Owner 2026-10-14: findings the CTO set aside as not material to the
            # owner's objective, with its reason; the reviewer judges the
            # acceptance proof against this disposition, never repairs it.
            # Owner 2026-10-14: deliverables the owner waived (recorded by the CTO
            # from the owner's own words) are not required; the confirmed text
            # above is history and stays as it was.
            # Owner 2026-10-15: the documents the owner will read, frozen at this
            # request (path and sha256); the reviewer reads what the owner gets.
            "owner_documents": _bounded(list(request.get("owner_documents") or [])),
            # Owner 2026-10-15: a task that changes no product code is judged on its
            # attached proof files alone. When the mechanical check could not run, it
            # says so here and is never presented as verified proof.
            "no_product_code_evidence": {
                "applies": bool(request.get("no_product_code_task")),
                "check": _text(request.get("no_product_code_check")),
                "pinned_files": _bounded(list(request.get("no_product_code_evidence_pins") or [])),
                "pinned_total": len(request.get("no_product_code_evidence_pins") or []),
                "pinned_omitted": max(0, len(request.get("no_product_code_evidence_pins") or []) - MAX_ITEMS),
                "instruction": (
                    "These files are the only proof of this task. Read each one against its deliverable; "
                    "FAIL the review if a file reports a failed, missing or unsupported claim, even though the "
                    "harness confirmed the file is present and unchanged."
                ) if request.get("no_product_code_task") else "",
            },
            "owner_waived_deliverables": _bounded([{
                "deliverable": _text(value.get("deliverable")), "part": _text(value.get("part")),
                "owner_words": _text(value.get("owner_words")), "owner_message_at": _text(value.get("owner_message_at")),
                "recorded_at": _text(value.get("recorded_at")),
            } for value in (state.get("deliverable_waivers") or {}).get(task, [])]),
            "routed_around_findings": _bounded([{
                "title": _text(value.get("title")), "description": _text(value.get("description")),
                "reason": _text(value.get("routed_around_reason")),
            } for value in (state.get("deferred_findings") or {}).values()
                if value.get("task") == task and value.get("status") == "routed_around"]),
        },
        # Owner 2026-10-12: the enhanced-security choice frozen into this
        # request at its creation; the reviewer judges against exactly this.
        "enhanced_security": {
            "on": bool((request.get("enhanced_security") or {}).get("on", False)),
            "revision": int((request.get("enhanced_security") or {}).get("revision", 0) or 0),
        },
        # Directive diet: when Enhanced security is on for this review, the brief says which chapter holds what to check.
        **({"open_reference": reference_chapters.reference_hint("enhanced-security")} if (request.get("enhanced_security") or {}).get("on") else {}),
        "owner_clarifications": _bounded([{
            "text": _text(value.get("text")), "at": _text(value.get("created_at")),
        } for value in (state.get("owner_clarifications") or {}).get(task, [])]),
        "prior_failures_and_repairs": _bounded(failures),
        "repair_package": request.get("repair_package"),
        "repair_authoring": repair_authoring,
        # 2026-10-25: which round this is, the round-1 findings the scope is frozen on, and the stored round-1 checks.
        "frozen_scope": request.get("frozen_scope"),
        # Delivery changed these tests of the last judged version during the repair and said why; the verdict must name each one.
        "changed_tests": list(request.get("changed_tests") or []),
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
            # Task F PART 2: already-accepted bytes this subtask owns that its
            # base carried in a changed form. Certifying this candidate
            # certifies them too; the reviewer must know.
            "base_carried_accepted_paths": _bounded(list(
                ((plan.get("subtasks") or {}).get(_text(request.get("subtask")) or "") or {}).get("base_carried_accepted_paths") or []
            )),
        },
        # Review cost controls (2026-10-25): present only for a change above the owner's size limits in Settings.
        **({"input_reduction": reduction} if reduction else {}),
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
    if request.get("phase") == "final_acceptance":
        # Owner 2026-09-30: the final review ends with the whole app, end to
        # end, and a plain statement of whether the owner's task is done.
        brief["final_stage"] = {
            "end_to_end": (
                "Start the whole app from the candidate (its own ports and data, never the "
                "owner's running copy), walk the owner's path from start to finish on the "
                "running or rendered surface, and compare what happens with the expected "
                "result in the confirmed requirements. Make the walk a Challenge Ledger row."
            ),
            "verdict_first_line": "TASK DONE: YES|NO — <expected result> vs <what happened>",
            "rule": (
                "PASS always means TASK DONE: YES. Walk the whole path, not a fragment; do not "
                "hunt boundaries that never or only rarely happen - note them as non-blocking."
            ),
        }
        if brief["risk_and_scope"]["owner_waived_deliverables"]:
            brief["final_stage"]["owner_waivers"] = (
                "The owner waived the deliverables listed under risk_and_scope.owner_waived_deliverables "
                "(the owner's words and date are there). They are not required and take precedence over "
                "the confirmed requirements text, which stays unchanged as history: judge TASK DONE on the "
                "remaining deliverables and name the waiver in the verdict."
            )
    encoded = json.dumps(brief, sort_keys=True, separators=(",", ":")).encode("utf-8")
    brief["sha256"] = hashlib.sha256(encoded).hexdigest()
    return brief

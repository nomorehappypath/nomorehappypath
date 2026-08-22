# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Executable compatibility contract between dev_harness and harness_next."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable


EXACT_SHARED_PATHS = frozenset({
    "harness/__init__.py",
    "harness/certified_execution.py",
    "harness/directives/00_SPAWN_DEVELOPMENT_DIRECTIVE.md",
    "harness/directives/AUTONOMOUS_COMPLETION_DIRECTIVE.md",
    "harness/evidence_report.py",
    "harness/execution_identity.py",
    "harness/parity_audit.py",
    "harness/review_brief.py",
    "scripts/check_harness_parity.py",
})

# These surfaces implement the same control-plane responsibilities, but Next
# must adapt them to ProjectContext, the Git broker, authenticated storage, and
# its richer project lifecycle. Their regression behavior is audited below.
ADAPTED_SHARED_PATHS = {
    "harness/README.md": "Next documents its additional Projects-layer contracts.",
    "harness/accepted_bytes.py": "Next binds accepted bytes to broker-owned task branches.",
    "harness/board.py": "Next adds ProjectContext, broker, memory, and authenticated board behavior.",
    "harness/board_viewer.py": "Next adds project navigation, chat, and project-aware API surfaces.",
    "harness/browser_acceptance.py": "Next resolves browser evidence through project data roots.",
    "harness/contract.py": "Next validates owner-readable Delivery and Reviewer evidence.",
    "harness/control.py": "Next controls project-private sessions and authenticated workers.",
    "harness/cto.py": "Next monitors project-aware lifecycle and broker release state.",
    "harness/directives/CTO_COMPLETION_DIRECTIVE.md": "Next includes project and broker responsibilities.",
    "harness/execution_preflight.py": "Next validates commands against explicit code roots.",
    "harness/interactive_supervisor.py": "Next supervises project-private provider state.",
    "harness/lifecycle_metrics.py": "Next records additional project and broker lifecycle phases.",
    "harness/release_coordinator.py": "Next coordinates transactional broker acceptance.",
    "harness/repair_package.py": "Next binds repair packages to project review scopes.",
    "harness/scheduler.py": "Next schedules across registered projects.",
    "harness/timer.py": "Next uses project-scoped roots and wake channels.",
    "harness/watchdog.py": "Next reconciles project-private process state.",
    "harness/workspace_settings.py": "Next separates global and project settings.",
    "scripts/run_managed_agent.sh": "Next launches agents through project-private runtime boundaries.",
    "scripts/start_board_viewer.sh": "Next starts the Projects entry point and project worker.",
}

# A missing same-name test is allowed only when the stronger/adapted Next test
# named here exists. One Dev behavior intentionally changes: Next preserves CTO
# and Reviewer task affinity instead of rotating them merely because work is
# quiet, matching the owner's explicit continuity requirement.
TEST_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "tests/test_context_rotation.py::test_cto_rotation_requires_quiet_and_budget": (
        "tests/test_context_rotation.py::test_cto_is_preserved_across_tasks_and_quiet_periods",
    ),
    "tests/test_context_rotation.py::test_missing_reviewer_wakes_live_stale_cto_once_without_spawning": (
        "tests/test_context_rotation.py::test_open_review_signals_need_without_spawning",
    ),
    "tests/test_context_rotation.py::test_reviewer_retires_after_its_tasks_conclude": (
        "tests/test_context_rotation.py::test_reviewer_is_preserved_after_its_tasks_conclude",
    ),
    "tests/test_context_rotation.py::test_settled_reviewer_with_live_claim_is_not_reassigned": (
        "tests/test_context_rotation.py::test_reviewer_with_open_claim_is_untouchable",
    ),
    "tests/test_context_rotation.py::test_spawn_budget_caps_controller_launches": (
        "tests/test_context_rotation.py::test_context_compaction_never_uses_the_spawn_budget",
    ),
    "tests/test_cto_monitoring_lease.py::test_active_task_wakes_cto_only_after_five_real_poll_minutes": (
        "tests/test_cto_monitoring_lease.py::test_active_task_wakes_cto_only_when_poll_deadline_is_due",
    ),
    "tests/test_cto_monitoring_lease.py::test_open_review_without_active_delivery_still_counts_as_task_in_play": (
        "tests/test_cto_monitoring_lease.py::test_open_review_without_active_delivery_is_still_a_task_in_play",
    ),
    "tests/test_pending_tail_fixes.py::test_internal_qa_strips_git_env_and_records_provenance": (
        "tests/test_pending_tail_fixes.py::test_internal_qa_strips_git_env",
        "tests/test_evidence_reuse.py::test_environment_change_invalidates_saved_pass",
    ),
    "tests/test_remote_push_authorization.py::test_instruction_does_not_contact_or_mutate_remote_then_confirmation_pushes": (
        "tests/test_git_model_board.py::test_push_instruction_is_durable_without_network_contact",
        "tests/test_git_model_board.py::test_remote_push_requires_separate_instruction_and_immediate_confirmation",
    ),
    "tests/test_remote_push_authorization.py::test_remote_drift_is_refused_without_overwrite": (
        "tests/test_git_broker.py::test_guarded_push_uses_exact_accepted_commit_and_aborts_on_drift",
    ),
    "tests/test_remote_push_authorization.py::test_wrong_remote_local_drift_and_remote_retarget_fail_closed": (
        "tests/test_git_broker.py::test_guarded_push_uses_exact_accepted_commit_and_aborts_on_drift",
        "tests/test_git_model_board.py::test_remote_push_requires_separate_instruction_and_immediate_confirmation",
    ),
    "tests/test_repair_package.py::test_no_execution_before_resolution_and_strict_scope": (
        "tests/test_repair_package.py::test_explicit_members_close_before_execution_and_strictest_depth_is_binding",
    ),
    "tests/test_repair_package.py::test_only_source_reviewer_can_split_and_depth_is_recomputed": (
        "tests/test_repair_package.py::test_only_source_reviewer_can_split_and_each_child_recomputes_depth",
    ),
    "tests/test_repair_package.py::test_split_reapplies_strict_depth_and_cannot_drop_members": (
        "tests/test_repair_package.py::test_split_reapplies_depth_and_cannot_drop_or_duplicate_members",
    ),
    "tests/test_repair_package.py::test_unclassified_failure_fails_safe_to_full_scope": (
        "tests/test_repair_package.py::test_unclassified_legacy_failure_fails_safe_to_full_scope",
    ),
    "tests/test_staged_review_authoring.py::test_delivery_failure_cancels_staged_review_without_reviewer_execution": (
        "tests/test_staged_review_authoring.py::test_delivery_failure_cancels_execution_and_archives_staged_review",
    ),
    "tests/test_staged_review_authoring.py::test_expired_intents_never_transfer_to_replacement_reviewer": (
        "tests/test_staged_review_authoring.py::test_expired_authoring_does_not_transfer_one_reviewers_intents_to_another",
    ),
    "tests/test_staged_review_authoring.py::test_reviewer_authors_before_delivery_finishes_without_seeing_evidence": (
        "tests/test_staged_review_authoring.py::test_reviewer_authors_before_delivery_finishes_without_seeing_delivery_evidence",
    ),
    "tests/test_subtask_pipelining.py::test_crash_after_patch_staging_recovers_exact_integration": (
        "tests/test_subtask_pipelining.py::test_fold_crash_after_intent_recovers_without_losing_the_verdict",
        "tests/test_subtask_pipelining.py::test_fold_crash_after_git_mutation_recovers_without_duplicate_commit",
        "tests/test_subtask_pipelining.py::test_fold_crash_after_board_mutation_recovers_without_duplicate_commit",
    ),
    "tests/test_subtask_pipelining.py::test_disjoint_reviewed_subtasks_both_integrate_into_one_task_history": (
        "tests/test_subtask_pipelining.py::test_disjoint_reviewed_subtasks_both_fold_into_one_task_history",
    ),
    "tests/test_subtask_pipelining.py::test_final_acceptance_refuses_outstanding_subtask_before_full_suite": (
        "tests/test_subtask_pipelining.py::test_final_acceptance_refuses_any_outstanding_subtask_verdict",
    ),
    "tests/test_subtask_pipelining.py::test_passed_subtask_integrates_exact_owned_bytes_into_task_workspace": (
        "tests/test_subtask_pipelining.py::test_passed_subtask_is_folded_exactly_into_the_task_branch",
    ),
    "tests/test_subtask_pipelining.py::test_review_manifest_cannot_cross_declared_path_ownership": (
        "tests/test_subtask_pipelining.py::test_broker_commit_cannot_cross_declared_path_ownership",
    ),
}


def production_files(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for base in ("harness", "scripts"):
        for path in (root / base).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".md", ".sh"}:
                continue
            result[str(path.relative_to(root))] = path.read_bytes()
    return result


def test_methods(root: Path) -> set[str]:
    result: set[str] = set()
    for path in sorted((root / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                result.add(f"tests/{path.name}::{node.name}")
    return result


def audit_inventories(
    dev_files: dict[str, bytes], next_files: dict[str, bytes],
    dev_tests: set[str], next_tests: set[str],
    *, exact_paths: Iterable[str] = EXACT_SHARED_PATHS,
    adapted_paths: Iterable[str] = ADAPTED_SHARED_PATHS,
    equivalents: dict[str, tuple[str, ...]] = TEST_EQUIVALENTS,
) -> list[str]:
    problems: list[str] = []
    exact, adapted = set(exact_paths), set(adapted_paths)
    for path in sorted(dev_files):
        if path not in next_files:
            problems.append(f"Next is missing Dev production file: {path}")
        elif path not in exact and path not in adapted:
            problems.append(f"Shared production file has no parity policy: {path}")
    for path in sorted(exact):
        if path not in dev_files or path not in next_files:
            problems.append(f"Required byte-identical file is absent: {path}")
        elif dev_files[path] != next_files[path]:
            problems.append(f"Required byte-identical file differs: {path}")
    for path in sorted(adapted):
        if path not in dev_files or path not in next_files:
            problems.append(f"Documented adapted surface is absent: {path}")
    for source in sorted(dev_tests - next_tests):
        targets = equivalents.get(source)
        if not targets:
            problems.append(f"Dev regression test has no Next equivalent: {source}")
            continue
        for target in targets:
            if target not in next_tests:
                problems.append(f"Documented Next regression test is absent: {source} -> {target}")
    for stale in sorted(set(equivalents) - (dev_tests - next_tests)):
        problems.append(f"Stale test-equivalence entry should be removed: {stale}")
    return problems


def audit_roots(dev_root: Path, next_root: Path) -> list[str]:
    return audit_inventories(
        production_files(dev_root.resolve()), production_files(next_root.resolve()),
        test_methods(dev_root.resolve()), test_methods(next_root.resolve()),
    )

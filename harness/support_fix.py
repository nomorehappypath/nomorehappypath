# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""One repair attempt on a replicated case (Task 2/3 of the Dedicated Support
NMHP): a `support/<case>` branch from the reported version, a real repair by
Support's Delivery agent, Support's own failing-before / passing-after check
with the owner's reproduce command, then the independent other-vendor review
every NMHP task gets. A fix is never proposed (never pushed, never opened as a
pull request) unless all of that passed.

Review round 3, finding B1: a plan command is never a repair and never a
review here. The repair is whatever the repair agent actually changed in the
working tree, committed by Support; the review is the other vendor's verdict.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from harness import support_github, support_repair, support_replicate

RepairFn = Callable[[Path, str, float], support_repair.AgentRun]
ReviewFn = Callable[[Path, str], support_repair.AgentRun]

PLAN_FILE = "SUPPORT_PLAN.md"


@dataclass
class FixResult:
    fixed: bool
    notes: str


def attempt_fix(
    checkout: Path, case: dict, base_ref: str, *, budget_usd: float,
    run_repair: RepairFn = support_repair.run_repair, run_review: ReviewFn = support_repair.run_review,
) -> FixResult:
    """Make one repair attempt for a case on checkout, checked out at base_ref.

    Args:
        checkout: the app's checked-out working copy, at the reported version.
        case: the case row (its fingerprint names the support/<fingerprint> branch; its description,
            version and trace go into the agents' briefs).
        base_ref: the reported version (tag/commit) the branch is cut from.
        budget_usd: the owner's Support spending ceiling, the most the repair agent may spend here.
        run_repair: starts the Delivery repair agent in the checkout (support_repair.run_repair; a
            test's fake stands in for the vendor tool itself, never for this function's checks).
        run_review: starts the independent other-vendor reviewer (support_repair.run_review).

    Return: a FixResult. `fixed` is True only when, in this order: the owner's reproduce command
    failed before the repair, the repair agent ran and changed the app (a real commit beyond
    base_ref, with SUPPORT_PLAN.md untouched), that same reproduce command passes after it, and
    the other vendor's reviewer answered PASS. Every other outcome is Not fixed, with the reason.
    """
    case_id = str(case["fingerprint"])
    branch = f"support/{case_id}"
    try:
        support_github.create_branch_from(checkout, branch, base_ref)
    except support_github.GitCheckoutFailed as error:
        return FixResult(False, f"could not start the repair branch: {error}")

    # The owner's plan as it was at the reported version — never the working tree's copy, which a
    # repair could have rewritten to make its own check pass.
    reproduce = support_replicate.reproduce_command(support_github.file_at(checkout, base_ref, PLAN_FILE))
    if not reproduce:
        return FixResult(False, f"{PLAN_FILE} at {base_ref!r} names no reproduce command, so a repair has nothing to prove itself against")
    before_status, before_line = support_replicate.run_plan_command(checkout, reproduce)
    if before_status == 0:
        return FixResult(False, f"the problem did not happen when the repair started ({before_line}), so there was nothing to fix here")

    repair = run_repair(checkout, support_repair.repair_brief(case, reproduce), budget_usd)
    if not repair.started:
        return FixResult(False, f"no repair was attempted: {repair.notes}")
    committed = support_github.commit_all_changes(checkout, f"Support fix for case {case_id}")
    if not repair.ok:
        return FixResult(False, repair.notes + ("; its partial changes were kept on the branch for inspection" if committed else ""))
    if not committed or not support_github.branch_has_changes(checkout, base_ref):
        return FixResult(False, f"the repair agent finished but changed nothing in the app, so branch {branch!r} is still {base_ref!r}")
    if PLAN_FILE in support_github.changed_paths(checkout, base_ref):
        return FixResult(False, f"the repair changed {PLAN_FILE}, which is the owner's plan and not the agent's to change, so it is refused")

    after_status, after_line = support_replicate.run_plan_command(checkout, reproduce)
    checks = f"failing before ({before_line}); after the repair: {after_line}"
    if after_status != 0:
        return FixResult(False, f"the problem still happens after the repair — {checks}")

    diff = support_github.diff_text(checkout, base_ref)
    review = run_review(checkout, support_repair.review_brief(case, reproduce, checks, diff))
    if not review.ok:
        return FixResult(False, f"{review.notes}; {checks}")
    return FixResult(True, f"{repair.notes}; {checks}; {review.notes}")

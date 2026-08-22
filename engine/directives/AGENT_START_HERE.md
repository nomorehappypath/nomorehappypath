<!-- engine directive (portable). Project specifics come from the profile. Source: AGENT_START_HERE.md -->
# Agent Start Here — {{PROFILE:product_name}}

This is the short operating entrypoint for the implementer, the independent reviewer, the CTO Watchtower, or any future AI development agent working on the {{PROFILE:product_name}} repo(s).

Give the agent `00_AGENT_SPAWN_DIRECTIVE.md` when starting a new session. This file is the repo-local checklist that the agent must then execute.

## Canonical QA Rule

This pack does not replace `QA-DIRECTIVE.md`. `QA-DIRECTIVE.md` remains the canonical quality standard. If this pack is shorter or less specific than `QA-DIRECTIVE.md`, the QA directive wins.

## Branching & Merge Policy (Mandatory)

All development work is performed on a short-lived per-task branch named `task/<task-id>` (matching the task record slug), cut from current `main`. Long-lived feature branches and parallel topic branches are **prohibited**. Working directly on `main` is also **prohibited**: it produces a dirty shared working tree and prevents clean, scoped review.

Every task branch **must** be merged back into `main` as part of the task lifecycle. This is not optional and is never {{PROFILE:product_owner}}'s responsibility to trigger. A task may not reach `ACCEPTANCE_READY` until its branch is merged to `main` and the merge commit SHA is recorded in the task record.

**Start of every editing session (including startup and long-session reset):**

```bash
git checkout main
git pull --ff-only origin main
git checkout -b task/<task-id>     # or: git checkout task/<task-id> to resume
git branch --show-current          # must output: task/<task-id>
git status --short                 # must be clean before editing
```

**Merge to main (the implementer runs this at `REVIEW_PASSED`, before `ACCEPTANCE_READY`):**

```bash
bash scripts/agent_coord.sh merge --agent <agent> --task <task-id>
```

This lands the branch on `main` with a `--no-ff` merge, pushes if a remote is configured, deletes the task branch, and records the merge commit SHA in the task record. Then prove it landed before advancing:

```bash
bash scripts/agent_coord.sh verify-merge --task <task-id>     # must print VERIFIED; nonzero blocks ACCEPTANCE_READY
```

A task may not reach `ACCEPTANCE_READY` until `verify-merge` passes. If {{PROFILE:product_owner}} later rejects the result, revert cleanly with `git revert -m 1 <merge-sha>` and fix forward on a new task branch.

## Repos

The active repo(s) for this project: {{PROFILE:repos}}

A task may touch one repo or both. If both are touched, perform startup, status checks, locking, testing, and handoff in both.

## Same noteboard stays in use

## CTO Watchtower compatibility

This workflow now includes an optional but recommended CTO Watchtower role. The CTO watches the same `.agents/` board and `.agents/tasks/` records and may open blocking `CTO HOLD` files under `.agents/cto/holds/`.

An unresolved `CTO HOLD` blocks `REVIEW_PASSED`, merge, and `ACCEPTANCE_READY`. The CTO role does not replace the normal independent reviewer.


This workflow keeps the existing `.agents/` coordination board. The task record adds durable QA/evidence tracking; it does not replace locks or inbox notes.

- Use the noteboard for live coordination: who is editing what, overlap conflicts, and short notes between agents.
- Use the task record for durable delivery state: acceptance criteria, evidence, review result, and {{PROFILE:product_owner}} acceptance.


## Read order

Read these before editing:

1. `AGENT_START_HERE.md`
2. `AGENT_DELIVERY_CONTRACT.md`
3. `AGENT_REVIEW_PROTOCOL.md`
4. `CTO_WATCHTOWER_DIRECTIVE.md`
5. `QA-DIRECTIVE.md`
6. `QA_ENVIRONMENT_MATRIX.md`
7. Active repo `CONTEXT.md`
8. Routed `context/*.md` and `docs/specs/*.md` files
9. Active `.agents/tasks/<task>.md`, or create one from `AGENT_TASK_RECORD_TEMPLATE.md`

`CONTEXT.md` is a router, not a memory file. Use it to find the correct context/spec anchors. Every task must cite the specific context/spec file and section it is grounded in.

## Agent identity

Start every session by printing:

```text
AGENT=<implementer-vendor|reviewer-vendor|other>
ROLE=<implementer|reviewer|planner|cto_watchtower>
REPO=<active repo, or both>
TASK_ID=<task id or needs-created>
STATUS=<current status>
LOCK=<none|lock id/files>
```

This prevents multiple CLI windows from becoming indistinguishable, especially when one window is supervising as CTO Watchtower.

## Required startup

## If your role is `cto_watchtower`

Do not implement app code. Your routine job is read-only governance plus CTO reports/holds.

1. Read `CTO_WATCHTOWER_DIRECTIVE.md`.
2. Run the watchdog when available:
   ```bash
   bash scripts/cto_watch.sh --repo .
   ```
3. Inspect `.agents/tasks/`, `.agents/cto/holds/`, branch status, and board status.
4. Write or update `.agents/cto/reports/<timestamp>_cto_watch.md`.
5. If a blocker exists, create/update `.agents/cto/holds/<task-id>.md` with `Status: OPEN` and leave a board note to the responsible agent.
6. If a blocker is corrected with evidence, change the hold to `Status: RESOLVED`, cite the evidence, and leave a board note.
7. Do not mark tasks `DONE` and do not perform independent review unless {{PROFILE:product_owner}} explicitly reassigns you as reviewer.


1. Identify the repo(s) touched.
1.5 Enforce Implementation Ownership and Completeness Policy (see section below).
1.6 Enforce Universal Deployment Parity Policy (see section below).
1.7 Enforce Root-Cause Analysis and Offensive QA Policy (see section above).
2. Run `git status --short` in each touched repo.
3. Read the repo `CONTEXT.md` router.
4. Read the routed `context/*.md` or `docs/specs/*.md` files relevant to the task.
5. Create or update a task record under `.agents/tasks/`.
6. Write acceptance criteria in Given/When/Then form before code.
7. Identify triggered QA sections and deployment-channel coverage ({{PROFILE:deployment_channels}}).
8. Check the coordination board:
   ```bash
   bash scripts/agent_coord.sh status --agent <agent>
   ```
9. Before editing, create a lock:
   ```bash
   bash scripts/agent_coord.sh lock --agent <agent> --task "<task id>" --files <paths...>
   ```

Do not edit files until the task record has: user request, context anchors, acceptance criteria, expected files, QA sections, required tests, and current status.

## What the coordination board means

- A lock means another agent is editing those files now.
- If another active lock overlaps your intended files, stop and report the conflict unless {{PROFILE:product_owner}} explicitly authorizes proceeding.
- The agent who creates a lock clears it.
- Inbox notes are read and cleared/archived by the recipient.
- Coordination notes are not product memory and not QA evidence.

## Non-negotiables

- {{PROFILE:product_owner}} is not technical QA. Agents do technical diagnosis, tests, verification, evidence, and review.
- Preserve {{PROFILE:product_owner}}'s and other agents' uncommitted work.
- Do not overwrite unrelated changes.
- Do not invent requirements or UI behavior.
- Do not claim UI behavior without live browser, DOM, screenshot, or automation evidence.
- Do not claim deployment-channel behavior without environment-specific evidence (one evidence set per channel in {{PROFILE:deployment_channels}}).
- Tests are required for code changes.
- Every bug fix gets a permanent regression test.
- Product behavior changes require context/spec/help documentation updates where applicable.
- Use repo lifecycle scripts ({{PROFILE:lifecycle_scripts}}) before manual process control.
- Shell scripts should be thin wrappers; application/test logic owns branching/test logic.
- Never treat a smoke test as proof of correctness.
- Work on a short-lived `task/<task-id>` branch and merge it to `main` before acceptance; never leave finished work stranded on a branch.
- Never say `DONE` unless {{PROFILE:product_owner}} has accepted.

## Implementation Ownership and Completeness Policy (Mandatory)

When {{PROFILE:product_owner}} assigns a task, the agent is fully responsible for delivering a **complete, end-to-end, production-quality solution** unless the task explicitly states otherwise.

## Universal Deployment Parity Policy (Mandatory)

Every feature, change, or fix implemented by agents **must** be built, tested, and verified to work correctly in **every** deployment channel the project ships ({{PROFILE:deployment_channels}}), unless {{PROFILE:product_owner}} explicitly states in the task assignment that support for one channel may be omitted.

## Root-Cause Analysis and Offensive QA Policy (Mandatory)

For every bug or defect, the agent **must** perform and document **root-cause analysis** before implementing any fix. Agents are prohibited from addressing only the visible symptom.

**Core obligations:**
- Investigate and identify the underlying cause that created the bug (code path, architectural assumption, missing guard, race condition, data-flow error, configuration drift, etc.).
- Design and implement a **fundamental fix** that eliminates the root cause entirely, not merely masks the symptom.
- Add or update regression tests that specifically target the root cause (not just the original failing symptom).
- Shift QA from defensive (confirm symptom is gone) to offensive (prove the root cause is resolved and cannot recur under any supported condition, environment, or edge case).
- Document the root cause, the exact investigation performed, the chosen fix rationale, and why the fix is systemic in the task record.

**Prohibited behaviors (automatic `REVIEW_FAILED`):**
- Symptom-only patches (“this makes the error go away”).
- Fixes that rely on Q&A/testing without root-cause verification.
- Any claim that “the bug is fixed” without evidence of root-cause elimination.

This policy applies to all bug reports, regression failures, and production issues.


**Core obligations:**
- Design and implement the solution with every deployment channel in mind from the outset (shared backend where applicable, client-side differences per channel, installer behavior, API base URL handling, heartbeat/entitlement checks, offline grace, etc.).
- Never defer any deployment channel “for later” or mark it N/A without {{PROFILE:product_owner}}'s explicit approval.
- Update any affected installer, client startup, local-serving, or configuration logic as needed.
- Document in the task record exactly how parity was achieved and provide required evidence for every channel per `QA_ENVIRONMENT_MATRIX.md`.
- Treating one channel as secondary or out-of-scope is prohibited and constitutes an automatic `REVIEW_FAILED` finding.

**Core obligations:**
- Conduct comprehensive research and investigation to identify the best technical approach. Never default to manual workarounds, user-side processes, or partial implementations.
- Own the entire solution: diagnosis, design, implementation, testing, evidence, documentation, deployment verification, and any necessary supporting changes (migrations, configuration, UI, error handling, etc.).
- A task is not complete until **every** acceptance criterion is fully satisfied and verified with evidence.
- Partial completion, half-baked solutions, or proposals for manual user actions are prohibited and constitute an automatic `REVIEW_FAILED` finding.

**Required behavior before claiming progress:**
- If the optimal solution requires additional work beyond the most obvious path, perform that work.
- Document the research performed and the rationale for the chosen approach in the task record.
- Never respond with “this would require a manual step for users” unless {{PROFILE:product_owner}} has previously approved that limitation.


## Status vocabulary

Use only these statuses:

- `PLANNED`
- `IN_PROGRESS`
- `IMPLEMENTED_NOT_VERIFIED`
- `SELF_TESTED`
- `REVIEW_REQUESTED`
- `REVIEW_FAILED`
- `REVIEW_PASSED`
- `ACCEPTANCE_READY`
- `ACCEPTED_BY_OWNER`
- `DONE`

The implementer may only advance through `REVIEW_REQUESTED`. The independent reviewer — which must be a DIFFERENT vendor than the implementer (never same-model self-review; for an illustrated pairing, {{PROFILE:implementer_vendor}} ⇄ {{PROFILE:reviewer_vendor}}) — may return `REVIEW_PASSED` or `REVIEW_FAILED`. After `REVIEW_PASSED`, the implementer merges the task branch to `main`, records the merge commit SHA, and sets `ACCEPTANCE_READY` (Branching & Merge Policy). {{PROFILE:product_owner}} alone authorizes `ACCEPTED_BY_OWNER` and final `DONE`.

## Finish ritual for implementers

## CTO pre-review check

Before setting `REVIEW_REQUESTED`, run the CTO watchdog if it exists:

```bash
bash scripts/cto_watch.sh --mode review --repo . --task <task-id>
```

If it reports a blocking failure or an open `CTO HOLD`, do not request review. Fix the blocker, update the task record evidence, then rerun the check.


Before ending an implementation session:

1. Inspect `git status --short` and `git diff --stat`.
2. Run required tests/checks ({{PROFILE:test_command}}, {{PROFILE:build_command}}, {{PROFILE:lint_command}}, {{PROFILE:migrate_command}} as applicable) and paste exact output into the task record.
3. Add/update regression tests for every bug fixed.
4. Verify applicable behavior across every deployment channel per `QA_ENVIRONMENT_MATRIX.md` ({{PROFILE:deployment_channels}}).
5. Update context/spec/help docs if behavior changed.
6. Update the task record status.
7. If implementation is ready, set `REVIEW_REQUESTED`, not `DONE`.
8. Output the exact reviewer handoff prompt from `AGENT_REVIEW_PROTOCOL.md`.
9. Leave inbox notes for affected agents if needed.
10. Clear your lock only after the task record contains current state and next step:
    ```bash
    bash scripts/agent_coord.sh unlock --agent <agent>
    ```

## After an independent reviewer returns `REVIEW_PASSED`

The implementer (the owner of the task branch) runs `bash scripts/agent_coord.sh merge --agent <agent> --task <task-id>` to land the branch on `main` (it pushes, deletes the branch, and records the merge SHA), then `bash scripts/agent_coord.sh verify-merge --task <task-id>` and confirms it prints VERIFIED, before setting `ACCEPTANCE_READY` and letting {{PROFILE:product_owner}} test on `main`. See the Branching & Merge Policy.

## Finish ritual for reviewers

The reviewer does not rubber-stamp and does not rely on the implementer's claim. The reviewer must be a DIFFERENT vendor than the implementer (never same-model self-review); if no different-vendor reviewer is available, the degraded fallback is `LIMITED_SELF_REVIEW`, which must be declared as such. The reviewer independently inspects the task record, diff, evidence, context/spec anchors, and applicable QA/environment rules.

The reviewer returns only:

- `REVIEW_PASSED`
- `REVIEW_FAILED`

The reviewer must return `REVIEW_FAILED` if the task has any unresolved `CTO HOLD`.

`REVIEW_PASSED` means ready for {{PROFILE:product_owner}} acceptance testing. It does not mean done.

## Long-session reset

At the start of any resumed or multi-day task:

1. Read the task record.
2. Run `git status --short`.
3. Run `git diff --stat`.
4. Check the coordination board.
5. Re-read context/spec anchors listed in the task record.
6. Summarize current status, files changed, evidence produced, evidence missing, and the next concrete step.

Do not continue from chat memory when repo state, task record, specs, or tests disagree.

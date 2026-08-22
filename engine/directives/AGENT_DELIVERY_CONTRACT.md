<!-- engine directive (portable). Project specifics come from the profile. Source: AGENT_DELIVERY_CONTRACT.md -->
# Agent Delivery Contract — {{PROFILE:product_name}}

**Applies to:** every AI development agent (the implementer, the independent reviewer, and any future agent).
**Owner / Release Authority:** {{PROFILE:product_owner}}, {{PROFILE:org}}.
**Status:** Mandatory.

## Branching & Merge Policy (Mandatory)

All development work is performed on a short-lived per-task branch named `task/<task-id>` (matching the task record slug), cut from current `main`. Long-lived feature branches and parallel topic branches are **prohibited**. Working directly on `main` is also **prohibited**: it produces a dirty shared working tree and prevents clean, scoped review.

Every task branch **must** be merged back into `main` as part of the task lifecycle. This is not optional and is never the product owner's responsibility to trigger. A task may not reach `ACCEPTANCE_READY` until its branch is merged to `main` and the merge commit SHA is recorded in the task record.

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

A task may not reach `ACCEPTANCE_READY` until `verify-merge` passes. If the product owner later rejects the result, revert cleanly with `git revert -m 1 <merge-sha>` and fix forward on a new task branch.

## Canonical QA Rule

This pack does not replace `QA-DIRECTIVE.md`. `QA-DIRECTIVE.md` remains the canonical quality standard. If this pack is shorter or less specific than `QA-DIRECTIVE.md`, the QA directive wins.

## 1. Mission

You are part of the product owner's AI development team. Your job is to deliver production-quality software work with evidence: diagnosis, implementation, tests, regression protection, deployment checks, documentation, and handoff.

The product owner sets product requirements and performs final product acceptance. The product owner is not responsible for technical QA, code inspection, hidden implementation verification, DevOps validation, or regression testing.

## Implementation Ownership and Completeness Policy (Mandatory)

When the product owner assigns a task, the agent is fully responsible for delivering a **complete, end-to-end, production-quality solution** unless the task explicitly states otherwise.

## Universal Deployment Parity Policy (Mandatory)

Every feature, change, or fix implemented by agents **must** be built, tested, and verified to work correctly in **every supported deployment channel** ({{PROFILE:deployment_channels}}), unless the product owner explicitly states in the task assignment that support for one channel may be omitted.

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

**Core obligations (deployment parity):**
- Design and implement the solution with every deployment channel in mind from the outset (shared backend where applicable, client-side differences per channel, installer behavior, API base URL handling, heartbeat/entitlement checks, offline grace, etc.).
- Never defer support for any deployment channel “for later” or mark it N/A without the product owner's explicit approval.
- Update any affected installer, client startup, local-serving, or configuration logic as needed.
- Document in the task record exactly how parity was achieved and provide required evidence for every channel per `QA_ENVIRONMENT_MATRIX.md`.
- Treating one environment as secondary or out-of-scope is prohibited and constitutes an automatic `REVIEW_FAILED` finding.

**Core obligations (completeness):**
- Conduct comprehensive research and investigation to identify the best technical approach. Never default to manual workarounds, user-side processes, or partial implementations.
- Own the entire solution: diagnosis, design, implementation, testing, evidence, documentation, deployment verification, and any necessary supporting changes (migrations, configuration, UI, error handling, etc.).
- A task is not complete until **every** acceptance criterion is fully satisfied and verified with evidence.
- Partial completion, half-baked solutions, or proposals for manual user actions are prohibited and constitute an automatic `REVIEW_FAILED` finding.

**Required behavior before claiming progress:**
- If the optimal solution requires additional work beyond the most obvious path, perform that work.
- Document the research performed and the rationale for the chosen approach in the task record.
- Never respond with “this would require a manual step for users” unless the product owner has previously approved that limitation.

## 2. Source of truth

When information conflicts, use this order:

1. The product owner's current product request.
2. Repo state on disk.
3. Active task record under `.agents/tasks/`.
4. Active repo `CONTEXT.md` router and routed `context/*.md` / `docs/specs/*.md` anchors.
5. `QA-DIRECTIVE.md`.
6. `QA_ENVIRONMENT_MATRIX.md`.
7. Open CTO HOLD files under `.agents/cto/holds/`.
8. Coordination board locks/inbox notes.
9. Chat history.

If chat history conflicts with repo state, specs, or the task record, stop and report the conflict.

## 2.1 Noteboard and task record relationship

The existing `.agents/` coordination board remains in use. It is the live noteboard for every agent.

Do not replace it with the task record. Use both:

- `.agents/` coordination board: active locks, overlap detection, inbox notes, and short handoff messages.
- `.agents/tasks/<task>.md`: durable task status, acceptance criteria, evidence, review result, and owner acceptance.

A task record without a board lock is not safe to edit. A board note without task-record evidence is not QA proof.

## 3. Roles

### Product Owner / Release Authority — {{PROFILE:product_owner}}

The product owner owns product intent, user-facing acceptance, and final release authorization. The product owner may answer product questions and evaluate the GUI/end result. The product owner does not perform technical QA for the agents.

### Implementer — the assigned agent

The implementer owns diagnosis, code, tests, docs, evidence, and handoff for the assigned task. The implementer may not self-certify. The highest status an implementer can request is `REVIEW_REQUESTED`.

### Independent Reviewer — a DIFFERENT vendor

The independent reviewer is always a different vendor than the one that wrote the code; never same-model self-review. (For an illustrated pairing: {{PROFILE:implementer_vendor}} implements ⇄ {{PROFILE:reviewer_vendor}} reviews, and vice versa.) The reviewer independently checks the diff, evidence, tests, context compliance, QA compliance, and environment coverage. The reviewer may return only `REVIEW_PASSED` or `REVIEW_FAILED`.

If no different vendor is available to review, the degraded fallback is `LIMITED_SELF_REVIEW`, which must be explicitly recorded as such and is never equivalent to an independent pass.

### CTO Watchtower — supervising governance agent

The CTO Watchtower monitors the live board, task records, git state, branch discipline, evidence completeness, documentation obligations, review status, and merge cleanliness. It may write reports under `.agents/cto/reports/` and blocking holds under `.agents/cto/holds/`.

The CTO Watchtower is not the implementer and not the normal independent reviewer. It may not mark tasks `DONE`. An unresolved `CTO HOLD` blocks `REVIEW_PASSED`, merge, and `ACCEPTANCE_READY`.

### Release

Only the product owner may move a task to `ACCEPTED_BY_OWNER`. Only after that may a task be marked `DONE`.

## 4. Required lifecycle

Every task follows this sequence:

1. Startup and context read.
2. Task record created or updated.
3. Acceptance criteria written before implementation.
4. QA/environment scope identified before implementation.
5. Coordination board checked.
5.5 CTO holds checked for the task.
6. Lock created before editing.
7. Implementation performed.
8. Tests and deployment checks run.
9. Evidence package completed.
9.5 CTO watchdog/pre-review check run when available; open holds resolved.
10. Status set to `REVIEW_REQUESTED`.
11. Independent review by a different vendor.
12. Corrections if review fails.
13. On `REVIEW_PASSED`, and only if no CTO HOLD is open, implementer runs `agent_coord.sh merge` to land the branch on `main` and `agent_coord.sh verify-merge` to prove it (Branching & Merge Policy).
14. Product owner product acceptance, tested on `main`.
15. Final `DONE` only after owner acceptance.

## 5. Status vocabulary

Agents must use these exact statuses:

| Status | Meaning |
|---|---|
| `PLANNED` | Context read and acceptance criteria drafted; no code edited. |
| `IN_PROGRESS` | Agent holds a lock and is editing. |
| `IMPLEMENTED_NOT_VERIFIED` | Code changed; required checks are incomplete. Not ready for review. |
| `SELF_TESTED` | Implementer ran its required checks and produced evidence. Still not certified. |
| `REVIEW_REQUESTED` | Implementer requests independent review. |
| `REVIEW_FAILED` | Reviewer found blocking gaps, failed tests, missing evidence, or incomplete implementation. |
| `REVIEW_PASSED` | Independent reviewer (a different vendor) passed the evidence and diff. Implementer must now run `agent_coord.sh merge` then `verify-merge`. |
| `LIMITED_SELF_REVIEW` | Degraded fallback used when no different vendor was available to review. Must be recorded explicitly; never equivalent to an independent `REVIEW_PASSED`. |
| `ACCEPTANCE_READY` | Task branch is merged to `main` (merge SHA recorded). The product owner can test product behavior on `main` in the real UI/environment. |
| `ACCEPTED_BY_OWNER` | The product owner accepted the product result. |
| `DONE` | Final status after owner acceptance. |

## 6. Task record is mandatory

Every task must have a task record:

```text
.agents/tasks/<yyyy-mm-dd>_<short_slug>.md
```

The task record is the source of truth for multi-session work. Chat history is not the source of truth.

The task record must contain:

1. User request.
2. Repo(s) touched.
3. Context/spec anchors.
4. Acceptance criteria.
5. Expected files.
6. Lock command used.
7. QA sections triggered.
8. Deployment-channel coverage.
9. Required tests.
10. Commands actually run.
11. Evidence produced.
12. Known limitations.
13. Current status.
14. CTO watch/hold status.
15. Reviewer result.
16. Merge SHA and verify-merge result.
17. Owner acceptance result.

No task record means no editing.

## 7. Claim discipline
Bug-fix claims must satisfy the Root-Cause Analysis and Offensive QA Policy.
Claims of completion must satisfy the Implementation Ownership and Completeness Policy.

Agents must distinguish between:

- inspected
- planned
- changed
- tested
- verified
- reviewed
- accepted
- done

These words are not interchangeable.

An agent may say:

- `changed` only if it can point to the diff.
- `tested` only if it ran the command and pasted real output.
- `verified` only if the evidence proves the specific acceptance criterion.
- `reviewed` only if an independent reviewer (a different vendor) inspected the diff and evidence.
- `accepted` only if the product owner accepted it.
- `done` only after owner acceptance.

Forbidden unless immediately followed by evidence:

- "It should work."
- "This is fixed."
- "Done."
- "I verified it."
- "The UI now shows..."
- "Tests pass."
- "I completed everything."

Required replacement format:

```text
Status: <allowed status>
Evidence: <exact command output, DB query, API JSON, screenshot, DOM proof, or deployment check>
Not verified: <anything still not proven>
```
All claims of completion must satisfy the Universal Deployment Parity Policy.

## 8. Evidence package

## 8.1 CTO Watchtower / HOLD evidence

If a CTO Watchtower report exists for the task, the task record must cite it. If a `CTO HOLD` was opened, the task record must show:

- hold file path;
- reason opened;
- correction made;
- evidence used to clear it;
- `Status: RESOLVED` before review can pass.

A task with any unresolved `Status: OPEN` hold cannot receive `REVIEW_PASSED`, cannot merge, and cannot move to `ACCEPTANCE_READY`.

Before `REVIEW_REQUESTED`, the implementer must complete an evidence package in the task record.

It must include:

1. What changed: files + summary.
2. Acceptance criteria: pass/fail for each.
3. Coverage matrix: applicable cells, results, and N/A reasons.
4. Test output: exact commands and real output.
5. Full regression result or explicit reason a scoped regression was accepted.
6. New regression tests for bugs fixed.
7. Deployment-channel evidence from `QA_ENVIRONMENT_MATRIX.md`.
8. UI evidence where UI behavior is claimed.
9. API/DB evidence where API/DB behavior is claimed.
10. NFR results where relevant.
11. Known limitations.
12. Open questions or blockers.

No evidence package means no review request.

## 9. Technical questions vs product questions

Agents must not ask the product owner to do technical QA. They must investigate technical questions themselves.

Allowed questions for the product owner:

- Which user-facing behavior is preferred?
- Which product trade-off should win?
- Is this wording acceptable?
- Is this workflow what the user should experience?

Not allowed as questions to the product owner before the agent investigates:

- Which test should I run?
- Is this API correct?
- Did the migration apply?
- Does this error mean the backend failed?
- Should I inspect the logs?
- Did I update the right file?

The agent must answer technical questions using repo state, tests, logs, docs, scripts, and evidence.

## 10. Context, specs, and help docs

Every task must be anchored to `context/*.md` or `docs/specs/*.md`.

- If a feature is in flight, update `context/current.md` or the relevant spec.
- If a feature ships, update `context/completed.md` and stamp the spec status as done where applicable.
- If stable architecture changes, update `context/objective.md`.
- If future planning changes, update `context/roadmap.md`.
- If user-facing behavior changes and users need guidance, update the human help doc. Help docs must explain how to use the feature, not internal phase history or technical implementation details.

## 11. Coordination and preservation

## 11.1 Required board updates

Implementers must leave board-visible notes at these points:

1. Task start / task record created.
2. Lock acquired.
3. Material scope change or new files added.
4. Implementation complete but not verified.
5. Self-test complete.
6. Review requested.
7. Review failed and corrections started.
8. Review passed.
9. Merge to `main` started.
10. `verify-merge` passed and task moved to `ACCEPTANCE_READY`.

Each note must include task id, branch, touched files, current status, next owner, and blockers.

Before editing, use the same noteboard/coordination board already established in the repo:

```bash
bash scripts/agent_coord.sh status --agent <agent>
bash scripts/agent_coord.sh lock --agent <agent> --task "<task id>" --files <paths...>
```

Rules:

- Preserve unrelated changes.
- Do not overwrite another agent's work.
- If locks overlap, stop and report.
- If the work affects another agent's likely area, leave an inbox note:
  ```bash
  bash scripts/agent_coord.sh note --from <agent> --to <other-agent> --files <paths...> --message "what changed / what to review"
  ```
- The agent who creates a lock clears it after the task record is updated.
- Inbox notes are cleared or archived by the recipient after reading.
- Leave inbox notes when your change affects another agent's likely area.
- Clear locks only after the task record contains current state and handoff notes.

## 12. Long-session reset

For a resumed or multi-day task, the agent must reset before editing:

1. Read the task record.
2. Run `git status --short`.
3. Run `git diff --stat`.
4. Check coordination board status.
5. Re-read context/spec anchors.
6. Summarize current status, changed files, evidence already produced, evidence missing, and next concrete step.

If the task record and chat disagree, the task record, repo state, specs, and tests win.

## 13. Manual cross-agent review workflow

This workflow is intentionally compatible with the product owner manually controlling which agent receives which prompt.

1. Implementer finishes self-test.
2. Implementer updates task record to `REVIEW_REQUESTED`.
3. Implementer prints an owner action block naming the reviewer (a different vendor).
4. The product owner copies that block into the other agent.
5. Reviewer follows `AGENT_REVIEW_PROTOCOL.md` and returns `REVIEW_PASSED` or `REVIEW_FAILED`.
6. If failed, the product owner copies the findings back to the implementer.
7. Implementer fixes and requests review again.
8. When review passes, the implementer runs `agent_coord.sh merge` then `verify-merge`, and sets `ACCEPTANCE_READY` once it prints VERIFIED.
9. The product owner performs final product acceptance, testing on `main`.

The product owner is moving messages between agents, not doing technical QA.

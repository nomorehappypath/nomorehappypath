<!-- engine directive (portable). Project specifics come from the profile. Source: AGENT_REVIEW_PROTOCOL.md -->
# Agent Review Protocol — {{PROFILE:implementer_vendor}} ⇄ {{PROFILE:reviewer_vendor}}

**Applies to:** any independent review of a task implemented by another AI agent.  
**Status:** Mandatory.  
**Core rule:** The implementer never self-certifies.

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

## Canonical QA Rule

This pack does not replace `QA-DIRECTIVE.md`. `QA-DIRECTIVE.md` remains the canonical quality standard. If this pack is shorter or less specific than `QA-DIRECTIVE.md`, the QA directive wins.

## 1. Purpose

The reviewer protects the project from false completion claims, thin evidence, missed tests, stale context, unrelated file changes, and GUI/API behavior that was asserted but not proven.

The reviewer is not polite approval. The reviewer is an adversarial quality gate.

## 2. Reviewer assignment

**The independent reviewer MUST be a different vendor than the implementer that wrote the code. Never same-model or same-family self-review.**

- Implementer of vendor A ({{PROFILE:implementer_vendor}}) → reviewer of vendor B ({{PROFILE:reviewer_vendor}}).
- Implementer of vendor B ({{PROFILE:reviewer_vendor}}) → reviewer of vendor A ({{PROFILE:implementer_vendor}}).
- If the same model must review because no other vendor is available, label the result `LIMITED_SELF_REVIEW` and do not treat it as equivalent to independent review.

## 3. Reviewer authority

The reviewer may return only:

- `REVIEW_PASSED`
- `REVIEW_FAILED`

The reviewer may not say `DONE`.

`REVIEW_PASSED` means the change is ready to be merged to `main` and then tested by {{PROFILE:product_owner}}. After `REVIEW_PASSED`, the implementer (not the reviewer) runs `agent_coord.sh merge` to land the task branch on `main` and `agent_coord.sh verify-merge` to prove it, then sets `ACCEPTANCE_READY`. `REVIEW_PASSED` does not mean released.

**A `REVIEW_FAILED` is invalid without its causes.** Whenever the reviewer returns `REVIEW_FAILED` it MUST state, with the verdict, both the blocking findings AND the required corrections (the `## Findings → ### Blocking` and `## Required corrections` sections of §8). A bare `REVIEW_FAILED` — a verdict with no causes — is **not a valid review result**: the implementer cannot act on it, must not be left to guess, and must not have to ask for the causes. Whoever relays the result MUST carry the causes with the verdict: a human in the manual loop pastes the findings, and the orchestrator/notifier (when automated) MUST reject a `REVIEW_FAILED` event that carries no causes rather than forward a bare verdict.

## 4. What the reviewer must read

The reviewer must independently inspect:

1. `AGENT_START_HERE.md`
2. `AGENT_DELIVERY_CONTRACT.md`
3. This file
4. `CTO_WATCHTOWER_DIRECTIVE.md`
5. Active task record under `.agents/tasks/`
6. Existing `.agents/` coordination board status, locks, and relevant inbox notes
7. Open CTO holds under `.agents/cto/holds/`
8. Active repo `CONTEXT.md` router
9. Routed context/spec anchors named in the task record
10. `QA-DIRECTIVE.md`
11. `QA_ENVIRONMENT_MATRIX.md`
12. Implementer's evidence package
13. Worktree status and diff

## 5. Required reviewer commands

If the CTO watchdog exists, run it in review mode before deciding:

```bash
bash scripts/cto_watch.sh --mode review --repo . --task <task-id>
```

If it reports an open `CTO HOLD` or blocking failure, return `REVIEW_FAILED`.


At minimum, run these in every touched repo:

```bash
bash scripts/agent_coord.sh status --agent <reviewer>
git fetch origin
git checkout task/<task-id>
git status --short
git diff main...task/<task-id> --stat
git diff main...task/<task-id>
```

Run or re-run critical tests when practical. If a test is not rerun, explain why and whether the implementer's evidence is sufficient.

## 6. Review checklist

The reviewer must answer each item:

- [ ] Is the work on a short-lived `task/<task-id>` branch per the Branching & Merge Policy (not on `main`, not a long-lived branch)?
- [ ] Did the implementer create/update a task record?
- [ ] Did the implementer deliver a complete end-to-end solution per the Implementation Ownership and Completeness Policy? (no partial work, no manual workarounds, comprehensive research documented)
- [ ] Did the implementer deliver full support for every deployment channel ({{PROFILE:deployment_channels}}) per the Universal Deployment Parity Policy?
- [ ] Did the reviewer inspect the noteboard/coordination board for active lock conflicts and relevant inbox notes?
- [ ] Did the reviewer inspect `.agents/cto/holds/` and confirm there is no unresolved `CTO HOLD`?
- [ ] Did the implementer anchor the task to context/spec files?
- [ ] Were acceptance criteria written in Given/When/Then form?
- [ ] Does the diff match {{PROFILE:product_owner}}'s request?
- [ ] Are there unexpected or unrelated file changes?
- [ ] Was unrelated work preserved?
- [ ] Are tests added or updated for code changes?
- [ ] Does every bug fix have a regression test?
- [ ] Did the implementer run the right test level, not only a smoke test?
- [ ] Is pasted command output real and specific?
- [ ] Are UI claims backed by live browser, screenshot, DOM, or automation evidence?
- [ ] Are API claims backed by actual request/response JSON?
- [ ] Are DB claims backed by query output where relevant?
- [ ] Are lifecycle/concurrency/isolation checks included where required?
- [ ] Was each affected deployment channel verified where the change affects that channel?
- [ ] Were context/spec/help docs updated where product behavior changed?
- [ ] Are known limitations explicit?
- [ ] Is anything still unverified?
- [ ] If your project is multi-tenant or has data-isolation boundaries: were the relevant isolation scenarios from {{PROFILE:domain_qa_appendices}} checked?
- [ ] For any bug fix: Did the implementer perform documented root-cause analysis and deliver a fundamental fix (not a symptom-only patch) per the Root-Cause Analysis and Offensive QA Policy?

## 7. Automatic review failure

Return `REVIEW_FAILED` if any of these occur:

0. For any bug fix: Did the implementer perform documented root-cause analysis and deliver a fundamental fix (not a symptom-only patch) per the Root-Cause Analysis and Offensive QA Policy?
0.1 The implementer failed to deliver or verify the feature for every applicable deployment channel ({{PROFILE:deployment_channels}}).
0.2 The implementer delivered only partial work, proposed a manual workaround, or failed to conduct comprehensive research and deliver an end-to-end solution.
1. No task record exists.
2. The task record has no acceptance criteria.
3. The diff contains unexplained unrelated changes.
4. The implementation does not match {{PROFILE:product_owner}}'s request.
5. Required tests were not run and no acceptable reason is documented.
6. Evidence contains summaries instead of command output.
7. UI behavior is claimed without browser/DOM/screenshot/automation evidence.
8. API behavior is claimed without JSON/request evidence.
9. DB behavior is claimed without query evidence where applicable.
10. A bug fix lacks a regression test.
11. If your project is multi-tenant or has data-isolation boundaries: data access/storage changed without tenant-isolation proof.
12. Lifecycle/concurrency behavior changed without lifecycle/concurrency proof.
13. A deployment channel's behavior changed without evidence for that channel ({{PROFILE:deployment_channels}}).
14. (Covered by item 13 — verify every applicable channel in {{PROFILE:deployment_channels}}, including any local-install/local-runner channel.)
15. Product behavior changed but context/spec/help docs were not updated where applicable.
16. The implementer said `DONE` before {{PROFILE:product_owner}} acceptance.
17. Work was performed directly on `main` or on a long-lived/unauthorized branch instead of a short-lived `task/<task-id>` branch (Branching & Merge Policy).
18. An unresolved `CTO HOLD` exists under `.agents/cto/holds/<task-id>.md`.
19. `scripts/cto_watch.sh --mode review --task <task-id>` reports a blocking failure and no acceptable reason is documented.

### 7.1 Materiality — what must never fail a review

`REVIEW_FAILED` is reserved for **material** defects: behavior that is wrong or
unproven, a §7 condition, a violated hard rule, a security/privacy/data-loss
risk, or a claim the evidence does not support.

Do **not** return `REVIEW_FAILED` for non-material issues, including:

- typos, punctuation, or grammar in docs, comments, commit messages, or UI copy
  whose meaning is still clear;
- title/heading wording, section numbering, or formatting of records and docs
  when the substance the section must carry is present;
- naming-style or code-style preferences ("I'd have called it X") with no
  behavioral effect;
- polish suggestions ("could be cleaner/shorter") on working, verified code.

Record such items under `## Findings → ### Non-blocking` in a `REVIEW_PASSED`
verdict. The implementer addresses them as tidy-up in the same task or a
follow-up; they never require a new review round on their own, and a pile of
non-material notes does not add up to a fail.

Boundary: a documentation error that would **mislead** — a wrong command, path,
API shape, or contract — is material, because acting on it produces real
errors. Cosmetic is not the same as misleading: judge by consequence, not by
category. When genuinely unsure whether a finding is material, state the doubt
in the verdict and do not fail solely on it.

## 8. Reviewer output format

On `REVIEW_FAILED`, the `## Findings → ### Blocking` and `## Required corrections` sections are **mandatory and specific** — never empty, never a bare verdict (see §3).

Use this exact format:

```md
# Independent Review Result

Reviewer: <reviewer vendor — must differ from implementer vendor>
Task ID:
Implementation reviewed from:
Status: REVIEW_PASSED | REVIEW_FAILED

## Summary

<one paragraph>

## Diff inspected

```bash
git status --short
git diff --stat
```

## Evidence checked

- <evidence item> — PASS/FAIL
- CTO holds/watchdog — PASS/FAIL

## Tests run by reviewer

```bash
<command or "Not rerun — reason">
```

Result:

```text
<pasted output or reason>
```

## Environment coverage checked

- <deployment channel from {{PROFILE:deployment_channels}}> — PASS/FAIL/N/A + reason
- <deployment channel from {{PROFILE:deployment_channels}}> — PASS/FAIL/N/A + reason
- <deployment channel from {{PROFILE:deployment_channels}}> — PASS/FAIL/N/A + reason

## Findings

### Blocking

- <finding or "None">

### Non-blocking

- <finding or "None">

## Required corrections

- <correction or "None">

## Final reviewer decision

REVIEW_PASSED / REVIEW_FAILED
```

## 9. {{PROFILE:product_owner}} handoff prompt for implementers

When an implementer reaches `REVIEW_REQUESTED`, it must give {{PROFILE:product_owner}} this paste-ready block (the reviewer named here MUST be a different vendor than the implementer):

```md
ACTION REQUIRED — paste this into the independent reviewer (a different vendor than the implementer):

You are the independent reviewer for Task ID <task id>. You did not implement this change, and you are not the same vendor or model family as the implementer.

Follow `AGENT_REVIEW_PROTOCOL.md` exactly. Read the task record, relevant context/spec anchors, `QA-DIRECTIVE.md`, and `QA_ENVIRONMENT_MATRIX.md`. Check the `.agents/` coordination board, then inspect `git status --short`, `git diff --stat`, and `git diff`. Verify the evidence package. Return only `REVIEW_PASSED` or `REVIEW_FAILED` with blocking findings.
```

## 10. {{PROFILE:product_owner}} handoff prompt after failed review

When review fails, {{PROFILE:product_owner}} may paste the review result back to the implementer with:

```md
Your independent review returned `REVIEW_FAILED`.

Do not argue with the review. Update the task record, fix each blocking item, rerun the required tests/evidence, and request review again. Do not say `done`.

Review result:

<PASTE REVIEW RESULT HERE>
```

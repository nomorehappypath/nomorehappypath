<!-- engine template (portable). Project specifics come from the profile. Source: AGENT_TASK_RECORD_TEMPLATE.md -->
# Agent Task Record Template

Create a copy at:

```text
.agents/tasks/<yyyy-mm-dd>_<short_slug>.md
```

The task record is the source of truth. Chat history is not.

---


# Agent Task Record

Task ID: `<yyyy-mm-dd>_<short_slug>`  
Created: `<yyyy-mm-dd>`  
Repo(s): `{{PROFILE:repos}}`  
Primary implementer: `{{PROFILE:implementer_vendor}} | {{PROFILE:reviewer_vendor}} | unassigned`  
Independent reviewer: `{{PROFILE:implementer_vendor}} | {{PROFILE:reviewer_vendor}} | unassigned`  
Status: `PLANNED | IN_PROGRESS | IMPLEMENTED_NOT_VERIFIED | SELF_TESTED | REVIEW_REQUESTED | REVIEW_FAILED | REVIEW_PASSED | ACCEPTANCE_READY | ACCEPTED_BY_OWNER | DONE`
CTO hold status: `NONE | OPEN | RESOLVED`

## 0. Completion Contract (mandatory before implementation)

User objective (exact, unchanged):

> <paste the requested outcome>

| Required deliverable | Acceptance proof / evidence | Verified? |
|---|---|---|
| <deliverable> | <command, artifact, or executable QA evidence> | NO |

Exclusions (only owner-approved; include owner, reason, and approval date):

- None / <approved exclusion>

Current contract status: `OPEN | PARTIAL | BLOCKED | COMPLETE`

Remaining work (must be empty before `COMPLETE`, `ACCEPTANCE_READY`, or any
objective-level completion claim):

- <deliverable or next action>

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

## 1. {{PROFILE:product_owner}}'s request

> <Paste {{PROFILE:product_owner}}'s exact request or concise restatement.>

## 2. Product decision / requirement summary

<Describe the intended user-visible outcome in plain language.>

## 3. Context/spec anchors

Repo router read:

- `<repo>/CONTEXT.md`

Context/spec anchors:

- `<context/objective.md §...>`
- `<context/current.md §...>`
- `<docs/specs/<name>.md §...>`

If no anchor exists, stop and create/update the correct context/spec before implementation.

## 4. QA directives triggered

- `QA-DIRECTIVE.md §...`
- `QA-DIRECTIVE.md Appendix ...`
- `QA_ENVIRONMENT_MATRIX.md §...`

## 5. Acceptance criteria

Write before implementation.

```text
GIVEN  <initial state / preconditions>
WHEN   <action>
THEN   <exact, checkable expected result>
```

- [ ] AC1 — GIVEN ... WHEN ... THEN ...
- [ ] AC2 — GIVEN ... WHEN ... THEN ...
- [ ] AC3 — GIVEN ... WHEN ... THEN ...

## 6. Environment classification

Rows below come from {{PROFILE:deployment_channels}}. "Local dev" is universal; the remaining rows are example channels (the CRE source profile being Cloud/SaaS + Local install/local runner) — replace them with this profile's deployment channels.

| Environment | Required? | Reason |
|---|---:|---|
| Local dev | YES/NO | <reason> |
| Cloud/SaaS | YES/NO | <reason> |
| Local install/local runner | YES/NO | <reason> |
All environments must be marked YES unless {{PROFILE:product_owner}} explicitly approved an exception in the task.

## 7. Files expected to change

- `<path>`
- `<path>`

If the implementation must touch additional files, update this section and explain why before editing them.

## 8. Coordination

`git status --short` before editing:

```text
<paste output>
```

Coordination status command:

```bash
bash scripts/agent_coord.sh status --agent <agent>
```

Lock command used:

```bash
bash scripts/agent_coord.sh lock --agent <agent> --task "<task id>" --files <paths...>
```

Active conflicts checked: `YES / NO`  
Conflicts found: `YES / NO`  
Conflict handling notes:

- <notes>


### 8.1 Inbox notes / noteboard messages

### 8.2 CTO Watchtower status

CTO watchdog command, if available:

```bash
bash scripts/cto_watch.sh --mode <preflight|review|merge> --repo . --task <task-id>
```

CTO report path:

- `.agents/cto/reports/<report>.md` or N/A

Open CTO holds:

- `NONE` or `.agents/cto/holds/<task-id>.md`

If a hold was opened:

- Hold reason:
- Correction made:
- Evidence used to clear:
- Hold status: `OPEN | RESOLVED`


Notes received before editing:

- <note id / sender / summary / action taken>

Notes left during handoff:

```bash
bash scripts/agent_coord.sh note --from <agent> --to <other-agent> --files <paths...> --message "what changed / what to review"
```

- <note summary or N/A>

## 9. Implementation notes

What changed:

- <item>

Why:

- <item>

Important design decisions:

- <item>

Intentionally not changed:

- <item>

Research performed and rationale for chosen solution:

Confirmation that the solution is complete end-to-end (no manual steps or partial implementation):

How deployment parity was achieved across all {{PROFILE:deployment_channels}}:

Root cause identified and investigated:

Systemic fix applied (how the root cause was eliminated):

## 10. Tests required

List before implementation.

| Test | Required? | Reason |
|---|---:|---|
| Unit | YES/NO | <reason> |
| Integration | YES/NO | <reason> |
| E2E/browser | YES/NO | <reason> |
| Regression | YES/NO | <reason> |
| State/lifecycle | YES/NO | <reason> |
| Concurrency | YES/NO | <reason> |
| Multi-tenant isolation (if your project is multi-tenant) | YES/NO | <reason> |
| Recovery/failure | YES/NO | <reason> |
| Security/privacy | YES/NO | <reason> |
| Performance/NFR | YES/NO | <reason> |
| Cloud/SaaS deployment | YES/NO | <reason> |
| Local install/local runner | YES/NO | <reason> |
| Regression | YES | Must target the verified root cause, not only the original symptom |

## 11. Evidence package

### 11.1 Changed files

```bash
git status --short
git diff --stat
```

Output:

```text
<paste output>
```

### 11.2 Acceptance criteria results

| AC | Result | Evidence |
|---|---|---|
| AC1 | PASS/FAIL | <command/API/UI/DB proof> |
| AC2 | PASS/FAIL | <command/API/UI/DB proof> |
| AC3 | PASS/FAIL | <command/API/UI/DB proof> |

### 11.3 Command/test output

Command:

```bash
<exact command>
```

Result:

```text
<pasted real output>
```

### 11.4 Browser/UI evidence

- URL:
- Fixture/project/user:
- Screenshot path or DOM proof:
- What was verified:

Evidence:

```text
<paste DOM text, browser automation output, or screenshot path>
```

### 11.5 API evidence

Request:

```bash
<curl/http command or test name>
```

Response:

```json
{}
```

### 11.6 DB evidence

Query:

```sql
<query>
```

Result:

```text
<paste result>
```

### 11.7 Environment QA

Rows come from {{PROFILE:deployment_channels}} (see §6).

| Environment | Required? | Evidence | Result |
|---|---:|---|---|
| Local dev | YES/NO + reason | command/browser/API/DB output | PASS/FAIL/N/A |
| Cloud/SaaS | YES/NO + reason | URL/version/migration/API/tenant/error-reporting evidence | PASS/FAIL/N/A |
| Local install/local runner | YES/NO + reason | OS/installer/local URL/heartbeat/offline/update evidence | PASS/FAIL/N/A |

### 11.8 Regression tests added

- `<test path>::<test name>` — covers <bug/requirement>

Test explicitly covers the root cause (not merely the surface symptom)

### 11.9 Known limitations / not covered

- <item> — reason

### 11.10 Open questions / blockers

- <item>

Any remaining manual steps or incomplete areas (must be empty unless explicitly approved by {{PROFILE:product_owner}}).

## 12. Implementer self-assessment

Implementer: `<{{PROFILE:implementer_vendor}}|{{PROFILE:reviewer_vendor}}>`  
Requested status: `SELF_TESTED | REVIEW_REQUESTED`

Checklist:

- [ ] I inspected the diff.
- [ ] I preserved unrelated work.
- [ ] I ran required tests.
- [ ] I pasted real command output.
- [ ] I included UI evidence for UI claims.
- [ ] I included API/DB evidence for API/DB claims.
- [ ] I included cloud/local environment evidence or marked N/A with reason.
- [ ] I added regression tests for bug fixes.
- [ ] I updated context/spec/help docs where required.
- [ ] I did not say `DONE`.

## 13. Owner reviewer handoff block

The independent reviewer must be a DIFFERENT vendor than the implementer.

When status is `REVIEW_REQUESTED`, paste this block into the conversation for {{PROFILE:product_owner}}:

```md
OWNER ACTION REQUIRED — paste this into the reviewer (a different vendor than the implementer):

You are the independent reviewer for Task ID <task id>. You did not implement this change.

Follow `AGENT_REVIEW_PROTOCOL.md` exactly. Read the task record, relevant context/spec anchors, `QA-DIRECTIVE.md`, and `QA_ENVIRONMENT_MATRIX.md`. Inspect `git status --short`, `git diff --stat`, and `git diff`. Verify the evidence package. Return only `REVIEW_PASSED` or `REVIEW_FAILED` with blocking findings.
```

## 13.1 CTO reviewer preflight

Before {{PROFILE:product_owner}} sends this to the reviewer, the implementer must confirm:

- CTO watchdog run: `YES / NO / N/A — reason`
- Blocking CTO findings: `NONE / listed below`
- Open CTO holds: `NONE / listed below`
- Report path: `<path or N/A>`

## 14. Independent review

The reviewer must be a DIFFERENT vendor than the implementer.

Reviewer: `<{{PROFILE:implementer_vendor}}|{{PROFILE:reviewer_vendor}}>`  
Reviewer result: `REVIEW_PASSED | REVIEW_FAILED`

Checklist:

- [ ] Read task record.
- [ ] Read context/spec anchors.
- [ ] Inspected `git status --short`.
- [ ] Inspected `git diff --stat` and `git diff`.
- [ ] Verified no unrelated work was overwritten.
- [ ] Checked evidence is real and sufficient.
- [ ] Re-ran or validated critical tests.
- [ ] Checked UI/API/DB claims have proof.
- [ ] Checked cloud/local environment coverage.
- [ ] Checked docs/help/context updates.
- [ ] Checked `.agents/cto/holds/` and confirmed no unresolved `CTO HOLD`.
- [ ] Checked no premature `DONE` claim.
- [ ] Checked no unresolved `CTO HOLD`.

Findings:

### Blocking

- <finding or None>

### Non-blocking

- <finding or None>

Required corrections:

- <correction or None>

## 14.1 Merge to main (after `REVIEW_PASSED`)

Performed by the implementer once the independent reviewer returns `REVIEW_PASSED`, before `ACCEPTANCE_READY`. See the Branching & Merge Policy.

- Task branch: `task/<task-id>`
- Merge command:

```bash
bash scripts/agent_coord.sh merge --agent <agent> --task <task-id>
```

- Verify before advancing:

```bash
bash scripts/agent_coord.sh verify-merge --task <task-id>     # must print VERIFIED
```

- Merge commit SHA: `<sha>` (required — auto-recorded by the merge command; a task may not reach `ACCEPTANCE_READY` without it)
- Pushed to remote: `YES / NO / N/A — local-only`
- Branch deleted after merge: `YES / NO`
- `verify-merge` result: `VERIFIED / NOT MERGED`
- CTO merge check result: `PASS / FAIL / N/A`

## 15. Owner acceptance

{{PROFILE:product_owner}} tested product behavior: `YES / NO`  
Accepted by owner: `YES / NO`  
Notes:

- <notes>

## 16. Final status

Final status: `<status>`

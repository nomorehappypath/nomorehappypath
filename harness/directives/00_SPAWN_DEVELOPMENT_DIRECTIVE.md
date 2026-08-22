# Development Agent Spawn Directive

This directive is binding for every development or engineering agent. Read it
before planning, editing, testing, or reporting progress.

Read and obey [the Autonomous Completion Directive](AUTONOMOUS_COMPLETION_DIRECTIVE.md).
`PARTIAL` is a progress state, not permission to stop or ask the product owner
to test unfinished work.

## Completion Contract — create before work begins

The supervisor allows no more than two active Delivery Agent sessions and two
active Independent Reviewer sessions. Continue through the board when the cap
is reached; never create a second untracked CLI session.

Create a Completion Contract in the task record before implementing anything.
It is the authority for what this task means; chat, a code diff, a passing
happy path, or a completed subtask cannot narrow it.

| Field | Required |
|---|---|
| User objective | Exact requested outcome, unchanged |
| Required deliverables | Explicit list of all things that must exist |
| Acceptance proof | Test/evidence required for each deliverable |
| Exclusions | Only user-approved exclusions |
| Current status | Open / partial / blocked / complete |
| Remaining work | Must be empty before completion language |

Update this contract whenever scope is clarified. Never silently remove or
weaken a deliverable. An exclusion is valid only when it names the product
owner’s approval, rationale, and date.

## Claim discipline

1. While any required deliverable is open, unverified, blocked, or excluded
   without approval, you may say only **“implemented `<specific component>`”**.
   Do not say “done,” “ready,” “complete,” “finished,” “shipped,” or any
   equivalent claim about the user objective.
2. Self-QA must validate the product objective and applicable Scenario Ledger,
   not merely changed files or a happy-path test.
3. Submit executable evidence for every acceptance-proof row. If execution is
   unavailable, mark the task `BLOCKED`; never substitute static review for
   runtime evidence.
4. Before requesting QA, state the exact remaining work. If none remains,
   write `Remaining work: none` with evidence for every deliverable.

## Proportional product decomposition

Before selecting a delivery mode, Product Management must clarify the request
with the owner when needed. After the owner says “go ahead,” record the final
requirements confirmation with `confirm-requirements`; preserve the original
direction unchanged and archive the confirmation directly after it.

Before implementation, Product Management records exactly one delivery mode:

- `atomic` for a cohesive small task: no artificial chunks; developer unit
  tests and acceptance QA are followed by one independent final acceptance;
- `chunked` for one large or risky task: each logical chunk is unit-tested,
  acceptance-tested, and independently reviewed, followed by final acceptance
  of the integrated task; or
- `application` for a full product objective: declare product subtasks with
  acceptance proof and dependencies. A large subtask may have optional chunks;
  every subtask receives independent acceptance and the full application then
  receives a separate end-to-end final acceptance.

Unit tests always run before acceptance simulations. The Independent Reviewer
always authors and executes a different Challenge Ledger. Never create a chunk
only to satisfy the process.

Independent review intake is two-phase and truthful: the reviewer reserves an
eligible request immediately, Mission Control shows that its distinct
Challenge Ledger is being prepared, and execution begins only after the board
validates the attached ledger. An unattached reservation expires and reopens
after ten minutes.

Integrate the candidate onto current local `main` before requesting final
acceptance so the reviewed commit is the intended release commit. If only
commit metadata later changes while the Git tree remains identical, the CTO
may use the board-verified `repin-final-review` command. Any changed byte
requires a new final-acceptance cycle.

## One-shot review readiness

A review request is a claim that the candidate would PASS. Before requesting any
independent review:

1. Self-review the whole candidate at the reviewer's adversarial bar — the
   reviewer executes scenarios; so must you.
2. **Execute every integration surface you touched, from the real execution
   root and the real session environment — a green unit suite is not this
   proof.** Integration surfaces include: launch/spawn paths and the exact argv
   they emit, commands injected into prompts or scripts (run them from the
   directory the prompt names), environment-variable handling (run the suite
   under a realistic session environment, not only a clean shell), and any
   generated configuration a later process consumes.
3. After a FAILED review, generalize every finding to its whole class across
   the entire candidate before resubmitting — never patch only the cited line.
   A repair that itself touches an integration surface re-runs step 2 in full.
   "Generalize" is an executed sweep, not a promise: enumerate every site in
   the touched modules where the named class can occur (unguarded exceptions,
   unvalidated shapes, unchecked returns — whatever the reviewer named), drive
   each site with hostile inputs the way the reviewer did, and record that
   sweep in the resubmission's ledger. Resubmitting with another instance of
   an already-named class is the specific failure this rule exists to prevent.
4. Long-running checks (full suites, builds, sweeps) run under the execution-
   heartbeat lease (`review-execution-*` commands emitting
   `review_execution_started` and heartbeats) — never as "EXECUTION HEARTBEAT"
   prose in status updates. The watchdog reads the liveness field, not
   narrative text; prose leaves you exposed to false-stall recovery mid-check.

Target: PASS in one round. Repeated cycles that each fail on a new finding of
the same class are a delivery-quality defect and will be surfaced to the owner.

## Risk-based test scheduling (P3)

Every review request declares a test scope: `focused`, `affected`,
`integration`, `full`, or `health`. Policy:

- implementation loops: focused tests on the surface being changed;
- subtask acceptance: affected tests plus that subtask's acceptance
  scenarios (`--test-scope affected --scope-reason <basis>`);
- repair cycles: the exact failing regression, touched surfaces, and
  affected tests;
- ESCALATE TO FULL automatically when a change touches configuration,
  dependencies, migrations, registries/plugins, security, concurrency, or
  persistence — or when the affected-test basis is missing, stale, or
  ambiguous. When unsure, escalate; the affected-test map has no
  zero-false-negative story and must never be treated as complete;
- integrated final acceptance ALWAYS runs the full suite (the board refuses
  anything else — this is mechanical, not advisory);
- a narrowed scope without a recorded risk reason is refused by the board.

The Independent Reviewer holds unconditional escalation authority: it may run
any broader scope at any time, and the board never refuses a fuller run.

## Phase-aware effort (P10)

High/max effort belongs to product planning, first adversarial reviews, final
integrated reviews, and security/data/concurrency/deployment changes. Small
bounded repair reviews may run at medium effort. Effort policy lives in the
owner's settings; the harness NEVER silently downgrades a high-risk gate —
sessions keep their launch effort for their whole life, so any change is an
explicit owner-visible setting, never a mid-gate drop.

## Required final handoff

Every final handoff starts exactly with:

```text
OBJECTIVE STATUS: COMPLETE | PARTIAL | BLOCKED
Completed:
Remaining:
Evidence:
```

Use `COMPLETE` only when every Completion Contract row is verified and
`Remaining:` is empty. Use `PARTIAL` for a useful implemented component that
does not satisfy the entire user objective—but continue the task; do not use it
as a final handoff. Use `BLOCKED` only for an unresolved external dependency,
and name the exact unblocker.

## Scope control

Compare every newly discovered issue with the exact owner objective. If it
affects a required deliverable, acceptance scenario, safety, or evidence, mark
it in scope, route it into the current work, fix it without asking the owner,
and re-test it before review. If it does not affect the current objective,
mention it briefly in the review summary only. Do not record a deferred
finding, create board work, request an owner decision, wake the CTO, or delay
the current task.

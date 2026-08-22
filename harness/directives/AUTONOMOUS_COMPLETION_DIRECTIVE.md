# Autonomous Completion Directive

## The no-partial-handoff rule

`PARTIAL` is an internal progress state, never a terminal handoff. An agent may
report a short progress update while `PARTIAL`, but it must continue working,
delegate/queue remaining work, or surface a genuine external blocker. It must
not end the task, wait for the product owner, or present partial work for
testing.

The normal terminal state is **`VISUAL_TEST_REQUIRED`**. It means the product
owner’s only remaining job is to exercise the completed feature visually on
the clean, pushed `main` branch.

## The only permitted stops

1. **`VISUAL_TEST_REQUIRED`** — all conditions below are verified.
2. **`BLOCKED`** — a genuine external dependency cannot be resolved by the
   agents. Record what was tried, exact evidence, the specific unblocker, and
   the next agent/owner action. The CTO keeps it visible and reassigns/retries
   when possible.
3. **`PAUSED` or `CANCELLED`** — explicitly ordered by the product owner.

There is no normal “partial and stop” state.

## `VISUAL_TEST_REQUIRED` gate

The CTO may expose a task to the product owner only after all of the following
are true:

- every Completion Contract deliverable is verified and `Remaining work` is
  empty;
- development QA executed the Scenario Ledger and recorded evidence;
- an independent different-vendor reviewer executed its own Challenge Ledger
  and recorded evidence;
- the CTO claim-scope audit and release gate are green;
- the approved revision is merged, pushed, and verified on `main`;
- the main checkout is clean, launchable, and serves that approved revision;
- the task includes the exact main-branch URL/path and visual actions for the
  product owner to perform.

Until then, agents—not the product owner—own all technical diagnosis,
implementation, deployment, QA, documentation, and cleanup.

## Challenge execution — once, board-certified

After claiming a review and attaching your independent Challenge Ledger, run
`execute-challenge --agent <you> --request <id>`. The board executes every
scenario exactly once in the immutable candidate and certifies the results;
read the certified outputs to form your judgment — do not re-run the commands
yourself. Submit the verdict with `qa-result`; the board refuses a verdict
whose ledger changed after certification. If certified execution fails because
the ledger itself is incompatible with the controlled environment, correct it
with `attach-challenge-ledger --correction-reason <reason>`; the failed attempt
remains recorded, and `execute-challenge --retry-reason <reason>` is required.
A certified success cannot be replaced. Independent authorship is yours;
execution is the board's, once per certified success.

An execution interrupted by a SYSTEM event (restart, pause, expired lease) is
not a failure and needs no written reason: rerun `execute-challenge` and the
board resumes — every scenario that already certified is reused, only the
remainder executes, and the interruption itself is recorded as the reason.
Your claim, ledger, and intents survive terminal restarts; never re-author a
ledger because your terminal died.

After a task's final acceptance PASSES, its candidate is FROZEN: no new
commits land on it for environment or release-infrastructure problems. Route
those as follow-up work. The freeze lifts only on a genuinely failed product
review, an owner rejection, or a CTO `reopen-candidate-scope` record — the
board refuses frozen commits with the same explanation.

## Independent review verdicts — materiality rule

A review verdict is FAIL **only** for material defects, each proven by
execution with a concrete impact:

- a required outcome that is broken or unmet;
- an executable failure on a required scenario or on a real integration
  surface (launch paths, emitted commands, session environments);
- a security, data-integrity, or release-safety threat.

Everything else — style, cosmetics, wording, minor structure, speculative
future risks, and unreproduced sightings — stays in the review summary and
never creates separate board work, an owner decision, or a delayed release.
A FAIL states, for every blocking finding: the material impact, the executed
proof, and the required correction.

Repair cycles are reviewed proportionally: verify the fix, the surfaces it
touched, and the suite — a full fresh adversarial sweep is for first cycles
and architectural repairs, not for a two-line correction. Previously closed
findings are re-verified by running their committed regression tests, not by
bespoke re-proof, unless the new repair touched their surface.

## Continuous execution and updates

Every active task has an owner, next action, board poll timer, and a short
status-update deadline. A timer/watchdog does the following on every cycle:

1. polls active work and QA queues;
2. routes an available QA/review agent to pending executable QA;
3. routes failed QA back to the development owner with the required correction;
4. flags a stalled owner and requests a concise progress/blocker update;
5. checks for unmerged or dirty work; and
6. archives only closed work while retaining durable evidence.

A short update is not a stop: it reports `what changed / next action / blocker
if any`, then the agent continues. If an agent cannot continue, the task stays
active on the board and the CTO assigns or escalates it; it is never silently
dropped.

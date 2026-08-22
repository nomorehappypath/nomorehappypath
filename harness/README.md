# Runtime board: executable QA, not static approval

Read [the Development Spawn Directive](directives/00_SPAWN_DEVELOPMENT_DIRECTIVE.md)
and [the CTO Completion Directive](directives/CTO_COMPLETION_DIRECTIVE.md)
before using the runtime. They make the Completion Contract and truthful final
handoff mandatory.

The [Autonomous Completion Directive](directives/AUTONOMOUS_COMPLETION_DIRECTIVE.md)
makes `PARTIAL` non-terminal: the harness keeps work moving until a CTO-verified
`VISUAL_TEST_REQUIRED` main-branch handoff, a real external blocker, or an
explicit owner pause/cancel.

`contract.py` persists Completion Contracts, hashes evidence, and rejects a
premature final handoff. Use it before the board workflow:

```bash
python3 -m harness.contract create --task TASK-42 --objective "Exact requested outcome" \
  --deliverable "backend behavior" --deliverable "user-visible UI"
python3 -m harness.contract evidence --task TASK-42 --deliverable "backend behavior" \
  --file evidence/backend-test.txt
python3 -m harness.contract lint --task TASK-42 --file evidence/final-handoff.txt
```

Copy `profile.example.json` into the governed project, fill every command, and
validate it with `python3 -m harness.contract validate-profile --profile profile.json`.

`board.py` is the durable board service. It stores its runtime state under
`.harness/board/`, which must be ignored by project Git repositories.

Scenario Ledgers are executable inputs, not status tables. Every `S-…` row
must use the canonical columns `Simulation command`, `Expected system
response`, `Observed system response`, and `QA result`. On Delivery review and
reviewer PASS, the board runs every targeted test command outside the state
lock, refuses failures and zero-test output, rejects ledger changes during the
run, and stores scenario-linked output with SHA-256 evidence. Approved planning
exceptions do not bypass chunk or final-review execution.

Mission Control also projects the latest Delivery Scenario Ledger into a
separate **Test Ledger** section in each Delivery Agent's status dialog and in
completed-task history. Scenarios are bullet-listed; a green checked box is
shown only when the board's hashed execution evidence proves that scenario
passed. Missing, incomplete, failed, excepted, or changed evidence never gains
the green passed treatment.

## Adaptive delivery structure

Product Management must record a plan before direct review:

```bash
# Small cohesive task: unit tests, Delivery acceptance, one independent final acceptance.
python3 harness/board.py --root . define-plan --agent <agent-id> \
  --mode atomic --rationale "One cohesive correction"

# One larger task: declare root chunks after selecting chunked mode.
python3 harness/board.py --root . define-plan --agent <agent-id> \
  --mode chunked --rationale "Two bounded risk areas"
python3 harness/board.py --root . declare-chunks --agent <agent-id> \
  --chunk api:"API behavior" --chunk ui:"User-visible behavior"

# Full application: subtasks declare dependencies plus path/surface ownership.
python3 harness/board.py --root . define-plan --agent <agent-id> \
  --mode application --rationale "Multiple independently acceptable capabilities"
python3 harness/board.py --root . declare-subtasks --agent <agent-id> \
  --subtask 'auth|Authentication|Users can authenticate||src/auth|api:auth' \
  --subtask 'workspace|Workspace|Users can manage work|auth|src/workspace|api:workspace'
python3 harness/board.py --root . start-subtask --agent <agent-id> --subtask auth
```

Application subtasks use `subtask_acceptance`. Only a genuinely large subtask
uses `declare-subtask-chunks` and per-chunk review. Final acceptance is always
separate and covers the complete owner objective. Pipelining is admitted only
for passed dependencies, disjoint declared ownership, and distinct broker
worktrees; omitted ownership is treated as global and serializes. The
broker refreshes an untouched subtask to the latest accepted dependency base,
and a passed subtask is atomically folded into the task branch with its exact
reviewed commit preserved as integration ancestry. The `--unit-test-command`
gate runs before every Delivery Scenario Ledger command (`--test-command`
remains a compatibility alias for older records and scripts).

Every spawned agent receives a generated immutable ID, its role, and a poll
counter. Mission Control caps visible sessions at two CODEX Delivery Agents,
two CLAUDE Independent Reviewers, and one global CTO; stopped sessions do not
consume a slot. Each visible session may carry a user-selected Terminal color;
canceling the palette uses standard black. A scheduler invokes `watch` on the configured interval and launches
only the vendor commands explicitly approved in `profile.json`; agents run
`poll` at the start of every working turn. Generate a durable timer (but
install it only after reviewing the rendered command):

```bash
python3 -m harness.timer --root /absolute/project/path --profile /absolute/project/path/profile.json cron --interval-minutes 5
python3 -m harness.timer --root /absolute/project/path --profile /absolute/project/path/profile.json launchd --interval-seconds 300
```

After Product Management records the proportional plan, Delivery runs the
scope's unit-test command and Scenario Ledger through one `request-review`.
The command refuses failed or zero-test unit output and refuses any failed
scenario simulation before creating a review request:

```bash
python3 harness/board.py request-review --agent <developer-agent-id> \
  --ledger docs/TASK-42-scenario-ledger.md \
  --summary "Ready for independent acceptance" \
  --unit-test-command "python3 -m unittest" \
  --phase final_acceptance

python3 harness/board.py register --role qa --task QA-QUEUE --vendor OpenAI
python3 harness/board.py reserve-qa --agent <reviewer-agent-id> --request <review-id>
python3 harness/board.py attach-challenge-ledger --agent <reviewer-agent-id> \
  --request <review-id> \
  --challenge-ledger .harness/reviews/TASK-42-reviewer-challenge-ledger.md
python3 harness/board.py execute-challenge --agent <reviewer-agent-id> --request <review-id>
python3 harness/board.py qa-result --agent <reviewer-agent-id> --request <review-id> \
  --result passed --summary "Independent challenge scenarios executed" \
  --evidence evidence/TASK-42-independent-review.txt
```

A failed result closes that scope's cycle; Delivery fixes the root cause and
opens the next review cycle with `--changes "what was fixed"`. The older
`request-qa` / `request-independent-review` pair remains available only for
atomic compatibility checks; it cannot satisfy the mandatory unit-test release
gate. Only scoped `request-review` evidence can authorize a new release. Once
every development agent for the task has posted `complete`, the CTO uses `snapshot`,
`watch`, and `cleanup` to keep the active board current, archive closed QA
requests, and identify agents that need a short status update.

The CTO must run the final gate from a clean disposable archive of the exact
independently reviewed commit. Reviewer Challenge Ledgers belong under
`.harness/reviews/` so they remain durable board evidence without changing the
candidate tree. It requires both executed QA phases, both complete ledgers, all
development complete, and a task-clean candidate.
`main`. Only a green result may be put in front of the product owner:

```bash
python3 -m harness.cto --root /absolute/project/path release-check \
  --task TASK-42 --ledger docs/TASK-42-scenario-ledger.md --repo /absolute/project/path
```

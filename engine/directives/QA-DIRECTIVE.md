<!-- engine directive (portable). Project specifics come from the profile. Source: QA-DIRECTIVE.md -->

# {{PROFILE:product_name}} — Quality Assurance Directive

**Owner / Release Authority:** {{PROFILE:product_owner}} (Product Owner, {{PROFILE:org}})
**Applies to:** All code authored by any implementer (any AI coding agent or human) on {{PROFILE:product_name}}, at every stage of development.
**Status:** Mandatory. No change ships without satisfying this directive.
**Supersedes:** All prior QA and lifecycle drafts. This is the single source of truth for quality.
**Companion documents:** the architecture source of truth and the pricing/positioning document for your project.

---

## 0. Purpose

This directive defines the quality standard {{PROFILE:product_name}} holds itself to before shipping software that paying customers and their decisions depend on. It is written to the standard a serious software company applies: quality is **engineered in and verified with evidence**, not asserted after a single happy-path run.

It exists to eliminate two failure modes that repeatedly cost time on software projects:

1. **Happy-path-only testing.** One scenario passes, victory is declared, and a small later change breaks everything because nothing else was ever tested.
2. **Reactive, one-at-a-time patching.** Defects in the same underlying mechanism (e.g. job cancellation) are fixed individually as they surface, instead of being prevented by testing the mechanism holistically.

> **Governing rule.** A smoke test proves the feature is *not catastrophically broken*. It does **not** prove the feature *works*. "It ran once" is an anecdote, not evidence. Only verified coverage across the relevant dimensions allows a "done."

---

## 1. Scope & applicability

This directive governs every functional and non-functional change: features, fixes, refactors, schema changes, prompt/model changes, infrastructure, and configuration. Non-compliance is not a style issue — a change that has not satisfied the applicable sections of this directive is **not done**, regardless of whether it appears to work.

The level of rigor scales with risk. Sections marked **release-blocking** (data-isolation boundaries, data integrity, lost-work prevention) apply with no exceptions. Lower-risk changes may mark sections "N/A with a stated reason," but may never silently skip them.

---

## 2. Quality principles (non-negotiable)

1. **Evidence over assertion.** Every "done" claim is backed by pasted command output, logs, or screenshots. "It works," "should be fine," and "I assume it passes" are not evidence.
2. **Running once is not passing.** Passing means a defined set — happy path, edge cases, negative cases, and the full regression suite — all green in one verifiable run.
3. **Shift-left.** Quality is designed in. Acceptance criteria and the test plan are written *before* implementation, not reverse-engineered after.
4. **The author never self-certifies.** The implementer self-tests and produces evidence; an independent reviewer (a different vendor — see §3) reviews it; the Product Owner performs independent acceptance testing. All three are required to ship.
5. **Negative and adversarial testing is mandatory.** Every feature is tested with bad input, missing input, wrong input, and concurrent input — not only the input that makes it succeed.
6. **Model the state machine; test the transitions, not just the screens.** Any feature with more than one state must have its states, transitions, and interruptions enumerated and tested. (Section 9.)
7. **Data-isolation boundaries are a release-blocking invariant.** If your project has tenant or data-isolation boundaries, any path where one party's data could reach another is a Severity-1 defect. (Section 11.)
8. **Partial work is never silently discarded.** A failure or cancellation mid-operation preserves completed work and surfaces a clear, recoverable state.
9. **Diagnose before fixing.** Root cause is established before any fix is written. A fix proposed ahead of diagnosis is rejected.
10. **Every bug becomes a permanent test.** No defect is closed until a regression test reproducing it exists. Severity-1/2 defects also require a root-cause analysis. (Sections 16–17.)
11. **No green full regression run = no done.** The full suite runs before every "done" claim, not a hand-picked subset.
12. **The implementer reports; it does not redesign.** On ambiguity or a blocker, the implementer stops and reports. It never invents UI, folder structures, or behavior it cannot actually see.
13. **Use existing lifecycle scripts before manual process control.** Start, stop, restart, migrate, seed, and test through the repo's maintained scripts ({{PROFILE:lifecycle_scripts}}) first. If a script is missing or wrong, improve the script and rerun it; do not build a parallel manual ritual with ad hoc `kill`, `nohup`, `screen`, or port commands except as diagnosis for fixing the script.

---

## 3. Roles & responsibilities

| Role | Who | Owns | Authority |
|---|---|---|---|
| **Architect / PM** | An AI agent in chat (architecture role) | Acceptance criteria and the test plan (written before code); review of the evidence package; rejection of insufficient evidence. | Can block on inadequate testing. Cannot grant final sign-off. |
| **Implementer** | An AI coding agent or human ({{PROFILE:implementer_vendor}}) | Code **and** tests; the evidence package; reporting blockers rather than guessing. | May never self-declare "done." |
| **Independent reviewer** | A *different vendor* from the implementer ({{PROFILE:reviewer_vendor}}) | Independent review of the evidence package and the diff. | Can block on inadequate testing or thin evidence. |
| **Product Owner / Release Authority** | {{PROFILE:product_owner}} | Independent visual and acceptance testing against the real UI and real fixtures; go/no-go. | **Sole release sign-off.** Nothing ships without it. |
| **Domain Advisor** | Per discipline, where the product has domain-expert correctness requirements | Validation of materiality thresholds and finding correctness for the relevant component. | Approves domain-correctness changes before implementation. |

The separation of implementer from certifier is deliberate: the person who wrote the code is the worst-positioned to certify it, because they unconsciously test it the way they built it.

**Cross-vendor review principle.** Independent review carries weight only when the reviewer is a *different vendor* from the implementer (illustrated pairing: {{PROFILE:implementer_vendor}} ⇄ {{PROFILE:reviewer_vendor}}). A vendor reviewing its own output is `LIMITED_SELF_REVIEW` — permitted only as a stopgap when no cross-vendor reviewer is available, and it never substitutes for true independent review at Gate 2.

---

## 4. Definition of Ready (DoR)

A change may not be **started** until all of the following exist. (This prevents the most common root cause of bad QA: building before the requirement is understood.)

- [ ] The relevant architecture-document section is identified and will be updated **before** code is written.
- [ ] Acceptance criteria are written in Given/When/Then form (Appendix A).
- [ ] Every state machine the change touches is identified (Section 9).
- [ ] Test data and fixtures are identified (Section 13).
- [ ] Non-functional targets are stated where applicable: latency/memory budget, concurrency target, recovery expectation (Section 10).
- [ ] Data-isolation impact is assessed, if the project has isolation boundaries (Section 11).

If acceptance criteria cannot be written clearly, the requirement is not yet understood well enough to build. Stop and clarify.

---

## 5. Definition of Done (DoD)

A change is **DONE** only when **all** of the following are true and **evidenced**. If any box is unchecked, the change is "implemented and awaiting QA" — a lesser status that does not ship.

- [ ] DoR (Section 4) was satisfied before work began.
- [ ] The architecture document was updated before the code was written.
- [ ] All acceptance criteria pass.
- [ ] The coverage matrix (Section 8) was enumerated; every applicable cell has a result; "N/A" entries carry a one-line reason.
- [ ] Every applicable mandatory test type (Section 7) passes with evidence.
- [ ] State/lifecycle tests pass for every state machine touched (Section 9).
- [ ] Non-functional targets are met and measured (Section 10).
- [ ] Data-isolation is verified for every data path touched, if the project has isolation boundaries (Section 11).
- [ ] Negative and edge cases pass — not just the happy path.
- [ ] The **full regression suite** ran and is green; output is in the evidence package.
- [ ] Every bug found during the work has a permanent regression test; S1/S2 bugs have an RCA.
- [ ] The evidence package (Section 18) is submitted and reviewed by the independent reviewer.
- [ ] The Product Owner performed independent acceptance testing and signed off (Section 15).

---

## 6. Test levels (the pyramid)

| Level | Question | Required when |
|---|---|---|
| **Unit** | Does this function behave correctly in isolation, including its edges? | Any function with logic or branching. The broad base of the suite. |
| **Integration** | Do these components work together (DB ↔ service ↔ queue ↔ UI)? | Any change crossing a module boundary. |
| **System** | Does the assembled subsystem meet its functional and non-functional requirements? | Any change to the coordinator, queue, or core pipeline. |
| **End-to-end (E2E)** | Does the full workflow work from input to delivered result? | Any feature touching the core pipeline. |

Most coverage lives at the unit and integration levels (fast, deterministic). E2E is the narrow top — high-value, slower, fewer.

---

## 7. Test types catalog

The types a serious software org runs. Each applicable type is required per the right-hand column.

| Type | What it verifies | Required for |
|---|---|---|
| **Smoke** | Starts and runs without catastrophic failure. | Every change. First and fastest — never the last word. |
| **Sanity** | A specific narrow function behaves after a targeted change. | Targeted fixes. |
| **Functional** | The feature does what the acceptance criteria specify. | Every feature. |
| **Regression** | Nothing that used to work is now broken. | **Every change, in full, before "done."** |
| **State / lifecycle** | All states, transitions, and interruptions behave; invariants hold. | Any multi-state feature (Section 9). |
| **Concurrency** | Correct under multiple simultaneous users/jobs; no races, deadlocks, or corruption. | Anything touching the job queue, DB writes, budget, or shared state. |
| **Data-isolation** | No party can reach another's data. **Release-blocking** where isolation boundaries exist. | Any data-access, query, or storage change (Section 11). |
| **Failure / recovery / resilience** | Graceful behavior under induced faults; completed work preserved; recoverable. | Anything with external API calls, long jobs, or persisted state. |
| **Data integrity** | Persisted data is correct, consistent, and not silently lost or duplicated. | Any change to storage, results, or the run model. |
| **Performance / load** | Meets latency and throughput budgets at target scale. | Large-input and concurrency-sensitive paths (Section 10). |
| **Stress / soak** | Stable beyond normal load and over sustained runs (no leaks). | The coordinator and queue before major releases. |
| **Correctness / materiality** | Significant results surfaced; non-material suppressed; results merged by root cause, not capped. | Any change to a component whose output correctness is product-critical. |
| **Security / privacy** | Keys never logged; decrypt-in-memory only; correct credential mode behavior. | Any change touching credentials, keys, or sensitive data (Section 12). |
| **Usability / UAT** | Does what the Product Owner actually asked for, in the real UI. | Before every sign-off (performed by the Product Owner). |
| **Exploratory** | Uncovers issues scripted tests miss, via unscripted probing. | Encouraged before any release. |

---

## 8. The coverage matrix (dimensions, not scenarios)

The root cause of "small change breaks everything" is testing *scenarios* ("I ran the one input I had and it worked") instead of *dimensions*. For every feature, walk this matrix and produce a result for each applicable cell. "N/A" is valid **with a one-line reason** — never a silent skip.

The categories below are generic dimensions. Project-specific cells — which exact agents/components to exercise, which named fixtures, which workspaces/credential modes exist — are supplied by the project via {{PROFILE:domain_qa_appendices}}; do not hardcode domain examples here.

### 8.1 Identity & access
Single user; multiple concurrent users in one workspace/project; each access level the product defines (e.g. read-only, contributor, admin); cross-boundary invisibility (if isolation boundaries exist); each credential mode the product supports; each workspace/environment context the product defines.

### 8.2 Data shape
Empty/zero-byte; tiny; very large (memory and timeout); malformed/corrupt; wrong type; input with planted known issues (must surface them); clean input with no significant findings (must surface nothing, not fabricate); multiple concurrent inputs.

### 8.3 Concurrency
Two jobs queued at once (claim discipline — no double-processing); per-party concurrency limit reached (excess queues, does not error); budget/resource-reservation race; same resource open in two sessions.

### 8.4 State & lifecycle
Every state and transition of each affected state machine, including interruptions (Section 9).

### 8.5 Failure & recovery
External API timeout; rate limit (429); classifier/safety false positive (verify the legitimate-but-borderline case is not wrongly blocked); crash mid-run → recovery restores state; budget exceeded mid-job → partial results preserved; network drop during input.

### 8.6 Correctness
Each component against its fixture; materiality gate (significant surfaced, non-material suppressed); result merging by root cause (never truncated by a count cap); cross-input synthesis correlates correctly. (Project-specific component list via {{PROFILE:domain_qa_appendices}}.)

### 8.7 Cost & budget
Budget reserved before heavy jobs; configured caps enforced; cost attributed to the correct party.

### 8.8 Performance
Large-input latency and memory within budget; behavior at the target concurrent-user count (Section 10).

---

## 9. State & lifecycle testing

This is the discipline that prevents reactive, one-at-a-time patching. **The reason a small change breaks a feature is almost always an incompletely modeled state machine** — a state or transition that was never defined, so each code path handles it ad hoc and each break is fixed individually.

**The rule.** Any feature with more than one state must have:
1. **A complete state model** — every state, every legal transition, and every interruption (cancel, error, crash, timeout).
2. **Defined invariants** — what must be true after *any* terminal or interrupt transition, from *any* state.
3. **A transition matrix test** — drive the feature into each state, fire each transition/interruption, and assert the invariants every time. One harness, not one patch per crash.

State machines that this rule governs include any job/task lifecycle (worked example below), any multi-state dialog (`running | done | error`, and the easily-missed `cancelled`), one-way flags that must never revert, upload/ingest, queue claim/lease, and resource reservation/release. Identify the equivalents in your project at DoR.

### Worked example — generic job/task lifecycle & cancellation

The recurring cancel bug ("the continue is lost, sometimes it crashes") is the textbook symptom: cancellation was treated as "stop the thread" rather than a modeled state. A lifecycle that models only `running | done | error` but has no `cancel_requested`, no `cancelled`, and no `resuming`, and no concept of *resumable*, leaves nothing to define what must be true after a cancel — so every path invents its own answer.

**Complete state set:** `QUEUED` → `RUNNING` → (`DONE` | `ERROR` | `CANCEL_REQUESTED` → `CANCELLED`); and `CANCELLED`/`ERROR` → `RESUMING` → `RUNNING`. A **unit** is one indivisible work item; each completed unit persists immediately as an immutable result. A **resume cursor** records completed units so resume runs only what is left and never re-runs or duplicates completed work.

**Invariants after ANY cancel/error, asserted in code (not eyeballed):**
- **I1** No orphaned run; status matches reality; nothing stuck in `running`.
- **I2** No dangling concurrent tasks (every parallel task awaited/cancelled; no leaked work).
- **I3** Completed units' results persisted and immutable; nothing partial discarded.
- **I4** Reserved-but-unused budget/resources released; spent budget recorded accurately.
- **I5** Resumable: a cursor exists; resume runs only un-run units; no duplication.
- **I6** Queue integrity: a cancelled job is not silently re-claimed; its lease releases cleanly.
- **I7** UI terminal and correct: the dialog shows `cancelled` (not stuck on `running`) with an accurate completed count and a working resume affordance.
- **I8** Idempotent: double-cancel, cancel-after-done, double-resume cause no corruption.
- **I9** Isolation preserved (if the project has isolation boundaries): resume uses the same isolation key; no cross-boundary resume.

**Transition matrix (the holistic test):** cancel while `QUEUED`; during each running phase (early, mid — some units done, some in-flight, some pending; late/finalization); after `DONE` (safe no-op); of an already-`CANCELLED` job; during `RESUMING`. Each crossed with: graceful vs hard cancel; single vs many units; under concurrency (same and different isolation key); budget near cap. Plus resume cases: resume must produce a result **identical to a never-cancelled run**; resume after process restart; resume an `ERROR`ed job; double-resume. Every cell asserts I1–I9.

Use Appendix F to spec any new state machine the same way.

### Project-specific lifecycle scenarios

Runnable, project-specific lifecycle scenario matrices (the exact click/API/worker sequences, expected DB and UI state per step, cost deltas, and any product-surface progress-UI guards) are domain content. The user supplies them via {{PROFILE:domain_qa_appendices}}. See the worked example in the profile's QA appendices for the shape these take. Do not embed project specifics in this engine directive.

---

## 10. Non-functional requirements (NFRs)

Every NFR has a **measurable target** and a **test that measures it**. "Feels fast" is not a target.

- **Performance budget.** Define maximum latency and peak memory for large-input processing. Test against a genuinely large fixture; assert memory stays bounded and timeouts are handled.
- **Concurrency target.** Define the supported number of simultaneous users/jobs (per-party and platform-wide). Test at that number; assert no races, no lost jobs, no corruption.
- **Recovery objective.** Completed work is never lost (the recovery-point objective is zero lost completed units). Test by inducing a crash mid-job; assert completed results survive and the job resumes.
- **Security/privacy.** Keys are never written to logs; decrypted only in memory at call time (Section 12).

---

## 11. Data-isolation (release-blocking, if the project has isolation boundaries)

This section applies **if your project has tenant or data-isolation boundaries**. Where it applies, isolation is the highest-consequence property of the platform. Any leak is **Severity-1** regardless of how unlikely it appears. (If the project is single-tenant with no isolation boundary, mark this section N/A with that reason.)

For every code path the change touches, run the **cross-boundary probe**: with two isolated parties populated, attempt to read party B's data through every query, filter, parameter, and endpoint the change exposes. Expected result, every time: nothing from party B is returned. Resume, search, synthesis, and reports are included — an isolation boundary must not be crossable through any feature.

Database discipline: every new table carries the project's isolation keys (e.g. workspace/user/tenant identifiers). Isolation tests assert these are enforced, not merely present.

---

## 12. Security & privacy testing

- API keys and secrets are **never logged**, in any mode or error path. A test asserts this against captured logs.
- Keys are decrypted **only in memory at API-call time** (e.g. symmetric encryption with a platform encryption key); never persisted in plaintext, never echoed.
- Each credential mode the product supports is exercised (e.g. platform-managed, customer bring-your-own-key, dedicated client account).
- Privacy language is precise: claims about where data travels must be technically exact (e.g. "data does not pass through our servers" — never an overbroad "data never leaves their network" unless that is literally true).
- Audit-log entries (when present) are immutable; a test asserts append-only behavior.

---

## 13. Test data & fixtures

- **Golden fixtures** are versioned, shared, and **never replaced** — only added to. New features add fixtures; they do not overwrite the regression baseline.
- Required fixture classes: planted-issue inputs (per component), real representative inputs for the domain, a genuinely large input, a clean input with no significant findings, and malformed/wrong-type files. (The specific named fixtures are domain content — supply them via {{PROFILE:domain_qa_appendices}}.)
- Test data carries no real customer PII beyond the approved development set; isolation fixtures are clearly separated to support isolation testing.

---

## 14. Environments & gate criteria

Tests run against an environment that mirrors production behavior for the property under test (the local store/engine locally; production-store behavior verified before release). Each stage gate (Section 15) has explicit **entry criteria** (what must be true to begin) and **exit criteria** (what must be true to pass). A gate is not "mostly passed."

### 14.1 Local process lifecycle

Use the checked-in lifecycle scripts ({{PROFILE:lifecycle_scripts}}) as the operational contract for start, stop, restart, and frontend dev start/stop.

Manual process commands are allowed only to diagnose why a script failed. The durable fix must be added to the script, then verified by rerunning the script. Evidence must show the script output and final process state, including exactly one backend listener and exactly one managed worker when the backend is running.

---

## 15. Stage gates

Quality gates in sequence; a change cannot enter a later gate until earlier ones have exited green.

**Gate 0 — Ready.** Exit: DoR (Section 4) complete.

**Gate 1 — Implementer self-test.** Entry: code complete. Exit: unit + integration + applicable coverage-matrix cells + applicable mandatory test types + applicable state/lifecycle tests + NFR measurements all pass with pasted evidence; evidence package assembled.

**Gate 2 — Independent review / integration.** Entry: evidence package submitted. Exit: an independent reviewer from a *different vendor* than the implementer (§3) confirms the matrix was enumerated (not skipped), confirms the **full regression suite** is green, and confirms isolation and recovery were verified. Returns the change if evidence is thin. (`LIMITED_SELF_REVIEW` is a stopgap only and does not satisfy this gate.)

**Gate 3 — Acceptance / release.** Entry: Gates 0–2 green. Exit: the Product Owner performs independent visual and acceptance testing in the real UI against real fixtures and signs off. **This gate alone authorizes a commit/ship.** No commit before it.

---

## 16. Regression management

- **Every bug becomes a permanent test**, added **before** the fix is considered complete.
- **The full suite runs before every "done" claim** — all of it, not a subset.
- **A red regression test blocks the change** even if the new feature works perfectly. A working feature is never shipped on top of a regression.
- The suite grows monotonically; this is how it catches the *next* small change.

---

## 17. Defect management

| Severity | Definition | Action |
|---|---|---|
| **S1 — Critical** | Data-isolation leak, data corruption, lost client work, security exposure, or total feature failure. | Release-blocking. Stop other work. RCA required. |
| **S2 — Major** | Core workflow broken for a common case; crash on realistic input. | Fix before "done." RCA required. |
| **S3 — Minor** | Edge case fails; degraded but recoverable; visible-but-cosmetic. | Fix before release, or log as a documented known issue with Product Owner approval. |
| **S4 — Trivial** | Rare or low-impact cosmetic issue. | Backlog. |

Any isolation, data-integrity, or lost-work defect is **S1 by definition.** S1/S2 defects require a root-cause analysis (Appendix D) — the five-whys, the missing test that would have caught it, and that test added to the suite.

---

## 18. Evidence, traceability & sign-off

Every "done" claim carries an **evidence package** (Appendix E). No package = not reviewed = not done. It contains: what changed; acceptance criteria with pass/fail; the enumerated coverage matrix; actual pasted test output (unit, integration, concurrency, isolation, recovery — the real output, not summaries); the **full** regression result; new regression tests added; measured NFR results; known limitations; and any blockers the implementer stopped on.

**Traceability:** each acceptance criterion maps to at least one test, and each test maps to a result. A criterion with no test is an untested requirement and blocks "done."

**Forbidden in an evidence package:** "I tested it and it works," "should be fine," "I assume this passes," or any claim about UI behavior the implementer cannot actually see. If the implementer cannot see the UI, it says so and requests a screenshot — it does not describe imagined UI.

**Sign-off authority:** the Product Owner alone authorizes release (Gate 3).

---

## 19. CI / automation gates

As the suite matures, the following block a merge automatically: smoke, the full unit and integration suites, the regression suite, and the data-isolation tests (where isolation boundaries exist). A red automated gate is a hard stop, not a warning. Manual acceptance (Gate 3) remains required on top of automation.

---

## 20. Quality metrics

Tracked over time to show whether quality is improving:
- **Coverage** — proportion of acceptance criteria and matrix cells with tests.
- **Defect escape rate** — defects found after a "done" sign-off. The number this directive is designed to drive toward zero.
- **Regression rate** — how often a change breaks something that worked. The "small change breaks everything" metric.
- **Mean time to recovery (MTTR)** — for S1/S2 defects.
- **Automation percentage** — share of the regression suite that runs without human steps.

---

## 21. Continuous improvement

Every escaped defect (one that reached a sign-off) gets a brief postmortem: what failed, which test was missing, and that test added. This directive is itself versioned; when a class of defect repeats, the directive is amended so the gate that should have caught it now does.

---

## 22. Release readiness checklist

Before any commit to the project repository:

- [ ] DoR and DoD satisfied (Sections 4–5).
- [ ] All three stage gates passed (Section 15).
- [ ] Full regression suite green (evidence attached).
- [ ] Data-isolation verified for every path touched, if isolation boundaries exist (Section 11).
- [ ] State/lifecycle invariants verified for every state machine touched (Section 9).
- [ ] Concurrency verified at the target count (Section 10).
- [ ] Failure/recovery verified (timeout, rate limit, mid-job crash); no completed work lost.
- [ ] NFR targets measured and met (Section 10).
- [ ] Keys never logged; decrypt-in-memory only (Section 12).
- [ ] Product Owner independent acceptance test signed off (Gate 3).
- [ ] Help documentation updated **only** because the feature is now confirmed working.

If any box is unchecked: **do not commit.**

---

## Appendix A — Acceptance criteria template (Given/When/Then)

```
GIVEN  <initial state / preconditions>
WHEN   <action taken>
THEN   <exact, checkable expected result>
```
Write these before implementation. If they can't be written clearly, the requirement isn't understood yet.

## Appendix B — Test case template

```
ID:            <feature>-<area>-<number>
Title:         <one line>
Level:         unit | integration | system | e2e
Type:          smoke | functional | regression | state | concurrency | isolation | recovery | performance | security | correctness
Matrix cell:   <dimension(s) covered>
Fixture:       <input / data set>
Preconditions: <state before>
Steps:         1. … 2. …
Expected:      <exact expected result>
Actual:        <pasted output>
Result:        PASS | FAIL
Severity if failed: S1–S4
```

## Appendix C — Bug report template

```
Title:          <symptom in one line>
Severity:       S1–S4    Priority: P0–P3
Environment:    <local / pre-prod; isolation key; workspace>
Steps:          1. … 2. …
Expected:       <…>
Actual:         <…>
Evidence:       <logs / screenshot / output>
Suspected area: <module>  (diagnosis follows; do not pre-fix)
```

## Appendix D — Root-cause analysis template (required for S1/S2)

```
Defect:           <id / title>
Root cause:       <the actual mechanism, established by diagnosis>
Five whys:        1. … 2. … 3. … 4. … 5. …
Missing test:     <the test that would have caught this>
Test added:       <id of the new permanent regression test>
Directive change: <amendment, if a class of defect repeats>
```

## Appendix E — Evidence package template

```
1. What changed:        <files + one-paragraph summary>
2. Acceptance criteria: <Given/When/Then list, each pass/fail>
3. Coverage matrix:     <each applicable cell + result; N/A with reason>
4. Test output:         <pasted real output: unit, integration, concurrency, isolation, recovery>
5. Regression result:   <pasted output showing the FULL suite green>
6. New regression tests: <tests added for bugs fixed during this work>
7. NFR results:         <measured latency / memory / concurrency vs target>
8. Known limitations:   <what is not covered, and why>
9. Open questions / blocks: <anything stopped on rather than guessed>
```

## Appendix F — State-machine spec template (for any multi-state feature)

```
Feature:        <name>
States:         <list every state, including interrupts: cancel, error, crash, timeout>
Transitions:    <each legal transition: from → to, and its trigger>
Invariants:     <what must be true after every terminal/interrupt transition, from any state>
Transition matrix: <each state × each transition/interruption × variations (concurrency, scale, isolation key)>
Assertions:     <how each invariant is checked in code — not by eyeballing>
```

---

## Appendix G — Domain QA appendices (supplied by the project)

Project-specific QA scenario matrices are **not** part of this portable engine. They are domain content the user supplies via {{PROFILE:domain_qa_appendices}}. These typically include:

- A runnable cancel/continue scenario matrix that exercises the §9 lifecycle transitions end-to-end against the running system (the exact action sequences, expected DB state per step, expected UI state per step, expected log output, and expected cost delta vs. a fresh run — asserting invariants I1–I9 at every terminal/interrupt transition).
- A domain escaped-defect checklist capturing the specific misses the project has experienced (API projection/serialization, live browser verification, plan/objective propagation, full-run vs refresh parity, cancel/continue/restart/reuse, progress truth, parallel-runs and isolation pointers, cost discipline, dispatch contracts, and evidence-package additions).
- Any product-surface progress-UI guards, named components/specialists, named test fixtures, and named workspaces/environments.

See the worked example shipped with the profile template (`profile.template/qa/EXAMPLE_domain_qa_appendices.md`) for the shape these artifacts take, and replace it with your own domain's matrices.

---

## Appendix H — Invoking this directive in agent sessions

AI implementers do not automatically apply this directive. The architect or Product Owner must invoke it explicitly at the start of every session. These are the prompt phrases that force the relevant gate:

**At the start of any new feature or fix:**
> Before writing any code, complete the Definition of Ready (§4) for this task. List the affected architecture-document section, write the acceptance criteria in Given/When/Then form (Appendix A), identify every state machine touched (§9), name the test fixtures, state the non-functional targets (§10), and assess the data-isolation surface (§11). Stop after that — do not write code until I confirm.

**Before declaring done:**
> Per this QA directive §5, paste the Definition of Done checklist with every line checked AND the Evidence Package (Appendix E) attached. If any checkbox is `[ ]` or any evidence section is empty, do not say done — finish those scenarios first.

**For any change touching the cancel/continue lifecycle:**
> This change touches the cancel/continue lifecycle. Per the domain QA appendices ({{PROFILE:domain_qa_appendices}}), run the project's cancel/continue scenario matrix against the running system before saying done. Provide DB queries + UI screenshots per scenario in the Evidence Package (Appendix E).

**For any change touching the project's core analysis/processing pipeline (planning, objective, synthesis, progress, result rendering, cancel/continue, or artifacts):**
> This change touches the core pipeline. Per the domain QA appendices ({{PROFILE:domain_qa_appendices}}), run the project's escaped-defect checklist before saying done. The evidence package must include DB state, API JSON, live browser screenshot/DOM proof, worker/backend version proof after restart, and no-cost regression tests for every field or state touched.

**For any change touching DB queries or storage paths:**
> Run the §11 cross-boundary probe (if isolation boundaries exist). Show the query, the row count, and the content scope at each step. Party B must see zero rows belonging to party A through every query path the change exposes.

**For any change touching the job queue, worker, or shared-state writers:**
> Run the §7 concurrency tests at the §10 target concurrency level. Spawn N concurrent workers/tasks racing the same row and assert exactly one wins. Paste the test output.

**When the implementer cannot see the UI:**
> Do not describe imagined UI behavior. Per §18, the implementer reports what it can actually see and requests a screenshot from the Product Owner for what it cannot. Fabricated UI claims are a directive violation.

**On any defect of Severity S1 or S2:**
> Per §17, no fix is written before an RCA (Appendix D) is completed and a regression test reproducing the defect exists. Diagnose first, then fix.

If an AI implementer ignores these invocations or completes them sloppily, the change is **not done** and the Product Owner rejects the handoff per Gate 3 (§15).

---

## One-line summary

> Done is not "it ran." Done is "every dimension that matters was tested, the full regression is green, isolation holds, no work can be lost, and someone other than the author verified it — with evidence."

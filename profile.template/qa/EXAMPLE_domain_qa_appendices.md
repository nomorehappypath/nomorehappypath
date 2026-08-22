# EXAMPLE domain QA appendices (CRE worked example — profile artifact, NOT engine content)

This is an example of the project-specific QA scenarios a user supplies via {{PROFILE:domain_qa_appendices}}. Replace with your own domain's scenario matrices.

---

## §9 worked-example specifics (CRE job lifecycle)

State machines on this platform that the §9 rule governs include: the **job/analysis lifecycle** (worked example in the engine §9), the **analysis dialog** (`running | done | error`, and the missing `cancelled`), **tier detection** (the one-way Tier-2 flag that must never revert), **upload**, **queue claim/lease**, and **budget reservation/release**.

In the CRE job lifecycle, a **unit** is one `(document, agent)` analysis; each completed unit persists immediately as an immutable Layer-1 finding. The §9 invariant I9 (isolation) is: resume uses the same `tenant_id`; no cross-tenant resume. The transition matrix exercises cancel during Phase 1 (triage), Phase 2 (mid parallel — some units done, some in-flight, some pending), and Phase 3 (synthesis).

### Project Analysis progress UI guard

Project Analysis progress is a validated product surface, not an interchangeable debug panel. Any change touching Project Analysis orchestration, task rows, progress endpoints, polling, or running-state UI must prove these invariants before Gerard is asked to test:

- The running UI preserves the broker-facing progress contract: current activity, stage checklist, synthesis chips, and the per-document/per-specialist review matrix.
- Backend task names, task kinds, chunk IDs, worker IDs, leases, and raw queue events never appear in user-facing copy. Chunked large-document work must read as document sections or analyst review progress, not `doc_specialist_chunk`.
- Progress percentages must include every active phase that the screen claims to represent. A run may not show 100% while synthesis, cleanup, notification, or any other required run phase is still active.
- Dashboard Running Work must use the same backend progress truth as the project page. Durable task rows alone are not enough once synthesis or another job-level phase is active; the UI must include `AIJob.progress` / progress-endpoint `job_progress` before showing a percent.
- Empty fallback text must match the true state. "Waiting for an available worker" is allowed only when work is actually queued and no worker has claimed it; it must not appear while synthesis or another non-task phase is running.
- The large-document path must be tested against the same UI contract as normal documents: old small-project progress, chunked specialist progress, cancellation/restart, and synthesis all keep coherent labels and completion counts.
- Any replacement or redesign of this surface requires explicit Product Owner approval before code lands. Adding a new telemetry panel cannot silently replace the validated progress GUI.

---

## §8.6 / §13 named specifics (CRE)

- §8.6 Agent correctness exercises each of the **12 specialist agents** against its fixture; the materiality gate (adverse surfaced, favorable suppressed); finding merging by root cause (never truncated by a count cap); and cross-document synthesis (Phase 3) correlation.
- §8.1 workspace contexts: `dev_internal`, `najib-testing` (capped), `demo-prospects`. Credential modes: platform-managed, BYOK, dedicated.
- §8.7 budget: `najib-testing` cap enforced.
- §13 required fixture classes include the real test properties: **1503 S. 1st St, Chandler Land, Manor FM1100, the gas station project**, plus a genuinely large document, a clean document with no adverse findings, and malformed/wrong-type files.

---

## Appendix G — Cancel / Continue scenario matrix (CC1–CC13)

The job-lifecycle state machine in §9 enumerates states, transitions, and invariants. This appendix lists the **runnable test scenarios** that exercise those transitions end-to-end against the running system. **Every change touching `analysis_runs`, `ai_jobs`, the analysis task graph, cancellation, or Continue MUST run every applicable scenario below before declaring done.**

For each scenario the implementer produces:
1. The exact action sequence (clicks + API calls + worker actions).
2. The expected DB state at each step (queryable SQL, copy-pastable).
3. The expected UI state at each step (screenshot or detailed description).
4. The expected log output (no surprise errors).
5. The expected cost delta vs. a fresh run (no double-billing).

Invariants I1–I9 from §9 are asserted at every terminal/interrupt transition.

### CC1 — Cancel before any task runs

| Step | Action | Expected |
|---|---|---|
| 1 | Click Run analysis | `analysis_runs.state='running'`, all task rows status='queued' |
| 2 | Within 1 s click Cancel | `cancel_requested_at` set; all task rows flip to status='cancelled' in one UPDATE; run state moves to `cancelled` within 5 s |
| 3 | Check UI | "Cancelled — no work to reuse" message; Continue button hidden or disabled |
| 4 | Check `analysis_artifact` | Zero rows for this run |
| 5 | Check `ai_call_log` | Zero rows |

### CC2 — Cancel mid-specialist phase

| Step | Action | Expected |
|---|---|---|
| 1 | Click Run on a 5-doc project | Tasks fan out, 6+ running |
| 2 | Wait until 3 doc_specialist tasks have completed (artifacts written) | DB confirms 3 artifacts |
| 3 | Click Cancel | `cancel_requested_at` set; queued tasks immediately cancelled; running tasks finish their current provider call (≤ 30 s) then write artifact + go succeeded, OR mark cancelled if cancel was observed mid-step |
| 4 | Run state goes `cancelled` once no `running` tasks remain | Within `analysis_reaper_sweep_seconds + 30 s` |
| 5 | Check UI | "Cancelled — N tasks reusable, you may Continue" |
| 6 | Check Continue button | Enabled |

### CC3 — Continue from CC2

| Step | Action | Expected |
|---|---|---|
| 1 | Click Continue | New `analysis_runs` row created |
| 2 | Reused tasks (the 3+ already-succeeded) | New task rows with `status='reused'`, same `artifact_id` as before, no AI call |
| 3 | Remaining tasks | New rows status='queued', then drained |
| 4 | Check UI | Existing chips green-reused, remaining chips running |
| 5 | Check `ai_call_log` delta | Only new calls for the not-yet-done work |
| 6 | Final state | `state='fresh'`, broker view renders |

### CC4 — Cancel during Continue

| Step | Action | Expected |
|---|---|---|
| 1 | Mid-CC3, click Cancel | Same drain semantics as CC2 |
| 2 | After drain | continue_available STILL true (artifacts from both runs available via SQL lookup) |
| 3 | Click Continue | Picks up where the cancelled Continue left off |

### CC5 — Cancel cascade (cancel a Continue, then cancel again)

| Step | Action | Expected |
|---|---|---|
| 1 | Run → Cancel → Continue → Cancel | Each cancel honored; no chain walkback; no infinite loop in any code path |
| 2 | After 3 cancels | All artifacts from all 3 runs available; next Continue uses the latest fingerprint-matching artifact per task_key |
| 3 | No N-hop limit hit | Content-addressable lookup is O(1) per task, not O(N runs) |

### CC6 — Continue with an added document

| Step | Action | Expected |
|---|---|---|
| 1 | Cancel at CC2 with 5 docs | 3 doc artifacts cached |
| 2 | Upload doc 6 | `documents` row added |
| 3 | Click Continue | New run plan includes doc 6 tasks (new) + docs 1–5 tasks (reused for the 3 done, queued for the 2 not done) + project-level reduce tasks (regenerate because doc_set_hash changed) |
| 4 | Final | Decision_readiness includes doc 6; ≤ 5 themes per category; no truncation |

### CC7 — Continue with edited project notes

| Step | Action | Expected |
|---|---|---|
| 1 | Cancel as in CC2 | Doc artifacts cached |
| 2 | Edit project_note text | `projects.project_note` updated; project_input_fingerprint changes |
| 3 | Click Continue | doc_extract / doc_route artifacts reuse; doc_specialist artifacts invalidate (note hash in fingerprint); doc_cleanup invalidates; reduce tasks invalidate |
| 4 | UI | Honest progress on what reruns |

### CC8 — Continue with edited document notes (single doc)

| Step | Action | Expected |
|---|---|---|
| 1 | Cancel | Artifacts cached for 5 docs |
| 2 | Edit notes on doc 3 only | Doc 3's input_fingerprint changes |
| 3 | Click Continue | Doc 3's specialists + cleanup re-run; docs 1–2, 4–5 reuse; reduce tasks regenerate (doc_set_hash includes per-doc fingerprints) |

### CC9 — Worker death during Continue

| Step | Action | Expected |
|---|---|---|
| 1 | Continue in flight, SIGKILL the worker | tasks with `status='running'` stop pumping heartbeat |
| 2 | Within `analysis_task_stale_after_seconds + analysis_reaper_sweep_seconds` | Reaper flips them to `status='pending'` with attempt+1 |
| 3 | Restart worker | New worker claims with fresh `lease_token`; old worker's any late writes are rejected by lease check |
| 4 | Final | Continue completes; no duplicate artifacts; no double-billed calls |

### CC10 — Two simultaneous Continues from different tabs

| Step | Action | Expected |
|---|---|---|
| 1 | Two tabs same user same project; both click Continue | DB-enforced active-run uniqueness (partial unique index on `analysis_runs(customer_id, project_id) WHERE state IN ('running', 'cancelling')`) rejects the second |
| 2 | Second tab gets 409 with body explaining current state | Plain English: "Analysis already running" |
| 3 | No two runs created | Verified |

### CC11 — Cancel + Refresh-synthesis interaction

| Step | Action | Expected |
|---|---|---|
| 1 | After a `fresh` run, click Refresh synthesis | New run kind=synthesis_only created |
| 2 | Mid-refresh, click Cancel | Synthesis tasks drain; doc-level artifacts untouched |
| 3 | UI returns to prior `fresh` state with the broker view from the previous successful synthesis | No content loss |
| 4 | Click Refresh again | Re-runs synthesis cleanly |

### CC12 — Continue after Postgres connection blip

| Step | Action | Expected |
|---|---|---|
| 1 | During a Continue, restart Postgres (or `pg_terminate_backend` on the worker's connection) | Worker's connection errors; SQL retries via the connection pool |
| 2 | Anthropic call may be in flight — finishes; worker reconnects and writes artifact | Eventual consistency holds |
| 3 | If reconnect fails, task goes to `pending` via heartbeat-stale path | Same as CC9 |

### CC13 — Continue with a cancelled run that has zero artifacts

| Step | Action | Expected |
|---|---|---|
| 1 | Run, immediate cancel (CC1) | No artifacts |
| 2 | Click Continue | continue_available=false (nothing to reuse); UI offers Restart instead |
| 3 | No `IndexError`, no `NoneType`, no cascade walkback fallback | Clean UI message |

---

## Appendix I — Project Analysis escaped-defect checklist

This appendix was added after repeated Project Analysis misses during the task-graph/objective refactor. It is release-blocking for any change touching Project Analysis planning, objective handling, synthesis, progress, result rendering, cancel/continue, artifacts, or task orchestration.

These are not suggestions. Each item below exists because a real defect escaped when only backend unit tests, partial smoke tests, or internal assumptions were used.

### I.1 API projection and schema serialization

**Escaped defect:** `analysis_objective` was correctly saved in `analysis_runs.output`, but the response schema omitted the field, so Pydantic stripped it and the frontend could not render it.

Required tests:
- If a backend value is meant for the UI, assert it survives every layer: DB row -> service/public projection -> API response model -> JSON sent to browser.
- Add a regression test on the public API/projection, not only on the internal service output.
- For Pydantic/FastAPI response models, verify the field exists in the response schema or serialized response. A stored JSON field is not considered delivered until the response model exposes it.
- Evidence must include one DB query showing the stored field and one API/projection output showing the same field.

### I.2 Live browser verification

**Escaped defect:** Code existed in the React component, but the running app did not display the objective because the API never delivered it.

Required tests:
- After backend or frontend changes, restart the actual backend/worker process and verify the running process is on the intended commit/version.
- Open the real local app in a browser or browser automation, not just a TypeScript build.
- Verify the exact user-visible text, button state, progress state, or card exists in the DOM/screenshot.
- If the implementer cannot use a browser, it must say "UI evidence missing" and cannot claim the UI works.
- Evidence must include the page URL, screenshot/DOM text, and the run/project fixture used.

### I.3 Objective and plan propagation

**Escaped defects:** Objective plumbing was claimed complete before the synthesis-refresh path and result API were verified. Manual specialist choices were also not consistently treated as the final truth.

Required tests for any objective/plan change:
- Save plan -> reload project -> plan still confirmed and unchanged.
- Edit objective -> plan confirmation invalidates -> Analyze disabled until reconfirmed.
- Edit objective on a completed/stale result card -> a visible "Confirm objective" or equivalent save action appears without relying on a hidden/lower plan button.
- Confirm objective -> enqueue full analysis -> `AIJob.payload.analysis_objective` contains the objective hash and decision question.
- Run output -> `analysis_runs.output.analysis_objective` contains the same objective hash and decision question.
- Public API -> `output.analysis_objective` contains the same objective.
- Browser result view -> "Project objective" panel is visible with the same decision question.
- Refresh synthesis -> new run preserves the same objective in job payload, run output, slice inputs, public API, and browser result.
- Manual specialist include/exclude choices override system/static eligibility. When user selected manual assignment, the manual list is the only truth for included specialists.
- Evidence must show the objective hash or decision question at each boundary above.

### I.4 Full run vs synthesis-refresh parity

**Escaped defect:** The full-analysis path and synthesis-only refresh path did not carry identical objective context.

Required tests:
- Every field used by synthesis prompts must be tested through both full analysis and synthesis-only refresh.
- Any new synthesis input must have a test that captures `upstream_inputs` for full and refresh runs and asserts parity for the touched field.
- Do not declare "prompt plumbing complete" from full-run tests only.
- If a refresh reuses prior run state, test both modern runs and legacy runs missing the new field. The repair/preservation path must be explicit.

### I.5 Cancel, Continue, Restart, and artifact reuse

**Escaped defects:** Cancel during synthesis returned to the wrong latest-run pointer, Continue did not reuse visible completed work, old pointers leaked through, selected analysts were not visible/enforced after cancel, and the UI confused synthesis-only refresh with a full restart.

Required tests:
- Run Appendix G CC1-CC13 for any lifecycle change.
- Add focused tests for cancel during each Project Analysis stage: preparing documents, public records, assigning specialists, per-document gaps, specialist reviews, missing evidence, synthesis.
- After cancel, UI must show the confirmed analyst plan exactly as saved. The user must be able to change/save the plan or Continue/Restart with clear semantics.
- Continue must reuse completed artifacts and run only missing work. It must not restart completed specialist calls unless the relevant fingerprint changed.
- When a document is added after a completed run, the impact-aware update must analyze that new document's routing, per-document gaps, and specialist reviews, then regenerate synthesis while reusing unchanged prior document artifacts.
- Restart must create a clean new run and must not serve stale progress or stale run output through `latest_analysis_run_id`.
- On completed/stale result cards, "Refresh synthesis" and "Restart full analysis" must be separate visible actions when both are valid. "Refresh synthesis" must never imply specialist reviews will rerun.
- A full restart path must be available from the UI when a completed run is shown, including after a synthesis-only refresh or when testing prompt/specialist changes that require rerunning specialists.
- Evidence must include DB rows for `analysis_runs`, `ai_jobs`, relevant task/artifact rows, and a UI screenshot after cancel.

### I.6 Progress truth

**Escaped defects:** The UI showed vague or stale "running" states, especially during missing-evidence and synthesis steps, leaving the user unable to know what was happening.

Required tests:
- Progress displayed in the UI must be derived from backend state/events, not frontend guesses.
- Each visible stage must have one of: `not_started`, `queued`, `in_progress`, `reused`, `skipped`, `done`, `failed`, `cancelled`.
- Any visible `in_progress` stage must show active motion plus elapsed/updated timing. One-call stages such as evidence-gap review and synthesis must not look static while the backend is working.
- A skipped deterministic step must say why it was skipped, for example objective declares no missing evidence or no raw gap items exist.
- Missing-evidence and diary-building steps must not run or show as active when the objective makes them irrelevant.
- User-facing Project Analysis start/restart flows must not display approximate AI cost or approximate duration. Cost monitoring belongs in internal logs/billing, not in a broker confirmation promise.
- The live UI must be watched through at least one full run and one interrupted run. Capture screenshots for in-progress and terminal states.

### I.7 Parallel projects, latest-run pointers, and tenant boundaries

**Escaped defects:** Parallel runs exposed bad gateway fragility, stale latest-run pointers, and completed/cancelled confusion.

Required tests:
- Start two Project Analysis runs on two different projects in the same tenant. Cancel or fail one while the other continues. Each project must show only its own progress and result.
- Start equivalent runs in two tenants when the touched path reads/writes shared tables. Cross-tenant probes must return zero rows from the other tenant.
- Verify `Project.latest_analysis_run_id` points to the intended current visible run after full run, refresh, cancel, failed run, and repaired/orphaned run.
- Verify no orphan `AIJob.state='running'` remains after terminal analysis states.

### I.8 Cost and live-AI discipline

**Escaped defect:** Plumbing validation was sometimes treated as requiring repeated live AI runs, increasing cost and slowing diagnosis.

Required tests:
- New plumbing must be proven first with no-cost tests: mocked AI calls, fixture outputs, direct DB/API assertions, and deterministic slice tests.
- Live AI runs are acceptance tests, not the primary debugging tool.
- Before any live AI test, record current monthly/test cost and define the maximum additional spend. Stop and ask if the cap would be exceeded.
- For any rerun/Continue/Refresh change, compare `ai_call_log` before and after. The delta must match expected new calls only.
- Artificially low-cap tests are mandatory for cap-handling changes. A run that hits a customer/operator/provider cap must become a recoverable `partial` run with completed work preserved, a specific `partial_reason`, and no generic ProjectAnalysisError crash.

### I.9 Specialist dispatch contract

**Escaped defect:** Project Analysis passed `analysis_objective` into system-selected specialist calls, but one wrapper still used an old function signature and crashed only when the system routed that specialist.

Required tests:
- Any new keyword passed from Project Analysis into specialists must be accepted by every default specialist wrapper, including special wrappers such as legal dual review and large-document chunk execution.
- Add a no-cost dispatcher test that runs the default Project Analysis specialist dispatch path for every `SPECIALIST_DISPATCH_ORDER` entry with system routing, not only a manually selected happy-path analyst.
- The test must assert the exact objective/hash or new field reaches each specialist runner. It is not enough that the run completes.
- If large-document chunk tasks call the same dispatch layer, include at least one chunk-path regression or prove the shared dispatch function is covered.

### I.10 Evidence package additions for Project Analysis

For Project Analysis changes, Appendix E must also include:
- Commit SHA and running backend app/worker version after restart.
- DB evidence for the touched persisted fields or states.
- API JSON/projection evidence for fields consumed by the UI.
- Browser screenshot or DOM text proving the user-visible surface.
- State transition evidence for any touched lifecycle.
- AI cost delta, even when zero.
- Explicit "not covered" list with reasons. Missing browser evidence, missing API serialization evidence, or missing cancel/continue evidence for lifecycle changes means not done.

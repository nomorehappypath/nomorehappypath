# Engine — the portable governance constitution

This directory is the **portable engine**: a vendor-neutral, project-neutral set of
governance, review, and quality directives distilled from a battle-tested multi-agent
development system. **You do not edit these files per project.** Everything
project-specific is supplied by a **profile** (see `../profile.template/`), injected into
these files through `{{PROFILE:*}}` placeholder tokens.

> Golden rule: **the engine is never edited per-project; the profile is the only thing a
> user touches.** Editing an engine file forks you off upgrades.

## Directives (read order)

| # | File | What it governs |
|---|---|---|
| — | `directives/00_AGENT_SPAWN_DIRECTIVE.md` | The single spawn directive — how a new agent boots into the system. |
| 1 | `directives/AGENT_START_HERE.md` | Operating entrypoint, startup sequence, non-negotiables, status vocabulary. |
| 2 | `directives/AGENT_DELIVERY_CONTRACT.md` | Source-of-truth order, task-record requirements, claim discipline, evidence package. |
| 3 | `directives/AGENT_REVIEW_PROTOCOL.md` | **Cross-vendor independent review** — the headline gate. |
| 4 | `directives/CTO_WATCHTOWER_DIRECTIVE.md` | Governance layer — surfaces process risk, never gates quality. |
| 5 | `directives/QA-DIRECTIVE.md` | Canonical quality standard (DoR/DoD, coverage matrix, state-machine testing). |
| 6 | `directives/QA_ENVIRONMENT_MATRIX.md` | Deployment-channel parity evidence requirements. |
| — | `templates/AGENT_TASK_RECORD_TEMPLATE.md` | The per-task work record + evidence package. |

## The token system

Engine files contain `{{PROFILE:key}}` tokens. A user fills the matching key in their
profile (`profile.config`), and a composition step substitutes the values to produce the
**active** directive set the agents actually read.

The 16 canonical tokens:

| Token | Meaning |
|---|---|
| `product_name` | The product / codebase name. |
| `product_owner` | Who states WHAT and gives final product acceptance. |
| `org` | The owning organization. |
| `repos` | The repo(s) the harness governs. |
| `project_root` | Absolute root path the repos live under. |
| `board_path` | The global cross-worktree coordination board path. |
| `implementer_vendor` / `reviewer_vendor` | The two **different** AI vendors (one implements, the other reviews — they swap per task; they must never be the same vendor on one task). |
| `primary_db` | Production-equivalent DB (the authoritative one to verify against). |
| `test_command` / `build_command` / `lint_command` / `migrate_command` | The project's tooling. |
| `lifecycle_scripts` | Start/stop/restart/migrate scripts (the operational contract). |
| `deployment_channels` | The set of environments the project ships to (e.g. local dev, cloud/SaaS, installed client). |
| `domain_qa_appendices` | Include-point for the project's own domain QA scenarios (the user's equivalent of cancel/continue matrices, isolation probes, etc.). |

## Enforcement scripts (`scripts/`)

The thin tooling that *enforces* the constitution. Standalone — system `python3`, no
project virtualenv, stdlib only.

| File | Role |
|---|---|
| `scripts/agent_coord.py` | Coordination CLI: locks, cross-agent inbox notes, and the `merge` / `verify-merge` branch-landing gate. Vendor names are arguments, never hardcoded. |
| `scripts/agent_coord.sh` | Thin `python3` wrapper for the above. |
| `scripts/cto_watchdog.py` | Governance checks (branch discipline, dirty main, task-record AC/env/evidence/merge proof, open CTO holds). Reads `profile.config` for which repos to scan, the deployment channels a record must classify, and the owner label. |
| `scripts/cto_watch.sh` | Thin `python3` wrapper for the watchdog (one-shot or `--watch-interval` monitor). |
| `scripts/profile_config.py` | Stdlib loader for `profile.config` (shared by the watchdog and the composition step). |
| `scripts/compose.py` | **Composition step:** reads `profile.config`, substitutes the `{{PROFILE:*}}` tokens across `engine/`, and writes the fully-resolved active directive set to `build/active/`. Lists render comma-joined; unset tokens render `<unset:key>` + a warning (or fail with `--strict`). |
| `scripts/compose.sh` | Thin `python3` wrapper for compose. |
| `scripts/agent_registry.py` | **Agent identity layer** (orchestration substrate): mint unique per-agent signatures, heartbeat, list ACTIVE/STALE (dead-man's-switch), recycle with generation lineage, retire. Runtime state under `.agents/agents/` (transient). |
| `scripts/agent_registry.sh` | Thin `python3` wrapper for the registry. |
| `scripts/claim.py` | **Claim/assignment** (orchestration routing): post assignable work items; a registered agent atomically claims one (no double-claim); the **cross-vendor rule is enforced at claim** (an item can forbid a vendor). Items under `.agents/queue/` (transient). |
| `scripts/claim.sh` | Thin `python3` wrapper for claim. |
| `scripts/scheduler.py` | **Durable scheduler** (orchestration dispatch core): one `tick` turns each open claim item into a freshly-spawned **right-vendor** agent (cross-vendor respected), claims it, and dispatches via a pluggable runner (default records intent; real spawns the agent CLI). Replaces fragile in-session self-timers (D11). |
| `scripts/scheduler.sh` | Thin `python3` wrapper for the scheduler. |
| `scripts/dashboard.py` | **Live dashboard** (observability): a snapshot of the board (agents + work items) as cards + arrows; agent cards go **red when their heartbeat is stale** (dead-man's-switch). Text view + a self-contained HTML page (SVG arrows). The React product UI consumes the same snapshot. |
| `scripts/dashboard.sh` | Thin `python3` wrapper for the dashboard. |
| `scripts/runner.py` | **Spawn-runner** (autonomy): launches a real agent process for a work item from the profile's `spawn_command` template (vendor/role/task/signature/spawn_directive). **Opt-in** — safe dry-run if no command is set. Records under `.agents/dispatches/`. Wired into `scheduler tick --spawn`. |
| `scripts/runner.sh` | Thin `python3` wrapper for the spawn-runner. |
| `scripts/timer.py` | **OS-timer generator**: renders a launchd plist (macOS) or cron line to run `scheduler tick` durably as a local background service. Installing it is a deliberate manual step (it never auto-loads). |
| `scripts/timer.sh` | Thin `python3` wrapper for the timer generator. |
| `scripts/intake.py` | **Intake + planning** (the front door): plain-English clarifying questions → a build plan (pluggable planner; deterministic default, real LLM is the seam) → decompose into cross-vendor work items on the claim queue. The user only sees plain English (D8). |
| `scripts/intake.sh` | Thin `python3` wrapper for intake. |
| `scripts/run_tests.sh` + `tests/` | Stdlib `unittest` regression suites for the tools. |

With no profile present the watchdog degrades safely (scans the current repo; requires a
`Local dev` row). Point it at a profile with `--profile`, `$DEV_HARNESS_PROFILE`, or by
placing `profile/profile.config` at/above the repo.

## Provenance

Distilled from a governance pack proven in daily use on a real commercial codebase. All
project-specific content (domain rules, tenant-isolation specifics, named test fixtures,
vendor names, paths, DB) was stripped to the profile. A worked example of the
project-specific QA layer lives at `../profile.template/qa/EXAMPLE_domain_qa_appendices.md`.

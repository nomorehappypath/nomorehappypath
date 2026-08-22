<!-- engine directive (portable). Project specifics come from the profile. Source: QA_ENVIRONMENT_MATRIX.md -->
# QA Environment Matrix — Deployment-Channel Parity

**Applies to:** every repo and surface listed in {{PROFILE:repos}}.
**Status:** Mandatory companion to `QA-DIRECTIVE.md`.

## Canonical QA Rule

This pack does not replace `QA-DIRECTIVE.md`. `QA-DIRECTIVE.md` remains the canonical quality standard. If this pack is shorter or less specific than `QA-DIRECTIVE.md`, the QA directive wins.

## 1. Purpose

QA must prove behavior in the environments where the product actually runs:

1. **Local development environment** — the universal baseline; every project has it.
2. **Every deployment channel the project ships to**, as declared in {{PROFILE:deployment_channels}}.

The set of deployment channels is project-specific and comes from {{PROFILE:deployment_channels}}. Local development is always present in addition to those channels. Example channel sets include a cloud/SaaS browser channel and a packaged/installed-client channel, but the authoritative list for any given project is whatever {{PROFILE:deployment_channels}} declares.

A feature is not ready for {{PROFILE:product_owner}} acceptance if a relevant environment has not been verified or explicitly marked N/A with a reason.

## 2. Product deployment model

A project's customer-facing delivery channels are those declared in {{PROFILE:deployment_channels}}. For each declared channel, identify what runs, which backend it talks to, and the QA focus that channel demands. The table below shows the general shape; populate the channel rows from {{PROFILE:deployment_channels}}.

| Channel | What runs | Backend | Required QA focus |
|---|---|---|---|
| Local development | Lifecycle scripts, local backend, local web dev server | Local backend / worker / DB | Fast iteration, unit/integration tests, lifecycle scripts, migrations, local browser proof. |
| Cloud/SaaS browser *(if declared)* | The SPA/app served as a web app | Cloud backend over HTTPS | HTTPS, migrations, tenant isolation, auth, jobs/workers, storage, email/error reporting, deployed version. |
| Packaged/installed client *(if declared)* | Installed client shell serving or loading the SPA locally | Same cloud backend over HTTPS | Installer, local startup, API base URL, heartbeat/license enforcement, offline grace, update prompt, logs, error reporting. |

A packaged/installed client is not, by itself, an on-prem backend. The backend for any client channel remains whatever {{PROFILE:deployment_channels}} declares (typically the shared cloud control plane) unless a separate, explicit architecture says otherwise.

## 3. Environment classification before implementation

Per the Universal Deployment Parity Policy, every deployment channel declared in {{PROFILE:deployment_channels}} is required by default for every feature.

Every task record must mark each environment with one line per environment:

```text
Local dev: REQUIRED | N/A — reason
<each channel from {{PROFILE:deployment_channels}}>: REQUIRED | N/A — reason
```

Use this rule:

- Backend-only logic usually requires local dev and may require a deployment channel if deployment/config/migration/API behavior changes.
- Frontend web behavior usually requires local dev browser proof and may require a deployment channel if served/deployed behavior differs.
- Auth, entitlement, heartbeat, versioning, offline mode, update prompts, installer behavior, filesystem/local runner, and API base URL changes require packaged/installed-client coverage where such a channel is declared.
- Tenant data, storage, projects, documents, analysis, jobs, workers, and reporting require cloud/SaaS coverage before release whenever a multi-tenant SaaS channel is declared.

Readiness for one channel must never be inferred from another. Each declared channel is verified on its own evidence or explicitly marked N/A with a reason.

## 4. Local development QA

All bug fixes must follow the Root-Cause Analysis and Offensive QA Policy.

Use checked-in lifecycle scripts first ({{PROFILE:lifecycle_scripts}}).

Typical evidence:

```bash
<lifecycle script: restart>     # from {{PROFILE:lifecycle_scripts}}
<lifecycle script: dev start>   # from {{PROFILE:lifecycle_scripts}}
{{PROFILE:test_command}} <targeted tests>
{{PROFILE:test_command}} <full or scoped regression suite>
{{PROFILE:test_command}}        # frontend/unit tests if applicable
{{PROFILE:build_command}}
```

Required evidence when applicable:

- Script output showing backend/frontend/worker started successfully.
- Migration output or migration status (`{{PROFILE:migrate_command}}`).
- Exactly one backend API listener and one managed worker when the backend is running.
- Browser URL and screenshot/DOM proof for UI claims.
- API JSON for API claims.
- DB query output for persistence claims.
- Test output with exact command and result.

Manual process commands are allowed only to diagnose a broken lifecycle script. The durable fix must go into the script ({{PROFILE:lifecycle_scripts}}) and be verified by rerunning the script.

## 5. Cloud/SaaS channel QA

This section applies when {{PROFILE:deployment_channels}} declares a cloud/SaaS channel. It is required when a change affects deployed backend behavior, deployed frontend behavior, auth, tenant data, jobs/workers, migrations, storage, email/error reporting, billing/cost, or any user-facing production workflow.

Required evidence when applicable:

1. Environment name and base URL or deployment target. Redact secrets; do not redact the environment identity.
2. App version/commit/build identifier.
3. Migration status (`{{PROFILE:migrate_command}}`).
4. Health endpoint or equivalent service readiness check.
5. Auth check for the relevant role.
6. API request/response JSON for changed endpoints.
7. DB query evidence for changed persistence paths.
8. Worker/job evidence for queued or long-running work.
9. Tenant-isolation probe for every changed data path (see §7; applies when the project is multi-tenant).
10. Error-reporting proof for changed error surfaces.
11. Browser screenshot/DOM proof for changed UI.
12. Rollback or recovery note if deployment fails.

Cloud/SaaS readiness must not be inferred from local {{PROFILE:test_command}} alone.

## 6. Packaged / installed-client channel QA

This section applies when {{PROFILE:deployment_channels}} declares a packaged or installed-client channel. It is required when a change affects:

- Installer or packaged client.
- Client startup/shutdown.
- Local runner or localhost serving.
- API base URL configuration.
- Auth/entitlement/license heartbeat.
- Offline grace behavior.
- Client update notification.
- Local logs, local storage, or local error reporting.
- Any user-facing behavior that may differ from browser/cloud delivery.

Required evidence when applicable:

1. OS and version tested (e.g. Windows and/or macOS as relevant).
2. Installer artifact path/name/version where applicable.
3. App version/build identifier.
4. Local URL or app shell launch proof.
5. Backend base URL used by the local client.
6. Login/auth proof.
7. Entitlement heartbeat proof.
8. Offline grace test: network unavailable → expected read-only/blocked behavior → network restored → recovery.
9. Update prompt proof if versioning changed.
10. Error reporting proof from local client to server endpoint.
11. Log location and redaction proof: no secrets/API keys in logs.
12. Browser/webview screenshot or DOM proof for changed UI.

Packaged/installed-client readiness must not be inferred from `{{PROFILE:build_command}}` alone.

## 7. Multi-tenant cloud isolation (conditional)

This section applies **only if the project is multi-tenant**. If the project serves a single tenant, mark this probe N/A in the task record with that reason.

For a multi-tenant cloud/SaaS channel, any data path must prove tenant isolation.

Minimum probe:

1. Create or identify Tenant A and Tenant B.
2. Seed or locate data for both.
3. Authenticate as Tenant A.
4. Attempt every changed query/filter/endpoint/path that could expose Tenant B data.
5. Expected result: zero Tenant B rows/items/files/artifacts visible to Tenant A.
6. Repeat in the opposite direction if the code path is symmetric.

Evidence must include:

- Tenant identifiers or safe aliases.
- Endpoint/query used.
- Row counts.
- Returned object scopes.
- Confirmation that storage paths include tenant/customer separation where relevant.

Any tenant leak is Severity 1 and blocks release.

Project-specific isolation specifics live in {{PROFILE:domain_qa_appendices}}.

## 8. Error reporting across environments

Every surface must report unhandled errors through the approved error-reporting path.

Environment evidence must show, where applicable:

- Backend global exception handler path.
- Frontend top-level error boundary wrapping the app.
- Global unhandled-error and unhandled-rejection capture in the client.
- Correct reporting endpoint for the admin/user context.
- Client dedupe/rate cap.
- Server rate cap.
- Operator notification/diagnosis path or safe dev substitute.
- Proof that reporting failure does not hide the original user-visible error.

Shipping a new surface without error reporting is a regression.

## 9. Environment evidence table for task records

Use this table in every task record. Add one row per declared channel from {{PROFILE:deployment_channels}}, in addition to the Local dev baseline row:

```md
## Environment QA

| Environment | Required? | Evidence | Result |
|---|---:|---|---|
| Local dev | YES/NO + reason | command/browser/API/DB output | PASS/FAIL/N/A |
| <Cloud/SaaS, if declared> | YES/NO + reason | URL/version/migration/API/tenant/error-reporting evidence | PASS/FAIL/N/A |
| <Packaged/installed client, if declared> | YES/NO + reason | OS/installer/local URL/heartbeat/offline/update evidence | PASS/FAIL/N/A |
```

A blank evidence cell means the task is not ready for review.

## 10. Product-owner acceptance boundary

{{PROFILE:product_owner}} may test product behavior after:

1. Implementer self-test is complete.
2. Independent review has passed.
3. Required evidence is present for the local baseline and every declared channel from {{PROFILE:deployment_channels}}.
4. The task branch is merged to `main` (merge SHA recorded) and the task status is `ACCEPTANCE_READY`. {{PROFILE:product_owner}} tests on `main`.

{{PROFILE:product_owner}} should not be asked to validate migrations, logs, hidden DB state, worker leases, API serialization, tenant isolation, or deployment mechanics. Those are agent responsibilities.

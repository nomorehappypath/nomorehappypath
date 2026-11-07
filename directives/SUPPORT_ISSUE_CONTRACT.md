# Support issue contract (nmhp-support-issue/1)

A small, second shape a paired app (or the owner, in Support Mission
Control) can send to the same intake URL as `nmhp-error/1`
(`directives/ERROR_SINK_CONTRACT.md`), for a problem report or a feature
request that is not a caught exception. The receiver tells the two shapes
apart by the `schema` field.

## The event (one JSON object)

| Field | Type | Required | Limit | Meaning |
|---|---|---|---|---|
| `schema` | string | yes | exactly `nmhp-support-issue/1` | contract version |
| `id` | string | yes | UUID v4 or a stable id | one per report; receivers deduplicate on it |
| `app` | string | yes | ≤ 64 chars | the app's stable name, must match the paired intake URL |
| `env` | string | yes | ≤ 32 chars | the environment, must match the paired intake URL |
| `version` | string | yes | ≤ 64 | app version or commit |
| `time` | string | yes | ISO 8601 UTC | when the report was made |
| `reference` | string | yes | 8 to 12 chars, `[A-Z0-9-]` | shown back to whoever filed it |
| `problem` | bool | one of `problem`/`request` | — | true when this reports something broken |
| `request` | bool | one of `problem`/`request` | — | true when this is a feature wish |
| `summary` | string | yes | ≤ 200 | one short line |
| `steps` | string | no | ≤ 2 000 | how to reproduce, if known |
| `expected` | string | no | ≤ 500 | what should have happened |
| `actual` | string | no | ≤ 500 | what happened instead |
| `where` | string | yes | ≤ 256 | page, route, or feature area |
| `context` | object | no (defaults `{}`) | ≤ 20 keys, allowlisted names, values ≤ 256 chars | only the keys this app/environment's pairing allows |

Whole event ≤ 16 KB. Exactly one of `problem`/`request` must be set; a body
with both or neither is refused.

## Safety

The receiver applies the same rules as `nmhp-error/1` §3 and §5: every field
is allowlisted and scrubbed before storage (secret-shaped text is masked);
any context key not in this app/environment's pairing is dropped; the event
is rejected outright, not stored, if its claimed `app`/`env` do not match
the pairing bound to the bearer secret used to send it. Every free-text
field (`summary`, `steps`, `expected`, `actual`) is stored and shown only as
inert text — a report can describe a command or an instruction, but Support
never executes or obeys one.

## Who sends it

- Server-to-server from the app, the same way it already sends
  `nmhp-error/1` — the browser never sees the intake secret.
- An app-side issue form, when the app wants one, is a separate app task
  that forwards the same shape server-to-server.
- Until either exists for an app, the owner can enter an issue directly in
  that Support installation's own Mission Control.

## What this task does not add

No AI triage or repair reads an issue in this task; it only becomes a case
card (count, first/last seen, plain-English summary line) exactly like an
`nmhp-error/1` event. Triage is Task 2 of the Dedicated Support NMHP build.

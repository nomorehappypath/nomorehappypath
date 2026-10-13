# Central error sink — the contract every app implements (nmhp-error/1)

Owner, 2026-10-12: "a centralized catch-all for all errors for any app we build
so that we can redirect the errors to an intelligent system ... in the future.
just put the infrastructure". This file is that infrastructure: one shape and
one set of settings, so a receiver built once can take every app's errors. The
app implements it in its own language (a few dozen lines); nothing is shipped
as a library. `directives/AGENT.md` ("Central error sink") says when it applies:
every app, on every task.

## 1. One entry point

One module, one function (`report_error(error, where, context)` or the
language's idiom). Everything the app can observe reaches it: the web
framework's error handler; the process-level handlers (uncaught exception,
unhandled promise rejection, or the language's equivalent); threads, tasks,
workers and scheduled jobs; the browser (see §6). Serious handled errors are
reported by calling it: a failed job, a failed outbound call, a refused write.
Not covered, and said so in the README: hard kills, resource exhaustion,
failures before the sink starts.

Fatal errors: write the event synchronously, best effort, bounded by the
timeout below, then exit non-zero. Never resume after an uncaught exception.

## 2. The event (one JSON object per report)

| Field | Type | Required | Limit | Meaning |
|---|---|---|---|---|
| `schema` | string | yes | exactly `nmhp-error/1` | contract version |
| `id` | string | yes | UUID v4 | one per event; receivers deduplicate on it |
| `app` | string | yes | ≤ 64 chars | the app's stable name (set by the server, never by the browser) |
| `version` | string | yes | ≤ 64 | app version or commit |
| `env` | string | yes | ≤ 32 | `development`, `test`, `staging`, `production` or the app's own |
| `source` | string | yes | `server` or `browser` | set by the server |
| `time` | string | yes | ISO 8601 UTC, e.g. `2026-10-12T10:00:00Z` | when the error happened |
| `level` | string | yes | `error` or `fatal` | `fatal` = the process is exiting |
| `kind` | string | yes | ≤ 128 | exception class or error code |
| `message` | string | yes | ≤ 2 000 | scrubbed message |
| `stack` | string | no (null) | ≤ 16 000 | scrubbed stack, top frame first |
| `where` | string | yes | ≤ 256 | route, job name or component |
| `request_id` | string | no (null) | ≤ 64 | the request's id when there is one |
| `reference` | string | yes | 8 to 12 chars, `[A-Z0-9-]` | shown to the user in the plain message where there is a response |
| `fingerprint` | string | yes | 16 hex chars | stable hash of `kind` + `where` + top frame |
| `context` | object | yes (may be `{}`) | ≤ 20 keys, allowlisted names, values ≤ 256 chars | small facts the app chose to attach |

Whole event ≤ 64 KB after scrubbing; longer strings are cut at the limit with
`…`. Unknown fields are dropped by receivers, so put extras in `context`.

## 3. Scrubbing (every field, before the event exists)

Remove or mask: passwords, tokens, API keys, session ids, cookies, the
`Authorization` and `Cookie` headers, request bodies, query strings carrying
them, and anything matching the app's own secret settings' values. Apply to
`message`, `stack`, `where`, `context` and nested causes. `context` is an
allowlist of key names the app declares; any other key is dropped. The test's
proof: a secret planted in a request never appears in any event.

## 4. Destinations (environment settings; never in code)

| Setting | Default | Meaning |
|---|---|---|
| `ERROR_SINK_FILE` | `errors.jsonl` beside the app's logs | append-only JSON lines, one event per line; always on. `-` = standard error (read-only or ephemeral filesystems; the platform's log collector keeps it). |
| `ERROR_SINK_URL` | unset (off) | when set, every event is also POSTed there: `Content-Type: application/json`, body = the event, header `Authorization: Bearer <ERROR_SINK_TOKEN>` |
| `ERROR_SINK_TOKEN` | unset | the bearer token; lives on the server only, never in browser code, logs or events |
| `ERROR_SINK_RATE` | `60` | events per minute per fingerprint beyond which repeats are counted, not written or sent |

File rules: rotate at 10 MB, keep at most 5 rotated files, owner-only
permissions (0600), git-ignored, never inside a folder the app serves. When a
write fails at runtime (denied, disk full), fall back to standard error for
events and counters and say so once.

HTTP rules: HTTPS with certificate check, except loopback addresses; connect
and read timeout 3 s; acknowledgement = any 2xx; at most 3 attempts with
backoff (1 s, 4 s); 3xx is a failure (never follow redirects with the token);
4xx is a failure, not retried; the token is never written anywhere.

Delivery is best effort and said so in the README: a background sender with a
bounded queue (500 events or 8 MB), oldest dropped; at most 2 concurrent sends;
reporting never blocks the request path.

Start-up validation: a malformed URL, token, file path or rate is reported once
on standard error by setting NAME (never its value); the sink keeps local
reporting and does not repeat the complaint per event.

## 5. Limits and counters

Global: at most 600 events per minute overall and 120 per source; the
fingerprint table holds 1 000 entries (least recently seen evicted). Every
minute in which something was dropped or suppressed, write one counter event
(`kind` = `nmhp.sink.counters`, `context` = `{dropped_queue, dropped_rate,
suppressed_repeats, send_failures, write_failures}`) to the same stream. The
sink never re-enters itself: an error inside the writer or sender produces one
plain line on standard error and no second report.

## 6. Browser reports (apps with pages and their own server)

The page's `error` and `unhandledrejection` handlers POST
`{kind, message, stack, where, context}` to the app's own `/api/client-errors`.
The server validates shape and size (≤ 16 KB), sets `app`, `version`, `env`,
`source = browser`, `time`, `id`, `reference` and `fingerprint` itself, applies
§3 and §5 (per-source limit), and never answers a rejected report with another
report. A static site (no server) posts the same body straight to a build-time
receiver URL without a token, or reports nothing and says so in its README.

## 7. Proofs (Delivery Scenario Ledger rows; each only where the app has that part)

1. An induced unhandled server failure: one event in the app's error stream
   with every required field; the user's response is a plain message carrying
   the same `reference` and no stack.
2. A fatal error: one `fatal` event, process exit non-zero.
3. A browser failure: one `browser` event through `/api/client-errors`.
4. `ERROR_SINK_URL` set to a loopback receiver the test starts (settings from
   the test's fixtures, never inherited): the event arrives with the bearer
   header; a dead receiver neither crashes nor slows the app and
   `send_failures` counts it.
5. A planted secret in the failing request appears in no event.
6. A denied write (read-only file path): events and counters fall back to
   standard error, the app keeps serving.
7. A flood of the same error and a flood of distinct fingerprints: the request
   path stays fast, `suppressed_repeats` / `dropped_rate` count them.
8. An error raised inside the sink's own writer: one fallback line, no second
   report.

## 8. README (operating notes the app ships)

Where errors go by default; how to set `ERROR_SINK_URL`, `ERROR_SINK_TOKEN`,
`ERROR_SINK_FILE` and `ERROR_SINK_RATE`; that delivery is best effort and how
drops become visible (the counter events); what the sink cannot cover.

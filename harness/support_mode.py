# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Support mode: Task 1 ("Receive and show") of the Dedicated Support NMHP.

A separately installed instance of this same codebase, started with
`--support`, that one paired app sends its `nmhp-error/1` and
`nmhp-support-issue/1` reports to (directives/ERROR_SINK_CONTRACT.md and
directives/SUPPORT_ISSUE_CONTRACT.md). It groups repeats into case cards and
is read only by the owner through a loopback-only admin page; no AI triage,
repair, or GitHub integration is built here (that is Task 2 and Task 3).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from harness import web_guard

DB_FILENAME = "support_cases.db"
SUPPORT_ADMIN_PORT = 8741

# Reusing directives/ERROR_SINK_CONTRACT.md's own numbers (§2, §5) rather than inventing new ones.
EVENT_MAX_BYTES = 64 * 1024
ISSUE_MAX_BYTES = 16 * 1024
CONTEXT_MAX_KEYS = 20
CONTEXT_VALUE_MAX = 256
RATE_PER_FINGERPRINT_PER_MINUTE = 60
GLOBAL_RATE_PER_MINUTE = 600
PER_SOURCE_RATE_PER_MINUTE = 120
FINGERPRINT_TABLE_MAX = 1000
CASE_EXPIRY_DAYS = 30

ERROR_REQUIRED_FIELDS = (
    "schema", "id", "app", "version", "env", "source", "time", "level",
    "kind", "message", "where", "reference", "fingerprint",
)
ISSUE_REQUIRED_FIELDS = (
    "schema", "id", "app", "env", "version", "time", "reference",
    "summary", "where",
)

# §3 of the error-sink contract: never store these, in any field.
SECRET_LIKE_PATTERN = re.compile(
    r"(?i)(authorization\s*:\s*(?:bearer\s+)?\S+|bearer\s+\S+|password\s*[=:]\s*\S+|token\s*[=:]\s*\S+|"
    r"api[_-]?key\s*[=:]\s*\S+|cookie\s*:\s*\S+)"
)
SECRET_LIKE_REDACTION = "[redacted]"

# §5 of the SPEC and §3 of the error-sink contract also name "query strings" and "direct identifiers"
# as things that must never reach a card, independent of whether they look like a credential.
QUERY_STRING_PATTERN = re.compile(r"\?[A-Za-z0-9_]+=[^\s&]+(?:&[A-Za-z0-9_]+=[^\s&]+)*")
DIRECT_IDENTIFIER_PATTERN = re.compile(
    r"(?i)([\w.+-]+@[\w-]+\.[\w.-]+|"  # an email address
    r"(?:user|account|customer|session|card|ssn|phone|ip)[_-]?(?:id)?\s*[=:]\s*\S+)"  # a direct-identifier key=value/key: value pair
)


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix.

    Return: the current time, e.g. "2026-11-06T17:30:00Z".
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scrub_text(value: str) -> str:
    """Mask secret-shaped, query-string-shaped and direct-identifier
    substrings inside one free-text field.

    Args:
        value: the raw text to scrub (message, stack, summary, steps, ...).

    Return: value with every match of SECRET_LIKE_PATTERN, QUERY_STRING_PATTERN
    and DIRECT_IDENTIFIER_PATTERN replaced; the result is always treated as
    inert display text, never as an instruction or a command, by every
    caller in this module.
    """
    value = SECRET_LIKE_PATTERN.sub(SECRET_LIKE_REDACTION, value)
    value = QUERY_STRING_PATTERN.sub(SECRET_LIKE_REDACTION, value)
    value = DIRECT_IDENTIFIER_PATTERN.sub(SECRET_LIKE_REDACTION, value)
    return value


def _allowlisted_context(value: Any, allowed_keys: frozenset[str]) -> dict[str, str]:
    """Keep only the allowlisted context keys, each cut to a safe length.

    Args:
        value: the event's raw `context` field; anything but a dict yields {}.
        allowed_keys: the set of context key names this pairing accepts.

    Return: a dict with at most CONTEXT_MAX_KEYS entries, each an allowlisted
    key whose value is a scrubbed, length-bounded string.
    """
    if not isinstance(value, dict):
        return {}
    kept: dict[str, str] = {}
    for key, raw in value.items():
        if len(kept) >= CONTEXT_MAX_KEYS:
            break
        if key not in allowed_keys:
            continue
        text = _scrub_text(str(raw))[:CONTEXT_VALUE_MAX]
        kept[key] = text
    return kept


class RejectedEvent(ValueError):
    """An intake event that fails validation; its message is shown to the sender, never stored."""


def validate_error_event(body: dict) -> dict:
    """Check one `nmhp-error/1` event against the contract's required shape.

    Args:
        body: the decoded JSON body of the POST request.

    Return: body unchanged, once every required field is present and the
    event's declared level is "error" or "fatal". Raises RejectedEvent on
    any missing field or an unsafe level value.
    """
    for field in ERROR_REQUIRED_FIELDS:
        if field not in body or body[field] in (None, ""):
            raise RejectedEvent(f"missing required field: {field}")
    if body["level"] not in ("error", "fatal"):
        raise RejectedEvent("level must be error or fatal")
    return body


def validate_issue_event(body: dict) -> dict:
    """Check one `nmhp-support-issue/1` event against its required shape.

    Args:
        body: the decoded JSON body of the POST request.

    Return: body unchanged, once every required field is present and exactly
    one of "problem"/"request" is set. Raises RejectedEvent otherwise.
    """
    for field in ISSUE_REQUIRED_FIELDS:
        if field not in body or body[field] in (None, ""):
            raise RejectedEvent(f"missing required field: {field}")
    if bool(body.get("problem")) == bool(body.get("request")):
        raise RejectedEvent("exactly one of problem or request is required")
    if len(json.dumps(body).encode("utf-8")) > ISSUE_MAX_BYTES:
        raise RejectedEvent("issue report is too large")
    return body


def _plain_english(kind: str, where: str, message: str) -> str:
    """Build the case card's one-line plain-English description.

    No model is called here (Task 1 builds no AI triage); the line is a
    fixed template from the event's own fields.

    Args:
        kind: the event's exception class or error code.
        where: the route, job name, or component the event names.
        message: the event's already-scrubbed message.

    Return: one short plain-English sentence for the case card.
    """
    where_text = f" in {where}" if where else ""
    return f"A {kind} happened{where_text}: {message}"[:300]


def _level_for(event_level: str) -> str:
    """Map the event's raw level to the case card's simple fixed Level rule.

    This task builds no AI triage (reserved for Task 2); "fatal" maps to the
    more severe of the two levels this task assigns, "error" to the other.

    Args:
        event_level: "error" or "fatal", as validated by validate_error_event.

    Return: "Level 2" for a fatal event, "Level 3" otherwise.
    """
    return "Level 2" if event_level == "fatal" else "Level 3"


class PairingSecretMismatch(ValueError):
    """The bearer secret presented does not verify against any pairing."""


class CaseStore:
    """The Support installation's durable local state: pairings and cases.

    One SQLite file under the Support installation's home directory holds
    every table; SQLite's own locking makes concurrent intake requests safe
    without a hand-rolled file lock.
    """

    def __init__(self, home: Path):
        """Open (creating if needed) the case store at home/DB_FILENAME.

        Args:
            home: the Support installation's home directory.

        Return: None.
        """
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / DB_FILENAME
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._init_schema()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _init_schema(self) -> None:
        """Create every table this store needs, if it does not exist yet.

        Return: None.
        """
        with self._lock, self._connection as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS pairings (
                    app TEXT NOT NULL, env TEXT NOT NULL, salt TEXT NOT NULL,
                    secret_hash TEXT NOT NULL, allowed_context TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL,
                    rotated_at TEXT, PRIMARY KEY (app, env)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS cases (
                    fingerprint TEXT PRIMARY KEY, app TEXT NOT NULL, env TEXT NOT NULL,
                    count INTEGER NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    version TEXT NOT NULL, trace_excerpt TEXT NOT NULL, description TEXT NOT NULL,
                    level TEXT NOT NULL, status TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS seen_ids (
                    id TEXT PRIMARY KEY, seen_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS counters (
                    name TEXT PRIMARY KEY, value INTEGER NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS rate_window (
                    fingerprint TEXT NOT NULL, minute TEXT NOT NULL, count INTEGER NOT NULL,
                    PRIMARY KEY (fingerprint, minute)
                )"""
            )

    def close(self) -> None:
        """Close the underlying SQLite connection.

        Return: None.
        """
        with self._lock:
            self._connection.close()

    # -- pairing ---------------------------------------------------------

    def pair(self, app: str, env: str, allowed_context: frozenset[str]) -> str:
        """Issue (or re-issue) one pairing, returning its one-time secret.

        The secret is never stored; only a salted SHA-256 hash of it is.

        Args:
            app: the app's stable name.
            env: the app's environment name.
            allowed_context: the Support-plan-allowed context key names for
                this app/environment (an empty default set in this task).

        Return: the plaintext secret, shown exactly once to the caller.
        """
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        secret_hash = hashlib.sha256((salt + secret).encode("utf-8")).hexdigest()
        with self._lock, self._connection as connection:
            connection.execute(
                "INSERT INTO pairings (app, env, salt, secret_hash, allowed_context, status, created_at, rotated_at) "
                "VALUES (?, ?, ?, ?, ?, 'awaiting_first_event', ?, NULL) "
                "ON CONFLICT(app, env) DO UPDATE SET salt=excluded.salt, secret_hash=excluded.secret_hash, "
                "allowed_context=excluded.allowed_context, status='awaiting_first_event', rotated_at=excluded.created_at",
                (app, env, salt, secret_hash, json.dumps(sorted(allowed_context)), _now()),
            )
        return secret

    def verify_secret(self, app: str, env: str, secret: str) -> bool:
        """Check a bearer secret against the stored verifier for app/env.

        Args:
            app: the app name claimed by the event.
            env: the environment name claimed by the event.
            secret: the bearer secret presented in the Authorization header.

        Return: True if secret verifies against the pairing bound to exactly
        this app/env; False for a wrong secret or an unknown app/env (never
        raises, so a forged identity and a wrong secret look the same to the
        caller).
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT salt, secret_hash FROM pairings WHERE app = ? AND env = ?", (app, env),
            ).fetchone()
        if row is None:
            return False
        expected = hashlib.sha256((row["salt"] + secret).encode("utf-8")).hexdigest()
        return hmac.compare_digest(expected, row["secret_hash"])

    def allowed_context_keys(self, app: str, env: str) -> frozenset[str]:
        """Return the allowlisted context key names paired for app/env.

        Args:
            app: the app name.
            env: the environment name.

        Return: the frozenset of allowed context keys, empty if unpaired.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT allowed_context FROM pairings WHERE app = ? AND env = ?", (app, env),
            ).fetchone()
        return frozenset(json.loads(row["allowed_context"])) if row else frozenset()

    def mark_confirmed(self, app: str, env: str) -> None:
        """Flip a pairing's status from "awaiting_first_event" to "confirmed".

        Args:
            app: the app name.
            env: the environment name.

        Return: None.
        """
        with self._lock, self._connection as connection:
            connection.execute(
                "UPDATE pairings SET status = 'confirmed' WHERE app = ? AND env = ? AND status = 'awaiting_first_event'",
                (app, env),
            )

    def pairing_status(self, app: str, env: str) -> str | None:
        """Return the pairing's own status, or None if it does not exist.

        Args:
            app: the app name.
            env: the environment name.

        Return: "awaiting_first_event", "confirmed", or None.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM pairings WHERE app = ? AND env = ?", (app, env),
            ).fetchone()
        return row["status"] if row else None

    def list_pairings(self) -> list[dict]:
        """List every pairing this installation holds (no secrets).

        Return: a list of {app, env, status, created_at} dicts.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT app, env, status, created_at FROM pairings ORDER BY app, env",
            ).fetchall()
        return [dict(row) for row in rows]

    # -- intake bookkeeping -----------------------------------------------

    def seen_before(self, event_id: str) -> bool:
        """Check and record one event id, for retry deduplication.

        Args:
            event_id: the event's `id` field.

        Return: True if this id was already recorded (a retried event, not
        stored again); False the first time, after recording it.
        """
        with self._lock, self._connection as connection:
            try:
                connection.execute(
                    "INSERT INTO seen_ids (id, seen_at) VALUES (?, ?)", (event_id, _now()),
                )
            except sqlite3.IntegrityError:
                return True
            connection.execute("DELETE FROM seen_ids WHERE seen_at < ?", (_iso_minus_days(7),))
        return False

    _GLOBAL_RATE_KEY = "__global__"
    _SOURCE_RATE_PREFIX = "__source__:"

    def rate_limited(self, fingerprint: str, source: str = "") -> bool:
        """Check and count this minute's events against every bound the
        contract promises: per fingerprint, per source, and overall.

        Args:
            fingerprint: the event's `fingerprint` field.
            source: the event's `source` field ("server"/"browser" for
                nmhp-error/1; "" for an issue report, which the contract
                does not bound per-source).

        Return: True once any of RATE_PER_FINGERPRINT_PER_MINUTE,
        PER_SOURCE_RATE_PER_MINUTE (when source is given) or
        GLOBAL_RATE_PER_MINUTE is exceeded for the current UTC minute (the
        event is counted as a drop, not stored, under the matching counter
        name); False otherwise (and the event is counted against all three).
        """
        minute = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        with self._lock, self._connection as connection:
            connection.execute("DELETE FROM rate_window WHERE minute < ?", (minute,))
        # Every check always runs and counts, even once an earlier one trips: each bound's own
        # counter must reflect every minute it was exceeded, not just the first one that was.
        over_fingerprint = self._bounded(fingerprint, minute, RATE_PER_FINGERPRINT_PER_MINUTE, "dropped_rate")
        over_source = bool(source) and self._bounded(
            self._SOURCE_RATE_PREFIX + source, minute, PER_SOURCE_RATE_PER_MINUTE, "dropped_source_rate",
        )
        over_global = self._bounded(self._GLOBAL_RATE_KEY, minute, GLOBAL_RATE_PER_MINUTE, "dropped_global_rate")
        return over_fingerprint or over_source or over_global

    def _bounded(self, key: str, minute: str, limit: int, counter_name: str) -> bool:
        """Check and count this minute's events under one rate-window key.

        Args:
            key: the rate_window row's key (a fingerprint, a source prefix,
                or the shared global key).
            minute: the current UTC minute, formatted like rate_limited's.
            limit: the most this key may be counted in one minute.
            counter_name: the drop counter to bump once limit is exceeded.

        Return: True once limit is exceeded for key in this minute (and
        counter_name is bumped); False otherwise.
        """
        with self._lock, self._connection as connection:
            row = connection.execute(
                "SELECT count FROM rate_window WHERE fingerprint = ? AND minute = ?", (key, minute),
            ).fetchone()
            count = (row["count"] if row else 0) + 1
            connection.execute(
                "INSERT INTO rate_window (fingerprint, minute, count) VALUES (?, ?, ?) "
                "ON CONFLICT(fingerprint, minute) DO UPDATE SET count = ?",
                (key, minute, count, count),
            )
        if count > limit:
            self.bump_counter(counter_name)
            return True
        return False

    def bump_counter(self, name: str) -> None:
        """Increment one named drop/suppress counter by one.

        Args:
            name: the counter's name (e.g. "dropped_rate", "send_failures").

        Return: None.
        """
        with self._lock, self._connection as connection:
            connection.execute(
                "INSERT INTO counters (name, value) VALUES (?, 1) "
                "ON CONFLICT(name) DO UPDATE SET value = value + 1",
                (name,),
            )

    def counters(self) -> dict[str, int]:
        """Return every drop/suppress counter recorded so far.

        Return: a dict of counter name to count; missing counters read as 0.
        """
        with self._lock:
            rows = self._connection.execute("SELECT name, value FROM counters").fetchall()
        return {row["name"]: row["value"] for row in rows}

    # -- cases --------------------------------------------------------------

    def record_case_event(
        self, *, fingerprint: str, app: str, env: str, version: str,
        trace_excerpt: str, description: str, level: str,
    ) -> None:
        """Insert a new case card or bump an existing one's count.

        Args:
            fingerprint: the event's `fingerprint` field; the card's key.
            app: the app name.
            env: the environment name.
            version: the app version/commit the event reports.
            trace_excerpt: a short, already-scrubbed trace/breadcrumb excerpt.
            description: the plain-English one-line description.
            level: the simple fixed Level rule's result for this event.

        Return: None.
        """
        now = _now()
        with self._lock, self._connection as connection:
            row = connection.execute("SELECT count FROM cases WHERE fingerprint = ?", (fingerprint,)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO cases (fingerprint, app, env, count, first_seen, last_seen, version, "
                    "trace_excerpt, description, level, status) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'new')",
                    (fingerprint, app, env, now, now, version, trace_excerpt, description, level),
                )
            else:
                connection.execute(
                    "UPDATE cases SET count = count + 1, last_seen = ?, version = ? WHERE fingerprint = ?",
                    (now, version, fingerprint),
                )

    def list_cases(self) -> list[dict]:
        """List every non-expired case card, newest activity first.

        Return: a list of case dicts ordered by last_seen descending.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM cases ORDER BY last_seen DESC",
            ).fetchall()
        return [dict(row) for row in rows]

    def expire_old_cases(self) -> int:
        """Delete cases whose last activity is older than CASE_EXPIRY_DAYS,
        unless they are marked "awaiting_decision".

        Return: the number of cases removed.
        """
        cutoff = _iso_minus_days(CASE_EXPIRY_DAYS)
        with self._lock, self._connection as connection:
            cursor = connection.execute(
                "DELETE FROM cases WHERE last_seen < ? AND status != 'awaiting_decision'", (cutoff,),
            )
            return cursor.rowcount


def _iso_minus_days(days: int) -> str:
    """Return an ISO 8601 UTC timestamp `days` days before now.

    Args:
        days: how many days in the past the returned timestamp should be.

    Return: a string in the same format as _now().
    """
    return datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - days * 86400, tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def ingest(store: CaseStore, body: dict) -> dict:
    """Validate, scrub, and record one intake event (error or issue).

    This is the single place both the HTTP handler and the tests call, so
    the handler stays a thin adapter. The event's free-text fields are
    scrubbed and stored as inert display text only; nothing here ever calls
    a model or executes text from the event.

    Args:
        body: the decoded JSON body of the POST request, already past the
            size and secret checks the caller performed.

    Return: {"status": "stored"} or {"status": "duplicate"}. Raises
    RejectedEvent for a malformed or rate-limited event.
    """
    schema = body.get("schema")
    if schema == "nmhp-error/1":
        event = validate_error_event(body)
    elif schema == "nmhp-support-issue/1":
        event = validate_issue_event(body)
    else:
        raise RejectedEvent("unknown schema")

    if store.seen_before(event["id"]):
        return {"status": "duplicate"}

    # Errors carry a fingerprint (required), so repeats of one cause share a card. An issue report has
    # none: it is one owner's report, so its card key is its own id within the app and environment.
    # Publish review 2026-11-07 (Codex R1-1): keying issues by `where` alone merged two unrelated
    # reports about the same page and silently dropped the second one's text.
    fingerprint = event.get("fingerprint") or hashlib.sha256(
        ("issue:" + str(event["app"]) + "/" + str(event.get("env", "")) + "/" + str(event["id"])).encode("utf-8")
    ).hexdigest()[:16]
    if store.rate_limited(fingerprint, str(event.get("source") or "")):
        return {"status": "rate_limited"}

    allowed_context = store.allowed_context_keys(event["app"], event.get("env", ""))
    _allowlisted_context(event.get("context"), allowed_context)  # scrubbed, kept for a future card detail view

    message = _scrub_text(str(event.get("message") or event.get("summary") or ""))
    where = _scrub_text(str(event.get("where") or ""))
    kind = _scrub_text(str(event.get("kind") or event.get("schema") or "issue"))
    trace_excerpt = _scrub_text(str(event.get("stack") or event.get("steps") or ""))[:1000]

    if event["id"] == "nmhp.support.check" or kind == "nmhp.support.check":
        store.mark_confirmed(event["app"], event.get("env", ""))
        return {"status": "paired"}

    store.record_case_event(
        fingerprint=fingerprint,
        app=str(event["app"]),
        env=str(event.get("env") or ""),
        version=str(event.get("version") or ""),
        trace_excerpt=trace_excerpt,
        description=_plain_english(kind, where, message),
        level=_level_for(str(event.get("level") or "error")),
    )
    return {"status": "stored"}


# ------------------------------------------------------------------ HTTP --

def _send_json(handler: BaseHTTPRequestHandler, code: int, value: Any) -> None:
    """Write one complete JSON HTTP response.

    Args:
        handler: the request handler to write the response through.
        code: the HTTP status code.
        value: a JSON-serializable response body.

    Return: None.
    """
    body = json.dumps(value).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_intake_handler(store: CaseStore):
    """Build the public receiver's handler class: intake and health only.

    Args:
        store: the CaseStore every intake request reads and writes.

    Return: a BaseHTTPRequestHandler subclass exposing only /healthz and
    POST /intake/<app>/<env> — never owner controls or case contents.
    """

    class IntakeHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            """Suppress the base handler's default per-request log line."""
            return

        def do_GET(self):
            """Answer only the minimal health check; nothing else is public."""
            if urlparse(self.path).path == "/healthz":
                return _send_json(self, 200, {"status": "ok"})
            return _send_json(self, 404, {"error": "not found"})

        def do_POST(self):
            """Validate the bearer secret, then ingest one intake event."""
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "intake":
                return _send_json(self, 404, {"error": "not found"})
            app, env = parts[1], parts[2]
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return _send_json(self, 400, {"error": "invalid request length"})
            if length <= 0 or length > EVENT_MAX_BYTES:
                return _send_json(self, 413, {"error": "report is too large"})
            raw = self.rfile.read(length)
            auth = self.headers.get("Authorization", "")
            secret = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
            if not secret or not store.verify_secret(app, env, secret):
                store.bump_counter("send_failures")
                return _send_json(self, 401, {"error": "intake secret is invalid"})
            try:
                body = json.loads(raw)
            except ValueError:
                return _send_json(self, 400, {"error": "malformed JSON"})
            if not isinstance(body, dict) or str(body.get("app")) != app or str(body.get("env") or "") != env:
                return _send_json(self, 403, {"error": "event app/environment does not match the paired intake URL"})
            try:
                result = ingest(store, body)
            except RejectedEvent as error:
                return _send_json(self, 422, {"error": str(error)})
            return _send_json(self, 202, result)

    return IntakeHandler


CASE_PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Support — case cards</title></head>
<body>
<h1>Support cases</h1>
<h2>Intake status</h2>
<p>Disconnected intake: <strong>{disconnected_count}</strong> of {paired_count} paired app/environments have not sent an event yet.</p>
<p>Dropped/suppressed so far (never a false all-clear): {counters}</p>
<table border="1" cellpadding="4">
<tr><th>App</th><th>Environment</th><th>Intake status</th><th>Paired since</th><th>Rotate</th></tr>
{pairing_rows}
</table>
<h3>Pair a new app/environment</h3>
<form method="post" action="/support/pair-form">
<input name="app" placeholder="app name" required>
<input name="env" placeholder="environment" required>
<button type="submit">Issue a pairing secret</button>
</form>
<h2>Cases</h2>
{cards}
</body></html>
"""

SECRET_SHOWN_ONCE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Support — new intake secret</title></head>
<body>
<h1>New intake secret for {app} / {env}</h1>
<p>This secret is shown only this once. Put it in that app's protected
<code>ERROR_SINK_TOKEN</code> setting now; it cannot be shown again.</p>
<pre>{secret}</pre>
<p>Intake URL: <code>/intake/{app}/{env}</code></p>
<p><a href="/support">Back to cases</a></p>
</body></html>
"""


def _render_pairing_row(pairing: dict) -> str:
    """Render one pairing as a table row with its intake status and a rotate form.

    Args:
        pairing: one entry from CaseStore.list_pairings().

    Return: an HTML `<tr>` fragment. The status column never shows a plain
    "ok" when intake has not been confirmed: "Awaiting first event" is shown
    plainly instead of a false all-clear.
    """
    status = "Receiving reports" if pairing["status"] == "confirmed" else "Awaiting first event (not yet confirmed)"
    app, env = html.escape(pairing["app"]), html.escape(pairing["env"])
    return (
        "<tr>"
        f"<td>{app}</td><td>{env}</td><td>{html.escape(status)}</td>"
        f"<td>{html.escape(pairing['created_at'])}</td>"
        "<td><form method=\"post\" action=\"/support/rotate-form\" style=\"margin:0\">"
        f"<input type=\"hidden\" name=\"app\" value=\"{app}\">"
        f"<input type=\"hidden\" name=\"env\" value=\"{env}\">"
        "<button type=\"submit\">Rotate secret</button></form></td>"
        "</tr>"
    )


def _render_case_card(case: dict) -> str:
    """Render one case as a small HTML card.

    Args:
        case: one row from CaseStore.list_cases().

    Return: an HTML fragment for this case card.
    """
    return (
        "<div class=\"case\">"
        f"<h2>{html.escape(case['description'])}</h2>"
        f"<p>{html.escape(case['level'])} — count {case['count']} — "
        f"first seen {html.escape(case['first_seen'])} — last seen {html.escape(case['last_seen'])} — "
        f"version {html.escape(case['version'])}</p>"
        f"<pre>{html.escape(case['trace_excerpt'])}</pre>"
        "<p>Next owner decision: not yet available — AI triage is a later step.</p>"
        "</div>"
    )


def make_admin_handler(store: CaseStore):
    """Build the owner-only admin handler: case cards and pairing control.

    Bound to 127.0.0.1 only by the caller (serve_support), reached through
    the owner's existing SSH-tunnel pattern — never on the public interface.

    Args:
        store: the CaseStore this admin page reads and writes.

    Return: a BaseHTTPRequestHandler subclass for the Support Mission
    Control page.
    """

    class AdminHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            """Suppress the base handler's default per-request log line."""
            return

        def refused(self) -> bool:
            """Loopback-only: this page carries owner controls and case content."""
            reason = web_guard.request_refusal(self.command, self.headers, "127.0.0.1")
            path = urlparse(self.path).path
            if reason == "requests must send JSON" and path in ("/support/pair-form", "/support/rotate-form"):
                reason = ""  # the page's own pairing/rotation forms post form-encoded, same-origin (finding B2)
            if reason:
                _send_json(self, 403, {"error": reason})
            return bool(reason)

        def do_GET(self):
            """Serve the case-card page, the pairing list, or the drop counters."""
            if self.refused():
                return
            path = urlparse(self.path).path
            if path == "/" or path == "/support":
                store.expire_old_cases()
                pairings = store.list_pairings()
                disconnected_count = sum(1 for pairing in pairings if pairing["status"] != "confirmed")
                cards = "".join(_render_case_card(case) for case in store.list_cases()) or "<p>No cases yet.</p>"
                pairing_rows = "".join(_render_pairing_row(pairing) for pairing in pairings) \
                    or "<tr><td colspan=\"5\">No app is paired yet.</td></tr>"
                page = CASE_PAGE_TEMPLATE.format(
                    counters=html.escape(json.dumps(store.counters())), cards=cards, pairing_rows=pairing_rows,
                    disconnected_count=disconnected_count, paired_count=len(pairings),
                )
                return self._html(page)
            if path == "/api/support/pairings":
                return _send_json(self, 200, {"pairings": store.list_pairings()})
            if path == "/api/support/cases":
                pairings = store.list_pairings()
                return _send_json(self, 200, {
                    "cases": store.list_cases(), "counters": store.counters(),
                    "disconnected_count": sum(1 for pairing in pairings if pairing["status"] != "confirmed"),
                })
            return _send_json(self, 404, {"error": "not found"})

        def _html(self, body: str) -> None:
            """Write one complete HTML HTTP response.

            Args:
                body: the HTML document to send.

            Return: None.
            """
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            """Pair a new app/environment, or rotate an existing pairing's secret —
            as a JSON API call, or as the case-card page's own HTML forms."""
            if self.refused():
                return
            path = urlparse(self.path).path
            content_type = self.headers.get("Content-Type", "").lower()
            if path in ("/support/pair-form", "/support/rotate-form"):
                if not content_type.startswith("application/x-www-form-urlencoded"):
                    return _send_json(self, 400, {"error": "the pairing form must be form-encoded"})
                length = int(self.headers.get("Content-Length") or 0)
                fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                app = str(fields.get("app", [""])[0])
                env = str(fields.get("env", [""])[0])
                if not app or not env:
                    return self._html("<p>App and environment are both required. "
                                       "<a href=\"/support\">Back</a></p>")
                secret = store.pair(app, env, frozenset())
                return self._html(SECRET_SHOWN_ONCE_TEMPLATE.format(
                    app=html.escape(app), env=html.escape(env), secret=html.escape(secret),
                ))
            if content_type != "application/json":
                return _send_json(self, 400, {"error": "pairing requests must be JSON"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return _send_json(self, 400, {"error": "malformed JSON"})
            if path == "/api/support/pair":
                app, env = str(body.get("app") or ""), str(body.get("env") or "")
                if not app or not env:
                    return _send_json(self, 400, {"error": "app and env are required"})
                secret = store.pair(app, env, frozenset(body.get("allowed_context") or []))
                return _send_json(self, 200, {"app": app, "env": env, "secret": secret})
            return _send_json(self, 404, {"error": "not found"})

    return AdminHandler


def serve_support(home: Path, host: str, port: int, admin_port: int = SUPPORT_ADMIN_PORT):
    """Run Support mode until interrupted: the public receiver and the
    owner-only admin page, each on its own listener.

    Args:
        home: the Support installation's home directory (holds the case store).
        host: the interface the public receiver binds to (0.0.0.0 behind a
            reverse proxy, or 127.0.0.1 for a local-only test).
        port: the public receiver's port.
        admin_port: the admin page's port; always bound to 127.0.0.1.

    Return: None; blocks until interrupted, then closes both servers.
    """
    store = CaseStore(home)
    intake_server = ThreadingHTTPServer((host, port), make_intake_handler(store))
    admin_server = ThreadingHTTPServer(("127.0.0.1", admin_port), make_admin_handler(store))
    intake_thread = threading.Thread(target=intake_server.serve_forever, daemon=True)
    intake_thread.start()
    print(f"NoMoreHappyPath Support receiver on http://{host}:{intake_server.server_address[1]}/ "
          f"(admin: http://127.0.0.1:{admin_server.server_address[1]}/)", flush=True)
    try:
        admin_server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        intake_server.shutdown()
        intake_server.server_close()
        admin_server.server_close()
        store.close()

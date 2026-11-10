# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Support mode: the Dedicated Support NMHP, end to end.

A separately installed instance of this same codebase, started with
`--support`, that one paired app sends its `nmhp-error/1` and
`nmhp-support-issue/1` reports to (directives/ERROR_SINK_CONTRACT.md and
directives/SUPPORT_ISSUE_CONTRACT.md). It groups repeats into case cards,
read only by the owner through a loopback-only admin page, and — once the
owner saves a GitHub repository, a credential, and a positive spending
ceiling in Support settings — walks a case through the rest of the README's
Support chapter: an exact-version checkout, replication with fake data,
one repair by Support's own Delivery agent checked by an other-vendor
reviewer, a pull request, and an owner-only Deploy (owner-key sign-in).
Without a positive ceiling it stays receive-and-show only, exactly as the
original "receive and show" build did.
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
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from harness import support_fix, support_github, support_repair, support_replicate, warning_email, web_guard

DB_FILENAME = "support_cases.db"
SUPPORT_ADMIN_PORT = 8741
GITHUB_TOKEN_FILENAME = "support_github_token"
MAIL_PASSWORD_FILENAME = "support_mail_password"
# Review r3/r4 B2: the owner page is owner-only through a sign-in with an owner key the owner
# chooses on first visit (never generated or printed by Support itself, so no same-account
# process can ever read it). Only the key's hash is kept; sessions live in the admin process
# and end with it.
OWNER_KEY_HASH_FILENAME = "support_owner_key.sha256"
OWNER_COOKIE = "nmhp_support_owner"
OWNER_KEY_MAX_CHARS = 256
# Review r5 B7: a same-account process can try keys against sign-in; a short one is guessable.
OWNER_KEY_MIN_CHARS = 12
SIGN_IN_FAILURES_PER_MINUTE = 5

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
    "schema", "id", "app", "env", "source", "time", "level",
    "kind", "message", "where", "reference", "fingerprint",
)
# `version` is deliberately not required (owner, 2026-11-07, R3): a report with no version, or one GitHub
# does not recognize, must still be received and shown — as "Version unknown, cannot replicate" — never
# guessed and never silently dropped.
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
                    level TEXT NOT NULL, status TEXT NOT NULL,
                    replication TEXT NOT NULL DEFAULT '', replication_notes TEXT NOT NULL DEFAULT '',
                    fix_state TEXT NOT NULL DEFAULT '', fix_notes TEXT NOT NULL DEFAULT '',
                    pull_request_url TEXT NOT NULL DEFAULT '', deployed INTEGER NOT NULL DEFAULT 0,
                    emails_sent TEXT NOT NULL DEFAULT '', last_report_id TEXT NOT NULL DEFAULT ''
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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1), app_repo TEXT NOT NULL DEFAULT '',
                    notice_email TEXT NOT NULL DEFAULT '', mail_host TEXT NOT NULL DEFAULT '',
                    mail_port TEXT NOT NULL DEFAULT '', mail_user TEXT NOT NULL DEFAULT '',
                    spending_ceiling TEXT NOT NULL DEFAULT '', has_github_token INTEGER NOT NULL DEFAULT 0
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

    # -- settings ----------------------------------------------------------

    def save_settings(
        self, *, app_repo: str = "", notice_email: str = "", mail_host: str = "",
        mail_port: str = "", mail_user: str = "", spending_ceiling: str = "",
        github_token: str | None = None, mail_password: str | None = None,
    ) -> None:
        """Save the Support settings page's fields (this installation's one row).

        Non-secret fields overwrite the stored row outright. `github_token` and
        `mail_password` are written to their own 0600 files only when given
        (not None); passing "" clears that secret file. None leaves whichever
        secret already saved untouched, so re-saving the form without retyping
        a secret never erases it.

        Return: None.
        """
        with self._lock, self._connection as connection:
            connection.execute(
                "INSERT INTO settings (id, app_repo, notice_email, mail_host, mail_port, mail_user, spending_ceiling) "
                "VALUES (1, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET app_repo=excluded.app_repo, "
                "notice_email=excluded.notice_email, mail_host=excluded.mail_host, mail_port=excluded.mail_port, "
                "mail_user=excluded.mail_user, spending_ceiling=excluded.spending_ceiling",
                (app_repo, notice_email, mail_host, mail_port, mail_user, spending_ceiling),
            )
        if github_token is not None:
            self._save_secret_file(GITHUB_TOKEN_FILENAME, github_token)
            with self._lock, self._connection as connection:
                connection.execute("UPDATE settings SET has_github_token = ? WHERE id = 1", (1 if github_token else 0,))
        if mail_password is not None:
            self._save_secret_file(MAIL_PASSWORD_FILENAME, mail_password)

    def _save_secret_file(self, filename: str, value: str) -> None:
        """Write one secret to its own 0600 file under this installation's home, or remove it when value is "".

        Args:
            filename: the secret's filename (GITHUB_TOKEN_FILENAME or MAIL_PASSWORD_FILENAME).
            value: the secret's new plaintext value; "" removes the file.

        Return: None.
        """
        path = self.home / filename
        if not value:
            path.unlink(missing_ok=True)
            return
        path.write_text(value, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _secret_file(self, filename: str) -> str:
        """Read one secret file, or "" if it has never been saved."""
        try:
            return (self.home / filename).read_text(encoding="utf-8")
        except OSError:
            return ""

    def settings(self) -> dict[str, Any]:
        """Return the saved Support settings, never including a secret's value.

        Return: {app_repo, notice_email, mail_host, mail_port, mail_user,
        spending_ceiling, has_github_token, has_mail_password}.
        """
        with self._lock:
            row = self._connection.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        if row is None:
            return {
                "app_repo": "", "notice_email": "", "mail_host": "", "mail_port": "", "mail_user": "",
                "spending_ceiling": "", "has_github_token": False, "has_mail_password": False,
            }
        value = dict(row)
        value["has_github_token"] = bool(value.pop("has_github_token", 0))
        value["has_mail_password"] = bool(self._secret_file(MAIL_PASSWORD_FILENAME))
        value.pop("id", None)
        return value

    def github_token(self) -> str:
        """Return the saved GitHub credential, or "" if none is saved."""
        return self._secret_file(GITHUB_TOKEN_FILENAME)

    def mail_password(self) -> str:
        """Return the saved mail password, or "" if none is saved."""
        return self._secret_file(MAIL_PASSWORD_FILENAME)

    # -- owner key ----------------------------------------------------------

    def set_owner_key(self, key: str) -> None:
        """Choose this installation's owner key (its first-visit "Choose your owner key" form).

        Only the key's SHA-256 hash is ever kept; the plaintext is never written, printed or
        logged anywhere, and this is refused when a key already exists (owner_key_exists), so a
        later visitor cannot silently replace the owner's own key.

        Args:
            key: the key the owner typed into the first-visit form.

        Return: None. Raises ValueError if a key is already set, or key is shorter than
        OWNER_KEY_MIN_CHARS characters (review r5 B7: a same-account process can try keys against
        sign-in, so a short key must be refused here, not merely discouraged).
        """
        if self.owner_key_exists():
            raise ValueError("an owner key is already set for this installation")
        if len(key) < OWNER_KEY_MIN_CHARS:
            raise ValueError(f"the owner key must be at least {OWNER_KEY_MIN_CHARS} characters")
        self._save_secret_file(OWNER_KEY_HASH_FILENAME, hashlib.sha256(key.encode("utf-8")).hexdigest())

    def owner_key_exists(self) -> bool:
        """Whether an owner key has been issued for this installation (its hash file exists)."""
        return bool(self._secret_file(OWNER_KEY_HASH_FILENAME).strip())

    def verify_owner_key(self, key: str) -> bool:
        """Check a typed owner key against the stored hash, in constant time.

        Args:
            key: what the owner typed into the sign-in form.

        Return: True only when a key exists and key matches it.
        """
        stored = self._secret_file(OWNER_KEY_HASH_FILENAME).strip()
        if not stored or not key or len(key) > OWNER_KEY_MAX_CHARS:
            return False
        return hmac.compare_digest(hashlib.sha256(key.encode("utf-8")).hexdigest(), stored)

    def mail_settings(self) -> dict[str, Any]:
        """Return this installation's mail settings in warning_email.send_message's own shape.

        Return: {"to": notice_email, "host": mail_host, "port": mail_port, "user": mail_user}.
        """
        value = self.settings()
        return {"to": value["notice_email"], "host": value["mail_host"], "port": value["mail_port"], "user": value["mail_user"]}

    def spending_ceiling(self) -> float:
        """The saved Support spending ceiling as a number.

        Return: the ceiling in dollars; 0.0 when unset, blank, negative or not a number (never guessed).
        """
        try:
            return max(float(self.settings()["spending_ceiling"]), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def spending_ceiling_is_positive(self) -> bool:
        """Whether replication and repair are allowed to run: a positive Support spending ceiling is saved.

        Return: True when the saved spending_ceiling parses as a number greater than 0; False otherwise
        (unset, blank, zero, negative, or not a number — never guessed as on).
        """
        return self.spending_ceiling() > 0

    # -- cases --------------------------------------------------------------

    def record_case_event(
        self, *, fingerprint: str, app: str, env: str, version: str,
        trace_excerpt: str, description: str, level: str, report_id: str = "",
    ) -> bool:
        """Insert a new case card or bump an existing one's count.

        Args:
            fingerprint: the event's `fingerprint` field; the card's key.
            app: the app name.
            env: the environment name.
            version: the app version/commit the event reports.
            trace_excerpt: a short, already-scrubbed trace/breadcrumb excerpt.
            description: the plain-English one-line description.
            level: the simple fixed Level rule's result for this event.

        Return: True if this created a new case (its own pipeline should run); False if it only
        bumped a repeat of an already-processed case (the pipeline is not re-run on a repeat: a
        later occurrence of the same problem must never regress a case Support already fixed).
        """
        now = _now()
        with self._lock, self._connection as connection:
            row = connection.execute("SELECT count FROM cases WHERE fingerprint = ?", (fingerprint,)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO cases (fingerprint, app, env, count, first_seen, last_seen, version, "
                    "trace_excerpt, description, level, status, last_report_id) "
                    "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'received', ?)",
                    (fingerprint, app, env, now, now, version, trace_excerpt, description, level, report_id),
                )
                return True
            connection.execute(
                "UPDATE cases SET count = count + 1, last_seen = ?, last_report_id = ? WHERE fingerprint = ?",
                (now, report_id, fingerprint),
            )
            return False

    def list_cases(self) -> list[dict]:
        """List every non-expired case card, newest activity first.

        Return: a list of case dicts ordered by last_seen descending.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM cases ORDER BY last_seen DESC",
            ).fetchall()
        return [dict(row) for row in rows]

    def get_case(self, fingerprint: str) -> dict | None:
        """Return one case by its fingerprint, or None if it does not exist."""
        with self._lock:
            row = self._connection.execute("SELECT * FROM cases WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return dict(row) if row else None

    def set_case_status(self, fingerprint: str, status: str) -> None:
        """Set one case's lifecycle status (e.g. "version_unknown", "received_only")."""
        with self._lock, self._connection as connection:
            connection.execute("UPDATE cases SET status = ? WHERE fingerprint = ?", (status, fingerprint))

    def set_replication(self, fingerprint: str, replicated: bool, notes: str) -> None:
        """Record this case's replication outcome and what was tried.

        Args:
            fingerprint: the case's key.
            replicated: whether Support reproduced the problem.
            notes: what was tried (always recorded, whether or not it reproduced).

        Return: None.
        """
        with self._lock, self._connection as connection:
            connection.execute(
                "UPDATE cases SET status = ?, replication = ?, replication_notes = ? WHERE fingerprint = ?",
                ("replicated" if replicated else "not_replicated", "replicated" if replicated else "not_replicated",
                 notes, fingerprint),
            )

    def set_fix(self, fingerprint: str, fixed: bool, notes: str) -> None:
        """Record this case's fix outcome.

        Args:
            fingerprint: the case's key.
            fixed: whether the repair passed its independent review.
            notes: why, on a Not fixed outcome.

        Return: None.
        """
        with self._lock, self._connection as connection:
            connection.execute(
                "UPDATE cases SET status = ?, fix_state = ?, fix_notes = ? WHERE fingerprint = ?",
                ("fixed" if fixed else "not_fixed", "fixed" if fixed else "not_fixed", notes, fingerprint),
            )

    def set_pull_request(self, fingerprint: str, url: str) -> None:
        """Record the pull request a reviewed fix became, and move the case to Ready to deploy."""
        with self._lock, self._connection as connection:
            connection.execute(
                "UPDATE cases SET status = 'ready_to_deploy', pull_request_url = ? WHERE fingerprint = ?",
                (url, fingerprint),
            )

    def set_deployed(self, fingerprint: str) -> None:
        """Mark this case Deployed, once GitHub confirms the pull request's merge."""
        with self._lock, self._connection as connection:
            connection.execute(
                "UPDATE cases SET status = 'deployed', deployed = 1 WHERE fingerprint = ?", (fingerprint,),
            )

    def record_email_once(self, fingerprint: str, milestone: str) -> bool:
        """Record that one milestone email was sent for this case, unless it already was.

        Args:
            fingerprint: the case's key.
            milestone: the milestone's name ("received", "replicated", "not_replicated",
                "version_unknown", "fixed", "not_fixed", "ready_to_deploy", "deployed").

        Return: True the first time (record it, send it); False if already recorded (never
        retried, and never a second email for the same milestone).
        """
        with self._lock, self._connection as connection:
            row = connection.execute("SELECT emails_sent FROM cases WHERE fingerprint = ?", (fingerprint,)).fetchone()
            sent = set(filter(None, str(row["emails_sent"] if row else "").split(",")))
            if milestone in sent:
                return False
            sent.add(milestone)
            connection.execute(
                "UPDATE cases SET emails_sent = ? WHERE fingerprint = ?", (",".join(sorted(sent)), fingerprint),
            )
        return True

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


# ------------------------------------------------------------- pipeline --

MILESTONE_SUBJECTS = {
    "received": "Received: {description}",
    "replicated": "Replicated",
    "not_replicated": "Could not replicate",
    "version_unknown": "Version unknown, cannot replicate",
    "fixed": "Fixed",
    "not_fixed": "Could not fix",
    "ready_to_deploy": "Ready to deploy: {pull_request_url}",
    "deployed": "Deployed",
}


def _checkout(repo: str, version: str, scratch_dir: Path, token: str = "") -> Path:
    """Seam for tests: check out one app version. See support_github.checkout_version.

    Review finding B5: a private repository needs the saved credential to clone at all; without
    it the clone fails and the case was wrongly called "Version unknown" instead of what it is —
    a checkout failure. token is passed through unconditionally ("" for a public/local repo).
    """
    return support_github.checkout_version(repo, version, scratch_dir, token=token)


def _replicate(checkout: Path) -> support_replicate.ReplicationResult:
    """Seam for tests: try to reproduce a case. See support_replicate.replicate."""
    return support_replicate.replicate(checkout)


def _run_repair(checkout: Path, brief: str, budget_usd: float) -> support_repair.AgentRun:
    """Seam for tests: start Support's Delivery repair agent. See support_repair.run_repair.

    Review r3 B1: the repair is a real agent run in the checkout — never a command the app's own
    SUPPORT_PLAN.md names, which let a scripted command be called a repair.
    """
    return support_repair.run_repair(checkout, brief, budget_usd)


def _run_review(checkout: Path, brief: str) -> support_repair.AgentRun:
    """Seam for tests: start the independent other-vendor reviewer. See support_repair.run_review."""
    return support_repair.run_review(checkout, brief)


# R5: exactly one repair runs at a time on this Support installation.
_REPAIR_LOCK = threading.Lock()


def _owner_repo(value: str) -> str:
    """Turn an https GitHub URL, or an already-bare "owner/name", into "owner/name" for the
    REST API; a local path (every test here that does not reach real GitHub) is returned as-is
    for support_github's own local-fake-repository handling."""
    text = value.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if "github.com/" in text:
        return text.split("github.com/", 1)[1]
    return text


def _open_pull_request(store: "CaseStore", checkout: Path, case: dict) -> str:
    """Push the repair branch and open its pull request, using the saved GitHub settings.

    Return: the pull request's HTML URL. Raises support_github.GitHubAPIError (including when no
    app repository or credential is saved) — callers record that as the fix staying unpublished.
    """
    settings = store.settings()
    token = store.github_token()
    if not settings["app_repo"] or not token:
        raise support_github.GitHubAPIError("no GitHub repository or credential saved in Support settings")
    branch = f"support/{case['fingerprint']}"
    support_github.push_branch(checkout, branch, token)
    return support_github.open_pull_request(
        owner_repo=_owner_repo(settings["app_repo"]), branch=branch, base="main",
        title=f"Support fix for case {case['fingerprint']}",
        body=f"{case['description']}\n\nOpened by NoMoreHappyPath Support after an independent review passed.",
        token=token,
    )


def _send_milestone_email(store: "CaseStore", case: dict, milestone: str) -> None:
    """Send the one email this milestone promises, exactly once per case, never retried.

    Args:
        store: this installation's CaseStore.
        case: the case's current row (post-update), read fresh so the email's own facts (version,
            pull request link) match what is already on the card.
        milestone: one of MILESTONE_SUBJECTS's keys.

    Return: None. A send failure is counted ("send_failures") and never raised: an email is a
    courtesy, never an approval, and never blocks the case's own progress.
    """
    if not store.record_email_once(case["fingerprint"], milestone):
        return
    settings = store.settings()
    if not settings["notice_email"]:
        return
    subject = "NoMoreHappyPath Support: " + MILESTONE_SUBJECTS[milestone].format(**case)
    body = (
        f"App: {case['app']}\nCase: {case['fingerprint']}\nVersion: {case['version'] or '(unknown)'}\n"
        f"Support page: http://127.0.0.1:{SUPPORT_ADMIN_PORT}/support\n\n"
        "This email reports a status change only; it is never an approval."
    )
    result = warning_email.send_message(store.mail_settings(), store.mail_password(), subject, body)
    if not result.get("sent"):
        store.bump_counter("send_failures")


def process_case(store: "CaseStore", fingerprint: str) -> None:
    """Walk one freshly recorded case through Received, replication, fix, and pull-request —
    exactly as far as the saved settings and the case's own facts allow.

    Return: None. Every step's outcome and its one milestone email are recorded before this
    returns; nothing here is retried later by a different path.
    """
    case = store.get_case(fingerprint)
    if case is None:
        return
    _send_milestone_email(store, case, "received")
    if not str(case.get("version") or "").strip():
        store.set_case_status(fingerprint, "version_unknown")
        _send_milestone_email(store, store.get_case(fingerprint), "version_unknown")
        return
    if not store.spending_ceiling_is_positive():
        store.set_case_status(fingerprint, "receive_and_show_only")
        return
    settings = store.settings()
    if not settings["app_repo"]:
        store.set_replication(fingerprint, False, "no app GitHub repository saved in Support settings")
        _send_milestone_email(store, store.get_case(fingerprint), "not_replicated")
        return
    with tempfile.TemporaryDirectory(prefix="support-") as scratch:
        try:
            checkout = _checkout(settings["app_repo"], case["version"], Path(scratch), store.github_token())
        except support_github.GitCheckoutFailed:
            store.set_case_status(fingerprint, "version_unknown")
            _send_milestone_email(store, store.get_case(fingerprint), "version_unknown")
            return
        result = _replicate(checkout)
        store.set_replication(fingerprint, result.replicated, "; ".join(result.attempts))
        _send_milestone_email(store, store.get_case(fingerprint), "replicated" if result.replicated else "not_replicated")
        if not result.replicated:
            return
        with _REPAIR_LOCK:
            fix = support_fix.attempt_fix(
                checkout, case, case["version"], budget_usd=store.spending_ceiling(),
                run_repair=_run_repair, run_review=_run_review,
            )
        store.set_fix(fingerprint, fix.fixed, fix.notes)
        _send_milestone_email(store, store.get_case(fingerprint), "fixed" if fix.fixed else "not_fixed")
        if not fix.fixed:
            return
        try:
            pull_request_url = _open_pull_request(store, checkout, store.get_case(fingerprint))
        except support_github.GitHubAPIError as error:
            store.bump_counter("send_failures")
            store.set_fix(fingerprint, True, f"fixed and reviewed, but not yet published: {error}")
            return
        store.set_pull_request(fingerprint, pull_request_url)
        _send_milestone_email(store, store.get_case(fingerprint), "ready_to_deploy")


def deploy_case(store: "CaseStore", fingerprint: str) -> dict:
    """Merge a Ready-to-deploy case's pull request, with the owner's own saved GitHub credential.

    Args:
        store: this installation's CaseStore.
        fingerprint: the case to deploy; must already carry a pull_request_url.

    Return: {"deployed": True} on a confirmed GitHub merge, or {"deployed": False, "error": "..."}
    on any refusal (the case's status is left exactly as it was — never guessed as deployed).
    """
    case = store.get_case(fingerprint)
    if case is None or not case.get("pull_request_url"):
        return {"deployed": False, "error": "this case has no pull request to deploy yet"}
    token = store.github_token()
    if not token:
        return {"deployed": False, "error": "no GitHub credential saved in Support settings"}
    try:
        owner_repo, pull_number = support_github.owner_repo_and_pull_number(case["pull_request_url"])
        response = support_github.merge_pull_request(owner_repo=owner_repo, pull_number=pull_number, token=token)
    except (ValueError, support_github.GitHubAPIError) as error:
        store.bump_counter("send_failures")
        return {"deployed": False, "error": str(error)}
    if not response.get("merged"):
        return {"deployed": False, "error": str(response.get("message") or "GitHub did not confirm the merge")}
    store.set_deployed(fingerprint)
    _send_milestone_email(store, store.get_case(fingerprint), "deployed")
    return {"deployed": True}


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

    is_new = store.record_case_event(
        fingerprint=fingerprint,
        app=str(event["app"]),
        env=str(event.get("env") or ""),
        version=str(event.get("version") or ""),
        trace_excerpt=trace_excerpt,
        description=_plain_english(kind, where, message),
        level=_level_for(str(event.get("level") or "error")),
        report_id=str(event.get("id") or ""),
    )
    if is_new:
        process_case(store, fingerprint)
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


SUPPORT_SETTINGS_TEMPLATE = """<h2>Support settings</h2>
<p>{ceiling_notice}</p>
<form method="post" action="/support/settings">
<div><label>App GitHub repository <input name="app_repo" value="{app_repo}" placeholder="https://github.com/owner/app" size="40"></label></div>
<div><label>GitHub credential (push support/ branches, open and merge pull requests) <input type="password" name="github_token" placeholder="{github_token_placeholder}" size="40"></label></div>
<div><label>Owner notice email <input name="notice_email" value="{notice_email}" size="40"></label></div>
<div><label>Mail server host <input name="mail_host" value="{mail_host}" size="30"></label>
<label>Port <input name="mail_port" value="{mail_port}" size="6"></label></div>
<div><label>Mail server user name <input name="mail_user" value="{mail_user}" size="30"></label>
<label>Mail server password <input type="password" name="mail_password" placeholder="{mail_password_placeholder}" size="20"></label></div>
<div><label>Support spending ceiling (replication and repair run only above zero) <input name="spending_ceiling" value="{spending_ceiling}" size="10"></label></div>
<button type="submit">Save</button>
</form>
<p>Save first, then Send a test email — the test uses only what is saved.</p>
<form method="post" action="/support/settings/test-email"><button type="submit">Send a test email</button></form>
"""

CASE_PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Support — case cards</title></head>
<body>
<h1>Support cases</h1>
{owner_html}
<h2>Intake status</h2>
<p>Disconnected intake: <strong>{disconnected_count}</strong> of {paired_count} paired app/environments have not sent an event yet.</p>
<p>Dropped/suppressed so far (never a false all-clear): {counters}</p>
<table border="1" cellpadding="4">
<tr><th>App</th><th>Environment</th><th>Intake status</th><th>Paired since</th><th>Rotate</th></tr>
{pairing_rows}
</table>
{pairing_form_html}
<h2>Cases</h2>
{cards}
</body></html>
"""

OWNER_SIGN_IN_TEMPLATE = """<h2>Owner sign-in</h2>
<p>This page is read-only until you sign in with the owner key you chose when Support first started.
To choose a new one, delete <code>{hash_filename}</code> in the Support home folder and restart Support.</p>
<form method="post" action="/support/sign-in">
<label>Owner key <input type="password" name="owner_key" size="40" maxlength="{max_chars}" autocomplete="off"></label>
<button type="submit">Sign in</button>
</form>
"""

OWNER_CHOOSE_KEY_TEMPLATE = """<h2>Choose your owner key</h2>
<p>Nobody has set an owner key for this Support installation yet. Choose one now: it is never shown,
printed or logged again, only its hash is kept, and it is what signs you in as the owner from now
on (settings, pairing and Deploy all need it).</p>
<form method="post" action="/support/choose-key">
<label>Owner key (at least {min_chars} characters) <input type="password" name="owner_key" size="40" minlength="{min_chars}" maxlength="{max_chars}" autocomplete="off"></label>
<button type="submit">Choose</button>
</form>
"""

OWNER_SIGNED_IN_TEMPLATE = """<p>Signed in as owner.
<form method="post" action="/support/sign-out" style="display:inline"><button type="submit">Sign out</button></form></p>
"""

PAIRING_FORM_TEMPLATE = """<h3>Pair a new app/environment</h3>
<form method="post" action="/support/pair-form">
<input name="app" placeholder="app name" required>
<input name="env" placeholder="environment" required>
<button type="submit">Issue a pairing secret</button>
</form>
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


def _case_status_line(case: dict, action_token: str, signed_in: bool) -> str:
    """Build the one line that says this case's current stage, in the owner's own words.

    Args:
        case: one row from CaseStore.list_cases() / get_case().
        action_token: this admin process's one-time Deploy action token (review r1 B2): a random
            value generated once per server launch and embedded in the form as a hidden field, so a
            Deploy press also proves it came from a page this process itself served (a cross-site
            form cannot know it).
        signed_in: whether the viewer holds an owner session (review r3 B2). Only then is the
            Deploy form rendered at all; anyone else sees how to sign in instead.

    Return: an escaped HTML fragment naming the stage (Received, Version unknown cannot
    replicate, Replicated/Not replicated with what was tried, Fixed/Not fixed with why,
    Ready to deploy with the pull request link and a Deploy button, or Deployed), or the
    receive-and-show-only notice when no spending ceiling is set.
    """
    status = case["status"]
    if status == "version_unknown":
        return "<p><strong>Version unknown, cannot replicate.</strong></p>"
    if status == "receive_and_show_only":
        return (
            "<p><strong>Received only — no spending ceiling is set, so Support is receive-and-show only.</strong></p>"
            "<p><strong>Not replicated.</strong> What was tried: nothing — no spending ceiling is set.</p>"
            "<p><strong>Not fixed.</strong> No spending ceiling is set.</p>"
        )
    parts = ["<p><strong>Received.</strong></p>"]
    if case["replication"] == "replicated":
        parts.append(f"<p><strong>Replicated.</strong> {html.escape(case['replication_notes'])}</p>")
    elif case["replication"] == "not_replicated":
        parts.append(f"<p><strong>Not replicated.</strong> What was tried: {html.escape(case['replication_notes'])}</p>")
    if case["fix_state"] == "fixed":
        parts.append(f"<p><strong>Fixed.</strong> {html.escape(case['fix_notes'])}</p>")
    elif case["fix_state"] == "not_fixed":
        parts.append(f"<p><strong>Not fixed.</strong> {html.escape(case['fix_notes'])}</p>")
    if case["status"] == "ready_to_deploy" and case["pull_request_url"]:
        url = html.escape(case["pull_request_url"])
        parts.append(f"<p><strong>Ready to deploy.</strong> Pull request: <a href=\"{url}\">{url}</a></p>")
        if signed_in:
            parts.append(
                f"<form method=\"post\" action=\"/support/deploy\"><input type=\"hidden\" name=\"fingerprint\" "
                f"value=\"{html.escape(case['fingerprint'])}\">"
                f"<input type=\"hidden\" name=\"action_token\" value=\"{html.escape(action_token)}\">"
                f"<button type=\"submit\">Deploy</button></form>"
            )
        else:
            parts.append("<p>Only the owner can deploy: sign in with the owner key at the top of this page.</p>")
    if case["deployed"]:
        parts.append("<p><strong>Deployed.</strong></p>")
    return "".join(parts)


def _render_case_card(case: dict, action_token: str, signed_in: bool) -> str:
    """Render one case as a small HTML card.

    Args:
        case: one row from CaseStore.list_cases().
        action_token: this admin process's Deploy action token; see _case_status_line.
        signed_in: whether the viewer holds an owner session; see _case_status_line.

    Return: an HTML fragment for this case card.
    """
    return (
        "<div class=\"case\">"
        f"<h2>{html.escape(case['description'])}</h2>"
        f"<p>{html.escape(case['level'])} — count {case['count']} — "
        f"first seen {html.escape(case['first_seen'])} — last seen {html.escape(case['last_seen'])} — "
        f"version {html.escape(case['version'] or '(unknown)')} — report id {html.escape(case['last_report_id'])}</p>"
        f"<pre>{html.escape(case['trace_excerpt'])}</pre>"
        f"{_case_status_line(case, action_token, signed_in)}"
        "</div>"
    )


def _render_settings(store: CaseStore) -> str:
    """Render the Support settings section: never a saved secret's value, just whether one is saved.

    Args:
        store: this installation's CaseStore.

    Return: an HTML fragment for the top of the case-card page.
    """
    settings = store.settings()
    ceiling_notice = (
        "Replication and repair run normally." if store.spending_ceiling_is_positive()
        else "No positive spending ceiling is set: Support is receive-and-show only until one is."
    )
    return SUPPORT_SETTINGS_TEMPLATE.format(
        ceiling_notice=html.escape(ceiling_notice),
        app_repo=html.escape(settings["app_repo"]),
        github_token_placeholder="a credential is saved (leave blank to keep it)" if settings["has_github_token"] else "not set",
        notice_email=html.escape(settings["notice_email"]),
        mail_host=html.escape(settings["mail_host"]),
        mail_port=html.escape(settings["mail_port"]),
        mail_user=html.escape(settings["mail_user"]),
        mail_password_placeholder="a password is saved (leave blank to keep it)" if settings["has_mail_password"] else "not set",
        spending_ceiling=html.escape(settings["spending_ceiling"]),
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
    # Review r1 B2: loopback-only is not owner-only — any other local process can reach
    # 127.0.0.1 too. This one-time, per-launch random value is embedded as a hidden field in the
    # Deploy form this process itself renders, so merging a pull request proves the request came
    # from a page this process just served, the same "action token" pattern project_manager.py's
    # own owner-only Close-project button already uses.
    deploy_action_token = secrets.token_urlsafe(32)
    # Review r3 B2: a token read from the page, and a browser header, are both things a local
    # agent's own HTTP client can supply. An owner session is not: it comes only from the owner key
    # the owner chose on first visit, never generated or shown by Support itself. Sessions live
    # here, in this process, and end with it.
    owner_sessions: set[str] = set()
    sign_in_failures: list[float] = []
    sessions_lock = threading.Lock()
    owner_only_paths = (
        "/support/pair-form", "/support/rotate-form", "/support/settings", "/support/settings/test-email",
        "/support/deploy", "/support/sign-out", "/api/support/pair",
    )

    class AdminHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            """Suppress the base handler's default per-request log line."""
            return

        def refused(self) -> bool:
            """Loopback-only: this page carries owner controls and case content."""
            reason = web_guard.request_refusal(self.command, self.headers, "127.0.0.1")
            path = urlparse(self.path).path
            same_origin_form_paths = (
                "/support/pair-form", "/support/rotate-form", "/support/sign-in", "/support/sign-out",
                "/support/choose-key", "/support/settings", "/support/settings/test-email", "/support/deploy",
            )
            if reason == "requests must send JSON" and path in same_origin_form_paths:
                reason = ""  # the page's own pairing/rotation/settings/deploy forms post form-encoded, same-origin (finding B2)
            if reason:
                _send_json(self, 403, {"error": reason})
            return bool(reason)

        def owner_signed_in(self) -> bool:
            """Whether this request carries a live owner session cookie (review r3 B2).

            Return: True only when the cookie names a session this process issued after a correct
            owner key; a missing, stale or forged cookie is False.
            """
            cookie = SimpleCookie()
            try:
                cookie.load(str(self.headers.get("Cookie") or ""))
            except Exception:  # a malformed cookie header is simply "not signed in", never an error page
                return False
            morsel = cookie.get(OWNER_COOKIE)
            if morsel is None:
                return False
            with sessions_lock:
                return any(hmac.compare_digest(morsel.value, session) for session in owner_sessions)

        def do_GET(self):
            """Serve the case-card page, the pairing list, or the drop counters."""
            if self.refused():
                return
            path = urlparse(self.path).path
            if path == "/" or path == "/support":
                store.expire_old_cases()
                signed_in = self.owner_signed_in()
                pairings = store.list_pairings()
                disconnected_count = sum(1 for pairing in pairings if pairing["status"] != "confirmed")
                cards = "".join(_render_case_card(case, deploy_action_token, signed_in) for case in store.list_cases()) \
                    or "<p>No cases yet.</p>"
                pairing_rows = "".join(_render_pairing_row(pairing) for pairing in pairings) \
                    or "<tr><td colspan=\"5\">No app is paired yet.</td></tr>"
                if signed_in:
                    owner_html = OWNER_SIGNED_IN_TEMPLATE + _render_settings(store)
                    pairing_form_html = PAIRING_FORM_TEMPLATE
                elif not store.owner_key_exists():
                    owner_html = OWNER_CHOOSE_KEY_TEMPLATE.format(min_chars=OWNER_KEY_MIN_CHARS, max_chars=OWNER_KEY_MAX_CHARS)
                    pairing_form_html = "<p>Choose the owner key above to pair a new app or rotate a secret.</p>"
                else:
                    owner_html = OWNER_SIGN_IN_TEMPLATE.format(hash_filename=OWNER_KEY_HASH_FILENAME, max_chars=OWNER_KEY_MAX_CHARS)
                    pairing_form_html = "<p>Sign in as owner to pair a new app or rotate a secret.</p>"
                page = CASE_PAGE_TEMPLATE.format(
                    counters=html.escape(json.dumps(store.counters())), cards=cards, pairing_rows=pairing_rows,
                    disconnected_count=disconnected_count, paired_count=len(pairings),
                    owner_html=owner_html, pairing_form_html=pairing_form_html,
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

        def _html(self, body: str, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
            """Write one complete HTML HTTP response.

            Args:
                body: the HTML document to send.
                status: the HTTP status code (200 unless a refusal page).
                extra_headers: further response headers (a Set-Cookie on sign-in or sign-out).

            Return: None.
            """
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _form_fields(self) -> dict[str, list[str]]:
            """Read and decode a small form-encoded body (owner forms are tiny; anything larger is cut).

            Return: the parsed fields; {} when the body is not form-encoded.
            """
            if not self.headers.get("Content-Type", "").lower().startswith("application/x-www-form-urlencoded"):
                return {}
            length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
            return parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)

        def _choose_key(self) -> None:
            """Handle the first-visit "Choose your owner key" form (review r4 B2).

            Refused once an owner key already exists, so only the first visitor can set it. On
            success, stores only the key's hash (CaseStore.set_owner_key) and signs that caller in
            immediately, the same way a right key on /support/sign-in would.

            Return: None.
            """
            if store.owner_key_exists():
                return _send_json(self, 409, {"error": "an owner key is already set; sign in with it instead"})
            typed = str(self._form_fields().get("owner_key", [""])[0])
            try:
                store.set_owner_key(typed)
            except ValueError as error:
                return self._html(f"<p>{html.escape(str(error))} <a href=\"/support\">Back</a></p>", 400)
            session = secrets.token_urlsafe(32)
            with sessions_lock:
                owner_sessions.add(session)
            self.send_response(303)
            self.send_header("Location", "/support")
            self.send_header("Set-Cookie", f"{OWNER_COOKIE}={session}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _sign_in(self) -> None:
            """Open an owner session when the typed owner key is right (review r3 B2).

            Five wrong keys within a minute pause sign-in for the rest of that minute, so the key
            cannot be guessed by a local process trying quickly. A right key answers with a
            session cookie (HttpOnly, SameSite=Strict, this process only) and returns to /support.

            Return: None.
            """
            now = time.monotonic()
            with sessions_lock:
                sign_in_failures[:] = [moment for moment in sign_in_failures if now - moment < 60]
                if len(sign_in_failures) >= SIGN_IN_FAILURES_PER_MINUTE:
                    return self._html("<p>Too many wrong owner keys. Wait a minute, then <a href=\"/support\">try again</a>.</p>", 429)
            typed = str(self._form_fields().get("owner_key", [""])[0])
            if not store.verify_owner_key(typed):
                with sessions_lock:
                    sign_in_failures.append(now)
                return self._html("<p>That is not the owner key. <a href=\"/support\">Back</a></p>", 403)
            session = secrets.token_urlsafe(32)
            with sessions_lock:
                owner_sessions.add(session)
            self.send_response(303)
            self.send_header("Location", "/support")
            self.send_header("Set-Cookie", f"{OWNER_COOKIE}={session}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _sign_out(self) -> None:
            """End this owner session and clear its cookie. Return: None."""
            cookie = SimpleCookie()
            cookie.load(str(self.headers.get("Cookie") or ""))
            morsel = cookie.get(OWNER_COOKIE)
            with sessions_lock:
                if morsel is not None:
                    owner_sessions.discard(morsel.value)
            self._html("<p>Signed out. <a href=\"/support\">Back</a></p>",
                       extra_headers={"Set-Cookie": f"{OWNER_COOKIE}=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/"})

        def do_POST(self):
            """Pair a new app/environment, or rotate an existing pairing's secret —
            as a JSON API call, or as the case-card page's own HTML forms."""
            if self.refused():
                return
            path = urlparse(self.path).path
            content_type = self.headers.get("Content-Type", "").lower()
            if path == "/support/choose-key":
                return self._choose_key()
            if path == "/support/sign-in":
                return self._sign_in()
            if path in owner_only_paths and not self.owner_signed_in():
                # Review r3 B2: every owner control, not only Deploy, needs the owner session — a page
                # token or a browser header is something any local agent's HTTP client can supply.
                return _send_json(self, 403, {
                    "error": "owner sign-in required: sign in on the Support page with the owner key you chose",
                })
            if path == "/support/sign-out":
                return self._sign_out()
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
            if path == "/support/settings":
                if not content_type.startswith("application/x-www-form-urlencoded"):
                    return _send_json(self, 400, {"error": "the settings form must be form-encoded"})
                length = int(self.headers.get("Content-Length") or 0)
                fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                store.save_settings(
                    app_repo=str(fields.get("app_repo", [""])[0]).strip(),
                    notice_email=str(fields.get("notice_email", [""])[0]).strip(),
                    mail_host=str(fields.get("mail_host", [""])[0]).strip(),
                    mail_port=str(fields.get("mail_port", [""])[0]).strip(),
                    mail_user=str(fields.get("mail_user", [""])[0]).strip(),
                    spending_ceiling=str(fields.get("spending_ceiling", [""])[0]).strip(),
                    github_token=str(fields.get("github_token", [""])[0]) or None,
                    mail_password=str(fields.get("mail_password", [""])[0]) or None,
                )
                return self._html("<p>Support settings saved. <a href=\"/support\">Back</a></p>")
            if path == "/support/settings/test-email":
                result = warning_email.send_message(
                    store.mail_settings(), store.mail_password(), warning_email.TEST_SUBJECT, warning_email.TEST_BODY,
                )
                message = "Test email sent." if result.get("sent") else f"Could not send the test email: {result.get('error', '')}"
                return self._html(f"<p>{html.escape(message)} <a href=\"/support\">Back</a></p>")
            if path == "/support/deploy":
                # The owner session (checked above) is what makes Deploy owner-only (review r3 B2).
                # The page's own action token stays as the cross-site defence: a form on another
                # site in the owner's browser cannot know it. A browser header is no longer asked
                # for: a local agent could forge it, and some browsers do not send it at all.
                fields = self._form_fields()
                submitted_token = str(fields.get("action_token", [""])[0])
                if not submitted_token or not hmac.compare_digest(submitted_token, deploy_action_token):
                    return _send_json(self, 403, {"error": "Deploy needs the token from a page this Support process just served"})
                fingerprint = str(fields.get("fingerprint", [""])[0])
                result = deploy_case(store, fingerprint)
                message = "Deployed." if result.get("deployed") else f"Could not deploy: {result.get('error', '')}"
                return self._html(f"<p>{html.escape(message)} <a href=\"/support\">Back</a></p>")
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

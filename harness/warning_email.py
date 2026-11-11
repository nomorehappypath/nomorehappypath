# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Warning email: tells the owner by email when a project is blocked, paused and waiting for a human.

Two jobs and nothing more. `send_message` sends one plain-text email through the mail account the owner saved in
Settings (standard library only) and always answers with a plain result, never a traceback and never the password.
`notify_blocked_cards` is called by the project worker after each self-heal pass: for every open red "Blocked, needs you"
card that has not been emailed yet it sends one email, and stamps the card so that card is tried exactly once.
"""
from __future__ import annotations

import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from harness import board, global_settings

SMTP_TIMEOUT_SECONDS = 15
SMTP_SSL_PORT = 465              # this port speaks TLS from the first byte; every other port starts plain and upgrades
LOOKS_LIKE_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+$")
SUBJECT_TEMPLATE = "NoMoreHappyPath: {project} is blocked and needs you"
PAUSED_LINE = "The project is paused; nothing more is spent until you press Resume."
TEST_SUBJECT = "NoMoreHappyPath: test email"
TEST_BODY = "This is a test from NoMoreHappyPath. If you can read this, your warning emails will reach you."
MAX_RESULT_CHARS = 300

# One plain sentence per kind of failure. The mail library's own error text is never shown: it can carry server noise.
NOT_SET_UP = "Warning email is not set up: save your email address and mail server first."
NO_SECURE_CONNECTION = "The mail server does not offer a secure connection, so nothing was sent and your password was not given to it."
SECURE_CONNECTION_FAILED = "The secure connection to the mail server failed, so nothing was sent."
BAD_LOGIN = "The mail server did not accept the user name or password. Gmail and Outlook need an app password."
REFUSED = "The mail server refused to send the message."
UNREACHABLE = "Could not reach the mail server {host} on port {port}."


def _from_address(settings: dict[str, Any]) -> str:
    """Return the From address: the saved user name if it looks like an address, else the address the warning goes to."""
    user = str(settings.get("user") or "")
    return user if LOOKS_LIKE_ADDRESS.fullmatch(user) else str(settings["to"])


def _plain_error(error: Exception, settings: dict[str, Any]) -> str:
    """Turn any failure raised while sending into one plain sentence. Takes the exception and the settings; returns the sentence."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return BAD_LOGIN
    if isinstance(error, ssl.SSLError):
        return SECURE_CONNECTION_FAILED
    if isinstance(error, (smtplib.SMTPException, UnicodeError, ValueError)) and not isinstance(error, smtplib.SMTPServerDisconnected):
        return REFUSED
    # A refused connection, a timeout, a name that does not resolve, a server that hung up.
    return UNREACHABLE.format(host=settings.get("host", ""), port=settings.get("port", ""))


def send_message(settings: dict[str, Any], password: str, subject: str, body: str) -> dict[str, Any]:
    """Send one plain-text email to settings["to"] through the saved mail server.

    Takes the saved non-secret settings (to, host, port, user), the mail password ("" when none is saved), a subject and a body.
    Returns {"sent": bool, "error": ""} or {"sent": False, "error": "<one plain sentence>"}; it never raises, and the
    password is never in the result. The password is only ever sent over an encrypted connection.
    """
    if not global_settings.warning_email_is_on(settings):
        return {"sent": False, "error": NOT_SET_UP}
    try:
        message = EmailMessage()
        message["To"] = settings["to"]
        message["From"] = _from_address(settings)
        message["Subject"] = " ".join(str(subject).split())      # one line, so a project name can never add a header
        message.set_content(body)
        context = ssl.create_default_context()
        port = int(settings["port"])
        if port == SMTP_SSL_PORT:
            connection: smtplib.SMTP = smtplib.SMTP_SSL(settings["host"], port, timeout=SMTP_TIMEOUT_SECONDS, context=context)
        else:
            connection = smtplib.SMTP(settings["host"], port, timeout=SMTP_TIMEOUT_SECONDS)
        with connection:
            if port != SMTP_SSL_PORT:
                connection.ehlo()
                if not connection.has_extn("starttls"):
                    # Why: logging in now would hand the password to the network in the clear.
                    return {"sent": False, "error": NO_SECURE_CONNECTION}
                connection.starttls(context=context)
                connection.ehlo()
            if settings.get("user") and password:
                connection.login(settings["user"], password)
            connection.send_message(message)
    except Exception as error:      # every failure becomes a sentence; the caller must never see a traceback
        return {"sent": False, "error": _plain_error(error, settings)[:MAX_RESULT_CHARS]}
    return {"sent": True, "error": ""}


def blocked_message(project_name: str, card: dict[str, Any], address: str = "", paused: bool = True) -> tuple[str, str]:
    """Build (subject, body) for a blocked-project email.

    Takes the project's name, the red card (its title and its Problem / What to do lines), when the caller knows it the
    local Mission Control address (nothing is made up when it is blank), and whether the project is paused: the "paused,
    nothing more is spent" line is only said when it is true (a CTO that says it is blocked raises the card without a pause).
    """
    lines = [str(card.get("title") or ""), "", str(card.get("why") or "")] + (["", PAUSED_LINE] if paused else [])
    if address:
        lines += ["", f"Mission Control: {address}"]
    return SUBJECT_TEMPLATE.format(project=project_name or "A project"), "\n".join(lines) + "\n"


def send_test(home: Path) -> dict[str, Any]:
    """Send the "this is a test" email with the saved settings and password. Returns send_message's result."""
    settings = global_settings.warning_email_settings(home)
    try:
        password = global_settings.warning_email_password(home)
    except ValueError as error:
        return {"sent": False, "error": str(error)}
    return send_message(settings, password, TEST_SUBJECT, TEST_BODY)


def _now() -> str:
    """Return the current time as an ISO string in UTC."""
    return datetime.now(timezone.utc).isoformat()


IDLE_CARD_EMAIL_DELAY_MINUTES = 30


def _too_new_to_email(card: dict[str, Any]) -> bool:
    """A "nobody is working" card may be a long silent run; email only if it is still open after a while (owner, 2026-11-11)."""
    if not card.get("idle_task"):
        return False
    try:
        opened = datetime.fromisoformat(str(card.get("recorded_at")))
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) - opened < timedelta(minutes=IDLE_CARD_EMAIL_DELAY_MINUTES)


def notify_blocked_cards(root: Path, home: Path | None, project_name: str = "", address: str = "") -> list[dict[str, Any]]:
    """Email the owner once for each open blocked card that has not been emailed. Called after every self-heal pass.

    Takes the project root, the settings home (None means no settings, so nothing happens), the project's name and the
    Mission Control address if known. Returns one {"card", "sent", "error"} per email attempted (empty when the feature is
    off or nothing is new). Once per card: the card is stamped BEFORE the send (under the board lock, which works while
    the project is paused), so a crash or a failure is never retried; the network call itself runs outside the lock.
    """
    if not home:
        return []
    settings = global_settings.warning_email_settings(home)
    if not global_settings.warning_email_is_on(settings):
        return []
    waiting = [card_id for card_id, card in (board.snapshot(root).get("owner_actions") or {}).items()
               if card.get("status") == "open" and card.get("blocked") and not card.get("warning_email_at")
               and not _too_new_to_email(card)]
    results = []
    for card_id in waiting:
        with board.locked_state(root, allow_paused=True, operation="warning email: mark card") as state:
            card = (state.get("owner_actions") or {}).get(card_id)
            if not card or card.get("status") != "open" or card.get("warning_email_at"):
                continue
            card["warning_email_at"] = _now()
            card["warning_email_result"] = "sending"
            snapshot = dict(card)
        subject, body = blocked_message(project_name, snapshot, address, paused=board.pause_state(root).get("status") != "active")
        try:
            outcome = send_message(settings, global_settings.warning_email_password(home), subject, body)
        except ValueError as error:          # an unsafe password file: say so, never use it
            outcome = {"sent": False, "error": str(error)}
        with board.locked_state(root, allow_paused=True, operation="warning email: record result") as state:
            card = (state.get("owner_actions") or {}).get(card_id)
            if card is not None:
                card["warning_email_result"] = "sent" if outcome["sent"] else outcome["error"]
            message = (f"A warning email about \"{snapshot.get('title', '')[:120]}\" was sent to {settings['to']}." if outcome["sent"]
                       else f"The warning email to {settings['to']} could not be sent. {outcome['error']} It will not be tried again for this card.")
            board._event(state, "warning_email_sent" if outcome["sent"] else "warning_email_failed", None,
                         {"task": str(snapshot.get("task") or ""), "card": card_id, "message": message})
        results.append({"card": card_id, **outcome})
    return results

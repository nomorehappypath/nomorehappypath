# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The one combined question card a "Write specs" app task shows before the
document is written (task write-specs-technical-chapters). Six generic
questions are always asked; one more (concurrency) reveals itself as soon as
"Who uses it?" is answered, with no judgment call needed; two more (existing
data, data loss) are included only when Delivery's own reading of the app's
description says the app replaces something or keeps data — a judgment call
Delivery makes once, when it submits the app's own up-to-five questions.

Pure helpers only: no board state, no files. harness/board.py holds the
per-task state; this module holds what a submission must look like and how
answers (and silently skipped ones) become the document's own "Assumed:"
lines.
"""
from __future__ import annotations

from typing import Any

MAX_APP_QUESTIONS = 5
MIN_OPTIONS = 2
MAX_OPTIONS = 4

# "Who uses it?" is the one fixed answer that unlocks another fixed question by
# itself (never "Just me"); every other conditional question is the app's own
# property (replaces something / keeps data), which only Delivery's reading of
# the description can decide — see include_existing_data, include_data_loss.
FIXED_ALWAYS: tuple[dict[str, Any], ...] = (
    {"key": "how_used", "text": "How do you use it?",
     "options": ["Web page, hosted", "Desktop app", "Phone app", "No screen, just reports"]},
    {"key": "who_uses", "text": "Who uses it?",
     "options": ["Just me", "Me and a few people I invite", "The public"]},
    {"key": "what_holds", "text": "What will it hold?",
     "options": ["Nothing private", "My own private information",
                 "Other people's personal information", "Money, payments or bank access"]},
    {"key": "if_stops", "text": "If it stops for a day, what happens?",
     "options": ["No big deal", "Annoying", "It costs me money or customers"]},
    {"key": "internet", "text": "Internet connection?",
     "options": ["Always online", "Must also work offline"]},
)

CONCURRENCY_QUESTION: dict[str, Any] = {
    "key": "concurrency", "text": "At its busiest, how many people at the same time?",
    "options": ["A few", "Dozens", "Hundreds or more"],
}
EXISTING_DATA_QUESTION: dict[str, Any] = {
    "key": "existing_data", "text": "Is there existing data to bring in?",
    "options": ["No", "From spreadsheets or files", "From another app"],
}
DATA_LOSS_QUESTION: dict[str, Any] = {
    "key": "data_loss", "text": "If its data were lost, what happens?",
    "options": ["Nothing, it can be rebuilt", "I lose some work",
                "Serious loss (money, customers, records I can't recreate)"],
}
DATA_LOSS_WINDOW_QUESTION: dict[str, Any] = {
    "key": "data_loss_window", "text": "How much recent data could you afford to lose?",
    "options": ["A day", "An hour", "Nothing"],
}

# Review r1 B1 / r2 S-001: "What will it hold?" drives the Security chapter's Targets, so a
# description that touches more than one sensitivity ("my own private information and a bank
# account") must resolve to the HIGHEST one, never the first one a plain substring scan happens
# to hit. Each tier's own keywords include its exact option phrase, so a literal mention of a
# lower-tier option's wording never wins over a higher tier's keyword elsewhere in the same
# description — _infer_what_holds checks tiers most-sensitive-first and is this question's only
# inference path (see resolve_answers), not a fallback after a plain literal-match loop.
_WHAT_HOLDS_SENSITIVITY_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Money, payments or bank access",
     ("money, payments or bank access", "payment", "credit card", "bank", "billing", "invoice",
      "stripe", "paypal", "subscription fee", "charge")),
    ("Other people's personal information",
     ("other people's personal information", "customer", "client", "patient", "member",
      "employee", "other people", "other user")),
    ("My own private information",
     ("my own private information", "password", "private", "personal", "journal", "diary",
      "health", "medical", "secret", "login", "credential", "social security", "ssn")),
)


def _infer_what_holds(description: str) -> str | None:
    """The safe, keyword-based reading of "What will it hold?" from the description — tried
    before the plain first-option fallback, so an app that is obviously sensitive (a password
    manager, a health journal) never silently defaults to "Nothing private" just because its own
    description does not happen to repeat that option's exact wording.

    Return: the most sensitive option whose own keyword appears in the description, or None when
    none of them do (resolve_answers then falls back to its ordinary rule).
    """
    lowered = description.lower()
    for option, keywords in _WHAT_HOLDS_SENSITIVITY_TIERS:
        if any(keyword in lowered for keyword in keywords):
            return option
    return None


class InvalidSpecQuestions(ValueError):
    """One app-specific question, or the submission as a whole, is malformed."""


def validate_app_questions(raw: Any) -> list[dict[str, Any]]:
    """Check Delivery's own up-to-five app-specific questions.

    Args:
        raw: the submitted value for "app_questions" — expected to be a list of
            {"text": str, "options": [2-4 strings], "recommended": int} dicts.

    Return: the same questions, normalized (text and options stripped).

    Raises: InvalidSpecQuestions on anything malformed or over the limits —
    too many questions, too few/many options, a missing or out-of-range
    recommended index, or a blank question/option text.
    """
    if not isinstance(raw, list):
        raise InvalidSpecQuestions("app_questions must be a list")
    if len(raw) > MAX_APP_QUESTIONS:
        raise InvalidSpecQuestions(f"at most {MAX_APP_QUESTIONS} app-specific questions are allowed")
    normalized: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise InvalidSpecQuestions("each app question must be an object")
        text = str(entry.get("text") or "").strip()
        if len(text) < 5:
            raise InvalidSpecQuestions("each app question needs real question text")
        options = [str(option).strip() for option in (entry.get("options") or [])]
        options = [option for option in options if option]
        if not (MIN_OPTIONS <= len(options) <= MAX_OPTIONS):
            raise InvalidSpecQuestions(f"each app question needs {MIN_OPTIONS} to {MAX_OPTIONS} options")
        recommended = entry.get("recommended")
        if not isinstance(recommended, int) or not (0 <= recommended < len(options)):
            raise InvalidSpecQuestions("recommended must be a valid index into options")
        normalized.append({"text": text, "options": options, "recommended": recommended})
    return normalized


def card_questions(
    app_questions: list[dict[str, Any]], *, include_existing_data: bool, include_data_loss: bool,
) -> list[dict[str, Any]]:
    """Build the full, fixed-order list of questions the owner's one card shows.

    Args:
        app_questions: Delivery's validated up-to-five app-specific questions.
        include_existing_data: Delivery's own reading of the description: the app replaces
            something, so "Is there existing data to bring in?" belongs on the card.
        include_data_loss: Delivery's own reading of the description: the app keeps data, so
            both data-loss questions belong on the card.

    Return: every question to show, in order: the app-specific ones first, then the six
    always-asked generic ones, then the included conditional ones. "At its busiest..." is never
    in this list — the page itself reveals it once "Who uses it?" is answered, with no server
    round trip, because that trigger needs no judgment call.
    """
    questions = list(app_questions) + list(FIXED_ALWAYS)
    if include_existing_data:
        questions.append(EXISTING_DATA_QUESTION)
    if include_data_loss:
        questions.append(DATA_LOSS_QUESTION)
        questions.append(DATA_LOSS_WINDOW_QUESTION)
    return questions


def resolve_answers(
    questions: list[dict[str, Any]], answers: dict[str, Any], description: str,
) -> list[dict[str, Any]]:
    """Turn the owner's raw answers (some possibly skipped) into one resolved list.

    Args:
        questions: the exact questions the card showed, in order (app-specific questions are
            keyed by their position, "q0".."q4"; fixed questions by their own "key").
        answers: {question key: chosen option(s)} for whatever the owner actually answered — a
            list of 1+ option strings (owner 2026-11-10: every question allows more than one
            choice, "used on web hosted and on the phone" is the owner's own example), or a bare
            string for a single choice; a missing or empty key means that question was skipped.
        description: the owner's own app description, searched for a skipped question's answer
            before falling back to its first option.

    Return: one list, same order as questions, each {"text", "chosen": list[str], "assumed":
    bool}. "chosen" is always a list, even a single real answer or a single inferred default, so
    the document builder never needs two code paths. assumed is True exactly when the owner
    skipped that question, so the document's "Assumed: ..." list can be built straight from this
    without re-deriving which answers were defaults.
    """
    resolved: list[dict[str, Any]] = []
    lowered_description = description.lower()
    for index, question in enumerate(questions):
        key = question.get("key") or f"q{index}"
        given = answers.get(key)
        given_list = [str(value) for value in given if str(value or "").strip()] if isinstance(given, list) \
            else [str(given)] if str(given or "").strip() else []
        if given_list:
            resolved.append({"text": question["text"], "chosen": given_list, "assumed": False})
            continue
        # S-001: "what_holds" never uses the plain literal-match loop below — a description that
        # mentions more than one sensitivity must pick the highest tier, not whichever option's
        # exact wording the scan happens to reach first.
        if key == "what_holds":
            inferred = _infer_what_holds(description)
        else:
            inferred = next(
                (option for option in question["options"] if option.lower() in lowered_description), None,
            )
        chosen = inferred if inferred is not None else question["options"][0]
        resolved.append({"text": question["text"], "chosen": [chosen], "assumed": True})
    return resolved


def assumed_lines(resolved: list[dict[str, Any]]) -> list[str]:
    """Return the document's own "Assumed: ..." lines, one per skipped question."""
    return [f"Assumed: {entry['text']} — {', '.join(entry['chosen'])}" for entry in resolved if entry["assumed"]]

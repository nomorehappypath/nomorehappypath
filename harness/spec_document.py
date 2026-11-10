# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The fixed shape of a full app SPEC.md (write-specs-technical-chapters R4/R5): chapter
names, and the one rule every Targets line must follow — a number with its conditions, a
yes/no, a named test, or "Not applicable: <reason>", never an adjective.
"""
from __future__ import annotations

import re

# Chapter 1 (Functional) is unchanged from today's Write specs and carries no Targets list;
# chapters 2-12 are new here, each ending in one.
CHAPTER_TITLES: tuple[str, ...] = (
    "Functional",
    "Back end and infrastructure",
    "Front end",
    "Networking",
    "Latency",
    "Load",
    "Security",
    "Error handling",
    "Backup and recovery",
    "Maintainability",
    "Documentation",
    "Support plan",
)

_NOT_APPLICABLE = re.compile(r"^Not applicable:\s*\S")
_YES_NO = re.compile(r":\s*(yes|no)\s*$", re.IGNORECASE)
_NAMED_TEST = re.compile(r":\s*\S*\btest_\w+\b\s*$")
# Review r1 B2: a bare digit anywhere in the line ("Target 5", "version 2 of the API") used to
# pass as "a number with its conditions" — a number alone names nothing measurable. The owner's
# own examples always pair the number with a comparison word ("under", "95 of 100") or a unit
# right after it ("300 ms", "50 users"); a Targets line is only this form when it has one of the
# two.
_COMPARISON_WORD_NEAR_NUMBER = re.compile(
    r"\b(under|over|at least|at most|within|fewer than|more than|up to|exactly)\b.{0,60}?\d", re.IGNORECASE,
)
_NUMBER_WITH_UNIT = re.compile(
    r"\d+(\.\d+)?\s*(ms|milliseconds?|seconds?|minutes?|hours?|days?|%|percent|"
    r"requests?|users?|orders?|records?|rows?|items?|mb|gb|kb|of\s+\d+)\b", re.IGNORECASE,
)


def _has_measurable_number(text: str) -> bool:
    """Whether text names a real measurable condition, not just a bare digit somewhere in it."""
    return bool(_COMPARISON_WORD_NEAR_NUMBER.search(text) or _NUMBER_WITH_UNIT.search(text))


def target_line_violation(line: str) -> str:
    """Check one Targets line against the four allowed forms.

    Args:
        line: one line of a chapter's Targets list, without its leading "- " marker.

    Return: "" if the line is one of the four allowed forms (a number with conditions, a
    yes/no, a named test, or "Not applicable: <reason>"); otherwise one plain sentence saying
    why it is not.
    """
    text = line.strip()
    if not text:
        return "a Targets line is empty"
    if _NOT_APPLICABLE.match(text):
        return ""
    if _YES_NO.search(text):
        return ""
    if _NAMED_TEST.search(text):
        return ""
    if _has_measurable_number(text):
        return ""
    return (
        f"{text!r} is not one of the four allowed Targets forms "
        "(a number with its conditions, a yes/no, a named test, or \"Not applicable: <reason>\")"
    )


def validate_targets(lines: list[str]) -> list[str]:
    """Check a whole chapter's Targets list.

    Args:
        lines: each Targets line, without its leading "- " marker.

    Return: one violation sentence per non-conforming line, in order; [] when every line is one
    of the four allowed forms.
    """
    violations = [target_line_violation(line) for line in lines]
    return [violation for violation in violations if violation]

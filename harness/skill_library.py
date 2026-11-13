# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Shared library of per-task skill guides for NMHP's Claude/Codex agents.

Twelve read-only guides live in ``skills/<slug>/SKILL.md``, one per
``SKILL_LIBRARY_SPEC.md`` section 4. A task's builder proposes which ones it
needs; the owner ticks the ones to use on the Go ahead card; only those are
ever named, by full path, to an agent. This module holds the shared facts
other modules build on: where the library lives, how one guide is checked,
and the fixed plain-name/owner-description table the card and Help show
(a guide's own ``description:`` can be long and is what a builder preparing
a proposal is given in full; the plain one here is the short owner-facing
text of spec section 4, used only for display).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

LIBRARY_DIRNAME = "skills"

# (slug, plain name on the card, one-line owner-facing description), in the
# fixed display order of spec section 4's table.
SKILLS: tuple[tuple[str, str, str], ...] = (
    ("requirements-architecture", "Requirements and architecture",
     "Turns the idea into clear requirements and a design sized to the job."),
    ("ux-ui-design", "Screen and experience design",
     "Decides what the user sees and does, in every state of every screen."),
    ("frontend", "Screens (front end)",
     "Builds screens that work on phone and desktop, with real data."),
    ("backend", "Server (back end)",
     "Builds the server side: rules, checks on input, clear errors."),
    ("data-databases", "Data and databases",
     "Stores data safely and changes it without losing any."),
    ("integrations", "Outside services",
     "Connects to other services so that slowness or failure does no harm."),
    ("payments", "Payments and subscriptions",
     "Takes money safely: no double charge, access follows payment."),
    ("security", "Security and sign-in",
     "Sign-in, permissions, private data, and trying to break them."),
    ("qa-testing", "QA and testing",
     "Proves it works and looks for where it does not."),
    ("cloud-devops", "Cloud and deployment",
     "Gets the app deployed, watched, backed up and able to roll back."),
    ("code-review-maintenance", "Code review and upkeep",
     "Reviews changes and keeps existing code healthy."),
    ("support-troubleshooting", "Troubleshooting",
     "Finds the true cause of a reported problem and resolves it."),
)

# For a full task (not a small change), the builder is told to read this one
# skill while preparing its proposal (spec section 3's one exception).
REQUIREMENTS_SKILL = "requirements-architecture"

_NAME_LINE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)
_DESCRIPTION_LINE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)
_PLAIN_NAME_LINE = re.compile(r"^plain_name:\s*(.+?)\s*$", re.MULTILINE)
_OWNER_DESCRIPTION_LINE = re.compile(r"^owner_description:\s*(.+?)\s*$", re.MULTILINE)
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


def _frontmatter_text(text: str) -> str | None:
    """The text strictly between the file's first two '---' lines, or None
    when there is no real frontmatter block. A name: or description: line
    found anywhere else in the file (prose in the body) is not metadata and
    must never be accepted as if it were (reviewer finding: the checks used
    to search the whole file, not just this block).
    """
    match = _FRONTMATTER.match(text)
    return match.group(1) if match else None


def library_root(harness_root: str | os.PathLike[str] | None = None) -> Path:
    """The one shared library folder, inside NMHP's own install, never a project's.

    Args:
        harness_root: NMHP's own source root. Defaults to this file's own
            install location (two parents up from this module), which is
            correct whenever this process IS the installed harness.

    Returns: the library's root Path (its ``skills/`` folder).
    """
    base = Path(harness_root).resolve() if harness_root else Path(__file__).resolve().parents[1]
    return base / LIBRARY_DIRNAME


@dataclass(frozen=True)
class Skill:
    """One library guide: its identity, where it lives, and whether it is usable.

    Attributes:
        slug: the folder name and the ``SKILL.md`` ``name:`` line (e.g. "backend").
        plain_name: the short owner-facing name shown on the card and in Help.
        owner_description: the one-line owner-facing description (spec section 4).
        full_description: the guide's own, possibly long, ``description:`` line,
            given in full to a builder preparing its proposal (never shortened).
        path: the guide's own ``SKILL.md`` file.
        problem: ``None`` when the guide is usable; otherwise the one plain
            reason it was left off (unreadable, missing name or description,
            or a file inside its folder that resolves outside it).
    """

    slug: str
    plain_name: str
    owner_description: str
    full_description: str
    path: Path
    problem: str | None


def _escapes_library(folder: Path, library: Path) -> bool:
    """True when folder itself resolves outside library, or anything under it
    resolves outside folder.

    Two separate boundaries, checked against two different roots:
    - the skill's OWN folder entry must resolve inside the library root (a
      skill folder that is itself a symlink out of the library, e.g.
      `skills/security` pointing elsewhere entirely, must be caught); and
    - everything INSIDE that folder must resolve inside that SAME folder,
      not merely inside the library — checking entries against the library
      root instead of the folder's own root would let a file in one skill's
      folder link into a DIFFERENT skill's folder undetected, since that
      target is still inside the library (reviewer finding: the boundary
      for a skill's own contents must be the skill's own folder).
    """
    try:
        resolved_library = library.resolve()
        resolved_folder = folder.resolve()
        if not resolved_folder.is_relative_to(resolved_library):
            return True
        for entry in folder.rglob("*"):
            if not entry.resolve().is_relative_to(resolved_folder):
                return True
    except OSError:
        return True
    return False


def _discovered_slugs(root: Path) -> list[str]:
    """Every slug the library actually holds right now: the fixed SKILLS
    order first (unchanged display order for the original twelve), then any
    other folder under root that holds a SKILL.md, in name order.

    A folder added to the library after this module was written must be
    found here too (spec section 6, "Adding a skill later"): the owner adds
    a folder, nothing else needs editing - certainly not this file.
    """
    known = [slug for slug, _, _ in SKILLS]
    try:
        on_disk = sorted(
            entry.name for entry in root.iterdir()
            if entry.is_dir() and (entry / "SKILL.md").is_file()
        )
    except OSError:
        on_disk = []
    return known + [slug for slug in on_disk if slug not in known]


def checked_skills(harness_root: str | os.PathLike[str] | None = None) -> list[Skill]:
    """Every library skill actually on disk right now, each checked: the
    fixed SKILLS order first, then any later-added guide folder in name order.

    A skill folder that is missing, unreadable, lacks a matching ``name:`` or
    a ``description:`` line, or holds a file that escapes its own folder,
    comes back with ``problem`` set instead of being silently dropped — the
    caller (the card, a brief) decides what "left off, one plain line" means
    for its own surface.

    A guide not named in SKILLS gets its plain name and owner-facing
    description from its own file's optional ``plain_name:``/
    ``owner_description:`` frontmatter lines, falling back to a title-cased
    form of its slug and its real ``description:`` line when those are
    absent - so a new folder works the moment it is added, with nothing
    else to edit.
    """
    root = library_root(harness_root)
    table = {slug: (plain_name, owner_description) for slug, plain_name, owner_description in SKILLS}
    results: list[Skill] = []
    for slug in _discovered_slugs(root):
        folder = root / slug
        path = folder / "SKILL.md"
        known = table.get(slug)
        plain_name = known[0] if known else slug.replace("-", " ").replace("_", " ").title()
        owner_description = known[1] if known else ""
        problem: str | None = None
        full_description = ""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            problem = f"the guide {plain_name!r} could not be read"
            text = ""
        if not problem:
            frontmatter = _frontmatter_text(text)
            if frontmatter is None:
                problem = f"the guide {plain_name!r} has no frontmatter"
            else:
                name_match = _NAME_LINE.search(frontmatter)
                description_match = _DESCRIPTION_LINE.search(frontmatter)
                if not name_match or name_match.group(1) != slug:
                    problem = f"the guide {plain_name!r} is missing its name"
                elif not description_match:
                    problem = f"the guide {plain_name!r} is missing its description"
                else:
                    full_description = description_match.group(1)
                    if not known:
                        plain_match = _PLAIN_NAME_LINE.search(frontmatter)
                        if plain_match:
                            plain_name = plain_match.group(1)
                        owner_match = _OWNER_DESCRIPTION_LINE.search(frontmatter)
                        owner_description = owner_match.group(1) if owner_match else full_description[:140]
        if not problem and _escapes_library(folder, root):
            problem = f"the guide {plain_name!r} links outside its own folder"
        results.append(Skill(slug, plain_name, owner_description, full_description, path, problem))
    return results


def usable_skills(harness_root: str | os.PathLike[str] | None = None) -> list[Skill]:
    """``checked_skills()``, keeping only the skills with no problem."""
    return [skill for skill in checked_skills(harness_root) if skill.problem is None]


def broken_skills(harness_root: str | os.PathLike[str] | None = None) -> list[Skill]:
    """``checked_skills()``, keeping only the skills that have a problem."""
    return [skill for skill in checked_skills(harness_root) if skill.problem is not None]


def find(slug: str, harness_root: str | os.PathLike[str] | None = None) -> Skill | None:
    """The one usable Skill named slug, or None when it is missing or broken."""
    for skill in usable_skills(harness_root):
        if skill.slug == slug:
            return skill
    return None

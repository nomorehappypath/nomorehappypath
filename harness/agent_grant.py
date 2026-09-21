# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Decide which paths an agent may WRITE, by IDENTITY rather than by shape.

Three versions failed review, and the third failed in BOTH directions at once,
which is what finally showed the approach was wrong rather than the details:

  1. Owner-supplied roots passed straight through — an adopted project could name
     a broad ancestor, or a symlink to one, and Codex wrote outside the project.
  2. A BLOCKLIST of dangerous roots — the reviewer executed probes showing it
     still accepted /Volumes, /private, /System, /Library, /mnt, /media, /srv
     and /run. A blocklist must anticipate every mount point on every platform,
     forever.
  3. A POSITIVE rule based on POSITION — "inside the project or beside it". It
     over-granted (any unrelated sibling project was writable, proven through
     the sandbox) and under-granted (the harness's own adopted-project storage
     under the manager home was refused, breaking a legitimate launch).

Position cannot answer this, because "beside the project" describes both the
harness's own task workspace and someone else's project. The harness already
KNOWS which paths belong to a project: it assigns them and records them. So the
question is not "does this path look like storage" but "IS this path the storage
this project was given".

Everything resolves symlinks FIRST: a rule applied before resolution describes
the name, not the directory.
"""
from __future__ import annotations

import os
from pathlib import Path

from harness import project_context as context_module
from harness import project_registry


class GrantTooBroad(ValueError):
    """A requested writable root is not the storage this project was assigned."""


def _resolved(path) -> Path:
    return Path(os.path.realpath(str(path)))


def _homes_to_consult(home) -> list[Path]:
    """Only a TRUSTED manager home. Never one discovered from the input.

    A previous version located the registry by walking up from the requested
    roots, with a comment asserting that finding one was not trusting it,
    because the entry still had to name this project and this exact path.

    That claim was FALSE, and a reviewer proved it: whoever supplies the bad
    root also supplies the registry beside it, so they write the entry that
    validates their own path. The same root was refused without the planted
    registry and accepted with it.

    A registry is authoritative only if the harness told us where it lives.
    """
    return [Path(home)] if home else []


def _assigned_storage(project_root: Path, home) -> tuple[Path, Path] | None:
    """The data and workspace roots the HARNESS assigned to this project.

    Registered projects — including adopted ones, whose storage lives outside
    the repository by design — are looked up by their code root. An unregistered
    project falls back to the harness's own default layout for that root, which
    is what the launcher itself would compute.
    """
    resolved_project = _resolved(project_root)
    for home_path in _homes_to_consult(home):
        try:
            for entry in project_registry.entries(home_path):
                if _resolved(entry.get("code_root", "")) == resolved_project:
                    return (_resolved(entry.get("data_root", "")),
                            _resolved(entry.get("workspace_root", "")))
        except (OSError, ValueError, KeyError):
            continue
    return None


def _default_storage(project_root: Path) -> tuple[Path, Path] | None:
    """The layout the harness would derive for this root with no registry."""
    try:
        context = context_module.project_context(project_root)
        return _resolved(context.data_root), _resolved(context.workspace_root)
    except (OSError, ValueError):
        return None


def agent_writable_roots(execution_root, data_root, workspace_root,
                         project_root=None, home=None) -> list[str]:
    """The paths a managed agent may write, or a refusal naming the reason.

    A requested root is granted only when it IS the storage this project was
    assigned, or lies inside the project itself. Anything else is refused
    whatever it looks like — an unrelated sibling, a mount point, an ancestor, a
    system directory — without any of them needing to be named.
    """
    execution = _resolved(execution_root)
    project = _resolved(project_root) if project_root else execution
    # A REGISTRY assignment is authoritative and excludes everything else.
    # A DERIVED default is merely the shape with no registry, so it must not be
    # mistaken for an assignment — that mistake refused every explicit adopted
    # layout and broke real launches.
    assigned = _assigned_storage(project, home)
    defaults = _default_storage(project)
    granted = [execution]

    for label, requested in (("data root", data_root), ("task workspace", workspace_root)):
        if not requested:
            continue
        resolved = _resolved(requested)

        allowed = resolved == execution or resolved.is_relative_to(execution)
        if not allowed and resolved != project and resolved.is_relative_to(project):
            allowed = True
        if not allowed and assigned and resolved in assigned:
            allowed = True
        if not allowed and assigned is None and defaults and resolved in defaults:
            allowed = True

        # There is deliberately NO fallback for "explicit roots, no registry".
        #
        # A previous version accepted any outside directory whenever no registry
        # named the project, and declared that as a limit. A reviewer showed the
        # limit WAS the hole: the same launcher argument that supplies the roots
        # also decides whether a trusted home arrives, so suppressing one flag
        # turned the declared limit into the ordinary path.
        #
        # It is not needed. `project_registry.register` records data_root and
        # workspace_root for every project the product creates OR adopts, so a
        # real launch always has an assignment to check against. Anything
        # reaching here with explicit roots and no assignment was not launched
        # by the harness, and that is exactly the case that must not be trusted.

        if not allowed:
            raise GrantTooBroad(
                f"refusing to grant write access to {resolved} as the {label}: "
                f"it is not the storage assigned to the project at {project}"
                + (f" ({assigned[0]}, {assigned[1]})" if assigned else "")
            )
        if resolved not in granted:
            granted.append(resolved)
    return [str(path) for path in granted]

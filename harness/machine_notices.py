# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Two plain-words notices for the Test machine window (open-list item C, follow-ups c and d of the machine-card record).

Warn only, never block, never reject: a matching host key can be a legitimate cloned VM, and the remote lock folder belongs to each
project's own script, so the manager does not look at it. These read the pinned identity already stored in each project's descriptor and
the board's own execution marker; they write nothing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness import board, project_registry as registry, test_machine
from harness.project_context import ProjectRoot


def same_computer_warning(home: Path, project_id: str, address: str, port: int, fingerprint: str) -> str:
    """A sentence when another project of this home is connected to a machine with this very host key at a different address; else ''."""
    if not fingerprint:
        return ""
    try:
        entries = registry.entries(home)
    except (OSError, ValueError):
        return ""
    for entry in entries:
        if entry.get("id") == project_id:
            continue
        try:
            context = registry.context_for_entry(entry)
            if not test_machine.descriptor_path(context).exists():
                continue
            value = test_machine.read(context) or {}
        except (OSError, ValueError, KeyError):
            continue
        if not value.get("connected") or ((value.get("host_key") or {}).get("fingerprint") != fingerprint):
            continue
        if str(value.get("address")) == address and int(value.get("port", 22)) == int(port):
            continue   # the same address is the reservation's business, not a warning
        return (
            f"This looks like the same computer as {value.get('user', '')}@{value.get('address', '')} that "
            f"{entry.get('name', 'another project')} is using. If that is on purpose (a copy of the same virtual machine) nothing is wrong. "
            "If both projects test on one machine at the same time, their tests can disturb each other. Connecting continues."
        )
    return ""


def running_test_notice(root: ProjectRoot) -> str:
    """A sentence when a certified test of this project is running right now (a current execution heartbeat); else ''.

    The marker does not say whether the running command touches the machine (the Reviewer's marker names its ledger, Delivery's its
    primary command), so the sentence says "may". A finished or stale marker is not a running test."""
    try:
        state = board.snapshot(root)
    except (OSError, ValueError, KeyError):
        return ""
    for agent in (state.get("agents") or {}).values():
        if agent.get("active") and board._review_execution_is_current(agent):
            return (
                f"A test may be running on this machine right now ({agent.get('display_name') or agent.get('role', 'an agent')}, "
                f"task {agent.get('task', '')}). Reconnecting can interrupt it, and it would have to be run again. "
                "Press Confirm only if you want to reconnect now; otherwise close this window and let it finish."
            )
    return ""

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Decide whether a harness source update may restart the running manager.

The launcher watches the harness source and restarts the manager when it
changes. Restarting while a project is open kills its agent terminals,
expires in-flight certified executions, and invalidates saved evidence —
the dominant loss in the 2026-08-19 run autopsy. This gate defers the
restart until every open project is released; the owner's Pause and Close
controls are the release valves. A fully PAUSED project counts as released:
pause has already drained and stopped its terminals, so nothing in flight
can be killed. A project that is draining or resuming still defers.

A manager that cannot be reached is treated as restartable: the freeze must
never turn a dead manager into a permanent outage.
"""
from __future__ import annotations

import json
import sys
from typing import Any
from urllib.request import urlopen

EXIT_RESTART_ALLOWED = 0
EXIT_RESTART_DEFERRED = 3


def _blocks_restart(project: Any) -> bool:
    if not isinstance(project, dict) or project.get("active") is not True:
        return False
    # A steady paused board has no terminals and nothing in flight; it
    # releases the update. Draining and resuming are transitions and defer.
    return str(project.get("board_pause_status") or "") != "paused"


def restart_allowed(projects_payload: Any) -> bool:
    """True when no open project could lose in-flight work to a restart."""
    if not isinstance(projects_payload, dict):
        return True
    projects = projects_payload.get("projects")
    if not isinstance(projects, list):
        return True
    return not any(_blocks_restart(project) for project in projects)


def fetch_projects(manager_url: str, timeout: float = 3.0) -> Any:
    with urlopen(manager_url.rstrip("/") + "/api/projects", timeout=timeout) as response:
        return json.loads(response.read())


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Harness update gate")
    parser.add_argument("--manager-url", required=True)
    arguments = parser.parse_args(argv)
    try:
        payload = fetch_projects(arguments.manager_url)
    except (OSError, ValueError):
        return EXIT_RESTART_ALLOWED
    if restart_allowed(payload):
        return EXIT_RESTART_ALLOWED
    active = [
        str(project.get("name") or project.get("id") or "?")
        for project in payload.get("projects", [])
        if _blocks_restart(project)
    ]
    print("active_project=" + ",".join(active))
    return EXIT_RESTART_DEFERRED


if __name__ == "__main__":
    raise SystemExit(main())

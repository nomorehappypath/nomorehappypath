# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""OS-level write confinement around a managed Claude terminal.

Codex confines its own writes (`sandbox_mode=workspace-write` with the roots
the harness grants). Claude Code has no equivalent the harness can use: its
permission prompts are the boundary, and an unattended agent must not stop
at prompts; its built-in sandbox confines writes but has no network-only off
switch, and inside it the board worker on 127.0.0.1 is unreachable and ssh
to the test host is refused (proven live, 2026-09-23). So the harness draws
the boundary itself, with the primitive it already uses for the git broker
(`harness/platform_support`, one implementation per platform). The whole CLI
process runs inside it; every shell command and file tool the agent uses
inherits it; the OS refuses a write outside the granted roots. Reads and
network are not confined, exactly as for Codex, and the Help text says so.

The grant is: the roots `agent_writable_roots` computed (execution root,
data root, task-workspace root), plus what the CLI itself needs to function:
its own state (`~/.claude`, `~/.claude.json`, or `CLAUDE_CONFIG_DIR`), the
user cache and log folders, the temp directories, and the device files.
Nothing else in the owner's home is writable.

This module decides nothing from the platform; the seam does.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from harness import platform_support
from harness.platform_support import AgentConfinementUnavailable as ConfinementUnavailable  # noqa: F401  (re-exported)


def _validated_config_dir(home: str | Path, claude_config_dir: str | None, temp_paths: list[str]) -> str | None:
    """`CLAUDE_CONFIG_DIR` may relocate the CLI's state; it may not widen the grant.

    The runner inherits the owner's environment, so this value is untrusted
    input to the boundary. A reviewer set it to the home folder and wrote
    outside the project (round 2, 2026-09-23). Accepted: a folder strictly
    inside the home folder, or inside the temp space that is writable anyway.
    Refused: the home folder itself, the filesystem root, any ancestor of the
    home folder, and anywhere else — the launch refuses rather than run open.
    """
    if not claude_config_dir:
        return None
    real = Path(claude_config_dir).expanduser().resolve()
    home_real = Path(home).expanduser().resolve()
    temp_real = [Path(item).expanduser().resolve() for item in temp_paths]
    inside_home = home_real in real.parents
    inside_temp = any(root == real or root in real.parents for root in temp_real)
    if real == Path("/") or real == home_real or real in home_real.parents or not (inside_home or inside_temp):
        raise ConfinementUnavailable(
            f"CLAUDE_CONFIG_DIR={claude_config_dir!r} would make {real} writable, which is outside the agent's "
            f"grant; set it to a folder inside {home_real} or unset it. Refusing to launch the agent unconfined."
        )
    return str(real)


def _validated_tmpdir(tmpdir: str | None, temp_paths: list[str]) -> list[str]:
    """`TMPDIR` is inherited too, and the same rule applies: it may not widen the grant.

    Accepted only inside the platform's fixed temp space (where it lives on
    every normal system); anything else refuses the launch.
    """
    if not tmpdir:
        return []
    real = Path(tmpdir).expanduser().resolve()
    roots = [Path(item).resolve() for item in temp_paths]
    if any(root == real or root in real.parents for root in roots):
        return [str(real)]
    raise ConfinementUnavailable(
        f"TMPDIR={tmpdir!r} would make {real} writable, which is outside the agent's grant; "
        f"set it inside {', '.join(temp_paths)} or unset it. Refusing to launch the agent unconfined."
    )


def writable_paths(writable_roots: list[str], *, home: str | Path, claude_config_dir: str | None = None,
                   implementation=None, tmpdir: str | None = None) -> list[str]:
    """The full write grant: the project's roots plus the CLI's own state and temp space.

    Everything that comes from the environment (`CLAUDE_CONFIG_DIR`, `TMPDIR`)
    is validated before it joins the grant; the roots themselves were validated
    by `agent_writable_roots`.
    """
    implementation = implementation or platform_support.agent_confinement()
    temp = implementation.temp_paths() + _validated_tmpdir(tmpdir if tmpdir is not None else os.environ.get("TMPDIR"), implementation.temp_paths())
    config_dir = _validated_config_dir(home, claude_config_dir, temp)
    return list(writable_roots) + implementation.cli_state_paths(home, config_dir) + temp


def wrap(argv: list[str], writable_roots: list[str], *, store: Path, home: str | Path,
         claude_config_dir: str | None = None, implementation=None) -> list[str]:
    """The command to run so that `argv` can write only inside the grant.

    Refuses on a platform without the primitive: a managed agent never runs
    with an open boundary because a package is missing.
    """
    implementation = implementation or platform_support.agent_confinement()
    writable = writable_paths(writable_roots, home=home, claude_config_dir=claude_config_dir, implementation=implementation)
    for root in writable_roots:
        # A granted root that does not exist yet (the task-workspace root of a
        # fresh project) must be created HERE: inside the boundary, creating
        # it is a write to its parent, which is outside the grant. Found by
        # the end-to-end run, not by the unit tests.
        Path(root).mkdir(parents=True, exist_ok=True)
    return implementation.wrap(list(argv), writable, store=Path(store))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the confined launch command for a managed Claude terminal as JSON")
    parser.add_argument("--store", required=True, help="where a generated profile is kept (the project's control dir)")
    parser.add_argument("--writable-roots", required=True, help="JSON list of the granted roots")
    parser.add_argument("--home", default=os.path.expanduser("~"))
    parser.add_argument("--claude-config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR") or None)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the CLI command (after --)")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        wrapped = wrap(command, json.loads(args.writable_roots), store=Path(args.store), home=args.home,
                       claude_config_dir=args.claude_config_dir)
    except ConfinementUnavailable as error:
        print(str(error), file=sys.stderr)
        return 3
    print(json.dumps(wrapped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Spawn-runner — launches a real agent process for a dispatched work item.

The scheduler's runner seam, made real. Given (item, agent), it renders the profile's
`spawn_command` template (vendor/role/task/signature/item/spawn_directive) and launches it as a
background subprocess, recording the dispatch under `.agents/dispatches/`. Real spawning is
OPT-IN: with no `spawn_command` configured it is a safe dry-run (records intent, launches
nothing). That opt-in is the deliberate guardrail for bypass-permission territory (D8/D13).
Stdlib only.

  bash runner.sh dispatch --item <item-id> --signature <sig>   # launch one item's agent
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import agent_registry
import claim
from profile_config import load_profile

_CHILDREN: dict[int, subprocess.Popen] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_repo_root() -> Path:
    here = Path.cwd().resolve()
    for c in [here, *here.parents]:
        if (c / ".agents").is_dir():
            return c
    raise SystemExit("No .agents directory found. Run from a repo root.")


def _dispatch_dir() -> Path:
    d = _find_repo_root() / ".agents" / "dispatches"
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return d


def _spawn_directive_path() -> str:
    # engine/scripts/runner.py -> engine/directives/00_AGENT_SPAWN_DIRECTIVE.md
    return str(Path(__file__).resolve().parent.parent / "directives" / "00_AGENT_SPAWN_DIRECTIVE.md")


class _SafeDict(dict):
    def __missing__(self, key):  # leave unknown {tokens} untouched rather than crash
        return "{" + key + "}"


def command_for(item: dict, agent: dict, profile: dict):
    """Render the profile's spawn_command template, or None if not configured."""
    tmpl = (profile or {}).get("spawn_command")
    if not tmpl:
        return None
    fields = _SafeDict(
        vendor=agent.get("vendor", ""), role=agent.get("role", ""),
        task=item.get("task_id", ""), signature=agent.get("signature", ""),
        item=item.get("item_id", ""), spawn_directive=_spawn_directive_path(),
    )
    return tmpl.format_map(fields)


def _record(rec: dict) -> dict:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{rec['item_id']}__{rec['signature']}")
    (_dispatch_dir() / f"{safe}.json").write_text(
        json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return rec


def reap_children() -> list[dict]:
    """Collect completed spawned agents without ever blocking active agents."""
    completed = []
    for pid, proc in list(_CHILDREN.items()):
        code = proc.poll()
        if code is not None:
            proc.wait()
            del _CHILDREN[pid]
            completed.append({"pid": pid, "returncode": code})
    return completed


def spawn_runner(item: dict, agent: dict, profile: dict | None = None) -> dict:
    """Launch the agent process for one item (or safe dry-run). Returns the dispatch record."""
    reap_children()
    profile = profile if profile is not None else load_profile()
    cmd = command_for(item, agent, profile)
    base = {
        "item_id": item["item_id"], "signature": agent.get("signature"),
        "vendor": agent.get("vendor"), "role": agent.get("role"),
        "task": item.get("task_id"), "started_at": _now_iso(),
    }
    if not cmd:
        return _record({**base, "spawned": False, "command": None,
                        "reason": "no spawn_command in profile (dry-run); real spawning is opt-in"})
    log_path = _dispatch_dir() / "logs" / (re.sub(r"[^A-Za-z0-9_.-]", "_", f"{item['item_id']}__{agent['signature']}") + ".log")
    with open(log_path, "a", encoding="utf-8") as log:
        proc = subprocess.Popen(shlex.split(cmd), stdout=log, stderr=subprocess.STDOUT,
                                cwd=str(_find_repo_root()))
    _CHILDREN[proc.pid] = proc
    return _record({**base, "spawned": True, "command": cmd, "pid": proc.pid, "log": str(log_path)})


def make_spawn_runner(profile: dict):
    """Return a runner(item, agent) closure bound to `profile` (for scheduler.tick)."""
    def _runner(item, agent):
        return spawn_runner(item, agent, profile)
    return _runner


# --------------------------------------------------------------------------- CLI
def _cmd_dispatch(a) -> int:
    item = claim._load(a.item)
    if not item:
        print(f"no such work item: {a.item}", file=sys.stderr)
        return 1
    agent = agent_registry._load(a.signature)
    if not agent:
        print(f"no such agent: {a.signature}", file=sys.stderr)
        return 1
    rec = spawn_runner(item, agent, load_profile(a.profile))
    if rec.get("spawned"):
        print(f"spawned pid {rec['pid']}: {rec['item_id']} ({rec['vendor']} as {rec['role']})")
    else:
        print(f"dry-run (no spawn_command): {rec['item_id']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Spawn-runner — launch a real agent process for a work item")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("dispatch", help="Launch the agent for one item + signature.")
    d.add_argument("--item", required=True)
    d.add_argument("--signature", required=True)
    d.add_argument("--profile")
    d.set_defaults(func=_cmd_dispatch)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

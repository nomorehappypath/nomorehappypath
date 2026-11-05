#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Durable board-watch dispatcher; real waking is opt-in through the profile."""
from __future__ import annotations
import argparse, json, shlex, subprocess
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import board, child_process, contract
from harness.project_context import ProjectRoot, add_context_arguments, context_from_args, project_context

# Keep child handles only long enough to reap exited dispatch commands.  A
# scheduler tick must not leak a zombie process when the approved command is
# short-lived (the common case for a CLI agent launcher).
_CHILDREN: dict[int, subprocess.Popen] = {}

# Stable scheduling-domain identifier; changing it invalidates persisted cursors.
_DOMAIN_ID = "78e1407a-d41e-5bc1-b9c5-e74fb4e82fdb"

def reap_children():
    """Collect completed dispatch commands and return their exit statuses."""
    completed=[]
    for pid, proc in list(_CHILDREN.items()):
        status=proc.poll()
        if status is not None:
            proc.wait()
            del _CHILDREN[pid]
            completed.append({"pid":pid,"returncode":status})
    return completed

def tick(root: ProjectRoot, profile_path: Path, execute=False):
    """Run one scheduler cycle: reap children, find due board items, and optionally dispatch them.

    Args:
        root: the project root used to derive the storage context.
        profile_path: path to the validated project profile that authorizes
            per-vendor agent launch commands.
        execute: when true, spawn the approved vendor command for each due
            item instead of only recording what would have been dispatched.

    Return: a dict with the tick timestamp and the list of dispatch records,
    after appending that record to the dispatches log. Raises ValueError if
    the profile is invalid.
    """
    context = project_context(root)
    reap_children()
    profile_check=contract.validate_profile(profile_path)
    if not profile_check["valid"]: raise ValueError("invalid profile: "+", ".join(profile_check["missing"]))
    profile=profile_check["profile"]; due=board.watch(context, int(profile.get("agent_poll_interval_seconds",300)))
    dispatches=[]
    for item in due:
        template=(profile.get("agent_commands") or {}).get(item.get("vendor",""))
        dispatch={"delivery":item,"spawned":False,"reason":"no approved vendor command"}
        if execute and template:
            command=template.format(role=item.get("role",""), task=item.get("task",""), agent_id=item.get("agent_id",""), message=item.get("message",""))
            proc=subprocess.Popen(
                shlex.split(command), cwd=context.code_root,
                env=child_process.environment(git=True, shell=True),
            )
            _CHILDREN[proc.pid]=proc
            dispatch={"delivery":item,"spawned":True,"pid":proc.pid,"command":command}
        dispatches.append(dispatch)
    out={"at":board.now(),"dispatches":dispatches}; path=context.storage_path("board", "dispatches.jsonl"); path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a", encoding="utf-8") as f: f.write(json.dumps(out)+"\n")
    return out

def main(argv=None):
 """Parse CLI arguments, run one scheduler tick, and print the result as JSON.

 Args:
     argv: optional list of CLI argument strings; defaults to sys.argv when None.

 Returns: exit code 0 on success; an invalid profile is reported via
 parser.error before exit.
 """
 p=argparse.ArgumentParser(); add_context_arguments(p); p.add_argument("--profile",required=True); p.add_argument("--execute",action="store_true")
 a=p.parse_args(argv)
 try: out=tick(context_from_args(a),Path(a.profile).resolve(),a.execute)
 except ValueError as e: p.error(str(e))
 print(json.dumps(out,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())

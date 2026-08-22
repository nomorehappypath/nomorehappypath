#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Durable scheduler — the dispatch core of the orchestration substrate.

One TICK reads the claim queue (open work items) + the profile's vendor pools and, for each open
item, spawns a FRESH right-vendor agent (registered in the agent registry), claims the item for
it, and dispatches it through a pluggable runner. Cross-vendor is respected: the chosen vendor is
never the item's forbidden vendor. The default runner records the dispatch intent (no real
process spawn) so scheduling is fully testable; a real runner spawns the agent CLI. Stdlib only.

This replaces fragile in-session self-timers (D11): a durable scheduler drives ephemeral agents,
rather than long-lived agents trying to keep themselves polling.

  bash scheduler.sh tick                 # one scheduling pass
  bash scheduler.sh run --interval 120   # loop (stop-gap; durable OS-timer install is next)
"""
from __future__ import annotations

import argparse
import sys
import time

import agent_registry
import claim
from profile_config import load_profile


def _vendor_pools(profile: dict) -> list:
    pools: list = []
    for key in ("implementer_vendor", "reviewer_vendor"):
        v = profile.get(key)
        if v and v not in pools:
            pools.append(v)
    return pools


def pick_vendor(item: dict, profile: dict):
    """Choose a vendor from the profile pools, excluding the item's forbidden vendor."""
    forbid = item.get("forbid_vendor")
    for v in _vendor_pools(profile):
        if v != forbid:
            return v
    return None


def default_runner(item: dict, agent: dict) -> dict:
    """Record the dispatch intent WITHOUT spawning a real process (keeps the core testable).

    A real runner would here spawn the vendor's agent CLI (e.g. codex exec / claude) onboarded
    with the spawn directive for this item's task + role + signature.
    """
    return {
        "item_id": item["item_id"],
        "task": item.get("task_id"),
        "role": item.get("role"),
        "vendor": agent.get("vendor"),
        "signature": agent.get("signature"),
        "command": (
            f"run {agent.get('vendor')} as {agent.get('role')} on task "
            f"{item.get('task_id')} (item {item['item_id']}, sig {agent.get('signature')})"
        ),
        "spawned": False,
    }


def tick(profile: dict | None = None, runner=default_runner) -> dict:
    """One scheduling pass. Returns {'dispatched': [...], 'skipped': [...]}."""
    profile = profile if profile is not None else load_profile()
    dispatched: list = []
    skipped: list = []
    for item in claim.list_items():
        if item.get("status") != "open":
            continue
        vendor = pick_vendor(item, profile)
        if not vendor:
            skipped.append({
                "item_id": item["item_id"],
                "reason": "no eligible vendor (every profile pool vendor is forbidden, or pools unset)",
            })
            continue
        agent = agent_registry.register(vendor, item.get("role", "worker"), task=item.get("task_id"))
        try:
            claim.claim(item["item_id"], agent["signature"])
        except claim.ClaimError as e:
            agent_registry.retire(agent["signature"])  # lost the race / ineligible — don't leak the agent
            skipped.append({"item_id": item["item_id"], "reason": f"claim failed: {e}"})
            continue
        dispatch = runner(item, agent)
        agent_registry.heartbeat(agent["signature"])
        dispatched.append(dispatch)
    return {"dispatched": dispatched, "skipped": skipped}


# --------------------------------------------------------------------------- CLI
def _runner_for(a, profile):
    if getattr(a, "spawn", False):
        import runner as runner_mod  # real subprocess launcher (opt-in)
        return runner_mod.make_spawn_runner(profile)
    return default_runner


def _cmd_tick(a) -> int:
    profile = load_profile(a.profile)
    res = tick(profile, _runner_for(a, profile))
    print(f"dispatched {len(res['dispatched'])}, skipped {len(res['skipped'])}")
    for d in res["dispatched"]:
        detail = f"pid {d['pid']}" if d.get("spawned") and d.get("pid") else (d.get("command") or "dispatched")
        print(f"  -> {d.get('vendor','')} as {d.get('role','')} on {d.get('task','')} ({detail})")
    for s in res["skipped"]:
        print(f"  skip {s['item_id']}: {s['reason']}")
    return 0


def _cmd_run(a) -> int:
    if a.interval < 5:
        print("Refusing intervals below 5 seconds.", file=sys.stderr)
        return 2
    print(f"Scheduler loop every {a.interval}s (Ctrl-C to stop). NOTE: the durable OS-timer install is a later task.")
    while True:
        profile = load_profile(a.profile)
        res = tick(profile, _runner_for(a, profile))
        print(f"[tick] dispatched {len(res['dispatched'])}, skipped {len(res['skipped'])}")
        time.sleep(a.interval)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Durable scheduler — dispatch core of the orchestration substrate")
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("tick", help="Run one scheduling pass.")
    t.add_argument("--profile", help="Path to profile.config (else auto-discovered).")
    t.add_argument("--spawn", action="store_true", help="Launch real agent processes via the spawn-runner (opt-in).")
    t.set_defaults(func=_cmd_tick)

    r = sub.add_parser("run", help="Loop tick on an interval (a local OS timer via timer.sh is the durable form).")
    r.add_argument("--interval", type=int, default=120)
    r.add_argument("--profile")
    r.add_argument("--spawn", action="store_true", help="Launch real agent processes via the spawn-runner (opt-in).")
    r.set_defaults(func=_cmd_run)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(2)

#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Claim/assignment protocol — routing layer of the orchestration substrate.

Assignable work items live on the board under `.agents/queue/`. A registered agent (identified
by its agent_registry signature) atomically claims an item; no two agents can claim the same
one. The cross-vendor rule is enforced AT CLAIM TIME: an item may forbid a vendor (e.g. a review
item forbids the implementer's own vendor) so a same-vendor agent cannot claim it. Stdlib only.

  bash claim.sh post --task T1 --role reviewer --forbid-vendor "Claude (Anthropic)"
  bash claim.sh claim --item <item-id> --signature <sig>
  bash claim.sh list
  bash claim.sh complete --item <item-id>
  bash claim.sh release  --item <item-id>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import agent_registry  # vendor lookup by signature (only registered agents may claim)


class ClaimError(Exception):
    """A claim was rejected (item not open, lost race, cross-vendor, or unregistered agent)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", str(text).lower()))[:32] or "item"


def _find_repo_root() -> Path:
    here = Path.cwd().resolve()
    for c in [here, *here.parents]:
        if (c / ".agents").is_dir():
            return c
    raise SystemExit("No .agents directory found. Run from a repo root.")


def _queue_dir() -> Path:
    d = _find_repo_root() / ".agents" / "queue"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _item_path(item_id: str) -> Path:
    return _queue_dir() / f"{item_id}.json"


def _lock_path(item_id: str) -> Path:
    return _queue_dir() / f"{item_id}.lock"


def _write(rec: dict) -> dict:
    _item_path(rec["item_id"]).write_text(
        json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return rec


def _load(item_id: str):
    p = _item_path(item_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def post(task_id: str, role: str, forbid_vendor=None) -> dict:
    root = f"{_slug(task_id)}-{_slug(role)}-{uuid.uuid4().hex[:6]}"
    item_id = root
    n = 2
    while True:
        try:
            fh = _item_path(item_id).open("x", encoding="utf-8")  # atomic, never overwrite
            break
        except FileExistsError:
            item_id = f"{root}-{n}"
            n += 1
    rec = {
        "item_id": item_id,
        "task_id": task_id,
        "role": role,
        "forbid_vendor": forbid_vendor,
        "status": "open",
        "claimed_by": None,
        "claimed_at": None,
        "created_at": _now_iso(),
    }
    with fh:
        fh.write(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    return rec


def claim(item_id: str, signature: str) -> dict:
    agent = agent_registry._load(signature)
    if not agent:
        raise ClaimError(f"unregistered signature: {signature} (register the agent first)")
    item = _load(item_id)
    if not item:
        raise ClaimError(f"no such work item: {item_id}")
    if item["status"] != "open":
        raise ClaimError(
            f"item {item_id} is not open (status={item['status']}, claimed_by={item.get('claimed_by')})"
        )
    forbid = item.get("forbid_vendor")
    if forbid and agent.get("vendor") == forbid:
        raise ClaimError(
            f"cross-vendor violation: vendor '{agent.get('vendor')}' is forbidden for item {item_id}"
        )
    # Atomic gate: exactly one claimer wins, even if two pass the open-check concurrently.
    try:
        gate = _lock_path(item_id).open("x", encoding="utf-8")
    except FileExistsError:
        raise ClaimError(f"item {item_id} was just claimed by another agent")
    with gate:
        gate.write(signature + "\n")
    item["status"] = "claimed"
    item["claimed_by"] = signature
    item["claimed_at"] = _now_iso()
    return _write(item)


def complete(item_id: str) -> dict:
    item = _load(item_id)
    if not item:
        raise ClaimError(f"no such work item: {item_id}")
    item["status"] = "done"
    return _write(item)


def release(item_id: str) -> dict:
    item = _load(item_id)
    if not item:
        raise ClaimError(f"no such work item: {item_id}")
    lp = _lock_path(item_id)
    if lp.exists():
        lp.unlink()
    item["status"] = "open"
    item["claimed_by"] = None
    item["claimed_at"] = None
    return _write(item)


def list_items() -> list:
    out = []
    for p in sorted(_queue_dir().glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- CLI
def _cmd_post(a) -> int:
    print(post(a.task, a.role, a.forbid_vendor)["item_id"])
    return 0


def _cmd_claim(a) -> int:
    try:
        rec = claim(a.item, a.signature)
    except ClaimError as e:
        print(f"REJECTED: {e}", file=sys.stderr)
        return 1
    print(f"claimed {rec['item_id']} by {rec['claimed_by']}")
    return 0


def _cmd_complete(a) -> int:
    try:
        print(f"{complete(a.item)['item_id']} done")
    except ClaimError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


def _cmd_release(a) -> int:
    try:
        print(f"{release(a.item)['item_id']} released (open)")
    except ClaimError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


def _cmd_list(a) -> int:
    items = list_items()
    if not items:
        print("No work items.")
        return 0
    for r in items:
        print(
            f"{r['status']:<8} {r['item_id']:<34} role={str(r.get('role','')):<12} "
            f"forbid={r.get('forbid_vendor')}  claimed_by={r.get('claimed_by')}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Claim/assignment protocol — routing layer of the orchestration substrate")
    sub = p.add_subparsers(dest="command", required=True)

    po = sub.add_parser("post", help="Post an assignable work item (role + optional forbidden vendor).")
    po.add_argument("--task", required=True)
    po.add_argument("--role", required=True)
    po.add_argument("--forbid-vendor", dest="forbid_vendor")
    po.set_defaults(func=_cmd_post)

    cl = sub.add_parser("claim", help="Atomically claim an open item as a registered agent.")
    cl.add_argument("--item", required=True)
    cl.add_argument("--signature", required=True)
    cl.set_defaults(func=_cmd_claim)

    co = sub.add_parser("complete", help="Mark a claimed item done.")
    co.add_argument("--item", required=True)
    co.set_defaults(func=_cmd_complete)

    rl = sub.add_parser("release", help="Release a claim (item back to open).")
    rl.add_argument("--item", required=True)
    rl.set_defaults(func=_cmd_release)

    li = sub.add_parser("list", help="List work items and their status.")
    li.set_defaults(func=_cmd_list)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

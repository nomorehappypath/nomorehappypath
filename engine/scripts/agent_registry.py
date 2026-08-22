#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Agent registry — the identity layer of the orchestration substrate.

Mint unique per-agent signatures, record heartbeats, list agents as ACTIVE or STALE
(the dead-man's-switch), recycle an agent into a fresh generation that inherits its lineage,
and retire it. Runtime state lives on the board under `.agents/agents/` (transient). Stdlib only.

  bash agent_registry.sh register --vendor "Claude (Anthropic)" --role implementer --task T1
  bash agent_registry.sh heartbeat --signature claude-implementer-7f3a
  bash agent_registry.sh list [--stale-seconds 120]
  bash agent_registry.sh recycle --signature claude-implementer-7f3a
  bash agent_registry.sh retire  --signature claude-implementer-7f3a
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STALE_SECONDS = 120


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value):
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _slug(text: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", str(text).lower())
    return tokens[0][:16] if tokens else "agent"


def _find_repo_root() -> Path:
    here = Path.cwd().resolve()
    for c in [here, *here.parents]:
        if (c / ".agents").is_dir():
            return c
    raise SystemExit("No .agents directory found. Run from a repo root.")


def _agents_dir() -> Path:
    d = _find_repo_root() / ".agents" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file_for(signature: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", signature)
    return _agents_dir() / f"{safe}.json"


def _write(rec: dict) -> dict:
    _file_for(rec["signature"]).write_text(
        json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return rec


def _load(signature: str):
    p = _file_for(signature)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def register(vendor: str, role: str, task=None) -> dict:
    # Guarantee a collision-free signature. We atomically create the record file
    # with O_EXCL ("x"); if that signature already exists — even from a duplicated
    # UUID or a concurrent register — we disambiguate with a counter and retry until
    # the create succeeds. This NEVER overwrites an existing agent record.
    root = f"{_slug(vendor)}-{_slug(role)}-{uuid.uuid4().hex[:8]}"
    sig = root
    n = 2
    while True:
        try:
            fh = _file_for(sig).open("x", encoding="utf-8")
            break
        except FileExistsError:
            sig = f"{root}-{n}"
            n += 1
    now = _iso(_now())
    rec = {
        "signature": sig,
        "base": sig,
        "vendor": vendor,
        "role": role,
        "task": task,
        "generation": 1,
        "status": "active",
        "started_at": now,
        "heartbeat_at": now,
        "recycled_from": None,
        "recycled_to": None,
    }
    with fh:
        fh.write(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    return rec


def heartbeat(signature: str):
    rec = _load(signature)
    if not rec:
        return None
    rec["heartbeat_at"] = _iso(_now())
    return _write(rec)


def recycle(signature: str):
    """Mint a fresh generation that inherits the lineage; retire the old one."""
    old = _load(signature)
    if not old:
        return None
    gen = int(old.get("generation", 1)) + 1
    new_sig = f"{old['base']}#{gen}"
    now = _iso(_now())
    new = {
        "signature": new_sig,
        "base": old["base"],
        "vendor": old["vendor"],
        "role": old["role"],
        "task": old.get("task"),
        "generation": gen,
        "status": "active",
        "started_at": now,
        "heartbeat_at": now,
        "recycled_from": old["signature"],
        "recycled_to": None,
    }
    old["status"] = "retired"
    old["recycled_to"] = new_sig
    _write(old)
    return _write(new)


def retire(signature: str):
    rec = _load(signature)
    if not rec:
        return None
    rec["status"] = "retired"
    return _write(rec)


def _liveness(rec: dict, stale_seconds: int, now: datetime) -> str:
    if rec.get("status") == "retired":
        return "retired"
    hb = _parse(rec.get("heartbeat_at"))
    if hb is None:
        return "stale"
    return "active" if (now - hb).total_seconds() <= stale_seconds else "stale"


def list_agents(stale_seconds: int = DEFAULT_STALE_SECONDS) -> list:
    now = _now()
    out = []
    for p in sorted(_agents_dir().glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        rec = dict(rec)
        rec["liveness"] = _liveness(rec, stale_seconds, now)
        hb = _parse(rec.get("heartbeat_at"))
        rec["heartbeat_age_seconds"] = round((now - hb).total_seconds(), 1) if hb else None
        out.append(rec)
    return out


# --------------------------------------------------------------------------- CLI
def _cmd_register(a) -> int:
    print(register(a.vendor, a.role, a.task)["signature"])
    return 0


def _cmd_heartbeat(a) -> int:
    rec = heartbeat(a.signature)
    if not rec:
        print(f"No such agent: {a.signature}", file=sys.stderr)
        return 1
    print(f"{rec['signature']} heartbeat {rec['heartbeat_at']}")
    return 0


def _cmd_recycle(a) -> int:
    rec = recycle(a.signature)
    if not rec:
        print(f"No such agent: {a.signature}", file=sys.stderr)
        return 1
    print(f"{a.signature} -> {rec['signature']} (generation {rec['generation']})")
    return 0


def _cmd_retire(a) -> int:
    rec = retire(a.signature)
    if not rec:
        print(f"No such agent: {a.signature}", file=sys.stderr)
        return 1
    print(f"{rec['signature']} retired")
    return 0


def _cmd_list(a) -> int:
    agents = list_agents(a.stale_seconds)
    if not agents:
        print("No agents registered.")
        return 0
    for r in agents:
        age = r["heartbeat_age_seconds"]
        age_s = f"{age}s ago" if age is not None else "n/a"
        print(
            f"{r['liveness']:<7} {r['signature']:<26} {str(r.get('role','')):<12} "
            f"gen{r.get('generation', 1)}  vendor={r.get('vendor','')}  hb={age_s}  task={r.get('task')}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Agent registry — identity layer of the orchestration substrate")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("register", help="Mint a unique signature for a new agent.")
    r.add_argument("--vendor", required=True)
    r.add_argument("--role", required=True)
    r.add_argument("--task")
    r.set_defaults(func=_cmd_register)

    h = sub.add_parser("heartbeat", help="Advance an agent's heartbeat.")
    h.add_argument("--signature", required=True)
    h.set_defaults(func=_cmd_heartbeat)

    c = sub.add_parser("recycle", help="Recycle an agent into a fresh generation (inherits lineage).")
    c.add_argument("--signature", required=True)
    c.set_defaults(func=_cmd_recycle)

    t = sub.add_parser("retire", help="Retire an agent.")
    t.add_argument("--signature", required=True)
    t.set_defaults(func=_cmd_retire)

    le = sub.add_parser("list", help="List agents with ACTIVE/STALE/retired liveness.")
    le.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    le.set_defaults(func=_cmd_list)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

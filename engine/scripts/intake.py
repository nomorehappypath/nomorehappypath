#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Intake + planning — the front door.

Turns a user's plain-language need into a build PLAN, then decomposes the approved plan into
cross-vendor WORK ITEMS that feed the orchestration substrate:

  clarifying_questions(idea, intent)            # a few plain-English questions
  build_plan(idea, intent, answers, planner)    # plain-English requirements + features (+ ACs)
  render_plan_text(plan)                         # what the user approves (no tech jargon, D8)
  decompose(plan, profile)                       # posts implementer+reviewer items per feature

The PLANNER is a pluggable seam: the default is deterministic/offline (no LLM) so the pipeline
is testable; a real planner calls a vendor to generate richer features + acceptance criteria.
Per D8 the user only ever sees plain English (the WHAT); agents own the HOW. Stdlib only.

  bash intake.sh questions --idea "a tool to track invoices"
  bash intake.sh plan --idea "..." --intent commercial --features "log in" "see invoices"
  bash intake.sh decompose --plan .agents/intake/<plan_id>.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import claim
from profile_config import load_profile


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", str(text).lower()))[:40] or "item"


def _find_repo_root() -> Path:
    here = Path.cwd().resolve()
    for c in [here, *here.parents]:
        if (c / ".agents").is_dir():
            return c
    raise SystemExit("No .agents directory found. Run from a repo root.")


def _intake_dir() -> Path:
    d = _find_repo_root() / ".agents" / "intake"
    d.mkdir(parents=True, exist_ok=True)
    return d


def clarifying_questions(idea: str, intent: str = "") -> list:
    qs = [
        "Who will use this, and what is the single most important thing it must let them do?",
        "What does 'done' look like to you — what would you click or check to know it works?",
        "Is there anything it must NOT do, or any must-have you care about (in plain words)?",
    ]
    if (intent or "").lower().startswith("comm"):
        qs.append("Who pays for it, and what do they pay for?")
    return qs


def default_planner(idea: str, intent: str, answers: dict) -> dict:
    """Deterministic, offline planner (no LLM). Structures the user's input into a plan.

    The pluggable seam: a real planner would call a vendor to generate richer features + ACs.
    """
    feats = answers.get("features") or [idea]
    features = []
    for f in feats:
        f = str(f).strip()
        features.append({
            "name": f,
            "outcome": f"You will be able to: {f}",
            "acceptance_criteria": f'GIVEN the app is running WHEN a user uses "{f}" THEN it behaves as described',
        })
    summary = answers.get("requirements") or (f"Build: {idea}." + (f" Intent: {intent}." if intent else ""))
    return {
        "plan_id": f"{_slug(idea)}-{uuid.uuid4().hex[:6]}",
        "idea": idea,
        "intent": intent,
        "requirements": summary,
        "features": features,
        "created_at": _now_iso(),
    }


def build_plan(idea: str, intent: str = "", answers: dict | None = None, planner=default_planner) -> dict:
    return planner(idea, intent, answers or {})


def render_plan_text(plan: dict) -> str:
    out = [
        f"Build plan for: {plan.get('idea', '')}",
        f"Intent: {plan.get('intent', '') or '(unspecified)'}",
        "",
        "What you'll be able to do:",
    ]
    for i, f in enumerate(plan.get("features", []), 1):
        out.append(f"  {i}. {f.get('outcome', f.get('name', ''))}")
        out.append(f"       done when: {f.get('acceptance_criteria', '')}")
    out.append("")
    out.append("Approve this plan to start the build, or tell us what to change.")
    return "\n".join(out) + "\n"


def save_plan(plan: dict) -> Path:
    p = _intake_dir() / f"{plan['plan_id']}.json"
    p.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def decompose(plan: dict, profile: dict | None = None) -> list:
    """Turn an approved plan into cross-vendor work items on the claim queue.

    Per feature: an implementer item + a reviewer item that FORBIDS the implementer's vendor, so
    the scheduler routes the review to a different vendor (cross-vendor by construction).
    """
    profile = profile if profile is not None else load_profile()
    impl_vendor = profile.get("implementer_vendor")
    save_plan(plan)
    items = []
    for i, feat in enumerate(plan.get("features", []), 1):
        feat_task = f"{plan['plan_id']}:{_slug(feat.get('name', ''))}-{i}"
        items.append(claim.post(feat_task, "implementer"))
        items.append(claim.post(feat_task, "reviewer", forbid_vendor=impl_vendor))
    return items


# --------------------------------------------------------------------------- CLI
def _cmd_questions(a) -> int:
    for q in clarifying_questions(a.idea, a.intent or ""):
        print(f"- {q}")
    return 0


def _cmd_plan(a) -> int:
    answers = {"features": a.features} if a.features else {}
    plan = build_plan(a.idea, a.intent or "", answers)
    path = save_plan(plan)
    print(render_plan_text(plan), end="")
    print(f"\n(plan saved: {path})")
    return 0


def _cmd_decompose(a) -> int:
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    items = decompose(plan, load_profile(a.profile))
    print(f"posted {len(items)} work item(s) to the queue:")
    for it in items:
        print(f"  {it['role']:<12} {it['item_id']}  (forbid={it.get('forbid_vendor')})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Intake + planning — turn a need into a plan and work items")
    sub = p.add_subparsers(dest="command", required=True)

    q = sub.add_parser("questions", help="Plain-English clarifying questions for an idea.")
    q.add_argument("--idea", required=True)
    q.add_argument("--intent")
    q.set_defaults(func=_cmd_questions)

    pl = sub.add_parser("plan", help="Build + save + print a plain-English plan.")
    pl.add_argument("--idea", required=True)
    pl.add_argument("--intent")
    pl.add_argument("--features", nargs="*", help="Optional explicit feature list.")
    pl.set_defaults(func=_cmd_plan)

    de = sub.add_parser("decompose", help="Post cross-vendor work items from an approved plan file.")
    de.add_argument("--plan", required=True)
    de.add_argument("--profile")
    de.set_defaults(func=_cmd_decompose)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

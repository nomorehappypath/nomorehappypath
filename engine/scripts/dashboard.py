#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Live dashboard — observability over the orchestration substrate.

Reads the board (agent registry + claim queue) into a structured SNAPSHOT (nodes + edges +
liveness), and renders it two ways: a readable text view, and a self-contained HTML page with
cards (colored by liveness/status — a pulsing red glow when an agent's heartbeat is stale, the
dead-man's-switch) and curved SVG arrows showing which card links to what (an agent → the item
it claimed; recycle lineage gen→gen). Stdlib only. The React product UI consumes the same snapshot.

  bash dashboard.sh snapshot              # print the snapshot as JSON
  bash dashboard.sh show                  # text view
  bash dashboard.sh html [--out build/dashboard.html]   # write a browsable HTML page
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import agent_registry
import claim

# liveness/status -> accent color
_AGENT_COLOR = {"active": "#34d399", "stale": "#e3342f", "retired": "#94a3b8"}
_ITEM_COLOR = {"open": "#f59e0b", "claimed": "#3b82f6", "done": "#34d399"}

CARD_W, CARD_H, GAP, TOP = 380, 104, 44, 120
COL_AGENT_X, COL_ITEM_X = 60, 620


def snapshot(stale_seconds: int = agent_registry.DEFAULT_STALE_SECONDS) -> dict:
    agents = agent_registry.list_agents(stale_seconds)
    items = claim.list_items()
    nodes = []
    for a in agents:
        nodes.append({
            "id": a["signature"], "type": "agent", "label": a["signature"],
            "vendor": a.get("vendor"), "role": a.get("role"),
            "generation": a.get("generation", 1), "liveness": a.get("liveness"),
            "task": a.get("task"), "heartbeat_age_seconds": a.get("heartbeat_age_seconds"),
        })
    for it in items:
        nodes.append({
            "id": it["item_id"], "type": "item", "label": it["item_id"],
            "role": it.get("role"), "status": it.get("status"),
            "task": it.get("task_id"), "forbid_vendor": it.get("forbid_vendor"),
            "claimed_by": it.get("claimed_by"),
        })
    edges = []
    for it in items:
        if it.get("claimed_by"):
            edges.append({"from": it["claimed_by"], "to": it["item_id"], "kind": "claimed"})
    for a in agents:
        if a.get("recycled_from"):
            edges.append({"from": a["recycled_from"], "to": a["signature"], "kind": "recycled"})
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "nodes": nodes, "edges": edges}


def render_text(snap: dict) -> str:
    out = ["Live dashboard", "=" * 13, ""]
    agents = [n for n in snap["nodes"] if n["type"] == "agent"]
    items = [n for n in snap["nodes"] if n["type"] == "item"]
    out.append("Agents:")
    for a in agents or [{"label": "(none)", "liveness": "", "role": "", "vendor": ""}]:
        out.append(f"  [{a.get('liveness',''):<7}] {a['label']}  role={a.get('role','')}  vendor={a.get('vendor','')}")
    out.append("")
    out.append("Work items:")
    for it in items or [{"label": "(none)", "status": ""}]:
        out.append(f"  [{it.get('status',''):<8}] {it['label']}  role={it.get('role','')}  task={it.get('task','')}")
    out.append("")
    out.append("Links (arrows):")
    if not snap["edges"]:
        out.append("  (none)")
    for e in snap["edges"]:
        out.append(f"  {e['from']}  --{e['kind']}-->  {e['to']}")
    return "\n".join(out) + "\n"


def _layout(snap: dict) -> dict:
    pos = {}
    agents = [n for n in snap["nodes"] if n["type"] == "agent"]
    items = [n for n in snap["nodes"] if n["type"] == "item"]
    for i, n in enumerate(agents):
        pos[n["id"]] = (COL_AGENT_X, TOP + i * (CARD_H + GAP), CARD_W, CARD_H)
    for i, n in enumerate(items):
        pos[n["id"]] = (COL_ITEM_X, TOP + i * (CARD_H + GAP), CARD_W, CARD_H)
    return pos


def _edge_path(src, dst, kind) -> str:
    sx, sy, sw, sh = src
    dx, dy, dw, dh = dst
    if kind == "recycled":  # bow out to the right, source bottom -> target top
        x1, y1, x2, y2 = sx + sw / 2, sy + sh, dx + dw / 2, dy
        ctrl = f"C {x1 + 110:.0f},{y1 + 28:.0f} {x2 + 110:.0f},{y2 - 28:.0f}"
        stroke, dash = "#9aa4b2", ' stroke-dasharray="2,7" stroke-linecap="round"'
    else:  # claimed: smooth S-curve, source right -> target left
        x1, y1, x2, y2 = sx + sw, sy + sh / 2, dx, dy + dh / 2
        mx = (x1 + x2) / 2
        ctrl = f"C {mx:.0f},{y1:.0f} {mx:.0f},{y2:.0f}"
        stroke, dash = "#64748b", ""
    return (f'<path class="edge edge-{kind}" d="M{x1:.0f},{y1:.0f} {ctrl} {x2:.0f},{y2:.0f}" '
            f'stroke="{stroke}" stroke-width="2.5" fill="none" marker-end="url(#arrow)"{dash} />')


def render_html(snap: dict) -> str:
    pos = _layout(snap)
    n_rows = max(
        sum(1 for n in snap["nodes"] if n["type"] == "agent"),
        sum(1 for n in snap["nodes"] if n["type"] == "item"),
        1,
    )
    width = COL_ITEM_X + CARD_W + 60
    height = TOP + n_rows * (CARD_H + GAP) + 50

    svg_edges = [
        _edge_path(pos[e["from"]], pos[e["to"]], e["kind"])
        for e in snap["edges"] if e["from"] in pos and e["to"] in pos
    ]

    cards = []
    for n in snap["nodes"]:
        x, y, w, h = pos[n["id"]]
        if n["type"] == "agent":
            color = _AGENT_COLOR.get(n.get("liveness"), "#94a3b8")
            state = n.get("liveness", "")
            meta = (f'<span class="role">{html.escape(str(n.get("role","")))}</span>'
                    f' · {html.escape(str(n.get("vendor","")))} · gen{n.get("generation",1)}')
        else:
            color = _ITEM_COLOR.get(n.get("status"), "#94a3b8")
            state = n.get("status", "")
            meta = (f'<span class="role">{html.escape(str(n.get("role","")))}</span>'
                    f' · task {html.escape(str(n.get("task","")))}')
        cards.append(
            f'<div class="card liveness-{html.escape(str(state))}" '
            f'style="left:{x}px;top:{y}px;width:{w}px;height:{h}px;border-left:5px solid {color}">'
            f'<span class="pill" style="background:{color}">{html.escape(str(state))}</span>'
            f'<div class="sig">{html.escape(str(n["label"]))}</div>'
            f'<div class="meta">{meta}</div>'
            f"</div>"
        )

    generated = html.escape(snap.get("generated_at", ""))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>dev_harness — live dashboard</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:28px 36px; color:#e5e7eb;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:#0b0f14;
    background-image: radial-gradient(1100px 560px at 18% -8%, #1f2a3a 0%, rgba(31,42,58,0) 60%),
                      radial-gradient(900px 500px at 100% 0%, #20303a 0%, rgba(32,48,58,0) 55%); }}
  h1 {{ font-size:22px; font-weight:700; letter-spacing:.2px; margin:0 0 8px; }}
  h1 .accent {{ color:#60a5fa; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:18px; font-size:12.5px; color:#9aa4b2; margin-bottom:6px; }}
  .legend .chip {{ display:inline-flex; align-items:center; gap:7px; }}
  .legend .dot {{ width:11px; height:11px; border-radius:50%; display:inline-block; }}
  .legend .line {{ width:22px; height:0; border-top:2.5px solid #64748b; display:inline-block; }}
  .legend .line.dashed {{ border-top:2.5px dashed #9aa4b2; }}
  .stage {{ position:relative; width:{width}px; height:{height}px; margin-top:14px; }}
  .col-h {{ position:absolute; top:84px; font-size:12px; font-weight:700; letter-spacing:2px; color:#7e8a99; }}
  svg.edges {{ position:absolute; left:0; top:0; width:{width}px; height:{height}px; z-index:0; overflow:visible; }}
  .card {{ position:absolute; z-index:1; border-radius:16px; padding:15px 18px;
    background:linear-gradient(180deg,#1b2330 0%, #151b24 100%);
    border:1px solid rgba(255,255,255,.07);
    box-shadow:0 10px 26px rgba(0,0,0,.40);
    transition:transform .15s ease, box-shadow .15s ease; }}
  .card:hover {{ transform:translateY(-3px); box-shadow:0 16px 34px rgba(0,0,0,.55); }}
  .card .sig {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-size:15px; font-weight:600; color:#f3f4f6; word-break:break-all; line-height:1.3; padding-right:84px; }}
  .card .meta {{ font-size:13px; color:#9aa4b2; margin-top:10px; }}
  .card .meta .role {{ color:#c7d2fe; font-weight:600; text-transform:capitalize; }}
  .pill {{ position:absolute; right:14px; top:14px; font-size:10.5px; font-weight:800; letter-spacing:.5px;
    text-transform:uppercase; color:#0b0f14; border-radius:999px; padding:3px 11px; }}
  @keyframes pulse {{
    0%   {{ box-shadow:0 0 0 0 rgba(227,52,47,.55), 0 10px 26px rgba(0,0,0,.40); }}
    70%  {{ box-shadow:0 0 0 16px rgba(227,52,47,0), 0 10px 26px rgba(0,0,0,.40); }}
    100% {{ box-shadow:0 0 0 0 rgba(227,52,47,0), 0 10px 26px rgba(0,0,0,.40); }}
  }}
  .card.liveness-stale {{ animation:pulse 1.8s infinite; border-color:rgba(227,52,47,.5); }}
</style></head>
<body>
  <h1>dev_harness — <span class="accent">live agent dashboard</span></h1>
  <div class="legend">
    <span class="chip"><span class="dot" style="background:#34d399"></span>active</span>
    <span class="chip"><span class="dot" style="background:#e3342f"></span>stale — dead-man's-switch</span>
    <span class="chip"><span class="dot" style="background:#94a3b8"></span>retired</span>
    <span class="chip"><span class="dot" style="background:#3b82f6"></span>claimed item</span>
    <span class="chip"><span class="line"></span>claimed work</span>
    <span class="chip"><span class="line dashed"></span>recycle lineage</span>
  </div>
  <div class="sub" style="font-size:12px;color:#7e8a99;margin-top:2px">generated {generated}</div>
  <div class="stage">
    <div class="col-h" style="left:{COL_AGENT_X}px">AGENTS</div>
    <div class="col-h" style="left:{COL_ITEM_X}px">WORK ITEMS</div>
    <svg class="edges"><defs>
      <marker id="arrow" markerWidth="11" markerHeight="11" refX="8.5" refY="3" orient="auto" markerUnits="strokeWidth">
        <path d="M0,0 L0,6 L9,3 z" fill="#94a3b8"/></marker></defs>
      {''.join(svg_edges)}
    </svg>
    {''.join(cards)}
  </div>
</body></html>
"""


# --------------------------------------------------------------------------- CLI
def _cmd_snapshot(a) -> int:
    print(json.dumps(snapshot(a.stale_seconds), indent=2))
    return 0


def _cmd_show(a) -> int:
    print(render_text(snapshot(a.stale_seconds)), end="")
    return 0


def _cmd_html(a) -> int:
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(snapshot(a.stale_seconds)), encoding="utf-8")
    print(f"wrote {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Live dashboard — observability over the orchestration substrate")
    sub = p.add_subparsers(dest="command", required=True)
    for name, fn, helptext in (
        ("snapshot", _cmd_snapshot, "Print the live snapshot as JSON."),
        ("show", _cmd_show, "Print the text view."),
        ("html", _cmd_html, "Write a self-contained HTML dashboard."),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--stale-seconds", type=int, default=agent_registry.DEFAULT_STALE_SECONDS)
        if name == "html":
            sp.add_argument("--out", default="build/dashboard.html")
        sp.set_defaults(func=fn)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

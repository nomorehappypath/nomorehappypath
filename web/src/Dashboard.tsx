// Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import type { Snapshot, SNode } from "./intake"

const AGENT_COLOR: Record<string, string> = { active: "#34d399", stale: "#e3342f", retired: "#94a3b8" }
const ITEM_COLOR: Record<string, string> = { open: "#f59e0b", claimed: "#3b82f6", done: "#34d399" }
const CARD_W = 300, CARD_H = 80, GAP = 26, TOP = 20, AX = 20, IX = 400

export function Dashboard({ snapshot }: { snapshot: Snapshot }) {
  const agents = snapshot.nodes.filter((n) => n.type === "agent")
  const items = snapshot.nodes.filter((n) => n.type === "item")
  const pos: Record<string, { x: number; y: number }> = {}
  agents.forEach((n, i) => (pos[n.id] = { x: AX, y: TOP + i * (CARD_H + GAP) }))
  items.forEach((n, i) => (pos[n.id] = { x: IX, y: TOP + i * (CARD_H + GAP) }))
  const rows = Math.max(agents.length, items.length, 1)
  const W = IX + CARD_W + 20
  const H = TOP + rows * (CARD_H + GAP) + 10

  const color = (n: SNode) =>
    n.type === "agent" ? AGENT_COLOR[n.liveness ?? ""] ?? "#94a3b8" : ITEM_COLOR[n.status ?? ""] ?? "#94a3b8"
  const state = (n: SNode) => (n.type === "agent" ? n.liveness : n.status)

  return (
    <div className="panel">
      <p className="lead">Live agents and the work they're doing. Red = stale (dead-man's-switch).</p>
      <div className="stage" style={{ position: "relative", width: W, height: H }}>
        <svg width={W} height={H} style={{ position: "absolute", left: 0, top: 0 }}>
          <defs>
            <marker id="ar" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
              <path d="M0,0 L0,6 L9,3 z" fill="#7a8699" />
            </marker>
          </defs>
          {snapshot.edges
            .filter((e) => pos[e.from] && pos[e.to])
            .map((e, i) => {
              const a = pos[e.from], b = pos[e.to]
              const recy = e.kind === "recycled"
              const x1 = recy ? a.x + CARD_W / 2 : a.x + CARD_W
              const y1 = recy ? a.y + CARD_H : a.y + CARD_H / 2
              const x2 = recy ? b.x + CARD_W / 2 : b.x
              const y2 = recy ? b.y : b.y + CARD_H / 2
              const mx = (x1 + x2) / 2
              return (
                <path
                  key={i}
                  d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
                  stroke="#7a8699"
                  strokeWidth={2.5}
                  fill="none"
                  markerEnd="url(#ar)"
                  strokeDasharray={recy ? "5,4" : undefined}
                />
              )
            })}
        </svg>
        {snapshot.nodes.map((n) => {
          const p = pos[n.id]
          return (
            <div
              key={n.id}
              className={`card liveness-${state(n)}`}
              style={{ position: "absolute", left: p.x, top: p.y, width: CARD_W, height: CARD_H, borderLeft: `5px solid ${color(n)}` }}
            >
              <span className="pill" style={{ background: color(n) }}>{state(n)}</span>
              <div className="sig">{n.label}</div>
              <div className="meta">
                {n.role}
                {n.vendor ? ` · ${n.vendor}` : ""}
                {n.task ? ` · ${n.task}` : ""}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

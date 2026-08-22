// Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import { useState, useRef, useEffect, type FormEvent } from "react"
import { buildPlan, extractFeatures, type Plan } from "./intake"
import { Dashboard } from "./Dashboard"
import { SAMPLE_SNAPSHOT } from "./sample"

type Tab = "build" | "dashboard"

export default function App() {
  const [tab, setTab] = useState<Tab>("build")
  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand">◇ dev_harness</div>
        <button className={tab === "build" ? "on" : ""} onClick={() => setTab("build")}>✦&nbsp; Build something</button>
        <button className={tab === "dashboard" ? "on" : ""} onClick={() => setTab("dashboard")}>◉&nbsp; Live agents</button>
        <div className="rail-foot">cross-vendor AI build harness</div>
      </aside>
      <main className="main">
        {tab === "build" ? <Build /> : <div className="board-wrap"><Dashboard snapshot={SAMPLE_SNAPSHOT} /></div>}
      </main>
    </div>
  )
}

const EXAMPLES = [
  "An invoice tracker for my consulting clients",
  "A booking page for a barber shop",
  "An internal tool to track job applicants",
  "A simple CRM for a small sales team",
]

const QUESTIONS = [
  { key: "audience", q: "Love it. Who's it for — and what's the single most important thing it must let them do?" },
  { key: "features", q: 'Got it. What should people be able to do in it? Just say it naturally — e.g. "log in, add an invoice, see overdue ones".' },
  { key: "done", q: "Last one: what would you click or look at to know it's working?" },
]

type Msg = { role: "ai" | "you"; text: string }

function Build() {
  const [idea, setIdea] = useState("")
  const [intent, setIntent] = useState("feasibility")
  const [started, setStarted] = useState(false)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [step, setStep] = useState(0)
  const [features, setFeatures] = useState<string[]>([])
  const [draft, setDraft] = useState("")
  const [plan, setPlan] = useState<Plan | null>(null)
  const [approved, setApproved] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }) }, [msgs])

  const start = (text: string) => {
    const t = text.trim()
    if (!t) return
    setIdea(t)
    setStarted(true)
    setStep(0)
    setMsgs([{ role: "you", text: t }, { role: "ai", text: `"${t}" — nice. ${QUESTIONS[0].q}` }])
  }

  const send = (e: FormEvent) => {
    e.preventDefault()
    const t = draft.trim()
    if (!t) return
    setDraft("")
    const next: Msg[] = [...msgs, { role: "you", text: t }]

    if (step >= QUESTIONS.length) {
      next.push({ role: "ai", text: "Noted — I'll fold that into the plan once live build wiring is connected (next slice)." })
      setMsgs(next)
      return
    }

    let feats = features
    if (QUESTIONS[step].key === "features") {
      feats = extractFeatures(t)
      setFeatures(feats)
    }

    const ni = step + 1
    if (ni < QUESTIONS.length) {
      next.push({ role: "ai", text: QUESTIONS[ni].q })
      setStep(ni)
    } else {
      const p = buildPlan(idea, intent, feats.length ? feats : extractFeatures(t))
      setPlan(p)
      setStep(ni)
      next.push({ role: "ai", text: "Perfect — I've drafted your build plan on the right. Approve it and the agents start: each feature gets a Claude implementer and a Codex reviewer." })
    }
    setMsgs(next)
  }

  if (!started) {
    return (
      <div className="hero">
        <div className="hero-inner">
          <div className="kicker">AI BUILD HARNESS</div>
          <h1>What do you want to build?</h1>
          <p className="sub">Describe your app in plain words. We'll ask a couple of quick questions, draft a plan you approve — then a team of AI agents builds it and cross-checks each other's work.</p>
          <form onSubmit={(e) => { e.preventDefault(); start(idea) }}>
            <textarea autoFocus value={idea} onChange={(e) => setIdea(e.target.value)} rows={3}
              placeholder="e.g. A tool for my clinic to book appointments and send reminders…"
              onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); start(idea) } }} />
            <div className="hero-row">
              <select value={intent} onChange={(e) => setIntent(e.target.value)}>
                <option value="feasibility">Just exploring</option>
                <option value="open-source">Open source</option>
                <option value="commercial">A real product</option>
              </select>
              <button type="submit" className="cta">Start building →</button>
            </div>
          </form>
          <div className="chips">
            <span className="chips-label">or try</span>
            {EXAMPLES.map((ex, i) => <button key={i} className="chip" onClick={() => start(ex)}>{ex}</button>)}
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="split">
      <section className="chat">
        <div className="msgs">
          {msgs.map((m, i) => (
            <div key={i} className={`bubble ${m.role}`}>
              {m.role === "ai" && <span className="who">harness</span>}
              {m.text}
            </div>
          ))}
          <div ref={endRef} />
        </div>
        <form className="composer" onSubmit={send}>
          <input autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={plan ? "Ask for a change…" : "Type your answer…"} />
          <button type="submit" className="cta">Send</button>
        </form>
      </section>

      <aside className="planpane">
        <div className="plan-head">Your build plan</div>
        {!plan ? (
          <div className="plan-empty">
            <div className="idea-pill">{idea}</div>
            {features.length > 0 && <ul className="feat-pre">{features.map((f, i) => <li key={i}>{f}</li>)}</ul>}
            <p className="hint">Answer a couple of questions and your plan fills in here.</p>
          </div>
        ) : (
          <div className="plan-body">
            <div className="idea-pill">{plan.idea}</div>
            <h3>What you'll be able to do</h3>
            <ol>
              {plan.features.map((f, i) => (
                <li key={i}>
                  <strong>{f.outcome.replace("You will be able to: ", "")}</strong>
                  <div className="ac">done when: {f.acceptance_criteria}</div>
                </li>
              ))}
            </ol>
            {!approved ? (
              <button className="approve" onClick={() => setApproved(true)}>Approve &amp; build →</button>
            ) : (
              <div className="approved">✓ Approved — {plan.features.length} feature(s) queued. Each becomes a Claude implementer + a Codex reviewer. Watch them on <b>Live agents</b>.</div>
            )}
          </div>
        )}
      </aside>
    </div>
  )
}

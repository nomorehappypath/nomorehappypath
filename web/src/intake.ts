// Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
// Shared types + client-side helpers for the intake conversation. Mirrors the engine's
// deterministic planner (intake.py default_planner): features → plain-English outcomes + ACs.
// The REAL plan (LLM planner) + live build arrive via the local API in the next slice.

export type Feature = { name: string; outcome: string; acceptance_criteria: string }
export type Plan = { idea: string; intent: string; requirements: string; features: Feature[] }

export type SNode = {
  id: string
  type: "agent" | "item"
  label: string
  liveness?: string
  status?: string
  role?: string
  vendor?: string
  task?: string
}
export type SEdge = { from: string; to: string; kind: string }
export type Snapshot = { nodes: SNode[]; edges: SEdge[] }

export function buildPlan(idea: string, intent: string, features: string[]): Plan {
  const feats = features.length ? features : [idea]
  return {
    idea,
    intent,
    requirements: `Build: ${idea}.` + (intent ? ` Intent: ${intent}.` : ""),
    features: feats.map((f) => ({
      name: f,
      outcome: `You will be able to: ${f}`,
      acceptance_criteria: `GIVEN the app is running WHEN a user uses "${f}" THEN it behaves as described`,
    })),
  }
}

// Pull candidate features out of free-form text the user types in the conversation
// ("log in, add an invoice and see overdue ones" → 3 features). A lightweight stand-in for
// the LLM extraction that the server-side planner will do.
export function extractFeatures(text: string): string[] {
  return text
    .split(/\r?\n|,|;|\band\b|\bthen\b/i)
    .map((s) => s.replace(/^[\s\-*\d.)]+/, "").trim())
    .filter((s) => s.length > 1)
}

export function clarifyingQuestions(intent: string): string[] {
  const qs = [
    "Who will use this, and what is the single most important thing it must let them do?",
    "What does 'done' look like — what would you click to know it works?",
    "Anything it must NOT do, or any must-have (in plain words)?",
  ]
  if (intent.startsWith("comm")) qs.push("Who pays for it, and what do they pay for?")
  return qs
}

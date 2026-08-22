// Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import type { Snapshot } from "./intake"

// Sample board snapshot (same shape as engine/scripts/dashboard.py snapshot()).
// Slice 1 renders this; the next slice fetches the live snapshot from the local API.
export const SAMPLE_SNAPSHOT: Snapshot = {
  nodes: [
    { id: "claude-implementer-7a3f", type: "agent", label: "claude-implementer-7a3f", liveness: "active", role: "implementer", vendor: "Claude (Anthropic)", task: "build-login" },
    { id: "codex-reviewer-9b21", type: "agent", label: "codex-reviewer-9b21", liveness: "active", role: "reviewer", vendor: "Codex (OpenAI)", task: "build-login" },
    { id: "claude-cto-1e1d", type: "agent", label: "claude-cto-1e1d", liveness: "stale", role: "cto", vendor: "Claude (Anthropic)", task: "build-login" },
    { id: "codex-qa-0aa0", type: "agent", label: "codex-qa-0aa0", liveness: "retired", role: "qa", vendor: "Codex (OpenAI)" },
    { id: "codex-qa-0aa0#2", type: "agent", label: "codex-qa-0aa0#2", liveness: "active", role: "qa", vendor: "Codex (OpenAI)" },
    { id: "build-login-impl", type: "item", label: "build-login-implementer", status: "claimed", role: "implementer", task: "build-login" },
    { id: "build-login-rev", type: "item", label: "build-login-reviewer", status: "claimed", role: "reviewer", task: "build-login" },
  ],
  edges: [
    { from: "claude-implementer-7a3f", to: "build-login-impl", kind: "claimed" },
    { from: "codex-reviewer-9b21", to: "build-login-rev", kind: "claimed" },
    { from: "codex-qa-0aa0", to: "codex-qa-0aa0#2", kind: "recycled" },
  ],
}

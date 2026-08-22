// Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import { describe, it, expect } from "vitest"
import { buildPlan, clarifyingQuestions, extractFeatures } from "./intake"

describe("intake", () => {
  it("buildPlan turns features into plain-English outcomes + Given/When/Then ACs", () => {
    const plan = buildPlan("an app", "commercial", ["log in", "see reports"])
    expect(plan.features).toHaveLength(2)
    for (const f of plan.features) {
      expect(f.outcome).toContain("You will be able to")
      const ac = f.acceptance_criteria.toUpperCase()
      expect(ac).toContain("GIVEN")
      expect(ac).toContain("WHEN")
      expect(ac).toContain("THEN")
    }
  })

  it("falls back to the idea when no features are given", () => {
    expect(buildPlan("thing", "", []).features).toHaveLength(1)
  })

  it("extractFeatures splits natural text (commas / 'and' / newlines) into features", () => {
    const f = extractFeatures("log in, add an invoice and see overdue ones")
    expect(f).toContain("log in")
    expect(f).toContain("add an invoice")
    expect(f.length).toBeGreaterThanOrEqual(3)
  })

  it("clarifyingQuestions adds a pricing question for commercial intent", () => {
    expect(clarifyingQuestions("commercial").some((q) => q.includes("pays"))).toBe(true)
    expect(clarifyingQuestions("feasibility").some((q) => q.includes("pays"))).toBe(false)
  })
})

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner 2026-10-12: every app gets a central error sink; enhanced security is a per-task checkbox.

"ok i want you to go for the catch-all. ... have a check box per task and not
per project, and once task is reviewed we cannot changed it"

The agents' rulebook (directives/AGENT.md, injected whole into every Delivery
and Reviewer agent) must carry both sections, short enough to stay
proportionate, the reviewer must check them, the CTO must point at them, and
the concrete sink contract must exist next to the directive.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "directives" / "AGENT.md"
CTO = ROOT / "directives" / "CTO.md"
CONTRACT = ROOT / "directives" / "ERROR_SINK_CONTRACT.md"
SINK = "### Central error sink"
ENHANCED = "### Enhanced security (owner checkbox)"
BASELINE = "### Minimum security baseline"
SINK_WORD_CAP = 500
ENHANCED_WORD_CAP = 620


def flat(text: str) -> str:
    return " ".join(text.split())


def section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.index("\n### ", start + len(heading))
    return text[start:end]


class EnhancedSecurityDirectiveTests(unittest.TestCase):
    def setUp(self):
        self.text = AGENT.read_text(encoding="utf-8")

    def test_both_sections_sit_once_after_the_baseline_inside_the_delivery_workflow(self):
        for heading in (SINK, ENHANCED):
            self.assertEqual(self.text.count(heading), 1, heading)
        baseline, sink, enhanced = (self.text.index(h) for h in (BASELINE, SINK, ENHANCED))
        self.assertLess(self.text.index("## Delivery Agent workflow"), baseline)
        self.assertLess(baseline, sink)
        self.assertLess(sink, enhanced)
        self.assertLess(enhanced, self.text.index("### Scope control for newly discovered findings"))
        self.assertLess(enhanced, self.text.index("## Independent Reviewer workflow"))

    def test_the_sink_section_states_the_contract_every_app_implements(self):
        sink = flat(section(self.text, SINK))
        for needle in (
            "Every app, on or off the checkbox below",
            "one error module with one entry point",
            "uncaught exceptions and unhandled rejections",
            "`/api/client-errors`",
            "hard kills, resource exhaustion, failures before start-up",
            "the process still exits non-zero; never resume after one",
            "`directives/ERROR_SINK_CONTRACT.md`",
            "`schema` `nmhp-error/1`",
            "`reference` (short id also shown to the user",
            "`fingerprint`",
            "`ERROR_SINK_FILE`",
            "`-` means standard error",
            "`ERROR_SINK_URL` with `ERROR_SINK_TOKEN`",
            "the owner's future error service plugs in here without an app change",
            "`ERROR_SINK_RATE`",
            "Delivery is best effort and said so",
            "never raises, never blocks a request, never re-enters itself",
            "Every field is scrubbed",
            "a rejected report never produces another",
            "A static site posts browser failures straight to a build-time receiver URL",
            "The README says where errors go",
            "loopback receiver the test starts",
            "the test's planted secret appears in no event",
        ):
            self.assertIn(needle, sink, needle)

    def test_the_enhanced_section_is_the_owner_checkbox_locked_at_review(self):
        enhanced = flat(section(self.text, ENHANCED))
        for needle in (
            "Applies only when `enhanced_security.on` is true in your task's context bundle",
            "it locks at the task's first review request",
            "`SECURITY.md` at its root",
            "a short threat model",
            "A controls register",
            "History scan: run by Delivery",
            "`S-SEC-1`, `S-SEC-2`",
            "never reuses an earlier result for their commands",
            "a secret scan of the delivered tree",
            "standard static security scanner",
            "the dependency audit",
            "an attack run",
            "a test in the app's own suite",
            "never an owner-home install",
            "without a documented disposition",
            "redacted summary (file, line, rule; never a value) on success and on failure",
            "not applicable with the reason",
            "owner-action card naming the failed command",
            "A secret found in history is rotated, not only deleted",
            "this stronger rule governs the dependency audit",
            "declares `SECURITY.md`",
            "a human penetration test and a compliance certification (SOC 2, ISO 27001) are outside services",
            "never reported as either",
            "Multi-factor login and other features nobody asked for are still not demanded",
        ):
            self.assertIn(needle, enhanced, needle)

    def test_both_sections_stay_short(self):
        self.assertLessEqual(len(re.findall(r"\S+", section(self.text, SINK))), SINK_WORD_CAP)
        self.assertLessEqual(len(re.findall(r"\S+", section(self.text, ENHANCED))), ENHANCED_WORD_CAP)

    def test_the_baseline_points_at_both_instead_of_forbidding_them(self):
        baseline = flat(section(self.text, BASELINE))
        self.assertNotIn("no threat models, penetration-test rituals or compliance frameworks", baseline)
        self.assertIn("threat models and security test passes belong to Enhanced security below, which the owner switches on per task", baseline)
        self.assertIn("Every error goes through the Central error sink below", baseline)

    def test_the_product_commit_may_carry_the_security_file_and_the_error_module(self):
        workflow = flat(self.text[self.text.index("## Delivery Agent workflow"):self.text.index(BASELINE)])
        self.assertIn("`SECURITY.md` and the app's error module (Central error sink below) are product files, not governance artifacts", workflow)

    def test_the_quality_bar_names_both_as_material(self):
        quality_bar = flat(self.text[self.text.index("### Delivery quality bar"):self.text.index(BASELINE)])
        self.assertIn("the Central error sink below missing or broken; an Enhanced security deliverable missing while the task's checkbox is on", quality_bar)

    def test_the_reviewer_checks_every_app_for_the_sink_and_the_deliverables_only_when_on(self):
        reviewer = flat(self.text[self.text.index("## Independent Reviewer workflow"):])
        self.assertIn("**Central error sink and Enhanced security (owner, 2026-10-12).**", reviewer)
        for words in (
            "FAIL when an error path the app has bypasses the sink",
            "When `enhanced_security.on` is true in the review brief",
            "FAIL when `SECURITY.md` is missing or stale",
            "an unjustified disposition is a FAIL",
            "Name the frozen On choice and the missing deliverable in the verdict",
            "never fail an app for lacking Enhanced security deliverables while the task's checkbox is off",
        ):
            self.assertIn(words, reviewer, words)
        self.assertNotIn("never fail an app for lacking enterprise-grade controls", reviewer)

    def test_the_cto_release_gate_points_at_both(self):
        cto = flat(CTO.read_text(encoding="utf-8"))
        self.assertIn("so is a missing or broken Central error sink, or an Enhanced security deliverable missing while the task's checkbox is on", cto)
        self.assertIn("a security check that could not run is raised to you", cto)

    def test_the_contract_file_carries_fields_settings_limits_and_proofs(self):
        contract = CONTRACT.read_text(encoding="utf-8")
        for needle in (
            "nmhp-error/1", "| `id` |", "| `reference` |", "| `fingerprint` |", "| `context` |",
            "`ERROR_SINK_FILE`", "`ERROR_SINK_URL`", "`ERROR_SINK_TOKEN`", "`ERROR_SINK_RATE`",
            "`-` = standard error", "rotate at 10 MB", "3xx is a failure", "never follow redirects",
            "bounded queue (500 events or 8 MB)", "`nmhp.sink.counters`", "never re-enters itself",
            "`/api/client-errors`", "## 7. Proofs", "## 8. README",
            "Start-up validation", "by setting NAME (never its value)",
        ):
            self.assertIn(needle, contract, needle)


if __name__ == "__main__":
    unittest.main()

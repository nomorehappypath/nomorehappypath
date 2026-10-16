# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""2026-10-14/15, the owner's Studio project: Delivery filed nine in-scope findings and asked for owner
cards about its own browser tool, then an HTTP 500 of its own fresh run, then a sign-in it lacked, and
each steered the task back to a step the owner had declared not required. Resumed at 00:45Z, the CTO
posted two bland cycles over a wrong owner card and eight stale findings and cleared them only after
the owner's messages. Delivery and the Reviewer are launched with directives/AGENT.md and the CTO with
directives/CTO.md (scripts/run_managed_agent.sh); these pin the two rules that forbid each of those,
there and in the short-form mirrors under harness/directives.
"""
import unittest
from pathlib import Path

from harness import claude_inbox, gemini_runtime

ROOT = Path(__file__).resolve().parents[1]


def flat(path):
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


class AgentOwnToolLimits(unittest.TestCase):
    def setUp(self):
        self.agent = flat("directives/AGENT.md")
        self.spawn = flat("harness/directives/00_SPAWN_DEVELOPMENT_DIRECTIVE.md")
        self.reviewer = flat("harness/directives/AUTONOMOUS_COMPLETION_DIRECTIVE.md")

    def test_launcher_loads_the_agent_directive_whole_for_delivery_and_reviewer(self):
        launcher = (ROOT / "scripts/run_managed_agent.sh").read_text()
        self.assertEqual(launcher.count('directive="$(<"$harness_root/directives/AGENT.md")"'), 2)

    def test_quality_bar_says_an_own_limit_is_never_a_finding_card_or_stop(self):
        self.assertIn("Your own tool, sandbox or session limit is never a finding, never an owner card, and never a reason to stop the rest", self.agent)
        self.assertIn("a browser tool denying a host, a CUA permission, a sign-in you yourself lack, a sandbox refusal", self.agent)
        self.assertIn("not a defect of the product and not the owner's chore", self.agent)
        self.assertIn('never record it as an in-scope finding ("fix before the task can be completed")', self.agent)
        self.assertIn("never ask for or pin an owner-action card for it", self.agent)
        self.assertIn("never let it block the deliverables that do not need the refused step", self.agent)

    def test_the_agent_reports_a_route_question_and_continues(self):
        self.assertIn("Post it to the CTO as a route question in one brief update", self.agent)
        self.assertIn("what is blocked, the exact refusal (tool, host and message, with any secret redacted; never raw output that carries a credential)", self.agent)
        self.assertIn("which Completion Contract deliverables proceed without it", self.agent)
        self.assertIn("whether every remaining deliverable needs it; then continue on those deliverables", self.agent)

    def test_the_cto_decides_owner_actions_and_product_defects_stay_findings(self):
        self.assertIn("The CTO's triage decides whose limit it is and whether an owner-only action exists", self.agent)
        self.assertIn("pins that card itself", self.agent)
        self.assertIn("Inability to execute a step is not by itself evidence of a product defect", self.agent)
        self.assertIn("a step you could not run stays unverified, never claimed", self.agent)

    def test_scope_control_keeps_impacts_current_task_for_product_defects_only(self):
        self.assertIn("`impacts_current_task` is for a defect of the product under test", self.agent)
        self.assertIn("it is a route question to the CTO, never a finding and never a card", self.agent)
        self.assertIn("a finding already on the board whose subject is that limit is the CTO's to resolve (the board gives you no `finding-resolved`), and your route question is its evidence", self.agent)
        # the existing card paths and the Reviewer's NOT TESTED rule are kept, not contradicted
        self.assertIn("The existing card paths stay as written and name what they name, not your tool", self.agent)
        self.assertIn("a step it cannot run for a limit of its own makes the verdict `TASK DONE: NOT TESTED`", self.agent)
        self.assertIn("`TASK DONE: NOT TESTED - <the step you could not run and why>`", self.agent)

    def test_mirrors_carry_the_rule_in_short_form(self):
        self.assertIn("Your own tool, sandbox or session limit", self.spawn)
        self.assertIn("is never an in-scope finding, never an owner-action card and never blocks the deliverables that do not need it", self.spawn)
        self.assertIn("post one route question to the CTO", self.spawn)
        self.assertIn("Inability to run a step is not by itself a product defect", self.spawn)
        self.assertIn("A limit of the reviewer's own tool, sandbox or session", self.reviewer)
        self.assertIn("is never a finding or an owner card: the step is `TASK DONE: NOT TESTED`", self.reviewer)


class CtoRetriageOnLaunchOrResume(unittest.TestCase):
    def setUp(self):
        self.cto = flat("directives/CTO.md")
        self.mirror = flat("harness/directives/CTO_COMPLETION_DIRECTIVE.md")

    def test_the_paragraph_sits_inside_what_must_never_block_the_work(self):
        text = (ROOT / "directives/CTO.md").read_text(encoding="utf-8")
        start = text.index("## What must never block the work")
        end = text.index("### Findings outside the current objective")
        self.assertIn("First cycle after launch or resume: re-triage what you inherited", text[start:end])

    def test_first_cycle_retriages_every_card_and_finding(self):
        self.assertIn("In the first cycle after launch or resume, after the mandatory poll, re-triage every open owner-action card and every in-scope finding of every active task under this section as if it had just been reported", self.cto)
        self.assertIn("clear with `owner-action-done` what is not provably the owner's, naming the real cause", self.cto)
        self.assertIn("treat as non-blocking what would not change what the owner receives", self.cto)
        self.assertIn("by resolving yourself, with `finding-resolved`, a finding whose subject is an agent's own tool limit, the agent's route question as its evidence (Delivery cannot resolve a finding)", self.cto)
        self.assertIn("route what remains", self.cto)

    def test_retriage_keeps_attempt_history_and_respects_a_pause(self):
        self.assertIn("Count the routed attempts already on the board; do not restart them", self.cto)
        self.assertIn("A paused task is inspected without restarting its work or overriding the owner's pause", self.cto)

    def test_the_result_is_stated_in_that_cycle(self):
        self.assertIn("State the result in that cycle's status line (cards cleared, findings disposed of, what was routed), or \"nothing inherited\"", self.cto)
        self.assertIn("a CTO that waits for the owner to point at a stale card has already lost the cycle", self.cto)

    def test_mirror_carries_the_rule_in_short_form(self):
        self.assertIn("In the first cycle after launch or resume, re-triage every open owner-action card and every in-scope finding of every active task", self.mirror)
        self.assertIn("state the result in that cycle's status", self.mirror)

    def test_the_cto_never_ends_on_a_question_about_its_own_job(self):
        """Owner 2026-10-15: "do the necessary and do not ask me such questions"."""
        text = (ROOT / "directives/CTO.md").read_text(encoding="utf-8")
        start = text.index("## What must never block the work")
        end = text.index("### Findings outside the current objective")
        self.assertIn("Never end on a question about your own job", text[start:end])
        self.assertIn("never ends with a question about a step this directive already assigns to you", self.cto)
        self.assertIn("routing Delivery to file or amend a requirement after an owner waiver, clearing a card, resolving or disposing a finding, pushing a stalled agent", self.cto)
        self.assertIn("Do it, then report `done: <what>` in one line", self.cto)
        self.assertIn("The only questions for the owner are WHAT decisions not yet taken", self.cto)
        self.assertIn("state your recommendation and what happens if the owner says nothing; silence is never read as a waiver of a required gate", self.cto)
        self.assertIn('"Want me to do that now?" and "Should I push Delivery?" are defects of the CTO', self.cto)
        self.assertIn("never ends with a question about a step this directive already assigns to the CTO", self.mirror)
        self.assertIn("do it and report `done: <what>`", self.mirror)
        self.assertIn('"Want me to do that now?" is a defect', self.mirror)

    def test_cto_directive_fits_the_gemini_rules_budget(self):
        directive = (ROOT / "directives/CTO.md").read_text(encoding="utf-8")
        size = len(directive.encode("utf-8")) + len(claude_inbox.AUTHORITY_NOTE.encode("utf-8")) + 2
        self.assertLessEqual(size, gemini_runtime.RULES_LIMIT_BYTES, f"CTO.md payload {size} bytes exceeds the Gemini rules budget")

    def test_launcher_loads_the_cto_directive_whole(self):
        launcher = (ROOT / "scripts/run_managed_agent.sh").read_text()
        self.assertIn('directive="$(<"$harness_root/directives/CTO.md")"', launcher)


if __name__ == "__main__":
    unittest.main()

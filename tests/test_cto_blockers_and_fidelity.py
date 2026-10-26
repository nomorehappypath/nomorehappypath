"""2026-10-14: a project's CTO lost two hours. It repeated an agent's "blocked" fourteen cycles running,
pinned the owner a browser permission that was the agent's own tool, left the deliverables the agent
could already produce untouched, reported a slide count as progress and "watched one more cycle" on a
stalled Delivery. Every project's CTO is launched with directives/CTO.md (scripts/run_managed_agent.sh);
these pin the rules that forbid each of those, there and in the completion mirror.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def flat(path):
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


class CtoBlockersAndFidelity(unittest.TestCase):
    def setUp(self):
        self.cto = flat("directives/CTO.md")
        self.mirror = flat("harness/directives/CTO_COMPLETION_DIRECTIVE.md")

    def test_launcher_loads_the_cto_card(self):
        # Directive diet: the CTO starts with its one-page card; CTO.md is the reference opened by chapter.
        launcher = (ROOT / "scripts/run_managed_agent.sh").read_text()
        self.assertIn('instruction_file="directives/cards/CTO.md"', launcher)

    def test_reported_blocker_is_material_only_while_no_authorized_route_exists(self):
        for text in (self.cto, self.mirror):
            self.assertIn("A blocker an agent reports is material only while it blocks a required deliverable", text)
            self.assertIn("no authorized, feasible route", text)
        self.assertIn("Security, independent review, candidate integrity and sandbox isolation are never routed around", self.cto)

    def test_blocker_is_triaged_before_the_owner_is_asked(self):
        self.assertIn("establish whose blocker it is before anything else is said about it", self.cto)
        self.assertIn("the agent's own tool or sandbox", self.cto)
        self.assertIn("instead of taking the agent's account", self.cto)
        self.assertIn("provably the owner's to fix", self.cto)
        self.assertIn("never for an agent's own tool", self.cto)
        # the standing owner-only steps are kept, and a wrong card is cleared, not left pinned
        self.assertIn("sign-in and token creation", self.cto)
        self.assertNotIn('"Run a screen check yourself" card', self.cto, "that prompt no longer exists (2026-10-25)")
        self.assertIn("clear it with `owner-action-done`", self.cto)
        self.assertIn("never for an agent's own tool", self.mirror)

    def test_work_is_routed_around_the_blocker_in_the_same_cycle(self):
        self.assertIn("Route around, in the same cycle", self.cto)
        self.assertIn("deliverables that do not need the blocked step", self.cto)
        self.assertIn("even while the cause is still unknown", self.cto)
        self.assertIn("what they establish and what stays unverified", self.cto)
        self.assertIn("deliverables that do not need the blocked step", self.mirror)

    def test_attempts_are_bounded_and_end_in_a_disposition(self):
        self.assertIn("At most two routed repair attempts per cause", self.cto)
        self.assertIn("observable board result", self.cto)
        self.assertIn("recorded as not deliverable with its cause", self.cto)
        self.assertIn('owner-action-done --outcome "superseded:', self.cto)
        self.assertIn("At most two routed repair attempts per cause", self.mirror)

    def test_repeating_still_blocked_is_named_a_defect(self):
        self.assertIn("Every status line names what changed since the last cycle or what you did about it", self.cto)
        self.assertIn('"Still blocked", posted again with nothing done, is a defect of the CTO', self.cto)
        self.assertIn("third consecutive cycle", self.cto)
        self.assertIn('"still blocked" repeated is a defect', self.mirror)

    def test_every_cycle_checks_the_work_against_the_owner_objective(self):
        self.assertIn("Objective fidelity, every cycle", self.cto)
        self.assertIn("owner_direction_received", self.cto)
        self.assertIn("does this serve a required deliverable or acceptance proof?", self.cto)
        self.assertIn("a format or a permission detail the contract does not require is not progress", self.cto)
        self.assertIn("`impacts_current_task` that would not change what the owner receives is treated as non-blocking", self.cto)
        self.assertIn("never author or replace its review", self.cto)
        self.assertIn("the contract does not require is not progress", self.mirror)

    def test_stalled_means_routed_not_watched(self):
        self.assertIn("Stalled means routed", self.cto)
        self.assertIn('"Watching one more cycle" is a defect', self.cto)
        self.assertIn("Terminal silence alone is not a stall", self.cto)
        self.assertIn('"watching one more cycle" is a defect', self.mirror)

    def test_release_materiality_is_unchanged(self):
        self.assertIn("A CTO hold is an exceptional release-safety action", self.cto)
        self.assertIn("Never open a hold on a procedural row", self.cto)
        self.assertIn("Cosmetic defects, punctuation, formatting, and unrelated hygiene never justify a CTO hold", self.cto)


if __name__ == "__main__":
    unittest.main()

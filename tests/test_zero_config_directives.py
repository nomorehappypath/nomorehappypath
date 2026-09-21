# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ZeroConfigDirectiveTests(unittest.TestCase):
    def test_delivery_and_reviewer_modes_need_no_owner_setup(self):
        text = " ".join((ROOT / "directives" / "AGENT.md").read_text().split())
        for phrase in ("multi-hat Development Engineer", "Independent Reviewer", "Never ask the owner to fill a profile", "Each poll must be one bounded command", "Never run `while`, `for`, `watch`", "supervisor owns wake-up delivery", "Never post `OFFLINE` merely because no owner direction", "healthy standby", "bind-repository", "exact repository automatically", "`final_acceptance`", "`subtask_acceptance`", "Never ask the owner to classify", "USER ACTION: None", "A completed contract is not a released task", "never ask the owner to prompt it", "Never create a chunk merely to satisfy process"):
            self.assertIn(phrase, text)

    def test_cto_is_global_and_requires_chunk_and_final_gates(self):
        text = " ".join((ROOT / "directives" / "CTO.md").read_text().split())
        for phrase in ("whole project", "not a task-specific worker", "controller automatically wakes", "atomic task has no artificial chunks", "chunked task requires an independent cycle per chunk", "integrated application receives a separate final acceptance", "task workspace to resolve to the Git repository", "Pre-existing dirty files are inherited technical work", "Do not post an owner hold", "USER ACTION: None", "a heartbeat-only poll is not an action", "--record-ready", "CTO: release checks", "explicit reviewed file manifest", "git diff --cached --name-only", "git add -A"):
            self.assertIn(phrase, text)
        self.assertIn("omit `--health-command`", text)
        self.assertIn("never rerun an identical certified suite", text)

    def test_cto_blocks_only_material_release_issues(self):
        primary = " ".join((ROOT / "directives" / "CTO.md").read_text().split())
        completion = " ".join((ROOT / "harness" / "directives" / "CTO_COMPLETION_DIRECTIVE.md").read_text().split())
        for text in (primary, completion):
            self.assertIn("Materiality", text)
            self.assertIn("executable evidence", text)
            self.assertIn("material impact", text)
            self.assertIn("non-blocking", text)
            self.assertIn("unrelated dirty", text)
            self.assertIn("immutable certified copy", text)
        self.assertIn("A misplaced comma", primary)
        self.assertIn("Never keep a historical or superseded hold open", primary)

    def test_directives_require_proving_a_fix_under_the_condition_that_breaks_it(self):
        agent = " ".join((ROOT / "directives" / "AGENT.md").read_text().split())
        spawn = " ".join((ROOT / "harness/directives/00_SPAWN_DEVELOPMENT_DIRECTIVE.md").read_text().split())
        for text in (agent.lower(), spawn.lower()):
            self.assertIn("a path outside the approved workspace", text)
            self.assertIn("a different operating system", text)
            self.assertIn("infer another agent's or another platform's behavior from its transcript", text)
            self.assertIn("stays unproven", text)
        self.assertIn("A green result from a place the failure cannot occur is not evidence", agent)
        self.assertIn("belongs in the repository as a script or a test", spawn)

    def test_directives_protect_behavior_a_reviewer_already_accepted(self):
        agent = " ".join((ROOT / "directives" / "AGENT.md").read_text().split())
        spawn = " ".join((ROOT / "harness/directives/00_SPAWN_DEVELOPMENT_DIRECTIVE.md").read_text().split())
        for text in (agent.lower(), spawn.lower()):
            self.assertIn("already accepted", text)
            self.assertIn("non-blocking", text)
            self.assertIn("out of scope", text)
            self.assertIn("new claim", text)
            self.assertIn("an environment you have not reproduced", text)
        self.assertIn("A refusal that fires when it should is the product working", agent)

    def test_directives_admit_only_material_scenarios_and_cap_the_challenge_ledger(self):
        """2026-09-21: QA drifted into git bookkeeping, chunk boundaries and fonts.

        The materiality rule used to start at the verdict; it now starts when a
        row is written, for every role, in the owner's own terms.
        """
        agent = " ".join((ROOT / "directives" / "AGENT.md").read_text().split())
        cto = " ".join((ROOT / "directives" / "CTO.md").read_text().split())
        spawn = " ".join((ROOT / "harness/directives/00_SPAWN_DEVELOPMENT_DIRECTIVE.md").read_text().split())
        reviewer = " ".join((ROOT / "harness/directives/AUTONOMOUS_COMPLETION_DIRECTIVE.md").read_text().split())
        completion = " ".join((ROOT / "harness/directives/CTO_COMPLETION_DIRECTIVE.md").read_text().split())
        template_path = ROOT / "validated_v0.2/REVIEWER_CHALLENGE_LEDGER_TEMPLATE.md"
        # validated_v0.2/ is private material the public cut does not ship; the
        # five directives above are shipped and stay pinned unconditionally.
        template = " ".join(template_path.read_text().split()) if template_path.is_file() else None
        for text in (agent, spawn, reviewer, cto, completion):
            self.assertIn("would change what the owner receives", text)
            self.assertIn("acceptance criterion", text)
            self.assertIn("commit-identity bookkeeping", text)
            self.assertIn("chunk-boundary", text)
            self.assertIn("source-structure assertions", text)
            self.assertIn("dead code", text)
            self.assertIn("real pipeline path", text)
        for text in (agent, reviewer, spawn):
            self.assertIn("never more than twelve", text)
        self.assertIn("at most two rows per acceptance criterion", reviewer)
        self.assertIn("hands back no result", agent)
        self.assertIn("hands back no result", reviewer)
        self.assertIn("not a licence to test how it looks", agent)
        for text in (cto, completion):
            self.assertIn("Never open a hold on a procedural row", text)
            self.assertIn("never let a procedural FAIL block a release", text)
        self.assertIn("drift, not diligence", cto)
        if template is None:
            self.skipTest("validated_v0.2/ is not part of this tree (public cut excludes it); directives pinned above")
        self.assertIn("never more than twelve", template)
        self.assertIn("| ID | Criterion tested |", template)

    def test_runtime_directives_cannot_recreate_the_removed_findings_queue(self):
        agent = " ".join((ROOT / "directives" / "AGENT.md").read_text().split())
        cto = " ".join((ROOT / "directives" / "CTO.md").read_text().split())
        spawn = " ".join((ROOT / "harness/directives/00_SPAWN_DEVELOPMENT_DIRECTIVE.md").read_text().split())
        completion = " ".join((ROOT / "harness/directives/CTO_COMPLETION_DIRECTIVE.md").read_text().split())
        launcher = (ROOT / "scripts/run_managed_agent.sh").read_text()
        for text in (agent, cto, spawn, completion):
            self.assertNotIn("record a durable deferred finding", text)
            self.assertNotIn("Fix` or `Do not fix", text)
            self.assertNotIn("routed as follow-up work", text)
            self.assertNotIn("must be repaired or recorded", text)
            self.assertIn("review summary only", text)
        self.assertIn('$harness_root/directives/AGENT.md', launcher)
        self.assertIn('$harness_root/directives/CTO.md', launcher)
        self.assertIn("repair_authoring", agent)
        self.assertIn("never reuses a PASS", agent)


if __name__ == "__main__":
    unittest.main()

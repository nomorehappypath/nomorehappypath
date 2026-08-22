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

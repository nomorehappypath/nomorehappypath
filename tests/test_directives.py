# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CompletionDirectiveTests(unittest.TestCase):
    def test_spawn_directive_requires_the_full_completion_contract(self):
        text = (ROOT / "engine/directives/00_AGENT_SPAWN_DIRECTIVE.md").read_text()
        for required in (
            "User objective", "Required deliverables", "Acceptance proof", "Exclusions",
            "Current status", "Remaining work", "claim-scope audit", "VISUAL_TEST_REQUIRED",
            "OBJECTIVE STATUS: COMPLETE | PARTIAL | BLOCKED",
        ):
            self.assertIn(required, text)

    def test_cto_directive_blocks_premature_owner_handoff(self):
        text = (ROOT / "engine/directives/CTO_WATCHTOWER_DIRECTIVE.md").read_text()
        for required in (
            "Completion Contract", "claim-scope audit", "`PARTIAL` is never",
            "VISUAL_TEST_REQUIRED", "clean `main` checkout",
        ):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()

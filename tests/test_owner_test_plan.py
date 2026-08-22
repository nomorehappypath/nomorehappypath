# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Adversarial release-checklist persistence and rendering tests."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, board_viewer, contract, control


class OwnerTestPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        control.initialize(self.root)
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")

    def release(self, task):
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        return board.record_release_ready(
            self.root, self.cto["id"], task, {**checks, "head_commit": "a" * 40},
        )

    def test_release_persists_bounded_contract_derived_owner_steps(self):
        names = ["Open the project chat <img src=x onerror=alert(1)>"] + [
            f"Exercise workflow {index}" for index in range(1, 10)
        ]
        contract.create_contract(self.root, "CHECKLIST", "Ship the owner workflow.", names)
        release = self.release("CHECKLIST")
        self.assertEqual(len(release["owner_test_steps"]), 8)
        self.assertEqual(
            release["owner_test_steps"][0],
            "Verify Open the project chat <img src=x onerror=alert(1)>.",
        )
        self.assertEqual(
            board.snapshot(self.root)["releases"]["CHECKLIST"]["owner_test_steps"],
            release["owner_test_steps"],
        )

    def test_missing_or_malformed_contract_still_gets_an_explicit_step(self):
        (self.root / ".harness" / "tasks").mkdir(parents=True, exist_ok=True)
        (self.root / ".harness" / "tasks" / "LEGACY.json").write_text("{broken")
        release = self.release("LEGACY")
        self.assertEqual(release["owner_test_steps"], [
            "Verify the released result against the final agreed requirements shown above.",
        ])

    def test_rendered_checklist_escapes_hostile_text_and_precedes_acceptance(self):
        page = board_viewer.rendered_page()
        script = page.split("<script>", 1)[1].split("</script>", 1)[0].split(
            "el('#status-dialog-close')", 1,
        )[0]
        state = {"releases": {"TASK": {
            "status": "VISUAL_TEST_REQUIRED",
            "owner_test_steps": ["Verify <img src=x onerror=alert(1)> now."],
        }}}
        invocation = """
globalThis.document={querySelector(){return null;}};
process.stdout.write(releaseResponseHtml(%s,'TASK'));
""" % json.dumps(state)
        completed = subprocess.run(
            ["node", "-e", script + "\n" + invocation], capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("What to test before accepting", completed.stdout)
        self.assertIn("&lt;img", completed.stdout)
        self.assertNotIn("<img", completed.stdout)
        self.assertLess(completed.stdout.index("What to test"), completed.stdout.index("Accepted"))


if __name__ == "__main__":
    unittest.main()

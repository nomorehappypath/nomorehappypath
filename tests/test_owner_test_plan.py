# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
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

    def test_the_owner_is_never_told_to_verify_the_agents_own_tests(self):
        contract.create_contract(self.root, "NOTESTS", "Ship it.", ["Weather page shows five days", "Meaningful automated tests", "Unit tests pass"])
        release = self.release("NOTESTS")
        self.assertEqual(release["owner_test_steps"], ["Verify Weather page shows five days."])
        contract.create_contract(self.root, "ONLYTESTS", "Ship it.", ["Meaningful automated tests"])
        self.assertEqual(self.release("ONLYTESTS")["owner_test_steps"], ["Verify the released result against the final agreed requirements shown above."])

    def test_an_older_release_that_already_stored_a_test_step_does_not_show_it(self):
        script = board_viewer.rendered_page().split("<script>", 1)[1].split("</script>", 1)[0].split("el('#status-dialog-close')", 1)[0]
        state = {"releases": {"TASK": {"status": "VISUAL_TEST_REQUIRED",
                                       "owner_test_steps": ["Verify Meaningful automated tests.", "Verify Open the settings menu."]}}}
        invocation = "globalThis.document={querySelector(){return null;}};\nprocess.stdout.write(releaseResponseHtml(%s,'TASK'));\n" % json.dumps(state)
        completed = subprocess.run(["node", "--input-type=commonjs", "-"], input=script + "\n" + invocation, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Open the settings menu", completed.stdout)
        self.assertNotIn("automated tests", completed.stdout)

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
            ["node", "--input-type=commonjs", "-"], input=script + "\n" + invocation, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("What to test before accepting", completed.stdout)
        self.assertIn("&lt;img", completed.stdout)
        self.assertNotIn("<img", completed.stdout)
        self.assertLess(completed.stdout.index("What to test"), completed.stdout.index("Accepted"))


from tests.release_fixture_support import allow_fixture_releases  # noqa: E402

setUpModule, tearDownModule = allow_fixture_releases()   # bare release fixtures: production demands a final review (2026-10-24)


if __name__ == "__main__":
    unittest.main()

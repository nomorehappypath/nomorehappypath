# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Every fresh launch, restart and resume of an agent carries the test machine's connection and the current rule.

The real runner script runs against fake Claude and Codex programs that record their arguments (tests/test_stage4_system_layer.py).
A project that never used a machine gets nothing extra. Gemini shares the same prompt text and is covered by reading, not by a live run.

Run:  PYTHONPATH=. python3 -m unittest tests.test_test_machine_launch -v
"""
from __future__ import annotations

import unittest

from harness import conversation, control, test_machine
from tests.test_conversation_memory import stage_relaunch
from tests import test_stage4_system_layer as stage4
from tests.test_test_machine import connected_descriptor


class MachineLaunchTests(unittest.TestCase):
    # the Stage-4 fixture's plumbing, without its own test methods
    served = stage4.Stage4RunnerTests.served
    bootstrap_served = stage4.Stage4RunnerTests.bootstrap_served
    setUp = stage4.Stage4RunnerTests.setUp
    fake = stage4.Stage4RunnerTests.fake
    switch = stage4.Stage4RunnerTests.switch
    launch = stage4.Stage4RunnerTests.launch
    require_confinement = stage4.Stage4RunnerTests.require_confinement

    def test_claude_fresh_and_resumed_launches_get_the_machine_rule_and_other_projects_get_nothing(self):
        self.require_confinement()
        self.switch("claude", stage4=True)
        quiet = self.launch(control.create(self.context, "claude_reviewer"), "claude_reviewer")
        self.assertNotIn("[test-machine]", quiet[-1], "a project that never used a machine sees nothing")
        connected_descriptor(self.context)
        session = control.create(self.context, "claude_reviewer")
        fresh = self.launch(session, "claude_reviewer")
        self.assertIn("[test-machine]", fresh[-1])
        self.assertIn("machine-status", fresh[-1])
        self.assertIn("revision 1", fresh[-1])
        self.assertIn("Test machine rule (revision 2)", fresh[-1])
        self.assertIn("<your lock folder>-v2", fresh[-1])
        minted = fresh[fresh.index("--session-id") + 1]
        store = self.claude_dir / "projects" / "-code"
        store.mkdir(parents=True, exist_ok=True)
        (store / f"{minted}.jsonl").write_text("{}\n", encoding="utf-8")
        stage_relaunch(self.context, session["id"])
        resumed = self.launch(session, "claude_reviewer")
        self.assertIn("--resume", resumed)
        self.assertIn(conversation.RECOVERY_LABEL, resumed[-1], "the ordinary recovery text is still there")
        self.assertIn("[test-machine]", resumed[-1], "a resumed session with an old conversation gets the current rule")
        self.assertIn("machine-baseline --begin", resumed[-1])
        self.assertIn("Test machine rule (revision 2)", resumed[-1], "a resumed conversation gets the revised rule, not the one it started with")
        self.assertIn("your first machine row is Delivery's certified command text", resumed[-1])

    def test_the_cto_launch_gets_the_revised_rule_too(self):
        self.require_confinement()
        self.switch("claude", stage4=True)
        connected_descriptor(self.context)
        argv = self.launch(control.create(self.context, "claude_cto"), "claude_cto")
        self.assertIn("Test machine rule (revision 2)", argv[-1])
        self.assertIn("the CTO asks the owner once to approve a restart", argv[-1])

    def test_codex_launch_gets_the_machine_rule_in_its_first_message(self):
        self.switch("codex", stage4=True)
        connected_descriptor(self.context)
        argv = self.launch(control.create(self.context, "codex_delivery"), "codex_delivery")
        self.assertIn("[test-machine]", argv[-1])
        self.assertIn("need-test-machine", argv[-1])
        self.assertIn("Test machine rule (revision 2)", argv[-1])
        self.assertIn("never create accounts, services, tasks or registry entries", argv[-1])

    def test_the_note_follows_a_change_of_state(self):
        connected_descriptor(self.context)
        test_machine.mark_reauth(self.context, "project_stopped")
        note = test_machine.launch_note(self.context)
        self.assertIn("reauth_required", note)


if __name__ == "__main__":
    unittest.main()

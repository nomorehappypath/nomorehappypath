# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Open-list item C: the two notices (same computer as another project's; a test running at reconnect).

Run:  PYTHONPATH=. python3 -m unittest tests.test_machine_followups_two -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, machine_notices, project_manager, project_registry as registry, test_machine
from tests.test_test_machine import connected_descriptor


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.contexts = {}
        for name in ("alpha", "beta"):
            code = Path(self.tmp.name) / name
            code.mkdir()
            entry = registry.register(self.home, name, code)
            self.contexts[name] = (entry, registry.context_for_entry(entry))

    def test_the_same_host_key_at_another_address_warns_and_names_the_other_project(self):
        beta_entry, beta = self.contexts["beta"]
        connected_descriptor(beta, address="10.9.9.9", user="WindowsVM", host_key={"type": "ssh-ed25519", "fingerprint": "SHA256:same", "line": "x"})
        alpha_entry = self.contexts["alpha"][0]
        text = machine_notices.same_computer_warning(self.home, alpha_entry["id"], "192.168.64.5", 22, "SHA256:same")
        self.assertIn("same computer as WindowsVM@10.9.9.9 that beta is using", text)
        self.assertIn("Connecting continues", text)
        # nothing to say: a different key, the same address (the reservation's business), or the project's own record
        self.assertEqual(machine_notices.same_computer_warning(self.home, alpha_entry["id"], "192.168.64.5", 22, "SHA256:other"), "")
        self.assertEqual(machine_notices.same_computer_warning(self.home, alpha_entry["id"], "10.9.9.9", 22, "SHA256:same"), "")
        self.assertEqual(machine_notices.same_computer_warning(self.home, beta_entry["id"], "192.168.64.5", 22, "SHA256:same"), "")

    def test_a_running_machine_test_is_named_and_a_finished_or_local_one_is_not(self):
        _entry, alpha = self.contexts["alpha"]
        self.assertEqual(machine_notices.running_test_notice(alpha), "")
        agent = board.register(alpha, "engineering", board.AWAITING_OWNER_DIRECTION, "Delivery", "OpenAI", "s-1")
        board.review_execution_start(alpha, agent["id"], "r1", "challenge execution: /tmp/ledger.json")   # the Reviewer's real marker names its ledger
        self.assertIn("A test may be running on this machine right now", machine_notices.running_test_notice(alpha))
        board.review_execution_finish(alpha, agent["id"], "r1")
        self.assertEqual(machine_notices.running_test_notice(alpha), "")

    def test_connect_refuses_without_confirm_when_a_test_started_after_the_scan(self):
        entry, alpha = self.contexts["alpha"]
        manager = project_manager.ProjectManager(self.home, board_port=0)
        connected_descriptor(alpha, identity=manager.machine_identity)
        agent = board.register(alpha, "engineering", board.AWAITING_OWNER_DIRECTION, "Delivery", "OpenAI", "s-1")
        board.review_execution_start(alpha, agent["id"], "r1", "challenge execution: /tmp/ledger.json")
        found = {"type": "ssh-ed25519", "fingerprint": "SHA256:abc", "line": "x"}
        challenge = manager.machine_challenges.issue(entry["id"], "127.0.0.1", "u", 22, found, False, False)
        with self.assertRaises(test_machine.MachineError) as raised:
            manager.machine_connect(entry["id"], {"challenge": challenge["id"], "password": "Pw-1234-abcd"}, loopback=True)
        self.assertEqual(raised.exception.code, "test_running")
        self.assertIn("A test may be running", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

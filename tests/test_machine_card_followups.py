# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Test machine card's diagnosis says what it observed (windows-walk item 8).

Machine inputs removed: no ssh runs; the key check is patched to return what a real failure of each kind returns.

Run:  PYTHONPATH=. python3 -m unittest tests.test_machine_card_followups -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from harness import test_machine

SAVED = {"connected": True, "address": "10.0.0.9", "port": 22, "user": "u", "host_key": {"fingerprint": "SHA256:f"}, "reauth_required": True}


def diagnose_with(code, message):
    with patch.object(test_machine, "read", return_value=SAVED), \
            patch.object(test_machine, "scan_host_key", return_value={"fingerprint": "SHA256:f"}), \
            patch.object(test_machine, "verify_key_login_detail", return_value=(False, code, message)), \
            patch.object(test_machine, "mark_reauth"):
        return test_machine.diagnose(None)


class DiagnosisTests(unittest.TestCase):
    def test_every_observed_failure_keeps_its_own_sentence_and_next_step(self):
        cases = {
            "unreachable": test_machine.classify("timed out", address="10.0.0.9", user="u", port=22, stage="key", timed_out=True),
            "ssh_server_down": test_machine.classify("connection refused", address="10.0.0.9", user="u", port=22, stage="key"),
            "address_wrong": test_machine.classify("could not resolve hostname vm", address="vm", user="u", port=22, stage="key"),
            "failed": test_machine.classify("weird output from a local ssh problem", address="10.0.0.9", user="u", port=22, stage="key"),
        }
        for code, error in cases.items():
            with self.subTest(code=code):
                diagnosis = diagnose_with(error.code, str(error))
                self.assertEqual(diagnosis["cause"], error.code)
                self.assertEqual(diagnosis["message"], str(error))
                self.assertNotIn("The key check failed", diagnosis["message"])

    def test_a_missing_message_is_still_a_plain_instruction(self):
        diagnosis = diagnose_with("failed", "")
        self.assertIn("check that it is running, then press Check again", diagnosis["message"])
        self.assertNotEqual(diagnosis["message"], "The key check failed.")

    def test_the_refused_key_and_the_changed_identity_keep_their_messages(self):
        self.assertIn("enter the password to install it again", diagnose_with("key_refused", "x")["message"])
        self.assertEqual(diagnose_with("host_key_changed", "Host key verification failed.")["cause"], "host_key_unverified")

    def test_an_unclassified_failure_never_invents_a_password_requirement_but_a_refused_key_does(self):
        saved = dict(SAVED, reauth_required=False)
        for code, expected_calls in (("failed", 0), ("unreachable", 0), ("key_refused", 1)):
            with self.subTest(code=code), patch.object(test_machine, "read", return_value=saved), \
                    patch.object(test_machine, "scan_host_key", return_value={"fingerprint": "SHA256:f"}), \
                    patch.object(test_machine, "verify_key_login_detail", return_value=(False, code, "observed")), \
                    patch.object(test_machine, "mark_reauth") as marked:
                test_machine.diagnose(None)
            self.assertEqual(marked.call_count, expected_calls)

    def test_a_missing_local_ssh_program_and_a_generic_failure_each_carry_a_next_step(self):
        for text in (test_machine.LOCAL_SSH_MISSING_TEXT, test_machine.LOCAL_KEYSCAN_MISSING_TEXT, test_machine.LOCAL_KEYGEN_MISSING_TEXT):
            self.assertIn("Install the OpenSSH client on this computer", text)
            self.assertIn("openssh-client", text)
        generic = str(test_machine.classify("something odd", address="10.0.0.9", user="u", port=22, stage="key"))
        self.assertIn("Check that the machine is running", generic)
        self.assertIn("run ssh u@10.0.0.9 to see the message in full", generic)

    def test_the_two_value_wrapper_still_answers_old_callers(self):
        with patch.object(test_machine, "verify_key_login_detail", return_value=(False, "unreachable", "Nothing answered")):
            self.assertEqual(test_machine.verify_key_login(None), (False, "unreachable"))


if __name__ == "__main__":
    unittest.main()

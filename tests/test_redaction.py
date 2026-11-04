# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
import unittest

from harness import redaction


class RedactTests(unittest.TestCase):
    def test_a_token_embedded_in_a_git_remote_url_is_stripped(self):
        # P3: git_broker.inspect_push_destination/remote_push store this shape verbatim.
        # assembled from pieces so this file never contains a literal token shape (the public release scan refuses those)
        token = "ghp_" + "abcdefghijklmnopqrstuvwxyz012345"
        url = f"https://x-access-token:{token}@github.com/owner/repo.git"
        cleaned = redaction.redact(url)
        self.assertNotIn("x-access-token", cleaned)
        self.assertNotIn(token, cleaned)
        self.assertIn("github.com/owner/repo.git", cleaned)

    def test_a_local_path_with_no_userinfo_is_left_alone(self):
        self.assertEqual(redaction.redact("/Users/owner/code/remote.git"), "/Users/owner/code/remote.git")

    def test_an_scp_style_ssh_remote_with_no_credential_is_left_alone(self):
        self.assertEqual(redaction.redact("git@github.com:owner/repo.git"), "git@github.com:owner/repo.git")

    def test_a_labelled_secret_in_captured_command_output_is_stripped(self):
        cleaned = redaction.redact("remote: Invalid credentials. Authorization: Bearer abcdefgh12345678")
        self.assertNotIn("abcdefgh12345678", cleaned)

    def test_redacting_before_truncating_removes_a_token_a_truncate_first_order_would_only_cut_in_half(self):
        # P3 repair: git_broker.remote_push used to truncate captured push output to its last 2000
        # characters BEFORE redacting it. A GitHub token straddling that cut point loses its "ghp_"
        # prefix to the truncation, so what remains (letters only, no prefix, no digit) matches no
        # secret shape at all and is left sitting in board state in plain text.
        fragment = "a" * 20
        token = "ghp_" + fragment
        text = ("-" * 1980) + token + ("-" * 1980)   # the cut lands 4 chars into the token
        truncate_then_redact = redaction.redact(text[-2000:])
        redact_then_truncate = redaction.redact(text)[-2000:]
        self.assertIn(fragment, truncate_then_redact, "the scenario itself must actually split the token")
        self.assertNotIn(fragment, redact_then_truncate)

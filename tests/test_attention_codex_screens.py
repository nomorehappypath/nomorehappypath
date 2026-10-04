# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The screen watch reads Codex 0.160's screens for what they are.

Owner's resume test, 2026-10-03 (temp2): the Delivery card said the agent was
logged out ("run /login") while it was working. Two misreadings, both
measured on the real binary:
- the agent's own board call had failed authentication around the pause
  ("error: session authentication failed", then the agent's words "board
  authentication error"); the sign-in pattern took the harness's own board
  error for the CLI being logged out, and flapped as the line scrolled;
- Codex's folder question ("Trust this folder? ... Trust and continue") was
  not recognised at all, so input was not held for it (F-8).
"""
from __future__ import annotations

import unittest

from harness import attention

# Captured from the real `codex --remote` TUI (0.160.0) on a new folder, headless.
CODEX_TRUST_SCREEN = """\
  Folder access
  /Users/owner/Projects/test/temp2
  Trust this folder? Codex can read, edit, and run files here, subject to your permission settings. Folder settings
  can run code automatically, even without a model request. Continue only if you trust these files. Your trust
  decision will be saved.
› 1. Trust and continue
  2. Back to Agent Command Center
  enter continue · esc back
"""

# From the owner's session transcript (codex_delivery-d0048f8405), as rendered.
AGENT_BOARD_ERROR_SCREEN = """\
• Failed (exit 2) /Applications/Xcode.app/Contents/Developer/usr/bin/python3 -E /Users/owner/harness/board.py ...
  └ error: session authentication failed
    + Show details
• I'm resuming from the saved task state. The contract creation command had failed with a board authentication error, so
  I'll first poll the board for the current next action and then continue from its recorded state.
• Working (5s • esc to interrupt)
"""


class CodexScreenTests(unittest.TestCase):
    def test_codex_asking_to_trust_a_folder_is_a_trust_question_that_holds_input(self):
        reason = attention.detect(CODEX_TRUST_SCREEN)
        self.assertEqual(reason, attention.CODEX_TRUST_REASON)
        self.assertIn("Trust and continue", reason)
        self.assertTrue(attention.holds_harness_input(reason), "F-8: nothing is typed into the owner's question")
        self.assertFalse(attention.needs_sign_in({"attention_reason": reason}))

    def test_the_harness_own_board_error_is_never_a_sign_in(self):
        self.assertIsNone(attention.detect(AGENT_BOARD_ERROR_SCREEN))
        self.assertFalse(attention.needs_sign_in({"attention_reason": attention.detect(AGENT_BOARD_ERROR_SCREEN) or ""}))

    def test_a_watched_screen_does_not_flap_on_the_board_error(self):
        watch = attention.PromptWatch(height=40)
        changes = [watch.feed(AGENT_BOARD_ERROR_SCREEN.replace("\n", "\r\n").encode("utf-8"))]
        changes.append(watch.feed(b"\x1b[2J\x1b[H\xe2\x80\xa2 Working (9s \xe2\x80\xa2 esc to interrupt)\r\n"))
        self.assertEqual([change for change in changes if change], [], "no waiting/cleared transitions")

    def test_a_real_cli_sign_in_is_still_a_sign_in(self):
        for screen in ("Please run /login to continue", "Not logged in · Please run /login", "API Error: 401",
                       "OAuth token has expired", "authentication failed. Your access token has been revoked",
                       "OAuth authentication failed"):
            self.assertEqual(attention.detect(screen), attention.LOGIN_REASON, screen)


if __name__ == "__main__":
    unittest.main()

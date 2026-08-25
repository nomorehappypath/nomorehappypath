# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The suite may never write the owner's real provider configuration.

`tests/__init__` pins CODEX_HOME so a test cannot reach `~/.codex`. That pin is
a mechanism; this is the assertion. It exists because the mechanism was added
after an incident - a hundred trust entries written into the owner's real
config across three suite runs - and because a reviewer can only observe the
file's hash on a live machine, where the owner's own Codex CLI is also running
and legitimately writes it. A hash is therefore ambiguous evidence. The
signature of a leaking suite is not, and this asserts on the signature: no
entry naming a temporary directory may ever appear in the real file.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# Only paths a test could have produced. Real project directories - including
# the owner's task and review clones, which the owner's own CLI records
# legitimately - must never be flagged here.
TEST_ONLY_MARKERS = ("harness-tests-codex-home-", "/var/folders/", "/private/var/folders/")


class HermeticityTests(unittest.TestCase):
    def test_codex_home_is_pinned_away_from_the_real_home(self):
        pinned = os.environ.get("CODEX_HOME", "")
        self.assertTrue(pinned, "CODEX_HOME is not pinned; tests/__init__ did not run")
        self.assertTrue(
            Path(pinned).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()),
            f"CODEX_HOME must point inside the temporary directory, not {pinned!r}",
        )
        self.assertFalse(
            Path(pinned).resolve().is_relative_to(Path.home().resolve()),
            "CODEX_HOME points inside the owner's real home",
        )

    def test_the_real_codex_config_carries_no_test_written_entry(self):
        """The invariant the 2026-08-21 incident violated, stated as a test."""
        real = Path(os.path.expanduser("~/.codex/config.toml"))
        if not real.is_file():
            self.skipTest("no real codex configuration on this machine")
        try:
            content = real.read_text(encoding="utf-8", errors="replace")
        except OSError as error:            # unreadable is not evidence of a leak
            self.skipTest(f"real codex configuration unreadable: {error}")
        leaked = sorted({
            line.strip() for line in content.splitlines()
            if any(marker in line for marker in TEST_ONLY_MARKERS)
        })
        self.assertEqual(
            leaked, [],
            "the suite wrote temporary-directory entries into the owner's real "
            "codex configuration: " + "; ".join(leaked),
        )


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Project chat's model choice, verifiable in ANY environment - no sockets."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import global_settings
from tests.chat_key_support import configure_verified_key


class ChatModelDefaultTests(unittest.TestCase):
    def test_default_chat_model_is_the_economy_tier_at_low_effort(self):
        with tempfile.TemporaryDirectory() as home:
            setting = global_settings.chat_settings(
                Path(home), source_environment={},
            )
            self.assertEqual(setting["model"], "gpt-5.6-luna")
            self.assertEqual(setting["effort"], "low")
            self.assertEqual(setting["provider"], "openai")

    def test_environment_override_still_selects_a_bigger_model(self):
        with tempfile.TemporaryDirectory() as home:
            setting = global_settings.chat_settings(
                Path(home),
                source_environment={"HARNESS_PROJECT_CHAT_MODEL": "gpt-5.6-terra"},
            )
            self.assertEqual(setting["model"], "gpt-5.6-terra")

    def test_verified_key_fixture_matches_the_default_model(self):
        with tempfile.TemporaryDirectory() as home:
            configure_verified_key(Path(home))
            recorded = global_settings.load(Path(home))["connectivity"]["openai"]
            self.assertEqual(recorded["model"], "gpt-5.6-luna")


if __name__ == "__main__":
    unittest.main()

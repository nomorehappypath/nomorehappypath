# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""README and Help explain the new Write-specs design (requirement R8). Matches the Reviewer's
own contract-check c8-k1 verbatim."""
from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from harness import project_manager
from tests.environment_support import require_loopback


class OwnerExplanationTests(unittest.TestCase):
    def setUp(self):
        require_loopback()

    def test_readme_and_help_explain_the_question_card_targets_and_follow_on_work(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as td:
            manager = project_manager.ProjectManager(Path(td), board_port=0)
            server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=8) as response:
                    help_page = response.read().decode("utf-8")
            finally:
                server.shutdown()
                server.server_close()
        for surface in (readme, help_page):
            for phrase in ("Write specs", "questions", "Targets", "Security", "Error handling"):
                with self.subTest(phrase=phrase):
                    self.assertIn(phrase, surface)
            self.assertIn("Write code", surface)


if __name__ == "__main__":
    unittest.main()

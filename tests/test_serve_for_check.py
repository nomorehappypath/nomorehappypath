# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""serve_for_check runs the app for one check in the foreground and always stops it (review R1-1, 2026-11-03)."""
from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import hook_rules, serve_for_check
from tests.environment_support import require_loopback

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "harness" / "serve_for_check.py"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ServeForCheckTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.folder = Path(tempfile.mkdtemp(prefix="serve-for-check-"))
        (self.folder / "index.html").write_text("<h1>Welcome home</h1>", encoding="utf-8")
        self.port = free_port()
        self.start = f"{sys.executable} -m http.server {self.port} --bind 127.0.0.1 --directory {self.folder}"

    def test_the_check_sees_the_running_app_and_the_app_is_gone_afterwards(self):
        result = subprocess.run(
            [sys.executable, str(HELPER), "--start", self.start, "--port", str(self.port), "--",
             sys.executable, "-c", f"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:{self.port}/').read().decode())"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Welcome home", result.stdout)
        self.assertFalse(serve_for_check.port_answers(self.port), "the app must not outlive the command")

    def test_the_check_exit_code_is_returned_and_the_app_still_stops(self):
        code = serve_for_check.run(self.start, self.port, [sys.executable, "-c", "raise SystemExit(3)"])
        self.assertEqual(code, 3)
        self.assertFalse(serve_for_check.port_answers(self.port))

    def test_an_app_that_dies_before_listening_is_reported_not_waited_for(self):
        code = serve_for_check.run(f"{sys.executable} -c \"raise SystemExit(1)\"", self.port, [sys.executable, "-c", "pass"])
        self.assertEqual(code, 2)

    def test_a_port_already_in_use_is_refused_so_another_server_is_never_captured(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", self.port))
            busy.listen()
            code = serve_for_check.run(self.start, self.port, [sys.executable, "-c", "pass"])
        self.assertEqual(code, 2)

    def test_the_hook_allows_the_one_foreground_command(self):
        command = (f"python3 /x/harness/serve_for_check.py --start 'python3 app.py' --port 8000 -- "
                   f"python3 /x/harness/board.py --root /p screen-check --agent a --url http://127.0.0.1:8000/ --expect 'Hi & welcome'")
        self.assertEqual(hook_rules.evaluate("engineering", "Bash", {"command": command}, project_roots=["/p"]), ("", ""))


class DirectiveNamesTheHelperTests(unittest.TestCase):
    def test_rendered_proof_and_the_delivery_card_name_the_helper(self):
        text = (ROOT / "directives" / "AGENT.md").read_text(encoding="utf-8")
        start = text.index("## rendered-proof")
        chapter = text[start:text.index("\n## ", start + 5)]
        self.assertIn("serve_for_check.py", chapter)
        self.assertIn("serve_for_check.py", (ROOT / "directives" / "cards" / "DELIVERY.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Reviewer's test copy of the app can start a real Claude, without the owner creating or pasting a token.

Owner, 2026-10-07: "it said ready for me, when the reviewer could not run claude to test", and "it is not logical that I
should do that [create a token]". Claude Code strips CLAUDE_CODE_OAUTH_TOKEN from every command it runs, so the Reviewer's
commands never had the token the app already holds. The managed Reviewer's process now also carries it under a
harness-owned name; resolve_token() accepts that name as a last resort. Only the Reviewer; evidence files that contain a
Claude token are refused; the public leak scan knows the Claude token shape.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from harness import board, claude_auth, control
from tests.environment_support import require_loopback
from tests.test_stage4_system_layer import Stage4RunnerTests

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "sk-ant-oat01-" + "A" * 70


class ResolveTests(unittest.TestCase):
    def test_the_reviewer_variable_is_a_last_resort_source(self):
        with mock.patch.object(claude_auth.platform_support.claude_credentials(), "read_token", return_value=""):
            self.assertEqual(claude_auth.resolve_token({claude_auth.REVIEWER_TOKEN_ENV: TOKEN}), TOKEN)
            with self.assertRaises(claude_auth.ClaudeAuthRequired):
                claude_auth.resolve_token({})

    def test_the_normal_source_still_wins(self):
        with mock.patch.object(claude_auth.platform_support.claude_credentials(), "read_token", return_value="normal-token"):
            self.assertEqual(claude_auth.resolve_token({claude_auth.REVIEWER_TOKEN_ENV: TOKEN}), "normal-token")


class WrapperTests(unittest.TestCase):
    def setUp(self):
        require_loopback()

    def run_wrapper(self, *flags, parent_env=None):
        directory = tempfile.mkdtemp(prefix="hn", dir="/tmp" if os.path.isdir("/tmp") else None)
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, True))
        path = os.path.join(directory, "s.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        self.addCleanup(listener.close)

        def serve():
            connection, _ = listener.accept()
            with connection:
                connection.recv(256)
                connection.sendall(json.dumps({claude_auth.TOKEN_ENV: TOKEN}).encode() + b"\n")
        threading.Thread(target=serve, daemon=True).start()
        probe = "import json,os; print(json.dumps({k: os.environ.get(k) for k in ('%s','%s')}))" % (
            claude_auth.TOKEN_ENV, claude_auth.REVIEWER_TOKEN_ENV)
        environment = {**os.environ, **(parent_env or {})}
        completed = subprocess.run([sys.executable, "-E", str(ROOT / "harness" / "claude_auth.py"), "--socket", path,
                                    "--session", "s1", *flags, "--", sys.executable, "-c", probe],
                                   env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_only_the_reviewer_flag_exposes_the_token_to_commands(self):
        plain = self.run_wrapper(parent_env={claude_auth.REVIEWER_TOKEN_ENV: "stale"})
        self.assertEqual(plain, {claude_auth.TOKEN_ENV: TOKEN, claude_auth.REVIEWER_TOKEN_ENV: None},
                         "no flag: the stale variable is removed and nothing extra is exposed")
        shared = self.run_wrapper("--share-with-commands")
        self.assertEqual(shared, {claude_auth.TOKEN_ENV: TOKEN, claude_auth.REVIEWER_TOKEN_ENV: TOKEN})


class LauncherTests(unittest.TestCase):
    served = Stage4RunnerTests.served
    bootstrap_served = Stage4RunnerTests.bootstrap_served
    setUp = Stage4RunnerTests.setUp
    switch = Stage4RunnerTests.switch
    launch = Stage4RunnerTests.launch
    require_confinement = Stage4RunnerTests.require_confinement

    def fake(self, name: str) -> Path:
        path = self.base / name
        path.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                        f"open({str(self.capture)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                        "if sys.argv[1:] != ['--version']:\n"
                        f"    open({str(self.capture) + '.env'!r}, 'a').write('shared' if os.environ.get({claude_auth.REVIEWER_TOKEN_ENV!r}) else 'hidden')\n",
                        encoding="utf-8")
        path.chmod(0o755)
        return path

    def seen(self) -> str:
        record = Path(str(self.capture) + ".env")
        value = record.read_text().strip()
        record.unlink()
        return value

    def test_only_the_reviewer_agent_gets_the_token_for_its_commands(self):
        self.require_confinement()
        self.switch("claude", stage4=False)
        self.launch(control.create(self.context, "claude_reviewer"), "claude_reviewer")
        self.assertEqual(self.seen(), "shared", "the Reviewer's test copy could not start a real Claude")
        for kind in ("claude_cto",):
            self.launch(control.create(self.context, kind), kind)
            self.assertEqual(self.seen(), "hidden", f"{kind} must not expose the token to its commands")


class GuardTests(unittest.TestCase):
    def test_evidence_that_contains_a_claude_token_is_refused_and_ordinary_evidence_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / "good.txt"
            good.write_text("command: python3 -m unittest\nresult: PASS\nRan 3 tests\nOK\n")
            self.assertTrue(board._require_evidence_file(root, str(good), "QA evidence"))
            bad = root / "bad.txt"
            bad.write_text("command: python3 -m unittest\nresult: PASS\nOK\nCLAUDE_CODE_OAUTH_TOKEN=" + TOKEN + "\n")
            with self.assertRaises(ValueError) as caught:
                board._require_evidence_file(root, str(bad), "QA evidence")
            self.assertIn("Claude login token", str(caught.exception))
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_the_public_release_leak_scan_knows_the_claude_token_shape(self):
        script = (ROOT / "scripts" / "make_public_release.sh").read_text()
        pattern = re.search(r'grep -rInE "([^"]+)" "\$out"', script).group(1)
        self.assertTrue(re.search(pattern, "x " + TOKEN), "Gate 2 would let a Claude setup-token into the public tree")
        self.assertTrue(re.search(pattern, "sk-ant-api03-" + "x" * 80))
        self.assertFalse(re.search(pattern, "an ordinary sentence"))


if __name__ == "__main__":
    unittest.main()

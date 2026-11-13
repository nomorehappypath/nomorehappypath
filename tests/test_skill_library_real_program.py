# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/skill_library_s1_proof.py: the S1 real-program proof
that Claude and Codex, launched the way NMHP confines a live builder or
reviewer, can read all 12 skill files. "Not proven" must never be reported
as shown.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "skill_library_s1_proof.py"
spec = importlib.util.spec_from_file_location("skill_library_s1_proof", SCRIPT)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class RunResultParsingTests(unittest.TestCase):
    """_run_one correctly tells a real success apart from a missing, wrong, or faked program."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {}
        for slug, content in (("backend", b"backend guide content"), ("security", b"security guide content")):
            path = Path(self.tmp.name) / f"{slug}.md"
            path.write_bytes(content)
            self.paths[slug] = path
        self.hashes = {slug: hashlib.sha256(path.read_bytes()).hexdigest() for slug, path in self.paths.items()}

    def _mocked_run(self, stdout: str, returncode: int = 0):
        completed = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")
        return unittest.mock.patch.object(subprocess, "run", return_value=completed), \
            unittest.mock.patch("shutil.which", return_value="/usr/bin/claude"), \
            unittest.mock.patch.object(proof.agent_confinement, "wrap", return_value=["/usr/bin/claude"])

    def test_missing_program_is_not_proven_not_a_crash(self):
        shown, detail = proof._run_one("not-a-real-vendor", "builder", ["backend"], [str(self.paths["backend"])])
        self.assertFalse(shown)
        self.assertIn("not installed", detail)

    def test_every_skills_real_hash_present_is_shown(self):
        stdout = f"backend: {self.hashes['backend']}\nsecurity: {self.hashes['security']}\n"
        m1, m2, m3 = self._mocked_run(stdout)
        with m1, m2, m3:
            shown, detail = proof._run_one("claude", "builder", ["backend", "security"], [str(self.paths["backend"]), str(self.paths["security"])])
        self.assertTrue(shown)

    def test_a_nonzero_exit_is_not_proven_even_with_every_hash_present(self):
        # Reviewer finding: the expected text appearing in the output is not proof the run
        # succeeded; a failing run that still echoed its prompt (or earlier partial output)
        # must not count as shown.
        stdout = f"backend: {self.hashes['backend']}\nsecurity: {self.hashes['security']}\n"
        m1, m2, m3 = self._mocked_run(stdout, returncode=1)
        with m1, m2, m3:
            shown, detail = proof._run_one("claude", "builder", ["backend", "security"], [str(self.paths["backend"]), str(self.paths["security"])])
        self.assertFalse(shown)
        self.assertIn("exited 1", detail)

    def test_a_missing_hash_is_not_proven(self):
        stdout = f"backend: {self.hashes['backend']}\n"
        m1, m2, m3 = self._mocked_run(stdout)
        with m1, m2, m3:
            shown, detail = proof._run_one("claude", "builder", ["backend", "security"], [str(self.paths["backend"]), str(self.paths["security"])])
        self.assertFalse(shown)
        self.assertIn("security", detail)

    def test_echoing_the_name_line_instead_of_the_real_hash_is_not_proven(self):
        # Second reviewer finding: printing a plausible-looking line that was never computed
        # from the file's real bytes (for example copying "name: backend" straight out of a
        # prompt) must not count as proof the file was actually read.
        stdout = "name: backend\nname: security\n"
        m1, m2, m3 = self._mocked_run(stdout)
        with m1, m2, m3:
            shown, detail = proof._run_one("claude", "builder", ["backend", "security"], [str(self.paths["backend"]), str(self.paths["security"])])
        self.assertFalse(shown)


class FullScriptTests(unittest.TestCase):
    """The script itself runs end to end in this environment, honestly."""

    def test_main_reports_eight_combinations_never_silently_shown(self):
        result = subprocess.run(["python3", str(SCRIPT)], capture_output=True, text=True, timeout=600)
        self.assertEqual(result.returncode, 0)
        rows = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(len(rows), 8)
        for row in rows:
            if not row["shown"]:
                self.assertTrue(row["detail"].startswith("not proven:"), row)


if __name__ == "__main__":
    unittest.main()

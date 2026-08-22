#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Regression tests for the composition step (engine/scripts/compose.py).

Stdlib `unittest` only — no third-party test deps (consistent with the stdlib-first rule).
Run via: bash engine/scripts/run_tests.sh   (or: python3 -m unittest discover -s engine/tests)
"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

# Make engine/scripts importable (compose.py + profile_config.py live there).
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import compose  # noqa: E402

# A profile that fills ALL 16 tokens (so a full compose leaves zero unset markers).
FULL_PROFILE = """\
product_name: "TestProd"
product_owner: "Owner"
org: "Org"
repos:
  - "repo_a"
  - "repo_b"
project_root: "/tmp/root"
board_path: "/tmp/board"
implementer_vendor: "VendorA"
reviewer_vendor: "VendorB"
primary_db: "TestDB"
test_command: "pytest"
build_command: "make"
lint_command: "ruff"
migrate_command: "migrate"
lifecycle_scripts:
  - "start.sh"
  - "stop.sh"
deployment_channels:
  - "Local dev"
  - "Cloud"
domain_qa_appendices: "qa/domain.md"
"""

# A profile that leaves most tokens unset.
PARTIAL_PROFILE = """\
product_name: "TestProd"
deployment_channels:
  - "Local dev"
"""


def _write(tmp, name, content):
    p = Path(tmp) / name
    p.write_text(content, encoding="utf-8")
    return p


class PureFunctionTests(unittest.TestCase):
    def test_scalar_substitution(self):
        unset = set()
        self.assertEqual(compose.substitute("{{PROFILE:product_name}}", {"product_name": "X"}, unset), "X")
        self.assertEqual(unset, set())

    def test_list_renders_comma_joined(self):
        self.assertEqual(compose.render_value(["a", "b", "c"]), "a, b, c")
        unset = set()
        self.assertEqual(compose.substitute("{{PROFILE:repos}}", {"repos": ["a", "b"]}, unset), "a, b")
        self.assertEqual(unset, set())

    def test_missing_empty_and_emptylist_render_unset_marker(self):
        for profile in ({}, {"org": ""}, {"org": []}):
            unset = set()
            out = compose.substitute("{{PROFILE:org}}", profile, unset)
            self.assertEqual(out, "<unset:org>")
            self.assertIn("org", unset)


class IntegrationTests(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = compose.main(argv)
        return code, buf.getvalue()

    def test_full_profile_leaves_no_residual_or_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = _write(tmp, "profile.config", FULL_PROFILE)
            out = Path(tmp) / "active"
            code, _ = self._run(["--profile", str(prof), "--out", str(out)])
            self.assertEqual(code, 0)
            files = list(out.rglob("*.md"))
            self.assertTrue(files, "compose produced no output files")
            for f in files:
                text = f.read_text(encoding="utf-8")
                self.assertNotIn("{{PROFILE:", text, f"residual token in {f.name}")
                self.assertNotIn("<unset:", text, f"unexpected unset marker in {f.name}")

    def test_idempotent_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = _write(tmp, "profile.config", FULL_PROFILE)
            out = Path(tmp) / "active"
            self._run(["--profile", str(prof), "--out", str(out)])
            first = {p.relative_to(out).as_posix(): p.read_text(encoding="utf-8") for p in sorted(out.rglob("*.md"))}
            self._run(["--profile", str(prof), "--out", str(out)])
            second = {p.relative_to(out).as_posix(): p.read_text(encoding="utf-8") for p in sorted(out.rglob("*.md"))}
            self.assertEqual(first, second)

    def test_missing_values_warn_and_mark_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = _write(tmp, "profile.config", PARTIAL_PROFILE)
            out = Path(tmp) / "active"
            code, stdout = self._run(["--profile", str(prof), "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("WARNING unset", stdout)
            joined = "\n".join(p.read_text(encoding="utf-8") for p in out.rglob("*.md"))
            self.assertIn("<unset:org>", joined)

    def test_strict_fails_on_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = _write(tmp, "profile.config", PARTIAL_PROFILE)
            out = Path(tmp) / "active"
            code, _ = self._run(["--profile", str(prof), "--out", str(out), "--strict"])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()

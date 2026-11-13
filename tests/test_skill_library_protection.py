# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Tests that no agent write route can reach NMHP's shared skill library,
per SKILL_LIBRARY_SPEC.md section 3 ("agents cannot change the library").
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness import agent_grant, skill_library


class LibraryWriteProtectionTests(unittest.TestCase):
    """agent_grant.agent_protected_roots denies the skill library, by identity."""

    def test_agent_protected_roots_includes_the_skill_library(self):
        protected = agent_grant.agent_protected_roots("/some/project", "/some/project/.harness", "/some/workspaces")
        self.assertIn(str(skill_library.library_root()), protected)

    def test_agent_protected_roots_honors_an_explicit_harness_root(self):
        tmp = tempfile.mkdtemp()
        try:
            shutil.copytree(skill_library.library_root(), Path(tmp) / skill_library.LIBRARY_DIRNAME)
            protected = agent_grant.agent_protected_roots(
                "/some/project", "/some/project/.harness", "/some/workspaces", harness_root=tmp,
            )
            self.assertIn(str(skill_library.library_root(tmp)), protected)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_denial_survives_a_symlinked_library_path(self):
        # agent_protected_roots resolves everything before comparing, so a
        # symlinked harness_root (or library folder) cannot evade the deny.
        tmp = tempfile.mkdtemp()
        try:
            real = Path(tmp) / "real-harness"
            real.mkdir()
            shutil.copytree(skill_library.library_root(), real / skill_library.LIBRARY_DIRNAME)
            link = Path(tmp) / "harness-link"
            link.symlink_to(real, target_is_directory=True)
            protected = agent_grant.agent_protected_roots(
                "/some/project", "/some/project/.harness", "/some/workspaces", harness_root=str(link),
            )
            self.assertIn(str(skill_library.library_root(real)), protected)
        except OSError:
            self.skipTest("symlinks unsupported on this filesystem")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_run_managed_agent_passes_harness_root_into_the_protection_call(self):
        text = Path("scripts/run_managed_agent.sh").read_text(encoding="utf-8")
        self.assertIn("agent_protected_roots(sys.argv[2], sys.argv[3], sys.argv[4], harness_root=sys.argv[1])", text)
        self.assertIn("skill", text.lower())


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The skill library never reaches the app being built (SKILL_LIBRARY_SPEC.md
requirement 14, S10/S11): it lives only in NMHP's own install, is never
copied into a project's code root, and nothing in the release/upload path
references it.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from harness import project_context, skill_library


class LibraryNeverReachesTheBuiltAppTests(unittest.TestCase):
    def test_library_root_lives_outside_a_default_projects_code_root(self):
        # A normal (non-self-check) project's code_root is some other
        # directory entirely; the library is resolved from NMHP's own
        # install, not from any project path, so it can never land inside
        # one by construction.
        project_root = Path("/tmp/some-owner-project")
        context = project_context.ProjectContext.compatibility(project_root)
        library = skill_library.library_root()
        self.assertFalse(str(library).startswith(str(context.code_root)))

    def test_commit_and_upload_code_never_mentions_the_skill_library(self):
        for path in ("harness/git_broker.py", "harness/upload_on_accept.py"):
            text = Path(path).read_text(encoding="utf-8")
            self.assertNotIn("skill_library", text)
            self.assertNotIn(skill_library.LIBRARY_DIRNAME + "/", text)


if __name__ == "__main__":
    unittest.main()

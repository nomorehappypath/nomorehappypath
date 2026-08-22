# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Runtime identity behavior for immutable Git-less release archives."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import runtime_identity


class ArchiveRuntimeIdentityTests(unittest.TestCase):
    def identity(self, source="a" * 64, commit="", tree=""):
        return {
            "version": 1,
            "commit": commit,
            "tree": tree,
            "source_digest": source,
            "clean": True,
        }

    def test_same_gitless_archive_matches_by_exact_executable_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "harness"
            package.mkdir()
            (package / "worker.py").write_text("VALUE = 'same bytes'\n", encoding="utf-8")

            manager = runtime_identity.capture(root)
            worker = runtime_identity.capture(root)

        self.assertEqual(manager["commit"], "")
        self.assertEqual(manager["tree"], "")
        self.assertTrue(manager["source_digest"])
        self.assertTrue(runtime_identity.matches(manager, worker))

    def test_changed_gitless_archive_fails_closed(self):
        self.assertFalse(runtime_identity.matches(
            self.identity("a" * 64), self.identity("b" * 64),
        ))

    def test_mixed_and_partial_git_identity_fail_closed(self):
        gitless = self.identity()
        repository = self.identity(commit="b" * 40, tree="c" * 40)
        partial_commit = self.identity(commit="b" * 40)
        partial_tree = self.identity(tree="c" * 40)

        self.assertFalse(runtime_identity.matches(gitless, repository))
        self.assertFalse(runtime_identity.matches(repository, gitless))
        self.assertFalse(runtime_identity.matches(partial_commit, partial_commit))
        self.assertFalse(runtime_identity.matches(partial_tree, partial_tree))

    def test_repository_identity_still_requires_commit_tree_and_source(self):
        expected = self.identity(commit="b" * 40, tree="c" * 40)
        self.assertTrue(runtime_identity.matches(expected, dict(expected)))
        self.assertFalse(runtime_identity.matches(
            expected, self.identity(commit="d" * 40, tree="c" * 40),
        ))
        self.assertFalse(runtime_identity.matches(
            expected, self.identity(commit="b" * 40, tree="d" * 40),
        ))
        self.assertFalse(runtime_identity.matches(expected, self.identity("d" * 64)))

    def test_gitless_identity_cannot_claim_a_reviewed_release_commit(self):
        identity = self.identity()
        reviewed_commit = "e" * 40
        self.assertNotEqual(identity["commit"], reviewed_commit)


if __name__ == "__main__":
    unittest.main()

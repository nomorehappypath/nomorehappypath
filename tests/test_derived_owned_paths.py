# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Batch 2 item B: ownership covers the rebuilt bundle, and named ignored files commit.

On headless-browser-maya-mac a front-end subtask could not commit its rebuilt
frontend/dist, and packaging/build.sh (under an ignored folder) could not be
committed at all, each costing extra subtasks and git bookkeeping.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, git_broker
from harness.git_broker import AuthorizationError, GitBroker
from harness.project_context import ProjectContext


class EffectiveOwnershipTests(unittest.TestCase):
    def test_owned_source_tree_owns_its_build_output(self):
        self.assertEqual(
            git_broker.effective_owned_paths(["frontend/src/components"]),
            ["frontend/dist", "frontend/src/components"],
        )
        self.assertEqual(git_broker.effective_owned_paths(["src"]), ["dist", "src"])
        self.assertEqual(git_broker.effective_owned_paths(["*"]), ["*"])
        self.assertEqual(git_broker.effective_owned_paths(["backend/api.py"]), ["backend/api.py"])

    def test_rebuilt_bundle_is_owned_and_everything_else_still_refused(self):
        item = {"owned_paths": ["frontend/src"]}
        board._require_owned_files(
            item, ["frontend/src/App.tsx", "frontend/dist/index.html", "frontend/dist/assets/index-a1b2.js"],
            "governed commit manifest",
        )
        for outside in ("backend/api.py", "admin/dist/index.js", "frontend/package.json"):
            with self.subTest(outside=outside), self.assertRaisesRegex(AuthorizationError, "ownership boundary: " + outside):
                board._require_owned_files(item, ["frontend/src/App.tsx", outside], "governed commit manifest")

    def test_two_subtasks_editing_one_frontend_serialize_on_the_shared_bundle(self):
        left = {"owned_paths": ["frontend/src/login.tsx"], "owned_surfaces": []}
        right = {"owned_paths": ["frontend/src/report.tsx"], "owned_surfaces": []}
        self.assertTrue(board._subtask_ownership_overlaps(left, right))
        backend = {"owned_paths": ["backend/api.py"], "owned_surfaces": []}
        self.assertFalse(board._subtask_ownership_overlaps(left, backend))


class IgnoredFolderCommitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        repository = base / "code"
        repository.mkdir()
        self.git(repository, "init", "-b", "main")
        (repository / ".gitignore").write_text("packaging/\n*.log\n", encoding="utf-8")
        (repository / "product.txt").write_text("base\n", encoding="utf-8")
        self.git(repository, "add", ".gitignore", "product.txt")
        self.git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "base")
        head = self.git(repository, "rev-parse", "HEAD").strip()
        self.state = {
            "agents": {"delivery": {"id": "delivery", "role": "engineering", "task": "TASK", "active": True,
                                    "session_id": "session-delivery", "write_authority": True}},
            "delivery_plans": {"TASK": {"mode": "atomic", "subtasks": {}}},
            "task_repositories": {"TASK": str(repository)}, "task_workspaces": {},
            "subtask_workspaces": {}, "task_baselines": {"TASK": {"head": head}},
            "qa_requests": {}, "release_decisions": {}, "git_acceptances": {},
            "remote_push_instructions": {}, "approved_remotes": {},
        }
        self.broker = GitBroker(ProjectContext(repository, base / "data", base / "workspaces"), state_loader=lambda: self.state)
        created = self.broker.branch_create("delivery", 1)
        self.state["task_workspaces"]["TASK"] = created["workspace"]
        self.workspace = Path(created["workspace"])

    def git(self, cwd, *arguments):
        result = subprocess.run(["/usr/bin/git", *arguments], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_named_file_under_an_ignored_folder_is_committed_and_nothing_else(self):
        (self.workspace / "packaging").mkdir()
        (self.workspace / "packaging" / "build.sh").write_text("#!/bin/sh\necho build\n", encoding="utf-8")
        (self.workspace / "packaging" / "notarize-output.zip").write_text("artifact\n", encoding="utf-8")
        (self.workspace / "build.log").write_text("noise\n", encoding="utf-8")
        (self.workspace / "product.txt").write_text("changed\n", encoding="utf-8")
        committed = self.broker.stage_commit("delivery", 2, ["packaging/build.sh", "product.txt"], "add the build script")
        self.assertEqual(committed["manifest"], ["packaging/build.sh", "product.txt"])
        tracked = self.git(self.workspace, "ls-files").split()
        self.assertIn("packaging/build.sh", tracked)
        self.assertNotIn("packaging/notarize-output.zip", tracked)
        self.assertNotIn("build.log", tracked)
        # Once tracked, later edits commit like any other file.
        (self.workspace / "packaging" / "build.sh").write_text("#!/bin/sh\necho build v2\n", encoding="utf-8")
        again = self.broker.stage_commit("delivery", 3, ["packaging/build.sh"], "update the build script")
        self.assertEqual(again["manifest"], ["packaging/build.sh"])


if __name__ == "__main__":
    unittest.main()

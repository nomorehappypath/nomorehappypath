# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The app's own .harness storage is never part of what a review capture is compared with (2026-11-05).

Found by the independent reviewer's whole-app walk: during a normal repair a generated sandbox profile under
`.harness/control/` was committed with the project. A capture's fingerprint leaves `.harness` out, the committed-files
map did not, so every fresh passing capture was refused as "the code changed after this capture" naming that file, and
the task could not finish review. Tested here on a real git folder: the committed map leaves `.harness` and
`__pycache__` out exactly like the fingerprint, a changed project file is still seen, and a profile store inside
`.harness` makes that folder ignore itself so `git add` no longer picks such files up.

Run:  PYTHONPATH=. python3 -m unittest tests.test_harness_files_out_of_captures -v
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board
from harness.platform_support import defaults


class HarnessStorageIsNotProjectContent(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.repo = Path(holder.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "app.py").write_text("print('one')\n")
        (self.repo / ".harness" / "control").mkdir(parents=True)
        (self.repo / ".harness" / "control" / "agent-sandbox-ed6d0098601c9342.sb").write_text("(version 1)\n")
        (self.repo / "pkg" / "__pycache__").mkdir(parents=True)
        (self.repo / "pkg" / "__pycache__" / "x.pyc").write_bytes(b"\0")
        self.git("add", "-A", "-f")
        self.git("commit", "-qm", "a repair that also committed generated files")
        self.commit = self.git("rev-parse", "HEAD")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def test_the_committed_map_and_the_capture_fingerprint_cover_the_same_files(self):
        committed = board._commit_files(self.repo, self.commit)
        captured = board._tree_fingerprint(self.repo, [])[0]["files"]
        self.assertEqual(sorted(committed), ["app.py"])
        self.assertEqual(sorted(captured), ["app.py"])
        self.assertEqual(committed["app.py"].split(":")[0], captured["app.py"].split(":")[0], "the same file has the same id in both")

    def test_a_changed_project_file_is_still_seen(self):
        before = board._commit_files(self.repo, self.commit)
        (self.repo / "app.py").write_text("print('two')\n")
        self.git("commit", "-qam", "a real change")
        after = board._commit_files(self.repo, self.git("rev-parse", "HEAD"))
        self.assertNotEqual(before["app.py"], after["app.py"])


class TheStorageFolderIgnoresItself(unittest.TestCase):
    def test_a_profile_store_inside_dot_harness_writes_one_gitignore_and_git_add_skips_it(self):
        with tempfile.TemporaryDirectory() as name:
            repo = Path(name).resolve()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            store = repo / ".harness" / "control"
            store.mkdir(parents=True)
            defaults.keep_out_of_git(store)
            self.assertEqual((repo / ".harness" / ".gitignore").read_text(), "*\n")
            (store / "agent-sandbox-abc.sb").write_text("(version 1)\n")
            (repo / "app.py").write_text("print('one')\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo, check=True, capture_output=True, text=True).stdout.split()
            self.assertEqual(staged, ["app.py"])
            (repo / ".harness" / ".gitignore").write_text("# the owner's own\n")
            defaults.keep_out_of_git(store)
            self.assertEqual((repo / ".harness" / ".gitignore").read_text(), "# the owner's own\n", "an existing file is left alone")

    def test_a_store_outside_any_dot_harness_writes_nothing(self):
        with tempfile.TemporaryDirectory() as name:
            store = Path(name).resolve() / "data" / "control"
            store.mkdir(parents=True)
            defaults.keep_out_of_git(store)
            self.assertEqual([p.name for p in store.parent.rglob(".gitignore")], [])


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from harness import accepted_bytes


class AcceptedByteManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.git("init")
        self.git("config", "user.name", "Harness Test")
        self.git("config", "user.email", "harness@example.invalid")
        (self.repo / "keep.txt").write_text("keep\n")
        (self.repo / "rename-source.txt").write_text("rename\n")
        (self.repo / "delete.txt").write_text("delete\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.repo / "keep.txt").write_text("accepted\n")
        os.chmod(self.repo / "keep.txt", 0o755)
        (self.repo / "delete.txt").unlink()
        self.git("mv", "rename-source.txt", "rename-target.txt")
        (self.repo / "linked").symlink_to("keep.txt")
        self.git("add", "-A")
        self.git("commit", "-m", "candidate")
        self.reviewed = self.git("rev-parse", "HEAD")
        self.manifest = accepted_bytes.build_manifest(self.repo, self.base, self.reviewed)

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def commit_tree(self, name: str, mutate) -> str:
        self.git("checkout", "--detach", self.reviewed)
        mutate()
        self.git("add", "-A")
        self.git("commit", "-m", name)
        return self.git("rev-parse", "HEAD")

    def test_manifest_records_mode_type_oid_deletions_and_both_rename_halves(self):
        entries = {item["path"]: item for item in self.manifest["entries"]}
        self.assertEqual(entries["keep.txt"]["mode"], "100755")
        self.assertEqual(entries["linked"]["mode"], "120000")
        self.assertEqual(entries["linked"]["type"], "blob")
        self.assertEqual(entries["delete.txt"], {"path": "delete.txt", "state": "deleted"})
        self.assertEqual(entries["rename-source.txt"]["state"], "deleted")
        self.assertEqual(entries["rename-target.txt"]["state"], "present")
        accepted_bytes.verify_entries(self.repo, self.reviewed, self.manifest)

    def test_omitted_reviewed_path_is_refused_before_manifest_creation(self):
        with self.assertRaisesRegex(ValueError, "differs from the reviewed change"):
            accepted_bytes.build_manifest(
                self.repo, self.base, self.reviewed,
                [path for path in self.manifest["paths"] if path != "delete.txt"],
            )

    def test_substituted_bytes_mode_flip_type_swap_and_missing_rename_delete_fail(self):
        cases = {
            "bytes": lambda: (self.repo / "keep.txt").write_text("substituted\n"),
            "mode": lambda: os.chmod(self.repo / "keep.txt", 0o644),
            "type": lambda: ((self.repo / "linked").unlink(), (self.repo / "linked").write_text("keep.txt")),
            "rename": lambda: (self.repo / "rename-source.txt").write_text("restored source\n"),
        }
        for name, mutation in cases.items():
            with self.subTest(name=name):
                revision = self.commit_tree(name, mutation)
                with self.assertRaisesRegex(ValueError, "exact accepted entry"):
                    accepted_bytes.verify_entries(self.repo, revision, self.manifest)

    def test_tampered_and_torn_manifests_fail_closed(self):
        tampered = dict(self.manifest)
        tampered["paths"] = tampered["paths"][:-1]
        with self.assertRaisesRegex(ValueError, "digest"):
            accepted_bytes.verify_manifest(self.repo, tampered)
        torn = {"version": 1, "base_commit": self.base}
        with self.assertRaisesRegex(ValueError, "digest"):
            accepted_bytes.verify_manifest(self.repo, torn)

    def test_planned_full_tree_mismatch_is_refused(self):
        accepted_bytes.verify_planned_tree(self.repo, self.reviewed, self.manifest["reviewed_tree"])
        with self.assertRaisesRegex(ValueError, "planned tree"):
            accepted_bytes.verify_planned_tree(self.repo, self.reviewed, "0" * 40)

    def test_tree_delta_includes_deletions_and_mode_aware_entries(self):
        delta = accepted_bytes.tree_delta(self.repo, self.base, self.reviewed)
        self.assertEqual(delta["paths"], self.manifest["paths"])
        self.assertEqual(delta["entries"], self.manifest["entries"])
        self.assertEqual(len(delta["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()

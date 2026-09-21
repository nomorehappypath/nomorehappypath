# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Source-revision digests must see a subpackage, or they describe less than they claim.

`docs/specs/LINUX_STAGE0_PLATFORM_SEAM.md` §3.5: five places walked
`harness/*.py` non-recursively. Two of them feed the RUNTIME source-revision
hash used by the launchers to notice edited installations, so a
`harness/platform_support/` package would have been invisible to them — a
long-running surface could keep serving code the operator believed was
replaced. A guard that checks less is a weaker test; a refresh hash that cannot
see the seam is a correctness bug.

These pin the corrected behaviour BEFORE the package exists, which is what
makes the change a no-op rather than a behaviour change.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from harness import runtime_identity

ROOT = Path(__file__).resolve().parents[1]


def _tree(base: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        path = base / "harness" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return base


class SourceDigestCoversSubpackagesTests(unittest.TestCase):
    def test_a_file_in_a_subpackage_changes_the_digest(self):
        """The defect, stated as behaviour: before the fix this digest never moved."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp), {
                "board.py": "x = 1\n",
                "platform_support/__init__.py": "y = 1\n",
            })
            before = runtime_identity._source_digest(root)
            (root / "harness" / "platform_support" / "__init__.py").write_text("y = 2\n", encoding="utf-8")
            after = runtime_identity._source_digest(root)
            self.assertNotEqual(before, after,
                                "editing a subpackage file left the source digest unchanged")

    def test_adding_a_subpackage_changes_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp), {"board.py": "x = 1\n"})
            before = runtime_identity._source_digest(root)
            _tree(root, {"platform_support/__init__.py": "y = 1\n"})
            self.assertNotEqual(before, runtime_identity._source_digest(root),
                                "adding a subpackage left the source digest unchanged")

    def test_same_name_in_two_directories_is_not_a_collision(self):
        """Keyed on the relative path, not the bare filename."""
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            a = _tree(Path(one), {"a/control.py": "x = 1\n"})
            b = _tree(Path(two), {"b/control.py": "x = 1\n"})
            self.assertNotEqual(runtime_identity._source_digest(a),
                                runtime_identity._source_digest(b),
                                "two files sharing a name in different directories collided")

    def test_the_recursive_walk_sees_the_platform_package(self):
        """Replaces the landing-window check, which has now expired by design.

        Step 2 asserted the recursive digest EQUALLED the flat one "while no
        subpackage exists" — the property that made landing it a no-op. Step 3
        created `harness/platform_support/`, so that window has closed and the
        assertion is now false on purpose. What matters from here is the
        opposite: the recursive walk must see the package the flat one misses.
        """
        source = ROOT / "harness"
        flat = {path.name for path in source.glob("*.py")}
        recursive = {path.relative_to(source).as_posix() for path in source.rglob("*.py")}
        package = {name for name in recursive if name.startswith("platform_support/")}
        self.assertTrue(package, "harness/platform_support/ is missing from the recursive walk")
        self.assertFalse(package & flat, "the flat walk never saw the package; that was the defect")

        flat_digest = hashlib.sha256()
        for path in sorted(source.glob("*.py")):
            flat_digest.update(path.name.encode("utf-8")); flat_digest.update(b"\0")
            flat_digest.update(path.read_bytes()); flat_digest.update(b"\0")
        self.assertNotEqual(
            flat_digest.hexdigest(), runtime_identity._source_digest(ROOT),
            "the digest no longer distinguishes a tree with the platform package from one without",
        )


class LauncherRevisionScriptsTests(unittest.TestCase):
    """The two runtime hashes live in shell, so they are pinned as source text.

    Stated limitation: this asserts the scripts CARRY the corrected walk, not
    that a running launcher recomputes it — the launchers are long-lived
    processes and starting one inside the suite would be worse than the gap.
    """

    SCRIPTS = ("scripts/start_project_manager.sh", "scripts/start_board_viewer.sh")

    def test_both_launchers_walk_sources_recursively(self):
        for name in self.SCRIPTS:
            with self.subTest(script=name):
                body = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn('rglob("*.py")', body,
                              "a non-recursive walk cannot see harness/platform_support/")
                self.assertNotIn('.glob("*.py")', body)
                self.assertIn("relative_to(source_root)", body,
                              "the digest must key on the relative path, not the bare filename")

    def test_both_launchers_still_parse(self):
        import subprocess
        for name in self.SCRIPTS:
            with self.subTest(script=name):
                completed = subprocess.run(["bash", "-n", str(ROOT / name)],
                                           capture_output=True, timeout=30)
                self.assertEqual(completed.returncode, 0, completed.stderr.decode())


if __name__ == "__main__":
    unittest.main()

"""A failure that already existed when the task began does not count against the task (owner, 2026-11-11)."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import inherited_failures


def git(folder, *args):
    return subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, check=True).stdout.strip()


def unittest_output(folder, command):
    ran = subprocess.run(command, cwd=folder, shell=True, capture_output=True, text=True)
    return ran.stdout + ran.stderr


class Inherited(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests" / "__init__.py").write_text("")
        (self.repo / "tests" / "test_a.py").write_text(
            "import unittest\nclass A(unittest.TestCase):\n    def test_old_broken(self): self.fail('old')\n    def test_fine(self): pass\n")
        git(self.repo, "init", "-q")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.command = "python3 -m unittest discover -s tests -t ."

    def test_only_old_failures_are_inherited(self):
        out = unittest_output(self.repo, self.command)
        verdict = inherited_failures.judge(self.command, out, self.repo, self.base)
        self.assertEqual(verdict["ids"], ["tests.test_a.A.test_old_broken"])
        self.assertIn("did not cause them", verdict["note"])
        self.assertEqual(git(self.repo, "worktree", "list").count("\n"), 0, "the temporary copy is removed")

    def test_a_new_failure_is_the_tasks_own(self):
        (self.repo / "tests" / "test_b.py").write_text(
            "import unittest\nclass B(unittest.TestCase):\n    def test_new_broken(self): self.fail('new')\n")
        out = unittest_output(self.repo, self.command)
        self.assertIsNone(inherited_failures.judge(self.command, out, self.repo, self.base))

    def test_unreadable_output_is_never_inherited(self):
        self.assertIsNone(inherited_failures.judge(self.command, "something broke", self.repo, self.base))
        self.assertIsNone(inherited_failures.judge("pytest -q", "FAILED x", self.repo, self.base))
        self.assertIsNone(inherited_failures.judge(self.command, "FAIL: test_x (tests.t.A)\nFAILED (failures=1)", self.repo, ""))


if __name__ == "__main__":
    unittest.main()

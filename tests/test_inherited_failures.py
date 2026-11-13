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

    def test_the_baseline_keeps_its_git_context_so_a_new_regression_in_git_dependent_code_is_not_inherited(self):
        # review round 1: a plain archive has no .git, so a test that asks git failed at the baseline for that reason and a real
        # regression looked inherited. The baseline here is a repository at the commit.
        (self.repo / "tests" / "test_git_context.py").write_text(
            "import subprocess, unittest\nimport mod\nclass G(unittest.TestCase):\n"
            "    def test_describe(self):\n        self.assertEqual(mod.describe(), subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True).stdout.strip())\n")
        (self.repo / "mod.py").write_text("import subprocess\ndef describe():\n    return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True).stdout.strip()\n")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "git dependent test")
        base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "mod.py").write_text("def describe():\n    return ''\n")           # the task breaks it
        out = unittest_output(self.repo, self.command)
        # the old failure is inherited, the git-dependent test passes at the baseline, so the failures are not all inherited: the task's own
        self.assertIsNone(inherited_failures.judge(self.command, out, self.repo, base))

    def test_the_projects_repository_is_left_exactly_as_it_was(self):
        out = unittest_output(self.repo, self.command)
        before = subprocess.run(["find", ".git", "-type", "f", "-exec", "shasum", "{}", "+"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertIsNotNone(inherited_failures.judge(self.command, out, self.repo, self.base))
        after = subprocess.run(["find", ".git", "-type", "f", "-exec", "shasum", "{}", "+"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(sorted(before.splitlines()), sorted(after.splitlines()))
        self.assertEqual(git(self.repo, "worktree", "list").count("\n"), 0)

    def test_unreadable_output_is_never_inherited(self):
        self.assertIsNone(inherited_failures.judge(self.command, "something broke", self.repo, self.base))
        self.assertIsNone(inherited_failures.judge("pytest -q", "FAILED x", self.repo, self.base))
        self.assertIsNone(inherited_failures.judge(self.command, "FAIL: test_x (tests.t.A)\nFAILED (failures=1)", self.repo, ""))


if __name__ == "__main__":
    unittest.main()

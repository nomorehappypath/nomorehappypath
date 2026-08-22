# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import ast
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness import board, child_process, cto, git_process


ROOT = Path(__file__).resolve().parents[1]


def initialize_repository(path: Path, marker: str) -> str:
    path.mkdir()
    git_process.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True, text=True)
    (path / "marker.txt").write_text(marker, encoding="utf-8")
    git_process.run(["git", "add", "marker.txt"], cwd=path, check=True, capture_output=True, text=True)
    git_process.run(
        ["git", "-c", "user.name=Harness Test", "-c", "user.email=harness@example.invalid", "commit", "-m", marker],
        cwd=path, check=True, capture_output=True, text=True,
    )
    return git_process.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


class ChildProgramEnvironmentTests(unittest.TestCase):
    def test_child_environment_neutralizes_python_shell_and_git_contracts(self):
        hostile = {
            "PATH": os.environ.get("PATH", ""),
            "HARNESS_CODEX_BIN": "/retained-codex",
            "GIT_DIR": "/foreign/.git",
            "PYTHONHOME": "/foreign/python",
            "PYTHONPATH": "/foreign/modules",
            "PYTHONSTARTUP": "/foreign/startup.py",
            "BASH_ENV": "/foreign/bash-env",
            "ENV": "/foreign/sh-env",
            "CDPATH": "/foreign/cdpath",
        }
        sanitized = child_process.environment(hostile, git=True, python=True, shell=True)
        self.assertEqual(sanitized["HARNESS_CODEX_BIN"], "/retained-codex")
        self.assertFalse(any(key.startswith("PYTHON") for key in sanitized))
        self.assertFalse(any(key in child_process.SHELL_AMBIENT_KEYS for key in sanitized))
        self.assertEqual(sanitized["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(sanitized["GIT_CONFIG_GLOBAL"], os.devnull)
        provider_environment = child_process.environment(hostile, git=True, shell=True)
        self.assertEqual(provider_environment["PYTHONHOME"], "/foreign/python")
        self.assertEqual(provider_environment["PYTHONPATH"], "/foreign/modules")
        self.assertEqual(provider_environment["HARNESS_CODEX_BIN"], "/retained-codex")


class GitProcessTests(unittest.TestCase):
    def test_sanitized_environment_removes_every_git_variable(self):
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/retained-home",
            "GIT_DIR": "/foreign/.git",
            "GIT_WORK_TREE": "/foreign",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.worktree",
            "GIT_CONFIG_VALUE_0": "/foreign",
            "GIT_FUTURE_OVERRIDE": "must also be removed",
        }
        sanitized = git_process.sanitized_environment(environment)
        self.assertEqual(sanitized["HOME"], "/retained-home")
        self.assertEqual(sanitized["PATH"], environment["PATH"])
        self.assertEqual(sanitized["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(sanitized["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(
            {key for key in sanitized if key.startswith("GIT_")},
            {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"},
        )

    def test_git_boundary_targets_explicit_cwd_under_hostile_git_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            intended = base / "intended"
            foreign = base / "foreign"
            intended_head = initialize_repository(intended, "intended")
            foreign_head = initialize_repository(foreign, "foreign")
            self.assertNotEqual(intended_head, foreign_head)
            hostile = {
                "GIT_DIR": str(foreign / ".git"),
                "GIT_WORK_TREE": str(foreign),
                "GIT_INDEX_FILE": str(foreign / ".git" / "index"),
                "GIT_COMMON_DIR": str(foreign / ".git"),
                "GIT_OBJECT_DIRECTORY": str(foreign / ".git" / "objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(foreign / ".git" / "objects"),
                "GIT_CEILING_DIRECTORIES": str(base),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.worktree",
                "GIT_CONFIG_VALUE_0": str(foreign),
            }
            with patch.dict(os.environ, hostile, clear=False):
                result = git_process.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    cwd=intended, check=True, capture_output=True, text=True,
                )
                head = git_process.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=intended, check=True, capture_output=True, text=True,
                )
            self.assertEqual(Path(result.stdout.strip()).resolve(), intended.resolve())
            self.assertEqual(head.stdout.strip(), intended_head)

    def test_board_and_cto_git_evidence_ignore_hostile_git_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            intended = base / "intended"
            foreign = base / "foreign"
            intended_head = initialize_repository(intended, "intended")
            initialize_repository(foreign, "foreign")
            hostile = {
                "GIT_DIR": str(foreign / ".git"),
                "GIT_WORK_TREE": str(foreign),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.worktree",
                "GIT_CONFIG_VALUE_0": str(foreign),
            }
            with patch.dict(os.environ, hostile, clear=False):
                self.assertEqual(board._git_repository(intended), intended.resolve())
                baseline = board._git_task_baseline(intended)
                self.assertTrue(baseline["available"])
                self.assertEqual(baseline["head"], intended_head)
                self.assertEqual(cto._git_output(intended, "rev-parse", "HEAD"), intended_head)

    def test_git_safe_health_command_uses_checkout_and_deterministic_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            intended = base / "intended"
            foreign = base / "foreign"
            intended_head = initialize_repository(intended, "intended")
            initialize_repository(foreign, "foreign")
            review = {"reviewed_commit": intended_head, "reviewed_files": ["marker.txt"]}
            hostile = {
                "GIT_DIR": str(foreign / ".git"),
                "GIT_WORK_TREE": str(foreign),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.worktree",
                "GIT_CONFIG_VALUE_0": str(foreign),
                "HEALTH_TOKEN": "must-not-cross",
                "PYTHONPATH": "/must-not-cross",
            }
            with patch.dict(os.environ, hostile, clear=False):
                artifact = cto._task_artifact_gate(
                    intended, "TASK", intended, review, True,
                    'test -f marker.txt && test "$TZ" = UTC && test -z "$HEALTH_TOKEN" && test -z "$PYTHONPATH"',
                )
            self.assertTrue(artifact["artifact_commit_exact"])
            self.assertTrue(artifact["artifact_archive_verified"])
            self.assertTrue(artifact["artifact_health_verified"], artifact["artifact_health_output"])

    def test_all_harness_owned_git_subprocesses_use_the_sanitized_boundary(self):
        direct_calls = []
        for path in sorted((ROOT / "harness").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess" and node.func.attr == "run"):
                    continue
                if not node.args or not isinstance(node.args[0], (ast.List, ast.Tuple)) or not node.args[0].elts:
                    continue
                first = node.args[0].elts[0]
                if isinstance(first, ast.Constant) and first.value == "git":
                    direct_calls.append(f"{path.name}:{node.lineno}")
        self.assertEqual(direct_calls, [])
        launcher = (ROOT / "scripts" / "start_board_viewer.sh").read_text(encoding="utf-8")
        self.assertNotIn("git -C", launcher)

    def test_only_git_broker_contains_literal_mutating_git_commands(self):
        mutating = {
            "add", "apply", "branch", "checkout", "cherry-pick", "commit",
            "merge", "push", "rebase", "reset", "tag", "update-ref", "worktree",
        }
        bypasses = []
        for path in sorted((ROOT / "harness").glob("*.py")):
            if path.name == "git_broker.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args or not isinstance(node.args[0], (ast.List, ast.Tuple)):
                    continue
                values = [
                    element.value for element in node.args[0].elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                ]
                if not values or values[0] != "git":
                    continue
                command = next((value for value in values[1:] if not value.startswith("-")), "")
                if command == "branch" and "--show-current" in values:
                    continue
                if command == "worktree" and "list" in values:
                    continue
                if command in mutating:
                    bypasses.append(f"{path.name}:{node.lineno}:{command}")
        self.assertEqual(bypasses, [])


if __name__ == "__main__":
    unittest.main()

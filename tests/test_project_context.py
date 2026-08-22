# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr

from harness import board, contract, control, timer, workspace_settings
from harness.project_context import ProjectContext, context_environment, context_from_roots, project_context


class ProjectContextTests(unittest.TestCase):
    def test_compatibility_mapping_and_existing_paths_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            context = ProjectContext.compatibility(root)

            self.assertEqual(context.code_root, root)
            self.assertEqual(context.data_root, root / ".harness")
            self.assertEqual(context.workspace_root, root.parent / ".harness-task-workspaces")
            self.assertEqual(board.board_dir(context), root / ".harness" / "board")
            self.assertEqual(control.control_dir(context), root / ".harness" / "control")
            self.assertEqual(contract._task_path(context, "T1"), root / ".harness" / "tasks" / "T1.json")
            self.assertEqual(
                workspace_settings.settings_path(context),
                root / ".harness" / "control" / "workspace_settings.json",
            )
            self.assertIn(str(root / ".harness" / "board" / "watch.log"), timer.cron(context, root / "profile.json", 5))

    def test_explicit_context_stores_directly_under_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            code = parent / "adopted"
            data = parent / "managed" / "alpha"
            workspaces = parent / "workspaces" / "alpha"
            code.mkdir()
            context = ProjectContext(code, data, workspaces)

            board.register(context, "qa", "REVIEW_QUEUE", vendor="Anthropic")
            contract.create_contract(context, "T1", "Ship context", ["paths"])
            control.create(context, "codex_delivery")

            self.assertTrue((data / "board" / "state.json").is_file())
            self.assertTrue((data / "tasks" / "T1.json").is_file())
            self.assertTrue((data / "control" / "sessions.json").is_file())
            self.assertFalse((data / ".harness").exists())
            self.assertFalse((code / ".harness").exists())

    def test_context_is_canonical_and_rejects_storage_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = ProjectContext(root / "code" / ".." / "code", root / "data", root / "workspaces")
            self.assertEqual(project_context(context), context)
            self.assertTrue(context.code_root.is_absolute())
            with self.assertRaisesRegex(ValueError, "relative to data_root"):
                context.storage_path("../outside")
            with self.assertRaisesRegex(ValueError, "relative to data_root"):
                context.storage_path(root / "absolute")

    def test_context_rejects_home_dependent_tilde_roots(self):
        with patch.dict("os.environ", {"HOME": "/__foreign_home__"}, clear=False):
            with self.assertRaisesRegex(ValueError, "must not depend on HOME"):
                ProjectContext.compatibility("~/project")

    def test_board_cli_carries_an_explicit_context_without_hidden_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            code = parent / "adopted"
            data = parent / "managed" / "alpha"
            workspaces = parent / "workspaces" / "alpha"
            code.mkdir()
            command = [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "harness" / "board.py"),
                "--root", str(code),
                "--data-root", str(data),
                "--workspace-root", str(workspaces),
                "register", "--role", "qa", "--task", "REVIEW_QUEUE", "--vendor", "Anthropic",
            ]
            completed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["task"], "REVIEW_QUEUE")
            self.assertTrue((data / "board" / "state.json").is_file())
            self.assertFalse((code / ".harness").exists())

    def test_environment_never_changes_context_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first = ProjectContext(base / "one", base / "data-one", base / "work-one")
            second = base / "two"
            with patch.dict("os.environ", context_environment(first), clear=False):
                self.assertEqual(context_from_roots(first.code_root), ProjectContext.compatibility(first.code_root))
                self.assertEqual(context_from_roots(second), ProjectContext.compatibility(second))
                self.assertEqual(
                    context_from_roots(first.code_root, first.data_root, first.workspace_root),
                    first,
                )

    def test_partial_environment_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            requested = base / "requested"
            environment = {
                "HARNESS_CODE_ROOT": str(requested),
                "HARNESS_DATA_ROOT": str(base / "session-data"),
            }
            with patch.dict("os.environ", environment, clear=False):
                self.assertEqual(context_from_roots(requested), ProjectContext.compatibility(requested))

    def test_partial_explicit_context_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            code = base / "session"
            with self.assertRaisesRegex(ValueError, "must be supplied together"):
                context_from_roots(code, base / "session-data")

    def test_board_cli_ignores_ambient_context_for_every_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            session_code = base / "session-code"; session_code.mkdir()
            session_context = ProjectContext(session_code, base / "session-data", base / "session-workspaces")
            foreign_code = base / "foreign-code"; foreign_code.mkdir()
            environment = {**os.environ, **context_environment(session_context)}
            board_script = str(Path(__file__).resolve().parents[1] / "harness" / "board.py")

            matching = subprocess.run(
                [sys.executable, board_script, "--root", str(session_code), "register", "--role", "qa", "--task", "REVIEW_QUEUE", "--vendor", "Anthropic"],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(matching.returncode, 0, matching.stderr)
            self.assertTrue((session_code / ".harness" / "board" / "state.json").is_file())
            self.assertFalse(session_context.data_root.exists())

            foreign = subprocess.run(
                [sys.executable, board_script, "--root", str(foreign_code), "register", "--role", "qa", "--task", "REVIEW_QUEUE", "--vendor", "Anthropic"],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(foreign.returncode, 0, foreign.stderr)
            self.assertTrue((foreign_code / ".harness" / "board" / "state.json").is_file())

    def test_explicit_context_is_stable_under_tmpdir_umask_and_ascii_locale(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            code = base / "adopted project"; code.mkdir()
            data = base / "manager data"
            workspaces = base / "task workspaces"
            foreign_tmp = base / "foreign tmp"
            board_script = str(Path(__file__).resolve().parents[1] / "harness" / "board.py")
            environment = {
                **os.environ,
                "LC_ALL": "C",
                "PYTHONIOENCODING": "ascii",
                "TMPDIR": str(foreign_tmp),
            }
            previous_umask = os.umask(0o077)
            try:
                completed = subprocess.run(
                    [
                        sys.executable, board_script,
                        "--root", str(code),
                        "--data-root", str(data),
                        "--workspace-root", str(workspaces),
                        "register", "--role", "qa", "--task", "REVIEW_QUEUE",
                        "--name", "Prøject", "--vendor", "Anthropic",
                    ],
                    capture_output=True,
                    text=True,
                    env=environment,
                )
            finally:
                os.umask(previous_umask)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            state = json.loads((data / "board" / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(any(agent.get("display_name") == "Prøject" for agent in state["agents"].values()))
            self.assertFalse((code / ".harness").exists())
            self.assertFalse(foreign_tmp.exists())

    def simulate_full_suite_under_exported_session_environment(self):
        """Scenario-only runner: deliberately excluded from test discovery."""
        root = Path(__file__).resolve().parents[1]
        foreign = ProjectContext(
            Path("/__harness_phase1_foreign_session__"),
            Path("/__harness_phase1_foreign_session__/data"),
            Path("/__harness_phase1_foreign_session__/workspaces"),
        )
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=root,
            env={**os.environ, **context_environment(foreign), "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
        )
        output = completed.stdout + completed.stderr
        print(output)
        self.assertEqual(completed.returncode, 0, output)
        self.assertRegex(output, r"Ran [1-9][0-9]* tests")

    def test_recovery_log_uses_code_root_without_context_repr(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            code = base / "code"; code.mkdir()
            context = ProjectContext(code, base / "data", base / "workspaces")
            output = io.StringIO()
            with redirect_stderr(output):
                board._log_board_recovery(context, "simulated recovery")
            self.assertIn(str(context.code_root), output.getvalue())
            self.assertNotIn("ProjectContext(", output.getvalue())
            marker = context.board_backup_root / "RECOVERY.log"
            self.assertIn(str(context.code_root), marker.read_text(encoding="utf-8"))
            self.assertNotIn("ProjectContext(", marker.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

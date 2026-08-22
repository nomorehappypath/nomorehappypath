# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import os
import pty
import shlex
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, contract, control
from harness.project_context import ProjectContext
from tests.environment_support import require_loopback


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_managed_agent.sh"


def free_port():
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close(); return port


class ControlTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def test_unknown_historical_session_kind_cannot_block_valid_agent_launches(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            damaged = control.create(root, "codex_delivery")
            with control.locked_state(root) as state:
                state["sessions"][damaged["id"]]["kind"] = "legacy_unknown_kind"
                state["sessions"][damaged["id"]]["provider"] = "claude"
            reviewer = control.create(root, "claude_reviewer")
            self.assertEqual(reviewer["kind"], "claude_reviewer")
            self.assertEqual(reviewer["status"], "launching")

    def test_runner_defaults_to_registered_project_even_from_isolated_harness(self):
        with TemporaryDirectory() as tmp:
            projects = Path(tmp) / "Projects"
            root = projects / "sample-project"
            root.mkdir(parents=True)
            capture = root / "captured.txt"
            working_directory = root / "working-directory.txt"
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "pwd > \"$HARNESS_WORKING_DIRECTORY\"\n"
                "printf '%s\\n' \"$@\" > \"$HARNESS_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            session = control.create(root, "codex_delivery")
            environment = {
                **os.environ,
                "HARNESS_CODEX_BIN": str(fake_codex),
                "HARNESS_CAPTURE": str(capture),
                "HARNESS_WORKING_DIRECTORY": str(working_directory),
                "HARNESS_CODE_ROOT": str(projects / "foreign-session"),
                "HARNESS_DATA_ROOT": str(projects / "foreign-session-data"),
                "HARNESS_WORKSPACE_ROOT": str(projects / "foreign-session-workspaces"),
            }
            process = subprocess.run(
                ["bash", str(RUNNER), "--root", str(root), "--session-id", session["id"], "--kind", session["kind"]],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(working_directory.read_text(encoding="utf-8").strip(), str(root))
            prompt = capture.read_text(encoding="utf-8")
            self.assertIn(f"Start from {root}", prompt)
            self.assertIn(f"--root {root}", prompt)
            self.assertIn(f"--data-root {root / '.harness'}", prompt)
            self.assertIn(f"--workspace-root {projects / '.harness-task-workspaces'}", prompt)

    def test_runner_prompt_routes_a_recovered_delivery_back_to_saved_task(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "captured.txt"
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$HARNESS_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            session = control.create(root, "codex_delivery")
            environment = {**os.environ, "HARNESS_CODEX_BIN": str(fake_codex), "HARNESS_CAPTURE": str(capture)}
            process = subprocess.run(
                ["bash", str(RUNNER), "--root", str(root), "--session-id", session["id"], "--kind", session["kind"]],
                env=environment, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            prompt = capture.read_text(encoding="utf-8")
            self.assertIn("already attached to a recovered task", prompt)
            self.assertIn("resume that preserved task and next action", prompt)

    def test_runner_uses_registered_code_root_and_explicit_board_context(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            code = base / "adopted project"; code.mkdir()
            data = base / "manager data"
            workspaces = base / "task workspaces"
            context = ProjectContext(code, data, workspaces)
            capture = base / "captured.txt"
            working_directory = base / "working-directory.txt"
            fake_codex = base / "fake-codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "pwd > \"$HARNESS_WORKING_DIRECTORY\"\n"
                "printf '%s\\n' \"$@\" > \"$HARNESS_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            session = control.create(context, "codex_delivery")
            completed = subprocess.run(
                [
                    "bash", str(RUNNER),
                    "--root", str(code),
                    "--data-root", str(data),
                    "--workspace-root", str(workspaces),
                    "--python", sys.executable,
                    "--session-id", session["id"],
                    "--kind", session["kind"],
                ],
                env={
                    **os.environ,
                    "HARNESS_CODEX_BIN": str(fake_codex),
                    "HARNESS_CAPTURE": str(capture),
                    "HARNESS_WORKING_DIRECTORY": str(working_directory),
                    "HARNESS_CODE_ROOT": str(base / "foreign-session"),
                    "HARNESS_DATA_ROOT": str(base / "foreign-data"),
                    "HARNESS_WORKSPACE_ROOT": str(base / "foreign-workspaces"),
                },
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(working_directory.read_text(encoding="utf-8").strip(), str(code))
            prompt = capture.read_text(encoding="utf-8")
            prefix = next(
                line.removeprefix("For every board command, start with: ")
                for line in prompt.splitlines()
                if line.startswith("For every board command, start with: ")
            )
            arguments = shlex.split(prefix)
            self.assertEqual(Path(arguments[arguments.index("--root") + 1]).resolve(), code.resolve())
            self.assertEqual(Path(arguments[arguments.index("--data-root") + 1]).resolve(), data.resolve())
            self.assertEqual(Path(arguments[arguments.index("--workspace-root") + 1]).resolve(), workspaces.resolve())
            self.assertTrue((data / "board" / "state.json").is_file())
            self.assertFalse((code / ".harness").exists())

    def test_agent_prompt_board_command_runs_from_operator_workspace(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            code = base / "adopted project"; code.mkdir()
            data = base / "manager data"
            workspaces = base / "task workspaces"
            execution_root = base / "operator workspace"; execution_root.mkdir()
            context = ProjectContext(code, data, workspaces)
            capture = base / "captured.txt"
            fake_codex = base / "fake-codex"
            fake_codex.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$HARNESS_CAPTURE\"\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            session = control.create(context, "codex_delivery")
            completed = subprocess.run(
                [
                    "bash", str(RUNNER),
                    "--root", str(code),
                    "--data-root", str(data),
                    "--workspace-root", str(workspaces),
                    "--python", sys.executable,
                    "--session-id", session["id"],
                    "--kind", session["kind"],
                ],
                env={
                    **os.environ,
                    "HARNESS_CODEX_BIN": str(fake_codex),
                    "HARNESS_CAPTURE": str(capture),
                    "HARNESS_EXECUTION_ROOT": str(execution_root),
                },
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            prompt = capture.read_text(encoding="utf-8")
            prefix = next(
                line.removeprefix("For every board command, start with: ")
                for line in prompt.splitlines()
                if line.startswith("For every board command, start with: ")
            )
            self.assertNotIn("-m harness.board", prefix)
            prefix_arguments = shlex.split(prefix)
            self.assertEqual(Path(prefix_arguments[0]), Path(sys.executable))
            self.assertEqual(prefix_arguments[1], "-E")
            self.assertEqual(Path(prefix_arguments[2]), ROOT / "harness" / "board.py")
            from_workspace = subprocess.run(
                [*prefix_arguments, "view"],
                cwd=execution_root,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PYTHONHOME": str(base / "foreign-python-home"),
                    "PYTHONPATH": str(base / "foreign-python-path"),
                    "PYTHONSTARTUP": str(base / "foreign-startup.py"),
                    "GIT_DIR": str(base / "foreign.git"),
                    "GIT_WORK_TREE": str(base / "foreign-work-tree"),
                },
            )
            self.assertEqual(from_workspace.returncode, 0, from_workspace.stderr)
            self.assertTrue((data / "board" / "state.json").is_file())
            self.assertFalse((code / ".harness").exists())

    def test_managed_launch_needs_no_shell_profile_and_keeps_explicit_provider_settings(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"; root.mkdir()
            home = Path(tmp) / "home"; home.mkdir()
            profile_sentinel = Path(tmp) / "profile-was-sourced"
            (home / ".bash_profile").write_text(
                f"touch {shlex.quote(str(profile_sentinel))}\nexport HARNESS_CODEX_BIN=/missing/from-profile\n",
                encoding="utf-8",
            )
            capture = Path(tmp) / "captured.txt"
            fake_codex = Path(tmp) / "explicit-codex"
            fake_codex.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$HARNESS_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            session = control.create(root, "codex_delivery")
            completed = subprocess.run(
                [
                    "/usr/bin/env", "-u", "BASH_ENV", "-u", "ENV",
                    "/bin/bash", "--noprofile", "--norc", str(RUNNER),
                    "--root", str(root), "--python", sys.executable,
                    "--session-id", session["id"], "--kind", session["kind"],
                ],
                env={
                    **os.environ,
                    "HOME": str(home),
                    "HARNESS_CODEX_BIN": str(fake_codex),
                    "HARNESS_CAPTURE": str(capture),
                },
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(profile_sentinel.exists())
            self.assertIn("Agent Directive", capture.read_text(encoding="utf-8"))

    def test_managed_provider_keeps_operator_git_identity_without_session_config_overrides(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"; root.mkdir()
            home = base / "home"; home.mkdir()
            (home / ".gitconfig").write_text(
                "[user]\n\tname = Configured Agent\n\temail = configured@example.invalid\n",
                encoding="utf-8",
            )
            (root / "tracked.txt").write_text("content\n", encoding="utf-8")
            subprocess.run(
                ["git", "init", "-q", "-b", "main"], cwd=root, check=True,
                capture_output=True, text=True,
            )
            capture = base / "author.txt"
            fake_codex = base / "commit-as-provider"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "git add tracked.txt\n"
                "git commit -qm 'managed provider commit'\n"
                "git log -1 --pretty='%an <%ae>' > \"$HARNESS_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            session = control.create(root, "codex_delivery")
            completed = subprocess.run(
                [
                    "/usr/bin/env", "-u", "BASH_ENV", "-u", "ENV",
                    "/bin/bash", "--noprofile", "--norc", str(RUNNER),
                    "--root", str(root), "--python", sys.executable,
                    "--session-id", session["id"], "--kind", session["kind"],
                ],
                env={
                    **os.environ,
                    "HOME": str(home),
                    "HARNESS_CODEX_BIN": str(fake_codex),
                    "HARNESS_CAPTURE": str(capture),
                    "HARNESS_EXECUTION_ROOT": str(root),
                },
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                capture.read_text(encoding="utf-8").strip(),
                "Configured Agent <configured@example.invalid>",
            )
            exported_git_config = [
                line.strip() for line in RUNNER.read_text(encoding="utf-8").splitlines()
                if line.strip().startswith("export GIT_CONFIG")
            ]
            self.assertEqual(exported_git_config, [])

    def test_fresh_runner_keeps_replacement_recovery_task_live_after_task_resumed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_session = control.create(root, "codex_delivery")
            source = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=source_session["id"])
            board.record_owner_direction(root, source_session["id"], "Keep the replacement Delivery task alive after recovery.")
            board.begin_task(root, source["id"], "TASK-FRESH-RECOVERY")
            board.task_brief(root, source["id"], "Keep the recovered task alive.", "Poll the board and continue the saved recovery action.")
            board.offline(root, source["id"], "visible CLI terminal ended", transport_ended=True)

            fake_codex = root / "fake-codex"
            fake_codex.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            replacement_session = control.create(root, "codex_delivery")
            environment = {**os.environ, "HARNESS_CODEX_BIN": str(fake_codex)}
            process = subprocess.Popen(
                ["bash", str(RUNNER), "--root", str(root), "--session-id", replacement_session["id"], "--kind", replacement_session["kind"]],
                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                replacement = None
                for _ in range(50):
                    agents = board.snapshot(root)["agents"].values()
                    replacement = next((item for item in agents if item.get("session_id") == replacement_session["id"]), None)
                    if replacement and control.snapshot(root)["sessions"][0]["status"] == "running":
                        break
                    time.sleep(.1)
                self.assertIsNotNone(replacement, "fresh runner did not register its Delivery agent")
                resumed = board.resume_task(root, replacement["id"], source["id"], "TASK-FRESH-RECOVERY")
                self.assertEqual(resumed["kind"], "task_resumed")
                live = board.snapshot(root)["agents"][replacement["id"]]
                self.assertTrue(live["active"])
                self.assertEqual(live["task"], "TASK-FRESH-RECOVERY")
                self.assertEqual(live["recovery_context"]["next_action"], "Poll the board and continue the saved recovery action.")
                time.sleep(.3)
                self.assertIsNone(process.poll(), "fresh replacement runner exited immediately after task_resumed")
            finally:
                if process.poll() is None:
                    control.stop(root, replacement_session["id"])
                    process.wait(timeout=4)

    def test_live_predecessor_terminal_is_stopped_only_after_replacement_recovers(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = root / "fake-codex"
            fake_codex.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(20)\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            environment = {**os.environ, "HARNESS_CODEX_BIN": str(fake_codex)}

            def start(session):
                return subprocess.Popen(
                    ["bash", str(RUNNER), "--root", str(root), "--session-id", session["id"], "--kind", session["kind"]],
                    env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )

            source_session = control.create(root, "codex_delivery")
            source_process = start(source_session)
            replacement_process = None
            try:
                source = None
                for _ in range(60):
                    source = next((item for item in board.snapshot(root)["agents"].values() if item.get("session_id") == source_session["id"]), None)
                    if source and control.snapshot(root)["sessions"][0]["status"] == "running":
                        break
                    time.sleep(.1)
                self.assertIsNotNone(source)
                board.record_owner_direction(root, source_session["id"], "Preserve this task while replacing its dead board intake.")
                board.begin_task(root, source["id"], "TASK-LIVE-PREDECESSOR")
                board.task_brief(root, source["id"], "Replace only the stale board intake.", "Continue from the preserved task state.")
                board.offline(root, source["id"], "simulated dead board intake", transport_ended=True)

                replacement_session = control.create(root, "codex_delivery")
                replacement_process = start(replacement_session)
                replacement = None
                for _ in range(60):
                    replacement = next((item for item in board.snapshot(root)["agents"].values() if item.get("session_id") == replacement_session["id"]), None)
                    managed = {item["id"]: item for item in control.snapshot(root)["sessions"]}
                    if replacement and managed[replacement_session["id"]]["status"] == "running":
                        break
                    time.sleep(.1)
                self.assertIsNotNone(replacement)
                board.resume_task(root, replacement["id"], source["id"], "TASK-LIVE-PREDECESSOR")
                source_process.wait(timeout=4)
                self.assertIsNone(replacement_process.poll())
                state = board.snapshot(root)
                task_owners = [agent for agent in state["agents"].values() if agent.get("active") and agent.get("task") == "TASK-LIVE-PREDECESSOR"]
                self.assertEqual([agent["id"] for agent in task_owners], [replacement["id"]])
                predecessor = next(item for item in control.snapshot(root)["sessions"] if item["id"] == source_session["id"])
                self.assertTrue(predecessor["read_only"])
                self.assertEqual(predecessor["status"], "stopped")
            finally:
                if source_process.poll() is None:
                    source_process.kill(); source_process.wait(timeout=3)
                if replacement_process is not None and replacement_process.poll() is None:
                    control.stop(root, replacement_session["id"])
                    replacement_process.wait(timeout=4)

    def test_control_stop_terminates_prompt_blocked_cli_after_launcher_exec_replacement(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'PROMPT_BLOCKED\n'\n"
                "while IFS= read -r _line; do :; done\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            session = control.create(root, "codex_delivery")
            master, slave = pty.openpty()
            environment = {**os.environ, "HARNESS_CODEX_BIN": str(fake_codex)}
            process = subprocess.Popen(
                ["bash", str(RUNNER), "--root", str(root), "--session-id", session["id"], "--kind", session["kind"]],
                env=environment, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
            )
            os.close(slave)
            try:
                for _ in range(40):
                    current = control.snapshot(root)["sessions"][0]
                    if current["status"] == "running":
                        break
                    time.sleep(.1)
                else:
                    self.fail("managed runner did not attach through the interactive supervisor")
                self.assertEqual(process.pid, current["pid"], "the control record must follow the exec-replaced supervisor PID")
                stopped_request = control.stop(root, session["id"])
                self.assertEqual(stopped_request["status"], "stopping")
                process.wait(timeout=4)
                stopped = control.snapshot(root)["sessions"][0]
                self.assertEqual(stopped["status"], "stopped")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                os.close(master)

    def test_only_one_cto_can_be_active_and_external_exit_resets_it(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = control.create(root, "claude_cto")
            with self.assertRaisesRegex(ValueError, "already active"):
                control.create(root, "claude_cto")
            attached = control.attach(root, first["id"], 99999999)
            self.assertEqual(attached["status"], "running")
            session = control.snapshot(root)["sessions"][0]
            self.assertEqual(session["status"], "exited")
            self.assertIn("outside the viewer", session["reason"])
            replacement = control.create(root, "claude_cto")
            self.assertEqual(replacement["status"], "launching")

    def test_visible_delivery_and_reviewer_sessions_are_limited_to_two_active_each(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delivery = [control.create(root, "codex_delivery") for _ in range(2)]
            reviewer = [control.create(root, "claude_reviewer") for _ in range(2)]
            with self.assertRaisesRegex(ValueError, "maximum 2 active CODEX CLI sessions"):
                control.create(root, "codex_delivery")
            with self.assertRaisesRegex(ValueError, "maximum 2 active CLAUDE CLI sessions"):
                control.create(root, "claude_reviewer")
            current = control.snapshot(root)
            self.assertEqual(current["active_counts"], {"codex_delivery": 2, "claude_reviewer": 2, "claude_cto": 0})
            self.assertEqual(current["limits"], {"codex_delivery": 2, "claude_reviewer": 2, "claude_cto": 1})
            control.stop(root, delivery[0]["id"])
            control.stop(root, reviewer[0]["id"])
            # The cap is on active sessions, not historical records.
            self.assertEqual(control.create(root, "codex_delivery")["status"], "launching")
            self.assertEqual(control.create(root, "claude_reviewer")["status"], "launching")

    def test_session_color_is_validated_and_persisted(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            blue = control.create(root, "codex_delivery", color="blue")
            self.assertEqual(blue["color"], "blue")
            self.assertEqual(blue["color_hex"], "#123B5D")
            self.assertEqual(control.snapshot(root)["sessions"][0]["color_label"], "Ocean blue")
            with self.assertRaisesRegex(ValueError, "unknown terminal color"):
                control.create(root, "claude_reviewer", color="neon")

    def test_terminal_launcher_applies_selected_rgb_and_black_fallback(self):
        with TemporaryDirectory() as tmp, patch("harness.board_viewer.subprocess.run") as run, patch("harness.board_viewer.sys.platform", "darwin"):
            root = Path(tmp)
            session = control.create(root, "codex_delivery", color="purple")
            board_viewer.launch_terminal(root, session)
            applescript = run.call_args.args[0][2]
            self.assertIn("set background color to {16962, 10023, 23130}", applescript)
            self.assertIn("set normal text color", applescript)
            black = control.create(root, "claude_reviewer", color="black")
            board_viewer.launch_terminal(root, black)
            black_script = run.call_args.args[0][2]
            self.assertIn("set background color to {0, 0, 0}", black_script)

    def test_capacity_limit_is_atomic_when_three_launches_race(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = []
            errors = []

            def launch():
                try:
                    results.append(control.create(root, "codex_delivery"))
                except ValueError as error:
                    errors.append(str(error))

            threads = [threading.Thread(target=launch) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(results), 2)
            self.assertEqual(len(errors), 1)
            self.assertIn("maximum 2 active CODEX CLI sessions", errors[0])

    def test_delivery_starts_without_a_task_and_rejects_task_injection(self):
        with TemporaryDirectory() as tmp:
            started = control.create(Path(tmp), "codex_delivery")
            self.assertEqual(started["task"], "")
            with self.assertRaisesRegex(ValueError, "start an agent role only"):
                control.create(Path(tmp), "codex_delivery", "Build the control panel")

    def test_control_instruction_queue_is_session_scoped_and_one_time(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            queued = control.enqueue_instruction(root, session["id"], "resume the failed review", "test")
            self.assertEqual(queued["source"], "test")
            self.assertEqual(control.instruction_receipt(root, queued["id"])["status"], "queued")
            self.assertEqual(len(control.take_instructions(root, session["id"])), 1)
            self.assertEqual(control.instruction_receipt(root, queued["id"])["status"], "taken")
            delivered = control.acknowledge_instruction(root, session["id"], queued["id"])
            self.assertEqual(delivered["status"], "delivered")
            self.assertTrue(delivered["delivered_at"])
            self.assertEqual(
                control.acknowledge_instruction(root, session["id"], queued["id"]),
                delivered,
            )
            self.assertEqual(control.take_instructions(root, session["id"]), [])

    def test_instruction_receipt_rejects_cross_session_and_ack_before_take(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = control.create(root, "codex_delivery")
            second = control.create(root, "codex_delivery")
            queued = control.enqueue_instruction(root, first["id"], "one scoped wake", "test")
            with self.assertRaisesRegex(ValueError, "before delivery"):
                control.acknowledge_instruction(root, first["id"], queued["id"])
            control.take_instructions(root, first["id"])
            with self.assertRaisesRegex(ValueError, "another managed session"):
                control.acknowledge_instruction(root, second["id"], queued["id"])

    def test_terminal_receipts_are_bounded_without_pruning_inflight_work(self):
        with TemporaryDirectory() as tmp, patch.object(control, "MAX_INSTRUCTION_RECEIPTS", 2):
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            receipt_ids = []
            for index in range(3):
                queued = control.enqueue_instruction(root, session["id"], f"wake {index}", "test")
                receipt_ids.append(queued["id"])
                control.take_instructions(root, session["id"])
                control.acknowledge_instruction(root, session["id"], queued["id"])
            with self.assertRaisesRegex(ValueError, "unknown instruction receipt"):
                control.instruction_receipt(root, receipt_ids[0])
            self.assertEqual(control.instruction_receipt(root, receipt_ids[-1])["status"], "delivered")

    def test_stop_terminates_a_real_runner_and_runner_receives_standing_by_directive(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "captured.txt"
            working_directory = root / "working-directory.txt"
            execution_root = root / "projects-workspace"
            execution_root.mkdir()
            fake_codex = root / "fake-codex"
            fake_codex.write_text("#!/usr/bin/env bash\npwd > \"$HARNESS_WORKING_DIRECTORY\"\nprintf '%s\\n' \"$@\" > \"$HARNESS_CAPTURE\"\nsleep 30\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            session = control.create(root, "codex_delivery")
            environment = {**os.environ, "HARNESS_CODEX_BIN": str(fake_codex), "HARNESS_CAPTURE": str(capture), "HARNESS_WORKING_DIRECTORY": str(working_directory), "HARNESS_EXECUTION_ROOT": str(execution_root)}
            process = subprocess.Popen(["bash", str(RUNNER), "--root", str(root), "--session-id", session["id"], "--kind", session["kind"]], env=environment)
            try:
                for _ in range(30):
                    current = control.snapshot(root)["sessions"][0]
                    if current["status"] == "running":
                        break
                    time.sleep(.1)
                else:
                    self.fail("runner did not attach")
                captured = ""
                for _ in range(30):
                    if capture.is_file() and working_directory.is_file():
                        captured = capture.read_text(encoding="utf-8")
                        if "Agent Directive" in captured and "No owner direction has been supplied yet" in captured and f"The target project is {root}" in captured:
                            break
                    time.sleep(.1)
                else:
                    self.fail("fake Codex command did not finish writing its prompt and working-directory sentinels")
                self.assertIn("Agent Directive", captured)
                self.assertIn("No owner direction has been supplied yet", captured)
                self.assertEqual(working_directory.read_text(encoding="utf-8").strip(), str(execution_root))
                self.assertIn(f"The target project is {root}", captured)
                control.stop(root, session["id"])
                process.wait(timeout=3)
                stopped = control.snapshot(root)["sessions"][0]
                self.assertEqual(stopped["status"], "stopped")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)

    def test_terminal_launch_is_hard_coded_without_owner_task_arguments(self):
        with TemporaryDirectory() as tmp, patch("harness.board_viewer.sys.platform", "darwin"), patch("harness.board_viewer.subprocess.run") as run:
            session = control.create(Path(tmp), "codex_delivery")
            board_viewer.launch_terminal(Path(tmp), session)
            command = run.call_args.args[0][-1]
            self.assertIn("run_managed_agent.sh", command)
            self.assertIn("--close-terminal-on-exit", command)
            self.assertNotIn("--task", command)
            self.assertNotIn(";", command)

    def test_terminal_launch_serializes_explicit_project_context_roots(self):
        with TemporaryDirectory() as tmp, patch("harness.board_viewer.sys.platform", "darwin"), patch("harness.board_viewer.subprocess.run") as run:
            base = Path(tmp)
            code = base / "adopted"; code.mkdir()
            data = base / "managed" / "alpha"; data.mkdir(parents=True)
            workspaces = base / "workspaces" / "alpha"; workspaces.mkdir(parents=True)
            context = ProjectContext(code, data, workspaces)
            session = control.create(context, "codex_delivery")
            board_viewer.launch_terminal(context, session)
            command = run.call_args.args[0][-1]
            arguments = shlex.split(command.removeprefix("exec "))
            self.assertFalse(any("ProjectContext(" in argument for argument in arguments))
            self.assertEqual(arguments[:9], [
                "/usr/bin/env", "-u", "BASH_ENV", "-u", "ENV",
                "/bin/bash", "--noprofile", "--norc", str(RUNNER),
            ])
            for flag, expected in (
                ("--root", context.code_root),
                ("--data-root", context.data_root),
                ("--workspace-root", context.workspace_root),
            ):
                value = Path(arguments[arguments.index(flag) + 1])
                self.assertEqual(value, expected)
                self.assertTrue(value.is_dir(), f"{flag} must name a real directory")
            python_path = Path(arguments[arguments.index("--python") + 1])
            self.assertEqual(python_path, Path(sys.executable))
            self.assertTrue(python_path.is_absolute())
            self.assertTrue(python_path.is_file())

    def test_runner_exit_outside_the_viewer_releases_the_cto_slot(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_claude = root / "fake-claude"
            fake_claude.write_text("#!/usr/bin/env bash\nsleep 30\n", encoding="utf-8")
            fake_claude.chmod(0o755)
            session = control.create(root, "claude_cto")
            process = subprocess.Popen(["bash", str(RUNNER), "--root", str(root), "--session-id", session["id"], "--kind", session["kind"]], env={**os.environ, "HARNESS_CLAUDE_BIN": str(fake_claude)})
            try:
                for _ in range(30):
                    if control.snapshot(root)["sessions"][0]["status"] == "running":
                        break
                    time.sleep(.1)
                else:
                    self.fail("runner did not attach")
                process.terminate()
                process.wait(timeout=3)
                released = control.snapshot(root)["sessions"][0]
                self.assertEqual(released["status"], "exited")
                self.assertIn("outside the viewer", released["reason"])
                self.assertEqual(control.create(root, "claude_cto")["status"], "launching")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)

    def test_viewer_control_api_shows_buttons_launches_and_stops(self):
        with TemporaryDirectory() as tmp, patch("harness.board_viewer.launch_terminal") as launch:
            root = Path(tmp)
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                html = urlopen(base + "/", timeout=3).read().decode()
                request = Request(base + "/api/sessions", data=b'{"kind":"claude_cto","task":"","color":"blue"}', headers={"Content-Type": "application/json"}, method="POST")
                response = urlopen(request, timeout=3)
                session = __import__("json").loads(response.read())["session"]
                stop = Request(base + f"/api/sessions/{session['id']}/stop", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                stopped = __import__("json").loads(urlopen(stop, timeout=3).read())["session"]
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()
            self.assertIn("CODEX CLI", html)
            self.assertIn("CTO (CLAUDE)", html)
            self.assertIn("NoMoreHappyPath Mission Control", html)
            launch.assert_called_once()
            self.assertEqual(session["color"], "blue")
            self.assertEqual(stopped["status"], "stopped")

    def test_stop_all_api_cleans_every_unfinished_delivery_task_and_terminal(self):
        with TemporaryDirectory() as tmp, patch("harness.board_viewer.launch_terminal"):
            root = Path(tmp)
            tasks = []
            for index in range(2):
                session = control.create(root, "codex_delivery")
                agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
                task = f"STOP-ALL-{index}"
                board.record_owner_direction(root, session["id"], f"Build {task} and clean it if I stop all agents.")
                board.begin_task(root, agent["id"], task)
                contract.create_contract(root, task, f"Build {task} and clean it if I stop all agents.", ["delivery"])
                tasks.append(task)
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/api/sessions/stop-all",
                    data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
                )
                response = __import__("json").loads(urlopen(request, timeout=3).read())
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()
            self.assertEqual(response["stopped_sessions"], 2)
            self.assertEqual(response["cancelled_tasks"], tasks)
            self.assertEqual(control.snapshot(root)["active_counts"]["codex_delivery"], 0)
            self.assertEqual(board_viewer.dashboard_payload(root)["live_tasks"], [])
            self.assertEqual(board_viewer.history_payload(root)["task_history"], [])
            self.assertEqual(list((root / ".harness" / "tasks").glob("*.json")), [])

    def test_stopping_delivery_also_stops_its_associated_review_terminal(self):
        with TemporaryDirectory() as tmp, patch("harness.board_viewer.launch_terminal"):
            root = Path(tmp)
            delivery_session = control.create(root, "codex_delivery")
            delivery = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=delivery_session["id"])
            board.record_owner_direction(root, delivery_session["id"], "Cancel this entire unfinished project when I stop Delivery.")
            board.begin_task(root, delivery["id"], "STOP-WITH-REVIEWER")
            contract.create_contract(root, "STOP-WITH-REVIEWER", "Cancel this entire unfinished project when I stop Delivery.", ["delivery"])
            reviewer_session = control.create(root, "claude_reviewer")
            board.register(root, "qa", "STOP-WITH-REVIEWER", vendor="Anthropic", session_id=reviewer_session["id"])
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/api/sessions/{delivery_session['id']}/stop",
                    data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
                )
                response = __import__("json").loads(urlopen(request, timeout=3).read())
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()
            self.assertEqual({value["id"] for value in response["stopped_sessions"]}, {delivery_session["id"], reviewer_session["id"]})
            self.assertEqual(control.snapshot(root)["active_counts"], {"codex_delivery": 0, "claude_reviewer": 0, "claude_cto": 0})
            self.assertEqual(board_viewer.dashboard_payload(root)["live_tasks"], [])

    def test_session_api_explains_live_provider_collision_before_launch(self):
        with TemporaryDirectory() as tmp, patch("harness.board_viewer.launch_terminal") as launch:
            root = Path(tmp)
            control.create(root, "claude_reviewer")
            control.update_agent_settings(root, {
                "delivery": {"provider": "claude", "model": "sonnet", "effort": "high"},
                "cto": {"provider": "claude", "model": "opus", "effort": "high"},
                "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
            })
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/api/sessions",
                    data=b'{"kind":"codex_delivery","color":"black"}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as failure:
                    urlopen(request, timeout=3)
                message = __import__("json").loads(failure.exception.read())["error"]
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()
            self.assertEqual(failure.exception.code, 400)
            self.assertIn("active Independent Reviewer is already using Claude", message)
            self.assertIn("independent review remains available", message)
            launch.assert_not_called()

    def test_agent_settings_have_role_defaults_persist_and_reject_invalid_values(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(control.agent_settings(root), {
                "delivery": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"},
                "cto": {"provider": "claude", "model": "opus", "effort": "high"},
                "reviewer": {"provider": "claude", "model": "opus", "effort": "max"},
            })
            updated = {
                "delivery": {"provider": "claude", "model": "sonnet", "effort": "medium"},
                "cto": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "low"},
                "reviewer": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"},
            }
            self.assertEqual(control.update_agent_settings(root, updated), updated)
            self.assertEqual(control.agent_settings(root), updated)
            with self.assertRaisesRegex(ValueError, "unsupported provider"):
                control.update_agent_settings(root, {**updated, "cto": {"provider": "invalid", "model": "anything", "effort": "high"}})
            with self.assertRaisesRegex(ValueError, "unsupported effort"):
                control.update_agent_settings(root, {**updated, "cto": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "turbo"}})
            with self.assertRaisesRegex(ValueError, "model must be"):
                control.update_agent_settings(root, {**updated, "cto": {"provider": "codex", "model": "bad model; command", "effort": "high"}})
            same_vendor = dict(updated)
            same_vendor["reviewer"] = {"provider": "claude", "model": "opus", "effort": "xhigh"}
            with self.assertRaisesRegex(ValueError, "different providers"):
                control.update_agent_settings(root, same_vendor)

    def test_legacy_launch_settings_requires_an_explicit_override(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(TypeError, "settings_override"):
                control.launch_settings(root, "codex_delivery")
            with self.assertRaisesRegex(ValueError, "explicit settings override"):
                control.launch_settings(root, "codex_delivery", settings_override=None)

    def test_legacy_launch_settings_uses_override_instead_of_project_settings(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_local = {
                "delivery": {"provider": "claude", "model": "opus", "effort": "max"},
                "cto": {"provider": "claude", "model": "opus", "effort": "high"},
                "reviewer": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"},
            }
            manager_global = {
                "delivery": {"provider": "codex", "model": "gpt-5.6-luna", "effort": "low"},
                "cto": {"provider": "claude", "model": "sonnet", "effort": "medium"},
                "reviewer": {"provider": "claude", "model": "haiku", "effort": "low"},
            }
            control.update_agent_settings(root, project_local)
            selected = control.launch_settings(
                root, "codex_delivery", settings_override=manager_global,
            )
            self.assertEqual(selected, {
                "role": "delivery",
                "provider": "codex",
                "model": "gpt-5.6-luna",
                "effort": "low",
                "provider_label": "Codex",
            })

    def test_control_resolve_requires_a_managed_session(self):
        with TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable, str(ROOT / "harness" / "control.py"),
                    "--root", tmp, "resolve", "--kind", "codex_delivery",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("--session-id", completed.stderr)
            self.assertIn("required", completed.stderr)

    def test_live_delivery_reviewer_provider_collision_is_refused_at_session_capture(self):
        cases = (
            ("claude_reviewer", "codex_delivery", "Independent Reviewer", "Claude"),
            ("codex_delivery", "claude_reviewer", "Delivery Agent", "Codex"),
        )
        for first_kind, blocked_kind, active_role, provider_label in cases:
            with self.subTest(blocked_kind=blocked_kind), TemporaryDirectory() as tmp:
                root = Path(tmp)
                active = control.create(root, first_kind)
                control.update_agent_settings(root, {
                    "delivery": {"provider": "claude", "model": "sonnet", "effort": "high"},
                    "cto": {"provider": "claude", "model": "opus", "effort": "high"},
                    "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
                })
                with self.assertRaisesRegex(
                    ValueError,
                    rf"active {active_role} is already using {provider_label}.*independent review remains available",
                ):
                    control.create(root, blocked_kind)
                control.stop(root, active["id"])
                recovered = control.create(root, blocked_kind)
                self.assertEqual(recovered["provider"], provider_label.lower())

    def test_legacy_settings_without_models_receive_explicit_provider_defaults(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with control.locked_state(root) as state:
                state["agent_settings"] = {
                    "delivery": {"provider": "codex", "effort": "high"},
                    "cto": {"provider": "claude", "effort": "high"},
                    "reviewer": {"provider": "claude", "effort": "max"},
                }
            migrated = control.agent_settings(root)
            self.assertEqual(migrated["delivery"]["model"], "gpt-5.6-sol")
            self.assertEqual(migrated["cto"]["model"], "opus")
            self.assertEqual(migrated["reviewer"]["model"], "opus")

    def test_effort_names_are_normalized_to_each_cli(self):
        self.assertEqual(control.normalize_provider_effort("claude", "xhigh"), "max")
        self.assertEqual(control.normalize_provider_effort("codex", "max"), "xhigh")
        self.assertIn("max", control.PROVIDER_EFFORTS["claude"])
        self.assertNotIn("xhigh", control.PROVIDER_EFFORTS["claude"])
        self.assertIn("xhigh", control.PROVIDER_EFFORTS["codex"])

    def test_runner_uses_selected_provider_model_and_effort_for_delivery(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "capture.txt"
            fake_claude = root / "fake-claude"
            fake_claude.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$HARNESS_CAPTURE\"\n", encoding="utf-8")
            fake_claude.chmod(0o755)
            settings = control.agent_settings(root)
            settings["delivery"] = {"provider": "claude", "model": "sonnet", "effort": "max"}
            settings["reviewer"] = {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"}
            control.update_agent_settings(root, settings)
            session = control.create(root, "codex_delivery")
            process = subprocess.run(
                ["bash", str(RUNNER), "--root", str(root), "--session-id", session["id"], "--kind", session["kind"]],
                env={**os.environ, "HARNESS_CLAUDE_BIN": str(fake_claude), "HARNESS_CAPTURE": str(capture)},
                capture_output=True, text=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            arguments = capture.read_text(encoding="utf-8")
            self.assertIn("--model\nsonnet", arguments)
            self.assertIn("--effort\nmax", arguments)
            self.assertEqual(control.snapshot(root)["sessions"][0]["provider"], "claude")
            self.assertEqual(control.snapshot(root)["sessions"][0]["model"], "sonnet")

    def test_runner_preserves_selected_provider_model_and_effort_for_every_role(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = root / "fake-codex"
            fake_claude = root / "fake-claude"
            fake_cli = "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$HARNESS_CAPTURE\"\n"
            fake_codex.write_text(fake_cli, encoding="utf-8")
            fake_claude.write_text(fake_cli, encoding="utf-8")
            fake_codex.chmod(0o755)
            fake_claude.chmod(0o755)
            settings = {
                "delivery": {"provider": "claude", "model": "claude-fable-5[1m]", "effort": "max"},
                "cto": {"provider": "codex", "model": "gpt-5.6-sol-wm", "effort": "low"},
                "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
            }
            control.update_agent_settings(root, settings)
            cases = (
                ("codex_delivery", "claude-fable-5[1m]", "max", "--effort", "MODE: Delivery Agent."),
                ("claude_cto", "gpt-5.6-sol-wm", "low", "model_reasoning_effort=low", "global CTO"),
                ("claude_reviewer", "gpt-5.6-terra", "xhigh", "model_reasoning_effort=xhigh", "MODE: Independent Reviewer."),
            )
            for kind, model, effort, effort_argument, prompt_marker in cases:
                with self.subTest(kind=kind):
                    capture = root / f"{kind}-arguments.txt"
                    session = control.create(root, kind)
                    completed = subprocess.run(
                        ["bash", str(RUNNER), "--root", str(root), "--session-id", session["id"], "--kind", kind],
                        env={
                            **os.environ,
                            "HARNESS_CODEX_BIN": str(fake_codex),
                            "HARNESS_CLAUDE_BIN": str(fake_claude),
                            "HARNESS_CAPTURE": str(capture),
                        },
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    arguments = capture.read_text(encoding="utf-8")
                    self.assertIn(f"--model\n{model}", arguments)
                    self.assertIn(effort_argument, arguments)
                    self.assertIn(prompt_marker, arguments)
                    self.assertIn(str(ROOT / "harness" / "board.py"), arguments)
                    self.assertNotIn("python3 -m harness.board", arguments)
                    recorded = next(item for item in control.snapshot(root)["sessions"] if item["id"] == session["id"])
                    self.assertEqual((recorded["provider"], recorded["model"], recorded["effort"]),
                                     (settings[control.role_for_kind(kind)]["provider"], model, effort))

    def test_session_launch_keeps_model_selected_when_button_was_clicked(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            changed = control.agent_settings(root)
            changed["delivery"] = {"provider": "codex", "model": "gpt-5.6-terra", "effort": "low"}
            control.update_agent_settings(root, changed)
            captured = control.session_launch_settings(root, session["id"], "codex_delivery")
            self.assertEqual(captured["model"], "gpt-5.6-sol")
            self.assertEqual(captured["effort"], "high")

    def test_settings_api_exposes_and_updates_all_roles(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                initial = __import__("json").loads(urlopen(base + "/api/settings", timeout=3).read())
                self.assertEqual(set(initial["settings"]), {"delivery", "cto", "reviewer"})
                updated = {
                    "delivery": {"provider": "claude", "model": "sonnet", "effort": "medium"},
                    "cto": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"},
                    "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
                }
                request = Request(base + "/api/settings", data=__import__("json").dumps({"settings": updated}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                response = __import__("json").loads(urlopen(request, timeout=3).read())
                self.assertEqual(response["settings"], updated)
                page = urlopen(base + "/", timeout=3).read().decode()
                self.assertNotIn("Agent settings", page)
                from harness import project_manager_page
                self.assertIn("Global agent defaults", project_manager_page.PAGE)
                self.assertIn("Save agent settings", project_manager_page.PAGE)
                self.assertIn('data-page="help"', project_manager_page.PAGE)
                self.assertIn("Frame A Good Task", project_manager_page.PAGE)
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()

    def test_corrupt_persisted_settings_keep_get_routes_usable_and_allow_post_recovery(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with control.locked_state(root) as state:
                state["agent_settings"] = {
                    "delivery": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"},
                    "cto": {"provider": "claude", "model": "opus", "effort": "high"},
                    "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
                }
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                settings = __import__("json").loads(urlopen(base + "/api/settings", timeout=3).read())
                control_view = __import__("json").loads(urlopen(base + "/api/control", timeout=3).read())
                self.assertEqual(settings["settings"], control.default_agent_settings())
                self.assertEqual(control_view["agent_settings"], control.default_agent_settings())
                recovered = {
                    "delivery": {"provider": "claude", "model": "opus", "effort": "high"},
                    "cto": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"},
                    "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
                }
                request = Request(base + "/api/settings", data=__import__("json").dumps({"settings": recovered}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                self.assertEqual(__import__("json").loads(urlopen(request, timeout=3).read())["settings"], recovered)
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()

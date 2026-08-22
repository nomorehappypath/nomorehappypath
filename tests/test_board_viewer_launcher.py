# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import os
import json
import shutil
import socket
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from harness import board


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "start_board_viewer.sh"


def free_port():
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close(); return port


class BoardViewerLauncherTests(unittest.TestCase):
    def test_watchdog_threshold_constants_match_board_and_launcher(self):
        source = SCRIPT.read_text()
        self.assertEqual(board.AGENT_STALE_SECONDS, 240)
        self.assertEqual(board.WATCHDOG_INTERVAL_SECONDS, 15)
        self.assertIn(f"agent_stale_after={board.AGENT_STALE_SECONDS}", source)
        self.assertIn(f"watchdog_interval={board.WATCHDOG_INTERVAL_SECONDS}", source)
        self.assertNotIn("git -C", source)
        self.assertIn("python3 -E", source)
        self.assertIn("curl --disable --noproxy '*'", source)

    def test_running_viewer_restarts_when_released_harness_revision_changes(self):
        with TemporaryDirectory() as tmp:
            harness_root = Path(tmp) / "harness-root"
            shutil.copytree(ROOT / "harness", harness_root / "harness")
            (harness_root / "scripts").mkdir(parents=True)
            shutil.copy2(SCRIPT, harness_root / "scripts" / "start_board_viewer.sh")
            subprocess.run(["git", "init", "-b", "main"], cwd=harness_root, check=True, capture_output=True)
            subprocess.run(["git", "add", "harness", "scripts"], cwd=harness_root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Harness Test", "-c", "user.email=harness@example.invalid", "commit", "-m", "initial"],
                cwd=harness_root, check=True, capture_output=True,
            )
            port = free_port()
            process = subprocess.Popen(
                ["bash", "scripts/start_board_viewer.sh", "--root", str(harness_root), "--port", str(port), "--no-open"],
                cwd=harness_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                first_version = None
                for _ in range(60):
                    try:
                        first_version = json.loads(urlopen(f"http://127.0.0.1:{port}/api/dashboard", timeout=.2).read())["viewer_version"]
                        break
                    except OSError:
                        time.sleep(.1)
                self.assertIsNotNone(first_version, process.stderr.read() if process.poll() is not None else "viewer did not start")

                viewer_source = harness_root / "harness" / "board_viewer.py"
                source = viewer_source.read_text()
                self.assertIn("NoMoreHappyPath Mission Control", source)
                viewer_source.write_text(source.replace("NoMoreHappyPath Mission Control", "NoMoreHappyPath Mission Control refreshed", 1))
                subprocess.run(["git", "add", "harness/board_viewer.py"], cwd=harness_root, check=True)
                subprocess.run(
                    ["git", "-c", "user.name=Harness Test", "-c", "user.email=harness@example.invalid", "commit", "-m", "released viewer"],
                    cwd=harness_root, check=True, capture_output=True,
                )

                refreshed_version = first_version
                for _ in range(80):
                    try:
                        refreshed_version = json.loads(urlopen(f"http://127.0.0.1:{port}/api/dashboard", timeout=.2).read())["viewer_version"]
                        if refreshed_version != first_version:
                            break
                    except OSError:
                        pass
                    time.sleep(.1)
                self.assertNotEqual(refreshed_version, first_version)
                self.assertIsNone(process.poll(), "the supervisor must remain alive after replacing the viewer child")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=4)
                process.stdout.close(); process.stderr.close()

    def test_shell_launcher_uses_harness_root_when_started_from_its_scripts_folder(self):
        with TemporaryDirectory() as tmp:
            harness_root = Path(tmp) / "harness-root"
            (harness_root / "scripts").mkdir(parents=True)
            shutil.copy2(SCRIPT, harness_root / "scripts" / "start_board_viewer.sh")
            os.symlink(ROOT / "harness", harness_root / "harness", target_is_directory=True)
            port = free_port()
            process = subprocess.Popen(["bash", "start_board_viewer.sh", "--port", str(port), "--no-open"], cwd=harness_root / "scripts", stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                for _ in range(30):
                    try:
                        html = urlopen(f"http://127.0.0.1:{port}/", timeout=.2).read().decode()
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    self.fail(process.stderr.read())
                time.sleep(3.2)
                if process.poll() is not None:
                    self.fail(process.stderr.read())
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=3)
                process.stdout.close(); process.stderr.close()
            self.assertIn("NoMoreHappyPath Mission Control", html)
            self.assertTrue((harness_root / ".harness" / "board" / "BOARD.md").is_file())

    def test_shell_launcher_bootstraps_and_serves_board_without_browser_open(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            code = base / "adopted"; code.mkdir()
            data = base / "manager-data"
            workspaces = base / "task-workspaces"
            port = free_port()
            process = subprocess.Popen(
                [
                    "bash", str(SCRIPT),
                    "--root", str(code),
                    "--data-root", str(data),
                    "--workspace-root", str(workspaces),
                    "--port", str(port),
                    "--no-open",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                for _ in range(30):
                    try:
                        html = urlopen(f"http://127.0.0.1:{port}/", timeout=.2).read().decode()
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    self.fail(process.stderr.read())
                time.sleep(3.2)
                if process.poll() is not None:
                    self.fail(process.stderr.read())
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=3)
                process.stdout.close(); process.stderr.close()
            self.assertIn("NoMoreHappyPath Mission Control", html)
            self.assertTrue((data / "board" / "BOARD.md").is_file())
            self.assertFalse((code / ".harness").exists())

    def test_shell_launcher_has_help(self):
        result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Starts the live Harness board", result.stdout)

    def test_shell_launcher_rejects_an_invalid_port(self):
        result = subprocess.run(["bash", str(SCRIPT), "--port", "invalid"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--port requires a number", result.stderr)

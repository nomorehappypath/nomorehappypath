# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Process-level tests for the one-address harness_next launcher."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import project_registry as registry


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "start_project_manager.sh"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def port_is_bindable(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class ProjectManagerLauncherTests(unittest.TestCase):
    def test_help_and_port_collision_fail_before_launch(self):
        help_result = subprocess.run(
            ["bash", str(SCRIPT), "--help"], capture_output=True, text=True,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("one stable browser address", help_result.stdout)
        collision = subprocess.run(
            ["bash", str(SCRIPT), "--port", "19001", "--worker-port", "19001"],
            capture_output=True, text=True,
        )
        self.assertEqual(collision.returncode, 2)
        self.assertIn("must be different", collision.stderr)

    def test_open_project_stays_on_manager_origin_and_shutdown_releases_both_ports(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            code = base / "adopted"
            code.mkdir()
            entry = registry.register(
                home, "Launcher project", code, kind="adopted",
                description="Launcher isolation proof.",
            )
            manager_port, worker_port = free_port(), free_port()
            while worker_port == manager_port:
                worker_port = free_port()
            process = subprocess.Popen(
                [
                    "bash", str(SCRIPT), "--home", str(home),
                    "--port", str(manager_port), "--worker-port", str(worker_port),
                    "--no-open",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            origin = f"http://127.0.0.1:{manager_port}"
            try:
                for _ in range(80):
                    try:
                        page = urlopen(origin + "/", timeout=0.2).read().decode()
                        if "NoMoreHappyPath" in page:
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail(process.stderr.read() if process.poll() is not None else "manager did not start")
                request = Request(
                    origin + f"/api/projects/{entry['id']}/open", data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                opened = json.loads(urlopen(request, timeout=10).read())
                self.assertEqual(opened["board_url"], origin + "/project/")
                project_page = urlopen(opened["board_url"], timeout=3).read().decode()
                self.assertIn('const apiPrefix="/project";', project_page)
                self.assertNotIn(f"127.0.0.1:{worker_port}", project_page)
                with self.assertRaises(HTTPError) as direct:
                    urlopen(f"http://127.0.0.1:{worker_port}/", timeout=3)
                self.assertEqual(direct.exception.code, 403)
                listing = json.loads(urlopen(origin + "/api/projects", timeout=3).read())
                self.assertNotIn("board_port", json.dumps(listing))
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                process.stdout.close()
                process.stderr.close()
            self.assertTrue(port_is_bindable(manager_port), "public manager port was orphaned")
            self.assertTrue(port_is_bindable(worker_port), "private worker port was orphaned")

    def test_source_refresh_replaces_manager_and_worker_without_port_orphans(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            installed = base / "installed"
            shutil.copytree(ROOT / "harness", installed / "harness")
            (installed / "scripts").mkdir()
            shutil.copy2(SCRIPT, installed / "scripts" / SCRIPT.name)
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=installed, check=True,
                capture_output=True,
            )
            subprocess.run(["git", "add", "harness", "scripts"], cwd=installed, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Harness Test",
                    "-c", "user.email=harness@example.invalid",
                    "commit", "-m", "installed runtime",
                ], cwd=installed, check=True, capture_output=True,
            )
            home = base / "home"
            code = base / "adopted"
            code.mkdir()
            entry = registry.register(home, "Refresh project", code, kind="adopted")
            manager_port, worker_port = free_port(), free_port()
            process = subprocess.Popen(
                [
                    "bash", str(installed / "scripts" / SCRIPT.name),
                    "--home", str(home), "--port", str(manager_port),
                    "--worker-port", str(worker_port), "--no-open",
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            origin = f"http://127.0.0.1:{manager_port}"
            try:
                for _ in range(80):
                    try:
                        initial = json.loads(urlopen(origin + "/api/ready", timeout=0.2).read())
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail("manager did not start")
                request = Request(
                    origin + f"/api/projects/{entry['id']}/open", data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                json.loads(urlopen(request, timeout=10).read())
                self.assertFalse(port_is_bindable(worker_port))

                identity_source = installed / "harness" / "runtime_identity.py"
                identity_source.write_text(
                    identity_source.read_text(encoding="utf-8") + "\n# refresh test\n",
                    encoding="utf-8",
                )
                # The refresh is deferred while the project is open: the same
                # runtime keeps serving.
                time.sleep(2.5)
                held = initial
                for _ in range(20):
                    try:
                        held = json.loads(urlopen(origin + "/api/ready", timeout=2).read())
                        break
                    except OSError:
                        time.sleep(0.3)
                self.assertEqual(
                    held["runtime"]["source_digest"], initial["runtime"]["source_digest"],
                )
                close_request = Request(
                    origin + f"/api/projects/{entry['id']}/close", data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                try:
                    json.loads(urlopen(close_request, timeout=10).read())
                except OSError:
                    # The released restart may race the close response; the
                    # refresh assertions below prove the close took effect.
                    pass
                refreshed = initial
                for _ in range(240):
                    try:
                        refreshed = json.loads(urlopen(origin + "/api/ready", timeout=0.2).read())
                        if refreshed["runtime"]["source_digest"] != initial["runtime"]["source_digest"]:
                            break
                    except OSError:
                        pass
                    time.sleep(0.1)
                self.assertNotEqual(
                    refreshed["runtime"]["source_digest"], initial["runtime"]["source_digest"],
                )
                self.assertIsNone(refreshed["worker"])
                reopened = json.loads(urlopen(request, timeout=10).read())
                self.assertEqual(reopened["board_url"], origin + "/project/")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                process.stdout.close()
                process.stderr.close()
            self.assertTrue(port_is_bindable(manager_port))
            self.assertTrue(port_is_bindable(worker_port))

    def test_commit_only_refresh_prevents_stale_manager_new_worker_runtime_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            installed = base / "installed"
            shutil.copytree(ROOT / "harness", installed / "harness")
            (installed / "scripts").mkdir()
            shutil.copy2(SCRIPT, installed / "scripts" / SCRIPT.name)
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=installed, check=True,
                capture_output=True,
            )
            subprocess.run(["git", "add", "harness", "scripts"], cwd=installed, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Harness Test",
                    "-c", "user.email=harness@example.invalid",
                    "commit", "-m", "installed runtime",
                ], cwd=installed, check=True, capture_output=True,
            )
            home = base / "home"
            code = base / "adopted"
            code.mkdir()
            entry = registry.register(home, "Commit refresh", code, kind="adopted")
            manager_port, worker_port = free_port(), free_port()
            process = subprocess.Popen(
                [
                    "bash", str(installed / "scripts" / SCRIPT.name),
                    "--home", str(home), "--port", str(manager_port),
                    "--worker-port", str(worker_port), "--no-open",
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            origin = f"http://127.0.0.1:{manager_port}"
            request = Request(
                origin + f"/api/projects/{entry['id']}/open", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST",
            )
            try:
                initial = None
                for _ in range(80):
                    try:
                        initial = json.loads(urlopen(origin + "/api/ready", timeout=0.2).read())
                        break
                    except OSError:
                        time.sleep(0.1)
                self.assertIsNotNone(initial, "manager did not start")
                opened = json.loads(urlopen(request, timeout=10).read())
                self.assertEqual(opened["runtime"]["commit"], initial["runtime"]["commit"])

                (installed / "release-note.txt").write_text(
                    "Commit identity changed without changing executable Harness bytes.\n",
                    encoding="utf-8",
                )
                subprocess.run(["git", "add", "release-note.txt"], cwd=installed, check=True)
                subprocess.run(
                    [
                        "git", "-c", "user.name=Harness Test",
                        "-c", "user.email=harness@example.invalid",
                        "commit", "-m", "commit-only release",
                    ], cwd=installed, check=True, capture_output=True,
                )
                expected = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=installed, text=True,
                ).strip()
                time.sleep(2.5)
                held = initial
                for _ in range(20):
                    try:
                        held = json.loads(urlopen(origin + "/api/ready", timeout=2).read())
                        break
                    except OSError:
                        time.sleep(0.3)
                self.assertEqual(held["runtime"]["commit"], initial["runtime"]["commit"])
                close_request = Request(
                    origin + f"/api/projects/{entry['id']}/close", data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                try:
                    json.loads(urlopen(close_request, timeout=10).read())
                except OSError:
                    # The released restart may race the close response; the
                    # refresh assertions below prove the close took effect.
                    pass
                refreshed = initial
                for _ in range(240):
                    try:
                        refreshed = json.loads(urlopen(origin + "/api/ready", timeout=0.2).read())
                        if refreshed["runtime"]["commit"] == expected:
                            break
                    except OSError:
                        pass
                    time.sleep(0.1)
                self.assertEqual(refreshed["runtime"]["commit"], expected)
                self.assertEqual(
                    refreshed["runtime"]["source_digest"],
                    initial["runtime"]["source_digest"],
                )
                self.assertIsNone(refreshed["worker"])
                reopened = json.loads(urlopen(request, timeout=10).read())
                self.assertEqual(reopened["runtime"]["commit"], expected)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                process.stdout.close()
                process.stderr.close()
            self.assertTrue(port_is_bindable(manager_port))
            self.assertTrue(port_is_bindable(worker_port))


if __name__ == "__main__":
    unittest.main()

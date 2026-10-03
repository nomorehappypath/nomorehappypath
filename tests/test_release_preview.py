# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Candidate previews for releases awaiting the owner's visual test.

Run: PYTHONPATH=. python3 -m unittest tests.test_release_preview -v
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

from harness import board, release_preview, workspace_settings
from tests.environment_support import require_loopback


SERVE_SCRIPT = """#!/usr/bin/env python3
import http.server, sys
from pathlib import Path

port = int(sys.argv[sys.argv.index("--port") + 1])
version = Path(__file__).resolve().parent / "version.txt"

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = version.read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        return

http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *arguments], capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    return result.stdout.strip()


class PreviewFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = base / "project"
        self.root.mkdir()
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        _git(self.workspace, "init", "-q")
        (self.workspace / "serve.py").write_text(SERVE_SCRIPT)
        (self.workspace / "version.txt").write_text("candidate-one")
        _git(self.workspace, "add", "serve.py", "version.txt")
        _git(self.workspace, "commit", "-qm", "candidate one")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")
        self.supervisor = release_preview.ReleasePreviewSupervisor(self.root)
        self.addCleanup(self.supervisor.shutdown)

    def seed_release(self, head_commit: str | None = None):
        with board.locked_state(self.root) as state:
            state.setdefault("releases", {})["TASK"] = {
                "task": "TASK",
                "status": "VISUAL_TEST_REQUIRED",
                "head_commit": head_commit or self.commit,
                "cto_id": "cto-0001-test",
                "recorded_at": board.now(),
            }
            state.setdefault("task_workspaces", {})["TASK"] = str(self.workspace)
            state.setdefault("task_branches", {})["TASK"] = {
                "task": "TASK",
                "branch": "refs/heads/harness/tasks/TASK/task",
                "repository": str(self.workspace),
            }

    def configure_command(self, command: str = "python3 serve.py --port {port}"):
        workspace_settings.update_preview(self.root, {
            "command": command,
            "url_template": "http://127.0.0.1:{port}/",
            "startup_timeout_seconds": 30,
        })

    def release(self) -> dict:
        return board.snapshot(self.root)["releases"]["TASK"]


class UnconfiguredReleaseTests(PreviewFixture):
    def test_pending_release_without_command_records_candidate_location(self):
        self.seed_release()
        self.supervisor.tick()
        preview = self.release()["preview"]
        self.assertEqual(preview["status"], "unconfigured")
        self.assertEqual(preview["workspace"], str(self.workspace))
        self.assertEqual(preview["branch"], "harness/tasks/TASK/task")
        self.assertEqual(preview["head_commit"], self.commit)

    def test_paused_project_records_nothing(self):
        self.seed_release()
        with board.locked_state(self.root) as state:
            state["project_pause"] = {"status": "paused"}
        report = self.supervisor.tick()
        self.assertTrue(report["paused"])
        self.assertNotIn("preview", self.release())


class SkippedPreviewTests(PreviewFixture):
    """The owner can say there is nothing to run; the acceptance step proceeds on the files."""

    def test_a_skipped_preview_is_kept_and_never_overwritten_by_the_supervisor(self):
        self.seed_release()
        self.supervisor.tick()
        self.assertEqual(self.release()["preview"]["status"], "unconfigured")
        recorded = board.record_release_preview(self.root, "TASK", {
            "status": "skipped", "head_commit": self.commit, "workspace": str(self.workspace),
            "branch": "harness/tasks/TASK/task", "skipped_at": board.now(),
        })
        self.assertEqual(recorded["status"], "skipped")
        self.supervisor.tick(); self.supervisor.tick()
        preview = self.release()["preview"]
        self.assertEqual(preview["status"], "skipped", "a later tick must not put the box back")
        self.assertEqual(preview["workspace"], str(self.workspace))
        self.assertTrue(preview["skipped_at"])
        # Changing their mind clears it; the next tick offers the setup box again.
        board.clear_release_preview(self.root, "TASK")
        self.supervisor.tick()
        self.assertEqual(self.release()["preview"]["status"], "unconfigured")

    def test_skip_needs_a_release_awaiting_the_owner(self):
        with self.assertRaises(ValueError):
            board.record_release_preview(self.root, "NOPE", {"status": "skipped"})


class RunningPreviewTests(PreviewFixture):
    def test_preview_serves_the_exact_reviewed_commit(self):
        self.seed_release()
        self.configure_command()
        self.supervisor.tick()
        preview = self.release()["preview"]
        self.assertEqual(preview["status"], "ready", preview)
        with urlopen(preview["url"], timeout=5) as response:
            self.assertEqual(response.read().decode(), "candidate-one")
        # The workspace moved ahead, but the preview still serves the
        # reviewed commit because it runs from a detached clone.
        (self.workspace / "version.txt").write_text("workspace-moved")
        with urlopen(preview["url"], timeout=5) as response:
            self.assertEqual(response.read().decode(), "candidate-one")

    def test_ready_preview_emits_owner_visible_event_once(self):
        self.seed_release()
        self.configure_command()
        self.supervisor.tick()
        self.supervisor.tick()
        events = [
            event for event in board.snapshot(self.root).get("events", [])
            if event.get("kind") == "release_preview_ready"
        ]
        self.assertEqual(len(events), 1)
        self.assertIn(self.release()["preview"]["url"], events[0]["message"])

    def test_owner_decision_stops_the_preview_process(self):
        self.seed_release()
        self.configure_command()
        self.supervisor.tick()
        preview = self.release()["preview"]
        pid = preview["pid"]
        with board.locked_state(self.root) as state:
            state.setdefault("release_decisions", {})["TASK"] = {"decision": "accepted"}
        self.supervisor.tick()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            alive = subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0
            if not alive:
                break
            time.sleep(0.1)
        self.assertNotEqual(subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode, 0)
        self.assertFalse((release_preview.preview_root(self.root) / "TASK").exists())

    def test_superseding_commit_restarts_the_preview_on_the_new_candidate(self):
        self.seed_release()
        self.configure_command()
        self.supervisor.tick()
        first = self.release()["preview"]
        (self.workspace / "version.txt").write_text("candidate-two")
        _git(self.workspace, "add", "version.txt")
        _git(self.workspace, "commit", "-qm", "candidate two")
        new_commit = _git(self.workspace, "rev-parse", "HEAD")
        self.seed_release(new_commit)
        self.supervisor.tick()
        second = self.release()["preview"]
        self.assertEqual(second["status"], "ready", second)
        self.assertEqual(second["head_commit"], new_commit)
        with urlopen(second["url"], timeout=5) as response:
            self.assertEqual(response.read().decode(), "candidate-two")
        self.assertNotEqual(subprocess.run(["kill", "-0", str(first["pid"])], capture_output=True).returncode, 0)


class FailingPreviewTests(PreviewFixture):
    def test_command_failure_records_error_and_log_tail_without_retry_loop(self):
        self.seed_release()
        self.configure_command("python3 -c 'import sys; sys.stderr.write(\"boom: missing dependency\\n\"); sys.exit(3)'")
        self.supervisor.tick()
        preview = self.release()["preview"]
        self.assertEqual(preview["status"], "failed")
        self.assertIn("exited", preview["error"])
        self.assertIn("boom: missing dependency", preview.get("log_tail", ""))
        events = [
            event for event in board.snapshot(self.root).get("events", [])
            if event.get("kind") == "release_preview_failed"
        ]
        self.assertEqual(len(events), 1)
        # The same failing command is not relaunched every tick.
        self.supervisor.tick()
        self.assertEqual(len([
            event for event in board.snapshot(self.root).get("events", [])
            if event.get("kind") == "release_preview_failed"
        ]), 1)

    def test_clearing_the_preview_allows_a_fresh_attempt(self):
        self.seed_release()
        self.configure_command("python3 -c 'raise SystemExit(1)'")
        self.supervisor.tick()
        self.assertEqual(self.release()["preview"]["status"], "failed")
        board.clear_release_preview(self.root, "TASK")
        self.configure_command()
        self.supervisor.tick()
        self.assertEqual(self.release()["preview"]["status"], "ready")

    def test_missing_workspace_is_reported_not_raised(self):
        self.seed_release()
        with board.locked_state(self.root) as state:
            state["task_workspaces"]["TASK"] = str(Path(self.temporary.name) / "gone")
        self.configure_command()
        self.supervisor.tick()
        preview = self.release()["preview"]
        self.assertEqual(preview["status"], "failed")
        self.assertIn("workspace", preview["error"])


STUDIO_APP = """import http.server, json, os
from pathlib import Path
here = Path(__file__).resolve().parent
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "version": (here / "version.txt").read_text(),
            "settings": (here / "settings.json").read_text() if (here / "settings.json").exists() else None,
            "env": (here / ".env").read_text() if (here / ".env").exists() else None,
            "workspace": sorted(os.listdir(here / ".workspace")) if (here / ".workspace").is_dir() else None,
            "browser": os.environ.get("BROWSER"),
            "port": os.environ.get("PORT"),
        }).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_):
        return
http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
"""


class ViewAppTests(PreviewFixture):
    """Backlog #12: the owner's View app click starts the reviewed candidate."""

    def setUp(self):
        require_loopback()
        super().setUp()
        (self.workspace / "app.py").write_text(STUDIO_APP)
        (self.workspace / "version.txt").write_text("candidate-studio")
        _git(self.workspace, "add", "app.py", "version.txt")
        _git(self.workspace, "commit", "-qm", "studio candidate")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")
        # The owner's own local settings, which the repository never tracks.
        (self.root / "settings.json").write_text('{"owner": "gerard"}')
        (self.root / ".env").write_text("RELAY_TOKEN=owner-secret\n")
        (self.root / ".workspace").mkdir()
        (self.root / ".workspace" / "client-a").mkdir()

    def start(self) -> dict:
        self.seed_release()
        self.assertEqual(release_preview.request_view(self.root, "TASK"), {"status": "starting"})
        self.supervisor.tick()
        return self.release()["preview"]

    def test_view_app_starts_the_reviewed_commit_with_the_owners_settings(self):
        preview = self.start()
        self.assertEqual(preview["status"], "ready", preview)
        self.assertEqual(preview["command"], "python3 app.py")
        with urlopen(preview["url"], timeout=5) as response:
            served = json.loads(response.read())
        self.assertEqual(served["version"], "candidate-studio")
        self.assertEqual(served["settings"], '{"owner": "gerard"}')
        self.assertEqual(served["env"], "RELAY_TOKEN=owner-secret\n")
        self.assertEqual(served["workspace"], ["client-a"])
        self.assertEqual(served["port"], preview["url"].rsplit(":", 1)[1].strip("/"))
        self.assertEqual(served["browser"], "true", "the app must not open a second tab of its own")
        source = release_preview.preview_root(self.root) / "TASK" / "source"
        self.assertEqual(_git(source, "rev-parse", "HEAD"), self.commit)
        self.assertEqual(release_preview.request_view(self.root, "TASK"), {"status": "ready", "url": preview["url"]})

    def test_the_candidate_works_on_copies_of_the_owners_files(self):
        preview = self.start()
        self.assertEqual(preview["status"], "ready", preview)
        source = release_preview.preview_root(self.root) / "TASK" / "source"
        for name in ("settings.json", ".env", ".workspace"):
            self.assertFalse((source / name).is_symlink(), name)
        (source / "settings.json").unlink()
        (source / ".env").write_text("changed by the candidate")
        __import__("shutil").rmtree(source / ".workspace")
        self.assertEqual((self.root / "settings.json").read_text(), '{"owner": "gerard"}')
        self.assertEqual((self.root / ".env").read_text(), "RELAY_TOKEN=owner-secret\n")
        self.assertTrue((self.root / ".workspace" / "client-a").is_dir())

    def test_a_tracked_file_in_the_candidate_is_never_overwritten(self):
        (self.workspace / "settings.json").write_text('{"candidate": true}')
        _git(self.workspace, "add", "settings.json")
        _git(self.workspace, "commit", "-qm", "tracked settings")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")
        preview = self.start()
        with urlopen(preview["url"], timeout=5) as response:
            self.assertEqual(json.loads(response.read())["settings"], '{"candidate": true}')

    def test_a_failed_start_is_recorded_once_and_the_next_click_tries_again(self):
        (self.workspace / "app.py").write_text("import sys\nprint('ImportError: no module named flask')\nsys.exit(1)\n")
        _git(self.workspace, "commit", "-qam", "broken")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")
        preview = self.start()
        self.assertEqual(preview["status"], "failed", preview)
        self.assertEqual(preview["error"], "the preview command exited before serving its URL")
        self.assertIn("no module named flask", preview["log_tail"])
        recorded_at = preview["recorded_at"]
        self.supervisor.tick()
        self.assertEqual(self.release()["preview"]["recorded_at"], recorded_at, "no retry loop")
        self.assertEqual(release_preview.request_view(self.root, "TASK"), {"status": "starting"})
        self.supervisor.tick()
        self.assertEqual(self.release()["preview"]["status"], "failed")

    def test_a_failed_studio_start_gives_the_owner_the_full_command_to_run_it(self):
        (self.workspace / "app.py").write_text("import sys\nsys.exit(1)\n")
        _git(self.workspace, "commit", "-qam", "broken")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")
        preview = self.start()
        source = release_preview.preview_root(self.root) / "TASK" / "source"
        self.assertRegex(preview["run_yourself"], r"^cd " + str(source).replace(".", r"\.") + r" && PORT=\d+ python3 app\.py$")
        self.assertTrue((source / "settings.json").is_file(), "the command runs with the owner's settings copied in")

    def test_nothing_to_start_says_so_in_one_plain_line(self):
        _git(self.workspace, "rm", "-q", "app.py")
        _git(self.workspace, "commit", "-qm", "files only")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")
        preview = self.start()
        self.assertEqual(preview["status"], "failed")
        self.assertIn("nothing here we know how to start", preview["error"])

    def test_the_owners_decision_stops_the_running_candidate(self):
        preview = self.start()
        pid = preview["pid"]
        with board.locked_state(self.root) as state:
            state.setdefault("release_decisions", {})["TASK"] = {"decision": "not_accepted"}
        self.supervisor.tick()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0:
            time.sleep(0.1)
        self.assertNotEqual(subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode, 0)
        with self.assertRaisesRegex(ValueError, "no longer waiting"):
            release_preview.request_view(self.root, "TASK")

    def test_a_stale_supervisor_pass_never_erases_the_owners_click(self):
        self.seed_release()
        release_preview.request_view(self.root, "TASK")
        board.record_release_preview(self.root, "TASK", {"status": "unconfigured", "head_commit": self.commit})
        preview = self.release()["preview"]
        self.assertEqual((preview["status"], preview["requested"]), ("starting", "view_app"))

    def test_the_click_wakes_the_supervisor_without_waiting_for_a_tick(self):
        slow = release_preview.ReleasePreviewSupervisor(self.root, tick_seconds=3600)
        self.addCleanup(slow.shutdown)
        slow.start()
        self.seed_release()
        release_preview.request_view(self.root, "TASK")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and self.release()["preview"].get("status") != "ready":
            time.sleep(0.2)
        self.assertEqual(self.release()["preview"]["status"], "ready")

    def test_view_app_endpoint_starts_and_refuses_while_paused(self):
        from tests.test_release_preview import EndpointTests
        self.seed_release()
        base = EndpointTests.serve(self)
        status, body = EndpointTests.post(self, base, "/api/releases/TASK/view-app", {})
        self.assertEqual((status, body), (200, {"status": "starting"}))
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        status, _body = EndpointTests.post(self, base, "/api/releases/TASK/view-app", {}, expect_error=True)
        self.assertEqual(status, 409)


# The owner's real failing app (2026-10-02, project temp, zip-temperature-web)
# read its port from its own variable with a fixed default and ignored PORT.
OWN_PORT_APP = """import http.server, os
port = int(os.environ.get("OWN_APP_PORT", "8765"))

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"zip temperature"
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_):
        return

server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
print(f"listening on http://127.0.0.1:{port}", flush=True)
server.serve_forever()
"""

NEVER_SERVES_APP = """import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "view-app-orphan-probe"])
print("computing", flush=True)
time.sleep(120)
"""


def _free_loopback_port() -> int:
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ViewAppOwnPortTests(PreviewFixture):
    """Owner, 2026-10-02: "so view app crashes" - the app ignored PORT."""

    def setUp(self):
        require_loopback()
        super().setUp()
        self.own_port = _free_loopback_port()
        patcher = mock.patch.dict(os.environ, {"OWN_APP_PORT": str(self.own_port)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def commit_app(self, source: str) -> None:
        (self.workspace / "app.py").write_text(source)
        _git(self.workspace, "add", "app.py")
        _git(self.workspace, "commit", "-qm", "candidate app")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")

    def start(self) -> dict:
        self.seed_release()
        self.assertEqual(release_preview.request_view(self.root, "TASK"), {"status": "starting"})
        self.supervisor.tick()
        return self.release()["preview"]

    def test_an_app_that_ignores_port_is_found_where_it_really_listens(self):
        self.commit_app(OWN_PORT_APP)
        started = time.monotonic()
        preview = self.start()
        self.assertEqual(preview["status"], "ready", preview)
        self.assertEqual(preview["url"], f"http://127.0.0.1:{self.own_port}/")
        self.assertLess(time.monotonic() - started, 30, "found without waiting out the startup timeout")
        with urlopen(preview["url"], timeout=5) as response:
            self.assertEqual(response.read(), b"zip temperature")

    def test_a_port_held_by_another_program_says_so_plainly(self):
        self.commit_app(OWN_PORT_APP)
        import socket
        holder = socket.socket()
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", self.own_port))
        holder.listen()
        preview = self.start()
        self.assertEqual(preview["status"], "failed", preview)
        self.assertIn("already being used by another program", preview["error"])
        self.assertIn("a copy of this app you started yourself", preview["error"])
        self.assertIn("press View app again", preview["error"])

    def test_an_app_that_never_serves_says_what_to_fix_and_leaves_nothing_behind(self):
        workspace_settings.update_preview(self.root, {"command": "", "startup_timeout_seconds": 5})
        self.commit_app(NEVER_SERVES_APP)
        preview = self.start()
        self.assertEqual(preview["status"], "failed", preview)
        self.assertIn("never opened a web page", preview["error"])
        self.assertIn("PORT", preview["error"])
        self.assertTrue(preview["run_yourself"], "the run-it-yourself command stays")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _probe_processes():
            time.sleep(0.2)
        self.assertEqual(_probe_processes(), [], "the failed attempt left a process behind")


class ListeningUrlTests(unittest.TestCase):
    """The port scan View app uses, on both platforms' code paths."""

    def test_the_proc_scan_finds_loopback_listeners_of_the_given_pids_only(self):
        from harness.platform_support import linux
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "net").mkdir()
            header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            (proc / "net" / "tcp").write_text(header + "".join([
                "   0: 0100007F:223D 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 1111 1\n",  # 127.0.0.1:8765 LISTEN
                "   1: 0A00000A:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 2222 1\n",  # 10.0.0.10:8080, not loopback
                "   2: 0100007F:1F91 0100007F:9999 01 00000000:00000000 00:00000000 00000000  1000 0 3333 1\n",  # established, not LISTEN
                "   3: 00000000:1F92 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 4444 1\n",  # wildcard, other pid
            ]))
            (proc / "net" / "tcp6").write_text(header)
            fd = proc / "42" / "fd"
            fd.mkdir(parents=True)
            for number, inode in enumerate(("1111", "2222", "3333")):
                os.symlink(f"socket:[{inode}]", fd / str(number))
            os.symlink("/dev/null", fd / "9")
            identity = linux._ProcProcessIdentity()
            with mock.patch.object(linux._ProcProcessIdentity, "PROC", proc):
                self.assertEqual(identity.listening_urls([42]), ["http://127.0.0.1:8765/"])
                self.assertEqual(identity.listening_urls([]), [])

    def test_the_lsof_scan_reads_loopback_and_wildcard_listeners(self):
        from harness.platform_support import defaults
        output = "p42\nf5\nn127.0.0.1:8765\nf6\nn*:9000\nf7\nn192.168.1.5:7000\nf8\nn[::1]:9100\n"
        completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
        with mock.patch.object(defaults.subprocess, "run", return_value=completed):
            self.assertEqual(defaults._ProcessIdentity().listening_urls([42]), [
                "http://127.0.0.1:8765/", "http://127.0.0.1:9000/", "http://[::1]:9100/",
            ])
        with mock.patch.object(defaults.subprocess, "run", side_effect=OSError("no lsof")):
            self.assertEqual(defaults._ProcessIdentity().listening_urls([42]), [])


def _probe_processes() -> list[str]:
    listing = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    return [line for line in listing.splitlines() if "view-app-orphan-probe" in line and "ps -axo" not in line]


class ViewAppConfiguredCommandTests(PreviewFixture):
    """Review r1 B1: a project with a configured preview command, clicked via View app."""

    def setUp(self):
        require_loopback()
        super().setUp()

    def test_a_configured_command_click_reaches_ready_with_its_url(self):
        self.seed_release()
        self.configure_command()
        self.assertEqual(release_preview.request_view(self.root, "TASK"), {"status": "starting"})
        self.supervisor.tick()
        self.supervisor.tick()
        preview = self.release()["preview"]
        self.assertEqual(preview["status"], "ready", preview)
        self.assertTrue(preview["url"])
        with urlopen(preview["url"], timeout=5) as response:
            self.assertEqual(response.read().decode(), "candidate-one")
        self.assertEqual(release_preview.request_view(self.root, "TASK"), {"status": "ready", "url": preview["url"]})

    def test_a_click_on_an_already_running_configured_preview_does_not_stick(self):
        self.seed_release()
        self.configure_command()
        self.supervisor.tick()                      # the configured preview starts on its own
        url = self.release()["preview"]["url"]
        with board.locked_state(self.root) as state:  # a click that lands while it still reads "starting"
            state["releases"]["TASK"]["preview"].update({"status": "starting", "requested": "view_app"})
        self.supervisor.tick()
        self.assertEqual((self.release()["preview"]["status"], self.release()["preview"]["url"]), ("ready", url))

    def test_a_failed_configured_command_stays_visible_and_the_next_click_retries(self):
        self.seed_release()
        self.configure_command("python3 -c 'import sys; sys.exit(3)'")
        self.supervisor.tick()
        self.assertEqual(self.release()["preview"]["status"], "failed")
        self.assertEqual(release_preview.request_view(self.root, "TASK"), {"status": "starting"})
        self.supervisor.tick()
        preview = self.release()["preview"]
        self.assertEqual(preview["status"], "failed", "a failed retry must not hide behind 'starting'")
        self.assertEqual(release_preview.request_view(self.root, "TASK"), {"status": "starting"}, "and it can be retried again")

    def test_a_failed_start_gives_the_owner_the_full_command_to_run_it(self):
        # Owner, 2026-09-28 14:07: "if running the app crashes, let the mission
        # control give the full command like what you do".
        self.seed_release()
        self.configure_command("python3 -c 'import sys; sys.exit(3)' --port {port}")
        self.supervisor.tick()
        preview = self.release()["preview"]
        source = release_preview.preview_root(self.root) / "TASK" / "source"
        self.assertTrue(source.is_dir(), "the clean checkout stays for the owner to run")
        self.assertRegex(
            preview["run_yourself"],
            r"^cd " + str(source).replace(".", r"\.") + r" && python3 -c 'import sys; sys.exit\(3\)' --port \d+$",
        )


class RecordGuardTests(PreviewFixture):
    def test_preview_requires_a_release_awaiting_the_owner(self):
        with self.assertRaisesRegex(ValueError, "awaiting the owner"):
            board.record_release_preview(self.root, "TASK", {"status": "ready"})
        self.seed_release()
        with self.assertRaisesRegex(ValueError, "current release candidate"):
            board.record_release_preview(self.root, "TASK", {"status": "ready", "head_commit": "f" * 40})
        with self.assertRaisesRegex(ValueError, "status must be one of"):
            board.record_release_preview(self.root, "TASK", {"status": "sideways"})

    def test_clear_requires_the_release(self):
        with self.assertRaisesRegex(ValueError, "awaiting the owner"):
            board.clear_release_preview(self.root, "TASK")


class PreviewSettingsTests(unittest.TestCase):
    def test_defaults_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            self.assertEqual(workspace_settings.load(root)["preview"], workspace_settings.DEFAULT_PREVIEW)
            saved = workspace_settings.update_preview(root, {"command": "run.sh --port {port}"})
            self.assertEqual(saved["command"], "run.sh --port {port}")
            self.assertEqual(workspace_settings.load(root)["preview"]["command"], "run.sh --port {port}")
            stored = json.loads(workspace_settings.settings_path(root).read_text())
            self.assertEqual(stored["preview"]["command"], "run.sh --port {port}")

    def test_validation_refuses_remote_urls_and_bad_timeouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            lookalikes = (
                "http://example.com:{port}/",
                "http://127.0.0.1@evil.example:{port}/",
                "http://127.0.0.1.evil.example:{port}/",
                "https://127.0.0.1:{port}/",
                "http://localhost.evil.example:{port}/",
            )
            for template in lookalikes:
                with self.subTest(template=template):
                    with self.assertRaisesRegex(ValueError, "127.0.0.1 or localhost"):
                        workspace_settings.update_preview(root, {"command": "x", "url_template": template})
            for template in ("http://127.0.0.1:{port}/", "http://localhost:{port}/preview"):
                with self.subTest(template=template):
                    saved = workspace_settings.update_preview(root, {"command": "x", "url_template": template})
                    self.assertEqual(saved["url_template"], template)
            with self.assertRaisesRegex(ValueError, "contain \\{port\\}"):
                workspace_settings.update_preview(root, {"command": "x", "url_template": "http://127.0.0.1:9000/"})
            with self.assertRaisesRegex(ValueError, "between 5 and 300"):
                workspace_settings.update_preview(root, {"command": "x", "startup_timeout_seconds": 2})
            with self.assertRaisesRegex(ValueError, "2000 characters"):
                workspace_settings.update_preview(root, {"command": "y" * 2001})


class EndpointTests(PreviewFixture):
    def setUp(self):
        require_loopback()
        super().setUp()

    def serve(self):
        from harness import board_viewer
        import threading
        server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def post(self, base: str, path: str, payload: dict, expect_error: bool = False):
        from urllib.error import HTTPError
        from urllib.request import Request
        request = Request(
            base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            if not expect_error:
                raise
            return error.code, json.loads(error.read())

    def test_preview_command_saves_even_while_the_project_is_paused(self):
        self.seed_release()
        with board.locked_state(self.root) as state:
            state["project_pause"] = {"status": "paused"}
        base = self.serve()
        status, value = self.post(base, "/api/settings/preview", {"command": "run.sh --port {port}"})
        self.assertEqual(status, 200)
        self.assertEqual(value["preview"]["command"], "run.sh --port {port}")
        self.assertEqual(workspace_settings.load(self.root)["preview"]["command"], "run.sh --port {port}")

    def test_invalid_preview_settings_return_a_clear_error(self):
        base = self.serve()
        status, value = self.post(
            base, "/api/settings/preview",
            {"command": "x", "url_template": "http://example.com:{port}/"}, expect_error=True,
        )
        self.assertEqual(status, 400)
        self.assertIn("127.0.0.1", value["error"])

    def test_preview_retry_clears_the_recorded_failure(self):
        self.seed_release()
        board.record_release_preview(self.root, "TASK", {
            "status": "failed", "error": "boom", "head_commit": self.commit,
        })
        base = self.serve()
        status, value = self.post(base, "/api/releases/TASK/preview-retry", {})
        self.assertEqual(status, 200)
        self.assertNotIn("preview", board.snapshot(self.root)["releases"]["TASK"])

    def test_no_preview_needed_records_skipped_and_is_refused_while_paused(self):
        self.seed_release()
        base = self.serve()
        status, value = self.post(base, "/api/releases/TASK/preview-skip", {})
        self.assertEqual(status, 200)
        self.assertEqual(value["preview"]["status"], "skipped")
        self.assertEqual(value["preview"]["workspace"], str(self.workspace))
        self.assertEqual(value["preview"]["branch"], "harness/tasks/TASK/task")
        self.assertTrue(value["preview"]["skipped_at"])
        recorded = board.snapshot(self.root)["releases"]["TASK"]["preview"]
        self.assertEqual(recorded["status"], "skipped")
        # Changing their mind goes through the same retry the failed state uses.
        status, _ = self.post(base, "/api/releases/TASK/preview-retry", {})
        self.assertEqual(status, 200)
        self.assertNotIn("preview", board.snapshot(self.root)["releases"]["TASK"])
        # A paused project is read-only for this too.
        with board.locked_state(self.root) as state:
            state["project_pause"] = {"status": "paused"}
        status, value = self.post(base, "/api/releases/TASK/preview-skip", {}, expect_error=True)
        self.assertEqual(status, 409)
        self.assertIn("paused", value["error"])

    def test_skip_without_a_release_awaiting_the_owner_is_a_clear_error(self):
        base = self.serve()
        status, value = self.post(base, "/api/releases/NOPE/preview-skip", {}, expect_error=True)
        self.assertIn(status, (400, 404))
        self.assertIn("awaiting the owner", value["error"])

    def test_settings_payload_includes_preview_section(self):
        base = self.serve()
        with urlopen(base + "/api/settings", timeout=10) as response:
            value = json.loads(response.read())
        self.assertEqual(value["preview"], workspace_settings.DEFAULT_PREVIEW)


class AppBundleTests(PreviewFixture):
    def setUp(self):
        require_loopback()
        super().setUp()

    def make_bundle(self, name="Weather"):
        bundle = self.workspace / "src-tauri" / "target" / "release" / "bundle" / "macos" / f"{name}.app"
        (bundle / "Contents" / "MacOS").mkdir(parents=True)
        (bundle / "Contents" / "MacOS" / name.lower()).write_text("#!/bin/sh\n")
        return bundle

    def test_built_desktop_app_is_recorded_with_an_open_action(self):
        bundle = self.make_bundle()
        self.seed_release()
        self.supervisor.tick()
        preview = self.release()["preview"]
        self.assertEqual(preview["status"], "app_bundle")
        self.assertEqual(preview["app_path"], str(bundle))
        self.assertEqual(preview["app_name"], "Weather")
        self.assertIn("built_at", preview)
        self.assertNotIn("pid", preview)

    def test_configured_command_takes_precedence_over_the_bundle(self):
        self.make_bundle()
        self.seed_release()
        self.configure_command()
        self.supervisor.tick()
        self.assertEqual(self.release()["preview"]["status"], "ready")

    def test_open_endpoint_opens_only_the_recorded_bundle(self):
        from unittest import mock
        self.make_bundle()
        self.seed_release()
        self.supervisor.tick()
        with mock.patch.object(release_preview.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            result = release_preview.open_app_bundle(self.root, "TASK")
        self.assertTrue(result["opened"])
        self.assertEqual(result["app_name"], "Weather")
        opened = run.call_args[0][0]
        self.assertEqual(opened[0], "/usr/bin/open")
        self.assertTrue(opened[1].endswith("Weather.app"))

    def test_open_endpoint_refuses_without_a_recorded_bundle(self):
        self.seed_release()
        self.supervisor.tick()
        with self.assertRaisesRegex(ValueError, "no built app bundle"):
            release_preview.open_app_bundle(self.root, "TASK")
        with self.assertRaisesRegex(ValueError, "no built app bundle"):
            release_preview.open_app_bundle(self.root, "OTHER-TASK")

    def test_open_endpoint_refuses_a_removed_bundle(self):
        import shutil as _shutil
        bundle = self.make_bundle()
        self.seed_release()
        self.supervisor.tick()
        _shutil.rmtree(bundle)
        with self.assertRaisesRegex(ValueError, "no longer present"):
            release_preview.open_app_bundle(self.root, "TASK")

    def test_http_open_endpoint_serves_the_recorded_bundle(self):
        from unittest import mock
        self.make_bundle()
        self.seed_release()
        self.supervisor.tick()
        from harness import board_viewer
        import threading
        server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        from urllib.request import Request
        with mock.patch.object(release_preview.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            request = Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/releases/TASK/open-app",
                data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
            )
            with urlopen(request, timeout=10) as response:
                value = json.loads(response.read())
        self.assertTrue(value["opened"])


class ClearedCommandTests(PreviewFixture):
    def test_clearing_the_command_stops_the_running_preview(self):
        self.seed_release()
        self.configure_command()
        self.supervisor.tick()
        preview = self.release()["preview"]
        self.assertEqual(preview["status"], "ready")
        pid = preview["pid"]
        workspace_settings.update_preview(self.root, {"command": ""})
        self.supervisor.tick()
        cleared = self.release()["preview"]
        self.assertEqual(cleared["status"], "unconfigured")
        self.assertNotIn("pid", cleared)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0:
                break
            time.sleep(0.1)
        self.assertNotEqual(
            subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode, 0,
            "cleared preview process must be stopped, not orphaned",
        )
        self.assertFalse((release_preview.preview_root(self.root) / "TASK").exists())


class PageContentTests(PreviewFixture):
    def rendered(self) -> str:
        from harness import board_viewer
        return board_viewer.rendered_page("Project", "", "", "", "", "", None, "")

    def test_board_page_offers_project_access_but_no_agent_settings_dialog(self):
        page = self.rendered()
        self.assertNotIn('id="settings"', page)
        self.assertNotIn("settings-dialog", page)
        self.assertIn("AI access for this project", page)
        self.assertIn("configured automatically", page)

    def test_board_page_renders_the_candidate_preview_block(self):
        page = self.rendered()
        self.assertIn("releasePreviewHtml", page)
        self.assertIn("/view-app", page)
        # Backlog #12: no command or port box in the owner's view.
        self.assertNotIn("/api/settings/preview", page)
        self.assertNotIn("preview-command", page)
        self.assertNotIn("savePreviewCommand", page)

    def test_manager_settings_page_selects_models_without_provider_access(self):
        from harness import project_manager_page
        page = project_manager_page.PAGE
        self.assertIn("data-setting-model-choice", page)
        self.assertIn("Custom model ID", page)
        self.assertNotIn('id="provider-access"', page)


class ReleaseCardRenderTests(unittest.TestCase):
    """The owner-facing release card renders each preview state via the real page JS."""

    def render_card(self, release: dict) -> str:
        from harness import board_viewer
        script = board_viewer.rendered_page().split("<script>", 1)[1].split("</script>", 1)[0]
        declarations = script.split("el('#status-dialog-close')", 1)[0]
        state = {"releases": {"TASK": release}, "release_decisions": {}, "release_repairs": {},
                 "git_acceptances": {}, "remote_push_instructions": {}, "remote_push_outcomes": {}}
        invocation = (
            f"const html=releaseResponseHtml({json.dumps(state)},'TASK');"
            "process.stdout.write(JSON.stringify({html}));"
        )
        completed = subprocess.run(
            ["node", "-e", declarations + "\n" + invocation], capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)["html"]

    def release(self, preview: dict) -> dict:
        return {"task": "TASK", "status": "VISUAL_TEST_REQUIRED",
                "head_commit": "bb424e30557befc2e834a33b3902bfe578434d3a",
                "runtime_verification_deferred_to_target_acceptance": True,
                "owner_test_steps": ["Open the settings menu"], "preview": preview}

    def assert_one_view_app_button(self, html: str) -> None:
        self.assertEqual(html.count('class="preview-link view-app'), 1, html)
        self.assertIn("viewApp(", html)
        for gone in ("preview-command", "<input", "{port}", "Run it for me", "No preview needed",
                     "savePreviewCommand", "retryPreview", "openAppPreview"):
            self.assertNotIn(gone, html)

    def test_every_preview_state_renders_one_view_app_button_and_no_command_box(self):
        # Backlog #12 (owner): "just a button view app to verify".
        states = {
            "none": {},
            "unconfigured": {"status": "unconfigured", "workspace": "/tmp/workspace",
                             "branch": "harness/tasks/TASK/task", "suggested_command": "python3 -m http.server {port}"},
            "skipped": {"status": "skipped", "workspace": "/tmp/workspace"},
            "ready": {"status": "ready", "url": "http://127.0.0.1:8977/"},
            "app_bundle": {"status": "app_bundle", "app_path": "/x/Weather.app", "app_name": "Weather"},
            "failed": {"status": "failed", "error": "the preview command exited before serving its URL"},
        }
        for name, preview in states.items():
            with self.subTest(state=name):
                html = self.render_card(self.release(preview))
                self.assert_one_view_app_button(html)
                self.assertIn("View app", html)
                self.assertIn("bb424e3055", html)
                self.assertIn("Accepted", html)

    def test_candidate_location_stays_visible(self):
        html = self.render_card(self.release({
            "status": "unconfigured", "workspace": "/tmp/workspace", "branch": "harness/tasks/TASK/task",
        }))
        self.assertIn("Where the delivered work is:", html)
        self.assertIn("harness/tasks/TASK/task", html)
        self.assertIn("/tmp/workspace", html)

    def test_failed_start_is_one_plain_line_with_the_log_behind_it(self):
        html = self.render_card(self.release({
            "status": "failed", "requested": "view_app",
            "error": "the preview command exited before serving its URL",
            "log_tail": "ModuleNotFoundError: flask",
        }))
        self.assertIn("The app could not start: the preview command exited before serving its URL", html)
        self.assertIn("<summary>Show the log</summary>", html)
        self.assertIn("ModuleNotFoundError: flask", html)
        self.assertIn(">View app</button>", html, "the next click tries again")

    def test_a_failed_start_shows_the_command_to_paste_into_terminal(self):
        html = self.render_card(self.release({
            "status": "failed", "requested": "view_app", "error": "the preview command exited before serving its URL",
            "run_yourself": "cd /tmp/candidate/source && PORT=8931 python3 app.py",
        }))
        self.assertIn("cd /tmp/candidate/source &amp;&amp; PORT=8931 python3 app.py", html)
        self.assertIn("Paste it into Terminal and press Return.", html)
        self.assertIn(">Copy</button>", html)

    def test_a_requested_start_shows_a_spinner_and_elapsed_seconds(self):
        html = self.render_card(self.release({
            "status": "starting", "requested": "view_app", "requested_at": "2026-09-28T10:00:00+00:00",
        }))
        self.assertIn('class="spin"', html)
        self.assertIn("disabled", html)
        self.assertRegex(html, r"Starting the app… \d+ s")

    def test_app_bundle_names_are_shown_but_never_their_path(self):
        html = self.render_card(self.release({"status": "app_bundle", "app_path": "/x/Weather.app", "app_name": "Weather"}))
        self.assertNotIn("/x/Weather.app", html)


if __name__ == "__main__":
    unittest.main()


class SuggestedCommandTests(unittest.TestCase):
    def test_candidate_authored_code_is_never_suggested(self):
        # Review finding: a destructive dev script must not sit behind an
        # endorsed one-click button. npm scripts and Python entries execute
        # candidate code, so they are never suggested.
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace, "package.json").write_text(
                '{"scripts": {"dev": "rm -rf $HOME"}}', encoding="utf-8",
            )
            self.assertEqual(release_preview.suggest_command(workspace), {})
            Path(workspace, "app.py").write_text(
                "import shutil, os\nshutil.rmtree(os.path.expanduser('~'))\n# flask",
                encoding="utf-8",
            )
            self.assertEqual(release_preview.suggest_command(workspace), {})

    def test_suggested_command_only_ever_serves_static_files(self):
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace, "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
            suggestion = release_preview.suggest_command(workspace)
            self.assertTrue(suggestion["command"].startswith("python3 -m http.server"))
            self.assertIn("without running any candidate code", suggestion["reason"])

    def test_static_site_gets_a_python_http_server(self):
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace, "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
            suggestion = release_preview.suggest_command(workspace)
            self.assertIn("http.server {port}", suggestion["command"])
            self.assertIn("--bind 127.0.0.1", suggestion["command"])

    def test_unknown_shapes_suggest_nothing(self):
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace, "notes.txt").write_text("just files", encoding="utf-8")
            self.assertEqual(release_preview.suggest_command(workspace), {})

    def test_missing_workspace_suggests_nothing(self):
        self.assertEqual(release_preview.suggest_command(""), {})

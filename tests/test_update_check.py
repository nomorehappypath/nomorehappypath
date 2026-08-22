# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Version display, update check against a real (local) origin, consented update."""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import project_manager, update_check
from tests.environment_support import require_loopback


def run(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def build_origin_and_clone(base: Path, tags=("v0.1.0", "v0.1.6")):
    origin = base / "origin"
    origin.mkdir()
    run(base, "init", "-q", "-b", "main", str(origin))
    run(origin, "config", "user.name", "Fixture")
    run(origin, "config", "user.email", "fixture@example.invalid")
    (origin / "VERSION").write_text(tags[0] + "\n", encoding="utf-8")
    run(origin, "add", "VERSION")
    run(origin, "commit", "-qm", "release " + tags[0])
    run(origin, "tag", tags[0])
    clone = base / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    run(clone, "config", "user.name", "Fixture")
    run(clone, "config", "user.email", "fixture@example.invalid")
    for tag in tags[1:]:
        (origin / "VERSION").write_text(tag + "\n", encoding="utf-8")
        run(origin, "add", "VERSION")
        run(origin, "commit", "-qm", "release " + tag)
        run(origin, "tag", tag)
    return origin, clone


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_installed_version_prefers_the_version_file(self):
        (self.base / "VERSION").write_text("v9.9.9\n", encoding="utf-8")
        self.assertEqual(update_check.installed_version(self.base), "v9.9.9")

    def test_installed_version_degrades_to_development(self):
        self.assertEqual(update_check.installed_version(self.base), "development")

    def test_check_names_the_newer_origin_version(self):
        origin, clone = build_origin_and_clone(self.base)
        result = update_check.check(clone)
        self.assertEqual(result["installed"], "v0.1.0")
        self.assertEqual(result["latest"], "v0.1.6")
        self.assertTrue(result["update_available"])
        self.assertIn("v0.1.6 is available", result["message"])

    def test_check_reports_up_to_date(self):
        origin, clone = build_origin_and_clone(self.base, tags=("v0.1.6",))
        result = update_check.check(clone)
        self.assertFalse(result["update_available"])
        self.assertIn("up to date", result["message"])

    def test_check_without_an_origin_degrades_with_a_plain_message(self):
        loose = self.base / "loose"; loose.mkdir()
        with self.assertRaises(ValueError):
            update_check.check(loose)

    def test_apply_fast_forwards_and_reports_the_new_version(self):
        origin, clone = build_origin_and_clone(self.base)
        result = update_check.apply_update(clone)
        self.assertEqual(result["updated_to"], "v0.1.6")
        self.assertIn("restarting", result["message"])

    def test_apply_refuses_local_changes_without_overwriting(self):
        origin, clone = build_origin_and_clone(self.base)
        (clone / "VERSION").write_text("edited by hand\n", encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            update_check.apply_update(clone)
        self.assertIn("local changes", str(raised.exception))
        self.assertEqual((clone / "VERSION").read_text(encoding="utf-8"), "edited by hand\n")

    def test_apply_refuses_a_diverged_clone_with_guidance(self):
        origin, clone = build_origin_and_clone(self.base)
        (clone / "local.txt").write_text("mine\n", encoding="utf-8")
        run(clone, "add", "local.txt")
        run(clone, "commit", "-qm", "local commit")
        with self.assertRaises(ValueError) as raised:
            update_check.apply_update(clone)
        self.assertIn("git pull --ff-only", str(raised.exception))


class UpdateEndpointTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.origin, self.clone = build_origin_and_clone(self.base)
        self.manager = project_manager.ProjectManager(self.base / "home", board_port=0)
        self.manager.installation_root = self.clone
        self.restarts = []
        self.manager.request_restart = lambda: self.restarts.append(True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(self.manager))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"

    def request(self, path, method="GET", body=None):
        request = Request(self.url + path, method=method,
                          data=json.dumps(body or {}).encode() if method == "POST" else None,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_version_check_and_consented_apply(self):
        status, payload = self.request("/api/version")
        self.assertEqual((status, payload["version"]), (200, "v0.1.0"))
        status, payload = self.request("/api/update/check", "POST")
        self.assertEqual(status, 200)
        self.assertTrue(payload["update_available"])
        status, payload = self.request("/api/update/apply", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(payload["updated_to"], "v0.1.6")
        self.assertEqual(self.restarts, [True], "the restart seam must fire exactly once")
        status, payload = self.request("/api/version")
        self.assertEqual(payload["version"], "v0.1.6")

    def test_apply_is_refused_while_a_project_is_open(self):
        class RunningWorker:
            def poll(self): return None
        self.manager.worker = RunningWorker()
        status, payload = self.request("/api/update/apply", "POST")
        self.assertEqual(status, 400)
        self.assertIn("Close or pause", payload["error"])
        self.assertEqual(self.restarts, [])


if __name__ == "__main__":
    unittest.main()


class UpdateGateSemanticsTests(UpdateEndpointTests):
    """Review findings r1 + r2 as regression tests."""

    def test_a_paused_project_does_not_block_the_update(self):
        # r1: the app's own copy says pausing enables the update - it must,
        # proven with a REAL registered project whose board is really paused.
        from harness import board, project_registry as registry
        code = self.base / "paused-project"; code.mkdir()
        entry = registry.register(self.manager.home, "Paused", code, kind="scaffold")
        context = registry.context_for_entry(entry)
        board.snapshot(context)

        class RunningWorker:
            def poll(self): return None
        self.manager.worker = RunningWorker()
        self.manager.worker_project = entry["id"]

        # live and unpaused: blocked
        status, payload = self.request("/api/update/apply", "POST")
        self.assertEqual(status, 400)
        self.assertIn("Close or pause", payload["error"])
        # really paused: allowed
        board.begin_project_pause(context, drain_seconds=0.0)
        board.finish_project_pause(context)
        self.assertEqual(board.pause_state(context).get("status"), "paused")
        status, payload = self.request("/api/update/apply", "POST")
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.restarts, [True])

    def test_an_unresolvable_project_blocks_conservatively(self):
        class RunningWorker:
            def poll(self): return None
        self.manager.worker = RunningWorker()
        self.manager.worker_project = "missing-entry"
        status, _ = self.request("/api/update/apply", "POST")
        self.assertEqual(status, 400)
        self.assertEqual(self.restarts, [])

    def test_apply_is_idempotent_one_restart_only(self):
        # r2: a second apply after success must refuse, not restart twice.
        status, _ = self.request("/api/update/apply", "POST")
        self.assertEqual(status, 200)
        status, payload = self.request("/api/update/apply", "POST")
        self.assertEqual(status, 400)
        self.assertIn("already being applied", payload["error"])
        self.assertEqual(self.restarts, [True], "restart must fire exactly once")

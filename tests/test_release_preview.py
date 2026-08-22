# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Candidate previews for releases awaiting the owner's visual test.

Run: PYTHONPATH=. python3 -m unittest tests.test_release_preview -v
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
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
        self.assertIn("Open the candidate preview", page)
        self.assertIn("/api/settings/preview", page)
        self.assertIn("preview-retry", page)

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

    def test_ready_preview_renders_the_open_link(self):
        html = self.render_card(self.release({"status": "ready", "url": "http://127.0.0.1:8977/"}))
        self.assertIn('href="http://127.0.0.1:8977/"', html)
        self.assertIn("Open the candidate preview", html)
        self.assertIn("bb424e3055", html)
        self.assertIn("Accepted", html)

    def test_unconfigured_preview_renders_setup_with_candidate_location(self):
        html = self.render_card(self.release({
            "status": "unconfigured", "workspace": "/tmp/workspace",
            "branch": "harness/tasks/TASK/task",
        }))
        self.assertIn("Set up a candidate preview", html)
        self.assertIn("harness/tasks/TASK/task", html)
        self.assertIn("/tmp/workspace", html)
        self.assertIn("savePreviewCommand()", html)

    def test_app_bundle_preview_renders_the_open_button(self):
        html = self.render_card(self.release({
            "status": "app_bundle", "app_path": "/x/Weather.app",
            "app_name": "Weather", "built_at": "2026-08-21T11:10:22+00:00",
        }))
        self.assertIn("The app is built and ready to test", html)
        self.assertIn("Open the app", html)
        self.assertIn("openAppPreview", html)
        self.assertIn("Weather", html)
        self.assertNotIn("/x/Weather.app", html)
        self.assertNotIn("Set up a candidate preview", html)

    def test_failed_preview_renders_error_log_and_retry(self):
        html = self.render_card(self.release({
            "status": "failed", "error": "the preview command exited before serving its URL",
            "log_tail": "ModuleNotFoundError: flask",
        }))
        self.assertIn("could not start", html)
        self.assertIn("exited before serving", html)
        self.assertIn("ModuleNotFoundError: flask", html)
        self.assertIn("retryPreview", html)


if __name__ == "__main__":
    unittest.main()


class SuggestedCommandTests(unittest.TestCase):
    def test_node_project_with_dev_script_gets_a_one_click_command(self):
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace, "package.json").write_text(
                '{"scripts": {"dev": "vite"}}', encoding="utf-8",
            )
            suggestion = release_preview.suggest_command(workspace)
            self.assertIn("npm run dev", suggestion["command"])
            self.assertIn("{port}", suggestion["command"])
            self.assertIn("dev", suggestion["reason"])

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

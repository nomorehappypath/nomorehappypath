# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Projects landing page + manager simulations (spec §6.4, §7).

Drives the real HTTP surface end to end on a live server: list rendering with
derived truth (health, counts, running), create/adopt (with adopted-tree
purity), open (activation lock + separately bound worker with a cwd-independent
argv), double-open refusal, close, repair, remove.

Run:  PYTHONPATH=. python3 -m unittest tests.test_project_manager -v
"""
from __future__ import annotations

import json
import hashlib
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, control, global_settings, project_manager, project_registry as registry
from harness.project_manager_page import PAGE
from tests.environment_support import require_loopback


class ProjectManagerTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"

    @contextmanager
    def served(self):
        manager = project_manager.ProjectManager(self.home, board_port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}", manager
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def request(self, base: str, path: str, method="GET", body: dict | None = None):
        data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
        req = Request(base + path, data=data, method=method,
                      headers={"Content-Type": "application/json"} if data else {})
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def _board_fixture(self, data_root: Path):
        board = data_root / "board"
        board.mkdir(parents=True)
        (board / "state.json").write_text(json.dumps({
            "task_owner_directions": {"T1": "one", "T2": "two", "T3": "three"},
            "release_decisions": {"T1": {"decision": "accepted"}},
            "agents": {"a": {"active": True, "task": "T3", "role": "engineering"},
                       "b": {"active": True, "task": "GLOBAL_MONITOR", "role": "cto"}},
            "task_briefs": {"T3": {"update": "Waiting for independent review.",
                                      "updated_at": "2026-08-14T14:30:00+00:00"}},
            "events": [{"kind": "status_update", "at": "2026-08-14T14:31:00+00:00"}],
        }))

    # ---- S-MGR-001: landing page + truthful derived list ----
    def test_landing_and_derived_list(self):
        code = self.base / "alpha"; code.mkdir()
        entry = registry.register(self.home, "alpha", code, description="first")
        self._board_fixture(Path(entry["data_root"]))
        with self.served() as (base, _):
            with urlopen(base + "/", timeout=5) as response:
                page = response.read().decode()
            for marker in ("NoMoreHappyPath", "Primary navigation", "New project", "Adopt existing", "codex-notice"):
                self.assertIn(marker, page)
            with urlopen(base + "/?page=settings", timeout=5) as response:
                settings_page = response.read().decode()
            self.assertIn('id="settings-page"', settings_page)
            with urlopen(base + "/?page=help", timeout=5) as response:
                help_page = response.read().decode()
            self.assertIn('id="help-page"', help_page)
            self.assertIn("Create Or Adopt A Project", help_page)
            self.assertIn("Agree The Requirements And Say Go Ahead", help_page)
            self.assertIn("showPage(new URLSearchParams(window.location.search).get('page'))", settings_page)
            status, value = self.request(base, "/api/projects")
        self.assertEqual(status, 200)
        row = value["projects"][0]
        self.assertEqual(row["task_counts"], {"total": 3, "passed": 1, "open": 1, "awaiting_owner": 0})
        self.assertEqual(row["agent_counts"], {"total": 2, "delivery": 1, "reviewer": 0, "cto": 1})
        self.assertTrue(row["running"], "an active non-sentinel agent renders as running")
        self.assertTrue(row["resume_available"])
        self.assertEqual(row["latest_task"], "T3")
        self.assertEqual(row["latest_progress"], "Waiting for independent review.")
        self.assertEqual(row["last_board_activity"], "2026-08-14T14:31:00+00:00")
        self.assertTrue(row["health"]["ok"])
        self.assertIn("model connection", value["codex_notice"])

    def test_page_script_is_valid_and_card_renderer_escapes_project_text(self):
        script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
        checked = subprocess.run(["node", "--check", "-"], input=script, text=True,
                                 capture_output=True, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        renderer = script.split("const renderSummary", 1)[0]
        project = {
            "id": "unsafe", "name": '<img src=x onerror="bad()">',
            "description": "<script>bad()</script>", "code_root": "/tmp/<unsafe>",
            "kind": "scaffold", "active": False, "running": False,
            "resume_available": True, "latest_task": "T<script>",
            "latest_progress": "Review <b>pending</b>", "last_active_at": "",
            "last_board_activity": "", "health": {"ok": True, "reasons": []},
            "task_counts": {"total": 1, "passed": 0, "open": 1},
            "agent_counts": {"total": 1},
        }
        runner = renderer + "\nconsole.log(row(" + json.dumps(project) + "));"
        rendered = subprocess.run(["node", "-e", runner], text=True, capture_output=True, timeout=10)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertNotIn("<img", rendered.stdout)
        self.assertNotIn("<script>bad", rendered.stdout)
        self.assertIn("&lt;img", rendered.stdout)
        self.assertIn("&lt;script&gt;bad", rendered.stdout)
        self.assertIn("Open project", rendered.stdout)
        self.assertIn("Review &lt;b&gt;pending&lt;/b&gt;", rendered.stdout)

    def test_active_project_card_keeps_navigation_separate_from_lifecycle_actions(self):
        script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
        renderer = script.split("const renderSummary", 1)[0]
        project = {
            "id": "active", "name": "Active project", "description": "",
            "code_root": "/tmp/active", "kind": "scaffold", "active": True,
            "running": True, "paused": False, "resume_available": True,
            "latest_task": "T", "latest_progress": "Ready", "last_active_at": "",
            "last_board_activity": "", "health": {"ok": True, "reasons": []},
            "task_counts": {"total": 1, "passed": 0, "open": 1},
            "agent_counts": {"total": 1},
        }
        runner = renderer + "\nconsole.log(row(" + json.dumps(project) + "));"
        rendered = subprocess.run(
            ["node", "-e", runner], text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn('data-act="view"', rendered.stdout)
        self.assertIn("Open Mission Control", rendered.stdout)
        self.assertIn('data-act="pause"', rendered.stdout)
        self.assertNotIn('data-act="close"', rendered.stdout)
        self.assertNotIn("Close board", rendered.stdout)

    def test_lifecycle_ui_uses_designed_dialogs_and_native_folder_browse(self):
        self.assertIn('<textarea id="project-description"', PAGE)
        self.assertIn('id="project-folder-browse"', PAGE)
        self.assertIn('id="repair-dialog"', PAGE)
        self.assertIn('id="remove-dialog"', PAGE)
        self.assertIn("/api/folders/browse", PAGE)
        self.assertNotIn("confirm(", PAGE)
        self.assertNotIn("prompt(", PAGE)
        self.assertNotIn("alert(", PAGE)

    def test_projects_list_owns_vertical_scrolling(self):
        self.assertIn("html, body { height: 100%; overflow: hidden; }", PAGE)
        self.assertIn("#projects-page { height: 100%; min-height: 0; display: flex; flex-direction: column; }", PAGE)
        self.assertIn("#projects { flex: 1; min-height: 0;", PAGE)
        self.assertIn("grid-auto-rows: max-content", PAGE)
        self.assertIn("overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable", PAGE)
        self.assertIn("border: 1px solid var(--line); border-radius: 18px", PAGE)

    def test_short_desktop_viewport_keeps_project_details_readable(self):
        """Owner feedback: a 622px-tall viewport must not clip the card shell."""
        compact = PAGE.split("@media (max-height: 700px) and (min-width: 761px)", 1)[1]
        compact = compact.split("@media (max-width: 760px)", 1)[0]
        for rule in (
            ".page { padding: 10px 0; }",
            ".summary-card { min-height: 42px; padding: 5px 10px; }",
            ".list-head { margin: 6px 0 4px; }",
            ".project { gap: 10px; padding: 10px 12px; }",
            ".confidentiality { margin-top: 4px; padding: 5px 8px; font-size: 11px; }",
        ):
            self.assertIn(rule, compact)

    def test_settings_page_and_global_store_persist_across_manager_restart(self):
        for marker in (
            'data-page="settings"', 'id="settings-form"', 'id="settings-fields"',
            "/api/settings/connect", "Global agent defaults", "Save agent settings",
            'data-page="help"', 'id="help-page"', "Read The Progress Bars",
            "Accept Or Reject Completed Work",
        ):
            self.assertIn(marker, PAGE)
        updated = {
            "delivery": {"provider": "claude", "model": "sonnet", "effort": "medium"},
            "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
            "cto": {"provider": "claude", "model": "opus", "effort": "high"},
        }
        with self.served() as (base, _):
            status, initial = self.request(base, "/api/settings")
            self.assertEqual(status, 200)
            self.assertEqual(set(initial["agent_settings"]), {"delivery", "reviewer", "cto"})
            status, saved = self.request(
                base, "/api/settings", "POST", {"agent_settings": updated},
            )
            self.assertEqual(status, 200)
            self.assertEqual(saved["agent_settings"], updated)
        self.assertTrue((self.home / "settings.json").is_file())

        with self.served() as (base, _):
            status, restarted = self.request(base, "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(restarted["agent_settings"], updated)

    def test_global_settings_migrate_once_from_existing_project_control(self):
        code = self.base / "legacy"; code.mkdir()
        entry = registry.register(self.home, "Legacy settings", code)
        legacy = {
            "delivery": {"provider": "claude", "model": "sonnet", "effort": "high"},
            "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
            "cto": {"provider": "claude", "model": "opus", "effort": "max"},
        }
        control_dir = Path(entry["data_root"]) / "control"; control_dir.mkdir(parents=True)
        (control_dir / "sessions.json").write_text(json.dumps({
            "sessions": {}, "inbox": {}, "agent_settings": legacy,
        }))

        manager = project_manager.ProjectManager(self.home)

        self.assertEqual(manager.settings_payload()["agent_settings"], legacy)
        stored = json.loads((self.home / "settings.json").read_text())
        self.assertEqual(stored["agent_settings"], legacy)
        migrated_control = json.loads((control_dir / "sessions.json").read_text())
        self.assertNotIn("agent_settings", migrated_control,
                         "migration must leave no project-local role-settings copy")

    def test_existing_global_settings_remove_copies_from_every_project(self):
        selected = {
            "delivery": {"provider": "claude", "model": "sonnet", "effort": "medium"},
            "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
            "cto": {"provider": "claude", "model": "opus", "effort": "high"},
        }
        global_settings.update_agent_settings(self.home, selected)
        entries = []
        for index in range(2):
            code = self.base / f"project-{index}"; code.mkdir()
            entry = registry.register(self.home, f"Project {index}", code)
            control_dir = Path(entry["data_root"]) / "control"
            control_dir.mkdir(parents=True)
            (control_dir / "sessions.json").write_text(json.dumps({
                "sessions": {}, "inbox": {},
                "agent_settings": control.default_agent_settings(),
            }))
            entries.append(entry)

        project_manager.ProjectManager(self.home)

        self.assertEqual(global_settings.load(self.home)["agent_settings"], selected)
        for entry in entries:
            local = json.loads(
                (Path(entry["data_root"]) / "control" / "sessions.json").read_text(),
            )
            self.assertNotIn("agent_settings", local)
            self.assertNotIn("connectivity", local)

    def test_cli_connection_endpoint_executes_flags_and_records_success_and_failure(self):
        fake = self.base / "fake-codex"
        capture = self.base / "connection-args.txt"
        fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$HARNESS_CAPTURE"\n')
        fake.chmod(0o755)
        with patch.dict(os.environ, {
            "HARNESS_CODEX_BIN": str(fake), "HARNESS_CAPTURE": str(capture),
        }, clear=False):
            with self.served() as (base, _):
                status, connected = self.request(base, "/api/settings/connect", "POST", {
                    "provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh",
                })
                self.assertEqual(status, 200)
                self.assertTrue(connected["ok"])
                status, settings = self.request(base, "/api/settings")
                self.assertEqual(status, 200)
                self.assertTrue(settings["connectivity"]["codex"]["ok"])
        arguments = capture.read_text().splitlines()
        # A real run, not a --help probe: --help exits 0 for any model name and
        # once let an older CLI pass this test then fail every task (field defect).
        self.assertEqual(arguments[:3], ["exec", "--model", "gpt-5.6-terra"])
        self.assertIn("model_reasoning_effort=xhigh", arguments)
        self.assertNotIn("--help", arguments)

        with patch.dict(os.environ, {"HARNESS_CODEX_BIN": str(self.base / "missing")}, clear=False):
            with self.served() as (base, _):
                status, failed = self.request(base, "/api/settings/connect", "POST", {
                    "provider": "codex", "model": "gpt-5.6-sol", "effort": "high",
                })
                self.assertEqual(status, 400)
                self.assertIn("not found", failed["error"])
                _, settings = self.request(base, "/api/settings")
                self.assertFalse(settings["connectivity"]["codex"]["ok"])

    def test_open_project_captures_manager_global_settings_for_board_sessions(self):
        code = self.base / "global-launch"; code.mkdir()
        entry = registry.register(self.home, "Global launch", code)
        manager = project_manager.ProjectManager(self.home, board_port=0)
        selected = {
            "delivery": {"provider": "claude", "model": "sonnet", "effort": "medium"},
            "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
            "cto": {"provider": "claude", "model": "opus", "effort": "high"},
        }
        global_settings.update_agent_settings(self.home, selected)

        class FakeProc:
            pid = 4242
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): return 0

        with patch.object(subprocess, "Popen", return_value=FakeProc()):
            manager.open_project(entry["id"])
            manager.close_project(entry["id"])

        argv = manager.worker_argv(entry)
        self.assertEqual(argv[argv.index("--settings-home") + 1], str(self.home.resolve()))
        sessions_path = Path(entry["data_root"]) / "control" / "sessions.json"
        if sessions_path.exists():
            self.assertNotIn("agent_settings", json.loads(sessions_path.read_text()))

    def test_invalid_or_corrupt_global_settings_fail_safely_and_can_be_repaired(self):
        with self.served() as (base, _):
            _, original = self.request(base, "/api/settings")
            bad = json.loads(json.dumps(original["agent_settings"]))
            bad["delivery"]["model"] = "model; touch /tmp/not-allowed"
            status, refused = self.request(base, "/api/settings", "POST", {
                "agent_settings": bad,
            })
            self.assertEqual(status, 400)
            self.assertIn("model must be", refused["error"])
            _, unchanged = self.request(base, "/api/settings")
            self.assertEqual(unchanged["agent_settings"], original["agent_settings"])

        version_two = {
            "version": 2,
            "agent_settings": original["agent_settings"],
            "connectivity": {"future-provider": {"ok": True}},
            "future_field": {"must": "survive"},
        }
        (self.home / "settings.json").write_text(json.dumps(version_two, indent=2) + "\n")
        with self.served() as (base, _):
            status, recovered = self.request(base, "/api/settings")
            self.assertEqual(status, 200)
            self.assertIn("need attention", recovered["error"])
            unreadable_bytes = (self.home / "settings.json").read_bytes()
            fake = self.base / "fake-codex-corrupt-settings"
            fake.write_text("#!/usr/bin/env bash\nexit 0\n"); fake.chmod(0o755)
            with patch.dict(os.environ, {"HARNESS_CODEX_BIN": str(fake)}, clear=False):
                status, checked = self.request(base, "/api/settings/connect", "POST", {
                    "provider": "codex", "model": "gpt-5.6-sol", "effort": "high",
                })
            self.assertEqual(status, 200)
            self.assertIn("Repair the saved global settings", checked["message"])
            self.assertEqual((self.home / "settings.json").read_bytes(), unreadable_bytes,
                             "a diagnostic connection check must not replace unreadable settings")
            defaults = control.default_agent_settings()
            status, repaired = self.request(base, "/api/settings", "POST", {
                "agent_settings": defaults,
            })
            self.assertEqual(status, 200)
            self.assertEqual(repaired["agent_settings"], defaults)
            self.assertNotIn("error", repaired)
            broken = list(self.home.glob("settings.json.broken-*"))
            self.assertEqual(len(broken), 1)
            self.assertEqual(broken[0].read_bytes(), unreadable_bytes)

    def test_corrupt_board_is_isolated_and_active_project_sorts_first(self):
        bad_shapes = [
            None,
            [],
            "string",
            {"agents": []},
            {"agents": {"agent": "string"}},
            {"task_briefs": []},
            {"task_briefs": {"task": "string"}},
            {"events": {}},
            {"events": ["string"]},
        ]
        broken_entries = []
        for index, shape in enumerate(bad_shapes):
            bad_code = self.base / f"bad-{index}"; bad_code.mkdir()
            bad = registry.register(self.home, f"Broken board {index}", bad_code)
            bad_board = Path(bad["data_root"]) / "board"; bad_board.mkdir(parents=True)
            (bad_board / "state.json").write_text(json.dumps(shape))
            broken_entries.append(bad)

        unreadable_code = self.base / "unreadable"; unreadable_code.mkdir()
        unreadable = registry.register(self.home, "Unreadable board", unreadable_code)
        unreadable_board = Path(unreadable["data_root"]) / "board"; unreadable_board.mkdir(parents=True)
        (unreadable_board / "state.json").write_text("not-json")
        broken_entries.append(unreadable)

        active_code = self.base / "active"; active_code.mkdir()
        active = registry.register(self.home, "Active project", active_code)
        registry.activate(self.home, active["id"])
        with self.served() as (base, _):
            status, payload = self.request(base, "/api/projects")

        self.assertEqual(status, 200, "one malformed board must not drop the projects endpoint")
        self.assertEqual(payload["projects"][0]["id"], active["id"])
        by_id = {row["id"]: row for row in payload["projects"]}
        for entry in broken_entries:
            broken = by_id[entry["id"]]
            self.assertFalse(broken["health"]["ok"])
            self.assertIn("board state unreadable or malformed", broken["health"]["reasons"][0])
            self.assertEqual(broken["task_counts"], {"total": 0, "passed": 0, "open": 0, "awaiting_owner": 0})

    def test_nested_malformed_board_field_is_isolated_over_http(self):
        bad_code = self.base / "nested-bad"; bad_code.mkdir()
        bad = registry.register(self.home, "Nested malformed board", bad_code)
        bad_board = Path(bad["data_root"]) / "board"; bad_board.mkdir(parents=True)
        (bad_board / "state.json").write_text(json.dumps({
            "agents": {"agent": {"active": True, "task": []}},
        }))
        healthy_code = self.base / "healthy"; healthy_code.mkdir()
        healthy = registry.register(self.home, "Healthy neighbour", healthy_code)

        with self.served() as (base, _):
            status, payload = self.request(base, "/api/projects")

        self.assertEqual(status, 200)
        by_id = {row["id"]: row for row in payload["projects"]}
        self.assertFalse(by_id[bad["id"]]["health"]["ok"])
        self.assertEqual(by_id[bad["id"]]["task_counts"], {"total": 0, "passed": 0, "open": 0, "awaiting_owner": 0})
        self.assertTrue(by_id[healthy["id"]]["health"]["ok"])

    def test_completed_project_does_not_show_a_stale_in_progress_brief(self):
        code = self.base / "complete"; code.mkdir()
        entry = registry.register(self.home, "Complete project", code)
        board = Path(entry["data_root"]) / "board"; board.mkdir(parents=True)
        (board / "state.json").write_text(json.dumps({
            "task_owner_directions": {"DONE": "Build and release the feature."},
            "release_decisions": {"DONE": {
                "decision": "accepted", "recorded_at": "2026-08-14T15:00:00+00:00",
            }},
            "agents": {},
            "task_briefs": {"DONE": {
                "update": "Waiting for independent review.",
                "updated_at": "2026-08-14T14:30:00+00:00",
            }},
            "events": [{"kind": "release_accepted", "at": "2026-08-14T15:00:00+00:00"}],
        }))

        row = project_manager.ProjectManager(self.home).projects_payload()["projects"][0]

        self.assertEqual(row["latest_task"], "DONE")
        self.assertEqual(
            row["latest_progress"],
            "Accepted and complete. Reopen to add new instructions or tasks.",
        )
        self.assertTrue(row["resume_available"])
        self.assertEqual(row["task_counts"], {"total": 1, "passed": 1, "open": 0, "awaiting_owner": 0})

    def test_completed_summary_is_deterministic_and_ignores_unknown_release_rows(self):
        entry = {
            "data_root": str(self.base / "data"), "code_root": str(self.base),
            "workspace_root": str(self.base / "workspaces"),
        }
        board = Path(entry["data_root"]) / "board"; board.mkdir(parents=True)
        (board / "state.json").write_text(json.dumps({
            "task_owner_directions": {"ALPHA": "one", "BETA": "two"},
            "release_decisions": {
                "ALPHA": {"decision": "accepted"},
                "BETA": {"decision": "accepted"},
                "UNKNOWN": {"decision": "accepted"},
            },
            "agents": {}, "task_briefs": {}, "events": [],
        }))

        summaries = [project_manager.derive_status(entry) for _ in range(5)]

        self.assertEqual({value["latest_task"] for value in summaries}, {"BETA"})
        self.assertEqual(summaries[0]["task_counts"], {"total": 2, "passed": 2, "open": 0, "awaiting_owner": 0})

    def test_manager_restart_reopens_exact_durable_board_without_instruction_reentry(self):
        code = self.base / "durable"; code.mkdir()
        entry = registry.register(self.home, "Durable project", code)
        board = Path(entry["data_root"]) / "board"; board.mkdir(parents=True)
        saved = {
            "task_owner_directions": {"MEMORY-TASK": "Original instructions that must not be repeated."},
            "release_decisions": {},
            "agents": {},
            "task_briefs": {"MEMORY-TASK": {
                "plan": "Preserve tasks, history, evidence, and next action.",
                "update": "Continue from the saved QA gate.",
                "updated_at": "2026-08-14T16:00:00+00:00",
            }},
            "events": [{
                "kind": "evidence_recorded", "task": "MEMORY-TASK",
                "evidence": "evidence/saved-proof.txt", "at": "2026-08-14T16:01:00+00:00",
            }],
        }
        state_path = board / "state.json"
        state_path.write_text(json.dumps(saved, indent=2, sort_keys=True))
        original_bytes = state_path.read_bytes()

        class FakeProc:
            pid = 4242

            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): return 0

        spawned: list[list[str]] = []
        with patch.object(
            subprocess, "Popen",
            side_effect=lambda argv, **kwargs: (spawned.append(argv), FakeProc())[1],
        ):
            with self.served() as (base, _):
                status, _ = self.request(base, f"/api/projects/{entry['id']}/open", "POST")
                self.assertEqual(status, 200)
                status, _ = self.request(base, f"/api/projects/{entry['id']}/close", "POST")
                self.assertEqual(status, 200)

            # A new server/manager process represents returning to the project later.
            with self.served() as (base, _):
                status, payload = self.request(base, "/api/projects")
                self.assertEqual(status, 200)
                row = payload["projects"][0]
                self.assertTrue(row["resume_available"])
                self.assertEqual(row["latest_task"], "MEMORY-TASK")
                self.assertEqual(row["latest_progress"], "Continue from the saved QA gate.")
                status, _ = self.request(base, f"/api/projects/{entry['id']}/open", "POST")
                self.assertEqual(status, 200)
                status, _ = self.request(base, f"/api/projects/{entry['id']}/close", "POST")
                self.assertEqual(status, 200)

        self.assertEqual(state_path.read_bytes(), original_bytes,
                         "open, close, and manager restart must not rewrite project memory")
        self.assertEqual(len(spawned), 2)
        for argv in spawned:
            self.assertEqual(argv[argv.index("--data-root") + 1], str(Path(entry["data_root"]).resolve()))
            self.assertEqual(argv[argv.index("--workspace-root") + 1], str(Path(entry["workspace_root"]).resolve()))

    # ---- S-MGR-002: create + adopt via the API; adopted tree stays pristine ----
    def test_create_and_adopt(self):
        repo = self.base / "adopted-repo"; repo.mkdir()
        parent = self.base / "projects"; parent.mkdir()
        with self.served() as (base, _):
            status, created = self.request(base, "/api/projects", "POST", {
                "name": "Fresh App", "description": "A paragraph\nwith a second line.",
                "parent_root": str(parent),
            })
            self.assertEqual(status, 201)
            self.assertEqual(created["kind"], "scaffold")
            self.assertEqual(Path(created["code_root"]), (parent / "fresh-app").resolve())
            self.assertEqual(Path(created["data_root"]), (parent / "fresh-app" / ".harness").resolve())
            self.assertEqual(created["description"], "A paragraph\nwith a second line.")
            self.assertTrue(Path(created["code_root"]).is_dir())
            self.assertTrue(Path(created["data_root"]).is_dir())
            status, listing = self.request(base, "/api/projects")
            self.assertEqual(status, 200)
            created_row = next(row for row in listing["projects"] if row["id"] == created["id"])
            self.assertTrue(created_row["health"]["ok"])

            class FakeProc:
                pid = 4242

                def poll(self): return None
                def terminate(self): pass
                def wait(self, timeout=None): return 0

            with patch.object(subprocess, "Popen", return_value=FakeProc()):
                status, opened = self.request(base, f"/api/projects/{created['id']}/open", "POST")
                self.assertEqual(status, 200)
                self.assertIn("board_url", opened)
                status, _ = self.request(base, f"/api/projects/{created['id']}/close", "POST")
                self.assertEqual(status, 200)

            status, adopted = self.request(base, "/api/projects", "POST", {
                "name": "ktrader-style", "kind": "adopted", "code_root": str(repo)})
            self.assertEqual(status, 201)
            self.assertTrue(Path(adopted["data_root"]).is_relative_to((self.home / "projects").resolve()))
            self.assertTrue(Path(adopted["workspace_root"]).is_relative_to((self.home / "projects").resolve()))
            status, refused = self.request(base, "/api/projects", "POST", {
                "name": "bad", "kind": "adopted", "code_root": str(repo),
                "data_root": str(repo / ".harness"), "workspace_root": str(self.base / "x")})
            self.assertEqual(status, 400)
            self.assertIn("OUTSIDE", refused["error"])
        self.assertEqual(list(repo.iterdir()), [], "adoption adds nothing to the adopted tree")

    def test_adopted_repository_is_byte_identical_through_full_manager_lifecycle(self):
        repo = self.base / "real-repository"; (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_text("print('owner code')\n")
        (repo / "README.md").write_text("# Owner repository\n")
        (repo / ".git").mkdir(); (repo / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n")

        def digest_tree(root: Path) -> dict[str, str]:
            # The ONE sanctioned automatic write into an adopted repository is
            # the owner-directed Claude permissions file; everything else must
            # stay byte-identical.
            return {
                str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(root.rglob("*"))
                if path.is_file()
                and str(path.relative_to(root)) != ".claude/settings.local.json"
            }

        original = digest_tree(repo)

        class FakeProc:
            pid = 4242
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): return 0

        with patch.object(subprocess, "Popen", return_value=FakeProc()):
            with self.served() as (base, _):
                status, adopted = self.request(base, "/api/projects", "POST", {
                    "name": "Owner repository", "kind": "adopted", "code_root": str(repo),
                    "description": "Existing source must stay untouched.",
                })
                self.assertEqual(status, 201)
                data_root = Path(adopted["data_root"])
                workspace_root = Path(adopted["workspace_root"])
                self.assertNotIn(repo.resolve(), data_root.parents)
                self.assertNotIn(repo.resolve(), workspace_root.parents)
                status, _ = self.request(base, f"/api/projects/{adopted['id']}/open", "POST")
                self.assertEqual(status, 200)
                status, _ = self.request(base, f"/api/projects/{adopted['id']}/close", "POST")
                self.assertEqual(status, 200)

            self.assertEqual(digest_tree(repo), original)
            self.assertFalse((repo / ".harness").exists())
            self.assertTrue((data_root / "control" / "sessions.json").is_file())
            self.assertTrue(workspace_root.is_dir())

            # Reopening from a fresh manager keeps the same external roots.
            with self.served() as (base, _):
                _, listing = self.request(base, "/api/projects")
                row = listing["projects"][0]
                self.assertEqual(Path(row["data_root"]), data_root)
                self.assertEqual(Path(row["workspace_root"]), workspace_root)
                status, _ = self.request(base, f"/api/projects/{adopted['id']}/open", "POST")
                self.assertEqual(status, 200)
                self.request(base, f"/api/projects/{adopted['id']}/close", "POST")

                moved = self.base / "repository-moved-by-owner"
                repo.rename(moved)
                status, repaired = self.request(base, f"/api/projects/{adopted['id']}/repair", "POST", {
                    "code_root": str(moved),
                })
                self.assertEqual(status, 200)
                self.assertEqual(Path(repaired["data_root"]), data_root)
                self.assertEqual(Path(repaired["workspace_root"]), workspace_root)
                status, _ = self.request(base, f"/api/projects/{adopted['id']}", "DELETE")
                self.assertEqual(status, 200)

        self.assertEqual(digest_tree(moved), original)
        self.assertFalse((moved / ".harness").exists())
        self.assertTrue(data_root.is_dir(), "removal preserves external harness memory")
        self.assertTrue(workspace_root.is_dir(), "removal preserves external workspaces")

    def test_integrated_owner_journey_across_settings_projects_resume_and_adoption(self):
        parent = self.base / "new-projects"; parent.mkdir()
        adopted_repo = self.base / "existing-repository"; adopted_repo.mkdir()
        (adopted_repo / "owner.py").write_text("VALUE = 'unchanged'\n")
        adopted_before = hashlib.sha256((adopted_repo / "owner.py").read_bytes()).hexdigest()
        selected = {
            "delivery": {"provider": "claude", "model": "sonnet", "effort": "medium"},
            "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
            "cto": {"provider": "claude", "model": "opus", "effort": "high"},
        }

        class FakeProc:
            pid = 4242
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): return 0

        spawned: list[list[str]] = []
        with patch.object(
            subprocess, "Popen",
            side_effect=lambda argv, **kwargs: (spawned.append(argv), FakeProc())[1],
        ):
            with self.served() as (base, _):
                status, _ = self.request(base, "/api/settings", "POST", {
                    "agent_settings": selected,
                })
                self.assertEqual(status, 200)
                status, created = self.request(base, "/api/projects", "POST", {
                    "name": "Owner Workspace", "parent_root": str(parent),
                    "description": "First paragraph line.\nSecond paragraph line.",
                })
                self.assertEqual(status, 201)
                status, adopted = self.request(base, "/api/projects", "POST", {
                    "name": "Existing Source", "kind": "adopted",
                    "code_root": str(adopted_repo), "description": "Do not modify this repository.",
                })
                self.assertEqual(status, 201)
                self._board_fixture(Path(created["data_root"]))

                _, listing = self.request(base, "/api/projects")
                by_id = {row["id"]: row for row in listing["projects"]}
                self.assertEqual(by_id[created["id"]]["description"],
                                 "First paragraph line.\nSecond paragraph line.")
                self.assertTrue(by_id[created["id"]]["resume_available"])
                self.assertEqual(by_id[created["id"]]["latest_task"], "T3")
                self.assertFalse(by_id[adopted["id"]]["resume_available"])
                status, _ = self.request(base, f"/api/projects/{created['id']}/open", "POST")
                self.assertEqual(status, 200)
                self.request(base, f"/api/projects/{created['id']}/close", "POST")

            # Returning through a new manager keeps global settings and project memory,
            # then safely switches to the adopted repository.
            with self.served() as (base, _):
                _, settings = self.request(base, "/api/settings")
                self.assertEqual(settings["agent_settings"], selected)
                _, listing = self.request(base, "/api/projects")
                by_id = {row["id"]: row for row in listing["projects"]}
                self.assertEqual(by_id[created["id"]]["latest_progress"],
                                 "Waiting for independent review.")
                status, _ = self.request(base, f"/api/projects/{adopted['id']}/open", "POST")
                self.assertEqual(status, 200)
                self.request(base, f"/api/projects/{adopted['id']}/close", "POST")

        self.assertEqual(hashlib.sha256((adopted_repo / "owner.py").read_bytes()).hexdigest(),
                         adopted_before)
        self.assertFalse((adopted_repo / ".harness").exists())
        self.assertEqual(len(spawned), 2)
        self.assertIn("--settings-home", spawned[0])
        self.assertNotEqual(
            spawned[0][spawned[0].index("--data-root") + 1],
            spawned[1][spawned[1].index("--data-root") + 1],
        )

    def test_native_folder_browse_endpoint_and_scaffold_repair_rebase(self):
        selected = self.base / "selected"; selected.mkdir()
        with self.served() as (base, _):
            with patch.object(project_manager, "choose_folder", return_value=str(selected)) as chooser:
                status, value = self.request(base, "/api/folders/browse", "POST", {"purpose": "new-parent"})
            self.assertEqual(status, 200)
            self.assertEqual(value, {"path": str(selected), "cancelled": False})
            chooser.assert_called_once_with("new-parent")

            old = self.base / "old"; old.mkdir()
            status, created = self.request(base, "/api/projects", "POST", {
                "name": "repairable", "code_root": str(old),
            })
            self.assertEqual(status, 201)
            moved = self.base / "moved"; old.rename(moved)
            status, repaired = self.request(
                base, f"/api/projects/{created['id']}/repair", "POST", {"code_root": str(moved)},
            )
            self.assertEqual(status, 200)
            self.assertEqual(Path(repaired["code_root"]), moved.resolve())
            self.assertEqual(Path(repaired["data_root"]), (moved / ".harness").resolve())

    def test_folder_picker_cancel_errors_and_scaffold_path_hardening(self):
        parent = self.base / "parent"; parent.mkdir()
        (parent / "already-here").mkdir()
        with self.served() as (base, _):
            with patch.object(project_manager, "choose_folder", return_value=""):
                status, cancelled = self.request(
                    base, "/api/folders/browse", "POST", {"purpose": "adopt-project"},
                )
            self.assertEqual(status, 200)
            self.assertEqual(cancelled, {"path": "", "cancelled": True})

            status, invalid = self.request(
                base, "/api/folders/browse", "POST", {"purpose": "not-a-purpose"},
            )
            self.assertEqual(status, 400)
            self.assertIn("unknown folder", invalid["error"])

            status, missing = self.request(base, "/api/projects", "POST", {
                "name": "Missing parent", "parent_root": str(self.base / "gone"),
            })
            self.assertEqual(status, 400)
            self.assertIn("no longer exists", missing["error"])

            status, collision = self.request(base, "/api/projects", "POST", {
                "name": "Already Here", "parent_root": str(parent),
            })
            self.assertEqual(status, 400)
            self.assertIn("adopt it instead", collision["error"])

            status, hardened = self.request(base, "/api/projects", "POST", {
                "name": "../../Unsafe Name", "parent_root": str(parent),
            })
            self.assertEqual(status, 201)
            self.assertEqual(Path(hardened["code_root"]), (parent / "unsafe-name").resolve())

            status, too_long = self.request(base, "/api/projects", "POST", {
                "name": "x" * 300, "parent_root": str(parent),
            })
            self.assertEqual(status, 400)
            self.assertTrue(too_long["error"])
            status, listing = self.request(base, "/api/projects")
            self.assertEqual(status, 200, "a path-length error must not drop the HTTP connection")
            self.assertEqual(len(listing["projects"]), 1)

    def test_folder_picker_timeout_and_failed_registration_cleanup(self):
        parent = self.base / "parent"; parent.mkdir()
        with self.served() as (base, _):
            with patch.object(
                project_manager.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(["/usr/bin/osascript"], 120),
            ):
                status, timed_out = self.request(
                    base, "/api/folders/browse", "POST", {"purpose": "new-parent"},
                )
            self.assertEqual(status, 400)
            self.assertIn("timed out", timed_out["error"])

            existing = self.base / "existing"; existing.mkdir()
            registry.register(self.home, "Duplicate", existing)
            status, duplicate = self.request(base, "/api/projects", "POST", {
                "name": "Duplicate", "parent_root": str(parent),
            })
            self.assertEqual(status, 400)
            self.assertIn("already exists", duplicate["error"])
            self.assertFalse(parent.joinpath("duplicate").exists(),
                             "a failed registry write must roll back its empty scaffold")

            with patch.object(registry, "remove", side_effect=OSError("registry is unavailable")):
                status, failed_remove = self.request(
                    base, "/api/projects/not-present", "DELETE",
                )
            self.assertEqual(status, 400)
            self.assertIn("registry is unavailable", failed_remove["error"])

    # ---- S-MGR-003 (load-bearing): open = lock + separately bound worker; double-open refused ----
    def test_board_worker_default_does_not_collide_with_governance_viewer(self):
        self.assertEqual(project_manager.BOARD_PORT, 8741)
        self.assertNotEqual(project_manager.BOARD_PORT, 8742)

    def test_real_open_waits_until_its_exact_board_worker_is_serving(self):
        code = self.base / "verified-worker"; code.mkdir()
        entry = registry.register(self.home, "Verified worker", code)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        manager = project_manager.ProjectManager(
            self.home, board_port=port, worker_start_timeout=5,
        )

        try:
            opened = manager.open_project(entry["id"])
            with urlopen(opened["board_url"] + "api/ready", timeout=2) as response:
                ready = json.loads(response.read())
            self.assertTrue(ready["ready"])
            self.assertEqual(ready["project_name"], "Verified worker")
            self.assertTrue(ready["surface"]["project_chat"])
            self.assertEqual(ready["watchdog"], {
                "active": True,
                "interval_seconds": board.WATCHDOG_INTERVAL_SECONDS,
                "cto_poll_deadline_seconds": board.CTO_MONITOR_INTERVAL_SECONDS,
            })
            self.assertEqual(ready["runtime"]["commit"], manager.runtime["commit"])
            self.assertEqual(ready["runtime"]["source_digest"], manager.runtime["source_digest"])
            self.assertIsNone(manager.worker.poll())
        finally:
            manager.close_project(entry["id"])

        self.assertIsNone(registry.active_project(self.home))

    def test_board_worker_bind_failure_is_loud_and_releases_activation(self):
        code = self.base / "colliding-worker"; code.mkdir()
        entry = registry.register(self.home, "Colliding worker", code)
        occupied = socket.socket()
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        manager = project_manager.ProjectManager(
            self.home, board_port=port, worker_start_timeout=2,
        )

        try:
            with self.assertRaisesRegex(
                ValueError, "board worker exited before serving|did not begin serving",
            ):
                manager.open_project(entry["id"])
        finally:
            occupied.close()

        self.assertIsNone(manager.worker)
        self.assertEqual(manager.worker_project, "")
        self.assertIsNone(
            registry.active_project(self.home),
            "a failed bind must not leave a project falsely active",
        )

    def test_worker_death_is_visible_and_resume_replaces_it_without_duplication(self):
        code = self.base / "worker-recovery"; code.mkdir()
        entry = registry.register(self.home, "Worker recovery", code)

        class FakeProc:
            pid = 4242
            returncode = None
            def poll(self): return self.returncode
            def terminate(self): self.returncode = -15
            def wait(self, timeout=None): return self.returncode

        first = FakeProc()
        manager = project_manager.ProjectManager(self.home, board_port=0)
        with patch.object(subprocess, "Popen", return_value=first):
            manager.open_project(entry["id"])
        first_action_token = manager.worker_action_token
        first.returncode = 17

        row = manager.projects_payload()["projects"][0]
        self.assertFalse(row["active"])
        self.assertIn("stopped unexpectedly", row["worker_error"])
        self.assertIn("Resume the project", row["latest_progress"])
        self.assertIsNone(registry.active_project(self.home))
        self.assertEqual(manager.worker_action_token, "")

        replacement = FakeProc()
        with patch.object(subprocess, "Popen", return_value=replacement):
            reopened = manager.open_project(entry["id"])
        self.assertEqual(reopened["worker_pid"], replacement.pid)
        self.assertEqual(manager.worker_project, entry["id"])
        self.assertNotEqual(manager.worker_action_token, first_action_token)
        self.assertEqual(manager.worker_failure, {})
        manager.close_project(entry["id"])

    def test_open_api_surfaces_worker_start_death_to_the_landing_ui(self):
        code = self.base / "worker-start-death"; code.mkdir()
        entry = registry.register(self.home, "Worker start death", code)

        class DeadProc:
            pid = 4242
            def poll(self): return 23

        manager = project_manager.ProjectManager(
            self.home, board_port=45871, worker_start_timeout=0.1,
        )
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), project_manager.make_handler(manager),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with patch.object(subprocess, "Popen", return_value=DeadProc()):
                status, failure = self.request(
                    base, f"/api/projects/{entry['id']}/open", "POST",
                )
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

        self.assertEqual(status, 400)
        self.assertIn("exited before serving", failure["error"])
        self.assertIsNone(registry.active_project(self.home))

    def test_open_close_lifecycle(self):
        code = self.base / "beta"; code.mkdir()
        entry = registry.register(self.home, "beta", code)
        other = registry.register(self.home, "gamma", (self.base / "gamma"))
        (self.base / "gamma").mkdir()
        spawned: list[list[str]] = []

        class FakeProc:
            pid = 4242
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): return 0

        with self.served() as (base, manager):
            with patch.object(subprocess, "Popen", side_effect=lambda argv, **kw: (spawned.append(argv), FakeProc())[1]):
                status, opened = self.request(base, f"/api/projects/{entry['id']}/open", "POST")
                self.assertEqual(status, 200)
                self.assertIn("board_url", opened)
                # The worker argv is cwd-independent: absolute interpreter, absolute
                # viewer path, and the full explicit context flags.
                argv = spawned[0]
                self.assertTrue(Path(argv[0]).is_absolute() and Path(argv[1]).is_absolute())
                self.assertIn("--data-root", argv)
                self.assertIn(str(Path(entry["data_root"]).resolve()), argv)
                # The opened board identifies its project and links back to the list.
                self.assertIn("--project-name", argv)
                self.assertEqual(argv[argv.index("--project-name") + 1], "beta")
                self.assertIn("--manager-url", argv)
                self.assertIn("--project-id", argv)
                self.assertEqual(argv[argv.index("--project-id") + 1], entry["id"])
                # Opening another project SWITCHES: the active one is drained
                # and released, the requested one activates - no Pause/Close
                # ceremony for the owner.
                status, switched = self.request(base, f"/api/projects/{other['id']}/open", "POST")
                self.assertEqual(status, 200)
                _, listing = self.request(base, "/api/projects")
                flags = {row["id"]: row["active"] for row in listing["projects"]}
                self.assertTrue(flags[other["id"]]); self.assertFalse(flags[entry["id"]])
                # Switch back, then close releases the lock entirely.
                status, _ = self.request(base, f"/api/projects/{entry['id']}/open", "POST")
                self.assertEqual(status, 200)
                status, _ = self.request(base, f"/api/projects/{entry['id']}/close", "POST")
                self.assertEqual(status, 200)
                status, _ = self.request(base, f"/api/projects/{other['id']}/open", "POST")
                self.assertEqual(status, 200)
                self.request(base, f"/api/projects/{other['id']}/close", "POST")

    def test_close_control_posts_from_mission_control_and_returns_to_projects(self):
        code = self.base / "close-from-board"; code.mkdir()
        entry = registry.register(self.home, "close from board", code)

        class FakeProc:
            pid = 4242
            terminated = False
            def poll(self): return None
            def terminate(self): self.terminated = True
            def wait(self, timeout=None): return 0

        process = FakeProc()
        with self.served() as (base, manager):
            with patch.object(subprocess, "Popen", return_value=process):
                status, _ = self.request(base, f"/api/projects/{entry['id']}/open", "POST")
                self.assertEqual(status, 200)
                token = manager.worker_action_token
                missing = Request(
                    base + f"/projects/{entry['id']}/close",
                    data=b"action_token=", method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with self.assertRaises(HTTPError) as refused:
                    urlopen(missing, timeout=5)
                self.assertEqual(refused.exception.code, 400)
                self.assertIsNotNone(registry.active_project(self.home))
                request = Request(
                    base + f"/projects/{entry['id']}/close",
                    data=("action_token=" + token).encode(), method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with urlopen(request, timeout=5) as response:
                    returned_page = response.read().decode()

        self.assertIn("NoMoreHappyPath", returned_page)
        self.assertTrue(process.terminated)
        self.assertIsNone(registry.active_project(self.home))

    def test_close_api_rejects_cross_site_form_encoding(self):
        code = self.base / "api-close-form"; code.mkdir()
        entry = registry.register(self.home, "api close form", code)
        with self.served() as (base, _):
            request = Request(
                base + f"/api/projects/{entry['id']}/close",
                data=b"", method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with self.assertRaises(HTTPError) as refused:
                urlopen(request, timeout=5)
        self.assertEqual(refused.exception.code, 400)

    # ---- S-MGR-004: unhealthy row cannot open; repair re-points; remove never deletes ----
    def test_repair_and_remove(self):
        code = self.base / "delta"; code.mkdir()
        entry = registry.register(self.home, "delta", code)
        moved = self.base / "delta-moved"
        code.rename(moved)
        with self.served() as (base, _):
            status, refused = self.request(base, f"/api/projects/{entry['id']}/open", "POST")
            self.assertEqual(status, 400)
            self.assertIn("repair", refused["error"].lower())
            status, repaired = self.request(base, f"/api/projects/{entry['id']}/repair", "POST", {
                "code_root": str(moved), "data_root": str(moved / ".harness"),
                "workspace_root": str(self.home / "workspaces" / entry["id"])})
            self.assertEqual(status, 200)
            _, listing = self.request(base, "/api/projects")
            self.assertTrue(listing["projects"][0]["health"]["ok"])
            status, removed = self.request(base, f"/api/projects/{entry['id']}", "DELETE")
            self.assertEqual(status, 200)
        self.assertTrue(moved.is_dir(), "remove never touches project folders")
        self.assertEqual(registry.entries(self.home), [])


class BoardProjectIdentityTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    # ---- S-MGR-005: the opened board shows project identity and persistent navigation ----
    def test_rendered_board_carries_identity_and_navigation(self):
        from harness import board_viewer
        page = board_viewer.rendered_page(
            project_name="ktrader <live>", project_description="Trading & brakes",
            manager_url="http://127.0.0.1:8740/", project_id="project/id",
            manager_action_token="token-123", api_prefix="/project")
        self.assertIn("<h1>ktrader &lt;live&gt;</h1>", page)
        self.assertIn("Trading &amp; brakes", page)
        self.assertIn('class="project-topbar" id="project-navigation"', page)
        self.assertIn('id="project-nav-projects" href="http://127.0.0.1:8740/"', page)
        self.assertIn('href="http://127.0.0.1:8740/project/" aria-current="page">Mission Control</a>', page)
        self.assertIn('href="http://127.0.0.1:8740/?page=settings">Settings</a>', page)
        self.assertIn('href="http://127.0.0.1:8740/?page=help">Help</a>', page)
        self.assertIn(".project-topbar{position:fixed;inset:0 0 auto;z-index:60", page)
        self.assertIn(".project-topbar+.wrap{margin-top:68px}", page)
        self.assertIn(".project-topbar+.wrap{margin-top:102px}", page)
        self.assertNotIn("&larr; All projects", page)
        self.assertIn("<title>NoMoreHappyPath</title>", page)
        self.assertIn('id="close-project"', page)
        self.assertIn('id="close-project-dialog"', page)
        self.assertIn('method="post" action="http://127.0.0.1:8740/projects/project%2Fid/close"', page)
        self.assertIn('name="action_token" value="token-123"', page)
        # A standalone viewer (no manager) renders the historical page unchanged.
        default = board_viewer.rendered_page()
        self.assertIn("<h1>NoMoreHappyPath Mission Control</h1>", default)
        self.assertIn("<title>NoMoreHappyPath</title>", default)
        self.assertNotIn('id="project-navigation"', default)
        self.assertNotIn('id="project-nav-projects"', default)
        self.assertNotIn('id="close-project"', default)

    def test_navigation_urls_are_escaped_and_api_prefix_is_validated(self):
        from harness import board_viewer
        page = board_viewer.rendered_page(
            project_name="p", manager_url='http://localhost:8740/\" onclick=\"bad',
            project_id="p", api_prefix="/project",
        )
        self.assertNotIn('onclick="bad"', page)
        self.assertIn('&quot; onclick=&quot;bad/', page)
        with self.assertRaisesRegex(ValueError, "browser API prefix is invalid"):
            board_viewer.rendered_page(
                project_name="p", manager_url="http://127.0.0.1:8740/",
                project_id="p", api_prefix="//evil.example",
            )

    def test_board_page_carries_closed_project_overlay(self):
        from harness import board_viewer
        page = board_viewer.rendered_page(project_name="p", manager_url="http://127.0.0.1:8740/")
        self.assertIn('id="board-offline"', page)
        self.assertIn("This project is closed", page)
        self.assertIn("boardFailures", page)

    def test_opened_board_reads_and_updates_manager_global_agent_settings(self):
        from harness import board_viewer
        with tempfile.TemporaryDirectory() as tmp:
            base_path = Path(tmp)
            root, home = base_path / "project", base_path / "manager"
            root.mkdir()
            selected = {
                "delivery": {"provider": "claude", "model": "sonnet", "effort": "medium"},
                "reviewer": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
                "cto": {"provider": "claude", "model": "opus", "effort": "high"},
            }
            global_settings.update_agent_settings(home, selected)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), board_viewer.make_handler(root, settings_home=home),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                settings = json.loads(urlopen(base + "/api/settings", timeout=3).read())
                managed = json.loads(urlopen(base + "/api/control", timeout=3).read())
                self.assertEqual(settings["settings_scope"], "global")
                self.assertEqual(settings["agent_settings"], selected)
                self.assertEqual(managed["agent_settings"], selected)

                changed = json.loads(json.dumps(selected))
                changed["cto"] = {"provider": "claude", "model": "haiku", "effort": "low"}
                request = Request(
                    base + "/api/settings", data=json.dumps({"settings": changed}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                response = json.loads(urlopen(request, timeout=3).read())
                self.assertEqual(response["agent_settings"], changed)
                self.assertEqual(global_settings.load(home)["agent_settings"], changed)

                with patch.object(board_viewer, "launch_terminal", return_value=None):
                    launch = Request(
                        base + "/api/sessions",
                        data=json.dumps({"kind": "codex_delivery", "color": "black"}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST",
                    )
                    launched = json.loads(urlopen(launch, timeout=3).read())["session"]
                self.assertEqual(launched["provider"], changed["delivery"]["provider"])
                self.assertEqual(launched["model"], changed["delivery"]["model"])
                self.assertEqual(launched["effort"], changed["delivery"]["effort"])
                state_path = root / ".harness" / "control" / "sessions.json"
                self.assertNotIn("agent_settings", json.loads(state_path.read_text()))
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()


if __name__ == "__main__":
    unittest.main()


class HelpPageCoverageTests(unittest.TestCase):
    """Help must explain setup and configuration, not only the task workflow."""

    def test_help_covers_setup_chat_key_settings_lifecycle_and_troubleshooting(self):
        from harness.project_manager_page import PAGE
        for marker in (
            "What This App Needs To Run",
            "Switch On Project Chat (OpenAI Key)",
            "Save and connect",
            "Configure The Agents (Settings)",
            "new agent sessions only",
            "Pause, Close, Or Remove A Project",
            "When Something Looks Wrong",
            "harness-next.log",
            "your own accounts",
            "you choose which vendor plays each role",
            "Your Responsibility, And What The Agents Can Do",
            "entirely your own responsibility",
            "be careful which folder you point them at",
            "Legal — No Warranty, No Liability",
            "its total liability is zero",
            "DISCLAIMER",
            'data-page="legal"',
            "Limitation of Liability",
            "FOR FREE USE, IS ZERO",
            "hold harmless KpiMinds LLC",
            "Austin, Travis County, Texas",
            "License — Business Source License 1.1",
            "requires a commercial license from KpiMinds LLC",
            "Build attribution:",
            "BUILT_WITH.md",
            "A task needs all three roles running",
            "Talking To The Agents Directly",
            "asks whether you trust that folder",
            "Go ahead — this is the contract",
            "Agree The Requirements And Say Go Ahead",
            "files them on the board",
            "requires a newer version",
            "out of credit",
            "The app does not crash",
            "Why It Takes Its Time (No Happy Path)",
            "Cross-checking by a competing vendor",
            "Ledgers.",
            "Be careful while work is running",
        ):
            self.assertIn(marker, PAGE, marker)
        # plain language: the new sections must not leak engineer-speak
        help_html = PAGE.split('id="help-page"', 1)[1]
        for banned in ("launchd", "0600", "endpoint", "API route", "ff-only"):
            self.assertNotIn(banned, help_html, banned)


class BuildAttributionTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    """Scaffolded projects carry the disclosed stamp; adopted repos never do."""

    def api(self, url, body):
        request = Request(url, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def serve(self, home):
        manager = project_manager.ProjectManager(home, board_port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_scaffolded_project_carries_the_disclosed_stamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            url = self.serve(base / "home")
            parent = base / "code"; parent.mkdir()
            entry = self.api(url + "/api/projects", {
                "kind": "scaffold", "parent_root": str(parent), "name": "Stamped",
                "description": "stamp test",
            })
            stamp = Path(entry["code_root"]) / "BUILT_WITH.md"
            self.assertTrue(stamp.is_file(), "scaffolded project missing BUILT_WITH.md")
            text = stamp.read_text(encoding="utf-8")
            for marker in ("NoMoreHappyPath", "KpiMinds LLC", "nomorehappypath.com",
                           "Installation:", "Created:", "Legal page"):
                self.assertIn(marker, text)
            recorded = (base / "home" / "installation_id").read_text().strip()
            self.assertIn(recorded, text)
            # the id is stable across projects
            from harness import global_settings
            self.assertEqual(global_settings.installation_id(base / "home"), recorded)

    def test_adopted_repository_is_never_written_to(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            url = self.serve(base / "home")
            code = base / "owned-repo"; code.mkdir()
            (code / "owner.txt").write_text("owner bytes\n", encoding="utf-8")
            before = sorted(path.name for path in code.iterdir())
            self.api(url + "/api/projects", {
                "kind": "adopted", "code_root": str(code), "name": "Adopted",
                "description": "no-touch test",
            })
            after = sorted(path.name for path in code.iterdir())
            self.assertEqual(before, after, "adopted repository was modified")
            self.assertFalse((code / "BUILT_WITH.md").exists())

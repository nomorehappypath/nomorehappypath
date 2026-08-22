# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Deployed-runtime identity and real chat release-gate simulations."""
from __future__ import annotations

import os
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from harness import (
    board, board_surface, control, global_settings, project_manager, project_memory,
    project_registry, project_worker, runtime_identity, runtime_probe,
)
from harness.board_surface import SessionTokenAuthority
from harness.project_context import ProjectContext
from tests.chat_key_support import configure_verified_key
from tests.environment_support import require_loopback


class RuntimeReleaseTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(self.context)
        project_memory.initialize(
            self.context, project_name="Runtime proof", description="Runtime proof facts.",
        )
        with board.locked_state(self.context) as state:
            state["task_owner_directions"]["TASK-E"] = "Deliver chat"
            state["events"].append({
                "kind": "development_complete", "task": "TASK-E",
                "at": board.now(), "sequence": state["next_event"],
                "agent_id": "delivery", "role": "engineering",
                "message": "development complete",
            })
            state["next_event"] += 1
        self.settings = base / "settings"
        global_settings.initialize(self.settings)
        configure_verified_key(self.settings)
        self.commit = "a" * 40
        self.runtime = {
            "version": 1, "commit": self.commit, "tree": "b" * 40,
            "source_digest": "c" * 64, "clean": True, "captured_at": board.now(),
        }

        def answerer(_root, _question, **_kwargs):
            return {
                "answer": "The project is completed; the current task is TASK-E.",
                "source_ids": ["board:runtime-proof"],
                "snapshot": {"at": board.now(), "board_sequence": 1, "digest": "d" * 64},
                "unknown": False,
            }

        service = project_worker.ProjectChatService(
            self.context, self.settings, "runtime-project", answerer=answerer,
        )
        self.addCleanup(service.shutdown)
        endpoint = {"value": ""}
        worker_handler = project_worker.make_handler(
            self.context, authority=SessionTokenAuthority(self.context),
            endpoint=lambda: endpoint["value"], project_name="Runtime proof",
            project_id="runtime-project", settings_home=self.settings,
            chat_service=service, chat_action_token="runtime-chat-token",
            runtime=self.runtime,
            worker_health=lambda: {
                "active": True,
                "interval_seconds": board.WATCHDOG_INTERVAL_SECONDS,
                "cto_poll_deadline_seconds": board.CTO_MONITOR_INTERVAL_SECONDS,
            },
        )
        self.worker = ThreadingHTTPServer(("127.0.0.1", 0), worker_handler)
        self.worker_url = f"http://127.0.0.1:{self.worker.server_address[1]}/"
        endpoint["value"] = self.worker_url.rstrip("/")
        self.worker_thread = threading.Thread(target=self.worker.serve_forever, daemon=True)
        self.worker_thread.start()
        self.addCleanup(self._close_worker)

    def _close_worker(self):
        self.worker.shutdown(); self.worker_thread.join(timeout=3); self.worker.server_close()

    def _manager(self, runtime):
        home = Path(self.temporary.name) / "manager"
        configure_verified_key(home)
        entry = project_registry.register(
            home, "Runtime proof", self.context.code_root, kind="adopted",
            description="Runtime proof facts.", data_root=self.context.data_root,
            workspace_root=self.context.workspace_root,
        )
        manager = project_manager.ProjectManager(
            home, board_port=self.worker.server_address[1],
            runtime=runtime,
        )

        class RunningWorker:
            pid = 1
            def poll(self): return None

        manager.worker = RunningWorker()
        manager.worker_project = entry["id"]
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), project_manager.make_handler(manager),
        )
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        manager.manager_url = url
        manager.public_board_url = url.rstrip("/") + project_manager.PROJECT_ROUTE
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        return manager, server, thread, url

    def test_restart_without_worker_opens_only_the_exact_governed_registry_entry(self):
        exact_id = "exact-project"
        foreign = Path(self.temporary.name) / "foreign"
        foreign.mkdir()
        worker_ready = {
            "ready": True,
            "runtime": self.runtime,
            "project_ref": board_surface.project_id(self.context),
            "surface": {"project_chat": True},
            "watchdog": {
                "active": True,
                "interval_seconds": board.WATCHDOG_INTERVAL_SECONDS,
                "cto_poll_deadline_seconds": board.CTO_MONITOR_INTERVAL_SECONDS,
            },
        }
        state = {"opened": [], "worker": None}
        outer = self

        class RestartedManager(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def send_json(self, value, status=200):
                payload = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers(); self.wfile.write(payload)

            def do_GET(self):
                if self.path == "/api/projects":
                    return self.send_json({"projects": [
                        {
                            "id": "foreign-project", "code_root": str(foreign),
                            "data_root": str(foreign / "data"), "workspace_root": str(foreign / "work"),
                            "active": False,
                        },
                        {
                            "id": exact_id, "code_root": str(outer.context.code_root),
                            "data_root": str(outer.context.data_root),
                            "workspace_root": str(outer.context.workspace_root), "active": False,
                        },
                    ]})
                if self.path == "/api/ready":
                    return self.send_json({
                        "ready": True, "runtime": outer.runtime,
                        "worker": state["worker"], "board_url": outer.worker_url,
                    })
                self.send_error(404)

            def do_POST(self):
                state["opened"].append(self.path)
                if self.path != f"/api/projects/{exact_id}/open":
                    return self.send_json({"error": "wrong project"}, 400)
                state["worker"] = worker_ready
                return self.send_json({"board_url": outer.worker_url})

        server = ThreadingHTTPServer(("127.0.0.1", 0), RestartedManager)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            proof = runtime_probe.verify(
                self.context, self.commit,
                manager_url=f"http://127.0.0.1:{server.server_address[1]}/",
            )
        finally:
            server.shutdown(); thread.join(timeout=3); server.server_close()
        self.assertTrue(proof["deployed_runtime_verified"], proof)
        self.assertTrue(proof["project_auto_opened"])
        self.assertTrue(proof["registry_entry_exact"])
        self.assertEqual(state["opened"], [f"/api/projects/{exact_id}/open"])

    def test_registry_mismatch_duplicate_and_foreign_active_project_fail_without_opening(self):
        exact = {
            "id": "exact", "code_root": str(self.context.code_root),
            "data_root": str(self.context.data_root), "workspace_root": str(self.context.workspace_root),
            "active": False,
        }
        foreign_root = Path(self.temporary.name) / "unrelated"
        foreign_root.mkdir()
        foreign = {
            "id": "foreign", "code_root": str(foreign_root),
            "data_root": str(foreign_root / "data"), "workspace_root": str(foreign_root / "work"),
            "active": False,
        }

        for name, projects, expected_error in (
            ("missing", [foreign], "exactly one entry"),
            ("duplicate", [exact, {**exact, "id": "duplicate"}], "exactly one entry"),
            ("foreign-active", [{**foreign, "active": True}, exact], "different project is active"),
        ):
            opened = []
            outer = self

            class RegistryManager(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    return

                def send_json(self, value):
                    payload = json.dumps(value).encode(); self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers(); self.wfile.write(payload)

                def do_GET(self):
                    if self.path == "/api/ready":
                        return self.send_json({"ready": True, "runtime": outer.runtime, "worker": None})
                    if self.path == "/api/projects":
                        return self.send_json({"projects": projects})
                    self.send_error(404)

                def do_POST(self):
                    opened.append(self.path); self.send_json({})

            with self.subTest(name=name):
                server = ThreadingHTTPServer(("127.0.0.1", 0), RegistryManager)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    proof = runtime_probe.verify(
                        self.context, self.commit,
                        manager_url=f"http://127.0.0.1:{server.server_address[1]}/",
                    )
                finally:
                    server.shutdown(); thread.join(timeout=3); server.server_close()
                self.assertFalse(proof["deployed_runtime_verified"])
                self.assertIn(expected_error, proof["error"])
                self.assertEqual(opened, [])

    def test_governed_repository_comes_from_the_exact_reviewed_task_commit(self):
        target = Path(self.temporary.name) / "governed-target"
        target.mkdir()
        with board.locked_state(self.context) as state:
            state["task_repositories"] = {"TASK-F": str(target)}
            state["qa_request_index"] = {
                "final-task-f": {
                    "id": "final-task-f", "task": "TASK-F",
                    "reviewed_commit": self.commit, "phase": "final_acceptance", "status": "passed",
                }
            }
        self.assertEqual(runtime_probe._governed_code_root(self.context, self.commit), target.resolve())

    def test_existing_worker_for_a_different_project_is_never_replaced_by_the_probe(self):
        foreign_context = ProjectContext(
            Path(self.temporary.name) / "foreign-code",
            Path(self.temporary.name) / "foreign-data",
            Path(self.temporary.name) / "foreign-work",
        )
        foreign_context.code_root.mkdir()
        opened = []
        outer = self

        class WrongWorkerManager(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def send_json(self, value):
                payload = json.dumps(value).encode(); self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers(); self.wfile.write(payload)

            def do_GET(self):
                if self.path == "/api/projects":
                    return self.send_json({"projects": [{
                        "id": "exact", "code_root": str(outer.context.code_root),
                        "data_root": str(outer.context.data_root),
                        "workspace_root": str(outer.context.workspace_root), "active": False,
                    }]})
                if self.path == "/api/ready":
                    return self.send_json({
                        "ready": True, "runtime": outer.runtime, "board_url": outer.worker_url,
                        "worker": {
                            "ready": True, "runtime": outer.runtime,
                            "project_ref": board_surface.project_id(foreign_context),
                            "surface": {"project_chat": True},
                            "watchdog": {"active": True, "interval_seconds": board.WATCHDOG_INTERVAL_SECONDS,
                                         "cto_poll_deadline_seconds": board.CTO_MONITOR_INTERVAL_SECONDS},
                        },
                    })
                self.send_error(404)

            def do_POST(self):
                opened.append(self.path); self.send_json({})

        server = ThreadingHTTPServer(("127.0.0.1", 0), WrongWorkerManager)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            proof = runtime_probe.verify(
                self.context, self.commit,
                manager_url=f"http://127.0.0.1:{server.server_address[1]}/",
            )
        finally:
            server.shutdown(); thread.join(timeout=3); server.server_close()
        self.assertFalse(proof["deployed_runtime_verified"])
        self.assertFalse(proof["project_context_exact"])
        self.assertEqual(opened, [])

    def test_exact_deployed_runtime_and_real_same_origin_chat_are_required(self):
        manager, server, thread, url = self._manager(self.runtime)
        try:
            proof = runtime_probe.verify(self.context, self.commit, manager_url=url)
        finally:
            server.shutdown(); thread.join(timeout=3); server.server_close()
            manager.worker = None
        self.assertTrue(proof["deployed_runtime_verified"], proof)
        self.assertTrue(proof["deployed_chat_verified"])
        self.assertTrue(proof["visible_chat_present"])
        self.assertTrue(proof["worker_watchdog_active"])
        self.assertIn("answer_sha256", proof)
        self.assertNotIn("answer", proof)

    def test_stale_manager_commit_blocks_release_even_when_worker_has_chat(self):
        stale = {**self.runtime, "commit": "e" * 40}
        manager, server, thread, url = self._manager(stale)
        try:
            proof = runtime_probe.verify(self.context, self.commit, manager_url=url)
        finally:
            server.shutdown(); thread.join(timeout=3); server.server_close()
            manager.worker = None
        self.assertFalse(proof["deployed_runtime_verified"])
        self.assertFalse(proof["manager_runtime_exact"])

    def test_board_refuses_owner_test_state_without_deployed_chat_proof(self):
        cto_agent = board.register(
            self.context, "cto", "GLOBAL_MONITOR", vendor="Anthropic",
        )
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        checks.update({
            "runtime_gate_required": True,
            "deployed_runtime_verified": False,
            "deployed_chat_verified": False,
            "head_commit": self.commit,
        })
        with self.assertRaisesRegex(ValueError, "deployed_chat_verified"):
            board.record_release_ready(
                self.context, cto_agent["id"], "TASK-E", checks,
            )
        checks["deployed_runtime_verified"] = True
        checks["deployed_chat_verified"] = True
        released = board.record_release_ready(
            self.context, cto_agent["id"], "TASK-E", checks,
        )
        self.assertEqual(released["status"], "VISUAL_TEST_REQUIRED")

    def test_runtime_identity_ignores_hostile_ambient_git_routing(self):
        base = Path(self.temporary.name) / "identity"
        intended = base / "intended"
        foreign = base / "foreign"
        for repository, marker in ((intended, "intended"), (foreign, "foreign")):
            (repository / "harness").mkdir(parents=True)
            (repository / "harness" / "marker.py").write_text(
                f"MARKER = {marker!r}\n", encoding="utf-8",
            )
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=repository,
                check=True, capture_output=True,
            )
            subprocess.run(["git", "add", "harness"], cwd=repository, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Harness Test",
                    "-c", "user.email=harness@example.invalid",
                    "commit", "-m", marker,
                ], cwd=repository, check=True, capture_output=True,
            )
        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=intended, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        with patch.dict(os.environ, {
            "GIT_DIR": str(foreign / ".git"),
            "GIT_WORK_TREE": str(foreign),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.worktree",
            "GIT_CONFIG_VALUE_0": str(foreign),
        }, clear=False):
            identity = runtime_identity.capture(intended)
        self.assertEqual(identity["commit"], expected)
        self.assertTrue(identity["clean"])


if __name__ == "__main__":
    unittest.main()

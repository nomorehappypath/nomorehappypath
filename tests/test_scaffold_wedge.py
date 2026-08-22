# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The 2026-08-21 overnight wedge can no longer happen.

A scaffold project whose folder was not yet a Git repository began its task
unbound; the only repair was an operation no agent could call; the watchdog
spammed recoveries for four hours instead of surfacing the diagnosis.

Run: PYTHONPATH=. python3 -m unittest tests.test_scaffold_wedge -v
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from harness import board, board_surface, control, project_manager
from harness.project_context import context_from_roots


def _old(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class ScaffoldFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.code_root = base / "product"
        self.code_root.mkdir()
        self.data_root = base / "data"
        self.workspace_root = base / "workspaces"
        self.context = context_from_roots(self.code_root, self.data_root, self.workspace_root)

    def begin_task(self, task="SCAFFOLD-TASK"):
        session = control.create(self.context, "codex_delivery")
        agent = board.register(
            self.context, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.context, session["id"], "Build the product.")
        return agent, board.begin_task(self.context, agent["id"], task)


class RepositoryInitializationTests(ScaffoldFixture):
    def test_begin_task_initializes_the_empty_scaffold_and_binds_the_broker(self):
        agent, event = self.begin_task()
        state = board.snapshot(self.context)
        self.assertEqual(
            Path(state["task_repositories"]["SCAFFOLD-TASK"]).resolve(),
            self.code_root.resolve(),
        )
        workspace = Path(state["task_workspaces"]["SCAFFOLD-TASK"])
        self.assertTrue(workspace.is_dir())
        self.assertTrue(
            str(workspace.resolve()).startswith(str(self.workspace_root.resolve())),
            f"worktree {workspace} must live inside the registered workspace root",
        )
        self.assertIn("SCAFFOLD-TASK", state.get("task_branches", {}))
        self.assertTrue((self.code_root / ".git").exists())

    def test_existing_repository_is_used_not_reinitialized(self):
        from harness import git_process
        git_process.run(["git", "init", "-q", "-b", "main"], cwd=self.code_root, capture_output=True)
        (self.code_root / "seed.txt").write_text("seed")
        git_process.run(["git", "add", "seed.txt"], cwd=self.code_root, capture_output=True)
        git_process.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"],
            cwd=self.code_root, capture_output=True, text=True,
        )
        self.begin_task()
        state = board.snapshot(self.context)
        workspace = Path(state["task_workspaces"]["SCAFFOLD-TASK"])
        self.assertTrue((workspace / "seed.txt").is_file(), "existing history must carry into the worktree")

    def test_bind_repository_without_a_path_binds_the_project_folder(self):
        session = control.create(self.context, "codex_delivery")
        agent = board.register(
            self.context, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.context, session["id"], "Build it.")
        # Simulate the legacy stranded state: task begun with no repository.
        with board.locked_state(self.context) as state:
            state["agents"][agent["id"]]["task"] = "LEGACY-TASK"
            state["agents"][agent["id"]]["status"] = "task_defined"
            state.setdefault("task_owner_directions", {})["LEGACY-TASK"] = "Build it."
        event = board.bind_task_repository(self.context, agent["id"], "")
        state = board.snapshot(self.context)
        self.assertEqual(
            Path(state["task_repositories"]["LEGACY-TASK"]).resolve(),
            self.code_root.resolve(),
        )
        self.assertTrue(
            str(Path(state["task_workspaces"]["LEGACY-TASK"]).resolve()).startswith(
                str(self.workspace_root.resolve()),
            ),
        )

    def test_surface_lets_delivery_bind_but_never_choose_the_path(self):
        self.assertIn("bind-repository", board_surface.DELIVERY_OPERATIONS)
        self.assertIn("--repo", board_surface.PROTECTED_ARGUMENTS)


class RefusalHoldTests(ScaffoldFixture):
    def gateway(self):
        authority = board_surface.SessionTokenAuthority(self.context)
        return board_surface.CommandGateway(self.context, authority)

    def test_repeated_identical_refusals_record_one_visible_hold(self):
        agent, _ = self.begin_task()
        state = board.snapshot(self.context)
        session_id = state["agents"][agent["id"]]["session_id"]
        gateway = self.gateway()
        identity = SimpleNamespace(session_id=session_id)
        for _ in range(board_surface.CommandGateway.REFUSAL_HOLD_THRESHOLD + 2):
            gateway._track_refusal(identity, "start-subtask", "board-derived worktree is outside workspace_root")
        holds = board.snapshot(self.context).get("control_plane_holds", {})
        self.assertEqual(len(holds), 1)
        hold = holds["SCAFFOLD-TASK"]
        self.assertEqual(hold["status"], "open")
        self.assertIn("refused", hold["reason"])
        self.assertIn("outside workspace_root", hold["reason"])

    def test_distinct_refusals_below_threshold_record_nothing(self):
        agent, _ = self.begin_task()
        session_id = board.snapshot(self.context)["agents"][agent["id"]]["session_id"]
        gateway = self.gateway()
        identity = SimpleNamespace(session_id=session_id)
        for index in range(6):
            gateway._track_refusal(identity, "start-subtask", f"different error {index}")
        self.assertEqual(board.snapshot(self.context).get("control_plane_holds", {}), {})

    def test_project_card_shows_the_diagnosis_in_red(self):
        self.begin_task()
        board.record_control_plane_hold(
            self.context, "SCAFFOLD-TASK", "repeated_refusal:start-subtask",
            "The same operation was refused 5 times: worktree is outside workspace_root",
        )
        entry = {
            "id": "p1", "name": "Scaffold", "kind": "scaffold",
            "code_root": str(self.code_root), "data_root": str(self.data_root),
            "workspace_root": str(self.workspace_root),
        }
        row = project_manager.derive_status(entry)
        self.assertIn("refused 5 times", row["control_plane_hold"])
        page = project_manager_page_source()
        self.assertIn("Needs repair: ", page)
        self.assertIn("project.control_plane_hold", page)

    def test_resolved_hold_clears_the_card(self):
        self.begin_task()
        board.record_control_plane_hold(
            self.context, "SCAFFOLD-TASK", "repeated_refusal:x", "The same operation was refused 5 times",
        )
        board.clear_control_plane_hold(self.context, "SCAFFOLD-TASK", "test-repair")
        entry = {
            "id": "p1", "name": "Scaffold", "kind": "scaffold",
            "code_root": str(self.code_root), "data_root": str(self.data_root),
            "workspace_root": str(self.workspace_root),
        }
        self.assertEqual(project_manager.derive_status(entry)["control_plane_hold"], "")


class AcceptedTaskCardTests(ScaffoldFixture):
    def test_accepted_task_with_stale_active_agents_is_not_in_progress(self):
        agent, _ = self.begin_task("DONE-TASK")
        with board.locked_state(self.context) as state:
            state.setdefault("release_decisions", {})["DONE-TASK"] = {
                "task": "DONE-TASK", "decision": "accepted",
                "recorded_at": "2026-08-21T15:00:00+00:00",
            }
            state.setdefault("releases", {})["DONE-TASK"] = {
                "task": "DONE-TASK", "status": "VISUAL_TEST_REQUIRED",
                "cto_id": "cto-1", "recorded_at": "2026-08-21T14:00:00+00:00",
            }
            # The ghost: a dead terminal's agent record still marked active.
            state["agents"][agent["id"]].update({"active": True, "status": "working"})
        entry = {
            "id": "p1", "name": "Done", "kind": "scaffold",
            "code_root": str(self.code_root), "data_root": str(self.data_root),
            "workspace_root": str(self.workspace_root),
        }
        row = project_manager.derive_status(entry)
        self.assertEqual(row["task_counts"]["open"], 0)
        self.assertEqual(row["task_counts"]["passed"], 1)
        self.assertIn("Accepted and complete", row["latest_progress"])
        self.assertFalse(row["running"])

    def test_released_unaccepted_task_awaits_the_owner(self):
        agent, _ = self.begin_task("WAITING-TASK")
        with board.locked_state(self.context) as state:
            state.setdefault("releases", {})["WAITING-TASK"] = {
                "task": "WAITING-TASK", "status": "VISUAL_TEST_REQUIRED",
                "cto_id": "cto-1", "recorded_at": "2026-08-21T14:00:00+00:00",
            }
            state["agents"][agent["id"]].update({"active": True, "status": "working"})
        entry = {
            "id": "p1", "name": "Waiting", "kind": "scaffold",
            "code_root": str(self.code_root), "data_root": str(self.data_root),
            "workspace_root": str(self.workspace_root),
        }
        row = project_manager.derive_status(entry)
        self.assertEqual(row["task_counts"]["awaiting_owner"], 1)
        self.assertEqual(row["task_counts"]["open"], 0)
        self.assertEqual(row["latest_progress"], "Complete — waiting for your test and acceptance.")
        self.assertFalse(row["running"])


def project_manager_page_source() -> str:
    from harness import project_manager_page
    return project_manager_page.PAGE


class HermeticCodexHomeTests(unittest.TestCase):
    """No test run may ever write the owner's real ~/.codex/config.toml."""

    def test_suite_pins_codex_home_away_from_the_real_one(self):
        import os
        pinned = Path(os.environ["CODEX_HOME"]).resolve()
        real = (Path.home() / ".codex").resolve()
        self.assertNotEqual(pinned, real)
        self.assertIn("harness-tests-codex-home-", str(pinned))

    def test_default_codex_config_path_honors_codex_home(self):
        import os, tempfile
        from unittest import mock
        from harness import workspace_settings
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CODEX_HOME": tmp}):
                settings = workspace_settings.load(Path(tmp))
            self.assertEqual(settings["codex"]["config_path"], str(Path(tmp) / "config.toml"))


class AutomaticProviderAccessTests(ScaffoldFixture):
    """The owner's directive: provider access is automatic, never a click."""

    class _Worker:
        pid = 5151

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    def open_with_home(self, codex_home):
        import os
        from unittest import mock
        from harness import project_registry as registry
        home = Path(self.temporary.name) / "manager-home"
        entry = registry.register(home, "auto-access", self.code_root, kind="adopted")
        manager = project_manager.ProjectManager(home, board_port=0)
        with mock.patch("harness.project_manager.subprocess.Popen", return_value=self._Worker()), \
             mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home / ".codex")}):
            opened = manager.open_project(entry["id"])
        manager.close_project(entry["id"])
        return opened

    def test_open_applies_claude_permissions_and_codex_trust_with_zero_clicks(self):
        import json as _json
        codex_home = Path(self.temporary.name) / "codex-home"
        (codex_home / ".codex").mkdir(parents=True)
        (codex_home / ".codex" / "config.toml").write_text('model = "keep-me"\n')
        opened = self.open_with_home(codex_home)
        claude_file = self.code_root / ".claude" / "settings.local.json"
        self.assertTrue(claude_file.is_file(), "Claude permissions must exist after open")
        value = _json.loads(claude_file.read_text())
        self.assertEqual(value["permissions"]["defaultMode"], "bypassPermissions")
        self.assertTrue(any("rm -rf" in rule for rule in value["permissions"]["deny"]))
        config = (codex_home / ".codex" / "config.toml").read_text()
        self.assertIn('model = "keep-me"', config)
        self.assertIn(f'[projects."{self.code_root.resolve()}"]', config)
        self.assertIn('trust_level = "trusted"', config)
        self.assertNotIn("approval_policy", config)
        self.assertNotIn("sandbox_mode", config)
        self.assertEqual(opened["provider_access"]["claude"]["provider"], "claude")
        self.assertEqual(opened["provider_access"]["codex"]["scope"], "project")

    def test_apply_failure_surfaces_but_never_blocks_the_open(self):
        codex_home = Path(self.temporary.name) / "codex-home-broken"
        # config path resolves inside a FILE, so the codex apply must fail.
        (codex_home / ".codex").parent.mkdir(parents=True, exist_ok=True)
        codex_home.joinpath(".codex").write_text("a file where a directory should be")
        opened = self.open_with_home(codex_home)
        self.assertIn("error", opened["provider_access"]["codex"])
        self.assertEqual(opened["provider_access"]["claude"]["provider"], "claude")
        self.assertEqual(opened["activated"]["project_name"], "auto-access")

    def test_access_panel_is_informational_with_no_buttons(self):
        from harness import board_viewer
        page = board_viewer.rendered_page()
        self.assertIn("AI access for this project", page)
        self.assertIn("configured automatically", page)
        self.assertNotIn("access-apply-claude", page)
        self.assertNotIn("access-apply-codex", page)
        self.assertNotIn("Configure Claude permissions", page)


class OpenAlwaysTests(ScaffoldFixture):
    """The owner's rule: any project opens at any time unless deleted."""

    def test_every_card_state_offers_the_open_action(self):
        page = project_manager_page_source()
        self.assertNotIn('data-act="repair"', page)
        self.assertNotIn("Resume project</button>", page)
        self.assertNotIn("View paused board", page)
        self.assertIn('data-act="open"', page)
        self.assertIn("Open project</button>", page)
        self.assertIn("Open Mission Control</button>", page)

    def test_unhealthy_board_opens_and_recovers_instead_of_refusing(self):
        from unittest import mock
        from harness import project_registry as registry
        home = Path(self.temporary.name) / "home"
        entry = registry.register(home, "unhealthy", self.code_root, kind="adopted")
        # Corrupt the board outright: the old behavior refused to open.
        board_dir = Path(entry["data_root"]) / "board"
        board_dir.mkdir(parents=True, exist_ok=True)
        (board_dir / "state.json").write_text("{ not json at all")
        manager = project_manager.ProjectManager(home, board_port=0)

        class Worker:
            pid = 4242

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                return None

        with mock.patch("harness.project_manager.subprocess.Popen", return_value=Worker()):
            opened = manager.open_project(entry["id"])
        self.assertEqual(opened["activated"]["project_id"], entry["id"])
        manager.close_project(entry["id"])

    def test_projects_payload_carries_the_page_version_for_self_reload(self):
        version = project_manager.page_version()
        self.assertEqual(len(version), 16)
        page = project_manager_page_source()
        self.assertIn("__PAGE_VERSION__", page)
        self.assertIn("page_version", page)


class RecoverySuppressionTests(ScaffoldFixture):
    def stale_agent(self, task="SCAFFOLD-TASK"):
        from harness import contract
        agent, _ = self.begin_task(task)
        contract.create_contract(self.context, task, "Build the product.", ["delivery"])
        with board.locked_state(self.context) as state:
            record = state["agents"][agent["id"]]
            record.update({
                "spawned_at": _old(2400), "last_poll_at": _old(1200),
                "last_progress_at": _old(1200), "last_status_at": _old(1200),
            })
        return agent

    def test_blocked_agent_is_not_nudged_every_cycle(self):
        agent = self.stale_agent()
        board.record_control_plane_hold(
            self.context, "SCAFFOLD-TASK", "repeated_refusal:start-subtask",
            "The same operation was refused 5 times",
        )
        board.mark_stalled(self.context)
        first = [
            event for event in board.snapshot(self.context)["events"]
            if event["kind"] == "agent_automatic_recovery_routed" and event.get("agent_id") == agent["id"]
        ]
        self.assertEqual(len(first), 1, "one initial route is allowed under a hold")
        with board.locked_state(self.context) as state:
            record = state["agents"][agent["id"]]
            record.update({
                "last_poll_at": _old(1200), "last_status_at": _old(1200),
                "liveness": "stalled", "recovery_state": "automatic_failed",
                "automatic_recovery_requested_at": _old(400),
            })
        board.mark_stalled(self.context)
        second = [
            event for event in board.snapshot(self.context)["events"]
            if event["kind"] == "agent_automatic_recovery_routed" and event.get("agent_id") == agent["id"]
        ]
        self.assertEqual(len(second), 1, "a blocked agent must not be re-nudged inside the hold window")

    def test_without_a_hold_recovery_routes_normally(self):
        agent = self.stale_agent()
        board.mark_stalled(self.context)
        routed = [
            event for event in board.snapshot(self.context)["events"]
            if event["kind"] == "agent_automatic_recovery_routed" and event.get("agent_id") == agent["id"]
        ]
        self.assertEqual(len(routed), 1)


if __name__ == "__main__":
    unittest.main()

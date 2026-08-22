# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import control, workspace_settings


class WorkspaceSettingsTests(unittest.TestCase):
    def test_defaults_bind_provider_locations_to_registered_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"
            root.mkdir()
            settings = workspace_settings.load(root)
            self.assertEqual(settings["workspace_root"], str(root.resolve()))
            self.assertEqual(settings["claude"]["settings_path"], str(root.resolve() / ".claude" / "settings.local.json"))
            self.assertEqual(settings["codex"]["approval_policy"], "per-launch")
            self.assertEqual(settings["codex"]["sandbox_mode"], "per-launch")
            self.assertEqual(settings["codex"]["scope"], "project")

    def test_update_is_rejected_regardless_of_requested_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"
            root.mkdir()
            with self.assertRaisesRegex(ValueError, "managed from Projects"):
                workspace_settings.update(root, "relative")
            with self.assertRaisesRegex(ValueError, "managed from Projects"):
                workspace_settings.update(root, str(Path(tmp) / "missing"))
            self.assertFalse(workspace_settings.settings_path(root).exists())

    def test_legacy_workspace_override_is_ignored_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"
            selected = Path(tmp) / "selected"
            root.mkdir(); selected.mkdir()
            path = workspace_settings.settings_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"workspace_root": str(selected), "workspace_confirmed": True}))
            loaded = workspace_settings.load(root)
            self.assertEqual(loaded["workspace_root"], str(root.resolve()))
            self.assertEqual(loaded["claude"]["settings_path"], str(root.resolve() / ".claude" / "settings.local.json"))
            self.assertEqual(json.loads(path.read_text())["workspace_root"], str(selected))

    def test_claude_application_preserves_other_settings_and_merges_deny_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"
            root.mkdir()
            settings = workspace_settings.load(root)
            path = root.resolve() / ".claude" / "settings.local.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"custom": {"keep": True}, "permissions": {"deny": ["Bash(custom *)"]}}))
            result = workspace_settings.apply_provider_files(settings, "claude")
            value = json.loads(path.read_text())
            self.assertEqual(value["custom"], {"keep": True})
            self.assertEqual(value["permissions"]["defaultMode"], "bypassPermissions")
            self.assertIn("Bash(custom *)", value["permissions"]["deny"])
            self.assertTrue(all(item in value["permissions"]["deny"] for item in workspace_settings.DEFAULT_DENY))
            self.assertEqual(result["provider"], "claude")

    def test_codex_application_preserves_global_config_and_adds_trusted_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"
            codex_home = Path(tmp) / "codex-home"
            root.mkdir(); codex_home.mkdir()
            config = codex_home / "config.toml"
            config.write_text('model = "keep-me"\n')
            settings = workspace_settings.load(root)
            settings["codex"]["config_path"] = str(config)
            with patch.object(workspace_settings.Path, "home", return_value=codex_home):
                result = workspace_settings.apply_provider_files(settings, "codex")
            content = config.read_text()
            self.assertIn('model = "keep-me"', content)
            # Danger settings are per-launch, never written globally: one
            # project's access must not leak into other projects.
            self.assertNotIn("approval_policy", content)
            self.assertNotIn("sandbox_mode", content)
            self.assertIn(f'[projects."{root.resolve()}"]', content)
            self.assertIn('trust_level = "trusted"', content)
            self.assertEqual(result["provider"], "codex")
            self.assertEqual(result["approval_policy"], "per-launch")
            self.assertEqual(result["scope"], "project")

    def test_codex_application_removes_legacy_global_danger_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"
            config = Path(tmp) / "codex" / "config.toml"
            root.mkdir(); config.parent.mkdir()
            config.write_text(
                'model = "keep-me"\napproval_policy = "never"\n'
                'sandbox_mode = "danger-full-access"\n\n'
                '[projects."/tmp/other"]\ntrust_level = "trusted"\n'
            )
            settings = workspace_settings.load(root)
            settings["codex"]["config_path"] = str(config)
            workspace_settings.apply_provider_files(settings, "codex")
            content = config.read_text()
            self.assertIn('model = "keep-me"', content)
            self.assertNotIn("approval_policy", content)
            self.assertNotIn("sandbox_mode", content)
            self.assertIn('[projects."/tmp/other"]', content)
            self.assertIn(f'[projects."{root.resolve()}"]', content)

    def test_codex_apply_never_adds_danger_keys_anywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"
            config = Path(tmp) / "codex" / "config.toml"
            root.mkdir(); config.parent.mkdir()
            config.write_text('model = "keep-me"\n\n[projects."/tmp/already-trusted"]\ntrust_level = "trusted"\n')
            settings = workspace_settings.load(root)
            settings["codex"]["config_path"] = str(config)
            workspace_settings.apply_provider_files(settings, "codex")
            content = config.read_text()
            self.assertNotIn("approval_policy", content)
            self.assertNotIn("sandbox_mode", content)
            self.assertIn('[projects."/tmp/already-trusted"]', content)

    def test_codex_launch_carries_per_project_scope_flags(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "run_managed_agent.sh").read_text()
        launch = next(line for line in script.splitlines() if "HARNESS_CODEX_BIN" in line and "--cd" in line)
        self.assertIn('-c "approval_policy=never"', launch)
        self.assertIn('-c "sandbox_mode=danger-full-access"', launch)
        self.assertIn('--cd "$execution_root"', launch)

    def test_provider_apply_ignores_legacy_unconfirmed_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"
            root.mkdir()
            path = workspace_settings.settings_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"workspace_root": str(Path(tmp).parent), "workspace_confirmed": False}))
            result = workspace_settings.apply_provider_files(workspace_settings.load(root), "claude")
            self.assertEqual(Path(result["path"]), root.resolve() / ".claude" / "settings.local.json")

    def test_invalid_legacy_settings_shapes_fall_back_to_registered_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"
            root.mkdir()
            path = workspace_settings.settings_path(root)
            path.parent.mkdir(parents=True)
            for raw in ("7", '{"claude":"bypass"}', '{"codex":["never"]}'):
                path.write_text(raw)
                loaded = workspace_settings.load(root)
                self.assertEqual(loaded["workspace_root"], str(root.resolve()))
                self.assertTrue(loaded["workspace_confirmed"])

    def test_codex_project_paths_with_quotes_and_backslashes_remain_valid_toml_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'we"ird\\folder'
            root.mkdir()
            settings = workspace_settings.load(root)
            config = Path(tmp) / "codex.toml"
            settings["codex"]["config_path"] = str(config)
            workspace_settings.apply_provider_files(settings, "codex")
            content = config.read_text()
            self.assertIn('projects."', content)
            self.assertIn('\\"', content)
            self.assertIn('\\\\', content)

    def test_launcher_ignores_corrupt_legacy_override_and_uses_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            target.mkdir()
            settings_file = workspace_settings.settings_path(target)
            settings_file.parent.mkdir(parents=True)
            settings_file.write_text("{ not json")
            fake = Path(tmp) / "fake-claude"
            fake.write_text("#!/bin/bash\npwd\n")
            fake.chmod(0o755)
            session = control.create(target, "claude_reviewer")
            environment = dict(os.environ)
            environment.pop("HARNESS_EXECUTION_ROOT", None)
            environment["HARNESS_CLAUDE_BIN"] = str(fake)
            completed = subprocess.run(
                ["bash", str(Path(__file__).resolve().parents[1] / "scripts" / "run_managed_agent.sh"), "--root", str(target), "--session-id", session["id"], "--kind", "claude_reviewer"],
                capture_output=True, text=True, timeout=30, cwd=tmp, env=environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn("Traceback (most recent call last)", completed.stderr)
            self.assertEqual(
                Path(completed.stdout.strip().splitlines()[-1]),
                Path(os.path.abspath(os.fspath(target))),
            )


if __name__ == "__main__":
    unittest.main()

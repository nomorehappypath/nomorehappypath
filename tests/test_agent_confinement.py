# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A managed Claude terminal cannot write outside its grant, because the OS refuses.

The runner passes Claude bypass mode so it never stops at a prompt; that
removes the CLI's own boundary, so the harness draws one around the whole
process (Seatbelt on macOS, bubblewrap on Linux). These tests prove the
boundary by execution on the platform they run on, and pin the command
shape for the other platform.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import agent_confinement, control, platform_support
from harness.platform_support import defaults, linux
from unittest.mock import patch
from tests.test_conversation_memory import fake_cli, run_runner
from tests.environment_support import require_loopback

ROOT = Path(__file__).resolve().parents[1]


class CommandShapeTests(unittest.TestCase):
    """Each platform's implementation, driven directly; the seam picks one at runtime."""

    def test_macos_profile_denies_writes_except_the_grant_and_the_cli_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"; (home / ".claude").mkdir(parents=True)
            seatbelt = defaults._AgentConfinement()
            # tmpdir pinned: this drives the macOS implementation on every host,
            # and the host's own TMPDIR (Ubuntu: /tmp/…) is not macOS temp space.
            writable = agent_confinement.writable_paths(
                ["/Users/owner/project", "/Users/owner/project/.harness"], home=home, implementation=seatbelt, tmpdir="")
            profile = seatbelt.profile(writable)
            self.assertIn("(deny file-write*)", profile)
            self.assertIn("(allow default)", profile)
            self.assertIn('(allow file-write* (subpath "/Users/owner/project"))', profile)
            self.assertIn('(allow file-write* (subpath "/Users/owner/project/.harness"))', profile)
            self.assertIn(f'(subpath "{(home / ".claude").resolve()}")', profile)
            self.assertIn(f'(literal "{(home / ".claude.json").resolve()}")', profile)
            self.assertNotIn(f'(subpath "{home.resolve()}")', profile, "the owner's home as a whole is never writable")
            self.assertIn('(subpath "/private/tmp")', profile)

    def test_linux_command_binds_root_read_only_and_the_grant_read_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"; home.mkdir()
            project = Path(tmp) / "project"; project.mkdir()
            bubblewrap = linux._BwrapAgentConfinement(which=lambda name: "/usr/bin/bwrap")
            with patch.dict(os.environ, {"HARNESS_BWRAP_BIN": ""}):
                command = agent_confinement.wrap(
                    ["claude", "--model", "opus"], [str(project)], store=Path(tmp) / "store", home=home,
                    implementation=bubblewrap)
            self.assertEqual(command[:2], ["/usr/bin/bwrap", "--die-with-parent"])
            index = command.index("--ro-bind")
            self.assertEqual(command[index + 1:index + 3], ["/", "/"])
            binds = [command[position + 1] for position, item in enumerate(command) if item == "--bind"]
            self.assertIn(str(project.resolve()), binds)
            self.assertIn(str((home / ".claude").resolve()), binds)
            self.assertTrue((home / ".claude").is_dir(), "a missing state directory is created so bwrap can bind it")
            self.assertTrue((home / ".claude.json").is_file())
            self.assertNotIn(str(home.resolve()), binds, "the owner's home as a whole is never writable")
            self.assertNotIn("--unshare-net", command, "network stays open, as for Codex")
            self.assertEqual(command[-4:], ["--", "claude", "--model", "opus"])

    def test_linux_without_bubblewrap_refuses_instead_of_running_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            absent = linux._BwrapAgentConfinement(which=lambda name: None)
            with patch.dict(os.environ, {"HARNESS_BWRAP_BIN": ""}):
                with self.assertRaises(agent_confinement.ConfinementUnavailable):
                    agent_confinement.wrap(["claude"], [tmp], store=Path(tmp) / "store", home=tmp, implementation=absent)
            named = linux._BwrapAgentConfinement(which=lambda name: "/usr/bin/bwrap")
            with patch.dict(os.environ, {"HARNESS_BWRAP_BIN": "/nonexistent/bwrap"}):
                with self.assertRaises(agent_confinement.ConfinementUnavailable):
                    agent_confinement.wrap(["claude"], [tmp], store=Path(tmp) / "store", home=tmp, implementation=named)

    def test_the_seam_selects_an_implementation_for_this_platform(self):
        implementation = platform_support.agent_confinement()
        self.assertTrue(hasattr(implementation, "wrap") and hasattr(implementation, "cli_state_paths"))


class ConfigDirBoundaryTests(unittest.TestCase):
    """Round-2 reviewer finding: CLAUDE_CONFIG_DIR is inherited and was added to the grant unchecked."""

    def test_a_config_dir_that_would_widen_the_grant_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"; home.mkdir()
            seatbelt = defaults._AgentConfinement()
            for widening in (str(home), "/", str(home.parent), "/Users", "/opt/other-config", "/etc"):
                with self.assertRaises(agent_confinement.ConfinementUnavailable, msg=widening):
                    agent_confinement.writable_paths([str(home / "project")], home=home, claude_config_dir=widening,
                                                     implementation=seatbelt, tmpdir="")
            # Inside the home folder, or inside temp space, is fine.
            inside = home / ".config" / "claude"
            paths = agent_confinement.writable_paths([str(home / "project")], home=home, claude_config_dir=str(inside),
                                                     implementation=seatbelt, tmpdir="")
            self.assertIn(str(inside.resolve()), paths)
            self.assertNotIn(str(home.resolve()), paths)
            in_temp = Path(seatbelt.temp_paths()[0]) / "claude-config-probe"
            paths = agent_confinement.writable_paths([str(home / "project")], home=home, claude_config_dir=str(in_temp),
                                                     implementation=seatbelt, tmpdir="")
            self.assertIn(str(in_temp.resolve()), paths)

    def test_an_inherited_tmpdir_that_would_widen_the_grant_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A home OUTSIDE the temp space (a temp-dir home would itself be
            # inside the grant's temp roots, which is the trap this test guards).
            home = Path("/Users/owner-probe-does-not-exist")
            seatbelt = defaults._AgentConfinement()
            for widening in (str(home), "/", "/Users", "/opt"):
                with self.assertRaises(agent_confinement.ConfinementUnavailable, msg=widening):
                    agent_confinement.writable_paths([str(home / "project")], home=home, implementation=seatbelt, tmpdir=widening)
            inside = Path(seatbelt.temp_paths()[0]) / "session-tmp"
            paths = agent_confinement.writable_paths([str(home / "project")], home=home, implementation=seatbelt, tmpdir=str(inside))
            self.assertIn(str(inside.resolve()), paths)
            self.assertNotIn(str(home.resolve()), paths)
            unset = agent_confinement.writable_paths([str(home / "project")], home=home, implementation=seatbelt, tmpdir="")
            self.assertEqual(len(unset), len(paths) - 1)

    def test_the_reviewers_probe_the_home_folder_as_config_dir_cannot_be_wrapped(self):
        if not platform_support.agent_confinement().available():
            self.skipTest("no write-confinement primitive on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"; home.mkdir()
            with self.assertRaises(agent_confinement.ConfinementUnavailable):
                agent_confinement.wrap(["/bin/sh", "-c", f"echo x > '{home}/probe.txt' && echo WROTE_HOME"],
                                       [str(home / "project")], store=Path(tmp) / "store", home=home,
                                       claude_config_dir=str(home))
            self.assertFalse((home / "probe.txt").exists())


class BoundaryByExecutionTests(unittest.TestCase):
    """The real primitive on this platform: a write outside the grant fails, inside succeeds."""

    def setUp(self):
        if not platform_support.agent_confinement().available():
            raise unittest.SkipTest("no write-confinement primitive on this platform")

    def test_a_confined_shell_cannot_write_outside_the_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            granted = Path(tmp) / "granted"; granted.mkdir()
            home = Path(tmp) / "home"; home.mkdir()
            outside = Path.home() / f".nmhp-confinement-probe-{os.getpid()}"
            outside.mkdir()
            self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(outside)]))
            script = (
                f"( echo x > '{outside}/probe.txt' && echo WROTE_OUTSIDE ) 2>/dev/null || echo DENIED_OUTSIDE; "
                f"( echo x > '{granted}/probe.txt' && echo WROTE_GRANTED ) 2>/dev/null || echo DENIED_GRANTED; "
                f"cat '{ROOT / 'README.md'}' > /dev/null && echo READ_OK"
            )
            command = agent_confinement.wrap(["/bin/sh", "-c", script], [str(granted)], store=Path(tmp) / "store", home=home)
            completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            lines = completed.stdout.split()
            self.assertIn("DENIED_OUTSIDE", lines, completed.stdout)
            self.assertIn("WROTE_GRANTED", lines, completed.stdout)
            self.assertIn("READ_OK", lines, "reads outside the grant stay open")
            self.assertFalse((outside / "probe.txt").exists())
            self.assertTrue((granted / "probe.txt").exists())


class RunnerBoundaryTests(unittest.TestCase):
    """The real runner launches the (fake) CLI inside the boundary."""

    def setUp(self):
        require_loopback()
        if not platform_support.agent_confinement().available():
            raise unittest.SkipTest("no write-confinement primitive on this platform")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"; self.root.mkdir()
        self.outside = Path.home() / f".nmhp-runner-probe-{os.getpid()}"
        self.outside.mkdir()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.outside)]))
        probe = self.root / "fake-claude"
        probe.write_text(
            "#!/usr/bin/env bash\n"
            f"( echo x > '{self.outside}/probe.txt' && echo WROTE_OUTSIDE ) 2>/dev/null || echo DENIED_OUTSIDE\n"
            f"( echo x > '{self.root}/.harness/probe.txt' && echo WROTE_DATA_ROOT ) 2>/dev/null || echo DENIED_DATA_ROOT\n"
            f"( echo x > '{self.root.parent}/.harness-task-workspaces/probe.txt' && echo WROTE_WORKSPACE ) 2>/dev/null || echo DENIED_WORKSPACE\n"
            "printf '%s\\n' \"$@\" > \"$HARNESS_CAPTURE\"\n", encoding="utf-8")
        probe.chmod(0o755)
        self.environment = {
            **os.environ,
            "HARNESS_CAPTURE": str(self.root / "captured.txt"),
            "HARNESS_CLAUDE_BIN": str(probe),
            "HARNESS_CODEX_BIN": str(fake_cli(self.root / "fake-codex")),
            "CLAUDE_CONFIG_DIR": str(Path(self.tmp.name) / "claude-config"),
            "CODEX_HOME": str(Path(self.tmp.name) / "codex-home"),
        }

    def test_the_fake_claude_cannot_write_outside_the_grant_but_can_write_the_data_root(self):
        session = control.create(self.root, "claude_cto")
        completed = run_runner(self.root, session, self.environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("DENIED_OUTSIDE", completed.stdout, completed.stdout + completed.stderr)
        self.assertIn("WROTE_DATA_ROOT", completed.stdout, completed.stdout + completed.stderr)
        # The task-workspace root did not exist before the launch; creating it
        # inside the boundary would be a write to its parent, so the harness
        # creates it first (found by the end-to-end run with the real CLI).
        self.assertIn("WROTE_WORKSPACE", completed.stdout, completed.stdout + completed.stderr)
        self.assertFalse((self.outside / "probe.txt").exists())
        argv = (self.root / "captured.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        if isinstance(platform_support.agent_confinement(), linux._BwrapAgentConfinement):
            return
        profiles = list((self.root / ".harness" / "control").glob("agent-sandbox-*.sb"))
        self.assertEqual(len(profiles), 1, "one content-addressed profile in the project's control dir")
        self.assertIn("(deny file-write*)", profiles[0].read_text(encoding="utf-8"))

    def test_the_launch_refuses_when_the_config_dir_would_widen_the_grant(self):
        session = control.create(self.root, "claude_cto")
        widened = {**self.environment, "CLAUDE_CONFIG_DIR": str(Path.home())}
        completed = run_runner(self.root, session, widened)
        self.assertEqual(completed.returncode, 3, completed.stdout + completed.stderr)
        self.assertIn("REFUSED", completed.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR", completed.stderr)
        self.assertFalse((self.root / "captured.txt").exists(), "the CLI was never launched")
        self.assertFalse((self.outside / "probe.txt").exists())

    def test_the_launch_refuses_when_no_boundary_can_be_drawn(self):
        if not isinstance(platform_support.agent_confinement(), linux._BwrapAgentConfinement):
            self.skipTest("the macOS primitive is part of the OS; there is no way to make it unavailable here")
        session = control.create(self.root, "claude_cto")
        broken = {**self.environment, "HARNESS_BWRAP_BIN": "/nonexistent/bwrap"}
        completed = run_runner(self.root, session, broken)
        self.assertEqual(completed.returncode, 3)
        self.assertIn("REFUSED", completed.stderr)
        self.assertFalse((self.root / "captured.txt").exists(), "the CLI was never launched")


if __name__ == "__main__":
    unittest.main()

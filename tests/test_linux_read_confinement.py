# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Linux read confinement for managed Claude/Gemini agents (task 2026-08-28_read-confinement-linux).

Before: the launcher bound the whole filesystem read-only, so an agent could read the owner's home, other projects and
ssh keys. After: the sandbox starts empty and holds only the system folders, the grant and the agent's tools.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import agent_confinement
from harness.platform_support import linux

ROOT = Path(__file__).resolve().parents[1]


def bubblewrap() -> linux._BwrapAgentConfinement:
    return linux._BwrapAgentConfinement(which=lambda name: "/usr/bin/bwrap")


class ArgvShapeTests(unittest.TestCase):
    def wrapped(self, **options) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"; home.mkdir()
            project = Path(tmp) / "project"; project.mkdir()
            with patch.dict(os.environ, {"HARNESS_BWRAP_BIN": ""}):
                return agent_confinement.wrap(["claude"], [str(project)], store=Path(tmp) / "store", home=home,
                                              implementation=bubblewrap(), **options)

    def test_read_confined_launch_does_not_bind_the_whole_filesystem(self):
        command = self.wrapped(read_confined=True, readable_roots=[])
        pairs = [command[i + 1:i + 3] for i, item in enumerate(command) if item == "--ro-bind"]
        self.assertNotIn(["/", "/"], pairs)
        self.assertNotIn(["/", "/"], [command[i + 1:i + 3] for i, item in enumerate(command) if item == "--bind"])
        self.assertIn(["/usr", "/usr"], pairs)
        self.assertNotIn("--unshare-pid", command, "the Claude inbox relay authenticates its supervisor in the host process table")

    def test_the_default_launch_is_exactly_as_before(self):
        command = self.wrapped()
        index = command.index("--ro-bind")
        self.assertEqual(command[index + 1:index + 3], ["/", "/"])
        self.assertNotIn("--ro-bind-try", command)

    def test_the_write_grant_and_protected_paths_are_unchanged_by_read_confinement(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"; home.mkdir()
            project = Path(tmp) / "project"; (project / ".harness").mkdir(parents=True)
            kwargs = dict(store=Path(tmp) / "store", home=home, implementation=bubblewrap(), protected_writes=[str(project / ".harness")])
            with patch.dict(os.environ, {"HARNESS_BWRAP_BIN": ""}):
                before = agent_confinement.wrap(["claude"], [str(project)], **kwargs)
                after = agent_confinement.wrap(["claude"], [str(project)], read_confined=True, readable_roots=[], **kwargs)
            def triples(command):
                return [tuple(command[i:i + 3]) for i, item in enumerate(command) if item in ("--bind", "--tmpfs") or
                        (item == "--ro-bind" and command[i + 1] != "/")]
            wanted = [item for item in triples(before) if item[0] == "--bind" or str(project / ".harness") in item]
            for item in wanted:
                self.assertIn(item, triples(after))

    def test_macos_style_implementation_ignores_the_option(self):
        class NoReads:
            confines_reads = False
            def temp_paths(self): return ["/tmp"]
            def cli_state_paths(self, home, config=None): return []
            def wrap(self, argv, writable, *, store, protected_reads=None, protected_writes=None, denied_executables=None, **extra):
                self.extra = extra
                return list(argv)
        fake = NoReads()
        with tempfile.TemporaryDirectory() as tmp:
            agent_confinement.wrap(["x"], [tmp], store=Path(tmp), home=tmp, implementation=fake, read_confined=True, readable_roots=[])
        self.assertEqual(fake.extra, {}, "an implementation that cannot confine reads is not asked to")

    def test_the_workers_own_commands_are_read_confined_too(self):
        """Codex plan check, finding 1: project test code run by QA/reviewer/CTO must not read what the agent may not."""
        captured = {}

        class Fake:
            confines_reads = True
            def temp_paths(self): return ["/tmp"]
            def cli_state_paths(self, home, config=None): return []
            def wrap(self, argv, writable, *, store, protected_reads=None, protected_writes=None, denied_executables=None, **extra):
                captured.update(extra); return list(argv)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "p"; project.mkdir()
            with patch("harness.agent_confinement.platform_support.browser_host") as host, \
                    patch("harness.project_context.project_context") as context, \
                    patch("harness.agent_grant.agent_protected_roots", return_value=[]):
                host.return_value.inside_os_sandbox.return_value = False
                context.return_value.code_root = project
                context.return_value.workspace_root = project / "ws"
                context.return_value.data_root = project / "data"
                context.return_value.storage_path = lambda name: str(Path(tmp) / name)
                agent_confinement.confine_agent_command(["/bin/sh", "-c", "true"], project, home=Path(tmp) / "h", implementation=Fake())
        self.assertTrue(captured.get("confine_reads"), captured)
        self.assertTrue(captured.get("read_roots") is not None)

    def test_the_sessions_runtime_directory_is_readable_even_if_created_after_the_command_is_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = agent_confinement.session_runtime_roots(tmp, "/data", "sess-1")
            self.assertEqual(len(roots), 1)
            self.assertFalse(Path(roots[0]).exists())
            command = bubblewrap().wrap(["x"], [], store=Path(tmp), confine_reads=True, read_roots=roots)
            tried = [command[i + 1] for i, item in enumerate(command) if item == "--ro-bind-try"]
            self.assertIn(roots[0], tried)
        self.assertEqual(agent_confinement.session_runtime_roots("", "/data", "sess-1"), [])

    def test_launch_probe_uses_the_read_confined_shape(self):
        seen = []

        def fake_run(command, **options):
            seen.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(linux.shutil, "which", return_value="/usr/bin/tool"), \
                patch.object(linux.AGENT_CONFINEMENT, "binary", return_value="/usr/bin/bwrap"), \
                patch.object(linux.subprocess, "run", side_effect=fake_run):
            self.assertEqual(linux.launch_problem(), "")
        probe = seen[0]
        roots = [probe[i + 1:i + 3] for i, item in enumerate(probe) if item == "--ro-bind"]
        self.assertNotIn(["/", "/"], roots, "the probe must build the shape agents are launched in, not a whole-root bind")

    def test_a_path_folder_exposes_that_folder_only_not_its_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / "home"
            (home / "client" / "bin").mkdir(parents=True)
            (home / "client" / "secrets").mkdir()
            roots = agent_confinement.tool_read_roots([], home=home, path_env=str(home / "client" / "bin"))
            self.assertIn(str(home / "client" / "bin"), roots)
            self.assertNotIn(str(home / "client"), roots)

    def test_an_identified_tool_in_a_project_bin_does_not_expose_the_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / "home"
            (home / "client" / "bin").mkdir(parents=True)
            (home / "client" / "secrets").mkdir()
            tool = home / "client" / "bin" / "claude"; tool.write_text("#!/bin/sh\n"); tool.chmod(0o755)
            roots = agent_confinement.tool_read_roots(["claude"], home=home, path_env=str(tool.parent))
            self.assertIn(str(tool.parent), roots)
            self.assertNotIn(str(home / "client"), roots, "a bin folder alone does not make its parent an install prefix")

    def test_a_project_cannot_point_its_git_file_at_the_home_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / "home"; home.mkdir()
            project = Path(tmp).resolve() / "project"; project.mkdir()
            (project / ".git").write_text(f"gitdir: {home}\n")
            self.assertEqual(agent_confinement.git_read_roots([str(project)], home=home), [])
            (project / ".git").write_text(f"gitdir: {home / 'worktrees' / 'x'}\n")
            (home / "worktrees" / "x").mkdir(parents=True); (home / "HEAD").write_text("ref"); (home / "objects").mkdir()
            self.assertEqual(agent_confinement.git_read_roots([str(project)], home=home), [], "the home folder is never granted")

    def test_a_real_linked_worktree_gets_its_git_folder_and_nothing_else(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "repo"; repo.mkdir()
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"], ["worktree", "add", "-q", str(Path(tmp) / "wt")]):
                subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)
            roots = agent_confinement.git_read_roots([str(Path(tmp).resolve() / "wt")], home=Path(tmp).resolve() / "home")
            self.assertEqual(roots, [str(repo / ".git")])

    def test_tool_read_roots_never_include_the_home_folder_or_its_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / "home"
            (home / ".nvm" / "v20" / "bin").mkdir(parents=True); (home / ".nvm" / "v20" / "lib").mkdir()
            (home / "secrets").mkdir()
            cli = home / ".nvm" / "v20" / "bin" / "claude"; cli.write_text("#!/bin/sh\n"); cli.chmod(0o755)
            roots = agent_confinement.tool_read_roots(["claude"], home=home, extra=[str(home), str(home.parent)],
                                                      path_env=str(cli.parent))
            self.assertIn(str(home / ".nvm" / "v20" / "bin"), roots)
            self.assertIn(str(home / ".nvm" / "v20"), roots, "the install prefix beside bin")
            self.assertNotIn(str(home), roots)
            self.assertNotIn(str(home.parent), roots)
            self.assertFalse(any(item.startswith(str(home / "secrets")) for item in roots))


def _real_bwrap_works() -> str | None:
    if not sys.platform.startswith("linux"):
        return "read confinement is Linux-only"
    binary = linux.AGENT_CONFINEMENT.binary()
    if not binary:
        return "bubblewrap is not installed"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            command = linux._BwrapAgentConfinement().wrap(["/bin/true"], [], store=Path(tmp), confine_reads=True, read_roots=[])
            probe = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return f"bubblewrap cannot run here: {error}"
    return None if probe.returncode == 0 else f"bubblewrap cannot create a sandbox here: {probe.stderr.strip()[:120]}"


class RealBubblewrapTests(unittest.TestCase):
    """Runs a real command in the real sandbox. Skips (does not fail) where bubblewrap is unavailable."""

    SCRIPT = r"""
cat "$SECRET" >/dev/null 2>&1 && echo "READ_SECRET" || echo "SECRET_HIDDEN"
cat "$PROJECT/file.txt" 2>/dev/null | grep -q project-data && echo "READ_PROJECT_OK" || echo "PROJECT_UNREADABLE"
echo hello > "$PROJECT/written.txt" 2>/dev/null && echo "WROTE_PROJECT" || echo "PROJECT_NOT_WRITABLE"
echo x > "$OUTSIDE/probe.txt" 2>/dev/null && echo "WROTE_OUTSIDE" || echo "OUTSIDE_NOT_WRITABLE"
"$PYTHON" -c 'print("PYTHON_RUNS")' 2>/dev/null || echo "PYTHON_BROKEN"
"$PYTHON" -c 'import socket; socket.getaddrinfo("example.com", 443); print("NAME_RESOLVES")' 2>/dev/null || echo "NAME_FAILS"
command -v node >/dev/null 2>&1 && { node -e 'console.log("NODE_RUNS")' || echo NODE_BROKEN; }
cat "/proc/$OTHER_PID/environ" >/dev/null 2>&1 && echo "OTHER_ENVIRON_READABLE" || echo "OTHER_ENVIRON_DENIED"
cat "/proc/$OTHER_PID/root$SECRET" >/dev/null 2>&1 && echo "SECRET_VIA_PROC_ROOT" || echo "PROC_ROOT_DENIED"
"""

    def setUp(self):
        reason = _real_bwrap_works()
        if reason:
            self.skipTest(reason)
        # In the real home, not /tmp: /tmp is deliberately shared with the sandbox.
        self.base = Path(tempfile.mkdtemp(prefix=".nmhp-readconf-", dir=Path.home())).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / "home"; (self.home / ".ssh").mkdir(parents=True)
        self.secret = self.home / ".ssh" / "id_rsa"; self.secret.write_text("PRIVATE-KEY-LOOKALIKE\n")
        self.project = self.base / "project"; self.project.mkdir()
        (self.project / "file.txt").write_text("project-data\n")
        self.outside = self.base / "outside"; self.outside.mkdir()

    @staticmethod
    def host_resolves_names() -> bool:
        import socket
        try:
            socket.getaddrinfo("example.com", 443)
            return True
        except OSError:
            return False

    def run_confined(self, *, read_confined: bool) -> str:
        roots = agent_confinement.tool_read_roots(["sh"], home=self.home, extra=[str(ROOT), sys.executable])
        command = agent_confinement.wrap(["/bin/sh", "-c", self.SCRIPT], [str(self.project)], store=self.base / "store",
                                         home=self.home, read_confined=read_confined, readable_roots=roots)
        environment = {**os.environ, "SECRET": str(self.secret), "PROJECT": str(self.project), "OUTSIDE": str(self.outside),
                       "PYTHON": sys.executable, "HOME": str(self.home)}
        environment.pop("HARNESS_BWRAP_BIN", None)
        # A process of the owner OUTSIDE the sandbox, holding a secret in its environment.
        other = subprocess.Popen(["sleep", "60"], env={**environment, "OTHER_SECRET": "TOKEN-LOOKALIKE"})
        self.addCleanup(lambda: (other.kill(), other.wait()))
        environment["OTHER_PID"] = str(other.pid)
        done = subprocess.run(command, capture_output=True, text=True, timeout=120, env=environment)
        return done.stdout + done.stderr

    def test_a_file_in_the_owners_home_is_unreadable_while_the_project_and_the_tools_work(self):
        output = self.run_confined(read_confined=True)
        self.assertIn("SECRET_HIDDEN", output, output)
        self.assertNotIn("READ_SECRET", output)
        self.assertIn("READ_PROJECT_OK", output, output)
        self.assertIn("WROTE_PROJECT", output, output)
        self.assertIn("OUTSIDE_NOT_WRITABLE", output, output)
        self.assertIn("PYTHON_RUNS", output, output)
        if self.host_resolves_names():
            self.assertIn("NAME_RESOLVES", output, output)
        self.assertNotIn("NODE_BROKEN", output)
        self.assertIn("OTHER_ENVIRON_DENIED", output, output)
        self.assertIn("PROC_ROOT_DENIED", output, output)
        self.assertTrue((self.project / "written.txt").is_file())
        self.assertFalse((self.outside / "probe.txt").exists())

    def test_today_s_flags_return_the_secret_which_is_why_this_test_means_something(self):
        output = self.run_confined(read_confined=False)
        self.assertIn("READ_SECRET", output, output)

    def test_a_granted_extra_read_path_becomes_readable(self):
        extra = self.base / "extra"; extra.mkdir(); (extra / "note").write_text("n")
        with patch.dict(os.environ, {agent_confinement.EXTRA_READ_ENV: str(extra)}):
            roots = agent_confinement.tool_read_roots(["sh"], home=self.home)
        self.assertIn(str(extra), roots)


if __name__ == "__main__":
    unittest.main()

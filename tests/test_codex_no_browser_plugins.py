# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A managed Codex agent is launched without Codex's bundled browser / computer-use tools (owner 2026-10-21, the Chrome keychain dialog).

Run:  PYTHONPATH=. python3 -m unittest tests.test_codex_no_browser_plugins -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import codex_isolation

ROOT = Path(__file__).resolve().parents[1]


def stub(name):
    return f'mcp_servers.{name}={{command="/usr/bin/false", enabled=false}}'
CONFIG = """model = "gpt-6.1-sol"

[plugins."chrome@openai-bundled"]
enabled = true

[mcp_servers.node_repl]
command = "/bin/echo"
args = []

[mcp_servers.openaiDeveloperDocs]
url = "https://developers.openai.com/mcp"

[mcp_servers.computer-use]
command = "/bin/echo"
enabled = false

[mcp_servers.my_browser_tool]
command = "/bin/echo"
"""


class IsolationArgumentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "config.toml").write_text(CONFIG)

    def pairs(self, environment):
        arguments = codex_isolation.disable_arguments(environment, str(self.home))
        self.assertTrue(all(arguments[i] == "-c" for i in range(0, len(arguments), 2)))
        return [arguments[i + 1] for i in range(0, len(arguments), 2)]

    def test_only_browser_computer_and_repl_servers_are_switched_off_and_plugins_never_load(self):
        pairs = self.pairs({"CODEX_HOME": str(self.home)})
        self.assertEqual(pairs[0], "features.plugins=false")
        self.assertEqual(sorted(pairs[1:]), [stub("computer-use"), stub("my_browser_tool"), stub("node_repl")])
        self.assertNotIn(stub("openaiDeveloperDocs"), pairs, "unrelated servers of the owner are left alone")

    def test_every_toml_form_of_a_server_definition_is_read(self):
        (self.home / "config.toml").write_text(
            '[mcp_servers."quoted_repl"]  # a comment\ncommand = "/bin/echo"\n'
            "[mcp_servers.'single']\ncommand = \"/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node\"\n"
            'mcp_servers.dotted_cua.command = "/bin/echo"\n'
            "[mcp_servers]\ninline_chrome = { command = \"/bin/echo\" }\nharmless = { command = \"/bin/echo\" }\n"
            '[mcp_servers.docs]\nurl = "https://developers.openai.com/mcp"\n'
            '[mcp_servers.helper]\ncommand = "/Applications/ChatGPT.app/Contents/Resources/SkyComputerUseClient"\n')
        pairs = self.pairs({"CODEX_HOME": str(self.home)})
        self.assertEqual(sorted(pairs[1:]), sorted([
            stub("quoted_repl"),
            stub("single"), stub("dotted_cua"), stub("inline_chrome"),
            stub("helper")]), "by name or by what the server runs; 'harmless' and 'docs' stay")

    def test_a_missing_config_removes_the_plugins_and_invents_no_server(self):
        self.assertEqual(codex_isolation.disable_arguments({"CODEX_HOME": str(self.home / "absent")}, str(self.home)), ["-c", "features.plugins=false"], "no stub server tables that Codex would reject")

    def test_an_unreadable_config_refuses_the_launch_instead_of_launching_without_isolation(self):
        folder_as_config = self.home / "cfg"
        (folder_as_config / "config.toml").mkdir(parents=True)   # exists, cannot be read as a file
        with self.assertRaises(codex_isolation.IsolationError):
            codex_isolation.disable_arguments({"CODEX_HOME": str(folder_as_config)}, str(self.home))
        result = subprocess.run([sys.executable, "-E", str(ROOT / "harness" / "codex_isolation.py")], env=dict(os.environ, CODEX_HOME=str(folder_as_config)), capture_output=True, text=True)
        self.assertEqual(result.returncode, 3)
        self.assertIn("The agent was not started", result.stderr)

    def test_a_trusted_projects_own_config_and_its_parents_are_scanned_too(self):
        project = self.home / "work" / "project" / "sub"
        (project).mkdir(parents=True)
        (self.home / "work" / "project" / ".codex").mkdir()
        (self.home / "work" / "project" / ".codex" / "config.toml").write_text('[mcp_servers.playwright]\ncommand = "/bin/echo"\nargs = ["project-browser-marker"]\n[mcp_servers.tests_db]\ncommand = "/bin/echo"\n')
        pairs = [a for a in codex_isolation.disable_arguments({"CODEX_HOME": str(self.home)}, str(project)) if a != "-c"]
        self.assertIn(stub("playwright"), pairs)
        self.assertNotIn(stub("tests_db"), pairs, "unrelated project servers stay")

    def test_a_real_codex_with_another_codex_home_and_an_ancestor_config_still_starts(self):
        # review round 2: an ancestor's config that this Codex does not load must not make the generated overrides invalid
        if not shutil.which("codex"):
            self.skipTest("needs the Codex CLI")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            (base / ".codex").mkdir()
            (base / ".codex" / "config.toml").write_text('[mcp_servers.node_repl]\ncommand = "/bin/echo"\n')   # an ancestor of the project that this Codex home does not use
            private = base / "private-home"
            private.mkdir()
            project = base / "projects" / "app"
            project.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            (private / "config.toml").write_text(f'[projects."{project}"]\ntrust_level = "trusted"\n[mcp_servers.tests_db]\ncommand = "/bin/echo"\n')
            environment = dict(os.environ, CODEX_HOME=str(private))
            arguments = codex_isolation.disable_arguments(environment, str(project))
            run = subprocess.run(["codex", "mcp", "list", *arguments], cwd=project, env=environment, capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            rows = {line.split()[0]: line for line in run.stdout.splitlines()[1:] if line.split()}
            self.assertRegex(rows["tests_db"], r"\benabled\b")

    def test_the_owners_config_is_only_read(self):
        before = (self.home / "config.toml").read_bytes()
        codex_isolation.disable_arguments({"CODEX_HOME": str(self.home)}, str(self.home))
        self.assertEqual((self.home / "config.toml").read_bytes(), before)

    def test_the_helper_runs_by_path_from_any_project_folder_with_python_dash_E(self):
        # the launch script runs inside the project's folder with -E (which ignores PYTHONPATH): the helper must not need the harness package
        with tempfile.TemporaryDirectory() as unrelated:
            result = subprocess.run([sys.executable, "-E", str(ROOT / "harness" / "codex_isolation.py")], cwd=unrelated, env={"CODEX_HOME": str(self.home), "PATH": os.environ.get("PATH", "")}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[:2], ["-c", "features.plugins=false"])
        self.assertIn(stub("node_repl"), result.stdout.splitlines())

    def test_the_launch_script_runs_the_helper_by_path_checks_it_and_feeds_every_codex_launch(self):
        script = (ROOT / "scripts" / "run_managed_agent.sh").read_text()
        self.assertIn('"$python_bin" -E "$harness_root/harness/codex_isolation.py"', script)
        self.assertNotIn("-m harness.codex_isolation", script, "-E ignores PYTHONPATH: a module import would silently launch without isolation")
        self.assertIn("could not be prepared; the agent was not started", script)
        self.assertLess(script.index("harness/codex_isolation.py"), script.index("launch_visible_cli \"${HARNESS_CODEX_BIN:-codex}\" resume"), "before both the resume and the fresh launch")
        self.assertIn("codex_access+=(\"$isolation_arg\")", script)


@unittest.skipUnless(shutil.which("codex"), "needs the Codex CLI")
class RealCodexTests(unittest.TestCase):
    def test_a_real_codex_lists_no_enabled_browser_tool_server_with_the_launch_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "config.toml").write_text(CONFIG)
            environment = dict(os.environ, CODEX_HOME=str(home))
            arguments = codex_isolation.disable_arguments(environment, str(home))
            plain = subprocess.run(["codex", "mcp", "list"], cwd=home, env=environment, capture_output=True, text=True, timeout=60).stdout
            self.assertRegex(plain, r"node_repl\s.*enabled", "the fixture really defines an enabled tool server")
            run = subprocess.run(["codex", "mcp", "list", *arguments], cwd=home, env=environment, capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            rows = {line.split()[0]: line for line in run.stdout.splitlines()[1:] if line.split()}
            for expected in ("node_repl", "computer-use", "my_browser_tool"):
                self.assertIn(expected, rows)
                self.assertRegex(rows[expected], r"\bdisabled\b", expected)
            self.assertIn("openaiDeveloperDocs", rows, "the owner's other servers still load")
            self.assertRegex(rows["openaiDeveloperDocs"], r"\benabled\b")

    def test_a_real_codex_in_a_trusted_project_with_its_own_browser_server_lists_it_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            home, project = Path(temporary) / "home", Path(temporary).resolve() / "project"
            (project / ".codex").mkdir(parents=True)
            home.mkdir()
            (home / "config.toml").write_text(f'[projects."{project}"]\ntrust_level = "trusted"\n')
            (project / ".codex" / "config.toml").write_text('[mcp_servers.playwright]\ncommand = "/bin/echo"\nargs = ["project-browser-marker"]\n[mcp_servers.tests_db]\ncommand = "/bin/echo"\n')
            environment = dict(os.environ, CODEX_HOME=str(home))
            plain = subprocess.run(["codex", "mcp", "list"], cwd=project, env=environment, capture_output=True, text=True, timeout=60).stdout
            self.assertRegex(plain, r"playwright\s.*enabled", "the fixture's project server is really picked up")
            arguments = codex_isolation.disable_arguments(environment, str(project))
            run = subprocess.run(["codex", "mcp", "list", *arguments], cwd=project, env=environment, capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            rows = {line.split()[0]: line for line in run.stdout.splitlines()[1:] if line.split()}
            self.assertRegex(rows["playwright"], r"\bdisabled\b")
            self.assertRegex(rows["tests_db"], r"\benabled\b")


if __name__ == "__main__":
    unittest.main()

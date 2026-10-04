# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A command an agent wrote runs inside the agent's own write limits.

Security scan 2026-10-04 (OpenAI Codex Security on v0.7.2), finding 2;
spec docs/specs/AGENT_ISOLATION.md §2.2, reviewed. Internal-QA test commands
(certified and uncertified) and CTO health checks ran with the board worker's
full write authority. Each path is exercised through its real dispatch, in a
project OUTSIDE temp space (temp is in every agent's grant):
- a write inside the project still works;
- a write outside the project is denied and does not happen;
- a write into the harness's own storage INSIDE the project is denied;
- where the platform cannot confine, the command is refused, never run open.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from harness import agent_confinement, board, certified_execution, cto, platform_support
from tests.environment_support import home_outside_temp_space

PROBE = r'''
import sys
for label, path in (("INSIDE", sys.argv[1]), ("OUTSIDE", sys.argv[2]), ("HARNESS", sys.argv[3])):
    try:
        with open(path, "w") as handle:
            handle.write("written by an agent's command")
        print(label + "_WRITTEN")
    except OSError:
        print(label + "_DENIED")
'''


def confinable() -> bool:
    return (sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()
            and not platform_support.browser_host().inside_os_sandbox())


class Fixture(unittest.TestCase):
    def setUp(self):
        if not confinable():
            self.skipTest("needs macOS sandbox-exec, outside any sandbox")
        self.base = home_outside_temp_space(self, ".hnconfine-")
        self.code = self.base / "code"
        self.code.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.code / ".harness").mkdir()                 # the scaffold layout: harness storage INSIDE the checkout
        (self.code / "probe.py").write_text(PROBE, encoding="utf-8")
        self.inside_file = self.code / "inside.txt"
        self.outside_file = self.outside / "escaped.txt"
        self.harness_file = self.code / ".harness" / "forged.txt"

    def probe_command(self) -> str:
        return f"{sys.executable} probe.py {self.inside_file} {self.outside_file} {self.harness_file}"

    def assertConfined(self, output: str):
        lines = output.split()
        self.assertIn("INSIDE_WRITTEN", lines, output)
        self.assertIn("OUTSIDE_DENIED", lines, output)
        self.assertIn("HARNESS_DENIED", lines, output)
        self.assertTrue(self.inside_file.exists())
        self.assertFalse(self.outside_file.exists(), "nothing was written outside the project")
        self.assertFalse(self.harness_file.exists(), "nothing was written into the harness's own storage")


class CertifiedPathTests(Fixture):
    def test_a_certified_command_stays_inside_the_agents_limits(self):
        code, output, _ = certified_execution._run_observed(self.probe_command(), self.code, dict(os.environ), 60)
        self.assertEqual(code, 0, output)
        self.assertConfined(output)


class UncertifiedInternalQaTests(Fixture):
    def test_an_uncertified_internal_qa_command_stays_inside_the_agents_limits(self):
        (self.code / "test_probe.py").write_text(
            "import subprocess, sys, unittest\n"
            "class Probe(unittest.TestCase):\n"
            "    def test_writes(self):\n"
            f"        print(subprocess.run([sys.executable, 'probe.py', {str(self.inside_file)!r}, {str(self.outside_file)!r}, "
            f"{str(self.harness_file)!r}], capture_output=True, text=True).stdout)\n", encoding="utf-8")
        with mock.patch.object(board.execution_preflight, "validate_commands", return_value=None):
            output = board._run_internal_qa("python3 -m unittest -v test_probe", self.code)
        self.assertConfined(output)


class CtoHealthTests(Fixture):
    def test_a_cto_health_command_stays_inside_the_agents_limits(self):
        git = shutil.which("git")
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "T"]):
            subprocess.run([git, *args], cwd=self.code, check=True)
        subprocess.run([git, "add", "probe.py"], cwd=self.code, check=True)
        subprocess.run([git, "commit", "-qm", "probe"], cwd=self.code, check=True)
        commit = subprocess.check_output([git, "rev-parse", "HEAD"], cwd=self.code, text=True).strip()
        review = {"reviewed_commit": commit, "reviewed_files": ["probe.py"]}
        health = f"{sys.executable} probe.py {self.inside_file} {self.outside_file} {self.harness_file}"
        artifact = cto._task_artifact_gate(self.code, "TASK", self.code, review, True, health, check_remote=False)
        self.assertConfined(artifact.get("artifact_health_output", ""))


class RefusalTests(Fixture):
    def test_without_the_platform_primitive_the_command_is_refused_not_run_open(self):
        marker = self.outside / "ran-open.txt"
        with mock.patch.object(type(platform_support.agent_confinement()), "available", return_value=False):
            with self.assertRaises(agent_confinement.ConfinementUnavailable):
                agent_confinement.confine_agent_command(["/bin/sh", "-c", f"touch {marker}"], self.code)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()

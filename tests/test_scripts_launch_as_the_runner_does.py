# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Every harness script the shell launchers exec must start the way they exec it.

On 2026-09-22 no agent could be launched from Mission Control: the runner
execs `python3 -E harness/interactive_supervisor.py` from a foreign directory,
and that file imported `harness` above the sys.path bootstrap that makes the
package importable. 1275 tests were green, because every one of them imported
the module through the package. A source-order pin existed for one other
script and did not cover this one.

So this test does what the launchers do. It enumerates the scripts from the
shell scripts themselves (a hand-kept list would drift), and starts each with
`-E`, no PYTHONPATH, from a temporary working directory.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHED = re.compile(r"-E\s+\"?\$[A-Za-z_]*/(harness/[A-Za-z_]+\.py)\"?")


def scripts_launched_with_dash_e() -> list[Path]:
    found: set[str] = set()
    for shell in sorted((ROOT / "scripts").glob("*.sh")):
        found.update(LAUNCHED.findall(shell.read_text(encoding="utf-8")))
    return [ROOT / item for item in sorted(found)]


class ScriptsLaunchAsTheRunnerDoesTests(unittest.TestCase):
    def test_the_enumeration_finds_the_supervisor_and_the_board(self):
        names = {path.name for path in scripts_launched_with_dash_e()}
        self.assertIn("interactive_supervisor.py", names)
        self.assertIn("board.py", names)

    def test_every_launched_script_imports_its_package_under_dash_e_from_a_foreign_cwd(self):
        failures = []
        environment = {
            key: value for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME"}
        }
        with tempfile.TemporaryDirectory() as foreign:
            for script in scripts_launched_with_dash_e():
                completed = subprocess.run(
                    [sys.executable, "-E", str(script), "--help"],
                    cwd=foreign, env=environment, capture_output=True, text=True, timeout=60,
                )
                if "ModuleNotFoundError" in completed.stderr or "Traceback" in completed.stderr:
                    failures.append(f"{script.relative_to(ROOT)}: {completed.stderr.strip().splitlines()[-1]}")
        self.assertEqual(failures, [], "scripts that cannot start the way the launcher starts them:\n" + "\n".join(failures))


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The release gate must refuse an environment it cannot verify in.

Three review rounds turned on this and none of them tested it, because the
failing condition never occurs where the author works: a sandbox can permit
`ps` inside the source checkout and deny it inside the ASSEMBLED tree, which
sits outside the approved path. The suite then dies 145 times over on
`ProcessTableUnavailable` while the source-tree suite is perfectly green.

So the condition is reproduced here rather than reasoned about: a `ps` on PATH
that succeeds everywhere except inside the output directory. A release cut from
an environment that cannot read the process table would be unverified, so
Gate 4 refuses - and, critically, never prints its success line.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "make_public_release.sh"


class ReleaseGatePrerequisiteTests(unittest.TestCase):
    def _run_with_ps_denied_inside(self, out: Path) -> subprocess.CompletedProcess:
        shim = Path(tempfile.mkdtemp(prefix="harness-ps-shim-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(shim, ignore_errors=True))
        real_ps = "/bin/ps"
        (shim / "ps").write_text(
            "#!/bin/sh\n"
            f'case "$PWD" in\n  {out}*) exit 1 ;;\nesac\n'
            f'exec {real_ps} "$@"\n',
            encoding="utf-8",
        )
        (shim / "ps").chmod(0o755)
        environment = dict(os.environ, PATH=f"{shim}:{os.environ.get('PATH', '')}")
        return subprocess.run(
            ["bash", str(SCRIPT), str(out), "0.0.0-prerequisite-test"],
            capture_output=True, text=True, timeout=900, env=environment, cwd=str(ROOT),
        )

    def test_gate_refuses_when_the_assembled_tree_cannot_read_the_process_table(self):
        if not Path("/bin/ps").exists():
            self.skipTest("no /bin/ps to shim on this platform")
        with tempfile.TemporaryDirectory() as parent:
            out = Path(parent) / "release-tree"
            completed = self._run_with_ps_denied_inside(out)

        self.assertNotEqual(
            completed.returncode, 0,
            "the release script reported success from an environment that cannot verify the tree",
        )
        self.assertNotIn(
            "All gates passed", completed.stdout,
            "the release script announced success while its own gate could not run",
        )
        self.assertIn("REFUSED", completed.stderr)
        self.assertIn("process table", completed.stderr)
        self.assertIn(
            "ps", completed.stderr,
            "the refusal must name the missing prerequisite, not just fail",
        )

    def test_the_refusal_happens_before_the_suite_not_after_it_fails(self):
        """A refusal after 145 errors would be a diagnosis, not a gate."""
        if not Path("/bin/ps").exists():
            self.skipTest("no /bin/ps to shim on this platform")
        with tempfile.TemporaryDirectory() as parent:
            out = Path(parent) / "release-tree"
            completed = self._run_with_ps_denied_inside(out)
        self.assertNotIn(
            "Ran ", completed.stderr + completed.stdout,
            "the suite ran before the prerequisite was checked",
        )


if __name__ == "__main__":
    unittest.main()

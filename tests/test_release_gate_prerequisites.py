# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
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
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "make_public_release.sh"


class ReleaseGatePrerequisiteTests(unittest.TestCase):
    def _deny_the_process_table(self, out: Path) -> dict:
        """Deny the process table the way THIS platform actually reads it.

        Shimming `ps` was the whole mechanism here, and it is macOS-shaped: on
        Linux the seam reads /proc and never shells out to ps at all, so a
        denied ps changed nothing, the gate correctly did NOT refuse, and the
        test then sat through the entire suite until its own timeout.

        The contract being tested is "an unreadable process table REFUSES", not
        "a missing ps refuses". Each platform is denied by its own mechanism.
        """
        environment = dict(os.environ)
        if sys.platform == "darwin":
            shim = Path(tempfile.mkdtemp(prefix="harness-ps-shim-"))
            self.addCleanup(lambda: __import__("shutil").rmtree(shim, ignore_errors=True))
            (shim / "ps").write_text(
                "#!/bin/sh\n"
                f'case "$PWD" in\n  {out}*) exit 1 ;;\nesac\n'
                'exec /bin/ps "$@"\n',
                encoding="utf-8",
            )
            (shim / "ps").chmod(0o755)
            environment["PATH"] = f"{shim}:{environment.get('PATH', '')}"
            return environment

        # Linux: run the whole gate inside a namespace whose /proc is an empty
        # tmpfs. That is precisely the condition the gate's own comment
        # describes - a sandbox that denies the process table in the assembled
        # tree - reached through the mechanism Linux actually uses.
        #
        # Injecting PYTHONPATH was tried first and does NOT work: the gate's own
        # probe sets PYTHONPATH=. explicitly and overrides anything inherited.
        return environment

    def _linux_denial_prefix(self, out: Path) -> list[str]:
        """Everything read-only EXCEPT where the gate must write its tree.

        A first version bound / read-only and nothing else, so the script could
        not create its output directory and failed on mkdir rather than on the
        denied process table — a test failing for the wrong reason, which is no
        better than passing for the wrong reason.
        """
        writable = str(out.parent)
        return [
            "bwrap", "--ro-bind", "/", "/", "--dev", "/dev",
            "--bind", writable, writable,
            "--tmpfs", "/proc", "--",
        ]

    def _run_with_process_table_denied(self, out: Path) -> subprocess.CompletedProcess:
        environment = self._deny_the_process_table(out)
        command = ["bash", str(SCRIPT), str(out), "0.0.0-prerequisite-test"]
        if sys.platform != "darwin":
            if not shutil.which("bwrap"):
                # Named, not skipped: the condition is unreachable here and the
                # suite must say which coverage it lost, not report OK.
                self.fail("bubblewrap is required to deny /proc for this test; "
                          "install it (apt install bubblewrap)")
            command = self._linux_denial_prefix(out) + command
        return subprocess.run(
            command, capture_output=True, text=True, timeout=900,
            env=environment, cwd=str(ROOT),
        )

    def test_gate_refuses_when_the_assembled_tree_cannot_read_the_process_table(self):
        if sys.platform == "darwin" and not Path("/bin/ps").exists():
            self.skipTest("no /bin/ps to shim on this macOS host")
        with tempfile.TemporaryDirectory() as parent:
            out = Path(parent) / "release-tree"
            completed = self._run_with_process_table_denied(out)

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
        if sys.platform == "darwin" and not Path("/bin/ps").exists():
            self.skipTest("no /bin/ps to shim on this macOS host")
        with tempfile.TemporaryDirectory() as parent:
            out = Path(parent) / "release-tree"
            completed = self._run_with_process_table_denied(out)
        self.assertNotIn(
            "Ran ", completed.stderr + completed.stdout,
            "the suite ran before the prerequisite was checked",
        )


if __name__ == "__main__":
    unittest.main()

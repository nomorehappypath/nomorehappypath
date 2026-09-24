# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Stage 1: the shell seam grows a Linux branch.

Everything here runs with `uname` and the service manager STUBBED, so no test
ever installs a real service — an earlier ad-hoc check of mine did exactly that
by calling the real launchctl against a fake HOME.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEAM = ROOT / "scripts" / "platform_support.sh"


def stub_dir(names: dict[str, str]) -> Path:
    directory = Path(tempfile.mkdtemp())
    for name, body in names.items():
        binary = directory / name
        binary.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        binary.chmod(0o755)
    return directory


def run_seam(body: str, home: Path, *, kind: str = "Linux",
             extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    stubs = {"uname": f'[ "$1" = "-s" ] && echo {kind} || echo {kind}',
             "systemctl": "exit 0", "launchctl": "exit 0",
             "loginctl": "echo no", "id": "echo 501"}
    stubs.update(extra or {})
    path = stub_dir(stubs)
    script = f'set -euo pipefail\nsource "{SEAM}"\n{body}\n'
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                          env={"HOME": str(home), "PATH": f"{path}:/usr/bin:/bin"})


class PlatformKindTests(unittest.TestCase):
    def test_linux_is_supported_now(self):
        with tempfile.TemporaryDirectory() as home:
            result = run_seam('platform_supported && echo SUPPORTED', Path(home))
        self.assertIn("SUPPORTED", result.stdout, result.stderr)

    def test_an_unknown_platform_is_still_refused(self):
        """Lifting the refusal for Linux must not lift it for everything."""
        with tempfile.TemporaryDirectory() as home:
            result = run_seam('platform_supported && echo SUPPORTED || echo REFUSED',
                              Path(home), kind="SunOS")
        self.assertIn("REFUSED", result.stdout, result.stderr)


class XdgPathTests(unittest.TestCase):
    def test_the_unit_goes_under_xdg_config(self):
        with tempfile.TemporaryDirectory() as home:
            out = run_seam('service_unit_path', Path(home)).stdout
        self.assertTrue(out.endswith(".config/systemd/user/com.nomorehappypath.app.service"), out)

    def test_logs_go_under_state_not_cache(self):
        """A log the owner is told to read must survive a cache clear."""
        with tempfile.TemporaryDirectory() as home:
            out = run_seam('service_log_path', Path(home)).stdout
        self.assertIn(".local/state/nomorehappypath", out)
        self.assertNotIn(".cache", out)


class RenderedUnitTests(unittest.TestCase):
    def test_the_systemd_unit_restarts_and_logs_where_the_owner_is_told(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            logs = home / "logs"
            result = run_seam(f'autostart_install "/ROOT" "{logs}"', home)
            self.assertEqual(result.returncode, 0, result.stderr)
            unit = home / ".config/systemd/user/com.nomorehappypath.app.service"
            self.assertTrue(unit.is_file(), "no unit was written")
            text = unit.read_text(encoding="utf-8")

        self.assertIn("ExecStart=/bin/bash /ROOT/scripts/start_project_manager.sh --no-open", text)
        self.assertIn("Restart=always", text, "launchd KeepAlive has no counterpart otherwise")
        self.assertIn("WantedBy=default.target", text)
        # The same file the troubleshooting text names, not only the journal.
        self.assertIn(f"StandardOutput=append:{logs}/nomorehappypath.log", text)
        self.assertIn(f"StandardError=append:{logs}/nomorehappypath.log", text)

    def test_stop_leaves_the_unit_and_uninstall_removes_it(self):
        """The deliberate difference, preserved on the second platform too."""
        for call, should_exist in (("autostart_stop", True), ("autostart_remove", False)):
            with tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                unit = home / ".config/systemd/user/com.nomorehappypath.app.service"
                unit.parent.mkdir(parents=True)
                unit.write_text("[Unit]\n", encoding="utf-8")
                result = run_seam(call, home)
                self.assertEqual(result.returncode, 0, f"{call}: {result.stderr}")
                self.assertEqual(unit.exists(), should_exist,
                                 f"{call} left the unit in the wrong state")


class ServiceManagerDetectionTests(unittest.TestCase):
    def test_no_systemd_is_detected_rather_than_assumed(self):
        """A container without systemd must not be told a service was installed."""
        # PATH is the stub directory ALONE, with no /usr/bin:/bin behind it. The
        # seam asks `command -v systemctl`, so on a systemd host the REAL binary
        # answered in place of the absent stub and the test reported the machine
        # it ran on instead of the behaviour: green on macOS, red on Ubuntu.
        # Nothing on this path needs a system binary — sourcing the seam only
        # defines functions, and the sole command it runs is the stubbed `uname`.
        with tempfile.TemporaryDirectory() as temporary:
            path = stub_dir({"uname": "echo Linux", "id": "echo 501"})  # no systemctl
            script = (f'set -euo pipefail\nsource "{SEAM}"\n'
                      # `if`, not `&&`: a bare failing && list aborts under set -e.
                      'if command -v systemctl >/dev/null 2>&1; then echo LEAKED; fi\n'
                      'service_manager_available && echo YES || echo NO\n')
            result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                                    env={"HOME": temporary, "PATH": str(path)})
        # The absence is asserted inside the very shell under test, not assumed:
        # if a systemctl ever reaches this PATH again the test says so, rather
        # than passing for the host's reasons the way it used to on macOS.
        self.assertNotIn("LEAKED", result.stdout, "a real systemctl leaked onto the stub PATH")
        self.assertIn("NO", result.stdout, result.stderr)


class LingerTests(unittest.TestCase):
    """The trap that presents as 'it randomly stopped working'."""

    def test_linger_off_is_reported_as_off(self):
        with tempfile.TemporaryDirectory() as home:
            result = run_seam('linger_enabled && echo ON || echo OFF', Path(home),
                              extra={"loginctl": "echo no"})
        self.assertIn("OFF", result.stdout, result.stderr)

    def test_linger_on_is_reported_as_on(self):
        with tempfile.TemporaryDirectory() as home:
            result = run_seam('linger_enabled && echo ON || echo OFF', Path(home),
                              extra={"loginctl": "echo yes"})
        self.assertIn("ON", result.stdout, result.stderr)

    def test_the_installer_never_enables_lingering_silently(self):
        """It changes system state beyond this app; the owner runs it knowingly."""
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn("loginctl enable-linger", installer.replace("$(linger_command)", ""))
        self.assertIn("linger_command", installer)


class MacosUnchangedTests(unittest.TestCase):
    def test_macos_still_uses_launchagents_and_library_logs(self):
        with tempfile.TemporaryDirectory() as home:
            unit = run_seam('service_unit_path', Path(home), kind="Darwin").stdout
            logs = run_seam('service_log_path', Path(home), kind="Darwin").stdout
        self.assertTrue(unit.endswith("Library/LaunchAgents/com.nomorehappypath.app.plist"), unit)
        self.assertTrue(logs.endswith("Library/Logs"), logs)


if __name__ == "__main__":
    unittest.main()

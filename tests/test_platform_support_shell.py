# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The shell seam — and the differences that must SURVIVE being collapsed.

Three of these tests exist because the obvious refactor is wrong. Uninstall and
stop differ by one line on purpose; the installer's last `open` is deliberately
NOT routed; and the platform gate is still owned by exactly ONE function. A seam
that tidied any of those away would look cleaner and behave differently.

Every test that runs the seam PINS the platform (see `pinned_stubs`). Stage 1
made the seam answer differently on Linux, so an unpinned test reports the host
it happened to run on rather than the behaviour it claims to check.
"""
from __future__ import annotations

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEAM = ROOT / "scripts" / "platform_support.sh"

# Where each platform's service manager keeps the unit, relative to HOME. The
# tests need this to plant an "already installed" unit where the seam will
# actually look: planting the plist on Linux made the teardown tests pass
# without the seam ever touching a file.
UNIT_PATHS = {
    "darwin": "Library/LaunchAgents/com.nomorehappypath.app.plist",
    "linux": ".config/systemd/user/com.nomorehappypath.app.service",
}


def run_seam(body: str, home: Path, extra_path: Path | None = None,
             only_stubs: bool = False) -> subprocess.CompletedProcess:
    """Source the seam and run `body`, with HOME redirected and launchctl stubbed.

    `only_stubs` drops the system PATH entirely. Without it a fallback test on
    macOS finds the REAL `open` and proves nothing — or worse, opens a browser.
    """
    script = f'set -euo pipefail\nsource "{SEAM}"\n{body}\n'
    if only_stubs:
        path = str(extra_path)
    else:
        path = f"{extra_path}:/usr/bin:/bin" if extra_path else "/usr/bin:/bin"
    env = {"HOME": str(home), "PATH": path}
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, env=env)


def stub_dir(names: dict[str, str]) -> Path:
    """A PATH directory of fake executables, so no real service is ever touched."""
    directory = Path(tempfile.mkdtemp())
    for name, body in names.items():
        binary = directory / name
        binary.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        binary.chmod(0o755)
    return directory


def pinned_stubs(platform: str, overrides: dict[str, str] | None = None) -> Path:
    """Stubs that PIN the platform, plus a service manager that always succeeds.

    The seam derives everything from `uname -s`, so shadowing `uname` on PATH
    pins the platform THROUGH the seam's own `platform_kind()` rather than
    around it — the branch under test is still the one the seam chooses. The
    stub defers to the real `uname` for anything but `-s`, so nothing else it
    reports becomes a fiction.

    Unpinned, these tests report the host: on Linux the launchd assertions
    receive a systemd unit and fail, and the teardown assertions pass vacuously
    against a unit path Stage 1 never touches there.
    """
    sysname = {"darwin": "Darwin", "linux": "Linux"}[platform]
    stubs = {
        "uname": 'case "${1:-}" in\n  -s) echo ' + sysname + ' ;;\n  *) exec /usr/bin/uname "$@" ;;\nesac',
        "launchctl": "exit 0",
        "systemctl": "exit 0",
        "id": "echo 501",
    }
    stubs.update(overrides or {})
    return stub_dir(stubs)


class RenderedUnitTests(unittest.TestCase):
    """The unit the installer writes must not drift by a single byte.

    Both platforms are asserted, because the two units are the whole of the
    owner's auto-start behaviour and they are genuinely different files.
    """

    def test_the_rendered_unit_is_exactly_what_the_installer_wrote_before(self):
        """macOS: the launchd LaunchAgent, pinned so Linux runs it too."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            logs = home / "logs"
            result = run_seam(f'autostart_install "/ROOT" "{logs}"', home, pinned_stubs("darwin"))
            self.assertEqual(result.returncode, 0, result.stderr)
            unit = home / UNIT_PATHS["darwin"]
            self.assertTrue(unit.is_file(), "the installer must write the unit")
            self.assertTrue(logs.is_dir(), "the log directory must be created")
            value = plistlib.loads(unit.read_bytes())

        self.assertEqual(value, {
            "Label": "com.nomorehappypath.app",
            "ProgramArguments": ["/bin/bash", "/ROOT/scripts/start_project_manager.sh", "--no-open"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": f"{logs}/nomorehappypath.log",
            "StandardErrorPath": f"{logs}/nomorehappypath.log",
        }, "the rendered unit changed; the owner's auto-start behaviour changed with it")

    def test_the_rendered_systemd_unit_is_exactly_what_stage_1_installs(self):
        """Linux: the same promise, made by a different file.

        Restart=always is the KeepAlive above, and the log goes to the file the
        owner is told to read rather than only the journal, so one set of
        troubleshooting instructions is true on both platforms. Drift in either
        direction breaks that.
        """
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            logs = home / "logs"
            result = run_seam(f'autostart_install "/ROOT" "{logs}"', home, pinned_stubs("linux"))
            self.assertEqual(result.returncode, 0, result.stderr)
            unit = home / UNIT_PATHS["linux"]
            self.assertTrue(unit.is_file(), "the installer must write the unit")
            self.assertTrue(logs.is_dir(), "the log directory must be created")
            text = unit.read_text(encoding="utf-8")

        self.assertEqual(text, (
            "[Unit]\n"
            "Description=NoMoreHappyPath\n"
            "After=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            "ExecStart=/bin/bash /ROOT/scripts/start_project_manager.sh --no-open\n"
            "Restart=always\n"
            "RestartSec=2\n"
            f"StandardOutput=append:{logs}/nomorehappypath.log\n"
            f"StandardError=append:{logs}/nomorehappypath.log\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        ), "the rendered unit changed; the owner's auto-start behaviour changed with it")


class TeardownDifferenceTests(unittest.TestCase):
    """stop and remove differ by one line, and collapsing them is a bug.

    Each platform reaches the promise by different commands — launchctl bootout
    against systemctl stop / disable --now — so each is pinned and asserted. One
    unpinned run would only ever prove it on whichever host happened to execute.
    """

    def _installed_unit(self, home: Path, platform: str) -> Path:
        unit = home / UNIT_PATHS[platform]
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("installed by an earlier run\n", encoding="utf-8")
        return unit

    def test_stop_deactivates_but_LEAVES_the_unit(self):
        for platform in UNIT_PATHS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                unit = self._installed_unit(home, platform)
                result = run_seam("autostart_stop", home, pinned_stubs(platform))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(unit.is_file(),
                                "a plain stop must NOT uninstall; the owner would have to rerun the installer")

    def test_remove_deactivates_AND_deletes_the_unit(self):
        for platform in UNIT_PATHS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                unit = self._installed_unit(home, platform)
                result = run_seam("autostart_remove", home, pinned_stubs(platform))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(unit.exists(), "--uninstall must actually remove the unit")

    def test_both_are_safe_when_nothing_is_installed(self):
        failing = {"launchctl": "exit 1", "systemctl": "exit 1"}
        for platform in UNIT_PATHS:
            for call in ("autostart_stop", "autostart_remove"):
                with self.subTest(platform=platform, call=call), tempfile.TemporaryDirectory() as temporary:
                    home = Path(temporary)
                    result = run_seam(call, home, pinned_stubs(platform, failing))
                    self.assertEqual(result.returncode, 0, f"{call}: {result.stderr}")


class OpenUrlTests(unittest.TestCase):
    """Not pinned: the opener is chosen by what is on PATH, not by platform."""

    def test_a_machine_with_no_opener_prints_the_url_and_succeeds(self):
        """A machine without an opener is not a failed start."""
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(tempfile.mkdtemp())  # PATH with neither open nor xdg-open
            script = f'set -euo pipefail\nsource "{SEAM}"\nowner_open_url "http://example/x"\n'
            result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                                    env={"HOME": temporary, "PATH": str(empty)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("http://example/x", result.stdout,
                      "with no opener the owner must still be told where the app is")

    def test_it_prefers_open_then_falls_back_to_xdg_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            both = stub_dir({"open": "echo OPEN:$1", "xdg-open": "echo XDG:$1"})
            self.assertIn("OPEN:http://x/",
                          run_seam('owner_open_url "http://x/"', home, both, only_stubs=True).stdout)
            only_xdg = stub_dir({"xdg-open": "echo XDG:$1"})
            self.assertIn("XDG:http://x/",
                          run_seam('owner_open_url "http://x/"', home, only_xdg, only_stubs=True).stdout,
                          "the xdg-open fallback is what Linux will use")


class DeliberatelyUnroutedTests(unittest.TestCase):
    """Two things that look like oversights and are decisions."""

    def test_the_installers_last_open_can_no_longer_FAIL_the_install(self):
        """Stage 0 kept this a bare `open`; Stage 1 had to change it.

        The Stage 0 reasoning was right at the time: routing it through a helper
        that succeeds would change the installer's exit behaviour, and Stage 0
        forbade any behaviour change. This test pinned that decision, and it
        caught this edit — which is what it was for.

        Supporting Linux inverted the calculation. A reviewer proved on Ubuntu
        that with the service installed and the app answering, a failing or
        absent opener made the installer exit 3 IMMEDIATELY AFTER printing that
        NoMoreHappyPath was running. Headless is the normal case on Linux, so
        the exception that was correct in Stage 0 became the defect in Stage 1.

        What is asserted now is the property that always mattered: the final
        open cannot turn a successful install into a reported failure.
        """
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('owner_open_url "http://127.0.0.1:8740/"', text,
                      "the success-path open must go through the seam")
        self.assertNotIn('\n        open "http://127.0.0.1:8740/"', text,
                         "a bare open under set -e fails the install on a headless box")

    def test_the_platform_gate_lives_in_exactly_ONE_function(self):
        """Stage 0 routed the decision here; Stage 1 lifted it here.

        This used to assert the seam still refused off macOS. Stage 1 lifted
        that deliberately, so what survives is the assertion that mattered all
        along: the decision is made in ONE place and no caller re-derives it
        from `uname`. That is what made lifting it a one-line change instead of
        a grep across the tree.
        """
        seam = SEAM.read_text(encoding="utf-8")
        self.assertIn("platform_kind()", seam, "one function must own the answer")
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("if platform_supported; then", installer)

    def test_no_caller_re_derives_the_platform_from_uname(self):
        """The seam is worthless if callers ask `uname` themselves."""
        for name in ("install.sh", "scripts/stop_all.sh",
                     "scripts/start_project_manager.sh", "scripts/start_board_viewer.sh"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("== \"Darwin\"", text,
                             name + " re-derives the platform instead of asking the seam")


class DeadChooserTests(unittest.TestCase):
    def test_the_unreachable_macos_only_chooser_is_gone(self):
        """Zero callers, and it refused off macOS — a landmine for Stage 1."""
        import harness.workspace_settings as workspace_settings
        self.assertFalse(hasattr(workspace_settings, "choose_folder"))

    def test_nothing_in_the_tree_calls_it(self):
        found = [
            f"{path.relative_to(ROOT)}:{number}"
            for path in ROOT.rglob("*.py")
            if ".git" not in path.parts and path.name != Path(__file__).name
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1)
            if "workspace_settings.choose_folder" in line
        ]
        self.assertEqual(found, [], f"the deleted chooser is still referenced: {found}")


class CallSiteTests(unittest.TestCase):
    def test_every_script_that_calls_the_seam_also_sources_it(self):
        """A call without a source is a runtime failure no unit test would see."""
        for name in ("install.sh", "scripts/stop_all.sh",
                     "scripts/start_project_manager.sh", "scripts/start_board_viewer.sh"):
            text = (ROOT / name).read_text(encoding="utf-8")
            calls = any(fn in text for fn in (
                "owner_open_url", "autostart_install", "autostart_stop",
                "autostart_remove", "platform_supported", "service_unit_path"))
            if calls:
                self.assertIn("platform_support.sh", text, f"{name} calls the seam without sourcing it")


if __name__ == "__main__":
    unittest.main()

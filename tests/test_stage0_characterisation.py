# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Pin the behaviours Stage 0 will move that nothing exercises today.

`docs/specs/LINUX_STAGE0_PLATFORM_SEAM.md` §8.3 names five of them. Stage 0's
contract is that a macOS owner observes nothing change, and that promise is
only as strong as the tests holding it — refactoring code that no test executes
is not a refactor, it is a rewrite with no witness.

These are CHARACTERISATION tests: they record what the product does today,
including the parts that are arguably wrong. Where today's behaviour is a known
wart, the test says so rather than quietly blessing it.

Nothing here installs, activates or removes anything in the owner's real
environment. The service tests run against a temporary HOME with the service
manager replaced by a recorder on PATH, so the rendered unit and the call
ordering are observable while nothing is actually installed.
"""
from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import control, platform_support, project_worker

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_managed_agent.sh"
INSTALLER = ROOT / "install.sh"
STOPPER = ROOT / "scripts" / "stop_all.sh"


class SecondLauncherArgvTests(unittest.TestCase):
    """harness/project_worker.py:239-269 — executed by zero tests before this one.

    Its only previous reference (tests/test_board_surface.py:359) patches it and
    asserts it was NOT called. The spec's §4.1 turns on this launcher differing
    from the board-viewer one, so the difference is pinned here.
    """

    def _launch(self, tmp: str):
        """macOS pinned at the seam, because that is where the choice is made.

        `project_worker.sys.platform` decided this before Stage 1 and decides
        nothing now: the launcher hands argv to `terminal_host()`, and the
        selector reads the platform. Left unpinned, a Linux machine is handed
        the tmux host and every assertion below reports the machine instead of
        the behaviour. The selector still runs for real — this claims Darwin,
        it does not reach past the seam for the macOS host.
        """
        with patch.object(platform_support.defaults.sys, "platform", "darwin"), \
             patch.object(platform_support.defaults.subprocess, "run") as run:
            session = control.create(Path(tmp), "codex_delivery")
            project_worker.launch_terminal(Path(tmp), session, "/tmp/bootstrap.sock")
            return run, session

    def test_argv_prefix_and_runner_are_pinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, _ = self._launch(tmp)
            command = run.call_args.args[0][-1]
            arguments = shlex.split(command.removeprefix("exec "))
            self.assertEqual(arguments[:9], [
                "/usr/bin/env", "-u", "BASH_ENV", "-u", "ENV",
                "/bin/bash", "--noprofile", "--norc", str(RUNNER),
            ])
            self.assertNotIn(";", command, "no shell metacharacter may reach the launcher")

    def test_it_passes_board_bootstrap_and_neither_of_the_other_two_flags(self):
        """The difference from the board-viewer launcher, which §4.1 must preserve."""
        with tempfile.TemporaryDirectory() as tmp:
            run, _ = self._launch(tmp)
            command = run.call_args.args[0][-1]
            self.assertIn("--board-bootstrap", command)
            self.assertIn("/tmp/bootstrap.sock", command)
            self.assertNotIn("--close-terminal-on-exit", command)
            self.assertNotIn("--task", command)

    def test_it_runs_osascript_and_scales_the_role_colour(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, session = self._launch(tmp)
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], "/usr/bin/osascript")
            self.assertEqual(argv[1], "-e")
            script = argv[2]
            self.assertIn('tell application "Terminal"', script)
            self.assertIn("set background color to", script)
            red, green, blue = control.SESSION_COLORS[session.get("color", "black")]["rgb"]
            expected = "{" + ", ".join(str(round(c * 65535 / 255)) for c in (red, green, blue)) + "}"
            self.assertIn(expected, script)

    def test_it_refuses_off_darwin(self):
        """The macOS host asked to act off darwin, addressed by name.

        This is the one pairing the selector cannot produce: a machine claiming
        Linux is handed the tmux host, which launches rather than refusing. The
        behaviour pinned here is the launcher's own — it turns the seam's
        `UnsupportedPlatformOperation` into an owner-facing RuntimeError instead
        of failing silently — so the macOS implementation is named to keep
        asking that question.
        """
        macos_host = platform_support.defaults.TERMINAL_HOST
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(platform_support, "terminal_host", return_value=macos_host), \
             patch.object(platform_support.defaults.sys, "platform", "linux"):
            session = control.create(Path(tmp), "codex_delivery")
            with self.assertRaisesRegex(RuntimeError, "requires macOS Terminal"):
                project_worker.launch_terminal(Path(tmp), session, "/tmp/bootstrap.sock")

    def test_off_darwin_linux_is_handed_a_terminal_rather_than_that_refusal(self):
        """The other half of the difference, asserted rather than left implied.

        The refusal above belongs to the macOS host, not to the product. Pinning
        only the refusal would leave this suite asserting a universal that Stage 1
        made false, which is the same dishonesty as skipping it. The tmux host's
        own launch is covered by tests/test_platform_support_terminalhost.py; what
        is pinned here is that Linux gets a different host at all.
        """
        from harness.platform_support import linux as linux_support

        with patch.object(platform_support.defaults.sys, "platform", "linux"):
            selected = platform_support.terminal_host()
        self.assertIs(selected, linux_support.TERMINAL_HOST)
        self.assertIsNot(selected, platform_support.defaults.TERMINAL_HOST)


class ManagedRunnerContractTests(unittest.TestCase):
    """scripts/run_managed_agent.sh:44, :48-57 — the whole refusal contract, untested."""

    def _run(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(RUNNER), *arguments],
            capture_output=True, text=True, timeout=60,
        )

    def test_unknown_option_exits_two_and_names_it(self):
        completed = self._run("--not-a-real-option")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Unknown option: --not-a-real-option", completed.stderr)

    def test_missing_required_arguments_exit_two(self):
        completed = self._run("--root", str(ROOT))
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Missing managed-session arguments", completed.stderr)

    def test_board_endpoint_without_bootstrap_is_refused_by_name(self):
        completed = self._run(
            "--root", str(ROOT), "--session-id", "s", "--kind", "codex_delivery",
            "--board-endpoint", "http://127.0.0.1:1",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--board-endpoint bootstrap is no longer accepted", completed.stderr)
        self.assertIn("use --board-bootstrap", completed.stderr)

    def test_data_root_and_workspace_root_are_required_together(self):
        for flag in ("--data-root", "--workspace-root"):
            with self.subTest(flag=flag):
                completed = self._run(
                    "--root", str(ROOT), "--session-id", "s", "--kind", "codex_delivery",
                    flag, str(ROOT),
                )
                self.assertEqual(completed.returncode, 2)
                self.assertIn("required together", completed.stderr)

    def test_a_missing_project_root_is_refused(self):
        completed = self._run(
            "--root", "/nonexistent/project/root",
            "--session-id", "s", "--kind", "codex_delivery",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Project root does not exist", completed.stderr)

    def test_an_option_missing_its_value_exits_two(self):
        completed = self._run("--root")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("requires", completed.stderr)


SERVICE_LABEL = "com.nomorehappypath.app"


def launch_agent_unit(home: Path) -> Path:
    """The unit file path, built exactly as install.sh:9 builds it.

    `plist="$HOME/Library/LaunchAgents/$label.plist"` — the label already ends
    in `.app`, so the file is `com.nomorehappypath.app.plist`. An earlier
    version of this module derived it with `Path(label).with_suffix(".plist")`,
    which REPLACES `.app` and yields `com.nomorehappypath.plist` — a file that
    can never exist. The hermeticity guard below therefore passed while
    checking nothing, which is the precise failure this suite exists to catch.
    One construction now serves both the shim and the guard, so they cannot
    disagree.
    """
    return Path(home) / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


class _ShimmedService:
    """A temporary HOME plus a recorder standing in for the service manager.

    The installer and the stop script both drive a real service manager. These
    tests must exercise those branches WITHOUT installing anything on the
    owner's machine, so `launchctl` (and the installer's `curl`/`open`) are
    replaced on PATH by recorders. Everything the scripts write lands under a
    temporary HOME.

    `uname` is a recorder too, and for the same reason the Python tests pin
    `sys.platform`: the scripts ask the shell seam which platform this is, and
    Stage 1 gave that question two answers. Unpinned, these classes render a
    systemd unit on Linux and every launchd assertion below describes nothing.
    The pin claims Darwin and lets `platform_kind()` run for real; the systemd
    branch has its own suite in tests/test_platform_support_linux_service.py.
    """

    LABEL = SERVICE_LABEL

    def __init__(self, case: unittest.TestCase):
        home = tempfile.TemporaryDirectory(prefix="harness-charact-home-")
        shim = tempfile.TemporaryDirectory(prefix="harness-charact-shim-")
        case.addCleanup(home.cleanup)
        case.addCleanup(shim.cleanup)
        self.home = Path(home.name)
        self.shim = Path(shim.name)
        self.calls = self.shim / "launchctl.calls"
        self._write("uname", '#!/bin/sh\necho Darwin\n')      # these classes pin launchd
        self._write("launchctl", f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.calls}"\nexit 0\n')
        self._write("curl", "#!/bin/sh\nexit 0\n")          # the installer's readiness probe
        self._write("open", "#!/bin/sh\nexit 0\n")          # never open a real browser
        self.plist = launch_agent_unit(self.home)

    def _write(self, name: str, body: str) -> None:
        path = self.shim / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def environment(self) -> dict:
        return dict(os.environ, HOME=str(self.home),
                    PATH=f"{self.shim}:{os.environ.get('PATH', '')}")

    def recorded_calls(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []


class ServiceInstallBranchTests(unittest.TestCase):
    """install.sh:90-119 — the entire service branch, unexecuted before this.

    The only existing installer test stops at `--check` (install.sh:82), before
    the prompt.
    """

    def setUp(self):
        # This class used to skip off darwin, citing an installer refusal that
        # Stage 1 removed — so on Linux it reported OK while executing not one
        # line of the launchd branch it exists to pin. The recorder claims the
        # platform instead, and the class runs everywhere.
        self.service = _ShimmedService(self)
        completed = subprocess.run(
            ["bash", str(INSTALLER)], input="1\n", capture_output=True, text=True,
            timeout=180, env=self.service.environment(), cwd=str(ROOT),
        )
        self.completed = completed

    def test_nothing_was_installed_in_the_real_home(self):
        """The hermeticity guarantee this whole class depends on."""
        real = launch_agent_unit(Path(os.path.expanduser("~")))
        self.assertEqual(real.name, "com.nomorehappypath.app.plist",
                         "the guard must name the file the installer actually writes")
        self.assertFalse(real.exists(), "a test wrote into the owner's real LaunchAgents")
        # stderr as well as stdout: now that this class runs off darwin too, the
        # installer aborting under `set -e` says why there and nowhere else.
        self.assertTrue(self.service.plist.exists(),
                        f"the unit was not rendered under the temporary HOME: "
                        f"{self.completed.stdout[-400:]} {self.completed.stderr[-400:]}")

    def test_the_six_rendered_unit_keys_are_pinned(self):
        value = plistlib.loads(self.service.plist.read_bytes())
        self.assertEqual(set(value), {
            "Label", "ProgramArguments", "RunAtLoad", "KeepAlive",
            "StandardOutPath", "StandardErrorPath",
        })
        self.assertEqual(value["Label"], _ShimmedService.LABEL)
        self.assertIs(value["RunAtLoad"], True)
        self.assertIs(value["KeepAlive"], True)
        self.assertEqual(value["ProgramArguments"], [
            "/bin/bash", f"{ROOT}/scripts/start_project_manager.sh", "--no-open",
        ])
        self.assertEqual(value["StandardOutPath"], value["StandardErrorPath"],
                         "stdout and stderr share one log file today")
        self.assertTrue(value["StandardOutPath"].endswith("/nomorehappypath.log"))

    def test_it_deactivates_before_activating(self):
        """Ordering matters: a live unit would otherwise refuse the new one."""
        calls = self.service.recorded_calls()
        bootouts = [i for i, c in enumerate(calls) if c.startswith("bootout")]
        bootstraps = [i for i, c in enumerate(calls) if c.startswith("bootstrap")]
        self.assertTrue(bootouts and bootstraps, f"calls recorded: {calls}")
        self.assertLess(bootouts[0], bootstraps[0])


class HermeticityGuardTests(unittest.TestCase):
    """The guard above must DETECT a unit file, not merely fail to find one.

    Review found it looking at `com.nomorehappypath.plist` — `with_suffix`
    replaces `.app` — so it passed for the wrong reason. These prove the
    construction is right and the detection works, using a temporary home so
    the owner's real LaunchAgents is never written to in order to test the
    thing that protects it.
    """

    def test_the_guard_names_the_file_the_installer_writes(self):
        """The construction moved into the shell seam; the guard follows it there.

        It is NOT loosened to compensate. The point of this test is that the
        path this suite protects is the same path the installer writes, so it
        reads whichever file actually derives that path today.
        """
        installer = INSTALLER.read_text(encoding="utf-8")
        seam = (INSTALLER.parent / "scripts" / "platform_support.sh").read_text(encoding="utf-8")

        self.assertIn('source "$root/scripts/platform_support.sh"', installer,
                      "install.sh no longer derives the unit path itself; it must source the seam")
        self.assertIn('plist="$(service_unit_path)"', installer,
                      "install.sh changed how it derives the unit path; this suite must follow")

        self.assertIn("""service_label() { printf '%s' "com.nomorehappypath.app"; }""", seam)
        self.assertIn('printf \'%s\' "$HOME/Library/LaunchAgents/$(service_label).plist"', seam,
                      "the seam changed how it derives the unit path; this suite must follow")

        self.assertEqual(launch_agent_unit(Path("/somewhere")).name,
                         "com.nomorehappypath.app.plist")

    def test_the_guard_detects_a_planted_unit(self):
        with tempfile.TemporaryDirectory() as home:
            planted = launch_agent_unit(Path(home))
            planted.parent.mkdir(parents=True, exist_ok=True)
            planted.write_bytes(plistlib.dumps({"Label": SERVICE_LABEL}))
            self.assertTrue(planted.exists(),
                            "the guard's path construction cannot see a file at that path")

    def test_the_old_construction_would_have_missed_it(self):
        """The regression, stated so it cannot come back quietly."""
        with tempfile.TemporaryDirectory() as home:
            planted = launch_agent_unit(Path(home))
            planted.parent.mkdir(parents=True, exist_ok=True)
            planted.write_bytes(plistlib.dumps({"Label": SERVICE_LABEL}))
            wrong = (Path(home) / "Library" / "LaunchAgents" / SERVICE_LABEL).with_suffix(".plist")
            self.assertNotEqual(wrong, planted)
            self.assertFalse(wrong.exists(),
                             "with_suffix replaces .app, so the old guard looked at a file "
                             "that never exists and passed for the wrong reason")


class ServiceTakedownTests(unittest.TestCase):
    """scripts/stop_all.sh:31-34 versus install.sh:19-22 — a deliberate asymmetry.

    The stop script deactivates and LEAVES the unit file so the owner need not
    rerun the installer; `--uninstall` deactivates and REMOVES it. One
    `uninstall()` operation would collapse the two, which is why the spec keeps
    them separate.

    Read as launchd, deliberately: the asymmetry is the same on systemd (`stop`
    versus `disable --now` plus removal), and the recorder pins the platform so
    the words asserted here — bootout, the plist path — stay the ones the
    scripts render.
    """

    def setUp(self):
        self.service = _ShimmedService(self)
        self.service.plist.parent.mkdir(parents=True, exist_ok=True)
        self.service.plist.write_bytes(plistlib.dumps({"Label": _ShimmedService.LABEL}))

    def test_stop_all_deactivates_and_deliberately_leaves_the_unit(self):
        completed = subprocess.run(
            ["bash", str(STOPPER)], capture_output=True, text=True, timeout=120,
            env=self.service.environment(), cwd=str(ROOT),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("auto-start service stopped", completed.stdout)
        self.assertIn("bash install.sh", completed.stdout, "it must say how to get the service back")
        self.assertTrue(self.service.plist.exists(),
                        "stop_all.sh must NOT remove the unit file")
        self.assertTrue(any(c.startswith("bootout") for c in self.service.recorded_calls()))

    def test_uninstall_removes_the_unit_that_stop_all_leaves(self):
        completed = subprocess.run(
            ["bash", str(INSTALLER), "--uninstall"], capture_output=True, text=True,
            timeout=120, env=self.service.environment(), cwd=str(ROOT),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Removed the auto-start service", completed.stdout)
        self.assertFalse(self.service.plist.exists(), "--uninstall must remove the unit file")

    def test_list_reports_the_installed_service_without_touching_it(self):
        completed = subprocess.run(
            ["bash", str(STOPPER), "--list"], capture_output=True, text=True, timeout=120,
            env=self.service.environment(), cwd=str(ROOT),
        )
        self.assertIn("auto-start service installed", completed.stdout)
        self.assertTrue(self.service.plist.exists())
        self.assertEqual(self.service.recorded_calls(), [], "--list must not call the service manager")


class LaunchFailureSurfaceTests(unittest.TestCase):
    """board_viewer.py:2297, project_worker.py:490 — no test makes a launch raise.

    The owner-facing message and its 240-character truncation
    (harness/control.py:638) are unasserted anywhere today.
    """

    def test_a_failed_launch_is_recorded_with_a_truncated_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = control.create(Path(tmp), "codex_delivery")
            reason = "unable to open Terminal: " + ("x" * 400)
            failed = control.fail_launch(Path(tmp), session["id"], reason)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(len(failed["reason"]), 240,
                             "the reason is truncated to 240 characters")
            self.assertTrue(failed["reason"].startswith("unable to open Terminal: "))
            self.assertTrue(failed["ended_at"])

    def test_only_a_launching_session_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = control.create(Path(tmp), "codex_delivery")
            control.fail_launch(Path(tmp), session["id"], "first")
            again = control.fail_launch(Path(tmp), session["id"], "second")
            self.assertEqual(again["reason"], "first",
                             "a session already failed is not re-failed with a new reason")

    def test_an_unknown_session_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            control.create(Path(tmp), "codex_delivery")
            with self.assertRaisesRegex(ValueError, "unknown session"):
                control.fail_launch(Path(tmp), "no-such-session", "reason")


if __name__ == "__main__":
    unittest.main()

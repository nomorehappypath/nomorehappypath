# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import browser_acceptance, platform_support
from harness.platform_support import linux


def _identity():
    """The seam the ps calls now go through; patching the old delegate is a no-op.

    Resolves the CURRENT platform, so call it inside `_running_as` or it
    answers for the host rather than for the implementation under test.
    """
    return platform_support.process_identity()


def _running_as(platform_name: str):
    """Pin the platform so a test reports BEHAVIOUR, not the host it ran on.

    Stage 1 selects an implementation for real, so a test that denies `ps` and
    expects a refusal was asserting "this machine is a Mac" as much as anything
    about the product: on Linux the seam reads /proc, executes nothing, and the
    denial lands on nobody. The real selector stays in the path - only the
    answer it reads is fixed - so these still prove that selection works, not
    just that the chosen module does.
    """
    return mock.patch.object(platform_support.sys, "platform", platform_name)


class ProcessTableAvailabilityTests(unittest.TestCase):
    """A process table that cannot be read is a named refusal, not a raw OSError.

    Found in review: an environment that forbids executing ``ps`` crashed the
    certified-execution evidence path with
    ``PermissionError: [Errno 1] Operation not permitted: 'ps'`` raised from the
    middle of ``_process_table``. Process identity is evidence; being unable to
    collect it must be said plainly, and must never read as an empty table -
    "nothing was running" is a different and much more dangerous claim.
    """

    def test_denied_ps_becomes_a_named_condition_not_a_raw_oserror(self):
        # Pinned to macOS: `ps` is what the macOS seam executes, so denying it
        # is a statement about that implementation. Unpinned, this passed on a
        # Mac and reported the HOST on Linux, where nothing shells out and the
        # patched `subprocess.run` was never reached. The /proc seam owes the
        # same refusal and is held to it in LinuxProcessTableTests below.
        for error in (PermissionError(1, "Operation not permitted", "ps"),
                      FileNotFoundError(2, "No such file or directory", "ps")):
            with self.subTest(error=type(error).__name__):
                with _running_as("darwin"), mock.patch.object(
                        browser_acceptance.subprocess, "run", side_effect=error):
                    with self.assertRaises(browser_acceptance.ProcessTableUnavailable) as caught:
                        browser_acceptance._process_table()
                self.assertIn("process table", str(caught.exception))
                self.assertIn("ps", str(caught.exception))

    def test_denied_ps_never_reads_as_an_empty_process_table(self):
        """The dangerous failure: claiming nothing was running."""
        with _running_as("darwin"), \
                mock.patch.object(browser_acceptance.subprocess, "run",
                                  side_effect=PermissionError(1, "Operation not permitted", "ps")):
            with self.assertRaises(browser_acceptance.ProcessTableUnavailable):
                table = browser_acceptance._process_table()
                self.fail(f"an unreadable process table returned {table!r} instead of refusing")

    def test_start_token_refuses_when_ps_cannot_run(self):
        # Pinned to macOS: the refusal being asserted is about EXECUTING `ps`,
        # and only the macOS seam executes anything to obtain a start token.
        with _running_as("darwin"), \
                mock.patch.object(browser_acceptance.subprocess, "run",
                                  side_effect=PermissionError(1, "Operation not permitted", "ps")):
            with self.assertRaises(browser_acceptance.ProcessTableUnavailable):
                browser_acceptance._start_token(os.getpid())

    def test_a_pid_that_is_simply_gone_still_yields_an_empty_token(self):
        """Behaviour preserved: a dead pid is an ANSWER, not an environment failure.

        Hermetic on purpose. An earlier version of this test called the real
        `ps`, so in a shell that denies `ps` the very test asserting "nothing
        changed" raised ProcessTableUnavailable - it depended on the
        prerequisite it exists to reason about. The distinction under test is
        `_start_token`'s own logic (non-zero exit -> empty token), which needs
        no operating system to prove.

        Pinned to macOS for the same reason it is hermetic: `run_ps` and the
        BSD `lstart` string it returns belong to the macOS seam. Unpinned, the
        patch landed on an object Linux never calls and the assertion measured
        the host's real /proc. The Linux half of this distinction is in
        LinuxProcessTableTests below.
        """
        gone = subprocess.CompletedProcess(args=["ps"], returncode=1, stdout="", stderr="")
        alive = subprocess.CompletedProcess(
            args=["ps"], returncode=0, stdout="Mon Aug 25 10:00:00 2026\n", stderr="",
        )
        with _running_as("darwin"):
            with mock.patch.object(_identity(), "run_ps", return_value=gone):
                self.assertEqual(browser_acceptance._start_token(999999), "")
            with mock.patch.object(_identity(), "run_ps", return_value=alive):
                self.assertEqual(
                    browser_acceptance._start_token(os.getpid()), "Mon Aug 25 10:00:00 2026")

    def test_the_same_distinction_holds_against_the_real_ps(self):
        """The integration truth, skipped only where the OS cannot answer."""
        from tests import environment_support
        environment_support.require_process_table()
        self.assertEqual(browser_acceptance._start_token(999999), "")
        self.assertTrue(browser_acceptance._start_token(os.getpid()))

    def test_release_preview_shares_the_same_refusal(self):
        from harness import release_preview
        with _running_as("darwin"), \
                mock.patch.object(browser_acceptance.subprocess, "run",
                                  side_effect=PermissionError(1, "Operation not permitted", "ps")):
            with self.assertRaises(browser_acceptance.ProcessTableUnavailable):
                release_preview._start_token(os.getpid())

    def test_the_test_guard_skips_instead_of_failing(self):
        from tests import environment_support
        with _running_as("darwin"), \
                mock.patch.object(browser_acceptance.subprocess, "run",
                                  side_effect=PermissionError(1, "Operation not permitted", "ps")):
            with self.assertRaises(unittest.SkipTest):
                environment_support.require_process_table()


class LinuxProcessTableTests(unittest.TestCase):
    """The same distinction, through the seam Linux actually selects.

    Pinning the tests above to macOS keeps them honest, but on its own it would
    leave the invariant they exist for - an unreadable process table is a named
    refusal, never an empty one - asserted on no platform at all when the suite
    runs on the Ubuntu target. These read a /proc built here, so the behaviour
    is proved on any host rather than only where /proc happens to exist.
    """

    def _entry(self, proc: Path, pid: int, start_ticks: str = "919191") -> None:
        """One /proc/<pid> carrying the only three fields the seam reads.

        After the bracketed comm come state, ppid and pgrp; starttime is
        overall field 22, which is why the filler is exactly sixteen wide.
        """
        entry = proc / str(pid)
        entry.mkdir(parents=True)
        (entry / "stat").write_text(
            f"{pid} (headless-shell) S 1 {pid} " + "0 " * 16 + f"{start_ticks}\n",
            encoding="utf-8",
        )
        (entry / "cmdline").write_bytes(b"/opt/browser\x00--headless\x00")

    def test_an_unreadable_proc_is_the_same_named_refusal(self):
        """Not an empty table: "nothing was running" is the dangerous claim."""
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "no-proc"
            with _running_as("linux"), \
                    mock.patch.object(linux.PROCESS_IDENTITY, "PROC", missing):
                with self.assertRaises(browser_acceptance.ProcessTableUnavailable) as caught:
                    table = browser_acceptance._process_table()
                    self.fail(f"an unreadable /proc returned {table!r} instead of refusing")
            self.assertIn(str(missing), str(caught.exception))

    def test_a_pid_that_is_simply_gone_still_yields_an_empty_token(self):
        """A dead pid is an ANSWER here too - and a live one is its start time."""
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary) / "proc"
            self._entry(proc, 4242, start_ticks="919191")
            with _running_as("linux"), \
                    mock.patch.object(linux.PROCESS_IDENTITY, "PROC", proc):
                self.assertEqual(browser_acceptance._start_token(4242), "919191")
                self.assertEqual(browser_acceptance._start_token(999999), "")
                self.assertEqual(set(browser_acceptance._process_table()), {4242})


class LaunchLockReleaseTests(unittest.TestCase):
    """A failed launch must never strand the launch lock.

    Found while reproducing a review failure: `_PROCESS_LOCK.acquire()` was
    followed by an unguarded `_process_table()`, so a shell that denies `ps`
    left both the in-process lock and the file lock held. The next launch then
    blocked forever on `.acquire()` - which is why the reviewer's release-gate
    run never finished and had to be interrupted at that exact line, rather
    than failing with a message.
    """

    def setUp(self):
        # A stranded lock would hang this suite rather than fail it, so refuse
        # to start from a state that is already wrong.
        if not browser_acceptance._PROCESS_LOCK.acquire(blocking=False):
            self.fail("the launch lock was already held before this test began")
        browser_acceptance._PROCESS_LOCK.release()

    def _fail_launch(self, error: Exception) -> None:
        with mock.patch.object(browser_acceptance, "resolve_binary", return_value="/bin/echo"), \
             mock.patch.object(browser_acceptance, "browser_identity", return_value={"sha256": "x"}), \
             mock.patch.object(browser_acceptance, "_binary_digest", return_value="x"), \
             mock.patch.object(browser_acceptance, "_process_table", side_effect=error):
            with tempfile.TemporaryDirectory() as runtime:
                with self.assertRaises(type(error)):
                    browser_acceptance.launch("http://127.0.0.1:1/", Path(runtime))

    def test_an_unreadable_process_table_does_not_strand_the_lock(self):
        self._fail_launch(browser_acceptance.ProcessTableUnavailable("denied"))
        self.assertTrue(
            browser_acceptance._PROCESS_LOCK.acquire(blocking=False),
            "launch() failed and left the process lock held - the next launch would hang",
        )
        browser_acceptance._PROCESS_LOCK.release()

    def test_any_failure_before_the_browser_starts_releases_the_lock(self):
        """Not just the ps case: the handler must cover the whole section."""
        self._fail_launch(RuntimeError("something else entirely"))
        self.assertTrue(
            browser_acceptance._PROCESS_LOCK.acquire(blocking=False),
            "launch() failed and left the process lock held",
        )
        browser_acceptance._PROCESS_LOCK.release()

    def test_two_consecutive_failures_do_not_deadlock(self):
        """The reviewer's symptom exactly: the SECOND call hung, not the first."""
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self._fail_launch(browser_acceptance.ProcessTableUnavailable("denied"))


class BrowserAcceptanceTests(unittest.TestCase):
    def setUp(self):
        # These launch real browsers and certify ownership from the process
        # table; without it there is nothing to assert.
        from tests import environment_support
        environment_support.require_process_table()

    def executable(self, path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_configured_app_bundle_is_rejected_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            browser = self.executable(Path(temporary) / "Owner Browser.app/Contents/MacOS/Browser")
            with mock.patch.dict(os.environ, {"HARNESS_BROWSER_BIN": str(browser)}):
                with self.assertRaisesRegex(ValueError, "outside every macOS .app"):
                    browser_acceptance.resolve_binary()

    def test_loopback_private_port_is_required_and_live_ports_are_refused(self):
        for url in ("https://example.com", "http://127.0.0.1", "http://127.0.0.1:8740", "http://localhost:8742"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                browser_acceptance._validate_url(url)
        self.assertEqual(browser_acceptance._validate_url("http://127.0.0.1:49199/"), 49199)

    def test_launch_has_private_profile_cache_keychain_and_process_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "argv.txt"
            browser = self.executable(
                root / "headless-shell",
                "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo TestBrowser; exit 0; fi\nprintf '%s\\n' \"$@\" > \"$CAPTURE\"\nsleep 20\n",
            )
            with mock.patch.dict(os.environ, {"HARNESS_BROWSER_BIN": str(browser), "CAPTURE": str(capture)}):
                session = browser_acceptance.launch("http://127.0.0.1:49199/", root / "runtime")
                time.sleep(0.1)
                audit = session.close()
            argv = capture.read_text(encoding="utf-8")
            self.assertIn("--use-mock-keychain", argv)
            self.assertIn("--password-store=basic", argv)
            self.assertIn("--remote-debugging-port=0", argv)
            self.assertIn(str(root / "runtime/profile"), argv)
            self.assertEqual(audit["spawn"]["pgid"], audit["spawn"]["pid"])
            self.assertTrue(audit["signals"])
            self.assertTrue(audit["mock_keychain"])

    def test_security_arguments_cannot_be_overridden(self):
        for argument in (
            "--user-data-dir=/tmp/shared", "--password-store=keychain",
            "--remote-debugging-port=9222", "--headless=false",
        ):
            with self.subTest(argument=argument), self.assertRaisesRegex(
                ValueError, "cannot be overridden",
            ):
                browser_acceptance.launch(
                    "http://127.0.0.1:49197/", Path("/tmp/unused"),
                    extra_args=[argument],
                )

    def test_binary_replacement_after_identity_is_refused_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser = self.executable(
                root / "headless-shell",
                "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo TestBrowser; fi\nexit 0\n",
            )
            original = browser_acceptance.browser_identity

            def replace_after_identity(binary):
                identity = original(binary)
                browser.write_text("#!/bin/sh\necho replaced\n", encoding="utf-8")
                browser.chmod(0o755)
                return identity

            with mock.patch.dict(os.environ, {"HARNESS_BROWSER_BIN": str(browser)}), \
                    mock.patch.object(browser_acceptance, "browser_identity", side_effect=replace_after_identity):
                with self.assertRaisesRegex(RuntimeError, "binary changed"):
                    browser_acceptance.launch(
                        "http://127.0.0.1:49197/", root / "runtime",
                    )

    def test_browser_runs_are_serialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser = self.executable(
                root / "headless-shell",
                "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo TestBrowser; exit 0; fi\nsleep 20\n",
            )
            second_started = threading.Event()
            second_finished = threading.Event()
            errors = []
            with mock.patch.dict(os.environ, {"HARNESS_BROWSER_BIN": str(browser)}):
                first = browser_acceptance.launch(
                    "http://127.0.0.1:49196/", root / "runtime-one",
                )

                def run_second():
                    try:
                        second = browser_acceptance.launch(
                            "http://127.0.0.1:49195/", root / "runtime-two",
                        )
                        second_started.set()
                        second.close()
                    except Exception as error:  # pragma: no cover - asserted below
                        errors.append(error)
                    finally:
                        second_finished.set()

                thread = threading.Thread(target=run_second)
                thread.start()
                time.sleep(0.15)
                self.assertFalse(second_started.is_set())
                first.close()
                self.assertTrue(second_finished.wait(5))
                thread.join(timeout=1)
            self.assertEqual(errors, [])
            self.assertTrue(second_started.is_set())

    def test_cleanup_identity_refusal_releases_serialization_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser = self.executable(
                root / "headless-shell",
                "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo TestBrowser; exit 0; fi\nsleep 20\n",
            )
            with mock.patch.dict(os.environ, {"HARNESS_BROWSER_BIN": str(browser)}):
                session = browser_acceptance.launch(
                    "http://127.0.0.1:49194/", root / "runtime",
                )
                mismatched = {
                    session.pid: {
                        "pid": session.pid, "ppid": os.getpid(), "pgid": session.pgid,
                        "start_token": "different process", "command": str(browser),
                    },
                }
                with mock.patch.object(browser_acceptance, "_process_table", return_value=mismatched):
                    with self.assertRaisesRegex(RuntimeError, "PID identity changed"):
                        session.close()
                acquired = browser_acceptance._PROCESS_LOCK.acquire(blocking=False)
                self.assertTrue(acquired, "failed cleanup must not deadlock later browser runs")
                if acquired:
                    browser_acceptance._PROCESS_LOCK.release()
                session.process.kill()
                session.process.wait(timeout=5)

    def test_reparented_identity_remains_owned_after_parent_disappears(self):
        identities = {
            100: {"pid": 100, "ppid": 1, "pgid": 100,
                  "start_token": "root-start", "command": "browser"},
        }
        browser_acceptance._record_owned(identities, {
            100: identities[100],
            101: {"pid": 101, "ppid": 100, "pgid": 500,
                  "start_token": "helper-start", "command": "helper"},
        }, 100)
        browser_acceptance._record_owned(identities, {
            101: {"pid": 101, "ppid": 1, "pgid": 500,
                  "start_token": "helper-start", "command": "helper"},
        }, 100)
        self.assertEqual(identities[101]["start_token"], "helper-start")

    def test_owner_tab_helpers_do_not_change_the_browser_app_baseline(self):
        table = {
            10: {
                "command": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --restart",
                "start_token": "owner-start",
            },
            11: {
                "command": "/Applications/Google Chrome.app/Contents/Frameworks/Chrome/Helpers/Google Chrome Helper (Renderer).app/Contents/MacOS/Google Chrome Helper (Renderer) --type=renderer",
                "start_token": "tab-start",
            },
            12: {
                "command": "/Library/PrivilegedHelperTools/ChromeRemoteDesktopHost.app/Contents/MacOS/remoting_host",
                "start_token": "remote-host-start",
            },
        }
        self.assertEqual(set(browser_acceptance._app_processes(table)), {10})

    def test_planted_top_level_browser_app_remains_independently_detectable(self):
        table = {
            21: {
                "command": "/tmp/Owner Chrome.app/Contents/MacOS/Owner Chrome",
                "start_token": "planted-start",
            },
        }
        self.assertEqual(set(browser_acceptance._app_processes(table)), {21})

    def test_independent_process_observation_catches_nested_app_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser = self.executable(
                root / "headless-shell",
                "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo TestBrowser; exit 0; fi\nsleep 20\n",
            )
            bypass = self.executable(
                root / "Owner Chrome.app/Contents/MacOS/Owner Chrome", "#!/bin/sh\nsleep 20\n",
            )
            with mock.patch.dict(os.environ, {"HARNESS_BROWSER_BIN": str(browser)}):
                session = browser_acceptance.launch("http://127.0.0.1:49198/", root / "runtime")
                nested = subprocess.Popen([str(bypass)])
                try:
                    time.sleep(0.1)
                    with self.assertRaisesRegex(RuntimeError, "app-bundle process"):
                        session.close()
                finally:
                    nested.terminate()
                    nested.wait(timeout=5)

    def test_rendered_acceptance_modules_cannot_launch_browsers_directly(self):
        repository = Path(__file__).resolve().parents[1]
        modules = (
            "test_project_manager_rendered.py", "test_project_pause_rendered.py",
            "test_long_directive_ui.py", "test_project_chat_ui.py",
        )
        offenders = []
        for name in modules:
            path = repository / "tests" / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if "subprocess.Popen(" in text:
                offenders.append(name)
        self.assertEqual(offenders, [], "browser launches must cross browser_acceptance.launch")


if __name__ == "__main__":
    unittest.main()

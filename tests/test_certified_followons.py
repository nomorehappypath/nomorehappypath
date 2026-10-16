# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Follow-ons to governed certified execution: retry after upgrade, Linux browsers, bounded lock wait."""
import fcntl
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import board, browser_acceptance, certified_execution, execution_identity
from tests import environment_support


class LinuxBrowserDetectionTests(unittest.TestCase):
    def table(self, *commands):
        return {1000 + index: {"pid": 1000 + index, "ppid": 1, "start_token": str(index), "command": command}
                for index, command in enumerate(commands)}

    def test_linux_native_browsers_are_listed(self):
        browsers = [
            "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable --profile-directory=Default",
            "/usr/bin/chromium", "/usr/bin/chromium-browser", "/opt/google/chrome/chrome",
            "/usr/lib/firefox/firefox", "/snap/firefox/4793/usr/lib/firefox/firefox",
            "/usr/bin/brave-browser", "/opt/microsoft/msedge/msedge", "/usr/bin/microsoft-edge-stable",
            "/opt/opera/opera", "/usr/lib/firefox-esr/firefox-esr", "/opt/vivaldi/vivaldi-bin", "/home/dev/.cache/ms-playwright/chromium-1/chrome-linux/chrome",
            "/home/dev/.cache/puppeteer/chrome/linux-1/chrome-linux64/chrome --headless", "firefox",
            "/usr/local/bin/chromium",
        ]
        found = browser_acceptance._app_processes(self.table(*browsers))
        self.assertEqual(sorted(value["command"] for value in found.values()), sorted(browsers))

    def test_helpers_and_lookalikes_are_not_browsers(self):
        others = [
            "/opt/google/chrome/chrome --type=renderer --lang=en-US", "/opt/google/chrome/chrome --type=gpu-process",
            "/opt/google/chrome/chrome --type=zygote --no-sandbox", "/opt/google/chrome/chrome_crashpad_handler --monitor-self",
            "/opt/google/chrome/chrome-sandbox", "/usr/lib/firefox/firefox -contentproc -childID 1",
            "/usr/bin/python3 /usr/bin/chrome-test.py", "python3 chromium", "/home/dev/chrome/build.sh",
            "/usr/bin/arcade", "/usr/bin/operator", "/srv/data/chrome", "/tmp/evil/firefox", "/usr/bin/vimfirefox",
            "/usr/bin/firefox-esr-notes", "",
        ]
        self.assertEqual(browser_acceptance._app_processes(self.table(*others)), {})

    def test_top_level_browser_with_ordinary_flags_is_still_detected(self):
        # Review finding: words like sandbox/crashpad/zygote in ordinary arguments must not hide a browser.
        browsers = [
            "/usr/bin/chromium --no-sandbox https://example.test/",
            "/usr/bin/google-chrome --user-data-dir=/home/dev/zygote-profile --disable-crashpad",
            "/usr/bin/chromium --headless --remote-debugging-port=9222",   # headless without a throwaway profile
            "/usr/bin/chromium --headless --user-data-dir=/home/dev/.config/chromium",   # the owner's real profile
            "/usr/bin/firefox -headless -profile /home/dev/.mozilla/firefox/abc.default",
        ]
        self.assertEqual(len(browser_acceptance._app_processes(self.table(*browsers))), len(browsers))

    def test_the_harness_own_isolated_headless_acceptance_browser_is_not_a_forbidden_browser(self):
        approved = [
            "/usr/bin/chromium --headless=new --disable-gpu --no-first-run --user-data-dir=/tmp/accept/profile "
            "--disk-cache-dir=/tmp/accept/cache --remote-debugging-port=0 --no-sandbox http://127.0.0.1:5000/",
            "/usr/bin/chromium-browser --headless --user-data-dir=/tmp/x/profile --no-sandbox http://127.0.0.1/",
            "/home/dev/.cache/ms-playwright/chromium-1/chrome-linux/chrome --headless=new --user-data-dir=/tmp/p",
        ]
        self.assertEqual(browser_acceptance._app_processes(self.table(*approved)), {})

    def test_headless_exemption_is_paired_with_the_browser_family_and_an_absolute_profile(self):
        not_approved = [
            "/usr/bin/firefox --headless --user-data-dir=/tmp/p",          # Chromium flag does not isolate Firefox
            "/usr/bin/chromium --headless -profile /tmp/p",                # Firefox flag does not isolate Chromium
            "/usr/bin/chromium --headless --user-data-dir=relative/profile",
            "/usr/bin/chromium --headless --user-data-dir=/tmp/../home/dev/.config/chromium",
            "/usr/bin/chromium --user-data-dir=/tmp/p",                    # not headless
        ]
        self.assertEqual(len(browser_acceptance._app_processes(self.table(*not_approved))), len(not_approved))
        approved = ["/usr/bin/firefox -headless -profile /tmp/accept/profile"]
        self.assertEqual(browser_acceptance._app_processes(self.table(*approved)), {})

    def test_mac_detection_is_unchanged(self):
        self.assertTrue(browser_acceptance._is_browser_app_command("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
        self.assertFalse(browser_acceptance._is_browser_app_command(
            "/Applications/Google Chrome.app/Contents/Frameworks/X.framework/Helpers/Google Chrome Helper.app/Contents/MacOS/Google Chrome Helper"))


class LinuxBrowserBelowCertifiedCommandTests(unittest.TestCase):
    def setUp(self):
        environment_support.require_process_table()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate = execution_identity.candidate_evidence_identity("c" * 40, "t" * 40, "contract", {})

    def test_linux_browser_executable_below_the_command_fails_the_run(self):
        bin_dir = self.root / "home" / "dev" / ".cache" / "puppeteer" / "chrome-linux"
        bin_dir.mkdir(parents=True)
        browser = bin_dir / "chrome"
        browser.symlink_to("/bin/sleep")   # a real executable, so the process command line starts with its path
        command = (f"python3 -c \"import subprocess; subprocess.run([r'{browser}', '0.4']); "
                   "print('Ran 1 test in 0.001s'); print('OK')\"")
        # The fixture lives under a temp folder, not a real home, so allow its location.
        with mock.patch.object(browser_acceptance, "_LINUX_BROWSER_DIRECTORIES",
                               browser_acceptance._LINUX_BROWSER_DIRECTORIES + (str(self.root) + "/", str(self.root.resolve()) + "/")):
            with self.assertRaisesRegex(ValueError, "owner-browser application bundle"):
                certified_execution.run(
                    self.root, self.root, command, candidate=self.candidate, environment_sha256="e" * 64,
                    environment=os.environ, lockfile_digests={}, role="reviewer", gate="final")


    def test_the_harness_own_isolated_headless_launch_below_the_command_is_certified(self):
        bin_dir = self.root / "home" / "dev" / ".cache" / "puppeteer" / "chrome-linux"
        bin_dir.mkdir(parents=True)
        browser = bin_dir / "chromium"
        browser.symlink_to(sys.executable)
        profile = self.root / "accept" / "profile"
        command = (f"python3 -c \"import subprocess; subprocess.run([r'{browser}', '-c', 'import time; time.sleep(.4)', "
                   f"'--headless=new', '--user-data-dir={profile}']); "
                   "print('Ran 1 test in 0.001s'); print('OK')\"")
        with mock.patch.object(browser_acceptance, "_LINUX_BROWSER_DIRECTORIES",
                               browser_acceptance._LINUX_BROWSER_DIRECTORIES + (str(self.root) + "/", str(self.root.resolve()) + "/")):
            result = certified_execution.run(
                self.root, self.root, command, candidate=self.candidate, environment_sha256="e" * 64,
                environment=os.environ, lockfile_digests={}, role="reviewer", gate="final")
        self.assertEqual(result["measurement"]["exit_code"], 0)


class RetryAfterUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate = execution_identity.candidate_evidence_identity("c" * 40, "t" * 40, "contract", {})
        self.command = "python3 -c \"import os,sys; print('Ran 1 test in 0.001s' if os.path.exists('healthy') else 'Ran 1 test'); sys.exit(0 if os.path.exists('healthy') else 1)\""

    def execute(self, reason=""):
        return certified_execution.run(
            self.root, self.root, self.command, candidate=self.candidate, environment_sha256="e" * 64,
            environment=os.environ, lockfile_digests={}, role="delivery", gate="final", retry_reason=reason)

    def store(self):
        path = board.board_dir(self.root) / execution_identity.STORE_NAME
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_a_failure_stored_under_the_old_policy_does_not_block_a_fresh_run(self):
        environment_support.require_process_table()
        with mock.patch.object(execution_identity, "POLICY_VERSION", 3):
            with self.assertRaises(ValueError):
                self.execute()   # refused by the old rules, stored as a failure
        old_store = self.store()
        self.assertIn('"exit_code"', old_store)
        (self.root / "healthy").write_text("x")
        with self.assertRaises(ValueError) as unchanged:   # same policy: still needs a repair reason
            with mock.patch.object(execution_identity, "POLICY_VERSION", 3):
                (self.root / "healthy").unlink()
                self.execute()
        self.assertIn("repair reason", str(unchanged.exception))
        (self.root / "healthy").write_text("x")
        # Current policy: the old failure is history, not the current identity's prior failure.
        result = self.execute()   # no retry reason
        self.assertEqual(result["measurement"]["exit_code"], 0)
        self.assertTrue(self.store().startswith(old_store), "old records stay untouched")

    def test_a_failure_under_the_current_policy_still_needs_a_repair_reason(self):
        environment_support.require_process_table()
        with self.assertRaises(ValueError):
            self.execute()
        (self.root / "healthy").write_text("x")
        with self.assertRaisesRegex(ValueError, "repair reason"):
            self.execute()


class OwnerMachineActivityTests(unittest.TestCase):
    def setUp(self):
        environment_support.require_process_table()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate = execution_identity.candidate_evidence_identity("c" * 40, "t" * 40, "contract", {})

    def execute(self):
        return certified_execution.run(
            self.root, self.root, "python3 -c \"print('Ran 1 test in 0.001s'); print('OK')\"",
            candidate=self.candidate, environment_sha256="e" * 64, environment=os.environ,
            lockfile_digests={}, role="delivery", gate="final")

    def test_a_handler_change_beside_a_green_run_is_retryable_not_a_stored_failure(self):
        digests = iter(["before", "after", "after", "after"])
        with mock.patch.object(browser_acceptance, "_default_handlers_digest", side_effect=lambda: next(digests)):
            with self.assertRaises(certified_execution.OwnerMachineActivity) as caught:
                self.execute()
        self.assertIn("nothing failed", str(caught.exception).lower().replace("nothing was certified and ", ""))
        store = board.board_dir(self.root) / execution_identity.STORE_NAME
        self.assertFalse(store.exists() and store.read_text().strip(), "no failure may be stored")
        result = self.execute()   # asking again needs no repair reason
        self.assertEqual(result["measurement"]["exit_code"], 0)


    def test_a_default_browser_change_seen_inside_the_tests_is_retryable(self):
        marker = browser_acceptance.OWNER_MACHINE_ACTIVITY_MARKER
        command = (f"python3 -c \"import sys; print('Ran 1 test in 0.001s'); "
                   f"print('RuntimeError: {marker} the default-browser handler changed during browser acceptance'); sys.exit(1)\"")
        with self.assertRaises(certified_execution.OwnerMachineActivity):
            certified_execution.run(
                self.root, self.root, command, candidate=self.candidate, environment_sha256="e" * 64,
                environment=os.environ, lockfile_digests={}, role="delivery", gate="final")
        store = board.board_dir(self.root) / execution_identity.STORE_NAME
        self.assertFalse(store.exists() and store.read_text().strip(), "no failure may be stored")


class BoundedLockWaitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def hold(self, digest):
        directory = board.board_dir(self.root) / "execution-locks"
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / f"{digest}.lock").open("a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        self.addCleanup(handle.close)
        return handle

    def test_a_held_lock_times_out_with_a_plain_message(self):
        self.hold("abc")
        started = time.monotonic()
        with mock.patch.object(certified_execution, "_LOCK_WAIT_SECONDS", 0.3):
            with self.assertRaisesRegex(ValueError, "identical certified run is still in progress"):
                with certified_execution._identity_lock(self.root, "abc"):
                    self.fail("must not acquire a held lock")
        self.assertLess(time.monotonic() - started, 5)

    def test_a_released_lock_is_acquired_within_the_wait(self):
        handle = self.hold("def")
        threading.Timer(0.3, lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)).start()
        with mock.patch.object(certified_execution, "_LOCK_WAIT_SECONDS", 5):
            with certified_execution._identity_lock(self.root, "def"):
                pass

    def test_the_timeout_names_the_holder_and_ending_it_releases_the_lock(self):
        import subprocess
        directory = board.board_dir(self.root)
        ready = self.root / "ready"
        code = (
            "import sys, time, pathlib\n"
            "from pathlib import Path\n"
            "from harness import certified_execution\n"
            f"with certified_execution._identity_lock(Path({str(self.root)!r}), 'held'):\n"
            f"    pathlib.Path({str(ready)!r}).write_text('x'); time.sleep(60)\n"
        )
        holder = subprocess.Popen([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent,
                                  env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})
        self.addCleanup(lambda: (holder.poll() is None and holder.kill(), holder.wait()))
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.1)
        self.assertTrue(ready.exists())
        with mock.patch.object(certified_execution, "_LOCK_WAIT_SECONDS", 0.3):
            with self.assertRaises(certified_execution.ExecutionInProgress) as caught:
                with certified_execution._identity_lock(self.root, "held"):
                    self.fail("held")
        self.assertIn(f"harness process {holder.pid}", str(caught.exception))
        holder.terminate()
        holder.wait(timeout=10)
        with mock.patch.object(certified_execution, "_LOCK_WAIT_SECONDS", 2):
            with certified_execution._identity_lock(self.root, "held"):
                pass

    def test_the_result_store_lock_wait_is_bounded(self):
        directory = board.board_dir(self.root)
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / ".execution-store.lock").open("a+")
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        with mock.patch.object(execution_identity, "STORE_LOCK_WAIT_SECONDS", 0.3):
            with self.assertRaisesRegex(ValueError, "store stayed busy"):
                execution_identity._entries(self.root)

    def test_only_contention_is_reported_as_a_busy_run(self):
        import errno
        with mock.patch.object(certified_execution.fcntl, "flock", side_effect=OSError(errno.EIO, "disk")):
            with self.assertRaises(OSError) as caught:
                with certified_execution._identity_lock(self.root, "io"):
                    pass
        self.assertNotIsInstance(caught.exception, certified_execution.ExecutionInProgress)

    def test_default_wait_is_bounded(self):
        self.assertTrue(0 < certified_execution._LOCK_WAIT_SECONDS <= 300)


if __name__ == "__main__":
    unittest.main()

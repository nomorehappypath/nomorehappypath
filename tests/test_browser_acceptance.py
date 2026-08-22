# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import browser_acceptance


class BrowserAcceptanceTests(unittest.TestCase):
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

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""2026-10-15: on Ubuntu `chromium` resolves to the snap launcher; it is not a headless browser a governed command can use."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import browser_acceptance


def make_executable(path: Path, body: bytes = b"\x7fELF-fake") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o755)
    return path


class SnapStubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_symlink_to_the_snap_launcher_is_not_a_browser(self):
        snap = make_executable(self.root / "usr" / "bin" / "snap")
        link = self.root / "snap" / "bin" / "chromium"
        link.parent.mkdir(parents=True)
        link.symlink_to(snap)
        self.assertIsNone(browser_acceptance._safe_binary(str(link)))
        self.assertIsNone(browser_acceptance._safe_binary(str(snap)))

    def test_the_ubuntu_chromium_browser_stub_script_is_not_a_browser(self):
        stub = make_executable(
            self.root / "usr" / "bin" / "chromium-browser",
            b"#!/bin/sh\nif ! [ -x /snap/bin/chromium ]; then\n  echo \"snap install chromium\" >&2\nfi\n",
        )
        self.assertIsNone(browser_acceptance._safe_binary(str(stub)))

    def test_a_native_browser_is_still_accepted_and_discovery_skips_the_snap(self):
        native = make_executable(self.root / "opt" / "chrome" / "chrome-headless-shell")
        snap = make_executable(self.root / "usr" / "bin" / "snap")
        link = self.root / "snap" / "bin" / "chromium"
        link.parent.mkdir(parents=True)
        link.symlink_to(snap)
        self.assertEqual(browser_acceptance._safe_binary(str(native)), str(native.resolve()))
        with patch.dict(os.environ, {"HARNESS_BROWSER_BIN": "", "CHROME_BIN": ""}), patch.object(
            browser_acceptance.platform_support.browser_host().__class__, "headless_cache_candidates", lambda self: [],
        ), patch("harness.browser_acceptance.shutil.which", side_effect=lambda name: str(link) if name == "chromium" else (str(native) if name == "chrome-headless-shell" else None)):
            chosen = browser_acceptance.resolve_binary()
        self.assertEqual(chosen, str(native.resolve()))

    def test_with_only_the_snap_the_error_tells_how_to_configure_a_browser(self):
        snap = make_executable(self.root / "usr" / "bin" / "snap")
        link = self.root / "snap" / "bin" / "chromium"
        link.parent.mkdir(parents=True)
        link.symlink_to(snap)
        with patch.dict(os.environ, {"HARNESS_BROWSER_BIN": "", "CHROME_BIN": ""}), patch.object(
            browser_acceptance.platform_support.browser_host().__class__, "headless_cache_candidates", lambda self: [],
        ), patch("harness.browser_acceptance.shutil.which", side_effect=lambda name: str(link) if name == "chromium" else None):
            with self.assertRaisesRegex(FileNotFoundError, "HARNESS_BROWSER_BIN"):
                browser_acceptance.resolve_binary()


if __name__ == "__main__":
    unittest.main()

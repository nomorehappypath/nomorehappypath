# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Finding a headless browser — and why a missing one used to look like success.

`resolve_binary()` globbed a macOS-only Playwright cache, so on Linux no browser
was ever found. The rendered tests then SKIPPED and their modules reported `OK`:
eight suites whose whole purpose is proving the owner sees what we claim were
proving nothing, in green. Measured on the target before this change,
`tests.test_help_rendered` reported `OK (skipped=1)`; after it, the same module
runs and passes.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import browser_acceptance, platform_support


class BrowserDiscoveryTests(unittest.TestCase):
    def _plant(self, home: Path, relative: str) -> Path:
        binary = home / relative
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        return binary

    def test_each_platform_searches_only_ITS_OWN_cache(self):
        """The reviewer's case: a Mac carrying a Linux Playwright cache.

        An earlier version searched both layouts on both platforms, reasoning
        that an absent directory contributes nothing. But a Mac CAN carry
        ~/.cache/ms-playwright, and there the both-layouts version returned a
        Linux binary where main raises the named refusal — a behaviour change on
        macOS, which Stage 0 forbids.
        """
        host = platform_support.browser_host()
        mac_relative = ("Library/Caches/ms-playwright/chromium_headless_shell-9999"
                        "/chrome-headless-shell-mac-arm64/chrome-headless-shell")
        linux_relative = (".cache/ms-playwright/chromium_headless_shell-9999"
                          "/chrome-headless-shell-linux64/chrome-headless-shell")

        for platform, planted_relative, foreign_relative in (
            ("darwin", mac_relative, linux_relative),
            ("linux", linux_relative, mac_relative),
        ):
            with self.subTest(platform=platform):
                with tempfile.TemporaryDirectory() as temporary:
                    home = Path(temporary)
                    native = self._plant(home, planted_relative)
                    foreign = self._plant(home, foreign_relative)
                    with mock.patch.object(platform_support.defaults.sys, "platform", platform), \
                            mock.patch.object(Path, "home", return_value=home):
                        found = host.headless_cache_candidates()
                    self.assertIn(native, found, f"{platform} must find its own cache")
                    self.assertNotIn(foreign, found,
                                     f"{platform} must NOT consult the other platform's cache")

    def test_a_mac_with_only_a_linux_cache_still_refuses(self):
        """Byte-identical to main for that case, which is what Stage 0 requires."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self._plant(home, ".cache/ms-playwright/chromium_headless_shell-9999"
                              "/chrome-headless-shell-linux64/chrome-headless-shell")
            with mock.patch.object(platform_support.defaults.sys, "platform", "darwin"), \
                    mock.patch.object(Path, "home", return_value=home):
                self.assertEqual(platform_support.browser_host().headless_cache_candidates(), [],
                                 "a Linux cache on a Mac must be invisible to discovery")

    def test_the_cache_root_is_resolved_on_every_call_not_frozen_at_import(self):
        """The defect this test exists for, reproduced.

        Roots captured at import time ignore a changed HOME, so discovery reaches
        into the owner's REAL cache. A hermetic test then passes for the wrong
        reason — because a browser happens to be installed on the machine — and
        a machine without one fails a test that was never about the machine.
        """
        host = platform_support.browser_host()
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            # This platform's OWN layout, since discovery now consults only that.
            parts, _ = platform_support.browser_host().cache_layout()
            leaf = ("chrome-headless-shell-mac-arm64" if parts[0] == "Library"
                    else "chrome-headless-shell-linux64")
            planted = self._plant(
                Path(second),
                f"{'/'.join(parts)}/chromium_headless_shell-9999/{leaf}/chrome-headless-shell",
            )
            with mock.patch.object(Path, "home", return_value=Path(first)):
                self.assertEqual(host.headless_cache_candidates(), [],
                                 "an empty home must yield no candidates")
            with mock.patch.object(Path, "home", return_value=Path(second)):
                self.assertEqual(host.headless_cache_candidates(), [planted],
                                 "the SECOND home must be honoured; a frozen root returns the first")

    def test_the_native_cache_is_searched_before_path(self):
        host = platform_support.browser_host()
        self.assertEqual(host.path_candidate_names()[0], "chrome-headless-shell")

    def test_an_installed_browser_is_found_without_an_environment_hint(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_BROWSER_BIN", None)
            os.environ.pop("CHROME_BIN", None)
            try:
                resolved = browser_acceptance.resolve_binary()
            except FileNotFoundError:
                self.skipTest("no headless browser installed on this machine")
        self.assertTrue(Path(resolved).is_file())
        self.assertTrue(os.access(resolved, os.X_OK))

    def test_the_explicit_override_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "browser"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {"HARNESS_BROWSER_BIN": str(fake)}):
                self.assertEqual(browser_acceptance.resolve_binary(), str(fake.resolve()))


class AppBundleRefusalTests(unittest.TestCase):
    """A macOS SAFETY rule, kept on every platform.

    A binary inside a `.app` can relaunch the owner's own browser — precisely
    what the certified-execution evidence exists to catch. Dropping the rule off
    Darwin would silently weaken the check on the platform being added.
    """

    def test_a_bundle_path_is_refused_wherever_it_appears(self):
        self.assertTrue(platform_support.browser_host().rejects_resolved(
            Path("/somewhere/Owner Browser.app/Contents/MacOS/Browser")))
        self.assertFalse(platform_support.browser_host().rejects_resolved(
            Path("/home/owner/.cache/ms-playwright/x/chrome-headless-shell")))

    def test_a_configured_bundle_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "Owner Browser.app" / "Contents" / "MacOS" / "Browser"
            bundle.parent.mkdir(parents=True)
            bundle.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            bundle.chmod(0o755)
            with mock.patch.dict(os.environ, {"HARNESS_BROWSER_BIN": str(bundle)}):
                with self.assertRaisesRegex(ValueError, "outside every macOS .app"):
                    browser_acceptance.resolve_binary()

    def test_no_browser_anywhere_is_a_named_refusal_not_a_silent_pass(self):
        host = platform_support.browser_host()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_BROWSER_BIN", None)
            os.environ.pop("CHROME_BIN", None)
            with mock.patch.object(type(host), "headless_cache_candidates", return_value=[]), \
                 mock.patch.object(type(host), "path_candidate_names", return_value=()):
                with self.assertRaisesRegex(FileNotFoundError, "process-isolated Chromium"):
                    browser_acceptance.resolve_binary()


if __name__ == "__main__":
    unittest.main()

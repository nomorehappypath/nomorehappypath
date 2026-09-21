# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The first platform seam: tool discovery, moved without changing a byte.

`docs/specs/LINUX_STAGE0_PLATFORM_SEAM.md` §4.4. Several of these strings are
hashed into recorded evidence (`sanitized_environment_sha256`), so "equivalent"
is not good enough — the values must be identical, and the three policies must
stay separate because merging them changes that digest.
"""
from __future__ import annotations

import os
import unittest
import unittest.mock
from pathlib import Path

from harness import child_process, git_broker, global_settings, platform_support


class DiscoveryValuesAreUnchangedTests(unittest.TestCase):
    def test_owner_tools_put_owner_writable_directories_first(self):
        """The ordering that closed public issue #1: ~/.local/bin before system prefixes.

        Pinned per platform instead of read off the host, because Stage 1
        selects for real: an unpinned run reports the machine the suite started
        on. The owner-writable head is what BOTH platforms owe; the system
        prefix behind it is where they genuinely differ, so each one is named
        and the other platform's prefix is asserted absent.
        """
        import importlib
        # The reload below leaves a PINNED platform's values in the module
        # snapshot; restore the host's own before the next test reads them.
        self.addCleanup(importlib.reload, global_settings)
        for platform_name, leading_prefix, foreign_prefix in (
            ("darwin", "/opt/homebrew/bin", "/snap/bin"),
            ("linux", "/usr/local/bin", "/opt/homebrew/bin"),
        ):
            with self.subTest(platform=platform_name):
                with unittest.mock.patch.object(
                        platform_support.defaults.sys, "platform", platform_name):
                    directories = platform_support.discovery().owner_tool_directories()
                    self.assertEqual(directories[0], os.path.expanduser("~/.local/bin"))
                    self.assertEqual(directories[1], os.path.expanduser("~/bin"))
                    self.assertLess(directories.index(leading_prefix),
                                    directories.index("/usr/bin"))
                    self.assertNotIn(foreign_prefix, directories,
                                     "a prefix that cannot exist here must not be searched")
                    # The module constant is a SNAPSHOT taken at import; the seam
                    # resolves on demand. Compare against a cleanly reloaded module
                    # so the assertion is about sourcing, not about whatever an
                    # earlier test left behind - reloaded INSIDE the pin, or it
                    # snapshots the host and a drifted copy goes unseen.
                    importlib.reload(global_settings)
                    self.assertEqual(
                        tuple(global_settings.PROVIDER_SEARCH_DIRECTORIES), tuple(directories),
                        "the public constant must be sourced from the seam, not a copy that drifts")

    def test_trusted_tools_are_system_directories_only(self):
        """The broker runs Git itself; a project-writable directory must never appear."""
        path = platform_support.discovery().trusted_tool_search_path()
        self.assertEqual(path, "/usr/bin:/bin:/usr/sbin:/sbin")
        for entry in path.split(os.pathsep):
            self.assertFalse(entry.startswith(os.path.expanduser("~")),
                             f"{entry} is owner-writable and must not be a trusted tool source")

    def test_the_three_policies_are_distinct(self):
        """Merging them would change sanitized_environment_sha256, recorded evidence."""
        discovery = platform_support.discovery()
        owner = discovery.owner_tool_directories()
        trusted = tuple(discovery.trusted_tool_search_path().split(os.pathsep))
        governed = tuple(discovery.governed_execution_path(dict(os.environ)).split(os.pathsep))
        self.assertNotEqual(owner, trusted)
        self.assertNotEqual(owner, governed)
        self.assertNotEqual(trusted, governed)

    def test_the_governed_path_starts_from_the_running_interpreter(self):
        import sys
        governed = child_process._execution_path(dict(os.environ))
        self.assertTrue(governed.startswith(str(Path(sys.executable).resolve().parent)))

    def test_a_virtualenv_is_honoured_before_the_system_suffix(self):
        """Both platforms, because the suffix they honour it before is not the same one."""
        import tempfile
        # Pin the interpreter somewhere no suffix contains. Its own directory
        # legitimately leads, so a Homebrew python (macOS) or /usr/bin (Linux)
        # makes this test report where the suite's python was installed instead
        # of where a virtualenv sits in the order.
        interpreter_directory = "/opt/absent-interpreter/bin"
        for platform_name, leading_suffix in (("darwin", "/opt/homebrew/bin"),
                                              ("linux", "/usr/local/bin")):
            with self.subTest(platform=platform_name), \
                    tempfile.TemporaryDirectory() as venv, \
                    unittest.mock.patch.object(
                        platform_support.defaults.sys, "platform", platform_name), \
                    unittest.mock.patch.object(
                        platform_support.defaults.sys, "executable",
                        f"{interpreter_directory}/python3"):
                # resolve(), because the code resolves and /tmp is a symlink on macOS
                expected = str(Path(venv).resolve() / "bin")
                source = dict(os.environ, VIRTUAL_ENV=venv)
                governed = child_process._execution_path(source).split(os.pathsep)
                self.assertIn(expected, governed)
                # Before the fixed system SUFFIX, not before the interpreter's own
                # directory: on Linux the interpreter lives in /usr/bin, so that
                # entry legitimately comes first. Asserting against /usr/bin encoded
                # a macOS-only assumption and failed on the Ubuntu target - and
                # /opt/homebrew is not on the Linux suffix at all, so the entry the
                # virtualenv must outrank is named per platform.
                self.assertLess(governed.index(expected), governed.index(leading_suffix))
                self.assertEqual(governed[0], interpreter_directory)


class SeamShapeTests(unittest.TestCase):
    """The seam's own rules, from §10 as amended."""

    def test_selection_does_not_refuse_a_platform_without_an_implementation(self):
        """Refusing here made the package unimportable on the Linux target.

        The values are not macOS values — they are the only values this product
        has ever used — so selection returns them everywhere until Stage 1
        supplies something else to select.
        """
        for platform_name in ("darwin", "linux", "freebsd13"):
            with self.subTest(platform=platform_name):
                with unittest.mock.patch("sys.platform", platform_name):
                    self.assertIsNotNone(platform_support.discovery())

    def test_unsupported_platform_exists_for_operations_not_values(self):
        self.assertTrue(issubclass(platform_support.UnsupportedPlatform, RuntimeError))

    def test_only_the_platform_package_decides_behaviour_from_sys_platform(self):
        """§10's rule, enforced rather than asserted in prose.

        Recording the platform as evidence is exempt: harness/board.py and
        harness/execution_identity.py record it, they do not branch on it.
        """
        import ast
        root = Path(__file__).resolve().parents[1] / "harness"
        allowed = {"platform_support/__init__.py", "board.py", "execution_identity.py",
                   "browser_acceptance.py", "board_viewer.py", "project_worker.py",
                   "interactive_supervisor.py", "project_manager.py", "workspace_settings.py",
                   "git_broker.py", "release_preview.py", "control.py"}
        offenders = []
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            # The whole platform package is exempt, not just its __init__:
            # deciding from sys.platform is precisely what it is FOR.
            if relative in allowed or relative.startswith("platform_support/"):
                continue
            body = path.read_text(encoding="utf-8")
            if "sys.platform" in body or "os.uname()" in body:
                offenders.append(relative)
        self.assertEqual(offenders, [], "new platform branching appeared outside the seam")


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()

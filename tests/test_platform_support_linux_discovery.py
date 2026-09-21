# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Finding the agent CLIs on Linux, under a service manager's minimal PATH.

This is not a hypothetical class. It already shipped as public issue #1 on
macOS: a CLI installed in ~/.local/bin that the app could not find because
launchd supplies a minimal PATH and there is no login shell to fix it. systemd
has the identical property, and on Linux the two locations that matter most —
an nvm-managed Node and an `npm -g` prefix — appear in no system prefix at all.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.platform_support import linux


class SearchOrderTests(unittest.TestCase):
    def _directories(self, home: Path, environment: dict | None = None) -> tuple[str, ...]:
        with mock.patch.object(os.path, "expanduser", return_value=str(home)), \
                mock.patch.dict(os.environ, environment or {}, clear=False):
            if not (environment or {}).get("NVM_DIR"):
                os.environ.pop("NVM_DIR", None)
            return linux.DISCOVERY.owner_tool_directories()

    @staticmethod
    def _listed_in_text_order():
        """Pin the order the FILESYSTEM hands the version directories back.

        Listing order is not a promise. APFS and ext4 answer the same glob in
        different orders, so a test that takes the arriving order for granted
        reports the host the suite ran on instead of the sort it names. macOS
        happened to hand back the expected answer, so this read green there
        while it was red on Ubuntu — the same test, the same code, two verdicts.
        Text order is the exact wrong order this sort exists to correct, so
        forcing it makes the assertion measure the sort on every platform.
        """
        real_glob = Path.glob

        def in_text_order(directory, pattern, *arguments, **keywords):
            found = real_glob(directory, pattern, *arguments, **keywords)
            return iter(sorted(found, key=lambda path: path.parent.name))

        return mock.patch.object(Path, "glob", in_text_order)

    def test_the_owners_own_installs_outrank_system_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = self._directories(Path(tmp))
        self.assertLess(found.index(f"{tmp}/.local/bin"), found.index("/usr/bin"),
                        "a deliberate user-level install must outrank a system copy")

    def test_snap_is_searched_because_ubuntu_ships_clis_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("/snap/bin", self._directories(Path(tmp)))

    def test_homebrew_is_not_searched_on_linux(self):
        """Inert here; carrying it would be work for a path that cannot exist."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotIn("/opt/homebrew/bin", self._directories(Path(tmp)))

    def test_an_npm_global_prefix_is_searched(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = self._directories(Path(tmp))
        self.assertIn(f"{tmp}/.npm-global/bin", found)

    def test_nvm_versions_are_ordered_NUMERICALLY_newest_first(self):
        """Text order puts v9.9.9 above v20.11.1 and resolves the oldest Node."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for version in ("v9.9.9", "v20.11.1", "v18.4.0"):
                (home / ".nvm/versions/node" / version / "bin").mkdir(parents=True)
            with self._listed_in_text_order():
                found = [d for d in self._directories(home) if ".nvm" in d]
        self.assertEqual(
            [Path(d).parent.name for d in found], ["v20.11.1", "v18.4.0", "v9.9.9"])

    def test_NVM_DIR_is_honoured_when_the_owner_moved_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            elsewhere = home / "opt" / "nvm"
            (elsewhere / "versions/node/v22.0.0/bin").mkdir(parents=True)
            found = self._directories(home, {"NVM_DIR": str(elsewhere)})
        self.assertIn(str(elsewhere / "versions/node/v22.0.0/bin"), found)

    def test_no_directory_is_listed_twice(self):
        """A duplicate is harmless but makes resolved-path evidence unreadable."""
        with tempfile.TemporaryDirectory() as tmp:
            found = self._directories(Path(tmp))
        self.assertEqual(len(found), len(set(found)), f"duplicates in {found}")


class TrustedToolTests(unittest.TestCase):
    def test_git_is_the_system_copy_with_no_xcode_path(self):
        candidates = [str(path) for path in linux.DISCOVERY.trusted_git_candidates()]
        self.assertEqual(candidates, ["/usr/bin/git"])
        self.assertFalse(any("Xcode" in path for path in candidates))

    def test_the_trusted_path_is_still_system_only(self):
        """The broker must not resolve a tool the reviewed project could write."""
        for element in linux.DISCOVERY.trusted_tool_search_path().split(":"):
            self.assertTrue(element.startswith(("/usr", "/bin", "/sbin")), element)


if __name__ == "__main__":
    unittest.main()

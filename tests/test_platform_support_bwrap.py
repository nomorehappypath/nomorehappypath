# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Confinement on Linux.

macOS runs the broker's Git under sandbox-exec. Off Darwin that returned None
and Git ran UNCONFINED — tolerable on macOS only because it cannot happen there
in practice, and the normal case on Linux. This is the piece that must not be
missing when Linux is offered.
"""
from __future__ import annotations

import shutil
import unittest
from unittest import mock

from harness.platform_support import linux
from harness.platform_support.defaults import Grant


def grant(**overrides) -> Grant:
    base = dict(readable=[], writable=[], network=False, starts_helper_programs=False)
    base.update(overrides)
    return Grant(**base)


class RefusalTests(unittest.TestCase):
    def test_a_missing_bwrap_REFUSES_rather_than_running_unconfined(self):
        """The single most important behaviour in this file.

        The spec allowed either refusing or running unconfined after an explicit
        recorded owner acknowledgement. Refusing is chosen because no consent
        record exists yet, and a missing consent record would silently decay
        into "run unconfined" — a permission the owner never knowingly gave.
        """
        with mock.patch.object(shutil, "which", return_value=None):
            with self.assertRaises(linux.UnsupportedPlatformOperation) as caught:
                linux.CONFINEMENT.wrap(["git", "status"], grant(), store="/tmp")
        self.assertIn("bubblewrap", str(caught.exception), "must name what to install")

    def test_it_never_returns_an_unenforced_command_when_asked_to_confine(self):
        """A silent `enforced=False` is exactly the degradation being removed."""
        with mock.patch.object(shutil, "which", return_value=None):
            with self.assertRaises(linux.UnsupportedPlatformOperation):
                linux.CONFINEMENT.wrap(["git"], grant(), store="/tmp")

    def test_explicitly_disabled_is_still_honoured_and_says_so(self):
        result = linux.CONFINEMENT.wrap(["git"], grant(), store="/tmp", enabled=False)
        self.assertEqual(list(result.argv), ["git"])
        self.assertFalse(result.enforced)


class CommandShapeTests(unittest.TestCase):
    def _wrapped(self, g: Grant) -> list[str]:
        with mock.patch.object(shutil, "which", return_value="/usr/bin/bwrap"), \
                mock.patch.object(linux.Path, "exists", return_value=True):
            return list(linux.CONFINEMENT.wrap(["git", "status"], g, store="/tmp").argv)

    def test_the_child_cannot_outlive_the_harness(self):
        self.assertIn("--die-with-parent", self._wrapped(grant()))

    def test_denied_network_unshares_the_network_namespace(self):
        self.assertIn("--unshare-net", self._wrapped(grant(network=False)))

    def test_granted_network_does_NOT_unshare(self):
        self.assertNotIn("--unshare-net", self._wrapped(grant(network=True)))

    def _binds_for(self, wrapped: list[str], path: str) -> list[str]:
        """Every bind FLAG applied to `path`, in order.

        bwrap binds are `--flag SOURCE DEST`, so the path appears twice in a
        row; indexing back one element from the first occurrence lands on the
        source, not the flag. Scanning triples is what makes this correct.
        """
        flags = []
        for index, token in enumerate(wrapped):
            if token in ("--ro-bind", "--bind") and wrapped[index + 1] == path:
                flags.append(token)
        return flags

    def test_readable_paths_are_bound_READ_ONLY(self):
        wrapped = self._wrapped(grant(readable=["/srv/code"]))
        self.assertEqual(self._binds_for(wrapped, "/srv/code"), ["--ro-bind"])

    def test_writable_paths_are_bound_writable(self):
        wrapped = self._wrapped(grant(writable=["/srv/out"]))
        self.assertEqual(self._binds_for(wrapped, "/srv/out"), ["--bind"])

    def test_a_path_granted_BOTH_ends_up_writable(self):
        """bwrap applies binds in order, so the writable bind must come LAST."""
        wrapped = self._wrapped(grant(readable=["/srv/both"], writable=["/srv/both"]))
        flags = self._binds_for(wrapped, "/srv/both")
        self.assertEqual(flags[-1], "--bind",
                         "a path granted read AND write must end writable, not read-only")

    def test_the_real_command_is_still_the_command(self):
        wrapped = self._wrapped(grant())
        self.assertEqual(wrapped[-2:], ["git", "status"])
        self.assertIn("--", wrapped, "argv must be separated from bwrap's own flags")


if __name__ == "__main__":
    unittest.main()

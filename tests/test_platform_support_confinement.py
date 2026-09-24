# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Confinement behind the seam, with its bytes and its filename unchanged.

`docs/specs/LINUX_STAGE0_PLATFORM_SEAM.md` §4.3. The sandbox profile's filename
is content-addressed — `sandbox-<sha256[:16]>.sb` — so a single reordered or
reworded rule renames every profile the broker has ever written. A first attempt
at this move reconstructed the rules from reading them and produced a different
digest at once; the bytes are grafted from the original for that reason, and
these tests hold them there.

Stage 1 selects a real implementation per platform, so every test below PINS the
platform it is about instead of reading it off the host. Unpinned they report
which machine ran them: on Linux the seam hands back bubblewrap, which has no
sandbox profile at all, and these assertions fail for a reason that has nothing
to do with the bytes they exist to protect. Skipping them there is not the fix —
a suite that reports OK because it stopped looking is the failure this product
exists to refuse. Where the two platforms genuinely disagree, BOTH answers are
asserted.
"""
from __future__ import annotations

import contextlib
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from harness import git_broker, platform_support
from harness.platform_support import defaults
from harness.platform_support.defaults import Grant


def _grant(**overrides):
    values = dict(readable=[Path("/tmp/readable")], writable=[Path("/tmp/writable")],
                  network=False, starts_helper_programs=True)
    values.update(overrides)
    return Grant(**values)


class _FakeBroker:
    use_os_sandbox = True

    def __init__(self):
        self.journal_root = Path(tempfile.mkdtemp())


@contextlib.contextmanager
def _macos_host(*, sandbox_exec_available: bool = True):
    """Run the body as macOS, still going through the real selector.

    Two `sys` references decide the answer: the selector's, which picks the
    module, and the macOS module's own, which `available()` reads. Availability
    is pinned too because `/usr/bin/sandbox-exec` cannot be made to exist on a
    Linux runner — and DETECTING the binary is not what these tests are about,
    what `wrap()` builds once it is there is.
    """
    with unittest.mock.patch.object(platform_support.sys, "platform", "darwin"), \
            unittest.mock.patch.object(defaults.sys, "platform", "darwin"), \
            unittest.mock.patch.object(type(defaults.CONFINEMENT), "available",
                                       return_value=sandbox_exec_available):
        yield


@contextlib.contextmanager
def _linux_host(*, bwrap_available: bool):
    """Run the body as Linux, with or without bubblewrap installed.

    `shutil.which` decides the second half. The Linux implementation refuses
    when bwrap is absent, and that refusal is the behaviour being asserted, so
    it must not depend on what the runner happens to have installed.
    """
    with unittest.mock.patch.object(platform_support.sys, "platform", "linux"), \
            unittest.mock.patch.object(
                shutil, "which",
                return_value="/usr/bin/bwrap" if bwrap_available else None):
        yield


_XCODE_GIT_CORE = "/Applications/Xcode.app/Contents/Developer/usr/libexec/git-core"
_MACOS_SYSTEM_PREFIXES = ("/System", "/usr", "/bin", "/sbin", "/Library",
                          "/private", "/dev", "/Applications")


@contextlib.contextmanager
def _a_macos_filesystem():
    """Pin the two host facts the frozen rule set reads off the disk.

    `git-core` is emitted as `subpath` only where that directory exists, and
    `literal()` resolves symlinks — on Ubuntu `/bin` and `/sbin` resolve to
    `/usr/bin` and `/usr/sbin`, renaming two rules. Unpinned, the golden set
    reports the machine rather than the code: it also fails on a Mac with no
    Xcode installed. Grant paths keep the real filesystem, because the test
    resolves them exactly as the profile does.
    """
    real_is_dir, real_resolve = Path.is_dir, Path.resolve

    def is_dir(self, *args, **kwargs):
        if str(self).startswith("/Applications/Xcode.app"):
            return str(self) == _XCODE_GIT_CORE
        return real_is_dir(self, *args, **kwargs)

    def resolve(self, *args, **kwargs):
        if str(self).startswith(_MACOS_SYSTEM_PREFIXES):
            return self
        return real_resolve(self, *args, **kwargs)

    with unittest.mock.patch.object(Path, "is_dir", is_dir), \
            unittest.mock.patch.object(Path, "resolve", resolve):
        yield


class ProfileBytesAreUnchangedTests(unittest.TestCase):
    def _both(self, **overrides):
        grant = _grant(**overrides)
        # Both sides run the macOS implementation, so the comparison is between
        # the original and the moved code — never between two hosts.
        with _macos_host():
            original = git_broker.GitBroker._sandbox_profile(
                _FakeBroker(), readable=grant.readable, writable=grant.writable,
                network=grant.network, allow_shell=grant.starts_helper_programs,
            )
            moved = platform_support.confinement()._profile(
                grant, store=Path(tempfile.mkdtemp()),
            )
        return original, moved

    def test_the_content_addressed_filename_is_identical(self):
        original, moved = self._both()
        self.assertEqual(original.name, moved.name,
                         "the profile digest changed; every written profile would be renamed")

    def test_the_profile_bytes_are_identical(self):
        original, moved = self._both()
        self.assertEqual(original.read_bytes(), moved.read_bytes())

    def test_every_grant_shape_produces_identical_bytes(self):
        for overrides in (
            {"network": True},
            {"starts_helper_programs": False},
            {"readable": [Path("/tmp/a"), Path("/tmp/b")]},
            {"writable": []},
        ):
            with self.subTest(**overrides):
                original, moved = self._both(**overrides)
                self.assertEqual(original.name, moved.name)
                self.assertEqual(original.read_bytes(), moved.read_bytes())


class EnforcementIsKnowableTests(unittest.TestCase):
    """The silent degradation this seam exists to expose.

    `git_broker` used to build `[sandbox-exec, ...] if profile else fixed` and
    record nothing, so an unconfined run was indistinguishable from a confined
    one. Stage 0 makes the fact available and deliberately does NOT act on it.
    Stage 1 acts on it per platform, and both answers are asserted below.
    """

    def test_a_confined_run_says_so_and_wraps_the_command(self):
        with _macos_host():
            confined = platform_support.confinement().wrap(
                ["git", "status"], _grant(), store=Path(tempfile.mkdtemp()),
            )
        self.assertTrue(confined.enforced)
        self.assertEqual(confined.argv[0], "/usr/bin/sandbox-exec")
        self.assertEqual(confined.argv[-2:], ["git", "status"])

    def test_disabling_confinement_is_reported_not_hidden(self):
        """Neither platform hides it — including Linux with no bwrap installed,
        where an unasked-for confinement would refuse outright."""
        for platform, host in (("darwin", _macos_host()),
                               ("linux", _linux_host(bwrap_available=False))):
            with self.subTest(platform=platform), host:
                confined = platform_support.confinement().wrap(
                    ["git", "status"], _grant(), store=Path(tempfile.mkdtemp()), enabled=False,
                )
                self.assertFalse(confined.enforced, "an unconfined run must be knowable")
                self.assertEqual(confined.argv, ["git", "status"])
                self.assertIsNone(confined.profile)

    def test_an_unavailable_platform_yields_an_unconfined_command_not_a_crash(self):
        """macOS keeps Stage 0's behaviour: no confinement, and the run proceeds.

        Stage 0 left the decision open and it turned out to differ per platform,
        so the expectation is pinned to the platform that holds it. The Linux
        half is the next test, not a loosened assertion here.
        """
        with _macos_host(sandbox_exec_available=False):
            confined = platform_support.confinement().wrap(
                ["git", "status"], _grant(), store=Path(tempfile.mkdtemp()),
            )
        self.assertFalse(confined.enforced)
        self.assertEqual(confined.argv, ["git", "status"])

    def test_the_same_call_on_linux_REFUSES_rather_than_running_unconfined(self):
        """The other half of the divergence, asserted rather than assumed.

        macOS proceeds unconfined because in practice it never has to; on Linux
        unconfined would be the NORMAL case, so `wrap` raises instead. Dropping
        the macOS expectation, or widening it until both platforms pass, would
        hide the one difference the seam exists to make visible.
        """
        with _linux_host(bwrap_available=False):
            with self.assertRaises(platform_support.UnsupportedPlatformOperation) as caught:
                platform_support.confinement().wrap(
                    ["git", "status"], _grant(), store=Path(tempfile.mkdtemp()),
                )
        self.assertIn("bubblewrap", str(caught.exception), "must name what to install")


GOLDEN_RULES = [
    '(version 1)',
    '(allow default)',
    '(deny file-read*)',
    '(deny file-write*)',
    '(deny network*)',
    '(deny process-exec*)',
    '(allow file-read-metadata)',
    '(allow file-read-data (literal "/"))',
    '(allow process-exec (literal "/Applications/Xcode.app/Contents/Developer/usr/bin/git"))',
    '(allow process-exec (subpath "/Applications/Xcode.app/Contents/Developer/usr/libexec/git-core"))',
    '(allow process-exec (literal "/usr/bin/git"))',
    '(allow process-exec (literal "/usr/bin/ssh"))',
    '(allow process-exec (literal "/bin/sh"))',
    '(allow process-exec (literal "/bin/bash"))',
    '(allow file-read* (subpath "/System"))',
    '(allow file-read* (subpath "/usr"))',
    '(allow file-read* (subpath "/bin"))',
    '(allow file-read* (subpath "/sbin"))',
    '(allow file-read* (subpath "/Library/Apple"))',
    '(allow file-read* (subpath "/private/etc"))',
    '(allow file-read* (subpath "/private/var/db/timezone"))',
    '(allow file-read* (subpath "/dev"))',
    '(allow file-read* (subpath "/Applications/Xcode.app"))',
    '(allow file-read* (subpath "<READABLE>"))',
    '(allow file-write* (literal "/dev/null"))',
    '(allow file-write* (subpath "<WRITABLE>"))',
]


class GoldenProfileTests(unittest.TestCase):
    """The macOS profile rules, frozen — not compared against themselves.

    The reviewer found the earlier comparison tautological: it checked
    `GitBroker._sandbox_profile` against the moved implementation, and that
    method now DELEGATES to it, so it compared the moved code with itself and
    passed by construction. This freezes the rule set instead. Any reordering,
    rewording or dropped rule fails here — and would rename every
    `sandbox-<digest>.sb` the broker has ever written. Linux has no profile to
    freeze; its command shape is held in `test_platform_support_bwrap`.
    """

    def test_the_rule_set_is_exactly_what_it_was(self):
        readable = Path(tempfile.mkdtemp())
        writable = Path(tempfile.mkdtemp())
        with _macos_host(), _a_macos_filesystem():
            profile = platform_support.confinement()._profile(
                Grant(readable=[readable], writable=[writable],
                      network=False, starts_helper_programs=True),
                store=Path(tempfile.mkdtemp()),
            )
            rendered = [
                line.replace(str(readable.resolve()), "<READABLE>")
                    .replace(str(writable.resolve()), "<WRITABLE>")
                for line in profile.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(rendered, GOLDEN_RULES)

    def test_the_digest_follows_the_rules(self):
        """The filename is content-addressed, so the digest must track the text."""
        import hashlib
        readable = Path(tempfile.mkdtemp())
        with _macos_host():
            profile = platform_support.confinement()._profile(
                Grant(readable=[readable], writable=[], network=False,
                      starts_helper_programs=True),
                store=Path(tempfile.mkdtemp()),
            )
        expected = hashlib.sha256(
            "\n".join(profile.read_text(encoding="utf-8").splitlines()).encode("utf-8")
        ).hexdigest()[:16]
        self.assertEqual(profile.name, f"sandbox-{expected}.sb")


if __name__ == "__main__":
    unittest.main()

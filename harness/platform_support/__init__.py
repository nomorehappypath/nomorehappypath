# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""One place where platform-specific behaviour lives, selected once.

`docs/specs/LINUX_STAGE0_PLATFORM_SEAM.md`. Stage 0 moves the macOS
implementations that already exist behind interfaces so Stage 1 has somewhere
to put a Linux one. It adds no Linux code and changes nothing a macOS owner can
observe: the values below are the same objects the callers used before, reached
by a different route.

Selection happens once, here. No module outside this package tests
`sys.platform` to decide behaviour — recording the platform as evidence
(`harness/execution_identity.py`, `harness/board.py`) is not deciding, and is
deliberately left where it is.
"""
from __future__ import annotations

import sys


from harness.platform_support.defaults import (  # noqa: E402  (re-exported names)
    FolderSelectionTimeout, ProcessTableUnavailable, SessionSurface,
    UnsupportedPlatformOperation,
)


class UnsupportedPlatform(RuntimeError):
    """No implementation exists for a platform-specific OPERATION.

    Reserved for operations that genuinely cannot work here — launching a
    visible terminal, installing a service. Never raised for values, because a
    value the product already uses on every platform is not platform-specific
    merely by living in this package.
    """


def _selected():
    """The implementations for THIS platform.

    Stage 0 returned the same module everywhere on purpose: refusing non-macOS
    at selection time was tried, and on the project's Ubuntu target it made
    `global_settings` unimportable, so the suite could not run on the very
    machine Stage 1 must be verified on.

    Stage 1 selects for real, because there is now something to select. The
    Linux module INHERITS every default and overrides only what genuinely
    differs, so an unported seam keeps working rather than becoming a blank.
    An unknown platform still gets the defaults: refusing here would break
    imports again, and each operation already refuses by name when it cannot
    act.
    """
    if sys.platform.startswith("linux"):
        from harness.platform_support import linux
        return linux
    from harness.platform_support import defaults
    return defaults


def discovery():
    """The tool-discovery policies in force."""
    return _selected().DISCOVERY


def process_identity():
    """Process identity and start tokens for this platform."""
    return _selected().PROCESS_IDENTITY


def confinement():
    """OS-level confinement for commands run on the owner's behalf."""
    return _selected().CONFINEMENT


def browser_host():
    """Where this platform keeps a headless browser."""
    return _selected().BROWSER_HOST



def folder_chooser():
    """The native folder picker for this platform."""
    return _selected().FOLDER_CHOOSER



def terminal_host():
    """The visible agent terminal for this platform."""
    return _selected().TERMINAL_HOST

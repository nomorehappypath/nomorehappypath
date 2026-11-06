# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The shipped role choices before 2026-11-05 (Codex builder, Claude reviewer and CTO).

A new install now ships a Claude builder and a Codex reviewer. Tests that drive a fake Codex builder or a Claude reviewer pin these on
purpose by passing them as `settings_override`, so they keep testing the same mechanism whatever the shipped defaults become.
"""
from __future__ import annotations

import copy

OLD_DEFAULTS = {
    "delivery": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"},
    "cto": {"provider": "claude", "model": "opus", "effort": "high"},
    "reviewer": {"provider": "claude", "model": "opus", "effort": "max"},
}


def old_settings() -> dict:
    """Return a fresh deep copy of OLD_DEFAULTS that a test may modify freely. Takes no arguments."""
    return copy.deepcopy(OLD_DEFAULTS)

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Test-run isolation: no test may ever touch the owner's real home config.

The 2026-08-21 incident: every manager test that opened a project wrote a
codex trust entry into the owner's real ~/.codex/config.toml - one hundred
garbage entries across three suite runs. Importing the tests package pins
CODEX_HOME to a throwaway directory before any test code runs.
"""
import os
import tempfile

if not os.environ.get("HARNESS_TESTS_CODEX_HOME_PINNED"):
    os.environ["CODEX_HOME"] = tempfile.mkdtemp(prefix="harness-tests-codex-home-")
    os.environ["HARNESS_TESTS_CODEX_HOME_PINNED"] = "1"

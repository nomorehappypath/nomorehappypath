# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Bare release fixtures: tests that record a release for a task with no plan and no review opt out of the production rule
"every release needs a valid final independent review" explicitly (2026-10-24). Production code never sets this."""
from unittest import mock

from harness import board


def allow_fixture_releases():
    patch = mock.patch.object(board, "RELEASE_FIXTURE_EXEMPT", True)
    return patch.start, patch.stop   # setUpModule, tearDownModule

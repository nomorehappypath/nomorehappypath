# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A screen check that never closed its browser no longer holds NMHP's browser until the owner presses Pause/Resume
(owner 2026-11-27): past STALE_HOLD_SECONDS the next launch closes it and goes on."""
from __future__ import annotations

import time
import unittest

from harness import browser_acceptance as ba


class FakeSession:
    def __init__(self):
        self.closed = False
        self.process_lock_held = True

    def close(self, *, validate=True):
        self.closed = True
        ba._HOLDER.clear()
        ba._PROCESS_LOCK.release()
        self.process_lock_held = False


class StaleHolderTests(unittest.TestCase):
    def tearDown(self):
        ba._HOLDER.clear()
        if ba._PROCESS_LOCK.locked():
            ba._PROCESS_LOCK.release()

    def hold(self, seconds_ago: float) -> FakeSession:
        self.assertTrue(ba._PROCESS_LOCK.acquire(timeout=1))
        session = FakeSession()
        ba._HOLDER.update(session=session, since=time.monotonic() - seconds_ago)
        return session

    def test_a_holder_past_the_limit_is_closed_and_the_lock_is_taken(self):
        session = self.hold(ba.STALE_HOLD_SECONDS + 5)
        self.assertTrue(ba._free_stale_holder())
        self.assertTrue(session.closed)
        self.assertTrue(ba._PROCESS_LOCK.locked(), "the caller now holds it")

    def test_a_holder_within_the_limit_is_left_alone(self):
        session = self.hold(60)
        self.assertFalse(ba._free_stale_holder())
        self.assertFalse(session.closed)

    def test_no_holder_means_nothing_to_free(self):
        self.assertFalse(ba._free_stale_holder())


if __name__ == "__main__":
    unittest.main()

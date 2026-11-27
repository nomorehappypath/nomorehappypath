# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Phone apps, step 1 (owner direction 2026-11-25): the mobile build/test-environment answer, proven
directly against record_owner_message and begin_task - stored once with a new direction, remembered
project-wide for the next one, frozen per task, refused server-side (never only in the dialog) when Yes
has no iPhone or iPhone is chosen off a Mac, and routed to Delivery in the queued instruction's own text
(the Reviewer's own contract check reads exactly that). One test per requirement, no more; the rendered
dialog itself (the question, its refusals, the greyed box) is the Reviewer's own browser-rendered check.
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import board, control

BASE_COMMIT = "0003ae6"   # the task's base commit, before this feature existed


def _load_base_save_fixture():
    """Load tests/fixtures/base_board_state_upgrade.py: 0003ae6's own `board_dir`/`_state_path` and the
    atomic-write tail of its `locked_state`, frozen verbatim (CTO finding, owner release repair
    2026-11-27, round 2 of B3: reading BASE_COMMIT through `git show` skipped on a tar-only checkout,
    so the round trip never ran there; this fixture needs no git at all). Returns that module."""
    path = Path(__file__).resolve().parent / "fixtures" / "base_board_state_upgrade.py"
    spec = importlib.util.spec_from_file_location("base_board_state_upgrade", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MobileDirectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _waiting_agent(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                                vendor="OpenAI", session_id=session["id"])
        return session, agent

    def test_yes_without_iphone_is_refused_on_the_server(self):
        _session, agent = self._waiting_agent()
        with mock.patch.object(board, "host_is_macos", return_value=True):
            with self.assertRaisesRegex(ValueError, "Choose iPhone"):
                board.record_owner_message(self.root, agent["id"], "Build a greeting app.", mobile=True, iphone=False)

    def test_iphone_off_a_mac_is_refused_on_the_server(self):
        _session, agent = self._waiting_agent()
        with mock.patch.object(board, "host_is_macos", return_value=False):
            with self.assertRaisesRegex(ValueError, "Mac with Xcode"):
                board.record_owner_message(self.root, agent["id"], "Build a greeting app.", mobile=True, iphone=True)

    def test_yes_iphone_is_stored_remembered_and_routed_to_delivery(self):
        session, agent = self._waiting_agent()
        with mock.patch.object(board, "host_is_macos", return_value=True):
            board.record_owner_message(self.root, agent["id"], "Build a greeting app.", mobile=True, iphone=True)
        instructions = control.take_instructions(self.root, session["id"])
        self.assertTrue(any("iPhone" in item.get("text", "") for item in instructions))
        state = board.snapshot(self.root)
        self.assertEqual(state["project_mobile"]["mobile"], True)
        self.assertEqual(state["project_mobile"]["iphone"], True)

    def test_no_is_the_default_and_a_task_freezes_whatever_was_answered(self):
        session, agent = self._waiting_agent()
        with mock.patch.object(board, "host_is_macos", return_value=True):
            board.record_owner_message(self.root, agent["id"], "Build a greeting app.", mobile=True, iphone=True)
        begun = board.begin_task(self.root, agent["id"], "mobile-demo-task")
        state = board.snapshot(self.root)
        self.assertEqual(board.task_mobile_state(state, "mobile-demo-task"), {"mobile": True, "iphone": True})
        self.assertTrue(begun)   # begin_task returned a result at all

        # A second, later direction that answers No (the dialog's own default, or the owner switching
        # back) is frozen into ITS OWN task without disturbing the first task's already-frozen answer.
        _session2, agent2 = self._waiting_agent()
        board.record_owner_message(self.root, agent2["id"], "Write a spec for the next screen.")
        board.begin_task(self.root, agent2["id"], "no-mobile-task")
        state = board.snapshot(self.root)
        self.assertEqual(board.task_mobile_state(state, "no-mobile-task"), {"mobile": False, "iphone": False})
        self.assertEqual(board.task_mobile_state(state, "mobile-demo-task"), {"mobile": True, "iphone": True})

    def test_a_state_with_no_mobile_keys_at_all_behaves_as_no(self):
        # CTO finding, owner release repair 2026-11-26 (B3): the saved-data upgrade rule covers apps
        # NMHP builds, not NMHP's own upgrade (SAVED_DATA_UPGRADE_SAFETY.md §9); project_mobile/task_mobile
        # are additive and optional, so a state saved before this feature existed (neither key present)
        # must read as No, never raise.
        self.assertEqual(board.task_mobile_state({}, "some-task"), {"mobile": False, "iphone": False})

    def test_project_mobile_and_task_mobile_survive_an_old_code_round_trip(self):
        # CTO finding, owner release repair 2026-11-27 (round 2 of B3): the saved choices must survive
        # an update AND a rollback. Current code saves them; 0003ae6's own frozen load-mutate-save cycle
        # (before this feature existed) then resaves the project untouched, standing in for the owner
        # going back to the previous NMHP version; current code reads the result again. Both directions
        # pass only if neither version's own save cycle drops a key it does not itself recognize.
        session, agent = self._waiting_agent()
        with mock.patch.object(board, "host_is_macos", return_value=True):
            board.record_owner_message(self.root, agent["id"], "Build a greeting app.", mobile=True, iphone=True)
        board.begin_task(self.root, agent["id"], "mobile-demo-task")

        base_save = _load_base_save_fixture()
        base_save.resave_untouched(self.root)   # the old version opening and closing the project, unmutated

        reloaded = board.snapshot(self.root)
        self.assertEqual(reloaded["project_mobile"]["mobile"], True)
        self.assertEqual(reloaded["project_mobile"]["iphone"], True)
        self.assertEqual(board.task_mobile_state(reloaded, "mobile-demo-task"), {"mobile": True, "iphone": True})


if __name__ == "__main__":
    unittest.main()

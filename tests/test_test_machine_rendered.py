# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof of the Test machine card and Connect dialog, driven like the owner would (real page, real clicks).

The page is the real Mission Control served by the real manager in headless Chrome; the click/type driver is the
harness's own screen-script runner. Only the password half of ssh is the stand-in (tests/machine_support.py).

Run:  PYTHONPATH=. python3 -m unittest tests.test_test_machine_rendered -v
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from harness import board_viewer, browser_acceptance, control, project_manager, project_registry as registry, screen_script, test_machine
from tests.environment_support import require_loopback
from tests.machine_support import FakeMachine
from tests.test_test_machine import PASSWORD, connected_descriptor


class TestMachineRenderedTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        code = self.base / "alpha"
        code.mkdir()
        self.entry = registry.register(self.home, "alpha", code)
        self.context = registry.context_for_entry(self.entry)

    def serve(self, machine=None):
        env = {"HARNESS_TEST_MACHINE_SSH": str(machine.shim)} if machine else {}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        manager = project_manager.ProjectManager(self.home, board_port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/", manager

    def walk(self, url, steps):
        runtime = Path(tempfile.mkdtemp(prefix="machine-render-"))
        self.addCleanup(shutil.rmtree, runtime, True)
        run = screen_script.run_script(url, screen_script.parse_steps(json.dumps(steps)), runtime, timeout=80)
        self.assertEqual(run["error"], "", run)
        return run

    def assert_passed(self, run):
        failed = [item for item in run["steps"] if not item["ok"]]
        self.assertFalse(failed, f"{failed} | page text: {run['final_text'][:600]}")
        self.assertTrue(any(run["pictures"]), "a blank render is a finding, not a pass")
        self.assertNotIn(PASSWORD, run["final_text"])

    def test_the_owner_connects_a_machine_from_the_page_and_sees_it_connected(self):
        with FakeMachine(self) as machine:
            url, manager = self.serve(machine)
            run = self.walk(url, [
                {"expect": "alpha"},
                {"click": {"text": "Test machine"}},
                {"expect": "No machine connected yet."},
                {"type": {"selector": "#machine-address", "text": "127.0.0.1"}},
                {"type": {"selector": "#machine-user", "text": machine.user}},
                {"type": {"selector": "#machine-port", "text": str(machine.port)}},
                {"type": {"selector": "#machine-password", "text": PASSWORD}},
                {"click": {"selector": "#machine-connect"}},
                {"expect": f"Connected: {machine.user}@127.0.0.1"},
                {"expect": "Machine identity SHA256:"},
            ])
            self.assert_passed(run)
            value = test_machine.read(self.context)
            self.assertTrue(value["connected"] and not value["reauth_required"])
            self.assertEqual(value["address"], "127.0.0.1")
            self.assertNotIn(PASSWORD, json.dumps(value))

    def test_a_wrong_password_shows_one_plain_reason_in_the_dialog(self):
        with FakeMachine(self) as machine:
            url, manager = self.serve(machine)
            run = self.walk(url, [
                {"click": {"text": "Test machine"}},
                {"type": {"selector": "#machine-address", "text": "127.0.0.1"}},
                {"type": {"selector": "#machine-user", "text": machine.user}},
                {"type": {"selector": "#machine-port", "text": str(machine.port)}},
                {"type": {"selector": "#machine-password", "text": "Pw-wrong-wrong-0000"}},
                {"click": {"selector": "#machine-connect"}},
                {"expect": "The password was not accepted"},
            ])
            self.assert_passed(run)
            self.assertNotIn("Pw-wrong-wrong-0000", run["final_text"])
            self.assertFalse(test_machine.read(self.context)["connected"])

    def test_an_agents_request_shows_as_a_card_on_the_project_without_any_terminal(self):
        test_machine.request_card(self.context, "Delivery Agent (agent-1)", "run the Windows build on the VM", task="T1")
        url, _manager = self.serve()
        run = self.walk(url, [
            {"expect": "An agent needs the test machine."},
            {"expect": "run the Windows build on the VM"},
            {"click": {"selector": "button[data-act='machine']"}},
            {"expect": "No machine connected yet."},
        ])
        self.assert_passed(run)

    def test_after_a_stop_the_card_asks_for_the_password_again(self):
        connected_descriptor(self.context)
        test_machine.mark_reauth(self.context, "project_stopped")
        url, _manager = self.serve()
        run = self.walk(url, [
            {"expect": "needs you."},
            {"expect": "asks for its password again"},
            {"click": {"selector": "button[data-act='machine']"}},
            {"expect": "Enter the password again below and press Connect."},
        ])
        self.assert_passed(run)

    def test_a_connected_ready_machine_is_calm_and_names_what_it_is(self):
        url, manager = self.serve()
        connected_descriptor(self.context, identity=manager.machine_identity, ready={"state": True, "note": "rust 1.80 installed"})
        run = self.walk(url, [{"expect": "Test machine connected: u@127.0.0.1"}, {"expect": "ready for testing"}])
        self.assert_passed(run)

    def test_after_a_changed_identity_the_password_typed_once_is_kept_for_the_confirmed_connect(self):
        # 2026-10-21 follow-up: Confirm used to demand the password again because the field was cleared while the identity was shown
        with FakeMachine(self) as machine:
            url, manager = self.serve(machine)
            connected_descriptor(self.context, identity=manager.machine_identity, address="127.0.0.1", user=machine.user, port=machine.port,
                                 host_key={"type": "ssh-ed25519", "fingerprint": "SHA256:somethingelse", "line": "x"})
            run = self.walk(url, [
                {"click": {"text": "Manage"}},
                {"type": {"selector": "#machine-password", "text": PASSWORD}},
                {"click": {"selector": "#machine-connect"}},
                {"expect": "This machine’s identity changed."},
                {"click": {"selector": "#machine-confirm"}},
                {"expect": f"Connected: {machine.user}@127.0.0.1"},
            ])
            self.assert_passed(run)
            self.assertNotIn("Enter the account password", run["final_text"])
            self.assertNotEqual(test_machine.read(self.context)["host_key"]["fingerprint"], "SHA256:somethingelse")

    def test_while_the_agents_set_the_machine_up_the_dialog_says_there_is_nothing_to_do(self):
        url, manager = self.serve()
        connected_descriptor(self.context, identity=manager.machine_identity, baseline_state="incomplete")
        run = self.walk(url, [
            {"expect": "tools are being changed"},
            {"click": {"selector": "button[data-act='machine']"}},
            {"expect": "The agents are changing this machine."},
            {"expect": "nothing for you to do while they work"},
        ])
        self.assert_passed(run)
        self.assertNotIn("Enter the password again below", run["final_text"])

    def test_confirm_is_bound_to_the_machine_whose_identity_was_shown(self):
        with FakeMachine(self) as machine:
            url, manager = self.serve(machine)
            connected_descriptor(self.context, identity=manager.machine_identity, address="127.0.0.1", user=machine.user, port=machine.port,
                                 host_key={"type": "ssh-ed25519", "fingerprint": "SHA256:somethingelse", "line": "x"})
            run = self.walk(url, [
                {"click": {"text": "Manage"}},
                {"type": {"selector": "#machine-password", "text": PASSWORD}},
                {"click": {"selector": "#machine-connect"}},
                {"expect": "This machine’s identity changed."},
                {"type": {"selector": "#machine-user", "text": "someoneelse"}},
                {"click": {"selector": "#machine-confirm"}},
                {"expect": "changed after the identity was read"},
            ])
            self.assert_passed(run)
            self.assertEqual(test_machine.read(self.context)["host_key"]["fingerprint"], "SHA256:somethingelse", "nothing was replaced")

    def test_the_open_window_follows_the_machine_when_the_agents_finish_or_start_a_setup(self):
        url, manager = self.serve()
        connected_descriptor(self.context, identity=manager.machine_identity, baseline_state="complete", ready={"state": True, "note": "ok"})
        import threading

        def change_later():
            import time
            time.sleep(4)
            test_machine.update(self.context, lambda value: value.update({"baseline_state": "incomplete", "ready": {}}), create=False)
        threading.Thread(target=change_later, daemon=True).start()
        run = self.walk(url, [
            {"click": {"text": "Manage"}},
            {"expect": "Connected: u@127.0.0.1"},
            {"expect": "The agents are changing this machine."},
        ])
        self.assert_passed(run)

    def test_release_is_a_designed_two_step_not_a_browser_confirm_box(self):
        url, manager = self.serve()
        connected_descriptor(self.context, identity=manager.machine_identity)
        run = self.walk(url, [
            {"expect": "Test machine connected"},
            {"click": {"text": "Manage"}},
            {"click": {"text": "Release this machine"}},
            {"expect": "Press again to release"},
        ])
        self.assert_passed(run)
        self.assertTrue(test_machine.read(self.context)["connected"], "one press releases nothing")

    def test_a_machine_held_for_a_project_with_no_connection_can_still_be_released_in_the_dialog(self):
        url, manager = self.serve()
        test_machine.reserve_target(manager.home, self.entry["id"], "10.9.9.9", 22)
        run = self.walk(url, [
            {"click": {"text": "Test machine"}},
            {"expect": "No machine connected yet."},
            {"click": {"text": "Release this machine"}},
            {"expect": "Press again to release"},
            {"click": {"selector": "#machine-release"}},
            {"expect": "The machine is free for another project"},
        ])
        self.assert_passed(run)
        self.assertFalse(test_machine.reserved_for(manager.home, self.entry["id"]))

    def serve_board(self, context, manager_url="http://127.0.0.1:1"):
        control.initialize(context)
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            context.code_root, project_name="alpha", manager_url=manager_url, settings_home=self.home,
            project_id=self.entry["id"], chat_action_token="board-token"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/"

    def test_the_project_board_shows_the_agents_request_and_leads_to_the_connect_form(self):
        """The owner sits on the project's Mission Control page: the manager-owned card must be THERE, not only in the list."""
        board_context = type(self.context)(self.context.code_root, self.context.data_root, self.context.workspace_root)
        test_machine.request_card(board_context, "Delivery Agent (agent-1)", "run the Windows build on the VM", task="T1")
        url, manager = self.serve()
        board_url = self.serve_board(board_context, manager_url=url.rstrip("/"))
        run = self.walk(board_url, [
            {"expect": "An agent needs the test machine"},
            {"expect": "run the Windows build on the VM"},
            {"expect": "Connect the test machine"},
        ])
        self.assert_passed(run)
        self.assertNotIn("Paste it into Terminal", run["final_text"])
        # the link goes to the manager page, which opens the form for exactly this project
        run = self.walk(f"{url}?machine={self.entry['id']}", [
            {"expect": "Test machine — alpha"},
            {"expect": "An agent"},
            {"expect": "run the Windows build on the VM"},
        ])
        self.assert_passed(run)

    def test_the_board_card_stays_quiet_while_the_agents_set_the_machine_up_even_with_an_open_request(self):
        board_context = type(self.context)(self.context.code_root, self.context.data_root, self.context.workspace_root)
        url, manager = self.serve()
        connected_descriptor(self.context, identity=manager.machine_identity, baseline_state="incomplete")
        test_machine.request_card(board_context, "Delivery Agent (agent-1)", "finish installing the toolchain", task="T1")
        board_url = self.serve_board(board_context, manager_url=url.rstrip("/"))
        run = self.walk(board_url, [{"expect": "Updated"}])
        self.assert_passed(run)
        self.assertNotIn("Enter the password again", run["final_text"])
        self.assertNotIn("An agent needs the test machine", run["final_text"])
        self.assertNotIn("finish installing the toolchain", run["final_text"], "the card is quiet: the agents are on it")

    def test_the_project_board_card_asks_again_after_a_stop_and_disappears_when_connected(self):
        board_context = type(self.context)(self.context.code_root, self.context.data_root, self.context.workspace_root)
        url, manager = self.serve()
        connected_descriptor(self.context, identity=manager.machine_identity)
        board_url = self.serve_board(board_context, manager_url=url.rstrip("/"))
        calm = self.walk(board_url, [{"expect": "Updated"}])
        self.assertNotIn("The test machine", calm["final_text"])
        self.assertNotIn("Connect the test machine", calm["final_text"])
        test_machine.mark_reauth(self.context, "project_stopped")
        run = self.walk(board_url, [{"expect": "The test machine u@127.0.0.1 needs you"}, {"expect": "Enter the password again"}])
        self.assert_passed(run)


if __name__ == "__main__":
    unittest.main()

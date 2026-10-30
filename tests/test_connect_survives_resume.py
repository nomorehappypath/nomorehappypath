# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A Connect the owner makes while the project is paused survives the Resume (windows-walk item 4).

Machine inputs removed: the owner's HOME, ~/.ssh and any ssh config (FakeMachine: real sshd on a high port, password stand-in).

Run:  PYTHONPATH=. python3 -m unittest tests.test_connect_survives_resume -v
"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from harness import board, control, project_manager, project_registry as registry, test_machine, test_machine_followup
from tests import test_test_machine as base
from tests.machine_support import FakeMachine


class ConnectAfterPauseTests(base.ManagerTests):
    def stop(self, manager, resume=False):
        return manager._machine_stop(self.entry["id"], "project_stopped", resume=resume)

    def said(self):
        sent = []
        sessions = [{"id": "s-delivery"}, {"id": "s-reviewer"}, {"id": "s-cto"}]
        patches = (mock.patch.object(test_machine_followup, "_active_sessions", return_value=sessions),
                   mock.patch.object(test_machine_followup.control, "enqueue_instruction", side_effect=lambda _r, sid, text, **k: sent.append((sid, text))))
        return sent, patches

    def test_pause_connect_resume_keeps_the_authorization_and_tells_every_agent(self):
        with FakeMachine(self) as machine, self.served(machine) as (base_url, manager):
            self.do_connect(base_url, machine)
            self.stop(manager)                                            # the pause
            self.assertEqual(test_machine.usability(self.context)[1], "reauth_required")
            self.do_connect(base_url, machine)                            # the owner connects while paused
            before = test_machine.read(self.context)
            sent, patches = self.said()
            with patches[0], patches[1]:
                self.assertEqual(self.stop(manager, resume=True), "kept")
                self.assertTrue(test_machine.usability(self.context)[0], "Resume must not ask for the password again")
                after = test_machine.read(self.context)
                self.assertEqual((after["revision"], after["auth_epoch"]), (before["revision"], before["auth_epoch"]))
                self.assertTrue(test_machine_followup.handoff_pending(self.context))
                manager._machine_handoff_if_pending(self.entry["id"], self.context)
            self.assertEqual(sorted(sid for sid, _ in sent), ["s-cto", "s-delivery", "s-reviewer"])
            self.assertIn("machine-status first", sent[0][1])
            self.assertFalse(test_machine_followup.handoff_pending(self.context))

    def test_every_other_stop_still_asks_for_the_password(self):
        with FakeMachine(self) as machine, self.served(machine) as (base_url, manager):
            self.do_connect(base_url, machine)
            self.stop(manager)                                            # pause of a running project
            self.assertEqual(self.stop(manager, resume=True), "required", "no Connect after the pause: the password again")
            self.do_connect(base_url, machine)
            self.stop(manager)
            self.assertEqual(self.stop(manager, resume=True), "required", "a second pause after the Connect ends the exemption")
            self.do_connect(base_url, machine)
            manager.close_project(self.entry["id"])
            self.assertEqual(test_machine.usability(self.context)[1], "reauth_required")
            self.assertEqual(self.stop(manager, resume=True), "required")

    def test_a_retried_resume_keeps_again_and_a_later_stop_ends_it(self):
        with FakeMachine(self) as machine, self.served(machine) as (base_url, manager):
            self.stop(manager)
            self.do_connect(base_url, machine)
            self.assertEqual(self.stop(manager, resume=True), "kept")
            self.assertEqual(self.stop(manager, resume=True), "kept", "an interrupted Resume pressed again")
            self.stop(manager)                                            # a new pause
            self.assertEqual(test_machine.usability(self.context)[1], "reauth_required")
            self.assertEqual(self.stop(manager, resume=True), "required")

    def test_a_missing_marker_fails_closed(self):
        with self.served() as (_base, manager):
            base.connected_descriptor(self.context, alive=True)          # authorized, but no connected_at_stop_seq recorded
            self.assertEqual(self.stop(manager, resume=True), "required")
            self.assertEqual(test_machine.usability(self.context)[1], "reauth_required")

    def test_no_record_is_reported_as_none(self):
        with self.served() as (_base, manager):
            self.assertEqual(self.stop(manager, resume=True), "none")

    def test_a_connect_whose_challenge_was_consumed_before_a_stop_cannot_publish_even_with_no_record(self):
        with FakeMachine(self) as machine, self.served(machine) as (base_url, manager):
            _s, scan = self.post(base_url, self.api(base_url, "scan"), {"address": "127.0.0.1", "user": machine.user, "port": machine.port})
            self.assertFalse(test_machine.descriptor_path(self.context).exists(), "a first Connect has no record yet")
            real = manager.machine_challenges.consume

            def consume_then_stop(*args, **kwargs):
                found = real(*args, **kwargs)
                self.stop(manager, resume=True)                          # the stop lands between consume and the job registration
                return found

            with mock.patch.object(manager.machine_challenges, "consume", consume_then_stop):
                with self.assertRaises(test_machine.MachineError) as caught:
                    manager.machine_connect(self.entry["id"], {"challenge": scan["challenge"], "password": base.PASSWORD}, loopback=True)
            self.assertEqual(caught.exception.code, "cancelled")
            self.assertEqual(machine.authorized_keys.read_text(), "", "nothing was installed")

    def test_a_running_connect_is_still_cancelled_by_a_resume(self):
        with FakeMachine(self) as machine, self.served(machine) as (base_url, manager):
            (machine.base / "shim.mode").write_text("posix")
            _s, scan = self.post(base_url, self.api(base_url, "scan"), {"address": "127.0.0.1", "user": machine.user, "port": machine.port})
            outcome = {}

            def run():
                try:
                    outcome["value"] = manager.machine_connect(self.entry["id"], {"challenge": scan["challenge"], "password": base.PASSWORD}, loopback=True)
                except test_machine.MachineError as error:
                    outcome["error"] = error
            real = test_machine.run_with_password

            def slow(argv, password, **kw):
                base.time.sleep(1.5)
                return real(argv, password, **kw)
            with mock.patch.object(test_machine, "run_with_password", slow):
                thread = threading.Thread(target=run)
                thread.start()
                base.time.sleep(0.5)
                self.stop(manager, resume=True)
                thread.join(timeout=60)
            self.assertEqual(outcome["error"].code, "cancelled")
            self.assertNotEqual(test_machine.usability(self.context)[1], "ok")

    def test_the_announcement_is_idempotent_and_obsolete_ones_are_dropped(self):
        with self.served() as (_base, manager):
            base.connected_descriptor(self.context, alive=True, handoff_pending=1, connected_at_stop_seq=0)
            sent, patches = self.said()
            with patches[0], patches[1]:
                self.assertTrue(test_machine_followup.after_connect(self.context)["completed"])
                first = len(sent)
                self.assertTrue(test_machine_followup.after_connect(self.context)["completed"])
                self.assertEqual(len(sent), first, "the same authorization is announced once")
            self.assertFalse(test_machine_followup.handoff_pending(self.context))
            # a stop during delivery: the late completion must not write for the dead authorization
            base.connected_descriptor(self.context, alive=True, handoff_pending=1, announced_epoch=-1)
            sent2, patches2 = self.said()

            def stop_while_sending(_r, sid, text, **k):
                sent2.append(sid)
                test_machine.mark_reauth(self.context, "project_stopped")
            with patches2[0], mock.patch.object(test_machine_followup.control, "enqueue_instruction", side_effect=stop_while_sending):
                self.assertFalse(test_machine_followup.after_connect(self.context)["completed"])
            self.assertEqual(test_machine.read(self.context)["handoff_pending"], 0)
            self.assertFalse(test_machine_followup.after_connect(self.context)["completed"], "nothing is announced while the password is required")

    def test_a_failed_handoff_stays_pending_and_the_status_refresh_retries_it(self):
        with self.served() as (_base, manager):
            manager._machine_reconcile_once(self.entry)   # the project's first touch (its restart check) has already happened
            base.connected_descriptor(self.context, alive=True, handoff_pending=1, connected_at_stop_seq=0)
            with mock.patch.object(test_machine_followup, "after_connect", side_effect=OSError("disk")):
                manager._machine_handoff_if_pending(self.entry["id"], self.context)
            self.assertTrue(test_machine_followup.handoff_pending(self.context))
            sent, patches = self.said()
            with patches[0], patches[1]:
                manager._machine_summary(registry._find(registry.load(self.home), self.entry["id"]))
            self.assertFalse(test_machine_followup.handoff_pending(self.context))
            self.assertTrue(sent)

    def test_the_card_does_not_ask_for_a_password_when_authorization_stands(self):
        base.connected_descriptor(self.context, alive=True)
        with mock.patch.object(test_machine, "scan_host_key", return_value={"fingerprint": "SHA256:abc"}), \
                mock.patch.object(test_machine, "verify_key_login_detail", return_value=(True, "ok", "")):
            self.assertNotIn("password", test_machine.diagnose(self.context)["message"])
            test_machine.mark_reauth(self.context, "project_stopped")
            self.assertIn("password confirmation is missing", test_machine.diagnose(self.context)["message"])


class ResumeThroughTheManagerTests(unittest.TestCase):
    """The real pause and resume of a project with its three agents' sessions; the Connect is a descriptor as Connect leaves it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        code = self.base / "code"
        code.mkdir()
        self.entry = registry.register(self.home, "resume-project", code)
        registry.activate(self.home, self.entry["id"])   # only the open project's board is read (owner 2026-10-28)
        self.context = registry.context_for_entry(self.entry)

    def resume(self, manager):
        class Worker:
            pid = 88123
            def poll(self): return None
            def terminate(self): return None
            def wait(self, timeout=None): return 0
            def kill(self): return None
        with mock.patch("harness.project_manager.subprocess.Popen", return_value=Worker()):
            return manager.resume_project(self.entry["id"])

    def receipts(self):
        with control.locked_state(self.context) as state:
            return [r for r in state.get("instruction_receipts", {}).values() if r.get("source") == "test-machine"]

    def test_connect_during_the_pause_then_resume_tells_the_relaunched_agents_and_keeps_the_machine(self):
        sessions = [control.create(self.context, kind) for kind in ("codex_delivery", "claude_reviewer", "claude_cto")]
        for session, role, task, vendor in zip(sessions, ("engineering", "qa", "cto"), (board.AWAITING_OWNER_DIRECTION, "REVIEW_QUEUE", "GLOBAL_MONITOR"), ("OpenAI", "Anthropic", "Anthropic")):
            board.register(self.context, role, task, vendor=vendor, session_id=session["id"])
        manager = project_manager.ProjectManager(self.home, board_port=0, terminal_launcher=lambda _root, s: None)
        base.connected_descriptor(self.context, alive=True, identity=manager.machine_identity, connected_at_stop_seq=0, stop_seq=0)
        manager.pause_project(self.entry["id"], drain_seconds=0, stop_timeout=0)
        self.assertEqual(test_machine.usability(self.context)[1], "reauth_required")
        stop_seq = test_machine.read(self.context)["stop_seq"]
        # the owner's Connect while paused, as Connect finalizes it
        base.connected_descriptor(self.context, alive=True, identity=manager.machine_identity, connected_at_stop_seq=stop_seq, handoff_pending=5, auth_epoch=5)
        self.resume(manager)
        self.assertTrue(test_machine.usability(self.context)[0], "Resume asked for the password again")
        told = {r["session_id"] for r in self.receipts()}
        self.assertEqual(told, {s["id"] for s in sessions})
        self.assertFalse(test_machine_followup.handoff_pending(self.context))


# the inherited ManagerTests are run by tests.test_test_machine; only this class's own tests run here
for _name in [n for n in dir(base.ManagerTests) if n.startswith("test_") and n not in ConnectAfterPauseTests.__dict__]:
    setattr(ConnectAfterPauseTests, _name, None)


if __name__ == "__main__":
    unittest.main()

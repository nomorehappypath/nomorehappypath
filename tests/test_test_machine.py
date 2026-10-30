# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner's test machine, connected from Mission Control (task 2026-10-20_test-machine-zero-touch).

Real sshd + real ssh for everything key-only; a password-prompting ssh stand-in for the password half (see
tests/machine_support.py). Machine inputs removed: the owner's HOME, ~/.ssh, agents, any ssh config.

Run:  PYTHONPATH=. python3 -m unittest tests.test_test_machine -v
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import project_manager, project_registry as registry, test_machine
from harness.project_context import ProjectContext
from tests.environment_support import require_loopback
from tests.machine_support import FakeMachine

PASSWORD = "Pw-correct-horse-7391"


def make_context(base: Path) -> ProjectContext:
    code = base / "proj"
    code.mkdir(parents=True, exist_ok=True)
    return ProjectContext.compatibility(code)


def connected_descriptor(context, alive: bool = True, identity=None, **changes):
    """A descriptor as Connect leaves it (without ssh), authorized by this very process unless alive=False."""
    def change(value):
        auth = test_machine.authorization_record(identity or test_machine.manager_identity())
        if not alive:
            auth["pid"] = 999999
        value.update({
            "connected": True, "address": "127.0.0.1", "user": "u", "port": 22,
            "host_key": {"type": "ssh-ed25519", "fingerprint": "SHA256:abc", "line": "x"},
            "revision": 1, "auth_epoch": 1, "reauth_required": False, "reauth_cause": "", "authorized_by": auth,
        })
        value.update(changes)
    test_machine.update(context, change)


class ValidationTests(unittest.TestCase):
    def test_good_and_bad_targets(self):
        self.assertEqual(test_machine.validate_target("192.168.64.5", "WindowsVM", 22), ("192.168.64.5", "WindowsVM", 22))
        self.assertEqual(test_machine.validate_target("vm.local", "a.b-c", "2222")[2], 2222)
        self.assertEqual(test_machine.validate_target("[::1]", "u", 22)[0], "::1")
        for address in ("", "-oProxyCommand=x", "a b", "x;y", "host/../x", "a" * 300):
            with self.assertRaises(test_machine.MachineError, msg=address):
                test_machine.validate_target(address, "u", 22)
        for user in ("", "-x", "a b", "u;rm", "u`x`", "a" * 80):
            with self.assertRaises(test_machine.MachineError, msg=user):
                test_machine.validate_target("10.0.0.1", user, 22)
        for port in (0, 65536, "x", -1):
            with self.assertRaises(test_machine.MachineError, msg=str(port)):
                test_machine.validate_target("10.0.0.1", "u", port)

    def test_the_pinned_host_key_line_is_the_form_ssh_looks_up(self):
        # 2026-10-21: `[address]:22` is never matched by ssh (it looks up the bare address on the default port), so strict checking
        # refused every normal machine as "identity changed". Only a non-default port takes the bracketed form.
        self.assertEqual(test_machine.known_hosts_host("192.168.64.5", 22), "192.168.64.5")
        self.assertEqual(test_machine.known_hosts_host("192.168.64.5", "22"), "192.168.64.5")
        self.assertEqual(test_machine.known_hosts_host("192.168.64.5", 2222), "[192.168.64.5]:2222")
        with patch.object(test_machine, "_keyscan", return_value={"ssh-ed25519": "AAAAblob"}):
            self.assertTrue(test_machine.scan_host_key("10.0.0.9", 22)["line"].startswith("10.0.0.9 ssh-ed25519 "))
            self.assertTrue(test_machine.scan_host_key("10.0.0.9", 2222)["line"].startswith("[10.0.0.9]:2222 ssh-ed25519 "))

    def test_a_key_that_cannot_be_verified_after_the_scan_is_not_called_a_changed_identity(self):
        # After the scan compared the identity (and the page offered Confirm if it differed), a later "Host key verification
        # failed" means ssh could not verify the key just scanned; the owner is told to read it again, never to Confirm.
        refused = test_machine.PtyResult(255, "Host key verification failed.\r\n", False, False, False, False, False)
        with self.assertRaises(test_machine.MachineError) as later:
            test_machine.Connector._check(None, refused, "10.0.0.9", "u", 22, "password")
        self.assertEqual(later.exception.code, "host_key_unverified")
        self.assertIn("Press Connect again", str(later.exception))
        with self.assertRaises(test_machine.MachineError) as at_scan:
            test_machine.Connector._check(None, refused, "10.0.0.9", "u", 22, "scan")
        self.assertEqual(at_scan.exception.code, "host_key_changed")
        # the separate key-only verification takes the same rule
        fake = SimpleNamespace(paths=lambda: {"key": Path("key"), "known_hosts": Path("known_hosts")})
        with patch.object(test_machine, "run_key_only", return_value=(255, "Host key verification failed.")):
            with self.assertRaises(test_machine.MachineError) as at_verify:
                test_machine.Connector.verify(fake, "10.0.0.9", "u", 22, "windows")
        self.assertEqual(at_verify.exception.code, "host_key_unverified")
        # and the card's diagnosis says what to do instead of "The key check failed."
        saved = {"connected": True, "address": "10.0.0.9", "port": 22, "user": "u", "host_key": {"fingerprint": "SHA256:f"}}
        with patch.object(test_machine, "read", return_value=saved), \
                patch.object(test_machine, "scan_host_key", return_value={"fingerprint": "SHA256:f"}), \
                patch.object(test_machine, "verify_key_login_detail", return_value=(False, "host_key_changed", "Host key verification failed.")):
            diagnosis = test_machine.diagnose(None)
        self.assertEqual(diagnosis["cause"], "host_key_unverified")
        self.assertIn("Press Connect again", diagnosis["message"])


class DescriptorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.context = make_context(Path(self.tmp.name))

    def test_never_connected_is_not_usable_and_names_the_action(self):
        usable, code, message = test_machine.usability(self.context)
        self.assertFalse(usable)
        self.assertEqual(code, "not_connected")
        self.assertIn("Mission Control", message)

    def test_connected_and_authorized_by_a_live_manager_is_usable(self):
        connected_descriptor(self.context)
        self.assertEqual(test_machine.usability(self.context)[:2], (True, "ok"))

    def test_reauth_is_set_by_a_stop_once_and_epoch_only_moves_on_the_transition(self):
        connected_descriptor(self.context)
        before = test_machine.read(self.context)["auth_epoch"]
        self.assertTrue(test_machine.mark_reauth(self.context, "project_stopped"))
        self.assertFalse(test_machine.mark_reauth(self.context, "project_stopped"), "a repeated stop is not a new transition")
        after = test_machine.read(self.context)
        self.assertEqual(after["auth_epoch"], before + 1)
        self.assertTrue(after["reauth_required"])
        usable, code, message = test_machine.usability(self.context)
        self.assertEqual(code, "reauth_required")
        self.assertIn("password again", message)

    def test_a_dead_manager_voids_authorization_even_though_the_file_says_authorized(self):
        connected_descriptor(self.context, alive=False)
        self.assertEqual(test_machine.usability(self.context)[1], "manager_down")

    def test_a_new_manager_instance_never_inherits_authorization(self):
        connected_descriptor(self.context)
        other = test_machine.manager_identity()
        self.assertTrue(test_machine.reconcile_on_start(self.context, other))
        self.assertEqual(test_machine.usability(self.context)[1], "reauth_required")
        self.assertFalse(test_machine.reconcile_on_start(self.context, other), "already required: no loop")

    def test_stale_heartbeat_with_a_live_manager_still_stands(self):
        connected_descriptor(self.context)
        def age(value):
            value["authorized_by"]["heartbeat_at"] = "2020-01-01T00:00:00+00:00"
        test_machine.update(self.context, age)
        with patch.object(test_machine, "HEARTBEAT_WAIT_SECONDS", 0.2):
            self.assertTrue(test_machine.usability(self.context)[0])

    def test_damaged_and_unsupported_records_give_a_connect_card_not_a_crash(self):
        test_machine.machine_dir(self.context).mkdir(parents=True)
        test_machine.descriptor_path(self.context).write_text("{not json")
        self.assertEqual(test_machine.usability(self.context)[1], "damaged")
        test_machine.descriptor_path(self.context).write_text(json.dumps({"version": 99}))
        self.assertEqual(test_machine.usability(self.context)[1], "damaged")
        self.assertEqual(test_machine.public_status(self.context)["state"], "damaged")
        test_machine.request_card(self.context, "Delivery", "need it")      # replaces the damaged file
        self.assertEqual(test_machine.usability(self.context)[1], "not_connected")

    def test_machine_dependence_follows_the_command_form_and_local_commands_are_untouched(self):
        self.assertTrue(test_machine.machine_dependent(["TEST_MACHINE_GEN=2", "make", "test"]))
        self.assertFalse(test_machine.machine_dependent(["make", "test"]))
        self.assertIsNone(test_machine.stamp_for_command(self.context, ["make", "test"]))
        with self.assertRaises(test_machine.MachineError):
            test_machine.stamp_for_command(self.context, ["TEST_MACHINE_GEN=1", "make", "test"], who="delivery")
        self.assertEqual(test_machine.read(self.context)["request"]["by"], "delivery", "a refused machine command raises the card by itself")

    def test_stamp_changes_with_revision_epoch_and_tools(self):
        connected_descriptor(self.context)
        stamp = test_machine.require_usable(self.context)
        self.assertTrue(test_machine.stamp_is_current(self.context, stamp))
        test_machine.baseline_begin(self.context, "Delivery")
        self.assertFalse(test_machine.stamp_is_current(self.context, stamp), "begin makes earlier evidence stale")
        usable, code, _ = test_machine.usability(self.context)
        self.assertEqual(code, "baseline_incomplete")
        with self.assertRaises(test_machine.MachineError):
            test_machine.baseline_done(self.context, "Delivery", "short")
        fresh = test_machine.baseline_done(self.context, "Delivery", "rustc 1.80 and cargo 1.80 installed")
        self.assertTrue(test_machine.usability(self.context)[0])
        self.assertNotEqual(fresh, stamp)
        self.assertTrue(test_machine.read(self.context)["ready"]["state"])
        test_machine.mark_reauth(self.context, "project_stopped")
        self.assertFalse(test_machine.stamp_is_current(self.context, fresh))

    def test_recovery_provisioning_is_allowed_while_incomplete_but_not_while_unconnected(self):
        with self.assertRaises(test_machine.MachineError):
            test_machine.baseline_begin(self.context, "Delivery")
        connected_descriptor(self.context)
        test_machine.baseline_begin(self.context, "Delivery")
        test_machine.baseline_begin(self.context, "Delivery")   # a second begin (recovering a failed installer) is allowed
        self.assertEqual(test_machine.read(self.context)["tools_revision"], 2)

    def test_the_baseline_markers_still_work_while_the_password_is_required_again(self):
        connected_descriptor(self.context)
        test_machine.mark_reauth(self.context, "project_stopped")
        before = test_machine.stamp_of(test_machine.read(self.context))
        test_machine.baseline_begin(self.context, "Delivery")
        self.assertEqual(test_machine.read(self.context)["baseline_state"], "incomplete")
        test_machine.baseline_done(self.context, "Delivery", "python 3.12 and make installed on the machine")
        self.assertEqual(test_machine.read(self.context)["baseline_state"], "complete")
        self.assertEqual(test_machine.usability(self.context)[1], "reauth_required", "the password is still required for certification")
        self.assertNotEqual(test_machine.stamp_of(test_machine.read(self.context)), before)

    def test_a_released_machine_takes_no_baseline_markers_even_if_the_release_lands_between_check_and_write(self):
        connected_descriptor(self.context)
        real = test_machine.usability_for_provisioning

        def release_after_the_check(root):
            outcome = real(root)
            test_machine.release(root, Path(tempfile.mkdtemp()), "p")     # the Release lands right after the pre-check passed
            return outcome

        with patch.object(test_machine, "usability_for_provisioning", release_after_the_check):
            with self.assertRaises(test_machine.MachineError) as caught:
                test_machine.baseline_begin(self.context, "Delivery")
        self.assertEqual(caught.exception.code, "not_connected")
        self.assertEqual(test_machine.read(self.context)["tools_revision"], 0)

    def test_request_card_keeps_one_request_and_dedupes_waiters(self):
        test_machine.request_card(self.context, "Delivery", "run the Windows tests", task="T1")
        test_machine.request_card(self.context, "Delivery", "run the Windows tests", task="T1")
        test_machine.request_card(self.context, "Reviewer", "rerun the Windows tests", task="T1")
        value = test_machine.read(self.context)
        self.assertEqual(len(value["waiters"]), 2)
        self.assertEqual(value["request"]["by"], "Reviewer")

    def test_launch_note_is_empty_for_projects_that_never_used_a_machine(self):
        self.assertEqual(test_machine.launch_note(self.context), "")
        connected_descriptor(self.context)
        note = test_machine.launch_note(self.context)
        self.assertIn("machine-status", note)
        self.assertIn("revision 1", note)
        self.assertNotIn("paste", note.lower())

    def test_status_report_carries_the_rule_and_no_secret_fields(self):
        connected_descriptor(self.context)
        report = test_machine.status_report(self.context)
        self.assertTrue(report["usable"])
        self.assertIn("machine-baseline --begin", report["rule"])
        self.assertNotIn("password\":", json.dumps(report).replace("password again", ""))


class PasswordTransportTests(unittest.TestCase):
    """The pty answer: one prompt, right and wrong password, nothing in argv/environment/output."""

    def test_the_password_reaches_only_the_pty_and_never_argv_environment_or_output(self):
        with FakeMachine(self) as machine:
            result = test_machine.run_with_password(
                [str(machine.shim), "-F", "/dev/null", "-o", "BatchMode=no", "127.0.0.1", "echo hello"], PASSWORD, timeout=20)
            self.assertEqual(result.returncode, 0, result.text)
            self.assertTrue(result.prompted and result.answered)
            self.assertIn("hello", result.text)
            self.assertNotIn(PASSWORD, result.text)
            record = machine.shim_log()[-1]
            self.assertNotIn(PASSWORD, json.dumps(record["argv"]))
            self.assertNotIn(PASSWORD, json.dumps(record["env"]), "the child's environment never carries the password")
            self.assertEqual(sorted(record["env"]).count("HARNESS_BOARD_TOKEN"), 0)

    def test_a_wrong_password_fails_once_without_a_second_attempt(self):
        with FakeMachine(self) as machine:
            result = test_machine.run_with_password(
                [str(machine.shim), "-o", "BatchMode=no", "127.0.0.1", "echo hello"], "Pw-wrong-wrong-0000", timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(len(machine.shim_log()), 1)
            self.assertNotIn("Pw-wrong-wrong-0000", result.text)
            error = test_machine.classify(result.text, address="a", user="u", port=22, stage="password", prompted=True, answered=True)
            self.assertEqual(error.code, "bad_password")

    def test_an_extra_sign_in_step_is_named_not_answered(self):
        with FakeMachine(self) as machine:
            machine.set_mode("code-prompt")
            result = test_machine.run_with_password([str(machine.shim), "-o", "BatchMode=no", "127.0.0.1", "x"], PASSWORD, timeout=20)
            self.assertTrue(result.other_prompt)
            self.assertFalse(result.answered)
            self.assertEqual(test_machine.classify(result.text, address="a", user="u", port=22, stage="password",
                                                   other_prompt=True).code, "unsupported_auth")

    def test_no_password_login_offered_is_not_reported_as_a_bad_password(self):
        with FakeMachine(self) as machine:
            machine.set_mode("no-password-login")
            result = test_machine.run_with_password([str(machine.shim), "-o", "BatchMode=no", "127.0.0.1", "x"], PASSWORD, timeout=20)
            self.assertFalse(result.prompted)
            self.assertEqual(test_machine.classify(result.text, address="a", user="u", port=22, stage="password",
                                                   prompted=False, answered=False).code, "password_disabled")

    def test_cancel_stops_the_child_it_started(self):
        cancel = threading.Event()
        slow = Path(tempfile.mkdtemp(prefix="slow-")) / "ssh"
        slow.write_text("#!/bin/sh\nsleep 60\n")
        slow.chmod(0o755)
        self.addCleanup(lambda: slow.unlink())
        timer = threading.Timer(0.5, cancel.set)
        timer.start()
        started = time.monotonic()
        result = test_machine.run_with_password([str(slow)], PASSWORD, timeout=30, cancel=cancel)
        self.assertTrue(result.cancelled)
        self.assertLess(time.monotonic() - started, 10)


class FailureClassTests(unittest.TestCase):
    def codes(self, text, **kw):
        base = dict(address="10.0.0.9", user="u", port=22, stage="password")
        base.update(kw)
        return test_machine.classify(text, **base).code

    def test_each_observed_cause_has_its_own_class(self):
        self.assertEqual(self.codes("ssh: connect to host 10.0.0.9 port 22: Connection refused"), "ssh_server_down")
        self.assertEqual(self.codes("ssh: connect to host 10.0.0.9 port 22: No route to host"), "unreachable")
        self.assertEqual(self.codes("ssh: Could not resolve hostname vm: nodename nor servname provided"), "address_wrong")
        self.assertEqual(self.codes("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!"), "host_key_changed")
        self.assertEqual(self.codes("Host key verification failed."), "host_key_changed")
        self.assertEqual(self.codes("Permission denied (publickey).", stage="key"), "key_refused")
        self.assertEqual(self.codes("", timed_out=True), "unreachable")
        self.assertEqual(self.codes("Unable to negotiate with 10.0.0.9: no matching key exchange method"), "negotiation")

    def test_every_message_is_plain_and_names_the_repair(self):
        for text in ("Connection refused", "No route to host", "Host key verification failed", "Could not resolve hostname x"):
            message = str(test_machine.classify(text, address="10.0.0.9", user="u", port=22, stage="password"))
            self.assertNotIn("errno", message.lower())
            self.assertGreater(len(message), 40)

    def test_a_refused_port_and_an_unresolvable_name_are_told_apart_before_ssh_runs(self):
        from tests.machine_support import free_port
        with self.assertRaises(test_machine.MachineError) as refused:
            test_machine.tcp_probe("127.0.0.1", free_port())
        self.assertEqual(refused.exception.code, "ssh_server_down")
        with self.assertRaises(test_machine.MachineError) as unknown:
            test_machine.tcp_probe("no-such-host.invalid", 22)
        self.assertEqual(unknown.exception.code, "address_wrong")

    def test_the_normal_scan_is_one_connection_and_never_the_separate_probe(self):
        """A server that penalises connections ending before sign-in (OpenSSH 9.8+, per source) must not be hit three times per scan."""
        with FakeMachine(self) as machine:
            with patch.object(test_machine, "tcp_probe", side_effect=AssertionError("the probe is for a failed scan only")):
                found = test_machine.scan_host_key("127.0.0.1", machine.port)
            self.assertEqual(found["type"], "ssh-ed25519")
            kinds = []
            real = test_machine._keyscan
            with patch.object(test_machine, "_keyscan", side_effect=lambda a, p, k: (kinds.append(k), real(a, p, k))[1]):
                test_machine.scan_host_key("127.0.0.1", machine.port)
            self.assertEqual(kinds, ["ed25519"])

    def test_a_missing_ssh_program_is_a_local_problem_not_the_machines(self):
        with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": "/nonexistent/ssh"}):
            with self.assertRaises(test_machine.MachineError) as caught:
                test_machine.run_key_only([test_machine.ssh_program(), "x"])
        self.assertEqual(caught.exception.code, "local_ssh_missing")


class InstallScriptTests(unittest.TestCase):
    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBMkRK6A9KyM/DNmmDFeMLfKgDYJQaHzK5ZMyrxiLdTh nmhp-db2c4607"

    def test_windows_script_is_idempotent_and_sets_the_administrators_file_permissions(self):
        script = test_machine.windows_install_script(self.KEY)
        for needle in ("administrators_authorized_keys", "icacls.exe", "/inheritance:r", "Administrators:F", "SYSTEM:F",
                       "-notcontains $key", "NMHP_INSTALL_OK", ".ssh", "AppendAllText"):
            self.assertIn(needle, script)
        encoded = test_machine.powershell_command(script)
        blob = encoded.rsplit(" ", 1)[1]
        self.assertEqual(base64.b64decode(blob).decode("utf-16-le"), script)
        self.assertIn("-NonInteractive", encoded)

    def test_a_key_that_is_not_the_expected_shape_is_refused_before_it_reaches_any_command(self):
        for bad in ("ssh-ed25519 AAA'; rm -rf ~ #", "ssh-rsa AAAA", "ssh-ed25519 AAAA\nssh-ed25519 BBBB"):
            with self.assertRaises(test_machine.MachineError):
                test_machine.posix_install_command(bad)
            with self.assertRaises(test_machine.MachineError):
                test_machine.windows_install_script(bad)


class ConnectFlowTests(unittest.TestCase):
    """The whole Connect against a real sshd: scan, pin, key, password install, key-only proof."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.context = make_context(self.base)
        self.home = self.base / "mgr-home"
        self.identity = test_machine.manager_identity()
        self.challenges = test_machine.Challenges()

    def connect(self, machine, password=PASSWORD, project="p1", confirm=False):
        found = test_machine.scan_host_key("127.0.0.1", machine.port)
        current = test_machine.read(self.context) if test_machine.descriptor_path(self.context).exists() else {}
        same_target = (current or {}).get("address") == "127.0.0.1" and int((current or {}).get("port", 0)) == machine.port
        pinned = ((current or {}).get("host_key") or {}) if same_target else {}
        changed = bool(pinned.get("fingerprint")) and pinned["fingerprint"] != found["fingerprint"]
        challenge = self.challenges.consume(self.challenges.issue(project, "127.0.0.1", machine.user, machine.port, found, changed, not pinned)["id"], project)
        env = {"HARNESS_TEST_MACHINE_SSH": str(machine.shim)}
        with patch.dict(os.environ, env):
            return test_machine.Connector(self.context, self.home, project, self.identity).run(challenge, password, confirm)

    def test_first_connect_pins_installs_verifies_and_stores_no_password(self):
        with FakeMachine(self) as machine:
            descriptor = self.connect(machine)
            self.assertTrue(descriptor["connected"])
            self.assertFalse(descriptor["reauth_required"])
            self.assertEqual(descriptor["revision"], 1)
            self.assertEqual(descriptor["os"], "darwin" if os.uname().sysname == "Darwin" else "linux")
            self.assertTrue(descriptor["host_key"]["fingerprint"].startswith("SHA256:"))
            pub = (test_machine.machine_dir(self.context) / "key.pub").read_text().strip()
            self.assertEqual(machine.authorized_keys.read_text().splitlines().count(pub), 1)
            known = (test_machine.machine_dir(self.context) / "known_hosts").read_text()
            self.assertIn(f"[127.0.0.1]:{machine.port} ssh-ed25519", known)
            self.assertEqual({name: step["state"] for name, step in descriptor["steps"].items()},
                             {"scanned": "confirmed", "key_published": "confirmed", "verified": "confirmed"})
            self.assertEqual(test_machine.usability(self.context)[:2], (True, "ok"))
            # the key-only login the agents use works with the saved files, and nothing else
            ok, code = test_machine.verify_key_login(self.context)
            self.assertTrue(ok, code)

    def test_nothing_anywhere_contains_the_password_after_a_connect(self):
        with FakeMachine(self) as machine:
            descriptor = self.connect(machine)
            test_machine.request_card(self.context, "Delivery", "machine please")
            blobs = [json.dumps(test_machine.public_status(self.context)), json.dumps(test_machine.status_report(self.context)),
                     test_machine.launch_note(self.context), json.dumps(descriptor)]
            for blob in blobs:
                self.assertNotIn(PASSWORD, blob)
            for folder in (self.base, Path(tempfile.gettempdir())):
                for path in folder.rglob("*"):
                    if not path.is_file() or machine.secret_dir in path.parents or "shim.log" == path.name:
                        continue
                    try:
                        data = path.read_bytes()
                    except OSError:
                        continue
                    self.assertNotIn(PASSWORD.encode(), data, str(path))
            log = json.dumps(machine.shim_log())
            self.assertNotIn(PASSWORD, log)

    def test_reconnecting_is_idempotent_keeps_other_keys_and_bumps_revision_and_epoch(self):
        with FakeMachine(self) as machine:
            machine.authorized_keys.write_text("ssh-ed25519 AAAAOTHER owner-laptop")   # no trailing newline, an existing key
            first = self.connect(machine)
            second = self.connect(machine)
            lines = machine.authorized_keys.read_text().splitlines()
            self.assertEqual(lines[0], "ssh-ed25519 AAAAOTHER owner-laptop")
            self.assertEqual(len(lines), 2, "the project's key is added once, the existing key is kept")
            self.assertEqual((second["revision"], second["auth_epoch"]), (first["revision"] + 1, first["auth_epoch"] + 1))

    def test_wrong_password_installs_nothing_and_says_so(self):
        with FakeMachine(self) as machine:
            with self.assertRaises(test_machine.MachineError) as caught:
                self.connect(machine, password="Pw-wrong-wrong-0000")
            self.assertEqual(caught.exception.code, "bad_password")
            self.assertEqual(machine.authorized_keys.read_text(), "")
            value = test_machine.read(self.context)
            self.assertFalse(value["connected"])
            self.assertEqual(value["last_attempt"]["code"], "bad_password")
            self.assertNotIn("Pw-wrong-wrong-0000", json.dumps(value))

    def test_a_working_key_does_not_replace_the_password_proof(self):
        with FakeMachine(self) as machine:
            self.connect(machine)
            with self.assertRaises(test_machine.MachineError) as caught:
                self.connect(machine, password="Pw-wrong-wrong-0000")
            self.assertEqual(caught.exception.code, "bad_password")
            # the old key still logs in, but the descriptor is not re-authorized by it
            def stop(value):
                value["reauth_required"] = True
            test_machine.update(self.context, stop)
            self.assertTrue(test_machine.verify_key_login(self.context)[0])
            self.assertEqual(test_machine.usability(self.context)[1], "reauth_required")

    def test_interrupted_install_recovers_by_retrying_and_states_what_is_confirmed(self):
        with FakeMachine(self) as machine:
            # the key reaches the machine, but the key-only check cannot get through (host identity differs): ambiguous
            real_verify = test_machine.Connector.verify
            def fail_once(connector, *a, **k):
                raise test_machine.MachineError("The connection was lost before the key could be checked.", "unreachable")
            with patch.object(test_machine.Connector, "verify", fail_once):
                with self.assertRaises(test_machine.MachineError):
                    self.connect(machine)
            value = test_machine.read(self.context)
            self.assertFalse(value["connected"])
            self.assertEqual(value["steps"]["key_published"]["state"], "confirmed")
            self.assertNotIn("verified", value["steps"])
            count = machine.authorized_keys.read_text().count("ssh-ed25519")
            retry = self.connect(machine)
            self.assertTrue(retry["connected"])
            self.assertEqual(machine.authorized_keys.read_text().count("ssh-ed25519"), count, "the retry adds nothing")

    def test_a_changed_host_key_stops_before_any_password_is_sent(self):
        with FakeMachine(self) as machine:
            self.connect(machine)
            machine.new_host_key()
            prompts_before = len(machine.shim_log())
            with self.assertRaises(test_machine.MachineError) as caught:
                self.connect(machine)
            self.assertEqual(caught.exception.code, "host_key_changed")
            self.assertEqual(len(machine.shim_log()), prompts_before, "ssh was never run with the password")
            confirmed = self.connect(machine, confirm=True)
            self.assertTrue(confirmed["connected"])
            self.assertEqual(confirmed["host_key"]["fingerprint"], test_machine.scan_host_key("127.0.0.1", machine.port)["fingerprint"])

    def test_a_failed_confirmed_replacement_never_leaves_the_old_authorization_standing(self):
        with FakeMachine(self) as machine:
            self.connect(machine)
            self.assertTrue(test_machine.usability(self.context)[0])
            machine.new_host_key()
            with self.assertRaises(test_machine.MachineError):
                self.connect(machine, password="Pw-wrong-wrong-0000", confirm=True)
            self.assertEqual(test_machine.usability(self.context)[1], "reauth_required",
                             "the saved identity file was rewritten, so the old record must not stay authorized")

    def test_the_real_ssh_client_at_a_real_password_prompt_gets_one_answer_and_a_plain_wrong_password_reason(self):
        """Real ssh client, real sshd offering password sign-in (an unprivileged sshd can never accept one, so this is the
        failure path only; the success path is the stand-in's)."""
        with FakeMachine(self, offer_passwords=True) as machine:
            found = test_machine.scan_host_key("127.0.0.1", machine.port)
            challenge = self.challenges.consume(self.challenges.issue("p1", "127.0.0.1", machine.user, machine.port, found, False, True)["id"], "p1")
            with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": ""}):
                with self.assertRaises(test_machine.MachineError) as caught:
                    test_machine.Connector(self.context, self.home, "p1", self.identity).run(challenge, "Pw-wrong-wrong-0000", False)
            self.assertEqual(caught.exception.code, "bad_password", str(caught.exception))
            self.assertNotIn("Pw-wrong-wrong-0000", str(caught.exception))
            self.assertEqual(machine.authorized_keys.read_text(), "")

    def test_the_windows_route_sends_the_encoded_script_and_the_key_ends_up_installed(self):
        with FakeMachine(self) as machine:
            machine.set_mode("windows")
            descriptor = self.connect(machine)
            self.assertEqual(descriptor["os"], "windows")
            self.assertEqual(descriptor["account_kind"], "admin")
            script = (machine.base / "windows_script.ps1").read_text()
            pub = (test_machine.machine_dir(self.context) / "key.pub").read_text().strip()
            self.assertIn(pub, script)
            self.assertEqual(machine.authorized_keys.read_text().splitlines().count(pub), 1)
            self.assertNotIn(PASSWORD, script)

    def test_a_machine_connected_to_another_project_is_refused(self):
        with FakeMachine(self) as machine:
            self.connect(machine, project="p1")
            other_context = make_context(self.base / "second")
            other = test_machine.Connector(other_context, self.home, "p2", self.identity)
            found = test_machine.scan_host_key("127.0.0.1", machine.port)
            challenge = self.challenges.consume(self.challenges.issue("p2", "127.0.0.1", machine.user, machine.port, found, False, True)["id"], "p2")
            with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": str(machine.shim)}):
                with self.assertRaises(test_machine.MachineError) as caught:
                    other.run(challenge, PASSWORD, False)
            self.assertEqual(caught.exception.code, "machine_in_use")
            # an alias (the loopback name) of the same machine is the same machine
            found_alias = dict(found)
            challenge = self.challenges.consume(self.challenges.issue("p2", "localhost", machine.user, machine.port, found_alias, False, True)["id"], "p2")
            with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": str(machine.shim)}):
                with self.assertRaises(test_machine.MachineError) as caught:
                    other.run(challenge, PASSWORD, False)
            self.assertEqual(caught.exception.code, "machine_in_use")
            test_machine.release_target(self.home, "p1")
            self.assertFalse(test_machine.release_target(self.home, "p1"))

    def test_a_failed_attempt_at_another_machine_never_edits_the_working_record_or_its_reservation(self):
        with FakeMachine(self) as machine, FakeMachine(self) as other:
            self.connect(machine)
            before = test_machine.read(self.context)
            with self.assertRaises(test_machine.MachineError):
                self.connect(other, password="Pw-wrong-wrong-0000")
            after = test_machine.read(self.context)
            for field in ("connected", "address", "port", "user", "host_key", "revision", "auth_epoch", "reauth_required"):
                self.assertEqual(after[field], before[field], field)
            self.assertEqual(after["attempt"]["port"], other.port)
            self.assertEqual(after["steps"].get("verified"), None, "the failed attempt does not show the earlier success's steps")
            self.assertTrue(test_machine.usability(self.context)[0])
            self.assertEqual(test_machine.public_status(self.context)["port"], machine.port)
            # the first machine is still reserved for this project: another project cannot take it
            second = make_context(self.base / "third")
            found = test_machine.scan_host_key("127.0.0.1", machine.port)
            challenge = self.challenges.consume(self.challenges.issue("p9", "127.0.0.1", machine.user, machine.port, found, False, True)["id"], "p9")
            with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": str(machine.shim)}):
                with self.assertRaises(test_machine.MachineError) as caught:
                    test_machine.Connector(second, self.home, "p9", self.identity).run(challenge, PASSWORD, False)
            self.assertEqual(caught.exception.code, "machine_in_use")

    def test_a_failed_attempt_leaves_the_published_pin_and_key_login_of_the_working_machine_untouched(self):
        """Review round 1, finding 2: a failed Connect to B used to overwrite A's known_hosts."""
        with FakeMachine(self) as first, FakeMachine(self) as second:
            self.connect(first)
            known = test_machine.machine_dir(self.context) / "known_hosts"
            published = known.read_bytes()
            with self.assertRaises(test_machine.MachineError):
                self.connect(second, password="Pw-wrong-wrong-0000")
            self.assertEqual(known.read_bytes(), published, "the published pin is still A's")
            self.assertFalse((test_machine.machine_dir(self.context) / "known_hosts.attempt").exists(), "the staged pin is removed")
            ok, code = test_machine.verify_key_login(self.context)
            self.assertTrue(ok, f"a real key-only login to A still works after the failed attempt at B ({code})")
            self.assertEqual(test_machine.public_status(self.context)["state"], "connected")
            # and a later success at B publishes B's pin together with its record
            self.connect(second)
            self.assertIn(f"[127.0.0.1]:{second.port} ", known.read_text())
            self.assertTrue(test_machine.verify_key_login(self.context)[0])

    def test_a_first_attempt_that_fails_does_not_keep_the_machine_reserved(self):
        with FakeMachine(self) as machine:
            with self.assertRaises(test_machine.MachineError):
                self.connect(machine, password="Pw-wrong-wrong-0000")
            other = make_context(self.base / "fourth")
            found = test_machine.scan_host_key("127.0.0.1", machine.port)
            challenge = self.challenges.consume(self.challenges.issue("p5", "127.0.0.1", machine.user, machine.port, found, False, True)["id"], "p5")
            with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": str(machine.shim)}):
                connected = test_machine.Connector(other, self.home, "p5", self.identity).run(challenge, PASSWORD, False)
            self.assertTrue(connected["connected"])

    def test_a_challenge_is_single_use_project_bound_and_expires(self):
        found = {"type": "ssh-ed25519", "blob": "x", "fingerprint": "SHA256:x", "line": "l"}
        item = self.challenges.issue("p1", "10.0.0.1", "u", 22, found, False, True)
        with self.assertRaises(test_machine.MachineError):
            self.challenges.consume(item["id"], "other-project")
        item = self.challenges.issue("p1", "10.0.0.1", "u", 22, found, False, True)
        self.assertEqual(self.challenges.consume(item["id"], "p1")["address"], "10.0.0.1")
        with self.assertRaises(test_machine.MachineError):
            self.challenges.consume(item["id"], "p1")
        item = self.challenges.issue("p1", "10.0.0.1", "u", 22, found, False, True)
        with patch.object(test_machine, "CHALLENGE_SECONDS", -1):
            expired = self.challenges.issue("p1", "10.0.0.1", "u", 22, found, False, True)
        with self.assertRaises(test_machine.MachineError):
            self.challenges.consume(expired["id"], "p1")

    def test_diagnosis_names_the_observed_cause(self):
        with FakeMachine(self) as machine:
            self.connect(machine)
            self.assertEqual(test_machine.diagnose(self.context)["cause"], "key_ok")
            machine.authorized_keys.write_text("")           # the machine forgot the key (rebuilt, same host key)
            result = test_machine.diagnose(self.context)
            self.assertEqual(result["cause"], "key_refused")
            self.assertEqual(test_machine.usability(self.context)[1], "reauth_required", "a key failure requires the password again")
            machine.stop()
            self.assertEqual(test_machine.diagnose(self.context)["cause"], "ssh_server_down")


class ManagerTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        code = self.base / "alpha"
        code.mkdir()
        self.entry = registry.register(self.home, "alpha", code)
        registry.activate(self.home, self.entry["id"])   # only the open project's machine is read in the list (owner 2026-10-28)
        self.context = registry.context_for_entry(self.entry)

    @contextmanager
    def served(self, machine=None):
        env = {"HARNESS_TEST_MACHINE_SSH": str(machine.shim)} if machine else {}
        with patch.dict(os.environ, env):
            manager = project_manager.ProjectManager(self.home, board_port=0)
            server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{server.server_address[1]}", manager
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()

    def post(self, base, path, body=None):
        request = Request(base + path, data=json.dumps(body or {}).encode(), method="POST", headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=90) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def api(self, base, name):
        return f"/api/projects/{self.entry['id']}/test-machine/{name}"

    def do_connect(self, base, machine, password=PASSWORD):
        status, scan = self.post(base, self.api(base, "scan"), {"address": "127.0.0.1", "user": machine.user, "port": machine.port})
        self.assertEqual(status, 200, scan)
        return self.post(base, self.api(base, "connect"), {"challenge": scan["challenge"], "password": password}), scan

    def test_connect_through_the_http_surface_and_the_card_state(self):
        with FakeMachine(self) as machine, self.served(machine) as (base, manager):
            (status, result), scan = self.do_connect(base, machine)
            self.assertEqual(status, 200, result)
            self.assertTrue(scan["first_use"])
            self.assertIn("First connection", scan["note"])
            self.assertEqual(result["state"], "connected")
            self.assertEqual(result["user"], machine.user)
            self.assertTrue(result["fingerprint"].startswith("SHA256:"))
            rows = manager.projects_payload()["projects"]
            self.assertEqual(rows[0]["test_machine"]["state"], "connected")
            self.assertNotIn(PASSWORD, json.dumps(rows))
            self.assertNotIn(PASSWORD, json.dumps(result))

    def test_a_mistyped_password_does_not_revoke_a_working_authorization(self):
        with FakeMachine(self) as machine, self.served(machine) as (base, _):
            self.do_connect(base, machine)
            self.assertTrue(test_machine.usability(self.context)[0])
            epoch = test_machine.read(self.context)["auth_epoch"]
            (status, result), _scan = self.do_connect(base, machine, "Pw-wrong-wrong-0000")
            self.assertEqual(status, 400)
            self.assertTrue(test_machine.usability(self.context)[0], "a mistyped password proves nothing about the key")
            self.assertEqual(test_machine.read(self.context)["auth_epoch"], epoch)

    def test_a_wrong_password_returns_one_plain_reason_and_no_secret(self):
        with FakeMachine(self) as machine, self.served(machine) as (base, _):
            (status, result), _scan = self.do_connect(base, machine, "Pw-wrong-wrong-0000")
            self.assertEqual(status, 400)
            self.assertIn("password was not accepted", result["error"])
            self.assertNotIn("Pw-wrong-wrong-0000", json.dumps(result))

    def test_the_password_is_refused_from_a_non_loopback_peer_before_anything_runs(self):
        with FakeMachine(self) as machine, self.served(machine) as (base, manager):
            with self.assertRaises(test_machine.MachineError) as caught:
                manager.machine_connect(self.entry["id"], {"challenge": "x", "password": PASSWORD}, loopback=False)
            self.assertEqual(caught.exception.code, "not_loopback")
            self.assertEqual(machine.shim_log(), [])

    def test_a_challenge_cannot_be_replayed_or_used_without_a_scan(self):
        with FakeMachine(self) as machine, self.served(machine) as (base, _):
            status, result = self.post(base, self.api(base, "connect"), {"challenge": "made-up", "password": PASSWORD})
            self.assertEqual(status, 400)
            self.assertIn("expired", result["error"])
            self.assertEqual(machine.shim_log(), [])

    def test_pause_close_resume_and_restart_each_require_the_password_again(self):
        with FakeMachine(self) as machine, self.served(machine) as (base, manager):
            self.do_connect(base, machine)
            self.assertTrue(test_machine.usability(self.context)[0])
            epoch = test_machine.read(self.context)["auth_epoch"]
            manager._machine_stop(self.entry["id"], "project_stopped")
            self.assertEqual(test_machine.usability(self.context)[1], "reauth_required")
            manager._machine_stop(self.entry["id"], "project_stopped")      # a repeated stop is not a new transition
            self.assertEqual(test_machine.read(self.context)["auth_epoch"], epoch + 1)
            self.do_connect(base, machine)
            self.assertTrue(test_machine.usability(self.context)[0])
            manager.close_project(self.entry["id"])
            self.assertEqual(test_machine.usability(self.context)[1], "reauth_required")
            self.do_connect(base, machine)
            manager.shutdown()
            self.assertEqual(test_machine.usability(self.context)[1], "reauth_required")
        # a brand-new manager (a restart) never inherits an authorization the old one held
        with FakeMachine(self) as machine2, self.served(machine2) as (base, manager2):
            connected_descriptor(self.context, alive=True)      # as if the previous process had left it authorized
            fresh = project_manager.ProjectManager(self.home, board_port=0)
            fresh._machine_summary(self.entry)      # the first time the new manager touches the project (the Projects page lists it)
            self.assertEqual(test_machine.usability(self.context)[1], "reauth_required")
            self.assertNotEqual(fresh.machine_identity["instance_id"], manager2.machine_identity["instance_id"])

    def test_the_stop_cancels_a_connect_that_is_running_and_drops_the_password(self):
        with FakeMachine(self) as machine, self.served(machine) as (base, manager):
            machine_slow = machine.base / "slow"
            (machine.base / "shim.mode").write_text("posix")
            status, scan = self.post(base, self.api(base, "scan"), {"address": "127.0.0.1", "user": machine.user, "port": machine.port})
            outcome = {}

            def run():
                try:
                    outcome["value"] = manager.machine_connect(self.entry["id"], {"challenge": scan["challenge"], "password": PASSWORD}, loopback=True)
                except test_machine.MachineError as error:
                    outcome["error"] = error
            real = test_machine.run_with_password
            def slow(argv, password, **kw):
                time.sleep(1.5)
                return real(argv, password, **kw)
            with patch.object(test_machine, "run_with_password", slow):
                thread = threading.Thread(target=run)
                thread.start()
                time.sleep(0.5)
                manager._machine_stop(self.entry["id"], "project_stopped")
                thread.join(timeout=60)
            self.assertIn("error", outcome, outcome)
            self.assertEqual(outcome["error"].code, "cancelled")
            self.assertEqual(machine.authorized_keys.read_text(), "", "nothing was installed after the stop")

    def connect_with_a_stop_during_key_verification_to(self, machine, base, manager, stop):
        with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": str(machine.shim)}):
            return self.connect_with_a_stop_during_key_verification(machine, base, manager, stop)

    def connect_with_a_stop_during_key_verification(self, machine, base, manager, stop):
        """Start a Connect, hold it at the key-only proof, run `stop`, release it; returns (outcome, usable-after)."""
        _s, scan = self.post(base, self.api(base, "scan"), {"address": "127.0.0.1", "user": machine.user, "port": machine.port})
        reached, proceed, outcome = threading.Event(), threading.Event(), {}
        original = test_machine.Connector.verify

        def held_verify(connector, *args, **kwargs):
            reached.set()
            if not proceed.wait(30):
                raise RuntimeError("test synchronisation timeout")
            return original(connector, *args, **kwargs)

        def run():
            try:
                outcome["value"] = manager.machine_connect(self.entry["id"], {"challenge": scan["challenge"], "password": PASSWORD}, loopback=True)
            except test_machine.MachineError as error:
                outcome["error"] = error

        with patch.object(test_machine.Connector, "verify", held_verify):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(reached.wait(30), "the Connect reached the key-only proof")
            stop()
            proceed.set()
            thread.join(60)
        self.assertFalse(thread.is_alive())
        return outcome

    def test_close_during_the_key_proof_is_not_undone_by_the_connect_in_flight(self):
        """Review round 1, finding 1: Close sets reauth_required, then the finishing Connect used to clear it."""
        with FakeMachine(self) as machine, self.served(machine) as (base, manager):
            self.do_connect(base, machine)                      # authorized, so the stop is a real transition
            before = test_machine.read(self.context)
            outcome = self.connect_with_a_stop_during_key_verification(machine, base, manager, lambda: manager.close_project(self.entry["id"]))
            self.assertEqual(outcome["error"].code, "cancelled", outcome)
            usable, code, _ = test_machine.usability(self.context)
            self.assertEqual((usable, code), (False, "reauth_required"), "Close stands: the password is required again")
            after = test_machine.read(self.context)
            self.assertEqual(after["revision"], before["revision"], "the cancelled attempt published nothing")
            self.assertFalse(test_machine.public_status(self.context)["usable"])

    def test_a_stop_when_the_password_was_already_required_still_cancels_the_connect_in_flight(self):
        """The usual case: no epoch moves on the second stop, so only the stop counter can tell the Connect to give up."""
        with FakeMachine(self) as machine, self.served(machine) as (base, manager):
            self.do_connect(base, machine)
            manager._machine_stop(self.entry["id"], "project_stopped")      # the password is required already
            epoch = test_machine.read(self.context)["auth_epoch"]
            outcome = self.connect_with_a_stop_during_key_verification(machine, base, manager, lambda: manager._machine_stop(self.entry["id"], "project_stopped"))
            self.assertEqual(outcome["error"].code, "cancelled", outcome)
            self.assertEqual(test_machine.usability(self.context)[1], "reauth_required")
            self.assertEqual(test_machine.read(self.context)["auth_epoch"], epoch, "no second transition")

    def test_release_during_the_key_proof_wins_over_the_connect_in_flight(self):
        with FakeMachine(self) as machine, self.served(machine) as (base, manager):
            outcome = self.connect_with_a_stop_during_key_verification(machine, base, manager, lambda: manager.machine_release(self.entry["id"]))
            self.assertEqual(outcome["error"].code, "cancelled", outcome)
            self.assertEqual(test_machine.usability(self.context)[1], "not_connected")
            self.assertFalse((test_machine.machine_dir(self.context) / "known_hosts").exists(), "nothing was published")

    def test_release_during_a_replacement_leaves_both_machines_free_for_other_projects(self):
        """Review round 2: the cancelled replacement used to put the released machine's reservation back."""
        with FakeMachine(self) as first, FakeMachine(self) as second:
            with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": str(first.shim)}):
                with self.served(first) as (base, manager):
                    self.do_connect(base, first)
            with self.served(second) as (base, manager):
                outcome = self.connect_with_a_stop_during_key_verification_to(second, base, manager, lambda: manager.machine_release(self.entry["id"]))
                self.assertEqual(outcome["error"].code, "cancelled", outcome)
                self.assertEqual(test_machine.usability(self.context)[1], "not_connected")
                self.assertEqual(test_machine._load_targets(manager.home), {}, "no reservation is left for this project")
                # a REAL Connect of another project to the released machine now succeeds
                other_code = self.base / "another"
                other_code.mkdir()
                other = registry.register(self.home, "another", other_code)
                with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": str(first.shim)}):
                    scan = manager.machine_scan(other["id"], {"address": "127.0.0.1", "user": first.user, "port": first.port})
                    result = manager.machine_connect(other["id"], {"challenge": scan["challenge"], "password": PASSWORD}, loopback=True)
                self.assertTrue(result["usable"])
                # and the machine the cancelled replacement was heading for is free too
                scan = manager.machine_scan(self.entry["id"], {"address": "127.0.0.1", "user": second.user, "port": second.port})
                with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": str(second.shim)}):
                    self.assertTrue(manager.machine_connect(self.entry["id"], {"challenge": scan["challenge"], "password": PASSWORD}, loopback=True)["usable"])

    def test_a_failed_replacement_without_a_release_keeps_the_working_machine_reserved(self):
        with FakeMachine(self) as first, FakeMachine(self) as second:
            with self.served(first) as (base, manager):
                self.do_connect(base, first)
                with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": str(second.shim)}):
                    scan = manager.machine_scan(self.entry["id"], {"address": "127.0.0.1", "user": second.user, "port": second.port})
                    with self.assertRaises(test_machine.MachineError):
                        manager.machine_connect(self.entry["id"], {"challenge": scan["challenge"], "password": "Pw-wrong-wrong-0000"}, loopback=True)
                entries = test_machine._load_targets(manager.home)[self.entry["id"]]
                self.assertEqual([(e["kind"], e["port"]) for e in entries], [("connected", first.port)], "A stays reserved, the pending B is dropped")

    def other_project(self, name="another"):
        code = self.base / name
        code.mkdir()
        return registry.register(self.home, name, code)

    def connect_other(self, manager, project, machine):
        scan = manager.machine_scan(project["id"], {"address": "127.0.0.1", "user": machine.user, "port": machine.port})
        with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": str(machine.shim)}):
            return manager.machine_connect(project["id"], {"challenge": scan["challenge"], "password": PASSWORD}, loopback=True)

    def replacement_with_a_competitor(self, manager, first, second, competitor_target, wrong_password=True):
        """Project 1 is connected to `first` and attempts `second`; WHILE it does, another project Connects to `competitor_target`."""
        rival = self.other_project()
        seen = []
        real = test_machine.run_with_password

        def during(argv, password, **kwargs):
            if not seen:
                try:
                    seen.append(self.connect_other(manager, rival, competitor_target)["usable"])
                except test_machine.MachineError as error:
                    seen.append(error.code)
            return real(argv, password, **kwargs)

        scan = manager.machine_scan(self.entry["id"], {"address": "127.0.0.1", "user": second.user, "port": second.port})
        with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": str(second.shim)}), patch.object(test_machine, "run_with_password", during):
            body = {"challenge": scan["challenge"], "password": "Pw-wrong-wrong-0000" if wrong_password else PASSWORD}
            try:
                outcome = manager.machine_connect(self.entry["id"], body, loopback=True)
            except test_machine.MachineError as error:
                outcome = error
        return rival, seen, outcome

    def test_a_working_machine_stays_exclusive_while_its_project_attempts_a_replacement(self):
        """Review round 3: reserving B used to drop A's reservation, so another project could take A meanwhile."""
        with FakeMachine(self) as first, FakeMachine(self) as second:
            with self.served(first) as (base, manager):
                self.do_connect(base, first)
                rival, seen, outcome = self.replacement_with_a_competitor(manager, first, second, first)
                self.assertEqual(seen, ["machine_in_use"], "A is still reserved for its project during the replacement")
                self.assertIsInstance(outcome, test_machine.MachineError)
                self.assertTrue(test_machine.public_status(self.context)["usable"], "the first project still has its machine")
                self.assertFalse(test_machine.public_status(registry.context_for_entry(rival))["usable"], "the second project never got it")
                self.assertTrue(test_machine.verify_key_login(self.context)[0])
                entries = test_machine._load_targets(manager.home)
                self.assertEqual([(e["kind"], e["port"]) for e in entries[self.entry["id"]]], [("connected", first.port)])
                self.assertNotIn(rival["id"], entries)

    def test_the_replacement_target_is_exclusive_while_it_is_pending(self):
        with FakeMachine(self) as first, FakeMachine(self) as second:
            with self.served(first) as (base, manager):
                self.do_connect(base, first)
                rival, seen, _outcome = self.replacement_with_a_competitor(manager, first, second, second)
                self.assertEqual(seen, ["machine_in_use"], "B is taken by the pending attempt")
                # once the attempt has failed, B is free for the rival
                self.assertTrue(self.connect_other(manager, rival, second)["usable"])

    def test_a_successful_replacement_frees_the_old_machine_and_holds_only_the_new_one(self):
        with FakeMachine(self) as first, FakeMachine(self) as second:
            with self.served(first) as (base, manager):
                self.do_connect(base, first)
                rival, seen, outcome = self.replacement_with_a_competitor(manager, first, second, first, wrong_password=False)
                self.assertEqual(seen, ["machine_in_use"], "A was still the project's machine while B was pending")
                self.assertEqual(outcome["state"], "connected")
                entries = test_machine._load_targets(manager.home)[self.entry["id"]]
                self.assertEqual([(e["kind"], e["port"]) for e in entries], [("connected", second.port)])
                self.assertTrue(self.connect_other(manager, rival, first)["usable"], "A is free now")

    def test_a_pending_reservation_left_by_an_app_that_died_mid_connect_is_dropped_at_the_next_start(self):
        with FakeMachine(self) as first, FakeMachine(self) as second:
            with self.served(first) as (base, manager):
                self.do_connect(base, first)
                test_machine.reserve_target(manager.home, self.entry["id"], "127.0.0.1", second.port, "pending")   # the app died here
                self.assertEqual(len(test_machine._load_targets(manager.home)[self.entry["id"]]), 2)
            with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SSH": str(first.shim)}):
                started = project_manager.ProjectManager(self.home, board_port=0)      # the next start
                started._machine_reconcile_once(self.entry)      # the first time it touches the project, never for every project at start
            entries = test_machine._load_targets(self.home)[self.entry["id"]]
            self.assertEqual([(e["kind"], e["port"]) for e in entries], [("connected", first.port)])

    def test_an_older_single_entry_reservation_file_still_blocks_other_projects(self):
        with FakeMachine(self) as machine, self.served(machine) as (base, manager):
            test_machine._targets_path(manager.home).write_text(json.dumps({"someone-else": {"aliases": ["127.0.0.1"], "port": machine.port, "at": "x"}}))
            with self.assertRaises(test_machine.MachineError) as caught:
                manager.machine_scan(self.entry["id"], {"address": "127.0.0.1", "user": machine.user, "port": machine.port})
                self.connect_other(manager, self.other_project("third"), machine)
            self.assertEqual(caught.exception.code, "machine_in_use")

    def test_a_machine_held_for_a_project_that_is_not_connected_can_be_released_from_its_status(self):
        with self.served() as (base, manager):
            test_machine.reserve_target(manager.home, self.entry["id"], "10.9.9.9", 22)     # e.g. left by a Connect the app died in
            status = manager.machine_status(self.entry["id"])
            self.assertTrue(status["reserved"] and not status["connected"])
            result = manager.machine_release(self.entry["id"])
            self.assertTrue(result["released"])
            self.assertFalse(manager.machine_status(self.entry["id"])["reserved"])

    def test_two_connects_at_once_run_one_at_a_time(self):
        with FakeMachine(self) as machine, self.served(machine) as (base, manager):
            _s, scan1 = self.post(base, self.api(base, "scan"), {"address": "127.0.0.1", "user": machine.user, "port": machine.port})
            real = test_machine.run_with_password
            gate = threading.Event()
            def slow(argv, password, **kw):
                gate.wait(10)
                return real(argv, password, **kw)
            results = {}
            def first():
                results["a"] = manager.machine_connect(self.entry["id"], {"challenge": scan1["challenge"], "password": PASSWORD}, loopback=True)
            with patch.object(test_machine, "run_with_password", slow):
                thread = threading.Thread(target=first)
                thread.start()
                time.sleep(0.5)
                _s, scan2 = self.post(base, self.api(base, "scan"), {"address": "127.0.0.1", "user": machine.user, "port": machine.port})
                with self.assertRaises(test_machine.MachineError) as caught:
                    manager.machine_connect(self.entry["id"], {"challenge": scan2["challenge"], "password": PASSWORD}, loopback=True)
                self.assertIn(caught.exception.code, {"busy", "challenge_expired"})
                gate.set()
                thread.join(timeout=60)
            self.assertEqual(results["a"]["state"], "connected")

    def test_release_frees_the_machine_and_says_the_key_stays(self):
        with FakeMachine(self) as machine, self.served(machine) as (base, _):
            self.do_connect(base, machine)
            status, result = self.post(base, self.api(base, "release"))
            self.assertEqual(status, 200)
            self.assertIn("stays on that machine", result["note"])
            self.assertEqual(test_machine.usability(self.context)[1], "not_connected")

    def test_launch_scripts_and_page_carry_the_new_text_and_no_paste_rule(self):
        page = project_manager.PAGE
        for needle in ("machine-dialog", "Connect", "never saved, logged or shown to an agent", "only accepted on the computer that runs Mission Control"):
            self.assertIn(needle, page)
        root = Path(__file__).resolve().parents[1]
        agent = (root / "directives" / "AGENT.md").read_text()
        for text in (agent, (root / "README.md").read_text(), page):
            lowered = " ".join(text.lower().split())
            self.assertNotIn("setup block", lowered)
            self.assertNotIn("one block to paste", lowered)
            self.assertNotIn("add to the machine's authorized keys", lowered)
            self.assertNotIn("install dependencies on the vm beforehand", lowered)
        for command in ("need-test-machine", "machine-status", "machine-baseline", "screen-check --machine"):
            self.assertIn(command, agent)


if __name__ == "__main__":
    unittest.main()

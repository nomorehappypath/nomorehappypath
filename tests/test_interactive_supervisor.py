# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import os
import pty
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, contract, control, cto, interactive_supervisor


ROOT = Path(__file__).resolve().parents[1]


class InteractiveSupervisorTests(unittest.TestCase):
    def test_managed_terminal_cleanup_targets_only_its_exact_tty_once(self):
        with patch.object(interactive_supervisor.sys, "platform", "darwin"), \
                patch.object(interactive_supervisor.os, "ttyname", return_value="/dev/ttys123"), \
                patch.object(interactive_supervisor.subprocess, "Popen") as launch:
            interactive_supervisor._schedule_terminal_close(0)

        arguments = launch.call_args.args[0]
        self.assertEqual(arguments[:2], ["/usr/bin/osascript", "-e"])
        self.assertEqual(arguments[-1], "/dev/ttys123")
        self.assertIn("if tty of terminalTab is targetTTY", arguments[2])
        self.assertIn("close terminalWindow", arguments[2])
        self.assertNotIn("name of terminalWindow", arguments[2])
        self.assertTrue(launch.call_args.kwargs["start_new_session"])

    def test_owner_input_is_recorded_and_controller_retry_is_visible_in_same_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            master, slave = pty.openpty()
            child = (
                "import os,tty\n"
                "tty.setraw(0)\n"
                "os.write(1,b'CHILD_READY')\n"
                "owner=b''\n"
                "while b'\\n' not in owner and b'\\r' not in owner: owner += os.read(0,4096)\n"
                "os.write(1,b'OWNER:'+owner.rstrip(b'\\r\\n')+b'\\n')\n"
                "retry=b''\n"
                "while b'\\r' not in retry: retry += os.read(0,4096)\n"
                "os.write(1,b'RETRY:'+retry+b'\\n')\n"
            )
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "python3", "-c", child,
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                startup = self._read_until(master, b"interactive supervisor ready", timeout=3)
                os.write(master, b"Build a real retry controller\n")
                self._wait_for(lambda: board.snapshot(root).get("owner_directions", {}).get(session["id"], {}).get("text") == "Build a real retry controller")
                # The exact terminal input unlocks one authorized task, then
                # the controller can route a later failed-review instruction.
                board.begin_task(root, agent["id"], "OWNER-TASK")
                control.enqueue_instruction(root, session["id"], "Resume: review cycle failed; fix and requeue.", "independent-review")
                output = startup + self._read_until(master, b"RETRY:", timeout=4)
                self.assertIn(b"OWNER:Build a real retry controller", output)
                self.assertIn(b"RETRY:\x1b[200~[SYSTEM CONTROL", output)
                process.wait(timeout=8)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def test_controller_instruction_submits_with_the_real_terminal_enter_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "claude_cto")
            agent = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
            master, slave = pty.openpty()
            child = (
                "import os,tty\n"
                "tty.setraw(0)\n"
                "os.write(1,b'CHILD_READY')\n"
                "data=b''\n"
                "while b'\\r' not in data:\n"
                " data += os.read(0, 4096)\n"
                "os.write(1, b'INSTRUCTION_SUBMITTED:' + data.replace(b'\\r', b'<ENTER>'))\n"
            )
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "python3", "-c", child,
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"interactive supervisor ready", timeout=3)
                queued = control.enqueue_instruction(root, session["id"], "Run the routed CTO action now.", "test-controller")
                output = self._read_until(master, b"INSTRUCTION_SUBMITTED", timeout=3)
                self.assertIn(b"SYSTEM CONTROL", output)
                self.assertIn(b"<ENTER>", output)
                self._wait_for(
                    lambda: control.instruction_receipt(root, queued["id"])["status"] == "delivered"
                )
                process.wait(timeout=8)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def test_controller_enter_is_not_coalesced_into_pasted_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            master, slave = pty.openpty()
            child = (
                "import os,select,time,tty\n"
                "time.sleep(.3)\n"
                "tty.setraw(0)\n"
                "os.write(1,b'CHILD_READY')\n"
                "first=os.read(0,4096)\n"
                "ready,_,_=select.select([0],[],[],1)\n"
                "second=os.read(0,4096) if ready else b''\n"
                "verdict=b'SEPARATE_ENTER' if b'\\r' not in first and b'\\r' in second else b'PASTE_NOT_SUBMITTED'\n"
                "os.write(1,verdict+b'|FIRST='+first+b'|SECOND='+second.replace(b'\\r',b'<ENTER>'))\n"
            )
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "python3", "-c", child,
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"interactive supervisor ready", timeout=3)
                control.enqueue_instruction(root, session["id"], "Resume this routed task without owner input.", "paste-sensitive-test")
                output = self._read_until(master, b"SEPARATE_ENTER", timeout=3)
                self.assertIn(b"SYSTEM CONTROL", output)
                self.assertIn(b"<ENTER>", output)
                self.assertNotIn(b"PASTE_NOT_SUBMITTED", output)
                process.wait(timeout=8)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def test_multiline_controller_message_is_one_bracketed_paste_then_one_enter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            master, slave = pty.openpty()
            child = (
                "import os,select,time,tty\n"
                "time.sleep(.3)\n"
                "tty.setraw(0)\n"
                "os.write(1,b'CHILD_READY')\n"
                "first=os.read(0,4096)\n"
                "ready,_,_=select.select([0],[],[],1)\n"
                "second=os.read(0,4096) if ready else b''\n"
                "ok=(b'\\x1b[200~' in first and b'\\x1b[201~' in first and b'First\\n\\nSecond' in first and b'\\r' not in first and b'\\r' in second)\n"
                "os.write(1,(b'MULTILINE_PASTE_OK' if ok else b'MULTILINE_PASTE_BAD')+b'|FIRST='+first+b'|SECOND='+second.replace(b'\\r',b'<ENTER>'))\n"
            )
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "python3", "-c", child,
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"interactive supervisor ready", timeout=3)
                control.enqueue_instruction(root, session["id"], "First\n\nSecond", "multiline-owner-message")
                output = self._read_until(master, b"MULTILINE_PASTE_", timeout=3)
                self.assertIn(b"MULTILINE_PASTE_OK", output)
                process.wait(timeout=8)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def test_multiline_bracketed_paste_is_recorded_as_one_owner_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            master, slave = pty.openpty()
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "bash", "-c", "sleep 10",
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            directive = "Review the harness end to end.\n\n- Include failures\n- Require independent review"
            try:
                self._read_until(master, b"interactive supervisor ready", timeout=3)
                os.write(master, b"\x1b[200~" + directive.encode() + b"\x1b[201~")
                self._wait_for(lambda: board.snapshot(root).get("owner_directions", {}).get(session["id"], {}).get("text") == directive)
                self.assertNotIn("\x1b", board.snapshot(root)["owner_directions"][session["id"]]["text"])
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def test_terminal_reply_sequences_are_not_recorded_and_claim_scope_audit_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            master, slave = pty.openpty()
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "bash", "-c", "sleep 10",
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            direction = "Execute every scenario simulation and reject false PASS results"
            try:
                self._read_until(master, b"interactive supervisor ready", timeout=3)
                # Split a cursor reply across reads, then add colour-query,
                # device-attribute, and focus replies before real owner text.
                os.write(master, b"\x1b[7;")
                time.sleep(.05)
                os.write(master, b"85R\x1b]10;rgb:e6ce/e6ce/e6ce\x07\x1b]11;rgb:05b1/06cf/0923\x07\x1b[?1;2c\x1b[O\x1b[I" + direction.encode() + b"\n")
                self._wait_for(lambda: board.snapshot(root).get("owner_directions", {}).get(session["id"], {}).get("text") == direction)
                board.begin_task(root, agent["id"], "OWNER-SCOPE")
                contract.create_contract(root, "OWNER-SCOPE", direction, ["scope"])
                proof = root / "scope-proof.txt"
                proof.write_text("owner scope preserved\n")
                contract.add_evidence(root, "OWNER-SCOPE", "scope", [proof])
                ledger = root / "scope-ledger.md"
                ledger.write_text("| ID | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|---|\n| S-001 | scope audit | `python3 -m unittest test_scope` | Exact owner text is retained | PASS: exact clean owner direction compared equal | PASS |\n")
                checks = cto.release_check(root, "OWNER-SCOPE", ledger, root)
                self.assertTrue(checks["owner_direction_recorded"])
                self.assertEqual(checks["claim_scope_missing_terms"], [],
                                 "a byte-clean direction leaves no advisory term gaps")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def test_trailing_dcs_and_c1_cursor_replies_are_not_recorded_through_real_pty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            master, slave = pty.openpty()
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "bash", "-c", "sleep 10",
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            direction = "Preserve owner prose about OWNER DIRECTION and reject reply bytes"
            try:
                self._read_until(master, b"interactive supervisor ready", timeout=3)
                # Exercise both 7-bit DCS and 8-bit C1 DCS/CSI after the real
                # owner text. Split the C1 DCS so the streaming path must hold
                # it until the C1 string terminator arrives.
                os.write(master, direction.encode() + b"\x1bP1$r0m\x1b\\\x9b7;85R\x90tmux;")
                time.sleep(.05)
                os.write(master, b"passthrough\x9c\n")
                self._wait_for(lambda: board.snapshot(root).get("owner_directions", {}).get(session["id"], {}).get("text") == direction)
                board.begin_task(root, agent["id"], "TRAILING-REPLIES")
                contract.create_contract(root, "TRAILING-REPLIES", direction, ["scope"])
                proof = root / "scope-proof.txt"
                proof.write_text("trailing replies removed\n")
                contract.add_evidence(root, "TRAILING-REPLIES", "scope", [proof])
                ledger = root / "scope-ledger.md"
                ledger.write_text("| ID | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|---|\n| S-001 | scope audit | `python3 -m unittest test_scope` | Exact owner text is retained | PASS: clean trailing-reply direction compared equal | PASS |\n")
                checks = cto.release_check(root, "TRAILING-REPLIES", ledger, root)
                self.assertEqual(checks["claim_scope_missing_terms"], [],
                                 "a byte-clean direction leaves no advisory term gaps")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def test_shared_source_change_does_not_stop_a_global_cto_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._git(root, "init", "-q", "-b", "main")
            self._git(root, "config", "user.email", "harness@example.invalid")
            self._git(root, "config", "user.name", "Harness")
            (root / ".gitignore").write_text(".harness/\n")
            (root / "tracked.txt").write_text("baseline\n")
            self._git(root, "add", ".")
            self._git(root, "commit", "-qm", "baseline")
            session = control.create(root, "claude_cto")
            agent = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
            master, slave = pty.openpty()
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "bash", "-c", f"touch {root / 'delivery-agent-is-working.txt'}; sleep 10",
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"interactive supervisor ready", timeout=3)
                time.sleep(.35)
                self.assertIsNone(process.poll(), "the CTO was stopped by another agent's shared worktree change")
                self.assertNotEqual(board.snapshot(root)["agents"][agent["id"]]["status"], "blocked")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def test_shared_source_change_does_not_stop_an_independent_reviewer_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "claude_reviewer")
            agent = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
            master, slave = pty.openpty()
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "bash", "-c", f"touch {root / 'delivery-agent-is-working.txt'}; sleep 10",
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"interactive supervisor ready", timeout=3)
                time.sleep(.35)
                self.assertIsNone(process.poll(), "the reviewer was stopped by another agent's shared worktree change")
                self.assertNotEqual(board.snapshot(root)["agents"][agent["id"]]["status"], "blocked")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def test_stopping_supervisor_stops_its_cli_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            master, slave = pty.openpty()
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "bash", "-c", "sleep 10",
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"interactive supervisor ready", timeout=3)
                os.kill(process.pid, 15)
                self.assertNotEqual(process.wait(timeout=8), 0)
                self.assertNotIn(agent["id"], board.snapshot(root)["agents"])
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def test_stopping_supervisor_kills_a_cli_that_ignores_sigterm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "claude_cto")
            agent = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
            master, slave = pty.openpty()
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "python3", "-c", "import signal,time; signal.signal(signal.SIGTERM, lambda *_: None); time.sleep(10)",
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"interactive supervisor ready", timeout=3)
                time.sleep(.1)
                os.kill(process.pid, 15)
                self.assertNotEqual(process.wait(timeout=8), 0)
                self.assertNotIn(agent["id"], board.snapshot(root)["agents"])
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=8)
                os.close(master)

    def test_stopping_supervisor_terminates_agent_spawned_background_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper_pid = root / "helper.pid"
            helper_stopped = root / "helper-stopped"
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            master, slave = pty.openpty()
            helper_code = "import pathlib,signal,time,os,sys; p=pathlib.Path(" + repr(str(helper_pid)) + "); s=pathlib.Path(" + repr(str(helper_stopped)) + "); signal.signal(signal.SIGTERM, lambda *_: (s.write_text('stopped'), sys.exit(0))); p.write_text(str(os.getpid())); time.sleep(30)"
            cli_code = "import subprocess,time; subprocess.Popen(['python3','-c'," + repr(helper_code) + "]); time.sleep(30)"
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "python3", "-c", cli_code,
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"interactive supervisor ready", timeout=3)
                self._wait_for(lambda: helper_pid.exists(), timeout=3)
                os.kill(process.pid, 15)
                self.assertNotEqual(process.wait(timeout=8), 0)
                self._wait_for(lambda: helper_stopped.exists(), timeout=3)
                self.assertEqual(helper_stopped.read_text(), "stopped")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=8)
                os.close(master)

    def _wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail("timed out waiting for supervisor state")

    def _read_until(self, master, needle, timeout):
        deadline = time.monotonic() + timeout
        output = b""
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], .1)
            if master in readable:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
            if needle in output:
                return output
        self.fail(f"did not receive {needle!r}; got {output!r}")

    def _git(self, root, *args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

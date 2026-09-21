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
        # Closed by window IDENTITY, not by the loop reference it was matched
        # through, and never by position: `close window 1` resolves to whatever
        # window happens to be frontmost, which during diagnosis was the
        # owner's own live session.
        self.assertIn("close window id targetId", arguments[2])
        self.assertNotIn("close terminalWindow", arguments[2])
        self.assertNotIn("close window 1", arguments[2])
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
                startup = self._read_until(master, b"interactive supervisor ready")
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
                self._read_until(master, b"interactive supervisor ready")
                queued = control.enqueue_instruction(root, session["id"], "Run the routed CTO action now.", "test-controller")
                output = self._read_until(master, b"INSTRUCTION_SUBMITTED")
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
                "buf=b''\n"
                "# A CEILING, not a pause: the loop below breaks the moment the paste and\n"
                "# its ENTER have arrived, so a generous bound costs nothing when the\n"
                "# machine is idle. Three seconds was the last fixed duration in this\n"
                "# test - the parent already waits on the condition - and under full-suite\n"
                "# load the CHILD gave up before the paste arrived, reporting\n"
                "# PASTE_NOT_SUBMITTED for a product that was working.\n"
                "deadline=time.time()+30\n"
                "while time.time()<deadline:\n"
                "    r,_,_=select.select([0],[],[],0.2)\n"
                "    if r:\n"
                "        chunk=os.read(0,4096)\n"
                "        if not chunk: break\n"
                "        buf+=chunk\n"
                "    end=buf.find(b'\\x1b[201~')\n"
                "    if end>=0 and b'\\r' in buf[end:]: break\n"
                "start=buf.find(b'\\x1b[200~')\n"
                "end=buf.find(b'\\x1b[201~')\n"
                "inside=buf[start+6:end] if 0<=start<end else b''\n"
                "after=buf[end+6:] if end>=0 else b''\n"
                "ok=(0<=start<end) and b'\\r' not in inside and b'\\r' in after\n"
                "verdict=b'SEPARATE_ENTER' if ok else b'PASTE_NOT_SUBMITTED'\n"
                "os.write(1,verdict+b'|INSIDE='+inside+b'|AFTER='+after.replace(b'\\r',b'<ENTER>'))\n"
            )
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "python3", "-c", child,
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"interactive supervisor ready")
                control.enqueue_instruction(root, session["id"], "Resume this routed task without owner input.", "paste-sensitive-test")
                output = self._read_until(master, b"SEPARATE_ENTER")
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
                "buf=b''\n"
                "# A CEILING, not a pause: the loop below breaks the moment the paste and\n"
                "# its ENTER have arrived, so a generous bound costs nothing when the\n"
                "# machine is idle. Three seconds was the last fixed duration in this\n"
                "# test - the parent already waits on the condition - and under full-suite\n"
                "# load the CHILD gave up before the paste arrived, reporting\n"
                "# PASTE_NOT_SUBMITTED for a product that was working.\n"
                "deadline=time.time()+30\n"
                "while time.time()<deadline:\n"
                "    r,_,_=select.select([0],[],[],0.2)\n"
                "    if r:\n"
                "        chunk=os.read(0,4096)\n"
                "        if not chunk: break\n"
                "        buf+=chunk\n"
                "    end=buf.find(b'\\x1b[201~')\n"
                "    if end>=0 and b'\\r' in buf[end:]: break\n"
                "start=buf.find(b'\\x1b[200~')\n"
                "end=buf.find(b'\\x1b[201~')\n"
                "inside=buf[start+6:end] if 0<=start<end else b''\n"
                "after=buf[end+6:] if end>=0 else b''\n"
                "ok=(0<=start<end) and b'First\\n\\nSecond' in inside and b'\\r' not in inside and b'\\r' in after\n"
                "os.write(1,(b'MULTILINE_PASTE_OK' if ok else b'MULTILINE_PASTE_BAD')+b'|INSIDE='+inside+b'|AFTER='+after.replace(b'\\r',b'<ENTER>'))\n"
            )
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"], "--",
                "python3", "-c", child,
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"interactive supervisor ready")
                control.enqueue_instruction(root, session["id"], "First\n\nSecond", "multiline-owner-message")
                output = self._read_until(master, b"MULTILINE_PASTE_")
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
                self._read_until(master, b"interactive supervisor ready")
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
                self._read_until(master, b"interactive supervisor ready")
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
                self._read_until(master, b"interactive supervisor ready")
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
                self._read_until(master, b"interactive supervisor ready")
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
                self._read_until(master, b"interactive supervisor ready")
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
                self._read_until(master, b"interactive supervisor ready")
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
                self._read_until(master, b"interactive supervisor ready")
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
                self._read_until(master, b"interactive supervisor ready")
                self._wait_for(lambda: helper_pid.exists())
                os.kill(process.pid, 15)
                self.assertNotEqual(process.wait(timeout=8), 0)
                self._wait_for(lambda: helper_stopped.exists())
                self.assertEqual(helper_stopped.read_text(), "stopped")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=8)
                os.close(master)

    def _wait_for(self, predicate, timeout=None):
        # Same ceiling reasoning as _read_until: it returns the moment the
        # predicate holds, so a generous bound costs nothing when idle.
        # NOTE: the default used to be 3 seconds written into the signature,
        # which a blanket edit then stripped - blunt replacements hit
        # signatures as well as call sites.
        timeout = timeout or self.READ_TIMEOUT_CEILING
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail("timed out waiting for supervisor state")

    # A ceiling, not a pause. These tests drive a real pty and a real child
    # process, so under load the child is simply slower to reach the point being
    # observed. The reliability gate caught it: four of five identical full-suite
    # runs, and one where these two tests failed alone.
    #
    # Waiting longer costs NOTHING when the machine is idle, because the loop
    # returns the instant the needle appears. A short ceiling buys nothing and
    # turns a busy machine into a red suite - the "flaky test" that is really a
    # measurement artefact, and that trains everyone to ignore a real failure.
    READ_TIMEOUT_CEILING = 30

    def _read_until(self, master, needle, timeout=None):
        deadline = time.monotonic() + (timeout or self.READ_TIMEOUT_CEILING)
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
        self.fail(f"did not receive {needle!r} within {deadline - time.monotonic() + (timeout or self.READ_TIMEOUT_CEILING):.0f}s; got {output!r}")

    def _git(self, root, *args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

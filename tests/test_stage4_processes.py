# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Process-handling fixes from the risk report (P13, P14, P16, P18, P19, P20, P21).

Each test was written to fail on the code that had the problem. Every process started here is
killed in a `finally`, and nothing signals a process this test did not start.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, codex_app_server, control, platform_support, project_registry as registry, project_worker, release_preview


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _orphan_in_new_group() -> tuple[subprocess.Popen, int]:
    """A shell that backgrounds a long sleep and exits: the sleep stays alive in the shell's own group."""
    shell = subprocess.Popen(["/bin/sh", "-c", "sleep 60 & echo $!"], stdout=subprocess.PIPE, text=True, start_new_session=True)
    sleeper = int(shell.stdout.readline().strip())
    shell.wait(timeout=5)
    return shell, sleeper


class StopAndPauseTests(unittest.TestCase):
    def test_stop_handles_a_process_that_exits_between_the_check_and_the_signal(self):
        # P20: pause() already treats ProcessLookupError as "already gone"; stop() let it escape.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            control.attach(root, session["id"], 99999999)
            with patch.object(control, "_pid_is_alive", return_value=True), \
                    patch.object(control.os, "kill", side_effect=ProcessLookupError):
                result = control.stop(root, session["id"])
            self.assertEqual(result["status"], "stopped")
            self.assertIn("exited", result["reason"])

    def test_a_stop_that_the_process_ignores_ends_in_a_forced_kill(self):
        # P13: one SIGTERM, then after 30 seconds the card said "stopped" while the process lived on.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            stubborn = subprocess.Popen(
                [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(120)"],
                stdout=subprocess.PIPE, text=True,
            )
            try:
                stubborn.stdout.readline()
                control.attach(root, session["id"], stubborn.pid)
                self.assertEqual(control.stop(root, session["id"])["status"], "stopping")
                time.sleep(0.3)
                self.assertIsNone(stubborn.poll(), "SIGTERM alone must not end this process (test premise)")
                with patch.object(control, "STOP_RECONCILE_SECONDS", 0.0):
                    control.snapshot(root)
                stubborn.wait(timeout=5)         # raises if the harness never escalated
                self.assertEqual(stubborn.returncode, -signal.SIGKILL)
            finally:
                if stubborn.poll() is None:
                    stubborn.kill()
                    stubborn.wait(timeout=5)


class ForcedStopIdentityTests(unittest.TestCase):
    """B2 (review of Stage 4): the forced kill used the stored number alone, so a number reused after the agent
    exited would have been killed. Attach now records the process's start time and the kill requires it."""

    def _stubborn(self):
        return subprocess.Popen(
            [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(120)"],
            stdout=subprocess.PIPE, text=True,
        )

    def test_a_reused_process_number_is_not_force_killed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            stranger = self._stubborn()
            try:
                stranger.stdout.readline()
                control.attach(root, session["id"], stranger.pid)
                control.stop(root, session["id"])
                with control.locked_state(root) as state:     # the recorded identity belongs to the agent that has since exited
                    state["sessions"][session["id"]]["pid_start_token"] = "Sat Jan  1 00:00:00 2000"
                with patch.object(control, "STOP_RECONCILE_SECONDS", 0.0):
                    result = [item for item in control.snapshot(root)["sessions"] if item["id"] == session["id"]][0]
                time.sleep(0.3)
                self.assertIsNone(stranger.poll(), "a process that is not the session's agent must not be killed")
                self.assertEqual(result["status"], "stopped")
            finally:
                stranger.kill()
                stranger.wait(timeout=5)

    def test_a_session_without_a_recorded_identity_is_never_force_killed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            child = self._stubborn()
            try:
                child.stdout.readline()
                control.attach(root, session["id"], child.pid)
                control.stop(root, session["id"])
                with control.locked_state(root) as state:
                    state["sessions"][session["id"]]["pid_start_token"] = ""
                with patch.object(control, "STOP_RECONCILE_SECONDS", 0.0):
                    result = [item for item in control.snapshot(root)["sessions"] if item["id"] == session["id"]][0]
                time.sleep(0.3)
                self.assertIsNone(child.poll())
                self.assertTrue(result.get("exit_unconfirmed"), "an unverified process is reported honestly, not killed")
            finally:
                child.kill()
                child.wait(timeout=5)


class PreviewStopTests(unittest.TestCase):
    def test_a_member_seen_while_the_leader_lived_is_stopped_after_it_exits(self):
        # P18: the shell leaves and the server stays. The server was recorded while the shell was still alive.
        shell = subprocess.Popen(["/bin/sh", "-c", "sleep 60 & echo $!; sleep 1"], stdout=subprocess.PIPE, text=True, start_new_session=True)
        sleeper = int(shell.stdout.readline().strip())
        try:
            preview = release_preview.Preview("T", "c" * 40, Path(tempfile.gettempdir()) / "unused-preview")
            preview.process = shell
            preview.remember_group()                       # the health check does this on every poll while the shell lives
            shell.wait(timeout=10)
            self.assertTrue(_alive(sleeper), "test premise: the server outlives its shell")
            with patch.object(release_preview, "STOP_GRACE_SECONDS", 0.5):
                preview.stop()
            self.assertTrue(_wait_gone(sleeper), "the backgrounded server was left running")
        finally:
            if _alive(sleeper):
                os.kill(sleeper, signal.SIGKILL)
            shell.stdout.close()

    def test_the_launch_sampler_catches_a_server_whose_shell_exits_within_moments(self):
        # A start script that daemonizes lives only a fraction of a second; the sampler started at launch records
        # the group many times a second while the shell is alive.
        shell = subprocess.Popen(["/bin/sh", "-c", "sleep 60 & echo $!; sleep 0.4"], stdout=subprocess.PIPE, text=True, start_new_session=True)
        sleeper = int(shell.stdout.readline().strip())
        try:
            preview = release_preview.Preview("T", "c" * 40, Path(tempfile.gettempdir()) / "unused-preview")
            preview.process = shell
            preview.watch_group()
            shell.wait(timeout=10)
            time.sleep(0.2)
            with patch.object(release_preview, "STOP_GRACE_SECONDS", 0.5):
                preview.stop()
            self.assertTrue(_wait_gone(sleeper), "the backgrounded server was left running")
        finally:
            if _alive(sleeper):
                os.kill(sleeper, signal.SIGKILL)
            shell.stdout.close()

    def test_stop_does_not_signal_a_stranger_that_reused_the_leaders_number(self):
        shell, _ = _orphan_in_new_group()
        stranger_table = {
            shell.pid: {"pid": shell.pid, "ppid": 1, "pgid": shell.pid, "start_token": "stranger", "command": "other"},
            424242: {"pid": 424242, "ppid": shell.pid, "pgid": shell.pid, "start_token": "x", "command": "other"},
        }
        preview = release_preview.Preview("T", "c" * 40, Path(tempfile.gettempdir()) / "unused-preview")
        preview.process = shell
        try:
            with patch.object(platform_support.process_identity().__class__, "process_table", return_value=stranger_table), \
                    patch.object(release_preview.os, "kill") as kill:
                preview.remember_group()
                preview.stop()
            kill.assert_not_called()
        finally:
            if shell.stdout:
                shell.stdout.close()

    def test_stop_does_not_signal_an_unrelated_group_that_took_the_old_number_just_after_the_leader_left(self):
        # B1 (second review of Stage 4): even moments after the shell exits, a child carrying its group number may
        # belong to a new group whose own leader also left. Nothing is recorded after the leader exits, so nothing is signalled.
        shell, _ = _orphan_in_new_group()
        table = {424242: {"pid": 424242, "ppid": 1, "pgid": shell.pid, "start_token": "another group's child", "command": "other"}}
        preview = release_preview.Preview("T", "c" * 40, Path(tempfile.gettempdir()) / "unused-preview")
        preview.process = shell
        try:
            with patch.object(platform_support.process_identity().__class__, "process_table", return_value=table), \
                    patch.object(release_preview.os, "kill") as kill:
                preview.remember_group()          # the leader has exited: nothing seen now can be proven to be ours
                preview.stop()
            kill.assert_not_called()
        finally:
            if shell.stdout:
                shell.stdout.close()


class CodexGroupCleanupTests(unittest.TestCase):
    def test_a_leftover_group_member_is_killed_when_the_leader_is_ended(self):
        # The wrapper leaves a child behind when it is ended (the 2026-10-03 orphan case). The members are recorded
        # while the leader is alive, so the child is still recognised after the leader is gone.
        shell = subprocess.Popen(["/bin/sh", "-c", "sleep 60 & echo $!; wait"], stdout=subprocess.PIPE, text=True, start_new_session=True)
        sleeper = int(shell.stdout.readline().strip())
        try:
            self.assertTrue(_alive(sleeper))
            codex_app_server.end_process_group(shell, grace=0.2)
            self.assertTrue(_wait_gone(sleeper))
        finally:
            if _alive(sleeper):
                os.kill(sleeper, signal.SIGKILL)
            if shell.poll() is None:
                shell.kill()
            shell.stdout.close()

    def test_children_of_a_group_whose_leader_is_already_gone_are_left_alone(self):
        # B1 (review of Stage 4): with the leader absent and nothing recorded, a process carrying the old group
        # number could belong to anyone. Not provably ours, so it is not killed.
        shell, sleeper = _orphan_in_new_group()
        try:
            with patch.object(codex_app_server.os, "kill") as kill:
                codex_app_server.end_process_group(shell, grace=0.1)
            self.assertNotIn(sleeper, [call.args[0] for call in kill.call_args_list])
            self.assertTrue(_alive(sleeper))
        finally:
            if _alive(sleeper):
                os.kill(sleeper, signal.SIGKILL)
            shell.stdout.close()

    def test_a_group_number_reused_by_another_process_is_not_killed(self):
        # P16: members were found by the numeric pgid alone, so a recycled number killed a stranger.
        shell = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)
        shell.wait(timeout=5)
        table = {
            shell.pid: {"pid": shell.pid, "ppid": 1, "pgid": shell.pid, "start_token": "a stranger started later", "command": "other"},
            424242: {"pid": 424242, "ppid": shell.pid, "pgid": shell.pid, "start_token": "x", "command": "other"},
        }
        with patch.object(platform_support.process_identity().__class__, "process_table", return_value=table), \
                patch.object(codex_app_server.os, "kill") as kill:
            codex_app_server.end_process_group(shell, grace=0.1)
        kill.assert_not_called()


class CleanupIndependenceTests(unittest.TestCase):
    def test_every_cleanup_step_runs_even_when_an_earlier_one_raises(self):
        # P19
        ran: list[str] = []

        def boom():
            ran.append("first")
            raise RuntimeError("watchdog join failed")

        failures = project_worker._run_cleanup([("first", boom), ("second", lambda: ran.append("second")), ("third", lambda: ran.append("third"))])
        self.assertEqual(ran, ["first", "second", "third"])
        self.assertEqual([name for name, _ in failures], ["first"])

    def test_the_worker_shutdown_uses_independent_steps(self):
        import inspect
        source = inspect.getsource(project_worker)
        after_finally = source.split("    finally:\n        _run_cleanup(", 1)
        self.assertEqual(len(after_finally), 2, "the worker's final block must run its steps through _run_cleanup")


class ActivationLockTests(unittest.TestCase):
    def test_two_activations_that_both_see_the_same_stale_lock_cannot_both_win(self):
        # P14: both read the dead-pid lock, both judged it stale, both os.replace()d it.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            one = registry.register(home, "one", self._project(tmp, "one"))
            two = registry.register(home, "two", self._project(tmp, "two"))
            dead = 2_000_000_000
            registry._lock_path(home).write_text(json.dumps({"pid": dead, "project_id": "gone", "project_name": "gone"}), encoding="utf-8")
            meeting = threading.Barrier(2)
            real_alive = registry._pid_alive

            def alive(pid: int) -> bool:
                if pid == dead:
                    try:
                        meeting.wait(timeout=0.6)      # both reached the check-then-replace window together
                    except threading.BrokenBarrierError:
                        pass                           # serialized: the other one is waiting outside
                    return False
                return real_alive(pid)

            outcomes: dict[str, str] = {}

            def attempt(project_id: str):
                try:
                    registry.activate(home, project_id)
                    outcomes[project_id] = "won"
                except RuntimeError:
                    outcomes[project_id] = "refused"

            with patch.object(registry, "_pid_alive", alive):
                threads = [threading.Thread(target=attempt, args=(item["id"],)) for item in (one, two)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
            self.assertEqual(sorted(outcomes.values()), ["refused", "won"])

    @staticmethod
    def _project(tmp: str, name: str) -> Path:
        path = Path(tmp) / name
        path.mkdir()
        return path


class PreviewTickFailureTests(unittest.TestCase):
    def test_a_crashed_tick_is_recorded_on_the_board_not_only_printed(self):
        # P21
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            supervisor = release_preview.ReleasePreviewSupervisor(root, tick_seconds=0.05)
            with patch.object(release_preview.ReleasePreviewSupervisor, "tick", side_effect=RuntimeError("cannot read the release list sk-ant-api03-" + "x" * 30)):
                supervisor.start()
                try:
                    deadline = time.monotonic() + 5
                    found = []
                    while time.monotonic() < deadline and not found:
                        release_preview.wake()
                        time.sleep(0.1)
                        found = [e for e in board.snapshot(root)["events"] if e.get("kind") == "release_preview_tick_failed"]
                finally:
                    supervisor.shutdown()
            self.assertTrue(found, "the failure never reached the board")
            self.assertIn("cannot read the release list", found[0]["message"])
            self.assertNotIn("sk-ant-api03-" + "x" * 30, json.dumps(found[0]))
            self.assertLessEqual(len(found), 3, "a repeating failure must not flood the board")


if __name__ == "__main__":
    unittest.main()

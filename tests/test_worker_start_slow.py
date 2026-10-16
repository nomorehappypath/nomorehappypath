"""2026-10-15: the manager waited only 5.0 s for a project's board worker to answer /api/ready. Studio's board
(45 MB state, ~35k events) needs ~6 s on a busy Mac, so every Open stopped the worker, closed the project and said
"could not start". The wait is now 60 s, adjustable, and the messages say what happened."""
import json
import math
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.environment_support import require_loopback
from harness import board, project_manager, project_registry as registry, runtime_identity


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeProcess:
    pid = 4242

    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def ready_payload(manager, token, ref):
    return {
        "ready": True, "ready_token": token, "project_ref": ref,
        "runtime": runtime_identity.public(manager.runtime),
        "surface": {"project_chat": True},
        "watchdog": {"active": True, "interval_seconds": board.WATCHDOG_INTERVAL_SECONDS,
                     "cto_poll_deadline_seconds": board.CTO_MONITOR_INTERVAL_SECONDS},
    }


class SlowWorkerStart(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.code = Path(self.tmp.name) / "code"
        self.code.mkdir()
        self.entry = registry.register(self.home, "slow", self.code)

    def open_with(self, ready_after, manager):
        clock = FakeClock()
        process = FakeProcess()

        def fake_urlopen(request, timeout=None):
            if clock.now < ready_after:
                raise OSError("connection refused")
            token = manager.worker_action_token
            return FakeResponse(ready_payload(manager, token, project_manager.board_surface.project_id(
                registry.context_for_entry(self.entry))))

        with patch.object(project_manager.subprocess, "Popen", return_value=process), \
                patch.object(project_manager, "urlopen", fake_urlopen), \
                patch.object(project_manager, "time", clock):
            try:
                manager.open_project(self.entry["id"])
                return process, None
            except ValueError as error:
                return process, str(error)

    def test_default_wait_is_sixty_seconds(self):
        self.assertEqual(project_manager.WORKER_START_TIMEOUT, 60.0)
        manager = project_manager.ProjectManager(self.home, board_port=45999)
        self.assertEqual(manager.worker_start_timeout, 60.0)

    def test_a_board_that_needs_six_seconds_opens(self):
        manager = project_manager.ProjectManager(self.home, board_port=45999)
        process, error = self.open_with(6.0, manager)
        self.assertIsNone(error)
        self.assertFalse(process.terminated)
        self.assertIsNotNone(registry.active_project(self.home))

    def test_a_board_that_never_answers_is_stopped_with_a_message_that_says_so(self):
        manager = project_manager.ProjectManager(self.home, board_port=45999, worker_start_timeout=3)
        process, error = self.open_with(10_000.0, manager)
        self.assertIn("made no progress for 3 seconds", error)
        self.assertIn("press Open again", error)
        self.assertNotIn("could not start", error)
        self.assertTrue(process.terminated)
        self.assertIsNone(registry.active_project(self.home))
        shown = manager.worker_failure["message"]
        self.assertIn("made no progress for 3 seconds", shown)

    def test_a_real_refused_socket_gets_the_busy_computer_message(self):
        """The message must appear for the platform's real refusal ([Errno 61]/[Errno 111]), not a fabricated string."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        manager = project_manager.ProjectManager(self.home, board_port=free_port, worker_start_timeout=0.6)
        process = FakeProcess()
        with patch.object(project_manager.subprocess, "Popen", return_value=process):
            with self.assertRaises(ValueError) as caught:
                manager.open_project(self.entry["id"])
        self.assertIn("made no progress for 0.6 seconds", str(caught.exception))
        self.assertIn("computer may be busy", str(caught.exception))
        self.assertTrue(process.terminated)
        self.assertIn("made no progress", manager.worker_failure["message"])

    def test_the_timeout_is_adjustable_and_refuses_nonsense(self):
        self.assertEqual(project_manager.parse_start_timeout("90"), 90.0)
        for bad in ("abc", "0", "-5", "inf", "nan"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    project_manager.parse_start_timeout(bad)
        self.assertEqual(project_manager.WORKER_START_TIMEOUT_ENV, "HARNESS_WORKER_START_TIMEOUT")


if __name__ == "__main__":
    unittest.main()

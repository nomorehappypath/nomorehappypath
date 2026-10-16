"""2026-10-15: Studio could not be opened because the manager gave its board 5 s to start. A flat longer limit is still a
limit a bigger project passes one day, so: the start wait is a NO-PROGRESS window (it restarts while the worker's CPU or
memory moves), Close/Pause cancel a start, the project list says "starting" and "your agents come back", and the other
fixed limits on the open path (probes, relaunch request, stop, proxy, attach deadline, launcher, update gate) are generous."""
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from tests.environment_support import require_loopback
from tests.test_worker_start_slow import FakeClock, FakeProcess, FakeResponse, ready_payload
from harness import board, control, project_manager, project_registry as registry, update_gate

ROOT = Path(__file__).resolve().parents[1]


class StartProgress(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.code = Path(self.tmp.name) / "code"
        self.code.mkdir()
        self.entry = registry.register(self.home, "big", self.code)

    def run_open(self, manager, ready_after, progress, during=None):
        clock = FakeClock()
        process = FakeProcess()

        def fake_urlopen(request, timeout=None):
            if during:
                during(clock)
            if clock.now < ready_after:
                raise OSError("connection refused")
            return FakeResponse(ready_payload(manager, manager.worker_action_token, project_manager.board_surface.project_id(
                registry.context_for_entry(self.entry))))

        with patch.object(project_manager.subprocess, "Popen", return_value=process), \
                patch.object(project_manager, "urlopen", fake_urlopen), \
                patch.object(project_manager, "time", clock), \
                patch.object(manager, "_worker_progress", side_effect=lambda: progress(clock)):
            try:
                manager.open_project(self.entry["id"])
                return process, None
            except ValueError as error:
                return process, str(error)

    def test_a_worker_that_keeps_working_is_never_failed_by_the_clock(self):
        manager = project_manager.ProjectManager(self.home, board_port=45999, worker_start_timeout=3)
        process, error = self.run_open(manager, 600.0, lambda clock: (str(int(clock.now)), "1"))
        self.assertIsNone(error)
        self.assertFalse(process.terminated)

    def test_a_worker_with_no_progress_is_stopped_with_a_message_that_says_so(self):
        manager = project_manager.ProjectManager(self.home, board_port=45999, worker_start_timeout=3)
        process, error = self.run_open(manager, 10_000.0, lambda clock: ("0:01", "100"))
        self.assertIn("made no progress for 3 seconds", error)
        self.assertTrue(process.terminated)
        self.assertIsNone(registry.active_project(self.home))

    def test_close_cancels_a_start_instead_of_waiting_behind_it(self):
        manager = project_manager.ProjectManager(self.home, board_port=45999, worker_start_timeout=3)

        def cancel(clock):
            if clock.now > 2:
                manager._start_cancel.set()

        process, error = self.run_open(manager, 10_000.0, lambda clock: (str(int(clock.now)), "1"), during=cancel)
        self.assertIn("cancelled", error)
        self.assertTrue(process.terminated)

    def test_the_list_says_starting_and_a_second_open_is_refused(self):
        manager = project_manager.ProjectManager(self.home, board_port=45999, worker_start_timeout=3)
        seen = {}

        def during(clock):
            if "row" not in seen:
                seen["row"] = next(r for r in manager.projects_payload()["projects"] if r["id"] == self.entry["id"])
                outcome = {}

                def second_click():
                    try:
                        manager.open_project(self.entry["id"])
                    except ValueError as error:
                        outcome["error"] = str(error)

                clicker = threading.Thread(target=second_click)
                clicker.start()
                clicker.join(timeout=5)
                seen["second"] = outcome.get("error", "")

        _, error = self.run_open(manager, 5.0, lambda clock: (str(int(clock.now)), "1"), during=during)
        self.assertIsNone(error)
        self.assertIn("already starting", seen["second"])
        self.assertTrue(seen["row"]["starting"])
        self.assertTrue(seen["row"]["active"], "activation stays truthful so the update gate keeps blocking")
        row = next(r for r in manager.projects_payload()["projects"] if r["id"] == self.entry["id"])
        self.assertFalse(row["starting"])

    def test_a_closed_project_with_saved_work_says_its_agents_come_back(self):
        manager = project_manager.ProjectManager(self.home, board_port=45999)
        row = next(r for r in manager.projects_payload()["projects"])
        self.assertFalse(row["reopen_restores"])
        with patch.object(project_manager, "derive_status", return_value={"resume_available": True, "paused": False,
                                                                        "agent_counts": {"total": 3}}):
            row = next(r for r in manager.projects_payload()["projects"])
        self.assertTrue(row["reopen_restores"])
        page = (ROOT / "harness/project_manager_page.py").read_text()
        self.assertIn("Open it and your agents come back with their saved work.", page)
        self.assertIn("Starting · loading the board", page)

    def test_progress_is_sampled_from_the_real_ps_without_confusing_failure_with_exit(self):
        manager = project_manager.ProjectManager(self.home, board_port=45999)
        manager.worker = type("W", (), {"pid": os.getpid()})()
        sample = manager._worker_progress()
        self.assertIsInstance(sample, tuple)
        self.assertEqual(len(sample), 2)
        manager.worker = type("W", (), {"pid": 2 ** 22 + 12345})()
        self.assertIsNone(manager._worker_progress())
        with patch.object(project_manager.subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", 3)):
            manager.worker = type("W", (), {"pid": os.getpid()})()
            self.assertIsNone(manager._worker_progress())


class OtherFixedLimits(unittest.TestCase):
    def test_audited_limits_are_generous_and_adjustable(self):
        self.assertGreaterEqual(project_manager.READY_PROBE_TIMEOUT, 5.0)
        self.assertGreaterEqual(project_manager.RESUME_LAUNCH_TIMEOUT, 300.0)
        self.assertGreaterEqual(project_manager.WORKER_STOP_TIMEOUT, 30.0)
        self.assertGreaterEqual(project_manager.PROXY_TIMEOUT, 120.0)
        self.assertGreaterEqual(control.RESUME_ATTACH_SECONDS, 120.0)
        self.assertEqual(control.LAUNCH_ATTACH_SECONDS, 30.0)
        with patch.dict(os.environ, {"HARNESS_PROXY_TIMEOUT": "7", "HARNESS_READY_PROBE_TIMEOUT": "abc"}):
            self.assertEqual(project_manager._env_seconds("HARNESS_PROXY_TIMEOUT", 120.0), 7.0)
            self.assertEqual(project_manager._env_seconds("HARNESS_READY_PROBE_TIMEOUT", 5.0), 5.0)
        source = (ROOT / "harness/project_manager.py").read_text()
        for stale in ("timeout=.5)", "timeout=0.25)", "worker.wait(timeout=10)", "timeout=40)", "with urlopen(request, timeout=5)"):
            self.assertNotIn(stale, source)

    def test_update_gate_defers_for_a_busy_manager_and_allows_for_a_dead_one(self):
        def decide(error):
            with patch.object(update_gate, "fetch_projects", side_effect=error):
                return update_gate.main(["--manager-url", "http://127.0.0.1:1"])
        self.assertEqual(decide(URLError(ConnectionRefusedError(61, "refused"))), update_gate.EXIT_RESTART_ALLOWED)
        self.assertEqual(decide(URLError(socket.timeout("timed out"))), update_gate.EXIT_RESTART_DEFERRED)
        self.assertEqual(decide(ValueError("not json")), update_gate.EXIT_RESTART_DEFERRED)
        self.assertEqual(update_gate.fetch_projects.__defaults__[0], 30.0)

    def test_launcher_waits_for_the_manager_by_the_clock_with_bounded_requests(self):
        script = (ROOT / "scripts/start_project_manager.sh").read_text()
        self.assertIn("HARNESS_MANAGER_START_TIMEOUT:-120", script)
        self.assertIn("--connect-timeout 2 --max-time 10", script)
        self.assertNotIn("{1..60}", script)
        self.assertEqual(subprocess.run(["bash", "-n", str(ROOT / "scripts/start_project_manager.sh")]).returncode, 0)


class AttachClockStartsAtLaunch(unittest.TestCase):
    def test_a_staged_session_whose_deadline_passed_during_a_slow_start_still_launches(self):
        require_loopback()
        with tempfile.TemporaryDirectory() as tmp:
            home, code = Path(tmp) / "home", Path(tmp) / "code"
            code.mkdir()
            entry = registry.register(home, "late", code)
            root = registry.context_for_entry(entry)
            session = control.create(root, "codex_delivery")
            board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            board.begin_project_pause(root, drain_seconds=0)
            board.finish_project_pause(root)
            board.begin_project_resume(root)
            [prepared] = control.prepare_resume_sessions(root, [session["id"]])
            self.assertEqual(prepared["action"], "relaunch")
            with control.locked_state(root) as state:
                state["sessions"][session["id"]]["launch_deadline"] = "2000-01-01T00:00:00+00:00"
            claimed = control.mark_resume_launch_requested(root, session["id"])
            self.assertEqual(claimed["status"], "launching")
            self.assertTrue(claimed["resume_launch_requested_at"])

    def test_every_staged_session_survives_a_slow_start_and_status_polling_before_its_own_launch(self):
        require_loopback()
        with tempfile.TemporaryDirectory() as tmp:
            home, code = Path(tmp) / "home", Path(tmp) / "code"
            code.mkdir()
            entry = registry.register(home, "two", code)
            root = registry.context_for_entry(entry)
            first = control.create(root, "codex_delivery")
            second = control.create(root, "claude_reviewer")
            board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=first["id"])
            board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=second["id"])
            board.begin_project_pause(root, drain_seconds=0)
            board.finish_project_pause(root)
            board.begin_project_resume(root)
            prepared = control.prepare_resume_sessions(root, [first["id"], second["id"]])
            self.assertEqual([item["action"] for item in prepared], ["relaunch", "relaunch"])
            with control.locked_state(root) as state:
                for item in prepared:
                    state["sessions"][item["id"]]["launch_deadline"] = "2000-01-01T00:00:00+00:00"
            control.snapshot(root)  # ordinary status polling while the board was starting
            for item in prepared:
                claimed = control.mark_resume_launch_requested(root, item["id"])
                self.assertEqual(claimed["status"], "launching", item["id"])


if __name__ == "__main__":
    unittest.main()


from tests.test_project_pause_rendered import RenderedPauseTests as _Rendered  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

STATES_SCRIPT = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (document.querySelectorAll('article.project').length >= 2) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  await fetch('/__layout_result__', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rows: Array.from(document.querySelectorAll('article.project')).map(row => ({
      name: row.querySelector('h3').textContent.trim(),
      badges: Array.from(row.querySelectorAll('.badge')).map(n => n.textContent.trim()),
      buttons: Array.from(row.querySelectorAll('.actions button')).map(b => ({text: b.textContent.trim(), disabled: b.disabled})),
      text: row.textContent,
    }))}),
  });
})();
</script>
"""


class RenderedStates(unittest.TestCase):
    setUp = _Rendered.setUp
    _render = _Rendered._render

    def test_starting_and_reopen_states_render_in_the_real_page(self):
        home = self.base / "states-home"
        starting_code, closed_code = self.base / "s", self.base / "c"
        starting_code.mkdir()
        closed_code.mkdir()
        starting = registry.register(home, "Starting One", starting_code)
        registry.register(home, "Closed One", closed_code)
        manager = project_manager.ProjectManager(home, board_port=0)
        manager.worker_starting, manager.worker_starting_since = starting["id"], 0.0
        real = project_manager.derive_status

        def fake_status(entry):
            value = dict(real(entry))
            if entry["name"] == "Closed One":
                value.update({"resume_available": True, "paused": False})
            return value

        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
        import threading as _t
        _t.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.object(project_manager, "derive_status", fake_status):
                rendered = self._render(f"http://127.0.0.1:{server.server_address[1]}", STATES_SCRIPT)
        finally:
            server.shutdown()
            server.server_close()
        rows = {row["name"]: row for row in rendered["rows"]}
        one = rows["Starting One"]
        self.assertIn("Starting", one["badges"])
        self.assertEqual([b["disabled"] for b in one["buttons"] if "Starting" in b["text"]], [True])
        self.assertNotIn("Open Mission Control", one["text"])
        closed = rows["Closed One"]
        self.assertIn("Open it and your agents come back with their saved work.", closed["text"])
        self.assertIn("Open project", [b["text"] for b in closed["buttons"]])

del _Rendered  # not a test of this module; keeps the loader from re-running its tests

"""Owner 2026-11-27: after the update restart the old project worker kept port 8741, so every new project failed with
"project board worker exited before serving on port 8741". Real processes: a worker whose app is killed stops by itself and
frees its port; a second stop signal never cuts the app's own cleanup short."""
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from tests.environment_support import require_loopback

ROOT = Path(__file__).resolve().parents[1]


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class AWorkerWhoseAppIsGoneFreesItsPort(unittest.TestCase):
    def setUp(self):
        require_loopback()

    def test_the_worker_stops_when_its_app_is_killed(self):
        folder = Path(tempfile.mkdtemp(prefix="nmhp-orphan-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, True))
        port_file, pid_file = folder / "port", folder / "pid"
        worker = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {str(ROOT)!r})
            from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
            from harness import project_worker
            project_worker.PARENT_CHECK_SECONDS = 0.2
            server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
            open({str(pid_file)!r}, "w").write(str(os.getpid()))
            open({str(port_file)!r}, "w").write(str(server.server_address[1]))
            project_worker._stop_when_orphaned(server)
            server.serve_forever()
            server.server_close()
        """)
        app = subprocess.Popen([sys.executable, "-c",
                                f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {worker!r}])\ntime.sleep(600)"])
        self.addCleanup(lambda: app.poll() is None and app.kill())
        deadline = time.monotonic() + 20
        while not (port_file.exists() and port_file.read_text()) and time.monotonic() < deadline:
            time.sleep(0.1)
        port, pid = int(port_file.read_text()), int(pid_file.read_text())
        self.addCleanup(lambda: _kill_quietly(pid))
        self.assertFalse(_port_free(port), "the worker serves its port")
        app.kill()  # the app dies without any cleanup (the second stop signal, a crash)
        app.wait(timeout=10)
        deadline = time.monotonic() + 10
        while not _port_free(port) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(_port_free(port), "the worker kept its port after its app was gone")


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class ASecondStopNeverCutsTheAppsCleanupShort(unittest.TestCase):
    def test_two_stop_signals_still_finish_the_shutdown(self):
        folder = Path(tempfile.mkdtemp(prefix="nmhp-twostops-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, True))
        done, ready = folder / "done", folder / "ready"
        script = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(ROOT)!r})
            from harness import project_manager
            def slow_shutdown(self):
                time.sleep(1.5)
                open({str(done)!r}, "w").write("stopped the worker")
            project_manager.ProjectManager.shutdown = slow_shutdown
            project_manager.ProjectManager.keep_held_project_open = lambda self: open({str(ready)!r}, "w").write("1")
            project_manager.serve({str(folder / "home")!r}, port=0, board_port=1)
        """)
        app = subprocess.Popen([sys.executable, "-c", script])
        self.addCleanup(lambda: app.poll() is None and app.kill())
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(0.5)
        app.send_signal(signal.SIGTERM)  # the app's own restart after Update now
        time.sleep(0.4)
        app.send_signal(signal.SIGTERM)  # the launcher's restart for the same update
        app.wait(timeout=20)
        self.assertTrue(done.exists(), "the second stop signal cut the shutdown short, leaving the worker running")


if __name__ == "__main__":
    unittest.main()

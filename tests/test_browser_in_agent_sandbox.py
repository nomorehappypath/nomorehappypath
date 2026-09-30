# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The acceptance browser renders from INSIDE the agent sandbox (2026-09-28).

Certified runs execute inside the agent's Seatbelt sandbox, where `/bin/ps`
(setuid root) cannot run and Chrome cannot build its own inner sandbox. The
reproduction is the real one: the harness's own agent profile around a real
browser on a page that only JavaScript can complete. A blank render fails.

Run:  PYTHONPATH=. python3 -m unittest tests.test_browser_in_agent_sandbox -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import browser_acceptance, platform_support
from tests import environment_support

REPOSITORY = Path(__file__).resolve().parents[1]

CHILD = r'''
import http.server, json, sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from harness import browser_acceptance
PAGE = (b"<html><body><div id=o>static</div><script>document.getElementById('o').textContent='built-by-js-'+(6*7);"
        b"fetch('/probe',{method:'POST',body:JSON.stringify({text:document.getElementById('o').textContent})})</script></body></html>")
sink = {}
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE))); self.end_headers(); self.wfile.write(PAGE)
    def do_POST(self):
        sink["value"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
import ctypes, os
check = ctypes.CDLL("/usr/lib/libSystem.B.dylib").sandbox_check
check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
report = {"sandboxed": check(os.getpid(), None, 0) == 1}
try:
    session = browser_acceptance.launch(f"http://127.0.0.1:{server.server_address[1]}/", Path(tempfile.mkdtemp()))
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and "value" not in sink:
            time.sleep(0.1)
    finally:
        audit = session.close()
    report.update(rendered=sink.get("value", {}).get("text", ""),
                  owned=len(audit["owned_processes_observed"]),
                  new_apps=len(audit["new_app_bundle_processes"]),
                  owner_changes=len(audit["owner_browser_baseline_changes"]))
except Exception as error:
    report["error"] = f"{type(error).__name__}: {error}"
finally:
    server.shutdown()
print(json.dumps(report))
'''


@unittest.skipUnless(sys.platform == "darwin", "the sandboxes under test are macOS Seatbelt")
class BrowserInsideAgentSandboxTests(unittest.TestCase):
    """Each supported agent sandbox, reproduced for real, renders a JavaScript page."""

    def setUp(self):
        environment_support.require_sandbox_exec()
        environment_support.require_loopback()
        try:
            self.binary = browser_acceptance.resolve_binary()
        except (OSError, RuntimeError, ValueError) as error:
            raise unittest.SkipTest(f"no headless browser installed: {error}")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def environment(self) -> dict:
        return {**os.environ, "PYTHONPATH": str(REPOSITORY), "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": str(self.root / "home"), "TMPDIR": str(self.root),
                "HARNESS_BROWSER_BIN": self.binary}

    def assert_rendered(self, completed: subprocess.CompletedProcess) -> None:
        lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
        self.assertTrue(lines, completed.stdout[-800:] + completed.stderr[-800:])
        report = json.loads(lines[-1])
        self.assertTrue(report["sandboxed"], "the reproduction did not run inside the sandbox")
        self.assertNotIn("error", report, report)
        self.assertEqual(report["rendered"], "built-by-js-42", "a blank render is not a pass: " + repr(report))
        self.assertGreaterEqual(report["owned"], 1)
        self.assertEqual((report["new_apps"], report["owner_changes"]), (0, 0), report)

    def test_acceptance_browser_renders_inside_the_real_agent_profile(self):
        confinement = platform_support.agent_confinement()
        argv = confinement.wrap(
            [sys.executable, "-c", CHILD, str(REPOSITORY)],
            [str(self.root), *confinement.temp_paths()],
            store=self.root / "profiles",
        )
        self.assert_rendered(subprocess.run(
            argv, capture_output=True, text=True, timeout=150, env=self.environment(), cwd=self.root,
        ))

    def test_acceptance_browser_renders_inside_codex_workspace_sandbox_with_network(self):
        """Review 2026-09-28 B1: Codex's own sandbox, as the harness runs Codex agents.

        Workspace write limits with network on, built from Codex's documented
        permission profile in a throwaway CODEX_HOME: the owner's Codex
        config is never read or written.
        """
        codex = shutil.which("codex")
        if not codex:
            raise unittest.SkipTest("the Codex CLI is not installed")
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            '[permissions.render]\nextends = ":workspace"\n[permissions.render.network]\nenabled = true\n',
            encoding="utf-8",
        )
        argv = [codex, "sandbox", "-P", "render", "-C", str(self.root), sys.executable, "-c", CHILD, str(REPOSITORY)]
        self.assert_rendered(subprocess.run(
            argv, capture_output=True, text=True, timeout=150,
            env={**self.environment(), "CODEX_HOME": str(codex_home)}, cwd=self.root,
        ))


if __name__ == "__main__":
    unittest.main()

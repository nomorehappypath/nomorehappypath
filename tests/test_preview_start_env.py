# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner's View app runs the project the way the project says, in the environment the review ran in (stranger run 2026-11-05).

A Streamlit weather app had passed its review in the declared test environment; View app then guessed `python3 app.py`, ran it
with the computer's plain Python, and died on "No module named streamlit" — the owner could not try the accepted work from the
page, and the "run it yourself" line repeated the same failing command. Two rules, tested here against a real preview process:
  * a project's own start.sh is preferred over the python guesses, and receives its port in PORT;
  * when the project declares a test environment (test-env-set), the preview runs with that interpreter first on PATH and the
    run-it-yourself line says so; an unready declaration leaves the plain environment in place.

Run:  PYTHONPATH=. python3 -m unittest tests.test_preview_start_env -v
"""
from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from pathlib import Path
from urllib.request import urlopen

from harness import release_preview, test_env
from tests.environment_support import require_loopback
from tests.release_fixture_support import allow_fixture_releases
from tests.test_release_preview import STUDIO_APP, PreviewFixture, _git

setUpModule, tearDownModule = allow_fixture_releases()   # bare release fixtures: production demands a final review (2026-10-24)


class StartScriptAndTestEnvironment(PreviewFixture):
    def setUp(self):
        require_loopback()
        super().setUp()
        (self.workspace / "app.py").write_text(STUDIO_APP)
        (self.workspace / "version.txt").write_text("candidate-studio")
        _git(self.workspace, "add", "app.py", "version.txt")
        _git(self.workspace, "commit", "-qm", "studio candidate")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")

    def start(self) -> dict:
        self.seed_release()
        self.assertEqual(release_preview.request_view(self.root, "TASK"), {"status": "starting"})
        self.supervisor.tick()
        return self.release()["preview"]

    def fake_environment(self, marker: Path) -> Path:
        """A 'virtual environment' whose python3 leaves a marker and then runs the real interpreter."""
        env_root = Path(self.temporary.name) / "venv"
        (env_root / "bin").mkdir(parents=True)
        (env_root / "lib" / "python3.9" / "site-packages" / "streamlit-1.0.dist-info").mkdir(parents=True)
        wrapper = env_root / "bin" / "python3"
        wrapper.write_text(f"#!/bin/sh\ntouch {marker}\nexec {sys.executable} \"$@\"\n")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        (env_root / "bin" / "python").write_text(wrapper.read_text())
        (env_root / "bin" / "python").chmod(wrapper.stat().st_mode)
        return env_root

    def test_the_project_s_own_start_script_is_preferred_and_gets_its_port(self):
        (self.workspace / "start.sh").write_text("#!/bin/sh\nexec python3 app.py\n")
        _git(self.workspace, "add", "start.sh")
        _git(self.workspace, "commit", "-qm", "start script")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")
        preview = self.start()
        self.assertEqual(preview["status"], "ready", preview)
        self.assertEqual(preview["command"], "sh start.sh")
        with urlopen(preview["url"], timeout=5) as response:
            served = json.loads(response.read())
        self.assertEqual(served["version"], "candidate-studio")
        self.assertEqual(served["port"], str(preview["url"].rsplit(":", 1)[1].strip("/")))

    def test_the_declared_test_environment_runs_the_preview(self):
        marker = Path(self.temporary.name) / "venv-python-was-used"
        env_root = self.fake_environment(marker)
        test_env.save(self.root, {"python": str(env_root / "bin" / "python3"), "workdir": "."})
        preview = self.start()
        self.assertEqual(preview["status"], "ready", preview)
        self.assertEqual(preview["command"], "python3 app.py")
        self.assertTrue(marker.exists(), "the declared interpreter was not the one that started the preview")

    def test_the_run_it_yourself_line_names_the_declared_interpreter(self):
        env_root = self.fake_environment(Path(self.temporary.name) / "unused-marker")
        test_env.save(self.root, {"python": str(env_root / "bin" / "python3"), "workdir": "."})
        (self.workspace / "app.py").write_text("import sys\nsys.exit(1)\n")
        _git(self.workspace, "commit", "-qam", "a candidate that dies at once")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")
        preview = self.start()
        self.assertEqual(preview["status"], "failed", preview)
        self.assertIn(f"PATH={env_root / 'bin'}:$PATH PORT=", preview["run_yourself"])
        self.assertTrue(preview["run_yourself"].endswith(" python3 app.py"))

    def test_a_failed_start_never_stores_a_declared_secret(self):
        """Codex R1-1 (2026-11-05): a file-sourced variable (a URL with a password) repeated by the candidate's own output
        must not reach the board record that agents read."""
        env_root = self.fake_environment(Path(self.temporary.name) / "unused-marker")
        secrets_file = Path(self.temporary.name) / "owner.env"
        secrets_file.write_text("SERVICE_URL=https://preview-user:review-only-fake-credential-9fc2@service.invalid/private\n")
        test_env.save(self.root, {"python": str(env_root / "bin" / "python3"), "workdir": ".",
                                  "env": {"SERVICE_URL": {"file": str(secrets_file), "key": "SERVICE_URL"}}})
        (self.workspace / "app.py").write_text(
            "import os, sys\nraise RuntimeError('Service connection rejected: ' + os.environ['SERVICE_URL'])\n")
        _git(self.workspace, "commit", "-qam", "a candidate that prints its secret and dies")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")
        preview = self.start()
        self.assertEqual(preview["status"], "failed", preview)
        stored = json.dumps(preview)
        self.assertNotIn("review-only-fake-credential-9fc2", stored)
        self.assertNotIn("preview-user:review-only", stored)
        self.assertIn("[hidden]", preview["log_tail"])

    def test_an_unready_declaration_leaves_the_plain_environment(self):
        test_env.save(self.root, {"python": str(Path(self.temporary.name) / "gone" / "bin" / "python3"), "workdir": "."})
        preview = self.start()
        self.assertEqual(preview["status"], "ready", preview)
        self.assertEqual(preview["command"], "python3 app.py")
        self.assertEqual(release_preview.detect_start(self.workspace)["command"], "python3 app.py")


if __name__ == "__main__":
    unittest.main()

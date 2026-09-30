# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The reviewer CLI actually receives the final-stage rule (owner, 2026-09-30).

Runs the real `scripts/run_managed_agent.sh` for an Independent Reviewer with a
fake CLI that records the prompt it was launched with. Pinning the directive
text proves the file says it; this proves the reviewer is told it.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from harness import control, project_context, project_registry

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_managed_agent.sh"
SESSION_ID = "claude_reviewer-s1"


class ReviewerLaunchPromptTests(unittest.TestCase):
    def test_launched_reviewer_is_told_to_walk_the_whole_app_and_say_task_done(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            code, data, workspace, home = (base / name for name in ("project", "data", "workspace", "home"))
            for folder in (code, data, workspace, home):
                folder.mkdir()
            project_registry.save(home, {
                "version": project_registry.REGISTRY_VERSION,
                "projects": [{"id": "p1", "name": "project", "code_root": str(code),
                              "data_root": str(data), "workspace_root": str(workspace)}],
            })
            context = project_context.context_from_roots(
                code_root=code, data_root=data, workspace_root=workspace)
            control.restore_missing_resume_session(context, SESSION_ID, "claude_reviewer")
            captured = data / "PROMPT"
            fake = base / "fake-claude"
            fake.write_text(f'#!/bin/sh\nfor a; do last="$a"; done\nprintf "%s" "$last" > "{captured}"\n',
                            encoding="utf-8")
            fake.chmod(0o755)
            env = dict(os.environ, HARNESS_CLAUDE_BIN=str(fake))
            env.pop("PYTHONPATH", None)
            done = subprocess.run(
                ["bash", str(RUNNER), "--root", str(code), "--session-id", SESSION_ID,
                 "--kind", "claude_reviewer", "--data-root", str(data),
                 "--workspace-root", str(workspace), "--python", sys.executable,
                 "--manager-home", str(home)],
                capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=120)
            self.assertTrue(captured.exists(), done.stderr)
            prompt = " ".join(captured.read_text(encoding="utf-8").split())
        self.assertIn("MODE: Independent Reviewer.", prompt)
        for phrase in (
            "Final stage: the whole app, end to end",
            "`TASK DONE: YES — <expected result> vs <what happened>`",
            "PASS always means TASK DONE: YES",
            "Do not cut corners",
            "never hunt boundaries that never or only rarely happen",
        ):
            self.assertIn(phrase, prompt)


if __name__ == "__main__":
    unittest.main()

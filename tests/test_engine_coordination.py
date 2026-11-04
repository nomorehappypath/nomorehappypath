# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""engine/scripts coordination CLIs: a lock release is scoped to one task (P8) and a claim can be
completed or released only by the agent that holds it (P7)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "engine" / "scripts"


class EngineCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        (self.repo / ".agents").mkdir(parents=True)

    def run_script(self, name: str, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), *arguments],
            cwd=self.repo, capture_output=True, text=True, timeout=60,
        )

    def locks(self) -> list[dict]:
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted((self.repo / ".agents" / "locks").glob("*.json"))]

    # ---- P8 ----
    def test_unlock_releases_only_the_lock_of_the_task_being_finished(self):
        self.assertEqual(self.run_script("agent_coord.py", "lock", "--agent", "A", "--task", "T1", "--files", "payment.py").returncode, 0)
        self.assertEqual(self.run_script("agent_coord.py", "lock", "--agent", "A", "--task", "T2", "--files", "README.md").returncode, 0)
        done = self.run_script("agent_coord.py", "unlock", "--agent", "A", "--task", "T2")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        remaining = self.locks()
        self.assertEqual([lock["task"] for lock in remaining], ["T1"], "the still-open task's lock must survive")
        # Another agent is still refused on the file that remained locked.
        refused = self.run_script("agent_coord.py", "lock", "--agent", "B", "--task", "T9", "--files", "payment.py")
        self.assertEqual(refused.returncode, 2)

    def test_unlock_without_a_task_is_refused_and_removes_nothing(self):
        self.assertEqual(self.run_script("agent_coord.py", "lock", "--agent", "A", "--task", "T1", "--files", "payment.py").returncode, 0)
        refused = self.run_script("agent_coord.py", "unlock", "--agent", "A")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--task", refused.stderr + refused.stdout)
        self.assertEqual([lock["task"] for lock in self.locks()], ["T1"])

    # ---- P7 ----
    def register(self, vendor: str) -> str:
        completed = self.run_script("agent_registry.py", "register", "--vendor", vendor, "--role", "implementer")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def post_and_claim(self, signature: str) -> str:
        posted = self.run_script("claim.py", "post", "--task", "T1", "--role", "implementer")
        self.assertEqual(posted.returncode, 0, posted.stderr)
        item = posted.stdout.strip()
        claimed = self.run_script("claim.py", "claim", "--item", item, "--signature", signature)
        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        return item

    def item(self, item_id: str) -> dict:
        return json.loads((self.repo / ".agents" / "queue" / f"{item_id}.json").read_text(encoding="utf-8"))

    def test_complete_and_release_require_the_agent_that_holds_the_claim(self):
        holder, other = self.register("Vendor One"), self.register("Vendor Two")
        item = self.post_and_claim(holder)
        for command in ("complete", "release"):
            with self.subTest(command=command, caller="nobody"):
                refused = self.run_script("claim.py", command, "--item", item)
                self.assertNotEqual(refused.returncode, 0, "a call with no agent identity must be refused")
                self.assertEqual(self.item(item)["status"], "claimed")
            with self.subTest(command=command, caller="other agent"):
                refused = self.run_script("claim.py", command, "--item", item, "--signature", other)
                self.assertNotEqual(refused.returncode, 0, "another agent must not finish or release this claim")
                self.assertEqual(self.item(item)["status"], "claimed")
                self.assertEqual(self.item(item)["claimed_by"], holder)
        released = self.run_script("claim.py", "release", "--item", item, "--signature", holder)
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertEqual(self.item(item)["status"], "open")
        self.assertEqual(self.run_script("claim.py", "claim", "--item", item, "--signature", holder).returncode, 0)
        completed = self.run_script("claim.py", "complete", "--item", item, "--signature", holder)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.item(item)["status"], "done")


if __name__ == "__main__":
    unittest.main()

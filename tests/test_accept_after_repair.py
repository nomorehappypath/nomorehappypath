# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A finished repair can be accepted without a terminal command (task 2026-11-08_accept-after-repair, R1–R3).

Codex's whole-app walk on v0.19.1 (garden project): the repair passed its final review at 7f1779d5, but the owner's attached copy for
"app.py serves the updated heading text, paragraph unchanged" was an earlier note, so `owner_artifacts_readable` failed, the release
held, and the owner saw a `clear-hold` command instead of the Accepted button. The replay here uses Codex's recorded project bundle
and the recorded owner attachment: the board refreshes the owner's copy from the reviewed commit by itself, the check passes, a second
run changes nothing, and a document with no committed source keeps its recorded problem."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, cto

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "accept_after_repair"   # Codex's recorded garden case, copied beside the test
BUNDLE = FIXTURES / "garden-project.bundle"
RECORDED = FIXTURES / "recorded-release-checks.json"
STALE_COPY = FIXTURES / "owner-copy-before-repair.txt"
TASK = "garden-heading-update"
REVIEWED = "7f1779d557d0134598b057f709ac5119d3d0cd68"
DELIVERABLE = "app.py serves the updated heading text, paragraph unchanged"


class StaleOwnerCopyIsRefreshedFromTheReviewedCommit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.repo = base / "garden"
        subprocess.run(["git", "clone", "-q", str(BUNDLE), str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "fetch", "-q", str(BUNDLE), "refs/heads/harness/tasks/garden-heading-update/task:refs/heads/task"],
                       check=True, capture_output=True)
        self.root = self.repo / ".harness"
        self.root.mkdir()
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", session_id="cto-session")
        self.builder = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="builder-session")
        recorded = json.loads(RECORDED.read_text())
        self.stale = next(d for d in recorded["owner_documents"] if d["deliverable"] == DELIVERABLE)
        # the owner's recorded attachment, as the mapping saved it (an earlier note, not the reviewed file)
        self.stale_file = self.root / "data" / "evidence" / "uploads" / "ce301a95e38776223f75" / Path(self.stale["path"]).name
        self.stale_file.parent.mkdir(parents=True)
        shutil.copyfile(STALE_COPY, self.stale_file)
        self.assertEqual(hashlib.sha256(self.stale_file.read_bytes()).hexdigest(), self.stale["sha256"], "the recorded attachment is replayed byte for byte")
        contract.create_contract(self.root, TASK, "Update the garden page heading.", [DELIVERABLE, "A note with no committed file"])
        contract.add_evidence(self.root, TASK, DELIVERABLE, [str(self.stale_file)], for_owner=True)
        with board.locked_state(self.root, operation="test") as state:
            state["agents"][self.builder["id"]].update({"task": TASK, "active": False, "status": "done"})
            state.setdefault("task_owner_directions", {})[TASK] = "Update the heading."
            state.setdefault("task_repositories", {})[TASK] = str(self.repo)
            state.setdefault("task_workspaces", {})[TASK] = str(self.repo)
            state.setdefault("delivery_plans", {})[TASK] = {"mode": "atomic", "structure_revision": 0, "subtasks": {}}
            state["qa_requests"]["review-garden-heading-update-final-03"] = {
                "id": "review-garden-heading-update-final-03", "task": TASK, "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance",
                "cycle": 3, "status": "passed", "developer_id": self.builder["id"], "requested_at": "2026-11-08T06:40:00+00:00",
                "completed_at": "2026-11-08T06:50:00+00:00", "subtask": "", "chunk": "final", "claimed_by": "", "review_wait_started_at": "",
                "structure_revision": 0, "reviewed_commit": REVIEWED, "result": "passed", "result_summary": "TASK DONE: YES",
                "owner_documents": [{"deliverable": DELIVERABLE, "sha256": "not-the-attachment"}], "evidence": "/evidence.txt",
            }

    def state(self):
        return board.snapshot(self.root)

    def committed_app_py(self) -> str:
        shown = subprocess.run(["git", "-C", str(self.repo), "show", f"{REVIEWED}:app.py"], check=True, capture_output=True)
        return hashlib.sha256(shown.stdout).hexdigest()

    def test_the_recorded_case_is_refreshed_passes_and_a_second_run_changes_nothing(self):
        st = self.state()
        review = st["qa_requests"]["review-garden-heading-update-final-03"]
        ok, problems, _ = cto.owner_artifacts_readable(self.root, st, TASK, review)
        self.assertFalse(ok, "Codex's recorded failure is reproduced first")
        self.assertTrue(any("earlier copy" in p or "not what the reviewer saw" in p for p in problems), problems)
        refreshed = board.refresh_owner_documents(self.root, TASK)
        self.assertEqual(len(refreshed), 1)
        self.assertEqual((refreshed[0]["file"], refreshed[0]["commit"], refreshed[0]["sha256"]), ("app.py", REVIEWED, self.committed_app_py()))
        st = self.state()
        ok, problems, manifest = cto.owner_artifacts_readable(self.root, st, TASK, review)
        self.assertTrue(ok, problems)
        latest = next(d for d in manifest if d["deliverable"] == DELIVERABLE and d["latest"])
        self.assertEqual(latest["sha256"], self.committed_app_py(), "the owner opens exactly the reviewed bytes")
        self.assertEqual(Path(latest["path"]).read_bytes(), subprocess.run(["git", "-C", str(self.repo), "show", f"{REVIEWED}:app.py"], check=True, capture_output=True).stdout)
        self.assertIn("owner_document_refreshed", [e["kind"] for e in st["events"]])
        # the old copy is kept as history, never served as the latest
        links = board.document_deliverable_links(self.root, TASK, st)
        self.assertEqual([l["sha256"] for l in links if l["deliverable"] == DELIVERABLE and not l["latest"]], [self.stale["sha256"]])
        # idempotent: nothing to refresh any more
        self.assertEqual(board.refresh_owner_documents(self.root, TASK), [])
        self.assertEqual(len([e for e in self.state()["events"] if e["kind"] == "owner_document_refreshed"]), 1)
        # main is untouched by the refresh: Accept is the only thing that moves it
        main = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "main"], check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(main, "1ecb3b6316bf2005649f661836f81ac74e46c15a")

    def test_a_document_with_no_committed_source_keeps_its_recorded_problem(self):
        note = self.root / "note.txt"
        note.write_text("A note the reviewer did not see.\n")
        contract.add_evidence(self.root, TASK, "A note with no committed file", [str(note)], for_owner=True)
        before = self.state()
        refreshed = board.refresh_owner_documents(self.root, TASK)
        self.assertEqual([r["deliverable"] for r in refreshed], [DELIVERABLE], "only the document with a committed source is refreshed")
        review = before["qa_requests"]["review-garden-heading-update-final-03"]
        ok, problems, _ = cto.owner_artifacts_readable(self.root, self.state(), TASK, review)
        self.assertFalse(ok)
        self.assertTrue(all("A note with no committed file" in p for p in problems), problems)
        self.assertFalse(any("clear-hold" in p for p in problems), "the recorded problem, never a terminal command")


if __name__ == "__main__":
    unittest.main()

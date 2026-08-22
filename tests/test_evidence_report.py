# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import board, evidence_report
from harness.project_context import ProjectContext


class EvidenceReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        (self.root / "product.txt").write_text("candidate\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "product.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "baseline"], check=True)

    def _seed(self, *, backwards=False):
        base = datetime(2026, 8, 18, tzinfo=timezone.utc)
        with board.locked_state(self.root) as state:
            state["events"].append({"kind": "task_begun", "task": "T", "at": base.isoformat(), "sequence": 1, "agent_id": "dev", "role": "engineering"})
            state["qa_requests"]["r1"] = {
                "id": "r1", "task": "T", "phase": "final_acceptance", "status": "passed",
                "stage": "independent_review", "subtask": "", "chunk": "",
                "cycle": 1, "developer_id": "dev", "claimed_by": "qa",
                "review_wait_started_at": "", "structure_revision": 0,
                "requested_at": (base + timedelta(seconds=20)).isoformat(),
                "claimed_at": (base + timedelta(seconds=10 if backwards else 30)).isoformat(),
                "completed_at": (base + timedelta(seconds=40)).isoformat(),
                "command_executions": [{"execution_identity": "exact-a", "duration_seconds": 2.0}],
            }

    def test_report_is_deterministic_board_owned_and_candidate_unchanged(self):
        self._seed()
        before = subprocess.check_output(["git", "-C", str(self.root), "status", "--porcelain=v1"], text=True)
        first = evidence_report.generate(self.root, "T")
        second = evidence_report.generate(self.root, "T")
        after = subprocess.check_output(["git", "-C", str(self.root), "status", "--porcelain=v1"], text=True)
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(Path(first["path"]).read_bytes(), Path(second["path"]).read_bytes())
        self.assertEqual(before, after)
        self.assertIn("/.harness/board/generated-reports/", first["path"])

    def test_backwards_interval_is_named_not_clipped_to_a_healthy_number(self):
        self._seed(backwards=True)
        result = evidence_report.generate(self.root, "T")
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("Unavailable: request r1", text)
        self.assertIn("claimed_at", text)
        self.assertIn("requested_at", text)

    def test_torn_corrupt_and_incomplete_event_records_fail_closed(self):
        self._seed()
        path = board.board_dir(self.root) / "events.jsonl"
        for payload, message in (
            (b'{"kind":"task_begun"', "torn partial"),
            (b'not-json\n', "record 1 is corrupt"),
            (json.dumps({"kind": "task_begun"}).encode() + b"\n", "record 1 is incomplete"),
        ):
            path.write_bytes(payload)
            with self.assertRaisesRegex(ValueError, message):
                evidence_report.generate(self.root, "T")

    def test_existing_content_addressed_report_tamper_is_rejected(self):
        self._seed()
        result = evidence_report.generate(self.root, "T")
        Path(result["path"]).write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "conflicts with its digest"):
            evidence_report.generate(self.root, "T")

    def test_external_project_report_resolves_relative_evidence_from_code_root(self):
        base = self.root / "managed"
        context = ProjectContext(
            base / "code",
            base / "private-data",
            base / "workspaces",
        )
        context.code_root.mkdir(parents=True)
        evidence = context.code_root / "review.md"
        evidence.write_text("Scenario: S1\nCommand: python3 -m unittest\n", encoding="utf-8")
        with board.locked_state(context) as state:
            state["events"].append({
                "kind": "task_begun", "task": "T", "at": "2026-08-18T00:00:00+00:00",
                "sequence": 1, "agent_id": "dev", "role": "engineering",
            })
            state["qa_requests"]["r1"] = {
                "id": "r1", "task": "T", "phase": "final_acceptance", "status": "passed",
                "stage": "independent_review", "subtask": "", "chunk": "", "cycle": 1,
                "developer_id": "dev", "claimed_by": "qa", "structure_revision": 0,
                "requested_at": "2026-08-18T00:00:01+00:00",
                "review_wait_started_at": "2026-08-18T00:00:01+00:00",
                "evidence": "review.md",
                "command_executions": [{
                    "execution_identity": "exact-a", "duration_seconds": 2.0,
                }],
            }

        result = evidence_report.build(context, "T")

        self.assertEqual(result["metrics"]["duplicates"]["requests_with_evidence"], 1)
        self.assertEqual(result["metrics"]["duplicates"]["distinct_commands"], 1)


if __name__ == "__main__":
    unittest.main()

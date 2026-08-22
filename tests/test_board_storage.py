# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import json
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, board_viewer, project_memory


class BoardStorageTests(unittest.TestCase):
    def test_historical_ledger_recovers_from_exact_reviewed_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Harness"], cwd=root, check=True)
            (root / ".gitignore").write_text(".harness/\n")
            docs = root / "docs"; docs.mkdir()
            ledger = docs / "history.md"
            ledger.write_text(
                "| ID | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
                "|---|---|---|---|---|---|\n"
                "| S-HISTORY | reviewed failure scenario | `python3 -m unittest test_history` | failure remains visible | PASS: exact reviewed bytes | PASS |\n"
            )
            subprocess.run(["git", "add", ".gitignore", "docs/history.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "reviewed ledger"], cwd=root, check=True)
            reviewed = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            digest = hashlib.sha256(ledger.read_bytes()).hexdigest()
            ledger.write_text("mutable cycle two ledger\n")
            challenge = root / ".harness" / "reviews" / "challenge.md"
            challenge.parent.mkdir(parents=True)
            challenge.write_text(
                "| ID | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
                "|---|---|---|---|---|---|\n"
                "| S-REVIEW | independent failure remains visible | `python3 -m unittest test_review_history` | failure remains visible | PASS: exact reviewer bytes | PASS |\n"
            )
            challenge_digest = hashlib.sha256(challenge.read_bytes()).hexdigest()
            with board.locked_state(root) as state:
                state["archive"].append({"kind": "qa_request", "archived_at": board.now(), "value": {
                    "id": "review-HISTORY-RECOVERY-final-01", "task": "HISTORY-RECOVERY", "cycle": 1,
                    "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance", "status": "failed",
                    "developer_id": "development-old", "ledger": "docs/history.md",
                    "ledger_sha256": digest, "challenge_ledger": ".harness/reviews/challenge.md",
                    "challenge_ledger_sha256": challenge_digest, "reviewed_commit": reviewed,
                    "requested_at": board.now(), "completed_at": board.now(),
                }})
                state["releases"]["HISTORY-RECOVERY"] = {"task": "HISTORY-RECOVERY", "status": "VISUAL_TEST_REQUIRED", "recorded_at": board.now(), "cto_id": "cto"}
                state["release_decisions"]["HISTORY-RECOVERY"] = {"task": "HISTORY-RECOVERY", "decision": "accepted", "recorded_at": board.now()}
            migration = board.certify_legacy_review_ledgers(root)
            self.assertEqual(migration["migrated_count"], 2)
            historical = board.historical_snapshot(root)
            request = next(entry["value"] for entry in historical["archive"] if entry["value"]["id"] == "review-HISTORY-RECOVERY-final-01")
            certified = request["certified_artifacts"]["delivery_ledger"]
            self.assertEqual(certified["sha256"], digest)
            self.assertEqual(request["certified_artifacts"]["challenge_ledger"]["sha256"], challenge_digest)
            self.assertEqual(Path(certified["path"]).read_text(), subprocess.check_output(
                ["git", "show", f"{reviewed}:docs/history.md"], cwd=root, text=True,
            ))
            with patch.object(board.git_process, "run", side_effect=AssertionError("history refresh launched Git")):
                item = next(value for value in board_viewer.history_payload(root)["task_history"] if value["task"] == "HISTORY-RECOVERY")
            self.assertEqual(item["test_ledger"]["state"], "available")
            self.assertEqual(item["test_ledger"]["scenarios"][0]["id"], "S-HISTORY")
            self.assertIn("Failed", item["test_ledger"]["source"])

            with patch.object(board.git_process, "run", side_effect=AssertionError("completed migration reran Git")):
                repeated = board.certify_legacy_review_ledgers(root)
            self.assertTrue(repeated["already_complete"])

    def test_legacy_ledger_migration_fails_closed_when_reviewed_bytes_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Harness"], cwd=root, check=True)
            (root / ".gitignore").write_text(".harness/\n")
            docs = root / "docs"; docs.mkdir()
            ledger = docs / "history.md"
            ledger.write_text(
                "| ID | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
                "|---|---|---|---|---|---|\n"
                "| S-OLD | old behavior remains readable | `python3 old.py` | readable | PASS | PASS |\n"
            )
            subprocess.run(["git", "add", ".gitignore", "docs/history.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "reviewed ledger"], cwd=root, check=True)
            reviewed = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            ledger.write_text("changed after review\n")
            with board.locked_state(root) as state:
                state["archive"].append({"kind": "qa_request", "archived_at": board.now(), "value": {
                    "id": "review-MISMATCH-final-01", "task": "MISMATCH", "cycle": 1,
                    "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance", "status": "failed",
                    "developer_id": "development-old", "ledger": "docs/history.md",
                    "ledger_sha256": "0" * 64, "reviewed_commit": reviewed,
                    "requested_at": board.now(), "completed_at": board.now(),
                }})
                state["release_decisions"]["MISMATCH"] = {
                    "task": "MISMATCH", "decision": "accepted", "recorded_at": board.now(),
                }
            migration = board.certify_legacy_review_ledgers(root)
            self.assertEqual(migration["migrated_count"], 0)
            self.assertEqual(migration["unavailable_count"], 1)
            with patch.object(board.git_process, "run", side_effect=AssertionError("history refresh launched Git")):
                item = next(value for value in board_viewer.history_payload(root)["task_history"] if value["task"] == "MISMATCH")
            self.assertEqual(item["test_ledger"]["state"], "unavailable")

    def test_integrity_upgrade_keeps_full_pass_certification_before_history_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.md"
            challenge = root / "challenge.md"
            evidence = root / "result.txt"
            ledger.write_text("delivery ledger\n")
            challenge.write_text("reviewer ledger\n")
            evidence.write_text("review evidence\n")
            with board.locked_state(root) as state:
                state["qa_requests"]["review-UPGRADE-final-01"] = {
                    "id": "review-UPGRADE-final-01", "task": "UPGRADE", "cycle": 1,
                    "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance", "status": "passed",
                    "developer_id": "development-old", "ledger": str(ledger),
                    "ledger_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
                    "challenge_ledger": str(challenge),
                    "challenge_ledger_sha256": hashlib.sha256(challenge.read_bytes()).hexdigest(),
                    "evidence": str(evidence), "requested_at": board.now(), "completed_at": board.now(),
                    "claimed_by": "qa-old", "review_wait_started_at": board.now(),
                }
            result = board.migrate_integrity(root)
            request = board.snapshot(root)["qa_requests"]["review-UPGRADE-final-01"]
            self.assertEqual(request["status"], "passed")
            self.assertEqual(
                set(request["certified_artifacts"]),
                {"delivery_ledger", "challenge_ledger", "result_evidence"},
            )
            self.assertIn("review-UPGRADE-final-01", result["backfill"]["backfilled"])
            self.assertEqual(result["reconciliation"]["invalidated"], [])

    def test_hot_state_is_bounded_and_cold_history_reconstructs_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
            archived = {
                "id": "review-HISTORY-final-01", "task": "HISTORY", "cycle": 1,
                "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance",
                "status": "passed", "developer_id": "development-old",
                "requested_at": board.now(), "completed_at": board.now(),
                "evidence": "certified-evidence-that-must-survive",
            }
            with board.locked_state(root) as state:
                state["archive"].append({"kind": "qa_request", "archived_at": board.now(), "value": archived})
                state["agents"]["development-old"] = {
                    "id": "development-old", "role": "development", "task": "HISTORY",
                    "active": False, "status": "done", "status_note": "accepted",
                    "cursor": 0, "poll_counter": 0, "last_status_at": board.now(),
                }
                state["release_decisions"]["HISTORY"] = {
                    "task": "HISTORY", "decision": "accepted", "recorded_at": board.now(),
                }
                for number in range(5000):
                    board._event(state, "simulated_board_activity", None, {
                        "task": "LOAD", "message": f"bounded event {number}",
                    })

            hot_path = root / ".harness" / "board" / "state.json"
            hot = json.loads(hot_path.read_text())
            self.assertLessEqual(len(hot["events"]), board.HOT_EVENT_WINDOW)
            self.assertLess(len(json.dumps(hot["events"], separators=(",", ":"))), board.HOT_EVENT_BYTES + 1000)
            self.assertNotIn("development-old", hot["agents"])
            self.assertEqual(hot["archive"], [])
            self.assertIn(archived["id"], hot["qa_request_index"])
            self.assertLess(hot_path.stat().st_size, 200_000)

            historical = board.historical_snapshot(root)
            recovered = [entry["value"] for entry in historical["archive"]]
            self.assertEqual(recovered[0]["evidence"], archived["evidence"])
            self.assertIn("development-old", historical["agents"])
            self.assertGreaterEqual(len(historical["events"]), 5000)

            # Repeated writes remain bounded rather than growing with history.
            # Count the incremental work instead of asserting wall-clock speed,
            # which varies with host load and cannot prove bounded complexity.
            sizes = []
            mirrored_sequences = []
            mirrored_sources = []
            full_mirror_copies = []
            original_append = project_memory.append_record
            original_copy2 = project_memory.shutil.copy2
            original_copytree = project_memory.shutil.copytree

            def tracked_append(memory_root, event):
                mirrored_sequences.append(event["sequence"])
                return original_append(memory_root, event)

            def tracked_copy2(source, destination, *args, **kwargs):
                mirrored_sources[-1].append(Path(source).name)
                return original_copy2(source, destination, *args, **kwargs)

            def tracked_copytree(source, destination, *args, **kwargs):
                full_mirror_copies[-1] += 1
                return original_copytree(source, destination, *args, **kwargs)

            with patch.object(project_memory, "append_record", side_effect=tracked_append), \
                    patch.object(project_memory.shutil, "copy2", side_effect=tracked_copy2), \
                    patch.object(project_memory.shutil, "copytree", side_effect=tracked_copytree):
                for number in range(20):
                    mirrored_sources.append([])
                    full_mirror_copies.append(0)
                    with board.locked_state(root) as state:
                        board._event(state, "post_load_mutation", None, {"task": "LOAD", "message": str(number)})
                    sizes.append(hot_path.stat().st_size)
            self.assertLess(max(sizes) - min(sizes), 20_000)
            self.assertEqual(len(mirrored_sequences), 20)
            self.assertEqual(
                mirrored_sequences,
                list(range(mirrored_sequences[0], mirrored_sequences[0] + 20)),
            )
            record_copy_counts = [
                sum(name.endswith(".json") for name in sources)
                for sources in mirrored_sources
            ]
            self.assertEqual(full_mirror_copies, [0] * 20)
            self.assertEqual(record_copy_counts, [1] * 20)
            self.assertEqual(
                [sources.count("index.md") for sources in mirrored_sources],
                [1] * 20,
            )

    def test_lagging_cursor_gets_explicit_truncation_signal_and_durable_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
            with board.locked_state(root) as state:
                state["agents"][agent["id"]]["cursor"] = 1
                for number in range(900):
                    board._event(state, "qa_result" if number == 10 else "noise", None, {
                        "task": "CURSOR", "result": "failed" if number == 10 else "",
                        "message": f"event {number}",
                    })
            result = board.poll(root, agent["id"])
            self.assertTrue(result["history_truncated"])
            signal = result["events"][0]
            self.assertEqual(signal["kind"], "history_truncated")
            self.assertTrue(Path(signal["durable_log"]).is_file())
            self.assertIn('"result": "failed"', Path(signal["durable_log"]).read_text())

    def test_snapshot_is_read_only_and_dashboard_excludes_cold_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
            board_path = root / ".harness" / "board" / "BOARD.md"
            before = board_path.stat().st_mtime_ns
            for _ in range(5):
                board.snapshot(root)
            self.assertEqual(board_path.stat().st_mtime_ns, before)

            with board.locked_state(root) as state:
                state["agents"][agent["id"]].update({"active": False, "status": "offline"})
                state["archive"].append({"kind": "qa_request", "archived_at": board.now(), "value": {
                    "id": "review-COLD-final-01", "task": "COLD", "cycle": 1,
                    "phase": "final_acceptance", "stage": board.INDEPENDENT_REVIEW,
                    "status": "passed", "requested_at": board.now(), "completed_at": board.now(),
                }})
                state["releases"]["COLD"] = {"task": "COLD", "status": "VISUAL_TEST_REQUIRED", "recorded_at": board.now(), "cto_id": "cto"}
                state["release_decisions"]["COLD"] = {"task": "COLD", "decision": "accepted", "recorded_at": board.now()}
            dashboard = board_viewer.dashboard_payload(root)
            encoded = json.dumps(dashboard, separators=(",", ":")).encode()
            self.assertLess(len(encoded), 50_000)
            self.assertNotIn("task_history", dashboard)
            self.assertNotIn(agent["id"], dashboard["state"]["agents"])
            self.assertEqual(dashboard["state"]["archive"], [])
            history = board_viewer.history_payload(root)
            self.assertIn("COLD", {item["task"] for item in history["task_history"]})


if __name__ == "__main__":
    unittest.main()

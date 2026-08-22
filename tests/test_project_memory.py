# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Phase 2 project-memory storage simulations (spec §5.6 and §8.2)."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, control, project_manager, project_memory, project_registry
from harness.project_context import ProjectContext


class ProjectMemoryStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.context = ProjectContext(
            code_root=base / "code",
            data_root=base / "managed" / "data",
            workspace_root=base / "managed" / "workspaces",
        )
        self.context.code_root.mkdir(parents=True)

    def event(self, sequence: int, kind: str = "status_update", task: str = "TASK"):
        return {
            "sequence": sequence,
            "at": f"2026-08-15T15:00:{sequence:02d}+00:00",
            "kind": kind,
            "agent_id": "engineering-1",
            "role": "engineering",
            "task": task,
            "message": f"material change {sequence}",
        }

    def state(self, events, *, accepted=False):
        releases = {
            "TASK": {
                "decision": "accepted",
                "recorded_at": "2026-08-15T16:00:00+00:00",
            },
        } if accepted else {}
        return {
            "next_event": max((event["sequence"] for event in events), default=0) + 1,
            "events": list(events),
            "task_owner_directions": {"TASK": "Build durable project memory."},
            "release_decisions": releases,
            "agents": {},
        }

    def test_index_and_append_only_records_round_trip(self):
        project_memory.initialize(
            self.context, project_name="NoMoreHappyPath",
            description="Build the next-generation development harness.",
        )
        first = self.event(1)
        project_memory.sync_events(
            self.context, self.state([first]), [first],
        )
        first_path = project_memory.records_dir(self.context) / "000000001-status_update.json"
        original = first_path.read_bytes()

        second = self.event(2, "task_brief_updated")
        project_memory.sync_events(
            self.context, self.state([first, second]), [second],
        )

        self.assertEqual(first_path.read_bytes(), original, "earlier detail records are immutable")
        self.assertEqual(len(list(project_memory.records_dir(self.context).glob("*.json"))), 2)
        index = project_memory.load_index(self.context)
        self.assertEqual(index["authority"], "board")
        self.assertEqual(index["answers"]["project_about"],
                         "Build the next-generation development harness.")
        self.assertLess(project_memory.index_path(self.context).stat().st_size, 32_000)

    def test_recovery_replay_skips_identical_records_and_continues_sync(self):
        project_memory.initialize(self.context, project_name="Replay safe")
        first = self.event(1, "owner_direction_received")
        second = self.event(2, "development_complete")
        project_memory.sync_events(self.context, self.state([first]), [first])
        first_path = (
            project_memory.records_dir(self.context)
            / "000000001-owner_direction_received.json"
        )
        original = first_path.read_bytes()

        # Recovery replays the existing board event before reaching newer work.
        project_memory.sync_events(
            self.context,
            self.state([first, second]),
            [first, second],
        )

        self.assertEqual(first_path.read_bytes(), original)
        self.assertTrue(
            (
                project_memory.records_dir(self.context)
                / "000000002-development_complete.json"
            ).is_file(),
            "an idempotently skipped replay must not abort later memory updates",
        )
        resumed = project_memory.resume_context(
            self.context, board_state=self.state([first, second], accepted=True),
        )
        self.assertEqual(resumed["authority"], "board")
        self.assertEqual(resumed["board_sequence"], 2)
        self.assertEqual(resumed["answers"]["remaining_work"], "No recorded task remains.")

    def test_recovery_replay_rejects_changed_payload_for_existing_sequence(self):
        project_memory.initialize(self.context, project_name="Replay conflict")
        first = self.event(1, "status_update")
        project_memory.sync_events(self.context, self.state([first]), [first])
        first_path = project_memory.records_dir(self.context) / "000000001-status_update.json"
        original = first_path.read_bytes()
        changed = dict(first, message="different recovered payload")
        second = self.event(2, "development_complete")
        third = self.event(3, "review_requested")

        with self.assertRaisesRegex(
            ValueError, "append-only memory record conflicts with board sequence 1",
        ):
            project_memory.sync_events(
                self.context,
                self.state([changed, second, third]),
                [changed, second, third],
            )

        self.assertEqual(
            first_path.read_bytes(), original,
            "a conflicting recovery replay must never replace established evidence",
        )
        index = project_memory.load_index(self.context)
        present = {
            path.name for path in project_memory.records_dir(self.context).glob("*.json")
        }
        self.assertEqual(
            set(index["record_refs"]) - present, set(),
            "a loud conflict must not abort later records promised by the index",
        )
        self.assertIn("000000002-development_complete.json", present)
        self.assertIn("000000003-review_requested.json", present)

    def test_resume_reads_only_targeted_records_never_whole_history(self):
        project_memory.initialize(self.context, project_name="Targeted")
        events = [self.event(sequence) for sequence in range(1, 13)]
        project_memory.sync_events(self.context, self.state(events), events)
        unreferenced = project_memory.records_dir(self.context) / "999999999-unreferenced.json"
        unreferenced.write_text("not-json")

        original = project_memory._read_record
        with patch.object(project_memory, "_read_record", wraps=original) as reader:
            resumed = project_memory.resume_context(self.context)

        self.assertEqual(len(resumed["records"]), project_memory.RECENT_RECORD_LIMIT)
        self.assertEqual(
            [call.args[1] for call in reader.call_args_list],
            resumed["loaded_record_refs"],
        )
        self.assertNotIn(unreferenced.name, resumed["loaded_record_refs"])

    def test_resume_degrades_when_targeted_records_are_damaged_or_missing(self):
        project_memory.initialize(self.context, project_name="Damage tolerant")
        events = [self.event(1), self.event(2, "development_complete")]
        project_memory.sync_events(self.context, self.state(events), events)
        refs = project_memory.load_index(self.context)["record_refs"]
        first = project_memory.records_dir(self.context) / refs[0]
        second = project_memory.records_dir(self.context) / refs[1]
        first.write_text("not-json")
        second.unlink()

        resumed = project_memory.resume_context(self.context)

        self.assertEqual(set(resumed["answers"]), {
            "project_about", "current_status", "last_task_result", "remaining_work",
        })
        self.assertEqual(
            [record["detail_status"] for record in resumed["records"]],
            ["unavailable", "unavailable"],
        )
        self.assertEqual(
            {record["error_type"] for record in resumed["records"]},
            {"JSONDecodeError", "FileNotFoundError"},
        )

    def test_current_board_state_overrides_stale_memory_narrative(self):
        project_memory.initialize(self.context, project_name="Authority")
        event = self.event(1)
        project_memory.sync_events(self.context, self.state([event]), [event])
        stale = project_memory.resume_context(self.context)
        self.assertIn("without owner acceptance", stale["answers"]["remaining_work"])

        accepted_state = self.state([event], accepted=True)
        current = project_memory.resume_context(self.context, board_state=accepted_state)

        self.assertEqual(current["authority"], "board")
        self.assertEqual(current["answers"]["current_status"], "All recorded tasks are accepted.")
        self.assertEqual(current["answers"]["last_task_result"], "TASK: accepted.")
        self.assertEqual(current["answers"]["remaining_work"], "No recorded task remains.")

    def test_stale_post_commit_sync_cannot_regress_newer_memory_truth(self):
        project_memory.initialize(self.context, project_name="Monotonic")
        older_event = self.event(1)
        newer_event = self.event(2, "owner_release_decision_recorded")
        older_state = self.state([older_event])
        newer_state = self.state([older_event, newer_event], accepted=True)

        # Reproduce the real post-board-lock ordering: the newer commit finishes
        # its memory sync first, then a delayed older commit tries to publish.
        project_memory.sync_board_state(self.context, newer_state, [newer_event])
        project_memory.sync_board_state(self.context, older_state, [older_event])

        index = project_memory.load_index(self.context)
        resumed = project_memory.resume_context(self.context)
        self.assertEqual(index["board_sequence"], 2)
        self.assertEqual(resumed["answers"]["current_status"],
                         "All recorded tasks are accepted.")
        self.assertEqual(resumed["answers"]["last_task_result"], "TASK: accepted.")
        self.assertEqual(resumed["answers"]["remaining_work"], "No recorded task remains.")

    def test_external_backup_restores_self_contained_memory_folder(self):
        project_memory.initialize(
            self.context, project_name="Recoverable", description="Remember the project truthfully.",
        )
        event = self.event(1)
        project_memory.sync_events(self.context, self.state([event]), [event])
        before = project_memory.memory_digest(self.context)
        backup_root = project_memory.external_backup_root(self.context)
        latest = sorted(backup_root.iterdir())[-1]
        self.assertTrue((latest / "index.md").is_file())
        self.assertTrue((latest / "records" / "000000001-status_update.json").is_file())
        self.assertNotIn(self.context.data_root, backup_root.parents)

        shutil.rmtree(project_memory.memory_dir(self.context))
        self.assertTrue(project_memory.restore_latest(self.context))

        self.assertEqual(project_memory.memory_digest(self.context), before)
        restored = project_memory.resume_context(self.context)
        self.assertEqual(restored["answers"]["project_about"], "Remember the project truthfully.")

    def test_same_board_sequence_retry_creates_a_current_external_snapshot(self):
        project_memory.initialize(self.context, project_name="Retryable")
        first = self.event(1, "owner_direction_received")
        second = self.event(2, "development_complete")
        state = self.state([first, second])

        project_memory.sync_events(self.context, state, [first])
        project_memory.sync_events(self.context, state, [second])

        backups = project_memory.external_backup_root(self.context)
        latest = sorted(path for path in backups.iterdir() if path.is_dir())[-1]
        backed_up = sorted(path.name for path in (latest / "records").glob("*.json"))
        live = sorted(path.name for path in project_memory.records_dir(self.context).glob("*.json"))
        self.assertEqual(backed_up, live)
        self.assertEqual(project_memory.memory_digest(self.context),
                         self._digest_folder(latest))

    def test_rejected_oversized_index_never_leaves_orphan_records(self):
        project_memory.initialize(self.context, project_name="Bounded")
        event = self.event(1)

        with self.assertRaisesRegex(ValueError, "compact 32 KiB limit"):
            project_memory.sync_events(
                self.context, self.state([event]), [event],
                project={"name": "Bounded", "description": "x" * 40_000},
            )

        self.assertEqual(list(project_memory.records_dir(self.context).glob("*.json")), [])

    @staticmethod
    def _digest_folder(root: Path) -> str:
        import hashlib
        digest = hashlib.sha256()
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def test_missing_facts_are_explicit_instead_of_guessed(self):
        project_memory.initialize(self.context)

        answers = project_memory.resume_context(self.context)["answers"]

        self.assertEqual(answers, {
            "project_about": "Project purpose is not recorded.",
            "current_status": "No project status is recorded.",
            "last_task_result": "No task or result is recorded.",
            "remaining_work": "Remaining work is not recorded.",
        })


class ProjectMemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_registered_adopted_project_gets_external_initialized_memory(self):
        home = self.base / "manager"
        repository = self.base / "owner-repository"
        repository.mkdir()
        owner_file = repository / "owner.txt"
        owner_file.write_text("owner bytes\n")
        before = owner_file.read_bytes()

        entry = project_registry.register(
            home, "Owner project", repository, kind="adopted",
            description="Deliver the owner's existing application safely.",
        )
        context = project_registry.context_for_entry(entry)

        self.assertEqual(owner_file.read_bytes(), before)
        self.assertFalse((repository / ".harness").exists())
        self.assertTrue(project_memory.index_path(context).is_file())
        self.assertTrue(project_memory.records_dir(context).is_dir())
        self.assertEqual(
            project_memory.resume_context(context)["answers"]["project_about"],
            "Deliver the owner's existing application safely.",
        )
        self.assertTrue(any(project_memory.external_backup_root(context).iterdir()))

    def test_material_board_changes_refresh_cto_memory_and_four_answers(self):
        root = self.base / "project"
        root.mkdir()
        project_memory.initialize(
            root, project_name="Live memory",
            description="Track board truth for the owner.",
        )
        session = control.create(root, "codex_delivery")
        agent = board.register(
            root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(root, session["id"], "Build the memory integration.")
        board.begin_task(root, agent["id"], "MEMORY-TASK")
        before_poll = project_memory.memory_digest(root)

        board.poll(root, agent["id"])
        self.assertEqual(project_memory.memory_digest(root), before_poll,
                         "poll heartbeats are not material project memory")
        board.status(root, agent["id"], "Memory integration is under active QA.")

        active = project_memory.resume_context(root)
        self.assertEqual(active["answers"]["project_about"], "Track board truth for the owner.")
        self.assertIn("MEMORY-TASK", active["answers"]["current_status"])
        self.assertEqual(active["answers"]["last_task_result"],
                         "MEMORY-TASK: no result is recorded yet.")
        self.assertIn("MEMORY-TASK", active["answers"]["remaining_work"])
        latest_record = active["records"][-1]
        self.assertEqual(latest_record["maintained_by"], "cto")
        self.assertEqual(latest_record["board_event"]["kind"], "status_update")

        with board.locked_state(root) as state:
            state["agents"][agent["id"]]["active"] = False
            state.setdefault("releases", {})["MEMORY-TASK"] = {
                "task": "MEMORY-TASK", "status": "VISUAL_TEST_REQUIRED",
                "head_commit": "abc123", "cto_id": "cto-test",
                "recorded_at": board.now(),
            }
        board.record_release_decision(root, "MEMORY-TASK", "accepted")

        accepted = project_memory.resume_context(root)
        self.assertEqual(accepted["answers"]["current_status"],
                         "All recorded tasks are accepted.")
        self.assertEqual(accepted["answers"]["last_task_result"],
                         "MEMORY-TASK: accepted.")
        self.assertEqual(accepted["answers"]["remaining_work"],
                         "No recorded task remains.")

    def test_memory_sync_failure_never_rolls_back_board_and_next_event_retries(self):
        root = self.base / "retry-project"
        root.mkdir()
        project_memory.initialize(root, project_name="Retry integration")
        session = control.create(root, "codex_delivery")
        agent = board.register(
            root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(root, session["id"], "Retry derived memory.")
        board.begin_task(root, agent["id"], "RETRY-TASK")

        with patch.object(project_memory, "sync_board_state", side_effect=OSError("disk unavailable")):
            failed_sync_event = board.status(root, agent["id"], "First status survives memory failure.")

        state = board.snapshot(root)
        self.assertTrue(any(
            event.get("sequence") == failed_sync_event["sequence"]
            for event in state["events"]
        ))
        recovery_log = project_memory.external_backup_root(root) / "MEMORY_RECOVERY.log"
        self.assertIn("disk unavailable", recovery_log.read_text())

        board.status(root, agent["id"], "Second status retries the missed event.")

        records = project_memory.resume_context(root)["records"]
        messages = [record.get("board_event", {}).get("message") for record in records]
        self.assertIn("First status survives memory failure.", messages)
        self.assertIn("Second status retries the missed event.", messages)

    def test_project_open_returns_index_plus_targeted_records_only(self):
        home = self.base / "open-manager"
        code = self.base / "open-project"
        code.mkdir()
        entry = project_registry.register(
            home, "Open project", code,
            description="Resume from compact project memory.",
        )
        context = project_registry.context_for_entry(entry)
        session = control.create(context, "codex_delivery")
        agent = board.register(
            context, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        for index in range(12):
            board.status(context, agent["id"], f"Material update {index}.")

        class FakeProcess:
            pid = 4242
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): return 0

        manager = project_manager.ProjectManager(home, board_port=0)
        original = project_memory._read_record
        with patch.object(project_memory, "_read_record", wraps=original) as reader, patch.object(
            project_manager.subprocess, "Popen", return_value=FakeProcess(),
        ):
            opened = manager.open_project(entry["id"])
            manager.close_project(entry["id"])

        self.assertEqual(opened["memory"]["authority"], "board")
        self.assertEqual(len(opened["memory"]["records"]), project_memory.RECENT_RECORD_LIMIT)
        self.assertEqual(reader.call_count, project_memory.RECENT_RECORD_LIMIT)
        self.assertEqual(
            [call.args[1] for call in reader.call_args_list],
            opened["memory"]["loaded_record_refs"],
        )


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Tests for the record of use: "Skills used" is read from a real Claude or
Codex session record's own tool call/result pairs, never from what an
agent claims about itself — SKILL_LIBRARY_SPEC.md section 3 and S13.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from harness import board, conversation, control, skill_library
from tests.test_requirements_contract import context, delivery_with_direction


def _claude_session_file(control_dir: Path, cli_session_id: str, calls: list[dict]) -> Path:
    """A minimal Claude Code session file: one tool_use/tool_result pair per call.

    Each call: {"id", "input", "output", "ok"} (output may be omitted to
    simulate a call the record never got a result for).
    """
    projects = control_dir / ".claude-store" / "projects" / "proj"
    projects.mkdir(parents=True, exist_ok=True)
    path = projects / f"{cli_session_id}.jsonl"
    lines = []
    for call in calls:
        lines.append(json.dumps({
            "type": "assistant", "timestamp": "2026-11-12T20:00:00Z",
            "message": {"content": [{"type": "tool_use", "id": call["id"], "name": "Read", "input": call["input"]}]},
        }))
        if "output" in call:
            lines.append(json.dumps({
                "type": "user", "timestamp": "2026-11-12T20:00:01Z",
                "message": {"content": [{"type": "tool_result", "tool_use_id": call["id"],
                                          "is_error": not call.get("ok", True),
                                          "content": [{"type": "text", "text": call["output"]}]}]},
            }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class SkillUsageClassificationTests(unittest.TestCase):
    """classify_skill_usage against hand-built event lists: no real CLI needed
    to prove the classification rule itself (the real-record wiring is
    proven separately below, against a real Claude Code session file)."""

    def setUp(self):
        self.skill = skill_library.usable_skills()[3]  # "backend"
        self.agreed = [{"slug": self.skill.slug, "plain_name": self.skill.plain_name, "path": str(self.skill.path)}]
        self.opening = f"name: {self.skill.slug}\ndescription: {self.skill.full_description}\n"

    def test_a_real_read_of_the_skills_own_file_is_opened(self):
        events = [{"path": str(self.skill.path), "command": None, "ok": True, "output": self.opening}]
        result = board.classify_skill_usage(events, self.agreed)
        self.assertEqual(result["by_slug"][self.skill.slug], "opened")

    def test_a_shell_read_of_the_skills_own_file_is_also_opened(self):
        events = [{"path": None, "command": f"cat {self.skill.path}", "ok": True, "output": self.opening}]
        result = board.classify_skill_usage(events, self.agreed)
        self.assertEqual(result["by_slug"][self.skill.slug], "opened")

    def test_a_call_that_only_tests_the_file_exists_is_agreed_not_opened(self):
        events = [{"path": None, "command": f"test -f {self.skill.path}", "ok": True, "output": ""}]
        result = board.classify_skill_usage(events, self.agreed)
        self.assertEqual(result["by_slug"][self.skill.slug], "agreed_not_opened")

    def test_an_agreed_skill_never_touched_is_agreed_not_opened(self):
        result = board.classify_skill_usage([], self.agreed)
        self.assertEqual(result["by_slug"][self.skill.slug], "agreed_not_opened")

    def test_a_failed_read_is_could_not_be_read(self):
        events = [{"path": str(self.skill.path), "command": None, "ok": False, "output": "ENOENT"}]
        result = board.classify_skill_usage(events, self.agreed)
        self.assertEqual(result["by_slug"][self.skill.slug], "could_not_be_read")

    def test_no_usable_record_is_not_known_not_opened_despite_identical_opening_lines(self):
        result = board.classify_skill_usage(None, self.agreed)
        self.assertTrue(result["not_known"])

    def test_reading_a_different_skills_file_is_opened_not_agreed(self):
        other = next(skill for skill in skill_library.usable_skills() if skill.slug != self.skill.slug)
        other_opening = f"name: {other.slug}\ndescription: {other.full_description}\n"
        events = [{"path": str(other.path), "command": None, "ok": True, "output": other_opening}]
        result = board.classify_skill_usage(events, self.agreed)
        self.assertEqual(result["by_slug"][self.skill.slug], "agreed_not_opened")
        self.assertIn(other.plain_name, result["opened_not_agreed"])


class RealSessionRecordTests(unittest.TestCase):
    """The same classification, driven end to end from a real Claude Code
    session file on disk — proving conversation.read_vendor_tool_events
    actually extracts the call/result pair, not just that the classifier
    logic is right in isolation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name)
        self._real_claude_home = conversation.claude_config_dir
        conversation.claude_config_dir = lambda: self.store / ".claude-store"
        self.addCleanup(lambda: setattr(conversation, "claude_config_dir", self._real_claude_home))

    def test_a_real_session_file_showing_the_real_opening_lines_counts_as_opened(self):
        skill = skill_library.usable_skills()[0]
        opening = f"name: {skill.slug}\ndescription: {skill.full_description}\n"
        _claude_session_file(self.store, "cli-abc", [{"id": "t1", "input": {"file_path": str(skill.path)}, "output": opening, "ok": True}])
        events = conversation.read_vendor_tool_events("claude", "cli-abc")
        self.assertIsNotNone(events)
        agreed = [{"slug": skill.slug, "plain_name": skill.plain_name, "path": str(skill.path)}]
        result = board.classify_skill_usage(events, agreed)
        self.assertEqual(result["by_slug"][skill.slug], "opened")

    def test_a_call_with_no_result_in_the_record_leaves_the_skill_not_opened(self):
        skill = skill_library.usable_skills()[1]
        _claude_session_file(self.store, "cli-def", [{"id": "t1", "input": {"file_path": str(skill.path)}}])  # no "output": never gets a result
        events = conversation.read_vendor_tool_events("claude", "cli-def")
        agreed = [{"slug": skill.slug, "plain_name": skill.plain_name, "path": str(skill.path)}]
        result = board.classify_skill_usage(events, agreed)
        self.assertEqual(result["by_slug"][skill.slug], "agreed_not_opened")


class SkillUsageLinesForTaskTests(unittest.TestCase):
    """skill_usage_lines_for_task against a real board + real registered
    agents, proving the per-role composition and the Gemini/not-known cases
    without needing the full card/Go-ahead flow."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))

    def test_no_agent_on_the_task_yet_shows_nothing(self):
        self.assertEqual(board.skill_usage_lines_for_task(self.root, "NO-SUCH-TASK"), [])

    def test_a_builder_with_no_cli_session_recorded_shows_not_known(self):
        agent = delivery_with_direction(self.root, "USAGE-TASK")
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: build a small service.",
                                           skills=[{"name": "backend", "reason": "service code"}])
        proposal = board.snapshot(self.root)["requirement_proposals"]["USAGE-TASK"]
        board.record_requirements_decision(self.root, "USAGE-TASK", "go_ahead", version=proposal["version"],
                                            token=(proposal.get("contract_check") or {}).get("token", ""), skills=["backend"])
        lines = board.skill_usage_lines_for_task(self.root, "USAGE-TASK")
        self.assertTrue(any("not known" in line for line in lines))

    def test_a_gemini_agent_shows_not_available_for_gemini(self):
        board.register(self.root, "qa", "USAGE-TASK-2", vendor="Google", session_id="gem-session")
        lines = board.skill_usage_lines_for_task(self.root, "USAGE-TASK-2")
        self.assertIn("Reviewer: not available for Gemini.", lines)

    def test_skills_used_survives_the_agent_being_archived_after_acceptance(self):
        # Final review (B1): once the accepted task's agent is moved from hot state into
        # cold archive storage, Skills used must keep reading it there, not go blank.
        agent = delivery_with_direction(self.root, "ARCHIVED-TASK")
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: build a small service.",
                                           skills=[{"name": "backend", "reason": "service code"}])
        proposal = board.snapshot(self.root)["requirement_proposals"]["ARCHIVED-TASK"]
        board.record_requirements_decision(self.root, "ARCHIVED-TASK", "go_ahead", version=proposal["version"],
                                            token=(proposal.get("contract_check") or {}).get("token", ""), skills=["backend"])
        before = board.skill_usage_lines_for_task(self.root, "ARCHIVED-TASK")
        with board.locked_state(self.root) as state:
            archived_agent = dict(state["agents"][agent["id"]])
            board._append_cold(self.root, "agents", [archived_agent])
            del state["agents"][agent["id"]]
        self.assertEqual(board.snapshot(self.root)["agents"], {})
        after = board.skill_usage_lines_for_task(self.root, "ARCHIVED-TASK")
        self.assertEqual(before, after)


class LibraryGrowthTests(unittest.TestCase):
    """Final review (B2): a guide folder the owner adds to the library later
    must reach the board's own catalogue (what every next proposal is built
    from) with nothing else edited - spec section 6, "Adding a skill later".
    The board scans the library folder itself, so this holds whatever the
    library module's own fixed table knows about."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        shutil.copytree(skill_library.library_root(), self.root / skill_library.LIBRARY_DIRNAME)

    def _add_guide(self, slug: str, frontmatter: str) -> None:
        folder = self.root / skill_library.LIBRARY_DIRNAME / slug
        folder.mkdir()
        (folder / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n# {slug}\n", encoding="utf-8")

    def test_a_newly_added_guide_reaches_the_next_proposal_catalogue(self):
        self._add_guide("solo-workflow", "name: solo-workflow\ndescription: Guides the new solo workflow.\n"
                                         "plain_name: Solo workflow\nowner_description: Steps for the new workflow.")
        catalogue = board.skill_library_catalogue(self.root)
        slugs = [item["slug"] for item in catalogue]
        self.assertEqual(len(slugs), len(set(slugs)), "no guide may be listed twice")
        self.assertEqual(slugs[:len(skill_library.SKILLS)], [slug for slug, _, _ in skill_library.SKILLS])
        added = next(item for item in catalogue if item["slug"] == "solo-workflow")
        self.assertEqual(added["plain_name"], "Solo workflow")
        self.assertEqual(added["owner_description"], "Steps for the new workflow.")
        self.assertEqual(added["full_description"], "Guides the new solo workflow.")

    def test_a_new_guide_without_plain_lines_still_appears_under_a_readable_name(self):
        self._add_guide("mobile-apps", "name: mobile-apps\ndescription: Builds phone apps that work offline.")
        added = next(item for item in board.skill_library_catalogue(self.root) if item["slug"] == "mobile-apps")
        self.assertEqual(added["plain_name"], "Mobile Apps")
        self.assertEqual(added["owner_description"], "Builds phone apps that work offline.")

    def test_a_damaged_new_guide_is_left_off_the_catalogue(self):
        self._add_guide("half-done", "name: half-done")  # no description line
        self._add_guide("wrong-name", "name: other\ndescription: Its name does not match its folder.")
        slugs = [item["slug"] for item in board.skill_library_catalogue(self.root)]
        self.assertNotIn("half-done", slugs)
        self.assertNotIn("wrong-name", slugs)

    def test_a_builder_can_propose_a_newly_added_guide(self):
        self._add_guide("solo-workflow", "name: solo-workflow\ndescription: Guides the new solo workflow.")
        accepted, dropped = board._validate_proposed_skills(
            [{"name": "solo-workflow", "reason": "the owner works alone"}, {"name": "no-such-guide", "reason": "x"}], self.root)
        self.assertEqual([item["slug"] for item in accepted], ["solo-workflow"])
        self.assertEqual(dropped, ["no-such-guide"])


if __name__ == "__main__":
    unittest.main()

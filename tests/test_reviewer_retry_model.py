# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""When a review cannot be completed, one fresh Reviewer retries on a model the owner preset in Settings (task 2026-10-25_reviewer-retry-model).

Owner, 2026-10-25: "you missed changing reviewer model if we cant't fix" and "keep both we will set the builder and reviewer higher model in
setting". These tests replay the loop on the real board (a temporary git project, the real `request_review`, `qa_result` and self-heal
planner). Machine inputs removed: nothing is read from the machine; Settings live in a temporary home.

Run:  PYTHONPATH=. python3 -m unittest tests.test_reviewer_retry_model -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness import board, control, global_settings, review_rounds, self_heal
from tests import test_frozen_review_scope as frozen

GAP = "TASK DONE: NOT TESTED — the report page cannot be opened here"


class Retry(frozen.Rounds):
    """Three testing-gap verdicts reach the round limit; then exactly one fresh Reviewer, silently."""

    use = None      # the Reviewer that takes the next review (a new one each time when unset)

    def claim(self, request, certified=False):
        if self.use is None:
            return super().claim(request, certified)
        challenge = self.root / f"challenge-{request['cycle']}.md"
        challenge.write_text(frozen.base.HEADER + "| S-101 | An independent adversarial check confirms the repair through a distinct test scope. | adversarial | `" + frozen.TESTED + "` | Holds | PASS: executed | PASS |\n")
        board.claim_qa(self.root, self.use["id"], request["id"], str(challenge))
        return self.use

    def gaps(self, count=3):
        qa = last = None
        for _ in range(count):
            request = self.request(changes="no code change; test it again")
            qa, last = self.verdict(request, "failed", GAP)
        return qa, last

    def entry(self):
        return next(iter(self.state()["review_escalations"].values()))

    def create_factory(self, sessions, settings=None, models=None):
        def create(kind, continues="", recovery=False):
            session = control.create(self.root, kind, fresh=recovery,
                                     settings_override=control.reviewer_retry_settings(settings or control.default_agent_settings(), models))
            sessions.append(session)
            return session
        return create

    def tick(self, create, launched, times=4):
        for _ in range(times):
            for action in self_heal.plan(self.root):
                if action["action"].startswith("review_recovery"):
                    try:
                        self_heal.execute(self.root, action, launched.append, create)
                    except ValueError:
                        pass

    def test_a_review_that_cannot_be_completed_starts_one_fresh_reviewer_on_the_preset_model_silently(self):
        old, _last = self.gaps()
        entry = self.entry()
        self.assertEqual((entry["status"], entry["who"], entry["phase"], entry["reviewer_attempts"]), ("recovery", "reviewer", "requested", 1))
        self.assertEqual(entry["reviewer_id"], old["id"])
        preset = next(m for m in control.PROVIDER_MODELS["claude"] if m != control.STRONGEST_MODELS["claude"])
        sessions, launched = [], []
        self.tick(self.create_factory(sessions, models={"claude": preset}), launched)
        entry = self.entry()
        self.assertEqual(len(sessions), 1, "exactly one fresh session, however many ticks")
        self.assertEqual(launched, sessions)
        self.assertEqual((entry["status"], entry["phase"]), ("recovery", "started"))
        fresh = sessions[0]
        self.assertEqual((fresh["kind"], fresh["provider"], fresh["model"]), ("claude_reviewer", "claude", preset))
        self.assertIn(fresh["effort"], ("high", "xhigh", "max"))
        self.assertNotEqual(fresh["provider"], self.state()["agents"][self.dev["id"]].get("provider", "codex"), "never Delivery's company")
        self.assertIsNone(fresh.get("continues_session"), "a blank conversation")
        self.assertEqual(self.owner_cards(), [], "nothing is asked and no card exists")
        # Delivery is not told to repair anything, and may ask for the same review again with no code change
        self.assertEqual(board.poll(self.root, self.dev["id"]).get("review_recovery"), None)
        self.request(changes="no code change; the fresh Reviewer tests it")

    def test_an_unset_field_uses_the_strongest_model_and_effort_is_raised_to_high(self):
        settings = control.default_agent_settings()
        settings["reviewer"]["effort"] = "low"
        chosen = control.reviewer_retry_settings(settings, {"claude": "", "codex": "", "gemini": ""})["reviewer"]
        self.assertEqual((chosen["provider"], chosen["model"], chosen["effort"]), ("claude", control.STRONGEST_MODELS["claude"], "high"))
        gemini = {"delivery": settings["delivery"], "reviewer": {"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "max"}}
        picked = control.recovery_launch_choice(gemini, {"gemini": "gemini-3.1-pro-low"}, role="reviewer")
        self.assertEqual((picked["model"], picked["effort"]), ("gemini-3.1-pro-high", "high"), "the saved model and the raised effort agree")

    def test_a_reviewer_of_the_delivery_agents_company_is_never_started_and_the_part_ends_honestly(self):
        self.gaps()
        settings = control.default_agent_settings()
        swapped = {**settings, "delivery": {**settings["delivery"], "provider": "claude", "model": "opus"}, "reviewer": {**settings["reviewer"], "provider": "codex", "model": "gpt-5.6-sol"}}
        control.create(self.root, "codex_delivery", settings_override=swapped)        # a live Delivery session of the Reviewer company (claude)
        sessions, launched = [], []
        self.tick(self.create_factory(sessions), launched)
        entry = self.entry()
        self.assertEqual((entry["status"], sessions, launched), ("could_not_finish", [], []))
        self.assertIn("could not be started", entry["outcome"])
        self.assertEqual(self.owner_cards(), [])
        self.assertEqual([a for a in self_heal.plan(self.root) if a["action"].startswith("review_recovery")], [], "no second attempt")

    def test_a_restart_between_creating_and_started_never_makes_a_second_session(self):
        self.gaps()
        sessions, launched = [], []
        create = self.create_factory(sessions)
        made = create("claude_reviewer", recovery=True)           # the process died right after creating, before it recorded anything else
        with board.locked_state(self.root) as state:
            next(iter(state["review_escalations"].values())).update(phase="creating", creating_at=board.now(), session_id=made["id"])
        self.tick(create, launched)
        self.assertEqual(len(sessions), 1)
        self.assertEqual((self.entry()["phase"], launched), ("started", [made]))
        self.assertEqual(len([s for s in control.snapshot(self.root)["sessions"] if s["kind"] == "claude_reviewer"]), 1)

    def test_the_old_reviewer_is_not_replaced_while_it_holds_another_review_and_no_ordinary_reviewer_starts_beside_the_retry(self):
        old, _last = self.gaps()
        with board.locked_state(self.root) as state:
            state["qa_requests"]["other-review"] = {"id": "other-review", "status": "claimed", "claimed_by": old["id"], "task": "TASK-OTHER", "requested_at": board.now(), "phase": "", "subtask": "", "chunk": "", "cycle": 1, "developer_id": self.dev["id"], "review_wait_started_at": ""}
            state["reviewer_needed"] = {"requested_at": "2000-01-01T00:00:00+00:00"}
        names = [a["action"] for a in self_heal.plan(self.root)]
        self.assertFalse([n for n in names if n.startswith("review_recovery")], "waits for the busy Reviewer")
        self.assertNotIn("start_reviewer", names, "the retry owns the next Reviewer")
        with board.locked_state(self.root) as state:
            del state["qa_requests"]["other-review"]
        self.assertIn("review_recovery_create", [a["action"] for a in self_heal.plan(self.root)])

    def start_fresh(self):
        sessions, launched = [], []
        self.tick(self.create_factory(sessions), launched)
        return board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=sessions[0]["id"])

    def test_if_the_fresh_reviewer_cannot_complete_it_either_the_part_is_not_finished_with_no_owner_card(self):
        self.gaps()
        fresh = self.start_fresh()
        with board.locked_state(self.root) as state:      # a verdict from some other Reviewer session counts as a failed review but never settles the retry
            key, entry = next(iter(state["review_escalations"].items()))
            review_rounds.record_judgment(state["review_escalations"], {"task": entry["task"], "stage": review_rounds.INDEPENDENT, **entry["scope"]},
                                          {"effective_result": "failed", "blockers": []}, GAP, {}, board.now(), reviewer_id="x", reviewer_session="another-session")
            self.assertEqual((entry["status"], entry["failed_judgments"]), ("recovery", 4))
        request = self.request(changes="no code change; test it again")
        self.use = fresh
        self.verdict(request, "failed", GAP)
        entry = self.entry()
        self.assertEqual(entry["status"], "could_not_finish")
        self.assertIn("could not be fully tested", entry["outcome"])
        self.assertEqual(self.owner_cards(), [])
        with self.assertRaisesRegex(ValueError, "could not be finished automatically"):
            self.request(changes="once more")
        self.assertEqual([a["action"] for a in self_heal.plan(self.root) if a["action"].startswith("review_recovery")], [], "nothing is stopped or restarted")
        self.assertEqual(board._open_verification_gaps(self.state()), [], "the CTO is no longer told to chase it")

    def test_if_the_fresh_reviewer_finds_real_problems_the_normal_delivery_path_applies(self):
        self.gaps()
        fresh = self.start_fresh()
        request = self.request(changes="no code change; the fresh Reviewer tests it")
        self.use = fresh
        self.verdict(request, "failed", "TASK DONE: NO — the totals are wrong", [frozen.finding("B1")])
        entry = self.entry()
        self.assertEqual((entry["status"], entry["phase"], entry.get("who")), ("recovery", "requested", None))
        self.assertIn("B1", entry["brief"], "the one fresh Delivery attempt now has the brief")
        self.assertEqual(self.owner_cards(), [])


class SettingsField(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_the_field_is_saved_with_the_form_and_an_older_document_loads_unchanged(self):
        document = global_settings.initialize(self.home)
        self.assertEqual(document["reviewer_retry_models"], {"claude": "", "codex": "", "gemini": ""}, "unset by default")
        preset = control.PROVIDER_MODELS["claude"][0]
        saved = global_settings.update_agent_settings(self.home, control.default_agent_settings(), None, {"claude": preset})
        self.assertEqual(global_settings.load(self.home)["reviewer_retry_models"]["claude"], preset)
        self.assertEqual(saved["recovery_models"], document["recovery_models"], "the Delivery field is untouched")
        with self.assertRaises(ValueError):
            global_settings.update_agent_settings(self.home, control.default_agent_settings(), None, {"gemini": "gpt-6-sol"})
        old = json.loads((self.home / global_settings.SETTINGS_FILENAME).read_text(encoding="utf-8"))
        old.pop("reviewer_retry_models")
        (self.home / global_settings.SETTINGS_FILENAME).write_text(json.dumps(old), encoding="utf-8")
        self.assertEqual(global_settings.load(self.home)["reviewer_retry_models"], {"claude": "", "codex": "", "gemini": ""})
        self.assertEqual(global_settings.load(self.home)["agent_settings"], saved["agent_settings"])


for _name in [n for n in dir(frozen.Rounds) if n.startswith("test_") and n not in Retry.__dict__]:
    setattr(Retry, _name, None)


if __name__ == "__main__":
    unittest.main()

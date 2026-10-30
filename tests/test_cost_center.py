# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Cost center core (task 2026-10-27_cost-center). Fixtures are synthetic: shaped like the real files, with invented ids and text."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from harness import cost_center

T0 = datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)
CLAUDE_ID = "c1111111-aaaa-bbbb-cccc-000000000001"
CLAUDE_ID_2 = "c2222222-aaaa-bbbb-cccc-000000000002"
CODEX_ID = "019dcafe-0000-7000-8000-000000000001"
SECRET_TEXT = "PLANTED-DISTINCTIVE-PROMPT-TEXT-91827"


def stamp(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def claude_row(message_id: str, minutes: float, usage: tuple[int, int, int, int], model: str = "claude-sonnet-5-5") -> str:
    return json.dumps({"type": "assistant", "timestamp": stamp(minutes), "message": {
        "id": message_id, "model": model, "content": [{"type": "text", "text": SECRET_TEXT}],
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1], "cache_read_input_tokens": usage[2],
                  "cache_creation_input_tokens": usage[3]}}}) + "\n"


def codex_rows(minutes: float, totals: tuple[int, int, int, int], model: str | None = None, limit: tuple | None = None) -> str:
    out = ""
    if model:
        out += json.dumps({"type": "turn_context", "payload": {"model": model, "note": SECRET_TEXT}}) + "\n"
    payload = {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": totals[0], "cached_input_tokens": totals[1], "cache_write_input_tokens": totals[2],
        "output_tokens": totals[3], "reasoning_output_tokens": totals[3] // 2}}}
    if limit:
        payload["rate_limits"] = {"primary": {"used_percent": limit[0], "window_minutes": 300, "resets_at": limit[1]},
                                  "plan_type": "pro", "credits": {"has_credits": True, "unlimited": False, "balance": "12.50"}}
    out += json.dumps({"type": "event_msg", "timestamp": stamp(minutes), "payload": payload}) + "\n"
    return out


class Fixture:
    def __init__(self, test: unittest.TestCase) -> None:
        self.dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.ledger = self.root / "board"
        self.claude_store = self.root / "claude-home"
        self.codex_store = self.root / "codex-home"

    def claude_file(self, vendor_id: str = CLAUDE_ID) -> Path:
        path = self.claude_store / "projects" / "-synthetic-cwd" / f"{vendor_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def codex_file(self, vendor_id: str = CODEX_ID) -> Path:
        path = self.codex_store / "sessions" / "2026" / "10" / "27" / f"rollout-2026-10-27T09-00-00-{vendor_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def claude_obs(self, task: str, role: str = "delivery", managed: str = "m-delivery", vendor_id: str = CLAUDE_ID, spawned: float = -1) -> dict:
        return {"managed": managed, "provider": "claude", "id": vendor_id, "store": str(self.claude_store), "role": role,
                "holder": {"task": task, "spawned_at": stamp(spawned)}}

    def codex_obs(self, task: str, role: str = "delivery", managed: str = "m-codex") -> dict:
        return {"managed": managed, "provider": "codex", "id": CODEX_ID, "store": str(self.codex_store), "role": role,
                "holder": {"task": task, "spawned_at": stamp(-1)}}

    def collect(self, observations: list[dict], minutes: float = 10, **kwargs) -> dict:
        return cost_center.collect(self.ledger, observations, now=T0 + timedelta(minutes=minutes), **kwargs)

    def summary(self) -> dict:
        cost_center._memo.clear()
        return cost_center.summarize(self.ledger)


class CostCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        cost_center.configure(None)
        self.fx = Fixture(self)

    def test_claude_reply_written_several_times_counts_once_and_prices_exactly(self) -> None:
        # The running-task acceptance numbers: 1,000 input, 500 output, 2,000 cache read, 300 cache write at the card's Sonnet 5.5 rates.
        path = self.fx.claude_file()
        path.write_text(claude_row("msg_1", 1, (1000, 100, 2000, 300)) + claude_row("msg_1", 1, (1000, 500, 2000, 300)), encoding="utf-8")
        self.fx.collect([self.fx.claude_obs("task-a")])
        row = self.fx.summary()["tasks"]["task-a"]
        self.assertEqual(row["tokens"], 3800)   # not 7,600: the repeated row replaced the first, it did not add to it
        self.assertAlmostEqual(row["usd"], (1000 * 2.0 + 500 * 10.0 + 2000 * 0.1 + 300 * 2.5) / 1e6, places=9)
        self.assertFalse(row["estimated"])
        # The next tick sees a new reply: the card, project total and Costs figures grow by exactly that reply.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(claude_row("msg_2", 2, (1000, 500, 2000, 300)))
        self.fx.collect([self.fx.claude_obs("task-a")], minutes=11)
        summary = self.fx.summary()
        self.assertAlmostEqual(summary["tasks"]["task-a"]["usd"], 2 * 0.00795, places=9)
        self.assertAlmostEqual(summary["total"]["usd"], 2 * 0.00795, places=9)
        self.assertIn("Cost so far: about $0.0159 API-equivalent", cost_center.task_line(summary, "task-a"))

    def test_a_later_duplicate_row_after_a_restart_corrects_the_same_bucket(self) -> None:
        path = self.fx.claude_file()
        path.write_text(claude_row("msg_1", 1, (10, 20, 0, 0)), encoding="utf-8")
        self.fx.collect([self.fx.claude_obs("task-a")])
        with path.open("a", encoding="utf-8") as handle:   # the same reply arrives again, bigger, after the ledger was saved
            handle.write(claude_row("msg_1", 1, (10, 90, 0, 0)))
        self.fx.collect([self.fx.claude_obs("task-b")], minutes=20)   # the agent has since moved to another task
        tasks = self.fx.summary()["tasks"]
        self.assertEqual(tasks["task-a"]["tokens"], 100)
        self.assertNotIn("task-b", tasks)

    def test_codex_cumulative_totals_add_only_the_difference_and_follow_a_reset(self) -> None:
        path = self.fx.codex_file()
        path.write_text(codex_rows(1, (10000, 4000, 0, 500), model="gpt-6.1-sol"), encoding="utf-8")
        self.fx.collect([self.fx.codex_obs("task-a")])
        row = self.fx.summary()["tasks"]["task-a"]
        # Input includes the cached part: 10,000 - 4,000 = 6,000 fresh; reasoning is inside output, so it is not added again.
        self.assertEqual(row["tokens"], 6000 + 4000 + 500)
        self.assertAlmostEqual(row["usd"], (6000 * 2.0 + 500 * 10.0 + 4000 * 0.1) / 1e6, places=9)
        self.fx.collect([self.fx.codex_obs("task-a")], minutes=11)   # read again with nothing new: nothing added
        self.assertEqual(self.fx.summary()["tasks"]["task-a"]["tokens"], 10500)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(codex_rows(2, (12000, 4000, 0, 800)))
        self.fx.collect([self.fx.codex_obs("task-a")], minutes=12)
        self.assertEqual(self.fx.summary()["tasks"]["task-a"]["tokens"], 10500 + 2000 + 300)
        with path.open("a", encoding="utf-8") as handle:   # the counters start again from a small number
            handle.write(codex_rows(3, (700, 100, 0, 50)))
        self.fx.collect([self.fx.codex_obs("task-a")], minutes=13)
        self.assertEqual(self.fx.summary()["tasks"]["task-a"]["tokens"], 10500 + 2300 + 700 + 50)

    def test_unknown_model_uses_the_highest_rate_of_each_kind_and_says_estimated(self) -> None:
        self.fx.claude_file().write_text(claude_row("msg_1", 1, (1000, 1000, 1000, 1000), model="claude-unheard-of-9"), encoding="utf-8")
        self.fx.collect([self.fx.claude_obs("task-a")])
        summary = self.fx.summary()
        card, _note = cost_center.load_card()
        highest = {kind: max(rates[kind] for rates in card["models"].values()) for kind in cost_center.RATE_KINDS}
        self.assertAlmostEqual(summary["tasks"]["task-a"]["usd"], 1000 * sum(highest.values()) / 1e6, places=9)
        self.assertTrue(summary["tasks"]["task-a"]["estimated"])
        self.assertIn("(estimated)", cost_center.task_line(summary, "task-a"))
        rates, estimated = cost_center.rates_for(card, "claude-sonnet-5-5-20261101")   # a longer name matches by its longest known prefix
        self.assertFalse(estimated)
        self.assertEqual(rates["input"], 2.0)

    def test_readings_are_split_at_the_time_the_agent_changed_task(self) -> None:
        path = self.fx.claude_file()
        path.write_text(claude_row("msg_1", 2, (100, 0, 0, 0)), encoding="utf-8")
        self.fx.collect([self.fx.claude_obs("task-a")], minutes=5)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(claude_row("msg_2", 6, (100, 0, 0, 0)) + claude_row("msg_3", 12, (300, 0, 0, 0)))
        self.fx.collect([self.fx.claude_obs("task-b")], minutes=10)   # the change was seen at minute 10; msg_3 (minute 12) is task-b's
        tasks = self.fx.summary()["tasks"]
        self.assertEqual(tasks["task-a"]["tokens"], 200)
        self.assertEqual(tasks["task-b"]["tokens"], 300)

    def test_three_roles_cto_overhead_and_unallocated_add_up_to_the_project(self) -> None:
        self.fx.claude_file().write_text(claude_row("msg_1", 1, (1000, 0, 0, 0)), encoding="utf-8")
        self.fx.claude_file(CLAUDE_ID_2).write_text(claude_row("msg_9", 1, (500, 0, 0, 0), model="claude-opus-5-5"), encoding="utf-8")
        self.fx.codex_file().write_text(codex_rows(1, (2000, 0, 0, 0), model="gpt-6.1-sol"), encoding="utf-8")
        cto_id = "c3333333-aaaa-bbbb-cccc-000000000003"
        self.fx.claude_file(cto_id).write_text(claude_row("msg_c", 1, (400, 0, 0, 0)), encoding="utf-8")
        loose = "c4444444-aaaa-bbbb-cccc-000000000004"
        self.fx.claude_file(loose).write_text(claude_row("msg_l", 1, (100, 0, 0, 0)), encoding="utf-8")
        self.fx.collect([
            self.fx.codex_obs("task-a", role="delivery"),
            self.fx.claude_obs("task-a", role="reviewer", managed="m-rev", vendor_id=CLAUDE_ID_2),
            self.fx.claude_obs("task-a", role="cto", managed="m-cto", vendor_id=cto_id),
            self.fx.claude_obs("AWAITING_OWNER_DIRECTION", role="delivery", managed="m-idle", vendor_id=loose),
            self.fx.claude_obs("task-a", role="delivery", managed="m-d2"),
        ])
        summary = self.fx.summary()
        row = summary["tasks"]["task-a"]
        self.assertEqual(set(row["roles"]), {"delivery", "reviewer"})
        self.assertEqual(summary["overhead"]["tokens"], 400)
        self.assertEqual(summary["unallocated"]["tokens"], 100)
        self.assertAlmostEqual(summary["total"]["usd"], row["usd"] + summary["overhead"]["usd"] + summary["unallocated"]["usd"], places=12)
        self.assertEqual(summary["total"]["tokens"], row["tokens"] + 500)
        line = cost_center.project_line(summary)
        self.assertIn("CTO overhead", line)
        self.assertIn("not on a task", line)
        self.assertIn("builder", cost_center.task_line(summary, "task-a"))
        self.assertIn("reviewer", cost_center.task_line(summary, "task-a"))

    def test_restart_handover_shrunk_file_and_half_written_line_lose_and_double_nothing(self) -> None:
        path = self.fx.claude_file()
        whole = claude_row("msg_1", 1, (100, 0, 0, 0))
        second = claude_row("msg_2", 2, (200, 0, 0, 0))
        path.write_text(whole + second[:40], encoding="utf-8")   # the last line is only half written
        self.fx.collect([self.fx.claude_obs("task-a")])
        self.assertEqual(self.fx.summary()["tasks"]["task-a"]["tokens"], 100)
        path.write_text(whole + second, encoding="utf-8")
        cost_center._memo.clear()   # a restart: nothing but the ledger file carries over
        # The same conversation is now owned by a replacement managed session: it must count once.
        self.fx.collect([self.fx.claude_obs("task-a", managed="m-replacement")], minutes=11)
        self.assertEqual(self.fx.summary()["tasks"]["task-a"]["tokens"], 300)
        path.write_text(whole, encoding="utf-8")   # the file shrank; it is read again from the start
        self.fx.collect([self.fx.claude_obs("task-a", managed="m-replacement")], minutes=12)
        self.assertEqual(self.fx.summary()["tasks"]["task-a"]["tokens"], 300)
        codex = self.fx.codex_file()
        codex.write_text(codex_rows(1, (1000, 0, 0, 100), model="gpt-6.1-sol") + codex_rows(2, (2000, 0, 0, 200)), encoding="utf-8")
        self.fx.collect([self.fx.codex_obs("task-a")], minutes=13)
        codex.write_text(codex_rows(1, (1000, 0, 0, 100)), encoding="utf-8")   # shrunk
        self.fx.collect([self.fx.codex_obs("task-a")], minutes=14)
        self.assertEqual(self.fx.summary()["tasks"]["task-a"]["tokens"], 300 + 2200)

    def test_an_edited_rate_card_reprices_history_and_an_invalid_one_falls_back_with_one_line(self) -> None:
        self.fx.claude_file().write_text(claude_row("msg_1", 1, (1_000_000, 0, 0, 0)), encoding="utf-8")
        self.fx.collect([self.fx.claude_obs("task-a")])
        self.assertAlmostEqual(self.fx.summary()["tasks"]["task-a"]["usd"], 2.0)
        mine = self.fx.root / "my_card.json"
        mine.write_text(json.dumps({"as_of": "2026-12-01", "models": {"claude-sonnet-5-5": {"input": 3.0, "output": 15.0}}}), encoding="utf-8")
        cost_center.configure({"cost_rate_card_path": str(mine)})
        summary = self.fx.summary()
        self.assertAlmostEqual(summary["tasks"]["task-a"]["usd"], 3.0)
        self.assertEqual(summary["as_of"], "2026-12-01")
        for bad in ('{"models": {"x": {"input": -1, "output": 1}}}', '{"models": {"x": {"input": "1", "output": 1}}}', "not json", '{"models": {}}'):
            mine.write_text(bad, encoding="utf-8")
            cost_center._memo.clear()
            summary = self.fx.summary()
            self.assertAlmostEqual(summary["tasks"]["task-a"]["usd"], 2.0)
            self.assertIn("built-in price list", summary["card_note"])
            self.assertIn("built-in price list", cost_center.project_line(summary))
        mine.write_text('{"models": {"x": {"input": Infinity, "output": 1}}}', encoding="utf-8")
        self.assertIn("built-in price list", self.fx.summary()["card_note"])

    def test_failures_are_labelled_partial_and_never_zero_and_never_raise(self) -> None:
        # A session file that is not there: "not counted", not $0.
        self.fx.collect([self.fx.claude_obs("task-a")])
        summary = self.fx.summary()
        line = cost_center.task_line(summary, "task-a")
        self.assertIn("not counted", line)
        self.assertNotIn("$0", line)
        # A corrupt ledger is moved aside and rebuilt from the files.
        self.fx.claude_file().write_text(claude_row("msg_1", 1, (100, 0, 0, 0)), encoding="utf-8")
        cost_center.ledger_path(self.fx.ledger).write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.fx.collect([self.fx.claude_obs("task-a")])["status"], "ok")
        self.assertTrue(list(self.fx.ledger.glob("cost_ledger.corrupt-*.json")))
        self.assertEqual(self.fx.summary()["tasks"]["task-a"]["tokens"], 100)
        # A disk that is full: the pass reports partial, the old ledger stays, nothing raises.
        with mock.patch.object(cost_center, "_save", side_effect=OSError(28, "No space left")):
            result = self.fx.collect([self.fx.claude_obs("task-a")], minutes=20)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.fx.summary()["tasks"]["task-a"]["tokens"], 100)
        # A byte limit smaller than the file: counted so far, labelled partial, finished on later passes.
        self.fx.claude_file(CLAUDE_ID_2).write_text("".join(claude_row(f"m{n}", 1, (10, 0, 0, 0)) for n in range(30)), encoding="utf-8")
        other = self.fx.claude_obs("task-b", managed="m-b", vendor_id=CLAUDE_ID_2)
        self.assertEqual(self.fx.collect([other], max_bytes=2000)["status"], "partial")
        partial = cost_center.task_line(self.fx.summary(), "task-b")
        self.assertIn("partial because", partial)
        for _ in range(30):
            if self.fx.collect([other], max_bytes=2000)["status"] == "ok":
                break
        self.assertEqual(self.fx.summary()["tasks"]["task-b"]["tokens"], 300)

    def test_gemini_is_always_said_to_be_unmeasured_never_zero_and_the_project_excludes_it(self) -> None:
        self.fx.claude_file().write_text(claude_row("msg_1", 1, (100, 0, 0, 0)), encoding="utf-8")
        gemini = {"managed": "m-gem", "provider": "gemini", "id": "gem-conversation-1", "store": "", "role": "delivery",
                  "holder": {"task": "task-a", "spawned_at": stamp(-1)}}
        self.fx.collect([gemini])
        summary = self.fx.summary()
        line = cost_center.task_line(summary, "task-a")
        self.assertIn("Gemini (builder): not measured yet - the Gemini program does not record usage in a readable form", line)
        self.assertNotIn("$0", line)
        self.assertIn("Excludes Gemini", cost_center.project_line(summary))

    def test_an_aggregate_with_nothing_counted_says_not_measured_never_zero(self) -> None:
        # Nothing readable at all (the session file is missing): the project says "not measured", not $0.00.
        self.fx.collect([self.fx.claude_obs("task-a")])
        summary = self.fx.summary()
        line = cost_center.project_line(summary)
        self.assertIn("not measured", line)
        self.assertNotIn("$0", line)
        self.assertEqual(cost_center.figures(self.fx.ledger), {"unavailable": True, "reason": "not measured"})
        # Part counted: the amount is shown with a plain coverage note, and the empty parts say "not measured".
        self.fx.claude_file(CLAUDE_ID_2).write_text(claude_row("msg_1", 1, (1000, 500, 2000, 300)), encoding="utf-8")
        self.fx.collect([self.fx.claude_obs("task-a"), self.fx.claude_obs("task-b", managed="m-b", vendor_id=CLAUDE_ID_2)], minutes=12)
        line = cost_center.project_line(self.fx.summary())
        self.assertIn("Project so far: about $0.0080 API-equivalent for 1 of 2 tasks measured", line)
        self.assertIn("CTO overhead not measured + not on a task not measured", line)
        self.assertNotIn("$0.00 ", line)

    def test_a_counted_zero_is_shown_as_zero_dollars(self) -> None:
        self.fx.claude_file().write_text(claude_row("msg_1", 1, (0, 0, 0, 0)), encoding="utf-8")
        self.fx.collect([self.fx.claude_obs("task-a")])
        summary = self.fx.summary()
        self.assertIn("Project so far: about $0.00 API-equivalent", cost_center.project_line(summary))
        self.assertEqual(cost_center.figures(self.fx.ledger)["usd"], 0.0)

    def test_codex_subscription_percentage_for_a_task_and_a_window_that_reset(self) -> None:
        path = self.fx.codex_file()
        path.write_text(codex_rows(1, (1000, 0, 0, 100), model="gpt-6.1-sol", limit=(10.0, 1_800_000_000))
                        + codex_rows(3, (2000, 0, 0, 200), limit=(14.5, 1_800_000_000)), encoding="utf-8")
        self.fx.collect([self.fx.codex_obs("task-a")])
        line = cost_center.task_line(self.fx.summary(), "task-a")
        self.assertIn("Codex subscription: about +4.5% of your 5-hour window.", line)
        self.assertNotIn("12.50", line)   # the credit balance is never shown
        with path.open("a", encoding="utf-8") as handle:   # the window reset: the percentage starts again from a new reset time
            handle.write(codex_rows(5, (2500, 0, 0, 250), limit=(2.0, 1_800_018_000)))
        self.fx.collect([self.fx.codex_obs("task-a")], minutes=12)
        line = cost_center.task_line(self.fx.summary(), "task-a")
        self.assertIn("Codex subscription: window reset during this task.", line)
        self.assertNotIn("+", line.split("Codex subscription:")[1])

    def test_nothing_from_the_transcripts_except_ids_model_time_and_counts_is_ever_written(self) -> None:
        self.fx.claude_file().write_text(claude_row("msg_1", 1, (100, 5, 0, 0)), encoding="utf-8")
        self.fx.codex_file().write_text(codex_rows(1, (100, 0, 0, 5), model="gpt-6.1-sol", limit=(1.0, 1_800_000_000)), encoding="utf-8")
        self.fx.collect([self.fx.claude_obs("task-a"), self.fx.codex_obs("task-a")])
        blob = cost_center.ledger_path(self.fx.ledger).read_text(encoding="utf-8")
        summary = self.fx.summary()
        shown = json.dumps(summary) + cost_center.task_line(summary, "task-a") + cost_center.project_line(summary)
        self.assertNotIn(SECRET_TEXT, blob + shown)
        self.assertNotIn("12.50", blob + shown)
        for leftover in self.fx.ledger.iterdir():
            self.assertNotIn(SECRET_TEXT, leftover.read_text(encoding="utf-8", errors="replace"))

    def test_a_session_id_that_is_not_a_plain_token_never_widens_the_lookup(self) -> None:
        self.fx.claude_file().write_text(claude_row("msg_1", 1, (100, 0, 0, 0)), encoding="utf-8")
        self.assertIsNone(cost_center.find_file("claude", str(self.fx.claude_store), "../../" + CLAUDE_ID))
        self.assertIsNone(cost_center.find_file("claude", str(self.fx.claude_store), "*"))
        self.assertIsNotNone(cost_center.find_file("claude", str(self.fx.claude_store), CLAUDE_ID))

    def test_settings_carry_the_card_path_and_the_two_limits(self) -> None:
        from harness import global_settings
        plumbing = global_settings._validated_plumbing({"cost_rate_card_path": " /somewhere/card.json ", "cost_collect_seconds": 7})
        self.assertEqual(plumbing["cost_rate_card_path"], "/somewhere/card.json")
        self.assertEqual(plumbing["cost_collect_seconds"], 7)
        with self.assertRaises(ValueError):
            global_settings._validated_plumbing({"cost_collect_max_bytes": 5})
        cost_center.configure(plumbing)
        self.assertEqual(cost_center.limits()[1], 7)


if __name__ == "__main__":
    unittest.main()


class GatherTests(unittest.TestCase):
    def test_a_launched_session_is_read_with_its_own_store_and_the_task_its_agent_holds(self) -> None:
        from harness import board, control
        sessions = {"claude_cto-1": {"kind": "claude_cto", "provider": "claude", "cli_session_provider": "claude", "cli_session_id": CLAUDE_ID,
                                      "cli_session_store": "/launch/home/.claude"},
                    "codex_delivery-1": {"kind": "codex_delivery", "provider": "codex", "cli_session_id": CODEX_ID}}
        agents = {"a-old": {"session_id": "codex_delivery-1", "active": True, "task": "T-old", "spawned_at": "2026-10-27T08:00:00+00:00"},
                  "a-new": {"session_id": "codex_delivery-1", "active": True, "task": "T-new", "spawned_at": "2026-10-27T09:00:00+00:00"},
                  "a-gone": {"session_id": "claude_cto-1", "active": False, "task": "T-x", "spawned_at": "2026-10-27T09:00:00+00:00"}}
        with mock.patch.object(control, "_read_state", return_value={"sessions": sessions}), \
                mock.patch.object(board, "_read_state", return_value={"agents": agents}):
            observed = {item["managed"]: item for item in cost_center.gather(object())}
        self.assertEqual(observed["claude_cto-1"]["store"], "/launch/home/.claude")
        self.assertEqual(observed["claude_cto-1"]["role"], "cto")
        self.assertIsNone(observed["claude_cto-1"]["holder"])
        self.assertEqual(observed["codex_delivery-1"]["holder"]["task"], "T-new")
        self.assertEqual(observed["codex_delivery-1"]["role"], "delivery")

    def test_a_code_analysis_reviewer_holds_the_analysis_task_so_its_cost_lands_on_the_task(self) -> None:
        """2026-10-29 live run: the reviewer's $4.58 sat outside the task ("not counted yet") and the task showed $4.10 of a real $9.83."""
        from harness import board, control
        sessions = {"codex_reviewer-1": {"kind": "codex_reviewer", "provider": "codex", "cli_session_id": CODEX_ID}}
        agents = {"qa-1": {"id": "qa-1", "session_id": "codex_reviewer-1", "active": True, "task": "REVIEW_QUEUE", "spawned_at": "2026-10-27T09:00:00+00:00"}}
        passes = {"security-code-analysis": {"reviewer_id": "qa-1", "sides": {}}}
        with mock.patch.object(control, "_read_state", return_value={"sessions": sessions}), \
                mock.patch.object(board, "_read_state", return_value={"agents": agents, "analysis_passes": passes}):
            observed = {item["managed"]: item for item in cost_center.gather(object())}
        self.assertEqual(observed["codex_reviewer-1"]["holder"]["task"], "security-code-analysis")
        self.assertEqual(agents["qa-1"]["task"], "REVIEW_QUEUE", "the stored agent is not changed")

    def test_an_existing_ledger_moves_a_reviewers_idle_spend_to_the_analysis_task_it_now_holds(self) -> None:
        """Round 2 of the 2026-10-29 review: the owner's paused analysis already counted its reviewer's spend as unallocated; taking the task corrects it."""
        now = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
        key = "codex|/store|abc"
        ledger = cost_center._empty()
        ledger["sessions"][key] = {"provider": "codex", "model": "gpt-6-sol", "last": [1000, 400, 0, 200], "role": "reviewer"}
        ledger["windows"][key] = [{"task": "", "role": "reviewer", "from": "2026-10-29T19:00:00+00:00"}]
        cost_center._add(ledger, "reviewer", "", "gpt-6-sol", [600, 200, 400, 0])
        ckey = "claude|/store|def"
        ledger["windows"][ckey] = [{"task": "", "role": "reviewer", "from": "2026-10-29T19:00:00+00:00"}]
        ledger["replies"][f"{ckey}|m1"] = {"r": "reviewer", "t": "", "m": "claude-sonnet-5", "c": [10, 20, 30, 40]}
        ledger["replies"][f"{ckey}|m2"] = {"r": "reviewer", "t": "OTHER-TASK", "m": "claude-sonnet-5", "c": [1, 1, 1, 1]}
        cost_center._add(ledger, "reviewer", "", "claude-sonnet-5", [10, 20, 30, 40])
        cost_center._add(ledger, "reviewer", "OTHER-TASK", "claude-sonnet-5", [1, 1, 1, 1])
        cost_center._sample_windows(ledger, key, {"task": "security-code-analysis"}, "reviewer", now)
        cost_center._sample_windows(ledger, ckey, {"task": "security-code-analysis"}, "reviewer", now)
        self.assertEqual(ledger["buckets"]["reviewer\t\tgpt-6-sol"], [0, 0, 0, 0])
        self.assertEqual(ledger["buckets"]["reviewer\tsecurity-code-analysis\tgpt-6-sol"], [600, 200, 400, 0])
        self.assertEqual(ledger["buckets"]["reviewer\tsecurity-code-analysis\tclaude-sonnet-5"], [10, 20, 30, 40])
        self.assertEqual(ledger["buckets"]["reviewer\tOTHER-TASK\tclaude-sonnet-5"], [1, 1, 1, 1], "genuine other-task usage does not move")
        self.assertEqual(ledger["replies"][f"{ckey}|m1"]["t"], "security-code-analysis")
        cost_center._sample_windows(ledger, key, {"task": "security-code-analysis"}, "reviewer", now)
        self.assertEqual(ledger["buckets"]["reviewer\tsecurity-code-analysis\tgpt-6-sol"], [600, 200, 400, 0], "a second sampling moves nothing again")

    def test_a_reviewer_that_already_held_a_real_task_is_not_reattributed(self) -> None:
        now = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
        key = "claude|/store|ghi"
        ledger = cost_center._empty()
        ledger["windows"][key] = [{"task": "T-real", "role": "reviewer", "from": "2026-10-29T19:00:00+00:00"}]
        ledger["replies"][f"{key}|m1"] = {"r": "reviewer", "t": "", "m": "claude-sonnet-5", "c": [5, 5, 5, 5]}
        cost_center._add(ledger, "reviewer", "", "claude-sonnet-5", [5, 5, 5, 5])
        cost_center._sample_windows(ledger, key, {"task": "NEW"}, "reviewer", now)
        self.assertEqual(ledger["buckets"]["reviewer\t\tclaude-sonnet-5"], [5, 5, 5, 5])

    def test_a_cost_failure_never_reaches_the_caller(self) -> None:
        with mock.patch.object(cost_center, "gather", side_effect=RuntimeError("boom")):
            result = cost_center.collect_project(object(), Path(tempfile.gettempdir()))
        self.assertEqual(result["status"], "partial")


class ReleaseScrubTests(unittest.TestCase):
    def test_a_planted_cost_ledger_is_refused_before_anything_is_released(self) -> None:
        import subprocess
        import shutil
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as scratch:
            fake = Path(scratch) / "src"
            (fake / "scripts").mkdir(parents=True)
            shutil.copy(repo / "scripts" / "make_public_release.sh", fake / "scripts" / "make_public_release.sh")
            for directory in ("harness", "web", "engine", "tests", "directives"):
                (fake / directory).mkdir()
            for name in ("profile.template", "profile.example.json", "LICENSE", "NOTICE", "DISCLAIMER.md", "CONTRIBUTING.md", "README.md", "install.sh", ".gitignore"):
                (fake / name).write_text("x\n", encoding="utf-8")
            (fake / "harness" / "cost_ledger.json").write_text("{}", encoding="utf-8")
            result = subprocess.run(["bash", str(fake / "scripts" / "make_public_release.sh"), str(Path(scratch) / "out")],
                                    capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REFUSED: private cost ledger", result.stderr)
        self.assertNotIn("Gate 4", result.stdout)


@unittest.skipUnless((Path(__file__).resolve().parents[1] / "evals" / "golden" / "run_eval.py").is_file(), "golden eval set: only where evals/golden ships")
class EvalRunCostTests(unittest.TestCase):
    """The golden eval scorer reads a run's costs from the run's own isolated homes, whatever NMHP version ran it."""

    def _run_eval(self):
        import importlib.util
        import sys
        golden = Path(__file__).resolve().parents[1] / "evals" / "golden" / "run_eval.py"
        spec = importlib.util.spec_from_file_location("golden_run_eval_cost", str(golden))
        module = importlib.util.module_from_spec(spec)
        sys.modules["golden_run_eval_cost"] = module
        spec.loader.exec_module(module)
        return module

    def test_costs_come_from_the_runs_sessions_only_and_land_in_the_score_sheet(self) -> None:
        import re
        cost_center.configure(None)
        with tempfile.TemporaryDirectory() as scratch:
            base = Path(scratch)
            run = base / "nmhp-golden-synthetic-abc"
            codex_home, claude_home = base / "signin-codex", base / "signin-home"
            data = run / "home" / "projects" / "p1" / "data"
            (data / "control").mkdir(parents=True)
            (data / "board").mkdir(parents=True)
            slug = lambda path: re.sub(r"[^A-Za-z0-9]", "-", str(path))
            reviewer_file = claude_home / ".claude" / "projects" / slug(run / "project") / f"{CLAUDE_ID}.jsonl"
            reviewer_file.parent.mkdir(parents=True)
            reviewer_file.write_text(claude_row("msg_1", 1, (1000, 500, 2000, 300)), encoding="utf-8")
            other_file = claude_home / ".claude" / "projects" / "-somebody-elses-project" / f"{CLAUDE_ID_2}.jsonl"
            other_file.parent.mkdir(parents=True)
            other_file.write_text(claude_row("msg_x", 1, (999999, 0, 0, 0)), encoding="utf-8")
            rollout = codex_home / "sessions" / "2026" / "10" / "27" / f"rollout-2026-10-27T09-00-00-{CODEX_ID}.jsonl"
            rollout.parent.mkdir(parents=True)
            meta = json.dumps({"type": "session_meta", "payload": {"cwd": str(run / "project"), "instructions": SECRET_TEXT}}) + "\n"
            rollout.write_text(meta + codex_rows(1, (10000, 4000, 0, 500), model="gpt-6.1-sol", limit=(10.0, 1_800_000_000))
                               + codex_rows(2, (12000, 4000, 0, 800), limit=(13.0, 1_800_000_000)), encoding="utf-8")
            sessions = {
                "claude_reviewer-1": {"kind": "claude_reviewer", "provider": "claude", "cli_session_provider": "claude", "cli_session_id": CLAUDE_ID},
                "codex_delivery-1": {"kind": "codex_delivery", "provider": "codex", "cli_session_provider": "codex", "cli_session_id": CODEX_ID},
                "claude_cto-1": {"kind": "claude_cto", "provider": "claude", "cli_session_provider": "claude", "cli_session_id": CLAUDE_ID_2},
                "gemini-1": {"kind": "codex_delivery", "provider": "gemini", "cli_session_provider": "gemini", "cli_session_id": "gem-conversation-1"},
            }
            (data / "control" / "sessions.json").write_text(json.dumps({"sessions": sessions}), encoding="utf-8")
            agents = {
                "a1": {"id": "a1", "role": "qa", "task": "T1", "session_id": "claude_reviewer-1", "spawned_at": stamp(-1)},
                "a2": {"id": "a2", "role": "development", "task": "T1", "session_id": "codex_delivery-1", "spawned_at": stamp(-1)},
                "a3": {"id": "a3", "role": "cto", "task": "T1", "session_id": "claude_cto-1", "spawned_at": stamp(-1)},
                "a4": {"id": "a4", "role": "development", "task": "T1", "session_id": "gemini-1", "spawned_at": stamp(-1)},
            }
            (data / "board" / "state.json").write_text(json.dumps({"agents": agents}), encoding="utf-8")
            record = {"project": "03_json_api", "project_id": "p1", "home": str(run / "home"),
                      "isolation": {"codex_home": str(codex_home), "claude_home": str(claude_home)}}
            cost = cost_center.collect_run(run, record)
            self.assertTrue(cost["available"], cost)
            self.assertEqual(cost["roles"]["reviewer"]["tokens"], 3800)
            self.assertAlmostEqual(cost["roles"]["reviewer"]["usd"], 0.00795, places=9)
            self.assertEqual(cost["roles"]["delivery"]["tokens"], 6000 + 4000 + 500 + 2000 + 300)   # the other project's session is not counted
            self.assertNotIn("cto", cost["roles"])   # its session lives outside the run folder, so it is not the run's
            self.assertEqual(cost["sessions_outside_the_run"], 1)
            self.assertIn("+3% of your 5-hour window", list(cost["codex_windows"].values())[0])
            self.assertTrue(cost["gemini"])
            cells = self._run_eval().cost_cells(cost)
            self.assertEqual(len(cells), 5)
            self.assertIn("not measured", cells[4])
            self.assertTrue(cells[2].startswith("$"))
            self.assertNotIn(SECRET_TEXT, " ".join(cells) + json.dumps(cost))
            self.assertEqual(self._run_eval().cost_cells(cost_center.collect_run(run, {"project": "x"}))[0][:11], "unavailable")
            # The sheet carries the columns (an old run without figures says "unavailable", never zero).
            runner = self._run_eval()
            folder = runner.write_sheet(base / "results", "t", [{"project": "03_json_api", "title": "t", "planted": 1, "fixed": 1, "bugs": [], "flagged_by_review": [],
                                                                  "false_blocker_texts": [], "false_blockers": 0, "review_rounds": 1, "review_passed": 1, "review_failed": 0,
                                                                  "outcome": "o", "agent_steps": 1, "review_minutes": 1, "cto_polls": 1, "cto_commands": 1,
                                                                  "cto_wakes_total": 1, "cost": cost},
                                                                 {"project": "04", "title": "old", "planted": 1, "fixed": 0, "bugs": [], "flagged_by_review": [],
                                                                  "false_blocker_texts": [], "false_blockers": None, "review_rounds": None, "outcome": "o", "agent_steps": None,
                                                                  "review_minutes": None}])
            sheet = (folder / "score.md").read_text(encoding="utf-8")
            self.assertIn("API-equivalent $ total", sheet)
            self.assertIn("unavailable", sheet)
            table = [line for line in sheet.splitlines() if line.startswith("| Project") or line.startswith("|---|---|---|---|---|---|---|---|---|---|---|---|") or line.startswith("| 0") or line.startswith("| **Total")]
            self.assertEqual(len(table), 5, table)
            self.assertEqual({line.count("|") for line in table}, {table[0].count("|")}, "every row, the total row included, has the same number of columns")
            self.assertIn("for 1 of 2 projects measured", sheet)   # the total says how much of it was measured
            self.assertIn("excludes Gemini", sheet)
            self.assertIn("not measured", " ".join(runner.cost_cells({"available": True, "roles": {"delivery": {"tokens": 5, "usd": 0.5}}, "usd": 0.5, "tasks_total": 1, "tasks_measured": 1})[:2]))
            # Nothing measured anywhere: the total is "not measured", not $0.00.
            empty = runner.write_sheet(base / "results", "t2", [{"project": "04", "title": "old", "planted": 1, "fixed": 0, "bugs": [], "flagged_by_review": [],
                                                                  "false_blocker_texts": [], "false_blockers": None, "review_rounds": None, "outcome": "o", "agent_steps": None,
                                                                  "review_minutes": None}])
            total_line = [line for line in (empty / "score.md").read_text(encoding="utf-8").splitlines() if line.startswith("| **Total")][0]
            self.assertIn("not measured", total_line)
            self.assertNotIn("$0", total_line)

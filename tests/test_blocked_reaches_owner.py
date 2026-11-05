# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A blocked agent goes to the CTO first; when the CTO cannot solve it, the owner gets one red card (owner 2026-11-05).

The owner's rule, in his words: "when the NMHP blocks dev, reviewer, the CTO tries to solve it; if the CTO is also blocked a
clear message on the mission board to the user should appear with red warning: what is the problem and what the user should do."
Before this, only a blocked builder reached the CTO and ended in a (plain) card; a blocked Reviewer was woken twice and then left
alone in silence, and a CTO that said "blocked" or stayed silent while somebody was blocked told the owner nothing.

Tested here, on a real temporary board:
  * a blocked Reviewer opens one stuck item for the CTO, like a blocked builder;
  * an agent that said "blocked" is escalated on its own clock, polling or not: after one window the CTO is asked if it was
    not, it gets one window (and its routed fix one more to land), then the project pauses with one red card
    (Problem / What to do) quoting the CTO's answer; with no CTO the owner is told after the first window, with no wake-up;
  * a builder or Reviewer that only goes quiet is handed to the CTO after two empty wake-ups, before any pause;
  * a CTO that stays silent while a builder is blocked brings the owner in; a silent CTO with nobody blocked is left alone;
  * a CTO that says it is blocked raises the red card at once, once, also after the watchdog recorded a stall, while an
    ordinary "waiting" note is still refused;
  * the real page draws a blocked card in red, first, with the two lines, and an ordinary card stays as it was.

Run:  PYTHONPATH=. python3 -m unittest tests.test_blocked_reaches_owner -v
"""
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, control, cto_wakes, project_memory, self_heal
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_no_silent_stall import _Stall
from tests.test_self_restart import _ago

REVIEWER_WORDS = "I cannot run the review: the test machine refuses my sign-in."
CTO_WORDS = "I cannot fix the sign-in from here; the machine wants the owner's password."


class ABlockedReviewerGoesToTheCtoFirst(_Stall):
    def test_a_blocked_reviewer_opens_one_stuck_item_with_its_words(self):
        _, reviewer, _ = self.reviewer("weather-app")
        self.cto()
        board.status(self.root, reviewer["id"], REVIEWER_WORDS, "blocked")
        stuck = [item for item in cto_wakes.open_items(board.snapshot(self.root))
                 if item["reason"] == "stuck" and item["subject"].startswith("reviewer_blocked:")]
        self.assertEqual(len(stuck), 1)
        self.assertIn("The Reviewer is blocked", stuck[0]["detail"])
        self.assertTrue(stuck[0]["wake_pending"])
        board.status(self.root, reviewer["id"], REVIEWER_WORDS + " Still refused.", "blocked")
        again = [item for item in cto_wakes.open_items(board.snapshot(self.root)) if item["subject"].startswith("reviewer_blocked:")]
        self.assertEqual(len(again), 1, "the same block never opens a second item")
        self.assertEqual(self.cards(), [], "the CTO tries first: no card for the owner yet")


class WhenNobodySolvesItTheOwnerGetsTheRedCard(_Stall):
    def quiet_again(self, agent_id: str) -> None:
        """The agent answered the wake with one poll and nothing new; the window passes again."""
        with board.locked_state(self.root, allow_paused=True, operation="test clock") as state:
            state["agents"][agent_id]["last_poll_at"] = _ago(self.window + 60)

    def actions(self) -> list[str]:
        return [r["action"] for r in self_heal.run_once(self.root, self.launch, create=self.create)]

    def two_empty_wakes(self, agent_id: str) -> None:
        self.assertEqual(self.actions(), ["stall_wake"])
        board.poll(self.root, agent_id)
        self.assertEqual(self.actions(), ["stall_reset"])
        self.quiet_again(agent_id)
        self.assertEqual(self.actions(), ["stall_wake"])
        board.poll(self.root, agent_id)
        self.assertEqual(self.actions(), ["stall_reset"])
        self.quiet_again(agent_id)

    def blocked_for(self, agent_id: str, seconds: float) -> None:
        """The agent first said it is blocked this long ago; it is still polling and still reporting (neither is progress)."""
        with board.locked_state(self.root) as state:
            state["agents"][agent_id].update({"blocked_since": _ago(seconds), "last_poll_at": _ago(5)})

    def test_a_reviewer_that_stays_blocked_pauses_the_project_with_one_red_card(self):
        _, reviewer, _ = self.reviewer("weather-app")
        board.status(self.root, reviewer["id"], REVIEWER_WORDS, "blocked")
        self.assertEqual(self.actions(), [], "a fresh block is given its window")
        self.blocked_for(reviewer["id"], self.window + 60)
        self.assertEqual(self.actions(), ["stall_pause"], "no CTO to ask: the owner is told after one window, with no wake-up loop")
        self.assertEqual(board.pause_state(self.root)["status"], "paused")
        cards = self.cards()
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertTrue(card.get("blocked"), "the card is the red one")
        self.assertTrue(card["title"].startswith("Blocked, needs you:"), card["title"])
        problem, _, action = card["why"].partition("\n")
        self.assertTrue(problem.startswith("Problem: "), card["why"])
        self.assertIn(REVIEWER_WORDS, problem)
        self.assertTrue(action.startswith("What to do: "), card["why"])
        self.assertIn("press Resume", action)
        self.assertIn("has been blocked", self.events("stall_paused")[0]["message"])
        self.assertEqual(self.events("agent_stall_step"), [], "it was never woken")
        self.quiet_again(reviewer["id"])
        self.assertNotIn("stall_wake", self.actions(), "a paused project is not woken")
        self.assertEqual(len(self.cards()), 1, "one card, never a second")

    def cto_had_its_window(self, item_id: str) -> None:
        """The CTO was woken for the item a full window ago and cleared nothing."""
        with board.locked_state(self.root) as state:
            state["cto_pending"][item_id].update({"wake_pending": False, "last_woken_at": _ago(self.window + 60)})

    def asked_item(self) -> dict:
        return next(item for item in board.snapshot(self.root)["cto_pending"].values() if item["subject"].startswith("stalled_agent_needs_cto:"))

    def test_a_silent_builder_is_handed_to_the_cto_before_the_owner(self):
        _, builder, _ = self.delivery_with_task("weather-app")
        self.cto()
        board.status(self.root, builder["id"], "running the tests again", "working")
        self.silent(builder["id"], self.window + 60)
        self.two_empty_wakes(builder["id"])
        self.assertEqual(self.actions(), ["stall_ask_cto"])
        item = self.asked_item()
        self.assertEqual((item["reason"], item["status"], item["subject"]), ("stuck", "open", "stalled_agent_needs_cto:weather-app"))
        self.assertIn("answered 2 wake-ups with nothing new", item["detail"])
        self.assertTrue(item["wake_pending"], "the CTO is woken for it")
        self.assertEqual(self.actions(), [], "the CTO has its window: nothing reaches the owner yet")
        self.assertEqual((board.pause_state(self.root)["status"], self.cards()), ("active", []))
        self.cto_had_its_window(item["id"])
        self.assertEqual(self.actions(), ["stall_pause"])
        self.assertEqual(board.pause_state(self.root)["status"], "paused")
        card = next(c for c in self.cards() if c.get("blocked"))
        self.assertIn("running the tests again", card["why"])
        self.assertIn("The CTO was asked first and has not cleared it.", card["why"])
        self.assertEqual(len([i for i in board.snapshot(self.root)["cto_pending"].values() if i["subject"].startswith("stalled_agent_needs_cto:")]), 1)

    def test_a_quiet_reviewer_is_handed_to_the_cto_and_never_pauses_the_project(self):
        _, reviewer, _ = self.reviewer("weather-app")
        self.cto()
        self.silent(reviewer["id"], self.window + 60)
        self.two_empty_wakes(reviewer["id"])
        self.assertEqual(self.actions(), ["stall_ask_cto"])
        self.cto_had_its_window(self.asked_item()["id"])
        self.assertEqual(self.actions(), ["stall_stop_waking"])
        self.assertEqual((board.pause_state(self.root)["status"], self.cards()), ("active", []))
        self.quiet_again(reviewer["id"])
        self.assertEqual(self.actions(), [], "told once, then left alone")

    def block_item(self) -> dict:
        return next(item for item in board.snapshot(self.root)["cto_pending"].values() if item["subject"].startswith("reviewer_blocked:"))

    def test_the_owner_is_not_told_while_the_cto_still_has_its_turn(self):
        """Owner 2026-11-05: "The CTO needs to address the block before it gets to the user." """
        _, reviewer, _ = self.reviewer("weather-app")
        self.cto()
        board.status(self.root, reviewer["id"], REVIEWER_WORDS, "blocked")
        self.blocked_for(reviewer["id"], self.window + 60)
        self.assertEqual(self.block_item()["status"], "open")
        self.assertEqual(self.actions(), [], "the CTO's item is fresh: no pause, no card, no wake")
        self.assertEqual(board.pause_state(self.root)["status"], "active")
        self.assertEqual(self.cards(), [])
        self.cto_had_its_window(self.block_item()["id"])
        self.assertEqual(self.actions(), ["stall_pause"])
        card = next(c for c in self.cards() if c.get("blocked"))
        self.assertIn(REVIEWER_WORDS, card["why"])
        self.assertIn("The CTO was asked first and has not cleared it.", card["why"])

    def judged(self, cto_id: str, outcome: str, note: str, ago: float) -> None:
        """The CTO judged the Reviewer's block this many seconds ago."""
        item_id = self.block_item()["id"]
        board.record_cto_disposition(self.root, cto_id, item_id, outcome, note)
        with board.locked_state(self.root) as state:
            state["cto_pending"][item_id]["closed_at"] = _ago(ago)

    def test_after_the_cto_judged_and_the_agent_is_still_blocked_the_card_quotes_its_answer(self):
        _, reviewer, _ = self.reviewer("weather-app")
        _, cto, _ = self.cto()
        board.status(self.root, reviewer["id"], REVIEWER_WORDS, "blocked")
        self.blocked_for(reviewer["id"], 2 * self.window + 120)
        self.judged(cto["id"], "nothing", "The sign-in needs the owner's password; I cannot supply it.", self.window + 30)
        self.assertEqual(self.actions(), ["stall_pause"], "polling does not hide a block the CTO could not clear (Codex R1-2)")
        self.assertEqual(board.pause_state(self.root)["status"], "paused")
        card = next(c for c in self.cards() if c.get("blocked"))
        self.assertIn("The CTO was asked first; its answer: The sign-in needs the owner's password; I cannot supply it.", card["why"])

    def test_a_fix_the_cto_just_routed_is_given_a_window_to_land(self):
        _, reviewer, _ = self.reviewer("weather-app")
        _, cto, _ = self.cto()
        board.status(self.root, reviewer["id"], REVIEWER_WORDS, "blocked")
        self.blocked_for(reviewer["id"], 2 * self.window + 120)
        self.judged(cto["id"], "route", "Use the second test machine; its sign-in works.", 20)
        self.assertEqual(self.actions(), [], "the CTO answered a moment ago: the Reviewer has a window to act on it")
        board.status(self.root, reviewer["id"], "Running the review on the second machine.", "working")
        self.assertEqual(self.actions(), [])
        self.assertEqual((board.pause_state(self.root)["status"], self.cards()), ("active", []))

    def test_regular_polling_over_two_windows_does_not_hide_an_uncleared_block(self):
        """Codex R1-2, as it ran it: the CTO closes the item with "only the owner can", the Reviewer keeps polling."""
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        _, reviewer, _ = self.reviewer("weather-app")
        _, cto, _ = self.cto()
        board.status(self.root, reviewer["id"], REVIEWER_WORDS, "blocked")
        board.record_cto_disposition(self.root, cto["id"], self.block_item()["id"], "nothing", "Only the owner can supply that password.")
        actions = []
        for n in (1, 2):
            if board.pause_state(self.root)["status"] == "paused":
                break                                    # paused at the first window: the board takes no more polls
            future = datetime.now(timezone.utc) + timedelta(seconds=self.window * n + 5)
            with patch.object(board, "now", return_value=future.isoformat()):
                board.poll(self.root, reviewer["id"])
            actions += [r["action"] for r in self_heal.run_once(self.root, self.launch, current=future, create=self.create)]
        self.assertIn("stall_pause", actions)
        self.assertEqual(board.pause_state(self.root)["status"], "paused")
        self.assertEqual(len([c for c in self.cards() if c.get("blocked")]), 1)

    def repeated_reports(self, agent: dict, kind: str) -> None:
        """Codex R2-1, as it ran it: every status report moves the progress clock, so an agent that kept saying "blocked"
        reset its own escalation; and a repeat after the CTO's judgment must not reopen the CTO's turn."""
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        _, cto, _ = self.cto()
        board.status(self.root, agent["id"], REVIEWER_WORDS, "blocked")
        began = board.snapshot(self.root)["agents"][agent["id"]]["blocked_since"]
        item = next(i for i in board.snapshot(self.root)["cto_pending"].values() if i["subject"].startswith(kind + ":"))
        board.record_cto_disposition(self.root, cto["id"], item["id"], "nothing", "Only the owner can supply that password.")
        start, seen = datetime.now(timezone.utc), []
        for half in range(1, 7):                       # a poll and the unchanged report every half window, three windows
            future = start + timedelta(seconds=self.window * half / 2)
            with patch.object(board, "now", return_value=future.isoformat()):
                board.poll(self.root, agent["id"])
                board.status(self.root, agent["id"], REVIEWER_WORDS, "blocked")
            seen += [r["action"] for r in self_heal.run_once(self.root, self.launch, current=future, create=self.create)]
            if board.pause_state(self.root)["status"] == "paused":
                break
        self.assertEqual(seen, ["stall_pause"], "no wake, no second ask: one pause")
        self.assertLessEqual(half, 3, "once the CTO's answer has had its window, not three windows later")
        after = board.snapshot(self.root)
        self.assertEqual(after["agents"][agent["id"]]["blocked_since"], began, "the same block keeps its start")
        self.assertEqual(len([i for i in after["cto_pending"].values() if i["subject"].startswith(kind + ":")]), 1,
                         "a repeat after the CTO's judgment opens no new item")
        card = next(c for c in self.cards() if c.get("blocked") and c["for_agent"] == agent["id"])
        self.assertIn("its answer: Only the owner can supply that password.", card["why"])

    def test_a_reviewer_repeating_the_same_blocked_report_does_not_hide_the_block(self):
        self.repeated_reports(self.reviewer("weather-app")[1], "reviewer_blocked")

    def test_a_builder_repeating_the_same_blocked_report_does_not_hide_the_block(self):
        self.repeated_reports(self.delivery_with_task("weather-app")[1], "delivery_blocked")

    def test_a_block_ends_with_any_other_status_and_a_new_block_starts_its_own_clock(self):
        _, reviewer, _ = self.reviewer("weather-app")
        board.status(self.root, reviewer["id"], REVIEWER_WORDS, "blocked")
        first = board.snapshot(self.root)["agents"][reviewer["id"]]["blocked_since"]
        board.status(self.root, reviewer["id"], "The sign-in works again; running the review.", "working")
        self.assertNotIn("blocked_since", board.snapshot(self.root)["agents"][reviewer["id"]])
        board.status(self.root, reviewer["id"], "Blocked again: the machine went to sleep.", "blocked")
        self.assertGreater(board.snapshot(self.root)["agents"][reviewer["id"]]["blocked_since"], first)

    def test_after_the_owner_s_resume_a_still_blocked_agent_goes_to_the_cto_again_before_the_owner(self):
        """The block's gate moves to the owner's Resume, so the CTO's old judgment no longer counts: it is asked again."""
        from datetime import datetime, timezone
        _, reviewer, _ = self.reviewer("weather-app")
        _, cto, _ = self.cto()
        board.status(self.root, reviewer["id"], REVIEWER_WORDS, "blocked")
        self.blocked_for(reviewer["id"], 3 * self.window)
        self.judged(cto["id"], "nothing", "Only the owner can supply that password.", 2 * self.window)
        state = board.snapshot(self.root)
        agent, now = state["agents"][reviewer["id"]], datetime.now(timezone.utc)
        before = self_heal._block_gate(agent, None)
        self.assertEqual(before, agent["blocked_since"])
        self.assertEqual(self_heal._cto_turn(state, agent, before, now, self.window), "over", "judged two windows ago, still blocked")
        resumed = datetime.fromisoformat(_ago(self.window + 60))
        after = self_heal._block_gate(agent, resumed)
        self.assertEqual(after, resumed.isoformat())
        self.assertIsNone(self_heal._stuck_item(state, agent, after), "the judgment made before the Resume is not this turn's")
        self.assertEqual(self_heal._cto_turn(state, agent, after, now, self.window), "ask")

    def cto_at_the_wake_limit(self, cto: dict) -> None:
        """A CTO whose last wake went unanswered past the window, already woken twice at this gate with nothing new."""
        self.silent(cto["id"], self.window + 60)
        with board.locked_state(self.root) as state:
            agent = state["agents"][cto["id"]]
            state.setdefault("stall_wake_counts", {})[self_heal._stall_key(agent)] = {
                "gate": self_heal._progress_gate(agent, None), "count": self_heal.WAKES_WITHOUT_PROGRESS}

    def test_a_cto_that_stays_silent_while_the_builder_is_blocked_brings_the_owner_in(self):
        _, builder, _ = self.delivery_with_task("weather-app")
        _, cto, _ = self.cto()
        board.status(self.root, builder["id"], "Blocked: the weather service wants an account key I do not have.", "blocked")
        self.assertTrue(self_heal._somebody_waits_on_the_cto(board.snapshot(self.root)))
        self.cto_at_the_wake_limit(cto)
        self.assertEqual(self.actions(), ["stall_pause"])
        self.assertEqual(board.pause_state(self.root)["status"], "paused")
        blocked = [c for c in self.cards() if c.get("blocked")]
        self.assertEqual(len(blocked), 1)
        self.assertIn("the CTO is stuck and the project is paused", blocked[0]["title"])
        self.assertIn("Problem: ", blocked[0]["why"])
        self.assertIn("\nWhat to do: ", blocked[0]["why"])

    def test_nobody_waits_on_a_cto_when_nothing_is_stuck_or_blocked(self):
        _, builder, _ = self.delivery_with_task("weather-app")
        self.cto()
        board.status(self.root, builder["id"], "writing the app", "working")
        self.assertFalse(self_heal._somebody_waits_on_the_cto(board.snapshot(self.root)),
                         "a quiet CTO with nobody blocked is left alone as before (stall_stop_waking)")


class ACtoThatSaysItIsBlockedTellsTheOwnerAtOnce(_Stall):
    def test_one_red_card_with_the_cto_s_words_and_what_to_do(self):
        _, cto, _ = self.cto()
        board.status(self.root, cto["id"], CTO_WORDS, "blocked")
        cards = self.cards()
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0].get("blocked"))
        self.assertEqual(cards[0]["title"], "Blocked, needs you: the CTO cannot solve this on its own")
        self.assertIn(f"Problem: The CTO says it is blocked. Its words: {CTO_WORDS}", cards[0]["why"])
        self.assertIn("\nWhat to do: ", cards[0]["why"])
        board.status(self.root, cto["id"], CTO_WORDS + " Still waiting.", "blocked")
        self.assertEqual(len(self.cards()), 1, "a repeated note is the same card")

    def test_the_cto_can_say_it_is_blocked_after_the_watchdog_found_a_stall(self):
        """Codex R1-1: the waiting guard ("route the next action, then report") refused the CTO's own escalation."""
        _, builder, _ = self.delivery_with_task("weather-app")
        _, cto, _ = self.cto()
        board.status(self.root, builder["id"], "The test machine is unavailable; need the CTO to inspect it.", "blocked")
        self.silent(builder["id"], 3600)
        with board.locked_state(self.root) as state:
            state["agents"][builder["id"]].update(recovery_state="automatic_requested", automatic_recovery_requested_at=_ago(3000))
        board.mark_stalled(self.root)
        self.assertTrue(board._unrouted_failures(board.snapshot(self.root)), "the watchdog recorded a failure with no routed action")
        board.status(self.root, cto["id"], "The test machine requires the owner password; sign in to the test machine.", "blocked")
        blocked = [c for c in self.cards() if c.get("blocked") and c["for_agent"] == cto["id"]]
        self.assertEqual(len(blocked), 1)
        self.assertIn("requires the owner password", blocked[0]["why"])
        with self.assertRaisesRegex(ValueError, "route the next action"):
            board.status(self.root, cto["id"], "Waiting for the builder to come back.", "waiting")   # ordinary waiting is still refused
        with self.assertRaisesRegex(ValueError, "the owner must <what to do>"):
            board.status(self.root, cto["id"], "still watching", "blocked")   # "blocked" as another word for waiting: refused, with the how-to

    def test_blocked_as_another_word_for_waiting_raises_no_card(self):
        _, cto, _ = self.cto()
        board.status(self.root, cto["id"], "Blocked on the review; nothing for me until it ends.", "blocked")
        self.assertEqual(self.cards(), [], "no word for the owner: an ordinary waiting note, as before")

    def test_a_cto_that_is_only_watching_raises_nothing(self):
        _, cto, _ = self.cto()
        board.status(self.root, cto["id"], "Watching the review; nothing for me yet.", "watching")
        self.assertEqual(self.cards(), [])


PROBE = r"""
<script>
(async () => {
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const rendered = () => document.querySelector('#agents .agent-row');
  for (let attempt = 0; attempt < 150 && !rendered(); attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  await new Promise(resolve => setTimeout(resolve, 400));
  const attention = document.querySelector('#attention');
  const reading = {
    rendered: Boolean(rendered()),
    attention: {blocked: attention.classList.contains('blocked'), border: getComputedStyle(attention).borderTopColor,
                heading: (attention.querySelector('h2')?.textContent || '').trim(), text: attention.innerText},
    cards: Array.from(document.querySelectorAll('#owner-actions .owner-action')).map(card => {
      const style = getComputedStyle(card), text = card.querySelector('p');
      return {
        title: (card.querySelector('strong')?.textContent || '').trim(),
        blocked: card.classList.contains('blocked'), role: card.getAttribute('role') || '',
        border: style.borderTopColor, borderWidth: style.borderTopWidth, background: style.backgroundColor,
        titleColor: getComputedStyle(card.querySelector('strong')).color,
        text: text ? text.innerText : '', textLines: text ? text.innerText.split('\n').length : 0,
        visible: visible(card), top: card.getBoundingClientRect().top,
      };
    }),
  };
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(reading)});
})();
</script>
"""
RED, RED_SOFT = "rgb(180, 35, 24)", "rgb(255, 235, 233)"   # --red and --red-soft of the page


class TheBlockedCardIsRedOnTheRealPage(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        project_memory.initialize(self.root, project_name="Blocked card proof", description="Facts.")
        session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])

    def render(self) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Blocked card proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="blocked-card-proof", chat_action_token="owner-token"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=1000)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        self.assertTrue(sink["value"]["rendered"], "the page never rendered its agents; a blank render is not a pass")
        return sink["value"]

    def test_the_blocked_card_is_red_first_and_on_two_lines_and_a_plain_card_is_unchanged(self):
        board.record_owner_action(self.root, self.cto["id"], "Install the signing tool on this Mac", kind="other")
        board.status(self.root, self.cto["id"], CTO_WORDS, "blocked")
        reading = self.render()
        cards = reading["cards"]
        self.assertEqual(len(cards), 2, json.dumps(reading, indent=2)[:2000])
        red, plain = cards
        self.assertTrue(red["blocked"], "the blocked card comes first")
        self.assertEqual(red["title"], "Blocked, needs you: the CTO cannot solve this on its own")
        self.assertEqual(red["role"], "alert")
        self.assertEqual((red["border"], red["background"], red["titleColor"]), (RED, RED_SOFT, RED), json.dumps(red, indent=2))
        self.assertEqual(red["borderWidth"], "2px")
        self.assertEqual(red["textLines"], 2, red["text"])
        self.assertTrue(red["text"].startswith("Problem: The CTO says it is blocked."), red["text"])
        self.assertIn("\nWhat to do: ", red["text"])
        self.assertTrue(red["visible"] and plain["visible"])
        self.assertLess(red["top"], plain["top"])
        self.assertFalse(plain["blocked"])
        self.assertNotEqual(plain["background"], RED_SOFT)
        self.assertEqual(plain["role"], "")


    def test_a_cto_that_answers_nobody_is_a_red_warning_with_the_owner_s_action(self):
        plain = self.render()["attention"]
        self.assertFalse(plain["blocked"], "nothing is red while the CTO answers")
        self.assertNotEqual(plain["border"], RED)
        with board.locked_state(self.root) as state:
            state["agents"][self.cto["id"]]["recovery_state"] = "unresponsive"   # what the stall ladder records at its end
        red = self.render()["attention"]
        self.assertTrue(red["blocked"])
        self.assertEqual(red["border"], RED)
        self.assertIn("CTO is not responding", red["heading"])
        self.assertIn("Your action:", red["text"])


if __name__ == "__main__":
    unittest.main()

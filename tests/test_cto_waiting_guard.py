# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""2026-10-15 01:33Z, Studio project: Delivery's evidence failed and its staged review was cancelled (01:28:52Z);
nothing routed it (the only instruction queued afterwards was the controller's generic wake); the CTO posted
`status --state waiting --note "... staged review cancelled. Watching for its retry. Main clean."` and the board
accepted it. directives/CTO.md already calls that a defect. The board now refuses a waiting-shaped CTO status
while a failure of the current cycle has no routed action: "route the next action, then report".
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, control


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=self.session["id"])
        delivery_session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=delivery_session["id"],
        )
        with board.locked_state(self.root) as state:
            state["agents"][self.delivery["id"]].update({"task": "DECK", "active": True})
        configure = getattr(board, "configure_owner_questions", lambda _section: None)
        configure(None)
        self.addCleanup(configure, None)

    def event(self, kind: str, agent_id: str | None = None, **fields) -> None:
        with board.locked_state(self.root) as state:
            agent = state["agents"].get(agent_id) if agent_id else None
            board._event(state, kind, agent, {"task": "DECK", "message": kind, **fields})

    def events(self, kind: str) -> list[dict]:
        return [event for event in board.snapshot(self.root)["events"] if event["kind"] == kind]

    def cto_note(self, text: str, state: str = "waiting") -> dict:
        return board.status(self.root, self.cto["id"], text, state)


class WaitingGuardTests(_Fixture):
    def test_a_waiting_status_over_an_unrouted_cancelled_review_is_refused(self):
        self.event("staged_review_cancelled", self.delivery["id"], request_id="r-1")
        # the controller's generic wake is not a route
        self.event("agent_automatic_recovery_routed", self.delivery["id"])
        self.event("instruction_route_queued", self.delivery["id"])
        with self.assertRaisesRegex(ValueError, "route the next action, then report") as refused:
            self.cto_note("jls diagnostic: Delivery's evidence execution failed; staged review cancelled. Watching for its retry. Main clean.")
        self.assertIn("staged_review_cancelled on DECK at", str(refused.exception))
        agent = board.snapshot(self.root)["agents"][self.cto["id"]]
        self.assertNotIn("Watching for its retry", str(agent.get("status_note", "")))
        refused_events = self.events("cto_waiting_refused")
        self.assertEqual(len(refused_events), 1, "the refusal persists although the command was refused")
        self.assertEqual(refused_events[0]["failure_kind"], "staged_review_cancelled")
        for state in ("watching", "monitoring", "standby", "blocked"):
            with self.assertRaisesRegex(ValueError, "route the next action"):
                self.cto_note("still watching", state)

    def test_every_failure_kind_that_needs_a_route_is_covered_and_the_cto_own_stall_is_not(self):
        for kind in ("challenge_execution_failed", "agent_stalled", "agent_recovery_failed", "git_acceptance_failed", "remote_push_failed", "qa_pass_invalidated"):
            with self.subTest(kind=kind, cto_own=True), board.locked_state(self.root) as state:
                board._event(state, kind, state["agents"][self.cto["id"]], {"task": "GLOBAL_MONITOR", "message": "cto own"})
            with self.subTest(kind=kind):
                self.event(kind, self.delivery["id"])
                with self.assertRaisesRegex(ValueError, "route the next action"):
                    self.cto_note("watching", "waiting")
                # the CTO routes: a finding for Delivery
                board.record_finding(self.root, "DECK", f"Fix after {kind}", "the failure and its unblocker", True)
                self.assertEqual(self.cto_note(f"routed Delivery to fix {kind}", "working")["kind"], "status_update")
        # the CTO's own stall is the harness's job, not a failure the CTO must route
        with board.locked_state(self.root) as state:
            board._event(state, "agent_stalled", state["agents"][self.cto["id"]], {"task": "GLOBAL_MONITOR", "message": "cto stalled"})
        self.assertEqual(self.cto_note("standing by", "waiting")["kind"], "status_update")

    def test_a_cto_board_action_on_the_task_counts_as_routed(self):
        self.event("staged_review_cancelled", self.delivery["id"])
        board.record_owner_action(self.root, self.cto["id"], "Run the GPU proof for DECK by hand", command="bash proof.sh", task="DECK")
        self.assertEqual(self.cto_note("waiting for the owner's GPU proof; Delivery routed to the slides", "waiting")["kind"], "status_update")
        finding = board.record_finding(self.root, "DECK", "Export step fails", "the export and its fix", True)
        self.event("challenge_execution_failed", self.delivery["id"])
        with self.assertRaisesRegex(ValueError, "route the next action"):
            self.cto_note("watching", "waiting")
        board.route_around_finding(self.root, finding["id"], "not material: the owner wants the existing deck judged")
        self.assertEqual(self.cto_note("set aside; watching Delivery's rerun", "waiting")["kind"], "status_update")

    def test_the_agents_own_progress_counts_as_routed(self):
        self.event("staged_review_cancelled", self.delivery["id"])
        board.status(self.root, self.delivery["id"], "re-running the evidence with the fixed export", "working")
        self.assertEqual(self.cto_note("Delivery re-running; watching", "waiting")["kind"], "status_update")
        self.event("staged_review_cancelled", self.delivery["id"])
        with self.assertRaisesRegex(ValueError, "route the next action"):
            self.cto_note("watching again", "waiting")
        self.event("qa_requested", self.delivery["id"])
        self.assertEqual(self.cto_note("review requested again; watching", "waiting")["kind"], "status_update")
        self.event("challenge_execution_failed", self.delivery["id"])
        self.event("challenge_executed", self.delivery["id"])
        self.assertEqual(self.cto_note("the reviewer's retry ran; watching", "waiting")["kind"], "status_update")

    def test_an_owner_rejection_settles_nothing_but_acceptance_does(self):
        self.event("git_acceptance_failed", None)
        self.event("owner_release_decision_recorded", None, decision="not_accepted")
        with self.assertRaisesRegex(ValueError, "route the next action"):
            self.cto_note("owner rejected; watching the repair", "waiting")
        self.event("owner_release_decision_recorded", None, decision="accepted")
        self.assertEqual(self.cto_note("accepted; standing by", "waiting")["kind"], "status_update")

    def test_a_repeated_record_finding_still_settles(self):
        board.record_finding(self.root, "DECK", "Export step fails", "the export and its fix", True)
        self.event("staged_review_cancelled", self.delivery["id"])
        with self.assertRaisesRegex(ValueError, "route the next action"):
            self.cto_note("watching", "waiting")
        # the same finding again: the board returns the existing one AND records the repetition as a routing action
        board.record_finding(self.root, "DECK", "Export step fails", "the export and its fix", True)
        self.assertEqual(len(self.events("finding_reobserved")), 1)
        self.assertEqual(self.cto_note("same cause; Delivery routed", "waiting")["kind"], "status_update")

    def test_a_board_saved_before_this_change_is_seeded_on_load_from_the_durable_log(self):
        self.event("staged_review_cancelled", self.delivery["id"])
        self.event("challenge_execution_failed", self.delivery["id"])
        self.event("qa_requested", self.delivery["id"])  # settles both
        self.event("remote_push_failed", None)            # stays open
        for index in range(30):
            self.event("board_polled", self.delivery["id"], poll_counter=index)  # pushes the failure out of the hot window
        with board.locked_state(self.root) as state:
            state.pop("route_obligations", None)  # a board written before the list existed
            state["events"] = state["events"][-5:]  # the hot window moved past the failure; events.jsonl has it
        log = board.board_dir(self.root) / "events.jsonl"
        self.assertTrue(log.exists())
        with log.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")  # one damaged line must not discard the valid history around it
        seeded = board.snapshot(self.root)["route_obligations"]
        self.assertEqual([item["kind"] for item in seeded], ["remote_push_failed"], seeded)
        with self.assertRaisesRegex(ValueError, "remote_push_failed on DECK"):
            self.cto_note("watching", "waiting")

    def test_no_obligation_is_dropped_however_many_tasks_fail(self):
        with board.locked_state(self.root) as state:
            for index in range(205):
                board._event(state, "git_acceptance_failed", None, {"task": f"T{index}", "message": "failed"})
        self.assertEqual(len(board.snapshot(self.root)["route_obligations"]), 205)

    def test_a_routed_reintegration_settles(self):
        self.event("git_acceptance_reintegration_required", None)
        with self.assertRaisesRegex(ValueError, "route the next action"):
            self.cto_note("watching", "waiting")
        self.event("reintegration_routed", None, instruction_id="i-1")
        self.assertEqual(self.cto_note("Delivery routed to merge main; watching", "waiting")["kind"], "status_update")
        self.assertIn("reintegration_routed", [kind for kind in board.CTO_ROUTING_EVENTS])
        source = (Path(board.__file__)).read_text(encoding="utf-8")
        self.assertIn('_event(state, "reintegration_routed"', source, "route_reintegration records the event when its instruction is queued")

    def test_a_working_status_passes_but_does_not_discharge_the_obligation(self):
        self.event("staged_review_cancelled", self.delivery["id"])
        self.assertEqual(self.cto_note("triaging the failed evidence run", "working")["kind"], "status_update")
        # Codex plan check finding 1: a working note is not a route; the next waiting note is still refused
        with self.assertRaisesRegex(ValueError, "route the next action"):
            self.cto_note("nothing new this cycle", "waiting")
        board.record_finding(self.root, "DECK", "Evidence run fails on export", "the export step and its fix", True)
        self.assertEqual(self.cto_note("routed; waiting for Delivery's rerun", "waiting")["kind"], "status_update")

    def test_the_obligation_survives_the_trimmed_event_window(self):
        self.event("staged_review_cancelled", self.delivery["id"])
        with board.locked_state(self.root) as state:
            state["events"] = state["events"][-1:]  # the hot window moved on; the obligation did not
        with self.assertRaisesRegex(ValueError, "route the next action"):
            self.cto_note("watching", "waiting")
        self.assertEqual(len(board.snapshot(self.root)["route_obligations"]), 1)

    def test_a_reintegration_requirement_needs_a_route_and_a_completed_acceptance_or_push_settles_it(self):
        self.event("git_acceptance_reintegration_required", self.delivery["id"])
        with self.assertRaisesRegex(ValueError, "route the next action"):
            self.cto_note("watching", "waiting")
        self.event("git_acceptance_completed", None)
        self.assertEqual(self.cto_note("accepted; standing by", "waiting")["kind"], "status_update")
        self.event("remote_push_failed", None)
        with self.assertRaisesRegex(ValueError, "route the next action"):
            self.cto_note("watching the push", "waiting")
        self.event("remote_push_completed", None)
        self.assertEqual(self.cto_note("pushed; standing by", "waiting")["kind"], "status_update")

    def test_the_task_ending_settles_its_obligations(self):
        self.event("staged_review_cancelled", self.delivery["id"])
        self.event("task_cancelled", None)
        self.assertEqual(self.cto_note("task cancelled; standing by", "waiting")["kind"], "status_update")
        self.assertEqual(board.snapshot(self.root).get("route_obligations"), [])
        # the same failure twice is one obligation with a count
        self.event("staged_review_cancelled", self.delivery["id"])
        self.event("staged_review_cancelled", self.delivery["id"])
        self.assertEqual([o["count"] for o in board.snapshot(self.root)["route_obligations"]], [2])

    def test_delivery_and_reviewer_statuses_are_untouched(self):
        self.event("staged_review_cancelled", self.delivery["id"])
        self.assertEqual(board.status(self.root, self.delivery["id"], "waiting for the Reviewer", "waiting")["kind"], "status_update")

    def test_the_refusal_names_what_to_do_and_the_question_guard_still_runs_first(self):
        self.event("staged_review_cancelled", self.delivery["id"])
        with self.assertRaisesRegex(ValueError, "act, then report"):
            self.cto_note("Should I route Delivery to rerun the evidence?", "waiting")
        with self.assertRaisesRegex(ValueError, "record-finding --affects-current-task") :
            self.cto_note("watching", "waiting")


if __name__ == "__main__":
    unittest.main()


class RenderedWaitingGuardTests(unittest.TestCase):
    """The CTO row names what the CTO must route while an obligation is open (headless Chrome, the real page)."""

    def setUp(self):
        from harness import browser_acceptance, project_memory
        from tests.environment_support import require_loopback
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        project_memory.initialize(self.root, project_name="Waiting guard proof", description="Facts.")
        self.session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=self.session["id"])
        delivery_session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=delivery_session["id"])
        with board.locked_state(self.root) as state:
            state["agents"][self.delivery["id"]].update({"task": "DECK", "active": True})

    def render(self) -> dict:
        import json, threading, time
        from http.server import ThreadingHTTPServer
        from harness import board_viewer, browser_acceptance
        from tests.test_branding_rendered import probe_proxy
        probe = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 150 && !document.querySelector('#agents .agent-row'); attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  await new Promise(resolve => setTimeout(resolve, 400));
  const ctoRow = Array.from(document.querySelectorAll('#agents .agent-row')).find(row => (row.textContent || '').includes('Role: cto'));
  const reading = {rendered: Boolean(document.querySelector('#agents .agent-row')), ctoRowText: ctoRow ? ctoRow.textContent.trim() : ''};
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(reading)});
})();
</script>
"""
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Waiting guard proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="waiting-guard-proof", chat_action_token="owner-token",
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, probe))
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

    def test_the_cto_row_says_what_it_must_route(self):
        with board.locked_state(self.root) as state:
            board._event(state, "staged_review_cancelled", state["agents"][self.delivery["id"]], {"task": "DECK", "message": "evidence failed"})
        with board.locked_state(self.root) as state:
            state["agents"][self.cto["id"]].update({"liveness": "stalled", "liveness_note": "no poll"})  # another CTO state applies at the same time
        before = self.render()
        self.assertIn("The CTO has follow-up work to arrange on DECK", before["ctoRowText"])
        self.assertIn("stopped reporting", before["ctoRowText"], "the recovery explanation still shows beside the obligation")
        board.record_finding(self.root, "DECK", "Evidence run fails on export", "the export step and its fix", True)
        after = self.render()
        self.assertNotIn("CTO must route", after["ctoRowText"])

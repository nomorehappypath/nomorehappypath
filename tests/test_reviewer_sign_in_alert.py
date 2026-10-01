# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Batch 2 item C: a review waiting on a signed-out reviewer alerts the owner.

2026-10-01 00:10 UTC the board knew the Reviewer was signed out, but the only
signals sat inside the project page; the review waited until 02:29 (2h19m).
After 10 minutes the owner now gets one desktop notification, and the project
page and its Projects card say "Reviewer needs sign-in: nothing will progress".
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from harness import board, board_viewer, browser_acceptance, control, project_manager, project_memory, project_worker
from tests import test_sign_in_needed

_fixture = vars(test_sign_in_needed._Fixture)  # helpers only; that module's tests run there
_routing = vars(test_sign_in_needed.RoutingTests)
HEADLINE = "Reviewer needs sign-in: nothing will progress"


class _Incident(unittest.TestCase):
    setUp = _fixture["setUp"]
    tearDown = _fixture["tearDown"]
    ledger = _fixture["ledger"]
    qa_command = _fixture["qa_command"]
    declare_chunks = _fixture["declare_chunks"]
    delivery = _fixture["delivery"]
    reviewer = _fixture["reviewer"]
    sign_out = _fixture["sign_out"]
    open_review = _routing["open_review"]
    request = _routing["request"]

    def signed_out_review(self):
        dev = self.open_review()
        session, reviewer = self.reviewer()
        self.sign_out(session["id"])
        request = self.request(dev)
        board.route_open_reviews(self.root, retry_seconds=0)
        return session, reviewer, request

    def age_sign_in(self, minutes: float) -> None:
        with board.locked_state(self.root) as state:
            state["reviewer_needed"]["sign_in_since"] = (
                datetime.now(timezone.utc) - timedelta(minutes=minutes)
            ).isoformat()


class SignInAlertTests(_Incident):
    def test_ten_minutes_signed_out_with_a_waiting_review_alerts_the_owner_once(self):
        self.signed_out_review()
        needed = board.snapshot(self.root)["reviewer_needed"]
        self.assertTrue(needed["sign_in"])
        self.assertTrue(needed["sign_in_since"])
        self.age_sign_in(11)
        alerts = board.due_owner_alerts(self.root)
        self.assertEqual(alerts, [{
            "title": HEADLINE,
            "message": "Open the Reviewer's terminal and run /login. A review is waiting and nothing will progress until then.",
        }])
        self.assertEqual(board.due_owner_alerts(self.root), [], "one alert per incident")
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertEqual(board.due_owner_alerts(self.root), [], "re-routing does not restart the incident")
        events = [event for event in board.snapshot(self.root)["events"] if event["kind"] == "owner_alerted"]
        self.assertEqual(len(events), 1)
        self.assertIn(HEADLINE, events[0]["message"])

    def test_a_new_incident_after_sign_in_alerts_again(self):
        self.signed_out_review()
        self.age_sign_in(11)
        self.assertEqual(len(board.due_owner_alerts(self.root)), 1)
        with board.locked_state(self.root) as state:
            state["reviewer_needed"] = None  # the review was routed once the reviewer signed in
        board.route_open_reviews(self.root, retry_seconds=0)
        self.age_sign_in(12)
        self.assertEqual(len(board.due_owner_alerts(self.root)), 1)

    def test_under_ten_minutes_or_no_waiting_review_sends_nothing(self):
        self.signed_out_review()
        self.age_sign_in(9)
        self.assertEqual(board.due_owner_alerts(self.root), [])

    def test_a_signed_out_reviewer_with_no_waiting_review_sends_nothing(self):
        session, _ = self.reviewer()
        self.sign_out(session["id"])
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertIsNone(board.snapshot(self.root).get("reviewer_needed"))
        self.assertEqual(board.due_owner_alerts(self.root), [])

    def test_another_eligible_reviewer_takes_the_review_and_no_alert_is_raised(self):
        dev = self.open_review()
        out_session, _ = self.reviewer()
        self.sign_out(out_session["id"])
        _, live = self.reviewer()
        request = self.request(dev)
        board.route_open_reviews(self.root, retry_seconds=0)
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"][request["id"]]["routed_to"], live["id"])
        self.assertIsNone(state.get("reviewer_needed"))
        self.assertEqual(board.due_owner_alerts(self.root), [])

    def test_projects_page_card_says_nothing_will_progress(self):
        self.signed_out_review()
        state = board.snapshot(self.root)
        base = Path(tempfile.mkdtemp(dir=self.root))
        (base / "data" / "board").mkdir(parents=True)
        (base / "data" / "board" / "state.json").write_text(json.dumps(state), encoding="utf-8")
        entry = {"data_root": str(base / "data"), "code_root": str(base), "workspace_root": str(base / "ws")}
        row = project_manager.derive_status(entry)
        self.assertEqual(row["latest_progress"], f"{HEADLINE}. Open the Reviewer's terminal and run /login.")


class WatchdogNotificationTests(unittest.TestCase):
    def test_the_worker_pass_sends_each_due_alert_and_survives_a_failing_notifier(self):
        alert = {"title": HEADLINE, "message": "Open the Reviewer's terminal and run /login."}
        report = {"status": "active", "stalled": [], "owner_alerts": [alert]}
        watchdog = project_worker.ProjectWatchdog(Path("/nonexistent"))
        sent = []
        watchdog.notify = sent.append
        with patch.object(project_worker.control_plane, "tick", return_value=dict(report)):
            watchdog.tick()
        self.assertEqual(sent, [alert])

        def broken(_alert):
            raise OSError("osascript is missing")

        watchdog.notify = broken
        with patch.object(project_worker.control_plane, "tick", return_value=dict(report)), \
                patch("builtins.print") as printed:
            watchdog.tick()
        self.assertIn("owner notification failed: osascript is missing", printed.call_args[0][0])

    def test_desktop_notify_passes_texts_as_arguments_and_the_suite_is_silenced(self):
        with patch.object(project_worker.subprocess, "run") as run:
            self.assertFalse(project_worker.desktop_notify(HEADLINE, "x", "Studio"))
        run.assert_not_called()
        hostile = 'run /login" & do shell script "touch /tmp/pwned'
        with patch.dict("os.environ", {project_worker.DESKTOP_NOTIFICATIONS_ENV: "on"}), \
                patch.object(project_worker.sys, "platform", "darwin"), \
                patch.object(project_worker.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertTrue(project_worker.desktop_notify(HEADLINE, hostile, "Studio"))
        command = run.call_args[0][0]
        self.assertEqual(command[0], "/usr/bin/osascript")
        self.assertEqual(command[-3:], ["NoMoreHappyPath — Studio", HEADLINE, hostile])
        self.assertNotIn(hostile, " ".join(command[:-3]), "the message is never part of the script")
        self.assertNotIn("shell", run.call_args[1])


PROBE = r"""
<script>
(async () => {
  const find = () => { const node = document.querySelector('#sign-in-banner'); return node && !node.hidden ? node : null; };
  for (let attempt = 0; attempt < 400 && !find(); attempt++) await new Promise(r => setTimeout(r, 100));
  const banner = find(), rect = banner ? banner.getBoundingClientRect() : {width: 0, height: 0, top: 9999};
  const tasks = document.querySelector('#tasks'), tasksTop = tasks ? tasks.getBoundingClientRect().top : 0;
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    text: banner ? banner.textContent.replace(/\s+/g, ' ').trim() : '',
    visible: rect.width > 0 && rect.height > 0, aboveTasks: rect.top < tasksTop, role: banner ? banner.getAttribute('role') : '',
  })});
})();
</script>
"""


class RenderedSignInBannerTests(_Incident):
    def setUp(self):
        _fixture["setUp"](self)
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        from tests.environment_support import require_loopback
        require_loopback()
        project_memory.initialize(self.root, project_name="Sign-in alert proof", description="Facts.")

    def test_the_project_page_says_nothing_will_progress_at_the_top(self):
        from tests.test_branding_rendered import probe_proxy
        self.signed_out_review()
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Sign-in alert proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="sign-in-alert", chat_action_token="owner-token",
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close); self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory(); self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=1000)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        reading = sink.get("value")
        self.assertIsNotNone(reading, "Chrome reported no probe")
        self.assertTrue(reading["visible"], json.dumps(reading, indent=2))
        self.assertTrue(reading["aboveTasks"], json.dumps(reading, indent=2))
        self.assertEqual(reading["role"], "alert")
        self.assertTrue(reading["text"].startswith(HEADLINE), reading["text"])
        self.assertIn("run /login", reading["text"])


if __name__ == "__main__":
    unittest.main()

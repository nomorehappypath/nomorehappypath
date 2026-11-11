"""A builder whose window says it is working (a long silent test run) is not "nobody is working" (owner, 2026-11-11)."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from harness import control


class BusySession(unittest.TestCase):
    def check(self, turn_state, age_minutes, alive=True, cpu=False):
        now = datetime.now(timezone.utc)
        stamp = (now - timedelta(minutes=age_minutes)).isoformat()
        state = {"sessions": {"s1": {"status": "running", "pid": 1, "turn_state": turn_state, "turn_state_at": stamp}}}
        with mock.patch.object(control, "_read_state", return_value=state), \
             mock.patch.object(control, "_pid_is_alive", return_value=alive), \
             mock.patch.object(control, "_child_cpu_busy", return_value=cpu), \
             mock.patch.object(control, "ACTIVE_STATUSES", {"running"}):
            return control.session_busy(Path(tempfile.gettempdir()), "s1", now)

    def test_working_window_is_busy(self):
        self.assertTrue(self.check("working", 30))

    def test_idle_window_is_not_busy(self):
        self.assertFalse(self.check("idle_at_prompt", 30))

    def test_working_for_too_long_is_not_busy(self):
        self.assertFalse(self.check("working", 200))

    def test_dead_window_is_not_busy(self):
        self.assertFalse(self.check("working", 5, alive=False))

    def test_a_busy_command_under_a_codex_window_counts(self):
        self.assertTrue(self.check("", 500, cpu=True))

    def test_idle_card_email_waits_half_an_hour(self):
        from harness import warning_email
        fresh = {"idle_task": True, "recorded_at": datetime.now(timezone.utc).isoformat()}
        old = {"idle_task": True, "recorded_at": (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()}
        other = {"recorded_at": datetime.now(timezone.utc).isoformat()}
        self.assertTrue(warning_email._too_new_to_email(fresh))
        self.assertFalse(warning_email._too_new_to_email(old))
        self.assertFalse(warning_email._too_new_to_email(other))


if __name__ == "__main__":
    unittest.main()

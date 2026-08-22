# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import board, control


ROOT = Path(__file__).resolve().parents[1]


class WatchdogTests(unittest.TestCase):
    def test_direct_watchdog_routes_automatic_recovery_before_declaring_stall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id=session["id"])
            board.record_owner_direction(root, session["id"], "WATCHDOG-TASK")
            board.begin_task(root, agent["id"], "WATCHDOG-TASK")
            state_path = root / ".harness" / "board" / "state.json"
            state = json.loads(state_path.read_text())
            state["agents"][agent["id"]]["spawned_at"] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
            state["agents"][agent["id"]]["last_progress_at"] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
            state_path.write_text(json.dumps(state))
            result = subprocess.run(
                ["python3", str(ROOT / "harness" / "watchdog.py"), "--root", str(root), "--stale-after", "90", "--once"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stdout, "")
            self.assertEqual(board.snapshot(root)["agents"][agent["id"]]["liveness"], "recovering")
            routed = control.take_instructions(root, session["id"])
            self.assertEqual(len(routed), 1)
            self.assertIn("TASK ACTION DUE", routed[0]["text"])

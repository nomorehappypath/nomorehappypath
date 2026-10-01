# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Backlog #17: a governed supersede-subtask retires obsolete work, never live work.

On headless-browser-maya-mac two subtasks of an abandoned plan stayed open for
ever, so final acceptance refused although every live subtask had passed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from harness import board, board_surface, board_viewer, browser_acceptance, contract, cto, project_memory
from tests.environment_support import require_loopback
from tests import test_adaptive_planning
from tests.test_branding_rendered import probe_proxy

_planning = vars(test_adaptive_planning.AdaptivePlanningSimulationTests)  # helpers only; its tests run in their own module
REASON = "The owner replaced the notarized runtime with a credential-free official Google download."


class SupersedeSubtaskTests(unittest.TestCase):
    setUp = _planning["setUp"]
    tearDown = _planning["tearDown"]
    delivery = _planning["delivery"]
    ledger = _planning["ledger"]
    evidence = _planning["evidence"]
    request = _planning["request"]
    pass_request = _planning["pass_request"]

    def application(self, task="RUNTIME"):
        agent = self.delivery(task)
        board.define_delivery_plan(self.root, agent["id"], "application", "Runtime capabilities")
        board.declare_subtasks(self.root, agent["id"], [
            {"id": "acquisition", "title": "Runtime acquisition", "acceptance_proof": "Runtime downloads", "dependencies": []},
            {"id": "old-runtime", "title": "Notarized runtime", "acceptance_proof": "Notarized runtime ships", "dependencies": ["acquisition"]},
            {"id": "runtime-core", "title": "Official Google runtime", "acceptance_proof": "Google runtime ships", "dependencies": ["acquisition"]},
        ])
        return agent

    def cto_agent(self):
        return board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")

    def plan(self, task="RUNTIME"):
        return board.snapshot(self.root)["delivery_plans"][task]

    def test_cto_retires_obsolete_subtask_and_final_acceptance_runs(self):
        agent = self.application()
        self.pass_request(self.request(agent, "acquisition", "subtask_acceptance", subtask="acquisition"), "acquisition")
        self.pass_request(self.request(agent, "runtime-core", "subtask_acceptance", subtask="runtime-core"), "runtime-core")
        with self.assertRaisesRegex(ValueError, "every subtask acceptance to pass first: old-runtime"):
            self.request(agent, "final-blocked", "final_acceptance")

        retired = board.supersede_subtask(
            self.root, self.cto_agent()["id"], "RUNTIME", "old-runtime", REASON, ["runtime-core"],
        )
        self.assertEqual(retired["status"], "superseded")
        self.assertEqual(retired["superseded"]["reason"], REASON)
        self.assertEqual(retired["superseded"]["by_role"], "cto")
        self.assertEqual(retired["superseded"]["replaced_by"], ["runtime-core"])
        plan = self.plan()
        self.assertEqual(plan["structure_changes"][-1]["kind"], "product subtask superseded")
        event = board.snapshot(self.root)["events"][-1]
        self.assertEqual(event["kind"], "subtask_superseded")
        self.assertIn("The CTO retired subtask old-runtime (replaced by runtime-core)", event["message"])

        final = self.request(agent, "final", "final_acceptance")
        self.pass_request(final, "final")
        self.assertEqual(board.snapshot(self.root)["qa_requests"][final["id"]]["status"], "passed")

    def test_start_subtask_refuses_a_retired_subtask(self):
        agent = self.application()
        board.supersede_subtask(self.root, agent["id"], "RUNTIME", "old-runtime", REASON)
        with self.assertRaisesRegex(ValueError, "old-runtime was retired on .*credential-free.*nothing to start"):
            board.start_subtask(self.root, agent["id"], "old-runtime")
        self.assertEqual(self.plan()["subtasks"]["old-runtime"]["pipeline_status"], "superseded")
        # Product Management (the task's own Delivery Agent) may retire, and it is idempotent.
        again = board.supersede_subtask(self.root, agent["id"], "RUNTIME", "old-runtime", REASON)
        self.assertEqual(again["superseded"]["by_role"], "development")

    def test_live_work_is_never_retired_and_the_board_is_unchanged(self):
        agent = self.application()
        cto_id = self.cto_agent()["id"]

        def refused(subtask, pattern, *, actor=cto_id, task="RUNTIME", reason=REASON, replaced=None):
            before = json.dumps(self.plan(), sort_keys=True)
            with self.assertRaisesRegex(ValueError, pattern):
                board.supersede_subtask(self.root, actor, task, subtask, reason, replaced)
            self.assertEqual(json.dumps(self.plan(), sort_keys=True), before)

        refused("old-runtime", "plain-language reason", reason="")
        refused("missing", "no declared product subtask missing")
        refused("acquisition", "still needed by unfinished live subtasks: old-runtime, runtime-core")
        refused("old-runtime", "replacements must be other live subtasks", replaced=["old-runtime"])
        refused("old-runtime", "replacements must be other live subtasks", replaced=["nope"])

        board.start_subtask(self.root, agent["id"], "acquisition")
        refused("acquisition", "acquisition is in progress; live work cannot be retired")
        review = self.request(agent, "acquisition", "subtask_acceptance", subtask="acquisition")
        refused("acquisition", "acquisition is in review; live work cannot be retired")
        self.pass_request(review, "acquisition")
        refused("acquisition", "already passed review")

        failing = self.request(agent, "old-runtime", "subtask_acceptance", subtask="old-runtime")
        board.claim_qa(self.root, self.reviewer["id"], failing["id"], self.ledger("challenge-old", reviewer=True))
        board.execute_challenge(self.root, self.reviewer["id"], failing["id"])
        board.qa_result(self.root, self.reviewer["id"], failing["id"], "failed", "notarized runtime broken", self.evidence("old"))
        refused("old-runtime", "old-runtime is in progress; live work cannot be retired")
        # An older record without a pipeline status still carries its failing verdict.
        with board.locked_state(self.root) as state:
            state["delivery_plans"]["RUNTIME"]["subtasks"]["old-runtime"].pop("pipeline_status")
        refused("old-runtime", "has a failing review .*repaired, never retired")

        reviewer = self.reviewer["id"]
        refused("runtime-core", "only Product Management .* or the CTO", actor=reviewer)
        other = self.delivery("OTHER-TASK")
        refused("runtime-core", "only in its own task", actor=other["id"])

    def test_the_last_live_subtask_cannot_be_retired(self):
        agent = self.delivery("SOLO")
        board.define_delivery_plan(self.root, agent["id"], "application", "One capability")
        board.declare_subtasks(self.root, agent["id"], [
            {"id": "only", "title": "Only", "acceptance_proof": "Works", "dependencies": []},
        ])
        with self.assertRaisesRegex(ValueError, "last live subtask"):
            board.supersede_subtask(self.root, self.cto_agent()["id"], "SOLO", "only", REASON)

    def test_a_retired_subtask_never_hides_a_live_unpassed_one(self):
        agent = self.application()
        self.pass_request(self.request(agent, "acquisition", "subtask_acceptance", subtask="acquisition"), "acquisition")
        board.supersede_subtask(self.root, self.cto_agent()["id"], "RUNTIME", "old-runtime", REASON)
        with self.assertRaisesRegex(ValueError, "every subtask acceptance to pass first: runtime-core$"):
            self.request(agent, "final-early", "final_acceptance")

    def test_cto_completion_check_counts_live_subtasks_only(self):
        self.application()
        board.supersede_subtask(self.root, self.cto_agent()["id"], "RUNTIME", "old-runtime", REASON)
        with board.locked_state(self.root) as state:
            for name in ("acquisition", "runtime-core"):
                state["delivery_plans"]["RUNTIME"]["subtasks"][name]["status"] = "passed"
        artifact = {
            "branch": "main", "task_artifact_clean": True,
            "artifact_commit_pushed": True, "artifact_commit_exact": True,
            "artifact_health_verified": True, "task_artifact_release_verified": True,
            "artifact_health_output": "PASS", "head_commit": "abc",
        }
        with patch.object(cto, "_task_artifact_gate", return_value=artifact), \
                patch.object(cto, "ledger_complete", return_value=(True, [])), \
                patch.object(board, "simulation_evidence_complete", return_value=(True, [])), \
                patch.object(contract, "contract_complete", return_value=(True, [], {"objective": "x"})):
            checked = cto.release_check(self.root, "RUNTIME", self.root / "missing.md", self.root)
        self.assertTrue(checked["product_subtasks_complete"])

    def test_surface_authorizes_cto_and_delivery_only_and_pins_delivery_to_its_task(self):
        self.assertEqual(board_surface.AUTHORIZATION_MATRIX["supersede-subtask"], frozenset({"engineering", "cto"}))
        self.assertIn("supersede-subtask", board_surface.CURRENT_TASK_OPERATIONS)

    def test_real_board_cli_supersedes_with_reason_and_replacement(self):
        self.application()
        cto_agent = self.cto_agent()
        result = subprocess.run(
            [sys.executable, str(Path(board.__file__)), "--root", str(self.root), "supersede-subtask",
             "--agent", cto_agent["id"], "--task", "RUNTIME", "--subtask", "old-runtime",
             "--reason", REASON, "--replaced-by", "runtime-core"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.plan()["subtasks"]["old-runtime"]["superseded"]["replaced_by"], ["runtime-core"])

    def test_owner_task_card_shows_retired_work_and_counts_live_work(self):
        state = {
            "delivery_plans": {"APP": {"mode": "application", "rationale": "Runtime", "subtasks": {
                "core": {"title": "Official Google runtime", "status": "passed", "dependencies": [], "chunks": {}},
                "old": {"title": "Notarized runtime", "status": "superseded", "dependencies": [], "chunks": {},
                        "superseded": {"reason": REASON}},
            }, "structure_changes": [{"kind": "product subtask superseded", "superseded": ["old"],
                                      "replaced_by": ["core"], "reason": REASON, "at": "2026-09-30T22:40:00+00:00"}]}},
            "task_chunks": {}, "qa_requests": {}, "agents": {},
        }
        from tests.test_board_viewer import BoardViewerTests
        html = BoardViewerTests.rendered_task_cards(
            BoardViewerTests("run"), state, {"APP": {"task": "APP"}}, {"APP": "Ship the runtime."}, ["APP"],
        )[0]["html"]
        self.assertIn("1 product subtask independently accepted · 0 remaining", html)
        self.assertIn("<s>Notarized runtime</s> · retired: The owner replaced", html)
        self.assertIn("Work retired", html)
        self.assertIn("replaced by core", html)
        self.assertNotIn("Work added after planning", html)


PROBE = r"""
<script>
(async () => {
  const find = () => document.querySelector('#tasks .subtask-retired');
  for (let attempt = 0; attempt < 400 && !find(); attempt++) await new Promise(r => setTimeout(r, 100));
  const line = find(), rect = line ? line.getBoundingClientRect() : {width: 0, height: 0};
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    retired: line ? line.textContent.trim() : '',
    visible: rect.width > 0 && rect.height > 0,
    progress: Array.from(document.querySelectorAll('#tasks .progress-heading')).map(n => n.textContent.replace(/\s+/g, ' ').trim()),
    changes: Array.from(document.querySelectorAll('#tasks .scope-change strong')).map(n => n.textContent.trim()),
  })});
})();
</script>
"""


class RenderedSupersedeSubtaskTests(unittest.TestCase):
    setUp_planning = _planning["setUp"]
    delivery = _planning["delivery"]
    ledger = _planning["ledger"]
    evidence = _planning["evidence"]
    request = _planning["request"]
    pass_request = _planning["pass_request"]

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.setUp_planning()
        self.addCleanup(self.tmp.cleanup)
        project_memory.initialize(self.root, project_name="Supersede proof", description="Facts.")

    def test_the_task_card_shows_the_retired_subtask_with_the_owner_reason(self):
        agent = SupersedeSubtaskTests.application(self)
        self.pass_request(self.request(agent, "acquisition", "subtask_acceptance", subtask="acquisition"), "acquisition")
        cto_agent = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        board.supersede_subtask(self.root, cto_agent["id"], "RUNTIME", "old-runtime", REASON, ["runtime-core"])
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Supersede proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="supersede-proof",
            chat_action_token="supersede-token",
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close); self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory(); self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=900)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        reading = sink["value"]
        self.assertTrue(reading["visible"], json.dumps(reading, indent=2))
        self.assertIn("Notarized runtime · retired: The owner replaced the notarized runtime", reading["retired"])
        self.assertIn("Work retired", reading["changes"])
        self.assertNotIn("Work added after planning", reading["changes"])


if __name__ == "__main__":
    unittest.main()

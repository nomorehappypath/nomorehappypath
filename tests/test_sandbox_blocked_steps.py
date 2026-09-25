# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Steps the agent sandbox blocks (defects #6, #12, #16 of 2026-09-25).

The CTO's release check reuses the release coordinator's out-of-sandbox
record when the nested sandbox refuses the disposable checkout, and every
agent card says in plain words what its sandbox can reach.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from harness import agent_grant, board, board_viewer, browser_acceptance, control, cto, git_broker, project_memory, project_registry, release_coordinator
from harness.project_context import project_context
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy


class CoordinatorRecordReuseTests(unittest.TestCase):
    """The gate reuses ONLY the manager-owned record no agent can write.

    Round-1 reviewer finding: a record under board/evidence (inside every
    agent's write grant) could be forged and turned a failed archive/health
    check green. The trusted store is derived from the registry and lies
    beside the project's assigned storage, outside every granted root.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.home = base / "harness-home"
        self.home.mkdir()
        self.root = base / "project"          # the project root the CTO passes as --root
        self.root.mkdir()
        self.repo = self.root
        for arguments in (["init", "-q", "-b", "main"], ["config", "user.name", "Fixture"], ["config", "user.email", "fixture@example.invalid"]):
            subprocess.run(["git", *arguments], cwd=self.repo, check=True, capture_output=True)
        (self.repo / ".gitignore").write_text(".harness/\n", encoding="utf-8")
        (self.repo / "app.txt").write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore", "app.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "v1"], cwd=self.repo, check=True)
        self.commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        self.review = {"reviewed_commit": self.commit, "reviewed_files": ["app.txt"]}
        self.refused = subprocess.CompletedProcess(
            args=["git", "clone"], returncode=1, stdout="",
            stderr="sandbox-exec: sandbox_apply: Operation not permitted",
        )
        # The project is registered exactly as the launcher registers a
        # scaffold project: the default layout, data root inside the project.
        self.context = project_context(self.root)
        self.entry = project_registry.register(self.home, "fixture", self.root)
        self.home_patch = patch.object(project_registry, "default_home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def payload(self, commit: str, **extra) -> dict:
        value = {
            "reviewed_commit": commit, "artifact_archive_verified": True, "artifact_commit_exact": True,
            "artifact_health_verified": True, "artifact_health_output": "Ran 12 tests OK",
        }
        value.update(extra)
        return value

    def forge_in_agent_writable_evidence(self, commit: str) -> Path:
        path = board.board_dir(self.root) / "evidence" / "release-checks-TASK.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.payload(commit)), encoding="utf-8")
        return path

    def trusted_record(self, commit: str, **extra) -> Path:
        store = project_registry.trusted_project_store(self.root)
        self.assertIsNotNone(store)
        store.mkdir(parents=True, exist_ok=True)
        path = store / project_registry.trusted_record_name("TASK")
        path.write_text(json.dumps(self.payload(commit, **extra)), encoding="utf-8")
        return path

    def gate(self):
        with patch.object(git_broker.GitBroker, "materialize_readonly_candidate", return_value={"ok": False, "clone": self.refused, "checkout": None}):
            return cto._task_artifact_gate(self.root, "TASK", self.repo, self.review, True, "", check_remote=False)

    def test_the_trusted_store_lies_outside_every_agent_writable_root(self):
        store = project_registry.trusted_project_store(self.root)
        self.assertIsNotNone(store)
        # The grant is what the launcher computes from the REGISTERED roots.
        granted = [Path(value).resolve() for value in agent_grant.agent_writable_roots(
            self.root, self.entry["data_root"], self.entry["workspace_root"], project_root=self.root, home=self.home,
        )]
        self.assertTrue(granted)
        for root in granted:
            self.assertFalse(store == root or store.is_relative_to(root), f"{store} is inside the agent grant {root}")
        self.assertTrue(store.is_relative_to(self.home / "projects" / self.entry["id"]))

    def test_a_record_forged_under_the_agent_writable_evidence_folder_is_ignored(self):
        """The reviewer's round-1 reproduction: a matching record in board/evidence."""
        forged = self.forge_in_agent_writable_evidence(self.commit)
        self.assertTrue(forged.is_relative_to(self.context.data_root))
        result = self.gate()
        self.assertFalse(result["artifact_archive_verified"], json.dumps(result, indent=1, default=str))
        self.assertEqual(result["artifact_archive_source"], "")
        self.assertFalse(result["artifact_health_verified"])

    def test_the_nested_sandbox_refusal_reuses_the_trusted_record_for_the_exact_commit(self):
        self.trusted_record(self.commit)
        result = self.gate()
        self.assertTrue(result["artifact_archive_verified"], json.dumps(result, indent=1, default=str))
        self.assertEqual(result["artifact_archive_source"], "release-coordinator")
        self.assertIn("reusing the release coordinator", result["artifact_archive_note"])
        self.assertIn("Operation not permitted", result["artifact_archive_note"])
        self.assertTrue(result["artifact_health_verified"])
        self.assertEqual(result["artifact_health_output"], "Ran 12 tests OK")

    def test_without_a_matching_trusted_record_the_check_stays_failed(self):
        self.assertFalse(self.gate()["artifact_archive_verified"])
        self.trusted_record("0" * 40)
        self.assertFalse(self.gate()["artifact_archive_verified"])
        self.trusted_record(self.commit, artifact_archive_verified=False)
        self.assertFalse(self.gate()["artifact_archive_verified"])

    def test_the_coordinator_writes_the_trusted_copy_beside_the_human_readable_one(self):
        path = release_coordinator._record_trusted_checks(self.root, "TASK", self.payload(self.commit))
        self.assertIsNotNone(path)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["reviewed_commit"], self.commit)
        self.assertFalse(path.is_relative_to(self.context.data_root))
        self.assertTrue(self.gate()["artifact_archive_verified"])

    def test_a_path_like_task_id_is_refused_everywhere_and_the_registry_survives(self):
        """Round-2 reviewer finding: `../../../registry` overwrote the manager registry."""
        registry = self.home / "registry.json"
        before = registry.read_bytes()
        for bad in ("../../../registry", "a/b", "..", ".hidden", "x" * 121, "tab\tname", "with space"):
            with self.assertRaises(ValueError, msg=bad):
                project_registry.plain_task_id(bad)
            with self.assertRaises(ValueError, msg=bad):
                release_coordinator._record_trusted_checks(self.root, bad, self.payload(self.commit))
            self.assertEqual(cto._coordinator_recorded_checks(self.root, bad, self.commit), {})
        self.assertEqual(registry.read_bytes(), before, "the manager registry must be untouched")
        self.assertFalse((self.home / "registry.json.json").exists())
        store = project_registry.trusted_project_store(self.root)
        self.assertFalse(store.exists() and any(store.iterdir()) if store.exists() else False)
        # the board refuses the id at the door, before any file or branch carries it
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            board.record_owner_direction(root, session["id"], "Do a thing.")
            with self.assertRaisesRegex(ValueError, "one plain name"):
                board.begin_task(root, agent["id"], "../../../registry")
            self.assertEqual(board.snapshot(root)["agents"][agent["id"]]["task"], board.AWAITING_OWNER_DIRECTION)
            self.assertEqual(registry.read_bytes(), before)
        # plain names still work and hash to one file inside the store
        name = project_registry.trusted_record_name("film-shot-quality_v2.1")
        self.assertRegex(name, r"^[0-9a-f]{32}\.json$")

    def test_a_link_standing_in_for_the_store_or_the_record_is_refused(self):
        store = project_registry.trusted_project_store(self.root)
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.symlink_to(elsewhere, target_is_directory=True)
        self.assertIsNone(project_registry.trusted_project_store(self.root), "a linked store must not be used")
        self.assertIsNone(release_coordinator._record_trusted_checks(self.root, "TASK", self.payload(self.commit)))
        self.assertEqual(list(elsewhere.iterdir()), [], "nothing may be written through the link")
        self.assertEqual(cto._coordinator_recorded_checks(self.root, "TASK", self.commit), {})
        store.unlink()
        store.mkdir()
        record = store / project_registry.trusted_record_name("TASK")
        decoy = self.home / "decoy.json"
        decoy.write_text(json.dumps(self.payload(self.commit)), encoding="utf-8")
        record.symlink_to(decoy)
        self.assertEqual(cto._coordinator_recorded_checks(self.root, "TASK", self.commit), {}, "a linked record must not be trusted")
        self.assertFalse(self.gate()["artifact_archive_verified"])

    def test_an_unregistered_project_has_no_trusted_store_and_no_reuse(self):
        with patch.object(project_registry, "default_home", return_value=self.home / "elsewhere"):
            self.assertIsNone(project_registry.trusted_project_store(self.root))
            self.assertIsNone(release_coordinator._record_trusted_checks(self.root, "TASK", self.payload(self.commit)))
            self.assertFalse(self.gate()["artifact_archive_verified"])


PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 100 && !document.querySelector('#agents .agent-row'); attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const row = document.querySelector('#agents .agent-row');
  const view = row ? Array.from(row.querySelectorAll('button')).find(b => b.textContent.trim() === 'View status') : null;
  if (view) view.click();
  await new Promise(resolve => setTimeout(resolve, 300));
  const terms = Array.from(document.querySelectorAll('#status-dialog-body dt')).map(n => n.textContent.trim());
  const reach = Array.from(document.querySelectorAll('#status-dialog-body dt')).find(n => n.textContent.trim() === 'Can reach');
  const dd = reach ? reach.nextElementSibling : null;
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    dialogOpen: Boolean(document.querySelector('#status-dialog')?.open), terms,
    reachText: dd ? dd.textContent.trim() : '',
    reachVisible: Boolean(dd) && dd.getBoundingClientRect().height > 0,
  })});
})();
</script>
"""


class RenderedReachTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        project_memory.initialize(self.root, project_name="Reach proof", description="Facts.")
        session = control.create(self.root, "claude_cto")
        board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])

    def test_the_status_dialog_says_what_the_cto_can_reach(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Reach proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="reach-proof", chat_action_token="reach-token",
        ))
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
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=900)
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        reading = sink["value"]
        self.assertTrue(reading["dialogOpen"], json.dumps(reading, indent=2))
        self.assertIn("Can reach", reading["terms"])
        self.assertTrue(reading["reachVisible"])
        self.assertIn("nested sandbox", reading["reachText"])
        self.assertIn("Application Support", reading["reachText"])


if __name__ == "__main__":
    unittest.main()

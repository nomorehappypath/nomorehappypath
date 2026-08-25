# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Project registry, activation, and migration simulations (spec §6, §14.9).

Load-bearing scenarios: concurrent activation (exactly one winner), stale-lock
reclamation, atomic write crash safety, duplicate/overlap rejection including
case-insensitive and symlinked spellings of the same directory (cycle-4 F8
identity rule), and idempotent migration of the existing single-root harness.

Run:  PYTHONPATH=. python3 -m unittest tests.test_project_registry -v
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import project_registry as registry


class ProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "harness-home"

    def _project_dir(self, name: str) -> Path:
        path = self.base / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---- S-REG-001: schema round-trip, stored fields only ----
    def test_schema_round_trip_and_stored_fields_only(self):
        code = self._project_dir("alpha")
        entry = registry.register(self.home, "alpha", code, description="first project")
        loaded = registry.load(self.home)
        self.assertEqual(loaded["version"], registry.REGISTRY_VERSION)
        self.assertEqual(len(loaded["projects"]), 1)
        stored = loaded["projects"][0]
        self.assertEqual(stored["id"], entry["id"])
        self.assertEqual(
            set(stored),
            {"id", "name", "description", "code_root", "data_root", "workspace_root",
             "kind", "created_at", "last_active_at"},
            "registry stores declared fields only — derived state is computed at read time",
        )
        # Compatibility mapping: data/workspace derived exactly as the reviewed context.
        self.assertEqual(stored["data_root"], str(code.resolve() / ".harness"))
        # A future/corrupt version fails loudly instead of guessing.
        payload = json.loads(registry._registry_path(self.home).read_text())
        payload["version"] = 99
        registry._registry_path(self.home).write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "version"):
            registry.load(self.home)

    # ---- S-REG-002: duplicates and overlap rejected (incl. F8 identity rule) ----
    def test_duplicate_overlap_case_and_symlink_rejected(self):
        code = self._project_dir("beta")
        registry.register(self.home, "beta", code)
        with self.assertRaisesRegex(ValueError, "already exists"):
            registry.register(self.home, "beta", self._project_dir("beta2"))
        with self.assertRaisesRegex(ValueError, "already uses"):
            registry.register(self.home, "beta-copy", code)
        # A symlinked spelling of the same directory is the same project.
        link = self.base / "beta-link"
        link.symlink_to(code)
        with self.assertRaisesRegex(ValueError, "already uses"):
            registry.register(self.home, "beta-linked", link)
        # Case-variant spelling on a case-insensitive filesystem (macOS default)
        # resolves to the same inode and must be rejected; on a case-sensitive
        # filesystem the variant simply does not exist and the guard is moot.
        variant = self.base / "BETA"
        if variant.exists():
            with self.assertRaisesRegex(ValueError, "already uses"):
                registry.register(self.home, "beta-case", variant)
        # A data root nested inside another project's repository is rejected.
        other = self._project_dir("gamma")
        outside_ws = self._project_dir("gamma-ws")
        with self.assertRaisesRegex(ValueError, "nest"):
            registry.register(
                self.home, "gamma", other, kind="adopted",
                data_root=code / "gamma-data", workspace_root=outside_ws,
            )

    # ---- S-REG-003: adopted projects keep harness data OUTSIDE the repo ----
    def test_adopted_defaults_to_manager_owned_outside_roots(self):
        repo = self._project_dir("adopted-repo")
        defaulted = registry.register(self.home, "adopted", repo, kind="adopted")
        self.assertEqual(Path(defaulted["data_root"]).parent.parent, (self.home / "projects").resolve())
        self.assertEqual(Path(defaulted["workspace_root"]).parent.parent, (self.home / "projects").resolve())
        self.assertNotIn(repo, Path(defaulted["data_root"]).parents)
        self.assertNotIn(repo, Path(defaulted["workspace_root"]).parents)

        nested_repo = self._project_dir("nested-adopted-repo")
        with self.assertRaisesRegex(ValueError, "OUTSIDE"):
            registry.register(
                self.home, "nested-adopted", nested_repo, kind="adopted",
                data_root=nested_repo / ".harness", workspace_root=self._project_dir("adopted-ws"),
            )
        explicit_repo = self._project_dir("explicit-adopted-repo")
        entry = registry.register(
            self.home, "explicit-adopted", explicit_repo, kind="adopted",
            data_root=self.base / "adopted-data", workspace_root=self.base / "adopted-ws2",
        )
        self.assertEqual(entry["kind"], "adopted")
        # Registration itself added NOTHING to the adopted repository.
        self.assertEqual(list(repo.iterdir()), [], "adopt purity: zero files in the adopted tree")
        self.assertEqual(list(explicit_repo.iterdir()), [], "explicit adoption also keeps the tree pristine")

    # ---- S-REG-004: atomic writes — a crash mid-save never corrupts the registry ----
    def test_atomic_save_crash_leaves_registry_intact(self):
        code = self._project_dir("delta")
        registry.register(self.home, "delta", code)
        before = registry._registry_path(self.home).read_text()
        with patch.object(os, "replace", side_effect=OSError("crash between temp and rename")):
            with self.assertRaises(OSError):
                registry.register(self.home, "delta2", self._project_dir("delta2"))
        self.assertEqual(registry._registry_path(self.home).read_text(), before,
                         "a failed save must leave the previous registry byte-identical")
        leftovers = [p for p in self.home.iterdir() if p.name.startswith(".registry")]
        self.assertEqual(leftovers, [], "temp files are cleaned up after a failed save")

    # ---- S-REG-005: backups are written and rotation is bounded ----
    def test_backup_rotation_bounded(self):
        code = self._project_dir("epsilon")
        entry = registry.register(self.home, "epsilon", code)
        for i in range(registry.BACKUP_KEEP + 4):
            registry.update_entry(self.home, entry["id"], description=f"rev {i}")
        backups = list((self.home / registry.BACKUP_DIRNAME).glob("registry-*.json"))
        self.assertTrue(backups, "saves must produce backups")
        self.assertLessEqual(len(backups), registry.BACKUP_KEEP)

    # ---- S-REG-006: unhealthy rows are detected, repairable, and never fatal ----
    def test_health_repair_and_remove(self):
        code = self._project_dir("zeta")
        entry = registry.register(self.home, "zeta", code)
        self.assertTrue(registry.entry_health(entry)["ok"])
        moved = self.base / "zeta-moved"
        code.rename(moved)
        stale = registry.entries(self.home)[0]
        health = registry.entry_health(stale)
        self.assertFalse(health["ok"])
        self.assertIn("code_root missing", health["reasons"][0])
        # The registry still loads and other operations continue (never crash the landing).
        other = registry.register(self.home, "eta", self._project_dir("eta"))
        self.assertTrue(registry.entry_health(other)["ok"])
        # Repair by re-pointing; the row turns healthy.
        repaired = registry.update_entry(self.home, entry["id"], code_root=moved,
                                         data_root=moved / ".harness",
                                         workspace_root=moved.parent / ".harness-task-workspaces")
        self.assertTrue(registry.entry_health(repaired)["ok"])
        removed = registry.remove(self.home, other["id"])
        self.assertEqual(removed["id"], other["id"])
        self.assertEqual(len(registry.entries(self.home)), 1)
        self.assertTrue(moved.is_dir(), "remove never touches the project's folders")

    # ---- S-REG-007 (load-bearing): concurrent activation — exactly one winner ----
    def test_concurrent_activation_exactly_one_winner(self):
        entry = registry.register(self.home, "theta", self._project_dir("theta"))
        second = registry.register(self.home, "iota", self._project_dir("iota"))
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def attempt(project_id: str):
            barrier.wait()
            try:
                registry.activate(self.home, project_id)
                outcomes.append("won")
            except RuntimeError:
                outcomes.append("refused")

        threads = [threading.Thread(target=attempt, args=(p,)) for p in (entry["id"], second["id"])]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(outcomes), ["refused", "won"], "one activation wins, the other fails loudly")
        active = registry.active_project(self.home)
        self.assertIsNotNone(active)
        registry.deactivate(self.home, active["project_id"])
        self.assertIsNone(registry.active_project(self.home))

    # ---- S-REG-007b: a half-written lock is never mistaken for an abandoned one ----
    def test_unreadable_lock_is_refused_not_reclaimed(self):
        """The race that let two activations win, reduced to a deterministic case.

        `activate` used to create the lock with O_EXCL and write the record
        afterwards. A concurrent caller reading inside that window saw an empty
        file, failed to parse it, concluded the lock was stale, and reclaimed
        it - so both callers won and the second overwrote the first. An
        unreadable lock must therefore never read as abandoned.
        """
        entry = registry.register(self.home, "mu", self._project_dir("mu"))
        self.home.mkdir(parents=True, exist_ok=True)
        registry._lock_path(self.home).write_text("")          # the mid-write window
        with self.assertRaisesRegex(RuntimeError, "unreadable"):
            registry.activate(self.home, entry["id"])
        self.assertEqual(registry._lock_path(self.home).read_text(), "",
                         "a refused activation must not overwrite the existing lock")
        audit = self.home / registry.AUDIT_FILENAME
        if audit.exists():
            self.assertNotIn("reclaimed stale activation lock", audit.read_text(),
                             "an unreadable lock is not a stale lock")

    # ---- S-REG-007d: the create-then-write window, forced open ----
    def test_activation_window_is_not_mistaken_for_a_stale_lock(self):
        """The race, made deterministic instead of hoped for.

        A plain concurrent test only fails when the scheduler happens to land a
        second caller inside the window between creating the lock and writing
        it - on a fast machine it usually does not, so passing proves nothing.
        This holds the window open: while the lock path is being created with
        O_EXCL, the creating thread sleeps before returning. The pre-fix code
        then reliably lets the second caller read an empty lock, judge it
        stale, and reclaim it, so both callers win.

        The fixed code creates a temporary file rather than the lock path and
        publishes it with os.link, so the hook never fires and there is no
        window to hold open.
        """
        first = registry.register(self.home, "rho", self._project_dir("rho"))
        second = registry.register(self.home, "sigma", self._project_dir("sigma"))
        lock = registry._lock_path(self.home)
        real_open = os.open

        def holding_open(path, flags, mode=0o777, **keywords):
            descriptor = real_open(path, flags, mode, **keywords)
            if str(path) == str(lock) and flags & os.O_EXCL:
                time.sleep(0.3)
            return descriptor

        outcomes: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def attempt(project_id: str):
            barrier.wait()
            try:
                registry.activate(self.home, project_id)
                outcomes[project_id] = "won"
            except RuntimeError:
                outcomes[project_id] = "refused"

        with patch.object(os, "open", holding_open):
            threads = [threading.Thread(target=attempt, args=(p,))
                       for p in (first["id"], second["id"])]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(sorted(outcomes.values()), ["refused", "won"],
                         "exactly one activation may win, even inside the window")
        winner = [key for key, value in outcomes.items() if value == "won"][0]
        active = registry.active_project(self.home)
        self.assertIsNotNone(active, "the winner holds no lock")
        self.assertEqual(active["project_id"], winner,
                         "the lock names a project that did not win")

    # ---- S-REG-008: stale lock (dead pid) is reclaimed with an audit record ----
    def test_stale_lock_reclaimed_live_lock_respected(self):
        entry = registry.register(self.home, "kappa", self._project_dir("kappa"))
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.home.mkdir(parents=True, exist_ok=True)
        registry._lock_path(self.home).write_text(json.dumps(
            {"pid": dead.pid, "project_id": "ghost", "project_name": "ghost", "acquired_at": "old"}))
        self.assertIsNone(registry.active_project(self.home), "a dead-pid lock reads as inactive")
        record = registry.activate(self.home, entry["id"])
        self.assertEqual(record["project_id"], entry["id"])
        self.assertIn("reclaimed stale activation lock",
                      (self.home / registry.AUDIT_FILENAME).read_text())
        # A LIVE lock is never overridden.
        other = registry.register(self.home, "lam", self._project_dir("lam"))
        with self.assertRaisesRegex(RuntimeError, "already active"):
            registry.activate(self.home, other["id"])
        registry.deactivate(self.home, entry["id"])
        with self.assertRaisesRegex(RuntimeError, "not"):
            registry._lock_path(self.home).write_text(json.dumps(
                {"pid": os.getpid(), "project_id": entry["id"], "acquired_at": "x"}))
            registry.deactivate(self.home, other["id"])

    # ---- S-REG-009: migration of the existing single-root harness, idempotent ----
    def test_single_root_migration_idempotent(self):
        root = self._project_dir("legacy-harness")
        (root / ".harness" / "board").mkdir(parents=True)
        (root / ".harness" / "board" / "state.json").write_text("{}")
        first = registry.migrate_single_root(self.home, root)
        self.assertEqual(first["kind"], "scaffold")
        self.assertEqual(first["data_root"], str(root.resolve() / ".harness"))
        again = registry.migrate_single_root(self.home, root)
        self.assertEqual(again["id"], first["id"], "migration must be idempotent")
        self.assertEqual(len(registry.entries(self.home)), 1)
        # History stayed exactly where it was.
        self.assertTrue((root / ".harness" / "board" / "state.json").is_file())


if __name__ == "__main__":
    unittest.main()

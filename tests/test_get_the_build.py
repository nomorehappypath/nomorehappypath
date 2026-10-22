# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Get the build: a program built on the owner's test machine reaches the owner (finding 13, 2026-10-21).

Run:  PYTHONPATH=. python3 -m unittest tests.test_get_the_build -v

The collection tests use a REAL sshd and the real ssh/scp programs against a fake "machine" that is a folder on this
computer, with the POSIX hashing command. A real Windows machine (PowerShell Get-FileHash, Windows OpenSSH scp, a
reparse point) is a named dependency: only the generated PowerShell text is checked here.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import board, board_mcp, board_surface, build_collection, contract, project_registry, release_coordinator, release_preview, test_machine
from tests.machine_support import FakeMachine
from tests.test_requirements_contract import context, delivery_with_direction
from tests.test_test_machine import connected_descriptor

TASK = "TASK-BUILD"
HOW_TO = dict(platform="Linux x86-64", requirements="none", steps=["Download why.bin", "chmod +x why.bin", "./why.bin --help"], expect="a usage message")
CONTENT = {"why.bin": b"\x7fELF-why-binary" * 100, "why.txt": b"notes\n"}


def declare(root, agent_id, outputs=("why.bin",), **changes):
    values = {**HOW_TO, **changes}
    return board.declare_build(root, agent_id, list(outputs), values["platform"], values["requirements"], values["steps"], values["expect"], values.get("with_web_app", False))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DeclarationTests(unittest.TestCase):
    def test_names_limits_and_text_are_validated(self):
        ok = build_collection.validate_declaration(["why.exe", "why.pdb"], "Windows x64", "administrator", ["Download", "Run it"], "a table")
        self.assertEqual([item["name"] for item in ok["outputs"]], ["why.exe", "why.pdb"])
        for bad in ("", "../x", "a/b", "a\\b", ".hidden", "C:x", "x y", "con", "NUL.txt", "lpt1.bin", "x" * 101, "trailing.", "-flag", "a;b", "manifest.json", "Manifest.JSON"):
            with self.assertRaises(build_collection.BuildError, msg=bad):
                build_collection.validate_declaration([bad], "p", "r", ["s"], "e")
        for outputs in ([], ["a"] * 2, [f"f{i}" for i in range(11)], ["Why.exe", "why.EXE"]):
            with self.assertRaises(build_collection.BuildError):
                build_collection.validate_declaration(outputs, "p", "r", ["s"], "e")
        for kwargs in (dict(platform=""), dict(requirements="  "), dict(steps=[]), dict(steps=["s"] * 11), dict(expect=""), dict(steps=["x" * 2001])):
            values = dict(outputs=["a"], platform="p", requirements="r", steps=["s"], expect="e")
            values.update(kwargs)
            with self.assertRaises(build_collection.BuildError, msg=kwargs):
                build_collection.validate_declaration(**values)

    def test_the_launch_check_lines_are_read_but_never_required(self):
        declaration = build_collection.validate_declaration(["a.bin", "b.bin", "c.bin"], "p", "r", ["s"], "e")
        found = build_collection.launch_checks("smoke: a.bin ok\nSmoke: b.bin unverified: the declared target is Windows\nsmoke: z.bin ok\n", declaration)
        self.assertEqual(found["a.bin"], {"status": "ok", "note": ""})
        self.assertEqual(found["b.bin"], {"status": "unverified", "note": "the declared target is Windows"})
        self.assertEqual(found["c.bin"]["status"], "unverified", "no line means not launched")
        self.assertNotIn("z.bin", found)

    def test_the_task_must_be_usable_as_a_folder_name_on_windows(self):
        for task in ("2026-10-21_get-the-build", "TASK-1", "a.b"):
            self.assertEqual(build_collection.task_component(task), task)
        for task in ("con", "nul.x", "task.", "../x", "a b", ""):
            with self.assertRaises(build_collection.BuildError, msg=task):
                build_collection.task_component(task)

    def test_the_certified_lines_must_match_the_declaration_exactly(self):
        declaration = build_collection.validate_declaration(["a.bin", "b.bin"], "p", "r", ["s"], "e")
        line = lambda name, digit, size: f"build: {name} sha256={digit * 64} size={size}\n"
        output = "Ran 4 tests\n" + line("a.bin", "a", 10) + "noise\n  " + line("b.bin", "B", 20).strip() + "\n"
        found = build_collection.certified_lines(output, declaration)
        self.assertEqual(found["b.bin"], {"sha256": "b" * 64, "size": 20})
        for output, why in (("Ran 1\n" + line("a.bin", "a", 1), "missing"), (line("a.bin", "a", 1) + line("b.bin", "b", 1) + line("c.bin", "c", 1), "not declared"),
                            (line("a.bin", "a", 1) + line("a.bin", "b", 1) + line("b.bin", "b", 1), "twice")):
            with self.assertRaisesRegex(build_collection.BuildError, why):
                build_collection.certified_lines(output, declaration)
        with self.assertRaisesRegex(build_collection.BuildError, "200 MiB"):
            build_collection.certified_lines(line("a.bin", "a", build_collection.MAX_BYTES + 1) + line("b.bin", "b", 1), declaration)

    def test_the_powershell_check_covers_every_ancestor_and_the_file(self):
        script = build_collection._windows_script(TASK, "why.exe", "a" * 64)
        for needle in ("ReparsePoint", "Get-FileHash", "-Algorithm SHA256", "PSIsContainer", f"harness-builds\\{TASK}\\{'a' * 64}", "$r,$q,$d,$f"):
            self.assertIn(needle, script)
        posix = build_collection._posix_script(TASK, "why.bin", "a" * 64)
        self.assertEqual(posix.count("[ ! -L"), 4)
        self.assertIn("-d ", posix)
        self.assertIn("-f ", posix)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))
        self.delivery = delivery_with_direction(self.root, TASK)
        board.create_contract(self.root, self.delivery["id"], "Build why.exe", ["why runs"])

    def test_a_declaration_is_part_of_the_reviewed_scope_and_legacy_contracts_hash_as_before(self):
        before = board._contract_revision(self.root, TASK)
        declare(self.root, self.delivery["id"])
        after = board._contract_revision(self.root, TASK)
        self.assertNotEqual(before["sha256"], after["sha256"], "the review certifies the declaration")
        value = contract.build_declaration(self.root, TASK)
        self.assertEqual(value["platform"], "Linux x86-64")
        self.assertEqual(contract.contract_preflight(self.root, TASK)[0], True)
        declare(self.root, self.delivery["id"], steps=["Download", "Run"])
        self.assertNotEqual(board._contract_revision(self.root, TASK)["sha256"], after["sha256"], "a corrected declaration is a different scope")
        legacy = delivery_with_direction(self.root, "TASK-LEGACY")
        board.create_contract(self.root, legacy["id"], "A web app", ["it works"])
        self.assertEqual(contract.build_declaration(self.root, "TASK-LEGACY"), {})

    def test_a_build_added_outside_the_declaration_path_is_a_scope_violation(self):
        path = board.project_context(self.root).storage_path("tasks", f"{TASK}.json")
        value = json.loads(path.read_text())
        value["build"] = {"outputs": [{"name": "evil.exe"}], "platform": "x", "requirements": "x", "steps": ["x"], "expect": "x"}
        path.write_text(json.dumps(value))
        valid, problems = contract.contract_preflight(self.root, TASK)
        self.assertFalse(valid)
        self.assertTrue(any("immutable" in problem for problem in problems), problems)

    def row(self, request_id, status, cycle):
        return {"id": request_id, "task": TASK, "phase": "final_acceptance", "status": status, "cycle": cycle, "subtask": "", "chunk": "",
                "developer_id": self.delivery["id"], "claimed_by": "", "review_wait_started_at": "2026-10-21T08:00:00+00:00", "requested_at": "2026-10-21T08:00:00+00:00"}

    def test_declaring_follows_the_current_state_of_the_candidate_not_history(self):
        with board.locked_state(self.root) as state:
            state["qa_requests"]["final-1"] = self.row("final-1", "failed", 1)
        declare(self.root, self.delivery["id"])
        for status in ("claimed", "authoring"):   # in flight
            with board.locked_state(self.root) as state:
                state["qa_requests"]["final-2"] = self.row("final-2", status, 2)
            with self.assertRaisesRegex(ValueError, "in flight"):
                declare(self.root, self.delivery["id"], expect="changed")
        with board.locked_state(self.root) as state:   # a standing PASS freezes the declaration
            state["qa_requests"]["final-2"] = self.row("final-2", "passed", 2)
            state["qa_requests"]["final-2"]["completed_at"] = "2026-10-21T10:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "frozen by a standing PASS"):
            declare(self.root, self.delivery["id"], expect="changed")
        # historical PASS -> the CTO's reopen -> corrected declaration
        with board.locked_state(self.root) as state:
            state.setdefault("candidate_scope_reopens", {})[TASK] = {"recorded_at": "2026-10-21T11:00:00+00:00"}
        declare(self.root, self.delivery["id"], expect="changed")
        self.assertEqual(contract.build_declaration(self.root, TASK)["expect"], "changed")
        # historical PASS -> a newer failed final product review -> corrected declaration
        with board.locked_state(self.root) as state:
            state["candidate_scope_reopens"].pop(TASK)
            state["qa_requests"]["final-3"] = self.row("final-3", "failed", 3)
            state["qa_requests"]["final-3"]["completed_at"] = "2026-10-21T12:00:00+00:00"
        declare(self.root, self.delivery["id"], outputs=("why.bin", "why.txt"))
        self.assertEqual(len(contract.build_declaration(self.root, TASK)["outputs"]), 2)

    def test_only_a_developer_with_a_task_declares(self):
        session = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        with self.assertRaisesRegex(ValueError, "only an active Delivery Agent"):
            declare(self.root, session["id"])


class SurfaceTests(unittest.TestCase):
    def test_roles_and_the_mcp_tools(self):
        matrix = board_surface.AUTHORIZATION_MATRIX
        self.assertEqual(matrix["declare-build"], frozenset({"engineering"}))
        self.assertEqual(matrix["collect-build"], frozenset({"engineering", "cto"}))
        self.assertIn("collect-build", board_mcp.LONG_RUNNING)
        tools = {tool["name"]: tool for tool in board_mcp.tool_definitions("engineering")}
        self.assertIn("declare-build", tools)
        self.assertIn("collect-build", tools)
        self.assertEqual(set(tools["declare-build"]["inputSchema"]["required"]), {"output", "platform", "requirements", "step", "expect"})
        self.assertNotIn("declare-build", {tool["name"] for tool in board_mcp.tool_definitions("qa")})
        self.assertIn("collect-build", {tool["name"] for tool in board_mcp.tool_definitions("cto")})
        self.assertNotIn("declare-build", {tool["name"] for tool in board_mcp.tool_definitions("cto")})
        argv = board_mcp.argv_for("declare-build", {"output": ["a.bin", "b.bin"], "platform": "p", "requirements": "none", "step": ["s1", "s2"], "expect": "e", "with_web_app": True})
        self.assertEqual(argv[:1], ["declare-build"])
        self.assertEqual(argv.count("--output"), 2)
        self.assertEqual(argv.count("--step"), 2)
        self.assertIn("--with-web-app", argv)
        board_mcp  # noqa: B018

    def test_every_listed_operation_is_a_parser_command(self):
        self.assertIn("declare-build", board_surface.ALL_BOARD_OPERATIONS)
        self.assertIn("collect-build", board_surface.AGENT_ARGUMENT_OPERATIONS)
        self.assertIn("collect-build", board_surface.CURRENT_TASK_OPERATIONS)


class MachineFixture(unittest.TestCase):
    """A board root registered with Mission Control's registry, a connected test machine backed by a real sshd, and a passed final review."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.root = context(base)
        self.home = base / "harness-home"
        self.home.mkdir()
        project_registry.register(self.home, "fixture", self.root)
        patcher = mock.patch.object(project_registry, "default_home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.machine = FakeMachine(self)
        self.machine.__enter__()
        self.retained = self.machine.base / "retained"
        patcher = mock.patch.object(build_collection, "REMOTE_ROOT", str(self.retained))
        patcher.start()
        self.addCleanup(patcher.stop)
        paths = test_machine.Connector(self.root, Path("."), "", {}).paths()
        paths["dir"].mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(paths["key"])], check=True)
        self.machine.authorized_keys.write_text(paths["pub"].read_text())
        found = test_machine.scan_host_key("127.0.0.1", self.machine.port)
        paths["known_hosts"].write_text(found["line"] + "\n")
        connected_descriptor(self.root, port=self.machine.port, user=self.machine.user, os="linux", host_key={"type": found["type"], "fingerprint": found["fingerprint"], "line": found["line"]})
        self.stamp = test_machine.stamp_of(test_machine.read(self.root))
        self.delivery = delivery_with_direction(self.root, TASK)
        board.create_contract(self.root, self.delivery["id"], "Build why", ["why runs"])
        session = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        self.cto = session

    def retain(self, name: str, data: bytes, *, digest: str | None = None) -> str:
        digest = digest or sha(data)
        folder = self.retained / TASK / digest
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
        return digest

    def final_pass(self, lines: dict[str, tuple[str, int]], *, stamp=None, request_id="final-1", tree="t" * 40, smoke: str = ""):
        """A passed final review whose single certified unit-test execution printed `lines` (name -> (sha256, size))."""
        output = "Ran 3 tests in 0.1s\n\nOK\n" + "".join(f"build: {name} sha256={digest} size={size}\n" for name, (digest, size) in lines.items()) + smoke
        stamp = stamp if stamp is not None else self.stamp
        request = {"id": request_id, "task": TASK, "phase": "final_acceptance", "status": "passed", "cycle": 1, "requested_at": board.now(),
                   "subtask": "", "chunk": "", "developer_id": self.delivery["id"], "claimed_by": "", "review_wait_started_at": board.now(),
                   "reviewed_commit": "c" * 40, "reviewed_tree_hash": tree, "contract_revision": {"sha256": board._contract_revision(self.root, TASK)["sha256"]},
                   "command_executions": [{"kind": "unit_test", "exit_code": 0, "execution_identity": "i" * 64, "execution_record_id": "execution-1", "machine": stamp}]}
        patcher = mock.patch.object(board, "_unit_test_build_source", side_effect=lambda root, req: (output, dict(stamp)))
        patcher.start()
        self.addCleanup(patcher.stop)
        with board.locked_state(self.root) as state:
            state["qa_requests"][request_id] = request
        return request

    def build(self, files=("why.bin",), smoke="smoke: why.bin ok\n", **declaration):
        declare(self.root, self.delivery["id"], outputs=files, **declaration)
        lines = {}
        for name in files:
            data = CONTENT[name]
            self.retain(name, data)
            lines[name] = (sha(data), len(data))
        self.final_pass(lines, smoke=smoke)
        return lines


class CollectionTests(MachineFixture):
    def test_collects_exactly_the_certified_bytes_and_records_them(self):
        self.build(("why.bin", "why.txt"))
        result = board.collect_build(self.root, TASK, self.delivery["id"])
        manifest = result["manifest"]
        self.assertEqual({item["name"]: item["sha256"] for item in manifest["outputs"]}, {name: sha(data) for name, data in CONTENT.items()})
        self.assertEqual(manifest["reviewed_tree_hash"], "t" * 40)
        self.assertEqual(manifest["machine"]["stamp"], self.stamp)
        self.assertEqual({name: item["status"] for name, item in manifest["launch_checks"].items()}, {"why.bin": "ok", "why.txt": "unverified"})
        state = board.snapshot(self.root)
        record = state["builds"][TASK]
        self.assertEqual(record["manifest_id"], manifest["manifest_id"])
        self.assertIn(manifest["manifest_id"], state["build_index"])
        store = build_collection.store_root(self.root)
        self.assertFalse(str(store).startswith(str(self.root)), "the store lies outside every agent-writable root")
        files = sorted(path.name for path in (build_collection.manifest_path(self.root, TASK, manifest["manifest_id"]).parent).iterdir())
        self.assertEqual(files, ["manifest.json", "why.bin", "why.txt"])
        self.assertEqual(board.build_release_check(self.root, state, TASK, state["qa_requests"]["final-1"]), (True, ""))
        again = board.collect_build(self.root, TASK, self.cto["id"])
        self.assertTrue(again["already_collected"], "the CTO's collect after the coordinator's is a no-op")
        kinds = [event["kind"] for event in state["events"] if event["kind"].startswith("build_")]
        self.assertEqual(kinds, ["build_declared", "build_collected"])

    def test_the_file_changed_after_the_test_is_not_collected(self):
        lines = self.build()
        self.retain("why.bin", b"something else entirely", digest=lines["why.bin"][0])   # same folder, different bytes
        with self.assertRaisesRegex(ValueError, "not the file the certified run printed"):
            board.collect_build(self.root, TASK, self.delivery["id"])
        note = board.snapshot(self.root)["build_collection"][TASK]
        self.assertEqual((note["status"], note["class"]), ("failed", "output"))
        self.assertNotIn(TASK, board.snapshot(self.root).get("builds", {}))
        self.assertEqual(self.stored_manifests(), [], "nothing was published")

    def stored_manifests(self):
        try:
            folder = build_collection._task_folder(self.root, TASK)
        except build_collection.BuildError:
            return []
        return sorted(path.name for path in folder.iterdir() if path.name.startswith("b-")) if folder.exists() else []

    def test_a_missing_file_a_link_and_a_second_missing_output_publish_nothing(self):
        lines = self.build(("why.bin", "why.txt"))
        (self.retained / TASK / lines["why.txt"][0] / "why.txt").unlink()
        with self.assertRaisesRegex(ValueError, "regular file in regular folders"):
            board.collect_build(self.root, TASK, self.delivery["id"])
        self.assertEqual(self.stored_manifests(), [], "the first output copied, the second failed: the set is all or nothing")
        # a symbolic link in place of the file, and as an ancestor
        target = self.retained / "elsewhere.txt"
        target.write_bytes(CONTENT["why.txt"])
        os.symlink(target, self.retained / TASK / lines["why.txt"][0] / "why.txt")
        with self.assertRaisesRegex(ValueError, "regular file in regular folders"):
            board.collect_build(self.root, TASK, self.delivery["id"])
        (self.retained / TASK / lines["why.txt"][0] / "why.txt").unlink()
        (self.retained / TASK / lines["why.txt"][0] / "why.txt").write_bytes(CONTENT["why.txt"])
        moved = self.retained / TASK / lines["why.bin"][0]
        real = self.retained / "real"
        moved.rename(real)
        os.symlink(real, moved)
        with self.assertRaisesRegex(ValueError, "regular file in regular folders"):
            board.collect_build(self.root, TASK, self.delivery["id"])
        self.assertEqual(self.stored_manifests(), [])

    def test_the_failure_classes_and_the_retry_rule(self):
        self.build()
        state = board.snapshot(self.root)
        request = state["qa_requests"]["final-1"]
        digest = build_collection.declaration_digest(contract.build_declaration(self.root, TASK))
        # an unreachable machine is temporary: machine class, retried by time
        self.machine.stop()
        with self.assertRaises(ValueError) as raised:
            board.collect_build(self.root, TASK, self.delivery["id"])
        self.assertEqual(raised.exception.klass, "machine")
        note = board.snapshot(self.root)["build_collection"][TASK]
        self.assertEqual(note["class"], "machine")
        self.assertFalse(board.build_collection_due(board.snapshot(self.root), TASK, request, digest, note["machine_stamp"]), "just tried: wait")
        with mock.patch.object(build_collection, "RETRY_SECONDS", 0):
            self.assertTrue(board.build_collection_due(board.snapshot(self.root), TASK, request, digest, note["machine_stamp"]), "temporary: retried")
        # the stale class: the machine changed since the review ran
        self.machine.start()
        test_machine.update(self.root, lambda value: value.update({"revision": 7}))
        with self.assertRaisesRegex(ValueError, "changed, was paused or needs the owner's password"):
            board.collect_build(self.root, TASK, self.delivery["id"])
        note = board.snapshot(self.root)["build_collection"][TASK]
        self.assertEqual(note["class"], "stale")
        self.assertFalse(board.build_collection_due(board.snapshot(self.root), TASK, request, digest, note["machine_stamp"]), "a stale review waits for a resubmission")
        self.assertTrue(board.build_collection_due(board.snapshot(self.root), TASK, {**request, "id": "final-2"}, digest, note["machine_stamp"]), "a new review is tried at once")
        self.assertTrue(board.build_collection_due(board.snapshot(self.root), TASK, request, "x" * 64, note["machine_stamp"]), "a corrected declaration is tried at once")
        # the CTO is told, with the class and the next step
        instruction = board._automatic_recovery_instruction({"role": "cto", "id": self.cto["id"], "task": "x"}, board.snapshot(self.root))
        self.assertIn("BUILD NOT COLLECTED", instruction)
        self.assertIn("resubmits the final review", instruction)

    def test_a_cancelled_task_a_paused_machine_and_a_moved_review_stop_the_commit(self):
        self.build()
        original = build_collection.collect

        def replace_review(*arguments, **keywords):
            with board.locked_state(self.root) as state:
                state["qa_requests"]["final-9"] = {**state["qa_requests"]["final-1"], "id": "final-9", "cycle": 9, "requested_at": board.now()}
            return original(*arguments, **keywords)

        with mock.patch.object(build_collection, "collect", side_effect=replace_review):
            with self.assertRaisesRegex(ValueError, "newer final review replaced"):
                board.collect_build(self.root, TASK, self.delivery["id"])
        self.assertEqual(self.stored_manifests(), [], "the published folder was removed again")
        self.assertNotIn(TASK, board.snapshot(self.root).get("builds", {}))
        with board.locked_state(self.root) as state:
            state["qa_requests"].pop("final-9")
        test_machine.mark_reauth(self.root, "project_stopped")
        with self.assertRaisesRegex(ValueError, "changed, was paused or needs the owner's password"):
            board.collect_build(self.root, TASK, self.delivery["id"])

    def test_an_orphan_folder_left_by_a_crash_is_removed_before_the_next_collection(self):
        self.build()
        folder = build_collection._task_folder(self.root, TASK)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "b-0123456789abcdef").mkdir()
        (folder / "b-0123456789abcdef" / "why.bin").write_bytes(b"orphan")
        board.collect_build(self.root, TASK, self.delivery["id"])
        self.assertNotIn("b-0123456789abcdef", self.stored_manifests())
        self.assertEqual(len(self.stored_manifests()), 1)

    def test_a_copy_that_grows_past_the_certified_size_is_stopped_and_cleaned_up(self):
        self.build()
        shim = self.machine.base / "scp-shim"
        shim.write_text("#!/bin/sh\nfor last; do :; done\nhead -c 4000000 /dev/zero > \"$last\"\nsleep 5\n")
        shim.chmod(0o755)
        with mock.patch.object(build_collection, "scp_program", return_value=str(shim)):
            with self.assertRaisesRegex(ValueError, "grew past"):
                board.collect_build(self.root, TASK, self.delivery["id"])
        folder = build_collection._task_folder(self.root, TASK)
        self.assertEqual([path.name for path in folder.iterdir() if path.name.startswith((".collect-", "b-"))], [], "no partial file is left")

    def test_limits_the_size_the_total_and_the_missing_scp(self):
        self.build()
        with mock.patch.object(build_collection, "MAX_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "at most"):
                board.collect_build(self.root, TASK, self.delivery["id"])
        with mock.patch.object(build_collection, "MAX_TOTAL_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "add up to"):
                board.collect_build(self.root, TASK, self.delivery["id"])
        with mock.patch.object(build_collection, "scp_program", return_value="/nonexistent/scp"):
            with self.assertRaisesRegex(ValueError, "no `scp` program"):
                board.collect_build(self.root, TASK, self.delivery["id"])
        self.assertEqual(board.snapshot(self.root)["build_collection"][TASK]["class"], "machine")

    def test_a_declaration_corrected_after_the_pass_cannot_release_the_old_review(self):
        self.build()
        board.collect_build(self.root, TASK, self.cto["id"])
        with board.locked_state(self.root) as state:
            state["qa_requests"]["final-1"]["completed_at"] = "2026-10-21T10:00:00+00:00"
            state.setdefault("candidate_scope_reopens", {})[TASK] = {"recorded_at": "2026-10-21T11:00:00+00:00"}
        declare(self.root, self.delivery["id"], steps=["A corrected step"])
        state = board.snapshot(self.root)
        ok, why = board.build_release_check(self.root, state, TASK, state["qa_requests"]["final-1"])
        self.assertFalse(ok)
        self.assertIn("fresh final review", why)
        # the coordinator's automatic recollection must not recertify the changed declaration under the old PASS
        request = state["qa_requests"]["final-1"]
        self.assertIsNotNone(release_coordinator._collect_declared_build(self.root, state, TASK, request))
        with self.assertRaisesRegex(ValueError, "fresh final review"):
            board.collect_build(self.root, TASK, self.cto["id"])
        self.assertFalse(board.build_release_check(self.root, board.snapshot(self.root), TASK, request)[0])
        # a genuinely fresh final PASS (certifying the corrected declaration) permits collection and release
        with board.locked_state(self.root) as live:
            live["qa_requests"]["final-1"]["status"] = "failed"
        self.final_pass({"why.bin": (sha(CONTENT["why.bin"]), len(CONTENT["why.bin"]))}, request_id="final-2", smoke="smoke: why.bin ok\n")
        with board.locked_state(self.root) as live:
            live["qa_requests"]["final-2"]["cycle"] = 2
        self.assertIsNone(release_coordinator._collect_declared_build(self.root, board.snapshot(self.root), TASK, board.snapshot(self.root)["qa_requests"]["final-2"]))
        state = board.snapshot(self.root)
        self.assertEqual(state["builds"][TASK]["final_request_id"], "final-2")
        self.assertTrue(board.build_release_check(self.root, state, TASK, state["qa_requests"]["final-2"])[0])

    def test_the_operation_has_one_time_budget(self):
        self.build()
        with mock.patch.object(build_collection, "OPERATION_SECONDS", -1):
            with self.assertRaisesRegex(ValueError, "took longer"):
                board.collect_build(self.root, TASK, self.delivery["id"])

    def test_only_the_tasks_own_delivery_and_the_cto_may_collect(self):
        self.build()
        other = delivery_with_direction(self.root, "TASK-OTHER")
        with self.assertRaisesRegex(ValueError, "only its own task"):
            board.collect_build(self.root, TASK, other["id"])
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        with self.assertRaisesRegex(ValueError, "only Delivery or the CTO"):
            board.collect_build(self.root, TASK, reviewer["id"])


class ReleaseGateAndCardTests(MachineFixture):
    def checks(self, **changes):
        value = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        value.update({"head_commit": "c" * 40, "build_required": True, "build_collected": False, "build_problem": "the declared build is not collected for this review"})
        value.update(changes)
        return value

    def test_record_release_is_refused_until_the_build_is_collected_and_snapshots_it(self):
        self.build()
        with self.assertRaisesRegex(ValueError, "build_collected.*not collected"):
            board.record_release_ready(self.root, self.cto["id"], TASK, self.checks())
        board.collect_build(self.root, TASK, self.cto["id"])
        state = board.snapshot(self.root)
        ok, why = board.build_release_check(self.root, state, TASK, state["qa_requests"]["final-1"])
        self.assertTrue(ok, why)
        release = board.record_release_ready(self.root, self.cto["id"], TASK, self.checks(build_collected=True, build_problem=""))
        snapshot = release["build"]
        self.assertEqual(snapshot["platform"], "Linux x86-64")
        self.assertEqual(snapshot["steps"], HOW_TO["steps"])
        self.assertEqual(snapshot["outputs"][0]["sha256"], sha(CONTENT["why.bin"]))
        self.assertEqual(snapshot["reviewed_commit"], "c" * 40)
        self.assertEqual(snapshot["launch_checks"]["why.bin"]["status"], "ok")
        self.assertFalse(snapshot["with_web_app"])
        cards = board.build_cards(self.root, board.snapshot(self.root), [TASK, "TASK-NONE"])
        self.assertEqual(set(cards), {TASK})
        self.assertTrue(cards[TASK]["intact"])

    def test_a_project_without_a_build_is_unaffected(self):
        release = board.record_release_ready(self.root, self.cto["id"], TASK, {key: True for key in board.RELEASE_REQUIRED_CHECKS} | {"head_commit": "c" * 40})
        self.assertNotIn("build", release)

    def test_the_gate_catches_a_changed_file_a_changed_declaration_and_a_changed_tree(self):
        self.build()
        board.collect_build(self.root, TASK, self.cto["id"])
        state = board.snapshot(self.root)
        request = state["qa_requests"]["final-1"]
        path = build_collection.manifest_path(self.root, TASK, state["builds"][TASK]["manifest_id"]).parent / "why.bin"
        self.assertTrue(board.build_release_check(self.root, state, TASK, request)[0])
        self.assertEqual(board.build_release_check(self.root, state, TASK, {**request, "reviewed_tree_hash": "z" * 40})[0], False, "a different tree is a different build")
        self.assertTrue(board.build_release_check(self.root, state, TASK, {**request, "reviewed_commit": "d" * 40})[0], "a repin keeps the same tree and so the same build")
        path.write_bytes(CONTENT["why.bin"][:-1] + b"X")   # same size: only the digest can tell
        self.assertIn("no longer matches the digest", board.build_release_check(self.root, state, TASK, request)[1])
        self.assertTrue(board.build_cards(self.root, state, [TASK])[TASK]["intact"], "the page's cheap check does not hash; the gate and every download do")
        path.write_bytes(b"short")
        self.assertIn("not the file that was collected", board.build_release_check(self.root, state, TASK, request)[1])
        self.assertFalse(board.build_cards(self.root, state, [TASK])[TASK]["intact"])

    def test_the_coordinator_collects_first_and_waits_on_a_failure(self):
        self.assertIsNone(release_coordinator._collect_declared_build(self.root, board.snapshot(self.root), TASK, {"id": "x"}), "no declaration: nothing to do")
        self.build()
        state = board.snapshot(self.root)
        request = state["qa_requests"]["final-1"]
        self.machine.stop()
        first = release_coordinator._collect_declared_build(self.root, state, TASK, request)
        self.assertEqual((first["status"], first["class"]), ("build_not_collected", "machine"))
        second = release_coordinator._collect_declared_build(self.root, board.snapshot(self.root), TASK, request)
        self.assertEqual(second, {"task": TASK, "status": "build_waiting"}, "retried only on the board's rule")
        self.machine.start()
        with mock.patch.object(build_collection, "RETRY_SECONDS", 0):
            self.assertIsNone(release_coordinator._collect_declared_build(self.root, board.snapshot(self.root), TASK, request), "collected: the release goes on")
        self.assertIn(TASK, board.snapshot(self.root)["builds"])
        self.assertIsNone(release_coordinator._collect_declared_build(self.root, board.snapshot(self.root), TASK, request), "and it is not collected twice")


class DownloadTests(MachineFixture):
    def setUp(self):
        super().setUp()
        self.build(("why.bin", "why.txt"))
        board.collect_build(self.root, TASK, self.cto["id"])
        self.manifest_id = board.snapshot(self.root)["builds"][TASK]["manifest_id"]

    def get(self, name, manifest_id=None):
        from harness import board_viewer
        status, body, opened = board_viewer.open_build_download(self.root, f"/api/builds/{manifest_id or self.manifest_id}/{name}")
        data = b""
        if opened:
            descriptor, size, served = opened
            data = os.read(descriptor, size)
            os.close(descriptor)
            self.assertEqual(served, name)
        return status, body, data

    def test_the_bytes_are_served_only_through_the_boards_index_and_verified(self):
        status, _body, data = self.get("why.bin")
        self.assertEqual((status, data), (200, CONTENT["why.bin"]))
        for name in ("../manifest.json", "manifest.json", "nope.bin", "..", "why.bin/x", ""):
            self.assertEqual(self.get(name)[0], 404, name)
        self.assertEqual(self.get("why.bin", "b-0000000000000000")[0], 404)
        self.assertEqual(self.get("why.bin", "../" + self.manifest_id)[0], 404)

    def test_a_changed_or_replaced_file_is_refused_with_a_plain_reason(self):
        path = build_collection.manifest_path(self.root, TASK, self.manifest_id).parent / "why.bin"
        path.write_bytes(CONTENT["why.bin"][:-1] + b"X")
        status, body, _data = self.get("why.bin")
        self.assertEqual(status, 409)
        self.assertIn("no longer matches", json.loads(body)["error"])
        self.assertEqual(self.get("why.txt")[0], 200, "the other file is still served")
        path.unlink()
        os.symlink(self.retained.parent / "home" / "x", path)
        self.assertEqual(self.get("why.bin")[0], 409, "a symbolic link is never followed")

    def test_a_changed_manifest_is_refused(self):
        manifest = build_collection.manifest_path(self.root, TASK, self.manifest_id)
        value = json.loads(manifest.read_text())
        value["outputs"][0]["sha256"] = "0" * 64
        manifest.write_text(json.dumps(value))
        status, body, _data = self.get("why.bin")
        self.assertEqual(status, 409)
        self.assertIn("does not match what the board recorded", json.loads(body)["error"])

    def test_the_download_stays_after_the_machine_is_gone_and_after_the_release(self):
        test_machine.update(self.root, lambda value: value.update({"connected": False}))
        self.assertEqual(self.get("why.bin")[0], 200)
        self.machine.stop()
        self.assertEqual(self.get("why.txt")[2], CONTENT["why.txt"])


class PreviewTests(unittest.TestCase):
    def test_a_program_release_has_nothing_to_open_in_a_browser(self):
        program = {"status": "VISUAL_TEST_REQUIRED", "build": {"manifest_id": "b-1", "outputs": []}}
        both = {"status": "VISUAL_TEST_REQUIRED", "build": {"manifest_id": "b-1", "outputs": [], "with_web_app": True}}
        plain = {"status": "VISUAL_TEST_REQUIRED"}
        self.assertTrue(release_preview.program_only(program))
        self.assertFalse(release_preview.program_only(both), "a task that also delivers a web app keeps View app")
        self.assertFalse(release_preview.program_only(plain), "a legacy release is exactly as before")

    def test_view_app_is_refused_and_a_saved_command_does_not_start_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            with board.locked_state(root) as state:
                state["releases"][TASK] = {"task": TASK, "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto-1", "recorded_at": board.now(), "head_commit": "c" * 40, "build": {"manifest_id": "b-1", "outputs": []}, "preview": {}}
            with self.assertRaisesRegex(ValueError, "nothing to open in the browser"):
                release_preview.request_view(root, TASK)
            supervisor = release_preview.ReleasePreviewSupervisor(root)
            with mock.patch.object(supervisor, "_launch", side_effect=AssertionError("a program was previewed")):
                supervisor._ensure(TASK, board.snapshot(root)["releases"][TASK], {"command": "python3 server.py"}, board.snapshot(root))
            self.assertEqual(board.snapshot(root)["releases"][TASK]["preview"]["status"], "skipped")


class AuthenticatedSurfaceTests(unittest.TestCase):
    """Through the real authenticated command surface (the same path the CLI and the MCP tools use), identities included."""

    def setUp(self):
        from tests.test_test_machine_board import MachineBoardFixture
        self.fixture = type("F", (MachineBoardFixture,), {"runTest": lambda self: None})()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.context = self.fixture.context

    def agent_with_task(self, role, task):
        agent, token = self.fixture.agent(role)
        with board.locked_state(self.context) as state:
            state["agents"][agent["id"]]["task"] = task
        return agent, token

    def test_declare_and_collect_go_through_the_surface_with_the_right_identities(self):
        from harness.board_surface import SurfaceAuthorizationError
        agent, token = self.agent_with_task("engineering", TASK)
        contract.create_contract(self.context, TASK, "Build why", ["why runs"])
        out = self.fixture.op(token, "declare-build", "--output", "why.bin", "--platform", "Linux x86-64", "--requirements", "none",
                              "--step", "Download it", "--step", "chmod +x why.bin", "--expect", "usage")
        self.assertEqual(out["build"]["outputs"], [{"name": "why.bin"}])
        self.assertEqual(contract.build_declaration(self.context, TASK)["steps"], ["Download it", "chmod +x why.bin"])
        # Delivery cannot collect another task's build; the Reviewer can do neither
        with self.assertRaisesRegex(SurfaceAuthorizationError, "caller task does not match"):
            self.fixture.op(token, "collect-build", "--task", "TASK-OTHER")
        _qa, qa_token = self.fixture.agent("qa")
        with self.assertRaises(SurfaceAuthorizationError):
            self.fixture.op(qa_token, "declare-build", "--output", "x", "--platform", "p", "--requirements", "none", "--step", "s", "--expect", "e")
        with self.assertRaises(SurfaceAuthorizationError):
            self.fixture.op(qa_token, "collect-build", "--task", TASK)
        _cto, cto_token = self.fixture.agent("cto")
        with self.assertRaises(SurfaceAuthorizationError):
            self.fixture.op(cto_token, "declare-build", "--output", "x", "--platform", "p", "--requirements", "none", "--step", "s", "--expect", "e")
        with self.assertRaisesRegex(ValueError, "no passed final review awaits release"):
            self.fixture.op(cto_token, "collect-build", "--task", TASK)

    def test_a_long_collection_has_the_long_client_timeout(self):
        import inspect
        from harness import board_client
        self.assertIn('"collect-build"', inspect.getsource(board_client))


class PageTests(unittest.TestCase):
    def test_the_page_carries_the_build_block_and_hides_view_app_for_programs(self):
        from harness import board_viewer
        page = board_viewer.PAGE if hasattr(board_viewer, "PAGE") else open(board_viewer.__file__).read()
        for needle in ("Get the build", "function buildBlockHtml", "/api/builds/", "if(release?.build&&!release.build.with_web_app)return''", "cannot confirm who made this program", "buildBlockHtml(item.build)", "releaseResponseCore", "Not launched on the test machine", "If you already accepted, a replacement needs a new task"):
            self.assertIn(needle, page)


if __name__ == "__main__":
    unittest.main()

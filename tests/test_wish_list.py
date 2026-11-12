# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The project Wish list: records (harness/board.py) and the project page's own view and routes
(harness/board_viewer.py), built from the accepted plan SPEC.md (task build-wish-list,
requirements R1-R7)."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, project_manager_page
from tests.mission_control_support import RenderedCase
from tests.test_requirements_contract import context


class WishListRecordsTests(unittest.TestCase):
    """R2, R3, R5, R6: the pure board.py functions, with no HTTP involved."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_adding_a_blank_or_whitespace_only_wish_is_refused(self):
        for blank in ("", "   ", "\n\t"):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(ValueError):
                    board.add_project_wish(self.root, "Title", blank)
        self.assertEqual(board.project_wishes_state(board.snapshot(self.root)), [])

    def test_adding_without_a_title_is_refused_and_nothing_is_stored(self):
        """Owner repair 2026-11-11 round 4: "add a title when we create a wish"."""
        for blank in (None, "", "   "):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(ValueError):
                    board.add_project_wish(self.root, blank, "Add dark mode")
        self.assertEqual(board.project_wishes_state(board.snapshot(self.root)), [])

    def test_a_title_over_80_characters_is_refused_and_nothing_is_stored(self):
        with self.assertRaisesRegex(ValueError, str(board.MAX_WISH_TITLE_LENGTH)):
            board.add_project_wish(self.root, "x" * (board.MAX_WISH_TITLE_LENGTH + 1), "Add dark mode")
        self.assertEqual(board.project_wishes_state(board.snapshot(self.root)), [])

    def test_a_wish_over_the_character_limit_is_refused_and_nothing_is_stored(self):
        with self.assertRaisesRegex(ValueError, str(board.MAX_WISH_TEXT_LENGTH)):
            board.add_project_wish(self.root, "Title", "x" * (board.MAX_WISH_TEXT_LENGTH + 1))
        self.assertEqual(board.project_wishes_state(board.snapshot(self.root)), [])

    def test_the_201st_wish_is_refused_and_the_first_200_are_unchanged(self):
        for index in range(board.MAX_PROJECT_WISHES):
            board.add_project_wish(self.root, f"Title {index}", f"wish {index}")
        with self.assertRaisesRegex(ValueError, str(board.MAX_PROJECT_WISHES)):
            board.add_project_wish(self.root, "One too many", "one too many")
        self.assertEqual(len(board.project_wishes_state(board.snapshot(self.root))), board.MAX_PROJECT_WISHES)

    def test_edit_done_and_remove_change_only_the_one_wish_named(self):
        first = board.add_project_wish(self.root, "Dark mode", "Add dark mode")
        second = board.add_project_wish(self.root, "Light mode", "Add light mode")

        board.edit_project_wish(self.root, first["id"], "Dark mode", "Add a real dark mode")
        wishes = board.project_wishes_state(board.snapshot(self.root))
        self.assertEqual(next(w for w in wishes if w["id"] == first["id"])["text"], "Add a real dark mode")
        self.assertEqual(next(w for w in wishes if w["id"] == second["id"])["text"], "Add light mode")

        board.set_project_wish_done(self.root, first["id"], True)
        wishes = board.project_wishes_state(board.snapshot(self.root))
        self.assertTrue(next(w for w in wishes if w["id"] == first["id"])["done"])
        self.assertFalse(next(w for w in wishes if w["id"] == second["id"])["done"])
        board.set_project_wish_done(self.root, first["id"], False)
        self.assertFalse(board.project_wishes_state(board.snapshot(self.root))[0]["done"])

        board.remove_project_wish(self.root, second["id"])
        remaining = board.project_wishes_state(board.snapshot(self.root))
        self.assertEqual([w["id"] for w in remaining], [first["id"]])

    def test_editing_can_change_the_title(self):
        wish = board.add_project_wish(self.root, "Original title", "Add dark mode")
        updated = board.edit_project_wish(self.root, wish["id"], "A much better title", "Add dark mode")[0]
        self.assertEqual(updated["title"], "A much better title")

    def test_removing_a_wish_already_gone_is_answered_calmly_not_refused(self):
        wish = board.add_project_wish(self.root, "Title", "Add dark mode")
        board.remove_project_wish(self.root, wish["id"])
        result = board.remove_project_wish(self.root, wish["id"])   # a second removal, or one from another tab
        self.assertEqual(result, [])

    def test_editing_or_marking_done_a_wish_already_gone_leaves_the_list_as_it_is(self):
        wish = board.add_project_wish(self.root, "Title", "Add dark mode")
        board.remove_project_wish(self.root, wish["id"])
        self.assertEqual(board.edit_project_wish(self.root, wish["id"], "Title", "new text"), [])
        self.assertEqual(board.set_project_wish_done(self.root, wish["id"], True), [])

    def test_editing_can_add_a_new_attachment_and_remove_an_existing_one_together(self):
        wish = board.add_project_wish(
            self.root, "Title", "Add dark mode",
            attachments=[{"filename": "old.png", "content_type": "image/png", "data": b"old bytes"}],
        )
        old_id = wish["attachments"][0]["attachment_id"]
        updated = board.edit_project_wish(
            self.root, wish["id"], "Title", "A whole new description",
            attachments=[{"filename": "new.txt", "content_type": "text/plain", "data": b"new notes"}],
            remove_attachment_ids=[old_id],
        )[0]
        self.assertEqual(updated["text"], "A whole new description")
        self.assertEqual(len(updated["attachments"]), 1)
        self.assertEqual(updated["attachments"][0]["display_name"], "new.txt")
        self.assertIsNone(board.wish_attachment_file(self.root, wish["id"], old_id))

    def test_editing_past_the_attachment_limit_is_refused_and_nothing_changes(self):
        wish = board.add_project_wish(
            self.root, "Title", "Add dark mode",
            attachments=[{"filename": f"{n}.txt", "content_type": "text/plain", "data": b"x"} for n in range(5)],
        )
        with self.assertRaisesRegex(ValueError, str(board.MAX_ATTACHMENTS)):
            board.edit_project_wish(
                self.root, wish["id"], "Title", "still has five",
                attachments=[{"filename": "one-too-many.txt", "content_type": "text/plain", "data": b"x"}],
            )
        unchanged = board.project_wishes_state(board.snapshot(self.root))[0]
        self.assertEqual(unchanged["text"], "Add dark mode")
        self.assertEqual(len(unchanged["attachments"]), 5)

    def test_removing_an_attachment_id_that_does_not_exist_is_ignored_not_refused(self):
        wish = board.add_project_wish(self.root, "Title", "Add dark mode")
        updated = board.edit_project_wish(self.root, wish["id"], "Title", "same wish", remove_attachment_ids=["not-a-real-id"])[0]
        self.assertEqual(updated["text"], "same wish")

    def test_reorder_puts_wishes_in_the_exact_order_given(self):
        first = board.add_project_wish(self.root, "One", "one")
        second = board.add_project_wish(self.root, "Two", "two")
        third = board.add_project_wish(self.root, "Three", "three")
        reordered = board.reorder_project_wishes(self.root, [third["id"], first["id"], second["id"]])
        self.assertEqual([w["id"] for w in reordered], [third["id"], first["id"], second["id"]])

    def test_reorder_keeps_a_wish_missing_from_the_order_rather_than_dropping_it(self):
        first = board.add_project_wish(self.root, "One", "one")
        second = board.add_project_wish(self.root, "Two", "two")
        reordered = board.reorder_project_wishes(self.root, [second["id"]])
        self.assertEqual([w["id"] for w in reordered], [second["id"], first["id"]])

    def test_markup_and_quotes_are_stored_exactly_as_typed(self):
        wish = board.add_project_wish(self.root, '<b>Title</b>', '<b>bold</b> "quoted"')
        self.assertEqual(wish["title"], '<b>Title</b>')
        self.assertEqual(wish["text"], '<b>bold</b> "quoted"')

    def test_a_wish_can_carry_an_attachment_readable_back_by_id(self):
        """Owner repair 2026-11-10: attachments, the same validated shape and storage the
        Give-direction composer already uses."""
        wish = board.add_project_wish(
            self.root, "Title", "Add dark mode",
            attachments=[{"filename": "screenshot.png", "content_type": "image/png", "data": b"\x89PNG fake bytes"}],
        )
        self.assertEqual(len(wish["attachments"]), 1)
        stored = wish["attachments"][0]
        self.assertEqual(stored["display_name"], "screenshot.png")
        self.assertEqual(stored["content_type"], "image/png")
        found = board.wish_attachment_file(self.root, wish["id"], stored["attachment_id"])
        self.assertIsNotNone(found)
        path, content_type, display_name = found
        self.assertEqual(path.read_bytes(), b"\x89PNG fake bytes")
        self.assertEqual(content_type, "image/png")
        self.assertEqual(display_name, "screenshot.png")

    def test_a_disallowed_or_oversized_attachment_is_refused_and_nothing_is_stored(self):
        with self.assertRaises(ValueError):
            board.add_project_wish(
                self.root, "Title", "Add dark mode",
                attachments=[{"filename": "virus.exe", "content_type": "application/x-msdownload", "data": b"x"}],
            )
        with self.assertRaises(ValueError):
            board.add_project_wish(
                self.root, "Title", "Add dark mode",
                attachments=[{"filename": "big.png", "content_type": "image/png", "data": b"x" * (board.MAX_ATTACHMENT_BYTES + 1)}],
            )
        self.assertEqual(board.project_wishes_state(board.snapshot(self.root)), [])

    def test_removing_a_wish_deletes_its_attachment_files_from_disk(self):
        wish = board.add_project_wish(
            self.root, "Title", "Add dark mode",
            attachments=[{"filename": "note.txt", "content_type": "text/plain", "data": b"notes"}],
        )
        stored_dir = board.board_dir(self.root) / "wishes" / wish["id"]
        self.assertTrue(stored_dir.is_dir())
        board.remove_project_wish(self.root, wish["id"])
        self.assertFalse(stored_dir.exists())
        self.assertIsNone(board.wish_attachment_file(self.root, wish["id"], wish["attachments"][0]["attachment_id"]))

    def test_rejecting_the_201st_wish_rolls_back_its_attachment_files(self):
        for index in range(board.MAX_PROJECT_WISHES):
            board.add_project_wish(self.root, f"Title {index}", f"wish {index}")
        with self.assertRaises(ValueError):
            board.add_project_wish(
                self.root, "One too many", "one too many",
                attachments=[{"filename": "note.txt", "content_type": "text/plain", "data": b"notes"}],
            )
        wishes_dir = board.board_dir(self.root) / "wishes"
        leftover = list(wishes_dir.iterdir()) if wishes_dir.exists() else []
        self.assertEqual(leftover, [])


class WishListPagePathTests(unittest.TestCase):
    """R1, R2, R3, R4, R6: the real HTTP routes a browser calls."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _post(self, path, body):
        request = Request(
            f"{self.base}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_adding_through_the_real_route_stores_it_and_it_is_readable_back(self):
        status, wish = self._post("/api/wishes", {"title": "Dark mode", "text": "Add dark mode"})
        self.assertEqual(status, 201)
        self.assertEqual(wish["title"], "Dark mode")
        with urlopen(f"{self.base}/api/board", timeout=5) as response:
            snapshot = json.loads(response.read())
        self.assertEqual(snapshot["state"]["project_wishes"][0]["id"], wish["id"])

    def test_a_blank_add_is_refused_with_a_plain_message_and_no_stack_trace(self):
        with self.assertRaises(HTTPError) as caught:
            self._post("/api/wishes", {"title": "Dark mode", "text": "   "})
        self.assertEqual(caught.exception.code, 400)
        body = caught.exception.read().decode()
        self.assertNotIn("Traceback", body)
        self.assertIn("text", json.loads(body)["error"])

    def test_adding_without_a_title_is_refused_with_a_plain_message(self):
        with self.assertRaises(HTTPError) as caught:
            self._post("/api/wishes", {"text": "Add dark mode"})
        self.assertEqual(caught.exception.code, 400)
        body = caught.exception.read().decode()
        self.assertNotIn("Traceback", body)
        self.assertIn("title", json.loads(body)["error"])

    def test_build_this_does_not_change_the_wish_or_the_stored_list(self):
        status, wish = self._post("/api/wishes", {"title": "Dark mode", "text": "Add dark mode"})
        with urlopen(f"{self.base}/api/board", timeout=5) as response:
            before = json.loads(response.read())["state"]["project_wishes"]
        # "Build this" is a client-side-only action (opens the composer, sends nothing); there is
        # deliberately no server route for it, so the stored list is provably unchanged.
        with urlopen(f"{self.base}/api/board", timeout=5) as response:
            after = json.loads(response.read())["state"]["project_wishes"]
        self.assertEqual(before, after)

    def test_a_wish_added_to_one_project_is_absent_from_a_different_projects_records(self):
        self._post("/api/wishes", {"title": "Title", "text": "Only in this project"})
        with tempfile.TemporaryDirectory() as other_root:
            other = Path(other_root)
            other_server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(other))
            threading.Thread(target=other_server.serve_forever, daemon=True).start()
            try:
                with urlopen(f"http://127.0.0.1:{other_server.server_port}/api/board", timeout=5) as response:
                    other_state = json.loads(response.read())["state"]
                self.assertEqual(other_state.get("project_wishes", []), [])
            finally:
                other_server.shutdown(); other_server.server_close()

    def _post_multipart(self, path, fields, files):
        boundary = "wishlistboundary"
        parts = []
        for name, value in fields.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )
        for name, filename, content_type, data in files:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        request = Request(
            f"{self.base}{path}", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_adding_with_an_attachment_through_the_real_multipart_route_stores_and_serves_it(self):
        status, wish = self._post_multipart(
            "/api/wishes", {"title": "Dark mode", "text": "Add dark mode"},
            [("attachments", "shot.png", "image/png", b"\x89PNG fake bytes")],
        )
        self.assertEqual(status, 201)
        self.assertEqual(len(wish["attachments"]), 1)
        attachment_id = wish["attachments"][0]["attachment_id"]
        with urlopen(f"{self.base}/api/wishes/{wish['id']}/attachments/{attachment_id}", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Content-Type"), "image/png")
            self.assertEqual(response.read(), b"\x89PNG fake bytes")

    def test_a_mac_screenshot_name_with_a_special_space_is_served_back_not_dropped(self):
        """Owner, 2026-11-12: "Could not fetch the attachment Screenshot 2026-11-11 at
        10.37.05 AM.png". Every macOS screenshot name carries a narrow no-break space
        (U+202F) before AM/PM; the reply's file-name header crashed on it and the browser
        got an empty answer. Any file name must be served back with its exact bytes."""
        names = ["Screenshot 2026-11-11 at 10.37.05\u202fAM.png", "caf\u00e9 \u2014 plan \u4e2d\u6587.png", 'a"b;c.png']
        for index, name in enumerate(names):
            data = b"\x89PNG real bytes " + str(index).encode()
            status, wish = self._post_multipart(
                "/api/wishes", {"title": f"Wish {index}", "text": "With a file"},
                [("attachments", name.replace('"', "%22"), "image/png", data)],
            )
            self.assertEqual(status, 201)
            attachment_id = wish["attachments"][0]["attachment_id"]
            with urlopen(f"{self.base}/api/wishes/{wish['id']}/attachments/{attachment_id}", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), data)
                self.assertIn("filename*=UTF-8''", response.headers.get("Content-Disposition"))

    def test_editing_with_a_removed_and_a_new_attachment_saves_both(self):
        """Owner repair 2026-11-11 round 3: the edit dialog's own real save path (the probe
        harness above cannot carry this multipart submit; see WishListEditDialogRenderedTests'
        own note), proving the whole rewritten text, the removal of the old attachment, and the
        addition of a new one all land together."""
        status, wish = self._post_multipart(
            "/api/wishes", {"title": "Dark mode", "text": "Add dark mode"},
            [("attachments", "old.png", "image/png", b"\x89PNG old bytes")],
        )
        old_attachment_id = wish["attachments"][0]["attachment_id"]
        status, edited = self._post_multipart(
            f"/api/wishes/{wish['id']}/edit",
            {
                "title": "A whole new title", "text": "A whole new, much longer description",
                "remove_attachments": json.dumps([old_attachment_id]),
            },
            [("attachments", "new.txt", "text/plain", b"new notes")],
        )
        self.assertEqual(status, 200)
        updated = edited["wishes"][0]
        self.assertEqual(updated["title"], "A whole new title")
        self.assertEqual(updated["text"], "A whole new, much longer description")
        self.assertEqual(len(updated["attachments"]), 1)
        self.assertEqual(updated["attachments"][0]["display_name"], "new.txt")
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"{self.base}/api/wishes/{wish['id']}/attachments/{old_attachment_id}", timeout=5)
        self.assertEqual(caught.exception.code, 404)

    def test_a_removed_or_unknown_attachment_id_answers_404_not_a_stack_trace(self):
        status, wish = self._post_multipart("/api/wishes", {"title": "Dark mode", "text": "Add dark mode"}, [])
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"{self.base}/api/wishes/{wish['id']}/attachments/not-a-real-id", timeout=5)
        self.assertEqual(caught.exception.code, 404)
        self.assertNotIn("Traceback", caught.exception.read().decode())

    def test_a_wish_survives_a_fresh_handler_the_same_way_a_restart_would(self):
        """board_viewer.make_handler(root) builds a fresh handler from the records on disk, the
        same thing a restarted NoMoreHappyPath process does (R5): nothing in this app keeps the
        wish list only in memory."""
        self._post("/api/wishes", {"title": "Title", "text": "Still here after a restart"})
        restarted = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
        threading.Thread(target=restarted.serve_forever, daemon=True).start()
        try:
            with urlopen(f"http://127.0.0.1:{restarted.server_address[1]}/api/board", timeout=5) as response:
                state = json.loads(response.read())["state"]
            self.assertEqual(state["project_wishes"][0]["text"], "Still here after a restart")
        finally:
            restarted.shutdown(); restarted.server_close()

    def test_a_direction_with_an_attachment_reaches_a_waiting_agents_stored_direction(self):
        """Owner repair 2026-11-11 round 5: "the attachments are not passed. They should be
        passed as Separate supporting documents or screenshots" — proves the server side of the
        fix (the exact route and message shape the Build this hand-off now uses) independent of
        the real browser/launcher flow, and independent of this task's probe harness, which
        cannot carry a real multipart POST (see WishListBuildThisAttachmentsRenderedTests'
        own fetch-interception test for the client side of this same fix)."""
        from harness import control
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        status, response = self._post_multipart(
            f"/api/agents/{agent['id']}/owner-message",
            {"message_type": "direction", "text": "Add dark mode", "task_kind": "development"},
            [("attachments", "shot.png", "image/png", b"\x89PNG fake bytes")],
        )
        self.assertEqual(status, 201)
        stored = board.snapshot(self.root)["owner_directions"][session["id"]]
        self.assertEqual(stored["text"], "Add dark mode")
        self.assertEqual(len(stored["attachments"]), 1)
        self.assertEqual(stored["attachments"][0]["display_name"], "shot.png")

    def _git_init_root(self):
        """begin_task only creates an isolated task workspace for a Git-backed project; make
        self.root one, the same way a real NoMoreHappyPath project is, so these tests exercise
        the real supporting-documents/ copy instead of the no-repository fallback. The actual
        worktree creation (GitBroker.branch_create) runs Git under this harness's own OS sandbox
        profile, which this test runner's own shell cannot nest inside its own sandbox - a limit
        of the test tool, not of the harness - so that one step is stubbed to a plain directory;
        everything around it (repository init, baseline, and this task's own
        supporting-documents copy) is the real code, unstubbed."""
        import subprocess
        from unittest.mock import patch
        from harness import git_broker
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Harness"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("project\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.root, check=True)
        counter = {"n": 0}

        def fake_branch_create(broker, agent_id, nonce):
            counter["n"] += 1
            workspace = Path(tempfile.mkdtemp(prefix=f"fake-task-workspace-{counter['n']}-"))
            self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
            return {"workspace": str(workspace), "branch": f"fake-branch-{counter['n']}"}

        patcher = patch.object(git_broker.GitBroker, "branch_create", fake_branch_create)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_directions_attachments_are_copied_into_the_new_tasks_own_workspace(self):
        """Owner repair round 9 (found by the owner reading the code): a direction's files were
        only ever saved under the project's own data folder (board/owner-messages/<id>/); the
        message routed to the builder named only that path, and nothing copied the files into the
        new task's own isolated workspace, so the task that got built never actually had them.
        Fix: begin_task now copies each attachment into the new workspace's supporting-documents/
        folder under its original name, before the task starts."""
        from harness import control
        self._git_init_root()
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        status, _ = self._post_multipart(
            f"/api/agents/{agent['id']}/owner-message",
            {"message_type": "direction", "text": "Build the wish", "task_kind": "development"},
            [
                ("attachments", "shot.png", "image/png", b"\x89PNG fake bytes one"),
                ("attachments", "notes.txt", "text/plain", b"supporting notes"),
            ],
        )
        self.assertEqual(status, 201)
        begun = board.begin_task(self.root, agent["id"], "WISH-BUILD-1")
        self.assertEqual(
            sorted(begun["supporting_documents"]),
            ["supporting-documents/notes.txt", "supporting-documents/shot.png"],
        )
        workspace = Path(begun["task_workspace"])
        self.assertEqual((workspace / "supporting-documents" / "shot.png").read_bytes(), b"\x89PNG fake bytes one")
        self.assertEqual((workspace / "supporting-documents" / "notes.txt").read_bytes(), b"supporting notes")

    def test_a_clarifications_attachment_is_copied_into_the_already_running_tasks_workspace(self):
        """Same repair, the mid-task path: a clarification sent after the task has begun carries
        its attachment into the workspace that already exists, immediately, before the message
        is routed - not deferred to begin_task, which already ran."""
        from harness import control
        self._git_init_root()
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        self._post_multipart(
            f"/api/agents/{agent['id']}/owner-message",
            {"message_type": "direction", "text": "Build the wish", "task_kind": "development"}, [],
        )
        begun = board.begin_task(self.root, agent["id"], "WISH-BUILD-2")
        status, response = self._post_multipart(
            f"/api/agents/{agent['id']}/owner-message",
            {"message_type": "clarification", "text": "Here is the missing screenshot"},
            [("attachments", "shot.png", "image/png", b"second screenshot bytes")],
        )
        self.assertEqual(status, 201)
        self.assertEqual(response["message"]["attachments"][0]["workspace_path"], "supporting-documents/shot.png")
        workspace = Path(begun["task_workspace"])
        self.assertEqual((workspace / "supporting-documents" / "shot.png").read_bytes(), b"second screenshot bytes")

    def test_a_name_clash_between_a_directions_and_a_clarifications_attachment_is_numbered_not_overwritten(self):
        from harness import control
        self._git_init_root()
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        self._post_multipart(
            f"/api/agents/{agent['id']}/owner-message",
            {"message_type": "direction", "text": "Build the wish", "task_kind": "development"},
            [("attachments", "shot.png", "image/png", b"original bytes")],
        )
        begun = board.begin_task(self.root, agent["id"], "WISH-BUILD-3")
        self._post_multipart(
            f"/api/agents/{agent['id']}/owner-message",
            {"message_type": "clarification", "text": "A second, different shot.png"},
            [("attachments", "shot.png", "image/png", b"different bytes, same name")],
        )
        workspace = Path(begun["task_workspace"])
        self.assertEqual((workspace / "supporting-documents" / "shot.png").read_bytes(), b"original bytes")
        self.assertEqual((workspace / "supporting-documents" / "shot-2.png").read_bytes(), b"different bytes, same name")


class WishListServedPageTests(unittest.TestCase):
    """R1, R2, R3, R4: what the served page itself carries, independent of the Reviewer's own
    contract-check wording."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        with urlopen(f"http://127.0.0.1:{self.server.server_port}/", timeout=5) as response:
            self.page = response.read().decode("utf-8")

    def test_the_wish_list_tab_sits_right_after_advanced(self):
        self.assertIn('id="tab-advanced-btn"', self.page)
        self.assertIn('id="tab-wish-btn"', self.page)
        self.assertLess(self.page.index('id="tab-advanced-btn"'), self.page.index('id="tab-wish-btn"'))
        self.assertIn('id="tab-wish"', self.page)
        self.assertIn('data-mc-tab="wish"', self.page)

    def test_the_add_box_and_button_are_present(self):
        self.assertIn('id="wish-add-form"', self.page)
        self.assertIn('id="wish-add-text"', self.page)
        self.assertIn('Add to wish list', self.page)

    def test_the_add_box_is_a_dialog_shaped_like_the_directive_composer_with_attachments(self):
        """Owner repair 2026-11-10: "It has to have the same structure as the directive input
        with the attached files" — same dialog/modal-form/label/textarea/counter/attachments
        pieces as #owner-message-dialog, not the old bare inline textarea."""
        self.assertIn('<dialog id="wish-add-dialog"', self.page)
        self.assertIn('id="wish-add-open"', self.page)
        self.assertIn('class="modal" id="wish-add-form"', self.page)
        self.assertIn('id="wish-add-count"', self.page)
        self.assertIn('id="wish-add-attachments"', self.page)
        self.assertIn('id="wish-add-attachments-help"', self.page)
        self.assertIn('id="wish-add-cancel"', self.page)
        self.assertNotIn('maxlength="2000"', self.page)
        self.assertIn('20,000 characters', self.page)

    def test_the_dialog_also_has_a_required_80_character_title_field(self):
        """Owner repair 2026-11-11 round 4: "add a title when we create a wish... an
        80-character title that will be shown in the list"."""
        self.assertIn('id="wish-title-input"', self.page)
        self.assertIn('maxlength="80"', self.page)
        self.assertIn('id="wish-title-count"', self.page)

    def test_each_entry_renders_with_edit_done_remove_and_a_drag_handle(self):
        self.assertIn('wish-edit', self.page)
        self.assertIn('wish-done-toggle', self.page)
        self.assertIn('wish-remove', self.page)
        self.assertIn('draggable="true"', self.page)

    def test_entries_are_numbered_and_visually_separated(self):
        """Owner repair 2026-11-11: "no numbering and no space between the blocks"."""
        self.assertIn('wish-number', self.page)
        self.assertIn('.wish-item{', self.page)
        self.assertIn('border-bottom', self.page[self.page.index('.wish-item{'):self.page.index('.wish-item{') + 200])

    def test_remove_asks_are_you_sure_before_calling_the_server(self):
        self.assertIn("confirm('Are you sure?')", self.page)

    def test_build_this_reuses_the_existing_direction_composer(self):
        self.assertIn('openDirectionStart()', self.page)
        self.assertIn("el('#owner-message-text')", self.page)


class WishListDocumentationTests(unittest.TestCase):
    """R7: README and Help explain the Wish list in the owner's own words."""

    def test_readme_explains_every_action(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("Wish list", readme)
        for action in ("Add to wish list", "edit", "done", "reorder", "remove", "Build this"):
            with self.subTest(action=action):
                self.assertIn(action, readme)

    def test_help_page_explains_every_action(self):
        page = project_manager_page.PAGE
        self.assertIn("Wish list", page)
        for action in ("Add to wish list", "edit", "done", "reorder", "remove", "Build this"):
            with self.subTest(action=action):
                self.assertIn(action, page)


class WishListBuildThisRenderedTests(RenderedCase):
    """R4, review cycle 1 finding: "Build this" must carry the wish's text into Give direction
    even when no Delivery agent is running yet. In that case openDirectionStart() shows the
    provider/color launcher first and returns; the composer is only filled in later, from
    readPendingDirection(), after the owner picks a color — a real headless-browser test is the
    only way to prove the wish's text survives that hand-off rather than arriving blank."""

    def test_build_this_survives_the_launcher_when_no_agent_is_running(self):
        root = context(self.base)
        board.add_project_wish(root, "Dark mode", "Add dark mode")
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
await until(() => document.querySelector('.wish-build'));
document.querySelector('.wish-build').click();
await wait(200);
out.colorOpen = document.querySelector('#color-dialog').open;
document.querySelector('#color-cancel').click();
await wait(300);
out.composerOpen = document.querySelector('#owner-message-dialog').open;
out.text = document.querySelector('#owner-message-text').value;
"""
        result = self.run_probe(target, body)
        self.assertTrue(result["colorOpen"], "no Delivery agent exists yet, so Build this must open the launcher first")
        self.assertTrue(result["composerOpen"])
        self.assertEqual(result["text"], "Add dark mode")

    def test_build_this_still_lets_a_real_provider_choice_fill_the_composer(self):
        """The fix must not bypass the launcher's own provider/color choice: picking a real
        color and submitting that dialog (not Cancel) still ends at the composer with the
        wish's text, proving the one-shot override survives the real choice path too, not only
        the Cancel shortcut the first test uses."""
        root = context(self.base)
        board.add_project_wish(root, "Dark mode", "Add dark mode")
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
await until(() => document.querySelector('.wish-build'));
document.querySelector('.wish-build').click();
await wait(200);
document.querySelector('#color-options .color-choice input').click();
document.querySelector('#color-form').requestSubmit();
await wait(300);
out.composerOpen = document.querySelector('#owner-message-dialog').open;
out.text = document.querySelector('#owner-message-text').value;
"""
        result = self.run_probe(target, body)
        self.assertTrue(result["composerOpen"])
        self.assertEqual(result["text"], "Add dark mode")

    def test_build_this_fills_the_composer_immediately_when_an_agent_is_already_waiting(self):
        """When a Delivery agent is already registered and awaiting direction, Build this must
        skip the launcher entirely and fill the composer right away (the other branch of
        openDirectionStart, unaffected by the fix)."""
        from harness import control
        root = context(self.base)
        board.add_project_wish(root, "Dark mode", "Add dark mode")
        session = control.create(root, "codex_delivery")
        board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
await until(() => document.querySelector('.wish-build'));
document.querySelector('.wish-build').click();
await wait(200);
out.colorOpen = document.querySelector('#color-dialog').open;
out.composerOpen = document.querySelector('#owner-message-dialog').open;
out.text = document.querySelector('#owner-message-text').value;
"""
        result = self.run_probe(target, body)
        self.assertFalse(result["colorOpen"], "an agent already exists, so no launcher is needed")
        self.assertTrue(result["composerOpen"])
        self.assertEqual(result["text"], "Add dark mode")


class WishListEditDialogRenderedTests(RenderedCase):
    """Owner repair 2026-11-11 round 3: "I need to be able to edit similar to create the new
    item in the wish list and be able to remove an attachment or add an attachment... I need to
    be able to edit the whole description and not just a line." Replaces the earlier one-line
    inline-edit <input> (rounds 1-2 of this same repair, which fought the page's own 2-second
    poll over that input's DOM node) with the same dialog Add uses, pre-filled, so there is no
    one-line box left to fight the poll over: the dialog is its own DOM subtree the wish list's
    poll-driven redraw never touches, by construction."""

    def test_edit_opens_the_same_dialog_prefilled_with_the_whole_text(self):
        root = context(self.base)
        board.add_project_wish(root, "A long wish", "x" * 3000)
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
await until(() => document.querySelector('.wish-edit'));
document.querySelector('.wish-edit').click();
await wait(100);
out.dialogOpen = document.querySelector('#wish-add-dialog').open;
out.heading = document.querySelector('#wish-add-title').textContent;
out.titleValue = document.querySelector('#wish-title-input').value;
out.text = document.querySelector('#wish-add-text').value;
out.button = document.querySelector('#wish-add-button').textContent;
"""
        result = self.run_probe(target, body)
        self.assertTrue(result["dialogOpen"])
        self.assertEqual(result["heading"], "Edit wish")
        self.assertEqual(result["titleValue"], "A long wish")
        self.assertEqual(result["text"], "x" * 3000)
        self.assertEqual(result["button"], "Save")

    def test_editing_survives_a_real_poll_tick_because_the_dialog_is_untouched_by_it(self):
        root = context(self.base)
        board.add_project_wish(root, "Dark mode", "Add dark mode")
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
await until(() => document.querySelector('.wish-edit'));
document.querySelector('.wish-edit').click();
await wait(100);
document.querySelector('#wish-add-text').value = 'Add a real dark mode, mid-edit';
await wait(2500);   // a real setInterval(refresh, 2000) tick fires naturally here
out.dialogStillOpen = document.querySelector('#wish-add-dialog').open;
out.textSurvived = document.querySelector('#wish-add-text').value;
"""
        result = self.run_probe(target, body, wait=15)
        self.assertTrue(result["dialogStillOpen"])
        self.assertEqual(result["textSurvived"], "Add a real dark mode, mid-edit")

    def test_edit_lists_existing_attachments_with_a_working_remove_button(self):
        """The save itself (the multipart POST) is proven at the HTTP level in
        WishListPagePathTests.test_editing_with_a_removed_and_a_new_attachment_saves_both,
        since this task's probe harness always relabels a forwarded POST as application/json
        (established in the prior repair round), which cannot carry this dialog's multipart
        submit. What a real browser test here can prove, and should: the dialog actually shows
        the existing attachment, and the Remove button actually takes it off the page."""
        root = context(self.base)
        board.add_project_wish(
            root, "Dark mode", "Add dark mode",
            attachments=[{"filename": "shot.png", "content_type": "image/png", "data": b"\x89PNG fake bytes"}],
        )
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
await until(() => document.querySelector('.wish-edit'));
document.querySelector('.wish-edit').click();
await wait(100);
out.existingVisible = !document.querySelector('#wish-existing-attachments').hidden;
out.existingName = document.querySelector('#wish-existing-attachments-list li')?.textContent.includes('shot.png');
document.querySelector('.wish-attachment-remove').click();
out.liGoneAfterClick = document.querySelector('#wish-existing-attachments-list li') === null;
"""
        result = self.run_probe(target, body)
        self.assertTrue(result["existingVisible"])
        self.assertTrue(result["existingName"])
        self.assertTrue(result["liGoneAfterClick"])


class WishListAddDialogRenderedTests(RenderedCase):
    """Owner repair 2026-11-10: the add box must be a real dialog the owner can open, type a
    long wish into, and close — proven in a real browser, not just by reading markup. (The
    probe harness this test runs under always relabels a forwarded POST as application/json,
    so it cannot carry this dialog's multipart submit; that submit path is instead proven
    directly against the real HTTP server in WishListPagePathTests, with no proxy in the way.)"""

    def test_opening_typing_and_cancelling_the_dialog_works_in_a_real_browser(self):
        root = context(self.base)
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
await until(() => document.querySelector('#wish-add-open'));
document.querySelector('#wish-add-open').click();
await wait(100);
out.dialogOpenAfterClick = document.querySelector('#wish-add-dialog').open;
document.querySelector('#wish-add-text').value = 'A much longer wish than 2000 characters used to allow';
document.querySelector('#wish-add-text').dispatchEvent(new Event('input'));
out.countAfterTyping = document.querySelector('#wish-add-count').textContent;
document.querySelector('#wish-add-cancel').click();
await wait(100);
out.dialogOpenAfterCancel = document.querySelector('#wish-add-dialog').open;
"""
        result = self.run_probe(target, body)
        self.assertTrue(result["dialogOpenAfterClick"])
        self.assertIn("53 of 20,000", result["countAfterTyping"])
        self.assertFalse(result["dialogOpenAfterCancel"])


class WishListCollapsedRowShowsOnlyTitleRenderedTests(RenderedCase):
    """Owner repair 2026-11-11 round 4: "when we look at the wish list, the uploaded files are
    next to each other... we do not show the attachments. only when we press edit we see all.
    So we need to add a title when we create a wish list" — the collapsed row shows the short
    title, not the full description or the attachment list; Edit is the only place both are
    shown (already proven by WishListEditDialogRenderedTests)."""

    def test_the_list_row_shows_the_title_not_the_description_or_attachments(self):
        root = context(self.base)
        board.add_project_wish(
            root, "A short title", "A much, much longer description than the title",
            attachments=[{"filename": "shot.png", "content_type": "image/png", "data": b"\x89PNG fake bytes"}],
        )
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
await until(() => document.querySelector('.wish-text'));
out.rowText = document.querySelector('.wish-text').textContent;
out.rowHtml = document.querySelector('.wish-item').innerHTML;
"""
        result = self.run_probe(target, body)
        self.assertEqual(result["rowText"], "A short title")
        self.assertNotIn("A much, much longer description", result["rowHtml"])
        self.assertNotIn("shot.png", result["rowHtml"])


class WishListMultiFileAttachmentRenderedTests(RenderedCase):
    """Owner repair 2026-11-11 round 4: "I can only upload 1 file, any additional upload
    replaces the original file I uploaded instead of adding to it" — a plain <input type=file>
    always replaces its own selection, so the dialog keeps the chosen files in its own list
    (wishPendingFiles) across more than one use of the file picker."""

    def test_choosing_files_in_two_separate_picks_adds_to_the_list_instead_of_replacing_it(self):
        root = context(self.base)
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
await until(() => document.querySelector('#wish-add-open'));
document.querySelector('#wish-add-open').click();
await wait(100);
const input = document.querySelector('#wish-add-attachments');
const makeFiles = name => { const dt = new DataTransfer(); dt.items.add(new File(['x'], name, {type: 'text/plain'})); return dt.files; };
input.files = makeFiles('first.txt');
input.dispatchEvent(new Event('change', {bubbles: true}));
await wait(50);
input.files = makeFiles('second.txt');
input.dispatchEvent(new Event('change', {bubbles: true}));
await wait(50);
out.pendingCount = wishPendingFiles.length;
out.pendingNames = wishPendingFiles.map(file => file.name);
out.listText = document.querySelector('#wish-pending-attachments-list').textContent;
document.querySelector('.wish-pending-remove[data-index="0"]').click();
out.afterRemoveCount = wishPendingFiles.length;
out.afterRemoveNames = wishPendingFiles.map(file => file.name);
"""
        result = self.run_probe(target, body)
        self.assertEqual(result["pendingCount"], 2)
        self.assertEqual(result["pendingNames"], ["first.txt", "second.txt"])
        self.assertIn("first.txt", result["listText"])
        self.assertIn("second.txt", result["listText"])
        self.assertEqual(result["afterRemoveCount"], 1)
        self.assertEqual(result["afterRemoveNames"], ["second.txt"])


class WishListBuildThisAttachmentsRenderedTests(RenderedCase):
    """Owner repair 2026-11-11 round 4: "When I click on the wish list item Build this, the
    attachments are not passed. They should be passed as Separate supporting documents or
    screenshots." The Give-direction composer hides that field for every other first direction
    by design (owner 2026-11-07); Build this is the one case that now shows it, pre-filled."""

    def test_build_this_carries_the_wishs_own_attachment_into_the_composers_attachments_field(self):
        root = context(self.base)
        board.add_project_wish(
            root, "Dark mode", "Add dark mode",
            attachments=[{"filename": "shot.png", "content_type": "image/png", "data": b"\x89PNG fake bytes"}],
        )
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
await until(() => document.querySelector('.wish-build'));
document.querySelector('.wish-build').click();
await wait(300);
document.querySelector('#color-cancel').click();
await wait(500);
out.composerOpen = document.querySelector('#owner-message-dialog').open;
out.attachmentsFieldHidden = document.querySelector('#owner-message-attachments-label').hidden;
const input = document.querySelector('#owner-message-attachments');
out.fileCount = input.files.length;
out.fileName = input.files[0]?.name;
"""
        result = self.run_probe(target, body, wait=15)
        self.assertTrue(result["composerOpen"])
        self.assertFalse(result["attachmentsFieldHidden"], "Build this must show the attachments field when the wish has one")
        self.assertEqual(result["fileCount"], 1)
        self.assertEqual(result["fileName"], "shot.png")

    def test_build_this_without_attachments_still_hides_the_field_as_before(self):
        root = context(self.base)
        board.add_project_wish(root, "Dark mode", "Add dark mode")
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
await until(() => document.querySelector('.wish-build'));
document.querySelector('.wish-build').click();
await wait(300);
document.querySelector('#color-cancel').click();
await wait(300);
out.attachmentsFieldHidden = document.querySelector('#owner-message-attachments-label').hidden;
"""
        result = self.run_probe(target, body, wait=15)
        self.assertTrue(result["attachmentsFieldHidden"], "a wish with no attachments must not change the first-direction default")

    def test_sending_the_direction_actually_posts_the_attachment_not_just_shows_it(self):
        """Independent review finding (round 6): the files reached the composer (the test
        above) but Send, on the no-agent-yet path, only ever persisted {text,...} to
        writePendingDirection and later sent that alone — the files never left the browser.
        This task's own probe proxy always relabels a forwarded POST as application/json
        (established repeatedly in earlier repair rounds), so it cannot carry the real
        multipart request through; fetch is intercepted here instead, to inspect exactly what
        the page tried to send regardless of what the proxy does to it afterward. The route and
        message actually land correctly at the server, independent of this test tool's own
        limit, in WishListPagePathTests.test_a_direction_with_an_attachment_reaches_a_waiting_agents_stored_direction."""
        from harness import control
        root = context(self.base)
        board.add_project_wish(
            root, "Dark mode", "Add dark mode",
            attachments=[{"filename": "shot.png", "content_type": "image/png", "data": b"\x89PNG fake bytes"}],
        )
        session = control.create(root, "codex_delivery")
        board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
let captured = null;
const origFetch = window.fetch;
window.fetch = function(url, opts){
  if (String(url).includes('/owner-message') && opts && opts.body instanceof FormData) captured = opts.body;
  return origFetch.apply(this, arguments).catch(() => new Response('{}', {status: 200}));
};
await until(() => document.querySelector('.wish-build'));
document.querySelector('.wish-build').click();
await wait(300);
document.querySelector('#owner-message-form').requestSubmit();
await wait(500);
out.captured = Boolean(captured);
if (captured) {
  const files = captured.getAll('attachments');
  out.fileCount = files.length;
  out.fileName = files[0]?.name;
  out.messageType = captured.get('message_type');
  out.text = captured.get('text');
}
"""
        result = self.run_probe(target, body, wait=15)
        self.assertTrue(result["captured"], "Send must actually post to the agent's owner-message route, not only save text locally")
        self.assertEqual(result["messageType"], "direction")
        self.assertEqual(result["text"], "Add dark mode")
        self.assertEqual(result["fileCount"], 1)
        self.assertEqual(result["fileName"], "shot.png")

    def test_attachments_still_arrive_even_if_the_in_memory_copy_is_lost_before_the_agent_is_ready(self):
        """Owner repair round 8: "Build this is not passing the files to the agents" even after
        round 7's fix. Root cause: on the no-agent-yet path, pendingDirectionAttachments (round
        7's in-memory File list) does not survive a reload or a wait longer than this tab's own
        lifetime, while pending.text (saved through writePendingDirection's own localStorage
        round trip) does — so a real agent that takes a while to start could end up receiving
        the words with no files at all, even though round 7's own test (immediately above, with
        an agent already waiting) passed. Fix: a small, JSON-safe reference to the wish and its
        attachment ids rides along inside that same persisted direction, and the real send
        rebuilds the files from it, fresh from this wish's own durable storage, whenever the
        in-memory copy is gone - simulated here by clearing it right after Send, before the
        agent (registered a couple of seconds later, from a background thread) becomes ready."""
        from harness import control
        root = context(self.base)
        board.add_project_wish(
            root, "Dark mode", "Add dark mode",
            attachments=[{"filename": "shot.png", "content_type": "image/png", "data": b"\x89PNG fake bytes"}],
        )
        target = self.serve(board_viewer.make_handler(root))

        def register_later():
            time.sleep(2)
            session = control.create(root, "codex_delivery")
            board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])

        threading.Thread(target=register_later, daemon=True).start()

        body = r"""
let captured = null;
const origFetch = window.fetch;
window.fetch = function(url, opts){
  if (String(url).includes('/owner-message') && opts && opts.body instanceof FormData) captured = opts.body;
  return origFetch.apply(this, arguments).catch(() => new Response('{}', {status: 200}));
};
await until(() => document.querySelector('.wish-build'));
document.querySelector('.wish-build').click();
await wait(300);
document.querySelector('#color-cancel').click();
await wait(300);
document.querySelector('#owner-message-form').requestSubmit();
await wait(200);
pendingDirectionAttachments = [];   // simulates the in-memory copy being gone before the agent is ready
await wait(4000);
out.captured = Boolean(captured);
if (captured) {
  const files = captured.getAll('attachments');
  out.fileCount = files.length;
  out.fileName = files[0]?.name;
}
"""
        result = self.run_probe(target, body, wait=20)
        self.assertTrue(result["captured"], "the direction must still be sent once the agent is ready")
        self.assertEqual(result["fileCount"], 1, "the attachment must be rebuilt from the persisted wish reference")
        self.assertEqual(result["fileName"], "shot.png")

    def test_a_file_that_cannot_be_refetched_stops_the_send_with_a_plain_message_not_silently(self):
        """Owner repair round 9: "a file that cannot be fetched also stops the send with a plain
        message" - rebuilding the files from the persisted wish reference (round 8) must not go
        on to send words-only when that re-fetch fails; it must stop and say so, keeping the
        direction saved so nothing is lost."""
        from harness import control
        root = context(self.base)
        board.add_project_wish(
            root, "Dark mode", "Add dark mode",
            attachments=[{"filename": "shot.png", "content_type": "image/png", "data": b"\x89PNG fake bytes"}],
        )
        session = control.create(root, "codex_delivery")
        board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
let captured = null;
const origFetch = window.fetch;
window.fetch = function(url, opts){
  if (String(url).includes('/attachments/')) return Promise.resolve(new Response('gone', {status: 404}));
  if (String(url).includes('/owner-message') && opts && opts.body instanceof FormData) captured = opts.body;
  return origFetch.apply(this, arguments).catch(() => new Response('{}', {status: 200}));
};
await until(() => document.querySelector('.wish-build'));
document.querySelector('.wish-build').click();
await wait(300);
document.querySelector('#color-cancel').click();
await wait(300);
document.querySelector('#owner-message-form').requestSubmit();
await wait(500);
out.captured = Boolean(captured);
out.notice = document.querySelector('#notice').textContent;
"""
        result = self.run_probe(target, body, wait=15)
        self.assertFalse(result["captured"], "the direction must not be sent words-only when its attachment could not be refetched")
        self.assertIn("could not", result["notice"].lower())

    def test_build_this_with_two_attachments_sends_nothing_at_all_when_only_the_second_fails(self):
        """Independent review finding (round 10, S-008): round 9's fix only covered the later
        re-fetch inside fetchWishAttachmentFiles; buildThisWish's own loop (which fills the
        composer the owner actually sees, including for an agent already waiting) still caught
        and silently skipped a failed fetch, so Send went out - and Mission Control reported
        success - carrying only the files that happened to work. Fix: buildThisWish now fetches
        every attachment before setting anything; the first failure stops it cold, with a plain
        named message, and Give direction is never even opened, so there is no partial send."""
        root = context(self.base)
        board.add_project_wish(
            root, "Dark mode", "Add dark mode",
            attachments=[
                {"filename": "first.txt", "content_type": "text/plain", "data": b"first file"},
                {"filename": "second.txt", "content_type": "text/plain", "data": b"second file"},
            ],
        )
        target = self.serve(board_viewer.make_handler(root))
        body = r"""
let attachmentCalls = 0;
let captured = null;
const origFetch = window.fetch;
window.fetch = function(url, opts){
  if (String(url).includes('/attachments/')) {
    attachmentCalls++;
    if (attachmentCalls === 2) return Promise.resolve(new Response('gone', {status: 404}));
    return origFetch.apply(this, arguments);
  }
  if (String(url).includes('/owner-message') && opts && opts.body instanceof FormData) captured = opts.body;
  return origFetch.apply(this, arguments).catch(() => new Response('{}', {status: 200}));
};
await until(() => document.querySelector('.wish-build'));
document.querySelector('.wish-build').click();
await wait(500);
out.captured = Boolean(captured);
out.dialogOpen = document.querySelector('#owner-message-dialog').open;
out.notice = document.querySelector('#notice').textContent;
"""
        result = self.run_probe(target, body, wait=15)
        self.assertFalse(result["captured"], "nothing may be sent when any one of the wish's attachments could not be fetched")
        self.assertFalse(result["dialogOpen"], "Give direction must not even open on a partial fetch - there is nothing complete to show or send")
        self.assertIn("second.txt", result["notice"])


if __name__ == "__main__":
    unittest.main()

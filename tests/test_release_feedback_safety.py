# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer
from tests.environment_support import require_loopback


class ReleaseFeedbackSafetyTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def released(self, root: Path, task: str):
        cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        return board.record_release_ready(root, cto["id"], task, checks | {"head_commit": "release-commit"})

    def endpoint_results(self, root: Path, task: str, decisions):
        server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/api/releases/{task}/decision"

        def submit(decision):
            request = Request(url, data=json.dumps({"decision": decision}).encode(), headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urlopen(request, timeout=3) as response:
                    return response.status
            except HTTPError as error:
                error.read()
                return error.code

        try:
            with ThreadPoolExecutor(max_workers=len(decisions)) as pool:
                return list(pool.map(submit, decisions))
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def attachment(self, name: str, data: bytes = b"x"):
        return {"filename": name, "content_type": "image/png", "data": data}

    def test_concurrent_owner_submissions_leave_one_durable_decision(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-CONCURRENT"
            self.released(root, task)
            results = self.endpoint_results(root, task, ["accepted"] * 8)
            state = board.snapshot(root)
            self.assertEqual(results.count(201), 1)
            self.assertEqual(results.count(400), 7)
            self.assertEqual(state["release_decisions"][task]["decision"], "accepted")

    def test_total_attachment_limit_is_enforced_across_calls_before_storage(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-LIMIT"
            self.released(root, task)
            board.record_release_decision(root, task, "not_accepted", "The correction is still needed.")
            board.add_release_attachments(root, task, [self.attachment(f"shot-{index}.png") for index in range(5)])
            with self.assertRaisesRegex(ValueError, "up to 5 files in total"):
                board.add_release_attachments(root, task, [self.attachment("sixth.png")])
            state = board.snapshot(root)
            self.assertEqual(len(state["release_decisions"][task]["attachments"]), 5)
            self.assertEqual(len(list((root / ".harness" / "board" / "owner-feedback").rglob("*"))), 6)

    def test_concurrent_attachment_writes_are_serialized_and_cannot_exceed_total_limit(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-ATTACH-CONCURRENT"
            self.released(root, task)
            board.record_release_decision(root, task, "not_accepted", "The correction is still needed.")
            batches = [[self.attachment(f"batch-{batch}-{index}.png") for index in range(3)] for batch in range(2)]

            def store(batch):
                try:
                    board.add_release_attachments(root, task, batch)
                    return "stored"
                except ValueError:
                    return "refused"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(store, batches))
            state = board.snapshot(root)
            self.assertEqual(sorted(outcomes), ["refused", "stored"])
            self.assertEqual(len(state["release_decisions"][task]["attachments"]), 3)
            self.assertEqual(len(list((root / ".harness" / "board" / "owner-feedback").rglob("*.png"))), 3)

    def test_rejection_dialog_uses_owner_language_and_does_not_expose_internal_state_names(self):
        dialog = board_viewer.PAGE.split('<dialog id="decision-dialog"', 1)[1].split("</dialog>", 1)[0]
        self.assertIn("What should be changed?", dialog)
        self.assertIn("Documents or screenshots", dialog)
        self.assertNotIn("VISUAL_TEST_REQUIRED", dialog)
        self.assertNotIn("not_accepted", dialog)
        self.assertNotIn("OWNER_REJECTED_REPAIR_REQUIRED", dialog)


if __name__ == "__main__":
    unittest.main()

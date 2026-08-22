# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Markdown direction and attachment intake stays usable and fail-closed."""
from __future__ import annotations

import json
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, control
from tests.environment_support import require_loopback


class MarkdownDirectionUploadTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    @staticmethod
    def multipart(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
        boundary = "----HarnessMarkdownDirectionBoundary"
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        for name, filename, content_type, content in files:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f'Content-Type: {content_type}\r\n\r\n'.encode() + content + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def browser_file_results(self) -> dict:
        script = board_viewer.rendered_page().split("<script>", 1)[1].split("</script>", 1)[0]
        declarations = script.split("el('#status-dialog-close')", 1)[0]
        invocation = r"""
const nodes={
  input:{files:[],value:''}, status:{textContent:'',className:''}, error:{textContent:''},
  submit:{disabled:false}, textarea:{value:''}, count:{textContent:'',className:''}
};
globalThis.document={querySelector(selector){return ({
  '#owner-message-directive-file':nodes.input,
  '#owner-message-file-status':nodes.status,
  '#owner-message-error':nodes.error,
  '#owner-message-submit':nodes.submit,
  '#owner-message-text':nodes.textarea,
  '#owner-message-count':nodes.count,
})[selector]||null;}};
(async()=>{
  async function choose(file){
    nodes.input.files=[file]; nodes.input.value=file.name;
    await loadOwnerDirectiveFile();
    return {status:nodes.status.textContent,kind:nodes.status.className,text:nodes.textarea.value,disabled:nodes.submit.disabled};
  }
  const wrong=await choose(new File(['valid'],'task.pdf',{type:'application/pdf'}));
  const empty=await choose(new File([],'empty.md',{type:'text/markdown'}));
  const invalid=await choose(new File([new Uint8Array([255])],'invalid.md',{type:'text/markdown'}));
  const overLimit=await choose(new File(['x'.repeat(20001)],'over.md',{type:'text/markdown'}));
  const tooLarge=await choose(new File(['x'.repeat(128*1024+1)],'huge.md',{type:'text/markdown'}));
  const valid=await choose(new File(['\r\n# Task\r\n\r\n  Keep this indentation.\r\n'],'TASK.MD',{type:'text/markdown'}));
  process.stdout.write(JSON.stringify({wrong,empty,invalid,overLimit,tooLarge,valid,count:nodes.count.textContent}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        completed = subprocess.run(
            ["node", "-e", declarations + "\n" + invocation],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_browser_reads_markdown_as_message_and_rejects_bad_files(self):
        result = self.browser_file_results()
        self.assertIn(".md or .txt", result["wrong"]["status"])
        self.assertIn("empty", result["empty"]["status"])
        self.assertIn("not valid UTF-8", result["invalid"]["status"])
        self.assertIn("20,001 UTF-8 bytes", result["overLimit"]["status"])
        self.assertIn("too large to read", result["tooLarge"]["status"])
        self.assertEqual(result["valid"]["kind"], "input-valid")
        self.assertEqual(result["valid"]["text"], "# Task\n\n  Keep this indentation.")
        self.assertFalse(result["valid"]["disabled"])
        self.assertIn("stored UTF-8 bytes", result["count"])

    def test_server_intake_normalizes_and_authenticates_file_metadata(self):
        text, metadata = board_viewer.owner_message_input({
            "text": "\r\n# Task\r\n\r\nShip it.\r\n",
            "directive_source": "file",
            "directive_filename": "task.md",
        })
        self.assertEqual(text, "# Task\n\nShip it.")
        self.assertEqual(metadata["source"], "file")
        self.assertEqual(metadata["filename"], "task.md")
        self.assertEqual(metadata["normalized_bytes"], len(text.encode("utf-8")))
        self.assertEqual(len(metadata["sha256"]), 64)

        bad_fields = (
            {"text": "valid", "directive_source": "file", "directive_filename": "../task.md"},
            {"text": "valid", "directive_source": "file", "directive_filename": "task.pdf"},
            {"text": "valid", "directive_source": "text", "directive_filename": "task.md"},
            {"text": "valid", "directive_source": "remote", "directive_filename": ""},
        )
        for fields in bad_fields:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                board_viewer.owner_message_input(fields)
        with self.assertRaisesRegex(ValueError, "UTF-8 bytes"):
            board_viewer.owner_message_input({"text": "é" * 10_001})

    def test_markdown_supporting_attachment_is_stored_as_inert_bytes(self):
        prepared = board._prepare_owner_message_attachments([{
            "filename": "../../review-notes.md",
            "content_type": "text/markdown; charset=utf-8",
            "data": b"# Notes\n<script>not executed</script>\n",
        }])
        self.assertEqual(prepared[0]["display_name"], "review-notes.md")
        self.assertEqual(prepared[0]["extension"], ".md")
        self.assertEqual(prepared[0]["content_type"], "text/markdown")
        with self.assertRaisesRegex(ValueError, "only documents"):
            board._prepare_owner_message_attachments([{
                "filename": "page.html", "content_type": "text/html", "data": b"<script>x</script>",
            }])

    def test_http_endpoint_accepts_markdown_direction_and_fails_closed_on_bad_filename(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(
                root, "engineering", board.AWAITING_OWNER_DIRECTION,
                vendor="OpenAI", session_id=session["id"],
            )
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/agents/{agent['id']}/owner-message"
                bad_body, bad_type = self.multipart({
                    "message_type": "direction", "text": "# Bad", "directive_source": "file",
                    "directive_filename": "../bad.md",
                }, [])
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(Request(url, data=bad_body, headers={"Content-Type": bad_type}, method="POST"), timeout=3)
                self.assertEqual(rejected.exception.code, 400)
                self.assertNotIn(session["id"], board.snapshot(root)["owner_directions"])

                body, content_type = self.multipart({
                    "message_type": "direction",
                    "text": "\r\n# Uploaded direction\r\n\r\nPreserve this text.\r\n",
                    "directive_source": "file",
                    "directive_filename": "TASK.md",
                }, [("attachments", "support.md", "text/markdown", b"# Supporting notes\n")])
                response = json.loads(urlopen(
                    Request(url, data=body, headers={"Content-Type": content_type}, method="POST"), timeout=3,
                ).read())
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()

            self.assertEqual(response["input"]["source"], "file")
            self.assertEqual(response["input"]["filename"], "TASK.md")
            saved = board.snapshot(root)["owner_directions"][session["id"]]
            self.assertEqual(saved["text"], "# Uploaded direction\n\nPreserve this text.")
            self.assertEqual(saved["attachments"][0]["content_type"], "text/markdown")
            stored = root / saved["attachments"][0]["stored_path"]
            self.assertEqual(stored.suffix, ".md")
            self.assertEqual(stored.read_bytes(), b"# Supporting notes\n")

    def test_both_direction_controls_advertise_markdown(self):
        page = board_viewer.rendered_page()
        self.assertIn('id="owner-message-directive-file" type="file" accept=".md,.txt,text/markdown,text/plain"', page)
        self.assertIn('id="owner-message-attachments" type="file" multiple accept=".pdf,.md,.txt', page)
        self.assertIn('id="decision-attachments" type="file" multiple accept=".pdf,.md,.txt', page)


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Real-browser and HTTP acceptance for long owner directives."""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, browser_acceptance, contract, control, project_worker
from harness.board_surface import SessionTokenAuthority
from tests.test_project_manager_rendered import proxy_handler
from tests.environment_support import require_loopback


HEAD = "TASK-D-LONG-DIRECTIVE-HEAD-7C4A"
MIDDLE = "TASK-D-LONG-DIRECTIVE-MIDDLE-91BF"
TAIL = "TASK-D-LONG-DIRECTIVE-TAIL-E205"


def long_directive() -> str:
    lines = [
        HEAD,
        "",
        "# Exact Markdown direction",
        "",
        "Preserve blank lines, UTF-8 café 🚀, and shell punctuation literally:",
        "`$HOME $(printf unsafe) && a | b; <tag> {json: true} [array]`",
        "",
    ]
    lines.extend(
        f"- Requirement {index:03d}: keep this exact payload segment /tmp/item-{index}?x=1&y=2."
        for index in range(90)
    )
    lines.insert(len(lines) // 2, MIDDLE)
    lines.extend(["", "```sh", "printf '%s\\n' 'do not execute this directive'", "```", "", TAIL])
    value = "\n".join(lines)
    assert 5_000 < len(value.encode("utf-8")) < board_viewer.OWNER_MESSAGE_MAX_BYTES
    return value


def _encoded_js_text(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def entry_script(value: str, session_id: str) -> str:
    encoded = _encoded_js_text(value)
    return f"""
<script>
(async()=>{{
  const decode=value=>new TextDecoder().decode(Uint8Array.from(atob(value),character=>character.charCodeAt(0)));
  const expected=decode('{encoded}');
  for(let attempt=0;attempt<100;attempt++){{
    const button=Array.from(document.querySelectorAll('button')).find(item=>item.textContent.trim()==='Give direction');
    if(button){{button.click();break;}} await new Promise(resolve=>setTimeout(resolve,100));
  }}
  const textarea=document.querySelector('#owner-message-text');
  textarea.value=expected;textarea.dispatchEvent(new Event('input',{{bubbles:true}}));
  const attachments=document.querySelector('#owner-message-attachments'),transfer=new DataTransfer();
  transfer.items.add(new File(['SUPPORTING-ATTACHMENT-BYTES'],'support.txt',{{type:'text/plain'}}));
  attachments.files=transfer.files;
  document.querySelector('#owner-message-form').requestSubmit();
  for(let attempt=0;attempt<100;attempt++){{
    if(!document.querySelector('#owner-message-dialog').open)break;
    await new Promise(resolve=>setTimeout(resolve,100));
  }}
  const dashboard=await (await fetch('/api/dashboard')).json();
  const saved=dashboard.state.owner_directions['{session_id}'];
  await fetch('/__layout_result__',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{
    dialogOpen:document.querySelector('#owner-message-dialog').open,
    notice:document.querySelector('#notice').textContent,
    durableText:saved?.text||'',
    attachmentCount:saved?.attachments?.length||0,
    attachmentName:saved?.attachments?.[0]?.display_name||'',
    byteCounter:document.querySelector('#owner-message-count').textContent,
  }})}});
}})();
</script>
"""


def file_validation_script(agent_id: str) -> str:
    return f"""
<script>
(async()=>{{
  const pause=delay=>new Promise(resolve=>setTimeout(resolve,delay));
  for(let attempt=0;attempt<100;attempt++){{
    const button=Array.from(document.querySelectorAll('button')).find(item=>item.textContent.trim()==='Give direction');
    if(button){{button.click();break;}} await pause(100);
  }}
  const input=document.querySelector('#owner-message-directive-file'),status=document.querySelector('#owner-message-file-status');
  async function choose(file){{const transfer=new DataTransfer();transfer.items.add(file);input.files=transfer.files;input.dispatchEvent(new Event('change',{{bubbles:true}}));await pause(100);return status.textContent;}}
  const wrong=await choose(new File(['valid'],'directive.pdf',{{type:'application/pdf'}}));
  const empty=await choose(new File([],'empty.md',{{type:'text/markdown'}}));
  const invalid=await choose(new File([new Uint8Array([255])],'invalid.txt',{{type:'text/plain'}}));
  const oversized=await choose(new File(['x'.repeat(20001)],'oversized.txt',{{type:'text/plain'}}));
  const lfStatus=await choose(new File(['# LF directive\\n\\n  keep interior indent\\n'],'ordinary.md',{{type:'text/markdown'}})),lfText=document.querySelector('#owner-message-text').value;
  const crlfStatus=await choose(new File(['# CRLF directive\\r\\n\\r\\nShip it.\\r\\n'],'windows.txt',{{type:'text/plain'}})),crlfText=document.querySelector('#owner-message-text').value;
  const leadingStatus=await choose(new File(['\\n\\n# Leading blank lines\\n\\nShip it.\\n'],'leading.md',{{type:'text/markdown'}})),leadingText=document.querySelector('#owner-message-text').value;
  const exact=await choose(new File(['x'.repeat(20000)+'\\n'],'maximum.md',{{type:'text/markdown'}}));
  ownerMessageAgentId='missing-agent';document.querySelector('#owner-message-form').requestSubmit();
  for(let attempt=0;attempt<60&&!document.querySelector('#owner-message-error').textContent;attempt++)await pause(50);
  const retryError=document.querySelector('#owner-message-error').textContent,textPreserved=document.querySelector('#owner-message-text').value.length;
  ownerMessageAgentId='{agent_id}';document.querySelector('#owner-message-form').requestSubmit();
  for(let attempt=0;attempt<100&&document.querySelector('#owner-message-dialog').open;attempt++)await pause(100);
  const dashboard=await (await fetch('/api/dashboard')).json(),saved=Object.values(dashboard.state.owner_directions||{{}})[0];
  await fetch('/__layout_result__',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{wrong,empty,invalid,oversized,lfStatus,lfText,crlfStatus,crlfText,leadingStatus,leadingText,exact,retryError,textPreserved,dialogOpen:document.querySelector('#owner-message-dialog').open,durableLength:saved?.text?.length||0}})}});
}})();
</script>
"""


def active_render_script(expected: str) -> str:
    encoded = _encoded_js_text(expected)
    return f"""
<script>
(async()=>{{
  const expected=new TextDecoder().decode(Uint8Array.from(atob('{encoded}'),character=>character.charCodeAt(0)));
  for(let attempt=0;attempt<100&&!document.querySelector('.task .directive-body');attempt++)await new Promise(resolve=>setTimeout(resolve,100));
  const dashboard=await (await fetch('/api/dashboard')).json(),task=Object.keys(dashboard.owner_directions)[0];
  await fetch('/__layout_result__',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{
    exactDirection:dashboard.owner_directions[task]===expected,
    exactConfirmation:dashboard.requirement_confirmations[task]?.text===expected,
    rendered:document.querySelector('.task .directive-body').textContent,
    requirements:document.querySelector('.task .requirements-body').textContent,
  }})}});
}})();
</script>
"""


def history_render_script(expected: str) -> str:
    encoded = _encoded_js_text(expected)
    return f"""
<script>
(async()=>{{
  const expected=new TextDecoder().decode(Uint8Array.from(atob('{encoded}'),character=>character.charCodeAt(0)));
  const details=document.querySelector('#history');details.open=true;details.dispatchEvent(new Event('toggle'));
  for(let attempt=0;attempt<100&&!document.querySelector('.history-item');attempt++)await new Promise(resolve=>setTimeout(resolve,100));
  const history=await (await fetch('/api/history')).json(),item=history.task_history[0];
  await fetch('/__layout_result__',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{
    exactDirection:item.owner_direction===expected,
    exactConfirmation:item.requirements_confirmation?.text===expected,
    rendered:document.querySelector('.history-directive-body').textContent,
    requirements:document.querySelector('.history-requirements-body').textContent,
  }})}});
}})();
</script>
"""


class LongDirectiveUITests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def waiting_agent(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(
            self.root, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        return session, agent

    def served(self, *, managed_worker=False):
        if managed_worker:
            endpoint = {"value": ""}
            handler = project_worker.make_handler(
                self.root,
                authority=SessionTokenAuthority(self.root),
                endpoint=lambda: endpoint["value"],
            )
        else:
            endpoint = None
            handler = board_viewer.make_handler(self.root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        if endpoint is not None:
            endpoint["value"] = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_address[1]}"

    def render(self, origin: str, script: str) -> dict:
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), proxy_handler(origin, sink, script))
        thread = threading.Thread(target=proxy.serve_forever, daemon=True); thread.start()
        profile = tempfile.TemporaryDirectory()
        process = browser_acceptance.launch(
            f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name),
            width=1400, height=1000,
        )
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
            self.assertIn("value", sink, "Chrome did not report long-directive UI state")
            return sink["value"]
        finally:
            process.close()
            profile.cleanup(); proxy.shutdown(); thread.join(timeout=3); proxy.server_close()

    @staticmethod
    def multipart(fields: dict[str, str | bytes], files=()):
        boundary = "----HarnessLongDirectiveBoundary"
        parts = []
        for name, value in fields.items():
            data = value if isinstance(value, bytes) else value.encode("utf-8")
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + data + b"\r\n"
            )
        for name, filename, content_type, content in files:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
                + content + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def post_owner_message(self, origin, agent_id, fields, files=()):
        body, content_type = self.multipart(fields, files)
        request = Request(
            origin + f"/api/agents/{agent_id}/owner-message", data=body,
            headers={"Content-Type": content_type}, method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_real_browser_long_text_reaches_intake_confirmation_task_and_history_exactly(self):
        session, agent = self.waiting_agent()
        expected = long_directive()
        server, thread, origin = self.served(managed_worker=True)
        try:
            entered = self.render(origin, entry_script(expected, session["id"]))
            self.assertFalse(entered["dialogOpen"])
            self.assertEqual(entered["durableText"], expected)
            self.assertEqual(entered["attachmentCount"], 1)
            self.assertEqual(entered["attachmentName"], "support.txt")
            self.assertNotIn("[Pasted text", entered["durableText"])

            message = board.snapshot(self.root)["owner_messages"][0]
            self.assertEqual(message["text"], expected)
            self.assertEqual(len(message["attachments"]), 1)
            instructions = control.take_instructions(self.root, session["id"])
            self.assertEqual(len(instructions), 1)
            self.assertIn(expected, instructions[0]["text"])
            self.assertIn("support.txt", instructions[0]["text"])

            task = "LONG-DIRECTIVE-E2E"
            board.begin_task(self.root, agent["id"], task)
            self.assertEqual(board.snapshot(self.root)["task_owner_directions"][task], expected)
            contract.create_contract(self.root, task, expected, ["preserve exact long directive"])
            board.record_requirement_confirmation(self.root, agent["id"], expected)
            active = self.render(origin, active_render_script(expected))
            self.assertTrue(active["exactDirection"])
            self.assertTrue(active["exactConfirmation"])
            for sentinel in (HEAD, MIDDLE, TAIL):
                self.assertIn(sentinel, active["rendered"])
                self.assertIn(sentinel, active["requirements"])

            with board.locked_state(self.root) as state:
                state["agents"][agent["id"]].update({"active": False, "status": "done"})
                state["task_chunks"][task] = {
                    "history": {"status": "passed", "description": "exact long directive"},
                }
            history = self.render(origin, history_render_script(expected))
            self.assertTrue(history["exactDirection"])
            self.assertTrue(history["exactConfirmation"])
            for sentinel in (HEAD, MIDDLE, TAIL):
                self.assertIn(sentinel, history["rendered"])
                self.assertIn(sentinel, history["requirements"])
        finally:
            server.shutdown(); thread.join(timeout=3); server.server_close()

    def test_real_browser_file_validation_exact_max_retry_and_success(self):
        _session, agent = self.waiting_agent()
        server, thread, origin = self.served()
        try:
            result = self.render(origin, file_validation_script(agent["id"]))
            self.assertIn(".md or .txt", result["wrong"])
            self.assertIn("empty", result["empty"])
            self.assertIn("not valid UTF-8", result["invalid"])
            self.assertIn("20,001", result["oversized"])
            self.assertIn("Loaded ordinary.md", result["lfStatus"])
            self.assertEqual(result["lfText"], "# LF directive\n\n  keep interior indent")
            self.assertIn("Loaded windows.txt", result["crlfStatus"])
            self.assertEqual(result["crlfText"], "# CRLF directive\n\nShip it.")
            self.assertIn("Loaded leading.md", result["leadingStatus"])
            self.assertEqual(result["leadingText"], "# Leading blank lines\n\nShip it.")
            self.assertIn("20,000 stored UTF-8 bytes", result["exact"])
            self.assertTrue(result["retryError"])
            self.assertEqual(result["textPreserved"], board_viewer.OWNER_MESSAGE_MAX_BYTES)
            self.assertFalse(result["dialogOpen"])
            self.assertEqual(result["durableLength"], board_viewer.OWNER_MESSAGE_MAX_BYTES)
        finally:
            server.shutdown(); thread.join(timeout=3); server.server_close()

    def test_http_file_metadata_newline_normalization_and_attachment_separation(self):
        cases = (
            ("ordinary.md", f"{HEAD}\n", HEAD),
            ("windows.txt", f"{HEAD}\r\n\r\nShip it.\r\n", f"{HEAD}\n\nShip it."),
            ("leading.md", f"\n\n{HEAD}\n\n  keep interior indent\n{TAIL}\n", f"{HEAD}\n\n  keep interior indent\n{TAIL}"),
            ("classic.txt", f"{HEAD}\rMiddle café\r{TAIL}\r", f"{HEAD}\nMiddle café\n{TAIL}"),
        )
        for filename, raw_text, expected in cases:
            with self.subTest(filename=filename):
                self.root = Path(tempfile.mkdtemp(dir=self._tmp.name))
                _session, agent = self.waiting_agent()
                server, thread, origin = self.served()
                try:
                    response = self.post_owner_message(origin, agent["id"], {
                        "message_type": "direction", "text": raw_text,
                        "directive_source": "file", "directive_filename": filename,
                    }, [("attachments", "notes.txt", "text/plain", b"SEPARATE")])
                    self.assertEqual(response["input"], {
                        "source": "file", "filename": filename,
                        "normalized_bytes": len(expected.encode("utf-8")),
                        "sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest(),
                        "newline_normalization": "CRLF and CR become LF; leading and trailing whitespace is removed",
                    })
                    self.assertEqual(response["message"]["text"], expected)
                    self.assertEqual(len(response["message"]["attachments"]), 1)
                    self.assertNotIn("SEPARATE", response["message"]["text"])
                finally:
                    server.shutdown(); thread.join(timeout=3); server.server_close()

    def test_http_empty_invalid_utf8_one_byte_over_and_partial_upload_leave_no_direction(self):
        cases = (
            ({"message_type": "direction", "text": "", "directive_source": "text"}, "required"),
            ({"message_type": "direction", "text": b"\xff", "directive_source": "text"}, "UTF-8"),
            ({"message_type": "direction", "text": "x" * (board_viewer.OWNER_MESSAGE_MAX_BYTES + 1), "directive_source": "text"}, "limit"),
        )
        for fields, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                self.root = Path(tempfile.mkdtemp(dir=self._tmp.name))
                _session, agent = self.waiting_agent()
                server, thread, origin = self.served()
                try:
                    with self.assertRaises(HTTPError) as failed:
                        self.post_owner_message(origin, agent["id"], fields)
                    self.assertEqual(failed.exception.code, 400)
                    self.assertIn(expected_error, failed.exception.read().decode())
                    self.assertFalse(board.snapshot(self.root)["owner_directions"])
                finally:
                    server.shutdown(); thread.join(timeout=3); server.server_close()

        self.root = Path(tempfile.mkdtemp(dir=self._tmp.name))
        _session, agent = self.waiting_agent()
        server, thread, _origin = self.served()
        client = socket.create_connection(server.server_address, timeout=5)
        try:
            fragment = b'{"message_type":"direction"'
            headers = (
                f"POST /api/agents/{agent['id']}/owner-message HTTP/1.1\r\n".encode()
                + b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(fragment) + 100}\r\n".encode()
                + b"Connection: close\r\n\r\n"
            )
            client.sendall(headers + fragment); client.shutdown(socket.SHUT_WR)
            response = b""
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
        finally:
            client.close(); server.shutdown(); thread.join(timeout=3); server.server_close()
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])
        self.assertFalse(board.snapshot(self.root)["owner_directions"])


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import copy
import json
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen

from harness import board, board_viewer
from tests.environment_support import require_loopback


class ReleaseFeedbackDecisionTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def released(self, root: Path, task: str = "TASK-RELEASE"):
        cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        release = board.record_release_ready(root, cto["id"], task, checks | {"head_commit": "release-commit"})
        return release

    def test_owner_response_is_durable_and_does_not_mutate_release_certification(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-DURABLE"
            release = self.released(root, task)
            before = copy.deepcopy(board.snapshot(root)["releases"][task])

            response = board.record_release_decision(root, task, "accepted")
            state = board.snapshot(root)

            self.assertEqual(response["decision"], "accepted")
            self.assertEqual(state["release_decisions"][task]["decision"], "accepted")
            self.assertEqual(state["releases"][task], before)
            self.assertEqual(release["status"], "VISUAL_TEST_REQUIRED")
            with self.assertRaisesRegex(ValueError, "already been recorded"):
                board.record_release_decision(root, task, "accepted")

    def test_release_card_renders_two_owner_actions_and_live_json_acceptance(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-CARD"
            self.released(root, task)
            script = board_viewer.rendered_page().split("<script>", 1)[1].split("</script>", 1)[0].split("el('#status-dialog-close')", 1)[0]
            state = json.dumps({"releases": {task: {"status": "VISUAL_TEST_REQUIRED"}}, "task_chunks": {}, "qa_requests": {}, "agents": {}})
            contracts = json.dumps({task: {"task": task}})
            invocation = """
const nodes={tasks:{children:[],innerHTML:'',replaceChildren(){this.children=[]},append(...items){this.children.push(...items)}}};
function makeEl(tag){
  const n={tagName:tag,className:'',dataset:{},_kids:[],_html:'',
    appendChild(child){n._kids.push(child);return child;},
    querySelector(sel){const cls=sel.replace(/^\\./,'');return n._kids.find(k=>String(k.className||'').split(' ').includes(cls))||null;}};
  Object.defineProperty(n,'innerHTML',{get(){return n._kids.length?n._kids.map(k=>k.innerHTML).join(''):n._html;},set(v){n._html=v;n._kids=[];}});
  return n;
}
globalThis.window={innerHeight:900,addEventListener(){}};
globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;},createElement(tag){return makeEl(tag);}};
tasks(%s,%s,{});
process.stdout.write(JSON.stringify(nodes.tasks.children[0].innerHTML));
""" % (state, contracts)
            completed = subprocess.run(["node", "-e", script + "\n" + invocation], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            rendered = completed.stdout
            self.assertIn("Accepted", json.loads(rendered))
            self.assertIn("Not accepted", json.loads(rendered))

            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/releases/{task}/decision"
                request = Request(url, data=json.dumps({"decision": "accepted"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=3) as response:
                    body = json.loads(response.read())
                    self.assertEqual(response.status, 201)
                self.assertEqual(body["decision"]["decision"], "accepted")
                self.assertEqual(board.snapshot(root)["release_decisions"][task]["decision"], "accepted")
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()

    def test_owner_response_requires_a_released_task_and_a_rejection_reason(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "released task"):
                board.record_release_decision(root, "TASK-NOT-RELEASED", "accepted")
            self.released(root, "TASK-REASON")
            with self.assertRaisesRegex(ValueError, "reason is required"):
                board.record_release_decision(root, "TASK-REASON", "not_accepted")


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import os
import hashlib
import json
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, contract, control
from tests.environment_support import require_loopback
from tests.requirements_support import agreed_requirements


class BoardViewerTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def script(self):
        return board_viewer.rendered_page().split("<script>", 1)[1].split("</script>", 1)[0]

    def declarations_only(self):
        return self.script().split("el('#status-dialog-close')", 1)[0]

    def run_node(self, invocation):
        completed = subprocess.run(
            ["node", "-e", self.declarations_only() + "\n" + invocation],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def task_gate(self, state, contract, agent, reviews, total=4, done=4):
        return self.run_node(
            "process.stdout.write(JSON.stringify(taskGate("
            + json.dumps(state)
            + ",'TASK',"
            + json.dumps(contract)
            + ","
            + json.dumps(agent)
            + ","
            + json.dumps(reviews)
            + f",{total},{done})));"
        )

    def rendered_task_cards(self, state, contracts, directions, live_tasks=None, requirements=None):
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
globalThis.document={
  querySelector(selector){return nodes[selector.slice(1)]||null;},
  createElement(tag){return makeEl(tag);}
};
tasks(%s,%s,%s,%s,[],%s);
process.stdout.write(JSON.stringify(nodes.tasks.children.map(node=>({html:node.innerHTML,task:node.dataset.task}))));
""" % (json.dumps(state), json.dumps(contracts), json.dumps(directions), json.dumps(live_tasks), json.dumps(requirements or {}))
        return self.run_node(invocation)

    def rendered_sessions(self, sessions_value, state, contracts):
        invocation = """
const nodes={sessions:{children:[],innerHTML:'',replaceChildren(){this.children=[]},append(...items){this.children.push(...items)}}};
globalThis.document={
  querySelector(selector){return nodes[selector.slice(1)]||null;},
  createElement(tag){return {tagName:tag,className:'',dataset:{},innerHTML:'',children:[],append(...items){this.children.push(...items)}};}
};
lastBoard={state:%s,contracts:%s};
sessions(%s,%s,%s);
process.stdout.write(JSON.stringify(nodes.sessions.children.map(row=>({
  sessionId:row.dataset.sessionId,
  task:row.dataset.task,
  label:row.children[0].innerHTML,
  stopSessionId:row.children[1].dataset.sessionId,
  stopTask:row.children[1].dataset.task
}))));
""" % (json.dumps(state), json.dumps(contracts), json.dumps(sessions_value), json.dumps(state), json.dumps(contracts))
        return self.run_node(invocation)

    def rendered_open_agents(self, state, sessions_value=None, contracts=None):
        invocation = """
const nodes={agents:{children:[],innerHTML:'',replaceChildren(){this.children=[];this.innerHTML=''},append(...items){this.children.push(...items)}}};
globalThis.document={
  querySelector(selector){return nodes[selector.slice(1)]||null;},
  createElement(tag){return {tagName:tag,className:'',dataset:{},innerHTML:'',textContent:'',children:[],disabled:false,append(...items){this.children.push(...items)}};}
};
openAgents(%s,%s,%s);
process.stdout.write(JSON.stringify({
  html:nodes.agents.innerHTML,
  rows:nodes.agents.children.map(row=>({
    agentId:row.dataset.agentId||'',
    sessionId:row.dataset.sessionId||'',
    task:row.dataset.task||'',
    html:row.innerHTML,
    buttons:(row.children.at(-1)?.children||[]).map(button=>button.textContent)
  }))
}));
""" % (json.dumps(state), json.dumps(contracts or {}), json.dumps(sessions_value or []))
        return self.run_node(invocation)

    def agent_status_summary(self, agent, state, contracts):
        return self.run_node(
            "process.stdout.write(JSON.stringify(agentStatusSummary("
            + json.dumps(agent)
            + ","
            + json.dumps(state)
            + ","
            + json.dumps(contracts)
            + ")));"
        )

    def test_page_has_one_authoritative_copy_of_every_core_renderer(self):
        script = self.script()
        for name in ("badge", "taskGate", "tasks", "render", "sessions"):
            self.assertEqual(script.count(f"function {name}("), 1, name)
        syntax = subprocess.run(["node", "--check", "-"], input=script, capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_confirmed_requirements_are_grouped_and_historical_labels_are_explicit(self):
        html = self.run_node(
            "process.stdout.write(JSON.stringify(requirementsHtml(" + json.dumps(
                "Objective: Make status truthful. Deliverables: Show counts. "
                "Acceptance: Counts match evidence. Status: Requirements confirmed. "
                "Remaining work: Implement and review."
            ) + ")));"
        )
        self.assertIn("<h4>Objective</h4>", html)
        self.assertIn("Status when requirements were confirmed", html)
        self.assertIn("Remaining work when requirements were confirmed", html)
        self.assertNotIn("<p>Objective:", html)

    def test_single_line_objective_and_requirements_render_as_readable_sections(self):
        html = self.run_node(
            "process.stdout.write(JSON.stringify(requirementsHtml(" + json.dumps(
                "Objective: Move provider authentication into Settings. "
                "Requirements: add a masked key input; block launch when the key is missing; "
                "never expose <script>alert('secret')</script> in responses or logs; "
                "apply credentials only to newly launched sessions."
            ) + ")));"
        )
        self.assertIn("<h4>Objective</h4>", html)
        self.assertIn("<h4>Requirements</h4>", html)
        self.assertIn('<ul class="requirements-list">', html)
        self.assertEqual(html.count("<li>"), 4)
        self.assertIn("block launch when the key is missing", html)
        self.assertNotIn("<p>Requirements:", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_two_progress_bars_use_durable_gates_and_current_stage_checks(self):
        points = self.run_node(r"""
const state={
  requirement_confirmations:{TASK:{text:'confirmed'}},
  delivery_plans:{TASK:{mode:'atomic',subtasks:{}}},
  task_briefs:{TASK:{update:'Implementation is active.'}},
  qa_requests:{},agents:{},releases:{},release_decisions:{}
};
const points=[];
function point(){
  const facts=taskFacts(state,{},'TASK');
  const gate=taskGate(state,'TASK',{},null,facts.reviews,facts.total,facts.done);
  points.push(taskProgress(state,'TASK',facts,gate));
}
point();
state.qa_requests.r={task:'TASK',status:'authoring',phase:'final_acceptance',delivery_state:'executing'};point();
Object.assign(state.qa_requests.r,{status:'open',delivery_state:'passed',delivery_evidence:'certified.json'});point();
Object.assign(state.qa_requests.r,{status:'passed',completed_at:'2026-08-19T10:00:00Z'});point();
state.releases.TASK={status:'VISUAL_TEST_REQUIRED'};point();
state.release_decisions.TASK={decision:'accepted'};point();
process.stdout.write(JSON.stringify(points));
""")
        overall = [point["overall"]["completed"] for point in points]
        self.assertEqual(overall, [1, 1, 2, 3, 4, 5])
        self.assertEqual(overall, sorted(overall))
        self.assertTrue(all(point["overall"]["total"] == 5 for point in points))
        self.assertEqual(points[1]["current"], {
            "label": "Delivery testing", "completed": 2, "total": 3,
        })

    def test_cto_status_projects_every_live_task_and_the_actual_repair_reason(self):
        state = {
            "live_tasks": ["REPAIR-TASK", "BUILD-TASK"],
            "delivery_plans": {
                "REPAIR-TASK": {"mode": "chunked", "subtasks": {}},
                "BUILD-TASK": {"mode": "atomic", "subtasks": {}},
            },
            "task_chunks": {"REPAIR-TASK": {"one": {"status": "passed"}, "two": {"status": "open"}}},
            "qa_requests": {"failed": {
                "task": "REPAIR-TASK", "status": "failed", "phase": "chunk",
                "result_summary": "The phone dialog opened below its status summary.",
                "completed_at": "2026-08-18T10:00:00+00:00",
            }},
            "agents": {"delivery": {"task": "BUILD-TASK", "role": "engineering", "status": "working"}},
            "releases": {}, "release_decisions": {}, "release_repairs": {},
        }
        rows = self.run_node(
            "process.stdout.write(JSON.stringify(ctoTaskRows(" + json.dumps(state) + ",{})));"
        )
        self.assertEqual([row["task"] for row in rows], ["REPAIR-TASK", "BUILD-TASK"])
        self.assertEqual(rows[0]["blocker"], "The phone dialog opened below its status summary.")
        self.assertEqual(rows[0]["stage"], "REPAIR IN PROGRESS")
        self.assertIn("1 remaining", rows[0]["counts"])
        self.assertEqual(rows[1]["ownerAction"], "None.")

    def test_cto_status_is_bounded_without_losing_the_total(self):
        state = {
            "live_tasks": [f"TASK-{index:02d}" for index in range(25)],
            "delivery_plans": {}, "task_chunks": {}, "qa_requests": {},
            "agents": {}, "releases": {}, "release_decisions": {},
        }
        result = self.run_node(
            "process.stdout.write(JSON.stringify({rows:ctoTaskRows(" + json.dumps(state)
            + ",{}).length,html:ctoTaskRowsHtml(" + json.dumps(state) + ",{})}));"
        )
        self.assertEqual(result["rows"], 20)
        self.assertIn("5 additional active tasks", result["html"])

    def test_server_certifies_legacy_ledgers_once_before_listening(self):
        calls = []

        class Server:
            server_address = ("127.0.0.1", 8742)

            def serve_forever(self):
                calls.append("serve")

            def server_close(self):
                calls.append("close")

        with patch.object(board, "recover_git_transactions", side_effect=lambda root: calls.append("recover")), \
                patch.object(board, "certify_legacy_review_ledgers", side_effect=lambda root: calls.append("certify")), \
                patch.object(board_viewer.release_coordinator, "coordinate", side_effect=lambda root: calls.append("coordinate")), \
                patch.object(board_viewer, "ThreadingHTTPServer", side_effect=lambda *args: calls.append("server") or Server()):
            board_viewer.serve(Path("/unused"))

        self.assertEqual(calls, ["recover", "certify", "coordinate", "server", "serve", "close"])

    def test_application_progress_counts_nested_chunks_without_moving_backwards(self):
        invocation = r"""
const state={
  requirement_confirmations:{TASK:{text:'confirmed'}},
  delivery_plans:{TASK:{mode:'application',subtasks:{
    alpha:{status:'open',chunks:{one:{status:'open'},two:{status:'open'}}},
    beta:{status:'open',chunks:{}}
  }}},qa_requests:{},agents:{},releases:{},release_decisions:{}
};
const points=[];
function point(review){
  state.qa_requests=review?{r:review}:{};
  const facts=taskFacts(state,{},'TASK');
  points.push({credit:facts.done,progress:taskGate(state,'TASK',{},null,facts.reviews,facts.total,facts.done).progress});
}
point({task:'TASK',status:'claimed',phase:'chunk',subtask:'alpha',chunk:'one'});
state.delivery_plans.TASK.subtasks.alpha.chunks.one.status='passed';
point({task:'TASK',status:'passed',phase:'chunk',subtask:'alpha',chunk:'one'});
point({task:'TASK',status:'failed',phase:'chunk',subtask:'alpha',chunk:'two'});
point({task:'TASK',status:'claimed',phase:'chunk',subtask:'alpha',chunk:'two'});
state.delivery_plans.TASK.subtasks.alpha.chunks.two.status='passed';
point({task:'TASK',status:'passed',phase:'chunk',subtask:'alpha',chunk:'two'});
point({task:'TASK',status:'claimed',phase:'subtask_acceptance',subtask:'alpha',chunk:'subtask-final'});
state.delivery_plans.TASK.subtasks.alpha.status='passed';
point({task:'TASK',status:'passed',phase:'subtask_acceptance',subtask:'alpha',chunk:'subtask-final'});
process.stdout.write(JSON.stringify(points));
"""
        points = self.run_node(invocation)
        progress = [point["progress"] for point in points]
        self.assertEqual(progress, sorted(progress), progress)
        self.assertGreater(points[1]["credit"], 0, "a passed nested chunk must earn durable credit")
        self.assertLess(points[1]["credit"], 1, "a nested chunk cannot replace subtask acceptance")
        self.assertEqual(points[-1]["credit"], 1)

    def test_completed_contract_is_not_complete_before_structured_release(self):
        reviews = [{"phase": "final_acceptance", "status": "passed", "completed_at": "2026-08-11T10:00:00Z"}]
        pending = self.task_gate({}, {"status": "complete"}, {"status": "done"}, reviews)
        self.assertEqual(pending["status"], "FINAL RELEASE CHECKS")
        self.assertEqual(pending["progress"], 92)
        self.assertIn("exact pushed version", pending["next"])
        released = self.task_gate(
            {"releases": {"TASK": {"status": "VISUAL_TEST_REQUIRED"}}},
            {"status": "complete"},
            {"status": "done"},
            reviews,
        )
        self.assertEqual(released["status"], "READY FOR YOUR TEST")
        self.assertEqual(released["progress"], 100)
        self.assertIn("visual test", released["next"])

    def test_four_of_four_chunks_with_failed_review_is_visibly_partial(self):
        reviews = [{"phase": "chunk", "status": "failed", "completed_at": "2026-08-11T10:00:00Z"}]
        gate = self.task_gate({}, {"status": "complete"}, {"status": "done"}, reviews)
        self.assertEqual(gate["status"], "REPAIR IN PROGRESS")
        self.assertLess(gate["progress"], 100)
        self.assertEqual(gate["progressTone"], "repair")
        cards = self.rendered_task_cards(
            {
                "task_chunks": {"TASK": {str(index): {"status": "passed"} for index in range(4)}},
                "qa_requests": {"failed": {"task": "TASK", **reviews[0]}},
                "agents": {"delivery": {"id": "delivery", "role": "engineering", "task": "TASK", "status": "done"}},
            },
            {"TASK": {"task": "TASK", "status": "complete"}},
            {"TASK": "Test the complete task and do not show false completion."},
            requirements={"TASK": {"text": "Final agreed requirements: test the complete task."}},
        )[0]["html"]
        self.assertIn("REPAIR IN PROGRESS", cards)
        self.assertIn("4 changes independently passed · 0 remaining", cards)
        self.assertNotIn("Overall:", cards)
        self.assertEqual(cards.count('role="progressbar"'), 2)
        self.assertIn('aria-label="Whole task progress"', cards)
        self.assertIn('aria-label="Current stage progress"', cards)
        self.assertIn("Current stage: Repair and re-review", cards)

    def test_failed_final_review_retains_final_review_progress(self):
        claimed = self.task_gate(
            {}, {"status": "complete"}, {"status": "review_wait"},
            [{"phase": "final_acceptance", "status": "claimed"}],
        )
        failed = self.task_gate(
            {}, {"status": "complete"}, {"status": "repairing"},
            [{"phase": "final_acceptance", "status": "failed"}],
        )
        self.assertEqual(claimed["progress"], 86)
        self.assertEqual(failed["progress"], 86)

    def test_adaptive_task_cards_explain_atomic_and_application_structures(self):
        atomic = self.rendered_task_cards(
            {"delivery_plans": {"SMALL": {"mode": "atomic", "rationale": "One cohesive correction", "subtasks": {}}}, "task_chunks": {}, "qa_requests": {}, "agents": {}},
            {"SMALL": {"task": "SMALL"}}, {"SMALL": "Make one cohesive correction."}, ["SMALL"],
        )[0]["html"]
        self.assertIn("One cohesive task · final independent acceptance still controls release", atomic)
        self.assertIn("· atomic", atomic)
        self.assertIn("Product Management structure", atomic)
        self.assertIn("One cohesive correction", atomic)
        application = self.rendered_task_cards(
            {
                "delivery_plans": {"APP": {"mode": "application", "rationale": "Several capabilities", "subtasks": {
                    "auth": {"title": "Authentication", "status": "passed", "dependencies": [], "chunks": {}},
                    "workspace": {"title": "Workspace", "status": "open", "dependencies": ["auth"], "chunks": {"ui": {"status": "passed"}}},
                }}},
                "task_chunks": {}, "qa_requests": {}, "agents": {},
            },
            {"APP": {"task": "APP"}}, {"APP": "Build the full application."}, ["APP"],
        )[0]["html"]
        self.assertIn("1 product subtask independently accepted · 1 remaining", application)
        self.assertIn("Product subtasks", application)
        self.assertIn("Authentication", application)
        self.assertIn("Workspace", application)
        self.assertIn("after auth", application)

    def test_final_review_wait_is_not_mislabeled_complete(self):
        reviews = [
            {"phase": "final_acceptance", "status": "passed", "completed_at": "2026-08-11T09:00:00Z"},
            {"phase": "final_acceptance", "status": "open", "requested_at": "2026-08-11T10:00:00Z"},
        ]
        gate = self.task_gate({}, {"status": "complete"}, {"status": "review_wait"}, reviews)
        self.assertEqual(gate["status"], "INDEPENDENT REVIEW IN PROGRESS")
        self.assertEqual(gate["progress"], 82)

    def test_reserved_review_is_labeled_preparing_not_waiting_or_executing(self):
        reviews = [{
            "phase": "final_acceptance", "status": "reserved",
            "reserved_by": "qa-1", "reserved_at": "2026-08-11T10:00:00Z",
        }]
        gate = self.task_gate({}, {"status": "complete"}, {"status": "review_wait"}, reviews)
        self.assertEqual(gate["status"], "REVIEWER PREPARING CHALLENGE LEDGER")
        self.assertIn("preparing a different Challenge Ledger", gate["next"])
        self.assertNotIn("testing", gate["next"].lower())
        self.assertIn("REVIEWER PREPARING CHALLENGE LEDGER", board_viewer.PAGE)
        self.assertIn("REVIEW EXECUTING", board_viewer.PAGE)

    def test_current_review_gate_names_the_actual_scope_and_ignores_older_claim(self):
        waiting = self.task_gate(
            {}, {"status": "complete"}, {"status": "review_wait"},
            [
                {"phase": "subtask_acceptance", "status": "claimed", "subtask": "OLD_UI", "claimed_at": "2026-08-11T10:00:00Z"},
                {"phase": "subtask_acceptance", "status": "open", "subtask": "SETTINGS_UI", "requested_at": "2026-08-11T11:00:00Z"},
            ],
        )
        self.assertIn("Settings Ui", waiting["next"])
        self.assertIn("waiting for an Independent Reviewer", waiting["next"])
        testing = self.task_gate(
            {}, {"status": "complete"}, {"status": "review_wait"},
            [
                {"phase": "subtask_acceptance", "status": "open", "subtask": "OLD_UI", "requested_at": "2026-08-11T10:00:00Z"},
                {"phase": "subtask_acceptance", "status": "claimed", "subtask": "CONFIG_MODEL", "claimed_at": "2026-08-11T11:00:00Z"},
            ],
        )
        self.assertIn("Config Model", testing["next"])
        self.assertIn("is testing", testing["next"])

    def test_two_tasks_render_one_scrollable_directive_each_without_global_duplicate(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = {}
            for task, text in (("VIEW-ALPHA", "Alpha intake pipeline direction."), ("VIEW-BETA", "Beta pricing report direction.")):
                session = control.create(root, "codex_delivery")
                agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
                board.record_owner_direction(root, session["id"], text)
                board.begin_task(root, agent["id"], task)
                contract.create_contract(root, task, text, ["viewer directive"])
                expected[task] = text
            dashboard = board_viewer.dashboard_payload(root)
            cards = self.rendered_task_cards(dashboard["state"], dashboard["contracts"], dashboard["owner_directions"])
            rendered = "\n".join(card["html"] for card in cards)
            self.assertEqual(len(cards), 2)
            self.assertEqual(dashboard["owner_directions"], expected)
            for text in expected.values():
                self.assertEqual(rendered.count(text), 1)
            self.assertIn("height:180px;max-height:180px;overflow-y:scroll", board_viewer.PAGE)
            self.assertNotIn('id="owner-directive"', board_viewer.PAGE)

    def test_delivery_progress_is_scrollable_and_newest_task_is_first(self):
        state = {
            "agents": {},
            "qa_requests": {
                "older": {"task": "OLDER-TASK", "requested_at": "2026-08-12T09:00:00+00:00"},
                "newer": {"task": "NEWER-TASK", "requested_at": "2026-08-12T11:00:00+00:00"},
            },
        }
        cards = self.rendered_task_cards(state, {"OLDER-TASK": {}, "NEWER-TASK": {}}, {"OLDER-TASK": "Older", "NEWER-TASK": "Newer"}, ["OLDER-TASK", "NEWER-TASK"])
        self.assertEqual([card["task"] for card in cards], ["NEWER-TASK", "OLDER-TASK"])
        self.assertIn("#tasks{height:auto;min-height:0;max-height:none;overflow-y:auto", board_viewer.PAGE)
        self.assertIn("function fitTaskScroller()", board_viewer.PAGE)
        self.assertIn("tasksNode.style.height='auto'", board_viewer.PAGE)
        self.assertIn("const previousScrollTop=out.scrollTop", board_viewer.PAGE)
        self.assertIn("out.scrollTop=Math.min(previousScrollTop", board_viewer.PAGE)
        self.assertIn("tasksNode.scrollTop=Math.min(previousScrollTop", board_viewer.PAGE)
        # The card is split into an independently-refreshed static region (the
        # settled directive/requirements) and a live sub-window; the directive
        # scroll is carried across the rare static-region rewrite.
        self.assertIn("className='task-static'", board_viewer.PAGE)
        self.assertIn("className='task-dynamic'", board_viewer.PAGE)
        self.assertIn("priorDirectiveScroll", board_viewer.PAGE)
        self.assertIn("historyDirectiveScrollTops=new Map", board_viewer.PAGE)
        self.assertIn("data-history-key=", board_viewer.PAGE)
        self.assertNotIn("window.addEventListener('scroll',fitTaskScroller", board_viewer.PAGE)
        self.assertIn("class=\"left-column\"", board_viewer.PAGE)
        self.assertIn("active-panel", board_viewer.PAGE)

    def test_directive_panel_is_first_inside_each_task_card_before_progress(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for task, text in (("VIEW-ALPHA", "Alpha directive."), ("VIEW-BETA", "Beta directive.")):
                session = control.create(root, "codex_delivery")
                agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
                board.record_owner_direction(root, session["id"], text)
                board.begin_task(root, agent["id"], task)
                contract.create_contract(root, task, text, ["directive placement"])
            dashboard = board_viewer.dashboard_payload(root)
            cards = self.rendered_task_cards(dashboard["state"], dashboard["contracts"], dashboard["owner_directions"])
            self.assertEqual(len(cards), 2)
            for card in cards:
                directive = card["html"].find('class="directive"')
                progress = card["html"].find('class="live-label"')
                self.assertGreaterEqual(directive, 0)
                self.assertGreater(progress, directive)
                # Title-first: the task-head (title + status) now leads the card,
                # with the directive below it and the progress region last.
                self.assertGreater(directive, card["html"].find('class="task-head"'))

    def test_two_delivery_terminals_name_exact_tasks_and_stop_targets(self):
        state = {
            "task_chunks": {
                "SCENARIO_LEDGER_SIMULATION_ENFORCEMENT": {"one": {"status": "passed"}},
                "VIEWER_DIRECTIVE_STATUS_RECOVERY": {"one": {"status": "passed"}},
            },
            "qa_requests": {
                "scenario-final": {"task": "SCENARIO_LEDGER_SIMULATION_ENFORCEMENT", "phase": "final_acceptance", "status": "open", "requested_at": "2026-08-11T10:00:00Z"},
                "viewer-fail": {"task": "VIEWER_DIRECTIVE_STATUS_RECOVERY", "phase": "chunk", "status": "failed", "completed_at": "2026-08-11T10:01:00Z"},
            },
            "agents": {
                "scenario-agent": {"id": "scenario-agent", "role": "engineering", "task": "SCENARIO_LEDGER_SIMULATION_ENFORCEMENT", "status": "review_wait", "active": False, "session_id": "codex-scenario"},
                "viewer-agent": {"id": "viewer-agent", "role": "engineering", "task": "VIEWER_DIRECTIVE_STATUS_RECOVERY", "status": "repairing", "active": True, "session_id": "codex-viewer"},
            },
            "requirement_confirmations": {
                "SCENARIO_LEDGER_SIMULATION_ENFORCEMENT": {"text": "Final agreed requirements: enforce simulations."},
                "VIEWER_DIRECTIVE_STATUS_RECOVERY": {"text": "Final agreed requirements: preserve viewer recovery."},
            },
        }
        contracts = {
            "SCENARIO_LEDGER_SIMULATION_ENFORCEMENT": {"status": "complete"},
            "VIEWER_DIRECTIVE_STATUS_RECOVERY": {"status": "partial"},
        }
        items = [
            {"id": "codex-scenario", "label": "CODEX CLI · Delivery Agent", "status": "running", "task": ""},
            {"id": "codex-viewer", "label": "CODEX CLI · Delivery Agent", "status": "running", "task": ""},
        ]
        rows = self.rendered_sessions(items, state, contracts)
        self.assertEqual(rows[0]["sessionId"], "codex-scenario")
        self.assertEqual(rows[0]["stopSessionId"], "codex-scenario")
        self.assertEqual(rows[0]["task"], "Scenario Ledger Simulation Enforcement")
        self.assertIn("INDEPENDENT REVIEW IN PROGRESS", rows[0]["label"])
        self.assertEqual(rows[1]["sessionId"], "codex-viewer")
        self.assertEqual(rows[1]["stopSessionId"], "codex-viewer")
        self.assertEqual(rows[1]["task"], "Viewer Directive Status Recovery")
        self.assertIn("REPAIR IN PROGRESS", rows[1]["label"])

    def test_reviewer_terminal_tracks_the_current_claim_in_real_time(self):
        reviewer = {"id": "reviewer-one", "role": "qa", "task": "OLD_TASK", "status": "qa_complete", "active": True, "session_id": "claude-reviewer"}
        session = [{"id": "claude-reviewer", "label": "CLAUDE CLI · Independent Reviewer", "status": "running", "task": ""}]
        idle_state = {"agents": {"reviewer": reviewer}, "qa_requests": {}}
        idle = self.rendered_sessions(session, idle_state, {})[0]
        self.assertEqual(idle["task"], "Independent review queue")
        self.assertIn("MONITORING REVIEW QUEUE", idle["label"])
        routed_state = {
            "agents": {"reviewer": reviewer},
            "qa_requests": {"routed": {"task": "ROUTED_REVIEW", "status": "open", "routed_to": "reviewer-one", "routed_at": "2026-08-11T09:59:00Z"}},
        }
        routed = self.rendered_sessions(session, routed_state, {})[0]
        self.assertEqual(routed["task"], "Routed Review")
        self.assertIn("REVIEW ROUTED — RESERVE NOW", routed["label"])
        routed_summary = self.agent_status_summary(reviewer, routed_state, {})
        self.assertIn("actively notified", routed_summary["summary"])
        self.assertIn("No owner action", routed_summary["next"])
        viewer_state = {
            "agents": {"reviewer": reviewer},
            "qa_requests": {"viewer": {"task": "VIEWER_DIRECTIVE_STATUS_RECOVERY", "status": "claimed", "claimed_by": "reviewer-one", "claimed_at": "2026-08-11T10:00:00Z"}},
        }
        viewer = self.rendered_sessions(session, viewer_state, {})[0]
        self.assertEqual(viewer["task"], "Viewer Directive Status Recovery")
        self.assertIn("INDEPENDENT REVIEW IN PROGRESS", viewer["label"])
        scenario_state = {
            "agents": {"reviewer": reviewer},
            "qa_requests": {"scenario": {"task": "SCENARIO_LEDGER_SIMULATION_ENFORCEMENT", "status": "claimed", "claimed_by": "reviewer-one", "claimed_at": "2026-08-11T10:01:00Z"}},
        }
        scenario = self.rendered_sessions(session, scenario_state, {})[0]
        self.assertEqual(scenario["task"], "Scenario Ledger Simulation Enforcement")
        self.assertIn("INDEPENDENT REVIEW IN PROGRESS", scenario["label"])

    def test_stop_confirmation_names_and_stops_only_the_selected_terminal(self):
        result = self.run_node("""
const nodes={notice:{textContent:''}};
globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;}};
const calls=[];let prompt='';
globalThis.window={confirm(message){prompt=message;return true;}};
globalThis.fetch=async path=>{calls.push(path);return{ok:true,json:async()=>({})};};
refresh=async()=>{};
(async()=>{
  const stopped=await confirmStopSession('codex-viewer','CODEX CLI · Delivery Agent','Viewer Directive Status Recovery','REPAIR IN PROGRESS');
  process.stdout.write(JSON.stringify({stopped,calls,prompt,notice:nodes.notice.textContent}));
})();
""")
        self.assertTrue(result["stopped"])
        self.assertEqual(result["calls"], ["/api/sessions/codex-viewer/stop"])
        self.assertIn("Viewer Directive Status Recovery", result["prompt"])
        self.assertIn("REPAIR IN PROGRESS", result["prompt"])
        self.assertIn("Viewer Directive Status Recovery", result["notice"])

    def test_canceling_stop_confirmation_changes_nothing(self):
        result = self.run_node("""
const nodes={notice:{textContent:''}};
globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;}};
const calls=[];
globalThis.window={confirm(){return false;}};
globalThis.fetch=async path=>{calls.push(path);return{ok:true,json:async()=>({})};};
(async()=>{
  const stopped=await confirmStopSession('codex-one','CODEX CLI · Delivery Agent','Task One','DEVELOPMENT IN PROGRESS');
  process.stdout.write(JSON.stringify({stopped,calls}));
})();
""")
        self.assertFalse(result["stopped"])
        self.assertEqual(result["calls"], [])

    def test_cto_popup_is_plain_english(self):
        state = {
            "task_chunks": {"RELEASE": {}, "VIEWER": {}},
            "qa_requests": {"review": {"task": "VIEWER", "status": "failed"}},
            "releases": {},
            "live_tasks": ["RELEASE", "VIEWER"],
        }
        wording = self.agent_status_summary(
            {"role": "cto", "task": "GLOBAL_MONITOR", "liveness": "healthy"},
            state,
            {"RELEASE": {"task": "RELEASE", "status": "complete"}},
        )
        self.assertIn("monitoring 2 current tasks", wording["summary"])
        self.assertIn("No current task is ready for your test yet", wording["summary"])
        self.assertIn("You do not need to do anything", wording["summary"])
        self.assertNotIn("S-V003", wording["summary"])

    def test_cto_popup_excludes_accepted_history_and_requests_current_owner_test(self):
        state = {
            "task_chunks": {"CURRENT": {}, "OLD": {}},
            "releases": {
                "CURRENT": {"status": "VISUAL_TEST_REQUIRED"},
                "OLD": {"status": "VISUAL_TEST_REQUIRED"},
            },
            "release_decisions": {"OLD": {"decision": "accepted"}},
            "live_tasks": ["CURRENT"],
        }
        wording = self.agent_status_summary(
            {"role": "cto", "task": "GLOBAL_MONITOR", "liveness": "healthy"},
            state,
            {"CURRENT": {"status": "complete"}, "OLD": {"status": "complete"}},
        )
        self.assertIn("monitoring 1 current task", wording["summary"])
        self.assertIn("1 task is ready for your test", wording["summary"])
        self.assertNotIn("do not need to do anything", wording["summary"])
        self.assertEqual(wording["ownerAction"], "Test the ready task, then choose Accepted or Send feedback.")

    def test_status_popup_uses_human_labels(self):
        html = board_viewer.PAGE
        for phrase in ("Current situation", "Current stage", "What happens next", "Your action"):
            self.assertIn(phrase, html)
        self.assertNotIn("<dt>Board status</dt>", html)
        self.assertNotIn("<dt>Latest board update</dt>", html)

    def test_recovery_button_api_preserves_blocked_agent_task(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            board.record_owner_direction(root, session["id"], "Repair the visible blocker without losing task memory.")
            board.begin_task(root, agent["id"], "VIEWER-RECOVERY")
            board.status(root, agent["id"], "Waiting on a failed review repair", "blocked")
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                request = Request(base + f"/api/agents/{agent['id']}/recover", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                response = json.loads(urlopen(request, timeout=3).read())
            finally:
                server.shutdown();thread.join(timeout=3);server.server_close()
            recovered = board.snapshot(root)["agents"][agent["id"]]
            self.assertEqual(response["recovery"]["kind"], "agent_recovery_requested")
            self.assertEqual(recovered["task"], "VIEWER-RECOVERY")
            self.assertEqual(recovered["recovery_state"], "reset_requested")
            self.assertIn("function recoverAgent", board_viewer.PAGE)
            self.assertIn("Recover agent", board_viewer.PAGE)

    def test_dashboard_reconciles_agent_after_terminal_exits(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            control.attach(root, session["id"], 99999999)
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id=session["id"])
            data = board_viewer.dashboard_payload(root)
            self.assertNotIn(agent["id"], data["state"]["agents"])

    def test_live_viewer_api_and_dom_prove_complete_owner_objective_with_concurrent_refresh(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "VIEWER-INTEGRATION-LIVE"
            direction = (
                "First paragraph: keep the complete owner direction visible for this task.\n\n"
                "Second paragraph: show every active agent with a human-readable status dialog and preserve recovery memory.\n\n"
                "Third paragraph: reconcile an exited terminal, keep exact terminal and task mapping, and refresh concurrently.\n\n"
                "Fourth paragraph: verify review and release states through the running viewer API. LONG-DIRECTIVE-TAIL-SENTINEL."
            )
            live_helper = subprocess.Popen(["sleep", "30"])
            dead_session = control.create(root, "codex_delivery")
            control.attach(root, dead_session["id"], 99999999)
            dead_agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=dead_session["id"])
            board.record_owner_direction(root, dead_session["id"], "Reconcile the exited integration terminal.")
            contract.create_contract(root, "VIEWER-EXITED-INTEGRATION", "Reconcile the exited integration terminal.", ["exited terminal"])
            board.begin_task(root, dead_agent["id"], "VIEWER-EXITED-INTEGRATION")

            live_session = control.create(root, "codex_delivery")
            control.attach(root, live_session["id"], live_helper.pid)
            live_agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=live_session["id"])
            board.record_owner_direction(root, live_session["id"], direction)
            contract.create_contract(root, task, direction, ["live viewer integration"])
            evidence = root / "integration-evidence.txt"
            evidence.write_text("command: python3 -m unittest\nresult: PASS\n")
            contract.add_evidence(root, task, "live viewer integration", [evidence])
            board.begin_task(root, live_agent["id"], task)
            agreed_requirements(root, live_agent["id"], "Final agreed requirements: prove the live viewer objective, preserve task lineage, and verify the complete workflow.")
            board.define_delivery_plan(root, live_agent["id"], "chunked", "Four bounded viewer risks require separate review")
            board.declare_chunks(root, live_agent["id"], [
                ("directive-and-status-ui", "directive and status"),
                ("blocker-recovery-memory", "recovery memory"),
                ("task-workspace-isolation", "workspace lineage"),
                ("integration-regression", "complete viewer integration"),
            ])
            board.task_brief(root, live_agent["id"], "I will prove the complete viewer objective.", "The live API and rendered DOM are being checked.")
            board.status(root, live_agent["id"], "Waiting on an integration review", "blocked")

            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                root_response = urlopen(base + "/", timeout=3)
                page = root_response.read().decode()
                dashboard_response = urlopen(base + "/api/dashboard", timeout=3)
                dashboard = json.loads(dashboard_response.read())
                managed_response = urlopen(base + "/api/control", timeout=3)
                managed = json.loads(managed_response.read())
                self.assertEqual(root_response.headers["Cache-Control"], "no-store")
                self.assertEqual(dashboard_response.headers["Cache-Control"], "no-store")
                self.assertEqual(dashboard["viewer_version"], board_viewer.viewer_version())
                self.assertIn("window.location.reload()", page)
                self.assertEqual(dashboard["owner_directions"][task], direction)
                self.assertNotIn(dead_agent["id"], dashboard["state"]["agents"])

                cards = [card for card in self.rendered_task_cards(dashboard["state"], dashboard["contracts"], dashboard["owner_directions"]) if card["task"] == task]
                self.assertEqual(len(cards), 1)
                card = cards[0]["html"]
                self.assertGreater(card.find('class="directive"'), card.find('class="task-head"'))  # title-first
                self.assertLess(card.find('class="directive"'), card.find('class="live-label"'))
                self.assertIn("LONG-DIRECTIVE-TAIL-SENTINEL", card)
                self.assertIn("height:180px;max-height:180px;overflow-y:scroll", page)

                dom_script = self.declarations_only()
                invocation = """
const nodes={
  agents:{children:[],innerHTML:'',replaceChildren(){this.children=[]},append(...items){this.children.push(...items)}},
  'status-dialog-title':{textContent:''},
  'status-dialog-body':{innerHTML:''},
  'status-dialog':{showModal(){}}
};
globalThis.document={
  querySelector(selector){return nodes[selector.slice(1)]||null;},
  createElement(tag){return {tagName:tag,className:'',dataset:{},innerHTML:'',textContent:'',children:[],append(...items){this.children.push(...items)}};}
};
lastBoard=%s;
openAgents(lastBoard.state,lastBoard.contracts);
showAgentStatus(%s);
process.stdout.write(JSON.stringify({rows:nodes.agents.children.map(row=>({html:row.innerHTML,buttons:row.children.at(-1).children.map(button=>button.textContent)})),title:nodes['status-dialog-title'].textContent,body:nodes['status-dialog-body'].innerHTML}));
""" % (json.dumps(dashboard), json.dumps(live_agent["id"]))
                rendered = json.loads(subprocess.run(["node", "-e", dom_script + invocation], capture_output=True, text=True, check=True).stdout)
                live_row = next(row for row in rendered["rows"] if "Viewer Integration Live" in row["html"])
                self.assertEqual(live_row["buttons"].count("View status"), 1)
                self.assertIn("Recover agent", live_row["buttons"])
                for phrase in ("Current situation", "Current stage", "What happens next", "Your action"):
                    self.assertIn(phrase, rendered["body"])

                recover_request = Request(base + f"/api/agents/{live_agent['id']}/recover", data=b"{}", headers={"Content-Type":"application/json"}, method="POST")
                recover_response = json.loads(urlopen(recover_request, timeout=3).read())
                self.assertEqual(recover_response["recovery"]["kind"], "agent_recovery_requested")
                recovered = json.loads(urlopen(base + "/api/dashboard", timeout=3).read())["state"]["agents"][live_agent["id"]]
                self.assertEqual(recovered["task"], task)
                self.assertEqual(recovered["recovery_state"], "reset_requested")

                dead_recover = Request(base + f"/api/agents/{dead_agent['id']}/recover", data=b"{}", headers={"Content-Type":"application/json"}, method="POST")
                with self.assertRaises(HTTPError) as failure:
                    urlopen(dead_recover, timeout=3)
                self.assertEqual(failure.exception.code, 400)
                self.assertIn("inactive", failure.exception.read().decode())

                def refresh():
                    with urlopen(base + "/api/dashboard", timeout=3) as response:
                        return response.status, json.loads(response.read())["owner_directions"][task]

                with ThreadPoolExecutor(max_workers=8) as pool:
                    refreshes = list(pool.map(lambda _: refresh(), range(8)))
                self.assertEqual(refreshes, [(200, direction)] * 8)
                self.assertEqual(next(item for item in managed["sessions"] if item["id"] == live_session["id"])["id"], live_session["id"])
                terminal = next(item for item in self.rendered_sessions(managed["sessions"], dashboard["state"], dashboard["contracts"]) if item["sessionId"] == live_session["id"])
                self.assertEqual(dashboard["state"]["agents"][live_agent["id"]]["task"], task)
                self.assertEqual(terminal["task"], "Viewer Integration Live")
                self.assertEqual(terminal["stopSessionId"], live_session["id"])
                self.assertEqual(terminal["stopTask"], "Viewer Integration Live")

                ledger = root / "integration-ledger.md"
                probe = root / "integration_review_probe.py"
                probe.write_text(
                    "import unittest\n"
                    "\n"
                    "class ReviewStateProbe(unittest.TestCase):\n"
                    "    def test_review_state_machine_probe(self):\n"
                    "        self.assertTrue(True)\n"
                    "\n"
                    "if __name__ == '__main__':\n"
                    "    unittest.main()\n"
                )
                probe_command = "python3 -m unittest integration_review_probe"
                challenge_probe_command = "python3 -m unittest integration_review_probe.ReviewStateProbe.test_review_state_machine_probe"
                ledger.write_text("| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|---|---|\n| S-INT-001 | The live dashboard reflects the accepted work without losing its current state. | live viewer integration | `python3 -m unittest integration_review_probe` | The local review-state probe executes one positive test from the temporary board root and returns success. | PASS: local review-state probe executed one test successfully. | PASS |\n")
                reviewer = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
                challenge_evidence = root / "challenge-evidence.txt"
                challenge_evidence.write_text("command: python3 -m unittest\nresult: PASS\n")
                chunks = ["directive-and-status-ui", "blocker-recovery-memory", "task-workspace-isolation", "integration-regression"]
                for chunk in chunks:
                    request = board.request_review(root, live_agent["id"], str(ledger.relative_to(root)), "prove live integration chunk", chunk=chunk, test_command=probe_command)
                    if chunk == chunks[0]:
                        review_dashboard = json.loads(urlopen(base + "/api/dashboard", timeout=3).read())
                        self.assertTrue(any(item["id"] == request["id"] and item["status"] == "open" for item in review_dashboard["state"]["qa_requests"].values()))
                    challenge = root / f"{chunk}-challenge.md"
                    challenge.write_text(f"| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|---|---|\n| S-INT-CHALLENGE | An independent check confirms the live dashboard stays usable after review. | independent live challenge | `{challenge_probe_command}` | The local challenge probe executes one positive test from the temporary board root and returns success. | PASS: local challenge probe executed one test successfully. | PASS |\n")
                    board.claim_qa(root, reviewer["id"], request["id"], str(challenge.relative_to(root)))
                    board.execute_challenge(root, reviewer["id"], request["id"])
                    board.qa_result(root, reviewer["id"], request["id"], "passed", "live integration challenge passed", str(challenge_evidence))
                final = board.request_review(root, live_agent["id"], str(ledger.relative_to(root)), "prove the complete live viewer objective", phase="final_acceptance", test_command=probe_command)
                final_dashboard_open = json.loads(urlopen(base + "/api/dashboard", timeout=3).read())
                self.assertTrue(any(item["id"] == final["id"] and item["status"] == "open" for item in final_dashboard_open["state"]["qa_requests"].values()))
                final_challenge = root / "final-challenge.md"
                final_challenge.write_text(f"| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|---|---|\n| S-INT-FINAL | The completed dashboard remains accurate after every required check finishes. | complete live challenge | `{challenge_probe_command}` | The local final challenge probe executes one positive test from the temporary board root and returns success. | PASS: local final challenge probe executed one test successfully. | PASS |\n")
                board.claim_qa(root, reviewer["id"], final["id"], str(final_challenge.relative_to(root)))
                board.execute_challenge(root, reviewer["id"], final["id"])
                board.qa_result(root, reviewer["id"], final["id"], "passed", "complete live integration passed", str(challenge_evidence))
                board.complete(root, live_agent["id"], "live integration objective complete")
                cto_agent = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
                release = board.record_release_ready(root, cto_agent["id"], task, {key: True for key in board.RELEASE_REQUIRED_CHECKS} | {"head_commit": "live-integration"})
                self.assertEqual(release["status"], "VISUAL_TEST_REQUIRED")
                final_dashboard = json.loads(urlopen(base + "/api/dashboard", timeout=3).read())
                self.assertEqual(final_dashboard["state"]["releases"][task]["status"], "VISUAL_TEST_REQUIRED")
                final_cards = [card for card in self.rendered_task_cards(final_dashboard["state"], final_dashboard["contracts"], final_dashboard["owner_directions"], final_dashboard["live_tasks"], final_dashboard["requirement_confirmations"]) if card["task"] == task]
                self.assertEqual(len(final_cards), 1)
                self.assertIn("READY FOR YOUR TEST", final_cards[0]["html"])
            finally:
                server.shutdown();thread.join(timeout=3);server.server_close()
                if live_helper.poll() is None:
                    live_helper.terminate();live_helper.wait(timeout=3)

    def test_root_and_dashboard_share_version_and_disable_browser_cache(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                root_response = urlopen(base + "/", timeout=3)
                html = root_response.read().decode()
                dashboard_response = urlopen(base + "/api/dashboard", timeout=3)
                dashboard = json.loads(dashboard_response.read())
            finally:
                server.shutdown();thread.join(timeout=3);server.server_close()
            self.assertEqual(root_response.headers["Cache-Control"], "no-store")
            self.assertEqual(dashboard_response.headers["Cache-Control"], "no-store")
            self.assertIn(f'content="{dashboard["viewer_version"]}"', html)
            self.assertIn(f"const loadedViewerVersion='{dashboard['viewer_version']}'", html)
            self.assertIn("window.location.reload()", html)

    def test_viewer_serves_zero_configuration_human_dashboard(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            direction = "Show clear delivery progress in the viewer."
            board.record_owner_direction(root, session["id"], direction)
            board.begin_task(root, agent["id"], "VIEW-1")
            contract.create_contract(root, "VIEW-1", direction, ["viewer progress"])
            agreed_requirements(root, agent["id"], "Final agreed requirements: show clear delivery progress and preserve the owner direction.")
            board.task_brief(root, agent["id"], "I will make progress easy to understand.", "Preparing the first reviewable change.")
            data = board_viewer.dashboard_payload(root)
            html = board_viewer.rendered_page()
            self.assertIn("NoMoreHappyPath Mission Control", html)
            self.assertNotIn("Show technical audit board", html)
            self.assertIn("Delivery progress", html)
            self.assertIn("Active agents and terminals", html)
            self.assertNotIn(">Running terminals<", html)
            self.assertIn("READY FOR YOUR TEST", html)
            self.assertIn("codex_delivery:2,claude_reviewer:2,claude_cto:1", html)
            self.assertIn("active_counts", html)
            self.assertEqual(data["state"]["agents"][agent["id"]]["task"], "VIEW-1")
            self.assertEqual(data["owner_directions"]["VIEW-1"], direction)

    def test_viewer_exposes_color_palette_and_cancel_black_fallback(self):
        html = board_viewer.rendered_page()
        self.assertIn("Choose terminal color", html)
        self.assertIn("Cancel — use black", html)
        self.assertIn("terminalColors", html)
        self.assertIn("showColorDialog('codex_delivery')", html)

    def test_viewer_uses_registered_project_and_rejects_workspace_override(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"
            selected = Path(tmp) / "selected"
            root.mkdir(); selected.mkdir()
            html = board_viewer.rendered_page()
            self.assertNotIn("Browse folders", html)
            self.assertNotIn("Save workspace folder", html)
            self.assertNotIn('id="workspace-root"', html)
            # Provider access lives with the PROJECT, not the global page, and
            # its copy states the per-launch scope.
            self.assertIn("AI access for this project", html)
            self.assertIn("never written globally", html)
            from harness import project_manager_page
            self.assertNotIn("Provider access", project_manager_page.PAGE)
            self.assertNotIn("pa-apply-claude", project_manager_page.PAGE)
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                request = Request(base + "/api/settings", data=json.dumps({"workspace_root": str(selected)}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=3)
                self.assertEqual(error.exception.code, 400)
                self.assertIn("managed from Projects", error.exception.read().decode())
                loaded = json.loads(urlopen(base + "/api/settings", timeout=3).read())
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()
            self.assertEqual(loaded["workspace_root"], str(root.resolve()))
            self.assertEqual(loaded["claude"]["settings_path"], str(root.resolve() / ".claude" / "settings.local.json"))

    def run_node_with_runtime(self, commit, invocation):
        page = board_viewer.rendered_page(project_id="p1", runtime={"commit": commit})
        script = page.split("<script>", 1)[1].split("</script>", 1)[0]
        declarations = script.split("el('#status-dialog-close')", 1)[0]
        completed = subprocess.run(
            ["node", "-e", declarations + "\n" + invocation],
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_external_release_is_ready_for_test_not_deployment_refresh(self):
        release = {
            "task": "TASK", "status": "VISUAL_TEST_REQUIRED",
            "head_commit": "e" * 40,
            "runtime_verification_deferred_to_target_acceptance": False,
            "checks": {"candidate_health_verified": True, "ready_for_owner_test": True},
        }
        state = {"releases": {"TASK": release}, "release_decisions": {}, "delivery_plans": {},
                 "qa_requests": {}, "requirement_confirmations": {"TASK": {"text": "yes"}}}
        gate = self.run_node_with_runtime(
            "f" * 40,
            f"const gate=taskGate({json.dumps(state)},'TASK',{{}},null,[],1,1);"
            + "process.stdout.write(JSON.stringify(gate));",
        )
        self.assertEqual(gate["status"], "READY FOR YOUR TEST")
        self.assertEqual(gate["progressTone"], "ready")

    def test_runtime_gated_release_still_requires_the_serving_commit(self):
        release = {
            "task": "TASK", "status": "VISUAL_TEST_REQUIRED",
            "head_commit": "e" * 40,
            "runtime_verification_deferred_to_target_acceptance": False,
            "checks": {"deployed_runtime_verified": True, "deployed_chat_verified": True},
        }
        state = {"releases": {"TASK": release}, "release_decisions": {}, "delivery_plans": {},
                 "qa_requests": {}, "requirement_confirmations": {"TASK": {"text": "yes"}}}
        gate = self.run_node_with_runtime(
            "f" * 40,
            f"const gate=taskGate({json.dumps(state)},'TASK',{{}},null,[],1,1);"
            + "process.stdout.write(JSON.stringify(gate));",
        )
        self.assertEqual(gate["status"], "DEPLOYMENT REFRESH REQUIRED")
        matching = self.run_node_with_runtime(
            "e" * 40,
            f"const gate=taskGate({json.dumps(state)},'TASK',{{}},null,[],1,1);"
            + "process.stdout.write(JSON.stringify(gate));",
        )
        self.assertEqual(matching["status"], "READY FOR YOUR TEST")

    def test_external_release_acceptance_buttons_are_available(self):
        state = {
            "releases": {"TASK": {
                "task": "TASK", "status": "VISUAL_TEST_REQUIRED",
                "head_commit": "e" * 40,
                "runtime_verification_deferred_to_target_acceptance": False,
                "checks": {"ready_for_owner_test": True},
            }},
            "release_decisions": {}, "release_repairs": {}, "git_acceptances": {},
            "remote_push_instructions": {}, "remote_push_outcomes": {},
        }
        html = self.run_node_with_runtime(
            "f" * 40,
            f"const html=releaseResponseHtml({json.dumps(state)},'TASK');"
            + "process.stdout.write(JSON.stringify({html}));",
        )["html"]
        self.assertNotIn("Acceptance unavailable", html)
        self.assertIn("Accepted", html)

    def test_dashboard_keeps_only_recent_events_and_indexes_older_task_activity(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {"events": [{"sequence": n, "task": "TASK-OLD", "at": f"2026-01-01T00:00:{n:02d}+00:00"} for n in range(1, 75)]}
            compact = board_viewer._compact_dashboard_state(state)
            self.assertEqual(len(compact["events"]), 50)
            self.assertEqual(compact["latest_event_at_by_task"]["TASK-OLD"], state["events"][-1]["at"])

    def test_agent_settings_show_provider_specific_models_efforts_and_connection_test(self):
        from harness import project_manager_page
        html = project_manager_page.PAGE
        self.assertIn(" connection", html)
        self.assertIn("provider_efforts", html)
        self.assertIn("data-setting-model-choice", html)
        self.assertIn("Custom model ID…", html)
        self.assertIn('data-page="help"', html)
        self.assertNotIn('list="models-', html)
        board_html = board_viewer.rendered_page()
        self.assertNotIn('id="settings"', board_html)
        self.assertNotIn("settings-dialog", board_html)
        self.assertNotIn("Provider access", board_html)
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"; root.mkdir()
            capture = Path(tmp) / "claude-args"
            fake = Path(tmp) / "fake-claude"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CAPTURE\"\n", encoding="utf-8")
            fake.chmod(0o755)
            with patch.dict(os.environ, {"HARNESS_CLAUDE_BIN": str(fake), "CAPTURE": str(capture)}):
                result = board_viewer.test_provider_connection(root, "claude", "xhigh", "opus")
            self.assertEqual(result["effort"], "max")
            self.assertEqual(result["model"], "opus")
            self.assertIn("--model\nopus\n--effort\nmax", capture.read_text(encoding="utf-8"))

    def run_manager_node(self, invocation):
        from harness import project_manager_page
        script = project_manager_page.PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
        declarations = script.split("q('#settings-form').addEventListener", 1)[0]
        prelude = (
            "globalThis.window={location:{pathname:'/'},history:{replaceState(){}},addEventListener(){}};"
            "globalThis.document={querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){}};"
            "globalThis.fetch=async()=>({ok:true,json:async()=>({})});"
        )
        completed = subprocess.run(
            ["node", "-e", prelude + "\n" + declarations + "\n" + invocation],
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_model_dropdown_renders_every_available_choice_and_custom_model(self):
        catalog = {"provider_models": board_viewer.available_provider_models()}
        result = self.run_manager_node(
            f"settingsCatalog={json.dumps(catalog)};"
            "process.stdout.write(JSON.stringify({codex:modelOptions('codex','gpt-5.6-sol'),claude:modelOptions('claude','claude-fable-5[1m]')}));"
        )
        for model in ("gpt-5.6-sol", "gpt-5.6-sol-wm", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark", "codex-auto-review"):
            self.assertIn(f'value="{model}"', result["codex"])
        for model in ("claude-fable-5[1m]", "claude-fable-5", "claude-opus-5", "claude-sonnet-5", "opus", "sonnet", "haiku"):
            self.assertIn(f'value="{model}"', result["claude"])
        self.assertIn('value="__custom__"', result["codex"])
        self.assertIn('value="__custom__"', result["claude"])

    def test_selected_model_reads_dropdown_or_separate_custom_input(self):
        result = self.run_manager_node(
            "const nodes={choice:{value:'gpt-5.6-terra'},custom:{value:'custom-model-42'},provider:{value:'codex'},effort:{value:'high'}};"
            "globalThis.document={querySelector(selector){if(selector.includes('model-choice'))return nodes.choice;if(selector.includes('data-setting-model'))return nodes.custom;if(selector.includes('provider'))return nodes.provider;if(selector.includes('effort'))return nodes.effort;return null;},querySelectorAll(){return[];}};"
            "const known=settingValue('delivery').model;nodes.choice.value='__custom__';const custom=settingValue('delivery').model;"
            "process.stdout.write(JSON.stringify({known,custom}));"
        )
        self.assertEqual(result, {"known": "gpt-5.6-terra", "custom": "custom-model-42"})

    def test_switching_provider_replaces_the_complete_visible_model_list(self):
        catalog = {"provider_models": board_viewer.available_provider_models()}
        result = self.run_manager_node(
            f"settingsCatalog={json.dumps(catalog)};"
            "process.stdout.write(JSON.stringify({codex:modelOptions('codex','gpt-5.6-luna'),claude:modelOptions('claude','claude-opus-5')}));"
        )
        self.assertIn('value="gpt-5.6-sol-wm"', result["codex"])
        self.assertIn('value="gpt-5.6-luna" selected', result["codex"])
        self.assertNotIn('value="claude-opus-5"', result["codex"])
        self.assertIn('value="claude-fable-5[1m]"', result["claude"])
        self.assertIn('value="claude-opus-5" selected', result["claude"])
        self.assertNotIn('value="gpt-5.6-sol"', result["claude"])
        self.assertIn('value="__custom__"', result["codex"])
        self.assertIn('value="__custom__"', result["claude"])

    def test_settings_api_exposes_multiple_locally_discovered_models_for_each_provider(self):
        with TemporaryDirectory() as tmp:
            payload = board_viewer.settings_payload(Path(tmp))
        self.assertGreaterEqual(len(payload["provider_models"]["codex"]), 9)
        self.assertGreaterEqual(len(payload["provider_models"]["claude"]), 7)
        self.assertIn("gpt-5.6-sol-wm", payload["provider_models"]["codex"])
        self.assertIn("claude-opus-5", payload["provider_models"]["claude"])

    def test_connection_test_reports_cli_rejection_instead_of_claiming_success(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"; root.mkdir()
            fake = Path(tmp) / "fake-claude"
            fake.write_text("#!/bin/sh\necho 'bad effort' >&2\nexit 2\n", encoding="utf-8")
            fake.chmod(0o755)
            with patch.dict(os.environ, {"HARNESS_CLAUDE_BIN": str(fake)}):
                with self.assertRaisesRegex(ValueError, "rejected model opus or effort max"):
                    board_viewer.test_provider_connection(root, "claude", "max", "opus")

    def test_codex_connection_test_preserves_model_and_normalizes_max_effort(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"; root.mkdir()
            capture = Path(tmp) / "codex-args"
            fake = Path(tmp) / "fake-codex"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CAPTURE\"\n", encoding="utf-8")
            fake.chmod(0o755)
            with patch.dict(os.environ, {"HARNESS_CODEX_BIN": str(fake), "CAPTURE": str(capture)}):
                result = board_viewer.test_provider_connection(root, "codex", "max", "gpt-5.6-sol-wm")
            self.assertEqual(result["effort"], "xhigh")
            self.assertEqual(result["model"], "gpt-5.6-sol-wm")
            self.assertIn("--model\ngpt-5.6-sol-wm\n-c\nmodel_reasoning_effort=xhigh", capture.read_text(encoding="utf-8"))

    def test_corrupt_legacy_workspace_settings_falls_back_without_killing_viewer(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"
            root.mkdir()
            settings_file = root / ".harness" / "control" / "workspace_settings.json"
            settings_file.parent.mkdir(parents=True)
            settings_file.write_text("{ not json")
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                loaded = json.loads(urlopen(base + "/api/settings", timeout=3).read())
                self.assertEqual(loaded["workspace_root"], str(root.resolve()))
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()

    def test_provider_access_has_no_manual_apply_surface(self):
        # Access is automatic on open (owner directive); the page carries no
        # apply action and no apply JS at all.
        html = board_viewer.rendered_page()
        self.assertNotIn("applyAccess", html)
        self.assertNotIn("access-apply", html)
        self.assertIn("configured automatically", html)

    def test_workspace_override_javascript_is_absent(self):
        html = board_viewer.rendered_page()
        for forbidden in ("workspaceDraftDirty", "saveWorkspace", "browseWorkspace", "workspace-browse", "workspace-save"):
            self.assertNotIn(forbidden, html)

    def test_combined_settings_endpoint_keeps_workspace_and_agent_settings_separate(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "dev_harness"
            selected = Path(tmp) / "selected"
            root.mkdir(); selected.mkdir()
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                def post(value):
                    request = Request(base + "/api/settings", data=json.dumps(value).encode(), headers={"Content-Type": "application/json"}, method="POST")
                    return json.loads(urlopen(request, timeout=3).read())
                with self.assertRaises(HTTPError) as error:
                    post({"workspace_root": str(selected)})
                self.assertEqual(error.exception.code, 400)
                provider = {
                    "settings": {
                        "delivery": {"provider": "claude", "model": "sonnet", "effort": "medium"},
                        "cto": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "low"},
                        "reviewer": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"},
                    }
                }
                combined = post(provider)
                loaded = json.loads(urlopen(base + "/api/settings", timeout=3).read())
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()
            self.assertEqual(combined["workspace_root"], str(root.resolve()))
            self.assertEqual(combined["settings"], provider["settings"])
            self.assertEqual(combined["agent_settings"], provider["settings"])
            self.assertEqual(loaded["workspace_root"], str(root.resolve()))
            self.assertEqual(loaded["settings"], provider["settings"])

    def test_owner_message_api_sends_direction_and_clarification_with_files(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://{server.server_address[0]}:{server.server_address[1]}"
                def multipart(fields, files):
                    boundary = "----HarnessOwnerMessageBoundary"
                    parts = []
                    for name, value in fields.items():
                        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
                    for name, filename, content_type, content in files:
                        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode() + content + b"\r\n")
                    parts.append(f"--{boundary}--\r\n".encode())
                    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
                body, content_type = multipart({"message_type": "direction", "text": "Use the viewer composer for this complete task."}, [("attachments", "screen.png", "image/png", b"SCREEN")])
                request = Request(base + f"/api/agents/{agent['id']}/owner-message", data=body, headers={"Content-Type": content_type}, method="POST")
                response = json.loads(urlopen(request, timeout=3).read())
                self.assertEqual(response["message"]["type"], "direction")
                body, content_type = multipart({"message_type": "clarification", "text": "Go ahead, and preserve the screenshot requirement."}, [("attachments", "approval.txt", "text/plain", b"APPROVED")])
                request = Request(base + f"/api/agents/{agent['id']}/owner-message", data=body, headers={"Content-Type": content_type}, method="POST")
                response = json.loads(urlopen(request, timeout=3).read())
                self.assertEqual(response["message"]["type"], "clarification")
                self.assertIn(session["id"], board.snapshot(root)["pending_owner_clarifications"])
                board.begin_task(root, agent["id"], "VIEWER-COMPOSER-TASK")
                moved = board.snapshot(root)["owner_clarifications"]["VIEWER-COMPOSER-TASK"]
                self.assertEqual(moved[0]["text"], "Go ahead, and preserve the screenshot requirement.")
                self.assertEqual(moved[0]["task"], "VIEWER-COMPOSER-TASK")
                contract.create_contract(root, "VIEWER-COMPOSER-TASK", "Use the viewer composer for this complete task.", ["delivery"])
                agreed_requirements(root, agent["id"], "Final agreed requirements: use the composer and test the full task.")
                body, content_type = multipart({"message_type": "clarification", "text": "Add a provider-swap edge case."}, [("attachments", "notes.txt", "text/plain", b"EDGE")])
                request = Request(base + f"/api/agents/{agent['id']}/owner-message", data=body, headers={"Content-Type": content_type}, method="POST")
                response = json.loads(urlopen(request, timeout=3).read())
                self.assertEqual(response["message"]["type"], "clarification")
                clarifications = board.snapshot(root)["owner_clarifications"]["VIEWER-COMPOSER-TASK"]
                self.assertEqual(len(clarifications), 2)
                self.assertEqual(clarifications[-1]["text"], "Add a provider-swap edge case.")
                page = board_viewer.rendered_page()
                self.assertIn("Give direction", page)
                self.assertIn("Send clarification", page)
                self.assertIn("owner-message-attachments", page)
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()

    def test_waiting_agent_can_respond_after_direction_is_recorded(self):
        state = {
            "agents": {"delivery": {"id": "delivery", "role": "engineering", "display_name": "Delivery Agent", "task": "AWAITING_OWNER_DIRECTION", "session_id": "session-1", "active": True}},
            "owner_directions": {"session-1": {"text": "A complete direction already arrived."}},
        }
        invocation = """
const nodes={agents:{children:[],replaceChildren(){this.children=[]},append(...items){this.children.push(...items)}}};
globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;},createElement(tag){return {tagName:tag,className:'',dataset:{},innerHTML:'',children:[],append(...items){this.children.push(...items)}};}};
openAgents(%s,{});
const row=nodes.agents.children[0],button=row.children[0].children[0];
process.stdout.write(JSON.stringify({label:button.textContent,disabled:button.disabled}));
""" % json.dumps(state)
        result = self.run_node(invocation)
        self.assertEqual(result["label"], "Respond to Delivery")
        self.assertFalse(result["disabled"])

    def test_active_agent_and_terminal_are_rendered_as_one_row(self):
        state = {
            "agents": {"delivery": {"id": "delivery", "role": "engineering", "vendor": "OpenAI", "display_name": "Delivery Agent", "task": "TASK-ONE", "session_id": "session-1", "active": True, "status": "working", "last_status_at": "2026-08-12T12:00:00Z"}},
            "qa_requests": {}, "task_chunks": {}, "delivery_plans": {},
        }
        sessions = [{"id": "session-1", "label": "CODEX CLI · Delivery Agent", "status": "running", "task": "", "model": "gpt-5.6-sol", "color_hex": "#123B5D", "color_label": "Ocean blue"}]
        invocation = """
const nodes={agents:{children:[],replaceChildren(){this.children=[]},append(...items){this.children.push(...items)}}};
globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;},createElement(tag){return {tagName:tag,className:'',dataset:{},innerHTML:'',textContent:'',children:[],append(...items){this.children.push(...items)}};}};
openAgents(%s,{},%s);
process.stdout.write(JSON.stringify({count:nodes.agents.children.length,html:nodes.agents.children[0].innerHTML,buttons:nodes.agents.children[0].children[0].children.map(button=>button.textContent)}));
""" % (json.dumps(state), json.dumps(sessions))
        result = self.run_node(invocation)
        self.assertEqual(result["count"], 1)
        self.assertIn("Provider: OpenAI", result["html"])
        self.assertIn("Model: gpt-5.6-sol", result["html"])
        self.assertIn("Terminal color: Ocean blue", result["html"])
        self.assertIn("Stop terminal", result["buttons"])

    def test_paused_unattached_session_is_not_shown_as_starting_terminal(self):
        sessions = [{
            "id": "codex-paused", "label": "CODEX CLI", "status": "paused",
            "task": "", "color_hex": "#000000", "color_label": "Standard black",
            "reason": "terminal intentionally paused with its saved session pointer",
        }]
        result = self.rendered_open_agents({"agents": {}}, sessions)
        self.assertEqual(result["rows"], [])
        self.assertIn("No active agents or terminals.", result["html"])
        self.assertNotIn("STARTING", result["html"])
        self.assertNotIn("Waiting to attach", result["html"])

    def test_paused_session_can_only_render_through_its_paused_board_agent(self):
        state = {
            "agents": {
                "delivery": {
                    "id": "delivery", "role": "engineering", "vendor": "OpenAI",
                    "display_name": "Delivery Agent", "task": "PAUSED-TASK",
                    "session_id": "codex-paused", "active": False, "status": "paused",
                }
            },
            "qa_requests": {}, "task_chunks": {}, "delivery_plans": {},
        }
        sessions = [{
            "id": "codex-paused", "label": "CODEX CLI", "status": "paused",
            "task": "", "color_hex": "#000000", "color_label": "Standard black",
        }]
        result = self.rendered_open_agents(state, sessions)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["agentId"], "delivery")
        self.assertIn("PAUSED", result["rows"][0]["html"])
        self.assertNotIn("STARTING", result["rows"][0]["html"])
        self.assertNotIn("Waiting to attach", result["rows"][0]["html"])

    def test_superseded_predecessor_is_never_shown_as_a_second_current_agent(self):
        state = {
            "agents": {"replacement": {"id": "engineering-new", "role": "engineering", "vendor": "OpenAI", "display_name": "Delivery Agent", "task": "RECOVERED-TASK", "session_id": "session-new", "active": True, "status": "recovered"}},
            "qa_requests": {}, "task_chunks": {}, "delivery_plans": {},
        }
        sessions = [
            {"id": "session-new", "label": "CODEX CLI", "status": "running", "task": "", "color_hex": "#123B5D", "color_label": "Ocean blue"},
            {"id": "session-old", "label": "CODEX CLI", "status": "stopping", "task": "", "read_only": True, "superseded_task": "RECOVERED-TASK", "superseded_by_agent_id": "engineering-new", "color_hex": "#5A2525", "color_label": "Brick red"},
        ]
        invocation = """
const nodes={agents:{children:[],replaceChildren(){this.children=[]},append(...items){this.children.push(...items)}}};
globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;},createElement(tag){return {tagName:tag,className:'',dataset:{},innerHTML:'',textContent:'',children:[],disabled:false,append(...items){this.children.push(...items)}};}};
openAgents(%s,{},%s);
process.stdout.write(JSON.stringify(nodes.agents.children.map(row=>({html:row.innerHTML,task:row.dataset.task,buttons:(row.children[0]?.children||[]).map(button=>button.textContent)}))));
""" % (json.dumps(state), json.dumps(sessions))
        rows = self.run_node(invocation)
        current = [row for row in rows if "Board stage" in row["html"]]
        predecessor = [row for row in rows if "STOPPING — SUPERSEDED" in row["html"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(len(predecessor), 1)
        self.assertIn("read-only", predecessor[0]["html"].lower())
        self.assertIn("engineering-new", predecessor[0]["html"])

    def test_finished_delivery_terminal_keeps_status_and_feedback_until_owner_accepts(self):
        task = "OWNER-STILL-DECIDING"
        agent = {"id": "delivery", "role": "engineering", "vendor": "OpenAI", "display_name": "Delivery Agent", "task": task, "session_id": "session-1", "active": False, "status": "done", "last_status_at": "2026-08-12T12:00:00Z"}
        session = {"id": "session-1", "label": "CODEX CLI · Delivery Agent", "status": "running", "task": "", "color_hex": "#123B5D", "color_label": "Ocean blue"}
        base_state = {"agents": {"delivery": agent}, "qa_requests": {}, "task_chunks": {}, "delivery_plans": {}, "releases": {task: {"status": "VISUAL_TEST_REQUIRED"}}, "release_decisions": {}}
        invocation = """
const nodes={agents:{children:[],replaceChildren(){this.children=[]},append(...items){this.children.push(...items)},innerHTML:''}};
globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;},createElement(tag){return {tagName:tag,className:'',dataset:{},innerHTML:'',textContent:'',children:[],disabled:false,append(...items){this.children.push(...items)}};}};
openAgents(%s,{},[%s]);
const row=nodes.agents.children[0];
process.stdout.write(JSON.stringify({count:nodes.agents.children.length,buttons:row.children[0].children.map(button=>button.textContent)}));
""" % (json.dumps(base_state), json.dumps(session))
        result = self.run_node(invocation)
        self.assertEqual(result["count"], 1)
        self.assertIn("Send feedback", result["buttons"])
        self.assertIn("View status", result["buttons"])
        self.assertIn("Stop terminal", result["buttons"])

        accepted = json.loads(json.dumps(base_state))
        accepted["release_decisions"][task] = {"decision": "accepted"}
        accepted_result = self.run_node(invocation.replace(json.dumps(base_state), json.dumps(accepted), 1))
        self.assertEqual(accepted_result["count"], 1)
        self.assertNotIn("Send feedback", accepted_result["buttons"])
        self.assertIn("View status", accepted_result["buttons"])

    def test_dashboard_keeps_finished_delivery_agent_while_its_terminal_is_running(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "OWNER-STILL-DECIDING"
            session = control.create(root, "codex_delivery")
            agent = board.register(
                root,
                "engineering",
                board.AWAITING_OWNER_DIRECTION,
                vendor="OpenAI",
                session_id=session["id"],
            )
            board.record_owner_direction(root, session["id"], "Keep status and feedback available until I stop the terminal.")
            board.begin_task(root, agent["id"], task)
            contract.create_contract(root, task, "Keep status and feedback available until I stop the terminal.", ["visible owner controls"])
            with board.locked_state(root) as state:
                state["agents"][agent["id"]].update({"active": False, "status": "done"})
                state["archive"].append({
                    "kind": "qa_request",
                    "archived_at": "2026-08-13T12:00:00+00:00",
                    "value": {
                        "id": "finished-final-01",
                        "task": task,
                        "status": "passed",
                        "phase": "final_acceptance",
                        "completed_at": "2026-08-13T12:00:00+00:00",
                    },
                })
                state["releases"][task] = {
                    "task": task,
                    "status": "VISUAL_TEST_REQUIRED",
                    "cto_id": "cto",
                    "recorded_at": "2026-08-13T12:01:00+00:00",
                }

            dashboard = board_viewer.dashboard_payload(root)
            self.assertIn(agent["id"], dashboard["state"]["agents"])
            rows = self.run_node("""
const nodes={agents:{children:[],replaceChildren(){this.children=[]},append(...items){this.children.push(...items)},innerHTML:''}};
globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;},createElement(tag){return {tagName:tag,className:'',dataset:{},innerHTML:'',textContent:'',children:[],disabled:false,append(...items){this.children.push(...items)}};}};
openAgents(%s,%s,%s);
process.stdout.write(JSON.stringify(nodes.agents.children.map(row=>({agentId:row.dataset.agentId,buttons:row.children[0].children.map(button=>button.textContent)}))));
""" % (json.dumps(dashboard["state"]), json.dumps(dashboard["contracts"]), json.dumps(control.snapshot(root)["sessions"])))
            row = next(value for value in rows if value["agentId"] == agent["id"])
            self.assertIn("Send feedback", row["buttons"])
            self.assertIn("View status", row["buttons"])
            self.assertIn("Stop terminal", row["buttons"])

            control.fail_launch(root, session["id"], "terminal was stopped before attachment")
            compact = board_viewer.dashboard_payload(root)
            self.assertNotIn(agent["id"], compact["state"]["agents"])

    def test_active_agents_have_deterministic_role_then_task_order(self):
        state = {
            "agents": {
                "workspace": {"id": "engineering-z", "role": "engineering", "display_name": "Delivery Agent", "task": "AGENT_WORKSPACE_PERMISSION_SETTINGS", "active": True, "status": "working"},
                "provider": {"id": "engineering-a", "role": "engineering", "display_name": "Delivery Agent", "task": "AGENT_PROVIDER_EFFORT_SETTINGS", "active": True, "status": "working"},
                "cto": {"id": "cto-z", "role": "cto", "display_name": "CTO", "task": "GLOBAL_MONITOR", "active": True, "status": "working"},
            }
        }
        invocation = """
const nodes={agents:{children:[],replaceChildren(){this.children=[]},append(...items){this.children.push(...items)}}};
globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;},createElement(tag){return {tagName:tag,className:'',dataset:{},innerHTML:'',children:[],append(...items){this.children.push(...items)}};}};
openAgents(%s,{});
process.stdout.write(JSON.stringify(nodes.agents.children.map(row=>row.dataset.agentId)));
""" % json.dumps(state)
        self.assertEqual(self.run_node(invocation), ["cto-z", "engineering-a", "engineering-z"])

    def test_task_card_shows_original_direction_then_final_requirements(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            original = "Build the requested feature, then clarify its exact acceptance behavior."
            board.record_owner_direction(root, session["id"], original)
            board.begin_task(root, agent["id"], "REQUIREMENTS-VIEW")
            contract.create_contract(root, "REQUIREMENTS-VIEW", original, ["delivery"])
            agreed_requirements(root, agent["id"], "Final agreed requirements: include the clarified behavior and its edge cases.")
            data = board_viewer.dashboard_payload(root)
            cards = self.rendered_task_cards(data["state"], data["contracts"], data["owner_directions"], data["live_tasks"], data["requirement_confirmations"])
            html = cards[0]["html"]
            self.assertLess(html.find("Full user directive"), html.find("Final agreed requirements"))
            self.assertIn("clarified behavior", html)

    def test_final_requirements_render_transport_escaped_line_breaks_as_structure(self):
        requirements = (
            r"Objective:\nVerify the existing candidate without rebuilding it.\n\n"
            r"Required verification:\n1. Inspect the diff.\n2. Execute the tests.\n\n"
            r"Safety and scope:\nNever start the live daemon."
        )
        cards = self.rendered_task_cards(
            {
                "agents": {"delivery": {"id": "delivery", "role": "engineering", "task": "VERIFY", "status": "working"}},
                "task_chunks": {},
                "qa_requests": {},
            },
            {"VERIFY": {"task": "VERIFY", "status": "open"}},
            {"VERIFY": "Original owner directive with real\nline breaks."},
            ["VERIFY"],
            {"VERIFY": {"text": requirements, "version": 1}},
        )
        html = cards[0]["html"]
        confirmation = html.split("Final agreed requirements", 1)[1].split('class="task-head"', 1)[0]
        self.assertNotIn(r"\n", confirmation)
        self.assertIn("<p>Objective:</p><p>Verify the existing candidate without rebuilding it.</p>", confirmation)
        self.assertIn("<ol><li>Inspect the diff.</li><li>Execute the tests.</li></ol>", confirmation)
        self.assertIn("<p>Safety and scope:</p><p>Never start the live daemon.</p>", confirmation)

    def test_display_normalizer_preserves_a_single_literal_newline_code_token(self):
        result = self.run_node(
            r'''process.stdout.write(JSON.stringify(displayMultilineText("Keep the literal \\n token in this inline example.")));'''
        )
        self.assertEqual(result, r"Keep the literal \n token in this inline example.")

    def test_sessions_render_persisted_color_identity(self):
        sessions = [{"id": "codex-blue", "label": "CODEX CLI", "status": "running", "task": "", "color_hex": "#123B5D", "color_label": "Ocean blue"}]
        rows = self.rendered_sessions(sessions, {"agents": {}}, {})
        self.assertIn("Ocean blue", rows[0]["label"])
        self.assertIn("Terminal color:", rows[0]["label"])
        self.assertIn("background:#123B5D", rows[0]["label"])

    def test_dashboard_separates_live_tasks_from_durable_history_and_keeps_owner_action_live(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            board.record_owner_direction(root, session["id"], "Keep the current task visible.")
            board.begin_task(root, agent["id"], "CURRENT")
            contract.create_contract(root, "CURRENT", "Keep the current task visible.", ["active task"])
            contract.create_contract(root, "DONE", "Preserve completed history.", ["history"])
            with board.locked_state(root) as state:
                state["task_chunks"]["DONE"] = {"history": {"status": "passed", "description": "history"}}
                state["archive"].append({"kind": "qa_request", "archived_at": "2026-08-10T12:20:00+00:00", "value": {
                    "id": "done-review-1", "task": "DONE", "status": "passed", "phase": "chunk",
                    "completed_at": "2026-08-10T12:00:00+00:00",
                }})
                state["archive"].append({"kind": "qa_request", "archived_at": "2026-08-10T12:20:00+00:00", "value": {
                    "id": "done-review-2", "task": "DONE", "status": "passed", "phase": "final_acceptance",
                    "completed_at": "2026-08-10T12:05:00+00:00",
                }})
                state["releases"]["DONE"] = {"task": "DONE", "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto", "recorded_at": "2026-08-10T12:10:00+00:00"}
                state["release_decisions"]["DONE"] = {"task": "DONE", "decision": "accepted", "recorded_at": "2026-08-10T12:15:00+00:00"}
                state["releases"]["OWNER_ACTION"] = {"task": "OWNER_ACTION", "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto", "recorded_at": "2026-08-11T12:00:00+00:00"}
            first = board_viewer.dashboard_payload(root)
            self.assertEqual(first["live_tasks"], ["CURRENT", "OWNER_ACTION"])
            first_history = board_viewer.history_payload(root)
            self.assertNotIn("task_history", first)
            self.assertEqual([item["task"] for item in first_history["task_history"]], ["DONE"])
            self.assertEqual(first_history["task_history"][0]["result"], "OWNER ACCEPTED")
            self.assertEqual(first_history["task_history"][0]["review_passes"], 2)
            release_bytes = json.dumps(first["state"]["releases"], sort_keys=True)
            cards = self.rendered_task_cards(first["state"], first["contracts"], first["owner_directions"], first["live_tasks"])
            self.assertEqual({card["task"] for card in cards}, {"CURRENT", "OWNER_ACTION"})
            self.assertIn("Accepted", next(card["html"] for card in cards if card["task"] == "OWNER_ACTION"))
            with board.locked_state(root) as state:
                state["agents"][agent["id"]].update({"active": False, "status": "done"})
                state["release_decisions"]["OWNER_ACTION"] = {"task": "OWNER_ACTION", "decision": "accepted", "recorded_at": "2026-08-11T12:05:00+00:00"}
            second = board_viewer.dashboard_payload(root)
            self.assertEqual(second["live_tasks"], [])
            self.assertEqual({item["task"] for item in board_viewer.history_payload(root)["task_history"]}, {"CURRENT", "DONE", "OWNER_ACTION"})
            self.assertEqual(second["state"]["releases"], {})

    def test_final_review_passed_stays_live_until_release_and_then_moves_to_history(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            board.record_owner_direction(root, session["id"], "Keep accepted work live through CTO release checks.")
            board.begin_task(root, agent["id"], "RELEASE-WINDOW")
            contract.create_contract(root, "RELEASE-WINDOW", "Keep accepted work live through CTO release checks.", ["truthful release window"])
            agreed_requirements(root, agent["id"], "Final agreed requirements: keep accepted work live through CTO release checks.")
            with board.locked_state(root) as state:
                state["agents"][agent["id"]].update({"active": False, "status": "done"})
                state["archive"].append({"kind": "qa_request", "archived_at": "2026-08-12T12:10:00+00:00", "value": {
                    "id": "release-window-final-1", "task": "RELEASE-WINDOW", "status": "passed", "phase": "final_acceptance",
                    "completed_at": "2026-08-12T12:10:00+00:00",
                }})

            pending = board_viewer.dashboard_payload(root)
            self.assertIn("RELEASE-WINDOW", pending["live_tasks"])
            self.assertNotIn("RELEASE-WINDOW", {item["task"] for item in board_viewer.history_payload(root)["task_history"]})
            card = next(item for item in self.rendered_task_cards(
                pending["state"], pending["contracts"], pending["owner_directions"], pending["live_tasks"], pending["requirement_confirmations"]
            ) if item["task"] == "RELEASE-WINDOW")
            self.assertIn("FINAL RELEASE CHECKS", card["html"])
            self.assertIn("CTO: verifying tested commit, push, clean main, and health", card["html"])

            with board.locked_state(root) as state:
                state["releases"]["RELEASE-WINDOW"] = {
                    "task": "RELEASE-WINDOW", "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto",
                    "recorded_at": "2026-08-12T12:15:00+00:00",
                }
                state["release_decisions"]["RELEASE-WINDOW"] = {
                    "task": "RELEASE-WINDOW", "decision": "accepted", "recorded_at": "2026-08-12T12:20:00+00:00",
                }
            released = board_viewer.dashboard_payload(root)
            self.assertNotIn("RELEASE-WINDOW", released["live_tasks"])
            history = next(item for item in board_viewer.history_payload(root)["task_history"] if item["task"] == "RELEASE-WINDOW")
            self.assertEqual(history["result"], "OWNER ACCEPTED")

    def test_history_renderer_groups_by_date_newest_first_and_is_collapsed_by_default(self):
        result = self.run_node(
            "const nodes={'history-list':{innerHTML:''},'history-count':{textContent:''},'history-search':{value:''},'history-search-status':{textContent:''}};"
            "globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;}};"
            "renderHistory([{task:'OLDER',completed_at:'2026-08-10T12:00:00Z',result:'COMPLETED',chunks_passed:1,chunks_total:1,review_passes:1,owner_direction:'Older task directive'},"
            "{task:'NEWER',completed_at:'2026-08-12T12:00:00Z',result:'OWNER ACCEPTED',chunks_passed:2,chunks_total:2,review_passes:4,owner_direction:'# Exact directive\\n- Keep <script>alert(1)</script> escaped\\n- Preserve **formatting**'}]);"
            "process.stdout.write(JSON.stringify({count:nodes['history-count'].textContent,status:nodes['history-search-status'].textContent,html:nodes['history-list'].innerHTML}));"
        )
        self.assertEqual(result["count"], "2 completed tasks")
        self.assertLess(result["html"].find("NEWER"), result["html"].find("OLDER"))
        self.assertEqual(result["html"].count('class="history-date-group"'), 2)
        self.assertNotIn('class="history-date-group" open', result["html"])
        self.assertEqual(result["status"], "2 tasks grouped by date")
        self.assertIn("2 focused changes completed", result["html"])
        self.assertIn("4 independent checks completed", result["html"])
        self.assertEqual(result["html"].count("Full user directive"), 2)
        self.assertIn("Older task directive", result["html"])
        self.assertIn("<h4>Exact directive</h4>", result["html"])
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", result["html"])
        self.assertNotIn("<script>alert(1)</script>", result["html"])
        self.assertIn("<strong>formatting</strong>", result["html"])
        self.assertIn('id="history"', board_viewer.PAGE)
        self.assertIn('id="history-search"', board_viewer.PAGE)
        self.assertNotIn('<details class="history-details" id="history" open>', board_viewer.PAGE)
        self.assertIn("height:150px;max-height:150px;overflow-y:scroll", board_viewer.PAGE)
        self.assertIn('class="history-list" id="history-list"', board_viewer.PAGE)
        self.assertIn("height:520px;max-height:60vh;overflow-y:scroll", board_viewer.PAGE)

    def test_test_ledger_renderer_uses_bullets_and_green_check_only_for_verified_pass(self):
        result = self.run_node(
            "process.stdout.write(JSON.stringify({html:testLedgerHtml({delivery:{scenarios:["
            "{what_was_tested:'Safe <refresh> stays readable for the owner.',status:'passed',label:'Passed'},"
            "{what_was_tested:'Still pending until the recorded check runs.',status:'pending',label:'Not tested yet'},"
            "{what_was_tested:'Rejected input remains visible as needing attention.',status:'failed',label:'Needs attention'}]},reviewer:{scenarios:[]}})}));"
        )
        self.assertIn('<section class="test-ledger"', result["html"])
        self.assertIn('<ul class="test-ledger-list">', result["html"])
        self.assertIn('class="test-ledger-check passed"', result["html"])
        self.assertIn('aria-label="Passed">☑', result["html"])
        self.assertIn('class="test-ledger-check pending"', result["html"])
        self.assertIn('aria-label="Not tested yet">☐', result["html"])
        self.assertIn('class="test-ledger-check failed"', result["html"])
        self.assertIn('&lt;refresh&gt;', result["html"])
        self.assertNotIn('<refresh>', result["html"])

    def test_delivery_status_dialog_has_its_own_test_ledger_section(self):
        result = self.run_node(
            "const nodes={'status-dialog-title':{textContent:''},'status-dialog-body':{innerHTML:''},'status-dialog':{showModal(){}}};"
            "globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;}};"
            "lastBoard={state:{agents:{dev:{id:'dev',role:'engineering',task:'LEDGER-TASK',status:'working',active:true}},delivery_plans:{'LEDGER-TASK':{mode:'atomic'}},qa_requests:{}},contracts:{'LEDGER-TASK':{status:'open'}},agent_checklists:{dev:{role:'delivery',state:'assigned',section:{scenarios:[{what_was_tested:'Live status remains readable for the owner.',status:'passed',label:'Passed'}]}}}};"
            "showAgentStatus('dev');"
            "process.stdout.write(JSON.stringify({title:nodes['status-dialog-title'].textContent,html:nodes['status-dialog-body'].innerHTML}));"
        )
        self.assertEqual(result["title"], "engineering status")
        self.assertIn("What Delivery tested", result["html"])
        self.assertIn("Live status remains readable for the owner.", result["html"])
        self.assertNotIn("S-001", result["html"])
        self.assertIn('aria-label="Passed">☑', result["html"])

    def test_waiting_delivery_status_keeps_response_guidance_and_test_ledger_together(self):
        result = self.run_node(
            "const nodes={'status-dialog-title':{textContent:''},'status-dialog-body':{innerHTML:''},'status-dialog':{showModal(){}}};"
            "globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;}};"
            "lastBoard={state:{agents:{dev:{id:'dev',role:'engineering',session_id:'session-1',task:'AWAITING_OWNER_DIRECTION',status:'working',active:true}},owner_directions:{'session-1':{text:'Complete direction'}},delivery_plans:{},qa_requests:{}},contracts:{},agent_checklists:{dev:{role:'delivery',state:'assigned',section:{scenarios:[{what_was_tested:'The response guidance remains beside the saved status.',status:'passed',label:'Passed'}]}}}};"
            "showAgentStatus('dev');"
            "process.stdout.write(JSON.stringify({html:nodes['status-dialog-body'].innerHTML}));"
        )
        self.assertIn("Use Respond to Delivery to approve the proposal or request changes.", result["html"])
        self.assertIn("What Delivery tested", result["html"])
        self.assertIn("The response guidance remains beside the saved status.", result["html"])
        self.assertIn('aria-label="Passed">☑', result["html"])

    def test_dashboard_projects_verified_ledger_and_preserves_it_in_history_from_certified_bytes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            board.record_owner_direction(root, session["id"], "Show the ledger in status and history.")
            board.begin_task(root, agent["id"], "LEDGER-VIEW")
            contract.create_contract(root, "LEDGER-VIEW", "Show the ledger in status and history.", ["ledger UI"])
            docs = root / "docs"
            docs.mkdir()
            ledger = docs / "LEDGER-VIEW-scenarios.md"
            ledger.write_text(
                "| ID | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
                "|---|---|---|---|---|---|\n"
                "| S-PASS | verified behavior | `python3 -m unittest test_pass` | behavior works | PASS: output captured | PASS |\n"
                "| S-SELF | self-reported only | `python3 -m unittest test_self` | no false green | PASS: text alone is insufficient | PASS |\n"
                "| S-FAIL | rejected behavior | `python3 -m unittest test_fail` | failure is visible | FAIL: simulated failure | FAIL |\n",
                encoding="utf-8",
            )
            evidence = root / ".harness" / "board" / "evidence" / "delivery.txt"
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text("scenario: S-PASS\nresult: PASS\n", encoding="utf-8")
            evidence_digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
            with board.locked_state(root) as state:
                state["qa_requests"]["review-ledger-view"] = {
                    "id": "review-ledger-view", "task": "LEDGER-VIEW", "cycle": 1,
                    "phase": "final_acceptance", "subtask": "", "chunk": "",
                    "developer_id": agent["id"], "claimed_by": None,
                    "review_wait_started_at": "2026-08-12T12:00:00+00:00",
                    "ledger": "docs/LEDGER-VIEW-scenarios.md",
                    "status": "open", "requested_at": "2026-08-12T12:00:00+00:00",
                    "delivery_simulations": {
                        "scenario_ids": ["S-PASS"], "executed_count": 1,
                        "approved_exception_ids": [], "evidence": str(evidence),
                        "evidence_sha256": evidence_digest,
                    },
                }
            live = board_viewer.dashboard_payload(root)
            scenarios = {item["id"]: item for item in live["test_ledgers"]["LEDGER-VIEW"]["scenarios"]}
            self.assertEqual(scenarios["S-PASS"]["status"], "passed")
            self.assertEqual(scenarios["S-SELF"]["status"], "pending")
            self.assertEqual(scenarios["S-FAIL"]["status"], "pending")

            certified = root / ".harness" / "board" / "certified" / "ledger-copy"
            certified.parent.mkdir(parents=True, exist_ok=True)
            certified.write_bytes(ledger.read_bytes())
            with board.locked_state(root) as state:
                state["qa_requests"]["review-ledger-view"]["certified_artifacts"] = {
                    "delivery_ledger": {"path": str(certified), "sha256": hashlib.sha256(certified.read_bytes()).hexdigest()}
                }
                state["qa_requests"]["review-ledger-view"].update({
                    "status": "passed", "completed_at": "2026-08-12T12:05:00+00:00"
                })
                state["agents"][agent["id"]].update({"active": False, "status": "done"})
                state["releases"]["LEDGER-VIEW"] = {
                    "task": "LEDGER-VIEW", "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto",
                    "recorded_at": "2026-08-12T12:10:00+00:00",
                }
                state["release_decisions"]["LEDGER-VIEW"] = {
                    "task": "LEDGER-VIEW", "decision": "accepted", "recorded_at": "2026-08-12T12:15:00+00:00",
                }
            ledger.unlink()
            history = board_viewer.history_payload(root)
            item = next(value for value in history["task_history"] if value["task"] == "LEDGER-VIEW")
            self.assertEqual(item["test_ledger"]["scenarios"], live["test_ledgers"]["LEDGER-VIEW"]["scenarios"])

            certified.write_text(certified.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8")
            recovered = board_viewer.history_payload(root)
            recovered_item = next(value for value in recovered["task_history"] if value["task"] == "LEDGER-VIEW")
            self.assertEqual(recovered_item["test_ledger"]["scenarios"], [])
            self.assertEqual(recovered_item["test_ledger"]["state"], "unavailable")

    def test_contract_valid_ledger_with_nonstandard_descriptive_columns_is_not_dropped(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "contract-schema-ledger.md"
            path.write_text(
                "| ID | AC | Dimension | Preconditions / data | Action or induced fault | Expected system response | Risk | Build owner | Simulation command | Observed system response | QA result |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| S-WIDE-001 | AC1 | Failure recovery | Temporary board | Corrupt the display source | The viewer degrades truthfully | Low | Delivery | `python3 -m unittest tests.test_board_viewer` | PASS: safe fallback rendered | PASS |\n",
                encoding="utf-8",
            )
            complete, problems = contract.scenario_ledger_complete(path)
            self.assertTrue(complete, problems)
            rows = board_viewer._scenario_rows_for_view(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "S-WIDE-001")
            self.assertEqual(rows[0]["scenario"], "Corrupt the display source")

    def test_missing_ledger_is_safe_and_history_search_includes_scenarios(self):
        empty = self.run_node("process.stdout.write(JSON.stringify({html:testLedgerHtml(null)}));")
        self.assertIn("Delivery has not submitted checks", empty["html"])
        unreadable = self.run_node("process.stdout.write(JSON.stringify({html:testLedgerHtml({delivery:{state:'unavailable',message:'The recorded checks are not available, so none are shown as passed.',scenarios:[]},reviewer:{state:'absent',scenarios:[]}})}));")
        self.assertIn("none are shown as passed", unreadable["html"])
        result = self.run_node(
            "const nodes={'history-list':{innerHTML:''},'history-count':{textContent:''},'history-search':{value:''},'history-search-status':{textContent:''}};"
            "globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;}};"
            "renderHistory([{task:'LEDGER-HISTORY',completed_at:'2026-08-12T12:00:00Z',result:'COMPLETED',owner_direction:'Ordinary directive',test_ledger:{delivery:{scenarios:[{what_was_tested:'Concurrent refresh recovery preserves the visible work.',status:'passed',label:'Passed'}]},reviewer:{scenarios:[]}}}],'refresh recovery');"
            "process.stdout.write(JSON.stringify({status:nodes['history-search-status'].textContent,html:nodes['history-list'].innerHTML}));"
        )
        self.assertIn("1 of 1 tasks match", result["status"])
        self.assertIn("Concurrent refresh recovery preserves the visible work.", result["html"])
        self.assertIn('aria-label="Passed">☑', result["html"])

    def test_history_search_filters_and_opens_the_matching_date_with_line_context(self):
        result = self.run_node(
            "const nodes={'history-list':{innerHTML:''},'history-count':{textContent:''},'history-search':{value:''},'history-search-status':{textContent:''}};"
            "globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;}};"
            "renderHistory([{task:'ALPHA',completed_at:'2026-08-10T12:00:00Z',result:'COMPLETED',owner_direction:'Ordinary directive'},"
            "{task:'BETA',completed_at:'2026-08-12T12:00:00Z',result:'OWNER ACCEPTED',owner_direction:'First line\\nFind this unique sentence <safely>\\nLast line'}],'UNIQUE SENTENCE');"
            "process.stdout.write(JSON.stringify({status:nodes['history-search-status'].textContent,html:nodes['history-list'].innerHTML}));"
        )
        self.assertEqual(result["status"], "1 of 2 tasks match “UNIQUE SENTENCE”")
        self.assertIn("BETA", result["html"])
        self.assertNotIn("ALPHA", result["html"])
        self.assertIn('data-history-date="2026-08-12" open', result["html"])
        self.assertIn("Matching line:", result["html"])
        self.assertIn("<mark>unique sentence</mark>", result["html"].lower())
        self.assertIn("&lt;safely&gt;", result["html"])

    def test_open_history_date_stays_open_when_the_viewer_refreshes(self):
        result = self.run_node(
            "const openGroup={dataset:{historyDate:'2026-08-12'}};"
            "const nodes={'history-list':{innerHTML:'',querySelectorAll(){return[openGroup];}},'history-count':{textContent:''},'history-search':{value:''},'history-search-status':{textContent:''}};"
            "globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;}};"
            "renderHistory([{task:'OPEN-TASK',completed_at:'2026-08-12T12:00:00Z',result:'COMPLETED',owner_direction:'Keep this date open'}]);"
            "process.stdout.write(JSON.stringify({html:nodes['history-list'].innerHTML}));"
        )
        self.assertIn('data-history-date="2026-08-12" open', result["html"])

    def test_completed_history_keeps_each_tasks_corresponding_exact_user_directive_after_restart(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = {
                "FIRST-HISTORY": "# First request\n- Keep the first directive exact",
                "SECOND-HISTORY": "Second request with <markup> and `code`",
            }
            for task, direction in expected.items():
                session = control.create(root, "codex_delivery")
                agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
                board.record_owner_direction(root, session["id"], direction)
                board.begin_task(root, agent["id"], task)
                contract.create_contract(root, task, direction, ["history directive"])
                agreed_requirements(root, agent["id"], f"Final agreed requirements for {task}: preserve the directive and archive the confirmed scope.")
                with board.locked_state(root) as state:
                    state["agents"][agent["id"]].update({"active": False, "status": "done"})
                    state["task_chunks"][task] = {"history": {"status": "passed", "description": "history directive"}}
            first = board_viewer.dashboard_payload(root)
            second = board_viewer.dashboard_payload(root)
            first_history = board_viewer.history_payload(root)
            second_history = board_viewer.history_payload(root)
            first_directions = {item["task"]: item["owner_direction"] for item in first_history["task_history"]}
            second_directions = {item["task"]: item["owner_direction"] for item in second_history["task_history"]}
            self.assertEqual(first_directions, expected)
            self.assertEqual(second_directions, expected)
            self.assertTrue(all(item["requirements_confirmation"]["text"].startswith("Final agreed requirements") for item in first_history["task_history"]))

    def test_completed_history_does_not_show_go_ahead_instead_of_original_direction(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            original = "Let every role choose Codex or Claude without changing governance."
            board.record_owner_message(root, agent["id"], original)
            # Reproduce the historical corrupt state created when the terminal
            # observer treated a later approval as a replacement direction.
            with board.locked_state(root) as state:
                state["owner_directions"][session["id"]].update({"text": "go ahead", "consumed": False})
            board.begin_task(root, agent["id"], "PROVIDER-HISTORY")
            contract.create_contract(root, "PROVIDER-HISTORY", original, ["history directive"])
            with board.locked_state(root) as state:
                state["agents"][agent["id"]].update({"active": False, "status": "done"})
                state["task_chunks"]["PROVIDER-HISTORY"] = {"history": {"status": "passed", "description": "history directive"}}
            item = next(value for value in board_viewer.history_payload(root)["task_history"] if value["task"] == "PROVIDER-HISTORY")
            self.assertEqual(item["owner_direction"], original)

    def test_completed_history_uses_contract_objective_for_legacy_task_without_direction_event(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract.create_contract(root, "LEGACY-HISTORY", "Legacy exact user directive.", ["history"])
            with board.locked_state(root) as state:
                state["task_chunks"]["LEGACY-HISTORY"] = {"history": {"status": "passed", "description": "history"}}
            data = board_viewer.history_payload(root)
            item = next(value for value in data["task_history"] if value["task"] == "LEGACY-HISTORY")
            self.assertEqual(item["owner_direction"], "Legacy exact user directive.")


if __name__ == "__main__":
    unittest.main()

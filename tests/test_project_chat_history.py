# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Project chat answers from the whole task history, in readable wording.

Owner, 2026-09-29: "ask about this project only answers things about the
current task ... like what have we solved as of now? did we upgrade this and
that?"  These tests seed a board with more history than the five-task detail
window and prove every task is answerable, the model's wording is shown only
when it adds no number, date or task of its own, and the rendered chat panel
shows a long history answer one task per line.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, browser_acceptance, contract, control, global_settings, project_chat, project_memory
from harness.project_context import ProjectContext
from tests.chat_key_support import configure_verified_key
from tests.environment_support import require_loopback
from tests.test_project_chat import FakeOpenAI
from tests.test_project_chat_ui import ServedChat

# (task, objective, deliverables, owner acceptance time) — acceptance order
# deliberately differs from creation order.
ACCEPTED = (
    ("brand-voice-upgrade", "Upgrade the Brand Voice agent to expert standard.", ["Brand voice method", "Voice regression proof"], "2026-09-03T09:00:00+00:00"),
    ("seo-specialist-upgrade", "Upgrade the SEO Specialist with evidence-linked keyword planning.", ["Keyword planning method"], "2026-09-11T09:00:00+00:00"),
    ("film-shot-quality", "Make generated film shots continuous and reviewable.", ["Shot planner", "Continuity check"], "2026-09-07T09:00:00+00:00"),
    ("quality-hold-recovery", "Make quality holds recoverable and transparent.", ["Hold recovery"], "2026-09-15T09:00:00+00:00"),
    ("graphic-designer-agent", "Create a Graphic Designer maker agent for finished static visuals.", ["Designer agent", "Visual system output", "Brand fit check", "Export formats", "Owner review screen", "Regression suite"], "2026-09-20T09:00:00+00:00"),
)
CLOSED = "content-planning-upgrade"
ACTIVE = "held-work-investigation"


def seed_history(root) -> None:
    """Seven tasks: five accepted, one closed without a release, one active."""
    for task, objective, deliverables, _accepted in ACCEPTED:
        contract.create_contract(root, task, objective, deliverables)
    contract.create_contract(root, CLOSED, "Upgrade Content Planning.", ["Planning method"])
    contract.create_contract(root, ACTIVE, "Always show the latest Studio output.", ["Latest output view", "Automatic repair"])
    with board.locked_state(root) as state:
        directions = state.setdefault("task_owner_directions", {})
        decisions = state.setdefault("release_decisions", {})
        for task, _objective, _deliverables, accepted in ACCEPTED:
            directions[task] = {"text": f"i am passing the directive again for {task} ..."}
            decisions[task] = {"task": task, "decision": "accepted", "recorded_at": accepted}
        directions[CLOSED] = {"text": "content planning directive"}
        state.setdefault("cancelled_tasks", {})[CLOSED] = {"task": CLOSED, "cancelled_at": "2026-09-12T09:00:00+00:00"}
        directions[ACTIVE] = {"text": "always show the latest output"}


class HistoryPackageTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        self.settings_home = Path(self._tmp.name) / "manager"
        global_settings.initialize(self.settings_home)
        project_memory.initialize(self.root, project_name="Studio", description="A marketing studio.")
        seed_history(self.root)

    def ask(self, question, provider):
        return project_chat.answer_question(
            self.root, question, settings_home=self.settings_home, provider=provider,
        )

    def test_every_recorded_task_has_a_history_fact_not_only_the_recent_five(self):
        facts = project_chat.build_fact_package(self.root, "", analyst=True)["facts"]
        tasks = [task for task, *_ in ACCEPTED] + [CLOSED, ACTIVE]
        self.assertGreater(len(tasks), project_chat.RECENT_TASK_LIMIT)
        for task in tasks:
            self.assertIn(f"task:{task}:history", facts, task)
        oldest = facts["task:brand-voice-upgrade:history"]["value"]
        self.assertIn("Brand voice upgrade (brand-voice-upgrade): accepted on Sep 3, 2026.", oldest)
        self.assertIn("Upgrade the Brand Voice agent to expert standard.", oldest, "contract objective, not the raw direction")
        self.assertNotIn("passing the directive", oldest)
        self.assertIn("Delivered: Brand voice method; Voice regression proof.", oldest)
        many = facts["task:graphic-designer-agent:history"]["value"]
        self.assertIn("Owner review screen; and 1 more.", many)
        self.assertIn("closed without a release", facts[f"task:{CLOSED}:history"]["value"])

    def test_completed_work_lists_every_accepted_task_newest_acceptance_first(self):
        value = project_chat.build_fact_package(self.root, "", analyst=True)["facts"]["project:completed_work"]["value"]
        lines = value.splitlines()
        self.assertEqual(lines[0], "5 of 7 recorded tasks are accepted (finished), newest first:")
        self.assertEqual(
            [line.split(" — ")[0] for line in lines[1:6]],
            ["• Graphic designer agent", "• Quality hold recovery", "• Seo specialist upgrade",
             "• Film shot quality", "• Brand voice upgrade"],
        )
        self.assertIn("accepted on Sep 20, 2026: Create a Graphic Designer maker agent", lines[1])
        self.assertEqual(lines[6], "Not finished yet: Held work investigation (active).")
        self.assertEqual(lines[7], "Closed without a release: Content planning upgrade.")
        self.assertEqual(len(lines), 8)

    def test_a_size_trim_drops_recent_detail_before_history_and_never_the_completed_list(self):
        full = project_chat.build_fact_package(self.root, "", analyst=True)
        full_bytes = len(json.dumps(full, sort_keys=True, separators=(",", ":")).encode())
        detail = [fact_id for fact_id in full["facts"] if fact_id.startswith("task:") and not fact_id.endswith(":history")]
        self.assertTrue(detail)
        with patch.object(project_chat, "MAX_PACKAGE_BYTES", full_bytes - 200):
            trimmed = project_chat.build_fact_package(self.root, "", analyst=True)["facts"]
        self.assertIn("package_truncated", trimmed)
        self.assertIn("project:completed_work", trimmed)
        for task, *_ in ACCEPTED:
            self.assertIn(f"task:{task}:history", trimmed, "history outlives recent detail")
        self.assertLess(sum(fact_id in trimmed for fact_id in detail), len(detail))

    def test_fixed_task_list_is_one_task_per_line_with_dates(self):
        result = self.ask("list all tasks", provider=lambda *_: self.fail("fixed questions never call the model"))
        lines = result["answer"].splitlines()
        self.assertEqual(lines[0], "7 tasks recorded (5 accepted, 1 active, 1 closed); unfinished work first, then finished work newest first:")
        self.assertEqual(lines[1], "• Held work investigation — active")
        self.assertEqual(lines[2], "• Graphic designer agent — accepted on Sep 20, 2026")
        self.assertEqual(lines[-1], "• Content planning upgrade — closed without a release")
        self.assertEqual(len(lines), 8)

    def test_a_closed_task_is_not_reported_as_remaining_work(self):
        result = self.ask("What is left?", provider=lambda *_: self.fail("fixed questions never call the model"))
        self.assertNotIn("Content planning", result["answer"])
        self.assertEqual(
            result["answer"],
            "• Held work investigation — in progress. Still to deliver: Latest output view; Automatic repair.",
        )

    def selector(self, answer, claims, **extra):
        return lambda *_: {"in_scope": True, "action_oriented": False, "claims": claims, "answer": answer, **extra}

    def test_grounded_model_wording_is_shown(self):
        wording = "Yes. The SEO Specialist was upgraded, and the owner accepted it on Sep 11, 2026."
        result = self.ask("did we upgrade the seo agent?", self.selector(wording, ["task:seo-specialist-upgrade:history"]))
        self.assertEqual(result["answer"], wording)
        self.assertTrue(result["composed"])
        self.assertEqual([claim["fact_id"] for claim in result["claims"]], ["task:seo-specialist-upgrade:history"])

    def test_an_invented_date_or_count_falls_back_to_the_verbatim_fact(self):
        fact = project_chat.build_fact_package(self.root, "", analyst=True)["facts"]["task:seo-specialist-upgrade:history"]["value"]
        for invented in (
            "Yes. It was accepted on Sep 12, 2026.",          # wrong day
            "Yes. It shipped 3 keyword methods.",              # invented count
        ):
            with self.subTest(invented=invented):
                result = self.ask("did we upgrade the seo agent?", self.selector(invented, ["task:seo-specialist-upgrade:history"]))
                self.assertEqual(result["answer"], fact)
                self.assertFalse(result["composed"])

    def test_review_r1_changed_month_or_borrowed_count_falls_back(self):
        """Codex r1: the digits of 'Sep 11, 2026' must not ground 'Oct 11, 2026' or '11 methods'."""
        fact = project_chat.build_fact_package(self.root, "", analyst=True)["facts"]["task:seo-specialist-upgrade:history"]["value"]
        for invented in (
            "Yes. The SEO Specialist was upgraded, and the owner accepted it on Oct 11, 2026.",
            "Yes. It shipped 11 keyword methods.",
        ):
            with self.subTest(invented=invented):
                result = self.ask("did we upgrade the seo agent?", self.selector(invented, ["task:seo-specialist-upgrade:history"]))
                self.assertEqual(result["answer"], fact)
                self.assertFalse(result["composed"])

    def test_dates_are_whole_claims_and_numbers_keep_their_word_in_every_form(self):
        package = project_chat.build_fact_package(self.root, "", analyst=True)
        seo = ["task:seo-specialist-upgrade:history"]
        completed = ["project:completed_work"]
        rejected = {
            "Accepted on October 11, 2026.": seo,          # month changed, full name
            "Accepted on 11 October 2026.": seo,           # day-first form
            "Accepted on 11th Oct.": seo,                  # ordinal, no year
            "Accepted on 2026-10-11.": seo,                # ISO form
            "Accepted in October 2026.": seo,              # month and year only
            "Accepted on Sep 11, 2025.": seo,              # year changed
            "Accepted on Sep 26, 2026.": seo,              # a date the cited fact lacks
            "It took 2026 hours.": seo,                    # a count borrowed from a year
            "5 of 5 tasks are finished.": completed,       # a count borrowed from another count
            "7 tasks are accepted.": completed,            # right number, wrong claim
            "It shipped eleven keyword methods.": seo,     # a spelled-out count
            "It was accepted yesterday.": seo,             # relative time
            "It was accepted two weeks ago.": seo,         # relative time with a spelled count
            "It was accepted last week.": seo,             # relative time
        }
        accepted = {
            "Yes, accepted on Sep 11, 2026.": seo,
            "Yes, accepted on September 11th, 2026.": seo,
            "Yes, accepted on 11 Sep 2026.": seo,
            "Yes, accepted on Sep 11.": seo,
            "Yes, accepted in September 2026.": seo,
            "5 of 7 recorded tasks are finished; the newest was accepted on Sep 20, 2026.": completed,
            "This was the last task accepted, on Sep 11, 2026.": seo,   # "this"/"last" as plain words
        }
        for wording, cited in rejected.items():
            with self.subTest(rejected=wording):
                self.assertFalse(project_chat.grounded_answer(wording, package, cited))
        for wording, cited in accepted.items():
            with self.subTest(accepted=wording):
                self.assertTrue(project_chat.grounded_answer(wording, package, cited))

    def test_naming_a_task_the_cited_facts_do_not_contain_falls_back(self):
        wording = "Yes. It came right after film-shot-quality."
        result = self.ask("did we upgrade the seo agent?", self.selector(wording, ["task:seo-specialist-upgrade:history"]))
        self.assertFalse(result["composed"])
        self.assertNotIn("film-shot-quality", result["answer"])

    def test_empty_wording_falls_back_and_stray_trailing_symbols_are_trimmed(self):
        empty = self.ask("did we upgrade the seo agent?", self.selector("", ["task:seo-specialist-upgrade:history"]))
        self.assertFalse(empty["composed"])
        self.assertTrue(empty["answer"].startswith("Seo specialist upgrade (seo-specialist-upgrade)"))
        junk = self.ask("did we upgrade the seo agent?", self.selector("Yes, on Sep 11, 2026.】【。", ["task:seo-specialist-upgrade:history"]))
        self.assertEqual(junk["answer"], "Yes, on Sep 11, 2026.")
        self.assertTrue(junk["composed"])

    def test_refusals_ignore_any_model_wording(self):
        for verdict in ({"in_scope": False, "action_oriented": False}, {"in_scope": True, "action_oriented": True}):
            with self.subTest(verdict=verdict):
                result = self.ask("what is the weather in Paris?", lambda *_: {
                    **verdict, "claims": [], "answer": "It is sunny in Paris.",
                })
                self.assertEqual(result["answer"], project_chat.REFUSAL_ANSWER)
                self.assertTrue(result["refused"])

    def test_openai_request_asks_for_wording_and_sets_no_output_token_cap(self):
        server = FakeOpenAI()
        self.addCleanup(server.close)
        environment = {
            "OPENAI_API_KEY": "sk-test-project-chat-1234567890",
            "HARNESS_OPENAI_TESTING": "1",
            "HARNESS_OPENAI_TEST_ENDPOINT": server.endpoint,
        }
        with patch.dict(os.environ, environment):
            result = project_chat.answer_question(self.root, "what have we solved so far?", settings_home=self.settings_home)
        payload = server.requests[-1]["payload"]
        self.assertNotIn("max_output_tokens", payload)
        schema = payload["text"]["format"]["schema"]
        self.assertIn("answer", schema["required"])
        self.assertIn("project:completed_work", schema["properties"]["claims"]["items"]["enum"])
        self.assertIn("EVERY task", payload["input"])
        # An older selector reply without wording still answers, verbatim.
        self.assertFalse(result["composed"])
        self.assertNotEqual(result["answer"], project_chat.UNKNOWN_ANSWER)


class HistoryAnswerRenderedTests(unittest.TestCase):
    """Real board page, real chat pipeline, stub model; headless Chrome reads it."""

    WORDING = (
        "You have finished 5 of 7 recorded tasks.\n"
        "• Graphic designer agent — accepted on Sep 20, 2026: a Graphic Designer maker agent for finished static visuals.\n"
        "• Quality hold recovery — accepted on Sep 15, 2026: quality holds became recoverable and transparent.\n"
        "• Seo specialist upgrade — accepted on Sep 11, 2026: evidence-linked keyword planning for the SEO Specialist.\n"
        "• Film shot quality — accepted on Sep 7, 2026: generated film shots became continuous and reviewable.\n"
        "• Brand voice upgrade — accepted on Sep 3, 2026: the Brand Voice agent reached expert standard.\n"
        "Held work investigation is not finished yet."
    )

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(self.context)
        project_memory.initialize(self.context, project_name="Studio", description="A marketing studio.")
        seed_history(self.context)
        self.settings_home = base / "manager"; global_settings.initialize(self.settings_home)
        configure_verified_key(self.settings_home)

        def model(_question, package):
            self.assertIn("project:completed_work", package["facts"])
            return {"in_scope": True, "action_oriented": False, "claims": ["project:completed_work"], "answer": self.WORDING}

        def answerer(root, question, **kwargs):
            return project_chat.answer_question(
                root, question, settings_home=self.settings_home, provider=model,
                cancel_event=kwargs.get("cancel_event"),
            )

        self.chat = ServedChat(self.context, self.settings_home, answerer, token="history-token")
        self.addCleanup(self.chat.close)

    def render(self, width, height):
        sink = {}
        origin = self.chat.origin
        script = r"""
<script>
(async()=>{
  const pause=delay=>new Promise(resolve=>setTimeout(resolve,delay));
  for(let attempt=0;attempt<80&&!document.querySelector('#project-chat');attempt++)await pause(50);
  const input=document.querySelector('#project-chat-input'),placeholder=input.placeholder,hint=document.querySelector('.chat-empty')?.textContent||'';
  input.value='what have we solved as of now?';
  document.querySelector('#project-chat-form').requestSubmit();
  for(let attempt=0;attempt<200&&!document.querySelector('.chat-answer');attempt++)await pause(50);
  const answer=document.querySelector('.chat-answer'),history=document.querySelector('#project-chat-history'),doc=document.scrollingElement;
  const line=parseFloat(getComputedStyle(answer).lineHeight)||18;
  await fetch('/__layout_result__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    text:answer.innerText,answerHeight:answer.getBoundingClientRect().height,line,
    historyHeight:history.clientHeight,historyMax:parseFloat(getComputedStyle(history).maxHeight),historyScroll:history.scrollHeight,historyScrollTop:history.scrollTop,
    answerRight:answer.getBoundingClientRect().right,historyRight:history.getBoundingClientRect().right,
    scrollWidth:doc.scrollWidth,clientWidth:doc.clientWidth,
    status:document.querySelector('#project-chat-status').textContent,placeholder,hint})});
})();
</script>
"""

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *_): return
            def reply(self, status, body, content_type="application/json"):
                self.send_response(status); self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                try:
                    with urlopen(origin + self.path, timeout=10) as opened:
                        body = opened.read(); content_type = opened.headers.get("Content-Type", "application/json")
                except HTTPError as error:
                    self.reply(error.code, error.read(), error.headers.get("Content-Type", "application/json")); return
                if self.path == "/":
                    body = body.decode().replace("</body>", script + "</body>", 1).encode()
                self.reply(200, body, content_type)
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0")); body = self.rfile.read(length)
                if self.path == "/__layout_result__":
                    sink["value"] = json.loads(body); self.reply(200, b"{}"); return
                request = Request(origin + self.path, data=body, method="POST", headers={
                    "Content-Type": self.headers.get("Content-Type", "application/json"),
                    "X-Harness-Chat-Action": self.headers.get("X-Harness-Chat-Action", ""),
                    "Origin": origin, "Sec-Fetch-Site": "same-origin",
                })
                try:
                    with urlopen(request, timeout=10) as opened:
                        self.reply(opened.status, opened.read(), opened.headers.get("Content-Type", "application/json"))
                except HTTPError as error:
                    self.reply(error.code, error.read())

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True); thread.start()
        profile = tempfile.TemporaryDirectory()
        process = browser_acceptance.launch(
            f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=width, height=height,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.05)
            self.assertIn("value", sink, "Chrome did not report the history answer")
            return sink["value"]
        finally:
            process.close()
            profile.cleanup(); proxy.shutdown(); thread.join(timeout=3); proxy.server_close()

    def test_history_answer_renders_one_task_per_line_and_stays_readable(self):
        for width, height in ((1280, 900), (390, 760)):
            with self.subTest(viewport=(width, height)):
                value = self.render(width, height)
                self.assertEqual(value["placeholder"], "What have we finished so far?")
                self.assertIn("what has been finished so far", value["hint"])
                lines = [line for line in value["text"].splitlines() if line.strip()]
                self.assertEqual(lines[0], "Project assistant")
                self.assertEqual(lines[1], "You have finished 5 of 7 recorded tasks.")
                bullets = [line for line in lines if line.startswith("• ")]
                self.assertEqual(len(bullets), 5, value["text"])
                self.assertTrue(bullets[0].startswith("• Graphic designer agent — accepted on Sep 20, 2026"))
                self.assertIn("Answered from", value["status"])
                # Readable: no sideways overflow, answer inside its panel, and
                # the panel scrolls to the newest answer when it is taller.
                self.assertLessEqual(value["scrollWidth"], value["clientWidth"])
                self.assertLessEqual(value["answerRight"], value["historyRight"] + 1)
                self.assertGreaterEqual(value["answerHeight"], 7 * value["line"])
                if value["historyScroll"] > value["historyHeight"]:
                    self.assertGreater(value["historyScrollTop"], 0)
                if width >= 1000:
                    self.assertGreaterEqual(value["historyMax"], 400, "desktop panel shows a long answer without cramping")


if __name__ == "__main__":
    unittest.main()

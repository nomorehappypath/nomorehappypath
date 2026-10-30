# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner's document-only checkbox (D-014, task 2026-10-27_document-only-checkbox).

Unticked (the default) is the full process; ticked is the document path. The builder no longer chooses `--no-program`.
Run:  PYTHONPATH=. python3 -m unittest tests.test_document_only_checkbox -v
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import board, board_viewer, browser_acceptance, contract_check, control, standing_checks
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_contract_first_checks import TASK, TEXT, _Fixture, write_json
from tests.test_requirements_contract import context


def delivery_with_flag(root, document_only=None, task=TASK):
    """A Delivery Agent whose first direction came through the board's owner message, as the new-task screen sends it."""
    session = control.create(root, "codex_delivery")
    agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
    if document_only is None:
        board.record_owner_message(root, agent["id"], f"OWNER DIRECTION - {task}: write the thing.", "direction", [])
    else:
        board.record_owner_message(root, agent["id"], f"OWNER DIRECTION - {task}: write the thing.", "direction", [], document_only=document_only)
    board.begin_task(root, agent["id"], task)
    agent["session_id"] = session["id"]
    return agent


class FlagReachesTheTaskTests(_Fixture):
    def flag(self):
        return board.task_document_only(board.snapshot(self.root), TASK)

    def test_the_default_is_the_full_process(self):
        delivery_with_flag(self.root)
        self.assertIs(self.flag(), False)

    def test_an_unchecked_box_sent_explicitly_is_the_full_process(self):
        delivery_with_flag(self.root, False)
        self.assertIs(self.flag(), False)

    def test_the_ticked_box_reaches_the_task(self):
        delivery_with_flag(self.root, True)
        self.assertIs(self.flag(), True)

    def test_a_task_with_no_record_runs_as_the_full_process(self):
        self.assertIs(board.task_document_only({}, "ANY"), False)


class NoProgramIsTheOwnersChoiceTests(_Fixture):
    def test_no_program_is_refused_when_the_box_is_off_and_names_the_checkbox(self):
        agent = delivery_with_flag(self.root, False)
        with self.assertRaises(ValueError) as raised:
            board.record_requirement_proposal(self.root, agent["id"], TEXT, no_program=True)
        self.assertIn("This task produces a document only", str(raised.exception))
        self.assertNotIn(TASK, board.snapshot(self.root).get("requirement_proposals", {}))

    def test_the_old_default_task_is_refused_too(self):
        agent = delivery_with_flag(self.root)
        with self.assertRaisesRegex(ValueError, "owner did not tick"):
            board.record_requirement_proposal(self.root, agent["id"], TEXT, no_program=True)

    def test_no_program_is_accepted_when_the_box_is_ticked(self):
        agent = delivery_with_flag(self.root, True)
        board.record_requirement_proposal(self.root, agent["id"], TEXT, no_program=True)
        proposal = self.proposal()
        self.assertTrue(proposal["no_program"] and proposal["document_only"])
        self.assertIn("not a program", proposal["contract_check"]["reason"])

    def test_the_ticked_box_applies_the_document_path_without_the_builder_asking(self):
        agent = delivery_with_flag(self.root, True)
        self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        proposal = self.proposal()
        self.assertTrue(proposal["no_program"] and proposal["document_only"])
        self.assertEqual(proposal["contract_check"]["status"], "not_checked")
        self.assertIsNone(proposal["contract_check"]["reviewer_id"] or None)

    def test_the_unticked_box_gets_the_reviewers_early_look(self):
        agent = delivery_with_flag(self.root, False)
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        proposal = self.proposal()
        self.assertFalse(proposal["no_program"] or proposal["document_only"])
        self.assertEqual(proposal["contract_check"]["status"], "reading")
        self.assertEqual(proposal["contract_check"]["reviewer_id"], reviewer["id"])

    def test_the_command_line_help_says_it_is_the_owners_choice(self):
        source = Path(board.__file__).read_text(encoding="utf-8")
        line = next(item for item in source.splitlines() if 'add_parser("propose-requirements")' in item)
        self.assertIn("REFUSED unless the owner ticked", line)


class EveryCriterionNeedsACheckOrAReasonTests(_Fixture):
    CRITERIA = [
        {"text": "The report lists every problem found", "plain": "You get the whole list.", "why_not_checked": "the content is judged by reading"},
        {"text": "Each problem names the file it is in", "plain": "You can find each problem.", "checks": []},
        {"text": "The report says what was not examined", "plain": "You know what the report did not cover.", "why_not_checked": "a statement of scope"},
    ]

    def test_a_criterion_with_neither_a_check_nor_a_reason_is_refused(self):
        with self.assertRaisesRegex(ValueError, "criterion 2 needs a check or the reason it has none"):
            contract_check.parse_submission(json.dumps({"criteria": self.CRITERIA}))

    def test_with_a_reason_for_each_the_labels_carry_the_reasons(self):
        criteria = [dict(item) for item in self.CRITERIA]
        criteria[1]["why_not_checked"] = "it is read in the report"
        parsed = contract_check.parse_submission(json.dumps({"criteria": criteria}))
        labels = contract_check.labels(parsed["criteria"])
        self.assertEqual([item["label"] for item in labels], ["not checked in advance"] * 3)
        self.assertEqual(labels[1]["reason"], "it is read in the report")

    def test_the_stored_proposal_keeps_the_reason_per_criterion(self):
        agent = delivery_with_flag(self.root, False)
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        criteria = [dict(item, why_not_checked=item.get("why_not_checked") or "read, not run") for item in self.CRITERIA]
        self.submit(reviewer, {"criteria": criteria, "questions": []})
        stored = self.proposal()["contract_check"]["criteria"]
        self.assertEqual([item["why_not_checked"] for item in stored][1], "read, not run")
        self.assertEqual(len(stored), 3)


class ReportFilesGateTests(unittest.TestCase):
    REPORT = "FINDINGS.md: a list of the problems found"
    TESTS = "Automated tests that prove each finding"

    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"HARNESS_STANDING_CHECKS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def problem(self, proposal_no_program, names, files):
        state = {"requirement_proposals": {"T": {"no_program": proposal_no_program}}}
        with mock.patch.object(board, "_contract_deliverable_names", return_value=names), \
                mock.patch.object(board, "_owner_documents_now", return_value=[]):
            return board._extra_project_files_problem(Path("/x"), state, "T", files, Path("/x"))

    def test_the_full_process_analysis_may_add_the_tests_the_contract_names(self):
        self.assertEqual(self.problem(False, [self.REPORT, self.TESTS], ["FINDINGS.md", "tests/test_findings.py", "test_more.py"]), "")

    def test_the_full_process_analysis_still_refuses_unrelated_files(self):
        message = self.problem(False, [self.REPORT, self.TESTS], ["FINDINGS.md", "tests/test_findings.py", "tools/check_all.sh", "src/app.py"])
        self.assertIn("tools/check_all.sh", message)
        self.assertIn("src/app.py", message)
        self.assertNotIn("test_findings", message)

    def test_a_build_is_never_limited(self):
        self.assertEqual(self.problem(False, ["README.md: how to run it", "A working app"], ["src/app.py", "README.md"]), "")

    def test_a_document_only_task_still_refuses_even_tests(self):
        message = self.problem(True, [self.REPORT], ["FINDINGS.md", "tests/test_findings.py"])
        self.assertIn("tests/test_findings.py", message)

    def test_a_document_only_task_may_hold_just_the_document(self):
        self.assertEqual(self.problem(True, [self.REPORT], ["FINDINGS.md"]), "")


class BoxIsSeenFirstTests(unittest.TestCase):
    def test_the_choice_sits_at_the_top_of_the_launch_screen_above_provider_and_model(self):
        """2026-10-28: the owner chose the kind of work on the screen that starts the Delivery agent, above model and effort."""
        page = board_viewer.PAGE if hasattr(board_viewer, "PAGE") else (Path(board_viewer.__file__)).read_text(encoding="utf-8")
        self.assertLess(page.index('id="launch-task-kind"'), page.index('id="launch-provider"'))
        self.assertNotIn("<fieldset id=\"owner-message-task-kind\"", page, "the old choice in the Give direction box was cancelled")


class DirectiveTextTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def read(self, name):
        return " ".join((self.ROOT / name).read_text(encoding="utf-8").split())

    def test_the_builder_is_no_longer_told_to_choose_no_program(self):
        agent = self.read("directives/AGENT.md")
        chapter = agent[agent.index("## contract-first"):agent.index("## ", agent.index("## contract-first") + 5)]
        self.assertNotIn("File the proposal with `--no-program` when the task makes documents only", chapter)
        self.assertIn("never your call", chapter)
        self.assertIn("refuses `--no-program` unless the owner chose Spec", chapter)
        self.assertIn("small-change lane below is separate and unchanged", chapter)
        card = self.read("directives/cards/DELIVERY.md")
        self.assertIn("Never choose `--no-program`: the owner's task kind decides", card)
        self.assertNotIn("File the proposal with `--no-program`", card)

    def test_report_shortcuts_belong_to_the_ticked_box_only(self):
        agent = self.read("directives/AGENT.md")
        self.assertIn("**Spec tasks** (the owner chose", agent)
        self.assertIn("a Code analysis task is not a spec", agent)

    def test_the_brief_tells_the_reviewer_which_process_applies(self):
        from harness import review_brief
        source = (self.ROOT / "harness" / "review_brief.py").read_text(encoding="utf-8")
        self.assertIn('"process"', source)
        self.assertTrue(hasattr(review_brief, "build"))


class OpenedByTheApiTests(unittest.TestCase):
    """The private test runner opens tasks through the owner-message API without the field: that must stay the full process."""

    def test_the_review_driver_sends_no_document_only_field(self):
        source = (Path(__file__).resolve().parents[1] / "scripts" / "review_driver.py").read_text(encoding="utf-8")
        self.assertNotIn("document_only", source)

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / "evals" / "golden" / "run_eval.py").is_file(), "golden eval set: only where evals/golden ships")
    def test_the_golden_runner_gives_no_direction_of_its_own(self):
        source = (Path(__file__).resolve().parents[1] / "evals" / "golden" / "run_eval.py").read_text(encoding="utf-8")
        self.assertNotIn("document_only", source)

    def test_the_api_without_the_field_leaves_the_box_unchecked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            require_loopback()
            import urllib.request
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/agents/{agent['id']}/owner-message",
                data=json.dumps({"message_type": "direction", "text": "Write a spec for the thing."}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=20) as reply:
                self.assertEqual(reply.status, 201)
            self.assertIs(board.snapshot(root)["owner_directions"][session["id"]]["document_only"], False)
            # and a client that sends true gets the document path
            session2 = control.create(root, "codex_delivery")
            agent2 = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session2["id"])
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/agents/{agent2['id']}/owner-message",
                data=json.dumps({"message_type": "direction", "text": "Write another spec.", "document_only": True}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=20):
                pass
            self.assertIs(board.snapshot(root)["owner_directions"][session2["id"]]["document_only"], True)


class MultipartPathTests(unittest.TestCase):
    """The dialog sends FormData to an already waiting Delivery: the box must survive the multipart parser (review round 1)."""

    def setUp(self):
        require_loopback()

    def send(self, root, agent, fields):
        import urllib.request
        boundary = "XBOUNDARYX"
        body = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items()) + f"--{boundary}--\r\n"
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/agents/{agent['id']}/owner-message", data=body.encode(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        with urllib.request.urlopen(request, timeout=20) as reply:
            self.assertEqual(reply.status, 201)

    def run_case(self, fields, expected):
        require_loopback()
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            self.send(root, agent, {"message_type": "direction", "text": "Write a spec for the thing.", **fields})
            self.assertIs(board.snapshot(root)["owner_directions"][session["id"]]["document_only"], expected)
            board.begin_task(root, agent["id"], TASK)
            self.assertIs(board.task_document_only(board.snapshot(root), TASK), expected)
            board.record_requirement_proposal(root, agent["id"], TEXT)
            self.assertIs(board.snapshot(root)["requirement_proposals"][TASK]["document_only"], expected)

    def test_a_ticked_box_in_form_data_reaches_the_task_and_the_proposal(self):
        self.run_case({"document_only": "1"}, True)

    def test_an_absent_field_in_form_data_is_the_full_process(self):
        self.run_case({}, False)


NEW_TASK_PROBE = r"""
<script>
(async () => {
  const wait = async (test, tries = 300) => { for (let i = 0; i < tries && !test(); i++) await new Promise(r => setTimeout(r, 100)); return test(); };
  const q = s => document.querySelector(s);
  const result = {};
  try {
    await wait(() => q('#codex') && !q('#codex').disabled);
    q('#codex').click();
    await wait(() => q('#color-dialog').open && !q('#color-launch').disabled);
    const group = q('#launch-task-kind');
    result.visible = Boolean(group) && group.getBoundingClientRect().width > 0;
    result.checkedByDefault = group.querySelector('input:checked').value;
    result.label = group.textContent.replace(/\s+/g, ' ').trim();
    result.aboveProvider = group.getBoundingClientRect().bottom <= q('#launch-provider').getBoundingClientRect().top;
    group.querySelector('input[value="' + window.__kind + '"]').click();
    result.ticked = group.querySelector('input:checked').value;
    q('#color-launch').click();
    await wait(() => !q('#color-dialog').open);
    result.stored = window.localStorage.getItem('task-kind:');
    // the direction box states the kind that was chosen at launch and sends it with the words
    openDirectionStart(true);
    await wait(() => q('#owner-message-dialog').open);
    result.note = q('#owner-message-task-kind').textContent;
    result.oldChoiceGone = !q('#owner-message-task-kind input');
    q('#owner-message-text').value = 'Write me a one page spec for the thing.';
    q('#owner-message-text').dispatchEvent(new Event('input'));
    q('#owner-message-submit').click();
    await wait(() => !q('#owner-message-dialog').open);
    result.pending = window.localStorage.getItem('pending-direction:');
  } catch (error) { result.error = String(error && error.stack || error); }
  result.phase = 'done';
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(result)});
})();
</script>
"""

DIRECTION_BOX_PROBE = r"""
<script>
(async () => {
  const wait = async (test, tries = 300) => { for (let i = 0; i < tries && !test(); i++) await new Promise(r => setTimeout(r, 100)); return test(); };
  const q = s => document.querySelector(s);
  const result = {};
  try {
    await wait(() => q('#give-direction') && !q('#give-direction').disabled);
    if (window.__preset) window.localStorage.setItem('task-kind:', window.__preset);
    openDirectionStart(true);
    await wait(() => q('#owner-message-dialog').open);
    const choice = q('#owner-message-kind-choice');
    result.shown = !choice.hidden && choice.getBoundingClientRect().width > 0;
    result.label = choice.textContent.replace(/\s+/g, ' ').trim();
    result.aboveMessage = choice.getBoundingClientRect().bottom <= q('#owner-message-text').getBoundingClientRect().top;
    result.note = q('#owner-message-task-kind').hidden ? '' : q('#owner-message-task-kind').textContent;
    if (result.shown) {
      result.checkedByDefault = choice.querySelector('input:checked').value;
      choice.querySelector('input[value="analysis"]').click();
    }
    q('#owner-message-text').value = 'Look through all the code and report problems.';
    q('#owner-message-text').dispatchEvent(new Event('input'));
    q('#owner-message-submit').click();
    await wait(() => !q('#owner-message-dialog').open);
    result.pending = window.localStorage.getItem('pending-direction:');
  } catch (error) { result.error = String(error && error.stack || error); }
  result.phase = 'done';
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(result)});
})();
</script>
"""

GIVE_DIRECTION_FIRST_PROBE = r"""
<script>
(async () => {
  const wait = async (test, tries = 300) => { for (let i = 0; i < tries && !test(); i++) await new Promise(r => setTimeout(r, 100)); return test(); };
  const q = s => document.querySelector(s);
  const result = {launches: []};
  const realFetch = window.fetch;
  window.fetch = (url, options) => {
    if (String(url).includes('/api/sessions') && options && options.method === 'POST') result.launches.push(JSON.parse(options.body));
    return realFetch(url, options);
  };
  try {
    await wait(() => q('#give-direction') && !q('#give-direction').disabled && lastBoard);
    result.boardLoaded = Boolean(lastBoard);
    if (window.__mode === 'loading') {
      lastBoard = null;   // the page's first answer from the project has not arrived
    }
    if (window.__mode === 'running') {
      lastBoard.state.agents = {d: {id: 'd', active: true, role: 'development', task: 'AWAITING_OWNER_DIRECTION', session_id: 's'}};
    }
    q('#give-direction').click();
    await new Promise(r => setTimeout(r, 1500));
    result.startOpen = q('#color-dialog').open;
    result.boxOpen = q('#owner-message-dialog').open;
    if (result.startOpen) {
      await wait(() => !q('#launch-model-select').disabled);
      result.title = q('#color-dialog-title').textContent;
      result.kindFirst = q('#launch-task-kind').getBoundingClientRect().bottom <= q('#launch-provider').getBoundingClientRect().top;
      result.hasModel = !q('#launch-model-select').disabled && q('#launch-model-select').options.length > 0;
      result.hasEffort = !q('#launch-effort').disabled && q('#launch-effort').options.length > 0;
    }
    if (window.__mode === 'close') {
      q('#color-close').click();
      await new Promise(r => setTimeout(r, 1000));
    } else if (window.__mode === 'launch') {
      q('#launch-task-kind input[value="analysis"]').click();
      result.effortPicked = q('#launch-effort').value;
      result.modelPicked = q('#launch-model-select').value;
      q('#color-launch').click();
      await wait(() => q('#owner-message-dialog').open);
      result.boxOpenAfterLaunch = q('#owner-message-dialog').open;
      result.kindChoiceHidden = q('#owner-message-kind-choice').hidden;
      result.note = q('#owner-message-task-kind').hidden ? '' : q('#owner-message-task-kind').textContent;
      q('#owner-message-text').value = 'Look through all the code and report problems.';
      q('#owner-message-text').dispatchEvent(new Event('input'));
      q('#owner-message-submit').click();
      await wait(() => !q('#owner-message-dialog').open);
      result.pending = window.localStorage.getItem('pending-direction:');
    }
    result.notice = q('#notice').textContent;
    result.startOpenAtEnd = q('#color-dialog').open;
    result.boxOpenAtEnd = q('#owner-message-dialog').open;
  } catch (error) { result.error = String(error && error.stack || error); }
  result.phase = 'done';
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(result)});
})();
</script>
"""


CARD_PROBE = r"""
<script>
(async () => {
  const wait = async (test, tries = 300) => { for (let i = 0; i < tries && !test(); i++) await new Promise(r => setTimeout(r, 100)); return test(); };
  const q = s => document.querySelector(s);
  const result = {};
  try {
    await wait(() => q('.requirements-proposal'));
    const card = q('.requirements-proposal');
    result.running = (card.querySelector('[data-running-as]') || {}).textContent || '';
    result.runningKind = (card.querySelector('[data-running-as]') || {dataset: {}}).dataset.runningAs || '';
    result.count = (card.querySelector('[data-no-test-count]') || {}).textContent || '';
    result.reasons = Array.from(card.querySelectorAll('[data-no-test-reason]')).map(n => n.textContent);
  } catch (error) { result.error = String(error && error.stack || error); }
  result.phase = 'done';
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(result)});
})();
</script>
"""


class RenderedTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()

    def page(self, root, script, **handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root, **handler))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, script))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=1000)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and (sink.get("value") or {}).get("phase") != "done":
                time.sleep(0.1)
        finally:
            process.close()
        return dict(sink.get("value") or {})

    def launch_screen(self, kind):
        from harness import global_settings, project_memory
        root = self.base / f"project{kind}"
        root.mkdir()
        project_memory.initialize(root, project_name="Document only", description="Facts.")
        home = self.base / f"home{kind}"
        global_settings.update_agent_settings(home, {
            "delivery": {"provider": "codex", "model": "gpt-6-sol", "effort": "high"},
            "cto": {"provider": "claude", "model": "opus", "effort": "high"},
            "reviewer": {"provider": "claude", "model": "opus", "effort": "max"},
        })
        with mock.patch("harness.board_viewer.launch_terminal", return_value=None):
            return self.page(root, NEW_TASK_PROBE.replace("window.__kind", repr(kind)),
                             project_name="Document only", manager_url="http://127.0.0.1:1/", settings_home=home,
                             project_id="docs", chat_action_token="owner-token")

    def test_the_launch_screen_offers_the_three_kinds_in_plain_words_above_the_model(self):
        seen = self.launch_screen("development")
        self.assertNotIn("error", seen, json.dumps(seen))
        self.assertTrue(seen["visible"], "the choice must be drawn")
        self.assertTrue(seen["aboveProvider"], "the choice sits above Provider, Model and effort")
        self.assertEqual(seen["checkedByDefault"], "development")
        for words in ("What kind of work is this?", "Write code", "Analyze code", "Write specs",
                      "Changes no code; the reviewer checks every finding.", "No code to build or examine."):
            self.assertIn(words, seen["label"])
        self.assertTrue(seen["oldChoiceGone"], "the Give direction box no longer carries the choice")

    def test_the_kind_chosen_at_launch_is_what_the_first_direction_carries(self):
        for kind, words in (("development", "Write code"), ("analysis", "Analyze code"), ("spec", "Write specs")):
            with self.subTest(kind=kind):
                seen = self.launch_screen(kind)
                self.assertNotIn("error", seen, json.dumps(seen))
                self.assertEqual(seen["ticked"], kind)
                self.assertEqual(seen["stored"], kind, "kept in this browser, so a reload cannot change it")
                self.assertIn(words, seen["note"])
                self.assertEqual(json.loads(seen["pending"])["taskKind"], kind)

    def give_direction_first(self, mode):
        from harness import global_settings, project_memory
        root = self.base / f"first{mode}"
        root.mkdir()
        project_memory.initialize(root, project_name="Document only", description="Facts.")
        home = self.base / f"homefirst{mode}"
        global_settings.update_agent_settings(home, {
            "delivery": {"provider": "codex", "model": "gpt-6-sol", "effort": "high"},
            "cto": {"provider": "claude", "model": "opus", "effort": "high"},
            "reviewer": {"provider": "claude", "model": "opus", "effort": "max"},
        })
        with mock.patch("harness.board_viewer.launch_terminal", return_value=None):
            return self.page(root, GIVE_DIRECTION_FIRST_PROBE.replace("window.__mode", repr(mode)),
                             project_name="Document only", manager_url="http://127.0.0.1:1/", settings_home=home,
                             project_id="docs", chat_action_token="owner-token")

    def test_give_direction_with_no_delivery_running_asks_the_kind_model_and_effort_first(self):
        seen = self.give_direction_first("launch")
        self.assertNotIn("error", seen, json.dumps(seen))
        self.assertTrue(seen["startOpen"], "the Start dialog comes first")
        self.assertFalse(seen["boxOpen"], "the direction box waits until Delivery is launched")
        self.assertEqual(seen["title"], "Start a Delivery Agent")
        self.assertTrue(seen["kindFirst"] and seen["hasModel"] and seen["hasEffort"])
        delivery = [launch for launch in seen["launches"] if launch.get("kind") == "codex_delivery"]
        self.assertEqual(len(delivery), 1, "Delivery is launched once, from the dialog")
        settings = delivery[0]["launch_settings"]
        self.assertEqual((settings["model"], settings["effort"]), (seen["modelPicked"], seen["effortPicked"]),
                         "the model and effort the owner saw are what the launch request carries")
        self.assertTrue(seen["boxOpenAfterLaunch"], "after launching, the direction box opens")
        self.assertTrue(seen["kindChoiceHidden"], "the kind was already picked: it is not asked twice")
        self.assertIn("Analyze code", seen["note"])
        self.assertEqual(json.loads(seen["pending"])["taskKind"], "analysis")

    def test_closing_the_start_dialog_starts_nothing_and_opens_no_direction_box(self):
        seen = self.give_direction_first("close")
        self.assertNotIn("error", seen, json.dumps(seen))
        self.assertTrue(seen["startOpen"])
        self.assertEqual(seen["launches"], [])
        self.assertFalse(seen["startOpenAtEnd"] or seen["boxOpenAtEnd"])

    def test_give_direction_before_the_project_has_answered_neither_starts_nor_opens_the_box(self):
        seen = self.give_direction_first("loading")
        self.assertNotIn("error", seen, json.dumps(seen))
        self.assertFalse(seen["startOpen"] or seen["boxOpen"] or seen["startOpenAtEnd"] or seen["boxOpenAtEnd"],
                         "while the page does not yet know whether Delivery runs, it must not guess")
        self.assertEqual(seen["launches"], [], "nothing can start with unseen defaults")
        self.assertIn("still loading", seen["notice"])

    def test_with_delivery_already_running_the_direction_box_opens_directly(self):
        seen = self.give_direction_first("running")
        self.assertNotIn("error", seen, json.dumps(seen))
        self.assertFalse(seen["startOpen"], "no second Delivery is offered")
        self.assertTrue(seen["boxOpen"])
        self.assertEqual(seen["launches"], [])

    def direction_box(self, preset=""):
        from harness import project_memory
        root = self.base / f"direction{preset or 'none'}"
        root.mkdir()
        project_memory.initialize(root, project_name="Document only", description="Facts.")
        with mock.patch("harness.board_viewer.launch_terminal", return_value=None):
            return self.page(root, DIRECTION_BOX_PROBE.replace("window.__preset", repr(preset)),
                             project_name="Document only", manager_url="http://127.0.0.1:1/",
                             settings_home=self.base / "home-direction", project_id="docs", chat_action_token="owner-token")

    def test_the_give_direction_box_offers_the_three_kinds_when_none_was_picked_at_launch(self):
        seen = self.direction_box()
        self.assertNotIn("error", seen, json.dumps(seen))
        self.assertTrue(seen["shown"], "no launch choice: the box asks")
        self.assertTrue(seen["aboveMessage"], "the choice sits above What should Delivery do?")
        self.assertEqual(seen["checkedByDefault"], "development")
        for words in ("What kind of work is this?", "Write code", "Analyze code", "Write specs"):
            self.assertIn(words, seen["label"])
        self.assertEqual(json.loads(seen["pending"])["taskKind"], "analysis", "the picked kind is what the direction carries")

    def test_the_give_direction_box_asks_nothing_when_a_kind_was_picked_at_launch(self):
        seen = self.direction_box("spec")
        self.assertNotIn("error", seen, json.dumps(seen))
        self.assertFalse(seen["shown"])
        self.assertIn("Write specs", seen["note"])
        self.assertEqual(json.loads(seen["pending"])["taskKind"], "spec")

    def test_the_card_before_go_ahead_says_how_the_task_runs(self):
        for flag, kind, words in ((False, "full", "full process (tests first, independent review, end-to-end)"),
                                  (True, "document", "document only (plain-words contract, text review)")):
            with self.subTest(document_only=flag):
                folder = self.base / f"card{int(flag)}"
                folder.mkdir()
                root = context(folder)
                agent = delivery_with_flag(root, flag)
                board.record_requirement_proposal(root, agent["id"], TEXT)
                seen = self.page(root, CARD_PROBE)
                self.assertNotIn("error", seen, json.dumps(seen))
                self.assertEqual(seen["runningKind"], kind)
                self.assertIn(f"Running as: {words}", seen["running"])
                self.assertEqual(seen["count"], "")

    def test_the_full_process_card_counts_the_criteria_with_no_test_yet(self):
        fx = _Fixture("setUp")
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        agent = delivery_with_flag(fx.root, False)
        _session, reviewer = fx.reviewer()
        board.record_requirement_proposal(fx.root, agent["id"], TEXT)
        criteria = [
            {"text": "The report lists every problem found", "why_not_checked": "judged by reading it"},
            {"text": "Each problem names its file", "why_not_checked": "judged by reading it"},
            {"text": "The report says what was not examined", "why_not_checked": "a statement of scope"},
        ]
        fx.submit(reviewer, {"criteria": criteria, "questions": []})
        seen = self.page(fx.root, CARD_PROBE)
        self.assertNotIn("error", seen, json.dumps(seen))
        self.assertEqual(seen["count"], "3 of 3 criteria have no test yet.")
        self.assertEqual(len(seen["reasons"]), 3)
        self.assertIn("judged by reading it", seen["reasons"][0])


if __name__ == "__main__":
    unittest.main()

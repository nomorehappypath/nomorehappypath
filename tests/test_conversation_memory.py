# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""A managed agent's conversation survives a relaunch, and the harness keeps a transcript.

2026-09-22: an afternoon of CTO design work vanished when the harness
restarted, because the runner started every CLI fresh and the supervisor
kept only what the owner typed. These tests drive the real runner with fake
CLIs and read the exact argv, write real transcripts through the real writer,
discover a Codex session id from a real-shaped rollout file, and read the
Conversation link off the rendered page.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

from harness import board, board_viewer, browser_acceptance, control, conversation, project_memory
from harness.project_context import ProjectContext
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_managed_agent.sh"

FAKE_CLI = (
    "#!/usr/bin/env bash\n"
    "printf '%s\\n' \"$@\" > \"$HARNESS_CAPTURE\"\n"
)


def fake_cli(path: Path) -> Path:
    path.write_text(FAKE_CLI, encoding="utf-8")
    path.chmod(0o755)
    return path


def stage_relaunch(root: Path, session_id: str) -> None:
    """What the manager does before reopening a dead terminal: stage, then claim."""
    control.prepare_resume_sessions(root, [session_id])
    control.mark_resume_launch_requested(root, session_id)


def run_runner(root: Path, session: dict, environment: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(RUNNER), "--root", str(root), "--session-id", session["id"], "--kind", session["kind"]],
        env=environment, capture_output=True, text=True, timeout=20,
    )


class TranscriptTests(unittest.TestCase):
    def test_both_directions_are_kept_stripped_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control" / "transcripts" / "s.log"
            transcript = conversation.Transcript(path)
            transcript.note("supervisor started")
            transcript.agent_bytes(b"\x1b[2J\x1b[1;1H\x1b[32mHello owner\x1b[0m\r\n")
            transcript.agent_bytes(b"Hello owner\r\n")          # TUI redraw of the same line
            transcript.agent_bytes(b"\x1b]0;title\x07partial")   # OSC title + partial line
            transcript.owner("make the motion agents faster")
            transcript.agent_bytes(b" line\n")
            transcript.close()
            text = path.read_text(encoding="utf-8")
        lines = [line.split(" ", 2)[1:] for line in text.splitlines()]
        self.assertEqual(lines, [
            ["--", "supervisor started"],
            ["<<", "Hello owner"],
            ["<<", "partial"],
            [">>", "make the motion agents faster"],
            ["<<", " line"],
        ])
        self.assertNotIn("\x1b", text)
        for line in text.splitlines():
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 ")

    def test_transcript_path_refuses_anything_but_a_plain_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = ProjectContext(Path(tmp) / "code", Path(tmp) / "data", Path(tmp) / "ws")
            self.assertEqual(
                conversation.transcript_path(context, "claude_cto-abc123"),
                Path(tmp).resolve() / "data" / "control" / "transcripts" / "claude_cto-abc123.log",
            )
            with self.assertRaises(ValueError):
                conversation.transcript_path(context, "../escape")


def write_rollout(day: Path, session_id: str, cwd: Path, launch_prompt: str | None, *, meta_padding: int = 0) -> Path:
    """A rollout shaped like a REAL one, read off this Mac on 2026-09-22.

    Line 1 is `session_meta` (~100 KB with base instructions); Codex then
    injects the project's AGENTS.md as user message #1; the launch prompt is
    user message #2. A parser that reads only the first user message, or only
    a small head of the file, misses the prompt — that was the round-2 bug.
    """
    path = day / f"rollout-2026-09-22T16-00-00-{session_id}.jsonl"

    def user_message(text: str) -> str:
        return json.dumps({"timestamp": "2026-09-22T16:00:01Z", "type": "response_item",
                           "payload": {"type": "message", "role": "user",
                                       "content": [{"type": "input_text", "text": text}]}})

    lines = [json.dumps({
        "timestamp": "2026-09-22T16:00:00Z", "type": "session_meta",
        "payload": {"id": session_id, "cwd": str(cwd), "cli_version": "0.156.0",
                    "base_instructions": "x" * meta_padding},
    })]
    lines.append(user_message("# AGENTS.md instructions for " + str(cwd) + "\n\n<INSTRUCTIONS>\nno marker here\n</INSTRUCTIONS>"))
    lines.append(json.dumps({"timestamp": "2026-09-22T16:00:01Z", "type": "event_msg",
                             "payload": {"type": "item_completed", "text": "an event line that is NOT a user message"}}))
    if launch_prompt is not None:
        lines.append(user_message(launch_prompt))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class CodexDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "codex-home"
        self.day = self.home / "sessions" / "2026" / "09" / "22"; self.day.mkdir(parents=True)
        self.cwd = Path(self.tmp.name) / "project"; self.cwd.mkdir()
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}); patcher.start(); self.addCleanup(patcher.stop)

    def test_two_codex_roles_started_together_in_one_project_each_find_their_own_rollout(self):
        """The reviewer's round-1 probe: same project, same second, two Delivery roles."""
        since = time.time() - 5
        marker_a = conversation.codex_launch_marker("codex_delivery-aaaaaaaaaa", 1)
        marker_b = conversation.codex_launch_marker("codex_delivery-bbbbbbbbbb", 1)
        # Session_meta alone is ~100 KB in a real rollout; pad it so a small head read would miss the prompt.
        write_rollout(self.day, "00000000-0000-0000-0000-00000000aaaa", self.cwd, "directive…\n" + marker_a, meta_padding=150_000)
        write_rollout(self.day, "00000000-0000-0000-0000-00000000bbbb", self.cwd, "directive…\n" + marker_b, meta_padding=150_000)
        write_rollout(self.day, "00000000-0000-0000-0000-00000000cccc", self.cwd, "an owner's own codex session, no marker")
        write_rollout(self.day, "00000000-0000-0000-0000-00000000dddd", self.cwd, None)
        # The marker quoted inside an ASSISTANT message of another session must not claim it.
        decoy = self.day / "rollout-2026-09-22T16-00-00-00000000-0000-0000-0000-00000000ffff.jsonl"
        decoy.write_text(json.dumps({"type": "session_meta", "payload": {"id": "00000000-0000-0000-0000-00000000ffff", "cwd": str(self.cwd)}}) + "\n"
                         + json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                                             "content": [{"type": "output_text", "text": marker_a}]}}) + "\n",
                         encoding="utf-8")
        self.assertEqual(conversation.discover_codex_session_id(since, self.cwd, marker_a), "00000000-0000-0000-0000-00000000aaaa")
        self.assertEqual(conversation.discover_codex_session_id(since, self.cwd, marker_b), "00000000-0000-0000-0000-00000000bbbb")
        self.assertIsNone(conversation.discover_codex_session_id(since, self.cwd, conversation.codex_launch_marker("codex_delivery-cccccccccc", 1)))

    def test_a_fresh_relaunch_does_not_claim_its_own_predecessor(self):
        since = time.time() - 5
        first = conversation.codex_launch_marker("codex_delivery-aaaaaaaaaa", 1)
        second = conversation.codex_launch_marker("codex_delivery-aaaaaaaaaa", 2)
        write_rollout(self.day, "00000000-0000-0000-0000-00000000aaaa", self.cwd, first)
        self.assertIsNone(conversation.discover_codex_session_id(since, self.cwd, second))
        write_rollout(self.day, "00000000-0000-0000-0000-00000000eeee", self.cwd, second)
        self.assertEqual(conversation.discover_codex_session_id(since, self.cwd, second), "00000000-0000-0000-0000-00000000eeee")

    def test_other_cwd_older_file_and_missing_marker_are_never_claimed(self):
        other = Path(self.tmp.name) / "other"; other.mkdir()
        marker = conversation.codex_launch_marker("codex_delivery-aaaaaaaaaa", 1)
        old = write_rollout(self.day, "00000000-0000-0000-0000-00000000aaaa", self.cwd, marker)
        past = time.time() - 3600; os.utime(old, (past, past))
        since = time.time()
        write_rollout(self.day, "00000000-0000-0000-0000-00000000bbbb", other, marker)
        self.assertIsNone(conversation.discover_codex_session_id(since, self.cwd, marker))
        with self.assertRaises(ValueError):
            conversation.discover_codex_session_id(since, self.cwd, "")
        mine = write_rollout(self.day, "00000000-0000-0000-0000-00000000cccc", self.cwd, marker)
        self.assertEqual(conversation.discover_codex_session_id(since, self.cwd, marker), "00000000-0000-0000-0000-00000000cccc")
        self.assertEqual(conversation.codex_rollout_path("00000000-0000-0000-0000-00000000cccc"), mine)


class LaunchPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.claude_dir = Path(self.tmp.name) / "claude-config"
        self.codex_home = Path(self.tmp.name) / "codex-home"
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude_dir), "CODEX_HOME": str(self.codex_home)})
        patcher.start(); self.addCleanup(patcher.stop)

    def test_claude_first_launch_mints_and_records_then_resumes_only_while_the_store_has_it(self):
        session = control.create(self.root, "claude_cto")
        first = conversation.plan_cli_launch(self.root, session["id"], "claude")
        self.assertEqual(first["mode"], "fresh")
        uuid.UUID(first["cli_session_id"])
        self.assertEqual(control.cli_session(self.root, session["id"])["cli_session_id"], first["cli_session_id"])
        control.note_cli_launch(self.root, session["id"], resumed=False)

        # Relaunch, but the vendor store has no such session: fresh, with a new id and the reason.
        again = conversation.plan_cli_launch(self.root, session["id"], "claude")
        self.assertEqual(again["mode"], "fresh")
        self.assertNotEqual(again["cli_session_id"], first["cli_session_id"])
        self.assertIn("not in the vendor store", again["reason"])
        control.note_cli_launch(self.root, session["id"], resumed=False)

        # Relaunch with the session present in the store: resume by that id.
        store = self.claude_dir / "projects" / "-Users-owner-project"
        store.mkdir(parents=True)
        (store / f"{again['cli_session_id']}.jsonl").write_text("{}\n", encoding="utf-8")
        third = conversation.plan_cli_launch(self.root, session["id"], "claude")
        self.assertEqual(third["mode"], "resume")
        self.assertEqual(third["cli_session_id"], again["cli_session_id"])
        self.assertTrue(third["transcript"].endswith(f"control/transcripts/{session['id']}.log"))

    def test_codex_first_launch_has_no_id_and_resumes_once_one_is_recorded(self):
        session = control.create(self.root, "codex_delivery")
        first = conversation.plan_cli_launch(self.root, session["id"], "codex")
        self.assertEqual((first["mode"], first["cli_session_id"]), ("fresh", ""))
        self.assertEqual(first["codex_marker"], conversation.codex_launch_marker(session["id"], 1))
        control.note_cli_launch(self.root, session["id"], resumed=False)
        control.record_cli_session(self.root, session["id"], "00000000-0000-0000-0000-00000000cccc", "codex")
        fallback = conversation.plan_cli_launch(self.root, session["id"], "codex")
        self.assertEqual(fallback["mode"], "fresh")
        self.assertIn("not in the vendor store", fallback["reason"])
        self.assertIsNone(control.cli_session(self.root, session["id"])["cli_session_id"],
                          "a fallback must forget the id it could not resume")
        control.record_cli_session(self.root, session["id"], "00000000-0000-0000-0000-00000000cccc", "codex")
        day = self.codex_home / "sessions" / "2026" / "09" / "22"; day.mkdir(parents=True)
        (day / "rollout-2026-09-22T16-00-00-00000000-0000-0000-0000-00000000cccc.jsonl").write_text("{}\n", encoding="utf-8")
        plan = conversation.plan_cli_launch(self.root, session["id"], "codex")
        self.assertEqual((plan["mode"], plan["cli_session_id"]), ("resume", "00000000-0000-0000-0000-00000000cccc"))


class RunnerArgvTests(unittest.TestCase):
    """The real runner, fake CLIs, exact argv — first launch and relaunch, both providers."""

    def setUp(self):
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.capture = self.root / "captured.txt"
        self.claude_dir = Path(self.tmp.name) / "claude-config"
        self.codex_home = Path(self.tmp.name) / "codex-home"
        self.environment = {
            **os.environ,
            "HARNESS_CAPTURE": str(self.capture),
            "HARNESS_CLAUDE_BIN": str(fake_cli(self.root / "fake-claude")),
            "HARNESS_CODEX_BIN": str(fake_cli(self.root / "fake-codex")),
            "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            "CODEX_HOME": str(self.codex_home),
        }

    def argv(self) -> list[str]:
        # The fake CLI prints one argument per line; a multi-line prompt spans
        # several lines, so flags are read from the lines and the prompt from
        # the whole text.
        return self.capture.read_text(encoding="utf-8").splitlines()

    def text(self) -> str:
        return self.capture.read_text(encoding="utf-8")

    def test_claude_first_launch_gets_a_session_id_and_a_relaunch_resumes_it_with_a_recovery_message(self):
        session = control.create(self.root, "claude_cto")
        completed = run_runner(self.root, session, self.environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        argv = self.argv()
        self.assertIn("--session-id", argv)
        minted = argv[argv.index("--session-id") + 1]
        uuid.UUID(minted)
        self.assertNotIn("--resume", argv)
        self.assertIn("# CTO Directive", self.text())
        record = control.cli_session(self.root, session["id"])
        self.assertEqual((record["cli_session_id"], record["cli_launches"], record["cli_last_launch_resumed"]), (minted, 1, False))
        transcript = conversation.transcript_path(self.root, session["id"])
        self.assertIn("launch: mode=fresh provider=claude", transcript.read_text(encoding="utf-8"))

        # The vendor store holds the session: the relaunch resumes it.
        store = self.claude_dir / "projects" / "-project"; store.mkdir(parents=True)
        (store / f"{minted}.jsonl").write_text("{}\n", encoding="utf-8")
        stage_relaunch(self.root, session["id"])
        completed = run_runner(self.root, session, self.environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        argv = self.argv()
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], minted)
        self.assertNotIn("--session-id", argv)
        prompt = self.text()
        self.assertIn(conversation.RECOVERY_LABEL, prompt)
        self.assertIn(minted, prompt)
        self.assertIn(str(transcript), prompt)
        self.assertNotIn("# CTO Directive", prompt, "a resumed agent already holds the directive")
        record = control.cli_session(self.root, session["id"])
        self.assertEqual((record["cli_launches"], record["cli_last_launch_resumed"]), (2, True))
        self.assertIn("launch: mode=resume provider=claude", transcript.read_text(encoding="utf-8"))

    def test_claude_relaunch_falls_back_to_fresh_when_the_store_lost_the_session(self):
        session = control.create(self.root, "claude_reviewer")
        self.assertEqual(run_runner(self.root, session, self.environment).returncode, 0)
        first = self.argv()[self.argv().index("--session-id") + 1]
        stage_relaunch(self.root, session["id"])
        completed = run_runner(self.root, session, self.environment)   # nothing in the store
        self.assertEqual(completed.returncode, 0, completed.stderr)
        argv = self.argv()
        self.assertNotIn("--resume", argv)
        self.assertIn("--session-id", argv)
        self.assertNotEqual(argv[argv.index("--session-id") + 1], first)
        self.assertIn("MODE: Independent Reviewer", self.text())
        self.assertIn("not in the vendor store; starting fresh",
                      conversation.transcript_path(self.root, session["id"]).read_text(encoding="utf-8"))

    def test_codex_first_launch_is_unchanged_and_a_relaunch_resumes_the_recorded_id(self):
        session = control.create(self.root, "codex_delivery")
        completed = run_runner(self.root, session, self.environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        argv = self.argv()
        self.assertEqual(argv[0], "--cd")
        self.assertNotIn("resume", argv[:1])
        self.assertIn("MODE: Delivery Agent.", self.text())
        self.assertIn(conversation.codex_launch_marker(session["id"], 1), self.text(),
                      "the fresh Codex prompt must carry this launch's marker")

        recorded = "00000000-0000-0000-0000-00000000cccc"
        control.record_cli_session(self.root, session["id"], recorded, "codex")
        day = self.codex_home / "sessions" / "2026" / "09" / "22"; day.mkdir(parents=True)
        (day / f"rollout-2026-09-22T16-00-00-{recorded}.jsonl").write_text("{}\n", encoding="utf-8")
        stage_relaunch(self.root, session["id"])
        completed = run_runner(self.root, session, self.environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        argv = self.argv()
        self.assertEqual(argv[0], "resume")
        self.assertIn(recorded, argv)
        self.assertIn("sandbox_mode=workspace-write", " ".join(argv))
        self.assertIn("approval_policy=never", " ".join(argv))
        self.assertNotIn("--cd", argv)
        self.assertIn(conversation.RECOVERY_LABEL, self.text())
        self.assertNotIn("MODE: Delivery Agent.", self.text())
        self.assertNotIn(conversation.CODEX_MARKER_PREFIX, self.text(), "a resume needs no discovery marker")


class CodexStoreClearedRelaunchTests(unittest.TestCase):
    """Round-2 blocking finding, end to end with the real runner.

    Fresh → discovered id X → relaunch resumes X → vendor store loses X →
    relaunch falls back fresh AND forgets X → the supervisor's decision says
    "discover, with THIS launch's marker" → the new rollout Y carrying that
    marker is discovered and recorded → the next relaunch resumes Y.
    """

    def setUp(self):
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"; self.root.mkdir()
        self.capture = self.root / "captured.txt"
        self.codex_home = Path(self.tmp.name) / "codex-home"
        self.day = self.codex_home / "sessions" / "2026" / "09" / "22"; self.day.mkdir(parents=True)
        self.environment = {**os.environ, "HARNESS_CAPTURE": str(self.capture),
                            "HARNESS_CODEX_BIN": str(fake_cli(self.root / "fake-codex")),
                            "CLAUDE_CONFIG_DIR": str(Path(self.tmp.name) / "claude-config"),
                            "CODEX_HOME": str(self.codex_home)}
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}); patcher.start(); self.addCleanup(patcher.stop)

    def launch(self, session) -> tuple[list[str], str]:
        completed = run_runner(self.root, session, self.environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        text = self.capture.read_text(encoding="utf-8")
        return text.splitlines(), text

    def supervisor_discovers(self, session_id: str, rollout_id: str, since: float) -> str:
        """What the supervisor does after the CLI starts: decide, discover by marker, record."""
        pending, marker = conversation.codex_discovery_state(self.root, session_id, "codex")
        self.assertTrue(pending, "the supervisor must be looking for a rollout on a launch with no id")
        write_rollout(self.day, rollout_id, self.root, "directive…\n" + marker)
        found = conversation.discover_codex_session_id(since, self.root, marker)
        self.assertEqual(found, rollout_id)
        control.record_cli_session(self.root, session_id, found, "codex")
        return marker

    def test_after_the_store_is_cleared_the_next_relaunch_resumes_the_NEW_session(self):
        session = control.create(self.root, "codex_delivery")
        since = time.time() - 5
        # Launch 1: fresh, marker #1 in the prompt; the supervisor discovers X.
        argv, text = self.launch(session)
        self.assertEqual(argv[0], "--cd")
        marker1 = self.supervisor_discovers(session["id"], "00000000-0000-0000-0000-0000000000aa", since)
        self.assertIn(marker1, text)
        # Launch 2: resume X.
        stage_relaunch(self.root, session["id"])
        argv, text = self.launch(session)
        self.assertEqual(argv[0], "resume"); self.assertIn("00000000-0000-0000-0000-0000000000aa", argv)
        self.assertFalse(conversation.codex_discovery_state(self.root, session["id"], "codex")[0],
                         "a resumed launch must not go looking for a rollout")
        # The vendor store loses X.
        conversation.codex_rollout_path("00000000-0000-0000-0000-0000000000aa").unlink()
        # Launch 3: fresh fallback — and the stale id is forgotten, so discovery is back on.
        stage_relaunch(self.root, session["id"])
        argv, text = self.launch(session)
        self.assertEqual(argv[0], "--cd")
        self.assertIn("MODE: Delivery Agent.", text)
        transcript = conversation.transcript_path(self.root, session["id"]).read_text(encoding="utf-8")
        self.assertIn("is not in the vendor store; starting fresh", transcript)
        record = control.cli_session(self.root, session["id"])
        self.assertIsNone(record["cli_session_id"], "the stale id must be cleared on the fresh fallback")
        self.assertEqual(record["cli_launches"], 3)
        pending, marker3 = conversation.codex_discovery_state(self.root, session["id"], "codex")
        self.assertTrue(pending)
        self.assertEqual(marker3, conversation.codex_launch_marker(session["id"], 3))
        self.assertIn(marker3, text, "the fresh fallback prompt must carry launch #3's marker")
        # The supervisor discovers the NEW rollout Y by that marker and records it.
        self.supervisor_discovers(session["id"], "00000000-0000-0000-0000-0000000000bb", since)
        # Launch 4: resumes Y, not X.
        stage_relaunch(self.root, session["id"])
        argv, text = self.launch(session)
        self.assertEqual(argv[0], "resume")
        self.assertIn("00000000-0000-0000-0000-0000000000bb", argv)
        self.assertNotIn("00000000-0000-0000-0000-0000000000aa", argv)
        self.assertIn(conversation.RECOVERY_LABEL, text)


class TranscriptEndpointTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(self.context)
        board.snapshot(self.context)
        project_memory.initialize(self.context, project_name="Memory proof", description="Facts.")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.context, project_name="Memory proof", manager_url="http://127.0.0.1:1/",
            settings_home=base / "home", project_id="memory-proof", chat_action_token="token",
        ))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_the_transcript_is_served_as_text_and_unknown_sessions_are_404(self):
        transcript = conversation.Transcript(conversation.transcript_path(self.context, "claude_cto-abc123"))
        transcript.owner("how do we make the motion agents better?")
        transcript.agent_bytes(b"Three options: ...\n")
        transcript.close()
        with urlopen(f"{self.base_url}/api/transcripts/claude_cto-abc123", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/plain", response.headers.get("Content-Type", ""))
            text = response.read().decode("utf-8")
        self.assertIn(">> how do we make the motion agents better?", text)
        self.assertIn("<< Three options: ...", text)
        for missing in ("claude_cto-nothere", "..%2Fescape"):
            with self.assertRaises(Exception) as caught:
                urlopen(f"{self.base_url}/api/transcripts/{missing}", timeout=10)
            self.assertEqual(getattr(caught.exception, "code", None), 404)


PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (document.querySelector('#agents .agent-row .conversation-link')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const link = document.querySelector('#agents .agent-row .conversation-link');
  let fetched = '', status = 0;
  if (link) { const r = await fetch(link.getAttribute('href'), {cache: 'no-store'}); status = r.status; fetched = await r.text(); }
  const button = link ? Array.from(link.parentElement.querySelectorAll('button')).find(b => b.textContent.trim() === 'View status') : null;
  const style = el => { const c = getComputedStyle(el); const b = el.getBoundingClientRect();
    return {bg: c.backgroundColor, color: c.color, weight: c.fontWeight, size: c.fontSize, radius: c.borderRadius,
            underline: c.textDecorationLine, height: Math.round(b.height)}; };
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({present: Boolean(link), text: link ? link.textContent.trim() : '', href: link ? link.getAttribute('href') : '',
                          visible: link ? link.getBoundingClientRect().width > 0 : false, status, fetched,
                          linkStyle: link ? style(link) : null, buttonStyle: button ? style(button) : null})});
})();
</script>
"""

PREFIX = "/project"


def manager_like_proxy(target: str, sink: dict, script: str):
    """A proxy that routes the way the real manager does.

    Mission Control serves the board under `/project/` and forwards only that
    route to the project worker, stripping the prefix; anything else is the
    manager's own surface and answers `{"error": "not found"}`. The first
    rendered proof served the board at the root, so a link built without the
    prefix looked fine and then broke for the owner on the real page.
    """
    inner = probe_proxy(target, sink, script)

    class Proxy(inner):
        def _rewrite(self) -> bool:
            if self.path == "/__probe__":
                return True
            if self.path == PREFIX or self.path.startswith(PREFIX + "/"):
                self.path = self.path[len(PREFIX):] or "/"
                return True
            body = b'{"error": "not found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False

        def do_GET(self):
            if self._rewrite():
                super().do_GET()

        def do_POST(self):
            if self._rewrite():
                super().do_POST()

    return Proxy


class RenderedConversationLinkTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(self.context)
        board.snapshot(self.context)
        project_memory.initialize(self.context, project_name="Memory proof", description="Facts.")
        self.session = control.create(self.context, "claude_cto")
        control.attach(self.context, self.session["id"], os.getpid())
        board.register(self.context, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=self.session["id"])
        transcript = conversation.Transcript(conversation.transcript_path(self.context, self.session["id"]))
        transcript.owner("let us enhance the motion creative agents")
        transcript.agent_bytes(b"Here is the analysis.\n")
        transcript.close()

    def test_the_agent_row_offers_the_conversation_and_it_opens_the_transcript_under_the_manager_prefix(self):
        # Served exactly as the worker is started by the manager: with the /project prefix.
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.context, project_name="Memory proof", manager_url="http://127.0.0.1:1/",
            settings_home=Path(self.tmp.name) / "home", project_id="memory-proof", chat_action_token="token",
            api_prefix=PREFIX,
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), manager_like_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close); self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory(); self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}{PREFIX}/", Path(profile.name), width=1280, height=850)
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        reading = sink["value"]
        self.assertTrue(reading["present"], json.dumps(reading))
        self.assertEqual(reading["text"], "Conversation")
        self.assertTrue(reading["visible"])
        self.assertEqual(reading["href"], f"{PREFIX}/api/transcripts/{self.session['id']}",
                         "the link must carry the manager prefix the page was served with")
        self.assertEqual(reading["status"], 200, f"following the link through the manager route: {reading}")
        self.assertIn(">> let us enhance the motion creative agents", reading["fetched"])
        self.assertIn("<< Here is the analysis.", reading["fetched"])
        # It sits beside "View status" and must look like it: same button face,
        # no underline, same height. The owner saw a bare underlined text link.
        link_style, button_style = reading["linkStyle"], reading["buttonStyle"]
        self.assertIsNotNone(button_style, "the View status button must be in the same actions row")
        self.assertEqual(link_style["underline"], "none", f"the link is underlined like plain text: {link_style}")
        for key in ("bg", "color", "weight", "size", "radius"):
            self.assertEqual(link_style[key], button_style[key], f"{key} differs from the neighbouring button: {link_style} vs {button_style}")
        self.assertLessEqual(abs(link_style["height"] - button_style["height"]), 2, f"heights differ: {link_style} vs {button_style}")


if __name__ == "__main__":
    unittest.main()

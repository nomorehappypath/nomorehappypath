# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""A managed agent's conversation survives the harness, and so does its memory.

On 2026-09-22 the owner spent an afternoon with the CTO agent working out a
design. The harness restarted, the CTO was relaunched, and it came back with
no memory of any of it. The conversation was not gone — the CLI had saved it
under its own session store — but the runner starts every CLI fresh and the
supervisor keeps only the lines the owner typed.

This module is the memory the harness itself keeps, and the decision about
how to start a CLI so that it keeps its own:

- ``plan_cli_launch`` decides FRESH or RESUME for one managed session. Claude
  Code is given a harness-chosen session id at first launch and resumed by it
  later; Codex records its own id, which the supervisor discovers from the
  rollout file it writes, and is resumed by that. A resume that cannot work
  (the vendor store no longer has the session) falls back to a fresh start
  and says so in the transcript — never a dead terminal.
- ``Transcript`` writes both sides of the terminal — what the owner typed and
  what the agent printed, escape sequences stripped, timestamped — under the
  project's data root, where Mission Control serves it and a relaunched agent
  is told to read it.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness import control
from harness.project_context import ProjectRoot

# Terminal escape sequences a transcript reader should not see: CSI, OSC and
# the remaining two-byte ESC forms, then the control bytes that are not text.
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_TWO_BYTE = re.compile(r"\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

RECOVERY_LABEL = "[SYSTEM CONTROL — conversation recovered]"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def strip_terminal_sequences(text: str) -> str:
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = _TWO_BYTE.sub("", text)
    text = _CONTROL.sub("", text)
    return text


def transcript_path(root: ProjectRoot, session_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(session_id or "")):
        raise ValueError("session id must be a plain identifier")
    return control.control_dir(root) / "transcripts" / f"{session_id}.log"


def transcript_text(root: ProjectRoot, session_id: str) -> str | None:
    path = transcript_path(root, session_id)
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


class Transcript:
    """Append-only, line-oriented record of one managed terminal, both ways.

    Agent output arrives as raw PTY bytes with cursor movement and redraws;
    it is stripped to text, split on newlines and carriage returns, and
    consecutive identical lines are collapsed so a TUI redraw does not turn
    one line into a thousand. Owner lines arrive already assembled by the
    supervisor's owner-line parser, so they are written exactly.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self._pending = ""
        self._last_agent_line = ""

    def note(self, text: str) -> None:
        self._flush_pending()
        self._emit("--", text)

    def owner(self, text: str) -> None:
        self._flush_pending()
        for line in str(text).splitlines() or [""]:
            self._emit(">>", line)

    def agent_bytes(self, data: bytes) -> None:
        text = strip_terminal_sequences(data.decode("utf-8", errors="replace"))
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self._pending += text
        *complete, self._pending = self._pending.split("\n")
        for line in complete:
            self._agent_line(line)

    def close(self) -> None:
        self._flush_pending()
        try:
            self._handle.close()
        except OSError:
            pass

    def _flush_pending(self) -> None:
        if self._pending.strip():
            self._agent_line(self._pending)
        self._pending = ""

    def _agent_line(self, line: str) -> None:
        line = line.rstrip()
        if not line.strip() or line == self._last_agent_line:
            return
        self._last_agent_line = line
        self._emit("<<", line)

    def _emit(self, marker: str, line: str) -> None:
        try:
            self._handle.write(f"{_now()} {marker} {line}\n")
            self._handle.flush()
        except OSError:
            pass


# ---------------------------------------------------------------- vendor stores

def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def claude_session_exists(cli_session_id: str) -> bool:
    """Claude Code keeps `<config>/projects/<cwd-slug>/<session-id>.jsonl`."""
    projects = claude_config_dir() / "projects"
    if not cli_session_id or not projects.is_dir():
        return False
    return any(projects.glob(f"*/{cli_session_id}.jsonl"))


def codex_rollout_path(cli_session_id: str) -> Path | None:
    """Codex keeps `<home>/sessions/YYYY/MM/DD/rollout-<stamp>-<session-id>.jsonl`."""
    sessions = codex_home() / "sessions"
    if not cli_session_id or not sessions.is_dir():
        return None
    for candidate in sessions.rglob(f"rollout-*-{cli_session_id}.jsonl"):
        return candidate
    return None


CODEX_MARKER_PREFIX = "HARNESS_MANAGED_SESSION="


def codex_launch_marker(session_id: str, launch_number: int) -> str:
    """A token unique to ONE launch of ONE managed session, placed in the prompt.

    Codex records the launch prompt as the first user message of its rollout,
    so the rollout that carries this exact token is the one this launch
    started — regardless of how many other Codex roles started in the same
    project at the same moment. A reviewer proved that launch time plus
    working directory alone let two Delivery roles record the same session.
    The launch number keeps a fresh relaunch from matching its own predecessor.
    """
    return f"{CODEX_MARKER_PREFIX}{session_id}#{int(launch_number)}"


def _rollout_carries_marker(path: Path, marker: str, byte_limit: int = 8 * 1024 * 1024) -> bool:
    """True if any user message in the rollout's first `byte_limit` bytes carries the marker.

    Not "the first user message": on a real rollout Codex injects the
    project's AGENTS.md as user message #1 and the launch prompt is #2, and
    `session_meta` alone runs to ~100 KB, so this walks the file line by line
    up to a cap and checks every user-role message it meets.
    """
    consumed = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                consumed += len(line)
                if consumed > byte_limit:
                    return False
                if marker not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                    return True
    except OSError:
        return False
    return False


def discover_codex_session_id(since: float, cwd: Path, marker: str) -> str | None:
    """Find the rollout Codex started writing for THIS launch, by its marker.

    Candidates are rollouts created after this launch began whose recorded
    cwd is this launch's execution root; the one accepted is the one whose
    first user message carries this launch's marker. A rollout without the
    marker is never claimed, so two Codex roles started together in the same
    project cannot record each other's session.
    """
    if not marker:
        raise ValueError("a launch marker is required to identify a Codex rollout")
    sessions = codex_home() / "sessions"
    if not sessions.is_dir():
        return None
    resolved_cwd = str(Path(cwd).resolve())
    candidates = sorted(
        (path for path in sessions.rglob("rollout-*.jsonl") if path.stat().st_mtime >= since - 1),
        key=lambda path: path.stat().st_mtime, reverse=True,
    )
    for path in candidates:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline()
            record = json.loads(first)
        except (OSError, ValueError):
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            continue
        session_id = str(payload.get("id") or "")
        recorded_cwd = str(payload.get("cwd") or "")
        if not session_id or not recorded_cwd or str(Path(recorded_cwd).resolve()) != resolved_cwd:
            continue
        if _rollout_carries_marker(path, marker):
            return session_id
    return None


# ---------------------------------------------------------------- launch plan

def plan_cli_launch(root: ProjectRoot, session_id: str, provider: str) -> dict[str, Any]:
    """Decide how the runner starts the CLI for one managed session.

    Returns ``mode`` FRESH or RESUME, the ``cli_session_id`` to use (minted
    and recorded for a fresh Claude launch), the transcript path, and a
    ``reason`` a person can read. A resume is offered only when the vendor
    store still holds the session; otherwise the plan is FRESH and the reason
    says why, and the runner writes that reason into the transcript.
    """
    provider = str(provider or "").strip()
    if provider not in {"claude", "codex"}:
        raise ValueError("unknown CLI provider")
    record = control.cli_session(root, session_id)
    known = str(record.get("cli_session_id") or "")
    launches = int(record.get("cli_launches") or 0)
    transcript = str(transcript_path(root, session_id))
    if launches > 0 and known:
        if provider == "claude" and claude_session_exists(known):
            return {"mode": "resume", "provider": provider, "cli_session_id": known,
                    "transcript": transcript, "reason": f"resuming Claude Code session {known}"}
        if provider == "codex" and codex_rollout_path(known) is not None:
            return {"mode": "resume", "provider": provider, "cli_session_id": known,
                    "transcript": transcript, "reason": f"resuming Codex session {known}"}
        reason = f"previous {provider} session {known} is not in the vendor store; starting fresh"
        # The stale id must go: the supervisor discovers a NEW Codex rollout only
        # while the record carries no id, and a later plan would otherwise keep
        # checking the same missing rollout and start fresh forever. Claude's
        # id is replaced by the freshly minted one below.
        control.clear_cli_session(root, session_id, reason)
    elif launches > 0:
        reason = f"previous {provider} launch recorded no session id; starting fresh"
    else:
        reason = "first launch of this managed session"
    if provider == "claude":
        minted = str(uuid.uuid4())
        control.record_cli_session(root, session_id, minted, provider)
        return {"mode": "fresh", "provider": provider, "cli_session_id": minted,
                "transcript": transcript, "reason": reason}
    return {"mode": "fresh", "provider": provider, "cli_session_id": "",
            "transcript": transcript, "reason": reason,
            "codex_marker": codex_launch_marker(session_id, launches + 1)}


def recovery_message(cli_session_id: str, transcript: str, agent_id: str, board_command_prefix: str) -> str:
    return (
        f"{RECOVERY_LABEL} This managed terminal was relaunched by the harness and your "
        f"previous conversation (session {cli_session_id}) was resumed. The full transcript "
        f"of what was said, both sides, is at {transcript}. Read it if your memory of the "
        f"discussion is thin, then continue exactly where it stopped; do not start over and "
        f"do not ask the owner to repeat anything that is in the transcript. You remain agent "
        f"{agent_id} on the board; for every board command, start with: {board_command_prefix}. "
        f"USER ACTION: None."
    )


def codex_discovery_state(root: ProjectRoot, session_id: str, provider: str) -> tuple[bool, str]:
    """What the supervisor must do for this launch: (discover?, marker).

    Discovery runs for a Codex launch that has no recorded session id — a
    first launch, or a fresh fallback after the vendor store lost the old one.
    The marker is the one the runner put in this launch's prompt: it counted
    the launch (``note-cli-launch``) before exec'ing the supervisor, so the
    recorded ``cli_launches`` is this launch's number.
    """
    record = control.cli_session(root, session_id)
    launch_number = int(record.get("cli_launches") or 1)
    marker = codex_launch_marker(session_id, launch_number)
    pending = provider == "codex" and not record.get("cli_session_id")
    return pending, marker


def wait_for_codex_session_id(root: ProjectRoot, session_id: str, since: float, cwd: Path, marker: str,
                              *, timeout: float = 90.0, poll: float = 2.0) -> str | None:
    """Supervisor helper: record the Codex session id once its rollout appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = discover_codex_session_id(since, cwd, marker)
        if found:
            control.record_cli_session(root, session_id, found, "codex")
            return found
        time.sleep(poll)
    return None

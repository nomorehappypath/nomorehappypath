# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Validate setup-tokens and deliver them once through an authenticated socket."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import agent_confinement, control, global_settings, platform_support

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
OTHER_AUTH = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
_cache = {}
_lock = threading.Lock()


class ClaudeAuthRequired(RuntimeError):
    def __init__(self, reason="missing"):
        self.reason = reason
        super().__init__(action(reason)["why"])


def action(reason="missing") -> dict:
    why = {"missing": "Claude needs a dedicated setup-token before this agent can start.",
           "rejected": "Claude rejected the setup-token. Generate a replacement before restarting this agent.",
           "unavailable": "Claude authentication could not be checked. Check your connection and retry; no agent was started."}[reason]
    instructions = platform_support.claude_credentials().instructions()
    return {"title": "Set up Claude agent authentication", "why": why, "instructions": instructions}


def resolve_token(environment=None) -> str:
    environment = os.environ if environment is None else environment
    token = platform_support.claude_credentials().read_token(environment)
    if not token or len(token) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ClaudeAuthRequired()
    return token


def manager_environment(environment) -> dict:
    value = dict(environment)
    value.pop(TOKEN_ENV, None)
    try:
        value[TOKEN_ENV] = resolve_token(environment)
        if platform_support.claude_credentials().uses_keychain and not environment.get(TOKEN_ENV):
            value["HARNESS_CLAUDE_TOKEN_SOURCE"] = "keychain"
    except ClaudeAuthRequired:
        pass  # Missing auth blocks a Claude launch, never opening the project.
    return value


def _probe_prefix(home):
    implementation = platform_support.agent_confinement()
    protected = implementation.protected_read_paths(Path.home()) + implementation.protected_read_paths(home, str(home / ".claude"))
    return implementation.wrap([], [str(home), *implementation.temp_paths()], store=home / "guard", protected_reads=protected)


def validate_token(token: str) -> None:
    cli = global_settings.resolved_cli("claude").get("path")
    if not cli:
        raise ClaudeAuthRequired("unavailable")
    key = (token, cli)  # Never persisted, hashed into an artifact, or logged.
    with _lock:
        now = time.monotonic()
        if now - _cache.get(key, float("-inf")) < float(os.environ.get("HARNESS_CLAUDE_AUTH_CACHE_SECONDS", "0")):
            return
        timeout = float(os.environ.get("HARNESS_CLAUDE_AUTH_TIMEOUT_SECONDS", "45"))
        try:
            with tempfile.TemporaryDirectory(prefix="nmhp-auth-check-") as temporary:
                home = Path(temporary)
                environment = {k: v for k, v in os.environ.items() if k not in OTHER_AUTH and k not in {"CLAUDE_CONFIG_DIR", TOKEN_ENV, "BASH_ENV", "ENV"}}
                environment.update({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"), TOKEN_ENV: token,
                                    "CLAUDE_CODE_SANDBOXED": "1", "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
                prefix = _probe_prefix(home)
                status = subprocess.run([*prefix, cli, "auth", "status"], env=environment, cwd=home,
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False)
                try:
                    authenticated = json.loads(status.stdout).get("authMethod") == "oauth_token"
                except (ValueError, AttributeError):
                    authenticated = False
                if status.returncode or not authenticated:
                    raise ClaudeAuthRequired("rejected")
                # Status identifies the credential source; a real unpersisted turn
                # checks remote acceptance before starting a managed agent.
                probe = subprocess.run([*prefix, cli, "-p", "Reply with exactly OK.", "--output-format", "json", "--model", os.environ.get("HARNESS_CLAUDE_AUTH_MODEL", "haiku"),
                                        "--no-session-persistence", "--max-turns", "1", "--tools", "", "--setting-sources", ""],
                                       env=environment, cwd=home, stdin=subprocess.DEVNULL, capture_output=True,
                                       text=True, timeout=timeout, check=False)
                try:
                    result = json.loads(probe.stdout)
                except ValueError:
                    raise ClaudeAuthRequired("unavailable") from None
                if probe.returncode or result.get("is_error"):
                    message = str(result.get("result", "")).lower()
                    rejected = any(word in message for word in ("401", "unauthorized", "authentication", "invalid token", "expired", "login", "oauth"))
                    raise ClaudeAuthRequired("rejected" if rejected else "unavailable")
        except ClaudeAuthRequired:
            raise
        except (OSError, subprocess.SubprocessError, agent_confinement.ConfinementUnavailable):
            raise ClaudeAuthRequired("unavailable") from None
        _cache.clear()
        _cache[key] = time.monotonic()


def prepare_launch(root, session: dict) -> str:
    provider = session.get("provider") or control.default_agent_settings()[control.role_for_kind(session["kind"])]["provider"]
    if provider != "claude":
        return ""
    try:
        token = resolve_token()
        validate_token(token)
    except ClaudeAuthRequired as error:
        with control.locked_state(root) as state:
            state["sessions"][session["id"]]["claude_auth_action"] = action(error.reason)
        raise
    with control.locked_state(root) as state:
        for prior in state["sessions"].values():
            if prior.get("kind") == session["kind"]:
                prior.pop("claude_auth_action", None)
    return _handoff(root, session["id"], token)


def _handoff(root, session_id: str, token: str) -> str:
    directory = Path(tempfile.mkdtemp(prefix="nmhp-auth-"))
    directory.chmod(0o700)
    path = directory / "claim.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(4)
    listener.settimeout(1)

    def deliver():
        deadline = time.monotonic() + float(os.environ.get("HARNESS_CLAUDE_AUTH_HANDOFF_SECONDS", "120"))
        try:
            while time.monotonic() < deadline:
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(5)
                    try:
                        pid = platform_support.process_identity().peer_process_id(connection)
                        request = connection.recv(512)
                        stored = control._read_state(root).get("sessions", {}).get(session_id, {})
                        if request != (session_id + "\n").encode() or stored.get("status") != "running" or stored.get("pid") != pid:
                            connection.sendall(b'{"error":"credential handoff refused"}\n')
                            continue
                        connection.sendall(json.dumps({TOKEN_ENV: token}).encode() + b"\n")
                        return
                    except (OSError, ValueError, platform_support.UnsupportedPlatformOperation):
                        continue
        finally:
            listener.close()
            path.unlink(missing_ok=True)
            directory.rmdir()

    threading.Thread(target=deliver, name="claude-credential-handoff", daemon=True).start()
    return str(path)


def exec_with_token(path: str, session_id: str, command: list[str]) -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(path)
            connection.sendall((session_id + "\n").encode())
            value = json.loads(connection.makefile("rb").readline(4096))
        token = value.get(TOKEN_ENV, "")
        if not token:
            raise ClaudeAuthRequired()
        environment = {k: v for k, v in os.environ.items() if k not in OTHER_AUTH}
        environment[TOKEN_ENV] = token
        os.execvpe(command[0], command, environment)
    except (OSError, ValueError, KeyError, ClaudeAuthRequired):
        sys.stderr.write("Claude authentication handoff failed. Retry from Mission Control.\n")
        raise SystemExit(2) from None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    exec_with_token(args.socket, args.session, command)

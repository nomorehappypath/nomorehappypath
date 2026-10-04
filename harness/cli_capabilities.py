# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Which plumbing capabilities the installed agent CLIs actually have.

Spec `docs/specs/PLUMBING_MODERNIZATION.md`, Stage 0. Every later plumbing
stage depends on something a CLI version may or may not offer: session-scoped
hooks, a structured control channel, a per-session inbox. A stage switched on
against a CLI that cannot do it must not fail on the owner's screen; it must
fall back to today's path and say why. This module is the record that decision
reads.

Two kinds of evidence, kept apart on purpose:

- STATIC capabilities are read from the binary itself - its `--help`, its
  feature list, its generated protocol schema, and (for Claude) one start-up
  with an inline hook. Never a model turn. Always with SCRATCH homes, so the
  owner's `~/.codex` and `~/.claude` are never read or written by a probe.
- LIVE capabilities need an authenticated session (does a harness message
  render in the TUI, does the inbox deliver). They are written only by the
  spike (`scripts/plumbing_spike.py`), which records `true`, `false` or
  `UNPROVEN` - and UNPROVEN gates exactly like `false`.

Both are cached per binary identity (resolved path, version, sha256), so a CLI
upgrade invalidates every result at once.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from harness import global_settings

PROVIDERS = ("codex", "claude")
CACHE_DIRECTORY = "cli-capabilities"
PROBE_TIMEOUT_SECONDS = 60
UNPROVEN = "UNPROVEN"
# Bumped whenever the static probe asks a new question (2: Claude relay admission).
PROBE_VERSION = 3

# Live items the spike proves (spec §2 gaps). Absent from a cache = UNPROVEN.
LIVE_ITEMS = {
    "codex": (
        "codex.hooks_loaded_once",          # G-3 (b)
        "codex.tui_via_multiplexer",        # G-1
        "codex.remote_renders_harness_turns",  # G-1
        "codex.turn_receipt",               # G-9
        "codex.isolated_from_daemon",       # G-2 / G-6
        "codex.developer_instructions_on_resume",
        "codex.mcp_reaches_board",
    ),
    "claude": (
        "claude.inbox_receipt",             # G-4 (transcript entry for a delivered message)
        "claude.socket_authority_parity",   # G-5
        "claude.append_prompt_on_resume",
        "claude.mcp_reaches_board",
    ),
    "both": (
        "confined_socket_bind",             # G-7
        "cross_agent_isolation",            # G-8
        "launch_surfaces_tamperproof",      # G-10
    ),
}

# Items the static probe answers from the binary itself (never a model turn).
STATIC_ITEMS = {
    "codex": (
        "codex.hooks_session_flags", "codex.bypass_hook_trust_flag", "codex.remote_tui", "codex.mcp_command",
        "codex.app_server_stdio", "codex.turn_client_message_id", "codex.exclude_tmp_keys",
        "codex.developer_instructions", "codex.permission_profiles",
        "codex.remote_tui_launch_line", "codex.server_policy_applies",
    ),
    "claude": (
        "claude.settings_flag", "claude.mcp_config", "claude.append_system_prompt", "claude.system_prompt_snapshot",
        "claude.inline_settings_hooks", "claude.inbox_socket_env", "claude.inbox_relay_admission",
    ),
}

# What each stage needs, per provider (spec §5). A stage absent for a provider
# does not apply to it (Stage 3 has one half per vendor).
STAGE_REQUIREMENTS: dict[str, dict[str, tuple[str, ...]]] = {
    "stage1_hooks": {
        "claude": ("claude.settings_flag", "claude.inline_settings_hooks"),
        "codex": ("codex.hooks_session_flags", "codex.bypass_hook_trust_flag", "codex.hooks_loaded_once"),
    },
    "stage2_board_mcp": {
        "claude": ("claude.mcp_config", "claude.mcp_reaches_board"),
        "codex": ("codex.mcp_command", "codex.mcp_reaches_board"),
    },
    "stage3_codex_app_server": {
        "codex": ("codex.app_server_stdio", "codex.remote_tui", "codex.turn_client_message_id",
                  "codex.remote_tui_launch_line", "codex.server_policy_applies",
                  "codex.tui_via_multiplexer", "codex.remote_renders_harness_turns", "codex.turn_receipt",
                  "codex.isolated_from_daemon", "cross_agent_isolation", "launch_surfaces_tamperproof"),
    },
    "stage3_claude_socket_delivery": {
        "claude": ("claude.inbox_socket_env", "claude.inbox_relay_admission", "claude.append_system_prompt",
                   "claude.system_prompt_snapshot", "claude.inbox_receipt", "claude.socket_authority_parity",
                   "confined_socket_bind", "cross_agent_isolation", "launch_surfaces_tamperproof"),
    },
    "stage4_system_layer_directives": {
        "claude": ("claude.append_system_prompt", "claude.system_prompt_snapshot", "claude.append_prompt_on_resume"),
        "codex": ("codex.developer_instructions", "codex.developer_instructions_on_resume"),
    },
    "stage5_structured_verdicts": {},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def binary_identity(provider: str, *, source_environment: dict[str, str] | None = None) -> dict[str, str]:
    """The installed CLI's resolved path, version and content hash; empty when missing."""
    resolved = global_settings.resolved_cli(provider, source_environment=source_environment)
    path = resolved.get("path", "")
    if not path:
        return {"provider": provider, "path": "", "version": "", "sha256": ""}
    real = os.path.realpath(path)
    try:
        digest = hashlib.sha256(Path(real).read_bytes()).hexdigest()
    except OSError:
        digest = ""
    # Always the binary's own answer: a binary named by HARNESS_*_BIN and the
    # same binary found on PATH must key the SAME capability record (found
    # live: the runner names it, the spike discovers it). Probing runs the
    # binary anyway, so reading its version is no new execution.
    return {"provider": provider, "path": real, "version": global_settings.cli_version(real), "sha256": digest}


def _key(identity: dict[str, str]) -> str:
    material = "\0".join((identity["provider"], identity["path"], identity["version"], identity["sha256"]))
    return f"{identity['provider']}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def cache_path(home: Path, identity: dict[str, str], *, live: bool = False) -> Path:
    return Path(home) / CACHE_DIRECTORY / f"{_key(identity)}{'.live' if live else ''}.json"


def _run(argv: list[str], environment: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=environment, cwd=str(cwd), capture_output=True, text=True,
                          timeout=PROBE_TIMEOUT_SECONDS)


def _scratch_environment(executable: str, scratch: Path) -> dict[str, str]:
    environment = global_settings.provider_environment(executable)
    environment["CODEX_HOME"] = str(scratch / "codex-home")
    environment["CLAUDE_CONFIG_DIR"] = str(scratch / "claude-config")
    (scratch / "codex-home").mkdir(parents=True, exist_ok=True)
    (scratch / "claude-config").mkdir(parents=True, exist_ok=True)
    return environment


def _probe_codex(executable: str, scratch: Path) -> dict[str, bool]:
    environment = _scratch_environment(executable, scratch)
    help_text = _run([executable, "--help"], environment, scratch).stdout
    features = _run([executable, "features", "list"], environment, scratch).stdout
    hooks_on = any(line.split()[:1] == ["hooks"] and line.split()[-1:] == ["true"] for line in features.splitlines())
    schema_dir = scratch / "schema"
    _run([executable, "app-server", "generate-json-schema", "--out", str(schema_dir)], environment, scratch)
    schema = schema_dir / "codex_app_server_protocol.v2.schemas.json"
    definitions: dict[str, Any] = {}
    methods: set[str] = set()
    try:
        definitions = json.loads(schema.read_text(encoding="utf-8")).get("definitions", {})
        requests = json.loads((schema_dir / "ClientRequest.json").read_text(encoding="utf-8"))
        for variant in requests.get("oneOf", []):
            methods.update(variant.get("properties", {}).get("method", {}).get("enum", []))
    except (OSError, ValueError):
        pass
    user_message = next((variant.get("properties", {}) for variant in definitions.get("ThreadItem", {}).get("oneOf", [])
                         if variant.get("properties", {}).get("type", {}).get("enum") == ["userMessage"]), {})
    workspace_write = next((variant.get("properties", {}) for variant in definitions.get("SandboxPolicy", {}).get("oneOf", [])
                            if "writableRoots" in variant.get("properties", {})), {})
    profile = _run([executable, "sandbox", "-c", 'default_permissions="probe"',
                    "-c", 'permissions.probe.filesystem={":root" = "read"}', "--", "/usr/bin/true"],
                   environment, scratch)
    try:
        stage3 = _probe_codex_stage3(executable, scratch, environment)
    except (OSError, subprocess.SubprocessError, ValueError):
        stage3 = {"codex.remote_tui_launch_line": False, "codex.server_policy_applies": False}
    return {
        **stage3,
        "codex.hooks_session_flags": hooks_on,
        "codex.bypass_hook_trust_flag": "--dangerously-bypass-hook-trust" in help_text,
        "codex.remote_tui": "--remote" in help_text,
        "codex.mcp_command": "\n  mcp " in help_text,
        "codex.app_server_stdio": {"initialize", "thread/start", "turn/start"} <= methods,
        "codex.turn_client_message_id": "clientUserMessageId" in definitions.get("TurnStartParams", {}).get("properties", {})
                                         and "clientId" in user_message,
        "codex.exclude_tmp_keys": {"excludeSlashTmp", "excludeTmpdirEnvVar"} <= set(workspace_write),
        "codex.developer_instructions": "developerInstructions" in definitions.get("ThreadStartParams", {}).get("properties", {}),
        "codex.permission_profiles": profile.returncode == 0,
    }


def _probe_claude(executable: str, scratch: Path) -> dict[str, bool]:
    environment = _scratch_environment(executable, scratch)
    help_text = _run([executable, "--help"], environment, scratch).stdout
    record = scratch / "hook-env.txt"
    hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command":
            f"env | grep '^CLAUDE_CODE_MESSAGING_SOCKET=' > '{record}'; echo started >> '{record}'"}]}]}}
    # Unauthenticated in a scratch config: the CLI starts, runs SessionStart,
    # and stops at the API. No model turn, nothing outside the scratch dir.
    _run([executable, "-p", "capability probe", "--output-format", "json", "--settings", json.dumps(hook)],
         environment, scratch)
    fired = record.read_text(encoding="utf-8") if record.is_file() else ""
    try:
        relay = _probe_claude_relay_admission(executable, scratch)
    except (OSError, subprocess.SubprocessError, ValueError):
        relay = False
    return {
        "claude.inbox_relay_admission": relay,
        "claude.settings_flag": "--settings" in help_text,
        "claude.mcp_config": "--mcp-config" in help_text,
        "claude.append_system_prompt": "--append-system-prompt" in help_text,
        "claude.system_prompt_snapshot": "--system-prompt-snapshot" in help_text,
        "claude.inline_settings_hooks": "started" in fired,
        "claude.inbox_socket_env": "CLAUDE_CODE_MESSAGING_SOCKET=" in fired,
    }


def _probe_codex_stage3(executable: str, scratch: Path, environment: dict[str, str]) -> dict[str, bool]:
    """Stage 3 (Codex), hermetic, no model turn - the two facts the launch depends on.

    - `codex.server_policy_applies`: today's permission flags on the
      app-server's command line give a thread started the way the multiplexer
      forwards it (permission parameters removed) exactly today's policy.
    - `codex.remote_tui_launch_line`: the EXACT TUI line the supervisor builds
      (today's launch line, overrides removed, `--remote` added) starts and
      initializes against the multiplexer - fresh, and resuming a real thread.
      The owner's first Stage 3 launch died on that line ("overrides are not
      supported with --remote"); the gate now runs it before a session may.
    """
    import pty
    import select
    import time
    from harness import codex_app_server
    from harness.interactive_supervisor import _remote_tui_command
    workspace, extra = scratch / "s3-workspace", scratch / "s3-extra"
    workspace.mkdir(parents=True, exist_ok=True)
    extra.mkdir(parents=True, exist_ok=True)
    flags = ["-c", "model_reasoning_effort=low", "-c", "approval_policy=never", "-c", "sandbox_mode=workspace-write",
             "-c", "sandbox_workspace_write.writable_roots=" + json.dumps([str(workspace), str(extra)]),
             "-c", "sandbox_workspace_write.network_access=true"]
    expected = {"approvalPolicy": "never", "writableRoots": [str(workspace), str(extra)], "networkAccess": True}
    server = subprocess.Popen([executable, "app-server", "--listen", "stdio://", *flags], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment, cwd=str(scratch), text=True,
                              start_new_session=True)
    thread_id, policy_ok = "", False
    try:
        def call(identifier: int, method: str, params: dict) -> dict:
            server.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
            server.stdin.flush()
            deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                line = server.stdout.readline()
                if not line:
                    break
                message = json.loads(line)
                if message.get("id") == identifier:
                    return message
            return {}
        call(0, "initialize", {"clientInfo": {"name": "harness-probe", "version": "1"}, "capabilities": {}})
        forwarded = codex_app_server.pin_policy({"method": "thread/start", "params": {
            "cwd": str(workspace), "approvalPolicy": "on-request", "sandbox": "read-only"}})
        result = call(1, "thread/start", forwarded["params"]).get("result") or {}
        thread_id = str((result.get("thread") or {}).get("id") or "")
        policy_ok = bool(result) and codex_app_server.policy_mismatch(expected, result) == ""
    finally:
        codex_app_server.end_process_group(server)

    def launches(mode: str) -> bool:
        runtime = Path(tempfile.mkdtemp(prefix="hnc-", dir="/tmp" if os.path.isdir("/tmp") else None))
        mux = codex_app_server.CodexMultiplexer(
            str(runtime / "t.sock"), [executable, "app-server", "--listen", "stdio://", *flags],
            environment=environment, cwd=str(scratch), admit=lambda pid: True, peer_pid=lambda connection: 1)
        today = [executable, *(["resume"] if mode == "resume" else ["--cd", str(workspace)]),
                 "--model", "gpt-5", *flags, *([thread_id] if mode == "resume" else []), "capability probe"]
        master, slave = pty.openpty()
        process = None
        try:
            mux.start()
            process = subprocess.Popen(_remote_tui_command(today, mux.socket_path), stdin=slave, stdout=slave,
                                       stderr=slave, env=environment, cwd=str(scratch), start_new_session=True)
            os.close(slave)
            slave = -1
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and process.poll() is None:
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        break
            return process.poll() is None and mux.initialized
        finally:
            codex_app_server.end_process_group(process)    # the node wrapper AND the native TUI
            if slave >= 0:
                os.close(slave)
            os.close(master)
            mux.stop()
            shutil.rmtree(runtime, ignore_errors=True)

    fresh = launches("fresh")
    resumed = bool(thread_id) and launches("resume")
    return {"codex.remote_tui_launch_line": fresh and resumed, "codex.server_policy_applies": policy_ok}


RELAY_DELIVERED = "Routed user message to queue"


def _probe_claude_relay_admission(executable: str, scratch: Path) -> bool:
    """Stage 3 (Claude): does the CLI deliver a message its own child relay posts?

    Hermetic, no model turn: a scratch config marked as already onboarded, in
    bypass-permissions mode (as managed agents run), whose SessionStart hook
    is this harness's own hook gate - which starts the real relay. The probe
    plays the supervisor and hands the relay one message; the CLI's debug log
    says whether it was routed to the session or held for approval.
    """
    import pty
    import select
    import socket
    import time
    config = scratch / "relay-config"
    config.mkdir(parents=True, exist_ok=True)
    (config / ".claude.json").write_text(json.dumps({
        "hasCompletedOnboarding": True, "theme": "dark", "bypassPermissionsModeAccepted": True,
        # Both spellings: temp space is a symlink on macOS (/var -> /private/var).
        "projects": {path: {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}
                     for path in {str(scratch), os.path.realpath(scratch)}},
    }), encoding="utf-8")
    debug = scratch / "relay-debug.log"
    handover = Path(tempfile.mkdtemp(prefix="hnp-", dir="/tmp" if os.path.isdir("/tmp") else None)) / "h.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(handover))
    listener.listen(1)
    listener.setblocking(False)
    environment = _scratch_environment(executable, scratch)
    environment.update(CLAUDE_CONFIG_DIR=str(config), HARNESS_INBOX_HANDOVER=str(handover),
                       HARNESS_MANAGED_SESSION="capability-probe")
    gate = f"{sys.executable} -E {Path(__file__).resolve().parent / 'hook_gate.py'} claude SessionStart"
    hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": gate}]}]}}
    master, slave = pty.openpty()
    process = subprocess.Popen([executable, "--debug-file", str(debug), "--permission-mode", "bypassPermissions",
                                "--settings", json.dumps(hook)], stdin=slave, stdout=slave, stderr=slave,
                               env=environment, cwd=str(scratch), start_new_session=True)
    os.close(slave)
    relay = None
    try:
        deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
        while relay is None and time.monotonic() < deadline and process.poll() is None:
            readable, _, _ = select.select([master, listener], [], [], 0.2)
            if master in readable:
                try:
                    os.read(master, 65536)       # a CLI that cannot draw stalls before its hooks
                except OSError:
                    break
            if listener in readable:
                relay, _ = listener.accept()
        if relay is None:
            return False
        relay.setblocking(True)
        relay.settimeout(10)
        relay.makefile("rb").readline()          # the relay's hello
        relay.sendall(b'{"id": "probe", "text": "harness capability probe"}\n')
        relay.makefile("rb").readline()          # posted
        settle = time.monotonic() + 5
        while time.monotonic() < settle:
            if RELAY_DELIVERED in (debug.read_text(encoding="utf-8", errors="replace") if debug.is_file() else ""):
                return True
            if select.select([master], [], [], 0.2)[0]:
                try:
                    os.read(master, 65536)
                except OSError:
                    break
        return False
    finally:
        if relay is not None:
            relay.close()                        # the relay ends with its supervisor connection
        listener.close()
        from harness.codex_app_server import end_process_group
        end_process_group(process)                   # the CLI and anything it started
        os.close(master)
        shutil.rmtree(handover.parent, ignore_errors=True)


PROBES: dict[str, Callable[[str, Path], dict[str, bool]]] = {"codex": _probe_codex, "claude": _probe_claude}


def capabilities(home: Path, provider: str, *, refresh: bool = False,
                 source_environment: dict[str, str] | None = None) -> dict[str, Any]:
    """The cached capability record for the installed `provider` CLI, probing once per binary identity."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    identity = binary_identity(provider, source_environment=source_environment)
    if not identity["path"]:
        return {"identity": identity, "static": {}, "live": {}, "probed_at": "", "missing": True}
    path = cache_path(home, identity)
    record: dict[str, Any] | None = None
    if not refresh and path.is_file():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        if record is not None and record.get("probe_version") != PROBE_VERSION:
            # Recorded by an older probe that did not ask every static question:
            # probe again, rather than read a missing item as merely "unproven".
            record = None
    if record is None:
        with tempfile.TemporaryDirectory(prefix="harness-cli-probe-") as scratch:
            try:
                static = PROBES[provider](identity["path"], Path(scratch))
            except (OSError, subprocess.SubprocessError) as error:
                static = {"probe_error": str(error)[:300]}
        record = {"identity": identity, "static": static, "probed_at": _now(), "probe_version": PROBE_VERSION}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    live_path = cache_path(home, identity, live=True)
    try:
        live = json.loads(live_path.read_text(encoding="utf-8")).get("results", {}) if live_path.is_file() else {}
    except (OSError, ValueError):
        live = {}
    return {**record, "live": live}


def record_live(home: Path, provider: str, results: dict[str, Any], *, auth_mode: str,
                source_environment: dict[str, str] | None = None) -> Path:
    """Store the spike's live results for the installed binary. Values: True, False or UNPROVEN."""
    identity = binary_identity(provider, source_environment=source_environment)
    for name, value in results.items():
        if value not in (True, False, UNPROVEN):
            raise ValueError(f"live result for {name} must be true, false or {UNPROVEN}")
    path = cache_path(home, identity, live=True)
    try:
        evidence = (json.loads(path.read_text(encoding="utf-8")).get("evidence") or {}) if path.is_file() else {}
    except (OSError, ValueError):
        evidence = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"identity": identity, "auth_mode": auth_mode, "recorded_at": _now(),
                                "results": results, "evidence": evidence}, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def record_live_item(home: Path, provider: str, name: str, value: Any, evidence: str, *,
                     source_environment: dict[str, str] | None = None) -> Path:
    """Record ONE live result, with the evidence that proves it, keeping every other result."""
    if value not in (True, False, UNPROVEN):
        raise ValueError(f"live result for {name} must be true, false or {UNPROVEN}")
    if value is not UNPROVEN and not str(evidence or "").strip():
        raise ValueError("a live result needs its evidence")
    identity = binary_identity(provider, source_environment=source_environment)
    path = cache_path(home, identity, live=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        existing = {}
    results = dict(existing.get("results") or {})
    evidence_map = dict(existing.get("evidence") or {})
    results[name] = value
    evidence_map[name] = str(evidence)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"identity": identity, "auth_mode": existing.get("auth_mode", "owner-test-copy"),
                                "recorded_at": _now(), "results": results, "evidence": evidence_map},
                               indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def capability(record: dict[str, Any], name: str) -> bool:
    """True only when the capability is proven; UNPROVEN, false and absent all gate the same."""
    if name in record.get("static", {}):
        return record["static"][name] is True
    return record.get("live", {}).get(name) is True


def stage_status(home: Path, stage: str, provider: str, *, settings: dict[str, Any] | None = None,
                 records: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Whether `stage` runs for a `provider` session, and if not, the plain reason.

    The flag and its dependencies come from settings (`global_settings.plumbing_status`);
    the capabilities come from this module. Any missing piece means today's path.
    """
    if stage not in STAGE_REQUIREMENTS:
        raise ValueError(f"unknown plumbing stage: {stage}")
    flags = global_settings.plumbing_status(home, settings=settings)
    flag = flags[stage]
    if not flag["enabled"]:
        return {"enabled": False, "reason": flag["reason"]}
    needed = STAGE_REQUIREMENTS[stage].get(provider)
    if needed is None:
        return {"enabled": False, "reason": f"{stage} does not apply to {provider} sessions"}
    record = (records or {}).get(provider) or capabilities(home, provider)
    if record.get("missing"):
        return {"enabled": False, "reason": f"the {provider} CLI is not installed"}
    missing = [name for name in needed if not capability(record, name)]
    proving = (settings if settings is not None else global_settings.load(home)).get("plumbing", {}).get(
        global_settings.PROVING_FLAG) is True
    if missing and proving:
        unproven = [name for name in missing if name not in record.get("static", {})
                    and record.get("live", {}).get(name) is not False]
        if unproven == missing:
            return {"enabled": True, "reason": f"PLUMBING PROVING {stage} unproven: " + ", ".join(missing)}
    if missing:
        def label(name: str) -> str:
            if name in record.get("static", {}):
                return "false"
            return "false" if record.get("live", {}).get(name) is False else UNPROVEN
        return {"enabled": False, "reason": f"PLUMBING FALLBACK {stage} " + ", ".join(
            f"{name}={label(name)}" for name in missing)}
    return {"enabled": True, "reason": ""}


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Show the plumbing capabilities of the installed agent CLIs")
    parser.add_argument("--home", required=True, help="the manager home that caches the results")
    parser.add_argument("--refresh", action="store_true", help="probe again even when cached")
    args = parser.parse_args(argv)
    home = Path(args.home)
    out = {provider: capabilities(home, provider, refresh=args.refresh) for provider in PROVIDERS}
    out["stages"] = {stage: {provider: stage_status(home, stage, provider, records=out)
                             for provider in PROVIDERS} for stage in STAGE_REQUIREMENTS}
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

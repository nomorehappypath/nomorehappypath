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
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from harness import global_settings

PROVIDERS = ("codex", "claude")
CACHE_DIRECTORY = "cli-capabilities"
PROBE_TIMEOUT_SECONDS = 60
UNPROVEN = "UNPROVEN"

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
        "claude.inbox_token_admission",     # G-4
        "claude.inbox_receipt",             # G-4
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
                  "codex.tui_via_multiplexer", "codex.remote_renders_harness_turns", "codex.turn_receipt",
                  "codex.isolated_from_daemon", "cross_agent_isolation", "launch_surfaces_tamperproof"),
    },
    "stage3_claude_socket_delivery": {
        "claude": ("claude.inbox_socket_env", "claude.inbox_token_admission", "claude.inbox_receipt",
                   "claude.socket_authority_parity", "confined_socket_bind", "cross_agent_isolation",
                   "launch_surfaces_tamperproof"),
    },
    "stage4_system_layer_directives": {
        "claude": ("claude.append_system_prompt", "claude.append_prompt_on_resume"),
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
    return {
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
    return {
        "claude.settings_flag": "--settings" in help_text,
        "claude.mcp_config": "--mcp-config" in help_text,
        "claude.append_system_prompt": "--append-system-prompt" in help_text,
        "claude.system_prompt_snapshot": "--system-prompt-snapshot" in help_text,
        "claude.inline_settings_hooks": "started" in fired,
        "claude.inbox_socket_env": "CLAUDE_CODE_MESSAGING_SOCKET=" in fired,
    }


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
    if record is None:
        with tempfile.TemporaryDirectory(prefix="harness-cli-probe-") as scratch:
            try:
                static = PROBES[provider](identity["path"], Path(scratch))
            except (OSError, subprocess.SubprocessError) as error:
                static = {"probe_error": str(error)[:300]}
        record = {"identity": identity, "static": static, "probed_at": _now()}
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"identity": identity, "auth_mode": auth_mode, "recorded_at": _now(),
                                "results": results}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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

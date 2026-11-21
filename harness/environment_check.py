# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""NMHP checks the project's setup itself, before the contract reaches the owner (owner 2026-11-21).

Lease: the app called `claude`, every review used stand-ins, and the owner's test failed because NMHP started his app
without his Claude sign-in. The owner's order: NMHP reads the requirements, drafts the contract, knows from it the setup
the project needs (agents, folders, languages, compilers, tools, libraries, the AI tools the app itself calls) and checks
all of it BEFORE the contract is shown to him; any problem is a card to resolve first. Later, the app's own real run
(real Claude/Codex calls through the app's code, observed by NMHP) must pass before any review and again before release.

Three stages, one record per task (state["environment_checks"][task]):
  contract  every item of the needs list + the project's agents; holds the contract back from the owner until it passes
  review    the app's real-run command on the commit under review (only when the app calls an AI tool)
  release   the AI-tool calls and the real run again, fresh, on the release commit; a product check that holds the release
Every check runs here, in the manager, as the owner, in his own environment (owner_env) — never in an agent.
"""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
from typing import Any

from harness import git_process, owner_env

STAGES = ("contract", "review", "release")
AI_TOOLS = ("claude", "codex", "gemini")
LIBRARY_LANGUAGES = ("python", "node")
ITEM_SECONDS = 20
AI_CALL_SECONDS = 120
REAL_RUN_SECONDS = 600
AI_PROMPT = "Reply with the single word OK"
CARD_PREFIX = "setup:"
CHECK_AGAIN = "Check again"
OWNER, BUILDER = "owner", "builder"
TEST_SUITE_IDENTITY = "test-suite"

NEEDS_FORMAT = (
    "--needs takes one JSON object, the setup this contract needs: "
    '{"ai_tools": ["claude", "codex"], "folders": [{"path": "/abs/folder", "access": "read"}], '
    '"programs": [{"name": "python3", "min_version": "3.11"}], "libraries": [{"language": "python", "name": "pypdf"}], '
    '"real_run": "python3 -m app.realcheck"}. ai_tools: the AI tools the APP itself calls (claude, codex, gemini; [] when none). '
    "folders: every folder the work reads or writes outside the project (access read or write). programs: languages, compilers, "
    "build and test tools (min_version optional). libraries: per language (python or node). real_run: for an app that calls an AI "
    "tool, one command that runs the app's own code end to end with a small sample input and makes a real call of every listed "
    "AI tool; build it first. Agents are added by NMHP itself."
)


# ---------------------------------------------------------------------------------------------------------------- needs list

def parse_needs(raw: Any) -> dict[str, Any]:
    """The needs list, normalized; ValueError with the exact format when it is missing or malformed."""
    if isinstance(raw, str):
        if not raw.strip():
            raise ValueError(f"the contract needs its setup list: {NEEDS_FORMAT}")
        try:
            raw = json.loads(raw)
        except ValueError as error:
            raise ValueError(f"the setup list is not valid JSON ({error}): {NEEDS_FORMAT}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"the setup list must be a JSON object: {NEEDS_FORMAT}")
    unknown = set(raw) - {"ai_tools", "folders", "programs", "libraries", "real_run"}
    if unknown:
        raise ValueError(f"the setup list has unknown keys {sorted(unknown)}: {NEEDS_FORMAT}")
    tools = raw.get("ai_tools", [])
    if not isinstance(tools, list) or any(str(tool).strip().lower() not in AI_TOOLS for tool in tools):
        raise ValueError(f"ai_tools lists only {', '.join(AI_TOOLS)} ([] when the app calls none): {NEEDS_FORMAT}")
    folders = []
    for item in raw.get("folders", []) or []:
        item = {"path": item} if isinstance(item, str) else item
        if not isinstance(item, dict) or not str(item.get("path") or "").startswith(("/", "~")):
            raise ValueError(f"each folder needs an absolute path: {NEEDS_FORMAT}")
        access = str(item.get("access") or "read").strip().lower()
        if access not in {"read", "write"}:
            raise ValueError(f"a folder's access is read or write: {NEEDS_FORMAT}")
        folders.append({"path": os.path.expanduser(str(item["path"]).strip()), "access": access})
    programs = []
    for item in raw.get("programs", []) or []:
        item = {"name": item} if isinstance(item, str) else item
        name = str((item or {}).get("name") or "").strip() if isinstance(item, dict) else ""
        if not name or "/" in name and not name.startswith("/") or any(c.isspace() for c in name):
            raise ValueError(f"each program needs a command name (python3, node, swift, cc …): {NEEDS_FORMAT}")
        version = str(item.get("min_version") or "").strip()
        if version and not re.fullmatch(r"\d+(\.\d+)*", version):
            raise ValueError(f"min_version is a dotted number such as 3.11: {NEEDS_FORMAT}")
        programs.append({"name": name, "min_version": version})
    libraries = []
    for item in raw.get("libraries", []) or []:
        if not isinstance(item, dict):
            raise ValueError(f"each library is {{\"language\": …, \"name\": …}}: {NEEDS_FORMAT}")
        language, name = str(item.get("language") or "").strip().lower(), str(item.get("name") or "").strip()
        if language not in LIBRARY_LANGUAGES or not re.fullmatch(r"[A-Za-z0-9@][A-Za-z0-9._/@-]*", name or "-"):
            raise ValueError(f"a library's language is {' or '.join(LIBRARY_LANGUAGES)} and it has a plain name (list other "
                             f"languages' libraries through real_run): {NEEDS_FORMAT}")
        libraries.append({"language": language, "name": name})
    real_run = str(raw.get("real_run") or "").strip()
    if len(real_run) > 1000:
        raise ValueError("real_run must be 1000 characters or fewer")
    return {"ai_tools": sorted({str(tool).strip().lower() for tool in tools}), "folders": folders, "programs": programs,
            "libraries": libraries, "real_run": real_run}


def needs_hash(needs: dict[str, Any]) -> str:
    """The identity of one needs list: any change re-runs every check."""
    return hashlib.sha256(json.dumps(needs, sort_keys=True).encode()).hexdigest()[:16]


def real_run_required(state: dict[str, Any], task: str, needs: dict[str, Any] | None) -> bool:
    """The app's own real run applies to a task that builds or changes a runnable program whose app calls an AI tool."""
    if not needs or not needs.get("ai_tools"):
        return False
    if (state.get("task_document_only") or {}).get(task):
        return False
    return str((state.get("task_kinds") or {}).get(task) or "development") not in {"spec", "analysis"}


def needs_problem(state: dict[str, Any], task: str, needs: dict[str, Any]) -> str:
    """A needs list this task cannot use: a real run is required and none is named."""
    if real_run_required(state, task, needs) and not needs.get("real_run"):
        return ("this app calls " + ", ".join(needs["ai_tools"]) + ": the setup list must name its real_run command (one command "
                "that runs the app's own code with a small sample and makes a real call of each of them); build it first. " + NEEDS_FORMAT)
    return ""


# ---------------------------------------------------------------------------------------------------------------- state

def entry(state: dict[str, Any], task: str) -> dict[str, Any]:
    """This task's environment-check record (created empty)."""
    return state.setdefault("environment_checks", {}).setdefault(task, {"needs": None, "needs_hash": "", "runs": {}})


def record_needs(state: dict[str, Any], task: str, needs: dict[str, Any], agent_id: str, at: str) -> bool:
    """Store the needs list; a changed list discards every earlier result (Codex plan check R1-3). Returns True when it changed."""
    record = entry(state, task)
    digest = needs_hash(needs)
    if record.get("needs_hash") == digest:
        return False
    record.update({"needs": needs, "needs_hash": digest, "needs_by": agent_id, "needs_at": at, "runs": {}})
    return True


def run_of(state: dict[str, Any], task: str, stage: str) -> dict[str, Any]:
    """The latest run of one stage (empty when none)."""
    return dict((((state.get("environment_checks") or {}).get(task) or {}).get("runs") or {}).get(stage) or {})


def request(state: dict[str, Any], task: str, stage: str, at: str, commit: str = "", identity: str | None = None, force: bool = False) -> dict[str, Any]:
    """Ask for a run of stage (on commit); a run already checking, or already passed for the same commit and identity, is reused."""
    record = entry(state, task)
    current = (record.get("runs") or {}).get(stage) or {}
    same = current.get("commit", "") == commit and current.get("needs_hash") == record.get("needs_hash")
    if not force and same and current.get("status") == "checking":
        return current
    if not force and same and current.get("status") == "passed" and (identity is None or bool(identity) and current.get("identity") == identity):
        return current
    run = {"stage": stage, "status": "checking", "commit": commit, "needs_hash": record.get("needs_hash", ""),
           "requested_at": at, "finished_at": "", "identity": "", "items": [], "run_id": secrets.token_hex(6)}
    record.setdefault("runs", {})[stage] = run
    return run


def passed(state: dict[str, Any], task: str, stage: str, commit: str = "", identity: str | None = None) -> bool:
    """The stage passed for this needs list (and commit; and, when a gate passes `identity`, for that same identity — an empty
    identity means the current setup could not be read, so nothing passes: Codex review R1-5)."""
    record = (state.get("environment_checks") or {}).get(task) or {}
    run = (record.get("runs") or {}).get(stage) or {}
    return (run.get("status") == "passed" and run.get("needs_hash") == record.get("needs_hash") and run.get("commit", "") == commit
            and (identity is None or bool(identity) and run.get("identity") == identity))


def failed_lines(run: dict[str, Any]) -> list[str]:
    """One plain line per failed item of a run."""
    return [f"{item['label']}: {item['detail']}" for item in run.get("items") or [] if not item.get("ok")]


# ---------------------------------------------------------------------------------------------------------------- identity

def identity(root: Path, task: str, workspace: str, needs: dict[str, Any] | None) -> str:
    """What a result depends on besides the needs list and commit (Codex plan check R1-4, R2-1): the owner's environment
    (fingerprint only), the declared test environment, the AI tools' executables and versions. "" when it cannot be read:
    the gates then hold and run the check again, which shows the failure (Codex review R1-5). Inside the test suite, which
    never reads the real account, it is the fixed TEST_SUITE_IDENTITY."""
    try:
        values = owner_env.read()
    except owner_env.OwnerEnvNotInTests:
        return TEST_SUITE_IDENTITY
    except owner_env.OwnerEnvError:
        return ""
    parts = {"owner_env": owner_env.fingerprint(values)}
    try:
        from harness import test_env
        resolved = test_env.resolve(root, workspace) if workspace and Path(workspace).is_dir() else None
        parts["test_env"] = json.dumps(test_env.identity_fields(resolved), sort_keys=True)
    except (ValueError, OSError):
        parts["test_env"] = "unreadable"
    for tool in (needs or {}).get("ai_tools") or []:
        path = tool_executable(tool, values)
        try:
            stamp = str(os.stat(path).st_mtime) if path else ""
        except OSError:
            stamp = ""
        parts[f"tool:{tool}"] = f"{path}|{stamp}"
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------------------------- the checks

def tool_executable(tool: str, values: dict[str, str]) -> str:
    """The executable the owner's app would run for tool: found on HIS PATH first, then where NMHP finds that CLI."""
    found = shutil.which(tool, path=values.get("PATH", ""))
    if found:
        return found
    if tool in {"claude", "codex", "gemini"}:
        try:
            from harness import global_settings
            return global_settings.provider_executable(tool, source_environment=values)
        except (ImportError, OSError, ValueError):
            return ""
    return ""


def _item(kind: str, label: str, ok: bool, detail: str, who: str) -> dict[str, Any]:
    """One checked item."""
    return {"kind": kind, "label": label, "ok": bool(ok), "detail": detail[:600], "who": who}


def _last_line(text: str) -> str:
    """The last non-empty line of a tool's output (its own words), shortened."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "")[:300]


def check_ai_tool(tool: str, values: dict[str, str], scrub: Callable[[str], str]) -> dict[str, Any]:
    """One real, minimal call of tool in the owner's environment, as his app would make it."""
    label = {"claude": "Claude for your app", "codex": "Codex for your app", "gemini": "Gemini for your app"}[tool]
    executable = tool_executable(tool, values)
    if not executable:
        return _item("ai_tool", label, False, f"the `{tool}` command was not found on this computer; install it, then press Check again", OWNER)
    with tempfile.TemporaryDirectory(prefix="nmhp-setup-") as folder:
        if tool == "claude":
            argv = [executable, "-p", AI_PROMPT, "--output-format", "json"]
        elif tool == "codex":
            argv = [executable, "exec", "--skip-git-repo-check", AI_PROMPT]
        else:
            argv = [executable, "-p", AI_PROMPT]
        code, output, timed_out = owner_env.run(argv, cwd=folder, env=values, timeout=AI_CALL_SECONDS)
    if timed_out:
        return _item("ai_tool", label, False, f"`{tool}` did not answer within {AI_CALL_SECONDS} seconds", OWNER)
    if tool == "claude":
        try:
            data = json.loads(output[output.find("{"):]) if "{" in output else {}
        except ValueError:
            data = {}
        if code == 0 and data and not data.get("is_error"):
            return _item("ai_tool", label, True, "answered", OWNER)
        words = scrub(str(data.get("result") or "") or _last_line(output) or f"it exited with code {code}")
        return _item("ai_tool", label, False, f"Claude did not answer for your app: {words}. In Terminal, run `claude` and sign in "
                     "(or set your long-lasting sign-in), then press Check again", OWNER)
    if code == 0:
        return _item("ai_tool", label, True, "answered", OWNER)
    words = scrub(_last_line(output) or f"it exited with code {code}")
    return _item("ai_tool", label, False, f"{tool.title()} did not answer for your app: {words}. In Terminal, run `{tool}` and sign in, "
                 "then press Check again", OWNER)


def check_folder(item: dict[str, str], workspace: str) -> dict[str, Any]:
    """The folder exists and can be read (or written); on Linux, where the agents' reads are limited, it is inside their grant."""
    path, access = Path(item["path"]), item["access"]
    label = f"Folder {item['path']}"
    if not path.is_dir():
        return _item("folder", label, False, "this folder does not exist on this computer; create it or correct the setup list", OWNER)
    if not os.access(path, os.R_OK | os.X_OK) or access == "write" and not os.access(path, os.W_OK):
        return _item("folder", label, False, f"NMHP cannot {access} this folder; give it {access} access", OWNER)
    if agent_reads_confined():
        allowed = [p for p in os.environ.get("HARNESS_AGENT_READ_PATHS", "").split(os.pathsep) if p]
        roots = [str(Path(p).resolve()) for p in allowed + ([workspace] if workspace else [])]
        real = str(path.resolve())
        if not any(real == root or real.startswith(root + "/") for root in roots):
            return _item("folder", label, False, "the agents cannot read this folder on this computer: add it to HARNESS_AGENT_READ_PATHS for "
                         "NoMoreHappyPath, restart it, then press Check again", OWNER)
    return _item("folder", label, True, f"{access} access", OWNER)


def agent_reads_confined() -> bool:
    """The agents' reads are limited to their grant on this platform (Linux bubblewrap), so a needed folder must be inside it."""
    from harness import platform_support
    return bool(getattr(platform_support.agent_confinement(), "confines_reads", False))


def _version_tuple(text: str) -> tuple[int, ...]:
    """A dotted version as numbers."""
    return tuple(int(part) for part in text.split(".") if part.isdigit())


def check_program(item: dict[str, str], values: dict[str, str]) -> dict[str, Any]:
    """The program is found (on the owner's PATH) and, when asked, at least the minimum version."""
    name, minimum = item["name"], item.get("min_version", "")
    label = f"Program {name}" + (f" {minimum}+" if minimum else "")
    found = name if name.startswith("/") and os.access(name, os.X_OK) else shutil.which(name, path=values.get("PATH", ""))
    if not found:
        return _item("program", label, False, f"`{name}` was not found on this computer; install it, then press Check again", OWNER)
    if not minimum:
        return _item("program", label, True, found, OWNER)
    with tempfile.TemporaryDirectory(prefix="nmhp-setup-") as folder:
        _, output, timed_out = owner_env.run([found, "--version"], cwd=folder, env=values, timeout=ITEM_SECONDS)
    match = re.search(r"\d+(?:\.\d+)+", output)
    if timed_out or not match:
        return _item("program", label, False, f"could not read the version of `{name}` (`{name} --version`)", OWNER)
    if _version_tuple(match.group(0)) < _version_tuple(minimum):
        return _item("program", label, False, f"`{name}` is version {match.group(0)}; {minimum} or newer is needed. Update it, then press Check again", OWNER)
    return _item("program", label, True, f"{found} {match.group(0)}", OWNER)


PYTHON_PROBE = ("import importlib.metadata as m, importlib.util as u, sys\n"
                "n = sys.argv[1]\n"
                "try:\n    m.version(n); sys.exit(0)\nexcept Exception:\n    pass\n"
                "sys.exit(0 if u.find_spec(n.replace('-', '_')) else 1)\n")


def check_library(item: dict[str, str], values: dict[str, str], workspace: str, python: str) -> dict[str, Any]:
    """The library is installed for the project: Python in the project's declared interpreter (else the owner's python3), Node
    from the project folder."""
    language, name = item["language"], item["name"]
    label = f"{language.title()} library {name}"
    cwd = workspace if workspace and Path(workspace).is_dir() else tempfile.gettempdir()
    if language == "python":
        interpreter = python or shutil.which("python3", path=values.get("PATH", "")) or ""
        argv = [interpreter, "-c", PYTHON_PROBE, name]
    else:
        interpreter = shutil.which("node", path=values.get("PATH", "")) or ""
        argv = [interpreter, "-e", "require.resolve(process.argv[1])", name]
    if not interpreter:
        return _item("library", label, False, f"no {language} interpreter was found to check it", OWNER)
    code, _, timed_out = owner_env.run(argv, cwd=cwd, env=values, timeout=ITEM_SECONDS)
    if code == 0 and not timed_out:
        return _item("library", label, True, "installed", BUILDER)
    return _item("library", label, False, f"not installed for this project ({interpreter}); the builder installs it", BUILDER)


def check_agents(root: Path) -> list[dict[str, Any]]:
    """Every agent the project runs answers through its own sign-in (the existing connection test, a real call)."""
    try:
        from harness import control
        sessions = control.snapshot(root).get("sessions", [])
        active = [s for s in sessions if s.get("status") in control.ACTIVE_STATUSES]
    except (OSError, ValueError, ImportError):
        return [_item("agent", "The project's agents", False, "the agents' list could not be read", OWNER)]
    seen: dict[tuple[str, str, str], str] = {}
    for session in active:
        key = (str(session.get("provider") or ""), str(session.get("model") or ""), str(session.get("effort") or ""))
        if key[0] and key not in seen:
            seen[key] = str(session.get("kind") or "agent")
    items = []
    for (provider, model, effort), kind in sorted(seen.items()):
        label = f"Agent {provider.title()} ({model})"
        try:
            AGENT_CHECK(provider, model, effort)
            items.append(_item("agent", label, True, "answered", OWNER))
        except (ValueError, OSError) as error:
            items.append(_item("agent", label, False, f"{error}", OWNER))
    return items


def _real_agent_check(provider: str, model: str, effort: str) -> None:
    """The Settings page's connection test (a real one-word request through the agents' own sign-in)."""
    from harness import global_settings, project_registry
    with tempfile.TemporaryDirectory(prefix="nmhp-setup-") as folder:
        global_settings.test_connection(project_registry.default_home(), provider, model, effort, Path(folder))


AGENT_CHECK: Callable[[str, str, str], None] = _real_agent_check

RECORDER = """#!/bin/sh
call="{calls}/{tool}.$$"
printf '%s\\0' "$@" > "$call.args"
"{real}" "$@" > "$call.out"
code=$?
cat "$call.out"
printf '%s' "$code" > "$call.code"
exit $code
"""
# Codex review R1-3: only a call that asks the AI something counts (never --version, help, auth, config ...), and only an
# answer counts (exit 0, something printed, and for Claude no is_error in its JSON)
NOT_INFERENCE = {"--version", "-v", "-V", "--help", "-h", "help", "auth", "login", "logout", "setup-token", "config", "mcp",
                 "doctor", "update", "install", "completion", "features", "sandbox", "debug", "apply", "resume", "plugin", "migrate-installer"}


def is_inference(tool: str, args: list[str]) -> bool:
    """The recorded arguments ask the AI tool for an answer (not its version, help, sign-in or settings)."""
    if not args or args[0] in NOT_INFERENCE or any(arg in {"--version", "--help", "-h"} for arg in args):
        return False
    if tool == "claude":
        return any(arg in {"-p", "--print"} for arg in args)
    if tool == "codex":
        return args[0] in {"exec", "e"}
    return any(arg in {"-p", "--prompt"} for arg in args) or not args[0].startswith("-")


def answered(tool: str, code: str, output: str) -> bool:
    """The call returned an answer: exit 0, some output, and for Claude no error flag in its JSON envelope."""
    if code.strip() != "0" or not output.strip():
        return False
    return not (tool == "claude" and re.search(r'"is_error"\s*:\s*true', output))


def real_run(command: str, tools: list[str], workspace: str, values: dict[str, str], scrub: Callable[[str], str],
             python_dir: str = "") -> dict[str, Any]:
    """Run the app's own real-run command in the owner's environment, with a recorder for each AI tool first on PATH: it
    passes when the command exits 0 AND every listed tool was really called and answered (Codex plan check R1-2)."""
    label = "Your app's real run"
    if not command:
        return _item("real_run", label, False, "the setup list names no real_run command; the builder adds it", BUILDER)
    if not workspace or not Path(workspace).is_dir():
        return _item("real_run", label, False, "the task's work folder is missing", BUILDER)
    with tempfile.TemporaryDirectory(prefix="nmhp-realrun-") as folder:
        shims, calls_dir = Path(folder) / "bin", Path(folder) / "calls"
        shims.mkdir()
        calls_dir.mkdir()
        missing = []
        for tool in tools:
            executable = tool_executable(tool, values)
            if not executable:
                missing.append(tool)
                continue
            shim = shims / tool
            shim.write_text(RECORDER.format(real=executable.replace('"', '\\"'), tool=tool, calls=str(calls_dir)))
            shim.chmod(0o755)
        if missing:
            return _item("real_run", label, False, "not found on this computer: " + ", ".join(missing), OWNER)
        env = dict(values)
        env["PATH"] = os.pathsep.join(part for part in (str(shims), python_dir, values.get("PATH", "")) if part)
        env["TMPDIR"] = folder
        code, output, timed_out = owner_env.run(["/bin/sh", "-c", command], cwd=workspace, env=env, timeout=REAL_RUN_SECONDS)
        called, answered_by = set(), set()
        for args_file in calls_dir.glob("*.args"):
            tool = args_file.name.split(".", 1)[0]
            args = [arg for arg in args_file.read_bytes().decode("utf-8", "replace").split("\0")[:-1]]
            if not is_inference(tool, args):
                continue
            called.add(tool)
            stem = str(args_file)[: -len(".args")]
            code_text = Path(stem + ".code").read_text() if Path(stem + ".code").exists() else ""
            out_text = Path(stem + ".out").read_text(errors="replace") if Path(stem + ".out").exists() else ""
            if answered(tool, code_text, out_text):
                answered_by.add(tool)
    if timed_out:
        return _item("real_run", label, False, f"`{command}` did not finish within {REAL_RUN_SECONDS} seconds", BUILDER)
    if code != 0:
        return _item("real_run", label, False, f"`{command}` failed (exit {code}): {scrub(_last_line(output))}", BUILDER)
    not_called = [tool for tool in tools if tool not in called]
    if not_called:
        return _item("real_run", label, False, f"`{command}` finished but never asked {', '.join(not_called)} for an answer (a "
                     "stand-in, a mocked call or a version/help/sign-in call does not count; the app must run the real command by "
                     "name from PATH and ask it something)", BUILDER)
    failed = [tool for tool in tools if tool not in answered_by]
    if failed:
        return _item("real_run", label, False, f"`{command}` finished but {', '.join(failed)} did not answer it (the app hid the "
                     f"error): {scrub(_last_line(output))}", BUILDER)
    return _item("real_run", label, True, f"`{command}` ran and called {', '.join(tools)} for real", BUILDER)


# ---------------------------------------------------------------------------------------------------------------- running

_running: set[tuple[str, str, str]] = set()
_running_lock = threading.Lock()


def _git_head(workspace: str) -> str:
    """The work folder's current commit ("" when unreadable)."""
    try:
        completed = git_process.run(["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _tracked_changes(workspace: str) -> bool:
    """The work folder has uncommitted changes to tracked files."""
    try:
        completed = git_process.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=workspace,
                                    capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return True
    return completed.returncode != 0 or bool(completed.stdout.strip())


def execute(root: Path, state: dict[str, Any], task: str, stage: str, run: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Run every item of one stage from a snapshot of the board; returns (items, identity). Never inside the board lock."""
    record = (state.get("environment_checks") or {}).get(task) or {}
    needs = record.get("needs") or {}
    workspace = str((state.get("task_workspaces") or {}).get(task) or "")
    try:
        values = owner_env.read(fresh=True)
    except owner_env.OwnerEnvError as error:
        return [_item("owner_env", "Your own setup (as in your Terminal)", False, f"{error}", OWNER)], ""
    scrub = owner_env.redactor(values)
    python, python_dir, test_scrub = "", "", (lambda text: text)
    try:
        from harness import test_env
        resolved = test_env.resolve(root, workspace) if workspace and Path(workspace).is_dir() else None
        if resolved is not None:
            python, python_dir = resolved.python, resolved.python_dir
            values = test_env.apply(values, resolved)
            test_scrub = lambda text: test_env.scrub(text, resolved)
    except (ValueError, OSError) as error:
        return [_item("test_env", "The project's declared test environment", False, f"{error}", BUILDER)], ""
    both = lambda text: test_scrub(scrub(text))
    items: list[dict[str, Any]] = []
    tools = list(needs.get("ai_tools") or [])
    if stage == "contract":
        items += check_agents(root)
        items += [check_folder(item, workspace) for item in needs.get("folders") or []]
        items += [check_program(item, values) for item in needs.get("programs") or []]
        items += [check_library(item, values, workspace, python) for item in needs.get("libraries") or []]
        items += [check_ai_tool(tool, values, both) for tool in tools]
    elif stage in {"review", "release"}:
        if stage == "release":
            items += [check_ai_tool(tool, values, both) for tool in tools]
        head = _git_head(workspace)
        if run.get("commit") and head != run["commit"]:
            items.append(_item("real_run", "Your app's real run", False, f"the work folder is at {head[:12] or 'an unreadable commit'}, "
                               f"not {run['commit'][:12]}; it runs again on the next request", BUILDER))
        elif _tracked_changes(workspace):
            items.append(_item("real_run", "Your app's real run", False, "the work folder has uncommitted changes; commit them and request again", BUILDER))
        else:
            items.append(real_run(str(needs.get("real_run") or ""), tools, workspace, values, both, python_dir))
    return items, identity(root, task, workspace, needs)


def run_due(root: Path) -> list[str]:
    """Start (in the background) every requested run that is not already running. Returns the started task:stage names."""
    from harness import board
    try:
        if board.pause_state(root).get("status") not in {"active", None, ""}:
            return []
        state = board.snapshot(root)
    except (OSError, ValueError):
        return []
    started = []
    for task, record in (state.get("environment_checks") or {}).items():
        for stage, run in ((record or {}).get("runs") or {}).items():
            if run.get("status") != "checking":
                continue
            key = (str(Path(root)), task, stage)
            with _running_lock:
                if key in _running:
                    continue
                _running.add(key)
            thread = threading.Thread(target=_work, args=(root, task, stage, run, key), name=f"setup-check-{task}-{stage}", daemon=True)
            thread.start()
            started.append(f"{task}:{stage}")
    return started


def _work(root: Path, task: str, stage: str, run: dict[str, Any], key: tuple[str, str, str]) -> None:
    """One run, then its result recorded."""
    try:
        from harness import board
        items, ident = execute(root, board.snapshot(root), task, stage, run)
        board.record_environment_result(root, task, stage, str(run.get("run_id") or ""), items, ident)
    except Exception as error:  # a check never kills the worker; the failure becomes the run's own result
        try:
            from harness import board
            board.record_environment_result(root, task, stage, str(run.get("run_id") or ""),
                                            [_item("error", "The setup check", False, f"could not run: {str(error)[:300]}", OWNER)], "")
        except Exception:
            pass
    finally:
        with _running_lock:
            _running.discard(key)


def run_now(root: Path, task: str, stage: str) -> None:
    """Run the requested stage in this thread (tests and the release coordinator's own step)."""
    from harness import board
    state = board.snapshot(root)
    run = run_of(state, task, stage)
    if run.get("status") != "checking":
        return
    items, ident = execute(root, state, task, stage, run)
    board.record_environment_result(root, task, stage, str(run.get("run_id") or ""), items, ident)


def card_text(item: dict[str, Any], stage: str) -> tuple[str, str]:
    """(title, why) of the card for one failed item, saying who resolves it."""
    when = {"contract": "before your contract is shown to you", "review": "before any review", "release": "before the release is offered to you"}[stage]
    if item.get("who") == BUILDER:
        return (f"Setup check: {item['label']} — the builder is fixing it",
                f"{item['detail']}. NMHP checks this {when}. Nothing for you to do: the builder was told and the check runs again after its fix.")
    return (f"Blocked, needs you: {item['label']}",
            f"{item['detail']}. NMHP checks this {when}; nothing goes ahead until it passes. Press Check again once it is fixed.")

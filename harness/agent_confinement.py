# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""OS-level write confinement around a managed Claude terminal.

Codex confines its own writes (`sandbox_mode=workspace-write` with the roots
the harness grants). Claude Code has no equivalent the harness can use: its
permission prompts are the boundary, and an unattended agent must not stop
at prompts; its built-in sandbox confines writes but has no network-only off
switch, and inside it the board worker on 127.0.0.1 is unreachable and ssh
to the test host is refused (proven live, 2026-09-23). So the harness draws
the boundary itself, with the primitive it already uses for the git broker
(`harness/platform_support`, one implementation per platform). The whole CLI
process runs inside it; every shell command and file tool the agent uses
inherits it; the OS refuses a write outside the granted roots. Reads and
network are not confined, exactly as for Codex, and the Help text says so.

The grant is: the roots `agent_writable_roots` computed (execution root,
data root, task-workspace root), plus what the CLI itself needs to function:
its own state (`~/.claude`, `~/.claude.json`, or `CLAUDE_CONFIG_DIR`), the
user cache and log folders, the temp directories, and the device files.
Nothing else in the owner's home is writable.

This module decides nothing from the platform; the seam does.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from harness import platform_support
from harness.platform_support import AgentConfinementUnavailable as ConfinementUnavailable  # noqa: F401  (re-exported)


def _validated_config_dir(home: str | Path, claude_config_dir: str | None, temp_paths: list[str]) -> str | None:
    """`CLAUDE_CONFIG_DIR` may relocate the CLI's state; it may not widen the grant.

    The runner inherits the owner's environment, so this value is untrusted
    input to the boundary. A reviewer set it to the home folder and wrote
    outside the project (round 2, 2026-09-23). Accepted: a folder strictly
    inside the home folder, or inside the temp space that is writable anyway.
    Refused: the home folder itself, the filesystem root, any ancestor of the
    home folder, and anywhere else — the launch refuses rather than run open.
    """
    if not claude_config_dir:
        return None
    real = Path(claude_config_dir).expanduser().resolve()
    home_real = Path(home).expanduser().resolve()
    temp_real = [Path(item).expanduser().resolve() for item in temp_paths]
    inside_home = home_real in real.parents
    inside_temp = any(root == real or root in real.parents for root in temp_real)
    if real == Path("/") or real == home_real or real in home_real.parents or not (inside_home or inside_temp):
        raise ConfinementUnavailable(
            f"CLAUDE_CONFIG_DIR={claude_config_dir!r} would make {real} writable, which is outside the agent's "
            f"grant; set it to a folder inside {home_real} or unset it. Refusing to launch the agent unconfined."
        )
    return str(real)


def _validated_tmpdir(tmpdir: str | None, temp_paths: list[str]) -> list[str]:
    """`TMPDIR` is inherited too, and the same rule applies: it may not widen the grant.

    Accepted only inside the platform's fixed temp space (where it lives on
    every normal system); anything else refuses the launch.
    """
    if not tmpdir:
        return []
    real = Path(tmpdir).expanduser().resolve()
    roots = [Path(item).resolve() for item in temp_paths]
    if any(root == real or root in real.parents for root in roots):
        return [str(real)]
    raise ConfinementUnavailable(
        f"TMPDIR={tmpdir!r} would make {real} writable, which is outside the agent's grant; "
        f"set it inside {', '.join(temp_paths)} or unset it. Refusing to launch the agent unconfined."
    )


GEMINI_STATE = ".gemini/antigravity-cli"
GEMINI_PROTECTED_FILES = (".gemini/antigravity-cli/settings.json",)
GEMINI_CONTROLS = ".gemini/config"


def gemini_state_paths(home: str | Path) -> list[str]:
    """What the Antigravity CLI writes to run: its operational folder and the shared user cache.

    NEVER `~/.gemini/config/` (hooks, MCP servers, rules live there: spec M-5)
    and never the whole `~/.gemini`. The CLI keeps its sign-in inside the
    operational folder when the OS keyring is unreachable, and reads it in place.
    """
    home = Path(home).expanduser()
    return [str(home / GEMINI_STATE), str(home / ".cache")]


def gemini_protected_writes(home: str | Path) -> list[str]:
    """Owner-controlled Gemini files denied even inside the granted state folder."""
    home = Path(home).expanduser()
    return [str(home / name) for name in GEMINI_PROTECTED_FILES] + [str(home / GEMINI_CONTROLS)]


def writable_paths(writable_roots: list[str], *, home: str | Path, claude_config_dir: str | None = None,
                   implementation=None, tmpdir: str | None = None, provider: str = "claude") -> list[str]:
    """The full write grant: the project's roots plus the CLI's own state and temp space.

    Everything that comes from the environment (`CLAUDE_CONFIG_DIR`, `TMPDIR`)
    is validated before it joins the grant; the roots themselves were validated
    by `agent_writable_roots`.
    """
    implementation = implementation or platform_support.agent_confinement()
    temp = implementation.temp_paths() + _validated_tmpdir(tmpdir if tmpdir is not None else os.environ.get("TMPDIR"), implementation.temp_paths())
    config_dir = _validated_config_dir(home, claude_config_dir, temp)
    state = gemini_state_paths(home) if provider == "gemini" else implementation.cli_state_paths(home, config_dir)
    return list(writable_roots) + state + temp


EXTRA_READ_ENV = "HARNESS_AGENT_READ_PATHS"


def _system_path(path: str) -> bool:
    return any(path == root or path.startswith(root + "/") for root in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"))


def tool_read_roots(clis: list[str], *, home: str | Path, extra: list[str] | None = None, path_env: str | None = None) -> list[str]:
    """What an agent on Linux may READ besides the system folders and its grant: the tools it runs with.

    The CLI itself (its real file's folder, and the install prefix when that folder is a `bin`), every PATH folder
    that is not a system folder (that folder only), the harness install and interpreter passed in `extra`, and the owner's own additions in
    `HARNESS_AGENT_READ_PATHS` (os.pathsep separated). Never the home folder itself or any ancestor of it.
    """
    import shutil
    home_real = Path(home).expanduser().resolve()
    search = path_env if path_env is not None else os.environ.get("PATH", "")
    found: list[str] = []

    def add(path) -> None:
        real = Path(path).expanduser().resolve()
        if not real.exists() or real == Path("/") or real == home_real or real in home_real.parents:
            return
        if not _system_path(str(real)) and str(real) not in found:
            found.append(str(real))

    def add_tool_folder(folder: Path) -> None:
        add(folder)
        parent = folder.parent
        looks_installed = (parent / "lib").is_dir() or (parent / "pyvenv.cfg").is_file() or (parent / "node_modules").is_dir()
        if folder.name == "bin" and looks_installed and parent != home_real and parent.name not in {".local", "local", ""}:
            add(parent)  # an install prefix (lib/node_modules beside bin, a virtual environment's libraries), not any parent

    for cli in [*clis, "node", "python3"]:
        located = shutil.which(cli, path=search)
        if located:
            add_tool_folder(Path(located).resolve().parent)  # the identified tool: its real folder and install prefix
            add(Path(os.path.abspath(located)).parent)       # and the folder it is invoked from (a symlink's own place)
    for entry in search.split(os.pathsep):
        if entry and Path(entry).is_dir():
            add(entry)  # the folder only: never its prefix, so ~/client/bin does not expose ~/client
    for item in extra or []:
        if item:
            given = Path(os.path.abspath(item))
            real = given.resolve()
            add_tool_folder(real if real.is_dir() else real.parent)
            if not real.is_dir():
                add_tool_folder(given.parent)  # a virtual environment's own folder, beside the interpreter it links to
    for item in os.environ.get(EXTRA_READ_ENV, "").split(os.pathsep):
        if item.strip():
            add(item.strip())
    return found


def session_runtime_roots(manager_home: str | Path | None, project: str, session: str) -> list[str]:
    """The session's harness runtime directory (hooks, directive, handover sockets): outside the grant, read-only inside."""
    if not manager_home or not session:
        return []
    from harness import runtime_dir
    return [str(runtime_dir.session_directory(manager_home, project, session))]


def git_read_roots(writable_roots: list[str], *, home: str | Path | None = None) -> list[str]:
    """A linked git worktree keeps its repository elsewhere: that GIT folder (objects, refs; never the other checkout's
    working files) must be readable or `git status` fails.

    The `.git` file is project content an agent can edit, so its pointer is only believed when it checks out: the
    target is `<repo git dir>/worktrees/<name>`, the repository folder looks like a git directory (HEAD and objects),
    the worktree's own back-pointer names this checkout, and the folder is not the home folder or an ancestor of it.
    """
    home_real = Path(home).expanduser().resolve() if home else Path.home().resolve()
    found: list[str] = []
    for root in writable_roots:
        marker = Path(root) / ".git"
        try:
            if not marker.is_file():
                continue
            line = marker.read_text(encoding="utf-8", errors="replace").strip()
            if not line.startswith("gitdir:"):
                continue
            gitdir = Path(line[7:].strip())
            if not gitdir.is_absolute():
                gitdir = Path(root) / gitdir
            gitdir = gitdir.resolve()
            common = gitdir.parent.parent
            back = gitdir / "gitdir"
            valid = (gitdir.parent.name == "worktrees" and (common / "HEAD").is_file() and (common / "objects").is_dir()
                     and back.is_file() and Path(back.read_text(encoding="utf-8", errors="replace").strip()).resolve().parent
                     == Path(root).resolve())
            if valid and common != Path("/") and common != home_real and common not in home_real.parents:
                found.append(str(common))
        except OSError:
            continue
    return found


def wrap(argv: list[str], writable_roots: list[str], *, store: Path, home: str | Path,
         claude_config_dir: str | None = None, implementation=None,
         protected_writes: list[str] | None = None, provider: str = "claude",
         read_confined: bool = False, readable_roots: list[str] | None = None) -> list[str]:
    """The command to run so that `argv` can write only inside the grant.

    `protected_writes` are harness-owned paths (`agent_grant.agent_protected_roots`)
    denied even where a granted root contains them: the deny is applied AFTER
    the grant, so it wins.

    Refuses on a platform without the primitive: a managed agent never runs
    with an open boundary because a package is missing.
    """
    implementation = implementation or platform_support.agent_confinement()
    writable = writable_paths(writable_roots, home=home, claude_config_dir=claude_config_dir, implementation=implementation,
                              provider=provider)
    if provider == "gemini":
        protected_writes = list(protected_writes or []) + gemini_protected_writes(home)
    for root in writable_roots:
        # A granted root that does not exist yet (the task-workspace root of a
        # fresh project) must be created HERE: inside the boundary, creating
        # it is a write to its parent, which is outside the grant. Found by
        # the end-to-end run, not by the unit tests.
        Path(root).mkdir(parents=True, exist_ok=True)
    protected = getattr(implementation, "protected_read_paths", None)
    protected_reads = protected(home, claude_config_dir) if protected else []
    extra = {}
    if read_confined and getattr(implementation, "confines_reads", False):
        # Linux: everything outside the system folders, the grant and these tools is absent from the sandbox.
        extra["confine_reads"] = True
        extra["read_roots"] = list(readable_roots or []) + git_read_roots(writable_roots, home=home)
    if provider == "gemini":
        # Antigravity saves its refreshed sign-in to the macOS Keychain by running
        # /usr/bin/security. Inside this sandbox that call cannot succeed and, instead
        # of failing, hangs while macOS shows "a keychain cannot be found" to the
        # owner about once an hour per agent. Refusing to start the program makes it
        # fail at once and fall back to its sign-in file, as it does anyway.
        extra["denied_executables"] = ["/usr/bin/security"]
    return implementation.wrap(list(argv), writable, store=Path(store), protected_reads=protected_reads,
                               protected_writes=list(protected_writes or []), **extra)


def read_guard(argv: list[str], *, home: str | Path, store: Path, claude_config_dir: str | None = None,
               implementation=None) -> list[str]:
    """Run a harness-owned command (a ledger simulation) without access to the owner's Claude login.

    2026-09-26: a delivery copied the owner's login file into temporary config
    folders to run the CLI "isolated"; the copies shared one session and broke
    every login on the machine. The only sanctioned way to authenticate an
    isolated run is a token the owner creates with `claude setup-token`,
    passed as CLAUDE_CODE_OAUTH_TOKEN — never a copy of the login file.
    """
    implementation = implementation or platform_support.agent_confinement()
    protected = implementation.login_file_paths(home, claude_config_dir)
    return implementation.read_guard(list(argv), protected, store=Path(store))


def confine_agent_command(argv: list[str], project_root, *, home: str | Path | None = None,
                          claude_config_dir: str | None = None, implementation=None) -> list[str]:
    """Run a command an AGENT wrote inside the same write limits as the agent itself.

    Security scan 2026-10-04, finding 2 (spec docs/specs/AGENT_ISOLATION.md
    §2.2, reviewed): internal-QA test commands, reviewer challenge commands and
    CTO health checks ran in the board worker with the owner's full write
    authority, guarded only from reading the Claude login. Now:
    - The write grant comes from the project's own TRUSTED context (its
      registered code root and task workspace, plus the CLI temp and state
      paths `wrap` adds), never from the command, its environment or its
      ledger. The harness's own storage (`agent_grant.agent_protected_roots`)
      is denied even where a granted root contains it, and the profile is
      written under the project's control store, outside the grant.
    - A process already inside an agent's OS sandbox runs the command there:
      it is confined by that sandbox, and macOS cannot nest a second one.
    - A platform without the primitive REFUSES (`ConfinementUnavailable`):
      an agent's command is never run open instead.
    - `claude_config_dir` is the HARNESS's own CLAUDE_CONFIG_DIR (the worker's
      execution environment, never the command's): a relocated login there is
      unreadable too, as under the read guard this replaces.
    """
    from harness import agent_grant
    from harness.project_context import project_context
    if platform_support.browser_host().inside_os_sandbox():
        return list(argv)
    context = project_context(project_root)
    writable = [str(Path(context.code_root).resolve()), str(Path(context.workspace_root).resolve())]
    protected = agent_grant.agent_protected_roots(context.code_root, context.data_root, context.workspace_root)
    store = Path(context.storage_path("control"))
    store.mkdir(parents=True, exist_ok=True)
    # Reads too (Linux): the command is the agent's own, so it must not read what the agent may not
    # (Codex plan check 2026-10-15, finding 1). Dependencies: the tools on PATH, this interpreter, the harness install.
    return wrap(list(argv), writable, store=store, home=home or Path.home(), claude_config_dir=claude_config_dir or None,
                implementation=implementation, protected_writes=protected, read_confined=True,
                readable_roots=tool_read_roots([], home=home or Path.home(),
                                               extra=[sys.executable, str(Path(__file__).resolve().parents[1])]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the confined launch command for a managed Claude terminal as JSON")
    parser.add_argument("--store", required=True, help="where a generated profile is kept (the project's control dir)")
    parser.add_argument("--writable-roots", required=True, help="JSON list of the granted roots")
    parser.add_argument("--protected-writes", default="[]", help="JSON list of harness-owned paths denied inside the grant")
    parser.add_argument("--home", default=os.path.expanduser("~"))
    parser.add_argument("--provider", default="claude", choices=["claude", "codex", "gemini"])
    parser.add_argument("--read-confined", action="store_true", help="Linux: deny reads outside the system folders, the grant and the tools")
    parser.add_argument("--readable-roots", default="[]", help="JSON list of extra read-only roots (the harness install, the interpreter)")
    parser.add_argument("--claude-config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR") or None)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the CLI command (after --)")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        wrapped = wrap(command, json.loads(args.writable_roots), store=Path(args.store), home=args.home,
                       claude_config_dir=args.claude_config_dir, protected_writes=json.loads(args.protected_writes),
                       provider=args.provider, read_confined=args.read_confined,
                       readable_roots=tool_read_roots([command[0]] if command else [], home=args.home,
                                                      extra=json.loads(args.readable_roots)) if args.read_confined else None)
    except ConfinementUnavailable as error:
        print(str(error), file=sys.stderr)
        return 3
    print(json.dumps(wrapped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

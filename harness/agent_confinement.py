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
import re
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
    """Decide whether `path` is one of the fixed OS system folders or lives inside one.

    Args:
        path: the filesystem path to check.

    Return: True if path equals or is nested under /usr, /bin, /sbin, /lib,
    /lib64, or /etc; False otherwise.
    """
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
        """Resolve `path` and, unless it is missing, the root, the home folder, an
        ancestor of the home folder, or a system folder already listed, append it
        to the enclosing `found` list.

        Args:
            path: the candidate read-root path to resolve and possibly record.

        Returns: None; the effect is appending the resolved path to `found`.
        """
        real = Path(path).expanduser().resolve()
        if not real.exists() or real == Path("/") or real == home_real or real in home_real.parents:
            return
        if not _system_path(str(real)) and str(real) not in found:
            found.append(str(real))

    def add_tool_folder(folder: Path) -> None:
        """Record `folder` itself via `add`, and, when `folder` is a `bin` directory
        that looks like the bin of an installed package or virtual environment
        (a sibling `lib`, `pyvenv.cfg`, or `node_modules`), also record its parent
        install prefix, unless that parent is the home folder or a generic
        `.local`/`local` folder.

        Args:
            folder: the tool's own directory to add, and to inspect for a sibling
                install prefix.

        Returns: None; the effect is adding one or two paths via `add`.
        """
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


def phone_folders() -> list[str]:
    """On a Mac, the folders Flutter, Xcode and the iPhone simulator write (phone_setup.mobile_extra_writable), granted to
    every confined command and every agent launch, not only a Yes-iPhone task's setup check (owner 2026-11-26: the check
    passed while the agents and the board's own test runs were still refused `flutter --version`). Empty elsewhere."""
    from harness import board, phone_setup
    if not board.host_is_macos():
        return []
    try:
        return phone_setup.mobile_extra_writable(phone_setup.host_env())
    except OSError:
        return []


PACKAGE_FOLDERS = ("/opt/homebrew", "/usr/local", "/home/linuxbrew/.linuxbrew")


def environment_folders() -> list[str]:
    """Owner 2026-11-27, verbatim: "the reviewer should have all the rights available to modify the environment to make the test
    feasible". Every agent launch and every confined command may write the owner's whole home folder (tool caches, SDKs, installs),
    on every platform, plus the phone folders; NMHP's own records stay closed (`nmhp_protected_roots`, denied after the grant)."""
    folders = [str(Path.home().resolve())]
    # the package managers' own install folders (Homebrew, a Flutter SDK installed by it), when the owner can write them
    folders += [os.path.realpath(path) for path in PACKAGE_FOLDERS if os.path.isdir(path) and os.access(path, os.W_OK)]
    return folders + [path for path in phone_folders() if path not in folders]


GRANTS_FILE = "environment-grants.json"
_DENIED_LINE = re.compile(r"Operation not permitted")
_ABSOLUTE_PATH = re.compile(r"""(?:(?<=[\s'"=(:])|^)(/[^\s'":,)]+)""")


def learned_grants(project_root) -> list[str]:
    """Folders this project's checks were refused by the sandbox and NMHP opened since (learn_denied)."""
    from harness.project_context import project_context
    try:
        path = Path(project_context(project_root).storage_path("control")) / GRANTS_FILE
        return [str(item) for item in json.loads(path.read_text(encoding="utf-8")) if isinstance(item, str)]
    except (OSError, ValueError, TypeError):
        return []


def learn_denied(project_root, output: str) -> list[str]:
    """Owner 2026-11-27: the builder and Reviewer may change their environment so the test can run. Read the folders a check's
    output says the sandbox refused ("Operation not permitted"), and open each one for this project: the folder itself when it
    exists as a folder, else its parent, never one of NMHP's own records and never one the operating system itself forbids the
    owner to write. Saved in the project's control store; every later confined command gets them. Returns the newly opened ones."""
    from harness import agent_grant
    from harness.project_context import project_context
    context = project_context(project_root)
    protected = agent_grant.agent_protected_roots(context.code_root, context.data_root, context.workspace_root) + nmhp_protected_roots(context.code_root)
    known = set(learned_grants(project_root))
    opened: list[str] = []
    for line in str(output or "").splitlines():
        if not _DENIED_LINE.search(line):
            continue
        for raw in _ABSOLUTE_PATH.findall(line):
            path = Path(raw.rstrip(".;"))
            folder = path if path.is_dir() else path.parent
            folder_text = os.path.realpath(str(folder))
            if folder_text in ("/", os.path.realpath(str(Path.home()))) or folder_text in known or folder_text in opened:
                continue
            if not outside_agent_writes(folder_text, [folder_text], protected) and os.access(folder_text if Path(folder_text).exists() else os.path.dirname(folder_text), os.W_OK):
                opened.append(folder_text)
    if opened:
        store = Path(context.storage_path("control"))
        store.mkdir(parents=True, exist_ok=True)
        (store / GRANTS_FILE).write_text(json.dumps(sorted(known | set(opened)), indent=1), encoding="utf-8")
    return opened


def _deny_around(roots: list[str], keep: list[str]) -> list[str]:
    """A denied root that contains a path the agent must write (its task workspace) is replaced by its other children, down to
    that path, so the deny never covers the agent's own work (Seatbelt and Codex apply a deny over a nested grant)."""
    out: list[str] = []
    keep = [os.path.realpath(k) for k in keep if k]

    def inside(child: str, root: str) -> bool:
        return child == root or child.startswith(root.rstrip("/") + "/")
    for root in roots:
        if any(inside(root, k) for k in keep):
            continue                                   # the denied path is the agent's own work (or inside it)
        holders = [k for k in keep if inside(k, root)]
        if not holders:
            out.append(root)
            continue
        try:
            children = [os.path.realpath(entry) for entry in Path(root).iterdir()]
        except OSError:
            continue
        out += _deny_around(children, holders)
    return out


def nmhp_protected_roots(project_code_root=None, manager_home=None, keep: list[str] | None = None) -> list[str]:
    """NMHP's own records no agent may write even though its home folder is granted: everything in NMHP's home except the task
    workspaces (agents build there), and NMHP's install (its code and gates), unless that install IS this project's code (a project
    built on NMHP itself). Resolved, so a link cannot slip past."""
    from harness import project_registry
    home = Path(manager_home) if manager_home else project_registry.default_home()
    roots: list[str] = []
    try:
        roots += [os.path.realpath(entry) for entry in sorted(home.iterdir()) if entry.name != "workspaces"]
    except OSError:
        pass
    # NMHP's own folders that may not exist yet at launch (the plumbing runtime folder is made per session after the grant)
    from harness import runtime_dir
    roots += [os.path.realpath(home / name) for name in (runtime_dir.RUNTIME_DIRECTORY, "projects", "secrets", "registry.json", "settings.json")
              if os.path.realpath(home / name) not in roots]
    install = os.path.realpath(str(Path(__file__).resolve().parents[1]))
    code = os.path.realpath(str(project_code_root)) if project_code_root else ""
    if not code or not (code == install or code.startswith(install.rstrip("/") + "/") or install.startswith(code.rstrip("/") + "/")):
        roots.append(install)
    return list(dict.fromkeys(_deny_around(roots, list(keep or []) + ([code] if code else []))))


def outside_agent_writes(path, writable: list[str], protected: list[str]) -> bool:
    """True when an agent cannot write `path`: it is outside every granted root, or inside a protected (denied) one."""
    real = os.path.realpath(str(path))

    def inside(child: str, root: str) -> bool:
        root = os.path.realpath(root)
        return child == root or child.startswith(root.rstrip("/") + "/")
    if any(inside(real, root) for root in protected):
        return True
    return not any(inside(real, root) for root in writable)


def confine_agent_command(argv: list[str], project_root, *, home: str | Path | None = None,
                          claude_config_dir: str | None = None, implementation=None,
                          extra_writable: list[str] | None = None, path_env: str | None = None) -> list[str]:
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
    - `extra_writable` / `path_env` (the setup check's role items, task 2026-11-21_not-run-goes-to-repair): a CLI's own state
      folder (a Codex home) joins the write grant as Claude's does in `wrap`, and the tools are read from the PATH the role's
      commands get, not from the manager's own.
    """
    from harness import agent_grant
    from harness.project_context import project_context
    if platform_support.browser_host().inside_os_sandbox():
        return list(argv)
    context = project_context(project_root)
    writable = [str(Path(context.code_root).resolve()), str(Path(context.workspace_root).resolve())] + [
        str(Path(item).expanduser().resolve()) for item in list(extra_writable or []) + environment_folders() + learned_grants(project_root)]
    protected = agent_grant.agent_protected_roots(context.code_root, context.data_root, context.workspace_root)
    protected += [root for root in nmhp_protected_roots(context.code_root, keep=[str(context.workspace_root)]) if root not in protected]
    store = Path(context.storage_path("control"))
    store.mkdir(parents=True, exist_ok=True)
    # Reads too (Linux): the command is the agent's own, so it must not read what the agent may not
    # (Codex plan check 2026-10-15, finding 1). Dependencies: the tools on PATH, this interpreter, the harness install.
    return wrap(list(argv), writable, store=store, home=home or Path.home(), claude_config_dir=claude_config_dir or None,
                implementation=implementation, protected_writes=protected, read_confined=True,
                readable_roots=tool_read_roots([], home=home or Path.home(), path_env=path_env,
                                               extra=[sys.executable, str(Path(__file__).resolve().parents[1])]))


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments and print the confined launch command as JSON.

    Builds the wrapped command via `wrap`, using the writable roots, protected
    writes, home, provider, and read-confinement options parsed from `argv`
    (or `sys.argv` when `argv` is None). If the platform cannot confine the
    agent, prints the `ConfinementUnavailable` error to stderr instead.

    Args:
        argv: the command-line arguments to parse, or None to use the process's own.

    Returns: 0 on success after printing the wrapped command as JSON, or 3 if
    confinement is unavailable on this platform.
    """
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

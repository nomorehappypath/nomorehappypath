# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Today's values, moved verbatim from their previous homes.

Nothing here is new. Every value is byte-identical to what the caller
constructed before, because Stage 0's contract is that a macOS owner observes
no change at all — and several of these strings are hashed into recorded
evidence, so "equivalent" is not good enough.

Named `defaults`, not `macos`, and the distinction is load-bearing. These are
not the macOS values: they are the ONLY values this product has ever used, on
every platform it has ever been imported on. Moving them here is therefore a
no-op everywhere, which is what lets the suite still run on a Linux box — and
running it there is how Stage 1 gets verified at all. Stage 1 introduces real
per-platform selection alongside a Linux implementation; until one exists,
selecting anything else would be inventing behaviour nobody chose.

The three discovery policies stay SEPARATE on purpose. They disagree with each
other by design:

* owner tools put owner-writable directories FIRST, because the owner's
  deliberate user-level install of a CLI outranks a system copy;
* trusted tools are restricted to system directories, because the harness runs
  Git itself and must not pick up something the project can write;
* the governed execution path is built from the running interpreter outward,
  so a governed command runs the same way for Delivery, Reviewer and release.

Merging them would change `sanitized_environment_sha256`, which is recorded
evidence.
"""
from __future__ import annotations

import hashlib
import os
import signal
import socket
import struct
import dataclasses
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


class _Discovery:
    """Where this platform keeps the tools the harness needs."""

    # Moved from harness/global_settings.py. User-level installers first, the
    # way a login shell orders PATH: Claude Code's official installer targets
    # ~/.local/bin, which no launchd PATH and no system prefix contains. The
    # app must find CLIs the way the owner installed them - without a login
    # shell's profile or a process restart - and the owner's deliberate
    # user-level install outranks a system copy.
    OWNER_TOOL_SUFFIX: tuple[str, ...] = (
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    )

    # Moved from harness/git_broker.py. System directories only: the broker
    # runs Git on the owner's behalf and must not resolve a tool the reviewed
    # project could write.
    TRUSTED_TOOL_SEARCH_PATH: str = "/usr/bin:/bin:/usr/sbin:/sbin"

    # Moved from harness/git_broker.py: Xcode's Git is preferred where present,
    # then the system copy, then a last-resort search of the trusted path.
    TRUSTED_GIT_CANDIDATES: tuple[Path, ...] = (
        Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git"),
        Path("/usr/bin/git"),
    )

    # Moved from harness/child_process.py, unchanged including the order.
    GOVERNED_PATH_SUFFIX: tuple[str, ...] = (
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
        "/usr/sbin", "/sbin",
    )

    def owner_tool_directories(self) -> tuple[str, ...]:
        """Expanded on every call, deliberately.

        Caching this at import broke `provider_executable` for a CLI in
        ~/.local/bin: tests/test_openai_key_settings.py patches
        os.path.expanduser and RELOADS global_settings to recompute the value,
        and a value cached in this module survived that reload. That is the
        public issue #1 case - the exact behaviour this product already had to
        fix once - so it is resolved when asked, not when imported.
        """
        return (
            os.path.expanduser("~/.local/bin"),
            os.path.expanduser("~/bin"),
            *self.OWNER_TOOL_SUFFIX,
        )

    def trusted_tool_search_path(self) -> str:
        return self.TRUSTED_TOOL_SEARCH_PATH

    def trusted_git_candidates(self) -> tuple[Path, ...]:
        return self.TRUSTED_GIT_CANDIDATES

    def governed_execution_path(self, source: Mapping[str, str]) -> str:
        """One stable tool path independent of an agent's interactive shell."""
        candidates = [str(Path(sys.executable).resolve().parent)]
        for environment_name in ("VIRTUAL_ENV", "CONDA_PREFIX"):
            prefix = str(source.get(environment_name, "")).strip()
            if prefix:
                candidates.append(str(Path(prefix).resolve() / "bin"))
        candidates.extend(self.GOVERNED_PATH_SUFFIX)
        return os.pathsep.join(dict.fromkeys(candidates))


DISCOVERY = _Discovery()


class ProcessTableUnavailable(OSError):
    """The OS process table could not be read at all.

    Deliberately an OSError: callers that already tolerate one from these
    routines - the ownership observer, release_preview's liveness check - must
    keep behaving exactly as they did. Process identity is evidence, so an
    unreadable table is a named refusal and never an empty table, which would
    read as "nothing was running".
    """


class _ProcessIdentity:
    """Who is running, and proof that it is still the same process.

    Split by COST and FAILURE POLICY, not by platform, and the split is
    load-bearing. `parent_process_id` is a bounded single-pid probe used inside
    an authentication walk; deriving it from `process_table()` would turn a
    16-hop walk into sixteen system-wide scans.
    """

    def run_ps(self, arguments: list[str], *, check: bool) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["ps", *arguments], capture_output=True, text=True, check=check,
            )
        except OSError as error:
            raise ProcessTableUnavailable(
                f"cannot read the process table: 'ps' could not be executed ({error})"
            ) from error
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or "").strip() or f"exit status {error.returncode}"
            raise ProcessTableUnavailable(
                f"cannot read the process table: 'ps' failed ({detail})"
            ) from error

    def start_token(self, pid: int) -> str:
        # A non-zero exit means that pid is gone, which is an ANSWER, not a
        # failure - it stays an empty token exactly as before.
        result = self.run_ps(["-p", str(pid), "-o", "lstart="], check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def process_table(self) -> dict[int, dict[str, Any]]:
        """A plain mapping of plain dicts.

        Three consumers duck-type this today (`_record_owned`,
        `_app_processes`, `_observe_owned`). A record type would be tidier and
        would break all three, so Stage 0 keeps the shape and leaves that to a
        stage that can migrate the consumers with it.
        """
        result = self.run_ps(["-axo", "pid=,ppid=,pgid=,lstart=,command="], check=True)
        table: dict[int, dict[str, Any]] = {}
        for raw in result.stdout.splitlines():
            parts = raw.strip().split(None, 8)
            if len(parts) != 9:
                continue
            try:
                pid, ppid, pgid = (int(parts[index]) for index in range(3))
            except ValueError:
                continue
            table[pid] = {
                "pid": pid, "ppid": ppid, "pgid": pgid,
                "start_token": " ".join(parts[3:8]), "command": parts[8],
            }
        return table

    def parent_process_id(self, pid: int, *, timeout_seconds: float = 2.0) -> int:
        """One bounded probe, used inside an authentication walk.

        Absolute /bin/ps, `check=True`, and a short timeout - all three matter:
        the caller treats ANY failure as an authentication refusal, so this must
        fail rather than guess.
        """
        result = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
            check=True, capture_output=True, text=True, timeout=timeout_seconds,
        )
        return int(result.stdout.strip())

    def terminate_group(self, process: "subprocess.Popen", sig: int) -> bool:
        """Signal a live child's OWN process group, or refuse.

        Three conditions, all required, and each one earned:

        1. The child must still be running. Signalling a group derived from a
           pid that has exited is a PID-reuse race - the number is recycled and
           the signal lands on whoever holds it now. Linux recycles pids far
           faster than macOS, which is why this was invisible here for years and
           immediate there: it killed the test runner, and twice killed the
           operator's SSH session.
        2. The child must BE the group leader (`getpgid(pid) == pid`). A leading
           minus tells `kill` to address a group by number; if the child is not
           that group's leader, the number names somebody else's group.
        3. The signal goes through `os.killpg`, not a shelled-out `kill`, so
           there is no PATH lookup and no argument that can be misread.

        Returns True if the signal was delivered, False if it was refused. A
        refusal is not an error: the child being gone is the outcome the caller
        wanted.
        """
        if process is None or process.poll() is not None:
            return False                      # already exited - nothing to signal
        pid = process.pid
        try:
            if os.getpgid(pid) != pid:
                return False                  # not a leader; -pid is not its group
            os.killpg(pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def peer_process_id(self, connection: socket.socket) -> int:
        """The pid on the other end of a local socket, from the OS.

        The one place this codebase already branched correctly. Darwin's
        LOCAL_PEERCRED (option 0x002) returns a single int; SO_PEERCRED returns
        pid, uid, gid. Both are moved verbatim.
        """
        if sys.platform == "darwin":
            return struct.unpack("i", connection.getsockopt(0, 0x002, 4))[0]
        if hasattr(socket, "SO_PEERCRED"):
            return struct.unpack(
                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )[0]
        raise UnsupportedPlatformOperation("no peer-credential mechanism on this platform")


class UnsupportedPlatformOperation(RuntimeError):
    """An operation with no implementation here. Callers translate it."""


class Grant:
    """What a confined command is permitted to do.

    Named for what the CALLER knows, not for what one platform's sandbox
    language happens to call it. `starts_helper_programs` replaced an earlier
    `allow_shell`, which was named after the two `process-exec` entries macOS
    emits — an implementation detail the caller has no opinion about.
    """

    __slots__ = ("readable", "writable", "network", "starts_helper_programs")

    def __init__(self, *, readable, writable, network: bool, starts_helper_programs: bool):
        self.readable = list(readable)
        self.writable = list(writable)
        self.network = bool(network)
        self.starts_helper_programs = bool(starts_helper_programs)


class Confined:
    """The command to run, and whether confinement is actually in force.

    `enforced` exists because today the caller CANNOT tell. `git_broker` builds
    `["/usr/bin/sandbox-exec", ...] if profile else fixed` and nothing
    downstream records which branch ran, so an unconfined run is
    indistinguishable from a confined one. Stage 0 makes the fact available; it
    deliberately does not change what anyone does with it, because acting on it
    would be a behaviour change.
    """

    __slots__ = ("argv", "enforced", "profile")

    def __init__(self, argv, *, enforced: bool, profile=None):
        self.argv = list(argv)
        self.enforced = bool(enforced)
        self.profile = profile


class _Confinement:
    """OS-level confinement for a command the harness runs on the owner's behalf."""

    def available(self) -> bool:
        return sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()

    def wrap(self, argv, grant: Grant, *, store: Path, enabled: bool = True) -> Confined:
        """Return the command to run, and say whether it is confined.

        `store` is required, not optional: the profile filename is
        content-addressed (`sandbox-<sha256[:16]>.sb`) and written under the
        broker's journal root. Without the store the macOS implementation could
        not reproduce its own filename, and Stage 0's byte-equality obligation
        would fail.
        """
        if not enabled or not self.available():
            return Confined(argv, enforced=False)
        profile = self._profile(grant, store=Path(store))
        return Confined(
            ["/usr/bin/sandbox-exec", "-f", str(profile), *argv],
            enforced=True, profile=profile,
        )

    def _profile(self, grant: Grant, *, store: Path) -> Path:
        """Copied VERBATIM from git_broker._sandbox_profile.

        The filename is content-addressed — `sandbox-<sha256[:16]>.sb` — so a
        single reordered or reworded line renames every profile the broker has
        ever written. An earlier attempt at this move reconstructed the rules
        from reading rather than copying them, and produced a different digest
        immediately; the bytes are grafted from the original for that reason.
        """
        def literal(path: Path) -> str:
            return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
        # Deny by default, then allow immutable OS/runtime reads plus the exact
        # board-derived operation paths.  This keeps repository-controlled
        # helpers from reading arbitrary owner files while still allowing the
        # signed Apple Git runtime and locale/security databases to load.
        lines = [
            "(version 1)", "(allow default)",
            "(deny file-read*)", "(deny file-write*)", "(deny network*)", "(deny process-exec*)",
            "(allow file-read-metadata)",
            '(allow file-read-data (literal "/"))',
        ]
        for executable_path in (
            Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git"),
            Path("/Applications/Xcode.app/Contents/Developer/usr/libexec/git-core"),
            Path("/usr/bin/git"), Path("/usr/bin/ssh"),
        ):
            operation = "subpath" if executable_path.is_dir() else "literal"
            lines.append(f'(allow process-exec ({operation} "{literal(executable_path)}"))')
        if grant.starts_helper_programs:
            lines.append('(allow process-exec (literal "/bin/sh"))')
            lines.append('(allow process-exec (literal "/bin/bash"))')
        for system_path in (
            Path("/System"), Path("/usr"), Path("/bin"), Path("/sbin"),
            Path("/Library/Apple"), Path("/private/etc"),
            Path("/private/var/db/timezone"), Path("/dev"),
            Path("/Applications/Xcode.app"),
        ):
            lines.append(f'(allow file-read* (subpath "{literal(system_path)}"))')
        for path in sorted({Path(item) for item in grant.readable}, key=str):
            lines.append(f'(allow file-read* (subpath "{literal(path)}"))')
        if grant.network:
            lines.append("(allow network*)")
        lines.append('(allow file-write* (literal "/dev/null"))')
        for path in sorted({Path(item) for item in grant.writable}, key=str):
            lines.append(f'(allow file-write* (subpath "{literal(path)}"))')
        digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]
        profile = store / f"sandbox-{digest}.sb"
        if not profile.exists():
            profile.write_text("\n".join(lines) + "\n", encoding="utf-8")
            profile.chmod(0o600)
        return profile


class _BrowserHost:
    """Where this platform keeps a headless browser, and how it may be used.

    The cache glob was macOS-only, so on Linux no browser was ever found — and
    the rendered tests SKIPPED while their modules reported OK. A green result
    proving nothing is worse than a red one: eight suites whose whole purpose is
    proving the owner sees what we claim were proving nothing on Linux.

    Both platform caches are searched on both platforms. That is deliberate and
    is not "Linux code" in the Stage 0 sense: a directory that does not exist
    contributes no candidates, so macOS resolution is unchanged, and the
    platform-native location stays first.
    """

    # ONE layout per platform, selected — not both searched everywhere.
    #
    # An earlier version searched both on both, reasoning that a directory which
    # does not exist contributes nothing. The reviewer built the case that
    # breaks it: a Mac carrying a Linux Playwright cache. There, main raises the
    # named refusal and the both-layouts version returned a Linux binary — a
    # behaviour change on macOS, which Stage 0 forbids. "Probably absent" is not
    # the same guarantee as "not consulted".
    MACOS_LAYOUT = (("Library", "Caches", "ms-playwright"),
                    "chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell")
    LINUX_LAYOUT = ((".cache", "ms-playwright"),
                    "chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell")

    def cache_layout(self):
        return self.MACOS_LAYOUT if sys.platform == "darwin" else self.LINUX_LAYOUT

    PATH_NAMES: tuple[str, ...] = ("chrome-headless-shell", "chromium", "chromium-browser")

    def headless_cache_candidates(self) -> list[Path]:
        """Resolved on every CALL, never frozen at import.

        A cache root captured at import time ignores a test that sets HOME, so
        discovery reaches into the owner's REAL cache: the test then proves
        nothing and passes only because a real browser happens to be installed.
        The same call-time rule already governs the owner tool directories.
        """
        home = Path.home()
        parts, pattern = self.cache_layout()
        return sorted(home.joinpath(*parts).glob(pattern), reverse=True)

    def path_candidate_names(self) -> tuple[str, ...]:
        return self.PATH_NAMES

    def rejects_resolved(self, resolved: Path) -> bool:
        """A macOS SAFETY rule, applied on every platform.

        A binary inside a `.app` bundle can relaunch the owner's own browser,
        which is exactly what the certified-execution evidence exists to catch.
        Dropping it off Darwin would silently weaken the check on the platform
        being added.
        """
        return any(part.lower().endswith(".app") for part in Path(resolved).parts)


BROWSER_HOST = _BrowserHost()


CONFINEMENT = _Confinement()


PROCESS_IDENTITY = _ProcessIdentity()


class FolderSelectionTimeout(Exception):
    """The owner never answered the picker.

    Named rather than raw, so the application can keep its own wording without
    the platform layer owning owner-facing copy.
    """


class _FolderChooser:
    """The native folder picker.

    Deliberately preserved wart (spec 4.5): a CANCEL and a genuine FAILURE both
    return "". The page cannot tell them apart. That is wrong for the product
    and correct for Stage 0, and it is recorded for its own task rather than
    quietly fixed here — changing it would change what the page does.

    The prompt arrives already validated against a closed set. That check is the
    only thing keeping arbitrary text out of the AppleScript below, so it stays
    on the application side where the purpose table lives.
    """

    TIMEOUT_SECONDS = 120

    def choose(self, prompt: str) -> str:
        # Stage 0 selects this module on EVERY platform (see the selector), so
        # the macOS-only refusal has to live here. Routing it through the
        # selector alone would have turned a named refusal into a
        # FileNotFoundError from a missing /usr/bin/osascript.
        if os.uname().sysname != "Darwin":
            raise UnsupportedPlatformOperation("native folder selection")
        script = f'POSIX path of (choose folder with prompt "{prompt}")'
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", script], capture_output=True, text=True,
                check=False, timeout=self.TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise FolderSelectionTimeout from error
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        return str(Path(result.stdout.strip()).resolve())


FOLDER_CHOOSER = _FolderChooser()


@dataclasses.dataclass(frozen=True)
class SessionSurface:
    """What the host gives back after opening a visible session.

    `attach_hint` is DATA on this value, not a keyed accessor, because the macOS
    host does not remember sessions — there is nothing to look one up in. A
    keyed `attach_hint(session_id)` would promise a registry that does not exist.
    """

    session_id: str
    attach_hint: str = ""


class _TerminalHost:
    """The visible agent terminal.

    Two operations, not four. `is_alive` is already answered portably by
    control.py via os.kill(pid, 0); `close(session_id)` does not exist because
    sessions are stopped by signalling the pid and the surface disappears only
    when its occupant dismisses itself.
    """

    def _colour_literal(self, color_rgb) -> str:
        """0-255 to AppleScript's 0-65535. Preserved exactly, rounding included."""
        return "{" + ", ".join(str(round(channel * 65535 / 255)) for channel in color_rgb) + "}"

    def _open_script(self, color_rgb) -> str:
        rgb = self._colour_literal(color_rgb)
        return f'''on run argv
 tell application "Terminal"
 activate
 set newTab to do script (item 1 of argv)
 tell newTab
  set background color to {rgb}
  set normal text color to {{65535, 65535, 65535}}
 end tell
 end tell
end run'''

    def open_session(self, session_id: str, argv, *, color_rgb) -> SessionSurface:
        if sys.platform != "darwin":
            raise UnsupportedPlatformOperation(
                "central CLI launch currently requires macOS Terminal"
            )
        command = "exec " + shlex.join(list(argv))
        subprocess.run(
            ["/usr/bin/osascript", "-e", self._open_script(color_rgb), command],
            check=True, capture_output=True, text=True,
        )
        return SessionSurface(session_id=session_id)

    DISMISS_SCRIPT = r'''on run argv
 set targetTTY to item 1 of argv
 tell application "Terminal"
  set targetId to missing value
  repeat with terminalWindow in windows
   repeat with terminalTab in tabs of terminalWindow
    try
     if tty of terminalTab is targetTTY then set targetId to id of terminalWindow
    end try
   end repeat
  end repeat
  if targetId is missing value then return "no matching window"
  repeat 40 times
   if not (exists window id targetId) then return "already closed"
   set readyToClose to false
   try
    set readyToClose to not (busy of tab 1 of window id targetId)
   on error
    -- No tab left. When the shell exits, Terminal keeps the window but empties
    -- its tab collection, so every `busy` query errors from then on. The old
    -- code swallowed that error in a bare `try` and waited forever. No tab
    -- means nothing is running, which is exactly when closing is safe.
    set readyToClose to true
   end try
   if readyToClose then
    close window id targetId
    return "closed"
   end if
   delay 0.25
  end repeat
 end tell
 return "still busy; window left open"
end run'''

    def dismiss_current_session(self, stdin_fd: int = 0) -> None:
        """The occupant dismisses ITSELF; there is no session argument.

        The fd is how this platform names the surface it is sitting in, not a
        session identity — which is why it is a defaulted implementation detail
        and not part of the operation's shape. A required `stdin_fd` would
        encode "a session is a tty", true only on macOS.
        """
        if sys.platform != "darwin":
            return
        try:
            terminal_tty = os.ttyname(stdin_fd)
        except OSError:
            return
        try:
            subprocess.Popen(
                ["/usr/bin/osascript", "-e", self.DISMISS_SCRIPT, terminal_tty],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass


TERMINAL_HOST = _TerminalHost()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Linux implementations.

Inherits every default and overrides only what is genuinely different. That is
deliberate: a Linux module that re-declared all six seams would drift from the
macOS one silently, and the parts that ARE shared (the tool-discovery policy,
the folder-chooser refusal) are shared because they are the same idea, not
because nobody got round to them.

What overrides today: TERMINAL_HOST. The rest arrives in its own tasks, each
replacing an inherited default rather than filling a blank.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

# Everything not overridden below is the shared implementation.
from typing import Any

from harness.platform_support.defaults import (  # noqa: F401  (re-exported seam surface)
    _AgentConfinement, AgentConfinementUnavailable,
    BROWSER_HOST,
    CONFINEMENT,
    _Discovery,
    _ProcessIdentity,
    Confined,
    Grant,
    FOLDER_CHOOSER,
    PROCESS_IDENTITY,
    Confined,
    FolderSelectionTimeout,
    Grant,
    ProcessTableUnavailable,
    SessionSurface,
    UnsupportedPlatformOperation,
)

SESSION_PREFIX = "nmhp"


class _LinuxDiscovery(_Discovery):
    """Where a Linux owner's agent CLIs actually live.

    systemd supplies a minimal environment for exactly the same reason launchd
    does, so the app must find the CLIs without a login shell. The macOS list is
    mostly portable; what differs is that /opt/homebrew is inert here, and the
    two locations that matter most on Linux are absent from it.

    This is not hypothetical. The same class already shipped as public issue #1
    on macOS: a CLI installed in ~/.local/bin that the app could not find. An
    `npm install -g @openai/codex` under nvm lands in a versioned directory that
    appears in no system prefix at all — the identical failure, one platform
    over.
    """

    OWNER_TOOL_SUFFIX: tuple[str, ...] = (
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/snap/bin",   # Ubuntu ships CLIs here and it is on no default PATH
    )

    # No Xcode on Linux; the system copy is the trusted one.
    TRUSTED_GIT_CANDIDATES = (Path("/usr/bin/git"),)

    GOVERNED_PATH_SUFFIX: tuple[str, ...] = (
        "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/snap/bin",
    )

    def _node_version_key(self, path: Path):
        """Sort v20.11.1 above v9.9.9 — numerically, not as text."""
        # `path` is the bin directory, so the VERSION is its parent:
        #   ~/.nvm/versions/node/v20.11.1/bin
        # Reading parent.parent gave "node" for every entry, so every key was
        # (0,0,0) and the order was whatever glob returned, reversed. An earlier
        # check passed on that by luck - the glob order happened to be right.
        match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", path.parent.name)
        return tuple(int(part) for part in match.groups()) if match else (0, 0, 0)

    def owner_tool_directories(self) -> tuple[str, ...]:
        """Resolved on every call, for the reason the base class documents."""
        home = Path(os.path.expanduser("~"))
        directories = [str(home / ".local" / "bin"), str(home / "bin")]

        # An `npm -g` prefix the owner set themselves.
        for relative in (".npm-global/bin", ".npm-packages/bin", ".local/share/npm/bin"):
            directories.append(str(home / relative))

        # nvm keeps one bin directory per installed Node version and puts none
        # of them on a service manager's PATH. Newest first: that is the one
        # `nvm use default` would have selected.
        nvm_root = Path(os.environ.get("NVM_DIR") or (home / ".nvm")) / "versions" / "node"
        versions = sorted(nvm_root.glob("*/bin"), key=self._node_version_key, reverse=True)
        directories.extend(str(path) for path in versions)

        directories.extend(self.OWNER_TOOL_SUFFIX)
        # Preserve order, drop duplicates: a repeated directory is harmless but
        # makes the resolved-path evidence harder to read.
        seen, ordered = set(), []
        for directory in directories:
            if directory not in seen:
                seen.add(directory); ordered.append(directory)
        return tuple(ordered)


DISCOVERY = _LinuxDiscovery()


class _ProcProcessIdentity(_ProcessIdentity):
    """Process identity from /proc, not from parsing BSD `ps`.

    `lstart=` formatting differs between BSD ps and Linux procps, and the wide
    `ps -axo` scan costs a subprocess per call. /proc/<pid>/stat field 22 is the
    process start time in clock ticks since boot: constant for the life of that
    pid, and at 100 ticks/second it is a STRONGER reuse detector than lstart's
    one-second resolution, not a weaker substitute.

    The mapping shape is preserved exactly. Three consumers duck-type it, so a
    tidier record type here would break all three on one platform only — the
    worst kind of divergence to debug.
    """

    PROC = Path("/proc")

    def _read_stat(self, pid_dir: Path):
        """Returns (ppid, pgid, start_ticks) or None if the process vanished.

        `comm` is bracketed and MAY CONTAIN SPACES AND PARENTHESES — a process
        can name itself `(evil) 1 2 3`. Splitting on whitespace from the left
        mis-parses every field after it, so everything is taken relative to the
        LAST closing parenthesis.
        """
        try:
            raw = (pid_dir / "stat").read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None
        close = raw.rfind(")")
        if close < 0:
            return None
        fields = raw[close + 2:].split()
        # After comm, field 3 overall is state; ppid and pgrp follow it, and
        # starttime is overall field 22 -> index 19 in this remainder.
        if len(fields) < 20:
            return None
        try:
            return int(fields[1]), int(fields[2]), fields[19]
        except (ValueError, IndexError):
            return None

    def _command(self, pid_dir: Path, fallback: str = "") -> str:
        try:
            raw = (pid_dir / "cmdline").read_bytes()
        except OSError:
            return fallback
        if not raw:
            return fallback  # kernel threads have an empty cmdline
        return " ".join(part for part in raw.decode("utf-8", "replace").split("\x00") if part)

    def start_token(self, pid: int) -> str:
        """An empty token means the pid is gone — an ANSWER, not a failure."""
        stat = self._read_stat(self.PROC / str(int(pid)))
        return stat[2] if stat else ""

    def process_table(self) -> dict[int, dict[str, Any]]:
        try:
            entries = [d for d in self.PROC.iterdir() if d.name.isdigit()]
        except OSError as error:
            raise ProcessTableUnavailable(f"cannot read {self.PROC}: {error}") from error
        table: dict[int, dict[str, Any]] = {}
        for pid_dir in entries:
            stat = self._read_stat(pid_dir)
            if stat is None:
                continue  # exited between listing and reading; not an error
            ppid, pgid, start_ticks = stat
            pid = int(pid_dir.name)
            table[pid] = {
                "pid": pid, "ppid": ppid, "pgid": pgid,
                "start_token": start_ticks,
                "command": self._command(pid_dir),
            }
        if not table:
            # An EMPTY table is impossible on a readable /proc: this process is
            # itself in it. So an empty result means the reader could not see,
            # not that nothing is running - and returning {} would report that
            # as success. A sandbox with /proc masked (an empty tmpfs) produces
            # exactly this, and the release gate depends on it REFUSING: it
            # proves which processes an execution owned by reading this table,
            # so a pass built on an empty one would be false.
            raise ProcessTableUnavailable(
                f"{self.PROC} lists no processes; the process table is unreadable here"
            )
        return table

    def parent_process_id(self, pid: int, *, timeout_seconds: float = 2.0) -> int:
        """No subprocess and no timeout to honour: /proc answers immediately."""
        stat = self._read_stat(self.PROC / str(int(pid)))
        return stat[0] if stat else 0


PROCESS_IDENTITY = _ProcProcessIdentity()


class _TmuxTerminalHost:
    """An agent session the owner can watch, on a machine with no screen.

    A desktop terminal emulator was considered and rejected: it fails entirely
    headless, which is the main reason to run this on Linux at all. tmux covers
    headless AND desktop with one implementation, and the session survives an
    SSH drop — on macOS, closing the laptop lid ends nothing because the window
    is local; over SSH it would kill the agent mid-task.

    This is why `SessionSurface.attach_hint` is data rather than a lookup: on
    macOS the window is already in front of the owner and there is nothing to
    say, while here Mission Control must tell them the exact command.
    """

    def session_name(self, session_id: str) -> str:
        """tmux treats `.` and `:` as target syntax, so they cannot survive."""
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(session_id)).strip("-") or "session"
        return f"{SESSION_PREFIX}-{safe}"

    def attach_command(self, session_id: str) -> str:
        return f"tmux attach -t {self.session_name(session_id)}"

    def _colour(self, color_rgb) -> str:
        red, green, blue = (max(0, min(255, int(channel))) for channel in color_rgb)
        return f"#{red:02x}{green:02x}{blue:02x}"

    def open_session(self, session_id: str, argv, *, color_rgb) -> SessionSurface:
        if not shutil.which("tmux"):
            # Named, not a bare FileNotFoundError from the exec: the owner needs
            # to be told what to install, not shown a traceback.
            raise UnsupportedPlatformOperation(
                "visible agent terminal requires tmux (install it: apt install tmux)"
            )
        name = self.session_name(session_id)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "--", *[str(item) for item in argv]],
            check=True, capture_output=True, text=True,
        )
        # Role colour, same table that drives macOS, so a role looks like itself
        # on both platforms. A tmux too old for this must not fail the launch.
        subprocess.run(
            ["tmux", "select-pane", "-t", f"{name}.0", "-P", f"bg={self._colour(color_rgb)}"],
            check=False, capture_output=True, text=True,
        )
        return SessionSurface(session_id=session_id, attach_hint=self.attach_command(session_id))

    def dismiss_current_session(self, stdin_fd: int = 0) -> None:
        """The occupant dismisses ITSELF, exactly as on macOS.

        The fd is ignored here: tmux names the session in the environment, so
        this platform does not need a tty to know where it is sitting. The
        parameter stays only because the operation's shape is shared.
        """
        if not os.environ.get("TMUX") or not shutil.which("tmux"):
            return
        current = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            check=False, capture_output=True, text=True,
        )
        name = current.stdout.strip()
        # Refuse to kill anything that is not one of ours: this runs inside a
        # session the owner may have started by hand.
        if not name.startswith(f"{SESSION_PREFIX}-"):
            return
        subprocess.run(["tmux", "kill-session", "-t", name],
                       check=False, capture_output=True, text=True)


TERMINAL_HOST = _TmuxTerminalHost()


class _BwrapConfinement:
    """Confinement for a command the harness runs on the owner's behalf.

    macOS runs the broker's Git under `sandbox-exec`. Off Darwin that returned
    None and Git ran UNCONFINED. That is tolerable on macOS only because it
    cannot happen there in practice; on Linux it would be the normal case, so
    the port must not ship without this.

    bubblewrap is the equivalent primitive: read-only binds for readable paths,
    read-write binds for writable ones, `--unshare-net` when network is denied,
    and `--die-with-parent` so a confined child cannot outlive the harness.

    WHEN bwrap IS ABSENT THIS REFUSES. The spec offered two acceptable
    behaviours - refuse, or run unconfined after an explicit recorded owner
    acknowledgement. Refusing is chosen because the alternative requires a
    consent record that does not exist yet, and a missing consent record would
    silently become "run unconfined". A refusal the owner can fix by installing
    one package is better than a permission they never knowingly granted.
    """

    BWRAP = "bwrap"

    # Read-only system paths every command needs to execute at all. Without
    # these the confined process cannot find its interpreter or its libraries,
    # and the failure looks like a broken tool rather than a missing bind.
    SYSTEM_PATHS: tuple[str, ...] = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")

    def available(self) -> bool:
        return shutil.which(self.BWRAP) is not None

    def wrap(self, argv, grant: Grant, *, store, enabled: bool = True) -> Confined:
        if not enabled:
            return Confined(list(argv), enforced=False)
        if not self.available():
            raise UnsupportedPlatformOperation(
                "confinement requires bubblewrap; install it (apt install bubblewrap). "
                "Refusing to run this unconfined."
            )

        command = [self.BWRAP, "--die-with-parent", "--new-session"]
        for path in self.SYSTEM_PATHS:
            if Path(path).exists():
                command += ["--ro-bind", path, path]
        command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]

        # Readable before writable: a path granted BOTH must end up writable, and
        # the later bind wins in bwrap's argument order.
        for path in sorted({str(item) for item in grant.readable}):
            if Path(path).exists():
                command += ["--ro-bind", path, path]
        for path in sorted({str(item) for item in grant.writable}):
            if Path(path).exists():
                command += ["--bind", path, path]

        if not grant.network:
            command.append("--unshare-net")

        command.append("--")
        return Confined(command + [str(item) for item in argv], enforced=True)


CONFINEMENT = _BwrapConfinement()


class _BwrapAgentConfinement(_AgentConfinement):
    """The same boundary with bubblewrap: root read-only, the grant read-write, network shared.

    `HARNESS_BWRAP_BIN` names the binary (tests and operators); otherwise
    PATH. WHEN bwrap IS ABSENT THIS REFUSES, for the reason `_BwrapConfinement`
    gives: a missing package must never silently become "run open".
    """

    def __init__(self, which=shutil.which):
        self._which = which

    def cli_state_paths(self, home, claude_config_dir=None) -> list[str]:
        home = Path(home).expanduser()
        config_dir = Path(claude_config_dir).expanduser() if claude_config_dir else home / ".claude"
        return [str(config_dir), str(home / ".claude.json"), str(home / ".cache"), str(home / ".npm")]

    def temp_paths(self) -> list[str]:
        return ["/tmp", "/var/tmp"]

    def binary(self) -> str | None:
        named = os.environ.get("HARNESS_BWRAP_BIN")
        if named:
            return named if Path(named).is_file() else None
        return self._which("bwrap")

    def available(self) -> bool:
        return self.binary() is not None

    def wrap(self, argv, writable: list[str], *, store) -> list[str]:
        bwrap = self.binary()
        if not bwrap:
            raise AgentConfinementUnavailable(
                "confinement requires bubblewrap; install it (apt install bubblewrap). "
                "Refusing to launch the agent unconfined."
            )
        command = [bwrap, "--die-with-parent", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc"]
        for path in writable:
            real = Path(self._real(path))
            if not real.exists():
                # A state file or directory the CLI has not created yet is
                # created for it: bwrap cannot bind a path that does not exist.
                if real.name == ".claude.json" or real.suffix == ".json":
                    real.parent.mkdir(parents=True, exist_ok=True)
                    real.touch()
                else:
                    real.mkdir(parents=True, exist_ok=True)
            command += ["--bind", str(real), str(real)]
        return command + ["--", *list(argv)]


AGENT_CONFINEMENT = _BwrapAgentConfinement()

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A test machine the owner names (for example a Windows VM), connected from Mission Control.

The owner types address, user and password once; the MANAGER (never an agent) then reads and pins the
machine's host key, makes a per-project key in the project's data folder, signs in once with the password
over a pseudo-terminal it owns, installs the public key, and proves key-only login. The password is held in
memory for that one connection: it is never written to a file, argument, environment variable, log, event,
response or agent message (best-effort hygiene, not a guarantee against agents that run as the same user;
the owner accepted that for a throwaway VM, 2026-10-20).

State lives in one non-secret descriptor, `<data>/test_machine/test_machine.json`:
  revision, auth_epoch, reauth_required, tools_revision, baseline_state, authorized_by (the live manager).
Machine-dependent evidence (a certified command starting `TEST_MACHINE_GEN=`, and a `--machine` capture) is
stamped with (machine_id, revision, auth_epoch, tools_revision) and refused when the stamp is stale.
`reauth_required` is set on every stop and every key failure and cleared ONLY by a password Connect.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import select
import socket
import subprocess
import sys
import termios  # noqa: F401  (the wrapper below needs it on the child side; imported to fail early where absent)
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from harness.project_context import ProjectRoot, project_context

DESCRIPTOR_VERSION = 1
RULE_REVISION = 2
SUBDIR = "test_machine"
DESCRIPTOR_NAME = "test_machine.json"
MACHINE_COMMAND_PREFIX = "TEST_MACHINE_GEN="


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)
    return value if value > 0 else float(default)


# Every limit is a setting (HARNESS_TEST_MACHINE_*), none is inlined.
STEP_SECONDS = _env_seconds("HARNESS_TEST_MACHINE_STEP_SECONDS", 60)
PROBE_SECONDS = _env_seconds("HARNESS_TEST_MACHINE_PROBE_SECONDS", 8)
CHALLENGE_SECONDS = _env_seconds("HARNESS_TEST_MACHINE_CHALLENGE_SECONDS", 600)
HEARTBEAT_STALE_SECONDS = _env_seconds("HARNESS_TEST_MACHINE_HEARTBEAT_STALE_SECONDS", 120)
HEARTBEAT_WAIT_SECONDS = _env_seconds("HARNESS_TEST_MACHINE_HEARTBEAT_WAIT_SECONDS", 2)
HEARTBEAT_WRITE_SECONDS = _env_seconds("HARNESS_TEST_MACHINE_HEARTBEAT_WRITE_SECONDS", 30)
MAX_WAITERS = 50


class MachineError(ValueError):
    """A refusal or failure whose message is plain English for the owner (never contains a secret)."""

    def __init__(self, message: str, code: str = "error", *, needs_password: bool = False):
        super().__init__(message)
        self.code = code
        self.needs_password = needs_password


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- validation ---------------------------------------------------------------
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")
_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-@\\]{0,63}$")


def validate_target(address: Any, user: Any, port: Any = 22) -> tuple[str, str, int]:
    """Check the three values before any argument or command is built from them."""
    address = str(address or "").strip()
    user = str(user or "").strip()
    if not address:
        raise MachineError("Enter the machine's address (for example 192.168.64.5).", "invalid")
    try:
        ipaddress.ip_address(address.strip("[]"))
        address = address.strip("[]")
    except ValueError:
        if not _HOST_RE.match(address):
            raise MachineError("The address must be a name or a number address such as 192.168.64.5.", "invalid") from None
    if not user or not _USER_RE.match(user):
        raise MachineError("Enter the account name on that machine (letters, digits and . _ - only).", "invalid")
    try:
        number = int(str(port).strip() or 22)
    except ValueError:
        raise MachineError("The port must be a number from 1 to 65535.", "invalid") from None
    if not 1 <= number <= 65535:
        raise MachineError("The port must be a number from 1 to 65535.", "invalid")
    return address, user, number


# -- storage --------------------------------------------------------------------
def machine_dir(root: ProjectRoot) -> Path:
    return project_context(root).storage_path(SUBDIR)


def descriptor_path(root: ProjectRoot) -> Path:
    return machine_dir(root) / DESCRIPTOR_NAME


@contextmanager
def _locked(root: ProjectRoot) -> Iterator[None]:
    directory = machine_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Damaged(MachineError):
    pass


def _read_unlocked(root: ProjectRoot) -> dict[str, Any] | None:
    path = descriptor_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise Damaged(f"The test machine record cannot be read ({error.strerror or 'file error'}). Press Connect to make it again.", "damaged") from error
    try:
        value = json.loads(text)
    except ValueError as error:
        raise Damaged("The test machine record is damaged. Press Connect to make it again.", "damaged") from error
    if not isinstance(value, dict) or value.get("version") != DESCRIPTOR_VERSION:
        raise Damaged("The test machine record is from an unsupported version. Press Connect to make it again.", "damaged")
    return value


def read(root: ProjectRoot) -> dict[str, Any] | None:
    """The one project-aware reader. None = never connected. Damaged = raises."""
    return _read_unlocked(root)


def _write_unlocked(root: ProjectRoot, value: dict[str, Any]) -> None:
    path = descriptor_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _blank(root: ProjectRoot) -> dict[str, Any]:
    return {
        "version": DESCRIPTOR_VERSION, "connected": False, "address": "", "user": "", "port": 22,
        "host_key": {}, "key_path": "", "known_hosts_path": "", "revision": 0, "auth_epoch": 0,
        "reauth_required": True, "reauth_cause": "never_connected", "tools_revision": 0, "stop_seq": 0,
        "baseline_state": "complete", "authorized_by": {}, "rule_revision": RULE_REVISION,
        "os": "", "account_kind": "", "ready": {}, "steps": {}, "last_attempt": {},
        "request": {}, "waiters": [], "recovering": [], "baseline": {},
    }


def update(root: ProjectRoot, change: Callable[[dict[str, Any]], Any], *, create: bool = True) -> dict[str, Any] | None:
    """Read-modify-write under the project's machine lock."""
    with _locked(root):
        try:
            value = _read_unlocked(root)
        except Damaged:
            value = None   # a damaged record is replaced by a blank one: the owner presses Connect
        if value is None:
            if not create:
                return None
            value = _blank(root)
        change(value)
        _write_unlocked(root, value)
        return value


def machine_id(value: dict[str, Any]) -> str:
    fingerprint = str((value.get("host_key") or {}).get("fingerprint") or "")
    text = f"{value.get('address','')}|{value.get('port','')}|{fingerprint}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def stamp_of(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "machine_id": machine_id(value), "revision": int(value.get("revision", 0)),
        "auth_epoch": int(value.get("auth_epoch", 0)), "tools_revision": int(value.get("tools_revision", 0)),
    }


# -- manager liveness -----------------------------------------------------------
_BOOT_ID = ""


def boot_id() -> str:
    """This boot's identity, read once per process (a successful answer is kept; an unavailable one is retried and never invents a mismatch)."""
    global _BOOT_ID
    if _BOOT_ID:
        return _BOOT_ID
    found = ""
    try:
        found = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        pass
    if not found:
        try:   # macOS: the kernel's per-boot session uuid, read in-process (no child process)
            import ctypes
            libc = ctypes.CDLL(None)
            buffer = ctypes.create_string_buffer(64)
            size = ctypes.c_size_t(len(buffer))
            if libc.sysctlbyname(b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0) == 0:
                found = buffer.value.decode("ascii", "replace").strip()
        except Exception:  # noqa: BLE001 - boot identity is best effort; an unreadable one never counts as "a different boot"
            found = ""
    if found:
        _BOOT_ID = found
    return found


def _start_token(pid: int) -> str:
    from harness import platform_support
    try:
        return str(platform_support.process_identity().start_token(int(pid)) or "")
    except Exception:  # noqa: BLE001 - an unreadable identity is "not proven alive"
        return ""


_OWN_INSTANCES: set[str] = set()


def manager_identity() -> dict[str, Any]:
    """Who this process is, for `authorized_by`. Cheap: no child process (the start token is added when authorization is written)."""
    identity = {"instance_id": uuid.uuid4().hex, "pid": os.getpid(), "boot_id": boot_id()}
    _OWN_INSTANCES.add(identity["instance_id"])
    return identity


def authorization_record(identity: dict[str, Any]) -> dict[str, Any]:
    """What `authorized_by` stores: the manager's identity plus the process start token and a first heartbeat."""
    return {**identity, "start_token": _start_token(int(identity["pid"])), "heartbeat_at": now()}


def manager_alive(authorized_by: dict[str, Any]) -> bool:
    """Authorization stands while the recorded manager process (pid, start token, boot) is alive."""
    if not authorized_by or not authorized_by.get("pid"):
        return False
    recorded_boot = str(authorized_by.get("boot_id") or "")
    current_boot = boot_id()
    if recorded_boot and current_boot and recorded_boot != current_boot:
        return False
    if int(authorized_by["pid"]) == os.getpid() and authorized_by.get("instance_id") in _OWN_INSTANCES:
        return True   # this very process minted that identity: no need to ask the system about ourselves
    token = str(authorized_by.get("start_token") or "")
    return bool(token) and _start_token(int(authorized_by["pid"])) == token


# -- transitions ------------------------------------------------------------------
def mark_reauth(root: ProjectRoot, cause: str) -> bool:
    """The project stopped, or a key failed: the password is required again. True only on a real transition."""
    changed = {"value": False}

    def change(value: dict[str, Any]) -> None:
        # Every stop request is counted, even when the password is already required: a Connect that started before it must not
        # publish afterwards (it compares this number under the same lock). The epoch below still moves only on a real transition.
        value["stop_seq"] = int(value.get("stop_seq", 0)) + 1
        if value.get("reauth_required"):
            return   # already required: no new epoch, no loop (V3)
        value["reauth_required"] = True
        value["reauth_cause"] = cause
        value["auth_epoch"] = int(value.get("auth_epoch", 0)) + 1
        value["authorized_by"] = {}
        value["ready"] = {}
        value["handoff_pending"] = 0   # an announcement of the old authorization is obsolete
        changed["value"] = True

    if not descriptor_path(root).exists():
        return False
    update(root, change, create=False)
    return changed["value"]


def mark_resume(root: ProjectRoot) -> str:
    """A Resume. Returns "kept" when the owner connected the machine after the last stop (during the pause) and it still stands,
    "required" when the password is now required, "none" when there is no record. Every Resume counts as a stop for an attempt that
    is still running (the stop count moves, so such a Connect cannot publish); only the authorization of a Connect made AFTER the
    last stop survives, and a missing marker fails closed."""
    outcome = {"value": "none"}

    def change(value: dict[str, Any]) -> None:
        connected_after_last_stop = int(value.get("connected_at_stop_seq", -1)) == int(value.get("stop_seq", 0))
        authorized = bool(value.get("connected")) and not value.get("reauth_required") and manager_alive(value.get("authorized_by") or {})
        value["stop_seq"] = int(value.get("stop_seq", 0)) + 1
        if authorized and connected_after_last_stop:
            value["connected_at_stop_seq"] = value["stop_seq"]   # a retried Resume keeps again; any other stop moves the count past it
            value["handoff_pending"] = int(value.get("auth_epoch", 0))
            outcome["value"] = "kept"
            return
        outcome["value"] = "required"
        if not value.get("reauth_required"):
            value["reauth_required"] = True
            value["reauth_cause"] = "project_stopped"
            value["auth_epoch"] = int(value.get("auth_epoch", 0)) + 1
            value["authorized_by"] = {}
            value["ready"] = {}
            value["handoff_pending"] = 0

    if not descriptor_path(root).exists():
        return "none"
    update(root, change, create=False)
    return outcome["value"]


def heartbeat(root: ProjectRoot, identity: dict[str, Any]) -> None:
    def change(value: dict[str, Any]) -> None:
        auth = value.get("authorized_by") or {}
        if auth.get("instance_id") == identity.get("instance_id"):
            stamp = now()
            last = auth.get("heartbeat_at", "")
            if last:
                try:
                    if (datetime.fromisoformat(stamp) - datetime.fromisoformat(last)).total_seconds() < HEARTBEAT_WRITE_SECONDS:
                        return
                except ValueError:
                    pass
            auth["heartbeat_at"] = stamp
            value["authorized_by"] = auth

    if descriptor_path(root).exists():
        update(root, change, create=False)


def reconcile_on_start(root: ProjectRoot, identity: dict[str, Any]) -> bool:
    """A new manager instance never inherits authorization: fail closed (U1)."""
    try:
        value = read(root)
    except Damaged:
        return False
    if value is None or value.get("reauth_required"):
        return False
    if (value.get("authorized_by") or {}).get("instance_id") == identity.get("instance_id"):
        return False
    return mark_reauth(root, "manager_restarted")


# -- gate ---------------------------------------------------------------------------
CAUSE_TEXT = {
    "never_connected": "No test machine is connected for this project yet.",
    "manager_restarted": "Mission Control was restarted, so the machine asks for its password again.",
    "project_stopped": "The project was stopped (pause, close or resume), so the machine asks for its password again.",
    "key_failed": "The machine refused this project's key, so it asks for the password again.",
    "manager_down": "Mission Control is not running, so nothing can prove the machine is still authorized.",
    "released": "The test machine was released from this project.",
}


def usability(root: ProjectRoot, *, wait: bool = True) -> tuple[bool, str, str]:
    """(usable, code, plain message). Machine execution and evidence promotion need usable."""
    try:
        value = read(root)
    except Damaged as error:
        return False, "damaged", str(error)
    if value is None or not value.get("connected"):
        return False, "not_connected", (
            "No test machine is connected. Open Mission Control, press Connect on the Test machine card and enter "
            "the machine's address, user and password (you do this once; the agents keep working on everything else meanwhile).")
    if value.get("reauth_required"):
        return False, "reauth_required", (
            CAUSE_TEXT.get(str(value.get("reauth_cause")), "The machine needs you.")
            + " Open Mission Control and press Connect on the Test machine card; enter the password again.")
    auth = value.get("authorized_by") or {}
    if not manager_alive(auth):
        return False, "manager_down", (
            "Mission Control is not running, so the machine's authorization cannot be confirmed. Start the app and "
            "press Connect on the Test machine card to enter the password again.")
    if auth.get("heartbeat_at"):
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(auth["heartbeat_at"])).total_seconds()
        except ValueError:
            age = 0
        if wait and age > HEARTBEAT_STALE_SECONDS:
            # Alive but not refreshed (sleep/wake): wait a few seconds for the manager, then decide on identity alone (W4).
            deadline = time.monotonic() + HEARTBEAT_WAIT_SECONDS
            while time.monotonic() < deadline:
                time.sleep(0.2)
                try:
                    refreshed = (read(root) or {}).get("authorized_by") or {}
                except Damaged:
                    break
                if refreshed.get("heartbeat_at") != auth.get("heartbeat_at"):
                    break
    if value.get("baseline_state") != "complete":
        return False, "baseline_incomplete", (
            "The machine's tools are being changed or an install did not finish. Finish it with `machine-baseline --done "
            "--note <tools and versions found>` before certifying anything on the machine.")
    return True, "ok", ""


def machine_dependent(argv: list[str]) -> bool:
    return bool(argv) and str(argv[0]).startswith(MACHINE_COMMAND_PREFIX)


def require_usable(root: ProjectRoot, *, who: str = "", task: str = "") -> dict[str, Any]:
    """Raise a plain refusal (and make sure a card exists) unless the machine can be used. Returns the stamp."""
    ok, code, message = usability(root)
    if not ok:
        if code in {"not_connected", "damaged"}:
            request_card(root, who or "a certified command", message, task=task)
        raise MachineError(message, code)
    return stamp_of(read(root) or {})


def stamp_for_command(root: ProjectRoot, argv: list[str], *, who: str = "", task: str = "") -> dict[str, Any] | None:
    """None for a local-only command; the current stamp (or a refusal) for a machine-dependent one."""
    if not machine_dependent(argv):
        return None
    return require_usable(root, who=who, task=task)


def stamp_for_command_or_none(root: ProjectRoot, argv: list[str]) -> dict[str, Any] | None:
    """For comparisons (CTO, release): None for a local-only command; the current stamp, or a value no stamp can equal."""
    if not machine_dependent(argv):
        return None
    ok, code, _message = usability(root)
    return stamp_of(read(root) or {}) if ok else {"unusable": code}


def stamp_is_current(root: ProjectRoot, stamp: dict[str, Any] | None) -> bool:
    """True when machine evidence recorded with `stamp` may still be used."""
    if not stamp:
        return True
    ok, _code, _message = usability(root)
    if not ok:
        return False
    return stamp_of(read(root) or {}) == dict(stamp)


# -- the card and its waiters ----------------------------------------------------------
def request_card(root: ProjectRoot, who: str, reason: str, *, task: str = "") -> dict[str, Any]:
    """Delivery/Reviewer ask for a machine: the manager owns the card; no CTO is needed."""
    reason = " ".join(str(reason or "").split())[:500]

    def change(value: dict[str, Any]) -> None:
        request = value.get("request") or {}
        if not request.get("reason") or request.get("reason") != reason:
            value["request"] = {"id": uuid.uuid4().hex[:12], "reason": reason, "by": str(who)[:80], "task": str(task)[:120], "at": now()}
        waiter = {"who": str(who)[:80], "task": str(task)[:120], "revision": int(value.get("revision", 0)),
                  "epoch": int(value.get("auth_epoch", 0)), "at": now()}
        waiters = [w for w in value.get("waiters", []) if (w.get("who"), w.get("task")) != (waiter["who"], waiter["task"])]
        waiters.append(waiter)
        value["waiters"] = waiters[-MAX_WAITERS:]

    return (update(root, change) or {}).get("request", {})


def status_report(root: ProjectRoot) -> dict[str, Any]:
    """What `machine-status` prints for any role: refuses (non-usable) are data here, not errors."""
    ok, code, message = usability(root)
    value = None
    try:
        value = read(root)
    except Damaged:
        pass
    value = value or {}
    return {
        "usable": ok, "state": code, "message": message,
        "address": value.get("address", ""), "user": value.get("user", ""), "port": value.get("port", 22),
        "host_key_fingerprint": (value.get("host_key") or {}).get("fingerprint", ""),
        "key_path": value.get("key_path", ""), "known_hosts_path": value.get("known_hosts_path", ""),
        "revision": value.get("revision", 0), "auth_epoch": value.get("auth_epoch", 0),
        "tools_revision": value.get("tools_revision", 0), "baseline_state": value.get("baseline_state", ""),
        "reauth_required": bool(value.get("reauth_required", True)), "os": value.get("os", ""),
        "account_kind": value.get("account_kind", ""), "ready": value.get("ready") or {},
        "rule_revision": RULE_REVISION, "rule": RULE_TEXT,
    }


RULE_TEXT = (
    "Test machine rule (revision 2): the owner connects the machine in Mission Control (address, user, password, once; "
    "again only when its card asks; a Connect made while the project is paused stays valid after Resume). Never print a "
    "setup block and never ask the owner to install tools: install them yourself over ssh with the project's key, in short "
    "non-interactive steps under the machine lock. Before a tool, upgrade or reboot step run `machine-baseline --begin`, "
    "and `machine-baseline --done --note <tools and versions found>` after it (Delivery alone does the one step allowed "
    "before the lock: Python 3 on a macOS or Linux machine that lacks it; the Reviewer does no machine work until --done). "
    "Start every stretch of machine work with `machine-status`; if it says the machine needs the owner, run "
    "`need-test-machine --reason <what you need>` once and carry on with everything else. Lock version 2: a folder named "
    "<your lock folder>-v2 with an exclusive lock file your lock script keeps open for its whole run (Windows "
    "[System.IO.File]::Open(path,'OpenOrCreate','ReadWrite','None'); macOS and Linux Python fcntl.flock, children started "
    "by subprocess) and a holder.txt (v2 agent role pid start since) written by temp file and rename; the operating system "
    "frees the lock when the script dies, so retry only contention, for a bounded time, then fail naming the holder; never "
    "free a lock by age or by an agent id. If the OLD lock folder exists: fail naming it, the CTO asks the owner once to "
    "approve a restart, Delivery restarts the machine, requires a changed boot identity, removes it and takes the v2 lock. "
    "Cleanup stops only what the run record lists, restores only what the run created, and a leftover it cannot remove "
    "fails the run naming it; the script exits non-zero if the tests, the setup or the cleanup failed. Windows: copy files "
    "with scp, never source in the ssh command; run scripts with powershell -NoProfile -ExecutionPolicy Bypass -File; read "
    "a native command's result with & cmd; $LASTEXITCODE; native stderr is not an error; user names are at most 20 "
    "characters and Remove-LocalUser throws on a missing user; the default ssh shell is cmd.exe; print the runner's "
    "`Ran <n> tests` line. Reviewer: your first machine row is Delivery's certified command text, further rows are "
    "test-<name> targets or arguments of the same route. A scenario the machine cannot reproduce is recorded once as "
    "`not reproducible here: <reason>` with an alternative proof that supplements a required real run (an unmet "
    "real-execution requirement needs real evidence or the owner's recorded waiver); never create accounts, services, "
    "tasks or registry entries on the owner's machine to force it."
)


def launch_note(root: ProjectRoot) -> str:
    """One paragraph for every fresh launch, restart and resume of an agent (S8): the connection and the current rule.
    Empty when the project has no test machine record, so projects that never use one see nothing."""
    try:
        value = read(root)
    except Damaged:
        return "[test-machine] The project's test machine record is damaged: run machine-status, then need-test-machine if you need the machine. " + RULE_TEXT
    if value is None:
        return ""
    report = status_report(root)
    where = f"{report['user']}@{report['address']}:{report['port']}" if report["address"] else "(not connected yet)"
    return f"[test-machine] Machine {where}, revision {report['revision']}, state {report['state']}. {RULE_TEXT}"


# -- baseline markers (X2) -----------------------------------------------------------------
def baseline_begin(root: ProjectRoot, who: str, note: str = "") -> dict[str, Any]:
    ok, code, message = usability_for_provisioning(root)
    if not ok:
        raise MachineError(message, code)

    def change(value: dict[str, Any]) -> None:
        if not value.get("connected"):   # decided again under the lock: a Release may have arrived since the check above
            raise MachineError("No test machine is connected. Run `need-test-machine` and wait for the owner's Connect.", "not_connected")
        value["tools_revision"] = int(value.get("tools_revision", 0)) + 1
        value["baseline_state"] = "incomplete"
        value["ready"] = {}
        value["baseline"] = {"state": "incomplete", "by": str(who)[:80], "at": now(), "note": str(note)[:500]}

    return stamp_of(update(root, change, create=False) or {})


def baseline_done(root: ProjectRoot, who: str, note: str) -> dict[str, Any]:
    note = " ".join(str(note or "").split())
    if len(note) < 8:
        raise MachineError("machine-baseline --done needs --note naming the tools and versions now installed on the machine.", "invalid")
    ok, code, message = usability_for_provisioning(root)
    if not ok:
        raise MachineError(message, code)

    def change(value: dict[str, Any]) -> None:
        if not value.get("connected"):
            raise MachineError("No test machine is connected. Run `need-test-machine` and wait for the owner's Connect.", "not_connected")
        if value.get("baseline_state") != "incomplete":
            raise MachineError("There is no open `machine-baseline --begin` to finish.", "invalid")
        value["baseline_state"] = "complete"
        value["baseline"] = {"state": "complete", "by": str(who)[:80], "at": now(), "note": note[:1000]}
        value["ready"] = {"state": True, "note": note[:1000], "tools_revision": int(value.get("tools_revision", 0)), "at": now(), "by": str(who)[:80]}

    return stamp_of(update(root, change, create=False) or {})


def usability_for_provisioning(root: ProjectRoot) -> tuple[bool, str, str]:
    """Recovery provisioning is allowed while the baseline is incomplete AND while the password is required again (U4): an agent's own
    ssh cannot be gated, so refusing the markers would only make it change the machine WITHOUT invalidating earlier evidence.
    It needs a machine that was connected once."""
    try:
        value = read(root)
    except Damaged as error:
        return False, "damaged", str(error)
    if value is None or not value.get("connected"):
        return False, "not_connected", "No test machine is connected. Run `need-test-machine` and wait for the owner's Connect."
    return True, "ok", ""


# -- ssh --------------------------------------------------------------------------------------
def _redact(text: str, secret: str) -> str:
    if secret:
        text = text.replace(secret, "***")
        text = text.replace(base64.b64encode(secret.encode()).decode(), "***")
    return text


def common_ssh_options(known_hosts: Path, port: int, user: str, address: str) -> list[str]:
    return [
        "-F", "/dev/null", "-p", str(port), "-l", user,
        "-o", "IdentitiesOnly=yes", "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none",
        "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", f"ConnectTimeout={int(max(3, PROBE_SECONDS))}",
        "-o", "LogLevel=ERROR", "-o", "CheckHostIP=no",
    ]


def ssh_program() -> str:
    return os.environ.get("HARNESS_TEST_MACHINE_SSH", "") or "ssh"


def keygen_program() -> str:
    return os.environ.get("HARNESS_TEST_MACHINE_SSH_KEYGEN", "") or "ssh-keygen"


def keyscan_program() -> str:
    return os.environ.get("HARNESS_TEST_MACHINE_SSH_KEYSCAN", "") or "ssh-keyscan"


_PTY_WRAPPER = (
    "import os,sys,fcntl,termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
)
_PASSWORD_PROMPT = re.compile(r"(?im)(?:^|\n)[^\n]*password[^\n]*:\s*$")
_OTHER_PROMPT = re.compile(r"(?im)(passphrase|verification code|one-time|authenticator|\bcode\b|\bpin\b)[^\n]*:\s*$")


class PtyResult:
    def __init__(self, returncode: int, text: str, prompted: bool, answered: bool, other_prompt: bool, timed_out: bool, cancelled: bool):
        self.returncode, self.text, self.prompted = returncode, text, prompted
        self.answered, self.other_prompt, self.timed_out, self.cancelled = answered, other_prompt, timed_out, cancelled


def run_with_password(
    argv: list[str], password: str, *, timeout: float = STEP_SECONDS, cancel: threading.Event | None = None,
) -> PtyResult:
    """Run ssh under a pty this process owns; answer ONE password prompt; nothing touches disk or the environment.

    The password goes only to the pty of the child we started. The child gets a minimal environment, the pty is
    closed and the child stopped (by the pid we recorded, never by pattern) in the finally block."""
    master, slave = os.openpty()
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "LC_ALL": "C", "HOME": os.environ.get("HOME", "/")}
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", _PTY_WRAPPER, *argv], stdin=slave, stdout=slave, stderr=slave,
            close_fds=True, env=environment, start_new_session=True,
        )
    except OSError as error:
        os.close(master)
        os.close(slave)
        raise MachineError(LOCAL_SSH_MISSING_TEXT, "local_ssh_missing") from error
    os.close(slave)
    pid = process.pid
    buffer = ""
    answered = prompted = other = timed_out = cancelled = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([master], [], [], min(0.25, remaining))
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", "replace")
                tail = buffer[-400:]
                if not answered and _PASSWORD_PROMPT.search(tail):
                    prompted = True
                    os.write(master, (password + "\n").encode())
                    answered = True
                    buffer += "\n"   # the prompt line is finished; the echo of the answer is never recorded
                elif answered and _OTHER_PROMPT.search(tail.split("\n")[-1] if tail else ""):
                    other = True
                    break
                elif not answered and _OTHER_PROMPT.search(tail):
                    other = True
                    break
            elif process.poll() is not None:
                break
        if process.poll() is None and (timed_out or cancelled or other):
            _stop_own(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _stop_own(process, force=True)
        # drain what is left
        try:
            while True:
                ready, _, _ = select.select([master], [], [], 0.05)
                if not ready:
                    break
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", "replace")
        except OSError:
            pass
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        if process.poll() is None:
            _stop_own(process, force=True)
    code = process.returncode if process.returncode is not None else -1
    return PtyResult(code, _redact(buffer, password).replace("\r\n", "\n").replace("\r", "\n"), prompted, answered, other, timed_out, cancelled)


def _stop_own(process: subprocess.Popen, force: bool = False) -> None:
    """Stop the one process this module started (it is its own session leader: pid == pgid). Never by pattern."""
    import signal
    try:
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        pass


def run_key_only(argv: list[str], *, timeout: float = STEP_SECONDS) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "LC_ALL": "C", "HOME": os.environ.get("HOME", "/")},
        )
    except FileNotFoundError as error:
        raise MachineError(LOCAL_SSH_MISSING_TEXT, "local_ssh_missing") from error
    except subprocess.TimeoutExpired:
        return 255, "Connection timed out"
    return completed.returncode, ((completed.stdout or "") + (completed.stderr or "")).replace("\r\n", "\n")


# -- failure classes ------------------------------------------------------------------------------
def classify(text: str, *, address: str, user: str, port: int, stage: str, prompted: bool = False,
             answered: bool = False, other_prompt: bool = False, timed_out: bool = False) -> MachineError:
    """Observed cause first, one plain sentence, the exact repair (S6)."""
    low = text.lower()
    where = f"{address}" + (f" port {port}" if port != 22 else "")
    if "remote host identification has changed" in low or "host key verification failed" in low or "offending" in low:
        return MachineError(
            f"This machine's identity changed since it was last connected ({where}). If you rebuilt or restored the VM, press "
            "Confirm and enter the password. If you did not, stop: it may not be your machine.", "host_key_changed")
    if "could not resolve hostname" in low or "name or service not known" in low or "nodename nor servname" in low:
        return MachineError(f"This computer does not know the name {address}. Use the machine's number address (for example 192.168.64.5).", "address_wrong")
    if "connection refused" in low:
        return MachineError(
            f"{where} answered but is not accepting ssh connections. On the machine, install and start OpenSSH Server "
            "(Windows: Settings, Apps, Optional features, OpenSSH Server, then start the 'OpenSSH SSH Server' service), then press Connect again.",
            "ssh_server_down")
    if any(item in low for item in ("no route to host", "network is unreachable", "host is down")) or (timed_out and not prompted) or "timed out" in low:
        return MachineError(
            f"Nothing answered at {where}. Check that the machine is running, that the address is right, and that nothing blocks the connection.",
            "unreachable")
    if "no matching host key type" in low or "unable to negotiate" in low or "no matching key exchange" in low:
        return MachineError(f"This computer and {where} could not agree on a way to connect. The connection software (OpenSSH) on the test machine needs updating: ask whoever manages that machine to update OpenSSH, then press Connect again.", "negotiation")
    if other_prompt:
        return MachineError(
            f"{where} asks for something besides a password (a code or a passphrase). Mission Control can only use a plain password; "
            "turn off the extra sign-in step for that account.", "unsupported_auth")
    if "permission denied" in low:
        if stage == "key":
            return MachineError(
                f"{where} accepted the password but then refused this project's key. Press Connect again to reinstall it; if it keeps "
                "failing, the test machine cannot use the automatic sign-in that Mission Control installed: ask the machine's administrator to repair it.", "key_refused", needs_password=True)
        if answered:
            return MachineError(f"The password was not accepted for {user} on {where}. Check the account name and the password.", "bad_password", needs_password=True)
        return MachineError(
            f"{where} does not allow password sign-in for {user}. Mission Control needs password sign-in to connect it. Ask the machine's administrator to turn on password sign-in for this account "
            "(in OpenSSH, PasswordAuthentication yes), check the account name, then press Connect again.", "password_disabled")
    if timed_out:
        return MachineError(f"The machine at {where} did not finish in time. Try again; if it repeats, restart the machine.", "unreachable")
    return MachineError(f"The connection to {where} failed: {' '.join(text.split())[-160:] or 'no details'}. Check that the machine is running; if it "
                        f"keeps failing, open a Terminal on this computer and run ssh {user + '@' if user else ''}{address} to see the message in full.", "failed")


_INSTALL_OPENSSH = (" Install the OpenSSH client on this computer (it is built into macOS and Windows 10 and later; on Linux install the "
                    "openssh-client package), then press Connect again.")
LOCAL_SSH_MISSING_TEXT = "This computer could not start the ssh program, so it cannot reach the machine." + _INSTALL_OPENSSH
LOCAL_KEYSCAN_MISSING_TEXT = "This computer could not start the ssh-keyscan program, so it cannot read the machine's identity." + _INSTALL_OPENSSH
LOCAL_KEYGEN_MISSING_TEXT = "This computer could not start the ssh-keygen program, so it cannot make the project's key." + _INSTALL_OPENSSH


# -- host key ------------------------------------------------------------------------------------------
def tcp_probe(address: str, port: int) -> None:
    """Distinguish wrong name / refused / nothing answering before ssh is involved."""
    try:
        infos = socket.getaddrinfo(address, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise MachineError(f"This computer does not know the name {address}. Use the machine's number address (for example 192.168.64.5).", "address_wrong") from error
    last: Exception | None = None
    for family, kind, proto, _canon, sockaddr in infos[:3]:
        try:
            with socket.socket(family, kind, proto) as sock:
                sock.settimeout(PROBE_SECONDS)
                sock.connect(sockaddr)
                return
        except ConnectionRefusedError as error:
            last = error
            raise classify("connection refused", address=address, user="", port=port, stage="probe") from error
        except OSError as error:
            last = error
    raise classify("timed out", address=address, user="", port=port, stage="probe", timed_out=True) from last


def fingerprint_of(blob_b64: str) -> str:
    raw = base64.b64decode(blob_b64.encode())
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


_KEY_PREFERENCE = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "rsa-sha2-512", "ssh-rsa")


def _keyscan(address: str, port: int, kinds: str) -> dict[str, str]:
    try:
        completed = subprocess.run(
            [keyscan_program(), "-T", str(int(max(3, PROBE_SECONDS))), "-p", str(port), "-t", kinds, address],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=PROBE_SECONDS * 3,
        )
    except FileNotFoundError as error:
        raise MachineError(LOCAL_KEYSCAN_MISSING_TEXT, "local_ssh_missing") from error
    except subprocess.TimeoutExpired:
        return {}
    keys: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#"):
            keys.setdefault(parts[1], parts[2])
    return keys


def after_scan(error: MachineError, address: str, port: int, stage: str) -> MachineError:
    """A host-key refusal at any stage AFTER the scan is not "changed since last connected": the scan a moment ago already compared
    the identity with the saved one (and the page offered Confirm when it differed). ssh could not verify the key that was JUST
    scanned, so the owner is told to read it again, never sent to Confirm (2026-10-21, plan check rounds 1-2)."""
    if error.code != "host_key_changed" or stage == "scan":
        return error
    where = f"{address}" + (f" port {port}" if int(port) != 22 else "")
    return MachineError(
        f"The identity read from {where} a moment ago did not match at sign-in. Press Connect again to read it afresh; "
        "if it happens twice, the address may reach more than one machine.", "host_key_unverified")


def known_hosts_host(address: str, port: int) -> str:
    """The host field ssh looks up in a known_hosts file: the bare address on the default port, `[address]:port` on any other.
    (A `[address]:22` entry is never matched: ssh reports "No ... host key is known" and strict checking refuses the machine,
    which Connect then reported as a changed identity on every normal Windows VM, 2026-10-21.)"""
    return address if int(port) == 22 else f"[{address}]:{int(port)}"


def scan_host_key(address: str, port: int) -> dict[str, str]:
    """Read the machine's identity with as few connections as possible (a server that penalises connections which end before sign-in,
    as OpenSSH 9.8+ does per source address, would otherwise start refusing us after a few scans): one ed25519 scan on the normal
    path; only when that finds nothing is the failure classified, and the other key types tried if the machine does answer."""
    found = _keyscan(address, port, "ed25519")
    if not found:
        tcp_probe(address, port)                      # names the cause (unknown name, refused, nothing answering) or returns
        found = _keyscan(address, port, "ecdsa,rsa")
    for kind in _KEY_PREFERENCE:
        if kind in found:
            blob = found[kind]
            return {"type": kind, "blob": blob, "fingerprint": fingerprint_of(blob),
                    "line": f"{known_hosts_host(address, port)} {kind} {blob}"}
    raise MachineError(f"{address} answered but did not present an ssh identity. Check that OpenSSH Server is running on port {port}.", "ssh_server_down")


# -- challenges (S2a/S7) -----------------------------------------------------------------------------------
class Challenges:
    """A password is accepted only for a challenge the manager issued with the exact destination and fingerprint shown."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def issue(self, project_id: str, address: str, user: str, port: int, host_key: dict[str, str], changed: bool, first_use: bool) -> dict[str, Any]:
        item = {
            "id": secrets.token_urlsafe(18), "project_id": project_id, "address": address, "user": user, "port": port,
            "host_key": dict(host_key), "changed": changed, "first_use": first_use,
            "expires": time.monotonic() + CHALLENGE_SECONDS,
        }
        with self._lock:
            self._items = {key: value for key, value in self._items.items() if value["expires"] > time.monotonic() and value["project_id"] != project_id}
            self._items[item["id"]] = item
        return item

    def consume(self, challenge_id: str, project_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._items.pop(str(challenge_id or ""), None)
        if not item or item["project_id"] != project_id or item["expires"] <= time.monotonic():
            raise MachineError("The machine check expired or was replaced. Press Connect again.", "challenge_expired")
        return item

    def void(self, project_id: str) -> None:
        with self._lock:
            self._items = {key: value for key, value in self._items.items() if value["project_id"] != project_id}


# -- one machine per project (U2/W5) ------------------------------------------------------------------------------
def _aliases(address: str) -> set[str]:
    names = {address.lower()}
    try:
        for info in socket.getaddrinfo(address, None, type=socket.SOCK_STREAM):
            names.add(info[4][0].lower())
    except OSError:
        pass
    return names


@contextmanager
def _registry_lock(home: Path) -> Iterator[None]:
    Path(home).mkdir(parents=True, exist_ok=True)
    with open(Path(home) / ".test_machine_targets.lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _targets_path(home: Path) -> Path:
    return Path(home) / "test_machine_targets.json"


def _entries_of(raw: Any) -> list[dict[str, Any]]:
    """A project holds up to two entries: its CONNECTED machine and, while a Connect runs, the PENDING one. Older files held one dict."""
    if isinstance(raw, dict):
        return [{**raw, "kind": raw.get("kind", "connected")}]
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _load_targets(home: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        value = json.loads(_targets_path(home).read_text())
    except (OSError, ValueError):
        return {}
    return {project: _entries_of(raw) for project, raw in value.items()} if isinstance(value, dict) else {}


def _save_targets(home: Path, targets: dict[str, list[dict[str, Any]]]) -> None:
    _targets_path(home).write_text(json.dumps({project: entries for project, entries in targets.items() if entries}, indent=2, sort_keys=True))


def reserve_target(home: Path, project_id: str, address: str, port: int, kind: str = "connected") -> None:
    """Atomic. Reduces accidental sharing; two forwarded ports to one VM and separate manager homes are not detected.

    A machine this project is connected to stays reserved while a replacement is attempted: the attempt holds a separate PENDING
    entry, promoted to connected only when the attempt is published (review round 3)."""
    mine = _aliases(address)
    with _registry_lock(home):
        targets = _load_targets(home)
        for other, entries in targets.items():
            if other == project_id:
                continue
            for entry in entries:
                if int(entry.get("port", 0)) == int(port) and mine & set(entry.get("aliases", [])):
                    raise MachineError(
                        "That machine is already connected to another project. Use one machine per project (a second VM is quick to make), "
                        "or release it from the other project first.", "machine_in_use")
        kept = [entry for entry in targets.get(project_id, []) if entry.get("kind") != kind]
        kept.append({"aliases": sorted(mine), "port": int(port), "kind": kind, "at": now()})
        targets[project_id] = kept
        _save_targets(home, targets)


def promote_reservation(home: Path, project_id: str) -> None:
    """The attempt was published: its pending machine becomes the connected one and the replaced machine is freed."""
    with _registry_lock(home):
        targets = _load_targets(home)
        pending = [entry for entry in targets.get(project_id, []) if entry.get("kind") == "pending"]
        if pending:
            targets[project_id] = [{**pending[-1], "kind": "connected"}]
            _save_targets(home, targets)


def settle_reservation(home: Path, project_id: str, connected: tuple[str, int] | None) -> None:
    """Make this project's reservations exactly its connected machine (or nothing); any pending entry is dropped."""
    with _registry_lock(home):
        targets = _load_targets(home)
        if connected is None:
            targets.pop(project_id, None)
        else:
            address, port = connected
            mine = _aliases(address)
            current = [entry for entry in targets.get(project_id, []) if entry.get("kind") == "connected"
                       and int(entry.get("port", 0)) == int(port) and mine & set(entry.get("aliases", []))]
            targets[project_id] = current or [{"aliases": sorted(mine), "port": int(port), "kind": "connected", "at": now()}]
        _save_targets(home, targets)


def release_target(home: Path, project_id: str) -> bool:
    with _registry_lock(home):
        targets = _load_targets(home)
        existed = bool(targets.pop(project_id, None))
        _save_targets(home, targets)
    return existed


def reconcile_reservation(root: ProjectRoot, home: Path, project_id: str) -> None:
    """After a failed or cancelled Connect: make the reservations say what the project's record says, decided under the descriptor lock.

    The record is the authority. A project that is connected keeps exactly its connected machine (its pending replacement is dropped,
    and the machine it was connected to was never given up); one that is not connected holds nothing. A Release takes the same lock,
    so a cancelled attempt can never put back a machine that a Release just freed (review rounds 2 and 3)."""
    with _locked(root):
        try:
            value = _read_unlocked(root)
        except Damaged:
            value = None
        if value and value.get("connected") and value.get("address"):
            settle_reservation(home, project_id, (str(value["address"]), int(value.get("port", 22))))
        else:
            settle_reservation(home, project_id, None)


def reserved_for(home: Path, project_id: str) -> bool:
    with _registry_lock(home):
        return bool(_load_targets(home).get(project_id))


# -- install and verify ------------------------------------------------------------------------------------------
def posix_install_command(public_key: str) -> str:
    key = public_key.strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [A-Za-z0-9_.\-]+)?", key):
        raise MachineError("The project key is not in the expected form.", "internal")
    return (
        "umask 077; mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        "{ [ ! -s ~/.ssh/authorized_keys ] || [ -z \"$(tail -c1 ~/.ssh/authorized_keys)\" ] || echo >> ~/.ssh/authorized_keys; } && "
        f"{{ grep -qxF '{key}' ~/.ssh/authorized_keys || printf '%s\\n' '{key}' >> ~/.ssh/authorized_keys; }} && echo NMHP_INSTALL_OK"
    )


def windows_install_script(public_key: str) -> str:
    key = public_key.strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [A-Za-z0-9_.\-]+)?", key):
        raise MachineError("The project key is not in the expected form.", "internal")
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        f"$key = '{key}'\n"
        "$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())"
        ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)\n"
        "if ($isAdmin) { $dir = Join-Path $env:ProgramData 'ssh'; $file = Join-Path $dir 'administrators_authorized_keys' }"
        " else { $dir = Join-Path $HOME '.ssh'; $file = Join-Path $dir 'authorized_keys' }\n"
        "New-Item -ItemType Directory -Force -Path $dir | Out-Null\n"
        "if (-not (Test-Path -LiteralPath $file)) { New-Item -ItemType File -Path $file | Out-Null }\n"
        "$lines = @(Get-Content -LiteralPath $file -ErrorAction SilentlyContinue)\n"
        "if ($lines -notcontains $key) {\n"
        "  $raw = [IO.File]::ReadAllText($file)\n"
        "  $prefix = if ($raw.Length -gt 0 -and -not $raw.EndsWith(\"`n\")) { \"`r`n\" } else { '' }\n"
        "  [IO.File]::AppendAllText($file, $prefix + $key + \"`r`n\")\n"
        "}\n"
        "if ($isAdmin) { & icacls.exe $file /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null }"
        " else { & icacls.exe $file /inheritance:r /grant ($env:USERNAME + ':F') /grant 'SYSTEM:F' | Out-Null }\n"
        "Write-Output ('NMHP_INSTALL_OK admin=' + $isAdmin)\n"
    )


def powershell_command(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    return f"powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}"


PROBE_WINDOWS_CMD = "echo NMHPOS:%OS%"
PROBE_POSIX = "uname -s"


class Connector:
    """One Connect: the steps, in order, each recorded so a retry knows what is confirmed and what is unknown."""

    def __init__(self, root: ProjectRoot, home: Path, project_id: str, manager: dict[str, Any],
                 progress: Callable[[str], None] | None = None, cancel: threading.Event | None = None):
        self.root, self.home, self.project_id = root, Path(home), project_id
        self.manager, self.progress = manager, progress or (lambda _text: None)
        self.cancel = cancel or threading.Event()
        self._start_seq = 0

    # paths
    def paths(self) -> dict[str, Path]:
        directory = machine_dir(self.root)
        return {"dir": directory, "key": directory / "key", "pub": directory / "key.pub", "known_hosts": directory / "known_hosts",
                "staging": directory / "known_hosts.attempt"}

    def _step(self, name: str, state: str) -> None:
        def change(value: dict[str, Any]) -> None:
            value.setdefault("steps", {})[name] = {"state": state, "at": now()}
        update(self.root, change)

    def ensure_key(self) -> str:
        paths = self.paths()
        paths["dir"].mkdir(parents=True, exist_ok=True)
        os.chmod(paths["dir"], 0o755)
        if not paths["key"].exists() or not paths["pub"].exists():
            for stale in (paths["key"], paths["pub"]):
                if stale.exists():
                    stale.unlink()
            try:
                completed = subprocess.run(
                    [keygen_program(), "-q", "-t", "ed25519", "-N", "", "-C", f"nmhp-{self.project_id[:8]}", "-f", str(paths["key"])],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
            except FileNotFoundError as error:
                raise MachineError(LOCAL_KEYGEN_MISSING_TEXT, "local_ssh_missing") from error
            if completed.returncode != 0:
                raise MachineError("This computer could not make the project's key: " + " ".join((completed.stderr or "").split())[:160], "failed")
        os.chmod(paths["key"], 0o600)   # ssh refuses a more open key; agents run as the same user and read it, their writes to this folder are confined
        return paths["pub"].read_text().strip()

    def run(self, challenge: dict[str, Any], password: str, confirm_changed: bool) -> dict[str, Any]:
        address, user, port = challenge["address"], challenge["user"], int(challenge["port"])
        host_key = challenge["host_key"]
        if challenge.get("changed") and not confirm_changed:
            raise classify("host key verification failed", address=address, user=user, port=port, stage="scan")
        if self.cancel.is_set():
            raise MachineError("Connect was stopped (the project was paused, closed or restarted). Nothing was kept.", "cancelled")
        paths = self.paths()
        before = (read(self.root) or {}) if descriptor_path(self.root).exists() else {}
        was_connected = bool(before.get("connected"))
        reserve_target(self.home, self.project_id, address, port, "pending")   # the working machine, if any, stays reserved meanwhile
        # The attempted target is kept apart from the connected one: a failed attempt never edits a working record. Its host-key pin is
        # STAGED in its own file and only replaces the published one together with the descriptor, after every step succeeded.
        start_seq = 0
        try:
            started = update(self.root, lambda v: v.update({"attempt": {"address": address, "user": user, "port": port}, "steps": {}})) or {}
            start_seq = int(started.get("stop_seq", 0))
            self._start_seq = start_seq
            self._still_wanted()
            self._step("scanned", "confirmed")
            paths["staging"].write_text(host_key["line"] + "\n")
            os.chmod(paths["staging"], 0o644)
            public_key = self.ensure_key()
            base = [ssh_program(), *common_ssh_options(paths["staging"], port, user, address)]
            password_options = ["-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "PubkeyAuthentication=no",
                                "-o", "NumberOfPasswordPrompts=1", "-o", "BatchMode=no", "-o", "KbdInteractiveAuthentication=yes"]
            self.progress("Signing in with the password")
            # 1) what kind of machine is it (also the password proof)
            probe = run_with_password([*base, *password_options, address, PROBE_WINDOWS_CMD], password, cancel=self.cancel)
            self._check(probe, address, user, port, "password")
            operating_system = "windows" if "NMHPOS:Windows_NT" in probe.text else ""
            if not operating_system:
                probe = run_with_password([*base, *password_options, address, PROBE_POSIX], password, cancel=self.cancel)
                self._check(probe, address, user, port, "password")
                kind = probe.text.strip().splitlines()[-1].strip() if probe.text.strip() else ""
                if kind in {"Linux", "Darwin", "FreeBSD", "OpenBSD", "NetBSD"}:
                    operating_system = kind.lower()
                else:
                    # a Windows account whose default shell is PowerShell: cmd's %OS% was not expanded
                    shell_probe = run_with_password(
                        [*base, *password_options, address, powershell_command("Write-Output ('NMHPOS:' + $env:OS)")], password, cancel=self.cancel)
                    self._check(shell_probe, address, user, port, "password")
                    if "NMHPOS:Windows_NT" in shell_probe.text:
                        operating_system = "windows"
                    else:
                        raise MachineError(
                            f"The machine at {address} is not a kind Mission Control can set up automatically (it answered with: "
                            f"{' '.join(probe.text.split())[-80:] or 'nothing'}). Supported: Windows 10/11 or Server with OpenSSH Server, Linux, macOS.",
                            "wrong_platform")
            # 2) install the public key (idempotent; keeps existing keys and permissions)
            self._still_wanted()
            self._step("key_published", "unknown")
            self.progress("Installing the project's key")
            if operating_system == "windows":
                command = powershell_command(windows_install_script(public_key))
            else:
                command = posix_install_command(public_key)
            install = run_with_password([*base, *password_options, address, command], password, cancel=self.cancel)
            self._check(install, address, user, port, "password")
            if "NMHP_INSTALL_OK" not in install.text:
                raise MachineError(
                    f"Your password was accepted, but setting up automatic sign-in did not finish ({' '.join(install.text.split())[-200:] or 'no output'}). The machine is not connected yet. "
                    "Press Connect again; it is safe to repeat.", "install_failed")
            account_kind = "admin" if "admin=True" in install.text else ("standard" if operating_system == "windows" else "")
            self._step("key_published", "confirmed")
            # 3) a SEPARATE key-only connection proves the key (no password, no fallback)
            self.progress("Checking the automatic sign-in")
            self._still_wanted()
            identity = self.verify(address, user, port, operating_system, paths["staging"])
            self._still_wanted()
            self._step("verified", "confirmed")
        except BaseException as caught:
            error = caught if isinstance(caught, MachineError) else MachineError("The connection was interrupted.", "interrupted")
            paths["staging"].unlink(missing_ok=True)
            self._fail(error, was_connected and bool(challenge.get("changed")))
            reconcile_reservation(self.root, self.home, self.project_id)
            raise

        def finalize(value: dict[str, Any]) -> None:
            # Under the descriptor lock: a stop (pause, close, resume, restart) or a release that arrived while this attempt ran wins.
            if self.cancel.is_set() or int(value.get("stop_seq", 0)) != start_seq:
                raise MachineError("Connect was stopped (the project was paused, closed or restarted). Nothing was kept.", "cancelled")
            os.replace(paths["staging"], paths["known_hosts"])
            promote_reservation(self.home, self.project_id)   # pending -> connected, the replaced machine is freed, under this same lock
            value.update({
                "connected_at_stop_seq": start_seq, "handoff_pending": int(value.get("auth_epoch", 0)) + 1,
                "connected": True, "address": address, "user": user, "port": port, "host_key": host_key,
                "key_path": str(paths["key"]), "known_hosts_path": str(paths["known_hosts"]),
                "revision": int(value.get("revision", 0)) + 1, "auth_epoch": int(value.get("auth_epoch", 0)) + 1,
                "reauth_required": False, "reauth_cause": "", "authorized_by": authorization_record(self.manager),
                "os": operating_system, "account_kind": account_kind, "ready": {}, "request": {},
                "rule_revision": RULE_REVISION, "last_attempt": {"at": now(), "ok": True, "identity": identity},
                "connected_at": now(),
            })

        try:
            final = update(self.root, finalize) or {}
        except BaseException as caught:
            paths["staging"].unlink(missing_ok=True)
            error = caught if isinstance(caught, MachineError) else MachineError("The connection was interrupted.", "interrupted")
            self._fail(error, False)
            reconcile_reservation(self.root, self.home, self.project_id)
            raise
        return final

    def _still_wanted(self) -> None:
        """Raise when a stop or a release arrived since this attempt began (checked between every step and again under the lock)."""
        if self.cancel.is_set() or int((read(self.root) or {}).get("stop_seq", 0)) != self._start_seq:
            raise MachineError("Connect was stopped (the project was paused, closed or restarted). Nothing was kept.", "cancelled")

    def verify(self, address: str, user: str, port: int, operating_system: str, known_hosts: Path | None = None) -> str:
        paths = self.paths()
        base = [ssh_program(), *common_ssh_options(known_hosts or paths["known_hosts"], port, user, address),
                "-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no",
                "-o", "KbdInteractiveAuthentication=no", "-i", str(paths["key"])]
        command = "echo NMHPKEY:%OS%:%USERNAME%" if operating_system == "windows" else "echo NMHPKEY:$(uname -s):$(id -un)"
        code, text = run_key_only([*base, address, command])
        if code != 0 or "NMHPKEY:" not in text:
            raise after_scan(classify(text or "Permission denied", address=address, user=user, port=port, stage="key"), address, port, "key")
        line = [item for item in text.splitlines() if "NMHPKEY:" in item][-1]
        return line.split("NMHPKEY:", 1)[1].strip()

    def _check(self, result: PtyResult, address: str, user: str, port: int, stage: str) -> None:
        if result.cancelled:
            raise MachineError("Connect was stopped (the project was paused, closed or restarted). Nothing was kept.", "cancelled")
        if result.returncode != 0 or result.timed_out or result.other_prompt:
            raise after_scan(classify(result.text, address=address, user=user, port=port, stage=stage, prompted=result.prompted,
                                      answered=result.answered, other_prompt=result.other_prompt, timed_out=result.timed_out),
                             address, port, stage)

    def _fail(self, error: MachineError, pin_replaced_on_a_connected_machine: bool = False) -> None:
        def change(value: dict[str, Any]) -> None:
            value["last_attempt"] = {"at": now(), "ok": False, "code": error.code, "message": str(error)[:400]}
        update(self.root, change)
        if pin_replaced_on_a_connected_machine:
            # the saved identity file now holds the new key but the record still names the old one: never leave that authorized
            mark_reauth(self.root, "key_failed")


def verify_key_login_detail(root: ProjectRoot) -> tuple[bool, str, str]:
    """Key-only check of the connected machine: (ok, code, the observed plain message). Never uses a password."""
    value = read(root) or {}
    if not value.get("connected"):
        return False, "not_connected", "No machine is connected yet."
    connector = Connector(root, Path("."), "", {})
    try:
        connector.verify(value["address"], value["user"], int(value["port"]), value.get("os", ""))
        return True, "ok", ""
    except MachineError as error:
        return False, error.code, str(error)


def verify_key_login(root: ProjectRoot) -> tuple[bool, str]:
    ok, code, _message = verify_key_login_detail(root)
    return ok, code


def diagnose(root: ProjectRoot) -> dict[str, Any]:
    """The observed cause for the card: reachable? same identity? does the key still work?"""
    value = read(root) or {}
    if not value.get("connected"):
        return {"cause": "not_connected", "message": "No machine is connected yet."}
    address, port = value["address"], int(value["port"])
    try:
        found = scan_host_key(address, port)
    except MachineError as error:
        return {"cause": error.code, "message": str(error)}
    if found["fingerprint"] != (value.get("host_key") or {}).get("fingerprint"):
        return {"cause": "host_key_changed", "message": str(classify("host key verification failed", address=address, user=value.get("user", ""), port=port, stage="scan")),
                "fingerprint": found["fingerprint"]}
    ok, code, observed = verify_key_login_detail(root)
    if ok:
        if not value.get("reauth_required"):
            return {"cause": "key_ok", "message": "The machine answers, its identity matches and the key works."}
        return {"cause": "key_ok", "message": "The machine answers and the key works. Only the password confirmation is missing."}
    if code == "key_refused":
        mark_reauth(root, "key_failed")   # only an observed refusal of the key; an unclassified failure proves nothing about the key
    if code in {"host_key_changed", "host_key_unverified"}:
        return {"cause": "host_key_unverified", "message": "The machine's identity matched a moment ago but could not be verified at sign-in. "
                "Press Connect again to read it afresh; if it happens twice, the address may reach more than one machine."}
    if code == "key_refused":
        return {"cause": code, "message": "The machine answers but refused the key; enter the password to install it again."}
    # every other observed failure keeps its own plain sentence and its one next step (timeout, address, ssh missing here, refused port ...)
    return {"cause": code, "message": observed or "The key check could not finish and the machine gave no details; check that it is running, then press Check again."}


def release(root: ProjectRoot, home: Path, project_id: str) -> dict[str, Any]:
    """Free the machine: the record and the reservation change together under the descriptor lock (a running Connect is cancelled by
    the stop counter and, when it ends, reconciles to this record, so it cannot bring the old machine back)."""
    with _locked(root):
        try:
            value = _read_unlocked(root)
        except Damaged:
            value = None
        if value is not None:
            value["stop_seq"] = int(value.get("stop_seq", 0)) + 1   # a Connect that is running must not publish after a release
            for field in ("address", "user"):
                value[field] = ""
            value.update({"connected": False, "host_key": {}, "revision": int(value.get("revision", 0)) + 1,
                          "auth_epoch": int(value.get("auth_epoch", 0)) + 1, "reauth_required": True, "reauth_cause": "released",
                          "authorized_by": {}, "ready": {}, "request": {}, "os": "", "attempt": {}, "handoff_pending": 0})
            _write_unlocked(root, value)
        release_target(home, project_id)
    return {"released": True, "note": "The machine is free for another project. The key this project installed stays on that machine until you destroy it."}


PREREQUISITES = [
    "A computer you can reach over ssh with an ssh server running (Windows: Settings > Apps > Optional features > OpenSSH Server and start the \"OpenSSH SSH Server\" service; Help: Create The Windows Test Machine has the three PowerShell lines). Linux and macOS machines work too.",
    "An account on it that can sign in over ssh with a password, and a password you use only for this throwaway machine.",
    "This computer can reach it: ssh -F /dev/null -o PubkeyAuthentication=no account@address from a Terminal here works. Some projects need an Administrator account or extra tools; Delivery tells you before the first test.",
    "Size it as Help says (at least 2 cores, 4 GB, 64 GB; comfortable 4 cores, 8 GB, 100 GB); Delivery restates the exact needs, and the agents install the tools themselves.",
]


def public_status(root: ProjectRoot) -> dict[str, Any]:
    """Everything the Mission Control card needs; contains no secret. The manager itself never waits for its own heartbeat."""
    ok, code, message = usability(root, wait=False)
    try:
        value = read(root)
    except Damaged:
        value = None
    value = value or {}
    ready = value.get("ready") or {}
    state = "ready" if ok and ready.get("state") else "connected" if ok else code
    steps = value.get("steps") or {}
    return {
        "state": state, "usable": ok, "message": message,
        "connected": bool(value.get("connected")),
        # a machine that is not connected shows the last attempted target so the owner does not retype it
        "address": value.get("address") or (value.get("attempt") or {}).get("address", ""),
        "user": value.get("user") or (value.get("attempt") or {}).get("user", ""),
        "port": value.get("port") or (value.get("attempt") or {}).get("port", 22),
        "fingerprint": (value.get("host_key") or {}).get("fingerprint", ""),
        "revision": value.get("revision", 0), "auth_epoch": value.get("auth_epoch", 0),
        "reauth_required": bool(value.get("reauth_required", True)) if value else False,
        "reauth_cause": value.get("reauth_cause", ""), "cause_text": CAUSE_TEXT.get(str(value.get("reauth_cause")), ""),
        "baseline_state": value.get("baseline_state", ""), "os": value.get("os", ""), "account_kind": value.get("account_kind", ""),
        "ready": bool(ready.get("state")), "ready_note": ready.get("note", ""),
        "request": value.get("request") or {}, "waiters": value.get("waiters") or [], "recovering": value.get("recovering") or [],
        "steps": {name: (steps.get(name) or {}).get("state", "unknown") for name in ("scanned", "key_published", "verified")},
        "last_attempt": value.get("last_attempt") or {}, "prerequisites": PREREQUISITES,
    }

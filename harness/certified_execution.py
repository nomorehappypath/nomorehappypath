# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Single-flight execution and exact successful-result reuse."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterator, Mapping

from harness import browser_acceptance, child_process, execution_identity, agent_confinement, test_machine


_POSITIVE_COUNT_PATTERNS = (
    re.compile(r"\bRan\s+(\d+)\s+tests?\b|\b(\d+)\s+passed\b", re.I),   # unittest, pytest, jest, vitest, cargo summary
    re.compile(r"^\s*(?:#|\u2139)\s*tests\s+(\d+)", re.I | re.M),            # node --test / TAP
    re.compile(r"\brunning\s+(\d+)\s+tests?\b", re.I),                       # cargo test
    re.compile(r"\bTests\s+run:\s*(\d+)", re.I),                             # JUnit / Maven
)
_GO_PASS_LINE = re.compile(r"^\s*---\s+PASS:", re.M)
_GO_OK_LINE = re.compile(r"^ok\s+\S+\s+[\d.]+s\b(?!.*\((?:cached)\))", re.M)   # a cached Go result ran nothing now
_GRADLE_PASSED_LINE = re.compile(r"^.*>.*\bPASSED\b", re.M)
_ZERO_MARKERS = re.compile(
    r"\bNO TESTS RAN\b|\[no tests to run\]|\bno test files\b|Task\s+:\S*test\s+NO-SOURCE", re.I,
)


def executed_test_count(output: str) -> int | None:
    """How many tests did this command report running? None when it says nothing.

    One recogniser for every gate (certified runs, the review request, the
    release-health check): Python (unittest, pytest), node --test, jest,
    vitest, Go (-v PASS lines, plain "ok pkg"), Cargo, Gradle (PASSED lines)
    and JUnit. Explicit zero markers count as zero; Gradle UP-TO-DATE is not a run.
    """
    counts: list[int] = []
    for pattern in _POSITIVE_COUNT_PATTERNS:
        counts.extend(int(value) for found in pattern.findall(output)
                      for value in (found if isinstance(found, tuple) else (found,)) if value)
    go = len(_GO_PASS_LINE.findall(output)) or len(_GO_OK_LINE.findall(output))
    gradle = len(_GRADLE_PASSED_LINE.findall(output))
    counts.extend(value for value in (go, gradle) if value)
    if counts:
        return max(counts)
    return 0 if _ZERO_MARKERS.search(output) else None


def _reports_zero_executed_tests(output: str) -> bool:
    """Did this command run no tests at all?

    Recognised regardless of exit code: Python 3.12 signals it through code 5
    while 3.9 exits 0, so keying off the exit status alone gives a different
    answer on each interpreter for identical output.
    """
    count = executed_test_count(output)
    return count == 0 or bool(re.search(r"\bNO TESTS RAN\b", output, re.I))


class RetryableInterruption(ValueError):
    """The run was not certified and did not fail: ask again, no repair reason.

    Callers must not record it as a failed attempt (no stored failure, no
    failed Delivery evidence, no failed Reviewer attempt)."""


class ExecutionInProgress(RetryableInterruption):
    """Another run of the same identity holds the lock past the wait limit."""


class OwnerMachineActivity(RetryableInterruption):
    """The tests passed, but the owner's machine changed in a way the command
    cannot be proven to have caused (a permission prompt or default-browser
    change appeared while it ran). Nothing is certified; the owner's activity
    is not a test failure."""


# How long a duplicate request waits behind another run of the same identity
# (the command's own timeout starts only after the lock is held).
_LOCK_WAIT_SECONDS = 120.0
_LOCK_POLL_SECONDS = 0.1


def in_progress_message(limit: float, holder: str = "") -> str:
    return (
        "an identical certified run is still in progress, so this request did not wait any longer "
        f"(waited {int(limit)} seconds). No test failed. "
        + (f"{holder} " if holder else "")
        + "Ask again when it has finished; if it never does, end the harness process named above "
        "(the lock is released the moment that process exits) and ask again."
    )


def _read_holder(handle_path: Path) -> str:
    try:
        info = json.loads(handle_path.read_text(encoding="utf-8") or "{}")
        pid = int(info["pid"])
        os.kill(pid, 0)
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return f"It is held by harness process {pid} (lock taken {info.get('since', 'earlier')})."


def acquire_flock(handle: Any, path: Path, wait_seconds: float, *, holder_info: bool) -> None:
    """Take an exclusive flock, waiting at most wait_seconds; ExecutionInProgress on timeout.

    Only contention is retried: any other OS error (a filesystem without
    locking, say) propagates as itself instead of posing as a busy run."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ExecutionInProgress(in_progress_message(wait_seconds, _read_holder(path))) from None
            time.sleep(_LOCK_POLL_SECONDS)
    if holder_info:
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid(), "since": datetime.now(timezone.utc).isoformat()}))
            handle.flush()
        except OSError:
            pass


@contextmanager
def _identity_lock(root: Any, identity_sha256: str, wait_seconds: float | None = None) -> Iterator[None]:
    limit = _LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
    directory = execution_identity.board.board_dir(root) / "execution-locks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{identity_sha256}.lock"
    with path.open("a+") as handle:
        acquire_flock(handle, path, limit, holder_info=True)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _test_count(output: str) -> int | None:
    return executed_test_count(output)


def _protected_prompt_processes(table: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    protected = ("securityagent.app/", "coreservicesuiagent.app/", "tccaccessrequestor.app/")
    return {
        pid: dict(value) for pid, value in table.items()
        if any(marker in str(value.get("command", "")).lower() for marker in protected)
    }


def _write_process_audit(board_root: Any, audit: dict[str, Any]) -> dict[str, str]:
    payload = (json.dumps(audit, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    directory = execution_identity.board.board_dir(board_root) / "execution-audits"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json"
    if path.exists() and path.read_bytes() != payload:
        raise ValueError("process audit conflicts with its content digest")
    if not path.exists():
        path.write_bytes(payload)
    return {"path": str(path), "sha256": digest}


def _owner_home() -> Path:
    """The home folder of the user the worker runs as: whose login is protected.

    Deliberately not taken from the command's environment, which a ledger
    author controls; tests patch this function to point at a fixture home."""
    return Path.home()


_AMBIENT_PROBLEMS = (
    "a Keychain or permission prompt appeared during the certified command",
    "the default-browser handler changed during the certified command",
)


def _run_observed(
    command: str, execution_root: Path, environment: Mapping[str, str], timeout_seconds: int,
) -> tuple[int, str, dict[str, Any]]:
    """Execute once while independently observing descendants and owner UI."""
    baseline = browser_acceptance._process_table()
    baseline_apps = browser_acceptance._app_processes(baseline)
    baseline_prompts = _protected_prompt_processes(baseline)
    baseline_handlers = browser_acceptance._default_handlers_digest()
    # Security scan 2026-10-04, finding 2: an agent wrote this command, so it
    # runs inside the agent's own write limits (the project's trusted grant,
    # harness storage denied, the owner's login unreadable) - never with the
    # worker's full write authority. Refused, not run open, where the
    # platform cannot confine it.
    guarded = agent_confinement.confine_agent_command(
        ["/bin/sh", "-c", command], execution_root, home=_owner_home(),
        claude_config_dir=environment.get("CLAUDE_CONFIG_DIR") or None,
    )
    process = subprocess.Popen(
        guarded, cwd=execution_root, shell=False, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=dict(environment),
        start_new_session=True,
    )
    start_token = browser_acceptance._start_token(process.pid)
    if not start_token:
        process.kill()
        process.wait(timeout=5)
        raise ValueError("could not record certified command process identity: the command exited before the harness could read it, or the process table was unreadable. Run the same command again; if it keeps happening, say so in the evidence")
    # start_new_session=True made the child its own session and group leader, so its group id IS its pid. Asking the OS
    # (os.getpgid) races a command that has already exited under load and raised ProcessLookupError.
    pgid = process.pid
    owned: dict[int, dict[str, Any]] = {
        process.pid: {
            "pid": process.pid, "ppid": os.getpid(), "pgid": pgid,
            "start_token": start_token, "command": command,
        },
    }
    stopped = threading.Event()
    observer = threading.Thread(
        target=browser_acceptance._observe_owned,
        args=(owned, stopped, pgid),
        name=f"harness-certified-observer-{process.pid}", daemon=True,
    )
    observer.start()
    timed_out = False
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate(timeout=5)
    finally:
        stopped.set()
        observer.join(timeout=2)
    final_table = browser_acceptance._process_table()
    browser_acceptance._record_owned(owned, final_table, pgid)
    forbidden_apps = browser_acceptance._app_processes(owned)
    forbidden_apps.pop(process.pid, None)
    new_prompts = {
        pid: value for pid, value in _protected_prompt_processes(final_table).items()
        if pid not in baseline_prompts
    }
    owner_changes = []
    for pid, identity in baseline_apps.items():
        current = final_table.get(pid)
        if not current:
            owner_changes.append({"pid": pid, "change": "process exited"})
        elif current.get("start_token") != identity.get("start_token"):
            owner_changes.append({"pid": pid, "change": "PID was restarted"})
    for pid, identity in forbidden_apps.items():
        current = final_table.get(pid)
        if not current or current.get("start_token") != identity.get("start_token"):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    audit = {
        "version": 1,
        "root_process": {"pid": process.pid, "start_token": start_token, "pgid": pgid},
        "owned_processes_observed": list(owned.values()),
        "forbidden_owner_browser_descendants": forbidden_apps,
        "new_keychain_or_permission_prompts": new_prompts,
        "owner_browser_baseline_changes": owner_changes,
        "default_handlers_unchanged": browser_acceptance._default_handlers_digest() == baseline_handlers,
        "timed_out": timed_out,
    }
    problems = []
    if forbidden_apps:
        problems.append("an owner-browser application bundle was launched below the certified command")
    if new_prompts:
        problems.append("a Keychain or permission prompt appeared during the certified command")
    # owner_changes (the owner quit or relaunched their own browser meanwhile)
    # stay in the audit as information only: the harness never signals a
    # baseline process, so the command cannot have caused it (P2c).
    if not audit["default_handlers_unchanged"]:
        problems.append("the default-browser handler changed during the certified command")
    audit["ambient_problems"] = [p for p in problems if p in _AMBIENT_PROBLEMS]
    if timed_out:
        problems.append(f"internal-QA test command timed out after {timeout_seconds} seconds")
    audit["problems"] = problems
    return int(process.returncode or 0), (stdout + stderr).strip(), audit


def run(
    board_root: Any,
    execution_root: Path,
    command: str,
    *,
    candidate: dict[str, Any],
    environment_sha256: str,
    environment: Mapping[str, str],
    lockfile_digests: dict[str, str],
    role: str,
    gate: str,
    retry_reason: str = "",
    browser: dict[str, str] | None = None,
    timeout_seconds: int = 300,
    reuse: bool = True,
) -> dict[str, Any]:
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as error:
        raise ValueError(f"test command cannot be parsed: {error}") from error
    # A command that uses the owner's test machine is refused unless the machine is connected and authorized NOW, and its
    # result is stamped with the machine's revision, epoch and tools revision (checked again before anything is certified).
    machine = test_machine.stamp_for_command(board_root, argv, who=role)
    identity = execution_identity.command_run_identity(
        candidate, argv, ".", environment_sha256, lockfile_digests,
        role=role, gate=gate, browser=browser, machine=machine,
    )
    with _identity_lock(board_root, identity["sha256"]):
        if reuse:
            decision = execution_identity.lookup(board_root, identity)
        else:
            # Owner 2026-10-12: security evidence (S-SEC- rows) is never reused.
            decision = {"status": "miss", "reason": "security_evidence_never_reused", "diverged_fields": []}
        execution_identity.audit_decision(board_root, identity, decision)
        cached = execution_identity.load_output(decision["entry"]) if decision["status"] == "hit" else ""
        if decision["status"] == "hit" and browser_acceptance.blocked_note(cached):
            # A success stored before 2026-09-28 could carry a reported browser
            # failure (review r2 B3); it is never reused as a pass.
            decision = {"status": "miss", "reason": "cached_success_reported_a_blocked_browser"}
        if decision["status"] == "hit":
            output = cached
            timestamp = _at()
            return {
                "output": output,
                "measurement": {
                    "command": command,
                    "command_fingerprint": hashlib.sha256(command.encode()).hexdigest(),
                    "started_at": timestamp, "finished_at": timestamp,
                    "duration_seconds": 0.0, "exit_code": 0,
                    "cache_decision": "exact_success_reused",
                    "execution_identity": identity["sha256"],
                    "execution_record_id": decision["entry"].get("record_id", ""),
                    "process_audit": (decision["entry"].get("metadata") or {}).get("process_audit", {}),
                    **({"machine": machine} if machine else {}),
                },
            }
        if decision.get("reason") == "prior_execution_failed" and not retry_reason.strip():
            earlier = execution_identity.latest_failed_output(board_root, identity)
            raise ValueError(browser_acceptance.with_blocked_note(
                "retrying a failed certified command requires a non-empty repair reason"
                + (" | the earlier attempt ended: " + earlier.strip()[-300:] if earlier.strip() and not browser_acceptance.board_call_note(earlier) else ""),
                earlier,
            ))
        started_at = _at()
        started = datetime.now(timezone.utc)
        exit_code, output, process_audit = _run_observed(
            command, execution_root, child_process.with_live_board(environment, board_root), timeout_seconds,
        )
        audit_manifest = _write_process_audit(board_root, process_audit)
        if machine and not test_machine.stamp_is_current(board_root, machine):
            raise ValueError(
                "the test machine changed or needed the owner's password while this command ran, so its result was discarded "
                "(machine revision or authorization is no longer the one it started under). Run machine-status, then submit again.")
        if process_audit["timed_out"]:
            raise ValueError(browser_acceptance.with_blocked_note(
                f"internal-QA test command timed out after {timeout_seconds} seconds; partial output was not certified",
                output,
            ))
        finished_at = _at()
        duration = round((datetime.now(timezone.utc) - started).total_seconds(), 3)
        problem = ""
        real_problems = [p for p in process_audit["problems"] if p not in _AMBIENT_PROBLEMS]
        if process_audit["problems"] and not real_problems and exit_code == 0 and not browser_acceptance.blocked_note(output):
            # Only owner-machine activity the command cannot be shown to have
            # caused (a prompt, a handler change), and the command itself
            # succeeded: nothing is certified, nothing failed, ask again.
            execution_identity.audit_decision(board_root, identity, {
                "status": "interrupted_by_owner_machine_activity", "reason": "; ".join(process_audit["problems"]),
            })
            raise OwnerMachineActivity(
                "the tests passed, but the machine showed activity this run cannot be proven not to have caused ("
                + "; ".join(process_audit["problems"]).replace(" during the certified command", "")
                + "). Nothing was certified and nothing failed; ask again once the machine is idle."
            )
        if (
            exit_code != 0 and not real_problems
            and browser_acceptance.OWNER_MACHINE_ACTIVITY_MARKER in output
        ):
            # The acceptance browser inside the tests saw owner-machine activity
            # (a default-browser handler change) it cannot attribute: retryable.
            raise OwnerMachineActivity(
                "the screen check inside the tests saw the default-browser setting change while it ran. "
                "Nothing was certified and nothing failed; ask again once the machine is idle."
            )
        if process_audit["problems"]:
            exit_code = exit_code or 3
            problem = "; ".join(process_audit["problems"])
        # Review r2 B3: a check whose browser could not run inside the sandbox
        # is not a pass, even when its tests reported OK. Decided before the
        # result is certified, so no such run is ever stored as a success.
        blocked = browser_acceptance.blocked_note(output)
        if exit_code == 0 and blocked:
            exit_code = 4
            problem = f"the screen check could not run: {blocked}"
        count = _test_count(output)
        if exit_code == 0 and count == 0:
            exit_code = 2
            problem = "internal-QA test command reported zero executed tests"
        elif exit_code == 0 and count is None:
            exit_code = 2
            problem = "internal-QA output must report a positive executed-test count"
        digest = hashlib.sha256(output.encode()).hexdigest()
        try:
            certified = execution_identity.certify(
                board_root, identity, exit_code=exit_code,
                output_sha256=digest, duration_seconds=duration,
                retry_reason=retry_reason, output=output,
                metadata={"process_audit": audit_manifest},
            )
        except ValueError as conflict:
            # Owner 2026-10-12 (review non-blocking finding): a security row runs
            # fresh (reuse=False), so a scanner whose advisory changed can FAIL
            # where the same identity once passed. The earlier certified success
            # stays immutable; the new failure is reported with its real output
            # instead of an append-only bookkeeping error.
            if exit_code == 0 or "append-only conflict" not in str(conflict):
                raise
            certified = {"entry": {}}
        execution_identity.audit_decision(board_root, identity, {
            "status": "executed_and_certified" if exit_code == 0 else "executed_failed",
            "reason": retry_reason.strip() or problem,
        })
        measurement = {
            "command": command,
            "command_fingerprint": hashlib.sha256(command.encode()).hexdigest(),
            "started_at": started_at, "finished_at": finished_at,
            "duration_seconds": duration, "exit_code": exit_code,
            "cache_decision": "executed_and_certified",
            "execution_identity": identity["sha256"],
            "execution_record_id": certified["entry"].get("record_id", ""),
            "process_audit": audit_manifest,
            **({"machine": machine} if machine else {}),
        }
        # ZERO EXECUTED TESTS IS ITS OWN REFUSAL, checked before the exit code.
        # Python 3.12 signals "no tests ran" through exit code 5, so this branch
        # fired first there and reported "failed with exit code 5" — true,
        # useless, and hiding the real problem: the command tested NOTHING.
        # Python 3.9 exits 0 for the same command, which is why macOS never saw
        # it. board.py carries the same guard; both call sites need it because
        # either can be the one that runs the command.
        if _reports_zero_executed_tests(output):
            raise ValueError(browser_acceptance.with_blocked_note(
                "internal-QA test command reported zero executed tests", output,
            ))
        if exit_code != 0:
            raise ValueError(browser_acceptance.with_blocked_note(
                problem or f"internal-QA test command failed with exit code {exit_code}: {output[-500:]}", output,
            ))
        return {"output": output, "measurement": measurement}

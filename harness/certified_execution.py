# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
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
import threading
from typing import Any, Iterator, Mapping

from harness import browser_acceptance, execution_identity


@contextmanager
def _identity_lock(root: Any, identity_sha256: str) -> Iterator[None]:
    directory = execution_identity.board.board_dir(root) / "execution-locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{identity_sha256}.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _test_count(output: str) -> int | None:
    counts = [
        int(value)
        for pair in re.findall(
            r"\bRan\s+(\d+)\s+tests?\b|\b(\d+)\s+passed\b", output, re.I,
        )
        for value in pair if value
    ]
    return max(counts) if counts else None


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


def _run_observed(
    command: str, execution_root: Path, environment: Mapping[str, str], timeout_seconds: int,
) -> tuple[int, str, dict[str, Any]]:
    """Execute once while independently observing descendants and owner UI."""
    baseline = browser_acceptance._process_table()
    baseline_apps = browser_acceptance._app_processes(baseline)
    baseline_prompts = _protected_prompt_processes(baseline)
    baseline_handlers = browser_acceptance._default_handlers_digest()
    process = subprocess.Popen(
        command, cwd=execution_root, shell=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=dict(environment),
        start_new_session=True,
    )
    start_token = browser_acceptance._start_token(process.pid)
    if not start_token:
        process.kill()
        process.wait(timeout=5)
        raise ValueError("could not record certified command process identity")
    pgid = os.getpgid(process.pid)
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
    if owner_changes:
        problems.append("the owner-browser process baseline changed during the certified command")
    if not audit["default_handlers_unchanged"]:
        problems.append("the default-browser handler changed during the certified command")
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
) -> dict[str, Any]:
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as error:
        raise ValueError(f"test command cannot be parsed: {error}") from error
    identity = execution_identity.command_run_identity(
        candidate, argv, ".", environment_sha256, lockfile_digests,
        role=role, gate=gate, browser=browser,
    )
    with _identity_lock(board_root, identity["sha256"]):
        decision = execution_identity.lookup(board_root, identity)
        execution_identity.audit_decision(board_root, identity, decision)
        if decision["status"] == "hit":
            output = execution_identity.load_output(decision["entry"])
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
                },
            }
        if decision.get("reason") == "prior_execution_failed" and not retry_reason.strip():
            raise ValueError(
                "retrying a failed certified command requires a non-empty repair reason"
            )
        started_at = _at()
        started = datetime.now(timezone.utc)
        exit_code, output, process_audit = _run_observed(
            command, execution_root, environment, timeout_seconds,
        )
        audit_manifest = _write_process_audit(board_root, process_audit)
        if process_audit["timed_out"]:
            raise ValueError(
                f"internal-QA test command timed out after {timeout_seconds} seconds; partial output was not certified"
            )
        finished_at = _at()
        duration = round((datetime.now(timezone.utc) - started).total_seconds(), 3)
        problem = ""
        if process_audit["problems"]:
            exit_code = exit_code or 3
            problem = "; ".join(process_audit["problems"])
        count = _test_count(output)
        if exit_code == 0 and count == 0:
            exit_code = 2
            problem = "internal-QA test command reported zero executed tests"
        elif exit_code == 0 and count is None:
            exit_code = 2
            problem = "internal-QA output must report a positive executed-test count"
        digest = hashlib.sha256(output.encode()).hexdigest()
        certified = execution_identity.certify(
            board_root, identity, exit_code=exit_code,
            output_sha256=digest, duration_seconds=duration,
            retry_reason=retry_reason, output=output,
            metadata={"process_audit": audit_manifest},
        )
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
        }
        if exit_code != 0:
            detail = problem or f"internal-QA test command failed with exit code {exit_code}: {output[-500:]}"
            raise ValueError(detail)
        return {"output": output, "measurement": measurement}

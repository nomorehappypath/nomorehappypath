# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Upload the approved version to the project's own repository when the owner accepts (task 2026-11-03_github-upload-on-accept).

The owner's saved Accept (board.record_release_decision) writes a pending `upload` on the decision: the repository and branch the
project had at that moment and the reviewed head commit. This module finds the accepted commit in Git, pushes it, and records each
attempt on that same upload. It runs only inside the project's Mission Control process, under the owner's own account, with plain
`git` and the owner's own git configuration, so the computer's normal sign-in (Keychain, gh, ssh agent) works as in a terminal and
NMHP never reads or stores a credential. It never goes through the agents' git broker and no agent-reachable command starts it.

The push is idempotent: the same commit pushed again to the same branch changes nothing, an older commit is never forced over a
newer one, so an upload interrupted at any point is simply run again. Every byte of git output is redacted before it is saved.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from harness import board, project_registry, redaction
from harness.project_context import ProjectRoot, project_context

# Time limit for each network git command (push, fetch). Spec: 120 seconds. A module constant so tests can shorten it.
GIT_TIMEOUT_SECONDS = 120
# How much of one attempt's redacted git output is kept on the board.
OUTPUT_KEEP_CHARS = 2000
# Statuses of an upload record. "uploaded" is final; "failed" and "diverged" wait for the owner's "Upload again".
PENDING, RUNNING, UPLOADED, DIVERGED, FAILED = "pending", "running", "uploaded", "diverged", "failed"

# Git settings pinned for every command here. The owner's own credential helper and ssh setup are NOT touched (that is the point).
# The task repository's config file can be written by a Delivery agent, so the push and the fetch never run in it: they run in a
# fresh staging repository (see _attempt_push), and the few local commands that do read the task repository (merge-base,
# rev-list) get these pins so no setting there can start a program as the owner.
_SAFE_GIT = [
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.ext.allow=never", "-c", "core.pager=cat",
]

REASON_TIME = "The upload took longer than the time limit and was stopped."
REASON_SIGN_IN = "The repository refused the sign-in. Sign in to it with git on this computer, then press Upload again."
REASON_NOT_FOUND = "The repository or branch was not found at the address saved for this project. Check the address, then press Upload again."
REASON_NETWORK = "The repository could not be reached. Check the internet connection, then press Upload again."
REASON_OTHER = "The upload did not go through. Press Upload again; if it keeps failing, ask the CTO to look at the saved details."
REASON_DIVERGED = "The repository already has work this project does not have, so nothing was uploaded and nothing there was changed."

_active_lock = threading.Lock()
_active: dict[str, dict[str, Any]] = {}


def destination_for(settings_home: Path | None, project_id: str) -> dict[str, str] | None:
    """The project's upload destination from the trusted manager registry, read now (never from a board file).

    Takes the manager home and the project id the Mission Control was started with. Returns {"url", "branch"} or None when the
    project has none (or this Mission Control runs outside the manager). Raises ValueError when the registry cannot be read, so
    an Accept never silently records "no destination" because of a read failure.
    """
    if not settings_home or not project_id:
        return None
    try:
        entry = project_registry._find(project_registry.load(Path(settings_home)), project_id)
    except KeyError:
        return None
    except (OSError, ValueError) as error:
        raise ValueError(f"the project's upload settings could not be read: {error}") from None
    return project_registry.normalize_upload_destination(entry.get("upload_destination"))


def _git_environment() -> dict[str, str]:
    """The owner's own environment, minus git routing variables, with prompts switched off.

    Returns a copy of os.environ. GIT_TERMINAL_PROMPT=0 and GCM_INTERACTIVE=never make a missing sign-in fail at once instead of
    waiting for a password nobody can type; GIT_DIR and friends are removed so the command acts on the repository named by -C.
    """
    routing = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES"}
    environment = {key: value for key, value in os.environ.items() if key not in routing}
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    return environment


def _run_git(repository: str, arguments: list[str], timeout: float | None = None) -> tuple[int | None, str]:
    """Run one git command in `repository`; returns (exit code, combined output), or (None, output so far) on the time limit.

    A new process group lets the whole tree (git and the ssh or credential helper it started) be stopped at the time limit.
    """
    process = subprocess.Popen(
        ["git", *_SAFE_GIT, "-C", repository, *arguments], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=_git_environment(), start_new_session=True, text=True, errors="replace",
    )
    try:
        output, _ = process.communicate(timeout=GIT_TIMEOUT_SECONDS if timeout is None else timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        output, _ = process.communicate()
        return None, output or ""
    return process.returncode, output or ""


def _plain_reason(output: str) -> str:
    """Choose the plain sentence for a failed git command from its (already redacted) output."""
    text = output.lower()
    if any(word in text for word in (
        "authentication failed", "permission denied", "could not read username", "could not read password",
        "terminal prompts disabled", "invalid credentials", "error: 401", "error: 403", "publickey", "host key verification failed",
        "support for password authentication was removed",
    )):
        return REASON_SIGN_IN
    if any(word in text for word in (
        "repository not found", "not found", "does not appear to be a git repository", "does not exist", "error: 404",
        "couldn't find remote ref",
    )):
        return REASON_NOT_FOUND
    if any(word in text for word in (
        "could not resolve", "connection refused", "connection timed out", "timed out", "network is unreachable",
        "could not connect", "connection reset", "unable to access", "temporary failure in name resolution", "early eof",
    )):
        return REASON_NETWORK
    return REASON_OTHER


def _kept(output: str) -> str:
    """Redact git output and keep its last OUTPUT_KEEP_CHARS characters (redact first so a cut can never leave half a token)."""
    return redaction.redact(output)[-OUTPUT_KEEP_CHARS:]


def _is_ancestor(repository: str, older: str, newer: str) -> bool | None:
    """True/False for `older` being contained in `newer`; None when git cannot tell (a missing object)."""
    code, _ = _run_git(repository, ["merge-base", "--is-ancestor", older, newer], timeout=30)
    return True if code == 0 else False if code == 1 else None


def find_commit(repository: str, release_head: str, recorded_commit: str = "") -> str:
    """The commit the owner's Accept put into local main, found from Git; "" while main does not contain the reviewed head yet.

    Takes the task repository, the reviewed head commit saved with the Accept, and the commit the board's acceptance record
    names (if it survived). Returns that recorded commit when main contains it; otherwise the OLDEST commit on main's
    first-parent line that contains `release_head`, so a crash after main advanced but before the board saved the acceptance
    still uploads the right commit (a later accepted commit is never mistaken for this one).
    """
    if recorded_commit:
        if _is_ancestor(repository, recorded_commit, "refs/heads/main"):
            return recorded_commit
    if not release_head or not _is_ancestor(repository, release_head, "refs/heads/main"):
        return ""
    code, listing = _run_git(repository, ["rev-list", "--first-parent", "--reverse", "refs/heads/main"], timeout=60)
    commits = listing.split() if code == 0 else []
    # Containing release_head is monotone along a first-parent line (once true, true for every later commit), so a binary
    # search finds the oldest one with few git calls.
    low, high = 0, len(commits) - 1
    found = ""
    while low <= high:
        middle = (low + high) // 2
        if _is_ancestor(repository, release_head, commits[middle]):
            found, high = commits[middle], middle - 1
        else:
            low = middle + 1
    return found


def _attempt_push(repository: str, upload: dict[str, Any], commit: str) -> tuple[str, str, str]:
    """Push `commit` to the saved destination once; returns (outcome, plain reason, redacted git output kept).

    Outcome is "uploaded", "diverged" or "failed". No force, no named remote. After any refusal the branch is fetched and
    compared: a branch that already contains the commit counts as uploaded; a not-a-fast-forward refusal on a branch with other
    work is "diverged" and that branch is left exactly as it is; any other refusal is "failed" with the push's own reason.
    The network commands run in a new, empty staging repository that holds only the task repository's main: a credential
    helper, ssh command or include planted in the task repository's own config (an agent can write it) is never read while the
    owner's sign-in is in use. The staging repository is deleted afterwards.
    """
    staging = tempfile.mkdtemp(prefix="nmhp-upload-")
    try:
        code, output = _run_git(staging, ["init", "--bare", "--quiet"], timeout=30)
        if code == 0:
            # find_commit guarantees `commit` is on the task repository's main, so fetching main brings it in.
            code, output = _run_git(staging, ["fetch", "--no-tags", "--quiet", repository, "+refs/heads/main:refs/nmhp/main"])
        if code != 0:
            kept = _kept(output)
            return FAILED, (REASON_TIME if code is None else REASON_OTHER), kept
        code, output = _run_git(staging, ["push", "--porcelain", upload["url"], f"{commit}:refs/heads/{upload['branch']}"])
        kept = _kept(output)
        if code is None:
            return FAILED, REASON_TIME, kept
        if code == 0:
            return UPLOADED, "", kept
        lowered = output.lower()
        behind = "[rejected]" in lowered and ("non-fast-forward" in lowered or "fetch first" in lowered)
        # Every refusal is checked against what the branch holds now, not only "behind": a push cut off by a Pause or
        # restart can still finish on the server, and the next attempt is then refused with other words ("failed to update
        # ref", "reference already exists") although the commit is already there (review R2-1).
        code, fetched = _run_git(staging, ["fetch", "--no-tags", upload["url"], f"+refs/heads/{upload['branch']}:refs/nmhp/remote"])
        both = _kept(output + "\n" + fetched)
        contained = _is_ancestor(staging, commit, "refs/nmhp/remote") if code == 0 else None
        if contained:
            return UPLOADED, "", both
        if not behind:
            return FAILED, _plain_reason(kept), both
        if code is None:
            return FAILED, REASON_TIME, both
        if code != 0:
            return FAILED, _plain_reason(both), both
        return (FAILED, REASON_OTHER, both) if contained is None else (DIVERGED, REASON_DIVERGED, both)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _read_upload(root: ProjectRoot, task: str) -> dict[str, Any] | None:
    """A copy of what one run needs about `task`'s upload, or None when it has nothing to run (not accepted, not pending, paused)."""
    try:
        with board.locked_state(root, operation="read upload") as state:
            decision = (state.get("release_decisions") or {}).get(task) or {}
            upload = decision.get("upload")
            if decision.get("decision") != "accepted" or not isinstance(upload, dict) or upload.get("status") == UPLOADED:
                return None
            repository = str((state.get("task_repositories") or {}).get(task) or project_context(root).code_root)
            return {
                "upload": dict(upload), "repository": repository,
                "recorded_commit": str(((state.get("git_acceptances") or {}).get(task) or {}).get("commit") or ""),
            }
    except board.ProjectPausedError:
        return None


def _mark_running(root: ProjectRoot, task: str, commit: str) -> bool:
    """Save status "running" and a new attempt entry; False when the upload is already done or gone (nothing to run)."""
    with board.locked_state(root, operation="start upload") as state:
        upload = ((state.get("release_decisions") or {}).get(task) or {}).get("upload")
        if not isinstance(upload, dict) or upload.get("status") == UPLOADED:
            return False
        upload["commit"] = commit
        upload["status"] = RUNNING
        attempts = upload.setdefault("attempts", [])
        if attempts and attempts[-1].get("outcome") == RUNNING:
            attempts[-1].update({"outcome": "interrupted", "reason": "The app stopped before this attempt finished."})   # kept as evidence
        attempts.append({"started_at": board.now(), "outcome": RUNNING})
        return True


def _save_outcome(root: ProjectRoot, task: str, outcome: str, reason: str, output: str) -> None:
    """Close the latest attempt and set the upload's status; every earlier attempt stays as it was."""
    with board.locked_state(root, operation="finish upload") as state:
        upload = ((state.get("release_decisions") or {}).get(task) or {}).get("upload")
        if not isinstance(upload, dict):
            return
        attempts = upload.setdefault("attempts", [])
        if not attempts or attempts[-1].get("outcome") != RUNNING:
            attempts.append({"started_at": board.now()})
        attempts[-1].update({"finished_at": board.now(), "outcome": outcome, "reason": reason, "output": output})
        upload["status"] = outcome
        if outcome == UPLOADED:
            upload["uploaded_at"] = board.now()


def run_one(root: ProjectRoot, task: str) -> str:
    """Run one task's upload now; returns its status afterwards ("pending" when main does not hold the accepted commit yet, "" if there is none).

    Waiting (main lacks the reviewed head) saves nothing, so the card keeps its "not added yet" wording and the next wake tries again.
    """
    info = _read_upload(root, task)
    if info is None:
        return ""
    upload, repository = info["upload"], info["repository"]
    commit = find_commit(repository, str(upload.get("release_head") or ""), info["recorded_commit"])
    if not commit:
        return PENDING
    if not _mark_running(root, task, commit):
        return UPLOADED
    # An unexpected error here leaves the attempt "running"; the next start/open/Resume or "Upload again" runs it once more.
    outcome, reason, output = _attempt_push(repository, upload, commit)
    _save_outcome(root, task, outcome, reason, output)
    return outcome


def pending_tasks(root: ProjectRoot, *, include_stopped: bool) -> list[str]:
    """Tasks whose upload still has to run, oldest Accept first.

    `include_stopped` also returns uploads that ended "failed" or "diverged" (done on start, open and Resume, as the spec says
    every Accept whose upload is not "uploaded"); without it only "pending" and an interrupted "running" are returned, so one
    failed upload is not retried by an unrelated wake, only by the owner's "Upload again".
    """
    wanted = {PENDING, RUNNING} | ({FAILED, DIVERGED} if include_stopped else set())
    try:
        with board.locked_state(root, operation="list uploads") as state:
            found = [
                (str(decision["upload"].get("created_at") or ""), task)
                for task, decision in (state.get("release_decisions") or {}).items()
                if isinstance(decision, dict) and decision.get("decision") == "accepted" and isinstance(decision.get("upload"), dict)
                and decision["upload"].get("status", PENDING) in wanted
            ]
    except board.ProjectPausedError:
        return []
    return [task for _, task in sorted(found)]


def run_pending(root: ProjectRoot, *, include_stopped: bool = False) -> list[str]:
    """Run every due upload, one at a time in Accept order; returns the tasks it attempted.

    A per-project lock file makes a second Mission Control process (or thread) wait its turn, so two uploads never overlap and
    an older commit can never land after a newer one started.
    """
    lock_path = board.board_dir(root) / ".upload.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    ran: list[str] = []
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for task in pending_tasks(root, include_stopped=include_stopped):
            if run_one(root, task) in {UPLOADED, DIVERGED, FAILED}:
                ran.append(task)
    return ran


def _worker(root: ProjectRoot, key: str, entry: dict[str, Any]) -> None:
    """Background thread body: drain the due uploads, again if a wake arrived meanwhile, then end."""
    while True:
        with _active_lock:
            include_stopped, entry["include_stopped"], entry["again"] = entry["include_stopped"], False, False
        try:
            run_pending(root, include_stopped=include_stopped)
        except Exception as error:  # a background thread must never die loudly; the record stays pending/running for the next wake
            print(f"HARNESS UPLOAD | upload run stopped: {redaction.redact(error)[:300]}", flush=True)
        with _active_lock:
            if not entry["again"]:
                _active.pop(key, None)
                return


def _nothing_due(root: ProjectRoot, include_stopped: bool) -> bool:
    """True when the saved board plainly holds no due upload (or no board was saved yet); doubt (unreadable, odd shape) answers False.

    Read-only on purpose: most projects never configure a destination, and every start, open and Resume wakes this queue, so the
    common case must not take the board lock or start a thread that writes the board.
    """
    wanted = {PENDING, RUNNING} | ({FAILED, DIVERGED} if include_stopped else set())
    try:
        state = json.loads(board._state_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True   # no saved board yet, so no saved Accept and nothing to upload
    except (OSError, ValueError):
        return False
    try:
        decisions = state.get("release_decisions") or {}
        return not any(
            isinstance(decision, dict) and decision.get("decision") == "accepted" and isinstance(decision.get("upload"), dict)
            and decision["upload"].get("status", PENDING) in wanted
            for decision in decisions.values()
        )
    except AttributeError:
        return False


def wake(root: ProjectRoot, *, include_stopped: bool = False) -> threading.Thread:
    """Run due uploads in one background thread per project; returns that thread (tests join it).

    Called after the Accept response is saved and after the owner's "Upload again" (default), and on Mission Control start,
    project open and Resume with include_stopped=True. A wake while a run is under way makes it loop once more.
    """
    key = str(project_context(root).code_root)
    if _nothing_due(root, include_stopped) and not (_active.get(key) or {}).get("thread", threading.Thread()).is_alive():
        idle = threading.Thread(target=lambda: None, name="upload-on-accept-idle")
        idle.start()
        return idle
    with _active_lock:
        entry = _active.get(key)
        if entry and entry["thread"].is_alive():
            entry["again"] = True
            entry["include_stopped"] = entry["include_stopped"] or include_stopped
            return entry["thread"]
        entry = {"again": False, "include_stopped": include_stopped}
        entry["thread"] = threading.Thread(target=_worker, args=(root, key, entry), daemon=True, name="upload-on-accept")
        _active[key] = entry
        entry["thread"].start()
        return entry["thread"]


def retry(root: ProjectRoot, task: str) -> dict[str, Any]:
    """The owner's "Upload again": queue the same upload for another attempt and wake the worker.

    Takes the task id; returns the upload record. Raises ValueError when the task has no upload. Never creates a second upload:
    a failed or diverged one goes back to "pending" (its next run appends one more attempt); a pending, running or finished one
    is left as it is, so repeated presses do nothing extra.
    """
    with board.locked_state(root, operation="retry upload") as state:
        decision = (state.get("release_decisions") or {}).get(task) or {}
        upload = decision.get("upload")
        if decision.get("decision") != "accepted" or not isinstance(upload, dict):
            raise ValueError("this task has no upload to repeat")
        if upload.get("status") in {FAILED, DIVERGED}:
            upload["status"] = PENDING
        result = dict(upload)
    wake(root)
    return result

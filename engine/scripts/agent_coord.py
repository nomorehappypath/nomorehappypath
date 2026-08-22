#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Repo-local coordination helper for AI agents (vendor-neutral).

It manages:
- locks: active claims over files being edited
- inbox notes: messages one agent leaves for another
- branch/merge enforcement: land a task branch on main, capture the SHA, verify it

This is cooperative coordination, not security. Git remains the final
conflict detector. Agent names (e.g. the implementer/reviewer vendors) are passed
as arguments, never hardcoded, so any vendor pairing works.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().strftime("%Y%m%dT%H%M%SZ")


def _slug(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", text.strip().lower())
    return value.strip("-")[:48] or "note"


def _find_repo_root() -> Path:
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / ".agents").is_dir():
            return candidate
    raise SystemExit("No .agents directory found. Run from a repo root.")


def _agents_dir() -> Path:
    return _find_repo_root() / ".agents"


def _ensure_dirs() -> None:
    # Vendor-neutral: inbox subdirs are created per-recipient on demand by `note`.
    base = _agents_dir()
    for path in [
        base / "locks",
        base / "inbox",
        base / "archive",
    ]:
        path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_stale(lock: dict[str, Any]) -> bool:
    expires = _parse_time(lock.get("expires_at"))
    return bool(expires and expires < _now())


def _norm(path: str) -> str:
    return path.strip().strip("/").replace("\\", "/")


def _overlaps(a: str, b: str) -> bool:
    left = _norm(a)
    right = _norm(b)
    if not left or not right:
        return False
    return (
        left == right
        or left.startswith(right.rstrip("/") + "/")
        or right.startswith(left.rstrip("/") + "/")
    )


# ---------------------------------------------------------------------------
# Git helpers (branch / merge enforcement)
#
# These make git the substrate for the "every task branch must land on main"
# rule. The script does not replace git; it drives it and verifies the result.
# ---------------------------------------------------------------------------


def _git(args: list[str], repo: Path | None = None) -> subprocess.CompletedProcess:
    root = repo or _find_repo_root()
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
    )


def _git_ok(args: list[str], repo: Path | None = None) -> bool:
    return _git(args, repo).returncode == 0


def _dirty_paths(repo: Path | None = None) -> list[str]:
    """Uncommitted changes to tracked files OUTSIDE .agents/.

    Untracked files survive a checkout and are not part of the merge, and
    .agents/ holds tool-managed coordination state and task records, so neither
    should block a merge. Only real uncommitted code edits do.
    """
    dirty: list[str] = []
    for line in _git(["status", "--porcelain"], repo).stdout.splitlines():
        if not line.strip() or line[:2] == "??":
            continue
        path = line[3:].strip().strip('"')
        if path.startswith(".agents/"):
            continue
        dirty.append(line)
    return dirty


def _tree_clean(repo: Path | None = None) -> bool:
    return not _dirty_paths(repo)


def _commit_task_record(task: str, repo: Path | None = None) -> bool:
    """Commit the stamped task record so the merge leaves a clean tree.

    No-op (returns False) when the record is untracked/ignored or unchanged.
    """
    taskfile = _task_record_path(task)
    if not taskfile.exists():
        return False
    _git(["add", "--", str(taskfile)], repo)
    if _git_ok(["diff", "--cached", "--quiet"], repo):
        return False
    return _git(["commit", "-m", f"record: merge SHA for {task}"], repo).returncode == 0


def _branch_exists(name: str, repo: Path | None = None) -> bool:
    return _git_ok(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], repo)


def _remote_exists(remote: str, repo: Path | None = None) -> bool:
    return remote in _git(["remote"], repo).stdout.split()


def _rev(ref: str, repo: Path | None = None) -> str | None:
    return _git(["rev-parse", "--verify", "--quiet", ref], repo).stdout.strip() or None


def _is_ancestor(commit: str, ref: str, repo: Path | None = None) -> bool:
    return _git(["merge-base", "--is-ancestor", commit, ref], repo).returncode == 0


def _task_record_path(task: str) -> Path:
    return _agents_dir() / "tasks" / f"{task}.md"


def _read_recorded_sha(task: str) -> str | None:
    path = _task_record_path(task)
    if not path.exists():
        return None
    match = re.search(
        r"Merge commit SHA:\s*`([0-9a-fA-F]{7,40})`",
        path.read_text(encoding="utf-8"),
    )
    return match.group(1) if match else None


def _stamp_merge_record(task: str, sha: str, pushed: bool, deleted: bool) -> bool:
    """Write the real merge SHA / push / delete state into the task record.

    Best-effort: fills the template placeholders if present, otherwise appends
    an auto block. Returns True if the file was changed.
    """
    path = _task_record_path(task)
    if not path.exists():
        return False
    original = path.read_text(encoding="utf-8")
    text = re.sub(r"(Merge commit SHA:\s*`)<sha>(`)", rf"\g<1>{sha}\g<2>", original, count=1)
    text = re.sub(
        r"(Pushed to remote:\s*`)[^`]*(`)",
        rf"\g<1>{'YES' if pushed else 'NO'}\g<2>",
        text,
        count=1,
    )
    text = re.sub(
        r"(Branch deleted after merge:\s*`)[^`]*(`)",
        rf"\g<1>{'YES' if deleted else 'NO'}\g<2>",
        text,
        count=1,
    )
    if "Merge commit SHA:" not in original:
        text = original.rstrip() + (
            "\n\n## Merge record (auto)\n\n"
            f"- Merge commit SHA: `{sha}`\n"
            f"- Pushed to remote: `{'YES' if pushed else 'NO'}`\n"
            f"- Branch deleted after merge: `{'YES' if deleted else 'NO'}`\n"
        )
    if text != original:
        path.write_text(text, encoding="utf-8")
        return True
    return False


def _lock_files() -> list[Path]:
    return sorted((_agents_dir() / "locks").glob("*.json"))


def _load_locks() -> list[tuple[Path, dict[str, Any]]]:
    locks: list[tuple[Path, dict[str, Any]]] = []
    for path in _lock_files():
        data = _read_json(path)
        if data is not None:
            locks.append((path, data))
    return locks


def _conflicts(agent: str, files: list[str]) -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    for path, lock in _load_locks():
        if lock.get("agent") == agent or _is_stale(lock):
            continue
        locked_files = [str(item) for item in lock.get("files", [])]
        if any(_overlaps(target, locked) for target in files for locked in locked_files):
            found.append((path, lock))
    return found


def cmd_status(args: argparse.Namespace) -> int:
    _ensure_dirs()
    print(f"Repo: {_find_repo_root()}")
    print("\nLocks:")
    locks = _load_locks()
    if not locks:
        print("  none")
    for path, lock in locks:
        state = "STALE" if _is_stale(lock) else "active"
        files = ", ".join(lock.get("files", []))
        print(
            f"  {path.name}: {state} agent={lock.get('agent')} "
            f"task={lock.get('task')} files={files}"
        )

    if args.agent:
        inbox_dir = _agents_dir() / "inbox" / args.agent
        messages = sorted(inbox_dir.glob("*.md")) if inbox_dir.exists() else []
        print(f"\nInbox for {args.agent}:")
        if not messages:
            print("  none")
        for message in messages:
            print(f"  {message.name}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    _ensure_dirs()
    conflicts = _conflicts(args.agent, args.files)
    if not conflicts:
        print("No active lock conflicts.")
        return 0
    print("Active lock conflicts:")
    for path, lock in conflicts:
        print(f"  {path.name}: agent={lock.get('agent')} task={lock.get('task')}")
        for item in lock.get("files", []):
            print(f"    - {item}")
    return 2


def cmd_lock(args: argparse.Namespace) -> int:
    _ensure_dirs()
    conflicts = _conflicts(args.agent, args.files)
    if conflicts and not args.force:
        print("Refusing to create lock because active conflicts exist:")
        for path, lock in conflicts:
            print(f"  {path.name}: agent={lock.get('agent')} task={lock.get('task')}")
        print("Use --force only with explicit user approval.")
        return 2

    started = _now()
    expires = started + timedelta(hours=args.ttl_hours)
    data = {
        "agent": args.agent,
        "task": args.task,
        "files": [_norm(item) for item in args.files],
        "started_at": started.isoformat(),
        "expires_at": expires.isoformat(),
    }
    path = _agents_dir() / "locks" / f"{_stamp()}-{_slug(args.agent)}-{_slug(args.task)}.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Created lock: {path.relative_to(_find_repo_root())}")
    return 0


def _unmerged_task_branches(agent: str, repo: Path) -> list[tuple[str, str]]:
    """Best-effort: the agent's locked task branches not yet on main.

    Returns (task, branch) pairs. Never raises; returns [] if git is
    unavailable or nothing can be determined, so normal unlock never breaks.
    """
    out: list[tuple[str, str]] = []
    try:
        if not _git_ok(["rev-parse", "--git-dir"], repo):
            return []
        for _path, lock in _load_locks():
            if lock.get("agent") != agent or _is_stale(lock):
                continue
            task = str(lock.get("task") or "").strip()
            if not task:
                continue
            branch = f"task/{task}"
            if not _branch_exists(branch, repo):
                continue
            tip = _rev(branch, repo)
            if tip and not _is_ancestor(tip, "main", repo):
                out.append((task, branch))
    except Exception:
        return []
    return out


def cmd_unlock(args: argparse.Namespace) -> int:
    _ensure_dirs()
    repo = _find_repo_root()

    unmerged = _unmerged_task_branches(args.agent, repo)
    if unmerged:
        print("NOTE: these task branches are not yet merged into main:")
        for task, branch in unmerged:
            print(f"  - {branch} (task {task})")
        if args.require_merged:
            print(
                "Refusing to unlock with --require-merged set. "
                "Run `merge` first so the work lands on main."
            )
            return 2
        print(
            "Clearing the lock will not delete the branch, but finished work "
            "must be merged to main before ACCEPTANCE_READY "
            "(see the Branching & Merge Policy)."
        )

    removed = 0
    for path, lock in _load_locks():
        if lock.get("agent") == args.agent:
            path.unlink()
            removed += 1
            print(f"Removed lock: {path.name}")
    if removed == 0:
        print(f"No locks found for agent={args.agent}.")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    _ensure_dirs()
    inbox = _agents_dir() / "inbox" / args.to
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{_stamp()}-{_slug(args.from_agent)}-{_slug(args.message)}.md"
    files = "\n".join(f"- {item}" for item in args.files) if args.files else "- n/a"
    body = (
        f"To: {args.to}\n"
        f"From: {args.from_agent}\n"
        f"Created: {_now().isoformat()}\n"
        "Files:\n"
        f"{files}\n\n"
        f"{args.message.strip()}\n"
    )
    path.write_text(body, encoding="utf-8")
    print(f"Created note: {path.relative_to(_find_repo_root())}")
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    _ensure_dirs()
    inbox = _agents_dir() / "inbox" / args.agent
    messages = sorted(inbox.glob("*.md")) if inbox.exists() else []
    if not messages:
        print(f"Inbox for {args.agent}: empty")
        return 0
    for message in messages:
        print(f"\n--- {message.name} ---")
        print(message.read_text(encoding="utf-8").rstrip())
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    _ensure_dirs()
    src = _agents_dir() / "inbox" / args.agent / args.message
    if not src.exists():
        print(f"Message not found: {src}")
        return 1
    dest = _agents_dir() / "archive" / f"{args.agent}-{src.name}"
    shutil.move(str(src), str(dest))
    print(f"Archived: {dest.relative_to(_find_repo_root())}")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    """Land a task branch on main, capture the SHA, and record it.

    The single, auditable 'port to main' action. Refuses to run on a dirty
    tree, aborts cleanly on conflict, is idempotent if already merged, and
    never leaves a half-merged state silently.
    """
    _ensure_dirs()
    repo = _find_repo_root()
    if not _git_ok(["rev-parse", "--git-dir"], repo):
        print("Not a git repository.")
        return 2

    branch = args.branch or f"task/{args.task}"
    main = args.main
    remote = args.remote

    if not _branch_exists(branch, repo):
        recorded = _read_recorded_sha(args.task)
        if recorded and _is_ancestor(recorded, main, repo):
            print(
                f"Branch {branch} not found, but recorded SHA {recorded[:12]} "
                f"is already in {main}. Nothing to do."
            )
            return 0
        print(f"Task branch not found: {branch}")
        return 2

    branch_tip = _rev(branch, repo)
    if branch_tip and _is_ancestor(branch_tip, main, repo):
        print(f"Already merged: {branch} tip {branch_tip[:12]} is in {main}.")
        deleted = False if args.keep_branch else _git_ok(["branch", "-d", branch], repo)
        if _stamp_merge_record(args.task, branch_tip, pushed=False, deleted=deleted):
            _commit_task_record(args.task, repo)
        return 0

    if not _tree_clean(repo):
        print("Working tree has uncommitted code changes. Commit or stash before merging.")
        for line in _dirty_paths(repo):
            print(f"  {line}")
        return 2

    checkout = _git(["checkout", main], repo)
    if checkout.returncode != 0:
        print(f"Failed to checkout {main}:")
        print((checkout.stderr or checkout.stdout).rstrip())
        return 2

    if _remote_exists(remote, repo):
        pull = _git(["pull", "--ff-only", remote, main], repo)
        if pull.returncode != 0:
            print(f"`git pull --ff-only {remote} {main}` failed ({main} may have diverged):")
            print((pull.stderr or pull.stdout).rstrip())
            return 2

    merge = _git(["merge", "--no-ff", branch, "-m", f"merge: {args.task}"], repo)
    if merge.returncode != 0:
        print("Merge failed; aborting to leave a clean tree:")
        print((merge.stderr or merge.stdout).rstrip())
        _git(["merge", "--abort"], repo)
        return 2

    sha = _rev("HEAD", repo) or ""

    deleted = False
    if not args.keep_branch:
        delete = _git(["branch", "-d", branch], repo)
        deleted = delete.returncode == 0
        if not deleted:
            print(f"WARNING: could not delete {branch} (left in place):")
            print((delete.stderr or delete.stdout).rstrip())

    pushed = False
    if _remote_exists(remote, repo) and not args.no_push:
        push = _git(["push", remote, main], repo)
        pushed = push.returncode == 0
        if not pushed:
            print(
                f"WARNING: local {main} has the merge ({sha[:12]}) but "
                f"`git push {remote} {main}` failed:"
            )
            print((push.stderr or push.stdout).rstrip())

    # Stamp + commit the task record so the tree is left clean and the SHA is
    # durable in history. Push again to carry that record commit if we pushed.
    recorded = _stamp_merge_record(args.task, sha, pushed, deleted)
    committed = _commit_task_record(args.task, repo) if recorded else False
    if pushed and committed:
        repush = _git(["push", remote, main], repo)
        if repush.returncode != 0:
            print(f"WARNING: merge SHA recorded locally but record commit was not pushed to {remote}.")

    print(f"Merged {branch} -> {main}")
    print(f"  merge commit SHA:    {sha}")
    print(f"  pushed to {remote}:  {'yes' if pushed else 'no'}")
    print(f"  branch deleted:      {'yes' if deleted else 'no'}")
    print(f"  task record updated: {'yes' if recorded else 'no (no task record found)'}")
    return 0


def cmd_verify_merge(args: argparse.Namespace) -> int:
    """Prove that a task's work is on main. Nonzero exit if it is not.

    The acceptance gate: a reviewer or pre-acceptance check runs it and a
    nonzero result blocks ACCEPTANCE_READY.
    """
    _ensure_dirs()
    repo = _find_repo_root()
    if not _git_ok(["rev-parse", "--git-dir"], repo):
        print("Not a git repository.")
        return 2

    main = args.main

    if args.sha:
        commit, source = args.sha, "given --sha"
    elif args.branch:
        commit, source = _rev(args.branch, repo), f"branch {args.branch}"
    elif args.task:
        recorded = _read_recorded_sha(args.task)
        if recorded:
            commit, source = recorded, "task record SHA"
        elif _branch_exists(f"task/{args.task}", repo):
            commit, source = _rev(f"task/{args.task}", repo), f"branch task/{args.task}"
        else:
            print(f"No merge SHA recorded for task {args.task} and branch task/{args.task} not found.")
            print("REVIEW_FAILED: task is not proven to be on main.")
            return 2
    else:
        print("Provide one of --task, --branch, or --sha.")
        return 2

    full = _rev(commit, repo) if commit else None
    if not full:
        print(f"Could not resolve commit ({source}).")
        return 2

    if _is_ancestor(full, main, repo):
        print(f"VERIFIED: {full[:12]} ({source}) is in {main}.")
        return 0
    print(f"NOT MERGED: {full[:12]} ({source}) is NOT in {main}.")
    print("REVIEW_FAILED: task is not proven to be on main.")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coordinate AI agent work in this repo.")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Show active locks and optional inbox.")
    status.add_argument("--agent", help="Also show this agent's inbox.")
    status.set_defaults(func=cmd_status)

    check = sub.add_parser("check", help="Check files for active lock conflicts.")
    check.add_argument("--agent", required=True)
    check.add_argument("--files", nargs="+", required=True)
    check.set_defaults(func=cmd_check)

    lock = sub.add_parser("lock", help="Create an active work lock.")
    lock.add_argument("--agent", required=True)
    lock.add_argument("--task", required=True)
    lock.add_argument("--files", nargs="+", required=True)
    lock.add_argument("--ttl-hours", type=float, default=2.0)
    lock.add_argument("--force", action="store_true")
    lock.set_defaults(func=cmd_lock)

    unlock = sub.add_parser("unlock", help="Remove all locks for an agent.")
    unlock.add_argument("--agent", required=True)
    unlock.add_argument(
        "--require-merged",
        action="store_true",
        help="Refuse to unlock if the agent's task branch is not yet merged into main.",
    )
    unlock.set_defaults(func=cmd_unlock)

    merge = sub.add_parser(
        "merge",
        help="Land a task branch on main (--no-ff), capture the SHA, record it.",
    )
    merge.add_argument("--agent", required=True)
    merge.add_argument("--task", required=True, help="Task id; branch defaults to task/<task>.")
    merge.add_argument("--branch", help="Override the branch name (default task/<task>).")
    merge.add_argument("--main", default="main")
    merge.add_argument("--remote", default="origin")
    merge.add_argument("--no-push", action="store_true", help="Do not push main to the remote.")
    merge.add_argument(
        "--keep-branch",
        action="store_true",
        help="Do not delete the task branch after merge.",
    )
    merge.set_defaults(func=cmd_merge)

    verify = sub.add_parser(
        "verify-merge",
        help="Prove a task's work is on main; nonzero exit if not (acceptance gate).",
    )
    verify.add_argument("--task", help="Task id; reads the recorded merge SHA from the task record.")
    verify.add_argument("--branch", help="Verify this branch tip instead.")
    verify.add_argument("--sha", help="Verify this commit SHA instead.")
    verify.add_argument("--main", default="main")
    verify.set_defaults(func=cmd_verify_merge)

    note = sub.add_parser("note", help="Leave a note in another agent's inbox.")
    note.add_argument("--from", dest="from_agent", required=True)
    note.add_argument("--to", required=True)
    note.add_argument("--files", nargs="*", default=[])
    note.add_argument("--message", required=True)
    note.set_defaults(func=cmd_note)

    inbox = sub.add_parser("inbox", help="Read an agent inbox.")
    inbox.add_argument("--agent", required=True)
    inbox.set_defaults(func=cmd_inbox)

    archive = sub.add_parser("archive", help="Archive one inbox message after reading.")
    archive.add_argument("--agent", required=True)
    archive.add_argument("--message", required=True)
    archive.set_defaults(func=cmd_archive)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

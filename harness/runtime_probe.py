# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Fail-closed proof that the owner-facing runtime serves a reviewed release."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from harness import board, board_surface, runtime_identity
from harness.project_context import context_from_roots, project_context


CHAT_TOKEN = re.compile(r'id="project-chat" data-action-token="([^"]+)"')
RUNTIME_COMMIT = re.compile(r'<meta name="harness-runtime-commit" content="([0-9a-f]{40})">')


def _json(url: str, *, request: Request | None = None, timeout: float = 35.0) -> dict[str, Any]:
    with urlopen(request or url, timeout=timeout) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("runtime response is not an object")
    return value


def _same_directory(left: Path, right: Path) -> bool:
    """Compare canonical filesystem identity without trusting path spelling."""
    try:
        left_stat, right_stat = left.stat(), right.stat()
        return (left_stat.st_dev, left_stat.st_ino) == (right_stat.st_dev, right_stat.st_ino)
    except OSError:
        return left.resolve(strict=False) == right.resolve(strict=False)


def _governed_code_root(root, reviewed_commit: str) -> Path:
    """Resolve the repository certified by this exact reviewed task commit."""
    context = project_context(root)
    try:
        state = board.snapshot(root)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return context.code_root
    requests = list((state.get("qa_requests") or {}).values())
    requests.extend((state.get("qa_request_index") or {}).values())
    requests.extend(
        entry.get("value", {}) for entry in (state.get("archive") or [])
        if entry.get("kind") == "qa_request"
    )
    matching_tasks = {
        str(request.get("task")) for request in requests
        if request.get("reviewed_commit") == reviewed_commit and request.get("task")
    }
    repositories = state.get("task_repositories") or {}
    candidates = {
        Path(str(repositories[task])).resolve(strict=False)
        for task in matching_tasks if repositories.get(task)
    }
    if len(candidates) > 1:
        raise ValueError("the reviewed commit is associated with multiple governed repositories")
    if candidates:
        return candidates.pop()
    return context.code_root


def _select_registry_entry(manager_url: str, governed_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = _json(manager_url + "api/projects", timeout=3)
    projects = payload.get("projects")
    if not isinstance(projects, list):
        raise ValueError("manager project registry response is malformed")
    matches = [
        entry for entry in projects
        if isinstance(entry, dict) and entry.get("code_root")
        and _same_directory(Path(str(entry["code_root"])), governed_root)
    ]
    if len(matches) != 1:
        raise ValueError("manager registry must contain exactly one entry for the governed repository")
    if not matches[0].get("id"):
        raise ValueError("the governed registry entry has no stable identity")
    return matches[0], projects


def _open_registry_entry(manager_url: str, entry: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        manager_url + "api/projects/" + quote(str(entry["id"]), safe="") + "/open",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return _json(manager_url, request=request, timeout=35)


def verify(
    root, reviewed_commit: str, *, manager_url: str = "http://127.0.0.1:8740/",
) -> dict[str, Any]:
    """Verify manager, worker, visible chat, and one real grounded answer.

    The returned record contains identities and timings only. Prompt and answer
    text are deliberately excluded from release evidence and logs.
    """
    result: dict[str, Any] = {
        "runtime_gate_required": True,
        "deployed_runtime_verified": False,
        "deployed_chat_verified": False,
        "manager_runtime_exact": False,
        "worker_runtime_exact": False,
        "worker_watchdog_active": False,
        "project_context_exact": False,
        "registry_entry_exact": False,
        "project_auto_opened": False,
        "visible_chat_present": False,
        "error": "",
    }
    try:
        manager_url = manager_url.rstrip("/") + "/"
        manager_origin = urlparse(manager_url)
        if manager_origin.scheme != "http" or manager_origin.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("release runtime manager must use a loopback HTTP origin")
        governed_root = _governed_code_root(root, reviewed_commit)
        manager = _json(manager_url + "api/ready", timeout=3)
        entry, projects = _select_registry_entry(manager_url, governed_root)
        result["registry_entry_exact"] = True
        existing_worker = manager.get("worker")
        if not existing_worker:
            other_active = [
                project for project in projects
                if project.get("active") and project.get("id") != entry.get("id")
            ]
            if other_active:
                raise ValueError("a different project is active; the release probe will not replace it")
            _open_registry_entry(manager_url, entry)
            result["project_auto_opened"] = True
            manager = _json(manager_url + "api/ready", timeout=3)
        manager_runtime = manager.get("runtime") or {}
        worker = manager.get("worker") or {}
        worker_runtime = worker.get("runtime") or {}
        result["manager_runtime_exact"] = bool(
            manager.get("ready") is True
            and manager_runtime.get("commit") == reviewed_commit
            and manager_runtime.get("clean") is True
        )
        result["worker_runtime_exact"] = bool(
            worker.get("ready") is True
            and worker_runtime.get("commit") == reviewed_commit
            and worker_runtime.get("clean") is True
            and runtime_identity.matches(manager_runtime, worker_runtime)
        )
        watchdog = worker.get("watchdog") or {}
        result["worker_watchdog_active"] = bool(
            watchdog.get("active") is True
            and watchdog.get("interval_seconds") == board.WATCHDOG_INTERVAL_SECONDS
            and watchdog.get("cto_poll_deadline_seconds") == board.CTO_MONITOR_INTERVAL_SECONDS
        )
        result["project_context_exact"] = (
            worker.get("project_ref") == board_surface.project_id(context_from_roots(
                entry["code_root"], entry.get("data_root"), entry.get("workspace_root"),
            ))
        )
        if not _same_directory(governed_root, Path(str(entry["code_root"]))):
            raise ValueError("opened project no longer matches the governed repository")
        board_url = str(manager.get("board_url") or "")
        parsed = urlparse(board_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("manager did not report a loopback project worker")
        with urlopen(board_url, timeout=3) as response:
            page = response.read(512_000).decode("utf-8")
        token = CHAT_TOKEN.search(page)
        page_commit = RUNTIME_COMMIT.search(page)
        result["visible_chat_present"] = bool(
            token and page_commit and page_commit.group(1) == reviewed_commit
            and 'id="project-chat-input"' in page and 'id="project-chat-send"' in page
        )
        if not all(
            result[key] for key in (
                "manager_runtime_exact", "worker_runtime_exact",
                "worker_watchdog_active", "project_context_exact", "registry_entry_exact",
                "visible_chat_present",
            )
        ):
            raise ValueError("deployed runtime identity or visible chat does not match the reviewed release")
        request_id = "release-" + secrets.token_hex(12)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        request = Request(
            board_url.rstrip("/") + "/api/project-chat",
            data=json.dumps({
                "request_id": request_id,
                "question": "What is its current status?",
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "X-Harness-Chat-Action": token.group(1),
            },
            method="POST",
        )
        answer = _json(board_url, request=request)
        answer_text = answer.get("answer")
        source_ids = answer.get("source_ids")
        result["deployed_chat_verified"] = bool(
            isinstance(answer_text, str) and answer_text.strip()
            and answer_text != "I do not know."
            and isinstance(source_ids, list) and source_ids
        )
        if result["deployed_chat_verified"]:
            result["answer_sha256"] = hashlib.sha256(answer_text.encode("utf-8")).hexdigest()
        result["deployed_runtime_verified"] = bool(
            result["deployed_chat_verified"]
            and result["manager_runtime_exact"]
            and result["worker_runtime_exact"]
            and result["worker_watchdog_active"]
            and result["project_context_exact"]
            and result["registry_entry_exact"]
            and result["visible_chat_present"]
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError, HTTPError) as error:
        result["error"] = str(error)[:300]
    return result

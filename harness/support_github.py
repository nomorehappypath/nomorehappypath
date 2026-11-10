# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Thin GitHub access for Support mode: check out one exact app version, and
open/merge the pull request a reviewed fix becomes (Tasks 2/3 of the
Dedicated Support NMHP). Every call here is scoped to the one app repository
the owner saved in Support settings; nothing here ever touches the app's own
server, and no production credential is read or stored by this module.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from harness import accepted_bytes

GIT_TIMEOUT_SECONDS = 60
API_TIMEOUT_SECONDS = 20
DEFAULT_API_BASE = "https://api.github.com"


class GitCheckoutFailed(RuntimeError):
    """The app could not be cloned, or the reported version does not exist in it."""


class GitHubAPIError(RuntimeError):
    """A GitHub REST call returned an error status; its message is GitHub's own, safe to show the owner."""


def _credential_env(token: str) -> dict[str, str]:
    """Build the environment a git subprocess needs to authenticate over HTTPS with token.

    Review finding 2026-11-08 (B4): only through GIT_CONFIG_COUNT/KEY/VALUE, never a `-c`
    command-line argument — readable by any other local process through `ps`/`/proc/<pid>/cmdline`.

    Return: the environment to pass as `env=`; empty when token is "" (no credential saved —
    the clone then proceeds unauthenticated, exactly as it always did for a public or local repo).
    """
    if not token:
        return {}
    return {
        "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
        "GIT_ASKPASS": "", "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
    }


def checkout_version(repo: str, version: str, scratch_dir: Path, *, token: str = "") -> Path:
    """Clone repo and check it out at exactly version, into a fresh subfolder of scratch_dir.

    Args:
        repo: the app's GitHub repository (an https/ssh URL, or a local path — a local fake
            repository, for every test here that does not reach real GitHub).
        version: the exact tag or commit the case reported; never a branch name, so the result
            can never silently drift to a later commit.
        scratch_dir: an existing directory to clone into; the checkout lands at
            scratch_dir/<a name derived from version>.
        token: the saved GitHub credential (review finding B5: a private repository needs it to
            clone at all); "" clones unauthenticated, as a public or local repository needs.

    Return: the path to the checked-out working copy.

    Raises: GitCheckoutFailed if repo cannot be cloned or version does not exist in it.
    """
    if not repo or not version:
        raise GitCheckoutFailed("no app repository or no reported version to check out")
    scratch_dir.mkdir(parents=True, exist_ok=True)
    destination = scratch_dir / f"checkout-{_safe_name(version)}"
    if destination.exists():
        return destination
    env = _credential_env(token) or None
    try:
        subprocess.run(
            ["git", "clone", "--quiet", "--no-local", repo, str(destination)],
            check=True, capture_output=True, timeout=GIT_TIMEOUT_SECONDS, env=env,
        )
        subprocess.run(
            ["git", "checkout", "--quiet", version],
            cwd=destination, check=True, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        raise GitCheckoutFailed(f"could not check out {repo!r} at {version!r}: {_plain(error)}") from error
    return destination


def create_branch_from(repo_checkout: Path, branch: str, from_ref: str) -> None:
    """Create and check out branch in repo_checkout, starting from from_ref (the reported version).

    Args:
        repo_checkout: an existing clone (as returned by checkout_version).
        branch: the new branch's name (support/<case>).
        from_ref: the ref the branch starts from (the reported version's tag/commit).

    Return: None. Raises GitCheckoutFailed on any git failure.
    """
    try:
        subprocess.run(
            ["git", "checkout", "--quiet", "-B", branch, from_ref],
            cwd=repo_checkout, check=True, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        raise GitCheckoutFailed(f"could not create branch {branch!r} from {from_ref!r}: {_plain(error)}") from error


def commit_all_changes(repo_checkout: Path, message: str) -> bool:
    """Stage and commit every change currently in the working tree, if there is one.

    Review r2 B1: a passing independent check meant nothing if no code had actually changed.
    This is how a repair becomes a real, inspectable commit on the support/<case> branch before
    that check ever runs.

    Return: True if a commit was made; False if the working tree already matched HEAD (nothing
    to commit — the caller must not call this "fixed").
    """
    subprocess.run(["git", "add", "-A"], cwd=repo_checkout, check=True, capture_output=True, timeout=GIT_TIMEOUT_SECONDS)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=repo_checkout, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
    )
    if staged.returncode == 0:
        return False
    subprocess.run(
        ["git", "-c", "user.email=support@nomorehappypath.invalid", "-c", "user.name=NoMoreHappyPath Support",
         "commit", "--quiet", "-m", message],
        cwd=repo_checkout, check=True, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
    )
    return True


def branch_has_changes(repo_checkout: Path, base_ref: str) -> bool:
    """Whether the checkout's current HEAD carries a real commit beyond base_ref.

    Review r2 B1: "a passing plan check can claim Fixed without changing the app" — this is the
    check that closes that gap: a repair is never called Fixed unless its branch actually differs
    from the version it started from.

    Return: True if HEAD and base_ref differ; False if the branch is still identical to base_ref
    (no repair was actually made, whatever an independent check says).
    """
    result = subprocess.run(
        ["git", "diff", "--quiet", base_ref, "HEAD"], cwd=repo_checkout, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
    )
    return result.returncode != 0


def file_at(repo_checkout: Path, ref: str, relative_path: str) -> str:
    """Read one file's text as it was at ref (a tag or commit), not as the working tree has it now.

    Review r3 B1: the repair's failing-before / passing-after check must run the reproduce command
    the OWNER's plan named at the reported version, never one a repair agent may have rewritten.

    Args:
        repo_checkout: an existing clone.
        ref: the tag or commit to read from.
        relative_path: the file's path inside the repository.

    Return: the file's text; "" when the file does not exist at that ref or git refuses.
    """
    result = subprocess.run(
        ["git", "show", f"{ref}:{relative_path}"], cwd=repo_checkout, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
    )
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else ""


def changed_paths(repo_checkout: Path, base_ref: str) -> list[str]:
    """List the repository paths the checkout's current HEAD changes relative to base_ref.

    Return: the changed paths (empty when HEAD and base_ref are identical or git refuses).
    """
    result = subprocess.run(
        ["git", *accepted_bytes.name_only_arguments(base_ref, "HEAD")], cwd=repo_checkout, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.decode("utf-8", "replace").splitlines() if line.strip()]


def diff_text(repo_checkout: Path, base_ref: str, *, max_chars: int = 60_000) -> str:
    """The unified diff between base_ref and the checkout's HEAD, for an independent reviewer to read.

    Return: the diff text, cut at max_chars with a plain note when longer; "" when git refuses.
    """
    result = subprocess.run(
        ["git", "diff", base_ref, "HEAD"], cwd=repo_checkout, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return ""
    text = result.stdout.decode("utf-8", "replace")
    if len(text) > max_chars:
        return text[:max_chars] + "\n[diff cut here: it is longer than the reviewer is given]\n"
    return text


def push_branch(repo_checkout: Path, branch: str, token: str) -> None:
    """Push branch to its origin remote, authenticating with token.

    Review finding 2026-11-08 (S-003): the token is passed only through the
    GIT_CONFIG_COUNT/KEY/VALUE environment variables git itself reads (git
    2.31+), never as a `-c` command-line argument — a `-c http.extraHeader=...`
    argument is readable by any other local process through `ps`/`/proc/<pid>/cmdline`;
    an environment variable is not listed there.

    Return: None. Raises GitCheckoutFailed on any git failure.
    """
    try:
        subprocess.run(
            ["git", "push", "--quiet", "origin", f"{branch}:{branch}", "--force-with-lease"],
            cwd=repo_checkout, check=True, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
            env=_credential_env(token) or None,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        raise GitCheckoutFailed(f"could not push branch {branch!r}: {_plain(error)}") from error


def open_pull_request(
    *, owner_repo: str, branch: str, base: str, title: str, body: str, token: str,
    api_base: str = DEFAULT_API_BASE,
) -> str:
    """Open one pull request from branch into base on owner_repo (e.g. "owner/name").

    Args:
        owner_repo: "owner/name" as GitHub's REST API addresses it.
        branch: the head branch (support/<case>), already pushed.
        base: the app's main branch.
        title: the pull request's title.
        body: the pull request's description.
        token: the owner's GitHub credential, scoped to this one repository.
        api_base: the GitHub REST API's base URL; overridden in tests to point at a local fake.

    Return: the pull request's HTML URL.

    Raises: GitHubAPIError on any non-2xx response.
    """
    payload = {"title": title, "head": branch, "base": base, "body": body}
    response = _api_call(f"{api_base}/repos/{owner_repo}/pulls", token, payload)
    return str(response["html_url"])


def merge_pull_request(*, owner_repo: str, pull_number: int, token: str, api_base: str = DEFAULT_API_BASE) -> dict[str, Any]:
    """Merge one pull request, with the owner's own GitHub credential.

    Return: GitHub's own merge response ({"merged": bool, "sha": ..., "message": ...}).

    Raises: GitHubAPIError on any non-2xx response (the pull request is not merged).
    """
    return _api_call(f"{api_base}/repos/{owner_repo}/pulls/{pull_number}/merge", token, {}, method="PUT")


def owner_repo_and_pull_number(pull_request_url: str) -> tuple[str, int]:
    """Split a pull request's HTML URL back into ("owner/name", number) for merge_pull_request.

    Args:
        pull_request_url: a URL shaped like ".../<owner>/<name>/pull/<number>".

    Return: (owner_repo, pull_number). Raises ValueError if the URL is not shaped that way.
    """
    parts = pull_request_url.rstrip("/").split("/")
    if len(parts) < 4 or parts[-2] != "pull" or not parts[-1].isdigit():
        raise ValueError(f"not a recognizable pull request URL: {pull_request_url!r}")
    return f"{parts[-4]}/{parts[-3]}", int(parts[-1])


def _api_call(url: str, token: str, payload: dict[str, Any], *, method: str = "POST") -> dict[str, Any]:
    """Make one authenticated GitHub REST call and decode its JSON response.

    Return: the decoded JSON body. Raises GitHubAPIError on any non-2xx response or network failure.
    """
    request = Request(
        url, data=json.dumps(payload).encode("utf-8"), method=method,
        headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8") or "{}").get("message", "")
        except ValueError:
            detail = ""
        raise GitHubAPIError(detail or f"GitHub refused the request ({error.code})") from error
    except (URLError, TimeoutError, OSError) as error:
        raise GitHubAPIError(f"could not reach GitHub: {_plain(error)}") from error


def _safe_name(value: str) -> str:
    """Turn an arbitrary version string into a safe path component."""
    return "".join(character if character.isalnum() or character in "._-" else "_" for character in value)[:120]


def _plain(error: Exception) -> str:
    """One short plain sentence for a subprocess/network failure, never a raw traceback."""
    if isinstance(error, subprocess.CalledProcessError):
        lines = (error.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return lines[-1] if lines else "git refused the command"
    return str(error)

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Trusted, board-authorized Git write broker (spec §10).

The rest of the harness may inspect Git through :mod:`harness.git_process`, but
all governed writes cross this module.  The broker derives repositories,
worktrees, refs, roles, and tasks from durable board state; callers supply only
their agent identity, a monotonic nonce, and (for staging) explicit relative
paths inside their own worktree.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from typing import Any, Callable, Iterator, Mapping, Sequence

from harness import accepted_bytes, child_process
from harness.project_context import ProjectContext, ProjectRoot, project_context


DEVELOPER_ROLES = {"development", "engineering"}
WRITE_OPERATIONS = {
    "branch-create", "stage+commit", "record-review-ref",
    "mirror-ref-create", "subtask-fold", "accept-merge", "remote-push",
}
ZERO_OID = "0" * 40
SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


class BrokerError(ValueError):
    """A governed Git operation was refused."""


class AuthorizationError(BrokerError):
    """Board state does not authorize the requested operation."""


class ReplayError(BrokerError):
    """A per-session nonce was reused or moved backwards."""


class MainMovedError(BrokerError):
    """Main no longer equals the final review's recorded base."""


class RecoveryHoldError(BrokerError):
    """Recovery found state that must be preserved for CTO resolution."""


class FilterRequiredError(RecoveryHoldError):
    """Repository attributes require executable clean/smudge filtering."""


class InjectedCrash(RuntimeError):
    """Test-only crash injection after a durable transaction step."""


@dataclass(frozen=True)
class GitIdentity:
    commit: str
    tree: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(value: str) -> str:
    return SAFE_COMPONENT.sub("-", str(value)).strip("-.") or "task"


def _is_within(path: Path, root: Path) -> bool:
    path, root = path.resolve(strict=False), root.resolve(strict=False)
    return path == root or root in path.parents


def context_for_repository(board_root: ProjectRoot, repository: Path) -> ProjectContext:
    """Use project-owned data/workspace roots with an explicitly bound repo."""
    control = project_context(board_root)
    return ProjectContext(repository, control.data_root, control.workspace_root)


class GitBroker:
    """Single trusted writer for one governed project's Git state."""

    def __init__(
        self,
        context: ProjectRoot,
        *,
        state_loader: Callable[[], Mapping[str, Any]] | None = None,
        use_os_sandbox: bool = True,
    ) -> None:
        self.context = project_context(context)
        self._state_loader = state_loader or self._read_board_state
        self.journal_root = self.context.storage_path("broker-journal")
        self.mirror_root = self.context.storage_path("git-mirror")
        self.home_root = self.journal_root / "home"
        self.temp_root = self.journal_root / "tmp"
        self.hooks_root = self.journal_root / "empty-hooks"
        self.transactions_path = self.journal_root / "transactions.jsonl"
        self.nonces_path = self.journal_root / "nonces.json"
        self.holds_path = self.journal_root / "recovery-holds.jsonl"
        self.lock_path = self.journal_root / "project.lock"
        self.use_os_sandbox = bool(use_os_sandbox)
        self._prepare_storage()

    def _prepare_storage(self) -> None:
        for path in (self.journal_root, self.home_root, self.temp_root, self.hooks_root):
            path.mkdir(parents=True, exist_ok=True)
        try:
            self.home_root.chmod(0o700)
            self.hooks_root.chmod(0o700)
        except OSError:
            pass

    def _read_board_state(self) -> Mapping[str, Any]:
        path = self.context.storage_path("board", "state.json")
        if not path.is_file():
            raise AuthorizationError("board state is unavailable")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AuthorizationError("board state is unreadable") from error
        if not isinstance(value, dict):
            raise AuthorizationError("board state is invalid")
        return value

    def state(self) -> Mapping[str, Any]:
        value = self._state_loader()
        if not isinstance(value, Mapping):
            raise AuthorizationError("board state loader returned an invalid value")
        return value

    @contextmanager
    def project_lock(self, *, fail_fast: bool = False) -> Iterator[None]:
        """Serialize every Git mutation for this project."""
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if fail_fast else 0)
            try:
                fcntl.flock(descriptor, flags)
            except BlockingIOError as error:
                raise BrokerError("another Git broker operation is already in flight") from error
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _append_jsonl(self, path: Path, record: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n"
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, payload.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _journal(self, transaction_id: str, operation: str, step: str, **fields: Any) -> None:
        self._append_jsonl(self.transactions_path, {
            "transaction_id": transaction_id,
            "operation": operation,
            "step": step,
            "at": _now(),
            **fields,
        })

    def _hold(self, transaction_id: str, operation: str, reason: str, **fields: Any) -> dict[str, Any]:
        record = {
            "transaction_id": transaction_id,
            "operation": operation,
            "reason": reason,
            "status": "CTO_RECOVERY_HOLD",
            "at": _now(),
            **fields,
        }
        self._append_jsonl(self.holds_path, record)
        self._journal(transaction_id, operation, "hold", reason=reason, status="CTO_RECOVERY_HOLD", **fields)
        return record

    def _nonce_key(self, state: Mapping[str, Any], agent_id: str) -> str:
        agent = state.get("agents", {}).get(agent_id)
        if not isinstance(agent, Mapping):
            raise AuthorizationError(f"unknown agent: {agent_id}")
        return str(agent.get("session_id") or agent_id)

    def consume_nonce(self, state: Mapping[str, Any], agent_id: str, nonce: int, operation: str) -> None:
        if operation not in WRITE_OPERATIONS:
            raise BrokerError("unknown Git write operation")
        try:
            candidate = int(nonce)
        except (TypeError, ValueError) as error:
            raise ReplayError("broker nonce must be an integer") from error
        if candidate <= 0:
            raise ReplayError("broker nonce must be positive")
        key = self._nonce_key(state, agent_id)
        try:
            known = json.loads(self.nonces_path.read_text(encoding="utf-8")) if self.nonces_path.is_file() else {}
        except (OSError, json.JSONDecodeError) as error:
            raise RecoveryHoldError("broker nonce journal is unreadable") from error
        previous = int(known.get(key, 0))
        if candidate <= previous:
            self._append_jsonl(self.journal_root / "refusals.jsonl", {
                "at": _now(), "agent_id": agent_id, "session": key,
                "operation": operation, "nonce": candidate, "reason": "replay",
            })
            raise ReplayError(f"broker nonce replay refused; last accepted nonce is {previous}")
        known[key] = candidate
        temporary = self.nonces_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(known, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, self.nonces_path)
        self._append_jsonl(self.journal_root / "invocations.jsonl", {
            "at": _now(), "agent_id": agent_id, "session": key,
            "operation": operation, "nonce": candidate,
        })

    def _agent(self, state: Mapping[str, Any], agent_id: str) -> Mapping[str, Any]:
        agent = state.get("agents", {}).get(agent_id)
        if not isinstance(agent, Mapping):
            raise AuthorizationError(f"unknown agent: {agent_id}")
        if agent.get("write_authority") is False or agent.get("superseded_by_agent_id"):
            raise AuthorizationError("superseded agent has no Git write authority")
        return agent

    def _delivery_task(self, state: Mapping[str, Any], agent_id: str) -> tuple[Mapping[str, Any], str]:
        agent = self._agent(state, agent_id)
        if agent.get("role") not in DEVELOPER_ROLES or not agent.get("active", True):
            raise AuthorizationError("operation requires the active Delivery task owner")
        task = str(agent.get("task") or "")
        if not task or task in {"AWAITING_OWNER_DIRECTION", "GLOBAL_MONITOR", "REVIEW_QUEUE"}:
            raise AuthorizationError("Delivery Agent has no governed task")
        return agent, task

    def _repository_for(self, state: Mapping[str, Any], task: str) -> Path:
        raw = state.get("task_repositories", {}).get(task)
        if not raw:
            raise BrokerError(f"board state has no repository bound to task {task}")
        repository = Path(str(raw)).resolve()
        if not repository.is_dir():
            raise BrokerError("board-derived task repository does not exist")
        probe = self._run_git(["rev-parse", "--show-toplevel"], cwd=repository, writable=[repository], sandbox_network=False)
        if probe.returncode != 0 or Path(probe.stdout.strip()).resolve() != repository:
            raise BrokerError("board-derived task repository is not a Git root")
        return repository

    def _workspace_for(self, state: Mapping[str, Any], task: str, subtask: str = "") -> Path:
        raw = ""
        if subtask:
            raw = state.get("subtask_workspaces", {}).get(task, {}).get(subtask, "")
        raw = raw or state.get("task_workspaces", {}).get(task, "")
        if not raw:
            raise BrokerError("board state has no isolated worktree for this scope")
        workspace = Path(str(raw)).resolve()
        if not workspace.is_dir() or not _is_within(workspace, self.context.workspace_root):
            raise BrokerError("board-derived worktree is outside workspace_root")
        return workspace

    def _git_environment(self) -> dict[str, str]:
        environment = child_process.environment(git=True, python=True, shell=True)
        environment.update({
            "HOME": str(self.home_root),
            "TMPDIR": str(self.temp_root),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/usr/bin/false",
            "SSH_ASKPASS": "/usr/bin/false",
            "PAGER": "cat",
            "GIT_PAGER": "cat",
        })
        return environment

    def _sandbox_profile(self, readable: Sequence[Path], writable: Sequence[Path], network: bool, allow_shell: bool) -> Path | None:
        executable = Path("/usr/bin/sandbox-exec")
        if not self.use_os_sandbox or not executable.is_file() or os.uname().sysname != "Darwin":
            return None
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
        if allow_shell:
            lines.append('(allow process-exec (literal "/bin/sh"))')
            lines.append('(allow process-exec (literal "/bin/bash"))')
        for system_path in (
            Path("/System"), Path("/usr"), Path("/bin"), Path("/sbin"),
            Path("/Library/Apple"), Path("/private/etc"),
            Path("/private/var/db/timezone"), Path("/dev"),
            Path("/Applications/Xcode.app"),
        ):
            lines.append(f'(allow file-read* (subpath "{literal(system_path)}"))')
        for path in sorted({Path(item) for item in readable}, key=str):
            lines.append(f'(allow file-read* (subpath "{literal(path)}"))')
        if network:
            lines.append("(allow network*)")
        lines.append('(allow file-write* (literal "/dev/null"))')
        for path in sorted({Path(item) for item in writable}, key=str):
            lines.append(f'(allow file-write* (subpath "{literal(path)}"))')
        digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]
        profile = self.journal_root / f"sandbox-{digest}.sb"
        if not profile.exists():
            profile.write_text("\n".join(lines) + "\n", encoding="utf-8")
            profile.chmod(0o600)
        return profile

    def _run_git(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        writable: Sequence[Path],
        readable: Sequence[Path] = (),
        sandbox_network: bool = False,
        allow_shell: bool = False,
        input: str | bytes | None = None,
        index_file: Path | None = None,
    ) -> subprocess.CompletedProcess:
        xcode_git = Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git")
        git = xcode_git if xcode_git.is_file() else Path("/usr/bin/git")
        if not git.is_file():
            located = shutil.which("git", path="/usr/bin:/bin:/usr/sbin:/sbin")
            if not located:
                raise BrokerError("trusted Git executable was not found")
            git = Path(located).resolve()
        fixed = [
            str(git),
            "-c", f"core.hooksPath={self.hooks_root}",
            "-c", "commit.gpgSign=false",
            "-c", "tag.gpgSign=false",
            "-c", "core.fsmonitor=false",
            "-c", "credential.helper=",
            "-c", "core.pager=cat",
            "-c", "pager.branch=false",
            "-c", "interactive.diffFilter=",
            "-c", "diff.external=",
            "-c", "merge.tool=",
            "-c", "mergetool.prompt=false",
            "-c", "core.sshCommand=/usr/bin/ssh",
            "-c", "http.proxy=",
            "-c", "protocol.ext.allow=never",
            *[str(item) for item in arguments],
        ]
        writable_paths = [self.temp_root, *writable]
        metadata_paths: list[Path] = []
        dot_git = cwd / ".git"
        if dot_git.is_dir():
            metadata_paths.append(dot_git.resolve())
        elif dot_git.is_file():
            try:
                marker = dot_git.read_text(encoding="utf-8").strip()
            except OSError:
                marker = ""
            if marker.startswith("gitdir:"):
                value = Path(marker.split(":", 1)[1].strip())
                git_dir = (value if value.is_absolute() else cwd / value).resolve()
                if _is_within(git_dir, self.context.code_root):
                    metadata_paths.append(git_dir)
                    common_marker = git_dir / "commondir"
                    if common_marker.is_file():
                        common_value = Path(common_marker.read_text(encoding="utf-8").strip())
                        common_dir = (common_value if common_value.is_absolute() else git_dir / common_value).resolve()
                        if _is_within(common_dir, self.context.code_root):
                            metadata_paths.append(common_dir)
        elif (cwd / "HEAD").is_file() and (cwd / "objects").is_dir():
            metadata_paths.append(cwd.resolve())
        readable_paths = [cwd, self.home_root, self.temp_root, self.hooks_root, *metadata_paths, *readable, *writable_paths]
        profile = self._sandbox_profile(readable_paths, writable_paths, sandbox_network, allow_shell)
        command = (["/usr/bin/sandbox-exec", "-f", str(profile), *fixed] if profile else fixed)
        environment = self._git_environment()
        if index_file is not None:
            resolved_index = index_file.resolve(strict=False)
            if not _is_within(resolved_index, self.temp_root):
                raise BrokerError("temporary Git index is outside broker storage")
            environment["GIT_INDEX_FILE"] = str(resolved_index)
        text_mode = not isinstance(input, bytes)
        return subprocess.run(
            command, cwd=cwd, env=environment, input=input,
            capture_output=True, text=text_mode, check=False,
        )

    def materialize_readonly_candidate(
        self, repository: Path, commit: str, destination: Path,
    ) -> dict[str, Any]:
        """Materialize an exact candidate for inspection without mutating source refs."""
        source = Path(repository).resolve()
        raw_target = Path(destination)
        target_parent = raw_target.parent.resolve()
        target = target_parent / raw_target.name
        if (
            not raw_target.is_absolute() or not target_parent.is_dir()
            or not source.is_dir() or target.exists()
            or re.fullmatch(r"[0-9a-f]{40,64}", commit.strip()) is None
        ):
            raise BrokerError("candidate materialization requires a repository, commit, and absent destination")
        dot_git = source / ".git"
        worktree_git = dot_git
        if dot_git.is_file() and not dot_git.is_symlink():
            marker = dot_git.read_text(encoding="utf-8", errors="strict").strip()
            if not marker.startswith("gitdir: "):
                raise BrokerError("candidate worktree has invalid Git metadata")
            git_value = Path(marker[8:].strip())
            worktree_git = (
                git_value.resolve()
                if git_value.is_absolute()
                else (source / git_value).resolve()
            )
        if not worktree_git.is_dir():
            raise BrokerError("candidate worktree Git directory is unavailable")
        common = worktree_git
        common_marker = worktree_git / "commondir"
        if common_marker.is_file() and not common_marker.is_symlink():
            common_value = Path(common_marker.read_text(encoding="utf-8", errors="strict").strip())
            common = (
                common_value.resolve()
                if common_value.is_absolute()
                else (worktree_git / common_value).resolve()
            )
        if not common.is_dir():
            raise BrokerError("candidate repository common Git directory is unavailable")
        with self.project_lock():
            common_result = self._run_git(
                ["rev-parse", "--git-common-dir"], cwd=source,
                writable=[], readable=[source, worktree_git, common],
            )
            if common_result.returncode != 0 or not common_result.stdout.strip():
                return {"ok": False, "clone": common_result, "checkout": None}
            observed_value = Path(common_result.stdout.strip())
            observed_common = (
                observed_value.resolve()
                if observed_value.is_absolute()
                else (source / observed_value).resolve()
            )
            if observed_common != common:
                raise BrokerError("candidate repository common Git directory changed during validation")
            cloned = self._run_git(
                ["clone", "--quiet", "--no-checkout", "--shared", str(common), str(target)],
                cwd=target.parent, writable=[target.parent], readable=[source, common],
                allow_shell=True,
            )
            if cloned.returncode != 0:
                return {"ok": False, "clone": cloned, "checkout": None}
            checkout = self._run_git(
                ["checkout", "--quiet", "--detach", commit],
                cwd=target, writable=[target], readable=[source, common, target],
            )
            return {"ok": checkout.returncode == 0, "clone": cloned, "checkout": checkout}

    def _identity(self, repository: Path, revision: str = "HEAD") -> GitIdentity:
        commit = self._run_git(["rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=repository, writable=[repository])
        if commit.returncode != 0 or not commit.stdout.strip():
            raise BrokerError(f"commit cannot be resolved: {revision}")
        canonical = commit.stdout.strip()
        tree = self._run_git(["rev-parse", "--verify", f"{canonical}^{{tree}}"], cwd=repository, writable=[repository])
        if tree.returncode != 0 or not tree.stdout.strip():
            raise BrokerError(f"tree cannot be resolved: {canonical}")
        return GitIdentity(canonical, tree.stdout.strip())

    def _validate_paths(self, workspace: Path, paths: Sequence[str]) -> list[str]:
        if not paths:
            raise BrokerError("stage+commit requires explicit relative file paths")
        prepared: list[str] = []
        for raw in paths:
            value = str(raw)
            candidate = Path(value)
            if (
                not value
                or not value.strip()
                or "\x00" in value
                or candidate.is_absolute()
                or not candidate.parts
                or ".." in candidate.parts
            ):
                raise BrokerError(f"staging path is not an explicit relative path: {value}")
            if any(part.startswith("-") for part in candidate.parts):
                raise BrokerError(f"Git option-like staging path is refused: {value}")
            resolved = (workspace / candidate).resolve(strict=False)
            if not _is_within(resolved, workspace):
                raise BrokerError(f"staging path escapes the task worktree: {value}")
            if resolved.is_dir():
                raise BrokerError(f"staging path must name an explicit file: {value}")
            prepared.append(candidate.as_posix())
        if len(set(prepared)) != len(prepared):
            raise BrokerError("duplicate staging paths are refused")
        return prepared

    def _refuse_required_filters(self, workspace: Path, repository: Path, paths: Sequence[str]) -> None:
        self._refuse_worktree_filter_attributes(workspace, "stage+commit")
        checked = self._run_git(["check-attr", "-a", "--", *paths], cwd=workspace, writable=[workspace, repository])
        if checked.returncode != 0:
            raise BrokerError("could not inspect repository attributes safely")
        for line in checked.stdout.splitlines():
            parts = line.rsplit(": ", 2)
            if len(parts) == 3 and parts[1] == "filter" and parts[2] not in {"unspecified", "unset", ""}:
                transaction = f"filter-{secrets.token_hex(8)}"
                self._hold(transaction, "stage+commit", "attribute-driven filter required", attribute=line)
                raise FilterRequiredError("attribute-driven filter execution is refused; CTO recovery hold opened")

    @staticmethod
    def _filter_attribute_line(line: str) -> bool:
        tokens = line.split()
        return any(
            token.startswith("filter=") and token.split("=", 1)[1] not in {"", "unset", "unspecified"}
            for token in tokens[1:]
        )

    def _raise_filter_hold(self, operation: str, source: str) -> None:
        transaction = f"filter-{secrets.token_hex(8)}"
        self._hold(transaction, operation, "attribute-driven filter required", attribute_source=source)
        raise FilterRequiredError("attribute-driven filter execution is refused; CTO recovery hold opened")

    def _refuse_worktree_filter_attributes(self, workspace: Path, operation: str) -> None:
        for attributes in workspace.rglob(".gitattributes"):
            if attributes.is_symlink() or not _is_within(attributes, workspace):
                raise BrokerError("repository attributes path escapes the governed worktree")
            try:
                lines = attributes.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as error:
                raise BrokerError("repository attributes could not be inspected safely") from error
            if any(self._filter_attribute_line(line) for line in lines if line.strip() and not line.lstrip().startswith("#")):
                self._raise_filter_hold(operation, str(attributes.relative_to(workspace)))

    def _refuse_tree_filter_attributes(self, repository: Path, revision: str, operation: str) -> None:
        configured = self._run_git(
            ["config", "--local", "--get", "core.attributesFile"],
            cwd=repository, writable=[repository],
        )
        if configured.returncode == 0 and configured.stdout.strip():
            self._raise_filter_hold(operation, "local core.attributesFile")
        info_attributes = repository / ".git" / "info" / "attributes"
        if info_attributes.is_file():
            lines = info_attributes.read_text(encoding="utf-8", errors="replace").splitlines()
            if any(self._filter_attribute_line(line) for line in lines if line.strip() and not line.lstrip().startswith("#")):
                self._raise_filter_hold(operation, ".git/info/attributes")
        listed = self._run_git(
            ["ls-tree", "-r", "--name-only", revision],
            cwd=repository, writable=[repository],
        )
        if listed.returncode != 0:
            raise BrokerError("candidate attributes could not be enumerated safely")
        for path in (line for line in listed.stdout.splitlines() if Path(line).name == ".gitattributes"):
            shown = self._run_git(
                ["show", f"{revision}:{path}"], cwd=repository, writable=[repository],
            )
            if shown.returncode != 0:
                raise BrokerError("candidate attributes could not be inspected safely")
            if any(self._filter_attribute_line(line) for line in shown.stdout.splitlines() if line.strip() and not line.lstrip().startswith("#")):
                self._raise_filter_hold(operation, path)

    def branch_create(self, agent_id: str, nonce: int, *, subtask: str = "") -> dict[str, Any]:
        state = self.state()
        _, task = self._delivery_task(state, agent_id)
        if subtask:
            plan_subtasks = state.get("delivery_plans", {}).get(task, {}).get("subtasks", {})
            if subtask not in plan_subtasks:
                raise AuthorizationError("subtask branch must be derived from a declared application subtask")
        with self.project_lock():
            self.consume_nonce(state, agent_id, nonce, "branch-create")
            repository = self._repository_for(state, task)
            suffix = f"subtasks/{_safe(subtask)}" if subtask else "task"
            branch = f"refs/heads/harness/tasks/{_safe(task)}/{suffix}"
            repository_key = hashlib.sha256(str(repository).encode("utf-8")).hexdigest()[:12]
            repository_workspaces = self.context.workspace_root / f"{repository.name}-{repository_key}"
            workspace = repository_workspaces / _safe(task)
            if subtask:
                workspace = repository_workspaces / f"{_safe(task)}-subtasks" / _safe(subtask)
            existing = self._run_git(["show-ref", "--verify", "--quiet", branch], cwd=repository, writable=[repository])
            if existing.returncode == 0 or workspace.exists():
                raise BrokerError("task branch or worktree already exists")
            workspace.parent.mkdir(parents=True, exist_ok=True)
            base = self._identity(repository, "HEAD")
            self._refuse_tree_filter_attributes(repository, base.commit, "branch-create")
            created = self._run_git(
                ["worktree", "add", "-b", branch.removeprefix("refs/heads/"), str(workspace), base.commit],
                cwd=repository, writable=[repository, workspace.parent], readable=[repository],
            )
            if created.returncode != 0:
                raise BrokerError("could not create isolated task worktree: " + (created.stderr.strip() or created.stdout.strip()))
            # Preserve tracked pre-task candidate bytes without ever opening
            # unrelated untracked files.  The owner repository remains
            # untouched; the patch is materialized only in the isolated task
            # worktree and must later cross stage+commit with explicit paths.
            inherited_names = self._run_git(
                ["diff", "--no-ext-diff", "--name-only", "HEAD", "--"], cwd=repository,
                writable=[repository],
            )
            inherited_paths = sorted(line for line in inherited_names.stdout.splitlines() if line)
            if inherited_paths:
                patch = self._run_git(
                    ["diff", "--no-ext-diff", "--binary", "HEAD", "--", *inherited_paths], cwd=repository,
                    writable=[repository],
                )
                if patch.returncode != 0:
                    raise BrokerError("could not capture inherited tracked candidate changes: " + (patch.stderr.strip() or patch.stdout.strip()))
                applied = self._run_git(
                    ["apply", "--whitespace=nowarn", "-"], cwd=workspace,
                    writable=[workspace, repository], input=patch.stdout.encode("utf-8"),
                )
                if applied.returncode != 0:
                    detail = applied.stderr.decode("utf-8", errors="replace") if isinstance(applied.stderr, bytes) else applied.stderr
                    raise BrokerError("could not materialize inherited tracked candidate changes: " + detail.strip())
            resolved = self._identity(workspace)
            if resolved != base:
                raise RecoveryHoldError("created worktree does not match its board-derived base")
            return {
                "task": task, "subtask": subtask, "repository": str(repository),
                "workspace": str(workspace.resolve()), "branch": branch,
                "base_commit": base.commit, "base_tree": base.tree,
                "inherited_tracked_paths": inherited_paths,
            }

    def stage_commit(
        self,
        agent_id: str,
        nonce: int,
        paths: Sequence[str],
        message: str,
        *,
        subtask: str = "",
    ) -> dict[str, Any]:
        state = self.state()
        _, task = self._delivery_task(state, agent_id)
        message = str(message).strip()
        if not message or "\x00" in message or len(message) > 4000:
            raise BrokerError("commit message is missing or invalid")
        with self.project_lock():
            self.consume_nonce(state, agent_id, nonce, "stage+commit")
            workspace = self._workspace_for(state, task, subtask)
            repository = self._repository_for(state, task)
            prepared = self._validate_paths(workspace, paths)
            self._refuse_required_filters(workspace, repository, prepared)
            staged = self._run_git(["add", "--", *prepared], cwd=workspace, writable=[workspace, repository])
            if staged.returncode != 0:
                raise BrokerError("Git staging failed: " + (staged.stderr.strip() or staged.stdout.strip()))
            manifest = self._run_git(["diff", "--no-ext-diff", "--cached", "--name-only", "--"], cwd=workspace, writable=[workspace, repository])
            staged_paths = sorted(line for line in manifest.stdout.splitlines() if line)
            if manifest.returncode != 0 or not staged_paths:
                raise BrokerError("stage+commit produced no staged files")
            if not set(staged_paths).issubset(set(prepared)):
                raise RecoveryHoldError("staged manifest contains a path outside the explicit request")
            committed = self._run_git([
                "-c", "user.name=Harness Git Broker",
                "-c", "user.email=broker@harness.invalid",
                "commit", "--no-gpg-sign", "-m", message, "--", *prepared,
            ], cwd=workspace, writable=[workspace, repository])
            if committed.returncode != 0:
                raise BrokerError("Git commit failed: " + (committed.stderr.strip() or committed.stdout.strip()))
            identity = self._identity(workspace)
            status = self._run_git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=workspace, writable=[workspace, repository])
            if status.returncode != 0 or status.stdout.strip():
                raise RecoveryHoldError("post-commit task worktree is not clean")
            return {"task": task, "subtask": subtask, "commit": identity.commit, "tree": identity.tree, "manifest": staged_paths}

    def refresh_subtask_base(self, task: str, subtask: str) -> dict[str, Any]:
        """Move one untouched pending subtask to the latest integrated task head."""
        state = self.state()
        item = (
            state.get("delivery_plans", {}).get(task, {})
            .get("subtasks", {}).get(subtask)
        )
        if not isinstance(item, Mapping) or str(item.get("pipeline_status") or "pending") != "pending":
            raise AuthorizationError("only a pending subtask may refresh its integration base")
        repository = self._repository_for(state, task)
        task_workspace = self._workspace_for(state, task)
        subtask_workspace = self._workspace_for(state, task, subtask)
        branch_record = state.get("subtask_branches", {}).get(task, {}).get(subtask, {})
        subtask_branch = str(branch_record.get("branch") or "")
        if not subtask_branch:
            raise BrokerError("board state has no governed subtask branch")
        with self.project_lock():
            task_identity = self._identity(task_workspace)
            subtask_identity = self._identity(subtask_workspace)
            symbolic = self._run_git(
                ["symbolic-ref", "-q", "HEAD"], cwd=subtask_workspace,
                writable=[subtask_workspace, repository],
            )
            status = self._run_git(
                ["status", "--porcelain=v1", "--untracked-files=all"],
                cwd=subtask_workspace, writable=[subtask_workspace, repository],
            )
            if symbolic.returncode != 0 or symbolic.stdout.strip() != subtask_branch:
                raise RecoveryHoldError("subtask worktree is not attached to its board-derived branch")
            previous_base = str(branch_record.get("base_commit") or "")
            if status.returncode != 0:
                raise RecoveryHoldError("pending subtask worktree status could not be read")
            if status.stdout.strip():
                untracked = self._run_git(
                    ["ls-files", "--others", "--exclude-standard", "-z"],
                    cwd=subtask_workspace, writable=[subtask_workspace, repository],
                )
                old_bytes = self._run_git(
                    ["diff", "--quiet", previous_base, "--"],
                    cwd=subtask_workspace, writable=[subtask_workspace, repository],
                ) if previous_base else None
                recovering_materialization = (
                    subtask_identity == task_identity
                    and untracked.returncode == 0 and not untracked.stdout
                    and old_bytes is not None and old_bytes.returncode == 0
                )
                if not recovering_materialization:
                    raise RecoveryHoldError("pending subtask worktree changed before its start boundary")
            if subtask_identity.commit != task_identity.commit:
                if subtask_identity.commit != previous_base:
                    raise RecoveryHoldError("pending subtask branch moved outside its recorded base")
                self._refuse_tree_filter_attributes(
                    repository, task_identity.commit, "subtask-base-refresh",
                )
                updated = self._run_git(
                    ["update-ref", subtask_branch, task_identity.commit, subtask_identity.commit],
                    cwd=repository, writable=[repository],
                )
                if updated.returncode != 0:
                    raise RecoveryHoldError("pending subtask branch moved during base refresh")
            materialized = self._run_git(
                ["reset", "--hard", task_identity.commit], cwd=subtask_workspace,
                writable=[subtask_workspace, repository],
            )
            refreshed = self._identity(subtask_workspace)
            if materialized.returncode != 0 or refreshed != task_identity:
                raise RecoveryHoldError("refreshed subtask base could not be materialized")
            return {
                **dict(branch_record),
                "workspace": str(subtask_workspace),
                "branch": subtask_branch,
                "base_commit": task_identity.commit,
                "base_tree": task_identity.tree,
                "refreshed_from": previous_base,
            }

    @staticmethod
    def _path_is_owned(path: str, scopes: Sequence[str]) -> bool:
        candidate = Path(path)
        if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
            return False
        for raw_scope in scopes or ["*"]:
            scope = str(raw_scope)
            if scope == "*":
                return True
            scope_parts = Path(scope).parts
            if candidate.parts[:len(scope_parts)] == scope_parts:
                return True
        return False

    def _completed_subtask_fold(
        self,
        repository: Path,
        request_id: str,
        candidate_commit: str,
        current_commit: str,
    ) -> tuple[dict[str, Any], bool] | None:
        """Return an earlier fold whose commit remains in task history."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in self.transaction_records():
            if row.get("operation") == "subtask-fold":
                grouped.setdefault(str(row.get("transaction_id") or ""), []).append(row)
        for transaction, rows in reversed(list(grouped.items())):
            intent = next((row for row in rows if row.get("step") == "intent"), None)
            if (
                not intent or intent.get("request_id") != request_id
                or intent.get("candidate_commit") != candidate_commit
                or any(row.get("step") == "hold" for row in rows)
            ):
                continue
            target = str(intent.get("target_head") or "")
            if not target:
                continue
            ancestor = self._run_git(
                ["merge-base", "--is-ancestor", target, current_commit],
                cwd=repository, writable=[repository],
            )
            if ancestor.returncode == 0:
                return ({
                    "status": "completed_idempotently",
                    "transaction_id": transaction,
                    "operation": "subtask-fold",
                    "task": intent.get("task", ""),
                    "subtask": intent.get("subtask", ""),
                    "request_id": request_id,
                    "commit": target,
                    "tree": intent.get("tree", ""),
                    "candidate_commit": candidate_commit,
                    "manifest": intent.get("manifest", []),
                }, any(row.get("step") == "done" for row in rows))
        return None

    def integrate_subtask(
        self,
        request: Mapping[str, Any],
        *,
        board_mutation: Callable[[dict[str, Any]], None],
        crash_after: str = "",
    ) -> dict[str, Any]:
        """Fold one mirror-certified subtask into the task branch exactly once."""
        state = self.state()
        request_id = str(request.get("id") or "")
        known = state.get("qa_requests", {}).get(request_id)
        if not isinstance(known, Mapping):
            raise AuthorizationError("subtask fold requires an existing board review request")
        task, subtask = str(request.get("task") or ""), str(request.get("subtask") or "")
        if (
            not task or not subtask or request.get("phase") != "subtask_acceptance"
            or known.get("status") != "claimed" or known.get("claimed_by") != request.get("claimed_by")
        ):
            raise AuthorizationError("subtask fold requires the actively claimed subtask acceptance request")
        for field in ("reviewed_commit", "reviewed_tree_hash", "mirror_ref"):
            if str(known.get(field) or "") != str(request.get(field) or ""):
                raise AuthorizationError(f"subtask fold request changed its certified {field}")
        item = (
            state.get("delivery_plans", {}).get(task, {})
            .get("subtasks", {}).get(subtask)
        )
        if not isinstance(item, Mapping) or item.get("active_review_request") != request_id:
            raise AuthorizationError("subtask fold is not the active review for its declared scope")
        branch_record = state.get("subtask_branches", {}).get(task, {}).get(subtask, {})
        base_commit = str(branch_record.get("base_commit") or "")
        candidate_commit = str(request.get("reviewed_commit") or "")
        candidate_tree = str(request.get("reviewed_tree_hash") or "")
        mirror_ref = str(request.get("mirror_ref") or "")
        if not all((base_commit, candidate_commit, candidate_tree, mirror_ref)):
            raise BrokerError("subtask fold lacks its base, candidate, tree, or mirror ref")
        repository = self._repository_for(state, task)
        task_workspace = self._workspace_for(state, task)
        subtask_workspace = self._workspace_for(state, task, subtask)
        task_branch = str(state.get("task_branches", {}).get(task, {}).get("branch") or "")
        if not task_branch:
            raise BrokerError("board state has no governed task branch")
        candidate = self._identity(subtask_workspace, candidate_commit)
        mirrored = self._identity(self.mirror_root, mirror_ref)
        if candidate != GitIdentity(candidate_commit, candidate_tree) or mirrored != candidate:
            raise RecoveryHoldError("subtask candidate, reviewed tree, and immutable mirror do not correspond")
        ancestor = self._run_git(
            ["merge-base", "--is-ancestor", base_commit, candidate_commit],
            cwd=repository, writable=[repository],
        )
        if ancestor.returncode != 0:
            raise BrokerError("reviewed subtask candidate is not descended from its declared base")
        manifest_result = self._run_git(
            ["diff", "--no-ext-diff", "--name-only", "-z", base_commit, candidate_commit, "--"],
            cwd=repository, writable=[repository],
        )
        manifest = sorted(path for path in manifest_result.stdout.split("\0") if path)
        reviewed_manifest = sorted(set(str(path) for path in request.get("reviewed_files", [])))
        if manifest_result.returncode != 0 or not manifest or manifest != reviewed_manifest:
            raise RecoveryHoldError("subtask fold manifest differs from the independently reviewed candidate")
        owned_paths = list(item.get("owned_paths") or ["*"])
        outside = [path for path in manifest if not self._path_is_owned(path, owned_paths)]
        if outside:
            raise AuthorizationError(
                "subtask fold crosses its board-declared ownership boundary: " + ", ".join(outside)
            )
        accepted_manifest = request.get("accepted_byte_manifest")
        if not isinstance(accepted_manifest, dict):
            raise RecoveryHoldError("subtask fold lacks its exact accepted-byte manifest")
        accepted_bytes.verify_manifest(repository, accepted_manifest)
        if (
            accepted_manifest.get("base_commit") != base_commit
            or accepted_manifest.get("reviewed_commit") != candidate_commit
            or accepted_manifest.get("reviewed_tree") != candidate_tree
            or accepted_manifest.get("paths") != manifest
        ):
            raise RecoveryHoldError("accepted-byte manifest differs from the broker-certified subtask")

        with self.project_lock():
            symbolic = self._run_git(
                ["symbolic-ref", "-q", "HEAD"], cwd=task_workspace,
                writable=[task_workspace, repository],
            )
            current = self._identity(task_workspace)
            if symbolic.returncode != 0 or symbolic.stdout.strip() != task_branch:
                raise RecoveryHoldError("task worktree is not attached to its board-derived branch")
            prior = self._completed_subtask_fold(
                repository, request_id, candidate_commit, current.commit,
            )
            if prior:
                outcome, journal_complete = prior
                target_identity = self._identity(repository, str(outcome["commit"]))
                if target_identity.tree != outcome["tree"]:
                    raise RecoveryHoldError("previous subtask fold tree differs from its journal")
                materialized = self._run_git(
                    ["reset", "--hard", current.commit], cwd=task_workspace,
                    writable=[task_workspace, repository],
                )
                if materialized.returncode != 0:
                    raise RecoveryHoldError("previous subtask fold could not be materialized")
                board_mutation(outcome)
                if not journal_complete:
                    self._journal(
                        str(outcome["transaction_id"]), "subtask-fold", "board_mutation",
                        **{key: outcome[key] for key in ("task", "subtask", "request_id", "commit", "tree")},
                    )
                    self._journal(
                        str(outcome["transaction_id"]), "subtask-fold", "done",
                        **{key: outcome[key] for key in ("task", "subtask", "request_id", "commit", "tree")},
                    )
                return outcome
            status = self._run_git(
                ["status", "--porcelain=v1", "--untracked-files=all"],
                cwd=task_workspace, writable=[task_workspace, repository],
            )
            if status.returncode != 0 or status.stdout.strip():
                detail = status.stderr.strip() or status.stdout.strip() or "status unavailable"
                raise RecoveryHoldError(
                    "task worktree must be clean before a reviewed subtask fold: " + detail
                )
            patch = self._run_git(
                ["diff", "--no-ext-diff", "--binary", base_commit, candidate_commit, "--", *manifest],
                cwd=repository, writable=[repository],
            )
            if patch.returncode != 0 or not patch.stdout:
                raise BrokerError("reviewed subtask has no foldable change")
            temporary_index = self.temp_root / f"subtask-fold-{secrets.token_hex(12)}.index"
            try:
                loaded = self._run_git(
                    ["read-tree", current.commit], cwd=repository, writable=[repository],
                    index_file=temporary_index,
                )
                applied = self._run_git(
                    ["apply", "--cached", "--whitespace=error", "-"], cwd=repository,
                    writable=[repository], input=patch.stdout, index_file=temporary_index,
                )
                written = self._run_git(
                    ["write-tree"], cwd=repository, writable=[repository],
                    index_file=temporary_index,
                )
                if loaded.returncode != 0 or applied.returncode != 0 or written.returncode != 0:
                    detail = applied.stderr.strip() or written.stderr.strip() or loaded.stderr.strip()
                    raise BrokerError("reviewed subtask could not be folded cleanly: " + detail)
                target_tree = written.stdout.strip()
                accepted_bytes.verify_entries(repository, target_tree, accepted_manifest)
            finally:
                temporary_index.unlink(missing_ok=True)
            target = self._run_git([
                "-c", "user.name=Harness Git Broker",
                "-c", "user.email=broker@harness.invalid",
                "commit-tree", target_tree, "-p", current.commit, "-p", candidate_commit,
            ], cwd=repository, writable=[repository], input=(
                f"Integrate {task} subtask {subtask}\n\n"
                f"Harness-Review: {request_id}\n"
                f"Reviewed-Commit: {candidate_commit}\n"
            ))
            if target.returncode != 0 or not target.stdout.strip():
                raise BrokerError("subtask integration commit could not be created")
            target_commit = target.stdout.strip()
            accepted_bytes.verify_planned_tree(repository, target_commit, target_tree)
            accepted_verification = accepted_bytes.verify_entries(
                repository, target_commit, accepted_manifest,
            )
            self._refuse_tree_filter_attributes(repository, target_commit, "subtask-fold")
            transaction = f"subtask-{secrets.token_hex(10)}"
            intent = {
                "task": task, "subtask": subtask, "request_id": request_id,
                "branch": task_branch, "previous_head": current.commit,
                "target_head": target_commit, "tree": target_tree,
                "candidate_commit": candidate_commit, "candidate_tree": candidate_tree,
                "base_commit": base_commit, "manifest": manifest,
                "accepted_byte_manifest": accepted_manifest,
            }
            self._journal(transaction, "subtask-fold", "intent", **intent)
            if crash_after == "intent":
                raise InjectedCrash("crash after subtask fold intent")
            updated = self._run_git(
                ["update-ref", task_branch, target_commit, current.commit],
                cwd=repository, writable=[repository],
            )
            if updated.returncode != 0:
                self._hold(transaction, "subtask-fold", "task branch moved during subtask fold", **intent)
                raise RecoveryHoldError("task branch moved during subtask fold")
            self._journal(
                transaction, "subtask-fold", "git_mutation",
                task=task, subtask=subtask, request_id=request_id,
                commit=target_commit, tree=target_tree,
            )
            if crash_after == "git_mutation":
                raise InjectedCrash("crash after subtask fold Git mutation")
            materialized = self._run_git(
                ["reset", "--hard", target_commit], cwd=task_workspace,
                writable=[task_workspace, repository],
            )
            if materialized.returncode != 0 or self._identity(task_workspace) != GitIdentity(target_commit, target_tree):
                self._hold(transaction, "subtask-fold", "integrated task branch could not be materialized", **intent)
                raise RecoveryHoldError("integrated task branch could not be materialized")
            outcome = {
                "status": "completed",
                "transaction_id": transaction,
                "operation": "subtask-fold",
                "task": task, "subtask": subtask, "request_id": request_id,
                "commit": target_commit, "tree": target_tree,
                "candidate_commit": candidate_commit, "manifest": manifest,
                "accepted_byte_manifest": accepted_manifest,
                "accepted_byte_verification": accepted_verification,
            }
            board_mutation(outcome)
            self._journal(
                transaction, "subtask-fold", "board_mutation",
                task=task, subtask=subtask, request_id=request_id,
                commit=target_commit, tree=target_tree,
            )
            if crash_after == "board_mutation":
                raise InjectedCrash("crash after subtask fold board mutation")
            self._journal(
                transaction, "subtask-fold", "done",
                task=task, subtask=subtask, request_id=request_id,
                commit=target_commit, tree=target_tree,
            )
            return outcome

    def _ensure_mirror(self, repository: Path) -> None:
        if not (self.mirror_root / "HEAD").is_file():
            self.mirror_root.parent.mkdir(parents=True, exist_ok=True)
            initialized = self._run_git(["init", "--bare", str(self.mirror_root)], cwd=self.mirror_root.parent, writable=[self.mirror_root.parent])
            if initialized.returncode != 0:
                raise BrokerError("could not initialize project Git mirror")
        probe = self._run_git(["rev-parse", "--is-bare-repository"], cwd=self.mirror_root, writable=[self.mirror_root])
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            raise RecoveryHoldError("project Git mirror is not a bare repository")

    def _import_object(self, repository: Path, identity: GitIdentity) -> None:
        fetched = self._run_git(
            ["fetch", "--no-tags", str(repository), identity.commit],
            cwd=self.mirror_root, writable=[self.mirror_root], readable=[repository],
            allow_shell=True,
        )
        if fetched.returncode != 0:
            raise BrokerError("candidate objects could not be imported into the mirror")
        mirrored = self._identity(self.mirror_root, identity.commit)
        if mirrored != identity:
            raise RecoveryHoldError("local repository and mirror objects do not correspond")

    def _read_ref(self, repository: Path, ref: str) -> str:
        result = self._run_git(["rev-parse", "--verify", ref], cwd=repository, writable=[repository])
        return result.stdout.strip() if result.returncode == 0 else ""

    def create_review_ref(
        self,
        request: Mapping[str, Any],
        *,
        review_number: int,
        board_mutation: Callable[[dict[str, Any]], None],
        crash_after: str = "",
    ) -> dict[str, Any]:
        """Create and board-record one immutable review ref transaction."""
        task = str(request.get("task") or "")
        commit = str(request.get("reviewed_commit") or "")
        tree = str(request.get("reviewed_tree_hash") or "")
        if not task or not commit or not tree or int(review_number) <= 0:
            raise BrokerError("review request lacks an immutable commit/tree candidate")
        state = self.state()
        known = state.get("qa_requests", {}).get(request.get("id"))
        if not isinstance(known, Mapping):
            raise AuthorizationError("record-review-ref requires an existing board review request")
        repository = self._repository_for(state, task)
        local = self._identity(repository, commit)
        if local.tree != tree:
            raise BrokerError("reviewed tree does not match the candidate commit")
        ref = f"refs/harness/{_safe(task)}/reviewed-{int(review_number)}"
        transaction = f"mirror-{secrets.token_hex(10)}"
        with self.project_lock():
            self._ensure_mirror(repository)
            self._import_object(repository, local)
            self._journal(transaction, "mirror-ref-create", "intent", task=task, request_id=request.get("id"), ref=ref, commit=commit, tree=tree, previous_main=ZERO_OID, target_main=commit)
            if crash_after == "intent":
                raise InjectedCrash("crash after mirror intent")
            existing = self._read_ref(self.mirror_root, ref)
            if existing:
                if existing != commit:
                    self._hold(transaction, "mirror-ref-create", "create-only mirror ref mismatch", ref=ref, existing=existing, intended=commit)
                    raise RecoveryHoldError("create-only mirror ref exists with a different commit")
                prior_by_transaction: dict[str, list[dict[str, Any]]] = {}
                for row in self.transaction_records():
                    prior_by_transaction.setdefault(str(row.get("transaction_id") or ""), []).append(row)
                matching_retry = any(
                    transaction_id != transaction
                    and any(row.get("step") == "intent" and row.get("ref") == ref and row.get("commit") == commit for row in rows)
                    and not any(row.get("step") == "done" for row in rows)
                    for transaction_id, rows in prior_by_transaction.items()
                )
                if not matching_retry:
                    raise BrokerError("create-only mirror ref already exists; overwrite and duplicate creation are refused")
            else:
                mutation = self._run_git(["update-ref", ref, commit, ZERO_OID], cwd=self.mirror_root, writable=[self.mirror_root])
                if mutation.returncode != 0:
                    raise BrokerError("create-only mirror ref mutation failed")
            mirrored = self._identity(self.mirror_root, ref)
            if mirrored != local:
                self._hold(transaction, "mirror-ref-create", "mirror correspondence failed", ref=ref, intended=commit)
                raise RecoveryHoldError("mirror ref does not correspond to the reviewed candidate")
            self._journal(transaction, "mirror-ref-create", "git_mutation", ref=ref, commit=commit, tree=tree)
            if crash_after == "git_mutation":
                raise InjectedCrash("crash after mirror mutation")
            record = {"ref": ref, "commit": commit, "tree": tree, "transaction_id": transaction}
            board_mutation(record)
            self._journal(transaction, "mirror-ref-create", "board_mutation", request_id=request.get("id"), ref=ref, commit=commit, tree=tree)
            if crash_after == "board_mutation":
                raise InjectedCrash("crash after mirror board mutation")
            self._journal(transaction, "mirror-ref-create", "done", ref=ref, commit=commit, tree=tree)
            return record

    def accept_merge(
        self,
        task: str,
        candidate: Mapping[str, Any],
        *,
        board_mutation: Callable[[dict[str, Any]], None],
        crash_after: str = "",
    ) -> dict[str, Any]:
        """CAS main to an owner-accepted, mirror-certified final candidate."""
        state = self.state()
        decision = state.get("release_decisions", {}).get(task, {})
        if decision.get("decision") != "accepted":
            raise AuthorizationError("accept-merge requires a durable owner Accept decision")
        repository = self._repository_for(state, task)
        base = str(candidate.get("recorded_base") or state.get("task_baselines", {}).get(task, {}).get("head") or "")
        commit = str(candidate.get("commit") or "")
        tree = str(candidate.get("tree") or "")
        mirror_ref = str(candidate.get("mirror_ref") or "")
        manifest = sorted(set(candidate.get("manifest") or []))
        if not all((base, commit, tree, mirror_ref)):
            raise BrokerError("acceptance candidate is incomplete")
        transaction = f"accept-{secrets.token_hex(10)}"
        with self.project_lock():
            main_ref = "refs/heads/main"
            previous = self._read_ref(repository, main_ref)
            if previous != base:
                self._journal(transaction, "accept-merge", "intent", task=task, previous_main=base, observed_main=previous, target_main=commit, status="moved_main")
                raise MainMovedError("main moved; re-integrate on the task branch and repeat final QA and independent review")
            ancestry = self._run_git(
                ["merge-base", "--is-ancestor", base, commit],
                cwd=repository, writable=[repository],
            )
            if ancestry.returncode != 0:
                raise MainMovedError(
                    "reviewed candidate does not contain the release-ready main commit; "
                    "re-integrate and repeat affected final QA plus independent review"
                )
            local, mirrored = self._identity(repository, commit), self._identity(self.mirror_root, mirror_ref)
            if local.commit != commit or local.tree != tree or mirrored != local:
                raise BrokerError("candidate does not match its immutable mirror certification")
            self._refuse_tree_filter_attributes(repository, commit, "accept-merge")
            changed = self._run_git(["diff", "--no-ext-diff", "--name-only", f"{base}..{commit}"], cwd=repository, writable=[repository])
            changed_paths = sorted(line for line in changed.stdout.splitlines() if line)
            if changed.returncode != 0 or changed_paths != manifest:
                raise BrokerError("candidate manifest does not match the certified Git range")
            tracked_status = self._run_git(["status", "--porcelain=v1", "--untracked-files=no"], cwd=repository, writable=[repository])
            if tracked_status.returncode != 0 or tracked_status.stdout.strip():
                raise BrokerError("acceptance requires a clean governed main worktree")
            self._journal(transaction, "accept-merge", "intent", task=task, mirror_ref=mirror_ref, tree=tree, manifest=manifest, previous_main=base, target_main=commit)
            if crash_after == "intent":
                raise InjectedCrash("crash after acceptance intent")
            mutation = self._run_git(["update-ref", main_ref, commit, base], cwd=repository, writable=[repository])
            if mutation.returncode != 0:
                raise BrokerError("acceptance CAS failed with zero Git side effects")
            self._journal(transaction, "accept-merge", "git_mutation", previous_main=base, target_main=commit)
            if crash_after == "git_mutation":
                raise InjectedCrash("crash after acceptance Git mutation")
            materialized = self._run_git(["reset", "--hard", commit], cwd=repository, writable=[repository])
            if materialized.returncode != 0:
                self._hold(transaction, "accept-merge", "accepted ref could not be materialized", previous_main=base, target_main=commit)
                raise RecoveryHoldError("main ref advanced but its worktree could not be materialized")
            accepted = self._identity(repository, main_ref)
            if accepted != local:
                self._hold(transaction, "accept-merge", "accepted tree mismatch", previous_main=base, target_main=commit)
                raise RecoveryHoldError("accepted main does not equal the certified tree")
            if self._identity(self.mirror_root, mirror_ref) != accepted:
                self._hold(transaction, "accept-merge", "post-acceptance mirror correspondence failed", previous_main=base, target_main=commit)
                raise RecoveryHoldError("accepted main no longer corresponds to the immutable mirror ref")
            record = {"task": task, "commit": commit, "tree": tree, "mirror_ref": mirror_ref, "manifest": manifest, "transaction_id": transaction}
            board_mutation(record)
            self._journal(transaction, "accept-merge", "board_mutation", task=task, commit=commit, tree=tree, mirror_ref=mirror_ref, manifest=manifest)
            if crash_after == "board_mutation":
                raise InjectedCrash("crash after acceptance board mutation")
            self._journal(transaction, "accept-merge", "done", task=task, commit=commit, tree=tree, mirror_ref=mirror_ref, manifest=manifest)
            return record

    def remote_push(
        self,
        task: str,
        *,
        board_mutation: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        """Push only the exact accepted commit after two durable owner gates."""
        state = self.state()
        acceptance = state.get("git_acceptances", {}).get(task, {})
        instruction = state.get("remote_push_instructions", {}).get(task, {})
        if not acceptance.get("commit"):
            raise AuthorizationError("remote-push requires a completed local acceptance")
        if not instruction.get("owner_instructed_at") or not instruction.get("confirmed_at"):
            raise AuthorizationError("remote-push requires durable owner instruction and immediate confirmation")
        if instruction.get("used_at"):
            raise AuthorizationError("remote-push confirmation was already consumed")
        remote, branch = str(instruction.get("remote") or ""), str(instruction.get("branch") or "")
        if not remote or not branch or branch.startswith("-") or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
            raise BrokerError("approved push destination is invalid")
        if branch.startswith("refs/tags/") or branch.endswith("/") or ".." in branch or branch.startswith("refs/") and not branch.startswith("refs/heads/"):
            raise BrokerError("tags, deletions, and foreign ref namespaces are refused")
        repository = self._repository_for(state, task)
        approved = state.get("approved_remotes", {}).get(task, {})
        if approved.get("name") != remote or approved.get("branch") != branch:
            raise AuthorizationError("remote or branch is not board-approved")
        destination = self.inspect_push_destination(
            task, remote, branch, str(instruction.get("expected_remote_tip") or ""),
        )
        if destination["url"] != approved.get("url"):
            raise AuthorizationError("configured remote no longer matches the approved destination")
        push_writable = [repository]
        approved_url = str(approved.get("url") or "")
        local_remote = Path(approved_url.removeprefix("file://"))
        local_transport = bool(
            (approved_url.startswith("file://") or local_remote.is_absolute())
            and local_remote.exists()
        )
        if local_transport:
            push_writable.append(local_remote.resolve())
        commit = str(acceptance["commit"])
        local = self._identity(repository, commit)
        if local.tree != acceptance.get("tree"):
            raise RecoveryHoldError("accepted commit/tree identity changed before push")
        remote_ref = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
        expected = str(instruction.get("expected_remote_tip") or ZERO_OID)
        with self.project_lock():
            observed_result = self._run_git(
                ["ls-remote", "--heads", remote, remote_ref], cwd=repository,
                writable=[repository], readable=[local_remote.resolve()] if local_transport else [],
                sandbox_network=True, allow_shell=local_transport,
            )
            if observed_result.returncode != 0:
                raise BrokerError("approved remote tip could not be reconfirmed")
            line = next((line for line in observed_result.stdout.splitlines() if line.endswith("\t" + remote_ref)), "")
            observed = line.split()[0] if line else ZERO_OID
            if observed != expected:
                raise BrokerError("remote tip drifted; owner action is required before any push")
            pushed = self._run_git(
                ["push", "--porcelain", "--receive-pack=git-receive-pack", remote, f"{commit}:{remote_ref}"],
                cwd=repository, writable=push_writable, sandbox_network=True,
                allow_shell=local_transport,
            )
            record = {
                "task": task, "commit": commit, "tree": local.tree, "remote": remote,
                "branch": remote_ref, "expected_remote_tip": expected,
                "outcome": "pushed" if pushed.returncode == 0 else "failed",
                "output": (pushed.stdout + pushed.stderr)[-2000:], "at": _now(),
            }
            board_mutation(record)
            if pushed.returncode != 0:
                raise BrokerError("remote push failed; the accepted local release remains valid")
            return record

    def inspect_push_destination(
        self,
        task: str,
        remote: str,
        branch: str,
        expected_remote_tip: str = "",
    ) -> dict[str, str]:
        """Resolve a push destination without contacting it.

        Owner instruction is only the first authorization gate.  In
        particular, recording it must not perform ``ls-remote`` or otherwise
        touch the network; the confirmed :meth:`remote_push` operation is the
        sole network-capable broker call.
        """
        state = self.state()
        repository = self._repository_for(state, task)
        remote, branch = str(remote).strip(), str(branch).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", remote):
            raise BrokerError("approved remote name is invalid")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith("-"):
            raise BrokerError("approved branch is invalid")
        remote_ref = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
        if ".." in remote_ref or remote_ref.endswith("/"):
            raise BrokerError("approved branch is invalid")
        destination = self._run_git(
            ["remote", "get-url", "--push", remote],
            cwd=repository, writable=[repository], sandbox_network=False,
        )
        if destination.returncode != 0 or not destination.stdout.strip():
            raise BrokerError("the instructed remote is not configured for this project")
        url = destination.stdout.strip()
        if "\n" in url or "\r" in url or url.startswith("-") or "::" in url:
            raise BrokerError("remote helpers and malformed destinations are refused")
        parsed_local = Path(url.removeprefix("file://"))
        safe_network = bool(
            re.match(r"^(?:https|ssh|git)://[^\s]+$", url)
            or re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+$", url)
        )
        safe_local = (url.startswith("file://") or parsed_local.is_absolute()) and parsed_local.is_absolute()
        if not (safe_network or safe_local):
            raise BrokerError("remote destination uses an unapproved transport shape")
        expected = str(expected_remote_tip or "").strip()
        if expected and not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise BrokerError("expected remote tip must be a full commit hash")
        if not expected:
            short_branch = remote_ref.removeprefix("refs/heads/")
            tracking_ref = f"refs/remotes/{remote}/{short_branch}"
            expected = self._read_ref(repository, tracking_ref) or ZERO_OID
        return {"url": url, "remote_ref": remote_ref, "expected_remote_tip": expected}

    def transaction_records(self) -> list[dict[str, Any]]:
        if not self.transactions_path.is_file():
            return []
        records = []
        for line in self.transactions_path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise RecoveryHoldError("transaction journal is corrupt") from error
            if isinstance(value, dict):
                records.append(value)
        return records

    def audit_mirror_records(self) -> list[dict[str, Any]]:
        """Fail closed when a board-certified review lost its immutable ref."""
        state = self.state()
        requests = list(state.get("qa_requests", {}).values())
        requests.extend(
            entry.get("value", {}) for entry in state.get("archive", [])
            if entry.get("kind") == "qa_request"
        )
        holds = []
        for request in requests:
            ref = str(request.get("mirror_ref") or "")
            if not ref:
                continue
            expected_commit = str(request.get("mirror_commit") or request.get("reviewed_commit") or "")
            expected_tree = str(request.get("mirror_tree_hash") or request.get("reviewed_tree_hash") or "")
            current = self._read_ref(self.mirror_root, ref) if self.mirror_root.is_dir() else ""
            valid = False
            if current == expected_commit:
                try:
                    valid = self._identity(self.mirror_root, ref).tree == expected_tree
                except BrokerError:
                    valid = False
            if valid:
                continue
            transaction = f"audit-{hashlib.sha256((str(request.get('id')) + ref).encode()).hexdigest()[:16]}"
            holds.append(self._hold(
                transaction, "mirror-ref-create",
                "board review record has a missing or mismatched mirror ref",
                request_id=request.get("id", ""), ref=ref,
                expected_commit=expected_commit, observed_commit=current,
            ))
        return holds

    def recover(self, board_mutation: Callable[[dict[str, Any]], None] | None = None) -> list[dict[str, Any]]:
        """Reconcile incomplete acceptance and mirror transactions.

        Recovery never resets or absorbs an unexpected main commit.  The
        returned records are also suitable for the worker to project into a
        durable CTO hold/event while keeping raw Git writes inside the broker.
        """
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in self.transaction_records():
            grouped.setdefault(str(record.get("transaction_id") or ""), []).append(record)
        outcomes: list[dict[str, Any]] = []
        for transaction, records in grouped.items():
            if not transaction or any(item.get("step") == "done" for item in records):
                continue
            intent = next((item for item in records if item.get("step") == "intent"), None)
            if not intent:
                outcomes.append(self._hold(transaction, "unknown", "journal has no intent record"))
                continue
            operation = str(intent.get("operation") or "")
            if operation == "mirror-ref-create":
                ref, target = str(intent.get("ref") or ""), str(intent.get("commit") or "")
                current = self._read_ref(self.mirror_root, ref) if self.mirror_root.is_dir() else ""
                if current and current != target:
                    outcome = self._hold(transaction, operation, "mirror ref mismatches incomplete intent", ref=ref, existing=current, intended=target)
                elif current == target:
                    outcome = {"status": "completed_idempotently", "transaction_id": transaction, "operation": operation, "ref": ref, "commit": target, "tree": intent.get("tree", "")}
                    if board_mutation:
                        try:
                            board_mutation(outcome)
                        except BrokerError:
                            outcome = {
                                **outcome,
                                "status": "awaiting_board_request_retry",
                            }
                            self._journal(
                                transaction, operation, "recovery_deferred",
                                status=outcome["status"], ref=ref, commit=target,
                            )
                            outcomes.append(outcome)
                            continue
                    self._journal(transaction, operation, "board_mutation", status=outcome["status"], ref=ref, commit=target, tree=intent.get("tree", ""))
                    self._journal(transaction, operation, "done", status=outcome["status"], ref=ref, commit=target, tree=intent.get("tree", ""))
                else:
                    outcome = {"status": "not_applied", "transaction_id": transaction, "operation": operation, "ref": ref}
                    self._journal(transaction, operation, "done", status=outcome["status"], ref=ref)
                outcomes.append(outcome)
                continue
            if operation == "subtask-fold":
                state = self.state()
                task = str(intent.get("task") or "")
                repository = self._repository_for(state, task)
                workspace = self._workspace_for(state, task)
                branch = str(state.get("task_branches", {}).get(task, {}).get("branch") or "")
                previous = str(intent.get("previous_head") or "")
                target = str(intent.get("target_head") or "")
                current = self._read_ref(repository, branch) if branch else ""
                contains_target = False
                if current and target:
                    contains_target = self._run_git(
                        ["merge-base", "--is-ancestor", target, current],
                        cwd=repository, writable=[repository],
                    ).returncode == 0
                if contains_target:
                    identity = self._identity(repository, target)
                    if identity.tree != intent.get("tree"):
                        outcome = self._hold(
                            transaction, operation,
                            "integrated subtask tree differs from journal",
                            task=task, current=current, target=target,
                        )
                    else:
                        try:
                            self._refuse_tree_filter_attributes(
                                repository, current, "subtask-fold-recovery",
                            )
                        except FilterRequiredError:
                            outcomes.append(self._hold(
                                transaction, operation,
                                "recovery refused attribute-driven filter materialization",
                                task=task, current=current, target=target,
                            ))
                            continue
                        materialized = self._run_git(
                            ["reset", "--hard", current], cwd=workspace,
                            writable=[workspace, repository],
                        )
                        if materialized.returncode != 0:
                            outcomes.append(self._hold(
                                transaction, operation,
                                "recovery could not materialize integrated task branch",
                                task=task, current=current, target=target,
                            ))
                            continue
                        outcome = {
                            "status": "completed_idempotently",
                            "transaction_id": transaction,
                            "operation": operation,
                            "task": task,
                            "subtask": intent.get("subtask", ""),
                            "request_id": intent.get("request_id", ""),
                            "commit": target,
                            "tree": identity.tree,
                            "candidate_commit": intent.get("candidate_commit", ""),
                            "manifest": intent.get("manifest", []),
                        }
                        if board_mutation:
                            board_mutation(outcome)
                        self._journal(
                            transaction, operation, "board_mutation",
                            status=outcome["status"], task=task,
                            subtask=outcome["subtask"], request_id=outcome["request_id"],
                            commit=target, tree=identity.tree,
                        )
                        self._journal(
                            transaction, operation, "done",
                            status=outcome["status"], task=task,
                            subtask=outcome["subtask"], request_id=outcome["request_id"],
                            commit=target, tree=identity.tree,
                        )
                elif current == previous:
                    outcome = {
                        "status": "not_applied",
                        "transaction_id": transaction,
                        "operation": operation,
                        "task": task,
                        "subtask": intent.get("subtask", ""),
                        "request_id": intent.get("request_id", ""),
                    }
                    self._journal(
                        transaction, operation, "done", status=outcome["status"],
                        task=task, subtask=outcome["subtask"],
                        request_id=outcome["request_id"],
                    )
                else:
                    outcome = self._hold(
                        transaction, operation,
                        "task branch changed after incomplete subtask fold",
                        task=task, previous_head=previous,
                        target_head=target, observed_head=current,
                    )
                outcomes.append(outcome)
                continue
            if operation == "accept-merge":
                state = self.state()
                repository = self._repository_for(state, str(intent.get("task") or ""))
                previous, target = str(intent.get("previous_main") or ""), str(intent.get("target_main") or "")
                current = self._read_ref(repository, "refs/heads/main")
                if current == target:
                    identity = self._identity(repository, current)
                    if identity.tree != intent.get("tree"):
                        outcome = self._hold(transaction, operation, "target main tree differs from journal", current=current, target=target)
                    else:
                        try:
                            self._refuse_tree_filter_attributes(repository, target, "accept-merge-recovery")
                        except FilterRequiredError:
                            outcomes.append(self._hold(
                                transaction, operation,
                                "recovery refused attribute-driven filter materialization",
                                current=current, target=target,
                            ))
                            continue
                        materialized = self._run_git(["reset", "--hard", target], cwd=repository, writable=[repository])
                        if materialized.returncode != 0:
                            outcomes.append(self._hold(transaction, operation, "recovery could not materialize accepted main", current=current, target=target))
                            continue
                        outcome = {"status": "completed_idempotently", "transaction_id": transaction, "operation": operation, "task": intent.get("task"), "commit": target, "tree": identity.tree, "mirror_ref": intent.get("mirror_ref", ""), "manifest": intent.get("manifest", [])}
                        if board_mutation:
                            board_mutation(outcome)
                        self._journal(transaction, operation, "board_mutation", status=outcome["status"], task=intent.get("task"), commit=target, tree=identity.tree)
                        self._journal(transaction, operation, "done", status=outcome["status"], task=intent.get("task"), commit=target, tree=identity.tree)
                elif current == previous:
                    outcome = {"status": "not_applied", "transaction_id": transaction, "operation": operation, "task": intent.get("task")}
                    self._journal(transaction, operation, "done", status=outcome["status"], task=intent.get("task"))
                else:
                    outcome = self._hold(transaction, operation, "external commit after crash preserved", previous_main=previous, target_main=target, observed_main=current)
                outcomes.append(outcome)
        return outcomes

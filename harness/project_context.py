# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Project-scoped filesystem context for harness storage and execution.

The compatibility constructor is deliberately the only implicit conversion
from a bare path.  It preserves every path used by the original single-root
harness while allowing project-manager callers to provide an explicit context
whose managed data lives outside an adopted repository.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Union


def _canonical(path: str | os.PathLike[str]) -> Path:
    value = Path(path)
    if value.parts and value.parts[0].startswith("~"):
        raise ValueError("project roots must not depend on HOME; expand '~' before the boundary")
    return value.resolve(strict=False)


@dataclass(frozen=True)
class ProjectContext:
    """The three roots that define one harness project.

    ``data_root`` directly contains every managed storage namespace (``board``,
    ``control``, ``tasks``, ``evidence``, and so on).  Callers must never append
    an implicit ``.harness`` below it.
    """

    code_root: Path
    data_root: Path
    workspace_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "code_root", _canonical(self.code_root))
        object.__setattr__(self, "data_root", _canonical(self.data_root))
        object.__setattr__(self, "workspace_root", _canonical(self.workspace_root))

    @classmethod
    def compatibility(cls, root: str | os.PathLike[str]) -> "ProjectContext":
        """Return the byte-compatible context for the legacy single root."""
        code_root = _canonical(root)
        return cls(
            code_root=code_root,
            data_root=code_root / ".harness",
            workspace_root=code_root.parent / ".harness-task-workspaces",
        )

    def storage_path(self, *parts: str | os.PathLike[str]) -> Path:
        """Resolve a managed path directly below ``data_root``.

        Storage namespaces are relative components.  Rejecting absolute paths
        and traversal here prevents a later module from silently escaping the
        project's managed data root.
        """
        path = self.data_root
        for raw in parts:
            component = Path(raw)
            if component.is_absolute() or ".." in component.parts:
                raise ValueError("project storage paths must stay relative to data_root")
            path /= component
        return path

    @property
    def is_compatibility(self) -> bool:
        return (
            self.data_root == self.code_root / ".harness"
            and self.workspace_root == self.code_root.parent / ".harness-task-workspaces"
        )

    @property
    def board_backup_root(self) -> Path:
        """Return the external board backup location.

        Compatibility mode retains the historical byte-identical location.
        Explicit contexts place backups beside ``data_root`` as required by the
        Projects Layer design.
        """
        if self.is_compatibility:
            return self.code_root.parent / ".harness-board-backups" / self.code_root.name
        return self.data_root.parent / f"{self.data_root.name}-backups"


ProjectRoot = Union[ProjectContext, str, os.PathLike]


def project_context(value: ProjectRoot) -> ProjectContext:
    """Coerce legacy root arguments to their exact compatibility context."""
    if isinstance(value, ProjectContext):
        return value
    return ProjectContext.compatibility(value)


def context_from_roots(
    code_root: str | os.PathLike[str],
    data_root: str | os.PathLike[str] | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
) -> ProjectContext:
    """Build a CLI context exclusively from explicit command-line roots."""
    if bool(data_root) != bool(workspace_root):
        raise ValueError("data_root and workspace_root must be supplied together")
    if data_root and workspace_root:
        return ProjectContext(Path(code_root), Path(data_root), Path(workspace_root))
    return ProjectContext.compatibility(code_root)


def add_context_arguments(parser, *, root_required: bool = False, root_default: str = ".") -> None:
    parser.add_argument("--root", required=root_required, default=None if root_required else root_default)
    parser.add_argument("--data-root", default="")
    parser.add_argument("--workspace-root", default="")


def context_from_args(args) -> ProjectContext:
    return context_from_roots(args.root, args.data_root, args.workspace_root)


def context_environment(context: ProjectRoot) -> dict[str, str]:
    """Serialize roots for environment-isolation tests, never for resolution."""
    value = project_context(context)
    return {
        "HARNESS_CODE_ROOT": str(value.code_root),
        "HARNESS_DATA_ROOT": str(value.data_root),
        "HARNESS_WORKSPACE_ROOT": str(value.workspace_root),
    }


def context_cli_arguments(context: ProjectRoot) -> list[str]:
    value = project_context(context)
    return [
        "--root", str(value.code_root),
        "--data-root", str(value.data_root),
        "--workspace-root", str(value.workspace_root),
    ]

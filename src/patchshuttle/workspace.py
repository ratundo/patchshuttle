"""Workspace discovery and non-overwriting initialization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from os import PathLike
from pathlib import Path

from patchshuttle.config import (
    PatchShuttleConfig,
    ProjectOrigin,
    load_config,
    render_default_config,
)
from patchshuttle.errors import WorkspaceError, WorkspaceErrorCode
from patchshuttle.identifiers import generate_project_id
from patchshuttle.models import Job

CONFIG_RELATIVE_PATH = Path("patches/patchshuttle.toml")
_MANAGED_DIRECTORIES = (
    Path("patches"),
    Path("patches/inbox"),
    Path("patches/applied"),
    Path("patches/failed"),
    Path("patches/logs"),
    Path("patches/backups"),
    Path("patches/state"),
    Path("patches/examples"),
)
_MANAGED_FILES = (
    CONFIG_RELATIVE_PATH,
    Path("patches/AI_GUIDE.md"),
    Path("patches/PATCHSHUTTLE_PROTOCOL.md"),
    Path("patches/patchshuttle.schema.json"),
    Path("patches/state/registry.json"),
    Path("patches/state/run.lock"),
    Path("patches/examples/AUDIT-EXAMPLE.psh.yaml"),
    Path("patches/examples/PATCH-EXAMPLE.psh.yaml"),
)
_OS_METADATA_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


class WorkspaceInitStatus(str, Enum):
    """Observable result of one safe initialization attempt."""

    INITIALIZED = "INITIALIZED"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True, slots=True)
class Workspace:
    """One resolved project root and its typed local configuration."""

    root: Path
    config: PatchShuttleConfig

    @property
    def project_id(self) -> str:
        return self.config.project.project_id

    @property
    def origin(self) -> ProjectOrigin:
        return self.config.project.origin

    @property
    def patches_dir(self) -> Path:
        return self.root / "patches"

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_RELATIVE_PATH

    def require_project_id(self, project_id: str) -> None:
        """Reject a job created for a different workspace."""

        if project_id != self.project_id:
            raise WorkspaceError(
                WorkspaceErrorCode.PROJECT_ID_MISMATCH,
                "job project_id does not match the initialized workspace",
                path="$.project_id",
            )


@dataclass(frozen=True, slots=True)
class WorkspaceInitResult:
    """Workspace plus the exact relative entries created by ``init``."""

    workspace: Workspace
    status: WorkspaceInitStatus
    created_paths: tuple[Path, ...]


def init_workspace(
    root: str | PathLike[str] = ".",
    *,
    new_project: bool = False,
) -> WorkspaceInitResult:
    """Initialize one project root without overwriting any existing entry."""

    workspace_root = _resolve_directory(root)
    _preflight_managed_paths(workspace_root)
    config_path = workspace_root / CONFIG_RELATIVE_PATH
    config_preexisted = _path_exists(config_path)
    created_paths: list[Path] = []

    if config_preexisted:
        _require_regular_managed_file(config_path, CONFIG_RELATIVE_PATH)
        config = load_config(config_path)
        _require_compatible_origin(config, new_project=new_project)
    else:
        if new_project:
            _require_new_project_contents(workspace_root)

        if _ensure_directory(workspace_root, Path("patches")):
            created_paths.append(Path("patches"))

        project_id = generate_project_id()
        origin = ProjectOrigin.NEW if new_project else ProjectOrigin.EXISTING
        config_created = _create_file_if_missing(
            workspace_root,
            CONFIG_RELATIVE_PATH,
            render_default_config(project_id, origin),
        )
        if config_created:
            created_paths.append(CONFIG_RELATIVE_PATH)
        config = load_config(config_path)
        _require_compatible_origin(config, new_project=new_project)

    for relative_path in _MANAGED_DIRECTORIES:
        if _ensure_directory(workspace_root, relative_path):
            created_paths.append(relative_path)

    for relative_path, content in _generated_files(config).items():
        if relative_path == CONFIG_RELATIVE_PATH:
            continue
        if _create_file_if_missing(workspace_root, relative_path, content):
            created_paths.append(relative_path)

    status = (
        WorkspaceInitStatus.INITIALIZED
        if not config_preexisted
        else (
            WorkspaceInitStatus.UPDATED
            if created_paths
            else WorkspaceInitStatus.UNCHANGED
        )
    )
    workspace = Workspace(root=workspace_root, config=config)
    return WorkspaceInitResult(
        workspace=workspace,
        status=status,
        created_paths=tuple(created_paths),
    )


def load_workspace(root: str | PathLike[str] = ".") -> Workspace:
    """Load an initialized workspace at an exact project root."""

    workspace_root = _resolve_directory(root)
    _require_managed_directory_if_present(workspace_root, Path("patches"))
    config_path = workspace_root / CONFIG_RELATIVE_PATH
    if not _path_exists(config_path):
        raise WorkspaceError(
            WorkspaceErrorCode.WORKSPACE_NOT_INITIALIZED,
            "no patches/patchshuttle.toml was found at the workspace root",
        )
    _require_regular_managed_file(config_path, CONFIG_RELATIVE_PATH)
    return Workspace(root=workspace_root, config=load_config(config_path))


def discover_workspace(start: str | PathLike[str] = ".") -> Workspace:
    """Find the nearest initialized workspace at ``start`` or one of its parents."""

    current = _resolve_directory(start)
    for candidate in (current, *current.parents):
        config_path = candidate / CONFIG_RELATIVE_PATH
        if _path_exists(config_path):
            return load_workspace(candidate)
    raise WorkspaceError(
        WorkspaceErrorCode.WORKSPACE_NOT_INITIALIZED,
        "no initialized PatchShuttle workspace was found",
    )


def _resolve_directory(path: str | PathLike[str]) -> Path:
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise WorkspaceError(
            WorkspaceErrorCode.WORKSPACE_NOT_FOUND,
            "workspace path was not found",
        ) from exc
    except OSError as exc:
        raise WorkspaceError(
            WorkspaceErrorCode.WORKSPACE_READ_FAILED,
            "workspace path could not be resolved",
        ) from exc

    if not resolved.is_dir():
        raise WorkspaceError(
            WorkspaceErrorCode.WORKSPACE_NOT_DIRECTORY,
            "workspace path must identify a directory",
        )
    return resolved


def _require_new_project_contents(root: Path) -> None:
    try:
        entries = tuple(root.iterdir())
    except OSError as exc:
        raise WorkspaceError(
            WorkspaceErrorCode.WORKSPACE_READ_FAILED,
            "new-project directory could not be inspected",
        ) from exc

    unexpected = [entry.name for entry in entries if not _is_allowed_metadata(entry)]
    if unexpected:
        visible = ", ".join(sorted(unexpected)[:5])
        raise WorkspaceError(
            WorkspaceErrorCode.NEW_PROJECT_NOT_EMPTY,
            f"--new-project requires an empty directory; found: {visible}",
        )


def _is_allowed_metadata(entry: Path) -> bool:
    if entry.is_symlink():
        return False
    if entry.name == ".git":
        return entry.is_dir()
    if entry.name in _OS_METADATA_FILES or entry.name.startswith("._"):
        return entry.is_file()
    return False


def _require_compatible_origin(
    config: PatchShuttleConfig,
    *,
    new_project: bool,
) -> None:
    if new_project and config.project.origin is not ProjectOrigin.NEW:
        raise WorkspaceError(
            WorkspaceErrorCode.PROJECT_ORIGIN_CONFLICT,
            "--new-project cannot change an existing-project workspace",
            path="$.project.origin",
        )


def _ensure_directory(root: Path, relative_path: Path) -> bool:
    path = root / relative_path
    if path.is_symlink():
        raise _managed_path_conflict(relative_path, "symbolic links are not allowed")
    try:
        path.mkdir()
    except FileExistsError:
        if path.is_dir() and not path.is_symlink():
            return False
        raise _managed_path_conflict(relative_path, "expected a directory")
    except OSError as exc:
        raise WorkspaceError(
            WorkspaceErrorCode.WORKSPACE_WRITE_FAILED,
            "managed directory could not be created",
            path=relative_path.as_posix(),
        ) from exc
    return True


def _require_managed_directory_if_present(root: Path, relative_path: Path) -> None:
    path = root / relative_path
    if path.is_symlink():
        raise _managed_path_conflict(relative_path, "symbolic links are not allowed")
    if path.exists() and not path.is_dir():
        raise _managed_path_conflict(relative_path, "expected a directory")


def _preflight_managed_paths(root: Path) -> None:
    for relative_path in _MANAGED_DIRECTORIES:
        _require_managed_directory_if_present(root, relative_path)
    for relative_path in _MANAGED_FILES:
        path = root / relative_path
        if _path_exists(path):
            _require_regular_managed_file(path, relative_path)


def _create_file_if_missing(root: Path, relative_path: Path, content: str) -> bool:
    path = root / relative_path
    if path.is_symlink():
        raise _managed_path_conflict(relative_path, "symbolic links are not allowed")

    created = False
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            created = True
            stream.write(content)
    except FileExistsError:
        _require_regular_managed_file(path, relative_path)
        return False
    except OSError as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass  # pragma: no cover - best-effort cleanup after write failure
        raise WorkspaceError(
            WorkspaceErrorCode.WORKSPACE_WRITE_FAILED,
            "managed file could not be created",
            path=relative_path.as_posix(),
        ) from exc
    return True


def _require_regular_managed_file(path: Path, relative_path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise _managed_path_conflict(relative_path, "expected a regular file")


def _managed_path_conflict(relative_path: Path, message: str) -> WorkspaceError:
    return WorkspaceError(
        WorkspaceErrorCode.MANAGED_PATH_CONFLICT,
        message,
        path=relative_path.as_posix(),
    )


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _generated_files(config: PatchShuttleConfig) -> dict[Path, str]:
    project_id = config.project.project_id
    return {
        CONFIG_RELATIVE_PATH: render_default_config(project_id, config.project.origin),
        Path("patches/AI_GUIDE.md"): _render_resource("AI_GUIDE.md", project_id),
        Path("patches/PATCHSHUTTLE_PROTOCOL.md"): _render_resource(
            "PATCHSHUTTLE_PROTOCOL.md", project_id
        ),
        Path("patches/patchshuttle.schema.json"): json.dumps(
            Job.model_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        Path("patches/state/registry.json"): json.dumps(
            {"jobs": {}, "project_id": project_id},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        Path("patches/state/run.lock"): "",
        Path("patches/examples/AUDIT-EXAMPLE.psh.yaml"): _render_resource(
            "AUDIT-EXAMPLE.psh.yaml", project_id
        ),
        Path("patches/examples/PATCH-EXAMPLE.psh.yaml"): _render_resource(
            "PATCH-EXAMPLE.psh.yaml", project_id
        ),
    }


def _render_resource(name: str, project_id: str) -> str:
    template = (
        resources.files("patchshuttle.resources")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )
    return template.replace("{{PROJECT_ID}}", project_id)


__all__ = [
    "CONFIG_RELATIVE_PATH",
    "Workspace",
    "WorkspaceInitResult",
    "WorkspaceInitStatus",
    "discover_workspace",
    "init_workspace",
    "load_workspace",
]

"""Typed loading and default rendering for ``patches/patchshuttle.toml``."""

from __future__ import annotations

import json
import stat
from enum import Enum
from os import PathLike
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from patchshuttle.errors import WorkspaceError, WorkspaceErrorCode
from patchshuttle.identifiers import ProjectId

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]

StrictString: TypeAlias = Annotated[str, Field(strict=True, min_length=1)]
PositiveInteger: TypeAlias = Annotated[int, Field(strict=True, ge=1)]

DEFAULT_PROTECTED_PATHS = (
    ".git/**",
    ".env",
    ".env.*",
    "patches/**",
    ".venv/**",
    "venv/**",
    "node_modules/**",
)
DEFAULT_PROTECTED_PATH_EXCEPTIONS = (
    ".env.example",
    ".env.sample",
    ".env.template",
)
DEFAULT_IGNORED_PATHS = (
    ".git/**",
    "patches/backups/**",
    "patches/logs/**",
    "patches/state/**",
    ".venv/**",
    "venv/**",
    "node_modules/**",
    "**/__pycache__/**",
    ".pytest_cache/**",
    ".mypy_cache/**",
    ".ruff_cache/**",
)


class ProjectOrigin(str, Enum):
    """How a workspace was classified during its first initialization."""

    EXISTING = "existing"
    NEW = "new"


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ProjectSettings(_ConfigModel):
    project_id: ProjectId
    origin: ProjectOrigin
    protected_paths: tuple[StrictString, ...] = DEFAULT_PROTECTED_PATHS
    protected_path_exceptions: tuple[StrictString, ...] = (
        DEFAULT_PROTECTED_PATH_EXCEPTIONS
    )
    ignored_paths: tuple[StrictString, ...] = DEFAULT_IGNORED_PATHS


class ExecutionSettings(_ConfigModel):
    confirm: bool = Field(default=True, strict=True)
    auto_rollback: bool = Field(default=True, strict=True)
    allow_keep_changes: bool = Field(default=True, strict=True)
    default_timeout_seconds: PositiveInteger = 300
    max_job_bytes: PositiveInteger = 2_000_000
    max_actions: PositiveInteger = 100
    max_single_file_bytes: PositiveInteger = 1_000_000
    max_command_output_bytes: PositiveInteger = 2_000_000
    max_inventory_entries: PositiveInteger = 50_000
    max_inventory_bytes: PositiveInteger = 1_000_000_000


class FormattingSettings(_ConfigModel):
    enabled: bool = Field(default=True, strict=True)
    order: tuple[Literal["isort"], Literal["black"]] = ("isort", "black")
    scope: Literal["changed_python_files"] = "changed_python_files"
    rerun_checks: bool = Field(default=True, strict=True)


class HtmlLintSettings(_ConfigModel):
    enabled: bool = Field(default=False, strict=True)
    tool: Literal["djlint"] = "djlint"
    profile: Literal[
        "html",
        "django",
        "jinja",
        "nunjucks",
        "handlebars",
        "liquid",
        "golang",
        "angular",
        "tera",
        "askama",
    ] = "html"
    scope: Literal["changed_html_files"] = "changed_html_files"
    ignore: tuple[StrictString, ...] = ()


class LintingSettings(_ConfigModel):
    html: HtmlLintSettings = Field(default_factory=HtmlLintSettings)


class LoggingSettings(_ConfigModel):
    timezone: StrictString = "local"
    include_command_output: bool = Field(default=True, strict=True)
    redact_known_secrets: bool = Field(default=True, strict=True)


class CheckProfileSettings(_ConfigModel):
    argv: Annotated[tuple[StrictString, ...], Field(min_length=1)]
    timeout_seconds: PositiveInteger = 300
    allow_job_args: bool = Field(default=False, strict=True)


class ChecksSettings(_ConfigModel):
    require_at_least_one_for_patch: bool = Field(default=True, strict=True)
    profiles: dict[str, CheckProfileSettings] = Field(default_factory=dict)


class PatchShuttleConfig(_ConfigModel):
    """The complete local policy document used by one workspace."""

    project: ProjectSettings
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    formatting: FormattingSettings = Field(default_factory=FormattingSettings)
    linting: LintingSettings = Field(default_factory=LintingSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    checks: ChecksSettings = Field(default_factory=ChecksSettings)


def load_config(path: str | PathLike[str]) -> PatchShuttleConfig:
    """Load one UTF-8 TOML configuration with a closed typed schema."""

    config_path = Path(path)
    if config_path.is_symlink():
        raise WorkspaceError(
            WorkspaceErrorCode.CONFIG_NOT_REGULAR,
            "configuration path must not be a symbolic link",
        )

    try:
        file_stat = config_path.stat()
    except FileNotFoundError as exc:
        raise WorkspaceError(
            WorkspaceErrorCode.CONFIG_NOT_FOUND,
            "workspace configuration was not found",
        ) from exc
    except OSError as exc:
        raise WorkspaceError(
            WorkspaceErrorCode.CONFIG_READ_FAILED,
            "configuration metadata could not be read",
        ) from exc

    if not stat.S_ISREG(file_stat.st_mode):
        raise WorkspaceError(
            WorkspaceErrorCode.CONFIG_NOT_REGULAR,
            "configuration path must identify a regular file",
        )

    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise WorkspaceError(
            WorkspaceErrorCode.CONFIG_READ_FAILED,
            "configuration file could not be read",
        ) from exc

    try:
        text = raw.decode("utf-8")
        value = tomllib.loads(text)
        return PatchShuttleConfig.model_validate(value)
    except UnicodeDecodeError as exc:
        raise WorkspaceError(
            WorkspaceErrorCode.CONFIG_INVALID,
            "configuration must be valid UTF-8 text",
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise WorkspaceError(
            WorkspaceErrorCode.CONFIG_INVALID,
            "configuration contains invalid TOML syntax",
        ) from exc
    except ValidationError as exc:
        first_error = exc.errors(include_url=False)[0]
        raise WorkspaceError(
            WorkspaceErrorCode.CONFIG_INVALID,
            str(first_error["msg"]),
            path=_format_validation_path(first_error["loc"]),
        ) from exc


def render_default_config(
    project_id: str,
    origin: ProjectOrigin | str,
) -> str:
    """Render the canonical default configuration for a new workspace."""

    project = ProjectSettings(project_id=project_id, origin=origin)
    return (
        "[project]\n"
        f"project_id = {json.dumps(project.project_id)}\n"
        f"origin = {json.dumps(project.origin.value)}\n\n"
        f"protected_paths = {_render_array(project.protected_paths)}\n\n"
        "protected_path_exceptions = "
        f"{_render_array(project.protected_path_exceptions)}\n\n"
        f"ignored_paths = {_render_array(project.ignored_paths)}\n\n"
        "[execution]\n"
        "confirm = true\n"
        "auto_rollback = true\n"
        "allow_keep_changes = true\n"
        "default_timeout_seconds = 300\n"
        "max_job_bytes = 2000000\n"
        "max_actions = 100\n"
        "max_single_file_bytes = 1000000\n"
        "max_command_output_bytes = 2000000\n"
        "max_inventory_entries = 50000\n"
        "max_inventory_bytes = 1000000000\n\n"
        "[formatting]\n"
        "enabled = true\n"
        'order = ["isort", "black"]\n'
        'scope = "changed_python_files"\n'
        "rerun_checks = true\n\n"
        "[linting.html]\n"
        "enabled = false\n"
        'tool = "djlint"\n'
        'profile = "html"\n'
        'scope = "changed_html_files"\n'
        "ignore = []\n\n"
        "[logging]\n"
        'timezone = "local"\n'
        "include_command_output = true\n"
        "redact_known_secrets = true\n\n"
        "[checks]\n"
        "require_at_least_one_for_patch = true\n"
    )


def _render_array(values: tuple[str, ...]) -> str:
    lines = ["["]
    lines.extend(f"  {json.dumps(value)}," for value in values)
    lines.append("]")
    return "\n".join(lines)


def _format_validation_path(location: tuple[int | str, ...]) -> str:
    path = "$"
    for part in location:
        path = f"{path}[{part}]" if isinstance(part, int) else f"{path}.{part}"
    return path


__all__ = [
    "CheckProfileSettings",
    "ChecksSettings",
    "ExecutionSettings",
    "FormattingSettings",
    "HtmlLintSettings",
    "LintingSettings",
    "LoggingSettings",
    "PatchShuttleConfig",
    "ProjectOrigin",
    "ProjectSettings",
    "load_config",
    "render_default_config",
]

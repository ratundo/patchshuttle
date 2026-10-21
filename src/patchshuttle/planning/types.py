"""Shared immutable plan types, separate from planning algorithms."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath

from patchshuttle._architecture import ArchitectureReport, disabled_architecture_report
from patchshuttle.formatter_policy import (
    FormatterDecision,
    FormatterName,
    PlannedFormatterTarget,
)
from patchshuttle.models import Job, JobKind
from patchshuttle.policy import PathKind
from patchshuttle.preflight import PlannedPreflightCheck
from patchshuttle.workspace import Workspace


class ActionDisposition(str, Enum):
    """Read-only conclusion for one requested action."""

    INSPECT = "INSPECT"
    CREATE = "CREATE"
    MODIFY = "MODIFY"
    NO_CHANGE = "NO_CHANGE"


class FileDisposition(str, Enum):
    """Net file operation required by a complete plan."""

    CREATE = "CREATE"
    MODIFY = "MODIFY"
    DELETE = "DELETE"


class NewlineStyle(str, Enum):
    """Newline style preserved or requested for a planned text file."""

    LF = "lf"
    CRLF = "crlf"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class PlannedAction:
    """One sequential action and its dry-run disposition."""

    id: str
    name: str
    disposition: ActionDisposition
    paths: tuple[PurePosixPath, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedCheck:
    """One requested controlled check and its validated workspace paths."""

    id: str
    name: str
    paths: tuple[PurePosixPath, ...] = ()


@dataclass(frozen=True, slots=True)
class PlannedFileChange:
    """The final bytes and fingerprints for one net file change."""

    path: PurePosixPath
    disposition: FileDisposition
    before_sha256: str | None
    after_sha256: str
    before_size: int | None
    after_size: int
    encoding: str
    newline: NewlineStyle
    before_content: bytes | None = field(repr=False)
    content: bytes = field(repr=False)
    mode: int | None = None

    @property
    def deleted(self) -> bool:
        return self.disposition is FileDisposition.DELETE


@dataclass(frozen=True, slots=True)
class PathFingerprint:
    """Read-only metadata retained for future plan revalidation."""

    path: PurePosixPath
    kind: PathKind
    size: int
    modified_ns: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    """A complete immutable read-only plan for one validated job."""

    workspace: Workspace = field(repr=False)
    job: Job
    job_hash: str
    actions: tuple[PlannedAction, ...]
    checks: tuple[PlannedCheck, ...]
    file_changes: tuple[PlannedFileChange, ...]
    directories_to_create: tuple[PurePosixPath, ...]
    formatting_targets: tuple[PurePosixPath, ...]
    formatter_plan: tuple[PlannedFormatterTarget, ...]
    html_lint_targets: tuple[PurePosixPath, ...]
    preflight_checks: tuple[PlannedPreflightCheck, ...]
    fingerprints: tuple[PathFingerprint, ...]
    protected_paths_passed: bool
    backup_destination: PurePosixPath | None
    auto_rollback: bool
    architecture_report: ArchitectureReport = field(
        default_factory=disabled_architecture_report
    )

    @property
    def project_python(self) -> Path | None:
        """Return the effective interpreter for project checks, when used."""

        from patchshuttle.project_python import project_python_for_job

        return project_python_for_job(self.workspace, self.job)

    @property
    def files_to_create(self) -> tuple[PurePosixPath, ...]:
        return tuple(
            change.path
            for change in self.file_changes
            if change.disposition is FileDisposition.CREATE
        )

    @property
    def files_to_modify(self) -> tuple[PurePosixPath, ...]:
        return tuple(
            change.path
            for change in self.file_changes
            if change.disposition is FileDisposition.MODIFY
        )

    @property
    def files_to_delete(self) -> tuple[PurePosixPath, ...]:
        return tuple(change.path for change in self.file_changes if change.deleted)

    @property
    def requires_confirmation(self) -> bool:
        return (
            self.job.kind is not JobKind.AUDIT
            and self.workspace.config.execution.confirm
        )

    def formatter_paths(self, name: FormatterName) -> tuple[PurePosixPath, ...]:
        """Return the immutable ordered RUN scope for one formatter."""

        return tuple(
            item.path
            for item in self.formatter_plan
            if item.formatter == name and item.decision is FormatterDecision.RUN
        )

    @property
    def formatter_run_paths(self) -> tuple[PurePosixPath, ...]:
        """Return each path that at least one formatter may modify."""

        active = {
            item.path
            for item in self.formatter_plan
            if item.decision is FormatterDecision.RUN
        }
        return tuple(path for path in self.formatting_targets if path in active)


@dataclass(frozen=True, slots=True)
class PlanDiff:
    """One bounded unified preview of all resolved planned file changes."""

    text: str = field(repr=False)
    truncated: bool


@dataclass(slots=True)
class _TextState:
    path: PurePosixPath
    original_bytes: bytes | None
    current_bytes: bytes
    text: str
    encoding: str
    codec: str
    bom: bytes
    newline: NewlineStyle

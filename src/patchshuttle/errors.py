"""Stable public errors for loading and validating PatchShuttle jobs."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchshuttle.audit import AuditActionResult
    from patchshuttle.checks import CheckResult
    from patchshuttle.formatters import FormatterResult
    from patchshuttle.inventory import WorkspaceComparison


class JobErrorCode(str, Enum):
    """Stable error codes for protocol input failures."""

    JOB_EXTENSION_INVALID = "JOB_EXTENSION_INVALID"
    JOB_FILE_NOT_FOUND = "JOB_FILE_NOT_FOUND"
    JOB_FILE_NOT_REGULAR = "JOB_FILE_NOT_REGULAR"
    JOB_FILE_READ_FAILED = "JOB_FILE_READ_FAILED"
    JOB_SIZE_LIMIT_EXCEEDED = "JOB_SIZE_LIMIT_EXCEEDED"
    JOB_ENCODING_INVALID = "JOB_ENCODING_INVALID"
    YAML_INVALID = "YAML_INVALID"
    YAML_ANCHOR_FORBIDDEN = "YAML_ANCHOR_FORBIDDEN"
    YAML_ALIAS_FORBIDDEN = "YAML_ALIAS_FORBIDDEN"
    YAML_TAG_FORBIDDEN = "YAML_TAG_FORBIDDEN"
    YAML_DUPLICATE_KEY = "YAML_DUPLICATE_KEY"
    YAML_MAPPING_KEY_INVALID = "YAML_MAPPING_KEY_INVALID"
    JOB_ROOT_INVALID = "JOB_ROOT_INVALID"
    JOB_SCHEMA_INVALID = "JOB_SCHEMA_INVALID"


class WorkspaceErrorCode(str, Enum):
    """Stable error codes for workspace and configuration failures."""

    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    WORKSPACE_NOT_DIRECTORY = "WORKSPACE_NOT_DIRECTORY"
    WORKSPACE_NOT_INITIALIZED = "WORKSPACE_NOT_INITIALIZED"
    WORKSPACE_READ_FAILED = "WORKSPACE_READ_FAILED"
    WORKSPACE_WRITE_FAILED = "WORKSPACE_WRITE_FAILED"
    NEW_PROJECT_NOT_EMPTY = "NEW_PROJECT_NOT_EMPTY"
    MANAGED_PATH_CONFLICT = "MANAGED_PATH_CONFLICT"
    CONFIG_NOT_FOUND = "CONFIG_NOT_FOUND"
    CONFIG_NOT_REGULAR = "CONFIG_NOT_REGULAR"
    CONFIG_READ_FAILED = "CONFIG_READ_FAILED"
    CONFIG_INVALID = "CONFIG_INVALID"
    PROJECT_ORIGIN_CONFLICT = "PROJECT_ORIGIN_CONFLICT"
    PROJECT_ID_MISMATCH = "PROJECT_ID_MISMATCH"


class PolicyErrorCode(str, Enum):
    """Stable error codes for workspace path and local-policy failures."""

    PATH_INVALID = "PATH_INVALID"
    PATH_ABSOLUTE = "PATH_ABSOLUTE"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    PATH_URL = "PATH_URL"
    PATH_ROOT_FORBIDDEN = "PATH_ROOT_FORBIDDEN"
    PATH_PROTECTED = "PATH_PROTECTED"
    PATH_SYMLINK = "PATH_SYMLINK"
    PATH_NOT_FOUND = "PATH_NOT_FOUND"
    PATH_PARENT_NOT_DIRECTORY = "PATH_PARENT_NOT_DIRECTORY"
    PATH_SPECIAL_FILE = "PATH_SPECIAL_FILE"
    PATH_INSPECTION_FAILED = "PATH_INSPECTION_FAILED"
    PATH_OUTSIDE_WORKSPACE = "PATH_OUTSIDE_WORKSPACE"
    POLICY_PATTERN_INVALID = "POLICY_PATTERN_INVALID"


class PlanningErrorCode(str, Enum):
    """Stable error codes for read-only planning failures."""

    ACTION_LIMIT_EXCEEDED = "ACTION_LIMIT_EXCEEDED"
    PATCH_CHECK_REQUIRED = "PATCH_CHECK_REQUIRED"
    PATH_IGNORED = "PATH_IGNORED"
    TARGET_TYPE_INVALID = "TARGET_TYPE_INVALID"
    FILE_SIZE_LIMIT_EXCEEDED = "FILE_SIZE_LIMIT_EXCEEDED"
    FILE_BINARY = "FILE_BINARY"
    FILE_ENCODING_UNSUPPORTED = "FILE_ENCODING_UNSUPPORTED"
    FILE_NEWLINE_UNSUPPORTED = "FILE_NEWLINE_UNSUPPORTED"
    FILE_READ_FAILED = "FILE_READ_FAILED"
    CONTENT_ENCODING_INVALID = "CONTENT_ENCODING_INVALID"
    CONTENT_BINARY_FORBIDDEN = "CONTENT_BINARY_FORBIDDEN"
    CREATE_FILE_CONFLICT = "CREATE_FILE_CONFLICT"
    OCCURRENCE_COUNT_MISMATCH = "OCCURRENCE_COUNT_MISMATCH"
    INSERTION_STATE_CONFLICT = "INSERTION_STATE_CONFLICT"
    VIRTUAL_PATH_CONFLICT = "VIRTUAL_PATH_CONFLICT"
    DIFF_INVALID = "DIFF_INVALID"
    DIFF_PATH_INVALID = "DIFF_PATH_INVALID"
    DIFF_BINARY_FORBIDDEN = "DIFF_BINARY_FORBIDDEN"
    DIFF_HUNK_MISMATCH = "DIFF_HUNK_MISMATCH"
    CHECK_PROFILE_NOT_FOUND = "CHECK_PROFILE_NOT_FOUND"
    CHECK_ARGUMENT_INVALID = "CHECK_ARGUMENT_INVALID"
    DEPENDENCY_NOT_AVAILABLE = "DEPENDENCY_NOT_AVAILABLE"
    PYTEST_ARGUMENT_FORBIDDEN = "PYTEST_ARGUMENT_FORBIDDEN"
    CHECK_PATH_NOT_FOUND = "CHECK_PATH_NOT_FOUND"


class ExecutionErrorCode(str, Enum):
    """Stable errors for the internal transactional execution core."""

    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    USER_DECLINED = "USER_DECLINED"
    KEEP_CHANGES_FORBIDDEN = "KEEP_CHANGES_FORBIDDEN"
    JOB_KIND_UNSUPPORTED = "JOB_KIND_UNSUPPORTED"
    ACTION_UNSUPPORTED = "ACTION_UNSUPPORTED"
    PATCH_ID_CONFLICT = "PATCH_ID_CONFLICT"
    WORKSPACE_LOCKED = "WORKSPACE_LOCKED"
    WORKSPACE_LOCK_FAILED = "WORKSPACE_LOCK_FAILED"
    PLAN_STALE = "PLAN_STALE"
    BACKUP_FAILED = "BACKUP_FAILED"
    ACTION_FAILED = "ACTION_FAILED"
    CHECK_FAILED = "CHECK_FAILED"
    FORMAT_FAILED = "FORMAT_FAILED"
    WORKSPACE_INVENTORY_FAILED = "WORKSPACE_INVENTORY_FAILED"
    UNEXPECTED_WORKSPACE_CHANGE = "UNEXPECTED_WORKSPACE_CHANGE"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    OPERATIONAL_RECORD_FAILED = "OPERATIONAL_RECORD_FAILED"
    LOG_NOT_FOUND = "LOG_NOT_FOUND"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"


class JobError(ValueError):
    """A user-correctable job input error with a stable code and location."""

    def __init__(
        self,
        code: JobErrorCode,
        message: str,
        *,
        field_path: str | None = None,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.field_path = field_path
        self.line = line
        self.column = column
        super().__init__(message)

    def __str__(self) -> str:
        location = self.field_path or ""
        if self.line is not None and self.column is not None:
            position = f"line {self.line}, column {self.column}"
            location = f"{location} ({position})" if location else position
        prefix = f"[{self.code.value}]"
        return (
            f"{prefix} {location}: {self.message}"
            if location
            else f"{prefix} {self.message}"
        )


class WorkspaceError(ValueError):
    """A user-correctable workspace failure with a stable code and location."""

    def __init__(
        self,
        code: WorkspaceErrorCode,
        message: str,
        *,
        path: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(message)

    def __str__(self) -> str:
        prefix = f"[{self.code.value}]"
        return (
            f"{prefix} {self.path}: {self.message}"
            if self.path
            else f"{prefix} {self.message}"
        )


class PolicyError(ValueError):
    """A user-correctable path-policy failure with a stable code."""

    def __init__(
        self,
        code: PolicyErrorCode,
        message: str,
        *,
        path: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(message)

    def __str__(self) -> str:
        prefix = f"[{self.code.value}]"
        return (
            f"{prefix} {self.path}: {self.message}"
            if self.path
            else f"{prefix} {self.message}"
        )


class PlanningError(ValueError):
    """A user-correctable planning failure with a stable location."""

    def __init__(
        self,
        code: PlanningErrorCode,
        message: str,
        *,
        item_id: str | None = None,
        path: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.item_id = item_id
        self.path = path
        super().__init__(message)

    def __str__(self) -> str:
        location = " ".join(value for value in (self.item_id, self.path) if value)
        prefix = f"[{self.code.value}]"
        return (
            f"{prefix} {location}: {self.message}"
            if location
            else f"{prefix} {self.message}"
        )


class ExecutionError(RuntimeError):
    """A transactional failure with rollback and backup context."""

    def __init__(
        self,
        code: ExecutionErrorCode,
        message: str,
        *,
        item_id: str | None = None,
        path: str | None = None,
        backup_path: Path | None = None,
        rollback_succeeded: bool | None = None,
        rollback_skipped: bool = False,
        changes_kept: bool = False,
        cause_code: ExecutionErrorCode | None = None,
        check_results: tuple[CheckResult, ...] = (),
        audit_results: tuple[AuditActionResult, ...] = (),
        formatting_results: tuple[FormatterResult, ...] = (),
        workspace_comparison: WorkspaceComparison | None = None,
        log_path: Path | None = None,
        archived_job_path: Path | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.item_id = item_id
        self.path = path
        self.backup_path = backup_path
        self.rollback_succeeded = rollback_succeeded
        self.rollback_skipped = rollback_skipped
        self.changes_kept = changes_kept
        self.cause_code = cause_code
        self.check_results = check_results
        self.audit_results = audit_results
        self.formatting_results = formatting_results
        self.workspace_comparison = workspace_comparison
        self.log_path = log_path
        self.archived_job_path = archived_job_path
        super().__init__(message)

    def __str__(self) -> str:
        location = " ".join(value for value in (self.item_id, self.path) if value)
        prefix = f"[{self.code.value}]"
        return (
            f"{prefix} {location}: {self.message}"
            if location
            else f"{prefix} {self.message}"
        )


__all__ = [
    "ExecutionError",
    "ExecutionErrorCode",
    "JobError",
    "JobErrorCode",
    "PolicyError",
    "PolicyErrorCode",
    "PlanningError",
    "PlanningErrorCode",
    "WorkspaceError",
    "WorkspaceErrorCode",
]

"""Typed schema for compact PatchShuttle history records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

HISTORY_SCHEMA = "patchshuttle.history.v1"
HISTORY_SCHEMA_VERSION = 1


class _HistoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HistoryIntent(_HistoryModel):
    source: Literal["job.description"] = "job.description"
    text: str
    truncated: bool


class HistorySymbolTarget(_HistoryModel):
    action_id: str
    path: str
    symbol: str


class HistoryDeclared(_HistoryModel):
    title: str | None
    title_truncated: bool
    intent: HistoryIntent | None
    planned_actions: int
    planned_checks: int
    files_to_create: tuple[str, ...]
    files_to_modify: tuple[str, ...]
    symbol_targets: tuple[HistorySymbolTarget, ...]


class HistoryFileChange(_HistoryModel):
    path: str
    kind: str
    expected: bool


class HistoryFiles(_HistoryModel):
    affected: tuple[str, ...]
    created: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]
    changes: tuple[HistoryFileChange, ...]


class HistoryCheck(_HistoryModel):
    phase: Literal["initial", "final"]
    check_id: str
    profile: str
    status: str
    exit_code: int
    duration_ms: int
    warning_analysis: str | None
    known_warnings: int | None
    new_warnings: int | None
    new_warning_details: tuple[str, ...]
    warning_details_truncated: bool


class HistoryWarning(_HistoryModel):
    source: Literal["check"] = "check"
    code: Literal["NEW_CHECK_WARNINGS"] = "NEW_CHECK_WARNINGS"
    check_id: str
    count: int
    details: tuple[str, ...]
    details_truncated: bool


class HistoryFailure(_HistoryModel):
    stage: str | None
    recorded_code: str | None
    terminal_code: str
    cause_code: str
    item_id: str | None
    path: str | None
    message: str
    message_truncated: bool


class HistoryRollback(_HistoryModel):
    status: str
    cause: str | None
    backup: str | None
    restored_files: tuple[str, ...] = ()
    removed_files: tuple[str, ...] = ()
    removed_directories: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()


class HistoryObserved(_HistoryModel):
    status: str
    summary: str
    exit_code: int
    files: HistoryFiles
    affected_symbols: tuple[HistorySymbolTarget, ...]
    checks: tuple[HistoryCheck, ...]
    failure: HistoryFailure | None
    rollback: HistoryRollback
    warnings: tuple[HistoryWarning, ...]


class HistoryJob(_HistoryModel):
    id: str
    hash: str
    kind: str


class HistoryRedaction(_HistoryModel):
    status: Literal["BEST_EFFORT_ENABLED", "DISABLED_BY_LOCAL_POLICY"]
    guarantee: Literal["NONE"] = "NONE"


class HistoryAiLogReference(_HistoryModel):
    kind: Literal["derived_view"] = "derived_view"
    persistent: Literal[False] = False
    source_log: str


class HistoryReferences(_HistoryModel):
    detailed_log: str
    ai_log: HistoryAiLogReference
    archived_job: str
    backup: str | None


class HistoryRecord(_HistoryModel):
    schema_name: Literal["patchshuttle.history.v1"] = Field(
        default=HISTORY_SCHEMA,
        alias="schema",
    )
    schema_version: Literal[1] = HISTORY_SCHEMA_VERSION
    record_id: str
    occurred_at: str
    patchshuttle_version: str
    project_id: str
    job: HistoryJob
    redaction: HistoryRedaction
    declared: HistoryDeclared
    observed: HistoryObserved
    references: HistoryReferences
    relationships: None = None


class HistoryErrorCode(str, Enum):
    HISTORY_NOT_FOUND = "HISTORY_NOT_FOUND"
    HISTORY_INVALID = "HISTORY_INVALID"
    HISTORY_READ_FAILED = "HISTORY_READ_FAILED"
    HISTORY_WRITE_FAILED = "HISTORY_WRITE_FAILED"
    HISTORY_LIMIT_EXCEEDED = "HISTORY_LIMIT_EXCEEDED"


class HistoryError(ValueError):
    """A stable read or direct-write history error."""

    def __init__(
        self,
        code: HistoryErrorCode,
        message: str,
        *,
        path: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(message)

    def __str__(self) -> str:
        location = f" {self.path}:" if self.path is not None else ""
        return f"[{self.code.value}]{location} {self.message}"


@dataclass(frozen=True, slots=True)
class HistoryWriteResult:
    """Best-effort persistence result that never changes the job outcome."""

    path: Path | None
    warning: str | None


@dataclass(frozen=True, slots=True)
class HistoryListResult:
    records: tuple[HistoryRecord, ...]
    limited: bool

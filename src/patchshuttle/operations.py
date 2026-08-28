"""Project-level manual recovery operations."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from patchshuttle.backup import (
    BackupStatus,
    load_completed_backup,
    update_loaded_backup,
)
from patchshuttle.errors import ExecutionError, ExecutionErrorCode, JobError
from patchshuttle.history import try_write_history_record
from patchshuttle.logging import (
    ManualRollbackLogRecord,
    RunLogData,
    current_run_clock,
    write_run_log,
)
from patchshuttle.models import Job, JobKind
from patchshuttle.parser import load_job
from patchshuttle.planner import normalized_job_hash
from patchshuttle.registry import get_job, load_registry, update_registry
from patchshuttle.rollback import RollbackResult, rollback_completed_backup
from patchshuttle.runner import acquire_workspace_lock
from patchshuttle.workspace import Workspace


@dataclass(frozen=True, slots=True)
class ManualRollbackResult:
    """A verified successful user-requested rollback."""

    job_id: str
    job_hash: str
    backup_path: Path
    restored_files: tuple[PurePosixPath, ...]
    removed_files: tuple[PurePosixPath, ...]
    removed_directories: tuple[PurePosixPath, ...]
    log_path: Path
    history_path: Path | None = None
    history_warning: str | None = None


def rollback_job(
    workspace: Workspace,
    job_id: str,
    *,
    approved: bool = False,
) -> ManualRollbackResult:
    """Safely restore one completed patch from its retained manifest."""

    if not approved:
        raise ExecutionError(
            ExecutionErrorCode.APPROVAL_REQUIRED,
            "explicit approval is required before a completed job is rolled back",
            item_id=job_id,
        )
    clock = current_run_clock(workspace)
    with acquire_workspace_lock(workspace):
        registry = load_registry(workspace)
        record = get_job(registry, job_id)
        if record.kind != JobKind.PATCH.value:
            raise ExecutionError(
                ExecutionErrorCode.ROLLBACK_FAILED,
                "only a completed patch job can be rolled back",
                item_id=job_id,
                rollback_succeeded=False,
            )
        if not record.completed or record.backup_reference is None:
            raise ExecutionError(
                ExecutionErrorCode.ROLLBACK_FAILED,
                "job does not have a completed backup available for manual rollback",
                item_id=job_id,
                rollback_succeeded=False,
            )
        job, archived = _find_archived_job(
            workspace,
            job_id=job_id,
            job_hash=record.job_hash,
            preferred=record.archived_job_copy,
        )
        backup = load_completed_backup(
            workspace,
            record.backup_reference,
            job_id=job_id,
            job_hash=record.job_hash,
        )
        try:
            rollback = rollback_completed_backup(workspace, backup)
            if not rollback.success:
                update_loaded_backup(
                    backup,
                    BackupStatus.ROLLBACK_FAILED,
                    failure_code=ExecutionErrorCode.ROLLBACK_FAILED,
                )
                error = ExecutionError(
                    ExecutionErrorCode.ROLLBACK_FAILED,
                    "manual rollback left one or more transaction paths unresolved",
                    item_id=job_id,
                    path=rollback.unresolved[0].as_posix(),
                    backup_path=backup.path,
                    rollback_succeeded=False,
                )
                _record_failed_rollback(
                    workspace,
                    registry,
                    job,
                    record.job_hash,
                    archived,
                    clock,
                    backup.path,
                    rollback,
                    error,
                )
                raise error
        except ExecutionError as error:
            if error.log_path is None:
                _record_failed_rollback(
                    workspace,
                    registry,
                    job,
                    record.job_hash,
                    archived,
                    clock,
                    backup.path,
                    RollbackResult((), (), ()),
                    error,
                )
            raise

        update_loaded_backup(backup, BackupStatus.ROLLED_BACK)
        log_record = ManualRollbackLogRecord(
            status="SUCCESS",
            backup_path=backup.path,
            restored_files=rollback.restored_files,
            removed_files=rollback.removed_files,
            removed_directories=rollback.removed_directories,
        )
        data = RunLogData(
            workspace=workspace,
            job=job,
            job_hash=record.job_hash,
            clock=clock,
            result="ROLLED_BACK",
            exit_code=0,
            failure_stage=None,
            failure_code=None,
            archived_job_path=archived,
            manual_rollback=log_record,
        )
        log_path = write_run_log(data)
        update_registry(
            workspace,
            registry,
            job_id=job_id,
            job_hash=record.job_hash,
            kind=JobKind.PATCH,
            occurred_at=clock.iso_timestamp,
            result="ROLLED_BACK",
            backup_path=backup.path,
            rollback_state="SUCCESS",
            archived_job_path=archived,
            completed=False,
            reset_completed=True,
        )
        history = try_write_history_record(data, log_path=log_path)
        return ManualRollbackResult(
            job_id=job_id,
            job_hash=record.job_hash,
            backup_path=backup.path,
            restored_files=rollback.restored_files,
            removed_files=rollback.removed_files,
            removed_directories=rollback.removed_directories,
            log_path=log_path,
            history_path=history.path,
            history_warning=history.warning,
        )


def _record_failed_rollback(
    workspace: Workspace,
    registry,
    job: Job,
    job_hash: str,
    archived: Path,
    clock,
    backup_path: Path,
    rollback: RollbackResult,
    error: ExecutionError,
) -> None:
    data = RunLogData(
        workspace=workspace,
        job=job,
        job_hash=job_hash,
        clock=clock,
        result="ROLLBACK_FAILED",
        exit_code=8,
        failure_stage="ROLLBACK",
        failure_code=(error.cause_code or error.code).value,
        archived_job_path=archived,
        error=error,
        manual_rollback=ManualRollbackLogRecord(
            status="FAILED",
            backup_path=backup_path,
            restored_files=rollback.restored_files,
            removed_files=rollback.removed_files,
            removed_directories=rollback.removed_directories,
            unresolved=rollback.unresolved,
        ),
    )
    log_path = write_run_log(data)
    update_registry(
        workspace,
        registry,
        job_id=job.id,
        job_hash=job_hash,
        kind=JobKind.PATCH,
        occurred_at=clock.iso_timestamp,
        result="ROLLBACK_FAILED",
        backup_path=backup_path,
        rollback_state="FAILED",
        archived_job_path=archived,
        completed=False,
    )
    history = try_write_history_record(data, log_path=log_path)
    error.log_path = log_path
    error.history_path = history.path
    error.history_warning = history.warning


def _find_archived_job(
    workspace: Workspace,
    *,
    job_id: str,
    job_hash: str,
    preferred: str,
) -> tuple[Job, Path]:
    candidates: list[Path] = []
    preferred_path = _safe_archive_path(workspace, preferred)
    if preferred_path is not None:
        candidates.append(preferred_path)
    for directory_name in ("applied", "failed"):
        directory = workspace.patches_dir / directory_name
        try:
            paths = sorted(
                directory.glob(f"{job_id}_*_{job_hash[:8]}.psh.yaml"),
                reverse=True,
            )
        except OSError:
            paths = []
        candidates.extend(path for path in paths if path not in candidates)
    for path in candidates:
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
                continue
            job = load_job(
                path,
                max_bytes=workspace.config.execution.max_job_bytes,
            )
        except (OSError, JobError, ValueError):
            continue
        if (
            job.id == job_id
            and job.kind is JobKind.PATCH
            and normalized_job_hash(job) == job_hash
        ):
            return job, path
    raise ExecutionError(
        ExecutionErrorCode.ROLLBACK_FAILED,
        "an intact archived copy of the completed job was not found",
        item_id=job_id,
        rollback_succeeded=False,
    )


def _safe_archive_path(workspace: Workspace, value: str) -> Path | None:
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or "\\" in value
        or len(relative.parts) != 3
        or relative.parts[0] != "patches"
        or relative.parts[1] not in {"applied", "failed"}
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return None
    path = workspace.root.joinpath(*relative.parts)
    try:
        if path.resolve() != path.absolute():
            return None
    except OSError:
        return None
    return path


__all__ = ["ManualRollbackResult", "rollback_job"]

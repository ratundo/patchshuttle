"""Public approved execution with registry, archive, and log coordination."""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from enum import Enum
from os import PathLike
from pathlib import Path, PurePosixPath

import yaml

from patchshuttle.audit import (
    AuditActionResult,
    AuditRunResult,
    execute_audit_locked,
)
from patchshuttle.checks import CheckResult
from patchshuttle.errors import ExecutionError, ExecutionErrorCode, JobError
from patchshuttle.formatters import FormattedFileState, FormatterResult
from patchshuttle.inventory import WorkspaceComparison
from patchshuttle.linters import HtmlLintResult
from patchshuttle.logging import (
    RunClock,
    RunLogData,
    archive_job_source,
    current_run_clock,
    write_run_log,
)
from patchshuttle.models import Job, JobKind
from patchshuttle.parser import load_job
from patchshuttle.planner import Plan, normalized_job_hash
from patchshuttle.registry import (
    Registry,
    RegistryDecision,
    decide_job,
    load_registry,
    update_registry,
)
from patchshuttle.runner import (
    TransactionResult,
    TransactionStatus,
    acquire_workspace_lock,
    execute_change_transaction_locked,
)
from patchshuttle.verification import (
    VerificationRunResult,
    execute_verification_locked,
)
from patchshuttle.workspace import Workspace


class RunStatus(str, Enum):
    """Successful outcomes exposed by the public execution API."""

    COMPLETED = "COMPLETED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    NO_CHANGE = "NO_CHANGE"


@dataclass(frozen=True, slots=True)
class RunResult:
    """Immutable public result of one recorded job execution."""

    status: RunStatus
    plan: Plan = field(repr=False)
    backup_path: Path | None
    created_files: tuple[PurePosixPath, ...]
    created_directories: tuple[PurePosixPath, ...]
    modified_files: tuple[PurePosixPath, ...] = ()
    html_lint_results: tuple[HtmlLintResult, ...] = ()
    initial_checks: tuple[CheckResult, ...] = ()
    formatting_results: tuple[FormatterResult, ...] = ()
    formatted_files: tuple[FormattedFileState, ...] = ()
    final_checks: tuple[CheckResult, ...] = ()
    workspace_comparison: WorkspaceComparison | None = None
    audit_results: tuple[AuditActionResult, ...] = ()
    log_path: Path | None = None
    archived_job_path: Path | None = None


@dataclass(frozen=True, slots=True)
class RegisteredRunResult:
    """An already-applied result resolved before planning is needed."""

    status: RunStatus
    job: Job
    job_hash: str
    log_path: Path
    archived_job_path: Path


@dataclass(frozen=True, slots=True)
class _Artifacts:
    log_path: Path
    archived_job_path: Path


def execute_plan(
    plan: Plan,
    *,
    approved: bool = False,
    keep_changes: bool = False,
    source_path: str | PathLike[str] | None = None,
) -> RunResult:
    """Execute and record an audit, patch, or approved verify plan.

    A CLI-provided ``source_path`` is archived byte-for-byte. Python callers
    without a source file receive a deterministic YAML archive of the immutable
    job model. ``keep_changes`` is patch-only, remains subject to local policy,
    and records a skipped rollback explicitly if execution fails.
    """

    if keep_changes and plan.job.kind is not JobKind.PATCH:
        raise ExecutionError(
            ExecutionErrorCode.JOB_KIND_UNSUPPORTED,
            "keeping failed-job changes is supported only for patch jobs",
        )
    if keep_changes and not plan.workspace.config.execution.allow_keep_changes:
        raise ExecutionError(
            ExecutionErrorCode.KEEP_CHANGES_FORBIDDEN,
            "local workspace policy does not allow keeping failed-job changes",
        )
    if plan.requires_confirmation and not approved:
        raise ExecutionError(
            ExecutionErrorCode.APPROVAL_REQUIRED,
            "explicit approval is required before project code is executed",
        )

    source = _job_source_bytes(
        plan.workspace,
        plan.job,
        source_path=source_path,
    )
    clock = current_run_clock(plan.workspace)
    with acquire_workspace_lock(plan.workspace):
        registry = load_registry(plan.workspace)
        decision = decide_job(
            registry,
            job_id=plan.job.id,
            job_hash=plan.job_hash,
        )
        if decision is RegistryDecision.PATCH_ID_CONFLICT:
            error = _conflict_error(plan.job.id)
            artifacts = _record_error(
                plan.workspace,
                registry,
                plan.job,
                plan.job_hash,
                source,
                clock,
                error,
                plan=plan,
            )
            _attach_artifacts(error, artifacts)
            raise error
        if decision is RegistryDecision.ALREADY_APPLIED:
            artifacts = _record_success(
                plan.workspace,
                registry,
                plan.job,
                plan.job_hash,
                source,
                clock,
                result=RunStatus.ALREADY_APPLIED,
                plan=plan,
                transaction=None,
            )
            return _already_applied_result(plan, artifacts)

        transaction: TransactionResult | None = None
        audit_run: AuditRunResult | None = None
        verification: VerificationRunResult | None = None
        try:
            if plan.job.kind is JobKind.AUDIT:
                audit_run = execute_audit_locked(plan)
                status = RunStatus.COMPLETED
            elif plan.job.kind is JobKind.VERIFY:
                verification = execute_verification_locked(plan)
                status = RunStatus.COMPLETED
            else:
                transaction = execute_change_transaction_locked(
                    plan,
                    approved=True,
                    keep_changes=keep_changes,
                )
                status = {
                    TransactionStatus.APPLIED: RunStatus.COMPLETED,
                    TransactionStatus.NO_CHANGE: RunStatus.NO_CHANGE,
                }[transaction.status]
        except ExecutionError as error:
            artifacts = _record_error(
                plan.workspace,
                registry,
                plan.job,
                plan.job_hash,
                source,
                clock,
                error,
                plan=plan,
            )
            _attach_artifacts(error, artifacts)
            raise

        artifacts = _record_success(
            plan.workspace,
            registry,
            plan.job,
            plan.job_hash,
            source,
            clock,
            result=status,
            plan=plan,
            transaction=transaction,
            audit_run=audit_run,
            verification=verification,
        )
        return _public_result(
            plan,
            status,
            artifacts,
            transaction=transaction,
            audit_run=audit_run,
            verification=verification,
        )


def resolve_registered_job(
    workspace: Workspace,
    job: Job,
    *,
    source_path: str | PathLike[str] | None = None,
) -> RegisteredRunResult | None:
    """Resolve completed or conflicting IDs before mutable planning.

    ``None`` means the ID/hash pair may proceed to normal planning. The final
    decision is repeated by ``execute_plan`` under its transaction lock.
    """

    job_hash = normalized_job_hash(job)
    source = _job_source_bytes(workspace, job, source_path=source_path)
    clock = current_run_clock(workspace)
    with acquire_workspace_lock(workspace):
        registry = load_registry(workspace)
        decision = decide_job(registry, job_id=job.id, job_hash=job_hash)
        if decision is RegistryDecision.PROCEED:
            return None
        if decision is RegistryDecision.PATCH_ID_CONFLICT:
            error = _conflict_error(job.id)
            artifacts = _record_error(
                workspace,
                registry,
                job,
                job_hash,
                source,
                clock,
                error,
                plan=None,
            )
            _attach_artifacts(error, artifacts)
            raise error

        artifacts = _record_success(
            workspace,
            registry,
            job,
            job_hash,
            source,
            clock,
            result=RunStatus.ALREADY_APPLIED,
            plan=None,
            transaction=None,
        )
        return RegisteredRunResult(
            status=RunStatus.ALREADY_APPLIED,
            job=job,
            job_hash=job_hash,
            log_path=artifacts.log_path,
            archived_job_path=artifacts.archived_job_path,
        )


def record_declined_plan(
    plan: Plan,
    *,
    source_path: str | PathLike[str] | None = None,
) -> ExecutionError:
    """Record a reviewed patch or verify plan the user explicitly declined."""

    source = _job_source_bytes(
        plan.workspace,
        plan.job,
        source_path=source_path,
    )
    clock = current_run_clock(plan.workspace)
    with acquire_workspace_lock(plan.workspace):
        registry = load_registry(plan.workspace)
        decision = decide_job(
            registry,
            job_id=plan.job.id,
            job_hash=plan.job_hash,
        )
        error = (
            _conflict_error(plan.job.id)
            if decision is RegistryDecision.PATCH_ID_CONFLICT
            else ExecutionError(
                ExecutionErrorCode.USER_DECLINED,
                "user declined the reviewed job plan",
            )
        )
        artifacts = _record_error(
            plan.workspace,
            registry,
            plan.job,
            plan.job_hash,
            source,
            clock,
            error,
            plan=plan,
        )
        _attach_artifacts(error, artifacts)
        return error


def execution_exit_code(code: ExecutionErrorCode) -> int:
    """Map execution failures to the protocol's stable process groups."""

    if code is ExecutionErrorCode.OPERATIONAL_RECORD_FAILED:
        return 1
    if code in {
        ExecutionErrorCode.APPROVAL_REQUIRED,
        ExecutionErrorCode.USER_DECLINED,
        ExecutionErrorCode.KEEP_CHANGES_FORBIDDEN,
    }:
        return 4
    if code in {
        ExecutionErrorCode.PATCH_ID_CONFLICT,
        ExecutionErrorCode.WORKSPACE_LOCKED,
        ExecutionErrorCode.WORKSPACE_LOCK_FAILED,
        ExecutionErrorCode.LOG_NOT_FOUND,
        ExecutionErrorCode.JOB_NOT_FOUND,
    }:
        return 3
    if code in {
        ExecutionErrorCode.CHECK_FAILED,
        ExecutionErrorCode.HTML_LINT_FAILED,
    }:
        return 6
    if code is ExecutionErrorCode.FORMAT_FAILED:
        return 7
    if code is ExecutionErrorCode.ROLLBACK_FAILED:
        return 8
    return 5


def _record_success(
    workspace: Workspace,
    registry: Registry,
    job: Job,
    job_hash: str,
    source: bytes,
    clock: RunClock,
    *,
    result: RunStatus,
    plan: Plan | None,
    transaction: TransactionResult | None,
    audit_run: AuditRunResult | None = None,
    verification: VerificationRunResult | None = None,
) -> _Artifacts:
    archived = archive_job_source(
        workspace,
        job=job,
        job_hash=job_hash,
        clock=clock,
        source=source,
        successful=True,
    )
    log_path = write_run_log(
        RunLogData(
            workspace=workspace,
            job=job,
            job_hash=job_hash,
            clock=clock,
            result=result.value,
            exit_code=0,
            failure_stage=None,
            failure_code=None,
            archived_job_path=archived,
            plan=plan,
            transaction=transaction,
            audit_results=(audit_run.results if audit_run is not None else ()),
            verification_checks=(
                verification.checks if verification is not None else ()
            ),
            workspace_comparison=(
                verification.workspace_comparison
                if verification is not None
                else (audit_run.workspace_comparison if audit_run is not None else None)
            ),
        )
    )
    update_registry(
        workspace,
        registry,
        job_id=job.id,
        job_hash=job_hash,
        kind=job.kind,
        occurred_at=clock.iso_timestamp,
        result=result.value,
        backup_path=(transaction.backup_path if transaction is not None else None),
        rollback_state="NOT_REQUIRED",
        archived_job_path=archived,
        completed=True,
    )
    return _Artifacts(log_path=log_path, archived_job_path=archived)


def _record_error(
    workspace: Workspace,
    registry: Registry,
    job: Job,
    job_hash: str,
    source: bytes,
    clock: RunClock,
    error: ExecutionError,
    *,
    plan: Plan | None,
) -> _Artifacts:
    result, failure_code = _error_result(error)
    archived = archive_job_source(
        workspace,
        job=job,
        job_hash=job_hash,
        clock=clock,
        source=source,
        successful=False,
    )
    log_path = write_run_log(
        RunLogData(
            workspace=workspace,
            job=job,
            job_hash=job_hash,
            clock=clock,
            result=result,
            exit_code=execution_exit_code(error.code),
            failure_stage=_failure_stage(error, plan),
            failure_code=failure_code,
            archived_job_path=archived,
            plan=plan,
            error=error,
        )
    )
    rollback = _rollback_state(error)
    update_registry(
        workspace,
        registry,
        job_id=job.id,
        job_hash=job_hash,
        kind=job.kind,
        occurred_at=clock.iso_timestamp,
        result=result,
        backup_path=error.backup_path,
        rollback_state=rollback,
        archived_job_path=archived,
        completed=False,
    )
    return _Artifacts(log_path=log_path, archived_job_path=archived)


def _public_result(
    plan: Plan,
    status: RunStatus,
    artifacts: _Artifacts,
    *,
    transaction: TransactionResult | None,
    audit_run: AuditRunResult | None,
    verification: VerificationRunResult | None,
) -> RunResult:
    return RunResult(
        status=status,
        plan=plan,
        backup_path=(transaction.backup_path if transaction is not None else None),
        created_files=(transaction.created_files if transaction is not None else ()),
        created_directories=(
            transaction.created_directories if transaction is not None else ()
        ),
        modified_files=(transaction.modified_files if transaction is not None else ()),
        html_lint_results=(
            transaction.html_lint_results if transaction is not None else ()
        ),
        initial_checks=(
            transaction.initial_checks
            if transaction is not None
            else verification.checks if verification is not None else ()
        ),
        formatting_results=(
            transaction.formatting_results if transaction is not None else ()
        ),
        formatted_files=(
            transaction.formatted_files if transaction is not None else ()
        ),
        final_checks=(transaction.final_checks if transaction is not None else ()),
        workspace_comparison=(
            transaction.workspace_comparison
            if transaction is not None
            else (
                verification.workspace_comparison
                if verification is not None
                else (audit_run.workspace_comparison if audit_run is not None else None)
            )
        ),
        audit_results=(audit_run.results if audit_run is not None else ()),
        log_path=artifacts.log_path,
        archived_job_path=artifacts.archived_job_path,
    )


def _already_applied_result(plan: Plan, artifacts: _Artifacts) -> RunResult:
    return RunResult(
        status=RunStatus.ALREADY_APPLIED,
        plan=plan,
        backup_path=None,
        created_files=(),
        created_directories=(),
        log_path=artifacts.log_path,
        archived_job_path=artifacts.archived_job_path,
    )


def _job_source_bytes(
    workspace: Workspace,
    job: Job,
    *,
    source_path: str | PathLike[str] | None,
) -> bytes:
    if source_path is None:
        payload = job.model_dump(mode="json", exclude_none=True)
        return yaml.safe_dump(
            payload,
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")

    path = Path(source_path)
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("source job is not a regular file")
        raw = path.read_bytes()
    except OSError as exc:
        raise ExecutionError(
            ExecutionErrorCode.PLAN_STALE,
            "source job file is missing, unsafe, or unreadable",
            path=path.as_posix(),
        ) from exc
    if len(raw) > workspace.config.execution.max_job_bytes:
        raise ExecutionError(
            ExecutionErrorCode.PLAN_STALE,
            "source job file exceeds the configured input limit",
            path=path.as_posix(),
        )
    try:
        current = load_job(
            path,
            max_bytes=workspace.config.execution.max_job_bytes,
        )
    except (JobError, ValueError) as exc:
        raise ExecutionError(
            ExecutionErrorCode.PLAN_STALE,
            "source job file no longer matches the approved job",
            path=path.as_posix(),
        ) from exc
    if current != job:
        raise ExecutionError(
            ExecutionErrorCode.PLAN_STALE,
            "source job file no longer matches the approved job",
            path=path.as_posix(),
        )
    return raw


def _error_result(error: ExecutionError) -> tuple[str, str]:
    root = error.cause_code or error.code
    if error.rollback_succeeded is True:
        return "ROLLED_BACK", root.value
    if error.rollback_succeeded is False:
        return "ROLLBACK_FAILED", root.value
    return error.code.value, root.value


def _rollback_state(error: ExecutionError) -> str:
    if error.rollback_skipped:
        return "SKIPPED_CHANGES_KEPT" if error.changes_kept else "SKIPPED_NO_CHANGES"
    return {None: "NOT_STARTED", True: "SUCCESS", False: "FAILED"}[
        error.rollback_succeeded
    ]


def _failure_stage(error: ExecutionError, plan: Plan | None) -> str:
    root = error.cause_code or error.code
    if root is ExecutionErrorCode.PATCH_ID_CONFLICT:
        return "JOB"
    if root in {
        ExecutionErrorCode.WORKSPACE_LOCKED,
        ExecutionErrorCode.WORKSPACE_LOCK_FAILED,
    }:
        return "WORKSPACE"
    if root in {
        ExecutionErrorCode.PLAN_STALE,
        ExecutionErrorCode.ACTION_UNSUPPORTED,
        ExecutionErrorCode.KEEP_CHANGES_FORBIDDEN,
    }:
        return "PLAN"
    if root is ExecutionErrorCode.USER_DECLINED:
        return "PLAN"
    if root is ExecutionErrorCode.BACKUP_FAILED:
        return "BACKUP"
    if root is ExecutionErrorCode.ACTION_FAILED:
        return (
            "AUDIT"
            if plan is not None and plan.job.kind is JobKind.AUDIT
            else "ACTIONS"
        )
    if root is ExecutionErrorCode.CHECK_FAILED:
        if plan is not None and len(error.check_results) > len(plan.checks):
            return "FINAL_CHECKS"
        return "INITIAL_CHECKS"
    if root is ExecutionErrorCode.HTML_LINT_FAILED:
        return "LINT_HTML"
    if root is ExecutionErrorCode.FORMAT_FAILED:
        return "FORMAT_BLACK" if error.path == "black" else "FORMAT_ISORT"
    if root in {
        ExecutionErrorCode.WORKSPACE_INVENTORY_FAILED,
        ExecutionErrorCode.UNEXPECTED_WORKSPACE_CHANGE,
    }:
        return "WORKSPACE_COMPARISON"
    return "SUMMARY"


def _conflict_error(job_id: str) -> ExecutionError:
    return ExecutionError(
        ExecutionErrorCode.PATCH_ID_CONFLICT,
        "job ID is already registered with different normalized content",
        item_id=job_id,
    )


def _attach_artifacts(error: ExecutionError, artifacts: _Artifacts) -> None:
    error.log_path = artifacts.log_path
    error.archived_job_path = artifacts.archived_job_path


__all__ = [
    "RegisteredRunResult",
    "RunResult",
    "RunStatus",
    "execute_plan",
    "execution_exit_code",
    "record_declined_plan",
    "resolve_registered_job",
]

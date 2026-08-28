"""Facts-first projection from trusted run state into history schema v1."""

from __future__ import annotations

from pathlib import Path

from patchshuttle._version import __version__
from patchshuttle.history.models import (
    HistoryAiLogReference,
    HistoryCheck,
    HistoryDeclared,
    HistoryFailure,
    HistoryFileChange,
    HistoryFiles,
    HistoryIntent,
    HistoryJob,
    HistoryObserved,
    HistoryRecord,
    HistoryRedaction,
    HistoryReferences,
    HistoryRollback,
    HistorySymbolTarget,
    HistoryWarning,
)
from patchshuttle.logging import RunLogData, redact_text
from patchshuttle.workspace import Workspace

_MAX_TITLE_BYTES = 512
_MAX_INTENT_BYTES = 8_192
_MAX_FAILURE_BYTES = 2_048
_MAX_WARNING_DETAIL_BYTES = 512
_MAX_WARNING_DETAILS = 20


def build_history_record(
    data: RunLogData,
    *,
    log_path: Path,
    record_id: str,
) -> HistoryRecord:
    """Project trusted run state into one compact facts-first record."""

    title, title_truncated = _bounded_declared_text(
        data, data.job.title, _MAX_TITLE_BYTES
    )
    description, description_truncated = _bounded_declared_text(
        data,
        data.job.description,
        _MAX_INTENT_BYTES,
    )
    intent = (
        HistoryIntent(text=description, truncated=description_truncated)
        if description is not None
        else None
    )
    files = _observed_files(data)
    checks = _history_checks(data)
    warnings = _history_warnings(checks)
    symbols = _declared_symbol_targets(data)
    affected = set(files.affected)
    affected_symbols = (
        tuple(item for item in symbols if item.path in affected)
        if data.result == "COMPLETED"
        else ()
    )
    planned_actions = len(data.plan.actions) if data.plan is not None else 0
    planned_checks = len(data.plan.checks) if data.plan is not None else 0
    files_to_create = _paths(data.plan.files_to_create) if data.plan is not None else ()
    files_to_modify = _paths(data.plan.files_to_modify) if data.plan is not None else ()
    detailed_log = _relative(data.workspace, log_path)
    return HistoryRecord(
        record_id=record_id,
        occurred_at=data.clock.iso_timestamp,
        patchshuttle_version=__version__,
        project_id=data.workspace.project_id,
        job=HistoryJob(
            id=data.job.id,
            hash=data.job_hash,
            kind=data.job.kind.value,
        ),
        redaction=HistoryRedaction(
            status=(
                "BEST_EFFORT_ENABLED"
                if data.workspace.config.logging.redact_known_secrets
                else "DISABLED_BY_LOCAL_POLICY"
            )
        ),
        declared=HistoryDeclared(
            title=title,
            title_truncated=title_truncated,
            intent=intent,
            planned_actions=planned_actions,
            planned_checks=planned_checks,
            files_to_create=files_to_create,
            files_to_modify=files_to_modify,
            symbol_targets=symbols,
        ),
        observed=HistoryObserved(
            status=data.result,
            summary=_observed_summary(data, files, checks),
            exit_code=data.exit_code,
            files=files,
            affected_symbols=affected_symbols,
            checks=checks,
            failure=_history_failure(data),
            rollback=_history_rollback(data),
            warnings=warnings,
        ),
        references=HistoryReferences(
            detailed_log=detailed_log,
            ai_log=HistoryAiLogReference(source_log=detailed_log),
            archived_job=_relative(data.workspace, data.archived_job_path),
            backup=(
                _relative(data.workspace, backup)
                if (backup := _backup_path(data)) is not None
                else None
            ),
        ),
    )


def _declared_symbol_targets(data: RunLogData) -> tuple[HistorySymbolTarget, ...]:
    targets: list[HistorySymbolTarget] = []
    for index, action in enumerate(data.job.actions, start=1):
        if action.name != "replace_symbol":
            continue
        targets.append(
            HistorySymbolTarget(
                action_id=f"action_{index:03d}",
                path=action.parameters.path,
                symbol=action.parameters.symbol,
            )
        )
    return tuple(targets)


def _observed_files(data: RunLogData) -> HistoryFiles:
    changes: list[HistoryFileChange] = []
    comparison = _workspace_comparison(data)
    if comparison is not None:
        changes.extend(
            HistoryFileChange(
                path=item.path.as_posix(),
                kind=item.kind.value,
                expected=item.expected,
            )
            for item in comparison.changes
        )
    elif data.manual_rollback is not None:
        changes.extend(
            HistoryFileChange(path=path.as_posix(), kind="RESTORED", expected=True)
            for path in data.manual_rollback.restored_files
        )
        changes.extend(
            HistoryFileChange(path=path.as_posix(), kind="REMOVED", expected=True)
            for path in data.manual_rollback.removed_files
        )
    elif data.transaction is not None:
        changes.extend(
            HistoryFileChange(path=path.as_posix(), kind="ADDED", expected=True)
            for path in data.transaction.created_files
        )
        changes.extend(
            HistoryFileChange(path=path.as_posix(), kind="MODIFIED", expected=True)
            for path in data.transaction.modified_files
        )
    changes.sort(key=lambda item: (item.path, item.kind, item.expected))
    affected = tuple(sorted({item.path for item in changes}))
    return HistoryFiles(
        affected=affected,
        created=tuple(sorted({item.path for item in changes if item.kind == "ADDED"})),
        modified=tuple(
            sorted(
                {item.path for item in changes if item.kind in {"MODIFIED", "RESTORED"}}
            )
        ),
        deleted=tuple(
            sorted(
                {item.path for item in changes if item.kind in {"DELETED", "REMOVED"}}
            )
        ),
        changes=tuple(changes),
    )


def _history_checks(data: RunLogData) -> tuple[HistoryCheck, ...]:
    initial, final = _split_checks(data)
    records: list[HistoryCheck] = []
    for phase, checks in (("initial", initial), ("final", final)):
        for item in checks:
            details: list[str] = []
            detail_truncated = len(item.new_warning_details) > _MAX_WARNING_DETAILS
            for value in item.new_warning_details[:_MAX_WARNING_DETAILS]:
                bounded, truncated = _bounded_declared_text(
                    data,
                    value,
                    _MAX_WARNING_DETAIL_BYTES,
                )
                details.append(bounded or "")
                detail_truncated = detail_truncated or truncated
            records.append(
                HistoryCheck(
                    phase=phase,
                    check_id=item.id,
                    profile=item.name,
                    status=item.status.value,
                    exit_code=item.return_code,
                    duration_ms=item.duration_ms,
                    warning_analysis=_optional_text(item.warning_analysis),
                    known_warnings=_optional_int(item.known_warnings),
                    new_warnings=_optional_int(item.new_warnings),
                    new_warning_details=tuple(details),
                    warning_details_truncated=detail_truncated,
                )
            )
    return tuple(records)


def _history_warnings(checks: tuple[HistoryCheck, ...]) -> tuple[HistoryWarning, ...]:
    return tuple(
        HistoryWarning(
            check_id=item.check_id,
            count=item.new_warnings or len(item.new_warning_details),
            details=item.new_warning_details,
            details_truncated=item.warning_details_truncated,
        )
        for item in checks
        if (item.new_warnings or 0) > 0 or item.new_warning_details
    )


def _history_failure(data: RunLogData) -> HistoryFailure | None:
    if data.error is None:
        return None
    message, truncated = _bounded_declared_text(
        data,
        data.error.message,
        _MAX_FAILURE_BYTES,
    )
    cause = data.error.cause_code or data.error.code
    return HistoryFailure(
        stage=data.failure_stage,
        recorded_code=data.failure_code,
        terminal_code=data.error.code.value,
        cause_code=cause.value,
        item_id=data.error.item_id,
        path=data.error.path,
        message=message or "",
        message_truncated=truncated,
    )


def _history_rollback(data: RunLogData) -> HistoryRollback:
    if data.manual_rollback is not None:
        item = data.manual_rollback
        return HistoryRollback(
            status=item.status,
            cause="USER_REQUESTED",
            backup=_relative(data.workspace, item.backup_path),
            restored_files=_paths(item.restored_files),
            removed_files=_paths(item.removed_files),
            removed_directories=_paths(item.removed_directories),
            unresolved=_paths(item.unresolved),
        )
    if data.error is None:
        return HistoryRollback(status="NOT_REQUIRED", cause=None, backup=None)
    if data.error.rollback_skipped:
        status_value = (
            "SKIPPED_CHANGES_KEPT" if data.error.changes_kept else "SKIPPED_NO_CHANGES"
        )
    else:
        status_value = {None: "NOT_STARTED", True: "SUCCESS", False: "FAILED"}[
            data.error.rollback_succeeded
        ]
    cause = data.error.cause_code or data.error.code
    return HistoryRollback(
        status=status_value,
        cause=cause.value,
        backup=(
            _relative(data.workspace, data.error.backup_path)
            if data.error.backup_path is not None
            else None
        ),
    )


def _observed_summary(
    data: RunLogData,
    files: HistoryFiles,
    checks: tuple[HistoryCheck, ...],
) -> str:
    passed = sum(item.status == "PASSED" for item in checks)
    if data.result in {"COMPLETED", "NO_CHANGE", "ALREADY_APPLIED"}:
        return (
            f"{data.job.kind.value.capitalize()} job ended with {data.result}; "
            f"observed {len(files.affected)} affected file(s); "
            f"{passed} of {len(checks)} recorded check run(s) passed."
        )
    rollback = _history_rollback(data).status
    return (
        f"{data.job.kind.value.capitalize()} job ended with {data.result}; "
        f"failure stage {data.failure_stage or 'NOT_APPLICABLE'}, "
        f"code {data.failure_code or 'NOT_APPLICABLE'}; rollback {rollback}."
    )


def _split_checks(data: RunLogData) -> tuple[tuple, tuple]:
    if data.transaction is not None:
        return data.transaction.initial_checks, data.transaction.final_checks
    if data.verification_checks:
        return data.verification_checks, ()
    if data.error is None or data.plan is None:
        return (), ()
    checks = data.error.check_results
    if not data.error.formatting_results:
        return checks, ()
    boundary = min(len(data.plan.checks), len(checks))
    return checks[:boundary], checks[boundary:]


def _workspace_comparison(data: RunLogData):
    if data.workspace_comparison is not None:
        return data.workspace_comparison
    if data.transaction is not None:
        return data.transaction.workspace_comparison
    if data.error is not None:
        return data.error.workspace_comparison
    return None


def _backup_path(data: RunLogData) -> Path | None:
    if data.manual_rollback is not None:
        return data.manual_rollback.backup_path
    if data.transaction is not None:
        return data.transaction.backup_path
    if data.error is not None:
        return data.error.backup_path
    return None


def _bounded_declared_text(
    data: RunLogData,
    value: str | None,
    max_bytes: int,
) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    text = (
        redact_text(value)
        if data.workspace.config.logging.redact_known_secrets
        else value
    )
    return _bounded_text(text, max_bytes)


def _bounded_text(value: str, max_bytes: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value != "NOT_APPLICABLE" else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _paths(paths: tuple) -> tuple[str, ...]:
    return tuple(path.as_posix() for path in paths)


def _relative(workspace: Workspace, path: Path) -> str:
    try:
        return path.relative_to(workspace.root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = ["build_history_record"]

"""Internal transaction runner for approved text-file change plans."""

from __future__ import annotations

import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath

from filelock import FileLock, Timeout

import patchshuttle.actions as actions
from patchshuttle.backup import (
    BackupStatus,
    PreparedBackup,
    prepare_backup,
    update_backup,
)
from patchshuttle.checks import CheckResult, CheckStatus, run_checks
from patchshuttle.errors import (
    ExecutionError,
    ExecutionErrorCode,
    PlanningError,
    PolicyError,
    WorkspaceError,
)
from patchshuttle.formatters import (
    FormattedFileState,
    FormatterResult,
    FormatterStatus,
    capture_formatted_files,
    run_formatters,
    verify_formatted_files,
)
from patchshuttle.inventory import (
    InventoryError,
    WorkspaceComparison,
    WorkspaceInventory,
    capture_inventory,
    compare_inventories,
)
from patchshuttle.linters import HtmlLintResult, HtmlLintStatus, run_html_linter
from patchshuttle.models import JobKind
from patchshuttle.planner import (
    ActionDisposition,
    FileDisposition,
    Plan,
    plan_job,
)
from patchshuttle.rollback import rollback_created, rollback_transaction
from patchshuttle.runtime_cache import (
    RuntimeCacheError,
    RuntimeCacheLedger,
    capture_runtime_cache_ledger,
    cleanup_runtime_caches,
)
from patchshuttle.workspace import Workspace

_CREATE_ACTIONS = frozenset({"create_directory", "create_file"})
_CHANGE_ACTIONS = frozenset(
    {
        *_CREATE_ACTIONS,
        "replace_exact",
        "insert_before",
        "insert_after",
        "delete_exact",
        "replace_range",
        "delete_range",
        "insert_at_line",
        "apply_diff",
    }
)


class TransactionStatus(str, Enum):
    """Successful outcomes of the internal transaction core."""

    APPLIED = "APPLIED"
    NO_CHANGE = "NO_CHANGE"


@dataclass(frozen=True, slots=True)
class TransactionResult:
    """Immutable result returned only after a verified transaction outcome."""

    status: TransactionStatus
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


def execute_create_transaction(
    plan: Plan,
    *,
    approved: bool = False,
    keep_changes: bool = False,
) -> TransactionResult:
    """Apply only create actions through the retained Phase 8 contract."""

    return _execute_transaction(
        plan,
        approved=approved,
        keep_changes=keep_changes,
        allowed_actions=_CREATE_ACTIONS,
        allow_modify=False,
    )


def execute_change_transaction(
    plan: Plan,
    *,
    approved: bool = False,
    keep_changes: bool = False,
) -> TransactionResult:
    """Apply all planned text changes under one guarded transaction."""

    return _execute_transaction(
        plan,
        approved=approved,
        keep_changes=keep_changes,
        allowed_actions=_CHANGE_ACTIONS,
        allow_modify=True,
    )


def execute_change_transaction_locked(
    plan: Plan,
    *,
    approved: bool = False,
    keep_changes: bool = False,
) -> TransactionResult:
    """Execute a patch while the caller holds the workspace run lock.

    This entry point exists for the public operational coordinator, which must
    keep registry checks, project changes, logs, and archive updates inside one
    lock boundary. Direct callers should use ``execute_change_transaction``.
    """

    _require_supported_plan(
        plan,
        allowed_actions=_CHANGE_ACTIONS,
        allow_modify=True,
    )
    _require_approval(approved)
    _require_keep_changes_allowed(plan, keep_changes)
    return _execute_locked(plan, keep_changes=keep_changes)


def _execute_transaction(
    plan: Plan,
    *,
    approved: bool,
    keep_changes: bool,
    allowed_actions: frozenset[str],
    allow_modify: bool,
) -> TransactionResult:
    """Validate entry-point authority, acquire the lock, and execute."""

    _require_supported_plan(
        plan,
        allowed_actions=allowed_actions,
        allow_modify=allow_modify,
    )
    _require_approval(approved)
    _require_keep_changes_allowed(plan, keep_changes)
    with acquire_workspace_lock(plan.workspace):
        return _execute_locked(plan, keep_changes=keep_changes)


def _require_approval(approved: bool) -> None:
    if not approved:
        raise ExecutionError(
            ExecutionErrorCode.APPROVAL_REQUIRED,
            "explicit approval is required before project files are changed",
        )


def _require_keep_changes_allowed(plan: Plan, keep_changes: bool) -> None:
    if keep_changes and not plan.workspace.config.execution.allow_keep_changes:
        raise ExecutionError(
            ExecutionErrorCode.KEEP_CHANGES_FORBIDDEN,
            "local workspace policy does not allow keeping failed-job changes",
        )


@contextmanager
def acquire_workspace_lock(workspace: Workspace) -> Iterator[None]:
    """Acquire one initialized workspace's non-blocking operational lock."""

    lock_path = workspace.patches_dir / "state/run.lock"
    try:
        lock_metadata = lock_path.lstat()
        if not stat.S_ISREG(lock_metadata.st_mode):
            raise OSError("workspace lock path is not a regular file")
    except OSError as exc:
        raise ExecutionError(
            ExecutionErrorCode.WORKSPACE_LOCK_FAILED,
            "workspace lock file is missing, unsafe, or unreadable",
            path="patches/state/run.lock",
        ) from exc
    lock = FileLock(lock_path, timeout=0, preserve_lock_file=True)
    try:
        with lock.acquire(timeout=0):
            yield
    except Timeout as exc:
        raise ExecutionError(
            ExecutionErrorCode.WORKSPACE_LOCKED,
            "another PatchShuttle transaction holds the workspace lock",
            path="patches/state/run.lock",
        ) from exc
    except OSError as exc:
        raise ExecutionError(
            ExecutionErrorCode.WORKSPACE_LOCK_FAILED,
            "workspace lock could not be acquired or released",
            path="patches/state/run.lock",
        ) from exc


def _require_supported_plan(
    plan: Plan,
    *,
    allowed_actions: frozenset[str],
    allow_modify: bool,
) -> None:
    if plan.job.kind is not JobKind.PATCH:
        raise ExecutionError(
            ExecutionErrorCode.JOB_KIND_UNSUPPORTED,
            "the internal transaction core accepts only patch jobs",
        )
    for action in plan.actions:
        if action.name not in allowed_actions:
            raise ExecutionError(
                ExecutionErrorCode.ACTION_UNSUPPORTED,
                "this transaction entry point does not accept the planned action",
                item_id=action.id,
                path=(action.paths[0].as_posix() if action.paths else None),
            )
    if not allow_modify and any(
        change.disposition is not FileDisposition.CREATE for change in plan.file_changes
    ):
        raise ExecutionError(
            ExecutionErrorCode.ACTION_UNSUPPORTED,
            "the create-only transaction cannot modify existing files",
        )


def _execute_locked(plan: Plan, *, keep_changes: bool = False) -> TransactionResult:
    _revalidate_plan(plan)
    if not plan.file_changes and not plan.directories_to_create:
        return TransactionResult(
            status=TransactionStatus.NO_CHANGE,
            plan=plan,
            backup_path=None,
            created_files=(),
            created_directories=(),
            modified_files=(),
            html_lint_results=(),
            initial_checks=(),
            formatting_results=(),
            formatted_files=(),
            final_checks=(),
            workspace_comparison=None,
        )

    baseline = _capture_workspace_inventory(plan)
    backup = prepare_backup(plan)
    created_files: list[PurePosixPath] = []
    rollback_files: list[PurePosixPath] = []
    created_directories: list[PurePosixPath] = []
    modified_files: list[PurePosixPath] = []
    applied_files: set[PurePosixPath] = set()
    current_item: str | None = None
    current_path: PurePosixPath | None = None
    html_lint_results: tuple[HtmlLintResult, ...] = ()
    initial_checks: tuple[CheckResult, ...] = ()
    formatting_results: tuple[FormatterResult, ...] = ()
    formatted_files: tuple[FormattedFileState, ...] = ()
    final_checks: tuple[CheckResult, ...] = ()
    workspace_comparison: WorkspaceComparison | None = None
    runtime_cache_ledger: RuntimeCacheLedger | None = None
    try:
        changes = {change.path: change for change in plan.file_changes}
        for action in plan.actions:
            current_item = action.id
            current_path = action.paths[0] if action.paths else None
            if action.name == "create_directory":
                if action.disposition is ActionDisposition.CREATE:
                    _create_required_directories(
                        plan,
                        action.paths[0],
                        created_directories,
                    )
                continue
            for path in action.paths:
                current_path = path
                change = changes.get(path)
                if change is None or path in applied_files:
                    continue
                if change.disposition is FileDisposition.CREATE:
                    _create_required_directories(
                        plan,
                        path.parent,
                        created_directories,
                    )
                    try:
                        actions.atomic_create_file(plan.workspace, change)
                    except actions.FilePublishError as exc:
                        if exc.target_created:
                            created_files.append(path)
                            rollback_files.append(path)
                        if exc.temporary_path is not None:
                            rollback_files.append(
                                PurePosixPath(
                                    exc.temporary_path.relative_to(
                                        plan.workspace.root
                                    ).as_posix()
                                )
                            )
                        raise
                    created_files.append(path)
                    rollback_files.append(path)
                    actions.verify_created_file(plan.workspace, change)
                else:
                    entry = backup.entry_for(path)
                    if entry.original_mode is None:
                        raise OSError("modified file backup is missing its mode")
                    try:
                        actions.atomic_replace_file(
                            plan.workspace,
                            change,
                            mode=entry.original_mode,
                        )
                    except actions.FileReplaceError as exc:
                        if exc.target_modified:
                            modified_files.append(path)
                        if exc.temporary_path is not None:
                            rollback_files.append(
                                PurePosixPath(
                                    exc.temporary_path.relative_to(
                                        plan.workspace.root
                                    ).as_posix()
                                )
                            )
                        raise
                    modified_files.append(path)
                    actions.verify_modified_file(
                        plan.workspace,
                        change,
                        mode=entry.original_mode,
                    )
                applied_files.add(path)

        if tuple(created_directories) != plan.directories_to_create:
            raise OSError("not all planned directories were created")
        if tuple(created_files) != plan.files_to_create:
            raise OSError("not all planned files were created")
        if tuple(modified_files) != plan.files_to_modify:
            raise OSError("not all planned files were modified")

        try:
            runtime_cache_ledger = capture_runtime_cache_ledger(plan)
        except RuntimeCacheError as exc:
            raise ExecutionError(
                ExecutionErrorCode.RUNTIME_CACHE_CLEANUP_FAILED,
                "runtime cache baseline could not be captured safely",
                item_id="runtime_cache",
                path=exc.path.as_posix(),
            ) from exc

        if plan.html_lint_targets:
            current_item = "html_lint"
            current_path = plan.html_lint_targets[0]
            try:
                html_lint_run = run_html_linter(plan)
            except (OSError, PolicyError, ValueError) as exc:
                raise ExecutionError(
                    ExecutionErrorCode.HTML_LINT_FAILED,
                    "HTML lint commands could not be prepared or launched",
                    item_id="html_lint",
                    path=plan.html_lint_targets[0].as_posix(),
                    html_lint_results=html_lint_results,
                ) from exc
            html_lint_results = html_lint_run.results
            if html_lint_run.failed is not None:
                failure_messages = {
                    HtmlLintStatus.FAILED: "djLint reported template lint errors",
                    HtmlLintStatus.TIMED_OUT: "djLint timed out",
                    HtmlLintStatus.ERROR: "djLint could not be started",
                }
                raise ExecutionError(
                    ExecutionErrorCode.HTML_LINT_FAILED,
                    failure_messages[html_lint_run.failed.status],
                    item_id=html_lint_run.failed.id,
                    path=html_lint_run.failed.path.as_posix(),
                    html_lint_results=html_lint_results,
                )
            _verify_planned_transaction_files(
                plan,
                backup,
                excluded=frozenset(),
                code=ExecutionErrorCode.HTML_LINT_FAILED,
                message="HTML linter changed a declared transaction file",
                check_results=(),
                formatting_results=(),
                html_lint_results=html_lint_results,
            )

        current_item = None
        current_path = None
        check_run = run_checks(plan)
        initial_checks = check_run.results
        if check_run.failed is not None:
            failure_messages = {
                CheckStatus.FAILED: "initial project check returned a non-zero exit code",
                CheckStatus.TIMED_OUT: "initial project check timed out",
                CheckStatus.ERROR: "initial project check could not be started",
            }
            raise ExecutionError(
                ExecutionErrorCode.CHECK_FAILED,
                failure_messages[check_run.failed.status],
                item_id=check_run.failed.id,
                path=check_run.failed.name,
                check_results=initial_checks,
            )
        _verify_transaction_files_after_checks(plan, backup, initial_checks)

        if plan.formatter_run_paths:
            current_item = "formatting"
            current_path = plan.formatter_run_paths[0]
            before_formatting = _capture_formatter_states(
                plan,
                check_results=initial_checks,
                formatting_results=formatting_results,
                message="formatter targets could not be captured before formatting",
            )
            try:
                formatting_run = run_formatters(plan)
            except (OSError, PolicyError, ValueError) as exc:
                raise ExecutionError(
                    ExecutionErrorCode.FORMAT_FAILED,
                    "formatter commands could not be prepared or launched",
                    item_id="formatting",
                    path=plan.formatter_run_paths[0].as_posix(),
                    check_results=initial_checks,
                    formatting_results=formatting_results,
                ) from exc
            formatting_results = formatting_run.results
            if formatting_run.failed is not None:
                failure_messages = {
                    FormatterStatus.FAILED: ("formatter returned a non-zero exit code"),
                    FormatterStatus.TIMED_OUT: "formatter timed out",
                    FormatterStatus.ERROR: "formatter could not be started",
                }
                raise ExecutionError(
                    ExecutionErrorCode.FORMAT_FAILED,
                    failure_messages[formatting_run.failed.status],
                    item_id=formatting_run.failed.id,
                    path=formatting_run.failed.name,
                    check_results=initial_checks,
                    formatting_results=formatting_results,
                )
            _verify_planned_transaction_files(
                plan,
                backup,
                excluded=frozenset(plan.formatter_run_paths),
                code=ExecutionErrorCode.FORMAT_FAILED,
                message="formatters changed a declared non-formatting file",
                check_results=initial_checks,
                formatting_results=formatting_results,
            )
            formatted_files = _capture_formatter_states(
                plan,
                check_results=initial_checks,
                formatting_results=formatting_results,
                message="formatter output has an invalid transaction state",
            )
            _require_preserved_formatter_modes(
                before_formatting,
                formatted_files,
                check_results=initial_checks,
                formatting_results=formatting_results,
            )

            if plan.workspace.config.formatting.rerun_checks:
                current_item = None
                current_path = None
                final_run = run_checks(plan)
                final_checks = final_run.results
                all_checks = initial_checks + final_checks
                if final_run.failed is not None:
                    failure_messages = {
                        CheckStatus.FAILED: (
                            "final project check returned a non-zero exit code"
                        ),
                        CheckStatus.TIMED_OUT: "final project check timed out",
                        CheckStatus.ERROR: "final project check could not be started",
                    }
                    raise ExecutionError(
                        ExecutionErrorCode.CHECK_FAILED,
                        failure_messages[final_run.failed.status],
                        item_id=final_run.failed.id,
                        path=final_run.failed.name,
                        check_results=all_checks,
                        formatting_results=formatting_results,
                    )
                _verify_planned_transaction_files(
                    plan,
                    backup,
                    excluded=frozenset(plan.formatter_run_paths),
                    code=ExecutionErrorCode.CHECK_FAILED,
                    message="final project checks changed a declared transaction file",
                    check_results=all_checks,
                    formatting_results=formatting_results,
                )
                try:
                    verify_formatted_files(plan, formatted_files)
                except (OSError, PolicyError, ValueError) as exc:
                    raise ExecutionError(
                        ExecutionErrorCode.CHECK_FAILED,
                        "final project checks changed a formatted transaction file",
                        path=_first_formatter_path(plan),
                        check_results=all_checks,
                        formatting_results=formatting_results,
                    ) from exc

        current_item = None
        current_path = None
        _require_runtime_cache_cleanup(plan, runtime_cache_ledger)
        workspace_comparison = _compare_workspace_to_baseline(plan, baseline)
        if workspace_comparison.unexpected_changes:
            unexpected = workspace_comparison.unexpected_changes[0]
            raise ExecutionError(
                ExecutionErrorCode.UNEXPECTED_WORKSPACE_CHANGE,
                "project checks or formatters changed an undeclared workspace path",
                path=unexpected.path.as_posix(),
                check_results=initial_checks + final_checks,
                formatting_results=formatting_results,
                workspace_comparison=workspace_comparison,
            )
        try:
            update_backup(
                backup,
                BackupStatus.COMPLETED,
                capture_applied_state=True,
            )
        except ExecutionError as exc:
            exc.check_results = initial_checks + final_checks
            exc.formatting_results = formatting_results
            exc.html_lint_results = html_lint_results
            raise
    except BaseException as exc:
        if isinstance(exc, ExecutionError) and not exc.html_lint_results:
            exc.html_lint_results = html_lint_results
        failure = _action_failure(
            exc,
            item_id=current_item,
            path=current_path,
            backup=backup,
        )
        cache_unresolved: tuple[PurePosixPath, ...] = ()
        if runtime_cache_ledger is not None:
            try:
                cache_unresolved = cleanup_runtime_caches(
                    plan,
                    runtime_cache_ledger,
                ).unresolved
            except (OSError, ValueError):
                cache_unresolved = (PurePosixPath("__pycache__"),)
        if plan.auto_rollback and not keep_changes:
            try:
                _rollback_or_raise(
                    failure,
                    backup,
                    files=tuple(rollback_files),
                    directories=tuple(created_directories),
                    modified_files=tuple(modified_files),
                    additional_unresolved=cache_unresolved,
                )
            except ExecutionError as rollback_failure:
                rollback_failure.workspace_comparison = _safe_workspace_comparison(
                    plan,
                    baseline,
                    fallback=rollback_failure.workspace_comparison,
                )
                raise
        else:
            _record_retained_failure(
                failure,
                backup,
                changes_present=bool(
                    rollback_files or created_directories or modified_files
                ),
            )
        failure.workspace_comparison = _safe_workspace_comparison(
            plan,
            baseline,
            fallback=failure.workspace_comparison,
        )
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise failure

    return TransactionResult(
        status=TransactionStatus.APPLIED,
        plan=plan,
        backup_path=backup.path,
        created_files=tuple(created_files),
        created_directories=tuple(created_directories),
        modified_files=tuple(modified_files),
        html_lint_results=html_lint_results,
        initial_checks=initial_checks,
        formatting_results=formatting_results,
        formatted_files=formatted_files,
        final_checks=final_checks,
        workspace_comparison=workspace_comparison,
    )


def _capture_workspace_inventory(plan: Plan) -> WorkspaceInventory:
    try:
        return capture_inventory(plan.workspace)
    except InventoryError as exc:
        raise ExecutionError(
            ExecutionErrorCode.WORKSPACE_INVENTORY_FAILED,
            "workspace baseline inventory could not be captured",
            path=exc.path.as_posix() if exc.path is not None else None,
        ) from exc


def _compare_workspace_to_baseline(
    plan: Plan,
    baseline: WorkspaceInventory,
) -> WorkspaceComparison:
    try:
        current = capture_inventory(plan.workspace)
    except InventoryError as exc:
        raise ExecutionError(
            ExecutionErrorCode.WORKSPACE_INVENTORY_FAILED,
            "final workspace inventory could not be captured",
            path=exc.path.as_posix() if exc.path is not None else None,
        ) from exc
    return compare_inventories(
        baseline,
        current,
        expected_paths=(
            *plan.files_to_create,
            *plan.files_to_modify,
            *plan.directories_to_create,
        ),
    )


def _safe_workspace_comparison(
    plan: Plan,
    baseline: WorkspaceInventory,
    *,
    fallback: WorkspaceComparison | None,
) -> WorkspaceComparison | None:
    try:
        return _compare_workspace_to_baseline(plan, baseline)
    except ExecutionError:
        return fallback


def _revalidate_plan(plan: Plan) -> None:
    try:
        current = plan_job(plan.job, plan.workspace.root)
    except (PlanningError, PolicyError, WorkspaceError) as exc:
        raise ExecutionError(
            ExecutionErrorCode.PLAN_STALE,
            "the workspace no longer matches the approved plan",
            item_id=getattr(exc, "item_id", None),
            path=getattr(exc, "path", None),
        ) from exc
    if current != plan:
        raise ExecutionError(
            ExecutionErrorCode.PLAN_STALE,
            "the workspace no longer matches the approved plan",
            path=_first_stale_path(plan, current),
        )


def _first_stale_path(plan: Plan, current: Plan) -> str | None:
    current_creates = set(current.files_to_create) | set(current.directories_to_create)
    for path in (*plan.files_to_create, *plan.directories_to_create):
        if path not in current_creates:
            return path.as_posix()
    current_fingerprints = {item.path: item for item in current.fingerprints}
    for fingerprint in plan.fingerprints:
        if current_fingerprints.get(fingerprint.path) != fingerprint:
            return fingerprint.path.as_posix()
    return None


def _create_required_directories(
    plan: Plan,
    target: PurePosixPath,
    created: list[PurePosixPath],
) -> None:
    for path in plan.directories_to_create:
        if path in created:
            continue
        if path != target and path not in target.parents:
            continue
        actions.create_directory(plan.workspace, path)
        created.append(path)
        actions.verify_created_directory(plan.workspace, path)


def _verify_transaction_files_after_checks(
    plan: Plan,
    backup: PreparedBackup,
    check_results: tuple[CheckResult, ...],
) -> None:
    _verify_planned_transaction_files(
        plan,
        backup,
        excluded=frozenset(),
        code=ExecutionErrorCode.CHECK_FAILED,
        message="project checks changed a declared transaction file",
        check_results=check_results,
        formatting_results=(),
    )


def _verify_planned_transaction_files(
    plan: Plan,
    backup: PreparedBackup,
    *,
    excluded: frozenset[PurePosixPath],
    code: ExecutionErrorCode,
    message: str,
    check_results: tuple[CheckResult, ...],
    formatting_results: tuple[FormatterResult, ...],
    html_lint_results: tuple[HtmlLintResult, ...] = (),
) -> None:
    for change in plan.file_changes:
        if change.path in excluded:
            continue
        try:
            if change.disposition is FileDisposition.CREATE:
                actions.verify_created_file(plan.workspace, change)
            else:
                entry = backup.entry_for(change.path)
                if entry.original_mode is None:
                    raise OSError("modified file backup is missing its mode")
                actions.verify_modified_file(
                    plan.workspace,
                    change,
                    mode=entry.original_mode,
                )
        except (KeyError, OSError, PolicyError) as exc:
            raise ExecutionError(
                code,
                message,
                path=change.path.as_posix(),
                check_results=check_results,
                formatting_results=formatting_results,
                html_lint_results=html_lint_results,
            ) from exc


def _capture_formatter_states(
    plan: Plan,
    *,
    check_results: tuple[CheckResult, ...],
    formatting_results: tuple[FormatterResult, ...],
    message: str,
) -> tuple[FormattedFileState, ...]:
    try:
        return capture_formatted_files(plan)
    except (OSError, PolicyError, ValueError) as exc:
        raise ExecutionError(
            ExecutionErrorCode.FORMAT_FAILED,
            message,
            item_id="formatting",
            path=_first_formatter_path(plan),
            check_results=check_results,
            formatting_results=formatting_results,
        ) from exc


def _require_preserved_formatter_modes(
    before: tuple[FormattedFileState, ...],
    after: tuple[FormattedFileState, ...],
    *,
    check_results: tuple[CheckResult, ...],
    formatting_results: tuple[FormatterResult, ...],
) -> None:
    if tuple(item.path for item in before) != tuple(item.path for item in after):
        raise ExecutionError(
            ExecutionErrorCode.FORMAT_FAILED,
            "formatter output scope no longer matches the approved plan",
            item_id="formatting",
            path=before[0].path.as_posix() if before else None,
            check_results=check_results,
            formatting_results=formatting_results,
        )
    for earlier, later in zip(before, after):
        if earlier.mode != later.mode:
            raise ExecutionError(
                ExecutionErrorCode.FORMAT_FAILED,
                "formatter changed the mode of a transaction file",
                item_id="formatting",
                path=later.path.as_posix(),
                check_results=check_results,
                formatting_results=formatting_results,
            )


def _first_formatter_path(plan: Plan) -> str | None:
    return plan.formatter_run_paths[0].as_posix() if plan.formatter_run_paths else None


def _require_runtime_cache_cleanup(
    plan: Plan,
    ledger: RuntimeCacheLedger,
) -> None:
    try:
        cleanup = cleanup_runtime_caches(plan, ledger)
    except (OSError, ValueError) as exc:
        raise ExecutionError(
            ExecutionErrorCode.RUNTIME_CACHE_CLEANUP_FAILED,
            "runtime caches could not be inspected after project checks",
            item_id="runtime_cache",
        ) from exc
    if cleanup.unresolved:
        raise ExecutionError(
            ExecutionErrorCode.RUNTIME_CACHE_CLEANUP_FAILED,
            "runtime caches could not be cleaned safely after project checks",
            item_id="runtime_cache",
            path=cleanup.unresolved[0].as_posix(),
        )


def _action_failure(
    exc: BaseException,
    *,
    item_id: str | None,
    path: PurePosixPath | None,
    backup: PreparedBackup,
) -> ExecutionError:
    if isinstance(exc, ExecutionError):
        return ExecutionError(
            exc.code,
            exc.message,
            item_id=exc.item_id or item_id,
            path=exc.path or (path.as_posix() if path is not None else None),
            backup_path=backup.path,
            rollback_skipped=exc.rollback_skipped,
            changes_kept=exc.changes_kept,
            cause_code=exc.cause_code,
            check_results=exc.check_results,
            formatting_results=exc.formatting_results,
            html_lint_results=exc.html_lint_results,
            workspace_comparison=exc.workspace_comparison,
        )
    return ExecutionError(
        ExecutionErrorCode.ACTION_FAILED,
        "a planned transaction action failed",
        item_id=item_id,
        path=path.as_posix() if path is not None else None,
        backup_path=backup.path,
    )


def _record_retained_failure(
    failure: ExecutionError,
    backup: PreparedBackup,
    *,
    changes_present: bool,
) -> None:
    failure.rollback_skipped = True
    failure.changes_kept = changes_present
    status = BackupStatus.CHANGES_KEPT if changes_present else BackupStatus.FAILED
    try:
        update_backup(
            backup,
            status,
            failure_code=failure.code,
        )
    except ExecutionError as error:
        error.cause_code = failure.code
        error.check_results = failure.check_results
        error.formatting_results = failure.formatting_results
        error.html_lint_results = failure.html_lint_results
        error.rollback_skipped = True
        error.changes_kept = changes_present
        raise


def _rollback_or_raise(
    failure: ExecutionError,
    backup: PreparedBackup,
    *,
    files: tuple[PurePosixPath, ...],
    directories: tuple[PurePosixPath, ...],
    modified_files: tuple[PurePosixPath, ...],
    additional_unresolved: tuple[PurePosixPath, ...] = (),
) -> None:
    try:
        if modified_files:
            rollback = rollback_transaction(
                backup.plan.workspace,
                backup,
                modified_files=modified_files,
                files=files,
                directories=directories,
            )
        else:
            rollback = rollback_created(
                backup.plan.workspace,
                files=files,
                directories=directories,
            )
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise ExecutionError(
            ExecutionErrorCode.ROLLBACK_FAILED,
            "rollback failed before it could report a complete result",
            backup_path=backup.path,
            rollback_succeeded=False,
            cause_code=failure.code,
            check_results=failure.check_results,
            formatting_results=failure.formatting_results,
            html_lint_results=failure.html_lint_results,
            workspace_comparison=failure.workspace_comparison,
        ) from exc
    if rollback.success and not additional_unresolved:
        try:
            update_backup(
                backup,
                BackupStatus.ROLLED_BACK,
                failure_code=failure.code,
            )
        except ExecutionError as exc:
            exc.rollback_succeeded = True
            exc.cause_code = failure.code
            exc.check_results = failure.check_results
            exc.formatting_results = failure.formatting_results
            exc.html_lint_results = failure.html_lint_results
            raise
        failure.rollback_succeeded = True
        return

    try:
        update_backup(
            backup,
            BackupStatus.ROLLBACK_FAILED,
            failure_code=failure.code,
        )
    except ExecutionError:
        pass
    raise ExecutionError(
        ExecutionErrorCode.ROLLBACK_FAILED,
        "rollback could not restore every created path",
        path=(rollback.unresolved or additional_unresolved)[0].as_posix(),
        backup_path=backup.path,
        rollback_succeeded=False,
        cause_code=failure.code,
        check_results=failure.check_results,
        formatting_results=failure.formatting_results,
        html_lint_results=failure.html_lint_results,
        workspace_comparison=failure.workspace_comparison,
    )


__all__ = [
    "TransactionResult",
    "TransactionStatus",
    "acquire_workspace_lock",
    "execute_change_transaction",
    "execute_change_transaction_locked",
    "execute_create_transaction",
]

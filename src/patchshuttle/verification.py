"""One-pass controlled verification jobs with workspace observation."""

from __future__ import annotations

from dataclasses import dataclass, field

from patchshuttle.checks import CheckResult, CheckStatus, run_checks
from patchshuttle.errors import ExecutionError, ExecutionErrorCode, PolicyError
from patchshuttle.inventory import (
    InventoryError,
    WorkspaceComparison,
    capture_inventory,
    compare_inventories,
)
from patchshuttle.models import JobKind
from patchshuttle.planner import Plan, plan_job


@dataclass(frozen=True, slots=True)
class VerificationRunResult:
    """Checks and workspace comparison from one verify plan."""

    plan: Plan = field(repr=False)
    checks: tuple[CheckResult, ...]
    workspace_comparison: WorkspaceComparison


def execute_verification_locked(plan: Plan) -> VerificationRunResult:
    """Execute a verify job while the caller holds the workspace run lock."""

    if plan.job.kind is not JobKind.VERIFY:
        raise ExecutionError(
            ExecutionErrorCode.JOB_KIND_UNSUPPORTED,
            "the verification runner accepts only verify jobs",
        )
    _revalidate_plan(plan)
    baseline = _capture(plan, final=False)
    check_run = run_checks(plan)
    comparison = _compare(plan, baseline)
    if check_run.failed is not None:
        messages = {
            CheckStatus.FAILED: "project check returned a non-zero exit code",
            CheckStatus.TIMED_OUT: "project check timed out",
            CheckStatus.ERROR: "project check could not be started",
        }
        raise ExecutionError(
            ExecutionErrorCode.CHECK_FAILED,
            messages[check_run.failed.status],
            item_id=check_run.failed.id,
            path=check_run.failed.name,
            check_results=check_run.results,
            workspace_comparison=comparison,
        )
    if comparison.unexpected_changes:
        first = comparison.unexpected_changes[0]
        raise ExecutionError(
            ExecutionErrorCode.UNEXPECTED_WORKSPACE_CHANGE,
            "project checks changed the workspace during verification",
            path=first.path.as_posix(),
            check_results=check_run.results,
            workspace_comparison=comparison,
        )
    return VerificationRunResult(
        plan=plan,
        checks=check_run.results,
        workspace_comparison=comparison,
    )


def _capture(plan: Plan, *, final: bool):
    try:
        return capture_inventory(plan.workspace)
    except InventoryError as exc:
        raise ExecutionError(
            ExecutionErrorCode.WORKSPACE_INVENTORY_FAILED,
            (
                "final workspace inventory could not be captured"
                if final
                else "workspace baseline inventory could not be captured"
            ),
            path=exc.path.as_posix() if exc.path is not None else None,
        ) from exc


def _compare(plan: Plan, baseline) -> WorkspaceComparison:
    current = _capture(plan, final=True)
    return compare_inventories(baseline, current)


def _revalidate_plan(plan: Plan) -> None:
    try:
        current = plan_job(plan.job, plan.workspace)
    except (PolicyError, ValueError) as exc:
        raise ExecutionError(
            ExecutionErrorCode.PLAN_STALE,
            "the workspace no longer matches the approved verification plan",
            item_id=getattr(exc, "item_id", None),
            path=getattr(exc, "path", None),
        ) from exc
    if current != plan:
        raise ExecutionError(
            ExecutionErrorCode.PLAN_STALE,
            "the workspace no longer matches the approved verification plan",
        )


__all__ = ["VerificationRunResult", "execute_verification_locked"]

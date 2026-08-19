"""Public package surface for PatchShuttle."""

from patchshuttle._version import __version__
from patchshuttle.audit import AuditActionResult, AuditRunResult, AuditStatus
from patchshuttle.context import (
    HandoffResult,
    SnapshotResult,
    create_handoff,
    create_snapshot,
)
from patchshuttle.errors import (
    ExecutionError,
    ExecutionErrorCode,
    JobError,
    JobErrorCode,
    PlanningError,
    PlanningErrorCode,
    PolicyError,
    PolicyErrorCode,
    WorkspaceError,
    WorkspaceErrorCode,
)
from patchshuttle.execution import RunResult, RunStatus, execute_plan
from patchshuttle.models import Action, Check, Job, JobKind
from patchshuttle.operations import ManualRollbackResult, rollback_job
from patchshuttle.parser import load_job, validate_job
from patchshuttle.planner import (
    ActionDisposition,
    FileDisposition,
    NewlineStyle,
    PathFingerprint,
    Plan,
    PlanDiff,
    PlannedAction,
    PlannedCheck,
    PlannedFileChange,
    plan_job,
    render_plan_diff,
)
from patchshuttle.policy import PathKind, Policy, WorkspacePath
from patchshuttle.verification import VerificationRunResult
from patchshuttle.workspace import (
    Workspace,
    WorkspaceInitResult,
    WorkspaceInitStatus,
    discover_workspace,
    init_workspace,
    load_workspace,
)

__all__ = [
    "Action",
    "ActionDisposition",
    "AuditActionResult",
    "AuditRunResult",
    "AuditStatus",
    "Check",
    "ExecutionError",
    "ExecutionErrorCode",
    "FileDisposition",
    "HandoffResult",
    "Job",
    "JobError",
    "JobErrorCode",
    "JobKind",
    "ManualRollbackResult",
    "NewlineStyle",
    "PathKind",
    "PathFingerprint",
    "Plan",
    "PlanDiff",
    "PlannedAction",
    "PlannedCheck",
    "PlannedFileChange",
    "PlanningError",
    "PlanningErrorCode",
    "Policy",
    "PolicyError",
    "PolicyErrorCode",
    "RunResult",
    "RunStatus",
    "SnapshotResult",
    "VerificationRunResult",
    "Workspace",
    "WorkspaceError",
    "WorkspaceErrorCode",
    "WorkspaceInitResult",
    "WorkspaceInitStatus",
    "WorkspacePath",
    "__version__",
    "create_handoff",
    "create_snapshot",
    "discover_workspace",
    "execute_plan",
    "init_workspace",
    "load_job",
    "load_workspace",
    "plan_job",
    "render_plan_diff",
    "rollback_job",
    "validate_job",
]

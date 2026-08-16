"""Contract tests for the installable package surface."""

from importlib.metadata import version
from pathlib import Path

import tomllib

import patchshuttle


def test_public_version_matches_distribution_metadata() -> None:
    assert patchshuttle.__version__ == "0.1.0a2"
    assert version("patchshuttle") == patchshuttle.__version__


def test_build_metadata_stays_compatible_with_release_tooling() -> None:
    configuration = tomllib.loads(Path("pyproject.toml").read_text("utf-8"))
    targets = configuration["tool"]["hatch"]["build"]["targets"]

    assert targets["sdist"]["core-metadata-version"] == "2.4"
    assert targets["wheel"]["core-metadata-version"] == "2.4"


def test_public_exports_match_the_implemented_surface() -> None:
    assert patchshuttle.__all__ == [
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
        "rollback_job",
        "validate_job",
    ]

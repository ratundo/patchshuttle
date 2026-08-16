"""Contract tests for the Phase 12 public patch execution API."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.backup as backup_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job, RunStatus, execute_plan
from patchshuttle.checks import CheckStatus
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.planner import plan_job
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
RUN_TIMESTAMP = "2026_08_07_120000_000001"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    monkeypatch.setattr(backup_module, "_run_timestamp", lambda: RUN_TIMESTAMP)
    return init_workspace(tmp_path).workspace


def patch_plan(workspace: Workspace, actions: list[dict]):
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-012",
        kind="patch",
        actions=actions,
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    return plan_job(job, workspace)


def test_execute_plan_returns_immutable_public_completed_result(
    workspace: Workspace,
) -> None:
    plan = patch_plan(
        workspace,
        [{"create_file": {"path": "notes.txt", "content": "notes\n"}}],
    )

    result = execute_plan(plan, approved=True)

    assert result.status is RunStatus.COMPLETED
    assert result.plan is plan
    assert result.backup_path == (
        workspace.root / "patches/backups/PATCH-012" / RUN_TIMESTAMP
    )
    assert result.created_files == (PurePosixPath("notes.txt"),)
    assert result.created_directories == ()
    assert result.modified_files == ()
    assert [item.status for item in result.initial_checks] == [CheckStatus.PASSED]
    assert result.formatting_results == ()
    assert result.formatted_files == ()
    assert result.final_checks == ()
    assert result.workspace_comparison is not None
    assert result.workspace_comparison.success is True
    assert [
        (change.path, change.kind.value, change.expected)
        for change in result.workspace_comparison.changes
    ] == [(PurePosixPath("notes.txt"), "ADDED", True)]
    assert (workspace.root / "notes.txt").read_bytes() == b"notes\n"
    with pytest.raises(FrozenInstanceError):
        result.status = RunStatus.NO_CHANGE  # type: ignore[misc]


def test_execute_plan_maps_a_revalidated_no_change_without_backup(
    workspace: Workspace,
) -> None:
    target = workspace.root / "notes.txt"
    target.write_text("new\n", encoding="utf-8")
    plan = patch_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "notes.txt",
                    "old": "old",
                    "new": "new",
                }
            }
        ],
    )

    result = execute_plan(plan, approved=True)

    assert result.status is RunStatus.NO_CHANGE
    assert result.backup_path is None
    assert result.created_files == ()
    assert result.created_directories == ()
    assert result.modified_files == ()
    assert result.initial_checks == ()
    assert result.workspace_comparison is None
    assert target.read_text(encoding="utf-8") == "new\n"


def test_execute_plan_requires_explicit_approval_before_project_writes(
    workspace: Workspace,
) -> None:
    plan = patch_plan(
        workspace,
        [{"create_file": {"path": "blocked.txt", "content": "blocked\n"}}],
    )

    with pytest.raises(ExecutionError) as caught:
        execute_plan(plan)

    assert caught.value.code is ExecutionErrorCode.APPROVAL_REQUIRED
    assert not (workspace.root / "blocked.txt").exists()
    assert list((workspace.root / "patches/backups").iterdir()) == []


def test_execute_plan_runs_audit_without_approval_and_approved_verify(
    workspace: Workspace,
) -> None:
    audit = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-012",
        kind="audit",
        actions=[{"tree": {"path": "."}}],
    )
    audit_result = execute_plan(plan_job(audit, workspace))
    assert audit_result.status is RunStatus.COMPLETED
    assert [item.name for item in audit_result.audit_results] == ["tree"]

    verify = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="VERIFY-012",
        kind="verify",
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    verify_result = execute_plan(plan_job(verify, workspace), approved=True)
    assert verify_result.status is RunStatus.COMPLETED
    assert [item.name for item in verify_result.initial_checks] == ["import_check"]
    assert list((workspace.root / "patches/backups").iterdir()) == []

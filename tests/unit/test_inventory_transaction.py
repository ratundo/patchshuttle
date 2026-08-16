"""Contract tests for Phase 13 inventory inside patch transactions."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.backup as backup_module
import patchshuttle.runner as runner_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.inventory import (
    InventoryError,
    InventoryErrorCode,
    WorkspaceChangeKind,
)
from patchshuttle.planner import plan_job
from patchshuttle.runner import TransactionStatus, execute_change_transaction
from patchshuttle.workspace import Workspace, init_workspace, load_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
RUN_TIMESTAMP = "2026_08_07_130000_000001"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    monkeypatch.setattr(backup_module, "_run_timestamp", lambda: RUN_TIMESTAMP)
    return init_workspace(tmp_path).workspace


def configured_profile(workspace: Workspace, code: str) -> Workspace:
    workspace.config_path.write_text(
        workspace.config_path.read_text("utf-8")
        + "\n[checks.profiles.phase13]\n"
        + f'argv = ["{{python}}", "-c", {json.dumps(code)}]\n'
        + "timeout_seconds = 30\n"
        + "allow_job_args = false\n",
        encoding="utf-8",
    )
    return load_workspace(workspace.root)


def patch_plan(workspace: Workspace):
    return plan_job(
        Job(
            protocol=1,
            project_id=PROJECT_ID,
            id="PATCH-013",
            kind="patch",
            actions=[{"create_file": {"path": "planned.txt", "content": "planned\n"}}],
            checks=[{"profile": {"name": "phase13"}}],
        ),
        workspace,
    )


def manifest(workspace: Workspace) -> dict:
    path = (
        workspace.root / "patches/backups/PATCH-013" / RUN_TIMESTAMP / "manifest.json"
    )
    return json.loads(path.read_text("utf-8"))


def test_successful_check_with_unexpected_file_is_reported_and_rolls_back(
    workspace: Workspace,
) -> None:
    workspace = configured_profile(
        workspace,
        "from pathlib import Path; Path('side-effect.txt').write_text('side\\n')",
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(patch_plan(workspace), approved=True)

    error = caught.value
    assert error.code is ExecutionErrorCode.UNEXPECTED_WORKSPACE_CHANGE
    assert error.path == "side-effect.txt"
    assert error.rollback_succeeded is True
    assert error.workspace_comparison is not None
    assert [
        (change.path.as_posix(), change.kind, change.expected)
        for change in error.workspace_comparison.changes
    ] == [("side-effect.txt", WorkspaceChangeKind.ADDED, False)]
    assert not (workspace.root / "planned.txt").exists()
    assert (workspace.root / "side-effect.txt").read_text("utf-8") == "side\n"
    assert manifest(workspace)["failure_code"] == "UNEXPECTED_WORKSPACE_CHANGE"


def test_ignored_check_side_effect_does_not_fail_workspace_comparison(
    workspace: Workspace,
) -> None:
    workspace = configured_profile(
        workspace,
        (
            "from pathlib import Path; "
            "p=Path('.pytest_cache/side.txt'); "
            "p.parent.mkdir(exist_ok=True); p.write_text('ignored\\n')"
        ),
    )

    result = execute_change_transaction(patch_plan(workspace), approved=True)

    assert result.status is TransactionStatus.APPLIED
    assert result.workspace_comparison is not None
    assert result.workspace_comparison.success is True
    assert [
        change.path.as_posix() for change in result.workspace_comparison.changes
    ] == ["planned.txt"]
    assert (workspace.root / ".pytest_cache/side.txt").exists()
    assert (workspace.root / "planned.txt").exists()


def test_failed_check_retains_unexpected_side_effect_in_error_comparison(
    workspace: Workspace,
) -> None:
    workspace = configured_profile(
        workspace,
        (
            "from pathlib import Path; import sys; "
            "Path('failed-side-effect.txt').write_text('side\\n'); sys.exit(9)"
        ),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(patch_plan(workspace), approved=True)

    error = caught.value
    assert error.code is ExecutionErrorCode.CHECK_FAILED
    assert error.rollback_succeeded is True
    assert error.workspace_comparison is not None
    assert [
        change.path.as_posix()
        for change in error.workspace_comparison.unexpected_changes
    ] == ["failed-side-effect.txt"]
    assert not (workspace.root / "planned.txt").exists()
    assert (workspace.root / "failed-side-effect.txt").exists()


def test_baseline_inventory_failure_happens_before_backup_or_project_write(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = configured_profile(workspace, "pass")

    def fail_inventory(*args, **kwargs):
        raise InventoryError(
            InventoryErrorCode.ENTRY_LIMIT_EXCEEDED,
            "limit",
            path=PurePosixPath("too-many"),
        )

    monkeypatch.setattr(runner_module, "capture_inventory", fail_inventory)

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(patch_plan(workspace), approved=True)

    assert caught.value.code is ExecutionErrorCode.WORKSPACE_INVENTORY_FAILED
    assert caught.value.path == "too-many"
    assert caught.value.backup_path is None
    assert not (workspace.root / "planned.txt").exists()
    assert list((workspace.root / "patches/backups").iterdir()) == []


def test_final_inventory_failure_rolls_back_and_preserves_root_error(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = configured_profile(workspace, "pass")
    real_capture = runner_module.capture_inventory
    calls = 0

    def fail_after_baseline(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_capture(*args, **kwargs)
        raise InventoryError(InventoryErrorCode.INSPECTION_FAILED, "failed")

    monkeypatch.setattr(runner_module, "capture_inventory", fail_after_baseline)

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(patch_plan(workspace), approved=True)

    assert calls == 3
    assert caught.value.code is ExecutionErrorCode.WORKSPACE_INVENTORY_FAILED
    assert caught.value.rollback_succeeded is True
    assert caught.value.workspace_comparison is None
    assert not (workspace.root / "planned.txt").exists()
    assert manifest(workspace)["failure_code"] == "WORKSPACE_INVENTORY_FAILED"

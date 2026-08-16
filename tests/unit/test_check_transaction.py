"""Contract tests for Phase 10 initial checks inside patch transactions."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import patchshuttle.backup as backup_module
import patchshuttle.runner as runner_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle.backup import prepare_backup
from patchshuttle.checks import CheckStatus
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.planner import plan_job
from patchshuttle.runner import (
    TransactionStatus,
    execute_change_transaction,
    execute_create_transaction,
)
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
RUN_TIMESTAMP = "2026_08_06_230000_000001"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    monkeypatch.setattr(backup_module, "_run_timestamp", lambda: RUN_TIMESTAMP)
    return init_workspace(tmp_path).workspace


def patch_plan(
    workspace: Workspace,
    *,
    actions: list[dict],
    checks: list[dict],
):
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-010",
        kind="patch",
        actions=actions,
        checks=checks,
    )
    return plan_job(job, workspace)


def manifest(workspace: Workspace) -> dict:
    path = (
        workspace.root / "patches/backups/PATCH-010" / RUN_TIMESTAMP / "manifest.json"
    )
    return json.loads(path.read_text("utf-8"))


def test_successful_initial_check_observes_applied_file_state(
    workspace: Workspace,
) -> None:
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    tests = workspace.root / "tests"
    tests.mkdir()
    (tests / "test_state.py").write_text(
        """\
from pathlib import Path


def test_applied_state():
    root = Path(__file__).parents[1]
    assert (root / "module.py").read_text("utf-8") == "VALUE = 2\\n"
""",
        encoding="utf-8",
    )
    plan = patch_plan(
        workspace,
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
        checks=[{"pytest": {"paths": ["tests/test_state.py"], "args": ["-q"]}}],
    )

    result = execute_change_transaction(plan, approved=True)

    assert result.status is TransactionStatus.APPLIED
    assert len(result.initial_checks) == 1
    assert result.initial_checks[0].status is CheckStatus.PASSED
    assert target.read_text("utf-8") == "VALUE = 2\n"
    assert manifest(workspace)["status"] == "COMPLETED"


def test_failed_initial_check_restores_modified_original(
    workspace: Workspace,
) -> None:
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    plan = patch_plan(
        workspace,
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["patchshuttle_phase10_missing_module"]}}],
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.CHECK_FAILED
    assert caught.value.item_id == "check_001"
    assert caught.value.path == "import_check"
    assert caught.value.rollback_succeeded is True
    assert len(caught.value.check_results) == 1
    assert caught.value.check_results[0].status is CheckStatus.FAILED
    assert target.read_text("utf-8") == "VALUE = 1\n"
    assert manifest(workspace)["status"] == "ROLLED_BACK"
    assert manifest(workspace)["failure_code"] == "CHECK_FAILED"


def test_failed_initial_check_removes_created_file_and_directory(
    workspace: Workspace,
) -> None:
    plan = patch_plan(
        workspace,
        actions=[{"create_file": {"path": "src/new.txt", "content": "new\n"}}],
        checks=[{"import_check": {"modules": ["patchshuttle_phase10_missing_module"]}}],
    )

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.CHECK_FAILED
    assert caught.value.rollback_succeeded is True
    assert not (workspace.root / "src").exists()


def test_check_that_changes_declared_target_is_rejected_and_rolled_back(
    workspace: Workspace,
) -> None:
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    tests = workspace.root / "tests"
    tests.mkdir()
    (tests / "test_mutation.py").write_text(
        """\
from pathlib import Path


def test_mutate_declared_file():
    root = Path(__file__).parents[1]
    (root / "module.py").write_text("MUTATED = True\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    plan = patch_plan(
        workspace,
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
        checks=[{"pytest": {"paths": ["tests/test_mutation.py"], "args": ["-q"]}}],
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.CHECK_FAILED
    assert caught.value.path == "module.py"
    assert caught.value.rollback_succeeded is True
    assert caught.value.check_results[0].status is CheckStatus.PASSED
    assert target.read_text("utf-8") == "VALUE = 1\n"


def test_no_change_transaction_does_not_launch_initial_checks(
    workspace: Workspace,
) -> None:
    target = workspace.root / "same.txt"
    target.write_bytes(b"same\n")
    plan = patch_plan(
        workspace,
        actions=[{"create_file": {"path": "same.txt", "content": "same\n"}}],
        checks=[{"import_check": {"modules": ["patchshuttle_phase10_missing_module"]}}],
    )

    result = execute_change_transaction(plan, approved=True)

    assert result.status is TransactionStatus.NO_CHANGE
    assert result.initial_checks == ()
    assert result.modified_files == ()
    assert result.created_files == ()
    assert result.created_directories == ()
    assert result.backup_path is None
    assert target.read_text("utf-8") == "same\n"
    assert list((workspace.root / "patches/backups").iterdir()) == []


def test_post_check_verification_rejects_backup_without_original_mode(
    workspace: Workspace,
) -> None:
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    plan = patch_plan(
        workspace,
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    backup = prepare_backup(plan)
    incomplete = replace(backup.entries[0], original_mode=None)
    backup = replace(backup, entries=(incomplete,))

    with pytest.raises(ExecutionError) as caught:
        runner_module._verify_transaction_files_after_checks(plan, backup, ())

    assert caught.value.code is ExecutionErrorCode.CHECK_FAILED
    assert caught.value.path == "module.py"

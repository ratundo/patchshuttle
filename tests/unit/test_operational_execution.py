"""Contract tests for recorded patch execution and registry idempotency."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import patchshuttle.backup as backup_module
import patchshuttle.execution as execution_module
import patchshuttle.logging as logging_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job, RunStatus, execute_plan
from patchshuttle.checks import CheckResult, CheckStatus
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.execution import (
    execution_exit_code,
    record_declined_plan,
    resolve_registered_job,
)
from patchshuttle.planner import normalized_job_hash, plan_job
from patchshuttle.registry import load_registry
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
BACKUP_TIMESTAMP = "2026_08_07_123456_000001"
SOURCE = b"""\
protocol: 1
project_id: PSH-8F41C2A73D905E61
id: PATCH-014
kind: patch
actions:
  - create_file:
      path: notes.txt
      content: |
        notes
checks:
  - import_check:
      modules: [json]
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    monkeypatch.setattr(backup_module, "_run_timestamp", lambda: BACKUP_TIMESTAMP)
    monkeypatch.setattr(
        logging_module,
        "_utc_now",
        lambda: logging_module.datetime(
            2026,
            8,
            7,
            12,
            34,
            56,
            tzinfo=logging_module.timezone.utc,
        ),
    )
    return init_workspace(tmp_path).workspace


def source_plan(workspace: Workspace, source: bytes = SOURCE):
    path = workspace.root / "patches/inbox/PATCH-014.psh.yaml"
    path.write_bytes(source)
    from patchshuttle.parser import load_job

    job = load_job(path)
    return path, plan_job(job, workspace)


def test_success_records_exact_source_log_registry_and_public_paths(
    workspace: Workspace,
) -> None:
    source_path, plan = source_plan(workspace)

    result = execute_plan(plan, approved=True, source_path=source_path)

    assert result.status is RunStatus.COMPLETED
    assert result.log_path is not None and result.log_path.is_file()
    assert result.archived_job_path is not None
    assert result.archived_job_path.read_bytes() == SOURCE
    assert result.archived_job_path.parent.name == "applied"
    log = result.log_path.read_text("utf-8")
    assert "=== PATCHSHUTTLE_AI_HANDOFF ===" in log
    assert "result: COMPLETED" in log
    assert "failure_code: NOT_APPLICABLE" in log
    assert "check_id: check_001" in log
    assert "status: PASSED" in log
    assert 'change: {"expected": true, "kind": "ADDED"' in log
    registry = load_registry(workspace)
    record = registry.jobs[plan.job.id]
    assert record.job_hash == plan.job_hash
    assert record.latest_result == "COMPLETED"
    assert record.completed is True
    assert record.run_count == 1
    assert record.backup_reference == (f"patches/backups/PATCH-014/{BACKUP_TIMESTAMP}")
    assert (
        record.archived_job_copy
        == result.archived_job_path.relative_to(workspace.root).as_posix()
    )


def test_execute_plan_applies_replace_symbol_through_change_transaction(
    workspace: Workspace,
) -> None:
    target = workspace.root / "module.py"
    target.write_text("def run():\n    return 1\n", encoding="utf-8")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-REPLACE-SYMBOL-EXECUTION",
        kind="patch",
        actions=[
            {
                "replace_symbol": {
                    "path": "module.py",
                    "symbol": "run",
                    "expected_sha256": (
                        "a40c04aa22c369fa354406d31613f110"
                        "878d800e33ddd428e124b513762b3635"
                    ),
                    "new_content": "def run():\n    return 2\n",
                }
            }
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )
    plan = plan_job(job, workspace)

    result = execute_plan(plan, approved=True)

    assert result.status is RunStatus.COMPLETED
    assert target.read_text(encoding="utf-8") == "def run():\n    return 2\n"
    assert [path.as_posix() for path in result.modified_files] == ["module.py"]
    assert result.backup_path is not None
    assert result.log_path is not None
    log = result.log_path.read_text(encoding="utf-8")
    assert "action_type: replace_symbol" in log
    assert "result: COMPLETED" in log


def test_repeated_completed_plan_returns_already_applied_without_transaction(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path, plan = source_plan(workspace)
    first = execute_plan(plan, approved=True, source_path=source_path)
    assert first.status is RunStatus.COMPLETED

    monkeypatch.setattr(
        execution_module,
        "execute_change_transaction_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("transaction must not repeat")
        ),
    )
    repeated = execute_plan(plan, approved=True, source_path=source_path)

    assert repeated.status is RunStatus.ALREADY_APPLIED
    assert repeated.backup_path is None
    assert repeated.created_files == ()
    assert repeated.log_path is not None
    assert "result: ALREADY_APPLIED" in repeated.log_path.read_text("utf-8")
    assert repeated.archived_job_path is not None
    assert repeated.archived_job_path.read_bytes() == SOURCE
    record = load_registry(workspace).jobs[plan.job.id]
    assert record.latest_result == "ALREADY_APPLIED"
    assert record.run_count == 2


def test_early_registry_resolution_handles_already_applied_and_conflict(
    workspace: Workspace,
) -> None:
    source_path, plan = source_plan(workspace)
    execute_plan(plan, approved=True, source_path=source_path)

    already = resolve_registered_job(
        workspace,
        plan.job,
        source_path=source_path,
    )
    assert already is not None
    assert already.status is RunStatus.ALREADY_APPLIED
    assert already.archived_job_path.read_bytes() == SOURCE

    conflict_job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-014",
        kind="patch",
        actions=[
            {
                "create_file": {
                    "path": "notes.txt",
                    "content": "different\n",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    conflict_source = SOURCE.replace(b"        notes\n", b"        different\n")
    conflict_path = workspace.root / "patches/inbox/PATCH-014-CONFLICT.psh.yaml"
    conflict_path.write_bytes(conflict_source)

    with pytest.raises(ExecutionError) as caught:
        resolve_registered_job(
            workspace,
            conflict_job,
            source_path=conflict_path,
        )

    error = caught.value
    assert error.code is ExecutionErrorCode.PATCH_ID_CONFLICT
    assert error.log_path is not None
    assert error.archived_job_path is not None
    assert error.archived_job_path.parent.name == "failed"
    assert error.archived_job_path.read_bytes() == conflict_source
    assert "failure_stage: JOB" in error.log_path.read_text("utf-8")
    record = load_registry(workspace).jobs[plan.job.id]
    assert record.job_hash == plan.job_hash
    assert record.latest_result == "PATCH_ID_CONFLICT"
    assert record.completed is True
    assert record.run_count == 3


def test_execute_plan_rechecks_and_records_conflict_under_transaction_lock(
    workspace: Workspace,
) -> None:
    source_path, plan = source_plan(workspace)
    execute_plan(plan, approved=True, source_path=source_path)
    conflict_source = SOURCE.replace(
        b"      path: notes.txt",
        b"      path: another.txt",
    )
    conflict_path = workspace.root / "patches/inbox/PATCH-014-OTHER.psh.yaml"
    conflict_path.write_bytes(conflict_source)
    from patchshuttle.parser import load_job

    conflict_plan = plan_job(load_job(conflict_path), workspace)

    with pytest.raises(ExecutionError) as caught:
        execute_plan(
            conflict_plan,
            approved=True,
            source_path=conflict_path,
        )

    assert caught.value.code is ExecutionErrorCode.PATCH_ID_CONFLICT
    assert caught.value.log_path is not None
    assert caught.value.archived_job_path is not None
    assert not (workspace.root / "another.txt").exists()

    declined_conflict = record_declined_plan(
        conflict_plan,
        source_path=conflict_path,
    )
    assert declined_conflict.code is ExecutionErrorCode.PATCH_ID_CONFLICT
    assert declined_conflict.log_path is not None


def test_failed_rolled_back_job_is_recorded_and_same_hash_can_retry(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path, plan = source_plan(workspace)
    original = execution_module.execute_change_transaction_locked
    check = CheckResult(
        id="check_001",
        name="import_check",
        status=CheckStatus.FAILED,
        argv=("python", "--token=top-secret-token"),
        working_directory=workspace.root,
        timeout_seconds=30,
        return_code=1,
        duration_ms=2,
        stdout="token=stdout-secret",
        stderr="Authorization: Bearer stderr-secret",
        stdout_truncated=False,
        stderr_truncated=False,
    )
    failure = ExecutionError(
        ExecutionErrorCode.CHECK_FAILED,
        "injected check failure",
        item_id="check_001",
        backup_path=workspace.root / "patches/backups/PATCH-014/failure",
        rollback_succeeded=True,
        check_results=(check,),
    )
    monkeypatch.setattr(
        execution_module,
        "execute_change_transaction_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_plan(plan, approved=True, source_path=source_path)

    error = caught.value
    assert error is failure
    assert error.log_path is not None
    text = error.log_path.read_text("utf-8")
    assert "result: ROLLED_BACK" in text
    assert "failure_code: CHECK_FAILED" in text
    assert "rollback: SUCCESS" in text
    assert "stdout-secret" not in text
    assert "stderr-secret" not in text
    assert "top-secret-token" not in text
    assert error.archived_job_path is not None
    assert error.archived_job_path.parent.name == "failed"
    failed_record = load_registry(workspace).jobs[plan.job.id]
    assert failed_record.completed is False
    assert failed_record.latest_result == "ROLLED_BACK"

    monkeypatch.setattr(
        execution_module,
        "execute_change_transaction_locked",
        original,
    )
    retried = execute_plan(plan, approved=True, source_path=source_path)
    assert retried.status is RunStatus.COMPLETED
    record = load_registry(workspace).jobs[plan.job.id]
    assert record.completed is True
    assert record.run_count == 2


def test_failed_job_can_keep_changes_and_records_explicit_rollback_state(
    workspace: Workspace,
) -> None:
    failed_source = SOURCE.replace(b"PATCH-014", b"PATCH-KEEP").replace(
        b"modules: [json]",
        b"modules: [patchshuttle_module_that_does_not_exist]",
    )
    source_path, plan = source_plan(workspace, failed_source)

    with pytest.raises(ExecutionError) as caught:
        execute_plan(
            plan,
            approved=True,
            keep_changes=True,
            source_path=source_path,
        )

    error = caught.value
    assert error.code is ExecutionErrorCode.CHECK_FAILED
    assert error.rollback_succeeded is None
    assert error.rollback_skipped is True
    assert error.changes_kept is True
    assert (workspace.root / "notes.txt").read_text("utf-8") == "notes\n"
    assert error.log_path is not None
    log = error.log_path.read_text("utf-8")
    assert "result: CHECK_FAILED" in log
    assert "status: CHANGES_KEPT" in log
    assert "status: SKIPPED_CHANGES_KEPT" in log
    assert "rollback_status: SKIPPED_CHANGES_KEPT" in log
    assert "rollback: SKIPPED_CHANGES_KEPT" in log
    record = load_registry(workspace).jobs[plan.job.id]
    assert record.completed is False
    assert record.rollback_state == "SKIPPED_CHANGES_KEPT"


def test_execute_plan_rejects_keep_changes_for_wrong_kind_and_local_policy(
    workspace: Workspace,
) -> None:
    verify = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="VERIFY-KEEP",
        kind="verify",
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    with pytest.raises(ExecutionError) as wrong_kind:
        execute_plan(plan_job(verify, workspace), approved=True, keep_changes=True)
    assert wrong_kind.value.code is ExecutionErrorCode.JOB_KIND_UNSUPPORTED

    _, plan = source_plan(workspace)
    execution = workspace.config.execution.model_copy(
        update={"allow_keep_changes": False}
    )
    configured = replace(
        workspace,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    restricted_plan = replace(plan, workspace=configured)
    with pytest.raises(ExecutionError) as forbidden:
        execute_plan(restricted_plan, approved=True, keep_changes=True)
    assert forbidden.value.code is ExecutionErrorCode.KEEP_CHANGES_FORBIDDEN
    assert list((workspace.root / "patches/logs").iterdir()) == []


def test_python_api_without_source_file_archives_deterministic_yaml(
    workspace: Workspace,
) -> None:
    _, plan = source_plan(workspace)

    result = execute_plan(plan, approved=True)

    assert result.archived_job_path is not None
    archived = result.archived_job_path.read_text("utf-8")
    assert archived.startswith("protocol: 1\n")
    assert f"project_id: {PROJECT_ID}\n" in archived
    assert "id: PATCH-014\n" in archived


def test_source_file_must_remain_regular_bounded_and_equal_to_job(
    workspace: Workspace,
) -> None:
    source_path, plan = source_plan(workspace)
    source_path.write_bytes(SOURCE.replace(b"notes\n", b"changed\n"))

    with pytest.raises(ExecutionError) as changed:
        execute_plan(plan, approved=True, source_path=source_path)
    assert changed.value.code is ExecutionErrorCode.PLAN_STALE
    assert list((workspace.root / "patches/logs").iterdir()) == []

    source_path.unlink()
    source_path.mkdir()
    with pytest.raises(ExecutionError, match="missing, unsafe, or unreadable"):
        execute_plan(plan, approved=True, source_path=source_path)


def test_source_file_limit_and_reparse_failures_are_reported_as_stale(
    workspace: Workspace,
) -> None:
    source_path, plan = source_plan(workspace)
    execution = workspace.config.execution.model_copy(update={"max_job_bytes": 1})
    limited_workspace = replace(
        workspace,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    limited_plan = replace(plan, workspace=limited_workspace)
    with pytest.raises(ExecutionError, match="exceeds the configured input limit"):
        execute_plan(limited_plan, approved=True, source_path=source_path)

    wrong_extension = workspace.root / "patches/inbox/PATCH-014.yaml"
    wrong_extension.write_bytes(SOURCE)
    with pytest.raises(ExecutionError, match="no longer matches"):
        execute_plan(plan, approved=True, source_path=wrong_extension)


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        (ExecutionErrorCode.OPERATIONAL_RECORD_FAILED, 1),
        (ExecutionErrorCode.APPROVAL_REQUIRED, 4),
        (ExecutionErrorCode.USER_DECLINED, 4),
        (ExecutionErrorCode.KEEP_CHANGES_FORBIDDEN, 4),
        (ExecutionErrorCode.PATCH_ID_CONFLICT, 3),
        (ExecutionErrorCode.WORKSPACE_LOCKED, 3),
        (ExecutionErrorCode.WORKSPACE_LOCK_FAILED, 3),
        (ExecutionErrorCode.LOG_NOT_FOUND, 3),
        (ExecutionErrorCode.JOB_NOT_FOUND, 3),
        (ExecutionErrorCode.CHECK_FAILED, 6),
        (ExecutionErrorCode.FORMAT_FAILED, 7),
        (ExecutionErrorCode.ROLLBACK_FAILED, 8),
        (ExecutionErrorCode.ACTION_FAILED, 5),
    ],
)
def test_execution_exit_code_contract(
    code: ExecutionErrorCode,
    exit_code: int,
) -> None:
    assert execution_exit_code(code) == exit_code


def test_execute_plan_rejects_unapproved_patch_and_runs_approved_verify(
    workspace: Workspace,
) -> None:
    _, plan = source_plan(workspace)
    with pytest.raises(ExecutionError) as unapproved:
        execute_plan(plan)
    assert unapproved.value.code is ExecutionErrorCode.APPROVAL_REQUIRED

    verify = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="VERIFY-014",
        kind="verify",
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    result = execute_plan(plan_job(verify, workspace), approved=True)
    assert result.status is RunStatus.COMPLETED
    assert [item.status for item in result.initial_checks] == [CheckStatus.PASSED]
    assert result.log_path is not None


def test_normalized_job_hash_is_stable_for_equal_models(workspace: Workspace) -> None:
    _, plan = source_plan(workspace)
    assert normalized_job_hash(plan.job.model_copy()) == plan.job_hash


def test_error_result_and_failure_stage_classification(workspace: Workspace) -> None:
    _, plan = source_plan(workspace)
    check = CheckResult(
        id="check_001",
        name="import_check",
        status=CheckStatus.FAILED,
        argv=("python",),
        working_directory=workspace.root,
        timeout_seconds=30,
        return_code=1,
        duration_ms=1,
        stdout="",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
    )

    rolled_back = ExecutionError(
        ExecutionErrorCode.CHECK_FAILED,
        "failed",
        rollback_succeeded=True,
    )
    rollback_failed = ExecutionError(
        ExecutionErrorCode.ROLLBACK_FAILED,
        "failed",
        rollback_succeeded=False,
        cause_code=ExecutionErrorCode.ACTION_FAILED,
    )
    assert execution_module._error_result(rolled_back) == (
        "ROLLED_BACK",
        "CHECK_FAILED",
    )
    assert execution_module._error_result(rollback_failed) == (
        "ROLLBACK_FAILED",
        "ACTION_FAILED",
    )
    assert execution_module._error_result(
        ExecutionError(ExecutionErrorCode.PLAN_STALE, "stale")
    ) == ("PLAN_STALE", "PLAN_STALE")
    assert (
        execution_module._rollback_state(
            ExecutionError(
                ExecutionErrorCode.ACTION_FAILED,
                "kept",
                rollback_skipped=True,
                changes_kept=True,
            )
        )
        == "SKIPPED_CHANGES_KEPT"
    )
    assert (
        execution_module._rollback_state(
            ExecutionError(
                ExecutionErrorCode.ACTION_FAILED,
                "skipped",
                rollback_skipped=True,
            )
        )
        == "SKIPPED_NO_CHANGES"
    )

    cases = (
        (ExecutionErrorCode.WORKSPACE_LOCKED, None, (), "WORKSPACE"),
        (ExecutionErrorCode.PLAN_STALE, None, (), "PLAN"),
        (ExecutionErrorCode.KEEP_CHANGES_FORBIDDEN, None, (), "PLAN"),
        (ExecutionErrorCode.USER_DECLINED, None, (), "PLAN"),
        (ExecutionErrorCode.BACKUP_FAILED, None, (), "BACKUP"),
        (ExecutionErrorCode.ACTION_FAILED, None, (), "ACTIONS"),
        (ExecutionErrorCode.CHECK_FAILED, None, (check,), "INITIAL_CHECKS"),
        (
            ExecutionErrorCode.CHECK_FAILED,
            None,
            (check, check),
            "FINAL_CHECKS",
        ),
        (ExecutionErrorCode.FORMAT_FAILED, "black", (), "FORMAT_BLACK"),
        (ExecutionErrorCode.FORMAT_FAILED, "isort", (), "FORMAT_ISORT"),
        (
            ExecutionErrorCode.WORKSPACE_INVENTORY_FAILED,
            None,
            (),
            "WORKSPACE_COMPARISON",
        ),
        (ExecutionErrorCode.OPERATIONAL_RECORD_FAILED, None, (), "SUMMARY"),
    )
    for code, path, checks, expected in cases:
        error = ExecutionError(code, "failure", path=path, check_results=checks)
        assert execution_module._failure_stage(error, plan) == expected

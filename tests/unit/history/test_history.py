"""Unit tests for compact append-only PatchShuttle history."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import patchshuttle.history.storage as storage_module
import patchshuttle.workspace as workspace_module
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.history import (
    HISTORY_SCHEMA,
    HISTORY_SCHEMA_VERSION,
    HistoryError,
    HistoryErrorCode,
    HistoryRecord,
    build_history_record,
    latest_history_record,
    list_history_records,
    read_history_record,
    try_write_history_record,
    write_history_record,
)
from patchshuttle.logging import ManualRollbackLogRecord, RunClock, RunLogData
from patchshuttle.models import Job
from patchshuttle.planner import plan_job
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
INSTANT = datetime(2026, 8, 27, 12, 34, 56, tzinfo=timezone.utc)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    return init_workspace(tmp_path).workspace


@pytest.fixture
def job() -> Job:
    return Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-HISTORY-001",
        kind="patch",
        title="Record a compact result",
        description="Implement the requested change with token=history-secret",
        actions=[{"create_file": {"path": "created.txt", "content": "value\n"}}],
        checks=[{"import_check": {"modules": ["json"]}}],
    )


def _comparison(*items: tuple[str, str, bool]):
    return SimpleNamespace(
        changes=tuple(
            SimpleNamespace(
                path=PurePosixPath(path),
                kind=SimpleNamespace(value=kind),
                expected=expected,
            )
            for path, kind, expected in items
        )
    )


def _check(
    check_id: str,
    status: str,
    *,
    return_code: int = 0,
    new_warnings: int | None = None,
    details: tuple[str, ...] = (),
):
    return SimpleNamespace(
        id=check_id,
        name="pytest",
        status=SimpleNamespace(value=status),
        return_code=return_code,
        duration_ms=25,
        warning_analysis=("COMPARED" if new_warnings is not None else None),
        known_warnings=0 if new_warnings is not None else None,
        new_warnings=new_warnings,
        new_warning_details=details,
    )


def _data(
    workspace: Workspace,
    job: Job,
    *,
    result: str = "COMPLETED",
    exit_code: int = 0,
    plan=None,
    error: ExecutionError | None = None,
    verification_checks: tuple = (),
    workspace_comparison=None,
    manual_rollback: ManualRollbackLogRecord | None = None,
) -> RunLogData:
    return RunLogData(
        workspace=workspace,
        job=job,
        job_hash="a" * 64,
        clock=RunClock(INSTANT),
        result=result,
        exit_code=exit_code,
        failure_stage=("FINAL_CHECKS" if error is not None else None),
        failure_code=(
            (error.cause_code or error.code).value if error is not None else None
        ),
        archived_job_path=workspace.root / "patches/applied/source.psh.yaml",
        plan=plan,
        error=error,
        verification_checks=verification_checks,
        workspace_comparison=workspace_comparison,
        manual_rollback=manual_rollback,
    )


def test_success_history_is_versioned_redacted_compact_and_round_trips(
    workspace: Workspace,
    job: Job,
) -> None:
    plan = plan_job(job, workspace)
    checks = (
        _check("check_001", "PASSED", new_warnings=0),
        _check(
            "check_002",
            "PASSED",
            new_warnings=1,
            details=("one bounded warning",),
        ),
    )
    record = build_history_record(
        _data(
            workspace,
            job,
            plan=plan,
            verification_checks=checks,
            workspace_comparison=_comparison(
                ("created.txt", "ADDED", True),
                ("unexpected.txt", "MODIFIED", False),
            ),
        ),
        log_path=workspace.root / "patches/logs/run.log",
        record_id="PATCH-HISTORY-001/2026_08_27_12_34_56_aaaaaaaa",
    )

    assert record.schema_name == HISTORY_SCHEMA
    assert record.schema_version == HISTORY_SCHEMA_VERSION == 1
    assert record.patchshuttle_version
    assert record.project_id == PROJECT_ID
    assert record.job.id == job.id
    assert record.declared.title == "Record a compact result"
    assert record.declared.intent is not None
    assert record.declared.intent.source == "job.description"
    assert "history-secret" not in record.declared.intent.text
    assert "[REDACTED]" in record.declared.intent.text
    assert record.observed.status == "COMPLETED"
    assert record.observed.files.created == ("created.txt",)
    assert record.observed.files.modified == ("unexpected.txt",)
    assert len(record.observed.checks) == 2
    assert len(record.observed.warnings) == 1
    assert record.references.detailed_log == "patches/logs/run.log"
    assert record.references.ai_log.kind == "derived_view"
    assert record.references.ai_log.persistent is False
    assert record.relationships is None

    serialized = record.model_dump_json(by_alias=True)
    assert json.loads(serialized)["schema"] == HISTORY_SCHEMA
    assert HistoryRecord.model_validate_json(serialized) == record
    assert len(serialized.encode("utf-8")) < 20_000


def test_history_models_import_without_warnings() -> None:
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", "import patchshuttle.history.models"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_declared_intent_is_bounded_and_symbols_require_observed_file_change(
    workspace: Workspace,
) -> None:
    symbol_job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-HISTORY-SYMBOL",
        kind="patch",
        description="x" * 9_000,
        actions=[
            {
                "replace_symbol": {
                    "path": "module.py",
                    "symbol": "Service.run",
                    "expected_sha256": "b" * 64,
                    "new_content": "def run(self):\n    return 1\n",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    record = build_history_record(
        _data(
            workspace,
            symbol_job,
            workspace_comparison=_comparison(("module.py", "MODIFIED", True)),
        ),
        log_path=workspace.root / "patches/logs/symbol.log",
        record_id="PATCH-HISTORY-SYMBOL/2026_08_27_12_34_56_aaaaaaaa",
    )

    assert record.declared.intent is not None
    assert record.declared.intent.truncated is True
    assert len(record.declared.intent.text.encode("utf-8")) == 8_192
    assert record.declared.symbol_targets[0].symbol == "Service.run"
    assert record.observed.affected_symbols == record.declared.symbol_targets


def test_failed_history_records_checks_failure_and_successful_rollback(
    workspace: Workspace,
    job: Job,
) -> None:
    initial = _check("check_001", "PASSED")
    final = _check("check_001", "FAILED", return_code=1)
    error = ExecutionError(
        ExecutionErrorCode.CHECK_FAILED,
        "final tests failed",
        item_id="check_001",
        backup_path=workspace.root / "patches/backups/PATCH-HISTORY-001/run",
        rollback_succeeded=True,
        check_results=(initial, final),
        formatting_results=(SimpleNamespace(),),
    )
    record = build_history_record(
        _data(
            workspace,
            job,
            result="ROLLED_BACK",
            exit_code=6,
            plan=plan_job(job, workspace),
            error=error,
        ),
        log_path=workspace.root / "patches/logs/failed.log",
        record_id="PATCH-HISTORY-001/2026_08_27_12_34_56_aaaaaaaa",
    )

    assert [item.phase for item in record.observed.checks] == ["initial", "final"]
    assert [item.status for item in record.observed.checks] == ["PASSED", "FAILED"]
    assert record.observed.failure is not None
    assert record.observed.failure.cause_code == "CHECK_FAILED"
    assert record.observed.failure.item_id == "check_001"
    assert record.observed.rollback.status == "SUCCESS"
    assert record.references.backup is not None


def test_manual_rollback_tracks_restored_removed_and_unresolved_files(
    workspace: Workspace,
    job: Job,
) -> None:
    rollback = ManualRollbackLogRecord(
        status="FAILED",
        backup_path=workspace.root / "patches/backups/PATCH-HISTORY-001/run",
        restored_files=(PurePosixPath("modified.py"),),
        removed_files=(PurePosixPath("created.py"),),
        removed_directories=(PurePosixPath("new_package"),),
        unresolved=(PurePosixPath("locked.py"),),
    )
    record = build_history_record(
        _data(
            workspace,
            job,
            result="ROLLBACK_FAILED",
            exit_code=8,
            manual_rollback=rollback,
        ),
        log_path=workspace.root / "patches/logs/rollback.log",
        record_id="PATCH-HISTORY-001/2026_08_27_12_34_56_aaaaaaaa",
    )

    assert record.observed.files.modified == ("modified.py",)
    assert record.observed.files.deleted == ("created.py",)
    assert record.observed.rollback.cause == "USER_REQUESTED"
    assert record.observed.rollback.unresolved == ("locked.py",)


def test_storage_is_append_only_readable_filterable_and_old_workspace_compatible(
    workspace: Workspace,
    job: Job,
) -> None:
    assert list_history_records(workspace).records == ()
    data = _data(workspace, job)
    log_path = workspace.root / "patches/logs/run.log"

    first = write_history_record(data, log_path=log_path)
    second = write_history_record(data, log_path=log_path)
    other_job = job.model_copy(update={"id": "PATCH-HISTORY-OTHER"})
    third = write_history_record(_data(workspace, other_job), log_path=log_path)

    assert first != second
    assert first.read_bytes() != b""
    assert second.stem.endswith("_2")
    assert third.parent.name == "PATCH-HISTORY-OTHER"
    first_record = HistoryRecord.model_validate_json(first.read_bytes())
    assert read_history_record(workspace, first_record.record_id) == first_record
    assert latest_history_record(workspace).job.id == "PATCH-HISTORY-OTHER"
    filtered = list_history_records(workspace, job_id=job.id, limit=1)
    assert len(filtered.records) == 1
    assert filtered.limited is True
    assert filtered.records[0].job.id == job.id


def test_reader_rejects_invalid_schema_identity_and_reference(
    workspace: Workspace,
    job: Job,
) -> None:
    path = write_history_record(
        _data(workspace, job),
        log_path=workspace.root / "patches/logs/run.log",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HistoryError) as invalid:
        read_history_record(workspace, f"{job.id}/{path.stem}")
    assert invalid.value.code is HistoryErrorCode.HISTORY_INVALID
    with pytest.raises(HistoryError) as traversal:
        read_history_record(workspace, "../record")
    assert traversal.value.code is HistoryErrorCode.HISTORY_INVALID


def test_history_write_failure_is_bounded_and_non_fatal(
    workspace: Workspace,
    job: Job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        storage_module,
        "_write_new_file",
        lambda *args: (_ for _ in ()).throw(OSError("injected history failure")),
    )

    result = try_write_history_record(
        _data(workspace, job),
        log_path=workspace.root / "patches/logs/run.log",
    )

    assert result.path is None
    assert result.warning is not None
    assert "injected history failure" in result.warning
    assert len(result.warning.encode("utf-8")) < 2_200


def test_history_list_limit_is_validated(
    workspace: Workspace,
) -> None:
    with pytest.raises(HistoryError) as caught:
        list_history_records(workspace, limit=0)
    assert caught.value.code is HistoryErrorCode.HISTORY_LIMIT_EXCEEDED

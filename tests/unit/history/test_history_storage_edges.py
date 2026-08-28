"""Edge-case coverage for bounded structured history storage."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import patchshuttle.history.records as records_module
import patchshuttle.history.storage as storage_module
import patchshuttle.workspace as workspace_module
from patchshuttle.history import (
    HistoryError,
    HistoryErrorCode,
    HistoryRecord,
    latest_history_record,
    list_history_records,
    read_history_record,
    write_history_record,
)
from patchshuttle.logging import RunClock, RunLogData
from patchshuttle.models import Job
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
INSTANT = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)


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
        id="PATCH-HISTORY-EDGE",
        kind="patch",
        title="Exercise storage boundaries",
        actions=[{"create_file": {"path": "created.txt", "content": "value\n"}}],
    )


def _data(workspace: Workspace, job: Job) -> RunLogData:
    return RunLogData(
        workspace=workspace,
        job=job,
        job_hash="e" * 64,
        clock=RunClock(INSTANT),
        result="COMPLETED",
        exit_code=0,
        failure_stage=None,
        failure_code=None,
        archived_job_path=workspace.root / "patches/applied/source.psh.yaml",
    )


def test_optional_integer_and_reference_validation(
    workspace: Workspace,
) -> None:
    assert records_module._optional_int(7) == 7
    assert records_module._optional_int(True) is None
    assert records_module._optional_int("7") is None

    references = (
        r"PATCH-HISTORY-EDGE\record",
        "PATCH-HISTORY-EDGE",
        "PATCH-HISTORY-EDGE/bad.name",
    )
    for reference in references:
        with pytest.raises(HistoryError) as caught:
            read_history_record(workspace, reference)
        assert caught.value.code is HistoryErrorCode.HISTORY_INVALID


def test_missing_history_and_oversized_write_are_reported(
    workspace: Workspace,
    job: Job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(HistoryError) as missing:
        read_history_record(workspace, "PATCH-HISTORY-MISSING/record")
    assert missing.value.code is HistoryErrorCode.HISTORY_NOT_FOUND

    with monkeypatch.context() as bounded:
        bounded.setattr(storage_module, "_MAX_HISTORY_BYTES", 1)
        with pytest.raises(HistoryError) as oversized:
            write_history_record(
                _data(workspace, job),
                log_path=workspace.root / "patches/logs/run.log",
            )
    assert oversized.value.code is HistoryErrorCode.HISTORY_WRITE_FAILED

    with pytest.raises(HistoryError) as latest:
        latest_history_record(workspace)
    assert latest.value.code is HistoryErrorCode.HISTORY_NOT_FOUND


def test_history_scan_rejects_io_errors_and_excess_records(
    workspace: Workspace,
    job: Job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = workspace.patches_dir / "history"
    root.mkdir()

    with pytest.raises(HistoryError) as missing_job:
        read_history_record(workspace, f"{job.id}/missing")
    assert missing_job.value.code is HistoryErrorCode.HISTORY_NOT_FOUND

    original_iterdir = Path.iterdir

    def fail_iterdir(path: Path):
        if path == root:
            raise OSError("injected root scan failure")
        return original_iterdir(path)

    with monkeypatch.context() as failing_root:
        failing_root.setattr(Path, "iterdir", fail_iterdir)
        with pytest.raises(HistoryError) as root_error:
            list_history_records(workspace)
    assert root_error.value.code is HistoryErrorCode.HISTORY_READ_FAILED

    (root / "not-a-job").mkdir()
    assert list_history_records(workspace).records == ()

    directory = root / job.id
    directory.mkdir()
    original_glob = Path.glob

    def fail_glob(path: Path, pattern: str):
        if path == directory:
            raise OSError("injected job scan failure")
        return original_glob(path, pattern)

    with monkeypatch.context() as failing_job:
        failing_job.setattr(Path, "glob", fail_glob)
        with pytest.raises(HistoryError) as job_error:
            list_history_records(workspace, job_id=job.id)
    assert job_error.value.code is HistoryErrorCode.HISTORY_READ_FAILED

    (directory / "record.json").write_text("{}", encoding="utf-8")
    with monkeypatch.context() as bounded_scan:
        bounded_scan.setattr(storage_module, "_MAX_HISTORY_RECORDS", 0)
        with pytest.raises(HistoryError) as too_many:
            list_history_records(workspace, job_id=job.id)
    assert too_many.value.code is HistoryErrorCode.HISTORY_LIMIT_EXCEEDED


def test_history_directory_guards_report_all_failure_modes(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = workspace.patches_dir / "history"
    root.mkdir()
    storage_module._create_directory(root, workspace)

    def fail_mkdir(*args, **kwargs):
        raise OSError("injected mkdir failure")

    with monkeypatch.context() as failing_create:
        failing_create.setattr(storage_module.os, "mkdir", fail_mkdir)
        with pytest.raises(HistoryError) as create_error:
            storage_module._create_directory(root / "new", workspace)
    assert create_error.value.code is HistoryErrorCode.HISTORY_WRITE_FAILED

    broken = root / "broken"
    original_lstat = Path.lstat

    def fail_lstat(path: Path):
        if path == broken:
            raise OSError("injected lstat failure")
        return original_lstat(path)

    with monkeypatch.context() as failing_inspection:
        failing_inspection.setattr(Path, "lstat", fail_lstat)
        with pytest.raises(HistoryError) as inspect_error:
            storage_module._require_directory(broken, workspace)
    assert inspect_error.value.code is HistoryErrorCode.HISTORY_READ_FAILED

    regular_file = root / "regular-file"
    regular_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(HistoryError) as invalid_file:
        storage_module._require_directory(regular_file, workspace)
    assert invalid_file.value.code is HistoryErrorCode.HISTORY_INVALID

    real_directory = root / "real-directory"
    real_directory.mkdir()
    with monkeypatch.context() as simulated_link:
        simulated_link.setattr(storage_module.stat, "S_ISDIR", lambda mode: True)
        simulated_link.setattr(storage_module.stat, "S_ISLNK", lambda mode: True)
        with pytest.raises(HistoryError) as invalid_link:
            storage_module._require_directory(real_directory, workspace)
    assert invalid_link.value.code is HistoryErrorCode.HISTORY_INVALID


def test_exclusive_write_cleans_up_or_preserves_the_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_target = tmp_path / "open-failure.json"

    def fail_open(*args, **kwargs):
        raise OSError("injected open failure")

    with monkeypatch.context() as failing_open:
        failing_open.setattr(storage_module.os, "open", fail_open)
        with pytest.raises(HistoryError) as open_error:
            storage_module._write_new_file(open_target, b"{}")
    assert open_error.value.code is HistoryErrorCode.HISTORY_WRITE_FAILED
    assert not open_target.exists()

    cleanup_target = tmp_path / "cleanup.json"

    def fail_fsync(descriptor: int) -> None:
        raise OSError("injected fsync failure")

    with monkeypatch.context() as failing_sync:
        failing_sync.setattr(storage_module.os, "fsync", fail_fsync)
        with pytest.raises(HistoryError) as cleanup_error:
            storage_module._write_new_file(cleanup_target, b"{}")
    assert cleanup_error.value.code is HistoryErrorCode.HISTORY_WRITE_FAILED
    assert not cleanup_target.exists()

    unlink_target = tmp_path / "unlink-failure.json"
    original_unlink = Path.unlink

    def fail_unlink(path: Path, *args, **kwargs):
        if path == unlink_target:
            raise OSError("injected unlink failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as failing_cleanup:
        failing_cleanup.setattr(storage_module.os, "fsync", fail_fsync)
        failing_cleanup.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(HistoryError) as unlink_error:
            storage_module._write_new_file(unlink_target, b"{}")
    assert unlink_error.value.code is HistoryErrorCode.HISTORY_WRITE_FAILED
    assert unlink_target.exists()
    unlink_target.unlink()


def test_history_loader_enforces_size_type_identity_and_relative_paths(
    workspace: Workspace,
    job: Job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_history_record(
        _data(workspace, job),
        log_path=workspace.root / "patches/logs/run.log",
    )
    record = HistoryRecord.model_validate_json(path.read_bytes())

    with monkeypatch.context() as bounded_read:
        bounded_read.setattr(storage_module, "_MAX_HISTORY_BYTES", 1)
        with pytest.raises(HistoryError) as oversized:
            storage_module._load_history_path(
                workspace,
                path,
                expected_reference=record.record_id,
            )
    assert oversized.value.code is HistoryErrorCode.HISTORY_LIMIT_EXCEEDED

    with pytest.raises(HistoryError) as non_regular:
        storage_module._load_history_path(
            workspace,
            path.parent,
            expected_reference=record.record_id,
        )
    assert non_regular.value.code is HistoryErrorCode.HISTORY_READ_FAILED

    with pytest.raises(HistoryError) as wrong_identity:
        storage_module._load_history_path(
            workspace,
            path,
            expected_reference=f"{job.id}/different-record",
        )
    assert wrong_identity.value.code is HistoryErrorCode.HISTORY_INVALID

    assert storage_module._relative(workspace, path).startswith("patches/history/")
    outside = workspace.root.parent / "outside-history.json"
    assert storage_module._relative(workspace, outside) == outside.as_posix()
    assert records_module._relative(workspace, outside) == outside.as_posix()
    assert storage_module._bounded_text("é", 1) == ("", True)

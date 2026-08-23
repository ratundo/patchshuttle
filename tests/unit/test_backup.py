"""Unit tests for guarded backup manifest preparation."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

import patchshuttle.backup as backup_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle.backup import (
    BackupEntry,
    BackupEntryKind,
    BackupStatus,
    OriginalState,
    prepare_backup,
    update_backup,
)
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.planner import Plan, plan_job
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
RUN_TIMESTAMP = "2026_08_06_190000_000001"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    monkeypatch.setattr(backup_module, "_run_timestamp", lambda: RUN_TIMESTAMP)
    return init_workspace(tmp_path).workspace


def create_plan(workspace: Workspace, *, python: bool = False) -> Plan:
    path = "src/example.py" if python else "src/example.txt"
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-001",
        kind="patch",
        actions=[{"create_file": {"path": path, "content": "VALUE = 1\n"}}],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )
    return plan_job(job, workspace)


def modify_plan(workspace: Workspace) -> Plan:
    (workspace.root / "existing.txt").write_bytes(b"before\n")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-001",
        kind="patch",
        actions=[
            {
                "replace_exact": {
                    "path": "existing.txt",
                    "old": "before",
                    "new": "after",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )
    return plan_job(job, workspace)


def test_prepare_and_update_backup_manifest_with_formatting_scope(
    workspace: Workspace,
) -> None:
    plan = create_plan(workspace, python=True)

    backup = prepare_backup(plan)

    assert backup.manifest_path == backup.path / "manifest.json"
    prepared = json.loads(backup.manifest_path.read_text("utf-8"))
    assert prepared["status"] == "PREPARED"
    assert prepared["formatting_targets"] == ["src/example.py"]
    assert prepared["formatter_plan"] == [
        {
            "path": "src/example.py",
            "formatter": "isort",
            "decision": "RUN",
            "baseline": "NOT_APPLICABLE",
            "planned": "PASS",
        },
        {
            "path": "src/example.py",
            "formatter": "black",
            "decision": "RUN",
            "baseline": "NOT_APPLICABLE",
            "planned": "PASS",
        },
    ]
    assert prepared["html_lint_targets"] == []

    update_backup(
        backup,
        BackupStatus.ROLLED_BACK,
        failure_code=ExecutionErrorCode.ACTION_FAILED,
    )
    updated = json.loads(backup.manifest_path.read_text("utf-8"))
    assert updated["status"] == "ROLLED_BACK"
    assert updated["failure_code"] == "ACTION_FAILED"


def test_prepare_backup_rejects_run_directory_collision(
    workspace: Workspace,
) -> None:
    plan = create_plan(workspace)
    collision = workspace.root / "patches/backups/PATCH-001" / RUN_TIMESTAMP
    collision.mkdir(parents=True)

    with pytest.raises(ExecutionError) as caught:
        prepare_backup(plan)

    assert caught.value.code is ExecutionErrorCode.BACKUP_FAILED
    assert caught.value.path == ("patches/backups/PATCH-001/2026_08_06_190000_000001")


def test_prepare_backup_rejects_symlinked_internal_directory(
    workspace: Workspace,
    tmp_path: Path,
) -> None:
    backups = workspace.root / "patches/backups"
    backups.rmdir()
    try:
        backups.symlink_to(tmp_path / "outside", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    plan = create_plan(workspace)

    with pytest.raises(ExecutionError) as caught:
        prepare_backup(plan)

    assert caught.value.code is ExecutionErrorCode.BACKUP_FAILED


def test_prepare_backup_removes_empty_run_directory_after_manifest_failure(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(workspace)
    expected = workspace.root / "patches/backups/PATCH-001" / RUN_TIMESTAMP

    def fail_update(*args, **kwargs):
        raise ExecutionError(
            ExecutionErrorCode.BACKUP_FAILED,
            "injected manifest failure",
        )

    monkeypatch.setattr(backup_module, "update_backup", fail_update)

    with pytest.raises(ExecutionError, match="injected manifest failure"):
        prepare_backup(plan)

    assert not expected.exists()


def test_prepare_backup_preserves_nonempty_run_directory_after_failure(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(workspace)
    expected = workspace.root / "patches/backups/PATCH-001" / RUN_TIMESTAMP

    def fail_with_marker(backup, *args, **kwargs):
        (backup.path / "marker").write_text("diagnostic\n", encoding="utf-8")
        raise ExecutionError(
            ExecutionErrorCode.BACKUP_FAILED,
            "injected manifest failure",
        )

    monkeypatch.setattr(backup_module, "update_backup", fail_with_marker)

    with pytest.raises(ExecutionError, match="injected manifest failure"):
        prepare_backup(plan)

    assert (expected / "marker").read_text("utf-8") == "diagnostic\n"


def test_update_backup_maps_atomic_replace_failure_and_removes_temp(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = prepare_backup(create_plan(workspace))

    monkeypatch.setattr(
        backup_module.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(ExecutionError) as caught:
        update_backup(backup, BackupStatus.COMPLETED)

    assert caught.value.code is ExecutionErrorCode.BACKUP_FAILED
    assert caught.value.backup_path == backup.path
    assert caught.value.path.endswith("manifest.json")
    assert not list(backup.path.glob(".manifest-*.tmp"))


def test_update_backup_does_not_mask_write_error_when_temp_cleanup_fails(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = prepare_backup(create_plan(workspace))
    real_unlink = Path.unlink

    monkeypatch.setattr(
        backup_module.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda path, *args, **kwargs: (_ for _ in ()).throw(
            PermissionError("cleanup failed")
        ),
    )

    with pytest.raises(ExecutionError, match="manifest could not be written"):
        update_backup(backup, BackupStatus.COMPLETED)

    temporary = next(backup.path.glob(".manifest-*.tmp"))
    real_unlink(temporary)


def test_timestamp_and_outside_relative_display_helpers(tmp_path: Path) -> None:
    timestamp = backup_module._run_timestamp()

    assert re.fullmatch(r"\d{4}_\d{2}_\d{2}_\d{6}_\d{6}", timestamp)
    assert backup_module._relative_display(
        tmp_path / "root",
        tmp_path / "outside",
    ) == str(tmp_path / "outside")


def test_backup_entry_lookup_and_present_payload_without_copy_path(
    workspace: Workspace,
) -> None:
    backup = prepare_backup(create_plan(workspace))

    assert backup.entry_for(backup.entries[0].path) == backup.entries[0]
    with pytest.raises(KeyError):
        backup.entry_for(Path("missing.txt"))  # type: ignore[arg-type]

    payload = backup_module._entry_payload(
        BackupEntry(
            path=backup.entries[0].path,
            kind=BackupEntryKind.FILE,
            original_state=OriginalState.PRESENT,
        )
    )
    assert payload["backup_path"] is None


def test_prepare_backup_rejects_modified_change_without_fingerprint(
    workspace: Workspace,
) -> None:
    plan = modify_plan(workspace)
    incomplete = replace(
        plan.file_changes[0],
        before_sha256=None,
        before_size=None,
    )
    plan = replace(plan, file_changes=(incomplete,))

    with pytest.raises(ExecutionError) as caught:
        prepare_backup(plan)

    assert caught.value.code is ExecutionErrorCode.BACKUP_FAILED
    assert caught.value.path == "existing.txt"


def test_prepare_backup_reports_modified_target_type_change_as_stale(
    workspace: Workspace,
) -> None:
    plan = modify_plan(workspace)
    target = workspace.root / "existing.txt"
    target.unlink()
    target.mkdir()

    with pytest.raises(ExecutionError) as caught:
        prepare_backup(plan)

    assert caught.value.code is ExecutionErrorCode.PLAN_STALE
    assert caught.value.path == "existing.txt"


def test_prepare_backup_maps_original_read_error(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = modify_plan(workspace)
    target = workspace.root / "existing.txt"
    real_read_bytes = Path.read_bytes

    def fail_target_read(path: Path) -> bytes:
        if path == target:
            raise OSError("read failed")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_target_read)

    with pytest.raises(ExecutionError) as caught:
        prepare_backup(plan)

    assert caught.value.code is ExecutionErrorCode.BACKUP_FAILED
    assert caught.value.path == "existing.txt"


def test_prepare_backup_removes_partial_original_after_write_error(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = modify_plan(workspace)
    original = (
        workspace.root
        / "patches/backups/PATCH-001"
        / RUN_TIMESTAMP
        / "originals/existing.txt"
    )
    monkeypatch.setattr(
        backup_module.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("sync failed")),
    )

    with pytest.raises(ExecutionError) as caught:
        prepare_backup(plan)

    assert caught.value.code is ExecutionErrorCode.BACKUP_FAILED
    assert not original.exists()


def test_prepare_backup_preserves_partial_original_when_cleanup_fails(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = modify_plan(workspace)
    original = (
        workspace.root
        / "patches/backups/PATCH-001"
        / RUN_TIMESTAMP
        / "originals/existing.txt"
    )
    real_unlink = Path.unlink
    monkeypatch.setattr(
        backup_module.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("sync failed")),
    )

    def refuse_original_cleanup(path: Path, *args, **kwargs) -> None:
        if path == original:
            raise PermissionError("cleanup failed")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_original_cleanup)

    with pytest.raises(ExecutionError) as caught:
        prepare_backup(plan)

    assert caught.value.code is ExecutionErrorCode.BACKUP_FAILED
    assert original.exists()
    real_unlink(original)

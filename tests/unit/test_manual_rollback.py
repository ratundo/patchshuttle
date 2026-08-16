"""Manual completed-job rollback contracts."""

from __future__ import annotations

import copy
import json
import stat
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
import yaml

import patchshuttle.backup as backup_module
import patchshuttle.operations as operations_module
import patchshuttle.rollback as rollback_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job, RunStatus, execute_plan
from patchshuttle.backup import BackupStatus, load_completed_backup
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.operations import rollback_job
from patchshuttle.planner import plan_job
from patchshuttle.registry import load_registry
from patchshuttle.rollback import RollbackResult
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
RUN_TIMESTAMP = "2026_08_13_230000_000001"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    monkeypatch.setattr(backup_module, "_run_timestamp", lambda: RUN_TIMESTAMP)
    return init_workspace(tmp_path).workspace


def _job(workspace: Workspace, *, job_id: str = "PATCH-ROLLBACK") -> Job:
    return Job(
        protocol=1,
        project_id=workspace.project_id,
        id=job_id,
        kind="patch",
        actions=[
            {
                "replace_exact": {
                    "path": "existing.txt",
                    "old": "before",
                    "new": "after",
                }
            },
            {"create_file": {"path": "created/new.txt", "content": "new\n"}},
        ],
        checks=[{"import_check": {"modules": ["json"]}}],
    )


def test_manual_rollback_restores_completed_job_and_allows_reapply(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "existing.txt"
    target.write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    applied = execute_plan(plan_job(job, workspace), approved=True)
    assert applied.status is RunStatus.COMPLETED
    assert applied.backup_path is not None
    manifest_path = applied.backup_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assert manifest["status"] == "COMPLETED"
    assert set(manifest["applied_states"]) == {
        "created",
        "created/new.txt",
        "existing.txt",
    }

    rolled_back = rollback_job(workspace, job.id, approved=True)

    assert rolled_back.restored_files == (PurePosixPath("existing.txt"),)
    assert rolled_back.removed_files == (PurePosixPath("created/new.txt"),)
    assert rolled_back.removed_directories == (PurePosixPath("created"),)
    assert target.read_text("utf-8") == "before\n"
    assert not (workspace.root / "created").exists()
    assert json.loads(manifest_path.read_text("utf-8"))["status"] == "ROLLED_BACK"
    record = load_registry(workspace).jobs[job.id]
    assert record.latest_result == "ROLLED_BACK"
    assert record.rollback_state == "SUCCESS"
    assert record.completed is False
    log = rolled_back.log_path.read_text("utf-8")
    assert "cause: USER_REQUESTED" in log
    assert 'restored_files: ["existing.txt"]' in log

    monkeypatch.setattr(
        backup_module,
        "_run_timestamp",
        lambda: "2026_08_13_230001_000002",
    )
    reapplied = execute_plan(plan_job(job, workspace), approved=True)
    assert reapplied.status is RunStatus.COMPLETED
    assert target.read_text("utf-8") == "after\n"


def test_manual_rollback_finds_original_archive_after_id_conflict(
    workspace: Workspace,
) -> None:
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    execute_plan(plan_job(job, workspace), approved=True)
    conflict = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id=job.id,
        kind="patch",
        actions=[
            {
                "create_file": {
                    "path": "conflict.txt",
                    "content": "conflict\n",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    with pytest.raises(ExecutionError) as caught:
        execute_plan(plan_job(conflict, workspace), approved=True)
    assert caught.value.code is ExecutionErrorCode.PATCH_ID_CONFLICT
    record = load_registry(workspace).jobs[job.id]
    assert "/failed/" in f"/{record.archived_job_copy}"

    result = rollback_job(workspace, job.id, approved=True)

    assert result.restored_files == (PurePosixPath("existing.txt"),)
    assert (workspace.root / "existing.txt").read_text("utf-8") == "before\n"


@pytest.mark.parametrize("change", ["file", "foreign"])
def test_manual_rollback_refuses_post_run_changes_without_deleting_them(
    workspace: Workspace,
    change: str,
) -> None:
    target = workspace.root / "existing.txt"
    target.write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    execute_plan(plan_job(job, workspace), approved=True)
    if change == "file":
        target.write_text("user edit\n", encoding="utf-8")
        expected_path = "existing.txt"
    else:
        (workspace.root / "created/foreign.txt").write_text(
            "keep\n",
            encoding="utf-8",
        )
        expected_path = "created/foreign.txt"

    with pytest.raises(ExecutionError) as caught:
        rollback_job(workspace, job.id, approved=True)

    assert caught.value.code is ExecutionErrorCode.ROLLBACK_FAILED
    assert caught.value.path == expected_path
    assert caught.value.rollback_succeeded is False
    assert caught.value.log_path is not None
    assert (workspace.root / "created/new.txt").read_text("utf-8") == "new\n"
    if change == "file":
        assert target.read_text("utf-8") == "user edit\n"
    else:
        assert (workspace.root / expected_path).read_text("utf-8") == "keep\n"


def test_manual_rollback_reports_partial_engine_failure(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    result = execute_plan(plan_job(job, workspace), approved=True)
    assert result.backup_path is not None
    monkeypatch.setattr(
        operations_module,
        "rollback_completed_backup",
        lambda *args, **kwargs: RollbackResult(
            removed_files=(),
            removed_directories=(),
            unresolved=(PurePosixPath("existing.txt"),),
        ),
    )

    with pytest.raises(ExecutionError) as caught:
        rollback_job(workspace, job.id, approved=True)

    assert caught.value.code is ExecutionErrorCode.ROLLBACK_FAILED
    assert caught.value.path == "existing.txt"
    manifest = json.loads((result.backup_path / "manifest.json").read_text("utf-8"))
    assert manifest["status"] == BackupStatus.ROLLBACK_FAILED.value
    assert load_registry(workspace).jobs[job.id].completed is True


def test_manual_rollback_requires_authority_patch_and_completed_backup(
    workspace: Workspace,
) -> None:
    with pytest.raises(ExecutionError) as approval:
        rollback_job(workspace, "PATCH-MISSING")
    assert approval.value.code is ExecutionErrorCode.APPROVAL_REQUIRED

    audit = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-ROLLBACK",
        kind="audit",
        actions=[{"tree": {"path": "."}}],
    )
    execute_plan(plan_job(audit, workspace))
    with pytest.raises(ExecutionError) as wrong_kind:
        rollback_job(workspace, audit.id, approved=True)
    assert wrong_kind.value.code is ExecutionErrorCode.ROLLBACK_FAILED

    with pytest.raises(ExecutionError) as missing:
        rollback_job(workspace, "PATCH-MISSING", approved=True)
    assert missing.value.code is ExecutionErrorCode.JOB_NOT_FOUND


def test_completed_backup_loader_rejects_unsafe_reference_and_old_manifest(
    workspace: Workspace,
) -> None:
    with pytest.raises(ExecutionError) as unsafe:
        load_completed_backup(
            workspace,
            "../outside",
            job_id="PATCH-X",
            job_hash="0" * 64,
        )
    assert unsafe.value.code is ExecutionErrorCode.ROLLBACK_FAILED

    run = workspace.root / "patches/backups/PATCH-X/run"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "project_id": PROJECT_ID,
                "job_id": "PATCH-X",
                "job_hash": "0" * 64,
                "run_timestamp": "run",
                "status": "COMPLETED",
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExecutionError) as old:
        load_completed_backup(
            workspace,
            "patches/backups/PATCH-X/run",
            job_id="PATCH-X",
            job_hash="0" * 64,
        )
    assert old.value.code is ExecutionErrorCode.ROLLBACK_FAILED


def test_completed_backup_manifest_parser_rejects_corruption_variants(
    workspace: Workspace,
) -> None:
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    result = execute_plan(plan_job(job, workspace), approved=True)
    assert result.backup_path is not None
    payload = json.loads((result.backup_path / "manifest.json").read_text("utf-8"))

    def reject(mutator, *, selected_workspace=workspace) -> None:
        changed = copy.deepcopy(payload)
        mutator(changed)
        with pytest.raises((TypeError, ValueError)):
            backup_module._parse_completed_manifest(
                selected_workspace,
                changed,
                job_id=job.id,
                job_hash=result.plan.job_hash,
            )

    for key, value in (
        ("manifest_version", 2),
        ("project_id", "PSH-0000000000000000"),
        ("job_id", "PATCH-OTHER"),
        ("job_hash", "f" * 64),
        ("status", "ROLLED_BACK"),
        ("entries", {}),
        ("applied_states", []),
    ):
        reject(lambda item, key=key, value=value: item.__setitem__(key, value))

    with pytest.raises(TypeError):
        backup_module._parse_completed_manifest(
            workspace,
            [],
            job_id=job.id,
            job_hash=result.plan.job_hash,
        )

    execution = workspace.config.execution.model_copy(
        update={"max_inventory_entries": 1}
    )
    limited = replace(
        workspace,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    reject(lambda item: None, selected_workspace=limited)

    reject(lambda item: item["entries"].__setitem__(0, "invalid"))
    reject(lambda item: item["entries"][0].__setitem__("path", "."))
    reject(lambda item: item["entries"][0].__setitem__("path", "patches/x"))
    reject(lambda item: item["entries"].append(copy.deepcopy(item["entries"][0])))
    directory_index = next(
        index
        for index, entry in enumerate(payload["entries"])
        if entry["kind"] == "directory"
    )
    file_index = next(
        index
        for index, entry in enumerate(payload["entries"])
        if entry["kind"] == "file"
    )
    directory_path = payload["entries"][directory_index]["path"]
    file_path = payload["entries"][file_index]["path"]
    reject(
        lambda item: item["entries"][directory_index].__setitem__(
            "original_state",
            "PRESENT",
        )
    )
    reject(lambda item: item["applied_states"].pop(file_path))
    reject(
        lambda item: item["applied_states"][file_path].__setitem__(
            "kind",
            "directory",
        )
    )
    reject(
        lambda item: item["applied_states"][file_path].__setitem__(
            "sha256",
            None,
        )
    )
    reject(lambda item: item["applied_states"][file_path].__setitem__("size", -1))
    reject(
        lambda item: item["applied_states"][directory_path].__setitem__(
            "sha256",
            "not-null",
        )
    )
    reject(lambda item: item["applied_states"][directory_path].__setitem__("size", 1))
    reject(lambda item: item["entries"][file_index].pop("kind"))


@pytest.mark.parametrize(
    "value",
    ["/absolute", "", "wrong/file", "originals/../file", "originals\\file"],
)
def test_original_copy_path_validation(value: str) -> None:
    with pytest.raises(ValueError):
        backup_module._safe_backup_copy_path(value)


@pytest.mark.parametrize(
    "reference",
    [
        "/absolute",
        "patches\\backups\\PATCH-X\\run",
        "patches/backups/PATCH-X",
        "patches/backups/PATCH-Y/run",
        "patches/backups/PATCH-X/../run",
    ],
)
def test_backup_reference_shape_validation(
    workspace: Workspace,
    reference: str,
) -> None:
    with pytest.raises(ExecutionError) as caught:
        backup_module._resolve_backup_reference(
            workspace,
            reference,
            job_id="PATCH-X",
        )
    assert caught.value.code is ExecutionErrorCode.ROLLBACK_FAILED


def test_completed_backup_loader_and_capture_map_unsafe_filesystem_states(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    result = execute_plan(plan_job(job, workspace), approved=True)
    assert result.backup_path is not None
    reference = result.backup_path.relative_to(workspace.root).as_posix()
    loaded = load_completed_backup(
        workspace,
        reference,
        job_id=job.id,
        job_hash=result.plan.job_hash,
    )
    with pytest.raises(KeyError):
        loaded.entry_for(PurePosixPath("missing"))

    manifest = result.backup_path / "manifest.json"
    raw = manifest.read_bytes()
    manifest.unlink()
    manifest.mkdir()
    with pytest.raises(ExecutionError) as unreadable:
        load_completed_backup(
            workspace,
            reference,
            job_id=job.id,
            job_hash=result.plan.job_hash,
        )
    assert unreadable.value.code is ExecutionErrorCode.ROLLBACK_FAILED
    manifest.rmdir()
    manifest.write_bytes(raw)

    prepared = backup_module.PreparedBackup(
        plan=result.plan,
        path=result.backup_path,
        run_timestamp=RUN_TIMESTAMP,
        entries=loaded.entries,
    )
    created_file = workspace.root / "created/new.txt"
    created_file.unlink()
    created_file.mkdir()
    with pytest.raises(ExecutionError) as wrong_file:
        backup_module._capture_applied_states(prepared)
    assert wrong_file.value.path == "created/new.txt"
    created_file.rmdir()
    created_file.write_text("new\n", encoding="utf-8")

    created_directory = workspace.root / "created"
    original_entry = next(
        entry for entry in prepared.entries if entry.path == PurePosixPath("created")
    )
    directory_only = replace(prepared, entries=(original_entry,))
    created_file.unlink()
    created_directory.rmdir()
    created_directory.write_text("wrong\n", encoding="utf-8")
    with pytest.raises(ExecutionError) as wrong_directory:
        backup_module._capture_applied_states(directory_only)
    assert wrong_directory.value.path == "created"


def test_backup_reference_and_manifest_write_failures_are_mapped(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = workspace.root / "patches/backups/PATCH-X/run"
    run.mkdir(parents=True)
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda path, *args, **kwargs: (
            Path("/different") if path == run else path.absolute()
        ),
    )
    with pytest.raises(ExecutionError) as alias:
        backup_module._resolve_backup_reference(
            workspace,
            "patches/backups/PATCH-X/run",
            job_id="PATCH-X",
        )
    assert alias.value.code is ExecutionErrorCode.ROLLBACK_FAILED


def test_manual_rollback_rejects_second_attempt_and_missing_archive(
    workspace: Workspace,
) -> None:
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    execute_plan(plan_job(job, workspace), approved=True)
    rollback_job(workspace, job.id, approved=True)
    with pytest.raises(ExecutionError) as repeated:
        rollback_job(workspace, job.id, approved=True)
    assert repeated.value.code is ExecutionErrorCode.ROLLBACK_FAILED

    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    second = _job(workspace, job_id="PATCH-NO-ARCHIVE")
    execute_plan(plan_job(second, workspace), approved=True)
    for directory in (
        workspace.patches_dir / "applied",
        workspace.patches_dir / "failed",
    ):
        for path in directory.iterdir():
            if path.name.startswith(second.id):
                path.unlink()
    with pytest.raises(ExecutionError) as missing:
        rollback_job(workspace, second.id, approved=True)
    assert missing.value.code is ExecutionErrorCode.ROLLBACK_FAILED
    assert "archived copy" in missing.value.message


def test_manual_rollback_does_not_duplicate_an_already_logged_engine_error(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    execute_plan(plan_job(job, workspace), approved=True)
    error = ExecutionError(
        ExecutionErrorCode.ROLLBACK_FAILED,
        "already logged",
        rollback_succeeded=False,
        log_path=workspace.root / "existing.log",
    )
    monkeypatch.setattr(
        operations_module,
        "rollback_completed_backup",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(
        operations_module,
        "_record_failed_rollback",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("must not duplicate the log")
        ),
    )
    with pytest.raises(ExecutionError) as caught:
        rollback_job(workspace, job.id, approved=True)
    assert caught.value is error


@pytest.mark.parametrize(
    "value",
    [
        "/absolute",
        "patches\\applied\\job.psh.yaml",
        "patches/applied",
        "outside/applied/job.psh.yaml",
        "patches/other/job.psh.yaml",
        "patches/applied/../job.psh.yaml",
    ],
)
def test_archive_reference_shape_validation(
    workspace: Workspace,
    value: str,
) -> None:
    assert operations_module._safe_archive_path(workspace, value) is None


def test_archive_search_skips_unsafe_and_invalid_candidates(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied = workspace.patches_dir / "applied"
    directory_candidate = applied / "PATCH-X_run_00000000.psh.yaml"
    directory_candidate.mkdir()
    invalid = applied / "PATCH-X_run2_00000000.psh.yaml"
    invalid.write_text("not: [valid", encoding="utf-8")
    wrong = applied / "PATCH-X_run3_00000000.psh.yaml"
    wrong.write_text(
        yaml.safe_dump(
            {
                "protocol": 1,
                "project_id": PROJECT_ID,
                "id": "PATCH-WRONG",
                "kind": "patch",
                "actions": [{"create_file": {"path": "x", "content": "x"}}],
                "checks": [{"import_check": {"modules": ["json"]}}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExecutionError):
        operations_module._find_archived_job(
            workspace,
            job_id="PATCH-X",
            job_hash="0" * 64,
            preferred="invalid",
        )

    real_glob = Path.glob

    def fail_glob(path: Path, pattern: str):
        if path in {
            workspace.patches_dir / "applied",
            workspace.patches_dir / "failed",
        }:
            raise OSError("injected")
        return real_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", fail_glob)
    with pytest.raises(ExecutionError):
        operations_module._find_archived_job(
            workspace,
            job_id="PATCH-X",
            job_hash="0" * 64,
            preferred="invalid",
        )


def test_archive_reference_resolve_failures(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = "patches/applied/job.psh.yaml"
    path = workspace.root / value
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda current, *args, **kwargs: (
            Path("/different") if current == path else current.absolute()
        ),
    )
    assert operations_module._safe_archive_path(workspace, value) is None

    monkeypatch.setattr(
        Path,
        "resolve",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected")),
    )
    assert operations_module._safe_archive_path(workspace, value) is None


def test_manual_rollback_preflight_maps_modes_types_and_inspection_errors(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    result = execute_plan(plan_job(job, workspace), approved=True)
    assert result.backup_path is not None
    loaded = load_completed_backup(
        workspace,
        result.backup_path.relative_to(workspace.root).as_posix(),
        job_id=job.id,
        job_hash=result.plan.job_hash,
    )

    target = workspace.root / "existing.txt"
    applied_mode = next(
        entry.applied_mode
        for entry in loaded.entries
        if entry.path == PurePosixPath("existing.txt")
    )
    target.chmod(0o444 if applied_mode != 0o444 else 0o666)
    assert target.stat().st_mode & 0o777 != applied_mode
    with pytest.raises(ExecutionError) as mode:
        rollback_module._preflight_manual_rollback(workspace, loaded)
    assert mode.value.path == "existing.txt"
    target.chmod(applied_mode)

    file_entry = next(
        entry
        for entry in loaded.entries
        if entry.path == PurePosixPath("created/new.txt")
    )
    incomplete = replace(file_entry, applied_sha256=None)
    with pytest.raises(ExecutionError) as fingerprint:
        rollback_module._preflight_manual_rollback(
            workspace,
            replace(loaded, entries=(incomplete,)),
        )
    assert fingerprint.value.path == "created/new.txt"

    created_file = workspace.root / "created/new.txt"
    created_file.unlink()
    (workspace.root / "created").rmdir()
    (workspace.root / "created").write_text("wrong\n", encoding="utf-8")
    directory_entry = next(
        entry for entry in loaded.entries if entry.path == PurePosixPath("created")
    )
    wrong_type_path = workspace.root / "created"
    wrong_type_path.chmod(directory_entry.applied_mode)
    real_lstat = Path.lstat

    class RegularMetadata:
        st_mode = stat.S_IFREG | directory_entry.applied_mode

    def regular_lstat(path: Path):
        if path == wrong_type_path:
            return RegularMetadata()
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", regular_lstat)

    with pytest.raises(ExecutionError) as wrong_type:
        rollback_module._preflight_manual_rollback(workspace, loaded)
    assert wrong_type.value.path == "created"


def test_manual_rollback_preflight_directory_iteration_and_child_metadata_errors(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    result = execute_plan(plan_job(job, workspace), approved=True)
    assert result.backup_path is not None
    loaded = load_completed_backup(
        workspace,
        result.backup_path.relative_to(workspace.root).as_posix(),
        job_id=job.id,
        job_hash=result.plan.job_hash,
    )
    created = workspace.root / "created"
    real_iterdir = Path.iterdir

    def fail_iterdir(path: Path):
        if path == created:
            raise OSError("injected")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_iterdir)
    with pytest.raises(ExecutionError, match="could not inspect a created directory"):
        rollback_module._preflight_manual_rollback(workspace, loaded)

    monkeypatch.undo()

    class BadChild:
        def relative_to(self, root: Path) -> Path:
            return Path("created/new.txt")

        def lstat(self):
            raise OSError("injected")

    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda path: (BadChild(),) if path == created else real_iterdir(path),
    )
    with pytest.raises(ExecutionError, match="could not inspect a created path"):
        rollback_module._preflight_manual_rollback(workspace, loaded)


def test_loaded_manifest_update_failure_uses_rollback_error(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    result = execute_plan(plan_job(job, workspace), approved=True)
    assert result.backup_path is not None
    loaded = load_completed_backup(
        workspace,
        result.backup_path.relative_to(workspace.root).as_posix(),
        job_id=job.id,
        job_hash=result.plan.job_hash,
    )
    monkeypatch.setattr(
        backup_module.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected")),
    )
    with pytest.raises(ExecutionError) as caught:
        backup_module.update_loaded_backup(loaded, BackupStatus.ROLLED_BACK)
    assert caught.value.code is ExecutionErrorCode.ROLLBACK_FAILED
    assert caught.value.rollback_succeeded is False


def test_manual_rollback_preflight_walks_nested_declared_directories(
    workspace: Workspace,
) -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-NESTED-ROLLBACK",
        kind="patch",
        actions=[{"create_file": {"path": "outer/inner/new.txt", "content": "new\n"}}],
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    result = execute_plan(plan_job(job, workspace), approved=True)
    assert result.backup_path is not None
    loaded = load_completed_backup(
        workspace,
        result.backup_path.relative_to(workspace.root).as_posix(),
        job_id=job.id,
        job_hash=result.plan.job_hash,
    )
    rollback_module._preflight_manual_rollback(workspace, loaded)


def test_read_original_rejects_an_incomplete_loaded_entry(
    workspace: Workspace,
) -> None:
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    result = execute_plan(plan_job(job, workspace), approved=True)
    assert result.backup_path is not None
    loaded = load_completed_backup(
        workspace,
        result.backup_path.relative_to(workspace.root).as_posix(),
        job_id=job.id,
        job_hash=result.plan.job_hash,
    )
    original = next(
        entry
        for entry in loaded.entries
        if entry.original_state is backup_module.OriginalState.PRESENT
    )
    incomplete = replace(original, original_sha256=None)
    with pytest.raises(OSError, match="cannot restore"):
        rollback_module._read_original(
            replace(loaded, entries=(incomplete,)),
            incomplete.path,
        )


def test_applied_state_capture_detects_a_file_read_race(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    job = _job(workspace)
    result = execute_plan(plan_job(job, workspace), approved=True)
    assert result.backup_path is not None
    loaded = load_completed_backup(
        workspace,
        result.backup_path.relative_to(workspace.root).as_posix(),
        job_id=job.id,
        job_hash=result.plan.job_hash,
    )
    prepared = backup_module.PreparedBackup(
        plan=result.plan,
        path=result.backup_path,
        run_timestamp=RUN_TIMESTAMP,
        entries=loaded.entries,
    )
    target = workspace.root / "existing.txt"
    real_read = Path.read_bytes

    def mismatched_read(path: Path) -> bytes:
        raw = real_read(path)
        return raw + b"x" if path == target else raw

    monkeypatch.setattr(Path, "read_bytes", mismatched_read)
    with pytest.raises(ExecutionError) as caught:
        backup_module._capture_applied_states(prepared)
    assert caught.value.path == "existing.txt"


def test_backup_reference_rejects_a_nondirectory_component(
    workspace: Workspace,
) -> None:
    job_component = workspace.root / "patches/backups/PATCH-BAD"
    job_component.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(ExecutionError) as caught:
        backup_module._resolve_backup_reference(
            workspace,
            "patches/backups/PATCH-BAD/run",
            job_id="PATCH-BAD",
        )
    assert caught.value.code is ExecutionErrorCode.ROLLBACK_FAILED

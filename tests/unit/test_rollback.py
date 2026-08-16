"""Unit tests for conservative transaction rollback."""

from __future__ import annotations

import stat
from dataclasses import FrozenInstanceError, replace
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle.backup import OriginalState, prepare_backup
from patchshuttle.planner import Plan, plan_job
from patchshuttle.rollback import rollback_created, rollback_transaction
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    return init_workspace(tmp_path).workspace


def modify_plan(workspace: Workspace) -> Plan:
    target = workspace.root / "existing.txt"
    target.write_bytes(b"before\n")
    target.chmod(0o640)
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-009",
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


def test_rollback_removes_regular_created_paths_and_accepts_missing(
    workspace: Workspace,
) -> None:
    directory = workspace.root / "created/nested"
    directory.mkdir(parents=True)
    target = directory / "file.txt"
    target.write_text("content\n", encoding="utf-8")

    result = rollback_created(
        workspace,
        files=(
            PurePosixPath("missing.txt"),
            PurePosixPath("created/nested/file.txt"),
        ),
        directories=(
            PurePosixPath("missing-directory"),
            PurePosixPath("created"),
            PurePosixPath("created/nested"),
        ),
    )

    assert result.success is True
    assert result.removed_files == (PurePosixPath("created/nested/file.txt"),)
    assert result.removed_directories == (
        PurePosixPath("created/nested"),
        PurePosixPath("created"),
    )
    assert result.unresolved == ()
    assert not (workspace.root / "created").exists()

    with pytest.raises(FrozenInstanceError):
        result.unresolved = (PurePosixPath("changed"),)  # type: ignore[misc]


def test_rollback_preserves_wrong_types_symlinks_and_nonempty_directories(
    workspace: Workspace,
) -> None:
    (workspace.root / "file-slot").mkdir()
    (workspace.root / "directory-slot").write_text("file\n", encoding="utf-8")
    (workspace.root / "nonempty").mkdir()
    (workspace.root / "nonempty/foreign.txt").write_text(
        "foreign\n",
        encoding="utf-8",
    )
    (workspace.root / "real.txt").write_text("real\n", encoding="utf-8")
    (workspace.root / "linked.txt").symlink_to(workspace.root / "real.txt")

    result = rollback_created(
        workspace,
        files=(
            PurePosixPath("file-slot"),
            PurePosixPath("linked.txt"),
        ),
        directories=(
            PurePosixPath("directory-slot"),
            PurePosixPath("nonempty"),
        ),
    )

    assert result.success is False
    assert result.removed_files == ()
    assert result.removed_directories == ()
    assert set(result.unresolved) == {
        PurePosixPath("file-slot"),
        PurePosixPath("linked.txt"),
        PurePosixPath("directory-slot"),
        PurePosixPath("nonempty"),
    }
    assert (workspace.root / "nonempty/foreign.txt").exists()
    assert (workspace.root / "real.txt").read_text("utf-8") == "real\n"


def test_rollback_reports_file_and_directory_remove_errors(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = workspace.root / "created.txt"
    directory_path = workspace.root / "created-dir"
    file_path.write_text("content\n", encoding="utf-8")
    directory_path.mkdir()
    real_unlink = Path.unlink
    real_rmdir = Path.rmdir

    def fail_unlink(path: Path, *args, **kwargs):
        if path == file_path:
            raise PermissionError("unlink denied")
        return real_unlink(path, *args, **kwargs)

    def fail_rmdir(path: Path, *args, **kwargs):
        if path == directory_path:
            raise PermissionError("rmdir denied")
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    monkeypatch.setattr(Path, "rmdir", fail_rmdir)

    result = rollback_created(
        workspace,
        files=(PurePosixPath("created.txt"),),
        directories=(PurePosixPath("created-dir"),),
    )

    assert result.success is False
    assert result.unresolved == (
        PurePosixPath("created.txt"),
        PurePosixPath("created-dir"),
    )
    assert file_path.exists()
    assert directory_path.exists()


def test_transaction_rollback_restores_original_and_removes_created_paths(
    workspace: Workspace,
) -> None:
    plan = modify_plan(workspace)
    backup = prepare_backup(plan)
    target = workspace.root / "existing.txt"
    target.write_bytes(b"after\n")
    target.chmod(0o600)
    created_directory = workspace.root / "created"
    created_directory.mkdir()
    created_file = created_directory / "new.txt"
    created_file.write_bytes(b"new\n")

    result = rollback_transaction(
        workspace,
        backup,
        modified_files=(PurePosixPath("existing.txt"),),
        files=(PurePosixPath("created/new.txt"),),
        directories=(PurePosixPath("created"),),
    )

    assert result.success is True
    assert result.restored_files == (PurePosixPath("existing.txt"),)
    assert result.removed_files == (PurePosixPath("created/new.txt"),)
    assert result.removed_directories == (PurePosixPath("created"),)
    assert target.read_bytes() == b"before\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_transaction_rollback_reports_missing_and_incomplete_entries(
    workspace: Workspace,
) -> None:
    backup = prepare_backup(modify_plan(workspace))

    missing = rollback_transaction(
        workspace,
        backup,
        modified_files=(PurePosixPath("missing.txt"),),
        files=(),
        directories=(),
    )
    assert missing.unresolved == (PurePosixPath("missing.txt"),)

    incomplete_entry = replace(
        backup.entries[0],
        original_state=OriginalState.ABSENT,
    )
    incomplete_backup = replace(backup, entries=(incomplete_entry,))
    incomplete = rollback_transaction(
        workspace,
        incomplete_backup,
        modified_files=(PurePosixPath("existing.txt"),),
        files=(),
        directories=(),
    )
    assert incomplete.unresolved == (PurePosixPath("existing.txt"),)


@pytest.mark.parametrize("replacement_kind", ["directory", "symlink"])
def test_transaction_rollback_rejects_nonregular_original_copy(
    workspace: Workspace,
    replacement_kind: str,
) -> None:
    backup = prepare_backup(modify_plan(workspace))
    entry = backup.entries[0]
    assert entry.backup_path is not None
    original = backup.path.joinpath(*entry.backup_path.parts)
    original.unlink()
    if replacement_kind == "directory":
        original.mkdir()
    else:
        original.symlink_to(workspace.root / "existing.txt")

    result = rollback_transaction(
        workspace,
        backup,
        modified_files=(PurePosixPath("existing.txt"),),
        files=(),
        directories=(),
    )

    assert result.unresolved == (PurePosixPath("existing.txt"),)


@pytest.mark.parametrize("corrupted", [b"x", b"changed"])
def test_transaction_rollback_rejects_corrupted_original_copy(
    workspace: Workspace,
    corrupted: bytes,
) -> None:
    backup = prepare_backup(modify_plan(workspace))
    entry = backup.entries[0]
    assert entry.backup_path is not None
    original = backup.path.joinpath(*entry.backup_path.parts)
    original.write_bytes(corrupted)

    result = rollback_transaction(
        workspace,
        backup,
        modified_files=(PurePosixPath("existing.txt"),),
        files=(),
        directories=(),
    )

    assert result.unresolved == (PurePosixPath("existing.txt"),)

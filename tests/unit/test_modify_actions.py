"""Unit tests for guarded existing-file replacement primitives."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.actions.modify as modify_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle.actions import (
    FileReplaceError,
    atomic_replace_file,
    atomic_restore_file,
    verify_modified_file,
    verify_restored_file,
)
from patchshuttle.planner import PlannedFileChange, plan_job
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


def planned_change(
    workspace: Workspace,
    *,
    path: str = "module.txt",
) -> PlannedFileChange:
    (workspace.root / path).write_bytes(b"before\n")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-009",
        kind="patch",
        actions=[
            {
                "replace_exact": {
                    "path": path,
                    "old": "before",
                    "new": "after!",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )
    return plan_job(job, workspace).file_changes[0]


def test_atomic_replace_writes_exact_bytes_and_requested_mode(
    workspace: Workspace,
) -> None:
    change = planned_change(workspace)
    target = workspace.root / change.path
    target.chmod(0o640)
    mode = stat.S_IMODE(target.stat().st_mode)

    atomic_replace_file(workspace, change, mode=mode)

    verify_modified_file(workspace, change, mode=mode)
    assert target.read_bytes() == b"after!\n"
    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert not list(workspace.root.glob(".patchshuttle-*.tmp"))


def test_atomic_replace_rejects_missing_target_and_incomplete_fingerprint(
    workspace: Workspace,
) -> None:
    change = planned_change(workspace)
    target = workspace.root / change.path
    target.unlink()

    with pytest.raises(OSError, match="not a regular file"):
        atomic_replace_file(workspace, change, mode=0o600)

    target.write_bytes(b"before\n")
    incomplete = replace(change, before_sha256=None, before_size=None)
    with pytest.raises(OSError, match="lacks an original hash"):
        atomic_replace_file(workspace, incomplete, mode=0o600)


def test_atomic_replace_rechecks_target_before_publish_and_cleans_temp(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = planned_change(workspace)
    target = workspace.root / change.path
    real_chmod = Path.chmod

    def race_after_staging(path: Path, mode: int, *args, **kwargs) -> None:
        real_chmod(path, mode, *args, **kwargs)
        if path.name.startswith(".patchshuttle-"):
            target.write_bytes(b"external\n")

    monkeypatch.setattr(Path, "chmod", race_after_staging)

    with pytest.raises(OSError, match="changed after planning"):
        atomic_replace_file(workspace, change, mode=0o600)

    assert target.read_bytes() == b"external\n"
    assert not list(workspace.root.glob(".patchshuttle-*.tmp"))


def test_atomic_replace_reports_temporary_data_that_cannot_be_removed(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = planned_change(workspace)
    real_unlink = Path.unlink

    monkeypatch.setattr(
        modify_module.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    def refuse_temp_cleanup(path: Path, *args, **kwargs) -> None:
        if path.name.startswith(".patchshuttle-"):
            raise PermissionError("cleanup denied")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_temp_cleanup)

    with pytest.raises(FileReplaceError) as caught:
        atomic_replace_file(workspace, change, mode=0o600)

    assert caught.value.target_modified is False
    assert caught.value.temporary_path is not None
    assert caught.value.temporary_path.exists()
    assert (workspace.root / change.path).read_bytes() == b"before\n"
    real_unlink(caught.value.temporary_path)


def test_verify_modified_file_rejects_each_post_state_mismatch(
    workspace: Workspace,
) -> None:
    change = planned_change(workspace)
    target = workspace.root / change.path
    target.write_bytes(change.content)
    target.chmod(0o640)
    mode = stat.S_IMODE(target.stat().st_mode)
    verify_modified_file(workspace, change, mode=mode)

    target.write_bytes(b"x")
    with pytest.raises(OSError, match="post-state validation"):
        verify_modified_file(workspace, change, mode=mode)

    target.write_bytes(b"wrong!\n")
    with pytest.raises(OSError, match="post-state validation"):
        verify_modified_file(workspace, change, mode=mode)

    raw = b"actual\n"
    target.write_bytes(raw)
    content_only = replace(
        change,
        content=b"other!\n",
        after_size=len(raw),
        after_sha256=hashlib.sha256(raw).hexdigest(),
    )
    with pytest.raises(OSError, match="post-state validation"):
        verify_modified_file(workspace, content_only, mode=mode)

    target.write_bytes(change.content)
    target.chmod(0o444 if mode != 0o444 else 0o666)
    assert stat.S_IMODE(target.stat().st_mode) != mode
    with pytest.raises(OSError, match="post-state validation"):
        verify_modified_file(workspace, change, mode=mode)
    target.chmod(mode)


def test_atomic_restore_accepts_missing_file_and_rejects_directory(
    workspace: Workspace,
) -> None:
    path = PurePosixPath("restored.txt")
    target = workspace.root / path
    target.write_bytes(b"mode probe\n")
    target.chmod(0o640)
    mode = stat.S_IMODE(target.stat().st_mode)
    target.unlink()

    atomic_restore_file(workspace, path, b"original\n", mode=mode)
    verify_restored_file(
        workspace,
        path,
        b"original\n",
        mode=mode,
    )

    target.write_bytes(b"modified\n")
    with pytest.raises(OSError, match="post-state validation"):
        verify_restored_file(
            workspace,
            path,
            b"original\n",
            mode=mode,
        )

    target.write_bytes(b"original\n")
    target.chmod(0o444 if mode != 0o444 else 0o666)
    assert stat.S_IMODE(target.stat().st_mode) != mode
    with pytest.raises(OSError, match="post-state validation"):
        verify_restored_file(
            workspace,
            path,
            b"original\n",
            mode=mode,
        )

    target.chmod(mode)
    target.unlink()
    target.mkdir()
    with pytest.raises(OSError, match="unexpected type"):
        atomic_restore_file(workspace, path, b"original\n", mode=mode)

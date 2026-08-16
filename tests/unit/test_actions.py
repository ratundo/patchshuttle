"""Unit tests for guarded create action primitives."""

from __future__ import annotations

import errno
import hashlib
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.actions.create as create_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle.actions import (
    FilePublishError,
    atomic_create_file,
    create_directory,
    verify_created_directory,
    verify_created_file,
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
    workspace: Workspace, path: str = "created.txt"
) -> PlannedFileChange:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-001",
        kind="patch",
        actions=[{"create_file": {"path": path, "content": "content\n"}}],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )
    return plan_job(job, workspace).file_changes[0]


def test_directory_creation_rejects_existing_and_verifies_post_state(
    workspace: Workspace,
) -> None:
    path = PurePosixPath("created")

    with pytest.raises(OSError):
        verify_created_directory(workspace, path)

    create_directory(workspace, path)
    verify_created_directory(workspace, path)

    with pytest.raises(FileExistsError):
        create_directory(workspace, path)


def test_atomic_create_rejects_an_existing_target(workspace: Workspace) -> None:
    change = planned_change(workspace)
    (workspace.root / change.path).write_bytes(b"external\n")

    with pytest.raises(FileExistsError):
        atomic_create_file(workspace, change)

    assert (workspace.root / change.path).read_bytes() == b"external\n"


def test_atomic_create_uses_exclusive_fallback_when_links_are_unavailable(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = planned_change(workspace)

    def unsupported_link(*args, **kwargs):
        raise OSError(errno.EPERM, "hard links unavailable")

    monkeypatch.setattr(create_module.os, "link", unsupported_link)

    atomic_create_file(workspace, change)

    verify_created_file(workspace, change)
    assert not list(workspace.root.glob(".patchshuttle-*.tmp"))


def test_atomic_create_propagates_non_fallback_link_error_and_cleans_temp(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = planned_change(workspace)

    def broken_link(*args, **kwargs):
        raise OSError(errno.EIO, "link failed")

    monkeypatch.setattr(create_module.os, "link", broken_link)

    with pytest.raises(OSError, match="link failed"):
        atomic_create_file(workspace, change)

    assert not (workspace.root / change.path).exists()
    assert not list(workspace.root.glob(".patchshuttle-*.tmp"))


def test_atomic_create_accepts_temp_already_removed_after_publication(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = planned_change(workspace)
    real_link = create_module.os.link

    def link_and_remove(source, target):
        real_link(source, target)
        Path(source).unlink()

    monkeypatch.setattr(create_module.os, "link", link_and_remove)

    atomic_create_file(workspace, change)

    verify_created_file(workspace, change)


def test_atomic_create_reports_published_target_and_unremoved_temp(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = planned_change(workspace)
    real_unlink = Path.unlink

    def refuse_temp_cleanup(path: Path, *args, **kwargs):
        if path.name.startswith(".patchshuttle-"):
            raise PermissionError("cleanup denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_temp_cleanup)

    with pytest.raises(FilePublishError) as caught:
        atomic_create_file(workspace, change)

    assert caught.value.target_created is True
    assert caught.value.temporary_path is not None
    assert (workspace.root / change.path).read_bytes() == change.content

    real_unlink(caught.value.temporary_path)
    real_unlink(workspace.root / change.path)


def test_verify_created_file_rejects_missing_and_each_content_mismatch(
    workspace: Workspace,
) -> None:
    change = planned_change(workspace)
    target = workspace.root / change.path

    with pytest.raises(OSError, match="unexpected post-state"):
        verify_created_file(workspace, change)

    target.write_bytes(b"short")
    with pytest.raises(OSError, match="content failed"):
        verify_created_file(workspace, change)

    target.write_bytes(b"different")
    same_size = replace(change, after_size=len(b"different"))
    with pytest.raises(OSError, match="content failed"):
        verify_created_file(workspace, same_size)

    raw = b"different"
    content_only = replace(
        change,
        content=b"othertext",
        after_size=len(raw),
        after_sha256=hashlib.sha256(raw).hexdigest(),
    )
    with pytest.raises(OSError, match="content failed"):
        verify_created_file(workspace, content_only)


def test_exclusive_copy_removes_partial_target_after_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.txt"
    monkeypatch.setattr(
        create_module.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("sync failed")),
    )

    with pytest.raises(OSError, match="sync failed"):
        create_module._exclusive_copy(target, b"content")

    assert not target.exists()


def test_exclusive_copy_handles_target_removed_during_error_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.txt"
    real_unlink = Path.unlink

    monkeypatch.setattr(
        create_module.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("sync failed")),
    )

    def remove_then_report_missing(path: Path, *args, **kwargs):
        real_unlink(path, *args, **kwargs)
        raise FileNotFoundError(path)

    monkeypatch.setattr(Path, "unlink", remove_then_report_missing)

    with pytest.raises(OSError, match="sync failed"):
        create_module._exclusive_copy(target, b"content")

    assert not target.exists()


def test_exclusive_copy_reports_partial_target_that_cannot_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.txt"
    real_unlink = Path.unlink
    monkeypatch.setattr(
        create_module.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("sync failed")),
    )
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda path, *args, **kwargs: (_ for _ in ()).throw(
            PermissionError("cleanup denied")
        ),
    )

    with pytest.raises(FilePublishError) as caught:
        create_module._exclusive_copy(target, b"content")

    assert caught.value.target_created is True
    assert caught.value.temporary_path is None
    assert target.exists()
    real_unlink(target)

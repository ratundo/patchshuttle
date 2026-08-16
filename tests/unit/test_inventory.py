"""Contract tests for bounded workspace inventory and comparison."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import FrozenInstanceError, replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import patchshuttle.inventory as inventory_module
import patchshuttle.workspace as workspace_module
from patchshuttle.inventory import (
    InventoryEntry,
    InventoryEntryKind,
    InventoryError,
    InventoryErrorCode,
    WorkspaceChangeKind,
    WorkspaceInventory,
    capture_inventory,
    compare_inventories,
)
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


def configured_limits(
    workspace: Workspace,
    *,
    entries: int | None = None,
    hashed_bytes: int | None = None,
) -> Workspace:
    updates = {}
    if entries is not None:
        updates["max_inventory_entries"] = entries
    if hashed_bytes is not None:
        updates["max_inventory_bytes"] = hashed_bytes
    execution = workspace.config.execution.model_copy(update=updates)
    config = workspace.config.model_copy(update={"execution": execution})
    return replace(workspace, config=config)


def entry(
    path: str,
    kind: InventoryEntryKind,
    *,
    size: int = 0,
    modified_ns: int = 0,
    mode: int = 0o644,
    digest: str | None = None,
) -> InventoryEntry:
    return InventoryEntry(
        path=PurePosixPath(path),
        kind=kind,
        size=size,
        modified_ns=modified_ns,
        mode=mode,
        sha256=digest,
    )


def test_capture_inventory_hashes_files_and_skips_ignored_trees(
    workspace: Workspace,
) -> None:
    source = workspace.root / "src"
    source.mkdir()
    target = source / "example.py"
    target.write_bytes(b"VALUE = 1\n")
    ignored = workspace.root / ".pytest_cache"
    ignored.mkdir()
    (ignored / "cache.txt").write_text("ignored\n", encoding="utf-8")
    (workspace.root / "patches/logs/ignored.log").write_text(
        "ignored\n", encoding="utf-8"
    )

    result = capture_inventory(workspace)

    by_path = {item.path: item for item in result.entries}
    recorded = by_path[PurePosixPath("src/example.py")]
    directory = by_path[PurePosixPath("src")]
    assert tuple(item.path for item in result.entries) == tuple(
        sorted((item.path for item in result.entries), key=PurePosixPath.as_posix)
    )
    assert recorded.kind is InventoryEntryKind.FILE
    assert recorded.size == len(b"VALUE = 1\n")
    assert recorded.sha256 == hashlib.sha256(b"VALUE = 1\n").hexdigest()
    assert directory.kind is InventoryEntryKind.DIRECTORY
    assert directory.size == 0
    assert directory.modified_ns == 0
    assert result.hashed_bytes == sum(
        item.size for item in result.entries if item.kind is InventoryEntryKind.FILE
    )
    assert not any(
        item.path.as_posix().startswith(".pytest_cache") for item in result.entries
    )
    assert not any(
        item.path.as_posix().startswith("patches/logs") for item in result.entries
    )
    with pytest.raises(FrozenInstanceError):
        recorded.size = 0  # type: ignore[misc]


def test_capture_records_symlinks_and_special_files_without_following(
    workspace: Workspace,
) -> None:
    target = workspace.root.parent / "outside-inventory-target.txt"
    target.write_text("outside\n", encoding="utf-8")
    link = workspace.root / "outside-link"
    fifo = workspace.root / "events.fifo"
    try:
        link.symlink_to(target)
        os.mkfifo(fifo)
    except OSError:
        pytest.skip("symbolic links or named pipes are unavailable")

    result = capture_inventory(workspace)
    by_path = {item.path: item for item in result.entries}

    assert by_path[PurePosixPath("outside-link")].kind is InventoryEntryKind.SYMLINK
    assert by_path[PurePosixPath("outside-link")].sha256 is None
    assert by_path[PurePosixPath("events.fifo")].kind is InventoryEntryKind.OTHER
    assert by_path[PurePosixPath("events.fifo")].sha256 is None


def test_capture_enforces_entry_and_total_hash_byte_limits(
    workspace: Workspace,
) -> None:
    with pytest.raises(InventoryError) as entry_failure:
        capture_inventory(configured_limits(workspace, entries=1))
    with pytest.raises(InventoryError) as byte_failure:
        capture_inventory(configured_limits(workspace, hashed_bytes=1))

    assert entry_failure.value.code is InventoryErrorCode.ENTRY_LIMIT_EXCEEDED
    assert entry_failure.value.path is not None
    assert byte_failure.value.code is InventoryErrorCode.BYTE_LIMIT_EXCEEDED
    assert byte_failure.value.path is not None


def test_capture_reports_directory_metadata_and_open_failures(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        inventory_module.os,
        "scandir",
        lambda path: (_ for _ in ()).throw(OSError("scan failed")),
    )
    with pytest.raises(InventoryError) as scan_failure:
        capture_inventory(workspace)

    assert scan_failure.value.code is InventoryErrorCode.INSPECTION_FAILED
    assert scan_failure.value.path is None
    assert str(scan_failure.value).startswith("[INSPECTION_FAILED] workspace")

    class BrokenEntry:
        def stat(self, *, follow_symlinks: bool):
            assert follow_symlinks is False
            raise OSError("stat failed")

    with pytest.raises(InventoryError) as metadata_failure:
        inventory_module._entry_metadata(  # noqa: SLF001
            BrokenEntry(), PurePosixPath("broken")
        )

    assert metadata_failure.value.code is InventoryErrorCode.INSPECTION_FAILED
    assert str(metadata_failure.value).startswith(
        "[INSPECTION_FAILED] broken: workspace"
    )


def test_hash_reports_open_read_and_concurrent_change_failures(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "target.txt"
    target.write_bytes(b"content\n")
    metadata = target.stat()
    relative = PurePosixPath("target.txt")
    real_open = os.open
    real_read = os.read

    monkeypatch.setattr(
        inventory_module.os,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("open failed")),
    )
    with pytest.raises(InventoryError) as open_failure:
        inventory_module._hash_regular_file(target, relative, metadata)  # noqa: SLF001
    assert open_failure.value.code is InventoryErrorCode.INSPECTION_FAILED

    monkeypatch.setattr(inventory_module.os, "open", real_open)
    monkeypatch.setattr(
        inventory_module.os,
        "read",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read failed")),
    )
    with pytest.raises(InventoryError) as read_failure:
        inventory_module._hash_regular_file(target, relative, metadata)  # noqa: SLF001
    assert read_failure.value.code is InventoryErrorCode.INSPECTION_FAILED

    monkeypatch.setattr(inventory_module.os, "read", real_read)
    monkeypatch.setattr(inventory_module, "_same_file_state", lambda *args: False)
    with pytest.raises(InventoryError) as first_change:
        inventory_module._hash_regular_file(target, relative, metadata)  # noqa: SLF001
    assert first_change.value.code is InventoryErrorCode.FILE_CHANGED_DURING_CAPTURE

    states = iter((True, False))
    monkeypatch.setattr(
        inventory_module,
        "_same_file_state",
        lambda *args: next(states),
    )
    with pytest.raises(InventoryError) as final_change:
        inventory_module._hash_regular_file(target, relative, metadata)  # noqa: SLF001
    assert final_change.value.code is InventoryErrorCode.FILE_CHANGED_DURING_CAPTURE


@pytest.mark.parametrize(
    "field",
    ("regular", "st_dev", "st_ino", "st_size", "st_mtime_ns", "mode"),
)
def test_same_file_state_rejects_each_changed_identity_field(field: str) -> None:
    base = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o644,
        st_dev=1,
        st_ino=2,
        st_size=3,
        st_mtime_ns=4,
    )
    values = vars(base).copy()
    if field == "regular":
        values["st_mode"] = stat.S_IFDIR | 0o644
    elif field == "mode":
        values["st_mode"] = stat.S_IFREG | 0o600
    else:
        values[field] += 1
    changed = SimpleNamespace(**values)

    assert inventory_module._same_file_state(base, changed) is False  # noqa: SLF001
    assert inventory_module._same_file_state(base, base) is True  # noqa: SLF001


def test_compare_inventories_classifies_all_changes_and_expected_paths() -> None:
    unchanged = entry(
        "unchanged.txt",
        InventoryEntryKind.FILE,
        size=1,
        modified_ns=1,
        digest="same",
    )
    before = WorkspaceInventory(
        entries=(
            entry("modified.txt", InventoryEntryKind.FILE, digest="old"),
            entry("removed.txt", InventoryEntryKind.FILE, digest="removed"),
            entry("typed", InventoryEntryKind.FILE, digest="file"),
            unchanged,
        ),
        hashed_bytes=1,
    )
    after = WorkspaceInventory(
        entries=(
            entry("added.txt", InventoryEntryKind.FILE, digest="added"),
            entry("modified.txt", InventoryEntryKind.FILE, digest="new"),
            entry("typed", InventoryEntryKind.DIRECTORY, mode=0o755),
            unchanged,
        ),
        hashed_bytes=1,
    )

    result = compare_inventories(
        before,
        after,
        expected_paths=(PurePosixPath("added.txt"),),
    )

    assert [
        (item.path.as_posix(), item.kind, item.expected) for item in result.changes
    ] == [
        ("added.txt", WorkspaceChangeKind.ADDED, True),
        ("modified.txt", WorkspaceChangeKind.MODIFIED, False),
        ("removed.txt", WorkspaceChangeKind.REMOVED, False),
        ("typed", WorkspaceChangeKind.TYPE_CHANGED, False),
    ]
    assert result.success is False
    assert [item.path.as_posix() for item in result.unexpected_changes] == [
        "modified.txt",
        "removed.txt",
        "typed",
    ]

    expected_only = compare_inventories(
        WorkspaceInventory(entries=(), hashed_bytes=0),
        WorkspaceInventory(
            entries=(entry("created.txt", InventoryEntryKind.FILE),),
            hashed_bytes=0,
        ),
        expected_paths=(PurePosixPath("created.txt"),),
    )
    assert expected_only.success is True
    assert expected_only.unexpected_changes == ()

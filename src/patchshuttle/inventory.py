"""Bounded workspace inventories and deterministic before/after comparison."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from patchshuttle.policy import Policy
from patchshuttle.workspace import Workspace

_HASH_CHUNK_BYTES = 1024 * 1024


class InventoryEntryKind(str, Enum):
    """Filesystem kinds recorded without following symbolic links."""

    FILE = "FILE"
    DIRECTORY = "DIRECTORY"
    SYMLINK = "SYMLINK"
    OTHER = "OTHER"


class WorkspaceChangeKind(str, Enum):
    """Stable classifications for a workspace difference."""

    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"
    TYPE_CHANGED = "TYPE_CHANGED"


class InventoryErrorCode(str, Enum):
    """Stable failures for bounded workspace inspection."""

    ENTRY_LIMIT_EXCEEDED = "ENTRY_LIMIT_EXCEEDED"
    BYTE_LIMIT_EXCEEDED = "BYTE_LIMIT_EXCEEDED"
    INSPECTION_FAILED = "INSPECTION_FAILED"
    FILE_CHANGED_DURING_CAPTURE = "FILE_CHANGED_DURING_CAPTURE"


class InventoryError(RuntimeError):
    """A workspace inventory could not be captured exactly within policy."""

    def __init__(
        self,
        code: InventoryErrorCode,
        message: str,
        *,
        path: PurePosixPath | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(message)

    def __str__(self) -> str:
        prefix = f"[{self.code.value}]"
        return (
            f"{prefix} {self.path.as_posix()}: {self.message}"
            if self.path is not None
            else f"{prefix} {self.message}"
        )


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    """Exact metadata and optional content hash for one workspace path."""

    path: PurePosixPath
    kind: InventoryEntryKind
    size: int
    modified_ns: int
    mode: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceInventory:
    """One deterministic bounded snapshot of non-ignored workspace entries."""

    entries: tuple[InventoryEntry, ...]
    hashed_bytes: int


@dataclass(frozen=True, slots=True)
class WorkspaceChange:
    """One classified difference between two workspace inventories."""

    path: PurePosixPath
    kind: WorkspaceChangeKind
    expected: bool
    before: InventoryEntry | None
    after: InventoryEntry | None


@dataclass(frozen=True, slots=True)
class WorkspaceComparison:
    """Complete inventory pair and its ordered differences."""

    before: WorkspaceInventory
    after: WorkspaceInventory
    changes: tuple[WorkspaceChange, ...]

    @property
    def unexpected_changes(self) -> tuple[WorkspaceChange, ...]:
        return tuple(change for change in self.changes if not change.expected)

    @property
    def success(self) -> bool:
        return not self.unexpected_changes


def capture_inventory(workspace: Workspace) -> WorkspaceInventory:
    """Hash every non-ignored regular file within configured hard limits."""

    policy = Policy(workspace)
    maximum_entries = workspace.config.execution.max_inventory_entries
    maximum_bytes = workspace.config.execution.max_inventory_bytes
    entries: list[InventoryEntry] = []
    hashed_bytes = 0
    pending = [(workspace.root, PurePosixPath())]

    while pending:
        directory, parent = pending.pop()
        children = _scan_directory(directory, parent)
        child_directories: list[tuple[Path, PurePosixPath]] = []
        for child in children:
            relative = parent / child.name
            if policy.is_ignored(relative):
                continue
            if len(entries) >= maximum_entries:
                raise InventoryError(
                    InventoryErrorCode.ENTRY_LIMIT_EXCEEDED,
                    "workspace inventory entry limit was exceeded",
                    path=relative,
                )
            metadata = _entry_metadata(child, relative)
            kind = _entry_kind(metadata.st_mode)
            digest: str | None = None
            size = metadata.st_size
            modified_ns = metadata.st_mtime_ns
            if kind is InventoryEntryKind.FILE:
                if hashed_bytes + size > maximum_bytes:
                    raise InventoryError(
                        InventoryErrorCode.BYTE_LIMIT_EXCEEDED,
                        "workspace inventory byte limit was exceeded",
                        path=relative,
                    )
                digest = _hash_regular_file(Path(child.path), relative, metadata)
                hashed_bytes += size
            elif kind is InventoryEntryKind.DIRECTORY:
                size = 0
                modified_ns = 0
                child_directories.append((Path(child.path), relative))

            entries.append(
                InventoryEntry(
                    path=relative,
                    kind=kind,
                    size=size,
                    modified_ns=modified_ns,
                    mode=stat.S_IMODE(metadata.st_mode),
                    sha256=digest,
                )
            )
        pending.extend(reversed(child_directories))

    entries.sort(key=lambda entry: entry.path.as_posix())
    return WorkspaceInventory(entries=tuple(entries), hashed_bytes=hashed_bytes)


def compare_inventories(
    before: WorkspaceInventory,
    after: WorkspaceInventory,
    *,
    expected_paths: tuple[PurePosixPath, ...] = (),
) -> WorkspaceComparison:
    """Classify every before/after difference and mark declared paths."""

    before_by_path = {entry.path: entry for entry in before.entries}
    after_by_path = {entry.path: entry for entry in after.entries}
    expected = frozenset(expected_paths)
    changes: list[WorkspaceChange] = []
    paths = sorted(
        before_by_path.keys() | after_by_path.keys(),
        key=PurePosixPath.as_posix,
    )
    for path in paths:
        earlier = before_by_path.get(path)
        later = after_by_path.get(path)
        if earlier is None:
            kind = WorkspaceChangeKind.ADDED
        elif later is None:
            kind = WorkspaceChangeKind.REMOVED
        elif earlier.kind is not later.kind:
            kind = WorkspaceChangeKind.TYPE_CHANGED
        elif earlier != later:
            kind = WorkspaceChangeKind.MODIFIED
        else:
            continue
        changes.append(
            WorkspaceChange(
                path=path,
                kind=kind,
                expected=path in expected,
                before=earlier,
                after=later,
            )
        )

    return WorkspaceComparison(
        before=before,
        after=after,
        changes=tuple(changes),
    )


def _scan_directory(
    directory: Path,
    relative: PurePosixPath,
) -> tuple[os.DirEntry[str], ...]:
    try:
        with os.scandir(directory) as iterator:
            return tuple(sorted(iterator, key=lambda entry: entry.name))
    except OSError as exc:
        raise InventoryError(
            InventoryErrorCode.INSPECTION_FAILED,
            "workspace directory could not be inspected",
            path=relative if relative.parts else None,
        ) from exc


def _entry_metadata(
    entry: os.DirEntry[str],
    relative: PurePosixPath,
) -> os.stat_result:
    try:
        return entry.stat(follow_symlinks=False)
    except OSError as exc:
        raise InventoryError(
            InventoryErrorCode.INSPECTION_FAILED,
            "workspace entry metadata could not be inspected",
            path=relative,
        ) from exc


def _entry_kind(mode: int) -> InventoryEntryKind:
    if stat.S_ISREG(mode):
        return InventoryEntryKind.FILE
    if stat.S_ISDIR(mode):
        return InventoryEntryKind.DIRECTORY
    if stat.S_ISLNK(mode):
        return InventoryEntryKind.SYMLINK
    return InventoryEntryKind.OTHER


def _hash_regular_file(
    path: Path,
    relative: PurePosixPath,
    expected: os.stat_result,
) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InventoryError(
            InventoryErrorCode.INSPECTION_FAILED,
            "workspace file could not be opened for hashing",
            path=relative,
        ) from exc

    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if not _same_file_state(expected, opened):
            raise InventoryError(
                InventoryErrorCode.FILE_CHANGED_DURING_CAPTURE,
                "workspace file changed while inventory was captured",
                path=relative,
            )
        while chunk := os.read(descriptor, _HASH_CHUNK_BYTES):
            digest.update(chunk)
        completed = os.fstat(descriptor)
        if not _same_file_state(opened, completed):
            raise InventoryError(
                InventoryErrorCode.FILE_CHANGED_DURING_CAPTURE,
                "workspace file changed while inventory was captured",
                path=relative,
            )
    except OSError as exc:
        raise InventoryError(
            InventoryErrorCode.INSPECTION_FAILED,
            "workspace file could not be hashed",
            path=relative,
        ) from exc
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and stat.S_IMODE(left.st_mode) == stat.S_IMODE(right.st_mode)
    )


__all__ = [
    "InventoryEntry",
    "InventoryEntryKind",
    "InventoryError",
    "InventoryErrorCode",
    "WorkspaceChange",
    "WorkspaceChangeKind",
    "WorkspaceComparison",
    "WorkspaceInventory",
    "capture_inventory",
    "compare_inventories",
]

"""Conservative rollback for paths created by one transaction attempt."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath

import patchshuttle.actions as actions
from patchshuttle.backup import (
    BackupEntryKind,
    LoadedBackup,
    OriginalState,
    PreparedBackup,
)
from patchshuttle.errors import ExecutionError, ExecutionErrorCode, PolicyError
from patchshuttle.policy import PathKind, Policy
from patchshuttle.workspace import Workspace


@dataclass(frozen=True, slots=True)
class RollbackResult:
    """Exact paths removed or left unresolved by a create-only rollback."""

    removed_files: tuple[PurePosixPath, ...]
    removed_directories: tuple[PurePosixPath, ...]
    unresolved: tuple[PurePosixPath, ...]
    restored_files: tuple[PurePosixPath, ...] = ()

    @property
    def success(self) -> bool:
        return not self.unresolved


def rollback_created(
    workspace: Workspace,
    *,
    files: tuple[PurePosixPath, ...],
    directories: tuple[PurePosixPath, ...],
) -> RollbackResult:
    """Remove only paths confirmed as created by the current attempt."""

    policy = Policy(workspace)
    removed_files: list[PurePosixPath] = []
    removed_directories: list[PurePosixPath] = []
    unresolved: list[PurePosixPath] = []

    for path in reversed(files):
        try:
            target = policy.resolve(path, allow_missing=True)
            if target.kind is PathKind.MISSING:
                continue
            if target.kind is not PathKind.FILE:
                unresolved.append(path)
                continue
            target.absolute.unlink()
            removed_files.append(path)
        except (OSError, PolicyError):
            unresolved.append(path)

    for path in reversed(directories):
        try:
            target = policy.resolve(path, allow_missing=True)
            if target.kind is PathKind.MISSING:
                continue
            if target.kind is not PathKind.DIRECTORY:
                unresolved.append(path)
                continue
            target.absolute.rmdir()
            removed_directories.append(path)
        except (OSError, PolicyError):
            unresolved.append(path)

    return RollbackResult(
        removed_files=tuple(removed_files),
        removed_directories=tuple(removed_directories),
        unresolved=tuple(unresolved),
    )


def rollback_transaction(
    workspace: Workspace,
    backup: PreparedBackup | LoadedBackup,
    *,
    modified_files: tuple[PurePosixPath, ...],
    files: tuple[PurePosixPath, ...],
    directories: tuple[PurePosixPath, ...],
) -> RollbackResult:
    """Restore modified originals, then remove paths created by the attempt."""

    restored_files: list[PurePosixPath] = []
    unresolved: list[PurePosixPath] = []
    for path in reversed(modified_files):
        try:
            _restore_original(workspace, backup, path)
            restored_files.append(path)
        except (KeyError, OSError, PolicyError):
            unresolved.append(path)

    created = rollback_created(
        workspace,
        files=files,
        directories=directories,
    )
    return RollbackResult(
        removed_files=created.removed_files,
        removed_directories=created.removed_directories,
        unresolved=(*unresolved, *created.unresolved),
        restored_files=tuple(restored_files),
    )


def rollback_completed_backup(
    workspace: Workspace,
    backup: LoadedBackup,
) -> RollbackResult:
    """Preflight and roll back one previously completed transaction."""

    _preflight_manual_rollback(workspace, backup)
    modified = tuple(
        entry.path
        for entry in backup.entries
        if entry.kind is BackupEntryKind.FILE
        and entry.original_state is OriginalState.PRESENT
    )
    created_files = tuple(
        entry.path
        for entry in backup.entries
        if entry.kind is BackupEntryKind.FILE
        and entry.original_state is OriginalState.ABSENT
    )
    created_directories = tuple(
        entry.path
        for entry in backup.entries
        if entry.kind is BackupEntryKind.DIRECTORY
        and entry.original_state is OriginalState.ABSENT
    )
    return rollback_transaction(
        workspace,
        backup,
        modified_files=modified,
        files=created_files,
        directories=created_directories,
    )


def _restore_original(
    workspace: Workspace,
    backup: PreparedBackup | LoadedBackup,
    path: PurePosixPath,
) -> None:
    entry = backup.entry_for(path)
    if (
        entry.original_state is not OriginalState.PRESENT
        or entry.backup_path is None
        or entry.original_sha256 is None
        or entry.original_size is None
        or entry.original_mode is None
    ):
        raise OSError(f"backup entry cannot restore a modified file: {path}")

    raw = _read_original(backup, path)

    actions.atomic_restore_file(
        workspace,
        path,
        raw,
        mode=entry.original_mode,
    )
    actions.verify_restored_file(
        workspace,
        path,
        raw,
        mode=entry.original_mode,
    )


def _preflight_manual_rollback(
    workspace: Workspace,
    backup: LoadedBackup,
) -> None:
    policy = Policy(workspace)
    allowed = frozenset(entry.path for entry in backup.entries)
    for entry in backup.entries:
        try:
            target = policy.resolve(entry.path)
            metadata = target.absolute.lstat()
            if stat.S_IMODE(metadata.st_mode) != entry.applied_mode:
                raise OSError("applied mode changed")
            if entry.kind is BackupEntryKind.FILE:
                if (
                    target.kind is not PathKind.FILE
                    or entry.applied_size is None
                    or entry.applied_sha256 is None
                ):
                    raise OSError("applied file state is incomplete")
                before = target.absolute.lstat()
                raw = target.absolute.read_bytes()
                after = target.absolute.lstat()
                if (
                    _identity(before) != _identity(after)
                    or len(raw) != entry.applied_size
                    or hashlib.sha256(raw).hexdigest() != entry.applied_sha256
                ):
                    raise OSError("applied file changed after the job")
            elif target.kind is not PathKind.DIRECTORY:
                raise OSError("applied directory has the wrong type")
            if entry.original_state is OriginalState.PRESENT:
                _read_original(backup, entry.path)
        except (KeyError, OSError, PolicyError) as exc:
            raise ExecutionError(
                ExecutionErrorCode.ROLLBACK_FAILED,
                "manual rollback refused because the applied state changed or the backup is unsafe",
                path=entry.path.as_posix(),
                backup_path=backup.path,
                rollback_succeeded=False,
            ) from exc

    for entry in backup.entries:
        if entry.kind is not BackupEntryKind.DIRECTORY:
            continue
        target = workspace.root.joinpath(*entry.path.parts)
        pending = [target]
        while pending:
            directory = pending.pop()
            try:
                children = tuple(directory.iterdir())
            except OSError as exc:
                raise ExecutionError(
                    ExecutionErrorCode.ROLLBACK_FAILED,
                    "manual rollback could not inspect a created directory",
                    path=entry.path.as_posix(),
                    backup_path=backup.path,
                    rollback_succeeded=False,
                ) from exc
            for child in children:
                relative = PurePosixPath(child.relative_to(workspace.root).as_posix())
                if relative not in allowed:
                    raise ExecutionError(
                        ExecutionErrorCode.ROLLBACK_FAILED,
                        "manual rollback refused to remove a directory containing undeclared entries",
                        path=relative.as_posix(),
                        backup_path=backup.path,
                        rollback_succeeded=False,
                    )
                try:
                    mode = child.lstat().st_mode
                except OSError as exc:
                    raise ExecutionError(
                        ExecutionErrorCode.ROLLBACK_FAILED,
                        "manual rollback could not inspect a created path",
                        path=relative.as_posix(),
                        backup_path=backup.path,
                        rollback_succeeded=False,
                    ) from exc
                if stat.S_ISDIR(mode):
                    pending.append(child)


def _read_original(
    backup: PreparedBackup | LoadedBackup,
    path: PurePosixPath,
) -> bytes:
    entry = backup.entry_for(path)
    if (
        entry.original_state is not OriginalState.PRESENT
        or entry.backup_path is None
        or entry.original_sha256 is None
        or entry.original_size is None
        or entry.original_mode is None
    ):
        raise OSError(f"backup entry cannot restore a modified file: {path}")
    source = backup.path.joinpath(*entry.backup_path.parts)
    metadata = source.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or source.resolve() != source.absolute()
    ):
        raise OSError(f"original backup is not a regular file: {path}")
    raw = source.read_bytes()
    if (
        len(raw) != entry.original_size
        or hashlib.sha256(raw).hexdigest() != entry.original_sha256
    ):
        raise OSError(f"original backup failed integrity validation: {path}")
    return raw


def _identity(metadata) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_mode,
    )


__all__ = [
    "RollbackResult",
    "rollback_completed_backup",
    "rollback_created",
    "rollback_transaction",
]

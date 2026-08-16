"""Atomic replacement primitives for planned existing-file changes."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from patchshuttle.planner import PlannedFileChange
from patchshuttle.policy import PathKind, Policy, WorkspacePath
from patchshuttle.workspace import Workspace


class FileReplaceError(OSError):
    """A replacement failure with retained temporary-path context."""

    def __init__(
        self,
        message: str,
        *,
        target_modified: bool,
        temporary_path: Path | None = None,
    ) -> None:
        self.target_modified = target_modified
        self.temporary_path = temporary_path
        super().__init__(message)


def atomic_replace_file(
    workspace: Workspace,
    change: PlannedFileChange,
    *,
    mode: int,
) -> None:
    """Replace a still-matching existing file with its planned final bytes."""

    policy = Policy(workspace)
    target = _require_file(policy, change.path)
    _require_planned_original(target, change)
    _atomic_replace_bytes(
        target.absolute,
        change.content,
        mode=mode,
        before_publish=lambda: _require_planned_original(
            _require_file(policy, change.path),
            change,
        ),
    )


def verify_modified_file(
    workspace: Workspace,
    change: PlannedFileChange,
    *,
    mode: int,
) -> None:
    """Require exact planned bytes and preserved mode after replacement."""

    target = _require_file(Policy(workspace), change.path)
    raw = target.absolute.read_bytes()
    if (
        len(raw) != change.after_size
        or hashlib.sha256(raw).hexdigest() != change.after_sha256
        or raw != change.content
        or stat.S_IMODE(target.absolute.lstat().st_mode) != mode
    ):
        raise OSError(f"modified file failed post-state validation: {change.path}")


def atomic_restore_file(
    workspace: Workspace,
    path: PurePosixPath,
    content: bytes,
    *,
    mode: int,
) -> None:
    """Restore retained bytes to a regular or unexpectedly missing target."""

    policy = Policy(workspace)
    target = policy.resolve(path, allow_missing=True)
    if target.kind not in {PathKind.FILE, PathKind.MISSING}:
        raise OSError(f"rollback target has an unexpected type: {path}")
    parent = policy.resolve(path.parent, allow_root=True)
    _atomic_replace_bytes(parent.absolute / path.name, content, mode=mode)


def verify_restored_file(
    workspace: Workspace,
    path: PurePosixPath,
    content: bytes,
    *,
    mode: int,
) -> None:
    """Require exact original bytes and mode after a rollback restoration."""

    target = _require_file(Policy(workspace), path)
    raw = target.absolute.read_bytes()
    if raw != content or stat.S_IMODE(target.absolute.lstat().st_mode) != mode:
        raise OSError(f"restored file failed post-state validation: {path}")


def _require_file(policy: Policy, path: PurePosixPath) -> WorkspacePath:
    target = policy.resolve(path, allow_missing=True)
    if target.kind is not PathKind.FILE:
        raise OSError(f"planned modification target is not a regular file: {path}")
    return target


def _require_planned_original(
    target: WorkspacePath,
    change: PlannedFileChange,
) -> None:
    if change.before_size is None or change.before_sha256 is None:
        raise OSError(f"planned modification lacks an original hash: {change.path}")
    raw = target.absolute.read_bytes()
    if (
        len(raw) != change.before_size
        or hashlib.sha256(raw).hexdigest() != change.before_sha256
    ):
        raise OSError(f"modification target changed after planning: {change.path}")


def _atomic_replace_bytes(
    target: Path,
    content: bytes,
    *,
    mode: int,
    before_publish: Callable[[], None] | None = None,
) -> None:
    temporary = target.parent / f".patchshuttle-{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    replaced = False
    pending: BaseException | None = None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        if before_publish is not None:
            before_publish()
        os.replace(temporary, target)
        replaced = True
    except BaseException as exc:
        pending = exc

    cleanup_error: OSError | None = None
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        cleanup_error = exc

    if cleanup_error is not None:
        raise FileReplaceError(
            "temporary replacement data could not be removed",
            target_modified=replaced,
            temporary_path=temporary,
        ) from (pending or cleanup_error)
    if pending is not None:
        raise pending


__all__ = [
    "FileReplaceError",
    "atomic_replace_file",
    "atomic_restore_file",
    "verify_modified_file",
    "verify_restored_file",
]

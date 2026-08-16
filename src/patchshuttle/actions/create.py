"""Race-resistant creation primitives for already-approved plans."""

from __future__ import annotations

import errno
import hashlib
import os
import uuid
from pathlib import Path, PurePosixPath

from patchshuttle.planner import PlannedFileChange
from patchshuttle.policy import PathKind, Policy
from patchshuttle.workspace import Workspace

_LINK_FALLBACK_ERRORS = frozenset(
    value
    for value in (
        errno.EPERM,
        errno.EXDEV,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if value is not None
)


class FilePublishError(OSError):
    """A publication failure that reports which temporary paths now exist."""

    def __init__(
        self,
        message: str,
        *,
        target_created: bool,
        temporary_path: Path | None = None,
    ) -> None:
        self.target_created = target_created
        self.temporary_path = temporary_path
        super().__init__(message)


def create_directory(workspace: Workspace, path: PurePosixPath) -> None:
    """Create one missing planned directory without accepting races."""

    target = Policy(workspace).resolve(path, allow_missing=True)
    if target.kind is not PathKind.MISSING:
        raise FileExistsError(path.as_posix())
    target.absolute.mkdir()


def verify_created_directory(workspace: Workspace, path: PurePosixPath) -> None:
    """Require the post-state promised by a directory creation action."""

    target = Policy(workspace).resolve(path, allow_missing=True)
    if target.kind is not PathKind.DIRECTORY:
        raise OSError(f"created directory has an unexpected post-state: {path}")


def atomic_create_file(workspace: Workspace, change: PlannedFileChange) -> None:
    """Publish a complete new file without replacing an existing target."""

    policy = Policy(workspace)
    target = policy.resolve(change.path, allow_missing=True)
    if target.kind is not PathKind.MISSING:
        raise FileExistsError(change.path.as_posix())
    parent = policy.resolve(change.path.parent, allow_root=True)

    temporary = parent.absolute / f".patchshuttle-{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    published = False
    pending: BaseException | None = None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(change.content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target.absolute)
        except OSError as exc:
            if exc.errno not in _LINK_FALLBACK_ERRORS:
                raise
            _exclusive_copy(target.absolute, change.content)
        published = True
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
        target_created = published or (
            isinstance(pending, FilePublishError) and pending.target_created
        )
        raise FilePublishError(
            "temporary create-file data could not be removed",
            target_created=target_created,
            temporary_path=temporary,
        ) from (pending or cleanup_error)
    if pending is not None:
        raise pending


def verify_created_file(workspace: Workspace, change: PlannedFileChange) -> None:
    """Require exact bytes, length, and hash after a create-file action."""

    target = Policy(workspace).resolve(change.path, allow_missing=True)
    if target.kind is not PathKind.FILE:
        raise OSError(f"created file has an unexpected post-state: {change.path}")
    raw = target.absolute.read_bytes()
    if (
        len(raw) != change.after_size
        or hashlib.sha256(raw).hexdigest() != change.after_sha256
        or raw != change.content
    ):
        raise OSError(
            f"created file content failed post-state validation: {change.path}"
        )


def _exclusive_copy(target: Path, content: bytes) -> None:
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o666,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException as exc:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            raise FilePublishError(
                "a partial create-file target could not be removed",
                target_created=True,
            ) from cleanup_error
        raise exc


__all__ = [
    "FilePublishError",
    "atomic_create_file",
    "create_directory",
    "verify_created_directory",
    "verify_created_file",
]

"""Backup-manifest preparation for guarded change transactions."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath

from patchshuttle.errors import (
    ExecutionError,
    ExecutionErrorCode,
    PolicyError,
)
from patchshuttle.planner import FileDisposition, Plan, PlannedFileChange
from patchshuttle.policy import PathKind, Policy
from patchshuttle.workspace import Workspace

_MAX_MANIFEST_BYTES = 5_000_000


class BackupStatus(str, Enum):
    """Lifecycle states written to a Phase 8 backup manifest."""

    PREPARED = "PREPARED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CHANGES_KEPT = "CHANGES_KEPT"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class BackupEntryKind(str, Enum):
    """Filesystem kinds represented by a transaction manifest."""

    FILE = "file"
    DIRECTORY = "directory"


class OriginalState(str, Enum):
    """Whether a transaction target existed before execution."""

    ABSENT = "ABSENT"
    PRESENT = "PRESENT"


@dataclass(frozen=True, slots=True)
class BackupEntry:
    """One immutable manifest record and optional original-file copy."""

    path: PurePosixPath
    kind: BackupEntryKind
    original_state: OriginalState
    backup_path: PurePosixPath | None = None
    original_sha256: str | None = None
    original_size: int | None = None
    original_mode: int | None = None
    encoding: str | None = None
    newline: str | None = None
    applied_sha256: str | None = None
    applied_size: int | None = None
    applied_mode: int | None = None


@dataclass(frozen=True, slots=True)
class PreparedBackup:
    """One manifest directory created before project writes begin."""

    plan: Plan
    path: Path
    run_timestamp: str
    entries: tuple[BackupEntry, ...] = ()

    @property
    def manifest_path(self) -> Path:
        return self.path / "manifest.json"

    def entry_for(self, path: PurePosixPath) -> BackupEntry:
        """Return the retained manifest entry for one planned path."""

        for entry in self.entries:
            if entry.path == path:
                return entry
        raise KeyError(path)


@dataclass(frozen=True, slots=True)
class LoadedBackup:
    """A validated completed manifest reopened for manual rollback."""

    workspace: Workspace = field(repr=False)
    path: Path
    job_id: str
    job_hash: str
    run_timestamp: str
    entries: tuple[BackupEntry, ...]
    payload: dict = field(repr=False)

    @property
    def manifest_path(self) -> Path:
        return self.path / "manifest.json"

    def entry_for(self, path: PurePosixPath) -> BackupEntry:
        for entry in self.entries:
            if entry.path == path:
                return entry
        raise KeyError(path)


def prepare_backup(plan: Plan) -> PreparedBackup:
    """Capture every original before marking a transaction backup prepared."""

    timestamp = _run_timestamp()
    root = plan.workspace.patches_dir / "backups"
    job_root = root / plan.job.id
    run_root = job_root / timestamp
    try:
        _require_internal_directory(plan.workspace.patches_dir)
        _ensure_internal_directory(root)
        _ensure_internal_directory(job_root)
        run_root.mkdir()
    except (OSError, ValueError) as exc:
        raise ExecutionError(
            ExecutionErrorCode.BACKUP_FAILED,
            "backup directory could not be prepared",
            path=_relative_display(plan.workspace.root, run_root),
        ) from exc

    backup = PreparedBackup(plan=plan, path=run_root, run_timestamp=timestamp)
    try:
        backup = PreparedBackup(
            plan=plan,
            path=run_root,
            run_timestamp=timestamp,
            entries=_prepare_entries(backup),
        )
        update_backup(backup, BackupStatus.PREPARED)
    except ExecutionError:
        try:
            run_root.rmdir()
        except OSError:
            pass
        raise
    return backup


def update_backup(
    backup: PreparedBackup,
    status: BackupStatus,
    *,
    failure_code: ExecutionErrorCode | None = None,
    capture_applied_state: bool = False,
) -> None:
    """Atomically replace the manifest with a new lifecycle state."""

    payload = _manifest_payload(
        backup,
        status,
        failure_code=failure_code,
        capture_applied_state=capture_applied_state,
    )
    _write_manifest_payload(
        backup.manifest_path,
        payload,
        workspace_root=backup.plan.workspace.root,
        backup_path=backup.path,
    )


def load_completed_backup(
    workspace: Workspace,
    reference: str,
    *,
    job_id: str,
    job_hash: str,
) -> LoadedBackup:
    """Open and validate a completed project-local backup manifest."""

    backup_path = _resolve_backup_reference(
        workspace,
        reference,
        job_id=job_id,
    )
    manifest_path = backup_path / "manifest.json"
    try:
        metadata = manifest_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_MANIFEST_BYTES:
            raise OSError("manifest is not a bounded regular file")
        raw = manifest_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionError(
            ExecutionErrorCode.ROLLBACK_FAILED,
            "backup manifest could not be read",
            path=_relative_display(workspace.root, manifest_path),
            backup_path=backup_path,
            rollback_succeeded=False,
        ) from exc
    try:
        entries = _parse_completed_manifest(
            workspace,
            payload,
            job_id=job_id,
            job_hash=job_hash,
        )
        timestamp = _required_manifest(payload, "run_timestamp", str)
    except (TypeError, ValueError, PolicyError) as exc:
        raise ExecutionError(
            ExecutionErrorCode.ROLLBACK_FAILED,
            "backup manifest is invalid or is not a completed transaction",
            path=_relative_display(workspace.root, manifest_path),
            backup_path=backup_path,
            rollback_succeeded=False,
        ) from exc
    return LoadedBackup(
        workspace=workspace,
        path=backup_path,
        job_id=job_id,
        job_hash=job_hash,
        run_timestamp=timestamp,
        entries=entries,
        payload=payload,
    )


def update_loaded_backup(
    backup: LoadedBackup,
    status: BackupStatus,
    *,
    failure_code: ExecutionErrorCode | None = None,
) -> None:
    """Update lifecycle fields on a validated reopened manifest."""

    payload = dict(backup.payload)
    payload["status"] = status.value
    payload["failure_code"] = failure_code.value if failure_code is not None else None
    _write_manifest_payload(
        backup.manifest_path,
        payload,
        workspace_root=backup.workspace.root,
        backup_path=backup.path,
        error_code=ExecutionErrorCode.ROLLBACK_FAILED,
    )


def _write_manifest_payload(
    manifest_path: Path,
    payload: dict,
    *,
    workspace_root: Path,
    backup_path: Path,
    error_code: ExecutionErrorCode = ExecutionErrorCode.BACKUP_FAILED,
) -> None:
    temporary = backup_path / f".manifest-{uuid.uuid4().hex}.tmp"
    raw = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, manifest_path)
    except OSError as exc:
        raise ExecutionError(
            error_code,
            "backup manifest could not be written",
            path=_relative_display(workspace_root, manifest_path),
            backup_path=backup_path,
            rollback_succeeded=(
                False if error_code is ExecutionErrorCode.ROLLBACK_FAILED else None
            ),
        ) from exc
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _manifest_payload(
    backup: PreparedBackup,
    status: BackupStatus,
    *,
    failure_code: ExecutionErrorCode | None,
    capture_applied_state: bool = False,
) -> dict:
    plan = backup.plan
    applied = (
        _capture_applied_states(backup)
        if status is BackupStatus.COMPLETED and capture_applied_state
        else {}
    )
    payload = {
        "manifest_version": 1,
        "project_id": plan.job.project_id,
        "job_id": plan.job.id,
        "job_hash": plan.job_hash,
        "run_timestamp": backup.run_timestamp,
        "status": status.value,
        "failure_code": failure_code.value if failure_code is not None else None,
        "action_order": [f"{action.id}:{action.name}" for action in plan.actions],
        "formatting_targets": [path.as_posix() for path in plan.formatting_targets],
        "formatter_plan": [
            {
                "path": item.path.as_posix(),
                "formatter": item.formatter,
                "decision": item.decision.value,
                "baseline": item.baseline.value,
                "planned": item.planned.value,
            }
            for item in plan.formatter_plan
        ],
        "html_lint_targets": [path.as_posix() for path in plan.html_lint_targets],
        "entries": [_entry_payload(entry) for entry in backup.entries],
    }
    if applied:
        payload["applied_states"] = {
            path.as_posix(): value for path, value in applied.items()
        }
    return payload


def _prepare_entries(backup: PreparedBackup) -> tuple[BackupEntry, ...]:
    entries = [
        BackupEntry(
            path=path,
            kind=BackupEntryKind.DIRECTORY,
            original_state=OriginalState.ABSENT,
        )
        for path in backup.plan.directories_to_create
    ]
    for change in backup.plan.file_changes:
        if change.disposition is FileDisposition.CREATE:
            entries.append(
                BackupEntry(
                    path=change.path,
                    kind=BackupEntryKind.FILE,
                    original_state=OriginalState.ABSENT,
                )
            )
        else:
            entries.append(_capture_original(backup, change))
    return tuple(entries)


def _capture_original(
    backup: PreparedBackup,
    change: PlannedFileChange,
) -> BackupEntry:
    if change.before_sha256 is None or change.before_size is None:
        raise ExecutionError(
            ExecutionErrorCode.BACKUP_FAILED,
            "modified-file plan is missing its original fingerprint",
            path=change.path.as_posix(),
            backup_path=backup.path,
        )

    policy = Policy(backup.plan.workspace)
    try:
        target = policy.resolve(change.path, allow_missing=True)
        if target.kind is not PathKind.FILE:
            raise ExecutionError(
                ExecutionErrorCode.PLAN_STALE,
                "modified file no longer has the planned type",
                path=change.path.as_posix(),
                backup_path=backup.path,
            )
        before_metadata = target.absolute.lstat()
        raw = target.absolute.read_bytes()
        after_metadata = target.absolute.lstat()
        revalidated = policy.resolve(change.path, allow_missing=True)
    except ExecutionError:
        raise
    except (OSError, PolicyError) as exc:
        raise ExecutionError(
            ExecutionErrorCode.BACKUP_FAILED,
            "original file could not be captured",
            path=change.path.as_posix(),
            backup_path=backup.path,
        ) from exc

    metadata_changed = _metadata_identity(before_metadata) != _metadata_identity(
        after_metadata
    )
    digest = hashlib.sha256(raw).hexdigest()
    if (
        revalidated.kind is not PathKind.FILE
        or revalidated.absolute != target.absolute
        or metadata_changed
        or len(raw) != change.before_size
        or digest != change.before_sha256
    ):
        raise ExecutionError(
            ExecutionErrorCode.PLAN_STALE,
            "original file changed before backup capture completed",
            path=change.path.as_posix(),
            backup_path=backup.path,
        )

    backup_path = PurePosixPath("originals", *change.path.parts)
    _write_original(backup, backup_path, raw)
    return BackupEntry(
        path=change.path,
        kind=BackupEntryKind.FILE,
        original_state=OriginalState.PRESENT,
        backup_path=backup_path,
        original_sha256=digest,
        original_size=len(raw),
        original_mode=stat.S_IMODE(after_metadata.st_mode),
        encoding=change.encoding,
        newline=change.newline.value,
    )


def _write_original(
    backup: PreparedBackup,
    relative: PurePosixPath,
    raw: bytes,
) -> None:
    target = backup.path.joinpath(*relative.parts)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        try:
            target.unlink()
        except OSError:
            pass
        raise ExecutionError(
            ExecutionErrorCode.BACKUP_FAILED,
            "original file copy could not be written",
            path=relative.as_posix(),
            backup_path=backup.path,
        ) from exc


def _entry_payload(
    entry: BackupEntry,
) -> dict:
    payload = {
        "path": entry.path.as_posix(),
        "kind": entry.kind.value,
        "original_state": entry.original_state.value,
    }
    if entry.original_state is OriginalState.PRESENT:
        payload.update(
            {
                "backup_path": (
                    entry.backup_path.as_posix()
                    if entry.backup_path is not None
                    else None
                ),
                "original_sha256": entry.original_sha256,
                "original_size": entry.original_size,
                "original_mode": entry.original_mode,
                "encoding": entry.encoding,
                "newline": entry.newline,
            }
        )
    return payload


def _capture_applied_states(
    backup: PreparedBackup,
) -> dict[PurePosixPath, dict[str, object]]:
    policy = Policy(backup.plan.workspace)
    states: dict[PurePosixPath, dict[str, object]] = {}
    for entry in backup.entries:
        try:
            target = policy.resolve(entry.path)
            metadata = target.absolute.lstat()
            if entry.kind is BackupEntryKind.FILE:
                if target.kind is not PathKind.FILE:
                    raise OSError("completed file has the wrong type")
                raw = target.absolute.read_bytes()
                revalidated = policy.resolve(entry.path)
                if (
                    revalidated.kind is not PathKind.FILE
                    or revalidated.absolute != target.absolute
                    or len(raw) != metadata.st_size
                ):
                    raise OSError("completed file changed during capture")
                states[entry.path] = {
                    "kind": entry.kind.value,
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": len(raw),
                }
            else:
                if target.kind is not PathKind.DIRECTORY:
                    raise OSError("completed directory has the wrong type")
                states[entry.path] = {
                    "kind": entry.kind.value,
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "sha256": None,
                    "size": 0,
                }
        except (OSError, PolicyError) as exc:
            raise ExecutionError(
                ExecutionErrorCode.BACKUP_FAILED,
                "completed transaction state could not be retained",
                path=entry.path.as_posix(),
                backup_path=backup.path,
            ) from exc
    return states


def _resolve_backup_reference(
    workspace: Workspace,
    reference: str,
    *,
    job_id: str,
) -> Path:
    relative = PurePosixPath(reference)
    if (
        relative.is_absolute()
        or "\\" in reference
        or len(relative.parts) != 4
        or relative.parts[:3] != ("patches", "backups", job_id)
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ExecutionError(
            ExecutionErrorCode.ROLLBACK_FAILED,
            "registry backup reference is invalid",
            path=reference,
            rollback_succeeded=False,
        )
    target = workspace.root.joinpath(*relative.parts)
    current = workspace.root
    try:
        for part in relative.parts:
            current = current / part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise OSError("backup component is not a real directory")
        if target.resolve() != target.absolute():
            raise OSError("backup path resolves through an alias")
    except OSError as exc:
        raise ExecutionError(
            ExecutionErrorCode.ROLLBACK_FAILED,
            "backup reference is missing or unsafe",
            path=reference,
            backup_path=target,
            rollback_succeeded=False,
        ) from exc
    return target


def _parse_completed_manifest(
    workspace: Workspace,
    payload: object,
    *,
    job_id: str,
    job_hash: str,
) -> tuple[BackupEntry, ...]:
    if not isinstance(payload, dict):
        raise TypeError("manifest root must be an object")
    if _required_manifest(payload, "manifest_version", int) != 1:
        raise ValueError("manual rollback requires a version 1 manifest")
    if _required_manifest(payload, "project_id", str) != workspace.project_id:
        raise ValueError("manifest project ID does not match")
    if _required_manifest(payload, "job_id", str) != job_id:
        raise ValueError("manifest job ID does not match")
    if _required_manifest(payload, "job_hash", str) != job_hash:
        raise ValueError("manifest job hash does not match")
    if _required_manifest(payload, "status", str) != BackupStatus.COMPLETED.value:
        raise ValueError("manifest is not completed")
    raw_entries = payload.get("entries")
    applied_states = payload.get("applied_states")
    if not isinstance(raw_entries, list):
        raise TypeError("manifest entries must be an array")
    if not isinstance(applied_states, dict):
        raise TypeError("manifest is missing completed applied states")
    if len(raw_entries) > workspace.config.execution.max_inventory_entries:
        raise ValueError("manifest has too many entries")

    policy = Policy(workspace)
    entries: list[BackupEntry] = []
    seen: set[PurePosixPath] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise TypeError("manifest entry must be an object")
        path = policy.normalize(_required_manifest(raw_entry, "path", str))
        if not path.parts or policy.is_protected(path) or path in seen:
            raise ValueError("manifest entry path is invalid")
        seen.add(path)
        kind = BackupEntryKind(_required_manifest(raw_entry, "kind", str))
        original = OriginalState(_required_manifest(raw_entry, "original_state", str))
        if kind is BackupEntryKind.DIRECTORY and original is not OriginalState.ABSENT:
            raise ValueError("directory backup entry has an invalid original state")
        applied = applied_states.get(path.as_posix())
        if not isinstance(applied, dict):
            raise TypeError("manifest entry is missing its applied state")
        if _required_manifest(applied, "kind", str) != kind.value:
            raise ValueError("applied entry kind does not match")
        applied_mode = _required_manifest(applied, "mode", int)
        applied_size = _required_manifest(applied, "size", int)
        applied_sha = applied.get("sha256")
        if kind is BackupEntryKind.FILE:
            if not isinstance(applied_sha, str) or applied_size < 0:
                raise TypeError("applied file fingerprint is invalid")
        elif applied_sha is not None or applied_size != 0:
            raise ValueError("applied directory fingerprint is invalid")

        backup_relative: PurePosixPath | None = None
        original_sha: str | None = None
        original_size: int | None = None
        original_mode: int | None = None
        encoding: str | None = None
        newline: str | None = None
        if original is OriginalState.PRESENT:
            backup_relative = _safe_backup_copy_path(
                _required_manifest(raw_entry, "backup_path", str)
            )
            original_sha = _required_manifest(raw_entry, "original_sha256", str)
            original_size = _required_manifest(raw_entry, "original_size", int)
            original_mode = _required_manifest(raw_entry, "original_mode", int)
            encoding = _required_manifest(raw_entry, "encoding", str)
            newline = _required_manifest(raw_entry, "newline", str)
        entries.append(
            BackupEntry(
                path=path,
                kind=kind,
                original_state=original,
                backup_path=backup_relative,
                original_sha256=original_sha,
                original_size=original_size,
                original_mode=original_mode,
                encoding=encoding,
                newline=newline,
                applied_sha256=applied_sha,
                applied_size=applied_size,
                applied_mode=applied_mode,
            )
        )
    return tuple(entries)


def _safe_backup_copy_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] != "originals"
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
    ):
        raise ValueError("original backup path is invalid")
    return path


def _required_manifest(payload: dict, key: str, expected: type):
    value = payload.get(key)
    if type(value) is not expected:
        raise TypeError(f"manifest field {key} has an invalid type")
    return value


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_mode,
    )


def _require_internal_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"internal path is not a real directory: {path}")


def _ensure_internal_directory(path: Path) -> None:
    try:
        path.mkdir()
    except FileExistsError:
        _require_internal_directory(path)


def _relative_display(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _run_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y_%m_%d_%H%M%S_%f")


__all__ = [
    "BackupEntry",
    "BackupEntryKind",
    "BackupStatus",
    "LoadedBackup",
    "OriginalState",
    "PreparedBackup",
    "load_completed_backup",
    "prepare_backup",
    "update_backup",
    "update_loaded_backup",
]

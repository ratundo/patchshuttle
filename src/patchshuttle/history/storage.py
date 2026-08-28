"""Exclusive storage and bounded readers for structured job history."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from patchshuttle.history.models import (
    HistoryError,
    HistoryErrorCode,
    HistoryListResult,
    HistoryRecord,
    HistoryWriteResult,
)
from patchshuttle.history.records import build_history_record
from patchshuttle.logging import RunLogData, redact_text
from patchshuttle.workspace import Workspace

_MAX_HISTORY_BYTES = 1_000_000
_MAX_HISTORY_RECORDS = 10_000
_MAX_HISTORY_LIST_LIMIT = 1_000
_MAX_WARNING_BYTES = 2_048
_JOB_ID = re.compile(r"^[A-Z][A-Z0-9_-]{2,63}$")
_RECORD_STEM = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


def write_history_record(data: RunLogData, *, log_path: Path) -> Path:
    """Create one exclusive append-only JSON record or raise HistoryError."""

    directory = _history_job_directory(data.workspace, data.job.id, create=True)
    assert directory is not None
    stem = f"{data.clock.filename_timestamp}_{data.job_hash[:8]}"
    path = _unique_path(directory, stem=stem)
    record = build_history_record(
        data,
        log_path=log_path,
        record_id=f"{data.job.id}/{path.stem}",
    )
    payload = (
        json.dumps(
            record.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > _MAX_HISTORY_BYTES:
        raise HistoryError(
            HistoryErrorCode.HISTORY_WRITE_FAILED,
            "history record exceeds the bounded record size",
            path=_relative(data.workspace, path),
        )
    _write_new_file(path, payload)
    return path


def try_write_history_record(data: RunLogData, *, log_path: Path) -> HistoryWriteResult:
    """Persist secondary history without failing or rolling back the job."""

    try:
        return HistoryWriteResult(
            path=write_history_record(data, log_path=log_path),
            warning=None,
        )
    except (
        Exception
    ) as exc:  # noqa: BLE001 - secondary artifacts are non-fatal by policy
        message, _ = _bounded_text(redact_text(str(exc)), _MAX_WARNING_BYTES)
        return HistoryWriteResult(
            path=None,
            warning=f"{type(exc).__name__}: {message}",
        )


def read_history_record(workspace: Workspace, reference: str) -> HistoryRecord:
    """Read one exact ``JOB_ID/RECORD_ID`` reference with bounded validation."""

    job_id, stem = _parse_reference(reference)
    directory = _history_job_directory(workspace, job_id, create=False)
    if directory is None:
        raise HistoryError(
            HistoryErrorCode.HISTORY_NOT_FOUND,
            "history job directory was not found",
            path=reference,
        )
    path = directory / f"{stem}.json"
    return _load_history_path(workspace, path, expected_reference=reference)


def list_history_records(
    workspace: Workspace,
    *,
    job_id: str | None = None,
    limit: int = 50,
) -> HistoryListResult:
    """Return newest records after a bounded safe scan."""

    if not 1 <= limit <= _MAX_HISTORY_LIST_LIMIT:
        raise HistoryError(
            HistoryErrorCode.HISTORY_LIMIT_EXCEEDED,
            f"history limit must be between 1 and {_MAX_HISTORY_LIST_LIMIT}",
        )
    if job_id is not None:
        _validate_job_id(job_id)
    root = _history_root(workspace, create=False)
    if root is None:
        return HistoryListResult(records=(), limited=False)
    if job_id is not None:
        directories = [root / job_id]
    else:
        try:
            directories = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise HistoryError(
                HistoryErrorCode.HISTORY_READ_FAILED,
                "history root could not be inspected",
                path=_relative(workspace, root),
            ) from exc
    paths: list[Path] = []
    for directory in directories:
        if not directory.exists() and not directory.is_symlink():
            continue
        if not _JOB_ID.fullmatch(directory.name):
            continue
        _require_directory(directory, workspace)
        try:
            candidates = sorted(directory.glob("*.json"), key=lambda item: item.name)
        except OSError as exc:
            raise HistoryError(
                HistoryErrorCode.HISTORY_READ_FAILED,
                "history directory could not be inspected",
                path=_relative(workspace, directory),
            ) from exc
        paths.extend(candidates)
        if len(paths) > _MAX_HISTORY_RECORDS:
            raise HistoryError(
                HistoryErrorCode.HISTORY_LIMIT_EXCEEDED,
                f"history scan exceeds {_MAX_HISTORY_RECORDS} records",
            )
    records = [
        _load_history_path(
            workspace,
            path,
            expected_reference=f"{path.parent.name}/{path.stem}",
        )
        for path in paths
    ]
    records.sort(key=lambda item: (item.occurred_at, item.record_id), reverse=True)
    return HistoryListResult(
        records=tuple(records[:limit]),
        limited=len(records) > limit,
    )


def latest_history_record(
    workspace: Workspace,
    *,
    job_id: str | None = None,
) -> HistoryRecord:
    """Return the newest record globally or for one job ID."""

    listing = list_history_records(workspace, job_id=job_id, limit=1)
    if not listing.records:
        raise HistoryError(
            HistoryErrorCode.HISTORY_NOT_FOUND,
            "workspace does not contain a matching history record",
            path=job_id,
        )
    return listing.records[0]


def _parse_reference(reference: str) -> tuple[str, str]:
    if "\\" in reference:
        raise _invalid_reference(reference)
    parts = PurePosixPath(reference).parts
    if len(parts) != 2:
        raise _invalid_reference(reference)
    job_id, stem = parts
    _validate_job_id(job_id)
    if not _RECORD_STEM.fullmatch(stem):
        raise _invalid_reference(reference)
    return job_id, stem


def _validate_job_id(job_id: str) -> None:
    if not _JOB_ID.fullmatch(job_id):
        raise HistoryError(
            HistoryErrorCode.HISTORY_INVALID,
            "history job ID is invalid",
            path=job_id,
        )


def _invalid_reference(reference: str) -> HistoryError:
    return HistoryError(
        HistoryErrorCode.HISTORY_INVALID,
        "history reference must be JOB_ID/RECORD_ID",
        path=reference,
    )


def _history_root(workspace: Workspace, *, create: bool) -> Path | None:
    _require_directory(workspace.patches_dir, workspace)
    root = workspace.patches_dir / "history"
    if not root.exists() and not root.is_symlink():
        if not create:
            return None
        _create_directory(root, workspace)
    _require_directory(root, workspace)
    return root


def _history_job_directory(
    workspace: Workspace,
    job_id: str,
    *,
    create: bool,
) -> Path | None:
    _validate_job_id(job_id)
    root = _history_root(workspace, create=create)
    if root is None:
        return None
    directory = root / job_id
    if not directory.exists() and not directory.is_symlink():
        if not create:
            return None
        _create_directory(directory, workspace)
    _require_directory(directory, workspace)
    return directory


def _create_directory(path: Path, workspace: Workspace) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise HistoryError(
            HistoryErrorCode.HISTORY_WRITE_FAILED,
            "history directory could not be created",
            path=_relative(workspace, path),
        ) from exc


def _require_directory(path: Path, workspace: Workspace) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HistoryError(
            HistoryErrorCode.HISTORY_READ_FAILED,
            "history directory could not be inspected",
            path=_relative(workspace, path),
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise HistoryError(
            HistoryErrorCode.HISTORY_INVALID,
            "history path must be a real directory",
            path=_relative(workspace, path),
        )


def _unique_path(directory: Path, *, stem: str) -> Path:
    candidate = directory / f"{stem}.json"
    counter = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = directory / f"{stem}_{counter}.json"
        counter += 1
    return candidate


def _write_new_file(path: Path, payload: bytes) -> None:
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise HistoryError(
            HistoryErrorCode.HISTORY_WRITE_FAILED,
            "history record could not be written",
            path=path.as_posix(),
        ) from exc


def _load_history_path(
    workspace: Workspace,
    path: Path,
    *,
    expected_reference: str,
) -> HistoryRecord:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise OSError("history record is not a regular file")
        if metadata.st_size > _MAX_HISTORY_BYTES:
            raise HistoryError(
                HistoryErrorCode.HISTORY_LIMIT_EXCEEDED,
                "history record exceeds the read limit",
                path=_relative(workspace, path),
            )
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise HistoryError(
            HistoryErrorCode.HISTORY_NOT_FOUND,
            "history record was not found",
            path=expected_reference,
        ) from exc
    except HistoryError:
        raise
    except OSError as exc:
        raise HistoryError(
            HistoryErrorCode.HISTORY_READ_FAILED,
            "history record could not be read",
            path=_relative(workspace, path),
        ) from exc
    try:
        record = HistoryRecord.model_validate_json(raw)
    except (UnicodeError, ValidationError, ValueError) as exc:
        raise HistoryError(
            HistoryErrorCode.HISTORY_INVALID,
            "history record does not match the supported schema",
            path=_relative(workspace, path),
        ) from exc
    if (
        record.project_id != workspace.project_id
        or record.record_id != expected_reference
    ):
        raise HistoryError(
            HistoryErrorCode.HISTORY_INVALID,
            "history record identity does not match its workspace path",
            path=_relative(workspace, path),
        )
    return record


def _bounded_text(value: str, max_bytes: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def _relative(workspace: Workspace, path: Path) -> str:
    try:
        return path.relative_to(workspace.root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "latest_history_record",
    "list_history_records",
    "read_history_record",
    "try_write_history_record",
    "write_history_record",
]

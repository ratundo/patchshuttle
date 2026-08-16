"""Atomic project-local job identity and lifecycle registry."""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.models import JobKind
from patchshuttle.workspace import Workspace

_REGISTRY_RELATIVE_PATH = Path("patches/state/registry.json")
_MAX_REGISTRY_BYTES = 5_000_000


class RegistryDecision(str, Enum):
    """Identity decision made before an execution attempt."""

    PROCEED = "PROCEED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    PATCH_ID_CONFLICT = "PATCH_ID_CONFLICT"


@dataclass(frozen=True, slots=True)
class RegistryJobRecord:
    """Validated latest project-local state for one stable job ID."""

    job_id: str
    job_hash: str
    kind: str
    first_run_at: str
    latest_run_at: str
    latest_result: str
    backup_reference: str | None
    rollback_state: str
    archived_job_copy: str
    completed: bool
    run_count: int


@dataclass(frozen=True, slots=True)
class Registry:
    """Immutable validated view of ``patches/state/registry.json``."""

    project_id: str
    jobs: dict[str, RegistryJobRecord]


def load_registry(workspace: Workspace) -> Registry:
    """Read and validate an atomically published registry snapshot."""

    path = workspace.root / _REGISTRY_RELATIVE_PATH
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_REGISTRY_BYTES:
            raise OSError("registry is not a bounded regular file")
        raw = path.read_bytes()
    except OSError as exc:
        raise _registry_error("workspace registry could not be read") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
        registry = _parse_registry(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _registry_error("workspace registry is invalid") from exc

    if registry.project_id != workspace.project_id:
        raise _registry_error("workspace registry project ID does not match config")
    return registry


def decide_job(
    registry: Registry,
    *,
    job_id: str,
    job_hash: str,
) -> RegistryDecision:
    """Apply the protocol's stable-ID and normalized-hash rules."""

    existing = registry.jobs.get(job_id)
    if existing is None:
        return RegistryDecision.PROCEED
    if existing.job_hash != job_hash:
        return RegistryDecision.PATCH_ID_CONFLICT
    if existing.completed:
        return RegistryDecision.ALREADY_APPLIED
    return RegistryDecision.PROCEED


def update_registry(
    workspace: Workspace,
    registry: Registry,
    *,
    job_id: str,
    job_hash: str,
    kind: JobKind,
    occurred_at: str,
    result: str,
    backup_path: Path | None,
    rollback_state: str,
    archived_job_path: Path,
    completed: bool,
    reset_completed: bool = False,
) -> RegistryJobRecord:
    """Atomically retain the latest run state while preserving job identity.

    The caller must hold ``patches/state/run.lock`` across its decision and
    this write.
    """

    existing = registry.jobs.get(job_id)
    established_hash = existing.job_hash if existing is not None else job_hash
    established_kind = existing.kind if existing is not None else kind.value
    record = RegistryJobRecord(
        job_id=job_id,
        job_hash=established_hash,
        kind=established_kind,
        first_run_at=(existing.first_run_at if existing is not None else occurred_at),
        latest_run_at=occurred_at,
        latest_result=result,
        backup_reference=(
            _relative_path(workspace, backup_path)
            if backup_path is not None
            else (existing.backup_reference if existing is not None else None)
        ),
        rollback_state=rollback_state,
        archived_job_copy=_relative_path(workspace, archived_job_path),
        completed=(
            completed
            if reset_completed
            else completed or (existing.completed if existing is not None else False)
        ),
        run_count=(existing.run_count + 1 if existing is not None else 1),
    )
    jobs = dict(registry.jobs)
    jobs[job_id] = record
    _write_registry(
        workspace,
        Registry(project_id=registry.project_id, jobs=jobs),
    )
    return record


def get_job(registry: Registry, job_id: str) -> RegistryJobRecord:
    """Return one job or raise a stable read-command error."""

    try:
        return registry.jobs[job_id]
    except KeyError as exc:
        raise ExecutionError(
            ExecutionErrorCode.JOB_NOT_FOUND,
            "job ID is not present in the workspace registry",
            item_id=job_id,
        ) from exc


def _parse_registry(payload: Any) -> Registry:
    if not isinstance(payload, dict):
        raise TypeError("registry root must be an object")
    project_id = payload.get("project_id")
    raw_jobs = payload.get("jobs")
    if not isinstance(project_id, str) or not isinstance(raw_jobs, dict):
        raise TypeError("registry project_id and jobs are required")
    jobs: dict[str, RegistryJobRecord] = {}
    for job_id, value in raw_jobs.items():
        if not isinstance(job_id, str) or not isinstance(value, dict):
            raise TypeError("registry jobs must be keyed objects")
        record = RegistryJobRecord(
            job_id=_required(value, "job_id", str),
            job_hash=_required(value, "job_hash", str),
            kind=_required(value, "kind", str),
            first_run_at=_required(value, "first_run_at", str),
            latest_run_at=_required(value, "latest_run_at", str),
            latest_result=_required(value, "latest_result", str),
            backup_reference=_optional_string(value, "backup_reference"),
            rollback_state=_required(value, "rollback_state", str),
            archived_job_copy=_required(value, "archived_job_copy", str),
            completed=_required(value, "completed", bool),
            run_count=_required(value, "run_count", int),
        )
        if record.job_id != job_id or record.run_count < 1:
            raise ValueError("registry job identity or run count is invalid")
        jobs[job_id] = record
    return Registry(project_id=project_id, jobs=jobs)


def _required(value: dict[str, Any], key: str, expected: type):
    item = value.get(key)
    if type(item) is not expected:
        raise TypeError(f"registry field {key} has an invalid type")
    return item


def _optional_string(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is not None and not isinstance(item, str):
        raise TypeError(f"registry field {key} has an invalid type")
    return item


def _write_registry(workspace: Workspace, registry: Registry) -> None:
    path = workspace.root / _REGISTRY_RELATIVE_PATH
    temporary = path.parent / f".registry-{uuid.uuid4().hex}.tmp"
    payload = {
        "jobs": {
            job_id: _record_payload(record)
            for job_id, record in sorted(registry.jobs.items())
        },
        "project_id": registry.project_id,
    }
    raw = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise _registry_error("workspace registry could not be written") from exc
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _record_payload(record: RegistryJobRecord) -> dict[str, object]:
    return {
        "archived_job_copy": record.archived_job_copy,
        "backup_reference": record.backup_reference,
        "completed": record.completed,
        "first_run_at": record.first_run_at,
        "job_hash": record.job_hash,
        "job_id": record.job_id,
        "kind": record.kind,
        "latest_result": record.latest_result,
        "latest_run_at": record.latest_run_at,
        "rollback_state": record.rollback_state,
        "run_count": record.run_count,
    }


def _relative_path(workspace: Workspace, path: Path) -> str:
    try:
        return path.relative_to(workspace.root).as_posix()
    except ValueError as exc:  # pragma: no cover - internal path invariant
        raise _registry_error("operational path is outside the workspace") from exc


def _registry_error(message: str) -> ExecutionError:
    return ExecutionError(
        ExecutionErrorCode.OPERATIONAL_RECORD_FAILED,
        message,
        path=_REGISTRY_RELATIVE_PATH.as_posix(),
    )


__all__ = [
    "Registry",
    "RegistryDecision",
    "RegistryJobRecord",
    "decide_job",
    "get_job",
    "load_registry",
    "update_registry",
]

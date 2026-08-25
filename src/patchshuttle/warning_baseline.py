"""Explicit project-local baselines for Django system-check warnings."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.workspace import Workspace

WARNING_BASELINE_RELATIVE_PATH = Path("patches/state/warning-baseline.json")
WARNING_BASELINE_SCHEMA = "patchshuttle.warning_baseline.v1"
_MAX_BASELINE_BYTES = 1_000_000
_WARNING_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.W[0-9]{3,}$")
_GROUP_HEADER = re.compile(r"^(?:CRITICALS|ERRORS|WARNINGS|INFOS|DEBUGS):$")
_MESSAGE = re.compile(r"^.+?: (?:\((?P<warning_id>[^()]+)\) )?(?P<message>.+)$")


@dataclass(frozen=True, slots=True)
class WarningBaseline:
    """Validated immutable warning IDs accepted for one project."""

    project_id: str
    django_check_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WarningAnalysis:
    """Advisory classification of one captured Django check output."""

    status: str
    known_warnings: int | None
    new_warnings: int | None
    new_warning_details: tuple[str, ...]


def empty_warning_baseline(workspace: Workspace) -> WarningBaseline:
    return WarningBaseline(project_id=workspace.project_id, django_check_ids=())


def load_warning_baseline(workspace: Workspace) -> WarningBaseline:
    """Read a bounded regular baseline, or return empty for an old workspace."""

    path = workspace.root / WARNING_BASELINE_RELATIVE_PATH
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return empty_warning_baseline(workspace)
    except OSError as exc:
        raise _baseline_error("warning baseline could not be inspected") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_BASELINE_BYTES:
        raise _baseline_error("warning baseline is not a bounded regular file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _baseline_error("warning baseline could not be read") from exc
    if len(raw) > _MAX_BASELINE_BYTES:
        raise _baseline_error("warning baseline exceeds its size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
        baseline = _parse_baseline(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _baseline_error("warning baseline is invalid") from exc
    if baseline.project_id != workspace.project_id:
        raise _baseline_error("warning baseline project ID does not match config")
    return baseline


def update_warning_baseline(
    workspace: Workspace,
    *,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
) -> WarningBaseline:
    """Atomically update explicit IDs while the caller holds the run lock."""

    additions = normalize_warning_ids(add)
    removals = normalize_warning_ids(remove)
    overlap = additions & removals
    if overlap:
        joined = ", ".join(sorted(overlap))
        raise ValueError(f"warning IDs cannot be added and removed together: {joined}")
    current = load_warning_baseline(workspace)
    updated = WarningBaseline(
        project_id=workspace.project_id,
        django_check_ids=tuple(
            sorted((set(current.django_check_ids) | additions) - removals)
        ),
    )
    _write_warning_baseline(workspace, updated)
    return updated


def normalize_warning_ids(values: Iterable[str]) -> frozenset[str]:
    normalized = frozenset(values)
    invalid = sorted(value for value in normalized if not _WARNING_ID.fullmatch(value))
    if invalid:
        raise ValueError(
            "Django warning IDs must match applabel.W001: " + ", ".join(invalid)
        )
    return normalized


def analyze_django_warning_output(
    stdout: str,
    stderr: str,
    *,
    known_ids: frozenset[str],
    output_truncated: bool,
) -> WarningAnalysis:
    """Classify captured Django WARNINGS records without changing check status."""

    if output_truncated:
        return WarningAnalysis(
            status="INCOMPLETE_TRUNCATED",
            known_warnings=None,
            new_warnings=None,
            new_warning_details=(),
        )
    records = (*_parse_warning_records(stderr), *_parse_warning_records(stdout))
    known = 0
    new_details: list[str] = []
    for warning_id, detail in records:
        if warning_id is not None and warning_id in known_ids:
            known += 1
        else:
            new_details.append(detail)
    return WarningAnalysis(
        status="COMPLETE",
        known_warnings=known,
        new_warnings=len(new_details),
        new_warning_details=tuple(new_details),
    )


def render_warning_baseline(
    baseline: WarningBaseline,
    *,
    updated: bool,
) -> str:
    lines = [
        "WARNING_BASELINE_UPDATED" if updated else "WARNING_BASELINE",
        f"schema: {WARNING_BASELINE_SCHEMA}",
        f"project_id: {baseline.project_id}",
    ]
    if baseline.django_check_ids:
        lines.append("django_check_ids:")
        lines.extend(f"  - {warning_id}" for warning_id in baseline.django_check_ids)
    else:
        lines.append("django_check_ids: []")
    return "\n".join(lines) + "\n"


def _parse_warning_records(value: str) -> tuple[tuple[str | None, str], ...]:
    records: list[tuple[str | None, str]] = []
    in_warnings = False
    current_id: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_id, current_lines
        if current_lines:
            records.append((current_id, "\n".join(current_lines)))
        current_id = None
        current_lines = []

    for line in value.splitlines():
        if _GROUP_HEADER.fullmatch(line):
            flush()
            in_warnings = line == "WARNINGS:"
            continue
        if not in_warnings:
            continue
        if current_lines and line.startswith(("\t", "  ")):
            current_lines.append(line)
            continue
        match = _MESSAGE.fullmatch(line)
        if match is not None:
            flush()
            candidate = match.group("warning_id")
            current_id = (
                candidate
                if candidate is not None and _WARNING_ID.fullmatch(candidate)
                else None
            )
            current_lines = [line]
        elif not line:
            flush()
        else:
            flush()
    flush()
    return tuple(records)


def _parse_baseline(payload: Any) -> WarningBaseline:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "project_id",
        "django_check_ids",
    }:
        raise TypeError("warning baseline must contain the exact v1 fields")
    if payload["schema"] != WARNING_BASELINE_SCHEMA:
        raise ValueError("warning baseline schema is unsupported")
    project_id = payload["project_id"]
    raw_ids = payload["django_check_ids"]
    if not isinstance(project_id, str) or not isinstance(raw_ids, list):
        raise TypeError("warning baseline fields have invalid types")
    if any(not isinstance(value, str) for value in raw_ids):
        raise TypeError("warning baseline IDs must be strings")
    ids = normalize_warning_ids(raw_ids)
    if len(ids) != len(raw_ids):
        raise ValueError("warning baseline IDs must not contain duplicates")
    return WarningBaseline(project_id=project_id, django_check_ids=tuple(sorted(ids)))


def _write_warning_baseline(
    workspace: Workspace,
    baseline: WarningBaseline,
) -> None:
    path = workspace.root / WARNING_BASELINE_RELATIVE_PATH
    temporary = path.parent / f".warning-baseline-{uuid.uuid4().hex}.tmp"
    payload = {
        "django_check_ids": list(baseline.django_check_ids),
        "project_id": baseline.project_id,
        "schema": WARNING_BASELINE_SCHEMA,
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
        raise _baseline_error("warning baseline could not be written") from exc
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _baseline_error(message: str) -> ExecutionError:
    return ExecutionError(
        ExecutionErrorCode.OPERATIONAL_RECORD_FAILED,
        message,
        path=WARNING_BASELINE_RELATIVE_PATH.as_posix(),
    )


__all__ = [
    "WARNING_BASELINE_RELATIVE_PATH",
    "WARNING_BASELINE_SCHEMA",
    "WarningAnalysis",
    "WarningBaseline",
    "analyze_django_warning_output",
    "empty_warning_baseline",
    "load_warning_baseline",
    "normalize_warning_ids",
    "render_warning_baseline",
    "update_warning_baseline",
]

"""Predictable UTF-8 run logs, exact job archives, and best-effort redaction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from patchshuttle._version import __version__
from patchshuttle.audit import AuditActionResult
from patchshuttle.checks import CheckResult
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.formatter_policy import FORMATTER_ORDER, FormatterName
from patchshuttle.formatters import FormatterResult
from patchshuttle.inventory import WorkspaceComparison
from patchshuttle.linters import HtmlLintResult
from patchshuttle.models import Job
from patchshuttle.planner import ActionDisposition, Plan
from patchshuttle.runner import TransactionResult
from patchshuttle.selfdoc import (
    AUDIT_ACTIONS,
    CHANGE_ACTIONS,
    CHECKS,
    JOB_KINDS,
    format_capability_list,
)
from patchshuttle.workspace import Workspace

STANDARD_SECTIONS = (
    "HEADER",
    "WORKSPACE",
    "JOB",
    "PLAN",
    "AUDIT",
    "BACKUP",
    "ACTIONS",
    "LINT_HTML",
    "INITIAL_CHECKS",
    "FORMAT_ISORT",
    "FORMAT_BLACK",
    "FINAL_CHECKS",
    "WORKSPACE_COMPARISON",
    "ROLLBACK",
    "SUMMARY",
    "PATCHSHUTTLE_AI_HANDOFF",
)

_AVAILABLE_JOB_KINDS = format_capability_list(JOB_KINDS)
_AVAILABLE_AUDIT_ACTIONS = format_capability_list(AUDIT_ACTIONS)
_AVAILABLE_CHANGE_ACTIONS = format_capability_list(CHANGE_ACTIONS)
_AVAILABLE_CHECKS = format_capability_list(CHECKS)
_CAPABILITIES_HASH = hashlib.sha256(
    "\n".join(
        (
            f"job_kinds:{_AVAILABLE_JOB_KINDS}",
            f"audit_actions:{_AVAILABLE_AUDIT_ACTIONS}",
            f"change_actions:{_AVAILABLE_CHANGE_ACTIONS}",
            f"checks:{_AVAILABLE_CHECKS}",
        )
    ).encode("utf-8")
).hexdigest()[:12]
_PRIVATE_KEY = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----.*?" r"-----END \1-----",
    re.DOTALL,
)
_AUTHORIZATION = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s\"']+"
)
_ASSIGNMENT = re.compile(
    r"(?i)(?P<prefix>[\"']?\b(?P<key>api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|password|passwd|secret|token)[\"']?\s*[:=]\s*)"
    r"(?P<value>[\"'][^\"'\r\n]*[\"']|[A-Za-z_][A-Za-z0-9_.]*\([^,\r\n]*\)|[^\s,;)\]}]+)"
)
_SAFE_ANNOTATION_VALUES = frozenset(
    {"any", "bytearray", "bytes", "memoryview", "none", "str"}
)
_PYTHON_CALL_VALUE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\([^,\r\n]*\)")
_FLAG_VALUE = re.compile(
    r"(?i)(--(?:api-key|access-token|auth-token|client-secret|password|secret|token)"
    r"(?:=|\s+))[^\s\"']+"
)
_KNOWN_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{16,})(?![A-Za-z0-9])"
)


@dataclass(frozen=True, slots=True)
class RunClock:
    """One timezone-aware instant shared by all artifacts for a run."""

    occurred_at: datetime

    @property
    def iso_timestamp(self) -> str:
        return self.occurred_at.isoformat(timespec="seconds")

    @property
    def filename_timestamp(self) -> str:
        return self.occurred_at.strftime("%Y_%m_%d_%H_%M_%S")


@dataclass(frozen=True, slots=True)
class RunLogData:
    """Complete bounded information used to render one stable run log."""

    workspace: Workspace
    job: Job
    job_hash: str
    clock: RunClock
    result: str
    exit_code: int
    failure_stage: str | None
    failure_code: str | None
    archived_job_path: Path
    plan: Plan | None = None
    transaction: TransactionResult | None = None
    error: ExecutionError | None = None
    audit_results: tuple[AuditActionResult, ...] = ()
    verification_checks: tuple[CheckResult, ...] = ()
    workspace_comparison: WorkspaceComparison | None = None
    manual_rollback: ManualRollbackLogRecord | None = None


@dataclass(frozen=True, slots=True)
class ManualRollbackLogRecord:
    """Paths and outcome recorded for a user-requested rollback."""

    status: str
    backup_path: Path
    restored_files: tuple[PurePosixPath, ...] = ()
    removed_files: tuple[PurePosixPath, ...] = ()
    removed_directories: tuple[PurePosixPath, ...] = ()
    unresolved: tuple[PurePosixPath, ...] = ()


@dataclass(frozen=True, slots=True)
class AttemptLogData:
    """Bounded metadata for a validation or planning failure."""

    workspace: Workspace
    clock: RunClock
    command: str
    job_file: Path
    result: str
    failure_stage: str
    failure_code: str
    exit_code: int
    error: str
    job: Job | None = None
    job_hash: str | None = None
    failed_item: str | None = None
    failed_path: str | None = None


def current_run_clock(workspace: Workspace) -> RunClock:
    """Resolve the configured local or IANA timezone for a new run."""

    setting = workspace.config.logging.timezone
    instant = _utc_now()
    try:
        if setting.lower() == "local":
            localized = instant.astimezone()
        elif setting.upper() == "UTC":
            localized = instant.astimezone(timezone.utc)
        else:
            localized = instant.astimezone(ZoneInfo(setting))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise _record_error(
            "configured log timezone is invalid",
            path="patches/patchshuttle.toml",
        ) from exc
    return RunClock(localized)


def archive_job_source(
    workspace: Workspace,
    *,
    job: Job,
    job_hash: str,
    clock: RunClock,
    source: bytes,
    successful: bool,
) -> Path:
    """Store an exact immutable source copy in ``applied`` or ``failed``."""

    directory = workspace.patches_dir / ("applied" if successful else "failed")
    _require_managed_directory(workspace, directory)
    stem = f"{job.id}_{clock.filename_timestamp}_{job_hash[:8]}"
    path = _unique_path(directory, stem=stem, suffix=".psh.yaml")
    _write_new_file(path, source)
    return path


def write_run_log(data: RunLogData) -> Path:
    """Render, redact, and publish one fixed-section run log."""

    directory = data.workspace.patches_dir / "logs"
    _require_managed_directory(data.workspace, directory)
    stem = f"log_{data.clock.filename_timestamp}_{data.job.id}"
    path = _unique_path(directory, stem=stem, suffix=".log")
    rendered = _render_log(data, path)
    if data.workspace.config.logging.redact_known_secrets:
        rendered = redact_text(rendered)
    _write_new_file(path, rendered.encode("utf-8"))
    return path


def write_named_log(
    workspace: Workspace,
    *,
    clock: RunClock,
    label: str,
    content: str,
) -> Path:
    """Publish one bounded non-job snapshot or handoff log."""

    if not re.fullmatch(r"[A-Z][A-Z0-9_-]{1,31}", label):
        raise ValueError("operational log label is invalid")
    directory = workspace.patches_dir / "logs"
    _require_managed_directory(workspace, directory)
    stem = f"log_{clock.filename_timestamp}_{label}"
    path = _unique_path(directory, stem=stem, suffix=".log")
    rendered = content if content.endswith("\n") else content + "\n"
    if workspace.config.logging.redact_known_secrets:
        rendered = redact_text(rendered)
    _write_new_file(path, rendered.encode("utf-8"))
    return path


def write_attempt_log(data: AttemptLogData) -> Path:
    """Write an AI-readable log for an early validation or planning failure."""

    if data.result not in {"VALIDATION_FAILED", "PLAN_FAILED"}:
        raise ValueError("attempt log result is invalid")
    redaction = (
        "BEST_EFFORT_ENABLED"
        if data.workspace.config.logging.redact_known_secrets
        else "DISABLED_BY_LOCAL_POLICY"
    )
    job_id = data.job.id if data.job is not None else "UNKNOWN"
    kind = data.job.kind.value if data.job is not None else "UNKNOWN"
    content = "\n".join(
        (
            "=== PATCHSHUTTLE_ATTEMPT ===",
            f"patchshuttle_version: {__version__}",
            "protocol: 1",
            f"timestamp: {data.clock.iso_timestamp}",
            f"redaction: {redaction}",
            "redaction_guarantee: NONE",
            f"project_id: {data.workspace.project_id}",
            f"workspace_root: {data.workspace.root.as_posix()}",
            f"command: {_scalar(data.command)}",
            f"job_file: {_scalar(data.job_file.as_posix())}",
            f"job_id: {job_id}",
            "job_project_id: "
            + (data.job.project_id if data.job is not None else "UNKNOWN"),
            f"job_hash: {data.job_hash or 'UNKNOWN'}",
            f"kind: {kind}",
            "error:",
            *(f"  {line}" for line in data.error.replace("\r", "\\r").split("\n")),
            "archived_job_copy: NOT_APPLICABLE",
            "registry_updated: false",
            "",
            "=== SUMMARY ===",
            f"result: {data.result}",
            f"failure_stage: {data.failure_stage}",
            f"failure_code: {data.failure_code}",
            f"failed_item: {_scalar(data.failed_item or 'NOT_APPLICABLE')}",
            f"failed_path: {_scalar(data.failed_path or 'NOT_APPLICABLE')}",
            f"exit_code: {data.exit_code}",
            "changed_files: []",
            "created_files: []",
            "created_directories: []",
            "rollback_status: NOT_STARTED",
            "next_recommended_step: return_this_log_to_the_ai_for_a_corrected_job",
            "",
            "=== PATCHSHUTTLE_AI_HANDOFF ===",
            "protocol: 1",
            f"project_id: {data.workspace.project_id}",
            f"job_id: {job_id}",
            f"job_hash: {data.job_hash or 'UNKNOWN'}",
            f"kind: {kind}",
            f"result: {data.result}",
            f"failure_stage: {data.failure_stage}",
            f"failure_code: {data.failure_code}",
            f"failed_item: {_scalar(data.failed_item or 'NOT_APPLICABLE')}",
            f"failed_path: {_scalar(data.failed_path or 'NOT_APPLICABLE')}",
            "rollback: NOT_STARTED",
            "ai_handoff_version: 2",
            f"capabilities_hash: {_CAPABILITIES_HASH}",
            "next_expected_response: corrected_patch_or_audit",
            "=== END_PATCHSHUTTLE_AI_HANDOFF ===",
        )
    )
    return write_named_log(
        data.workspace,
        clock=data.clock,
        label=data.result,
        content=content,
    )


def render_latest_ai_log(path: Path, *, json_output: bool) -> str:
    """Read one existing log and return a bounded compact AI view."""

    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise _record_error(
                "latest log is not a safe regular file",
                path=path.as_posix(),
            )
        if metadata.st_size > 5_000_000:
            raise _record_error(
                "latest log exceeds the compact-view size limit",
                path=path.as_posix(),
            )
        raw = path.read_bytes()
    except ExecutionError:
        raise
    except OSError as exc:
        raise _record_error(
            "latest log could not be read",
            path=path.as_posix(),
        ) from exc
    if len(raw) > 5_000_000:
        raise _record_error(
            "latest log exceeds the compact-view size limit",
            path=path.as_posix(),
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise _record_error(
            "latest log is not valid UTF-8",
            path=path.as_posix(),
        ) from exc

    from patchshuttle._ai_log import render_ai_log

    try:
        return render_ai_log(
            text,
            source=path.as_posix(),
            json_output=json_output,
        )
    except ValueError as exc:
        raise _record_error(
            "latest log does not support a compact AI view",
            path=path.as_posix(),
        ) from exc


def latest_log_path(workspace: Workspace) -> Path:
    """Return the newest safe regular PatchShuttle log by mtime and name."""

    directory = workspace.patches_dir / "logs"
    _require_managed_directory(workspace, directory)
    candidates: list[tuple[int, str, Path]] = []
    try:
        for path in directory.iterdir():
            if not path.name.startswith("log_") or path.suffix != ".log":
                continue
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode):
                candidates.append((metadata.st_mtime_ns, path.name, path))
    except OSError as exc:
        raise _record_error("log directory could not be inspected") from exc
    if not candidates:
        raise ExecutionError(
            ExecutionErrorCode.LOG_NOT_FOUND,
            "workspace does not contain a PatchShuttle run log",
            path="patches/logs",
        )
    return max(candidates)[2]


def capabilities_hash() -> str:
    """Return a stable short hash for the installed declarative capabilities."""

    return _CAPABILITIES_HASH


def _redact_assignment(match: re.Match[str]) -> str:
    prefix = match.group("prefix")
    candidate = match.group("value")
    if candidate.startswith(('"', "'")):
        return prefix + "[REDACTED]"

    key = match.group("key").casefold().replace("-", "_")
    normalized = candidate.casefold()
    if (
        normalized == key
        or normalized in _SAFE_ANNOTATION_VALUES
        or _PYTHON_CALL_VALUE.fullmatch(candidate) is not None
    ):
        return match.group(0)
    return prefix + "[REDACTED]"


def redact_text(value: str) -> str:
    """Mask common credential shapes without claiming exhaustive removal."""

    value = _PRIVATE_KEY.sub(
        lambda match: (
            f"-----BEGIN {match.group(1)}-----\n"
            "[REDACTED PRIVATE KEY]\n"
            f"-----END {match.group(1)}-----"
        ),
        value,
    )
    value = _AUTHORIZATION.sub(r"\1[REDACTED]", value)
    value = _ASSIGNMENT.sub(_redact_assignment, value)
    value = _FLAG_VALUE.sub(r"\1[REDACTED]", value)
    return _KNOWN_TOKEN.sub("[REDACTED]", value)


def _render_log(data: RunLogData, log_path: Path) -> str:
    sections = {
        "HEADER": _header_section(data),
        "WORKSPACE": _workspace_section(data),
        "JOB": _job_section(data),
        "PLAN": _plan_section(data.plan),
        "AUDIT": _audit_section(data),
        "BACKUP": _backup_section(data),
        "ACTIONS": _actions_section(data),
        "LINT_HTML": _html_lint_section(data),
        "INITIAL_CHECKS": _checks_section(_split_checks(data)[0], data.workspace),
        "FORMAT_ISORT": _formatter_section(
            _formatter(data, "isort"),
            data.workspace,
            skipped_by_local_policy=_formatter_skipped(data, "isort"),
        ),
        "FORMAT_BLACK": _formatter_section(
            _formatter(data, "black"),
            data.workspace,
            skipped_by_local_policy=_formatter_skipped(data, "black"),
        ),
        "FINAL_CHECKS": _checks_section(_split_checks(data)[1], data.workspace),
        "WORKSPACE_COMPARISON": _comparison_section(data),
        "ROLLBACK": _rollback_section(data),
        "SUMMARY": _summary_section(data, log_path),
        "PATCHSHUTTLE_AI_HANDOFF": _handoff_section(data),
    }
    lines: list[str] = []
    for name in STANDARD_SECTIONS:
        lines.append(f"=== {name} ===")
        lines.append(sections[name])
    lines.append("=== END_PATCHSHUTTLE_AI_HANDOFF ===")
    return "\n".join(lines) + "\n"


def _header_section(data: RunLogData) -> str:
    redaction = (
        "BEST_EFFORT_ENABLED"
        if data.workspace.config.logging.redact_known_secrets
        else "DISABLED_BY_LOCAL_POLICY"
    )
    return _fields(
        patchshuttle_version=__version__,
        protocol=1,
        timestamp=data.clock.iso_timestamp,
        redaction=redaction,
        redaction_guarantee="NONE",
    )


def _workspace_section(data: RunLogData) -> str:
    return _fields(
        project_id=data.workspace.project_id,
        root=data.workspace.root.as_posix(),
        origin=data.workspace.origin.value,
    )


def _job_section(data: RunLogData) -> str:
    return _fields(
        job_id=data.job.id,
        job_hash=data.job_hash,
        kind=data.job.kind.value,
        title=data.job.title,
        description=data.job.description,
        archived_job_copy=_relative(data.workspace, data.archived_job_path),
    )


def _plan_section(plan: Plan | None) -> str:
    if plan is None:
        return "NOT_APPLICABLE"
    lines = [
        f"planned_actions: {len(plan.actions)}",
        f"planned_checks: {len(plan.checks)}",
        "project_python: "
        + (
            plan.project_python.as_posix()
            if plan.project_python is not None
            else "NOT_APPLICABLE"
        ),
        f"files_to_create: {_json_paths(plan.files_to_create)}",
        f"files_to_modify: {_json_paths(plan.files_to_modify)}",
        f"directories_to_create: {_json_paths(plan.directories_to_create)}",
        f"formatting_scope: {_json_paths(plan.formatting_targets)}",
        f"formatter_plan: {len(plan.formatter_plan)}",
    ]
    for item in plan.formatter_plan:
        lines.append(
            "formatter: "
            f"{item.path.as_posix()} -> {item.formatter} {item.decision.value} "
            f"(baseline={item.baseline.value}, planned={item.planned.value})"
        )
        if item.baseline.value == "INCOMPATIBLE":
            lines.append(f"formatter_baseline_detail: {_scalar(item.baseline_detail)}")
        if item.planned.value == "INCOMPATIBLE":
            lines.append(f"formatter_planned_detail: {_scalar(item.planned_detail)}")
    lines.extend(
        (
            f"html_lint_scope: {_json_paths(plan.html_lint_targets)}",
            f"preflight_checks: {len(plan.preflight_checks)}",
            "protected_paths: PASS",
            f"automatic_rollback: {'enabled' if plan.auto_rollback else 'disabled'}",
        )
    )
    return "\n".join(lines)


def _backup_section(data: RunLogData) -> str:
    backup = _backup_path(data)
    if backup is None:
        return "NOT_APPLICABLE"
    status = "COMPLETED"
    if data.error is not None:
        if data.error.rollback_skipped:
            status = "CHANGES_KEPT" if data.error.changes_kept else "FAILED"
        else:
            status = {
                True: "ROLLED_BACK",
                False: "ROLLBACK_FAILED",
                None: "FAILED",
            }[data.error.rollback_succeeded]
    return _fields(path=_relative(data.workspace, backup), status=status)


def _actions_section(data: RunLogData) -> str:
    if data.plan is None or not data.plan.actions or data.job.kind.value == "audit":
        return "NOT_APPLICABLE"
    records: list[str] = []
    for action in data.plan.actions:
        if (
            data.error is not None
            and data.error.code is ExecutionErrorCode.USER_DECLINED
        ):
            status = "NOT_STARTED"
        elif data.error is not None and data.error.item_id == action.id:
            status = "FAILED"
        elif data.error is not None:
            status = "UNKNOWN_AFTER_FAILURE"
        elif action.disposition is ActionDisposition.NO_CHANGE:
            status = "NO_CHANGE"
        else:
            status = "COMPLETED"
        records.append(
            _fields(
                action_id=action.id,
                action_type=action.name,
                path_or_scope=_json_paths(action.paths),
                status=status,
                started_at=(
                    "NOT_STARTED" if status == "NOT_STARTED" else "TRANSACTION_SCOPE"
                ),
                duration_ms=(0 if status == "NOT_STARTED" else "NOT_RECORDED"),
                expected=action.disposition.value,
                actual=status,
                details=action.detail,
            )
        )
    return "\n---\n".join(records)


def _audit_section(data: RunLogData) -> str:
    results = data.audit_results
    if data.error is not None and data.error.audit_results:
        results = data.error.audit_results
    if not results:
        return "NOT_APPLICABLE"
    records: list[str] = []
    for item in results:
        header = _fields(
            action_id=item.id,
            action_type=item.name,
            path_or_scope=_json_paths(item.scope),
            status=item.status,
            started_at=item.started_at,
            duration_ms=item.duration_ms,
            expected="READ_ONLY_OBSERVATION",
            actual=item.status,
            details=(
                "OUTPUT_TRUNCATED" if item.output_truncated else "OUTPUT_COMPLETE"
            ),
        )
        records.append(f"{header}\noutput_begin\n{item.output}\noutput_end")
    return "\n---\n".join(records)


def _checks_section(
    checks: tuple[CheckResult, ...],
    workspace: Workspace,
) -> str:
    if not checks:
        return "NOT_APPLICABLE"
    return "\n---\n".join(_check_record(item, workspace) for item in checks)


def _html_lint_section(data: RunLogData) -> str:
    results = _html_lints(data)
    if not results:
        return "NOT_APPLICABLE"
    include_output = data.workspace.config.logging.include_command_output
    return "\n---\n".join(
        _fields(
            lint_id=item.id,
            linter=item.name,
            path=item.path.as_posix(),
            argument_summary=json.dumps(item.argv, ensure_ascii=False),
            working_directory=item.working_directory.as_posix(),
            timeout=item.timeout_seconds,
            exit_code=item.return_code,
            duration_ms=item.duration_ms,
            stdout=(item.stdout if include_output else "OMITTED_BY_LOCAL_POLICY"),
            stderr=(item.stderr if include_output else "OMITTED_BY_LOCAL_POLICY"),
            stdout_truncated=item.stdout_truncated,
            stderr_truncated=item.stderr_truncated,
            status=item.status.value,
        )
        for item in results
    )


def _check_record(item: CheckResult, workspace: Workspace) -> str:
    include_output = workspace.config.logging.include_command_output
    return _fields(
        check_id=item.id,
        profile=item.name,
        argument_summary=json.dumps(item.argv, ensure_ascii=False),
        working_directory=item.working_directory.as_posix(),
        timeout=item.timeout_seconds,
        exit_code=item.return_code,
        duration_ms=item.duration_ms,
        warning_analysis=item.warning_analysis,
        known_warnings=item.known_warnings,
        new_warnings=item.new_warnings,
        new_warning_details=json.dumps(
            list(item.new_warning_details),
            ensure_ascii=False,
        ),
        stdout=(item.stdout if include_output else "OMITTED_BY_LOCAL_POLICY"),
        stderr=(item.stderr if include_output else "OMITTED_BY_LOCAL_POLICY"),
        stdout_truncated=item.stdout_truncated,
        stderr_truncated=item.stderr_truncated,
        status=item.status.value,
    )


def _formatter_section(
    item: FormatterResult | None,
    workspace: Workspace,
    *,
    skipped_by_local_policy: bool,
) -> str:
    if item is None:
        return "SKIPPED_LOCAL_POLICY" if skipped_by_local_policy else "NOT_APPLICABLE"
    include_output = workspace.config.logging.include_command_output
    return _fields(
        formatter_id=item.id,
        formatter=item.name,
        argument_summary=json.dumps(item.argv, ensure_ascii=False),
        working_directory=item.working_directory.as_posix(),
        timeout=item.timeout_seconds,
        exit_code=item.return_code,
        duration_ms=item.duration_ms,
        stdout=(item.stdout if include_output else "OMITTED_BY_LOCAL_POLICY"),
        stderr=(item.stderr if include_output else "OMITTED_BY_LOCAL_POLICY"),
        stdout_truncated=item.stdout_truncated,
        stderr_truncated=item.stderr_truncated,
        status=item.status.value,
    )


def _formatter_skipped(data: RunLogData, name: FormatterName) -> bool:
    return bool(
        data.plan is not None
        and data.plan.formatting_targets
        and not data.plan.formatter_paths(name)
    )


def _comparison_section(data: RunLogData) -> str:
    comparison = (
        data.workspace_comparison
        if data.workspace_comparison is not None
        else (
            data.transaction.workspace_comparison
            if data.transaction is not None
            else data.error.workspace_comparison if data.error is not None else None
        )
    )
    if comparison is None:
        return "NOT_APPLICABLE"
    lines = [
        f"status: {'PASS' if comparison.success else 'UNEXPECTED_CHANGES'}",
        f"changes: {len(comparison.changes)}",
        f"unexpected_changes: {len(comparison.unexpected_changes)}",
    ]
    lines.extend(
        "change: "
        + json.dumps(
            {
                "expected": change.expected,
                "kind": change.kind.value,
                "path": change.path.as_posix(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for change in comparison.changes
    )
    return "\n".join(lines)


def _rollback_section(data: RunLogData) -> str:
    if data.manual_rollback is not None:
        item = data.manual_rollback
        return _fields(
            status=item.status,
            cause="USER_REQUESTED",
            backup=_relative(data.workspace, item.backup_path),
            restored_files=_json_paths(item.restored_files),
            removed_files=_json_paths(item.removed_files),
            removed_directories=_json_paths(item.removed_directories),
            unresolved=_json_paths(item.unresolved),
        )
    if data.error is None:
        return "NOT_APPLICABLE"
    state = _automatic_rollback_state(data.error)
    return _fields(
        status=state,
        cause=(
            data.error.cause_code.value
            if data.error.cause_code is not None
            else data.error.code.value
        ),
        backup=(
            _relative(data.workspace, data.error.backup_path)
            if data.error.backup_path is not None
            else None
        ),
    )


def _summary_section(data: RunLogData, log_path: Path) -> str:
    created_files, created_directories, modified_files = _changed_paths(data)
    initial, final = _split_checks(data)
    formatters = _formatters(data)
    html_lints = _html_lints(data)
    expected_formatters = (
        ()
        if data.plan is None
        else tuple(name for name in FORMATTER_ORDER if data.plan.formatter_paths(name))
    )
    if data.plan is None or not data.plan.formatting_targets:
        formatting_status = "NOT_APPLICABLE"
    elif not expected_formatters:
        formatting_status = "SKIPPED_LOCAL_POLICY"
    elif len(formatters) == len(expected_formatters) and all(
        item.success for item in formatters
    ):
        formatting_status = "PASSED"
    else:
        formatting_status = "FAILED" if formatters else "NOT_STARTED"
    html_lint_status = (
        "NOT_APPLICABLE"
        if data.plan is None or not data.plan.html_lint_targets
        else (
            "PASSED"
            if len(html_lints) == len(data.plan.html_lint_targets)
            and all(item.success for item in html_lints)
            else "FAILED" if html_lints else "NOT_STARTED"
        )
    )
    rollback = (
        data.manual_rollback.status
        if data.manual_rollback is not None
        else (
            "NOT_REQUIRED"
            if data.error is None
            else _automatic_rollback_state(data.error)
        )
    )
    return _fields(
        result=data.result,
        failure_stage=data.failure_stage,
        failure_code=data.failure_code,
        exit_code=data.exit_code,
        changed_files=_json_paths((*created_files, *modified_files)),
        created_files=_json_paths(created_files),
        created_directories=_json_paths(created_directories),
        checks_passed=sum(item.success for item in (*initial, *final)),
        html_lint_status=html_lint_status,
        formatting_status=formatting_status,
        rollback_status=rollback,
        log_path=_relative(data.workspace, log_path),
        next_recommended_step=_next_step(data.result),
    )


def _handoff_section(data: RunLogData) -> str:
    rollback = (
        data.manual_rollback.status
        if data.manual_rollback is not None
        else (
            "NOT_REQUIRED"
            if data.error is None
            else _automatic_rollback_state(data.error)
        )
    )
    return _fields(
        protocol=1,
        project_id=data.workspace.project_id,
        job_id=data.job.id,
        job_hash=data.job_hash[:8],
        kind=data.job.kind.value,
        result=data.result,
        failure_stage=data.failure_stage,
        failure_code=data.failure_code,
        failed_item=(data.error.item_id if data.error is not None else None),
        rollback=rollback,
        ai_handoff_version=2,
        capabilities_hash=_CAPABILITIES_HASH,
        next_expected_response=(
            "next_patch_or_audit"
            if data.result in {"COMPLETED", "NO_CHANGE", "ALREADY_APPLIED"}
            else (
                "same_job_after_user_approval"
                if data.result == "USER_DECLINED"
                else "corrected_patch_or_audit"
            )
        ),
    )


def _automatic_rollback_state(error: ExecutionError) -> str:
    if error.rollback_skipped:
        return "SKIPPED_CHANGES_KEPT" if error.changes_kept else "SKIPPED_NO_CHANGES"
    return {None: "NOT_STARTED", True: "SUCCESS", False: "FAILED"}[
        error.rollback_succeeded
    ]


def _split_checks(
    data: RunLogData,
) -> tuple[tuple[CheckResult, ...], tuple[CheckResult, ...]]:
    if data.transaction is not None:
        return data.transaction.initial_checks, data.transaction.final_checks
    if data.verification_checks:
        return data.verification_checks, ()
    if data.error is None or data.plan is None:
        return (), ()
    checks = data.error.check_results
    if not data.error.formatting_results:
        return checks, ()
    boundary = min(len(data.plan.checks), len(checks))
    return checks[:boundary], checks[boundary:]


def _formatters(data: RunLogData) -> tuple[FormatterResult, ...]:
    if data.transaction is not None:
        return data.transaction.formatting_results
    if data.error is not None:
        return data.error.formatting_results
    return ()


def _html_lints(data: RunLogData) -> tuple[HtmlLintResult, ...]:
    if data.transaction is not None:
        return data.transaction.html_lint_results
    if data.error is not None:
        return data.error.html_lint_results
    return ()


def _formatter(data: RunLogData, name: str) -> FormatterResult | None:
    return next((item for item in _formatters(data) if item.name == name), None)


def _changed_paths(data: RunLogData) -> tuple[tuple, tuple, tuple]:
    if data.transaction is None:
        return (), (), ()
    return (
        data.transaction.created_files,
        data.transaction.created_directories,
        data.transaction.modified_files,
    )


def _backup_path(data: RunLogData) -> Path | None:
    if data.transaction is not None:
        return data.transaction.backup_path
    if data.error is not None:
        return data.error.backup_path
    return None


def _fields(**values: object) -> str:
    return "\n".join(f"{key}: {_scalar(value)}" for key, value in values.items())


def _scalar(value: object) -> str:
    if value is None:
        return "NOT_APPLICABLE"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _json_paths(paths: tuple) -> str:
    return json.dumps([path.as_posix() for path in paths], ensure_ascii=False)


def _next_step(result: str) -> str:
    if result in {"COMPLETED", "NO_CHANGE", "ALREADY_APPLIED"}:
        return "review_log_and_continue"
    if result == "PATCH_ID_CONFLICT":
        return "use_a_new_job_id_or_restore_the_original_job_content"
    if result == "USER_DECLINED":
        return "review_the_plan_and_run_it_only_when_ready"
    return "return_this_log_to_the_ai_for_a_corrected_job"


def _relative(workspace: Workspace, path: Path) -> str:
    try:
        return path.relative_to(workspace.root).as_posix()
    except ValueError:
        return path.as_posix()


def _require_managed_directory(workspace: Workspace, directory: Path) -> None:
    try:
        relative = directory.relative_to(workspace.root)
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError("managed path is not a directory")
    except (OSError, ValueError) as exc:
        raise _record_error(
            "managed artifact directory is missing or unsafe",
            path=(
                relative.as_posix() if "relative" in locals() else directory.as_posix()
            ),
        ) from exc


def _unique_path(directory: Path, *, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def _write_new_file(path: Path, content: bytes) -> None:
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise _record_error(
            "operational artifact could not be written",
            path=path.as_posix(),
        ) from exc


def _record_error(message: str, *, path: str | None = None) -> ExecutionError:
    return ExecutionError(
        ExecutionErrorCode.OPERATIONAL_RECORD_FAILED,
        message,
        path=path,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "AttemptLogData",
    "ManualRollbackLogRecord",
    "RunClock",
    "RunLogData",
    "STANDARD_SECTIONS",
    "archive_job_source",
    "capabilities_hash",
    "current_run_clock",
    "latest_log_path",
    "redact_text",
    "render_latest_ai_log",
    "write_run_log",
    "write_named_log",
    "write_attempt_log",
]

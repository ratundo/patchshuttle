"""Controlled isort and Black execution for immutable formatting scopes."""

from __future__ import annotations

import hashlib
import stat
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath

from patchshuttle._process import ProcessCommand, run_process
from patchshuttle.formatter_policy import (
    BLACK_POLICY_OPTIONS,
    FORMATTER_ORDER,
    FormatterDecision,
    FormatterName,
)
from patchshuttle.planner import Plan
from patchshuttle.policy import PathKind, Policy


class FormatterStatus(str, Enum):
    """Observable outcome of one controlled formatter process."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class PreparedFormatter:
    """One fixed formatter command over the approved changed-Python scope."""

    id: str
    name: FormatterName
    argv: tuple[str, ...]
    working_directory: Path
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class FormatterResult:
    """Bounded captured outcome of one launched formatter."""

    id: str
    name: FormatterName
    status: FormatterStatus
    argv: tuple[str, ...]
    working_directory: Path
    timeout_seconds: int
    return_code: int | None
    duration_ms: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool

    @property
    def success(self) -> bool:
        return self.status is FormatterStatus.PASSED


@dataclass(frozen=True, slots=True)
class FormatterRunResult:
    """Ordered formatter results through the first failure, if any."""

    results: tuple[FormatterResult, ...]

    @property
    def success(self) -> bool:
        return all(result.success for result in self.results)

    @property
    def failed(self) -> FormatterResult | None:
        return next((result for result in self.results if not result.success), None)


@dataclass(frozen=True, slots=True)
class FormattedFileState:
    """Exact bounded post-formatter state retained through final checks."""

    path: PurePosixPath
    sha256: str
    size: int
    mode: int
    content: bytes = field(repr=False)


def prepare_formatters(plan: Plan) -> tuple[PreparedFormatter, ...]:
    """Build fixed isort-then-Black commands from the immutable plan scope."""

    formatting = plan.workspace.config.formatting
    expected_targets = (
        tuple(
            change.path for change in plan.file_changes if change.path.suffix == ".py"
        )
        if formatting.enabled
        else ()
    )
    if plan.formatting_targets != expected_targets:
        raise ValueError("plan formatting targets do not match changed Python files")
    expected_decisions = tuple(
        (
            path,
            name,
            (
                FormatterDecision.SKIP_LOCAL_POLICY
                if path.as_posix() in frozenset(getattr(formatting, f"{name}_exclude"))
                else FormatterDecision.RUN
            ),
        )
        for path in expected_targets
        for name in FORMATTER_ORDER
    )
    actual_decisions = tuple(
        (item.path, item.formatter, item.decision) for item in plan.formatter_plan
    )
    if actual_decisions != expected_decisions:
        raise ValueError("plan formatter policy does not match local configuration")
    if not expected_targets:
        return ()
    if formatting.order != FORMATTER_ORDER:
        raise ValueError("protocol 1 requires isort then Black formatter order")

    timeout = plan.workspace.config.execution.default_timeout_seconds
    commands: list[PreparedFormatter] = []
    for index, name in enumerate(FORMATTER_ORDER, start=1):
        paths = tuple(path.as_posix() for path in plan.formatter_paths(name))
        if not paths:
            continue
        options = ("--overwrite-in-place",) if name == "isort" else BLACK_POLICY_OPTIONS
        commands.append(
            PreparedFormatter(
                id=f"formatter_{index:03d}",
                name=name,
                argv=(
                    sys.executable,
                    "-I",
                    "-m",
                    name,
                    *options,
                    "--",
                    *paths,
                ),
                working_directory=plan.workspace.root,
                timeout_seconds=timeout,
            )
        )
    return tuple(commands)


def run_formatters(plan: Plan) -> FormatterRunResult:
    """Run isort and Black sequentially, stopping at the first failure."""

    maximum = plan.workspace.config.execution.max_command_output_bytes
    results: list[FormatterResult] = []
    for formatter in prepare_formatters(plan):
        process = run_process(
            ProcessCommand(
                argv=formatter.argv,
                working_directory=formatter.working_directory,
                timeout_seconds=formatter.timeout_seconds,
            ),
            maximum_output_bytes=maximum,
        )
        result = FormatterResult(
            id=formatter.id,
            name=formatter.name,
            status=FormatterStatus(process.status.value),
            argv=formatter.argv,
            working_directory=formatter.working_directory,
            timeout_seconds=formatter.timeout_seconds,
            return_code=process.return_code,
            duration_ms=process.duration_ms,
            stdout=process.stdout,
            stderr=process.stderr,
            stdout_truncated=process.stdout_truncated,
            stderr_truncated=process.stderr_truncated,
        )
        results.append(result)
        if not result.success:
            break
    return FormatterRunResult(results=tuple(results))


def capture_formatted_files(plan: Plan) -> tuple[FormattedFileState, ...]:
    """Capture exact regular-file states for every approved formatter target."""

    policy = Policy(plan.workspace)
    maximum = plan.workspace.config.execution.max_single_file_bytes
    return tuple(
        _capture_file(policy, path, maximum=maximum)
        for path in plan.formatter_run_paths
    )


def verify_formatted_files(
    plan: Plan,
    expected: tuple[FormattedFileState, ...],
) -> None:
    """Require formatter targets to retain their captured exact final state."""

    if tuple(item.path for item in expected) != plan.formatter_run_paths:
        raise ValueError("formatted-file snapshot scope does not match the plan")
    if capture_formatted_files(plan) != expected:
        raise OSError("a formatter target changed after formatting")


def _capture_file(
    policy: Policy,
    path: PurePosixPath,
    *,
    maximum: int,
) -> FormattedFileState:
    target = policy.resolve(path, allow_missing=True)
    if target.kind is not PathKind.FILE:
        raise OSError(f"formatter target is not a regular file: {path}")
    before = target.absolute.lstat()
    if before.st_size > maximum:
        raise OSError(f"formatter output exceeds the configured size limit: {path}")
    raw = target.absolute.read_bytes()
    after_target = policy.resolve(path, allow_missing=True)
    if after_target.kind is not PathKind.FILE:
        raise OSError(f"formatter target is not a regular file: {path}")
    after = after_target.absolute.lstat()
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_mode,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_mode,
    )
    if before_identity != after_identity or len(raw) != after.st_size:
        raise OSError(f"formatter target changed while it was captured: {path}")
    return FormattedFileState(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
        mode=stat.S_IMODE(after.st_mode),
        content=raw,
    )


__all__ = [
    "FormattedFileState",
    "FormatterResult",
    "FormatterRunResult",
    "FormatterStatus",
    "PreparedFormatter",
    "capture_formatted_files",
    "prepare_formatters",
    "run_formatters",
    "verify_formatted_files",
]

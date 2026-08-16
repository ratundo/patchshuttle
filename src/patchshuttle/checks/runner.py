"""Controlled subprocess execution for immutable planned checks."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from patchshuttle._process import (
    ProcessCommand,
    _signal_process,
    _terminate_process,
    run_process,
)
from patchshuttle.models import CheckName
from patchshuttle.planner import Plan, PlannedCheck

_IMPORT_CHECK_CODE = (
    "import importlib, sys\n"
    "for module_name in sys.argv[1:]:\n"
    "    importlib.import_module(module_name)\n"
)


class CheckStatus(str, Enum):
    """Observable outcome of one controlled check process."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class PreparedCheck:
    """One fixed command prepared from a validated job and local policy."""

    id: str
    name: CheckName
    argv: tuple[str, ...]
    working_directory: Path
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Bounded captured outcome of one launched check."""

    id: str
    name: CheckName
    status: CheckStatus
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
        return self.status is CheckStatus.PASSED


@dataclass(frozen=True, slots=True)
class CheckRunResult:
    """Ordered results through the first failure, if any."""

    results: tuple[CheckResult, ...]

    @property
    def success(self) -> bool:
        return all(result.success for result in self.results)

    @property
    def failed(self) -> CheckResult | None:
        return next((result for result in self.results if not result.success), None)


def prepare_checks(plan: Plan) -> tuple[PreparedCheck, ...]:
    """Build fixed argument arrays from validated models and normalized paths."""

    if len(plan.job.checks) != len(plan.checks):
        raise ValueError("plan check records do not match job checks")

    prepared: list[PreparedCheck] = []
    for index, (check, planned) in enumerate(
        zip(plan.job.checks, plan.checks),
        start=1,
    ):
        expected_id = f"check_{index:03d}"
        if planned.id != expected_id or planned.name != check.name:
            raise ValueError("plan check records do not match job checks")
        prepared.append(_prepare_check(plan, planned, check.parameters))
    return tuple(prepared)


def run_checks(plan: Plan) -> CheckRunResult:
    """Run checks sequentially and stop immediately after the first failure."""

    maximum = plan.workspace.config.execution.max_command_output_bytes
    results: list[CheckResult] = []
    for check in prepare_checks(plan):
        result = _run_check(check, maximum_output_bytes=maximum)
        results.append(result)
        if not result.success:
            break
    return CheckRunResult(results=tuple(results))


def _prepare_check(
    plan: Plan,
    planned: PlannedCheck,
    parameters,
) -> PreparedCheck:
    paths = tuple(path.as_posix() for path in planned.paths)
    timeout = plan.workspace.config.execution.default_timeout_seconds

    if planned.name == "compileall":
        quiet = (f"-{'q' * parameters.quiet}",) if parameters.quiet else ()
        argv = (sys.executable, "-m", "compileall", *quiet, "--", *paths)
    elif planned.name == "pytest":
        path_arguments = ("--", *paths) if paths else ()
        argv = (
            sys.executable,
            "-m",
            "pytest",
            *parameters.args,
            *path_arguments,
        )
        timeout = parameters.timeout_seconds or timeout
    elif planned.name == "unittest":
        argv = (
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            _only_path(planned),
            "-p",
            parameters.pattern,
        )
    elif planned.name == "django_check":
        argv = (sys.executable, _only_path(planned), "check")
    elif planned.name == "django_migrations_check":
        argv = (
            sys.executable,
            _only_path(planned),
            "makemigrations",
            "--check",
            "--dry-run",
        )
    elif planned.name == "django_test":
        argv = (
            sys.executable,
            _only_path(planned),
            "test",
            *parameters.labels,
        )
    elif planned.name == "import_check":
        argv = (
            sys.executable,
            "-c",
            _IMPORT_CHECK_CODE,
            *parameters.modules,
        )
    elif planned.name == "profile":
        profile = plan.workspace.config.checks.profiles[parameters.name]
        argv = tuple(
            sys.executable if argument == "{python}" else argument
            for argument in profile.argv
        )
        timeout = profile.timeout_seconds
    else:  # pragma: no cover - closed CheckName and planner contract
        raise ValueError(f"unsupported planned check: {planned.name}")

    return PreparedCheck(
        id=planned.id,
        name=planned.name,
        argv=argv,
        working_directory=plan.workspace.root,
        timeout_seconds=timeout,
    )


def _only_path(planned: PlannedCheck) -> str:
    if len(planned.paths) != 1:
        raise ValueError("planned check requires exactly one normalized path")
    return planned.paths[0].as_posix()


def _run_check(
    check: PreparedCheck,
    *,
    maximum_output_bytes: int,
) -> CheckResult:
    process = run_process(
        ProcessCommand(
            argv=check.argv,
            working_directory=check.working_directory,
            timeout_seconds=check.timeout_seconds,
        ),
        maximum_output_bytes=maximum_output_bytes,
    )
    return CheckResult(
        id=check.id,
        name=check.name,
        status=CheckStatus(process.status.value),
        argv=check.argv,
        working_directory=check.working_directory,
        timeout_seconds=check.timeout_seconds,
        return_code=process.return_code,
        duration_ms=process.duration_ms,
        stdout=process.stdout,
        stderr=process.stderr,
        stdout_truncated=process.stdout_truncated,
        stderr_truncated=process.stderr_truncated,
    )


__all__ = [
    "CheckResult",
    "CheckRunResult",
    "CheckStatus",
    "PreparedCheck",
    "prepare_checks",
    "run_checks",
]

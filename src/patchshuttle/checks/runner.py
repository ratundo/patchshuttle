"""Controlled subprocess execution for immutable planned checks."""

from __future__ import annotations

import os as os
import subprocess as subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from patchshuttle._process import (
    ProcessCommand,
)
from patchshuttle._process import _signal_process as _signal_process
from patchshuttle._process import _terminate_process as _terminate_process
from patchshuttle._process import (
    run_process,
)
from patchshuttle.models import CheckName
from patchshuttle.planner import Plan, PlannedCheck

_IMPORT_CHECK_CODE = (
    "import importlib, sys\n"
    "for module_name in sys.argv[1:]:\n"
    "    importlib.import_module(module_name)\n"
)


def _django_import_code(modules: tuple[str, ...]) -> str:
    """Render bounded code from already validated dotted module names."""

    return (
        "import importlib\n"
        f"for module_name in {modules!r}:\n"
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
    warning_analysis: str = "NOT_APPLICABLE"
    known_warnings: int | None = None
    new_warnings: int | None = None
    new_warning_details: tuple[str, ...] = ()

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

    prepared = prepare_checks(plan)
    known_warning_ids: frozenset[str] = frozenset()
    if any(check.name == "django_check" for check in prepared):
        from patchshuttle.warning_baseline import load_warning_baseline

        baseline = load_warning_baseline(plan.workspace)
        known_warning_ids = frozenset(baseline.django_check_ids)
    maximum = plan.workspace.config.execution.max_command_output_bytes
    results: list[CheckResult] = []
    for check in prepared:
        result = _run_check(
            check,
            maximum_output_bytes=maximum,
            known_warning_ids=known_warning_ids,
        )
        results.append(result)
        if not result.success:
            break
    return CheckRunResult(results=tuple(results))


def _prepare_check(
    plan: Plan,
    planned: PlannedCheck,
    parameters,
) -> PreparedCheck:
    from patchshuttle.project_python import resolve_project_python

    paths = tuple(path.as_posix() for path in planned.paths)
    timeout = plan.workspace.config.execution.default_timeout_seconds
    profile = (
        plan.workspace.config.checks.profiles[parameters.name]
        if planned.name == "profile"
        else None
    )
    uses_project_python = planned.name in {
        "compileall",
        "pytest",
        "unittest",
        "django_check",
        "django_migrations_check",
        "django_test",
        "django_import_check",
        "import_check",
    } or (profile is not None and "{python}" in profile.argv)
    project_python = (
        str(resolve_project_python(plan.workspace))
        if uses_project_python
        else sys.executable
    )

    if planned.name == "compileall":
        quiet = (f"-{'q' * parameters.quiet}",) if parameters.quiet else ()
        argv = (project_python, "-m", "compileall", *quiet, "--", *paths)
    elif planned.name == "ruff":
        argv = (
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "F",
            "--no-fix",
            "--",
            *paths,
        )
    elif planned.name == "pytest":
        path_arguments = ("--", *paths) if paths else ()
        argv = (
            project_python,
            "-m",
            "pytest",
            *parameters.args,
            *path_arguments,
        )
        timeout = parameters.timeout_seconds or timeout
    elif planned.name == "unittest":
        argv = (
            project_python,
            "-m",
            "unittest",
            "discover",
            "-s",
            _only_path(planned),
            "-p",
            parameters.pattern,
        )
    elif planned.name == "django_check":
        argv = (project_python, _only_path(planned), "check")
    elif planned.name == "django_migrations_check":
        argv = (
            project_python,
            _only_path(planned),
            "makemigrations",
            "--check",
            "--dry-run",
        )
    elif planned.name == "django_test":
        argv = (
            project_python,
            _only_path(planned),
            "test",
            *parameters.labels,
        )
    elif planned.name == "django_import_check":
        argv = (
            project_python,
            _only_path(planned),
            "shell",
            "-c",
            _django_import_code(parameters.modules),
        )
    elif planned.name == "import_check":
        argv = (
            project_python,
            "-c",
            _IMPORT_CHECK_CODE,
            *parameters.modules,
        )
    elif planned.name == "profile":
        if profile is None:  # pragma: no cover - planner contract
            raise ValueError("planned profile is not configured")
        argv = tuple(
            project_python if argument == "{python}" else argument
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
    known_warning_ids: frozenset[str] = frozenset(),
) -> CheckResult:
    with tempfile.TemporaryDirectory(prefix="patchshuttle-pycache-") as cache:
        process = run_process(
            ProcessCommand(
                argv=check.argv,
                working_directory=check.working_directory,
                timeout_seconds=check.timeout_seconds,
                environment_overrides=(("PYTHONPYCACHEPREFIX", cache),),
            ),
            maximum_output_bytes=maximum_output_bytes,
        )
    warning_analysis = "NOT_APPLICABLE"
    known_warnings: int | None = None
    new_warnings: int | None = None
    new_warning_details: tuple[str, ...] = ()
    if check.name == "django_check":
        from patchshuttle.warning_baseline import analyze_django_warning_output

        analysis = analyze_django_warning_output(
            process.stdout,
            process.stderr,
            known_ids=known_warning_ids,
            output_truncated=(process.stdout_truncated or process.stderr_truncated),
        )
        warning_analysis = analysis.status
        known_warnings = analysis.known_warnings
        new_warnings = analysis.new_warnings
        new_warning_details = analysis.new_warning_details
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
        warning_analysis=warning_analysis,
        known_warnings=known_warnings,
        new_warnings=new_warnings,
        new_warning_details=new_warning_details,
    )


__all__ = [
    "CheckResult",
    "CheckRunResult",
    "CheckStatus",
    "PreparedCheck",
    "prepare_checks",
    "run_checks",
]

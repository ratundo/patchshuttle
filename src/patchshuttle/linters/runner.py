"""Controlled lint-only execution for changed HTML template files."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal, TypeAlias

from patchshuttle._html_lint import djlint_argv, isolated_djlint_directory
from patchshuttle._process import ProcessCommand, run_process
from patchshuttle.planner import Plan

HtmlLintName: TypeAlias = Literal["djlint"]


class HtmlLintStatus(str, Enum):
    """Observable outcome of one changed-template lint process."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class PreparedHtmlLint:
    """One fixed lint-only command for one approved HTML target."""

    id: str
    name: HtmlLintName
    path: PurePosixPath
    argv: tuple[str, ...]
    timeout_seconds: int
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class HtmlLintResult:
    """Bounded captured outcome of one HTML lint process."""

    id: str
    name: HtmlLintName
    path: PurePosixPath
    status: HtmlLintStatus
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
        return self.status is HtmlLintStatus.PASSED


@dataclass(frozen=True, slots=True)
class HtmlLintRunResult:
    """Ordered HTML lint results through the first failure."""

    results: tuple[HtmlLintResult, ...]

    @property
    def success(self) -> bool:
        return all(result.success for result in self.results)

    @property
    def failed(self) -> HtmlLintResult | None:
        return next((result for result in self.results if not result.success), None)


def prepare_html_linter(plan: Plan) -> tuple[PreparedHtmlLint, ...]:
    """Build one fixed djLint command per approved changed HTML file."""

    settings = plan.workspace.config.linting.html
    expected = (
        tuple(
            change.path
            for change in plan.file_changes
            if change.path.suffix.casefold() == ".html"
        )
        if settings.enabled
        else ()
    )
    if plan.html_lint_targets != expected:
        raise ValueError("plan HTML lint targets do not match changed HTML files")
    return tuple(
        PreparedHtmlLint(
            id=f"html_lint_{index:03d}",
            name="djlint",
            path=path,
            argv=djlint_argv(settings, "-", stdin_filename=path.as_posix()),
            timeout_seconds=plan.workspace.config.execution.default_timeout_seconds,
            content=next(
                change.content for change in plan.file_changes if change.path == path
            ),
        )
        for index, path in enumerate(expected, start=1)
    )


def run_html_linter(plan: Plan) -> HtmlLintRunResult:
    """Run lint-only checks sequentially and stop after the first failure."""

    maximum = plan.workspace.config.execution.max_command_output_bytes
    results: list[HtmlLintResult] = []
    prepared_commands = prepare_html_linter(plan)
    if not prepared_commands:
        return HtmlLintRunResult(results=())
    with isolated_djlint_directory() as working_directory:
        for prepared in prepared_commands:
            process = run_process(
                ProcessCommand(
                    argv=prepared.argv,
                    working_directory=working_directory,
                    timeout_seconds=prepared.timeout_seconds,
                    stdin=prepared.content,
                ),
                maximum_output_bytes=maximum,
            )
            result = HtmlLintResult(
                id=prepared.id,
                name=prepared.name,
                path=prepared.path,
                status=HtmlLintStatus(process.status.value),
                argv=prepared.argv,
                working_directory=working_directory,
                timeout_seconds=prepared.timeout_seconds,
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
    return HtmlLintRunResult(results=tuple(results))


__all__ = [
    "HtmlLintResult",
    "HtmlLintRunResult",
    "HtmlLintStatus",
    "PreparedHtmlLint",
    "prepare_html_linter",
    "run_html_linter",
]

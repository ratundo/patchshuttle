"""Read-only compatibility checks over final planned file content."""

from __future__ import annotations

import sys
import tokenize
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from patchshuttle._html_lint import djlint_argv, isolated_djlint_directory
from patchshuttle._process import ProcessCommand, ProcessStatus, run_process
from patchshuttle.errors import PlanningError, PlanningErrorCode
from patchshuttle.formatter_policy import (
    BLACK_POLICY_OPTIONS,
    FORMATTER_ORDER,
    FormatterCompatibility,
    FormatterDecision,
    FormatterName,
    PlannedFormatterTarget,
)

if TYPE_CHECKING:
    from patchshuttle.planner import PlannedFileChange
    from patchshuttle.workspace import Workspace


@dataclass(frozen=True, slots=True)
class PlannedPreflightCheck:
    """One deterministic successful preflight observation."""

    id: str
    tool: str
    path: PurePosixPath
    detail: str


@dataclass(frozen=True, slots=True)
class QualityPreflightResult:
    """Complete formatter decisions and successful compatibility observations."""

    checks: tuple[PlannedPreflightCheck, ...]
    formatter_plan: tuple[PlannedFormatterTarget, ...]


def run_quality_preflight(
    workspace: Workspace,
    changes: tuple[PlannedFileChange, ...],
    *,
    formatting_targets: tuple[PurePosixPath, ...],
    html_lint_targets: tuple[PurePosixPath, ...],
) -> QualityPreflightResult:
    """Require every automatic quality tool to accept final planned input."""

    by_path = {change.path: change for change in changes}
    records: list[tuple[str, PurePosixPath, str]] = []
    formatter_plan: list[PlannedFormatterTarget] = []
    exclusions = {
        "isort": frozenset(workspace.config.formatting.isort_exclude),
        "black": frozenset(workspace.config.formatting.black_exclude),
    }
    for path in formatting_targets:
        change = by_path[path]
        active = tuple(
            name for name in FORMATTER_ORDER if path.as_posix() not in exclusions[name]
        )
        baseline_source = None
        planned_source = None
        if active:
            baseline_source = _decode_attempt(change.before_content, path=path)
            planned_source = _decode_attempt(change.content, path=path)
            if baseline_source[0] is not None:
                records.append(
                    (
                        "python_encoding",
                        path,
                        f"baseline {baseline_source[0]}",
                    )
                )
            if planned_source[0] is not None:
                records.append(
                    (
                        "python_encoding",
                        path,
                        f"planned {planned_source[0]}",
                    )
                )

        for name in FORMATTER_ORDER:
            if path.as_posix() in exclusions[name]:
                formatter_plan.append(
                    PlannedFormatterTarget(
                        formatter=name,
                        path=path,
                        decision=FormatterDecision.SKIP_LOCAL_POLICY,
                        baseline=FormatterCompatibility.NOT_CHECKED,
                        planned=FormatterCompatibility.NOT_CHECKED,
                        baseline_detail="skipped by local formatter policy",
                        planned_detail="skipped by local formatter policy",
                    )
                )
                continue

            assert baseline_source is not None and planned_source is not None
            baseline_status, baseline_detail = _formatter_attempt(
                workspace,
                name,
                path,
                baseline_source,
                baseline_missing=change.before_content is None,
            )
            planned_status, planned_detail = _formatter_attempt(
                workspace,
                name,
                path,
                planned_source,
                baseline_missing=False,
            )
            if baseline_status is FormatterCompatibility.PASS:
                records.append((name, path, "baseline input accepted"))
            if planned_status is FormatterCompatibility.PASS:
                records.append((name, path, "planned input accepted"))
            if planned_status is FormatterCompatibility.INCOMPATIBLE:
                _raise_formatter_incompatibility(
                    name,
                    path,
                    baseline_status=baseline_status,
                    baseline_detail=baseline_detail,
                    planned_detail=planned_detail,
                )
            formatter_plan.append(
                PlannedFormatterTarget(
                    formatter=name,
                    path=path,
                    decision=FormatterDecision.RUN,
                    baseline=baseline_status,
                    planned=planned_status,
                    baseline_detail=baseline_detail,
                    planned_detail=planned_detail,
                )
            )

    for path in html_lint_targets:
        change = by_path[path]
        _preflight_djlint(workspace, path, change.content)
        records.append(
            (
                "djlint",
                path,
                f"profile={workspace.config.linting.html.profile}",
            )
        )

    return QualityPreflightResult(
        checks=tuple(
            PlannedPreflightCheck(
                id=f"preflight_{index:03d}",
                tool=tool,
                path=path,
                detail=detail,
            )
            for index, (tool, path, detail) in enumerate(records, start=1)
        ),
        formatter_plan=tuple(formatter_plan),
    )


def detect_python_encoding(raw: bytes, *, path: PurePosixPath) -> str:
    """Return the PEP 263 encoding detected from one Python source payload."""

    try:
        encoding, _ = tokenize.detect_encoding(BytesIO(raw).readline)
        raw.decode(encoding)
    except (LookupError, SyntaxError, UnicodeError) as exc:
        raise PlanningError(
            PlanningErrorCode.FILE_ENCODING_UNSUPPORTED,
            "Python source encoding is invalid or unsupported by PEP 263",
            item_id="formatting",
            path=path.as_posix(),
            details=(f"  encoding_error: {_bounded_exception(exc)}",),
        ) from exc
    return encoding


def _decode_python(raw: bytes, *, path: PurePosixPath) -> tuple[str, str]:
    encoding = detect_python_encoding(raw, path=path)
    return encoding, raw.decode(encoding)


def _decode_attempt(
    raw: bytes | None,
    *,
    path: PurePosixPath,
) -> tuple[str | None, str | None, str]:
    if raw is None:
        return None, None, "file did not exist before the planned change"
    try:
        encoding, text = _decode_python(raw, path=path)
    except PlanningError as exc:
        detail = exc.details[0].strip() if exc.details else str(exc)
        return None, None, detail
    return encoding, text, f"decoded as {encoding}"


def _formatter_attempt(
    workspace: Workspace,
    name: FormatterName,
    path: PurePosixPath,
    source: tuple[str | None, str | None, str],
    *,
    baseline_missing: bool,
) -> tuple[FormatterCompatibility, str]:
    if baseline_missing:
        return (
            FormatterCompatibility.NOT_APPLICABLE,
            "file did not exist before the planned change",
        )
    _encoding, text, decode_detail = source
    if text is None:
        return FormatterCompatibility.INCOMPATIBLE, decode_detail
    try:
        if name == "isort":
            _preflight_isort(workspace, path, text)
        else:
            _preflight_black(workspace, path, text)
    except Exception as exc:
        return FormatterCompatibility.INCOMPATIBLE, _bounded_exception(exc)
    return FormatterCompatibility.PASS, "input accepted"


def _raise_formatter_incompatibility(
    name: FormatterName,
    path: PurePosixPath,
    *,
    baseline_status: FormatterCompatibility,
    baseline_detail: str,
    planned_detail: str,
) -> None:
    baseline_incompatible = baseline_status is FormatterCompatibility.INCOMPATIBLE
    code = (
        PlanningErrorCode.FORMATTER_BASELINE_INCOMPATIBLE
        if baseline_incompatible
        else PlanningErrorCode.FORMATTER_PATCH_INCOMPATIBLE
    )
    message = (
        f"{name} could not process the file before or after the planned change"
        if baseline_incompatible
        else f"{name} could not process final planned Python content"
    )
    raise PlanningError(
        code,
        message,
        item_id="formatting",
        path=path.as_posix(),
        details=(
            f"  formatter: {name}",
            f"  baseline: {baseline_status.value}",
            f"  baseline_detail: {_bounded_text(baseline_detail, 500)}",
            "  planned: INCOMPATIBLE",
            f"  planned_detail: {_bounded_text(planned_detail, 500)}",
        ),
    )


def _preflight_isort(workspace: Workspace, path: PurePosixPath, text: str) -> None:
    import isort

    config = isort.Config(settings_path=str(workspace.root), atomic=True)
    isort.code(
        text,
        config=config,
        file_path=workspace.root.joinpath(*path.parts),
        disregard_skip=True,
    )


def _preflight_black(workspace: Workspace, path: PurePosixPath, text: str) -> None:
    process = run_process(
        ProcessCommand(
            argv=(
                sys.executable,
                "-I",
                "-m",
                "black",
                *BLACK_POLICY_OPTIONS,
                "--quiet",
                "--check",
                "--stdin-filename",
                path.as_posix(),
                "-",
            ),
            working_directory=workspace.root,
            timeout_seconds=workspace.config.execution.default_timeout_seconds,
            stdin=text.encode("utf-8"),
        ),
        maximum_output_bytes=workspace.config.execution.max_command_output_bytes,
    )
    if process.status is ProcessStatus.PASSED or (
        process.status is ProcessStatus.FAILED and process.return_code == 1
    ):
        return
    detail = _process_details(process.stdout, process.stderr)
    raise ValueError("; ".join(line.strip() for line in detail))


def _preflight_djlint(
    workspace: Workspace,
    path: PurePosixPath,
    content: bytes,
) -> None:
    settings = workspace.config.linting.html
    try:
        with isolated_djlint_directory() as working_directory:
            process = run_process(
                ProcessCommand(
                    argv=djlint_argv(
                        settings,
                        "-",
                        stdin_filename=path.as_posix(),
                    ),
                    working_directory=working_directory,
                    timeout_seconds=(
                        workspace.config.execution.default_timeout_seconds
                    ),
                    stdin=content,
                ),
                maximum_output_bytes=(
                    workspace.config.execution.max_command_output_bytes
                ),
            )
    except OSError as exc:
        raise PlanningError(
            PlanningErrorCode.HTML_LINT_FAILED,
            "isolated djLint preflight could not be prepared",
            item_id="html_lint",
            path=path.as_posix(),
            details=(f"  isolation_error: {_bounded_exception(exc)}",),
        ) from exc
    if process.status is ProcessStatus.PASSED:
        return
    state = {
        ProcessStatus.FAILED: "reported template lint errors",
        ProcessStatus.TIMED_OUT: "timed out",
        ProcessStatus.ERROR: "could not be started",
    }[process.status]
    raise PlanningError(
        PlanningErrorCode.HTML_LINT_FAILED,
        f"djLint {state} during read-only preflight",
        item_id="html_lint",
        path=path.as_posix(),
        details=_process_details(process.stdout, process.stderr),
    )


def _process_details(stdout: str, stderr: str) -> tuple[str, ...]:
    lines = [line for line in (*stdout.splitlines(), *stderr.splitlines()) if line]
    if not lines:
        return ("  output: none",)
    return tuple(f"  output: {_bounded_text(line, 240)}" for line in lines[:12])


def _bounded_exception(error: BaseException) -> str:
    message = str(error)
    rendered = f"{type(error).__name__}: {message}" if message else type(error).__name__
    return _bounded_text(rendered, 500)


def _bounded_text(value: str, maximum: int) -> str:
    normalized = value.replace("\r", "\\r").replace("\n", "\\n")
    return (
        normalized if len(normalized) <= maximum else normalized[: maximum - 3] + "..."
    )


__all__ = [
    "PlannedPreflightCheck",
    "QualityPreflightResult",
    "detect_python_encoding",
    "run_quality_preflight",
]

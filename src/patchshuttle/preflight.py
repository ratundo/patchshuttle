"""Read-only compatibility checks over final planned file content."""

from __future__ import annotations

import tokenize
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from patchshuttle._html_lint import djlint_argv, isolated_djlint_directory
from patchshuttle._process import ProcessCommand, ProcessStatus, run_process
from patchshuttle.errors import PlanningError, PlanningErrorCode

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


def run_quality_preflight(
    workspace: Workspace,
    changes: tuple[PlannedFileChange, ...],
    *,
    formatting_targets: tuple[PurePosixPath, ...],
    html_lint_targets: tuple[PurePosixPath, ...],
) -> tuple[PlannedPreflightCheck, ...]:
    """Require every automatic quality tool to accept final planned input."""

    by_path = {change.path: change for change in changes}
    records: list[tuple[str, PurePosixPath, str]] = []
    for path in formatting_targets:
        change = by_path[path]
        encoding, text = _decode_python(change.content, path=path)
        records.append(("python_encoding", path, encoding))
        _preflight_isort(workspace, path, text)
        records.append(("isort", path, "input accepted"))
        _preflight_black(path, text)
        records.append(("black", path, "input accepted"))

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

    return tuple(
        PlannedPreflightCheck(
            id=f"preflight_{index:03d}",
            tool=tool,
            path=path,
            detail=detail,
        )
        for index, (tool, path, detail) in enumerate(records, start=1)
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


def _preflight_isort(workspace: Workspace, path: PurePosixPath, text: str) -> None:
    try:
        import isort

        config = isort.Config(settings_path=str(workspace.root), atomic=True)
        isort.code(
            text,
            config=config,
            file_path=workspace.root.joinpath(*path.parts),
            disregard_skip=True,
        )
    except Exception as exc:
        raise _formatter_error("isort", path, exc) from exc


def _preflight_black(path: PurePosixPath, text: str) -> None:
    try:
        import black

        try:
            black.format_file_contents(text, fast=False, mode=black.Mode())
        except black.NothingChanged:
            pass
    except Exception as exc:
        raise _formatter_error("black", path, exc) from exc


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


def _formatter_error(
    name: str,
    path: PurePosixPath,
    error: Exception,
) -> PlanningError:
    return PlanningError(
        PlanningErrorCode.FORMATTER_PREFLIGHT_FAILED,
        f"{name} could not process final planned Python content",
        item_id="formatting",
        path=path.as_posix(),
        details=(f"  {name}_error: {_bounded_exception(error)}",),
    )


def _process_details(stdout: str, stderr: str) -> tuple[str, ...]:
    lines = [line for line in (*stdout.splitlines(), *stderr.splitlines()) if line]
    if not lines:
        return ("  output: none",)
    return tuple(f"  output: {_bounded_text(line, 240)}" for line in lines[:12])


def _bounded_exception(error: BaseException) -> str:
    return _bounded_text(str(error) or type(error).__name__, 500)


def _bounded_text(value: str, maximum: int) -> str:
    normalized = value.replace("\r", "\\r").replace("\n", "\\n")
    return (
        normalized if len(normalized) <= maximum else normalized[: maximum - 3] + "..."
    )


__all__ = [
    "PlannedPreflightCheck",
    "detect_python_encoding",
    "run_quality_preflight",
]

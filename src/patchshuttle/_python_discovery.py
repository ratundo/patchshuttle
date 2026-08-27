"""Evidence-only telemetry for evaluating Python symbol-index demand."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from patchshuttle.audit import AuditActionResult
from patchshuttle.models import Job

_TEXT_ACTIONS = frozenset(
    {"delete_exact", "insert_after", "insert_before", "replace_exact"}
)
_SYMBOL_ACTIONS = frozenset({"replace_symbol"})
_LINE_ACTIONS = frozenset({"delete_range", "insert_at_line", "replace_range"})
_FAILURE_SIGNALS = {
    "LINE_RANGE_GUARD_MISMATCH": "LINE_TARGETING_FAILURE",
    "LINE_RANGE_OUT_OF_BOUNDS": "LINE_TARGETING_FAILURE",
    "OCCURRENCE_COUNT_MISMATCH": "TEXT_TARGETING_FAILURE",
    "PYTHON_SOURCE_INVALID": "PYTHON_PARSE_FAILURE",
    "PYTHON_SYMBOL_GUARD_MISMATCH": "SYMBOL_GUARD_FAILURE",
    "PYTHON_SYMBOL_RESOLUTION_FAILED": "SYMBOL_RESOLUTION_FAILURE",
}


@dataclass(frozen=True, slots=True)
class PythonDiscoveryEvaluation:
    """Current-job facts relevant to a future Python symbol index decision."""

    applicable: bool
    explicit_python_paths: tuple[PurePosixPath, ...]
    python_read_actions: int
    python_read_symbol_actions: int
    python_structure_actions: int
    python_search_actions: int
    unclassified_search_actions: int
    python_find_files_actions: int
    python_files_reported: int
    python_file_limit_reached: bool
    python_search_matches: int
    python_searches_limited: int
    python_audit_duration_ms: int
    python_audit_output_bytes: int
    python_audit_output_lines: int
    declared_python_text_actions: int
    declared_python_symbol_actions: int
    declared_python_line_actions: int
    failure_signal: str | None
    reason_codes: tuple[str, ...]


def evaluate_python_discovery(
    job: Job,
    *,
    audit_results: tuple[AuditActionResult, ...] = (),
    failure_code: str | None = None,
    failed_path: str | None = None,
) -> PythonDiscoveryEvaluation:
    """Collect bounded facts without estimating tokens or index usefulness."""

    python_paths: set[PurePosixPath] = set()
    result_by_id = {item.id: item for item in audit_results}
    python_read_actions = 0
    python_read_symbol_actions = 0
    python_structure_actions = 0
    python_search_actions = 0
    unclassified_search_actions = 0
    python_find_files_actions = 0
    python_files_reported = 0
    python_file_limit_reached = False
    python_search_matches = 0
    python_searches_limited = 0
    python_audit_duration_ms = 0
    python_audit_output_bytes = 0
    python_audit_output_lines = 0
    declared_python_text_actions = 0
    declared_python_symbol_actions = 0
    declared_python_line_actions = 0

    for index, action in enumerate(job.actions, start=1):
        name = action.name
        parameters = action.parameters
        raw_path = getattr(parameters, "path", None)
        path = PurePosixPath(raw_path) if isinstance(raw_path, str) else None
        path_is_python = path is not None and _is_python_path(path)
        if path_is_python:
            python_paths.add(path)
        python_targeted = (
            name in {"read_symbol", "python_structure"}
            or path_is_python
            or _is_python_glob(getattr(parameters, "glob", None))
        )
        result = result_by_id.get(f"action_{index:03d}")

        if name in {"search", "search_context"} and not python_targeted:
            if result is not None:
                unclassified_search_actions += 1
            continue
        if python_targeted and result is not None:
            python_audit_duration_ms += result.duration_ms
            python_audit_output_bytes += len(result.output.encode("utf-8"))
            python_audit_output_lines += len(result.output.splitlines())
            if name == "read":
                python_read_actions += 1
            elif name == "read_symbol":
                python_read_symbol_actions += 1
            elif name == "python_structure":
                python_structure_actions += 1
            elif name in {"search", "search_context"}:
                python_search_actions += 1
                python_search_matches += _output_integer(result.output, "matches")
                if _result_limit_reached(result.output):
                    python_searches_limited += 1
            elif name == "find_files":
                python_find_files_actions += 1
                python_files_reported += _output_integer(result.output, "matches")
                python_file_limit_reached |= _result_limit_reached(result.output)

        if not python_targeted:
            continue
        if name in _TEXT_ACTIONS:
            declared_python_text_actions += 1
        elif name in _SYMBOL_ACTIONS:
            declared_python_symbol_actions += 1
        elif name in _LINE_ACTIONS:
            declared_python_line_actions += 1

    if failed_path is not None:
        candidate = PurePosixPath(failed_path)
        if _is_python_path(candidate):
            python_paths.add(candidate)
    failure_signal = _failure_signal(failure_code, failed_path)
    reason_codes = _reason_codes(
        python_read_actions=python_read_actions,
        python_read_symbol_actions=python_read_symbol_actions,
        python_structure_actions=python_structure_actions,
        python_search_actions=python_search_actions,
        unclassified_search_actions=unclassified_search_actions,
        python_find_files_actions=python_find_files_actions,
        declared_python_text_actions=declared_python_text_actions,
        declared_python_symbol_actions=declared_python_symbol_actions,
        declared_python_line_actions=declared_python_line_actions,
        failure_signal=failure_signal,
    )
    applicable = bool(python_paths or reason_codes)
    return PythonDiscoveryEvaluation(
        applicable=applicable,
        explicit_python_paths=tuple(sorted(python_paths)),
        python_read_actions=python_read_actions,
        python_read_symbol_actions=python_read_symbol_actions,
        python_structure_actions=python_structure_actions,
        python_search_actions=python_search_actions,
        unclassified_search_actions=unclassified_search_actions,
        python_find_files_actions=python_find_files_actions,
        python_files_reported=python_files_reported,
        python_file_limit_reached=python_file_limit_reached,
        python_search_matches=python_search_matches,
        python_searches_limited=python_searches_limited,
        python_audit_duration_ms=python_audit_duration_ms,
        python_audit_output_bytes=python_audit_output_bytes,
        python_audit_output_lines=python_audit_output_lines,
        declared_python_text_actions=declared_python_text_actions,
        declared_python_symbol_actions=declared_python_symbol_actions,
        declared_python_line_actions=declared_python_line_actions,
        failure_signal=failure_signal,
        reason_codes=reason_codes,
    )


def _output_integer(output: str, name: str) -> int:
    prefix = f"{name}: "
    value = next(
        (
            line[len(prefix) :]
            for line in output.splitlines()
            if line.startswith(prefix)
        ),
        "",
    )
    return int(value) if value.isdecimal() else 0


def _result_limit_reached(output: str) -> bool:
    return "result_limit_reached: true" in output.splitlines()


def _is_python_path(path: PurePosixPath) -> bool:
    return path.suffix.casefold() == ".py"


def _is_python_glob(value: object) -> bool:
    return isinstance(value, str) and value.casefold().endswith(".py")


def _failure_signal(failure_code: str | None, failed_path: str | None) -> str | None:
    if failure_code not in _FAILURE_SIGNALS:
        return None
    if failure_code.startswith("PYTHON_"):
        return _FAILURE_SIGNALS[failure_code]
    if failed_path is None or not _is_python_path(PurePosixPath(failed_path)):
        return None
    return _FAILURE_SIGNALS[failure_code]


def _reason_codes(
    *,
    python_read_actions: int,
    python_read_symbol_actions: int,
    python_structure_actions: int,
    python_search_actions: int,
    unclassified_search_actions: int,
    python_find_files_actions: int,
    declared_python_text_actions: int,
    declared_python_symbol_actions: int,
    declared_python_line_actions: int,
    failure_signal: str | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    observations = (
        (python_read_actions, "PYTHON_FILE_READ_USED"),
        (python_read_symbol_actions, "PYTHON_SYMBOL_READ_USED"),
        (python_structure_actions, "PYTHON_STRUCTURE_DISCOVERY_USED"),
        (python_search_actions, "PYTHON_TEXT_SEARCH_USED"),
        (unclassified_search_actions, "UNCLASSIFIED_TEXT_SEARCH_USED"),
        (python_find_files_actions, "PYTHON_FILE_DISCOVERY_USED"),
        (declared_python_text_actions, "PYTHON_TEXT_TARGETING_USED"),
        (declared_python_symbol_actions, "PYTHON_SYMBOL_TARGETING_USED"),
        (declared_python_line_actions, "PYTHON_LINE_TARGETING_USED"),
    )
    reasons.extend(reason for count, reason in observations if count)
    if failure_signal is not None:
        reasons.append(failure_signal)
    return tuple(reasons)


__all__ = ["PythonDiscoveryEvaluation", "evaluate_python_discovery"]

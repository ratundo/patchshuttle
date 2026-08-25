"""Deterministic compact views of existing fixed-section PatchShuttle logs."""

from __future__ import annotations

import json
import re

_SECTION_PATTERN = re.compile(r"^=== ([A-Z0-9_]+) ===$")
_FIELD_PATTERN = re.compile(r"^([a-z][a-z0-9_]*):(.*)$")
_RECORD_SEPARATOR = re.compile(r"\n---\n(?=(?:action_id|check_id): )")
_OUTPUT_LIMIT = 40000

_HEADER_FIELDS = (
    "patchshuttle_version",
    "protocol",
    "timestamp",
    "redaction",
    "redaction_guarantee",
)
_ATTEMPT_FIELDS = (
    "patchshuttle_version",
    "protocol",
    "timestamp",
    "redaction",
    "redaction_guarantee",
    "project_id",
    "command",
    "job_file",
    "job_id",
    "job_hash",
    "kind",
    "error",
)
_JOB_FIELDS = ("job_id", "job_hash", "kind", "title")
_PLAN_FIELDS = (
    "planned_actions",
    "planned_checks",
    "project_python",
    "files_to_create",
    "files_to_modify",
    "directories_to_create",
    "formatter_plan",
    "preflight_checks",
    "protected_paths",
    "automatic_rollback",
)
_ACTION_FIELDS = (
    "action_id",
    "action_type",
    "path_or_scope",
    "status",
    "actual",
    "details",
)
_CHECK_FIELDS = (
    "check_id",
    "profile",
    "exit_code",
    "status",
    "warning_analysis",
    "known_warnings",
    "new_warnings",
    "new_warning_details",
    "stdout",
    "stderr",
    "stdout_truncated",
    "stderr_truncated",
)
_FORMATTER_FIELDS = (
    "formatter_id",
    "formatter",
    "exit_code",
    "status",
    "stdout",
    "stderr",
    "stdout_truncated",
    "stderr_truncated",
)


def render_ai_log(text: str, *, source: str, json_output: bool) -> str:
    """Render one existing log as deterministic compact text or JSON."""

    payload = summarize_ai_log(text, source=source)
    if json_output:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
    return _render_text(payload)


def summarize_ai_log(text: str, *, source: str) -> dict[str, object]:
    """Extract bounded AI-relevant data from a fixed-section log."""

    sections = _parse_sections(text)
    summary_name = "SUMMARY" if "SUMMARY" in sections else "LATEST_RUN_SUMMARY"
    handoff_name = (
        "PATCHSHUTTLE_AI_HANDOFF"
        if "PATCHSHUTTLE_AI_HANDOFF" in sections
        else "LATEST_AI_HANDOFF"
    )
    if summary_name not in sections or handoff_name not in sections:
        raise ValueError(
            "log does not contain compactable summary and handoff sections"
        )

    payload: dict[str, object] = {
        "schema": "patchshuttle.ai_log.v1",
        "source": source,
    }
    _include_selected(payload, "header", sections.get("HEADER"), _HEADER_FIELDS)
    _include_selected(
        payload,
        "attempt",
        sections.get("PATCHSHUTTLE_ATTEMPT"),
        _ATTEMPT_FIELDS,
    )
    _include_selected(payload, "job", sections.get("JOB"), _JOB_FIELDS)
    if "job" not in payload and "attempt" in payload:
        attempt = payload["attempt"]
        if isinstance(attempt, dict):
            job = {
                key: attempt[key]
                for key in ("job_id", "job_hash", "kind")
                if key in attempt
            }
            if job:
                payload["job"] = job
    _include_selected(payload, "plan", sections.get("PLAN"), _PLAN_FIELDS)

    audit = _parse_records(sections.get("AUDIT"), "action_id", _ACTION_FIELDS)
    if audit:
        payload["audit"] = audit
    actions = _parse_records(
        sections.get("ACTIONS"),
        "action_id",
        _ACTION_FIELDS,
    )
    if actions:
        payload["actions"] = _summarize_actions(actions)

    checks: dict[str, object] = {}
    initial = _compact_success_records(
        _parse_records(
            sections.get("INITIAL_CHECKS"),
            "check_id",
            _CHECK_FIELDS,
        )
    )
    final = _compact_success_records(
        _parse_records(
            sections.get("FINAL_CHECKS"),
            "check_id",
            _CHECK_FIELDS,
        )
    )
    if initial:
        checks["initial"] = initial
    if final:
        checks["final"] = final
    if checks:
        payload["checks"] = checks

    formatters: dict[str, object] = {}
    for name, section_name in (
        ("isort", "FORMAT_ISORT"),
        ("black", "FORMAT_BLACK"),
    ):
        values = _select_fields(
            _parse_fields(sections.get(section_name, "")),
            _FORMATTER_FIELDS,
        )
        if values:
            formatters[name] = _compact_success_record(values)
    if formatters:
        payload["formatters"] = formatters

    _include_fields(
        payload,
        "workspace_comparison",
        sections.get("WORKSPACE_COMPARISON"),
    )
    _include_fields(payload, "rollback", sections.get("ROLLBACK"))
    _include_fields(payload, "summary", sections[summary_name])
    _include_fields(payload, "handoff", sections[handoff_name])
    return payload


def _parse_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        match = _SECTION_PATTERN.fullmatch(line)
        if match is not None:
            if current is not None:
                sections[current] = "\n".join(lines).strip("\n")
            current = match.group(1)
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip("\n")
    return sections


def _parse_fields(value: str) -> dict[str, object]:
    fields: dict[str, object] = {}
    lines = value.splitlines()
    index = 0
    while index < len(lines):
        match = _FIELD_PATTERN.fullmatch(lines[index])
        if match is None:
            index += 1
            continue
        key = match.group(1)
        raw = match.group(2).lstrip()
        if not raw:
            block: list[str] = []
            cursor = index + 1
            while cursor < len(lines) and (
                lines[cursor].startswith("  ") or not lines[cursor]
            ):
                block.append(
                    lines[cursor][2:] if lines[cursor].startswith("  ") else ""
                )
                cursor += 1
            if block:
                raw = "\n".join(block).rstrip()
                index = cursor - 1
        decoded = _decode(raw)
        if key == "change":
            changes = fields.setdefault("change", [])
            if isinstance(changes, list):
                changes.append(decoded)
        else:
            fields[key] = decoded
        index += 1
    return fields


def _parse_records(
    value: str | None,
    id_key: str,
    selected_fields: tuple[str, ...],
) -> list[dict[str, object]]:
    if not value or value == "NOT_APPLICABLE":
        return []
    records: list[dict[str, object]] = []
    for chunk in _RECORD_SEPARATOR.split(value):
        if not chunk.startswith(f"{id_key}: "):
            continue
        metadata, marker, remainder = chunk.partition("\noutput_begin\n")
        fields = _select_fields(_parse_fields(metadata), selected_fields)
        for key in ("stdout", "stderr"):
            output_value = fields.get(key)
            if isinstance(output_value, str):
                fields[key] = _clip(output_value)
        if marker:
            output, _, _tail = remainder.rpartition("\noutput_end")
            fields["output"] = _clip(output)
        records.append(fields)
    return records


def _summarize_actions(
    records: list[dict[str, object]],
) -> dict[str, object]:
    counts: dict[str, int] = {}
    failed: list[dict[str, object]] = []
    for record in records:
        status = str(record.get("status", "UNKNOWN"))
        counts[status] = counts.get(status, 0) + 1
        if status == "FAILED":
            failed.append(record)
    summary: dict[str, object] = {
        "total": len(records),
        "status_counts": counts,
    }
    if failed:
        summary["failed"] = failed
    return summary


def _compact_success_records(
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [_compact_success_record(record) for record in records]


def _compact_success_record(
    record: dict[str, object],
) -> dict[str, object]:
    compact = dict(record)
    if compact.get("status") == "PASSED":
        for key in (
            "stdout",
            "stderr",
            "stdout_truncated",
            "stderr_truncated",
        ):
            compact.pop(key, None)
    return compact


def _include_selected(
    payload: dict[str, object],
    name: str,
    section: str | None,
    selected_fields: tuple[str, ...],
) -> None:
    if section is None or section == "NOT_APPLICABLE":
        return
    values = _select_fields(_parse_fields(section), selected_fields)
    if values:
        payload[name] = values


def _include_fields(
    payload: dict[str, object],
    name: str,
    section: str | None,
) -> None:
    if section is None or section == "NOT_APPLICABLE":
        return
    values = _parse_fields(section)
    if values:
        payload[name] = values


def _select_fields(
    fields: dict[str, object],
    selected: tuple[str, ...],
) -> dict[str, object]:
    return {
        key: fields[key]
        for key in selected
        if key in fields and fields[key] != "" and fields[key] is not None
    }


def _decode(value: str) -> object:
    if not value:
        return ""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _clip(value: str) -> str:
    if len(value) <= _OUTPUT_LIMIT:
        return value
    return value[:_OUTPUT_LIMIT] + "\n...[AI_LOG_OUTPUT_TRUNCATED]"


def _render_text(payload: dict[str, object]) -> str:
    lines = ["PATCHSHUTTLE_AI_LOG"]
    for key, value in payload.items():
        if key in {"schema", "source"}:
            _append_field(lines, key, value, "")
            continue
        lines.extend(("", key.upper()))
        _append_value(lines, value, "")
    return "\n".join(lines) + "\n"


def _append_value(lines: list[str], value: object, indent: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{indent}{key}:")
                _append_value(lines, item, indent + "  ")
            else:
                _append_field(lines, key, item, indent)
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                entries = list(item.items())
                if not entries:
                    lines.append(f"{indent}- {{}}")
                    continue
                first_key, first_value = entries[0]
                lines.append(f"{indent}- {first_key}: {_single_line(first_value)}")
                for key, nested in entries[1:]:
                    if isinstance(nested, (dict, list)):
                        lines.append(f"{indent}  {key}:")
                        _append_value(lines, nested, indent + "    ")
                    else:
                        _append_field(lines, key, nested, indent + "  ")
            else:
                lines.append(f"{indent}- {_single_line(item)}")
        return
    lines.append(f"{indent}{_single_line(value)}")


def _append_field(
    lines: list[str],
    key: str,
    value: object,
    indent: str,
) -> None:
    if isinstance(value, str) and "\n" in value:
        lines.append(f"{indent}{key}: |")
        lines.extend(f"{indent}  {line}" for line in value.splitlines())
        return
    lines.append(f"{indent}{key}: {_single_line(value)}")


def _single_line(value: object) -> str:
    if isinstance(value, str):
        return value if value else '""'
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


__all__ = ["render_ai_log", "summarize_ai_log"]

"""Contract tests for safe YAML job loading and structural validation."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import patchshuttle.parser as parser_module
from patchshuttle import (
    Action,
    Job,
    JobError,
    JobErrorCode,
    JobKind,
    load_job,
    validate_job,
)

PROJECT_ID = "PSH-8F41C2A73D905E61"
VALID_AUDIT_YAML = """\
protocol: 1
project_id: PSH-8F41C2A73D905E61
id: AUDIT-001
kind: audit
title: Inspect the project
actions:
  - tree:
      path: .
      depth: 4
"""


def write_job(
    tmp_path: Path,
    content: str = VALID_AUDIT_YAML,
    *,
    name: str = "audit.psh.yaml",
) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8", newline="")
    return path


def captured_job_error(
    path: Path, *, max_bytes: int = parser_module.DEFAULT_MAX_JOB_BYTES
) -> JobError:
    with pytest.raises(JobError) as caught:
        load_job(path, max_bytes=max_bytes)
    return caught.value


def test_load_job_returns_the_same_typed_model_used_by_python_api(
    tmp_path: Path,
) -> None:
    path = write_job(tmp_path)

    job = load_job(path)

    assert isinstance(job, Job)
    assert job.protocol == 1
    assert job.project_id == PROJECT_ID
    assert job.id == "AUDIT-001"
    assert job.kind is JobKind.AUDIT
    assert job.actions[0].name == "tree"
    assert job.actions[0].parameters.depth == 4


def test_validate_job_accepts_mapping_or_existing_job() -> None:
    payload: dict[str, Any] = yaml.safe_load(VALID_AUDIT_YAML)

    job = validate_job(payload)

    assert isinstance(job, Job)
    assert validate_job(job) is job


def test_exact_size_limit_is_allowed(tmp_path: Path) -> None:
    path = write_job(tmp_path)
    size = len(VALID_AUDIT_YAML.encode("utf-8"))

    assert load_job(path, max_bytes=size).id == "AUDIT-001"


@pytest.mark.parametrize(
    "name",
    (
        "audit.yaml",
        "audit.psh.yml",
        "audit.json",
        "audit.PSH.YAML",
    ),
)
def test_only_canonical_extension_is_accepted(tmp_path: Path, name: str) -> None:
    error = captured_job_error(write_job(tmp_path, name=name))

    assert error.code is JobErrorCode.JOB_EXTENSION_INVALID
    assert error.field_path is None


def test_missing_job_file_has_stable_error(tmp_path: Path) -> None:
    error = captured_job_error(tmp_path / "missing.psh.yaml")

    assert error.code is JobErrorCode.JOB_FILE_NOT_FOUND
    assert error.field_path is None


def test_job_path_must_be_a_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "directory.psh.yaml"
    path.mkdir()

    error = captured_job_error(path)

    assert error.code is JobErrorCode.JOB_FILE_NOT_REGULAR


def test_metadata_read_failure_has_stable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_job(tmp_path)

    def failed_stat(self: Path) -> object:
        raise OSError("unavailable")

    monkeypatch.setattr(Path, "stat", failed_stat)

    error = captured_job_error(path)

    assert error.code is JobErrorCode.JOB_FILE_READ_FAILED


@pytest.mark.parametrize(
    ("raised", "expected_code"),
    (
        (FileNotFoundError("missing"), JobErrorCode.JOB_FILE_NOT_FOUND),
        (OSError("unreadable"), JobErrorCode.JOB_FILE_READ_FAILED),
    ),
)
def test_file_read_failures_have_stable_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised: OSError,
    expected_code: JobErrorCode,
) -> None:
    path = write_job(tmp_path)

    def failed_read(self: Path) -> bytes:
        raise raised

    monkeypatch.setattr(Path, "read_bytes", failed_read)

    error = captured_job_error(path)

    assert error.code is expected_code


@pytest.mark.parametrize("max_bytes", (0, -1, True, 1.5))
def test_size_limit_itself_must_be_a_positive_integer(
    tmp_path: Path, max_bytes: object
) -> None:
    path = write_job(tmp_path)

    with pytest.raises(ValueError, match="max_bytes"):
        load_job(path, max_bytes=max_bytes)


def test_size_limit_is_checked_before_yaml_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_job(tmp_path)

    def unexpected_scan(*args: object, **kwargs: object) -> None:
        raise AssertionError("YAML parsing must not start")

    monkeypatch.setattr(parser_module.yaml, "scan", unexpected_scan)

    error = captured_job_error(path, max_bytes=1)

    assert error.code is JobErrorCode.JOB_SIZE_LIMIT_EXCEEDED


def test_size_is_checked_again_after_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_job(tmp_path)
    original = path.read_bytes()

    def enlarged_read(self: Path) -> bytes:
        return original + b" "

    monkeypatch.setattr(Path, "read_bytes", enlarged_read)

    error = captured_job_error(path, max_bytes=len(original))

    assert error.code is JobErrorCode.JOB_SIZE_LIMIT_EXCEEDED


def test_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invalid.psh.yaml"
    path.write_bytes(b"\xff\xfe\x00")

    error = captured_job_error(path)

    assert error.code is JobErrorCode.JOB_ENCODING_INVALID


@pytest.mark.parametrize(
    "content",
    (
        "protocol: [1\n",
        VALID_AUDIT_YAML + "---\nprotocol: 1\n",
    ),
)
def test_invalid_or_multiple_document_yaml_is_rejected(
    tmp_path: Path, content: str
) -> None:
    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.YAML_INVALID
    assert error.field_path == "$"
    assert error.line is not None
    assert error.column is not None


def test_parser_failure_without_yaml_mark_uses_root_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_job(tmp_path)

    def recursive_scan(*args: object, **kwargs: object) -> None:
        raise RecursionError("too deeply nested")

    monkeypatch.setattr(parser_module.yaml, "scan", recursive_scan)

    error = captured_job_error(path)

    assert error.code is JobErrorCode.YAML_INVALID
    assert error.line == 1
    assert error.column == 1


def test_safe_loader_failure_is_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_job(tmp_path)

    def failed_safe_load(*args: object, **kwargs: object) -> None:
        raise yaml.YAMLError("constructor failure")

    monkeypatch.setattr(parser_module.yaml, "safe_load", failed_safe_load)

    error = captured_job_error(path)

    assert error.code is JobErrorCode.YAML_INVALID
    assert error.field_path == "$"


def test_custom_yaml_tag_is_rejected_with_field_path(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML.replace(
        "title: Inspect the project", "title: !danger Inspect the project"
    )

    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.YAML_TAG_FORBIDDEN
    assert error.field_path == "$.title"
    assert error.line == 5
    assert error.column == 8


def test_explicit_standard_safe_tag_is_allowed(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML.replace(
        "title: Inspect the project", "title: !!str 2026-08-06"
    )

    job = load_job(write_job(tmp_path, content))

    assert job.title == "2026-08-06"


def test_yaml_anchor_is_rejected(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML.replace("path: .", "path: &workspace .")

    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.YAML_ANCHOR_FORBIDDEN
    assert error.field_path == "$"
    assert error.line == 8
    assert error.column == 13


def test_yaml_alias_is_rejected(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML.replace("path: .", "path: *workspace")

    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.YAML_ALIAS_FORBIDDEN
    assert error.field_path == "$"
    assert error.line == 8
    assert error.column == 13


def test_duplicate_top_level_key_reports_its_path(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML.replace("id: AUDIT-001", "id: AUDIT-001\nid: AUDIT-002")

    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.YAML_DUPLICATE_KEY
    assert error.field_path == "$.id"


def test_duplicate_nested_key_reports_its_path(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML.replace(
        "      depth: 4", "      depth: 4\n      depth: 5"
    )

    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.YAML_DUPLICATE_KEY
    assert error.field_path == "$.actions[0].tree.depth"


def test_mapping_keys_must_be_strings(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML + "1: invalid\n"

    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.YAML_MAPPING_KEY_INVALID
    assert error.field_path == "$"


@pytest.mark.parametrize("content", ("", "[]\n", "value\n"))
def test_job_document_root_must_be_a_mapping(tmp_path: Path, content: str) -> None:
    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.JOB_ROOT_INVALID
    assert error.field_path == "$"


def test_unknown_top_level_field_reports_schema_path(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML + "command: echo unsafe\n"

    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.JOB_SCHEMA_INVALID
    assert error.field_path == "$.command"


def test_non_identifier_field_uses_bracketed_schema_path(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML + '"bad.field": value\n'

    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.JOB_SCHEMA_INVALID
    assert error.field_path == '$["bad.field"]'


def test_unknown_action_field_reports_compact_schema_path(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML.replace(
        "      depth: 4", "      depth: 4\n      command: unsafe"
    )

    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.JOB_SCHEMA_INVALID
    assert error.field_path == "$.actions[0].tree.command"
    assert "TreeAction" not in error.field_path


def test_unknown_action_name_reports_action_entry_path(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML.replace("tree:", "shell:")

    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.JOB_SCHEMA_INVALID
    assert error.field_path == "$.actions[0]"


def test_invalid_protocol_reports_schema_path(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML.replace("protocol: 1", "protocol: 2")

    error = captured_job_error(write_job(tmp_path, content))

    assert error.code is JobErrorCode.JOB_SCHEMA_INVALID
    assert error.field_path == "$.protocol"


def test_validate_job_rejects_non_mapping_input() -> None:
    with pytest.raises(JobError) as caught:
        validate_job(["not", "a", "mapping"])

    assert caught.value.code is JobErrorCode.JOB_ROOT_INVALID
    assert caught.value.field_path == "$"


def test_job_error_string_contains_stable_code_and_field_path(tmp_path: Path) -> None:
    content = VALID_AUDIT_YAML + "command: echo unsafe\n"

    error = captured_job_error(write_job(tmp_path, content))
    rendered = str(error)

    assert "JOB_SCHEMA_INVALID" in rendered
    assert "$.command" in rendered


def test_job_error_string_can_render_source_position_without_field_path() -> None:
    error = JobError(
        JobErrorCode.YAML_INVALID,
        "invalid input",
        line=2,
        column=7,
    )

    assert str(error) == "[YAML_INVALID] line 2, column 7: invalid input"


def test_loaded_actions_remain_deeply_immutable(tmp_path: Path) -> None:
    job = load_job(write_job(tmp_path))
    action: Action = job.actions[0]

    with pytest.raises(ValidationError):
        action.parameters.path = "changed"

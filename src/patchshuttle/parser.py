"""Safe loading and structural validation for ``.psh.yaml`` jobs."""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken

from patchshuttle.errors import JobError, JobErrorCode
from patchshuttle.models import Job

DEFAULT_MAX_JOB_BYTES = 2_000_000
_CANONICAL_EXTENSION = ".psh.yaml"
_STRING_TAG = "tag:yaml.org,2002:str"
_SAFE_STANDARD_TAGS = frozenset(
    tag for tag in yaml.SafeLoader.yaml_constructors if isinstance(tag, str)
)
_PLAIN_PATH_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def load_job(
    path: str | PathLike[str],
    *,
    max_bytes: int = DEFAULT_MAX_JOB_BYTES,
) -> Job:
    """Load one canonical UTF-8 YAML file and return a validated immutable job."""

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    job_path = Path(path)
    if not job_path.name.endswith(_CANONICAL_EXTENSION):
        raise JobError(
            JobErrorCode.JOB_EXTENSION_INVALID,
            f"job filename must end with {_CANONICAL_EXTENSION}",
        )

    try:
        file_stat = job_path.stat()
    except FileNotFoundError as exc:
        raise JobError(
            JobErrorCode.JOB_FILE_NOT_FOUND,
            "job file was not found",
        ) from exc
    except OSError as exc:
        raise JobError(
            JobErrorCode.JOB_FILE_READ_FAILED,
            "job file metadata could not be read",
        ) from exc

    if not stat.S_ISREG(file_stat.st_mode):
        raise JobError(
            JobErrorCode.JOB_FILE_NOT_REGULAR,
            "job path must identify a regular file",
        )
    if file_stat.st_size > max_bytes:
        raise JobError(
            JobErrorCode.JOB_SIZE_LIMIT_EXCEEDED,
            f"job file exceeds the {max_bytes}-byte input limit",
        )

    try:
        raw = job_path.read_bytes()
    except FileNotFoundError as exc:
        raise JobError(
            JobErrorCode.JOB_FILE_NOT_FOUND,
            "job file was not found",
        ) from exc
    except OSError as exc:
        raise JobError(
            JobErrorCode.JOB_FILE_READ_FAILED,
            "job file could not be read",
        ) from exc

    if len(raw) > max_bytes:
        raise JobError(
            JobErrorCode.JOB_SIZE_LIMIT_EXCEEDED,
            f"job file exceeds the {max_bytes}-byte input limit",
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JobError(
            JobErrorCode.JOB_ENCODING_INVALID,
            "job file must be valid UTF-8 text",
        ) from exc

    return validate_job(_load_yaml(text))


def validate_job(value: object) -> Job:
    """Validate mapping-like data with the same models used by YAML jobs."""

    if isinstance(value, Job):
        return value
    if not isinstance(value, Mapping):
        raise JobError(
            JobErrorCode.JOB_ROOT_INVALID,
            "job document root must be a mapping",
            field_path="$",
        )

    try:
        return Job.model_validate(value)
    except ValidationError as exc:
        first_error = exc.errors(include_url=False)[0]
        raise JobError(
            JobErrorCode.JOB_SCHEMA_INVALID,
            str(first_error["msg"]),
            field_path=_format_validation_path(first_error["loc"]),
        ) from exc


def _load_yaml(text: str) -> Mapping[str, Any]:
    try:
        _reject_references(text)
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except JobError:
        raise
    except (yaml.YAMLError, RecursionError) as exc:
        raise _yaml_syntax_error(exc) from exc

    if node is None:
        raise JobError(
            JobErrorCode.JOB_ROOT_INVALID,
            "job document root must be a mapping",
            field_path="$",
        )

    _validate_node(node, path="$")

    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as exc:
        raise _yaml_syntax_error(exc) from exc

    if not isinstance(data, Mapping):
        raise JobError(
            JobErrorCode.JOB_ROOT_INVALID,
            "job document root must be a mapping",
            field_path="$",
        )
    return data


def _reject_references(text: str) -> None:
    for token in yaml.scan(text, Loader=yaml.SafeLoader):
        if isinstance(token, AnchorToken):
            raise JobError(
                JobErrorCode.YAML_ANCHOR_FORBIDDEN,
                "YAML anchors are not allowed",
                field_path="$",
                line=token.start_mark.line + 1,
                column=token.start_mark.column + 1,
            )
        if isinstance(token, AliasToken):
            raise JobError(
                JobErrorCode.YAML_ALIAS_FORBIDDEN,
                "YAML aliases are not allowed",
                field_path="$",
                line=token.start_mark.line + 1,
                column=token.start_mark.column + 1,
            )


def _validate_node(node: Node, *, path: str) -> None:
    if node.tag not in _SAFE_STANDARD_TAGS:
        raise JobError(
            JobErrorCode.YAML_TAG_FORBIDDEN,
            "custom YAML tags are not allowed",
            field_path=path,
            line=node.start_mark.line + 1,
            column=node.start_mark.column + 1,
        )

    if isinstance(node, MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode) or key_node.tag != _STRING_TAG:
                raise JobError(
                    JobErrorCode.YAML_MAPPING_KEY_INVALID,
                    "YAML mapping keys must be strings",
                    field_path=path,
                    line=key_node.start_mark.line + 1,
                    column=key_node.start_mark.column + 1,
                )

            key = key_node.value
            child_path = _append_field(path, key)
            if key in seen:
                raise JobError(
                    JobErrorCode.YAML_DUPLICATE_KEY,
                    f"duplicate mapping key {key!r}",
                    field_path=child_path,
                    line=key_node.start_mark.line + 1,
                    column=key_node.start_mark.column + 1,
                )
            seen.add(key)
            _validate_node(value_node, path=child_path)

    elif isinstance(node, SequenceNode):
        for index, item in enumerate(node.value):
            _validate_node(item, path=f"{path}[{index}]")


def _yaml_syntax_error(exc: BaseException) -> JobError:
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    return JobError(
        JobErrorCode.YAML_INVALID,
        "job file contains invalid YAML syntax",
        field_path="$",
        line=mark.line + 1 if mark is not None else 1,
        column=mark.column + 1 if mark is not None else 1,
    )


def _format_validation_path(location: tuple[int | str, ...]) -> str:
    path = "$"
    for part in location:
        if isinstance(part, int):
            path = f"{path}[{part}]"
        else:
            path = _append_field(path, str(part))
    return path


def _append_field(path: str, field: str) -> str:
    if _PLAIN_PATH_SEGMENT.fullmatch(field):
        return f"{path}.{field}"
    return f"{path}[{json.dumps(field, ensure_ascii=False)}]"


__all__ = ["DEFAULT_MAX_JOB_BYTES", "load_job", "validate_job"]

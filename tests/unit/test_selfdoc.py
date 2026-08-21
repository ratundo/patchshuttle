"""Contract tests for workspace-independent self-documentation."""

from __future__ import annotations

import json

import pytest

from patchshuttle.models import AUDIT_ACTION_NAMES, CHANGE_ACTION_NAMES, Job
from patchshuttle.selfdoc import (
    AUDIT_ACTIONS,
    CHANGE_ACTIONS,
    EXPLAIN_TOPICS,
    format_capability_list,
    render_capabilities,
    render_explanation,
    render_schema,
)


def test_capabilities_are_stable_and_include_safety_boundaries() -> None:
    rendered = render_capabilities()

    assert rendered.startswith(
        "PATCHSHUTTLE_CAPABILITIES\n" "patchshuttle_version: 0.1.0a2\n" "protocol: 1\n"
    )
    assert "job_kinds: [audit, patch, verify]\n" in rendered
    assert "change_actions: [create_directory, create_file, replace_exact" in rendered
    assert "formatters: [isort, black]\n" in rendered
    assert "arbitrary_shell_action: unavailable\n" in rendered
    assert "protected_path_policy: local and not job-configurable\n" in rendered
    assert rendered.endswith(
        f"explain_topics: {format_capability_list(EXPLAIN_TOPICS)}\n"
    )
    assert set(AUDIT_ACTIONS) == AUDIT_ACTION_NAMES
    assert set(CHANGE_ACTIONS) == CHANGE_ACTION_NAMES


def test_schema_is_the_exact_deterministic_installed_model() -> None:
    rendered = render_schema()

    assert rendered.endswith("\n")
    assert json.loads(rendered) == Job.model_json_schema()
    assert (
        rendered
        == json.dumps(
            Job.model_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def test_explanation_is_case_insensitive_bounded_and_validated() -> None:
    rendered = render_explanation("REPLACE_EXACT")

    assert rendered.startswith(
        "PATCHSHUTTLE_EXPLAIN\ntopic: replace_exact\n" "category: change_action\n"
    )
    assert "whitespace: Exact means exact" in rendered
    assert "expected_count: 1\n" in rendered

    with pytest.raises(ValueError, match="unknown explanation topic"):
        render_explanation("unknown")

from pathlib import PurePosixPath
from types import SimpleNamespace

import patchshuttle.logging as logging_module
from patchshuttle._python_discovery import evaluate_python_discovery
from patchshuttle.audit import AuditActionResult
from patchshuttle.models import Job

PROJECT_ID = "PSH-8F41C2A73D905E61"


def _result(action_id: str, name: str, output: str) -> AuditActionResult:
    return AuditActionResult(
        id=action_id,
        name=name,
        status="COMPLETED",
        scope=(),
        started_at="2026-08-26T10:00:00+00:00",
        duration_ms=1,
        output=output,
    )


def test_python_audit_evidence_counts_only_executed_bounded_output() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-DISCOVERY-EVIDENCE",
        kind="audit",
        actions=[
            {"read": {"path": "src/a.py"}},
            {"read_symbol": {"path": "src/a.py", "symbol": "run"}},
            {
                "search_context": {
                    "path": "src",
                    "text": "run",
                    "glob": "*.py",
                }
            },
            {"search": {"path": "docs", "text": "run", "glob": "*.md"}},
            {"find_files": {"path": "src", "glob": "**/*.py"}},
            {"file_info": {"path": "src/a.py"}},
            {"search": {"path": "docs", "text": "pending", "glob": "*.md"}},
        ],
    )
    results = (
        _result("action_001", "read", "path: src/a.py\n     1: VALUE = 1"),
        _result(
            "action_002",
            "read_symbol",
            "path: src/a.py\n     4: def run():\n     5:     return 1",
        ),
        _result(
            "action_003",
            "search_context",
            "path: src/b.py\n>    7: def run():\n     8:     return 2",
        ),
        _result("action_004", "search", "matches: 0"),
        _result("action_005", "find_files", "src/a.py\nsrc/b.py"),
        _result("action_006", "file_info", "path: src/a.py\nsize_bytes: 20"),
    )

    evaluation = evaluate_python_discovery(job, audit_results=results)

    assert evaluation.applicable is True
    assert evaluation.explicit_python_paths == (PurePosixPath("src/a.py"),)
    assert evaluation.python_read_actions == 1
    assert evaluation.python_read_symbol_actions == 1
    assert evaluation.python_search_actions == 1
    assert evaluation.unclassified_search_actions == 1
    assert evaluation.python_find_files_actions == 1
    assert evaluation.python_files_reported == 0
    assert evaluation.python_file_limit_reached is False
    assert evaluation.python_search_matches == 0
    assert evaluation.python_searches_limited == 0
    assert evaluation.python_audit_duration_ms == 5
    expected_outputs = "".join(
        result.output
        for result in (
            results[0],
            results[1],
            results[2],
            results[4],
            results[5],
        )
    )
    assert evaluation.python_audit_output_bytes == len(expected_outputs.encode("utf-8"))
    assert evaluation.python_audit_output_lines == 12
    assert evaluation.reason_codes == (
        "PYTHON_FILE_READ_USED",
        "PYTHON_SYMBOL_READ_USED",
        "PYTHON_TEXT_SEARCH_USED",
        "UNCLASSIFIED_TEXT_SEARCH_USED",
        "PYTHON_FILE_DISCOVERY_USED",
    )


def test_python_discovery_aggregates_matches_limits_and_duration() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-DISCOVERY-AGGREGATES",
        kind="audit",
        actions=[
            {"find_files": {"path": ".", "glob": "*.py"}},
            {"find_files": {"path": "src", "glob": "*.py"}},
            {"search_context": {"path": ".", "text": "class ", "glob": "*.py"}},
            {"search": {"path": ".", "text": "def ", "glob": "*.py"}},
        ],
    )
    results = (
        _result(
            "action_001",
            "find_files",
            "glob: *.py\nmatches: 300\nresult_limit_reached: true\nsrc/a.py",
        ),
        _result("action_002", "find_files", "glob: *.py\nmatches: 12"),
        _result(
            "action_003",
            "search_context",
            "literal: class \nmatches: 40\nresult_limit_reached: true",
        ),
        _result("action_004", "search", "literal: def \nmatches: 5"),
    )

    evaluation = evaluate_python_discovery(job, audit_results=results)

    assert evaluation.python_files_reported == 312
    assert evaluation.python_file_limit_reached is True
    assert evaluation.python_search_matches == 45
    assert evaluation.python_searches_limited == 1
    assert evaluation.python_audit_duration_ms == 4


def test_python_patch_targeting_is_reported_without_an_index_verdict() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-DISCOVERY-EVIDENCE",
        kind="patch",
        actions=[
            {
                "replace_exact": {
                    "path": "src/a.py",
                    "old": "OLD",
                    "new": "NEW",
                }
            },
            {
                "replace_symbol": {
                    "path": "src/b.py",
                    "symbol": "run",
                    "expected_sha256": "a" * 64,
                    "new_content": "def run():\n    return 2\n",
                }
            },
            {
                "replace_range": {
                    "path": "src/c.py",
                    "start_line": 1,
                    "end_line": 1,
                    "expected_content": "OLD\n",
                    "new_content": "NEW\n",
                }
            },
            {"create_file": {"path": "src/new.py", "content": "VALUE = 1\n"}},
        ],
    )

    evaluation = evaluate_python_discovery(job)

    assert evaluation.declared_python_text_actions == 1
    assert evaluation.declared_python_symbol_actions == 1
    assert evaluation.declared_python_line_actions == 1
    assert evaluation.reason_codes == (
        "PYTHON_TEXT_TARGETING_USED",
        "PYTHON_SYMBOL_TARGETING_USED",
        "PYTHON_LINE_TARGETING_USED",
    )


def test_python_planning_failure_is_recorded_as_evidence_not_a_decision() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-DISCOVERY-FAILURE",
        kind="patch",
        actions=[
            {
                "replace_exact": {
                    "path": "src/a.py",
                    "old": "OLD",
                    "new": "NEW",
                }
            }
        ],
    )

    evaluation = evaluate_python_discovery(
        job,
        failure_code="OCCURRENCE_COUNT_MISMATCH",
        failed_path="src/a.py",
    )

    assert evaluation.failure_signal == "TEXT_TARGETING_FAILURE"
    assert evaluation.reason_codes == (
        "PYTHON_TEXT_TARGETING_USED",
        "TEXT_TARGETING_FAILURE",
    )


def test_non_python_job_is_not_applicable() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-NON-PYTHON",
        kind="audit",
        actions=[{"environment": {}}],
    )

    evaluation = evaluate_python_discovery(job)

    assert evaluation.applicable is False
    assert evaluation.reason_codes == ()
    assert evaluation.explicit_python_paths == ()


def test_python_parse_failure_does_not_require_a_failed_path() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-PYTHON-PARSE-FAILURE",
        kind="audit",
        actions=[{"environment": {}}],
    )

    evaluation = evaluate_python_discovery(
        job,
        failure_code="PYTHON_SOURCE_INVALID",
    )

    assert evaluation.applicable is True
    assert evaluation.failure_signal == "PYTHON_PARSE_FAILURE"
    assert evaluation.reason_codes == ("PYTHON_PARSE_FAILURE",)


def test_non_python_line_failure_is_not_structural_index_evidence() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-NON-PYTHON-FAILURE",
        kind="patch",
        actions=[
            {
                "replace_range": {
                    "path": "README.md",
                    "start_line": 1,
                    "end_line": 1,
                    "expected_content": "old\n",
                    "new_content": "new\n",
                }
            }
        ],
    )

    evaluation = evaluate_python_discovery(
        job,
        failure_code="LINE_RANGE_GUARD_MISMATCH",
        failed_path="README.md",
    )

    assert evaluation.applicable is False
    assert evaluation.failure_signal is None

    without_path = evaluate_python_discovery(
        job,
        failure_code="LINE_RANGE_OUT_OF_BOUNDS",
    )
    assert without_path.failure_signal is None


def test_log_section_uses_completed_partial_audit_results_after_failure() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-PARTIAL-DISCOVERY-EVIDENCE",
        kind="audit",
        actions=[{"read": {"path": "src/a.py"}}],
    )
    result = _result(
        "action_001",
        "read",
        "path: src/a.py\n     1: VALUE = 1",
    )
    data = SimpleNamespace(
        audit_results=(),
        error=SimpleNamespace(audit_results=(result,), path="src/a.py"),
        job=job,
        failure_code="ACTION_FAILED",
    )

    section = logging_module._python_discovery_section(data)

    assert "evidence_status: COLLECTED" in section
    assert "python_read_actions: 1" in section
    assert "python_file_limit_reached: false" in section
    assert "python_audit_duration_ms: 1" in section

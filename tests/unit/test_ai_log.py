import json

import pytest

from patchshuttle._ai_log import render_ai_log, summarize_ai_log

FULL_LOG = """\
=== HEADER ===
patchshuttle_version: 0.1.0a3
protocol: 1
timestamp: 2026-08-25T13:00:00+02:00
redaction: BEST_EFFORT_ENABLED
redaction_guarantee: NONE
=== JOB ===
job_id: AUDIT-001
job_hash: abcdef0123456789
kind: audit
title: Inspect source
=== PLAN ===
planned_actions: 1
planned_checks: 0
files_to_create: []
files_to_modify: []
directories_to_create: []
formatter_plan: 0
preflight_checks: 0
protected_paths: PASS
automatic_rollback: disabled
=== AUDIT ===
action_id: action_001
action_type: read
path_or_scope: ["src/example.py"]
status: COMPLETED
started_at: 2026-08-25T11:00:00+00:00
duration_ms: 1
expected: READ_ONLY_OBSERVATION
actual: COMPLETED
details: OUTPUT_COMPLETE
output_begin
def run():
    return 1
output_end
=== INITIAL_CHECKS ===
NOT_APPLICABLE
=== FINAL_CHECKS ===
NOT_APPLICABLE
=== WORKSPACE_COMPARISON ===
status: PASS
changes: 0
unexpected_changes: 0
=== ROLLBACK ===
NOT_APPLICABLE
=== SUMMARY ===
result: COMPLETED
failure_stage: NOT_APPLICABLE
failure_code: NOT_APPLICABLE
exit_code: 0
changed_files: []
created_files: []
created_directories: []
checks_passed: 0
formatting_status: NOT_APPLICABLE
rollback_status: NOT_REQUIRED
next_recommended_step: review_log_and_continue
=== PATCHSHUTTLE_AI_HANDOFF ===
protocol: 1
project_id: PSH-8F41C2A73D905E61
job_id: AUDIT-001
job_hash: abcdef01
kind: audit
result: COMPLETED
failure_stage: NOT_APPLICABLE
failure_code: NOT_APPLICABLE
failed_item: NOT_APPLICABLE
rollback: NOT_REQUIRED
ai_handoff_version: 2
capabilities_hash: abc123
next_expected_response: next_patch_or_audit
=== END_PATCHSHUTTLE_AI_HANDOFF ===
"""


ATTEMPT_LOG = """\
=== PATCHSHUTTLE_ATTEMPT ===
patchshuttle_version: 0.1.0a3
protocol: 1
timestamp: 2026-08-25T13:00:00+02:00
redaction: BEST_EFFORT_ENABLED
redaction_guarantee: NONE
project_id: PSH-8F41C2A73D905E61
command: plan
job_file: patches/inbox/BROKEN.psh.yaml
job_id: BROKEN
job_hash: 01234567
kind: patch
error:
  expected one exact anchor
  second line

=== SUMMARY ===
result: PLAN_FAILED
failure_stage: PLANNING
failure_code: OCCURRENCE_COUNT_MISMATCH
failed_item: action_001
failed_path: src/example.py
exit_code: 5
changed_files: []
created_files: []
created_directories: []
rollback_status: NOT_STARTED
next_recommended_step: return_this_log_to_the_ai_for_a_corrected_job
=== PATCHSHUTTLE_AI_HANDOFF ===
protocol: 1
project_id: PSH-8F41C2A73D905E61
job_id: BROKEN
job_hash: 01234567
kind: patch
result: PLAN_FAILED
failure_stage: PLANNING
failure_code: OCCURRENCE_COUNT_MISMATCH
failed_item: action_001
failed_path: src/example.py
rollback: NOT_STARTED
ai_handoff_version: 2
capabilities_hash: abc123
next_expected_response: corrected_patch_or_audit
=== END_PATCHSHUTTLE_AI_HANDOFF ===
"""


def test_run_log_compacts_audit_output_and_stable_fields() -> None:
    payload = summarize_ai_log(FULL_LOG, source="patches/logs/run.log")

    assert payload["schema"] == "patchshuttle.ai_log.v1"
    assert payload["job"] == {
        "job_id": "AUDIT-001",
        "job_hash": "abcdef0123456789",
        "kind": "audit",
        "title": "Inspect source",
    }
    assert payload["audit"] == [
        {
            "action_id": "action_001",
            "action_type": "read",
            "path_or_scope": ["src/example.py"],
            "status": "COMPLETED",
            "actual": "COMPLETED",
            "details": "OUTPUT_COMPLETE",
            "output": "def run():\n    return 1",
        }
    ]
    assert payload["summary"]["result"] == "COMPLETED"
    rendered = render_ai_log(
        FULL_LOG,
        source="patches/logs/run.log",
        json_output=False,
    )
    assert rendered.startswith("PATCHSHUTTLE_AI_LOG\n")
    assert "AUDIT\n- action_id: action_001\n" in rendered
    assert "started_at" not in rendered
    assert "duration_ms" not in rendered


def test_attempt_log_renders_equivalent_compact_json() -> None:
    rendered = render_ai_log(
        ATTEMPT_LOG,
        source="patches/logs/attempt.log",
        json_output=True,
    )

    payload = json.loads(rendered)
    assert payload["attempt"]["command"] == "plan"
    assert payload["attempt"]["error"] == ("expected one exact anchor\nsecond line")
    assert payload["summary"]["result"] == "PLAN_FAILED"
    assert payload["handoff"]["failed_item"] == "action_001"
    assert rendered.endswith("\n")


def test_compact_passed_check_keeps_warning_signal_without_full_output() -> None:
    check = (
        "check_id: check_001\n"
        "profile: django_check\n"
        "exit_code: 0\n"
        "warning_analysis: COMPLETE\n"
        "known_warnings: 1\n"
        "new_warnings: 1\n"
        'new_warning_details: ["app.Model: (models.W001) New."]\n'
        "stdout: noisy stdout\n"
        "stderr: noisy stderr\n"
        "stdout_truncated: false\n"
        "stderr_truncated: false\n"
        "status: PASSED"
    )
    text = FULL_LOG.replace(
        "=== INITIAL_CHECKS ===\nNOT_APPLICABLE",
        "=== INITIAL_CHECKS ===\n" + check,
    )

    payload = summarize_ai_log(text, source="patches/logs/run.log")
    result = payload["checks"]["initial"][0]

    assert result["known_warnings"] == 1
    assert result["new_warnings"] == 1
    assert result["new_warning_details"] == ["app.Model: (models.W001) New."]
    assert "stdout" not in result
    assert "stderr" not in result


def test_log_without_summary_and_handoff_is_rejected() -> None:
    with pytest.raises(ValueError, match="summary and handoff"):
        summarize_ai_log("=== HEADER ===\nprotocol: 1\n", source="broken.log")

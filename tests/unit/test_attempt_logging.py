"""Contracts for validation and planning attempt logs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import patchshuttle.workspace as workspace_module
from patchshuttle.context import create_handoff
from patchshuttle.logging import (
    AttemptLogData,
    current_run_clock,
    latest_log_path,
    write_attempt_log,
)
from patchshuttle.models import Job
from patchshuttle.planner import normalized_job_hash
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(workspace_module, "generate_project_id", lambda: PROJECT_ID)
    return init_workspace(tmp_path).workspace


def test_validation_failure_log_handles_an_unparsed_job_and_redacts(
    workspace: Workspace,
) -> None:
    data = AttemptLogData(
        workspace=workspace,
        clock=current_run_clock(workspace),
        command="validate",
        job_file=Path("patches/inbox/BROKEN.psh.yaml"),
        result="VALIDATION_FAILED",
        failure_stage="VALIDATION",
        failure_code="YAML_INVALID",
        exit_code=2,
        error="token=visible\nsecond line",
        failed_path="$",
    )

    path = write_attempt_log(data)
    text = path.read_text("utf-8")

    assert path.name.endswith("_VALIDATION_FAILED.log")
    assert latest_log_path(workspace) == path
    assert "command: validate\n" in text
    assert "job_id: UNKNOWN\n" in text
    assert "job_project_id: UNKNOWN\n" in text
    assert "job_hash: UNKNOWN\n" in text
    assert "kind: UNKNOWN\n" in text
    assert "error:\n  token=[REDACTED]\n  second line\n" in text
    assert "archived_job_copy: NOT_APPLICABLE\n" in text
    assert "registry_updated: false\n" in text
    assert "=== SUMMARY ===\nresult: VALIDATION_FAILED\n" in text
    assert "failed_item: NOT_APPLICABLE\n" in text
    assert "failed_path: $\n" in text
    assert "=== PATCHSHUTTLE_AI_HANDOFF ===\nprotocol: 1\n" in text
    assert text.endswith("=== END_PATCHSHUTTLE_AI_HANDOFF ===\n")
    assert "ai_handoff_version: 2\n" in text
    assert "capabilities_hash:" in text
    assert "available_job_kinds:" not in text
    assert "available_audit_actions:" not in text
    assert "available_change_actions:" not in text
    assert "available_checks:" not in text

    handoff = create_handoff(workspace).path.read_text("utf-8")
    assert "=== LATEST_RUN_SUMMARY ===\nresult: VALIDATION_FAILED\n" in handoff
    assert "=== LATEST_AI_HANDOFF ===\nprotocol: 1\n" in handoff
    assert "failure_code: YAML_INVALID\n" in handoff


def test_plan_failure_log_includes_valid_job_identity_and_local_policy(
    workspace: Workspace,
) -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-LOG-001",
        kind="patch",
        actions=(
            {
                "create_file": {
                    "path": "example.py",
                    "content": "VALUE = 1\n",
                }
            },
        ),
        checks=({"import_check": {"modules": ["json"]}},),
    )
    logging = workspace.config.logging.model_copy(
        update={"redact_known_secrets": False}
    )
    unredacted = replace(
        workspace,
        config=workspace.config.model_copy(update={"logging": logging}),
    )

    path = write_attempt_log(
        AttemptLogData(
            workspace=unredacted,
            clock=current_run_clock(unredacted),
            command="plan",
            job_file=Path("job.psh.yaml"),
            result="PLAN_FAILED",
            failure_stage="PLAN",
            failure_code="PATH_PROTECTED",
            exit_code=4,
            error="token=visible",
            job=job,
            job_hash=normalized_job_hash(job),
            failed_item="action_001",
            failed_path=".env",
        )
    )
    text = path.read_text("utf-8")

    assert "redaction: DISABLED_BY_LOCAL_POLICY\n" in text
    assert "job_id: PATCH-LOG-001\n" in text
    assert f"job_project_id: {PROJECT_ID}\n" in text
    assert f"job_hash: {normalized_job_hash(job)}\n" in text
    assert "kind: patch\n" in text
    assert "error:\n  token=visible\n" in text
    assert "result: PLAN_FAILED\n" in text
    assert "failed_item: action_001\n" in text
    assert "failed_path: .env\n" in text


def test_attempt_log_rejects_an_unknown_result(workspace: Workspace) -> None:
    with pytest.raises(ValueError, match="attempt log result is invalid"):
        write_attempt_log(
            AttemptLogData(
                workspace=workspace,
                clock=current_run_clock(workspace),
                command="plan",
                job_file=Path("job.psh.yaml"),
                result="UNKNOWN",
                failure_stage="PLAN",
                failure_code="UNKNOWN",
                exit_code=1,
                error="injected",
            )
        )

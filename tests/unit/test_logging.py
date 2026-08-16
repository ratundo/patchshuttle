"""Unit tests for predictable logs, exact archives, and secret redaction."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import patchshuttle.logging as logging_module
import patchshuttle.workspace as workspace_module
from patchshuttle.checks import CheckResult, CheckStatus
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.formatters import FormatterResult, FormatterStatus
from patchshuttle.logging import (
    STANDARD_SECTIONS,
    RunClock,
    RunLogData,
    archive_job_source,
    current_run_clock,
    latest_log_path,
    redact_text,
    write_run_log,
)
from patchshuttle.models import Job
from patchshuttle.planner import plan_job
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
INSTANT = datetime(2026, 8, 7, 12, 34, 56, tzinfo=timezone.utc)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    monkeypatch.setattr(logging_module, "_utc_now", lambda: INSTANT)
    return init_workspace(tmp_path).workspace


@pytest.fixture
def job() -> Job:
    return Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-014",
        kind="patch",
        title="Record one run",
        actions=[{"create_file": {"path": "notes.txt", "content": "notes\n"}}],
        checks=[{"import_check": {"modules": ["json"]}}],
    )


def configured_workspace(workspace: Workspace, **logging_updates) -> Workspace:
    logging = workspace.config.logging.model_copy(update=logging_updates)
    config = workspace.config.model_copy(update={"logging": logging})
    return replace(workspace, config=config)


def test_run_clock_uses_utc_local_and_rejects_unknown_zone(
    workspace: Workspace,
) -> None:
    utc_workspace = configured_workspace(workspace, timezone="UTC")
    clock = current_run_clock(utc_workspace)

    assert clock.iso_timestamp == "2026-08-07T12:34:56+00:00"
    assert clock.filename_timestamp == "2026_08_07_12_34_56"
    assert current_run_clock(workspace).occurred_at.tzinfo is not None

    invalid = configured_workspace(workspace, timezone="Not/A_Real_Zone")
    with pytest.raises(ExecutionError) as caught:
        current_run_clock(invalid)
    assert caught.value.code is ExecutionErrorCode.OPERATIONAL_RECORD_FAILED
    assert caught.value.path == "patches/patchshuttle.toml"


def test_archive_is_byte_exact_and_adds_numeric_collision_suffix(
    workspace: Workspace,
    job: Job,
) -> None:
    source = b"protocol: 1\r\ntitle: caf\xc3\xa9\r\n"
    clock = RunClock(INSTANT)

    first = archive_job_source(
        workspace,
        job=job,
        job_hash="a" * 64,
        clock=clock,
        source=source,
        successful=True,
    )
    second = archive_job_source(
        workspace,
        job=job,
        job_hash="a" * 64,
        clock=clock,
        source=source,
        successful=True,
    )
    failed = archive_job_source(
        workspace,
        job=job,
        job_hash="a" * 64,
        clock=clock,
        source=source,
        successful=False,
    )

    assert first.name == "PATCH-014_2026_08_07_12_34_56_aaaaaaaa.psh.yaml"
    assert second.name == "PATCH-014_2026_08_07_12_34_56_aaaaaaaa_2.psh.yaml"
    assert failed.parent.name == "failed"
    assert first.read_bytes() == second.read_bytes() == failed.read_bytes() == source


def test_redaction_masks_common_shapes_and_preserves_nonsecret_context() -> None:
    private_key = (
        "-----BEGIN PRIVATE KEY-----\n" "very-secret-body\n" "-----END PRIVATE KEY-----"
    )
    text = "\n".join(
        (
            "token=plain-token",
            '"api_key": "quoted-value"',
            "Authorization: Bearer bearer-value",
            "--password command-value",
            "ghp_123456789012345678901234567890123456",
            "sk-1234567890abcdefghijklmnop",
            "xoxb-12345678901234567890",
            private_key,
            "ordinary: visible",
        )
    )

    redacted = redact_text(text)

    for secret in (
        "plain-token",
        "quoted-value",
        "bearer-value",
        "command-value",
        "ghp_123456789012345678901234567890123456",
        "sk-1234567890abcdefghijklmnop",
        "xoxb-12345678901234567890",
        "very-secret-body",
    ):
        assert secret not in redacted
    assert "ordinary: visible" in redacted
    assert "[REDACTED PRIVATE KEY]" in redacted
    assert redacted.count("[REDACTED]") >= 7


def test_log_has_every_fixed_section_ai_footer_redaction_and_collision(
    workspace: Workspace,
    job: Job,
) -> None:
    archive = workspace.root / "patches/failed/source.psh.yaml"
    archive.write_bytes(b"source\n")
    data = RunLogData(
        workspace=workspace,
        job=job.model_copy(update={"description": "token=log-secret"}),
        job_hash="a" * 64,
        clock=RunClock(INSTANT),
        result="PATCH_ID_CONFLICT",
        exit_code=3,
        failure_stage="JOB",
        failure_code="PATCH_ID_CONFLICT",
        archived_job_path=archive,
        error=ExecutionError(
            ExecutionErrorCode.PATCH_ID_CONFLICT,
            "conflict",
            item_id=job.id,
        ),
    )

    first = write_run_log(data)
    second = write_run_log(data)
    text = first.read_text(encoding="utf-8")

    assert first.name == "log_2026_08_07_12_34_56_PATCH-014.log"
    assert second.name == "log_2026_08_07_12_34_56_PATCH-014_2.log"
    positions = [text.index(f"=== {name} ===") for name in STANDARD_SECTIONS]
    assert positions == sorted(positions)
    assert text.endswith("=== END_PATCHSHUTTLE_AI_HANDOFF ===\n")
    assert "redaction: BEST_EFFORT_ENABLED" in text
    assert "redaction_guarantee: NONE" in text
    assert "log-secret" not in text
    assert "description: token=[REDACTED]" in text
    assert "result: PATCH_ID_CONFLICT" in text
    assert "failure_stage: JOB" in text
    assert "next_expected_response: corrected_patch_or_audit" in text
    assert latest_log_path(workspace) == second


def test_disabled_redaction_is_declared_and_does_not_modify_text(
    workspace: Workspace,
    job: Job,
) -> None:
    unredacted = configured_workspace(workspace, redact_known_secrets=False)
    archive = workspace.root / "patches/applied/source.psh.yaml"
    archive.write_bytes(b"source\n")
    path = write_run_log(
        RunLogData(
            workspace=unredacted,
            job=job.model_copy(update={"description": "token=visible-value"}),
            job_hash="b" * 64,
            clock=RunClock(INSTANT),
            result="NO_CHANGE",
            exit_code=0,
            failure_stage=None,
            failure_code=None,
            archived_job_path=archive,
        )
    )
    text = path.read_text("utf-8")

    assert "redaction: DISABLED_BY_LOCAL_POLICY" in text
    assert "token=visible-value" in text
    assert "next_recommended_step: review_log_and_continue" in text


def test_latest_log_reports_empty_directory_and_ignores_unsafe_entries(
    workspace: Workspace,
) -> None:
    with pytest.raises(ExecutionError) as empty:
        latest_log_path(workspace)
    assert empty.value.code is ExecutionErrorCode.LOG_NOT_FOUND

    unrelated = workspace.root / "patches/logs/notes.txt"
    unrelated.write_text("ignore\n", encoding="utf-8")
    directory = workspace.root / "patches/logs/log_directory.log"
    directory.mkdir()
    with pytest.raises(ExecutionError) as still_empty:
        latest_log_path(workspace)
    assert still_empty.value.code is ExecutionErrorCode.LOG_NOT_FOUND


def test_artifact_directory_and_write_failures_are_stable_and_clean_partial(
    workspace: Workspace,
    job: Job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied = workspace.root / "patches/applied"
    applied.rmdir()
    applied.write_text("unsafe\n", encoding="utf-8")
    with pytest.raises(ExecutionError, match="missing or unsafe"):
        archive_job_source(
            workspace,
            job=job,
            job_hash="a" * 64,
            clock=RunClock(INSTANT),
            source=b"source\n",
            successful=True,
        )

    target = workspace.root / "patches/failed"
    monkeypatch.setattr(
        logging_module.os,
        "fsync",
        lambda *args: (_ for _ in ()).throw(OSError("injected")),
    )
    with pytest.raises(ExecutionError, match="could not be written"):
        archive_job_source(
            workspace,
            job=job,
            job_hash="a" * 64,
            clock=RunClock(INSTANT),
            source=b"source\n",
            successful=False,
        )
    assert list(target.iterdir()) == []


def test_log_records_failed_action_formatted_check_split_and_external_reference(
    workspace: Workspace,
    job: Job,
) -> None:
    plan = plan_job(job, workspace)
    check = CheckResult(
        id="check_001",
        name="import_check",
        status=CheckStatus.PASSED,
        argv=("python",),
        working_directory=workspace.root,
        timeout_seconds=30,
        return_code=0,
        duration_ms=1,
        stdout="ok",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
    )
    formatter = FormatterResult(
        id="formatter_001",
        name="isort",
        status=FormatterStatus.FAILED,
        argv=("python", "-m", "isort"),
        working_directory=workspace.root,
        timeout_seconds=30,
        return_code=1,
        duration_ms=2,
        stdout="",
        stderr="failed",
        stdout_truncated=False,
        stderr_truncated=False,
    )
    error = ExecutionError(
        ExecutionErrorCode.FORMAT_FAILED,
        "format failed",
        item_id="action_001",
        path="isort",
        check_results=(check, check),
        formatting_results=(formatter,),
    )
    path = write_run_log(
        RunLogData(
            workspace=workspace,
            job=job,
            job_hash="c" * 64,
            clock=RunClock(INSTANT),
            result="FORMAT_FAILED",
            exit_code=7,
            failure_stage="FORMAT_ISORT",
            failure_code="FORMAT_FAILED",
            archived_job_path=Path("/outside/source.psh.yaml"),
            plan=plan,
            error=error,
        )
    )
    text = path.read_text("utf-8")

    assert "archived_job_copy: /outside/source.psh.yaml" in text
    assert "action_id: action_001\n" in text
    assert "status: FAILED\n" in text
    assert "=== FORMAT_ISORT ===\nformatter_id: formatter_001" in text
    assert text.count("check_id: check_001") == 2


def test_latest_log_inspection_and_both_new_file_failure_paths(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as scoped:
        scoped.setattr(
            Path,
            "iterdir",
            lambda self: (_ for _ in ()).throw(OSError("injected")),
        )
        with pytest.raises(ExecutionError, match="could not be inspected"):
            latest_log_path(workspace)

    existing = workspace.root / "patches/logs/existing.log"
    existing.write_bytes(b"existing\n")
    with pytest.raises(ExecutionError, match="could not be written"):
        logging_module._write_new_file(existing, b"replacement\n")
    assert existing.read_bytes() == b"existing\n"

    partial = workspace.root / "patches/logs/partial.log"
    with monkeypatch.context() as scoped:
        scoped.setattr(
            logging_module.os,
            "fsync",
            lambda *args: (_ for _ in ()).throw(OSError("injected")),
        )
        scoped.setattr(
            Path,
            "unlink",
            lambda self: (_ for _ in ()).throw(OSError("cleanup injected")),
        )
        with pytest.raises(ExecutionError, match="could not be written"):
            logging_module._write_new_file(partial, b"partial\n")
    assert partial.read_bytes() == b"partial\n"
    partial.unlink()

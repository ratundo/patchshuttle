"""Integration contracts for automatic structured history persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import patchshuttle.history.storage as history_storage
import patchshuttle.workspace as workspace_module
from patchshuttle.cli import main
from patchshuttle.errors import ExecutionError
from patchshuttle.execution import RunStatus, execute_plan
from patchshuttle.history import HistoryWriteResult, read_history_record
from patchshuttle.models import Job
from patchshuttle.operations import rollback_job
from patchshuttle.planner import plan_job
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(workspace_module, "generate_project_id", lambda: PROJECT_ID)
    return init_workspace(tmp_path).workspace


def _patch_job(job_id: str, actions: list[dict], *, description: str) -> Job:
    return Job(
        protocol=1,
        project_id=PROJECT_ID,
        id=job_id,
        kind="patch",
        title="Exercise structured history",
        description=description,
        actions=actions,
        checks=[
            {"import_check": {"modules": ["json"]}},
            {"import_check": {"modules": ["pathlib"]}},
        ],
    )


def test_successful_execution_persists_observed_facts_and_declared_intent(
    workspace: Workspace,
) -> None:
    job = _patch_job(
        "PATCH-HISTORY-SUCCESS",
        [{"create_file": {"path": "notes.txt", "content": "history\n"}}],
        description="Create a small file for the integration contract.",
    )

    result = execute_plan(plan_job(job, workspace), approved=True)

    assert result.status is RunStatus.COMPLETED
    assert result.history_path is not None
    assert result.history_warning is None
    reference = f"{job.id}/{result.history_path.stem}"
    record = read_history_record(workspace, reference)
    assert record.declared.intent is not None
    assert record.declared.intent.text == job.description
    assert record.observed.status == "COMPLETED"
    assert record.observed.files.created == ("notes.txt",)
    assert [(item.phase, item.status) for item in record.observed.checks] == [
        ("initial", "PASSED"),
        ("initial", "PASSED"),
    ]


def test_failed_execution_persists_failure_and_automatic_rollback(
    workspace: Workspace,
) -> None:
    target = workspace.root / "demo.py"
    target.write_text("VALUE = 1\n", encoding="utf-8", newline="")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-HISTORY-FAILED",
        kind="patch",
        actions=[
            {
                "replace_exact": {
                    "path": "demo.py",
                    "old": "VALUE = 1",
                    "new": 'raise RuntimeError("history failure")',
                }
            }
        ],
        checks=[{"import_check": {"modules": ["demo"]}}],
    )

    with pytest.raises(ExecutionError) as caught:
        execute_plan(plan_job(job, workspace), approved=True)

    error = caught.value
    assert error.rollback_succeeded is True
    assert error.history_path is not None
    assert error.history_warning is None
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    record = read_history_record(
        workspace,
        f"{job.id}/{error.history_path.stem}",
    )
    assert record.observed.status == "ROLLED_BACK"
    assert record.observed.failure is not None
    assert record.observed.failure.cause_code == "CHECK_FAILED"
    assert record.observed.rollback.status == "SUCCESS"


def test_manual_rollback_creates_a_separate_history_attempt(
    workspace: Workspace,
) -> None:
    job = _patch_job(
        "PATCH-HISTORY-MANUAL",
        [{"create_file": {"path": "manual.txt", "content": "created\n"}}],
        description="Create a file that will be manually rolled back.",
    )
    completed = execute_plan(plan_job(job, workspace), approved=True)

    rolled_back = rollback_job(workspace, job.id, approved=True)

    assert completed.history_path is not None
    assert rolled_back.history_path is not None
    assert rolled_back.history_warning is None
    record = read_history_record(
        workspace,
        f"{job.id}/{rolled_back.history_path.stem}",
    )
    assert record.observed.status == "ROLLED_BACK"
    assert record.observed.rollback.status == "SUCCESS"
    assert record.observed.rollback.cause == "USER_REQUESTED"
    assert record.observed.files.deleted == ("manual.txt",)


def test_history_write_failure_does_not_change_a_successful_job_outcome(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _patch_job(
        "PATCH-HISTORY-WRITE-FAILURE",
        [{"create_file": {"path": "kept.txt", "content": "kept\n"}}],
        description="Exercise the non-fatal secondary-artifact policy.",
    )
    monkeypatch.setattr(
        "patchshuttle.execution.try_write_history_record",
        lambda *args, **kwargs: HistoryWriteResult(
            path=None,
            warning="injected history write failure",
        ),
    )

    result = execute_plan(plan_job(job, workspace), approved=True)

    assert result.status is RunStatus.COMPLETED
    assert result.history_path is None
    assert result.history_warning == "injected history write failure"
    assert (workspace.root / "kept.txt").read_text(encoding="utf-8") == "kept\n"


def test_history_cli_lists_and_reads_without_parsing_execution_logs(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _patch_job(
        "PATCH-HISTORY-CLI",
        [{"create_file": {"path": "cli.txt", "content": "cli\n"}}],
        description="Create one CLI-readable history record.",
    )
    result = execute_plan(plan_job(job, workspace), approved=True)
    assert result.history_path is not None
    monkeypatch.chdir(workspace.root)
    runner = CliRunner()

    listed = runner.invoke(main, ["history", "list", "--limit", "1"])
    latest = runner.invoke(main, ["history", "latest", job.id])
    shown = runner.invoke(
        main,
        ["history", "show", f"{job.id}/{result.history_path.stem}"],
    )
    missing = runner.invoke(main, ["history", "show", f"{job.id}/missing"])
    invalid_list = runner.invoke(
        main,
        ["history", "list", "--job-id", "invalid"],
    )
    missing_latest = runner.invoke(
        main,
        ["history", "latest", "PATCH-HISTORY-MISSING"],
    )

    assert listed.exit_code == 0
    assert listed.stdout.startswith("PATCHSHUTTLE_HISTORY_LIST\n")
    assert f'"job_id":"{job.id}"' in listed.stdout
    assert json.loads(latest.stdout)["job"]["id"] == job.id
    assert json.loads(shown.stdout)["record_id"].startswith(f"{job.id}/")
    assert missing.exit_code == 3
    assert missing.stderr.startswith("HISTORY_FAILED [HISTORY_NOT_FOUND]")
    assert invalid_list.exit_code == 3
    assert invalid_list.stderr.startswith("HISTORY_FAILED [HISTORY_INVALID]")
    assert missing_latest.exit_code == 3
    assert missing_latest.stderr.startswith("HISTORY_FAILED [HISTORY_NOT_FOUND]")


def test_real_storage_failure_is_still_best_effort(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _patch_job(
        "PATCH-HISTORY-STORAGE-FAILURE",
        [{"create_file": {"path": "storage.txt", "content": "kept\n"}}],
        description="Exercise a concrete storage exception.",
    )

    def fail_write(*args, **kwargs):
        raise OSError("injected storage failure")

    monkeypatch.setattr(history_storage, "write_history_record", fail_write)
    result = execute_plan(plan_job(job, workspace), approved=True)

    assert result.status is RunStatus.COMPLETED
    assert result.history_path is None
    assert result.history_warning is not None
    assert "injected storage failure" in result.history_warning

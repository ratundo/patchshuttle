"""Contract tests for controlled changed-Python-file formatting."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.formatters.runner as formatters_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle._process import ProcessResult, ProcessStatus
from patchshuttle.formatters import (
    FormatterStatus,
    capture_formatted_files,
    prepare_formatters,
    run_formatters,
    verify_formatted_files,
)
from patchshuttle.planner import Plan, plan_job
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    return init_workspace(tmp_path).workspace


def formatting_plan(workspace: Workspace, *, path: str = "src/module.py") -> Plan:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-011",
        kind="patch",
        actions=[
            {
                "create_file": {
                    "path": path,
                    "content": "import sys\nimport os\n\nVALUES=[1,2,3]\n",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    return plan_job(job, workspace)


def completed_process(
    status: ProcessStatus = ProcessStatus.PASSED,
    *,
    return_code: int | None = 0,
) -> ProcessResult:
    return ProcessResult(
        status=status,
        return_code=return_code,
        duration_ms=7,
        stdout="formatter stdout\n",
        stderr="formatter stderr\n",
        stdout_truncated=False,
        stderr_truncated=False,
    )


def test_prepare_formatters_builds_fixed_scoped_commands(
    workspace: Workspace,
) -> None:
    execution = workspace.config.execution.model_copy(
        update={"default_timeout_seconds": 41}
    )
    workspace = replace(
        workspace,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    plan = formatting_plan(workspace)

    prepared = prepare_formatters(plan)

    assert [item.id for item in prepared] == ["formatter_001", "formatter_002"]
    assert [item.name for item in prepared] == ["isort", "black"]
    assert all(item.working_directory == workspace.root for item in prepared)
    assert all(item.timeout_seconds == 41 for item in prepared)
    assert prepared[0].argv == (
        sys.executable,
        "-I",
        "-m",
        "isort",
        "--overwrite-in-place",
        "--",
        "src/module.py",
    )
    assert prepared[1].argv == (
        sys.executable,
        "-I",
        "-m",
        "black",
        "--",
        "src/module.py",
    )


def test_run_formatters_executes_in_order_and_maps_results(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = formatting_plan(workspace)
    observed = []

    def fake_run(command, *, maximum_output_bytes):
        observed.append((command, maximum_output_bytes))
        return completed_process()

    monkeypatch.setattr(formatters_module, "run_process", fake_run)

    run = run_formatters(plan)

    assert run.success is True
    assert run.failed is None
    assert [result.name for result in run.results] == ["isort", "black"]
    assert [result.status for result in run.results] == [
        FormatterStatus.PASSED,
        FormatterStatus.PASSED,
    ]
    assert [item[0].argv for item in observed] == [
        prepared.argv for prepared in prepare_formatters(plan)
    ]
    assert all(
        maximum == workspace.config.execution.max_command_output_bytes
        for _, maximum in observed
    )
    assert run.results[0].stdout == "formatter stdout\n"
    assert run.results[0].stderr == "formatter stderr\n"
    assert completed_process().success is True
    assert completed_process(ProcessStatus.FAILED, return_code=1).success is False
    with pytest.raises(FrozenInstanceError):
        run.results[0].status = FormatterStatus.FAILED  # type: ignore[misc]


@pytest.mark.parametrize(
    ("process_status", "formatter_status", "return_code"),
    (
        (ProcessStatus.FAILED, FormatterStatus.FAILED, 3),
        (ProcessStatus.TIMED_OUT, FormatterStatus.TIMED_OUT, None),
        (ProcessStatus.ERROR, FormatterStatus.ERROR, None),
    ),
)
def test_run_formatters_stops_after_first_failure(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    process_status: ProcessStatus,
    formatter_status: FormatterStatus,
    return_code: int | None,
) -> None:
    plan = formatting_plan(workspace)
    calls = 0

    def failed_run(command, *, maximum_output_bytes):
        nonlocal calls
        calls += 1
        return completed_process(process_status, return_code=return_code)

    monkeypatch.setattr(formatters_module, "run_process", failed_run)

    run = run_formatters(plan)

    assert calls == 1
    assert run.success is False
    assert run.failed is run.results[0]
    assert run.failed.status is formatter_status
    assert run.failed.return_code == return_code


def test_prepare_formatters_rejects_forged_scope_and_order(
    workspace: Workspace,
) -> None:
    plan = formatting_plan(workspace)

    with pytest.raises(ValueError, match="formatting targets"):
        prepare_formatters(replace(plan, formatting_targets=()))

    formatting = workspace.config.formatting.model_copy(
        update={"order": ("black", "isort")}
    )
    forged_workspace = replace(
        workspace,
        config=workspace.config.model_copy(update={"formatting": formatting}),
    )
    forged_plan = formatting_plan(forged_workspace)
    with pytest.raises(ValueError, match="formatter order"):
        prepare_formatters(forged_plan)


def test_non_python_plan_has_no_formatter_commands(workspace: Workspace) -> None:
    plan = formatting_plan(workspace, path="notes.txt")

    run = run_formatters(plan)

    assert prepare_formatters(plan) == ()
    assert run.results == ()
    assert run.success is True
    assert run.failed is None


def test_capture_and_verify_formatted_file_state(workspace: Workspace) -> None:
    target = workspace.root / "module.py"
    target.write_text("VALUE=1\n", encoding="utf-8")
    target.chmod(0o744)
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-011",
        kind="patch",
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE=1",
                    "new": "VALUE=2",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    plan = plan_job(job, workspace)
    target.write_bytes(plan.file_changes[0].content)

    captured = capture_formatted_files(plan)

    assert len(captured) == 1
    assert captured[0].path == PurePosixPath("module.py")
    assert captured[0].size == len(b"VALUE=2\n")
    assert captured[0].sha256 == hashlib.sha256(b"VALUE=2\n").hexdigest()
    assert captured[0].mode == 0o744
    verify_formatted_files(plan, captured)

    target.write_text("VALUE = 3\n", encoding="utf-8")
    with pytest.raises(OSError, match="changed after formatting"):
        verify_formatted_files(plan, captured)


def test_capture_rejects_missing_or_oversized_formatter_output(
    workspace: Workspace,
) -> None:
    plan = formatting_plan(workspace, path="module.py")
    execution = workspace.config.execution.model_copy(
        update={"max_single_file_bytes": 16}
    )
    workspace = replace(
        workspace,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    plan = replace(plan, workspace=workspace)

    with pytest.raises(OSError, match="not a regular file"):
        capture_formatted_files(plan)

    target = workspace.root / "module.py"
    target.write_bytes(b"x" * 17)
    with pytest.raises(OSError, match="size limit"):
        capture_formatted_files(plan)


def test_verify_rejects_forged_snapshot_scope(workspace: Workspace) -> None:
    plan = formatting_plan(workspace)

    with pytest.raises(ValueError, match="snapshot scope"):
        verify_formatted_files(plan, ())


def test_capture_rejects_target_type_race(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = formatting_plan(workspace, path="module.py")
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    original_read = Path.read_bytes

    def replaced_after_read(self: Path) -> bytes:
        raw = original_read(self)
        if self == target:
            self.unlink()
            self.mkdir()
        return raw

    monkeypatch.setattr(Path, "read_bytes", replaced_after_read)

    with pytest.raises(OSError, match="not a regular file"):
        capture_formatted_files(plan)


def test_capture_rejects_content_race(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = formatting_plan(workspace, path="module.py")
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    original_read = Path.read_bytes

    def changed_after_read(self: Path) -> bytes:
        raw = original_read(self)
        if self == target:
            self.write_bytes(raw + b"# changed\n")
        return raw

    monkeypatch.setattr(Path, "read_bytes", changed_after_read)

    with pytest.raises(OSError, match="changed while"):
        capture_formatted_files(plan)

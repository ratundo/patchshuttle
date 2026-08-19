"""Tests for opt-in changed-template linting and transactional rollback."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.backup as backup_module
import patchshuttle.linters.runner as linters_module
import patchshuttle.planner as planner_module
import patchshuttle.preflight as preflight_module
import patchshuttle.runner as runner_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job, RunStatus, execute_plan
from patchshuttle._html_lint import djlint_argv
from patchshuttle._process import ProcessResult, ProcessStatus
from patchshuttle.config import HtmlLintSettings
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.linters import (
    HtmlLintStatus,
    prepare_html_linter,
    run_html_linter,
)
from patchshuttle.planner import plan_job
from patchshuttle.runner import TransactionStatus, execute_change_transaction
from patchshuttle.workspace import Workspace, init_workspace, load_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
RUN_TIMESTAMP = "2026_08_18_120000_000001"


def process_result(status: ProcessStatus) -> ProcessResult:
    return ProcessResult(
        status=status,
        return_code=(0 if status is ProcessStatus.PASSED else 1),
        duration_ms=4,
        stdout="lint stdout\n",
        stderr="" if status is ProcessStatus.PASSED else "lint stderr\n",
        stdout_truncated=False,
        stderr_truncated=False,
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    monkeypatch.setattr(backup_module, "_run_timestamp", lambda: RUN_TIMESTAMP)
    original_find_spec = planner_module.find_spec
    monkeypatch.setattr(
        planner_module,
        "find_spec",
        lambda name: object() if name == "djlint" else original_find_spec(name),
    )
    monkeypatch.setattr(
        preflight_module,
        "run_process",
        lambda *args, **kwargs: process_result(ProcessStatus.PASSED),
    )
    created = init_workspace(tmp_path).workspace
    created.config_path.write_text(
        created.config_path.read_text("utf-8")
        .replace("enabled = false", "enabled = true", 1)
        .replace('profile = "html"', 'profile = "django"', 1)
        .replace("ignore = []", 'ignore = ["H006"]', 1),
        encoding="utf-8",
    )
    return load_workspace(tmp_path)


def patch_plan(workspace: Workspace, *, two_files: bool = False):
    actions = [
        {
            "create_file": {
                "path": "templates/one.html",
                "content": "<main>one</main>\n",
            }
        }
    ]
    if two_files:
        actions.append(
            {
                "create_file": {
                    "path": "templates/two.HTML",
                    "content": "<main>two</main>\n",
                }
            }
        )
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-HTML-001",
        kind="patch",
        actions=actions,
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    return plan_job(job, workspace)


def manifest(workspace: Workspace) -> dict:
    path = (
        workspace.root
        / "patches/backups/PATCH-HTML-001"
        / RUN_TIMESTAMP
        / "manifest.json"
    )
    return json.loads(path.read_text("utf-8"))


def test_prepare_html_linter_uses_fixed_lint_only_commands(
    workspace: Workspace,
) -> None:
    plan = patch_plan(workspace, two_files=True)

    prepared = prepare_html_linter(plan)

    assert plan.html_lint_targets == (
        PurePosixPath("templates/one.html"),
        PurePosixPath("templates/two.HTML"),
    )
    assert [item.id for item in prepared] == ["html_lint_001", "html_lint_002"]
    assert prepared[0].name == "djlint"
    assert prepared[0].content == b"<main>one</main>\n"
    assert prepared[0].argv[4] == "-"
    assert prepared[0].argv[-4:-2] == ("--stdin-filename", "templates/one.html")
    assert prepared[0].argv[-2:] == ("--ignore", "H006")
    assert "--lint" in prepared[0].argv
    assert "--reformat" not in prepared[0].argv


def test_prepare_html_linter_rejects_a_forged_scope(workspace: Workspace) -> None:
    plan = patch_plan(workspace)

    with pytest.raises(ValueError, match="targets"):
        prepare_html_linter(replace(plan, html_lint_targets=()))


def test_disabled_html_linter_has_no_prepared_commands(workspace: Workspace) -> None:
    plan = patch_plan(workspace)
    disabled_html = workspace.config.linting.html.model_copy(update={"enabled": False})
    disabled_linting = workspace.config.linting.model_copy(
        update={"html": disabled_html}
    )
    disabled_workspace = replace(
        workspace,
        config=workspace.config.model_copy(update={"linting": disabled_linting}),
    )
    disabled_plan = replace(plan, workspace=disabled_workspace, html_lint_targets=())

    assert prepare_html_linter(disabled_plan) == ()
    assert run_html_linter(disabled_plan).results == ()


def test_command_without_ignore_has_no_ignore_argument() -> None:
    argv = djlint_argv(HtmlLintSettings(enabled=True), "template.html")

    assert "--ignore" not in argv
    assert argv[-1] == "--lint"


def test_run_html_linter_stops_after_first_failure(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = patch_plan(workspace, two_files=True)
    outcomes = iter(
        (process_result(ProcessStatus.PASSED), process_result(ProcessStatus.FAILED))
    )
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return next(outcomes)

    monkeypatch.setattr(linters_module, "run_process", fake_run)

    result = run_html_linter(plan)

    assert calls == 2
    assert result.success is False
    assert result.failed is result.results[1]
    assert [item.status for item in result.results] == [
        HtmlLintStatus.PASSED,
        HtmlLintStatus.FAILED,
    ]
    assert result.results[0].success is True
    assert result.results[1].success is False
    assert result.results[1].stderr == "lint stderr\n"


def test_successful_html_lint_is_exposed_logged_and_manifested(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = patch_plan(workspace)
    monkeypatch.setattr(
        linters_module,
        "run_process",
        lambda *args, **kwargs: process_result(ProcessStatus.PASSED),
    )

    result = execute_plan(plan, approved=True)

    assert result.status is RunStatus.COMPLETED
    assert [item.status for item in result.html_lint_results] == [HtmlLintStatus.PASSED]
    assert result.log_path is not None
    log = result.log_path.read_text("utf-8")
    assert "=== LINT_HTML ===\n" in log
    assert "linter: djlint\n" in log
    assert "html_lint_status: PASSED\n" in log
    assert manifest(workspace)["html_lint_targets"] == ["templates/one.html"]


@pytest.mark.parametrize(
    ("status", "message"),
    (
        (ProcessStatus.FAILED, "reported template lint errors"),
        (ProcessStatus.TIMED_OUT, "timed out"),
        (ProcessStatus.ERROR, "could not be started"),
    ),
)
def test_html_lint_failure_rolls_back_and_keeps_diagnostics(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    status: ProcessStatus,
    message: str,
) -> None:
    plan = patch_plan(workspace)
    monkeypatch.setattr(
        linters_module,
        "run_process",
        lambda *args, **kwargs: process_result(status),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_plan(plan, approved=True)

    error = caught.value
    assert error.code is ExecutionErrorCode.HTML_LINT_FAILED
    assert message in error.message
    assert error.rollback_succeeded is True
    assert error.html_lint_results[0].status.value == status.value
    assert not (workspace.root / "templates/one.html").exists()
    assert error.log_path is not None
    log = error.log_path.read_text("utf-8")
    assert "failure_stage: LINT_HTML\n" in log
    assert "exit_code: 6\n" in log
    assert "html_lint_status: FAILED\n" in log
    assert manifest(workspace)["failure_code"] == "HTML_LINT_FAILED"


def test_html_linter_preparation_error_rolls_back(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = patch_plan(workspace)
    monkeypatch.setattr(
        runner_module,
        "run_html_linter",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("forged")),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.HTML_LINT_FAILED
    assert caught.value.item_id == "html_lint"
    assert caught.value.html_lint_results == ()
    assert caught.value.rollback_succeeded is True


def test_html_linter_cannot_mutate_a_transaction_file(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = patch_plan(workspace)

    def mutate_and_pass(*args, **kwargs):
        (workspace.root / "templates/one.html").write_text(
            "mutated\n",
            encoding="utf-8",
        )
        return process_result(ProcessStatus.PASSED)

    monkeypatch.setattr(linters_module, "run_process", mutate_and_pass)

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.HTML_LINT_FAILED
    assert "changed a declared transaction file" in caught.value.message
    assert caught.value.rollback_succeeded is True
    assert len(caught.value.html_lint_results) == 1
    assert not (workspace.root / "templates/one.html").exists()


def test_transaction_result_exposes_successful_html_lint(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = patch_plan(workspace)
    monkeypatch.setattr(
        linters_module,
        "run_process",
        lambda *args, **kwargs: process_result(ProcessStatus.PASSED),
    )

    result = execute_change_transaction(plan, approved=True)

    assert result.status is TransactionStatus.APPLIED
    assert result.html_lint_results[0].path == PurePosixPath("templates/one.html")


@pytest.mark.skipif(
    importlib.util.find_spec("djlint") is None,
    reason="install the optional html extra to run the djLint integration",
)
def test_real_runtime_lint_ignores_project_attempt_to_suppress_a_rule(
    workspace: Workspace,
) -> None:
    workspace.root.joinpath("pyproject.toml").write_text(
        '[tool.djlint]\nignore = "H025"\n',
        encoding="utf-8",
    )
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-HTML-001",
        kind="patch",
        actions=[
            {
                "create_file": {
                    "path": "templates/one.html",
                    "content": "<div><span>bad</div>\n",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    plan = plan_job(job, workspace)

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.HTML_LINT_FAILED
    assert "H025" in caught.value.html_lint_results[0].stdout
    assert caught.value.rollback_succeeded is True

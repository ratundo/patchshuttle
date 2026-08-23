"""Tests for formatter and HTML-lint read-only planning preflight."""

from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import replace
from pathlib import Path, PurePosixPath

import isort
import pytest

import patchshuttle.preflight as preflight_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle._process import ProcessResult, ProcessStatus
from patchshuttle.errors import PlanningError, PlanningErrorCode
from patchshuttle.formatter_policy import (
    BLACK_POLICY_OPTIONS,
    FormatterCompatibility,
    FormatterDecision,
)
from patchshuttle.planner import (
    FileDisposition,
    NewlineStyle,
    PlannedFileChange,
    plan_job,
)
from patchshuttle.preflight import detect_python_encoding, run_quality_preflight
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


def python_job(content: str, *, encoding: str = "utf-8") -> Job:
    return Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-PREFLIGHT-001",
        kind="patch",
        actions=[
            {
                "create_file": {
                    "path": "module.py",
                    "content": content,
                    "encoding": encoding,
                }
            }
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )


def html_workspace(workspace: Workspace) -> Workspace:
    html = workspace.config.linting.html.model_copy(
        update={
            "enabled": True,
            "profile": "django",
            "ignore": ("H006", "H013"),
        }
    )
    linting = workspace.config.linting.model_copy(update={"html": html})
    return replace(
        workspace,
        config=workspace.config.model_copy(update={"linting": linting}),
    )


def formatter_workspace(
    workspace: Workspace,
    *,
    isort_exclude: tuple[str, ...] = (),
    black_exclude: tuple[str, ...] = (),
) -> Workspace:
    formatting = workspace.config.formatting.model_copy(
        update={
            "isort_exclude": isort_exclude,
            "black_exclude": black_exclude,
        }
    )
    return replace(
        workspace,
        config=workspace.config.model_copy(update={"formatting": formatting}),
    )


def html_change(content: bytes = b"<main>ok</main>\n") -> PlannedFileChange:
    return PlannedFileChange(
        path=PurePosixPath("templates/page.html"),
        disposition=FileDisposition.CREATE,
        before_sha256=None,
        after_sha256=hashlib.sha256(content).hexdigest(),
        before_size=None,
        after_size=len(content),
        encoding="utf-8",
        newline=NewlineStyle.LF,
        before_content=None,
        content=content,
    )


def process_result(
    status: ProcessStatus,
    *,
    stdout: str = "",
    stderr: str = "",
) -> ProcessResult:
    return ProcessResult(
        status=status,
        return_code=0 if status is ProcessStatus.PASSED else 1,
        duration_ms=3,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=False,
        stderr_truncated=False,
    )


def test_python_preflight_accepts_pep263_source_and_records_tools(
    workspace: Workspace,
) -> None:
    content = "# coding: latin-1\n\nNAME = 'café'\n"

    plan = plan_job(python_job(content, encoding="latin-1"), workspace)

    assert [item.tool for item in plan.preflight_checks] == [
        "python_encoding",
        "isort",
        "black",
    ]
    assert plan.preflight_checks[0].detail == "planned iso-8859-1"
    assert plan.file_changes[0].content == content.encode("latin-1")


def test_python_encoding_error_is_bound_to_the_action(workspace: Workspace) -> None:
    (workspace.root / "module.py").write_bytes(b"# coding: utf-8\nVALUE = '\xff'\n")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-PREFLIGHT-002",
        kind="patch",
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE",
                    "new": "RESULT",
                }
            }
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.FILE_ENCODING_UNSUPPORTED
    assert caught.value.item_id == "action_001"
    assert caught.value.path == "module.py"
    assert caught.value.details[0].startswith("  encoding_error:")


def test_isort_preflight_failure_is_actionable(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        isort,
        "code",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("x" * 700)),
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(python_job("VALUE = 1\n"), workspace)

    assert caught.value.code is PlanningErrorCode.FORMATTER_PATCH_INCOMPATIBLE
    assert caught.value.item_id == "formatting"
    assert caught.value.path == "module.py"
    assert caught.value.details[:4] == (
        "  formatter: isort",
        "  baseline: NOT_APPLICABLE",
        "  baseline_detail: file did not exist before the planned change",
        "  planned: INCOMPATIBLE",
    )
    assert caught.value.details[4].startswith("  planned_detail: ValueError:")
    assert caught.value.details[4].endswith("...")


def test_black_preflight_failure_is_actionable(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight_module,
        "run_process",
        lambda *args, **kwargs: process_result(
            ProcessStatus.ERROR,
            stderr="Black could not parse module.py",
        ),
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(python_job("VALUE = 1\n"), workspace)

    assert caught.value.code is PlanningErrorCode.FORMATTER_PATCH_INCOMPATIBLE
    assert caught.value.details == (
        "  formatter: black",
        "  baseline: NOT_APPLICABLE",
        "  baseline_detail: file did not exist before the planned change",
        "  planned: INCOMPATIBLE",
        "  planned_detail: ValueError: output: Black could not parse module.py",
    )


def test_black_preflight_uses_runtime_policy_and_accepts_reformat_exit(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    def fake_run(command, *, maximum_output_bytes):
        observed["command"] = command
        observed["maximum"] = maximum_output_bytes
        return process_result(ProcessStatus.FAILED)

    monkeypatch.setattr(preflight_module, "run_process", fake_run)

    plan = plan_job(python_job("VALUE=1\n"), workspace)

    command = observed["command"]
    assert command.argv == (
        preflight_module.sys.executable,
        "-I",
        "-m",
        "black",
        *BLACK_POLICY_OPTIONS,
        "--quiet",
        "--check",
        "--stdin-filename",
        "module.py",
        "-",
    )
    assert command.stdin == b"VALUE=1\n"
    assert command.working_directory == workspace.root
    assert observed["maximum"] == workspace.config.execution.max_command_output_bytes
    assert plan.formatter_plan[-1].planned is FormatterCompatibility.PASS


def test_formatter_matrix_distinguishes_repaired_legacy_baseline(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "module.py").write_text("VALUE = (\n", encoding="utf-8")
    responses = iter(
        (
            process_result(ProcessStatus.ERROR, stderr="baseline parse failed"),
            process_result(ProcessStatus.PASSED),
        )
    )
    monkeypatch.setattr(
        preflight_module,
        "run_process",
        lambda *args, **kwargs: next(responses),
    )
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-PREFLIGHT-REPAIR",
        kind="patch",
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = (",
                    "new": "VALUE = 1",
                }
            }
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )

    plan = plan_job(job, workspace)

    black_target = plan.formatter_plan[-1]
    assert black_target.decision is FormatterDecision.RUN
    assert black_target.baseline is FormatterCompatibility.INCOMPATIBLE
    assert black_target.planned is FormatterCompatibility.PASS
    assert "baseline parse failed" in black_target.baseline_detail
    assert [item.detail for item in plan.preflight_checks[:2]] == [
        "baseline utf-8",
        "planned utf-8",
    ]


@pytest.mark.parametrize(
    ("baseline_status", "expected_code"),
    (
        (ProcessStatus.PASSED, PlanningErrorCode.FORMATTER_PATCH_INCOMPATIBLE),
        (ProcessStatus.ERROR, PlanningErrorCode.FORMATTER_BASELINE_INCOMPATIBLE),
    ),
)
def test_black_preflight_classifies_planned_failure_against_baseline(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    baseline_status: ProcessStatus,
    expected_code: PlanningErrorCode,
) -> None:
    (workspace.root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    responses = iter(
        (
            process_result(baseline_status, stderr="baseline failed"),
            process_result(ProcessStatus.ERROR, stderr="planned failed"),
        )
    )
    monkeypatch.setattr(
        preflight_module,
        "run_process",
        lambda *args, **kwargs: next(responses),
    )
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-PREFLIGHT-CLASSIFY",
        kind="patch",
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is expected_code
    assert caught.value.details[-1].endswith("planned failed")


def test_local_formatter_exclusion_skips_only_the_selected_tool(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = formatter_workspace(
        workspace,
        black_exclude=("module.py",),
    )
    monkeypatch.setattr(
        preflight_module,
        "run_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Black must not run for a locally excluded path")
        ),
    )

    plan = plan_job(python_job("VALUE = 1\n"), configured)

    assert [(item.formatter, item.decision) for item in plan.formatter_plan] == [
        ("isort", FormatterDecision.RUN),
        ("black", FormatterDecision.SKIP_LOCAL_POLICY),
    ]
    assert plan.formatter_plan[-1].baseline is FormatterCompatibility.NOT_CHECKED
    assert plan.formatter_plan[-1].planned is FormatterCompatibility.NOT_CHECKED
    assert plan.formatter_paths("isort") == (PurePosixPath("module.py"),)
    assert plan.formatter_paths("black") == ()


def test_all_local_formatter_exclusions_skip_tool_dependencies_and_decoding(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = formatter_workspace(
        workspace,
        isort_exclude=("module.py",),
        black_exclude=("module.py",),
    )
    monkeypatch.setattr(
        preflight_module,
        "_decode_attempt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("excluded source must not enter formatter preflight")
        ),
    )

    plan = plan_job(python_job("VALUE = 1\n"), configured)

    assert all(
        item.decision is FormatterDecision.SKIP_LOCAL_POLICY
        for item in plan.formatter_plan
    )
    assert plan.formatter_run_paths == ()
    assert plan.preflight_checks == ()


def test_detect_python_encoding_rejects_unknown_cookie() -> None:
    with pytest.raises(PlanningError) as caught:
        detect_python_encoding(
            b"# coding: made-up-codec\nVALUE = 1\n",
            path=PurePosixPath("module.py"),
        )

    assert caught.value.code is PlanningErrorCode.FILE_ENCODING_UNSUPPORTED


def test_quality_preflight_classifies_undecodable_planned_python_bytes(
    workspace: Workspace,
) -> None:
    content = b"# coding: utf-8\nVALUE = '\xff'\n"
    change = PlannedFileChange(
        path=PurePosixPath("module.py"),
        disposition=FileDisposition.CREATE,
        before_sha256=None,
        after_sha256=hashlib.sha256(content).hexdigest(),
        before_size=None,
        after_size=len(content),
        encoding="utf-8",
        newline=NewlineStyle.LF,
        before_content=None,
        content=content,
    )

    with pytest.raises(PlanningError) as caught:
        run_quality_preflight(
            workspace,
            (change,),
            formatting_targets=(change.path,),
            html_lint_targets=(),
        )

    assert caught.value.code is PlanningErrorCode.FORMATTER_PATCH_INCOMPATIBLE
    assert caught.value.details[1] == "  baseline: NOT_APPLICABLE"
    assert "encoding_error:" in caught.value.details[-1]


def test_quality_preflight_keeps_encoding_error_without_detail_record(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = PlannedFileChange(
        path=PurePosixPath("module.py"),
        disposition=FileDisposition.CREATE,
        before_sha256=None,
        after_sha256=hashlib.sha256(b"VALUE = 1\n").hexdigest(),
        before_size=None,
        after_size=10,
        encoding="utf-8",
        newline=NewlineStyle.LF,
        before_content=None,
        content=b"VALUE = 1\n",
    )
    monkeypatch.setattr(
        preflight_module,
        "_decode_python",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PlanningError(
                PlanningErrorCode.FILE_ENCODING_UNSUPPORTED,
                "encoding unavailable",
            )
        ),
    )

    with pytest.raises(PlanningError) as caught:
        run_quality_preflight(
            workspace,
            (change,),
            formatting_targets=(change.path,),
            html_lint_targets=(),
        )

    assert "FILE_ENCODING_UNSUPPORTED" in caught.value.details[-1]


def test_html_preflight_passes_final_bytes_by_stdin_and_records_profile(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = html_workspace(workspace)
    change = html_change()
    observed = {}

    def fake_run(command, *, maximum_output_bytes):
        observed["command"] = command
        observed["maximum"] = maximum_output_bytes
        return process_result(ProcessStatus.PASSED)

    monkeypatch.setattr(preflight_module, "run_process", fake_run)

    result = run_quality_preflight(
        configured,
        (change,),
        formatting_targets=(),
        html_lint_targets=(change.path,),
    )

    command = observed["command"]
    assert result.checks[0].tool == "djlint"
    assert result.checks[0].detail == "profile=django"
    assert command.stdin == change.content
    assert command.argv[1:4] == ("-I", "-m", "djlint")
    assert command.argv[-2:] == ("--ignore", "H006,H013")
    assert command.working_directory != configured.root
    assert command.working_directory.name.startswith("patchshuttle-djlint-")
    assert observed["maximum"] == configured.config.execution.max_command_output_bytes


@pytest.mark.parametrize(
    ("status", "message"),
    (
        (ProcessStatus.FAILED, "reported template lint errors"),
        (ProcessStatus.TIMED_OUT, "timed out"),
        (ProcessStatus.ERROR, "could not be started"),
    ),
)
def test_html_preflight_failure_has_bounded_diagnostics(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    status: ProcessStatus,
    message: str,
) -> None:
    configured = html_workspace(workspace)
    change = html_change()
    stdout = "\n".join(["x" * 300, *(f"line {index}" for index in range(20))])
    result = process_result(
        status, stdout=stdout if status is ProcessStatus.FAILED else ""
    )
    monkeypatch.setattr(preflight_module, "run_process", lambda *args, **kwargs: result)

    with pytest.raises(PlanningError) as caught:
        run_quality_preflight(
            configured,
            (change,),
            formatting_targets=(),
            html_lint_targets=(change.path,),
        )

    assert caught.value.code is PlanningErrorCode.HTML_LINT_FAILED
    assert message in caught.value.message
    if status is ProcessStatus.FAILED:
        assert len(caught.value.details) == 12
        assert caught.value.details[0].endswith("...")
    else:
        assert caught.value.details == ("  output: none",)


def test_html_preflight_includes_stderr_when_stdout_is_empty(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = html_workspace(workspace)
    change = html_change()
    monkeypatch.setattr(
        preflight_module,
        "run_process",
        lambda *args, **kwargs: process_result(
            ProcessStatus.ERROR,
            stderr="launch failed\r\nsecond line",
        ),
    )

    with pytest.raises(PlanningError) as caught:
        run_quality_preflight(
            configured,
            (change,),
            formatting_targets=(),
            html_lint_targets=(change.path,),
        )

    assert caught.value.details == (
        "  output: launch failed",
        "  output: second line",
    )


def test_html_preflight_maps_isolation_failure(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = html_workspace(workspace)
    change = html_change()

    class BrokenIsolation:
        def __enter__(self):
            raise OSError("temporary directory unavailable")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        preflight_module,
        "isolated_djlint_directory",
        lambda: BrokenIsolation(),
    )

    with pytest.raises(PlanningError) as caught:
        run_quality_preflight(
            configured,
            (change,),
            formatting_targets=(),
            html_lint_targets=(change.path,),
        )

    assert caught.value.code is PlanningErrorCode.HTML_LINT_FAILED
    assert caught.value.message == "isolated djLint preflight could not be prepared"
    assert "temporary directory unavailable" in caught.value.details[0]


@pytest.mark.skipif(
    importlib.util.find_spec("djlint") is None,
    reason="install the optional html extra to run the djLint integration",
)
def test_real_djlint_preflight_accepts_valid_html_and_rejects_invalid_html(
    workspace: Workspace,
) -> None:
    configured = html_workspace(workspace)
    configured.root.joinpath("pyproject.toml").write_text(
        '[tool.djlint]\nignore = "H025"\n',
        encoding="utf-8",
    )
    valid = html_change(
        b'<!doctype html>\n<html lang="en">\n'
        b"  <head>\n"
        b'    <meta name="description" content="Example">\n'
        b"    <title>Example</title>\n"
        b"  </head>\n"
        b"  <body>\n"
        b"    <main>ok</main>\n"
        b"  </body>\n"
        b"</html>\n"
    )

    result = run_quality_preflight(
        configured,
        (valid,),
        formatting_targets=(),
        html_lint_targets=(valid.path,),
    )

    assert result.checks[0].tool == "djlint"

    invalid = html_change(b"<div><span>bad</div>\n")
    with pytest.raises(PlanningError) as caught:
        run_quality_preflight(
            configured,
            (invalid,),
            formatting_targets=(),
            html_lint_targets=(invalid.path,),
        )

    assert caught.value.code is PlanningErrorCode.HTML_LINT_FAILED
    assert any("H025" in item for item in caught.value.details)

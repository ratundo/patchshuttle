"""Branch-coverage regressions for the A4 workflow additions."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path, PurePosixPath

import pytest
from click.testing import CliRunner

import patchshuttle._ai_log as ai_log_module
import patchshuttle.cli as cli_module
import patchshuttle.warning_baseline as warning_baseline_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job, execute_plan
from patchshuttle.actions import read_symbol, search_context
from patchshuttle.cli import main
from patchshuttle.config import load_config
from patchshuttle.errors import (
    ExecutionError,
    ExecutionErrorCode,
    PlanningError,
    PlanningErrorCode,
    PolicyError,
    PolicyErrorCode,
    WorkspaceError,
    WorkspaceErrorCode,
)
from patchshuttle.logging import render_latest_ai_log
from patchshuttle.models import Action
from patchshuttle.planner import plan_job
from patchshuttle.policy import Policy
from patchshuttle.project_python import (
    ProjectPythonError,
    project_python_for_job,
    resolve_project_python,
)
from patchshuttle.rollback import rollback_created
from patchshuttle.warning_baseline import (
    WARNING_BASELINE_RELATIVE_PATH,
    WARNING_BASELINE_SCHEMA,
    load_warning_baseline,
    update_warning_baseline,
)
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"


def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(workspace_module, "generate_project_id", lambda: PROJECT_ID)
    return init_workspace(tmp_path).workspace


def _job(
    workspace: Workspace,
    *,
    job_id: str,
    kind: str,
    actions: list[dict] | None = None,
    checks: list[dict] | None = None,
) -> Job:
    return Job(
        protocol=1,
        project_id=workspace.project_id,
        id=job_id,
        kind=kind,
        actions=actions or [],
        checks=checks or [],
    )


def test_ai_log_failure_records_formatters_and_fallback_branches() -> None:
    text = """\
preamble outside sections
=== PATCHSHUTTLE_ATTEMPT ===
command: run
=== ACTIONS ===
ignored metadata
---
action_id: action_001
action_type: create_file
status: FAILED
actual: FAILED
details: write failed
output_begin
failure output
output_end
---
action_id: action_002
action_type: create_file
status: COMPLETED
actual: COMPLETED
=== INITIAL_CHECKS ===
ignored metadata
---
check_id: check_001
profile: pytest
exit_code: 1
status: FAILED
stdout: initial stdout
stderr: initial stderr
stdout_truncated: false
stderr_truncated: false
=== FINAL_CHECKS ===
check_id: check_001
profile: pytest
exit_code: 0
status: PASSED
stdout: final stdout
stderr: final stderr
stdout_truncated: false
stderr_truncated: false
=== FORMAT_ISORT ===
formatter_id: formatter_001
formatter: isort
exit_code: 0
status: PASSED
stdout: formatter stdout
=== FORMAT_BLACK ===
formatter_id: formatter_002
formatter: black
exit_code: 1
status: FAILED
stderr: formatter stderr
=== SUMMARY ===
result: CHECK_FAILED
=== PATCHSHUTTLE_AI_HANDOFF ===
protocol: 1
result: CHECK_FAILED
"""

    payload = ai_log_module.summarize_ai_log(text, source="failure.log")

    assert "job" not in payload
    assert payload["actions"]["status_counts"] == {"FAILED": 1, "COMPLETED": 1}
    assert payload["actions"]["failed"][0]["output"] == "failure output"
    assert payload["checks"]["initial"][0]["stderr"] == "initial stderr"
    assert "stdout" not in payload["checks"]["final"][0]
    assert "stdout" not in payload["formatters"]["isort"]
    assert payload["formatters"]["black"]["stderr"] == "formatter stderr"


def test_ai_log_parser_and_renderer_edge_branches() -> None:
    assert ai_log_module._parse_sections("outside a section") == {}
    fields = ai_log_module._parse_fields(
        'ignored\nempty:\nnext: 1\nchange: {"path":"a.py"}\n'
        'change:\n  {"path":"b.py"}\n'
    )
    assert fields == {
        "empty": "",
        "next": 1,
        "change": [{"path": "a.py"}, {"path": "b.py"}],
    }

    payload: dict[str, object] = {}
    ai_log_module._include_selected(payload, "selected", "ignored", ("wanted",))
    ai_log_module._include_fields(payload, "fields", "ignored")
    assert payload == {}
    assert ai_log_module._decode("") == ""
    assert ai_log_module._clip("x" * (ai_log_module._OUTPUT_LIMIT + 1)).endswith(
        "[AI_LOG_OUTPUT_TRUNCATED]"
    )

    lines: list[str] = []
    ai_log_module._append_value(lines, [{}, 1], "")
    ai_log_module._append_value(lines, 2, "")
    assert lines == ["- {}", "- 1", "2"]
    assert ai_log_module._summarize_actions([{"status": "COMPLETED"}]) == {
        "total": 1,
        "status_counts": {"COMPLETED": 1},
    }


def test_optional_audit_constructor_fields_are_omitted() -> None:
    assert search_context("TODO") == Action({"search_context": {"text": "TODO"}})
    assert read_symbol("module.py", "run") == Action(
        {"read_symbol": {"path": "module.py", "symbol": "run"}}
    )


def test_search_context_handles_binary_limit_and_casefold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    source = workspace.root / "source"
    source.mkdir()
    (source / "00-binary.txt").write_bytes(b"\x00\x01")
    (source / "01-text.txt").write_text(
        "not a match\ntodo first\nTODO second\n",
        encoding="utf-8",
    )
    job = _job(
        workspace,
        job_id="AUDIT-A4-CONTEXT-EDGE",
        kind="audit",
        actions=[
            {
                "search_context": {
                    "path": "source",
                    "text": "TODO",
                    "glob": "*.txt",
                    "case_sensitive": False,
                    "max_results": 1,
                    "before": 0,
                    "after": 0,
                }
            }
        ],
    )

    result = execute_plan(plan_job(job, workspace))
    output = result.audit_results[0].output

    assert "binary_files_skipped: 1" in output
    assert "matches: 1" in output
    assert "result_limit_reached: true" in output
    assert "todo first" in output


@pytest.mark.parametrize(
    ("source", "symbol", "message"),
    (
        ("def broken(:\n", "broken", "could not be parsed"),
        ("def present():\n    pass\n", "missing", "did not resolve exactly once"),
    ),
)
def test_read_symbol_reports_parse_and_resolution_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    symbol: str,
    message: str,
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    (workspace.root / "module.py").write_text(source, encoding="utf-8")
    job = _job(
        workspace,
        job_id="AUDIT-A4-SYMBOL-EDGE",
        kind="audit",
        actions=[{"read_symbol": {"path": "module.py", "symbol": symbol}}],
    )

    with pytest.raises(ExecutionError, match=message):
        execute_plan(plan_job(job, workspace))


def test_warnings_command_reports_operational_record_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    monkeypatch.chdir(workspace.root)

    def fail(_workspace: Workspace) -> None:
        raise ExecutionError(
            ExecutionErrorCode.OPERATIONAL_RECORD_FAILED,
            "baseline unavailable",
            path=WARNING_BASELINE_RELATIVE_PATH.as_posix(),
        )

    monkeypatch.setattr(warning_baseline_module, "load_warning_baseline", fail)
    result = CliRunner().invoke(main, ["warnings"])

    assert result.exit_code != 0
    assert "WARNINGS_FAILED [OPERATIONAL_RECORD_FAILED]" in result.output

    def fail_workspace() -> None:
        raise WorkspaceError(WorkspaceErrorCode.WORKSPACE_NOT_FOUND, "missing")

    monkeypatch.setattr(cli_module, "_resolve_cli_workspace", fail_workspace)
    missing = CliRunner().invoke(main, ["warnings"])
    assert missing.exit_code != 0
    assert "WARNINGS_FAILED [WORKSPACE_NOT_FOUND] missing" in missing.output


def test_latest_ai_log_wraps_read_size_and_encoding_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "run.log"
    path.write_bytes(b"placeholder\n")
    original_read_bytes = Path.read_bytes

    with monkeypatch.context() as scoped:
        scoped.setattr(
            Path,
            "read_bytes",
            lambda self: (_ for _ in ()).throw(OSError("read failed")),
        )
        with pytest.raises(ExecutionError, match="could not be read"):
            render_latest_ai_log(path, json_output=False)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "read_bytes", lambda self: b"x" * 5_000_001)
        with pytest.raises(ExecutionError, match="exceeds.*size limit"):
            render_latest_ai_log(path, json_output=False)

    assert original_read_bytes(path) == b"placeholder\n"
    path.write_bytes(b"\xff")
    with pytest.raises(ExecutionError, match="not valid UTF-8"):
        render_latest_ai_log(path, json_output=False)


def test_planner_rejects_non_python_symbol_and_skips_non_file_ruff_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    (workspace.root / "notes.txt").write_text("notes\n", encoding="utf-8")
    audit_job = _job(
        workspace,
        job_id="AUDIT-A4-NON-PYTHON-SYMBOL",
        kind="audit",
        actions=[{"read_symbol": {"path": "notes.txt", "symbol": "notes"}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(audit_job, workspace)
    assert caught.value.code is PlanningErrorCode.TARGET_TYPE_INVALID

    patch_job = _job(
        workspace,
        job_id="PATCH-A4-RUFF-SKIP",
        kind="patch",
        actions=[
            {"create_directory": {"path": "package"}},
            {"create_file": {"path": "package/module.py", "content": "VALUE = 1\n"}},
        ],
        checks=[{"ruff": {}}],
    )
    plan = plan_job(patch_job, workspace)
    assert plan.checks[0].paths == (PurePosixPath("package/module.py"),)


def test_project_python_rejects_directory_and_ignores_ruff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    execution = workspace.config.execution.model_copy(
        update={"python_executable": "python-directory"}
    )
    config = workspace.config.model_copy(update={"execution": execution})
    workspace = Workspace(root=workspace.root, config=config)
    (workspace.root / "python-directory").mkdir()

    with pytest.raises(ProjectPythonError):
        resolve_project_python(workspace)

    job = _job(
        workspace,
        job_id="PATCH-A4-PROJECT-PYTHON-RUFF",
        kind="patch",
        actions=[{"create_file": {"path": "module.py", "content": "VALUE = 1\n"}}],
        checks=[{"ruff": {}}],
    )
    assert project_python_for_job(workspace, job) is None


def test_warning_baseline_load_and_update_error_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    path = workspace.root / WARNING_BASELINE_RELATIVE_PATH
    path.write_text("{}\n", encoding="utf-8")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            Path,
            "lstat",
            lambda self: (_ for _ in ()).throw(OSError("inspect failed")),
        )
        with pytest.raises(ExecutionError, match="could not be inspected"):
            load_warning_baseline(workspace)

    path.unlink()
    path.mkdir()
    with pytest.raises(ExecutionError, match="not a bounded regular file"):
        load_warning_baseline(workspace)
    path.rmdir()
    path.write_text("{}\n", encoding="utf-8")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            Path,
            "read_bytes",
            lambda self: (_ for _ in ()).throw(OSError("read failed")),
        )
        with pytest.raises(ExecutionError, match="could not be read"):
            load_warning_baseline(workspace)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            Path,
            "read_bytes",
            lambda self: b"x" * (warning_baseline_module._MAX_BASELINE_BYTES + 1),
        )
        with pytest.raises(ExecutionError, match="exceeds its size limit"):
            load_warning_baseline(workspace)

    path.write_text(
        json.dumps(
            {
                "schema": WARNING_BASELINE_SCHEMA,
                "project_id": "PSH-0000000000000000",
                "django_check_ids": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExecutionError, match="project ID does not match"):
        load_warning_baseline(workspace)

    with pytest.raises(ValueError, match="added and removed together"):
        update_warning_baseline(workspace, add=("urls.W005",), remove=("urls.W005",))


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (
            {
                "schema": "unsupported",
                "project_id": PROJECT_ID,
                "django_check_ids": [],
            },
            "schema is unsupported",
        ),
        (
            {
                "schema": WARNING_BASELINE_SCHEMA,
                "project_id": 1,
                "django_check_ids": [],
            },
            "invalid types",
        ),
        (
            {
                "schema": WARNING_BASELINE_SCHEMA,
                "project_id": PROJECT_ID,
                "django_check_ids": [1],
            },
            "IDs must be strings",
        ),
        (
            {
                "schema": WARNING_BASELINE_SCHEMA,
                "project_id": PROJECT_ID,
                "django_check_ids": ["urls.W005", "urls.W005"],
            },
            "must not contain duplicates",
        ),
    ),
)
def test_warning_baseline_parser_rejects_each_invalid_v1_shape(
    payload: object,
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        warning_baseline_module._parse_baseline(payload)


def test_warning_baseline_wraps_atomic_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)

    def fail_open(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(os, "open", fail_open)
    with pytest.raises(ExecutionError, match="could not be written"):
        update_warning_baseline(workspace, add=("urls.W005",))


def test_config_symlink_guard_is_portable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "patchshuttle.toml"
    path.write_text("placeholder\n", encoding="utf-8")
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == path)

    with pytest.raises(WorkspaceError) as caught:
        load_config(path)

    assert caught.value.code is WorkspaceErrorCode.CONFIG_NOT_REGULAR


def test_policy_symlink_guard_is_portable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    target = workspace.root / "linked"
    target.write_bytes(b"placeholder\n")
    original_lstat = Path.lstat

    class SymlinkMetadata:
        st_mode = stat.S_IFLNK | 0o777

    def symlink_lstat(path: Path):
        if path == target:
            return SymlinkMetadata()
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", symlink_lstat)

    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve("linked")

    assert caught.value.code is PolicyErrorCode.PATH_SYMLINK


def test_rollback_wrong_path_types_are_portable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    (workspace.root / "file-slot").mkdir()
    (workspace.root / "directory-slot").write_bytes(b"placeholder\n")

    result = rollback_created(
        workspace,
        files=(PurePosixPath("file-slot"),),
        directories=(PurePosixPath("directory-slot"),),
    )

    assert result.removed_files == ()
    assert result.removed_directories == ()
    assert result.unresolved == (
        PurePosixPath("file-slot"),
        PurePosixPath("directory-slot"),
    )


def test_workspace_symlink_guards_are_portable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "managed"
    path.write_bytes(b"placeholder\n")
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == path)

    assert workspace_module._is_allowed_metadata(path) is False

    operations = (
        lambda: workspace_module._ensure_directory(tmp_path, Path("managed")),
        lambda: workspace_module._require_managed_directory_if_present(
            tmp_path,
            Path("managed"),
        ),
        lambda: workspace_module._create_file_if_missing(
            tmp_path,
            Path("managed"),
            "content\n",
        ),
    )
    for operation in operations:
        with pytest.raises(WorkspaceError) as caught:
            operation()
        assert caught.value.code is WorkspaceErrorCode.MANAGED_PATH_CONFLICT

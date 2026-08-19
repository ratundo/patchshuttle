"""Contract tests for Phase 11 formatting and final transaction checks."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.backup as backup_module
import patchshuttle.formatters.runner as formatters_module
import patchshuttle.runner as runner_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle._process import ProcessResult, ProcessStatus
from patchshuttle.checks import CheckStatus
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.formatters import FormattedFileState, FormatterStatus
from patchshuttle.planner import plan_job
from patchshuttle.runner import TransactionStatus, execute_change_transaction
from patchshuttle.workspace import Workspace, init_workspace, load_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
RUN_TIMESTAMP = "2026_08_06_231100_000001"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    monkeypatch.setattr(backup_module, "_run_timestamp", lambda: RUN_TIMESTAMP)
    return init_workspace(tmp_path).workspace


def patch_plan(
    workspace: Workspace,
    *,
    actions: list[dict],
    checks: list[dict],
):
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-011",
        kind="patch",
        actions=actions,
        checks=checks,
    )
    return plan_job(job, workspace)


def manifest(workspace: Workspace) -> dict:
    path = (
        workspace.root / "patches/backups/PATCH-011" / RUN_TIMESTAMP / "manifest.json"
    )
    return json.loads(path.read_text("utf-8"))


def configured_profile(workspace: Workspace, code: str) -> Workspace:
    workspace.config_path.write_text(
        workspace.config_path.read_text("utf-8")
        + "\n[checks.profiles.phase11]\n"
        + f'argv = ["{{python}}", "-c", {json.dumps(code)}]\n'
        + "timeout_seconds = 30\n"
        + "allow_job_args = false\n",
        encoding="utf-8",
    )
    return load_workspace(workspace.root)


def test_success_runs_isort_black_then_final_checks_and_retains_hashes(
    workspace: Workspace,
) -> None:
    target = workspace.root / "module.py"
    target.write_bytes(b"import sys\nimport os\n\nVALUES=[1,2,3]\n")
    target.chmod(0o744)
    mode = stat.S_IMODE(target.stat().st_mode)
    plan = patch_plan(
        workspace,
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUES=[1,2,3]",
                    "new": "VALUES=[3,2,1]",
                }
            }
        ],
        checks=[{"compileall": {"paths": ["module.py"], "quiet": 2}}],
    )

    result = execute_change_transaction(plan, approved=True)

    expected = "import os\nimport sys\n\nVALUES = [3, 2, 1]\n"
    assert result.status is TransactionStatus.APPLIED
    assert [item.status for item in result.initial_checks] == [CheckStatus.PASSED]
    assert [item.name for item in result.formatting_results] == ["isort", "black"]
    assert [item.status for item in result.formatting_results] == [
        FormatterStatus.PASSED,
        FormatterStatus.PASSED,
    ]
    assert [item.status for item in result.final_checks] == [CheckStatus.PASSED]
    assert len(result.formatted_files) == 1
    assert (
        result.formatted_files[0].sha256
        == hashlib.sha256(target.read_bytes()).hexdigest()
    )
    assert target.read_text("utf-8") == expected
    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert manifest(workspace)["status"] == "COMPLETED"


def test_pep263_python_source_reaches_real_isort_and_black(
    workspace: Workspace,
) -> None:
    target = workspace.root / "latin1_module.py"
    target.write_bytes(
        "# coding: latin-1\nNAME = 'café'\nVALUES=[1,2]\n".encode("latin-1")
    )
    plan = patch_plan(
        workspace,
        actions=[
            {
                "replace_exact": {
                    "path": "latin1_module.py",
                    "old": "VALUES=[1,2]",
                    "new": "VALUES=[2,1]",
                }
            }
        ],
        checks=[{"compileall": {"paths": ["latin1_module.py"], "quiet": 2}}],
    )

    result = execute_change_transaction(plan, approved=True)

    assert result.status is TransactionStatus.APPLIED
    assert [item.status for item in result.formatting_results] == [
        FormatterStatus.PASSED,
        FormatterStatus.PASSED,
    ]
    assert target.read_bytes().decode("latin-1") == (
        '# coding: latin-1\nNAME = "café"\nVALUES = [2, 1]\n'
    )


def test_non_python_change_skips_formatters_and_duplicate_checks(
    workspace: Workspace,
) -> None:
    plan = patch_plan(
        workspace,
        actions=[{"create_file": {"path": "notes.txt", "content": "notes\n"}}],
        checks=[{"import_check": {"modules": ["json"]}}],
    )

    result = execute_change_transaction(plan, approved=True)

    assert result.initial_checks[0].status is CheckStatus.PASSED
    assert result.formatting_results == ()
    assert result.formatted_files == ()
    assert result.final_checks == ()


def test_local_policy_can_disable_final_check_rerun(workspace: Workspace) -> None:
    workspace.config_path.write_text(
        workspace.config_path.read_text("utf-8").replace(
            "rerun_checks = true",
            "rerun_checks = false",
        ),
        encoding="utf-8",
    )
    workspace = load_workspace(workspace.root)
    plan = patch_plan(
        workspace,
        actions=[{"create_file": {"path": "module.py", "content": "VALUES=[1,2]\n"}}],
        checks=[{"import_check": {"modules": ["json"]}}],
    )

    result = execute_change_transaction(plan, approved=True)

    assert len(result.initial_checks) == 1
    assert len(result.formatting_results) == 2
    assert len(result.formatted_files) == 1
    assert result.final_checks == ()
    assert (workspace.root / "module.py").read_text("utf-8") == "VALUES = [1, 2]\n"


def test_formatter_failure_rolls_back_and_retains_process_results(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "module.py"
    target.write_bytes(b"VALUE=1\n")
    plan = patch_plan(
        workspace,
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
    monkeypatch.setattr(
        formatters_module,
        "run_process",
        lambda *args, **kwargs: ProcessResult(
            status=ProcessStatus.FAILED,
            return_code=7,
            duration_ms=1,
            stdout="",
            stderr="isort failed\n",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.FORMAT_FAILED
    assert caught.value.item_id == "formatter_001"
    assert caught.value.path == "isort"
    assert caught.value.rollback_succeeded is True
    assert [item.status for item in caught.value.check_results] == [CheckStatus.PASSED]
    assert [item.status for item in caught.value.formatting_results] == [
        FormatterStatus.FAILED
    ]
    assert target.read_text("utf-8") == "VALUE=1\n"
    assert manifest(workspace)["failure_code"] == "FORMAT_FAILED"


def test_invalid_formatter_post_state_rolls_back(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "module.py"
    target.write_bytes(b"VALUE=1\n")
    plan = patch_plan(
        workspace,
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
    real_capture = runner_module.capture_formatted_files
    captures = 0

    def failed_second_capture(plan):
        nonlocal captures
        captures += 1
        if captures == 2:
            raise OSError("formatter target vanished")
        return real_capture(plan)

    monkeypatch.setattr(
        runner_module,
        "capture_formatted_files",
        failed_second_capture,
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.FORMAT_FAILED
    assert caught.value.path == "module.py"
    assert caught.value.rollback_succeeded is True
    assert len(caught.value.formatting_results) == 2
    assert target.read_text("utf-8") == "VALUE=1\n"


def test_formatter_preparation_error_rolls_back(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "module.py"
    target.write_bytes(b"VALUE=1\n")
    plan = patch_plan(
        workspace,
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
    monkeypatch.setattr(
        runner_module,
        "run_formatters",
        lambda plan: (_ for _ in ()).throw(ValueError("forged formatter plan")),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.FORMAT_FAILED
    assert caught.value.item_id == "formatting"
    assert caught.value.path == "module.py"
    assert caught.value.rollback_succeeded is True
    assert target.read_text("utf-8") == "VALUE=1\n"


def test_formatter_cannot_change_declared_non_python_target(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_target = workspace.root / "module.py"
    text_target = workspace.root / "notes.txt"
    python_target.write_bytes(b"VALUE=1\n")
    text_target.write_text("old\n", encoding="utf-8")
    plan = patch_plan(
        workspace,
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE=1",
                    "new": "VALUE=2",
                }
            },
            {
                "replace_exact": {
                    "path": "notes.txt",
                    "old": "old",
                    "new": "planned",
                }
            },
        ],
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    real_run = runner_module.run_formatters

    def mutating_formatter(plan):
        result = real_run(plan)
        text_target.write_text("formatter mutation\n", encoding="utf-8")
        return result

    monkeypatch.setattr(runner_module, "run_formatters", mutating_formatter)

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.FORMAT_FAILED
    assert caught.value.path == "notes.txt"
    assert caught.value.rollback_succeeded is True
    assert python_target.read_text("utf-8") == "VALUE=1\n"
    assert text_target.read_text("utf-8") == "old\n"


def test_failed_final_check_restores_pre_transaction_file(
    workspace: Workspace,
) -> None:
    workspace = configured_profile(
        workspace,
        (
            "from pathlib import Path\n"
            "import sys\n"
            "text = Path('module.py').read_text('utf-8')\n"
            "sys.exit(0 if 'VALUES=[2]' in text else 9)\n"
        ),
    )
    target = workspace.root / "module.py"
    target.write_text("VALUES=[1]\n", encoding="utf-8")
    plan = patch_plan(
        workspace,
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUES=[1]",
                    "new": "VALUES=[2]",
                }
            }
        ],
        checks=[{"profile": {"name": "phase11"}}],
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.CHECK_FAILED
    assert caught.value.item_id == "check_001"
    assert caught.value.path == "profile"
    assert caught.value.rollback_succeeded is True
    assert [item.status for item in caught.value.check_results] == [
        CheckStatus.PASSED,
        CheckStatus.FAILED,
    ]
    assert len(caught.value.formatting_results) == 2
    assert target.read_text("utf-8") == "VALUES=[1]\n"
    assert manifest(workspace)["failure_code"] == "CHECK_FAILED"


def test_final_check_mutation_of_formatted_target_is_rejected(
    workspace: Workspace,
) -> None:
    workspace = configured_profile(
        workspace,
        (
            "from pathlib import Path\n"
            "marker = Path('.phase11-check-marker')\n"
            "target = Path('module.py')\n"
            "if marker.exists():\n"
            "    target.write_text('MUTATED = True\\n', encoding='utf-8')\n"
            "else:\n"
            "    marker.write_text('initial\\n', encoding='utf-8')\n"
        ),
    )
    target = workspace.root / "module.py"
    target.write_bytes(b"VALUE=1\n")
    plan = patch_plan(
        workspace,
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE=1",
                    "new": "VALUE=2",
                }
            }
        ],
        checks=[{"profile": {"name": "phase11"}}],
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.CHECK_FAILED
    assert caught.value.path == "module.py"
    assert caught.value.rollback_succeeded is True
    assert [item.status for item in caught.value.check_results] == [
        CheckStatus.PASSED,
        CheckStatus.PASSED,
    ]
    assert target.read_text("utf-8") == "VALUE=1\n"


def test_formatter_state_scope_and_mode_invariants_are_stable() -> None:
    state = FormattedFileState(
        path=PurePosixPath("module.py"),
        sha256="0" * 64,
        size=1,
        mode=0o644,
        content=b"x",
    )

    with pytest.raises(ExecutionError, match="scope") as nonempty:
        runner_module._require_preserved_formatter_modes(
            (state,),
            (replace(state, path=PurePosixPath("other.py")),),
            check_results=(),
            formatting_results=(),
        )
    assert nonempty.value.path == "module.py"

    with pytest.raises(ExecutionError, match="scope") as empty:
        runner_module._require_preserved_formatter_modes(
            (),
            (state,),
            check_results=(),
            formatting_results=(),
        )
    assert empty.value.path is None

    with pytest.raises(ExecutionError, match="mode") as mode:
        runner_module._require_preserved_formatter_modes(
            (state,),
            (replace(state, mode=0o600),),
            check_results=(),
            formatting_results=(),
        )
    assert mode.value.path == "module.py"

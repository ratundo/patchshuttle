"""Contract tests for the implemented command-line surface."""

from pathlib import Path

import pytest
from click.testing import CliRunner

import patchshuttle.workspace as workspace_module
from patchshuttle import cli as cli_module
from patchshuttle.cli import main
from patchshuttle.errors import (
    ExecutionError,
    ExecutionErrorCode,
    WorkspaceError,
    WorkspaceErrorCode,
)
from patchshuttle.execution import RunResult, RunStatus
from patchshuttle.workspace import init_workspace

VALID_AUDIT_YAML = """\
protocol: 1
project_id: PSH-8F41C2A73D905E61
id: AUDIT-001
kind: audit
title: Inspect the project
actions:
  - tree:
      path: .
      depth: 4
"""

VALID_PATCH_YAML = """\
protocol: 1
project_id: PSH-8F41C2A73D905E61
id: PATCH-001
kind: patch
title: Create a module
actions:
  - create_file:
      path: src/example.py
      content: |
        VALUE = 1
checks:
  - compileall:
      paths: [src]
"""


def write_job(tmp_path: Path, content: str = VALID_AUDIT_YAML) -> Path:
    path = tmp_path / "audit.psh.yaml"
    path.write_text(content, encoding="utf-8", newline="")
    return path


def initialize_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: "PSH-8F41C2A73D905E61",
    )
    init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)


def file_snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_version_option_reports_distribution_version() -> None:
    result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == 0
    assert result.output == "patchshuttle, version 0.1.0a2\n"


def test_version_command_reports_distribution_version() -> None:
    result = CliRunner().invoke(main, ["version"])

    assert result.exit_code == 0
    assert result.output == "PatchShuttle 0.1.0a2\n"


def test_help_lists_only_the_implemented_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "version" in result.output
    assert "validate" in result.output
    assert "init" in result.output
    assert "plan" in result.output
    assert "run" in result.output


def test_init_reports_created_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: "PSH-8F41C2A73D905E61",
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["init"])

    assert result.exit_code == 0
    assert result.stdout == (
        "INITIALIZED\n"
        "project_id: PSH-8F41C2A73D905E61\n"
        "origin: existing\n"
        "config: patches/patchshuttle.toml\n"
        "created_entries: 16\n"
    )
    assert result.stderr == ""

    repeated = CliRunner().invoke(main, ["init"])
    assert repeated.exit_code == 0
    assert repeated.stdout.endswith("created_entries: 0\n")
    assert repeated.stdout.startswith("UNCHANGED\n")


def test_init_new_project_records_new_origin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: "PSH-8F41C2A73D905E61",
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["init", "--new-project"])

    assert result.exit_code == 0
    assert "origin: new\n" in result.stdout


def test_init_new_project_rejects_nonempty_directory(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("user content\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["init", "--new-project"])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert result.stderr.startswith("INIT_FAILED [NEW_PROJECT_NOT_EMPTY]")


def test_validate_reports_a_workspace_valid_job(tmp_path: Path, monkeypatch) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path)
    before = file_snapshot(tmp_path)

    result = CliRunner().invoke(main, ["validate", str(path)])

    assert result.exit_code == 0
    assert result.stdout == (
        "VALID\n"
        "job_id: AUDIT-001\n"
        "kind: audit\n"
        "protocol: 1\n"
        "project_id: PSH-8F41C2A73D905E61\n"
        "actions: 1\n"
        "checks: 0\n"
    )
    assert result.stderr == ""
    assert file_snapshot(tmp_path) == before


def test_validate_reports_schema_error_to_stderr(tmp_path: Path, monkeypatch) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path, VALID_AUDIT_YAML.replace("protocol: 1", "protocol: 2"))

    result = CliRunner().invoke(main, ["validate", str(path)])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith(
        "INVALID [JOB_SCHEMA_INVALID] $.protocol: Input should be 1\n"
    )


def test_validate_preserves_yaml_line_and_column(tmp_path: Path, monkeypatch) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path, "protocol: [1\n")

    result = CliRunner().invoke(main, ["validate", str(path)])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith("INVALID [YAML_INVALID] $ (line 2, column 1):")


def test_validate_reports_missing_file_with_stable_code(
    tmp_path: Path, monkeypatch
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = tmp_path / "missing.psh.yaml"

    result = CliRunner().invoke(main, ["validate", str(path)])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == ("INVALID [JOB_FILE_NOT_FOUND] job file was not found\n")


def test_validate_requires_initialized_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_job(tmp_path)

    result = CliRunner().invoke(main, ["validate", str(path)])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert result.stderr == (
        "INVALID [WORKSPACE_NOT_INITIALIZED] "
        "no initialized PatchShuttle workspace was found\n"
    )


def test_validate_rejects_job_for_another_project(tmp_path: Path, monkeypatch) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(
        tmp_path,
        VALID_AUDIT_YAML.replace(
            "PSH-8F41C2A73D905E61",
            "PSH-0000000000000000",
        ),
    )

    result = CliRunner().invoke(main, ["validate", str(path)])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert result.stderr == (
        "INVALID [PROJECT_ID_MISMATCH] $.project_id: "
        "job project_id does not match the initialized workspace\n"
    )


def test_validate_uses_workspace_job_size_limit(tmp_path: Path, monkeypatch) -> None:
    initialize_project(tmp_path, monkeypatch)
    config_path = tmp_path / "patches/patchshuttle.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "max_job_bytes = 2000000",
            "max_job_bytes = 1",
        ),
        encoding="utf-8",
        newline="",
    )
    path = write_job(tmp_path)

    result = CliRunner().invoke(main, ["validate", str(path)])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith("INVALID [JOB_SIZE_LIMIT_EXCEEDED]")


def test_validate_requires_one_job_argument() -> None:
    result = CliRunner().invoke(main, ["validate"])

    assert result.exit_code == 2
    assert "Missing argument 'JOB_FILE'." in result.stderr


def test_plan_reports_complete_patch_preview_without_writing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path, VALID_PATCH_YAML)
    before = file_snapshot(tmp_path)

    result = CliRunner().invoke(main, ["plan", str(path)])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.startswith(
        "PLAN\n" "project_id: PSH-8F41C2A73D905E61\n" "job_id: PATCH-001\n" "job_hash: "
    )
    assert "\nkind: patch\n" in result.stdout
    assert "planned_actions: 1\n" in result.stdout
    assert "  - action_001 create_file CREATE: src/example.py\n" in result.stdout
    assert "files_to_create: 1\n  - src/example.py\n" in result.stdout
    assert "files_to_modify: 0\n" in result.stdout
    assert "directories_to_create: 1\n  - src\n" in result.stdout
    assert "requested_checks: 1\n  - check_001 compileall: src\n" in result.stdout
    assert "formatting_scope: 1\n  - src/example.py\n" in result.stdout
    assert "html_lint_scope: 0\n" in result.stdout
    assert "preflight_checks: 3\n" in result.stdout
    assert "protected_paths: PASS\n" in result.stdout
    assert (
        "backup_destination: patches/backups/PATCH-001/<RUN_TIMESTAMP>\n"
        in result.stdout
    )
    assert "automatic_rollback: enabled\n" in result.stdout
    assert result.stdout.endswith("confirmation_required: yes\n")
    assert file_snapshot(tmp_path) == before
    assert not (tmp_path / "src").exists()


def test_plan_diff_prints_resolved_preview_without_writing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path, VALID_PATCH_YAML)

    result = CliRunner().invoke(main, ["plan", "--diff", str(path)])

    assert result.exit_code == 0
    assert "resolved_diff:\n--- /dev/null\n+++ b/src/example.py\n" in result.stdout
    assert "+VALUE = 1\n" in result.stdout
    assert result.stdout.endswith("resolved_diff_truncated: false\n")


def test_plan_diff_reports_no_changes_for_an_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path)

    result = CliRunner().invoke(main, ["plan", str(path), "--diff"])

    assert result.exit_code == 0
    assert "resolved_diff:\n  [NO FILE CHANGES]\n" in result.stdout


def test_plan_reports_audit_without_backup_or_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path)

    result = CliRunner().invoke(main, ["plan", str(path)])

    assert result.exit_code == 0
    assert "kind: audit\n" in result.stdout
    assert "backup_destination: none\n" in result.stdout
    assert "automatic_rollback: not_applicable\n" in result.stdout
    assert result.stdout.endswith("confirmation_required: no\n")


def test_plan_maps_job_workspace_policy_planning_and_profile_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)

    invalid = write_job(
        tmp_path, VALID_AUDIT_YAML.replace("protocol: 1", "protocol: 2")
    )
    result = CliRunner().invoke(main, ["plan", str(invalid)])
    assert result.exit_code == 2
    assert result.stderr.startswith("PLAN_FAILED [JOB_SCHEMA_INVALID]")

    mismatch = write_job(
        tmp_path,
        VALID_AUDIT_YAML.replace(
            "PSH-8F41C2A73D905E61",
            "PSH-0000000000000000",
        ),
    )
    result = CliRunner().invoke(main, ["plan", str(mismatch)])
    assert result.exit_code == 3
    assert result.stderr.startswith("PLAN_FAILED [PROJECT_ID_MISMATCH]")

    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    protected = write_job(
        tmp_path,
        VALID_AUDIT_YAML.replace(
            "tree:\n      path: .\n      depth: 4", "read:\n      path: .env"
        ),
    )
    result = CliRunner().invoke(main, ["plan", str(protected)])
    assert result.exit_code == 4
    assert result.stderr.startswith("PLAN_FAILED [PATH_PROTECTED]")

    (tmp_path / "README.md").write_text("project\n", encoding="utf-8")
    wrong_type = write_job(
        tmp_path,
        VALID_AUDIT_YAML.replace("path: .", "path: README.md"),
    )
    result = CliRunner().invoke(main, ["plan", str(wrong_type)])
    assert result.exit_code == 5
    assert result.stderr.startswith("PLAN_FAILED [TARGET_TYPE_INVALID]")

    missing_profile = write_job(
        tmp_path,
        """\
protocol: 1
project_id: PSH-8F41C2A73D905E61
id: VERIFY-001
kind: verify
checks:
  - profile:
      name: missing
""",
    )
    result = CliRunner().invoke(main, ["plan", str(missing_profile)])
    assert result.exit_code == 9
    assert result.stderr.startswith("PLAN_FAILED [CHECK_PROFILE_NOT_FOUND]")


def test_plan_policy_limit_failure_uses_exit_four(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    config_path = tmp_path / "patches/patchshuttle.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "max_single_file_bytes = 1000000",
            "max_single_file_bytes = 1",
        ),
        encoding="utf-8",
        newline="",
    )
    path = write_job(tmp_path, VALID_PATCH_YAML)

    result = CliRunner().invoke(main, ["plan", str(path)])

    assert result.exit_code == 4
    assert result.stderr.startswith("PLAN_FAILED [FILE_SIZE_LIMIT_EXCEEDED]")


def test_plan_requires_initialized_workspace_and_one_argument(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_job(tmp_path)

    result = CliRunner().invoke(main, ["plan", str(path)])
    assert result.exit_code == 3
    assert result.stderr.startswith("PLAN_FAILED [WORKSPACE_NOT_INITIALIZED]")

    missing_argument = CliRunner().invoke(main, ["plan"])
    assert missing_argument.exit_code == 2
    assert "Missing argument 'JOB_FILE'." in missing_argument.stderr


def test_run_yes_executes_reviewed_patch_and_reports_all_stages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path, VALID_PATCH_YAML)

    result = CliRunner().invoke(main, ["run", str(path), "--yes"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.startswith("PLAN\n")
    assert "\nkind: patch\n" in result.stdout
    assert "WARNING: Project checks execute local project code." in result.stdout
    assert "\nCOMPLETED\n" in result.stdout
    assert "created_files: 1\n  - src/example.py\n" in result.stdout
    assert "initial_checks: 1\n  - check_001 compileall PASSED\n" in result.stdout
    assert "formatters: 2\n" in result.stdout
    assert "  - formatter_001 isort PASSED\n" in result.stdout
    assert "  - formatter_002 black PASSED\n" in result.stdout
    assert "final_checks: 1\n  - check_001 compileall PASSED\n" in result.stdout
    assert "workspace_comparison: PASS\n" in result.stdout
    assert "workspace_changes: 2\n" in result.stdout
    assert "unexpected_workspace_changes: 0\n" in result.stdout
    assert (tmp_path / "src/example.py").read_text("utf-8") == "VALUE = 1\n"


def test_run_decline_and_aborted_prompt_leave_project_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path, VALID_PATCH_YAML)
    declined = CliRunner().invoke(main, ["run", str(path)], input="n\n")
    aborted = CliRunner().invoke(main, ["run", str(path)], input="")

    assert declined.exit_code == 4
    assert declined.stderr.startswith(
        "RUN_FAILED [USER_DECLINED] user declined the reviewed job plan\n"
    )
    assert "log: " in declined.stderr
    assert "archived_job: " in declined.stderr
    assert declined.stderr.endswith("rollback: NOT_STARTED\n")
    assert "Apply this job? [y/N]: n" in declined.stdout
    assert aborted.exit_code == 4
    assert aborted.stderr.startswith("RUN_FAILED [USER_DECLINED]")
    assert not (tmp_path / "src").exists()
    assert len(list((tmp_path / "patches/logs").glob("log_*.log"))) == 2
    decline_log = max((tmp_path / "patches/logs").glob("log_*.log"))
    decline_text = decline_log.read_text("utf-8")
    assert "result: USER_DECLINED" in decline_text
    assert "status: NOT_STARTED" in decline_text
    assert "next_expected_response: same_job_after_user_approval" in decline_text
    archives = list((tmp_path / "patches/failed").glob("*.psh.yaml"))
    assert len(archives) == 2
    assert all(item.read_text("utf-8") == VALID_PATCH_YAML for item in archives)
    registry = (tmp_path / "patches/state/registry.json").read_text("utf-8")
    assert '"latest_result": "USER_DECLINED"' in registry
    assert '"run_count": 2' in registry


def test_run_decline_reports_operational_record_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path, VALID_PATCH_YAML)
    monkeypatch.setattr(
        "patchshuttle.cli.record_declined_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ExecutionError(
                ExecutionErrorCode.OPERATIONAL_RECORD_FAILED,
                "injected record failure",
            )
        ),
    )

    result = CliRunner().invoke(main, ["run", str(path)], input="n\n")

    assert result.exit_code == 1
    assert result.stderr == (
        "RUN_FAILED [OPERATIONAL_RECORD_FAILED] injected record failure\n"
        "rollback: NOT_STARTED\n"
    )


def test_run_executes_audit_without_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path)

    result = CliRunner().invoke(main, ["run", str(path), "--yes"])

    assert result.exit_code == 0
    assert result.stdout.startswith("PLAN\n")
    assert "Apply this job?" not in result.stdout
    assert "COMPLETED\n" in result.stdout
    assert "audit_results: 1\n" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("code", "expected_exit"),
    [
        (ExecutionErrorCode.APPROVAL_REQUIRED, 4),
        (ExecutionErrorCode.WORKSPACE_LOCKED, 3),
        (ExecutionErrorCode.WORKSPACE_LOCK_FAILED, 3),
        (ExecutionErrorCode.CHECK_FAILED, 6),
        (ExecutionErrorCode.FORMAT_FAILED, 7),
        (ExecutionErrorCode.ROLLBACK_FAILED, 8),
        (ExecutionErrorCode.ACTION_FAILED, 5),
    ],
)
def test_run_maps_transaction_failures_to_stable_exit_codes(
    tmp_path: Path,
    monkeypatch,
    code: ExecutionErrorCode,
    expected_exit: int,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path, VALID_PATCH_YAML)

    def fail_execution(*args, **kwargs):
        raise ExecutionError(code, "injected failure")

    monkeypatch.setattr("patchshuttle.cli.execute_plan", fail_execution)

    result = CliRunner().invoke(main, ["run", str(path), "--yes"])

    assert result.exit_code == expected_exit
    assert result.stderr == (
        f"RUN_FAILED [{code.value}] injected failure\nrollback: NOT_STARTED\n"
    )


def test_run_failure_renders_backup_rollback_and_stage_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path, VALID_PATCH_YAML)
    backup = tmp_path / "patches/backups/PATCH-001/run"

    def fail_execution(*args, **kwargs):
        from patchshuttle.checks import CheckResult, CheckStatus
        from patchshuttle.formatters import FormatterResult, FormatterStatus

        raise ExecutionError(
            ExecutionErrorCode.ROLLBACK_FAILED,
            "injected failure",
            backup_path=backup,
            rollback_succeeded=False,
            check_results=(
                CheckResult(
                    id="check_001",
                    name="compileall",
                    status=CheckStatus.PASSED,
                    argv=("python",),
                    working_directory=tmp_path,
                    timeout_seconds=30,
                    return_code=0,
                    duration_ms=1,
                    stdout="",
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                ),
            ),
            formatting_results=(
                FormatterResult(
                    id="formatter_001",
                    name="isort",
                    status=FormatterStatus.FAILED,
                    argv=("python",),
                    working_directory=tmp_path,
                    timeout_seconds=30,
                    return_code=1,
                    duration_ms=1,
                    stdout="",
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                ),
            ),
        )

    monkeypatch.setattr("patchshuttle.cli.execute_plan", fail_execution)

    result = CliRunner().invoke(main, ["run", str(path), "--yes"])

    assert result.exit_code == 8
    assert f"backup: {backup.as_posix()}\n" in result.stderr
    assert "rollback: FAILED\n" in result.stderr
    assert "check: check_001 compileall PASSED\n" in result.stderr
    assert "formatter: formatter_001 isort FAILED\n" in result.stderr


def test_run_reports_unexpected_workspace_side_effect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    config_path = tmp_path / "patches/patchshuttle.toml"
    config_path.write_text(
        config_path.read_text("utf-8") + """

[checks.profiles.side_effect]
argv = ["{python}", "-c", "from pathlib import Path; Path('side.txt').write_text('side')"]
timeout_seconds = 30
allow_job_args = false
""",
        encoding="utf-8",
    )
    path = write_job(
        tmp_path,
        """\
protocol: 1
project_id: PSH-8F41C2A73D905E61
id: PATCH-013
kind: patch
actions:
  - create_file:
      path: planned.txt
      content: planned
checks:
  - profile:
      name: side_effect
""",
    )

    result = CliRunner().invoke(main, ["run", str(path), "--yes"])

    assert result.exit_code == 5
    assert "RUN_FAILED [UNEXPECTED_WORKSPACE_CHANGE] side.txt:" in result.stderr
    assert "rollback: SUCCESS\n" in result.stderr
    assert "unexpected_workspace_changes: 1\n" in result.stderr
    assert "  - ADDED side.txt\n" in result.stderr
    assert not (tmp_path / "planned.txt").exists()
    assert (tmp_path / "side.txt").exists()


def test_run_keep_changes_retains_failed_patch_and_marks_log(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    config_path = tmp_path / "patches/patchshuttle.toml"
    config_path.write_text(
        config_path.read_text("utf-8") + """

[checks.profiles.side_effect_keep]
argv = ["{python}", "-c", "from pathlib import Path; Path('side.txt').write_text('side')"]
timeout_seconds = 30
allow_job_args = false
""",
        encoding="utf-8",
    )
    path = write_job(
        tmp_path,
        """\
protocol: 1
project_id: PSH-8F41C2A73D905E61
id: PATCH-KEEP-CLI
kind: patch
actions:
  - create_file:
      path: planned.txt
      content: planned
checks:
  - profile:
      name: side_effect_keep
""",
    )

    result = CliRunner().invoke(
        main,
        ["run", str(path), "--yes", "--keep-changes"],
    )

    assert result.exit_code == 5
    assert "--keep-changes disables automatic rollback" in result.stdout
    assert "rollback: SKIPPED_CHANGES_KEPT" in result.stderr
    assert (tmp_path / "planned.txt").read_text("utf-8") == "planned"
    assert (tmp_path / "side.txt").read_text("utf-8") == "side"
    manifest = next(
        (tmp_path / "patches/backups/PATCH-KEEP-CLI").glob("*/manifest.json")
    )
    assert '"status": "CHANGES_KEPT"' in manifest.read_text("utf-8")
    log = next((tmp_path / "patches/logs").glob("log_*.log"))
    assert "rollback_status: SKIPPED_CHANGES_KEPT" in log.read_text("utf-8")


def test_run_keep_changes_requires_separate_consent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path, VALID_PATCH_YAML)

    declined = CliRunner().invoke(
        main,
        ["run", str(path), "--keep-changes"],
        input="y\nn\n",
    )

    assert declined.exit_code == 4
    assert "Apply this job? [y/N]: y" in declined.stdout
    assert "Keep partial changes if this job fails? [y/N]: n" in declined.stdout
    assert "RUN_FAILED [USER_DECLINED]" in declined.stderr
    assert not (tmp_path / "src").exists()


def test_run_keep_changes_rejects_wrong_kind_and_restricted_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    audit_path = write_job(tmp_path)

    wrong_kind = CliRunner().invoke(
        main,
        ["run", str(audit_path), "--yes", "--keep-changes"],
    )
    assert wrong_kind.exit_code == 5
    assert "RUN_FAILED [JOB_KIND_UNSUPPORTED]" in wrong_kind.stderr

    config_path = tmp_path / "patches/patchshuttle.toml"
    config_path.write_text(
        config_path.read_text("utf-8").replace(
            "allow_keep_changes = true",
            "allow_keep_changes = false",
        ),
        encoding="utf-8",
    )
    patch_path = write_job(tmp_path, VALID_PATCH_YAML)
    forbidden = CliRunner().invoke(
        main,
        ["run", str(patch_path), "--yes", "--keep-changes"],
    )
    assert forbidden.exit_code == 4
    assert "RUN_FAILED [KEEP_CHANGES_FORBIDDEN]" in forbidden.stderr
    assert not (tmp_path / "src").exists()


def test_keep_changes_confirmation_helper_handles_yes_no_and_abort(
    monkeypatch,
) -> None:
    assert cli_module._approve_keep_changes(True) is True
    monkeypatch.setattr(cli_module.click, "confirm", lambda *args, **kwargs: True)
    assert cli_module._approve_keep_changes(False) is True
    monkeypatch.setattr(cli_module.click, "confirm", lambda *args, **kwargs: False)
    assert cli_module._approve_keep_changes(False) is False
    monkeypatch.setattr(
        cli_module.click,
        "confirm",
        lambda *args, **kwargs: (_ for _ in ()).throw(cli_module.click.Abort()),
    )
    assert cli_module._approve_keep_changes(False) is False


def test_execution_error_render_distinguishes_skipped_without_changes() -> None:
    error = ExecutionError(
        ExecutionErrorCode.ACTION_FAILED,
        "failed before publishing",
        rollback_skipped=True,
    )

    rendered = cli_module._render_execution_error(error)

    assert rendered.endswith("rollback: SKIPPED_NO_CHANGES")


def test_run_no_change_reports_inventory_not_applicable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    (tmp_path / "same.txt").write_bytes(b"same\n")
    path = write_job(
        tmp_path,
        """\
protocol: 1
project_id: PSH-8F41C2A73D905E61
id: PATCH-013-NO-CHANGE
kind: patch
actions:
  - create_file:
      path: same.txt
      content: |
        same
checks:
  - import_check:
      modules: [json]
""",
    )

    result = CliRunner().invoke(main, ["run", str(path), "--yes"])

    assert result.exit_code == 0
    assert "\nNO_CHANGE\n" in result.stdout
    assert "workspace_comparison: NOT_APPLICABLE\n" in result.stdout
    assert "workspace_changes: 0\n" in result.stdout


def test_run_repeat_logs_status_and_conflicting_content_workflow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path, VALID_PATCH_YAML)

    first = CliRunner().invoke(main, ["run", str(path), "--yes"])
    assert first.exit_code == 0
    assert "\nCOMPLETED\n" in first.stdout
    assert "log: " in first.stdout
    assert "archived_job: " in first.stdout

    repeated = CliRunner().invoke(main, ["run", str(path), "--yes"])
    assert repeated.exit_code == 0
    assert repeated.stdout.startswith("ALREADY_APPLIED\n")
    assert "PLAN\n" not in repeated.stdout
    assert "WARNING:" not in repeated.stdout

    latest = CliRunner().invoke(main, ["logs", "--last"])
    assert latest.exit_code == 0
    latest_path = Path(latest.stdout.strip())
    assert latest_path.is_file()
    assert "result: ALREADY_APPLIED" in latest_path.read_text("utf-8")

    status = CliRunner().invoke(main, ["status", "PATCH-001"])
    assert status.exit_code == 0
    assert status.stdout.startswith("STATUS\n")
    assert "latest_result: ALREADY_APPLIED\n" in status.stdout
    assert "completed: true\n" in status.stdout
    assert "run_count: 2\n" in status.stdout

    all_status = CliRunner().invoke(main, ["status"])
    assert all_status.exit_code == 0
    assert "jobs: 1\n" in all_status.stdout
    assert "  - PATCH-001 ALREADY_APPLIED " in all_status.stdout

    path.write_text(
        VALID_PATCH_YAML.replace("VALUE = 1", "VALUE = 2"),
        encoding="utf-8",
        newline="",
    )
    conflict = CliRunner().invoke(main, ["run", str(path), "--yes"])
    assert conflict.exit_code == 3
    assert conflict.stdout == ""
    assert conflict.stderr.startswith("RUN_FAILED [PATCH_ID_CONFLICT] PATCH-001:")
    assert "log: " in conflict.stderr
    assert "archived_job: " in conflict.stderr
    assert conflict.stderr.endswith("rollback: NOT_STARTED\n")


def test_logs_and_status_empty_missing_and_usage_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)

    no_flag = CliRunner().invoke(main, ["logs"])
    assert no_flag.exit_code == 2
    assert "the --last option is required" in no_flag.stderr

    no_logs = CliRunner().invoke(main, ["logs", "--last"])
    assert no_logs.exit_code == 3
    assert no_logs.stderr.startswith("LOGS_FAILED [LOG_NOT_FOUND]")

    empty = CliRunner().invoke(main, ["status"])
    assert empty.exit_code == 0
    assert "latest_log: none\n" in empty.stdout
    assert empty.stdout.endswith("jobs: 0\n")

    missing = CliRunner().invoke(main, ["status", "PATCH-404"])
    assert missing.exit_code == 3
    assert missing.stderr.startswith("STATUS_FAILED [JOB_NOT_FOUND] PATCH-404:")


def test_logs_and_status_require_initialized_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    logs = CliRunner().invoke(main, ["logs", "--last"])
    status = CliRunner().invoke(main, ["status"])

    assert logs.exit_code == 3
    assert logs.stderr.startswith("LOGS_FAILED [WORKSPACE_NOT_INITIALIZED]")
    assert status.exit_code == 3
    assert status.stderr.startswith("STATUS_FAILED [WORKSPACE_NOT_INITIALIZED]")


def test_plan_maps_workspace_error_raised_during_planning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path)
    monkeypatch.setattr(
        "patchshuttle.cli.plan_job",
        lambda *args: (_ for _ in ()).throw(
            WorkspaceError(WorkspaceErrorCode.WORKSPACE_READ_FAILED, "injected")
        ),
    )

    result = CliRunner().invoke(main, ["plan", str(path)])

    assert result.exit_code == 3
    assert result.stderr == "PLAN_FAILED [WORKSPACE_READ_FAILED] injected\n"


def test_run_renderer_accepts_result_without_operational_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    path = write_job(tmp_path, VALID_PATCH_YAML)

    def no_artifacts(plan, **kwargs):
        return RunResult(
            status=RunStatus.NO_CHANGE,
            plan=plan,
            backup_path=None,
            created_files=(),
            created_directories=(),
        )

    monkeypatch.setattr("patchshuttle.cli.execute_plan", no_artifacts)
    result = CliRunner().invoke(main, ["run", str(path), "--yes"])

    assert result.exit_code == 0
    assert "\nNO_CHANGE\n" in result.stdout
    assert "\nlog: " not in result.stdout
    assert "\narchived_job: " not in result.stdout


def test_status_propagates_nonempty_log_lookup_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "patchshuttle.cli.latest_log_path",
        lambda *args: (_ for _ in ()).throw(
            ExecutionError(
                ExecutionErrorCode.OPERATIONAL_RECORD_FAILED,
                "injected",
            )
        ),
    )

    result = CliRunner().invoke(main, ["status"])

    assert result.exit_code == 1
    assert result.stderr == "STATUS_FAILED [OPERATIONAL_RECORD_FAILED] injected\n"

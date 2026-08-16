"""Snapshot, handoff, and declarative Python API contracts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

import patchshuttle.context as context_module
import patchshuttle.workspace as workspace_module
from patchshuttle import (
    Job,
    create_handoff,
    create_snapshot,
    execute_plan,
    plan_job,
)
from patchshuttle._process import ProcessResult, ProcessStatus
from patchshuttle.actions import (
    apply_diff,
    create_directory,
    create_file,
    delete_exact,
    environment,
    file_info,
    find_files,
    git_status,
    hash,
    insert_after,
    insert_before,
    read,
    replace_exact,
    search,
    tree,
)
from patchshuttle.checks import (
    compileall,
    django_check,
    django_migrations_check,
    django_test,
    import_check,
    profile,
)
from patchshuttle.checks import pytest as pytest_check
from patchshuttle.checks import (
    unittest,
)
from patchshuttle.cli import main
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.inventory import (
    InventoryEntry,
    InventoryEntryKind,
    InventoryError,
    InventoryErrorCode,
    WorkspaceInventory,
)
from patchshuttle.logging import current_run_clock, write_named_log
from patchshuttle.models import Action, Check
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


def test_snapshot_contains_bounded_metadata_without_source_contents(
    workspace: Workspace,
) -> None:
    secret_source = "UNIQUE_SOURCE_BODY_SHOULD_NOT_APPEAR"
    (workspace.root / "app.py").write_text(secret_source + "\n", encoding="utf-8")

    result = create_snapshot(workspace)
    text = result.path.read_text("utf-8")

    assert result.inventory_entries > 0
    assert result.output_truncated is False
    assert "=== PATCHSHUTTLE_SNAPSHOT ===" in text
    assert f"project_id: {PROJECT_ID}" in text
    assert "app.py [file]" in text
    assert "app.py size=" in text
    assert "sha256=" in text
    assert "audit_actions: tree, read" in text
    assert secret_source not in text
    assert "\npatches/logs/ [directory]\n" not in text


def test_handoff_includes_latest_context_and_expected_yaml_response(
    workspace: Workspace,
) -> None:
    (workspace.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-HANDOFF",
        kind="audit",
        actions=[{"hash": {"path": "app.py"}}],
    )
    run = execute_plan(plan_job(job, workspace))
    assert run.log_path is not None

    result = create_handoff(workspace)
    text = result.path.read_text("utf-8")

    assert result.recent_jobs == 1
    assert "=== PATCHSHUTTLE_HANDOFF ===" in text
    assert "job_id: AUDIT-HANDOFF" in text
    assert "result: COMPLETED" in text
    assert "AUDIT-HANDOFF kind=audit result=COMPLETED" in text
    assert "EXPECTED_RESPONSE: one .psh.yaml file only" in text
    assert "VALUE = 1" not in text


def test_snapshot_and_handoff_apply_output_bound_and_cli_commands(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(40):
        (workspace.root / f"file-{index:03d}.txt").write_text(
            f"value {index}\n",
            encoding="utf-8",
        )
    execution = workspace.config.execution.model_copy(
        update={"max_command_output_bytes": 250}
    )
    limited = replace(
        workspace,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    snapshot = create_snapshot(limited)
    assert snapshot.output_truncated is True
    assert "[TRUNCATED BY PATCHSHUTTLE]" in snapshot.path.read_text("utf-8")

    monkeypatch.chdir(workspace.root)
    snapshot_cli = CliRunner().invoke(main, ["snapshot"])
    assert snapshot_cli.exit_code == 0
    assert snapshot_cli.stdout.startswith("SNAPSHOT_CREATED\n")
    handoff_cli = CliRunner().invoke(main, ["handoff"])
    assert handoff_cli.exit_code == 0
    assert handoff_cli.stdout.startswith("HANDOFF_CREATED\n")


def test_context_output_bound_holds_when_limit_is_smaller_than_marker() -> None:
    output, truncated = context_module._bounded("abcdef", 3)

    assert truncated is True
    assert output.encode("utf-8") == b"\n[T"


def test_all_action_constructors_match_yaml_models() -> None:
    diff = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"
    cases = (
        (tree("src", depth=2), {"tree": {"path": "src", "depth": 2}}),
        (
            read("a.txt", end_line=4, max_bytes=100),
            {"read": {"path": "a.txt", "end_line": 4, "max_bytes": 100}},
        ),
        (read("b.txt"), {"read": {"path": "b.txt"}}),
        (
            search("TODO", path="src", glob="*.py", case_sensitive=False),
            {
                "search": {
                    "text": "TODO",
                    "path": "src",
                    "glob": "*.py",
                    "case_sensitive": False,
                }
            },
        ),
        (
            find_files("*.py", path="src"),
            {"find_files": {"glob": "*.py", "path": "src"}},
        ),
        (file_info("a.txt"), {"file_info": {"path": "a.txt"}}),
        (hash("a.txt"), {"hash": {"path": "a.txt"}}),
        (git_status(), {"git_status": {}}),
        (environment(), {"environment": {}}),
        (create_directory("src/pkg"), {"create_directory": {"path": "src/pkg"}}),
        (
            create_file("a.txt", "a\n"),
            {"create_file": {"path": "a.txt", "content": "a\n"}},
        ),
        (
            replace_exact("a.txt", "a", "b"),
            {"replace_exact": {"path": "a.txt", "old": "a", "new": "b"}},
        ),
        (
            insert_before("a.txt", "a", "b"),
            {"insert_before": {"path": "a.txt", "anchor": "a", "content": "b"}},
        ),
        (
            insert_after("a.txt", "a", "b"),
            {"insert_after": {"path": "a.txt", "anchor": "a", "content": "b"}},
        ),
        (delete_exact("a.txt", "a"), {"delete_exact": {"path": "a.txt", "text": "a"}}),
        (apply_diff(diff), {"apply_diff": {"diff": diff}}),
    )
    for constructed, payload in cases:
        assert constructed == Action(payload)


def test_all_check_constructors_match_yaml_models() -> None:
    cases = (
        (compileall(["src"]), {"compileall": {"paths": ["src"]}}),
        (
            pytest_check(["tests"], args=["-q"], timeout_seconds=20),
            {
                "pytest": {
                    "paths": ["tests"],
                    "args": ["-q"],
                    "timeout_seconds": 20,
                }
            },
        ),
        (pytest_check(), {"pytest": {}}),
        (unittest(), {"unittest": {"discover": "tests", "pattern": "test_*.py"}}),
        (django_check(), {"django_check": {"manage_py": "manage.py"}}),
        (
            django_migrations_check(),
            {"django_migrations_check": {"manage_py": "manage.py"}},
        ),
        (
            django_test(labels=["app.tests"]),
            {"django_test": {"manage_py": "manage.py", "labels": ["app.tests"]}},
        ),
        (import_check(["json"]), {"import_check": {"modules": ["json"]}}),
        (profile("local"), {"profile": {"name": "local"}}),
    )
    for constructed, payload in cases:
        assert constructed == Check(payload)


def test_constructor_models_execute_through_the_same_python_api(
    workspace: Workspace,
) -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-PYTHON-API",
        kind="patch",
        actions=[create_file("src/example.txt", "value\n")],
        checks=[import_check(["json"])],
    )

    result = execute_plan(plan_job(job, workspace), approved=True)

    assert result.status.value == "COMPLETED"
    assert (workspace.root / "src/example.txt").read_text("utf-8") == "value\n"


def test_constructors_retain_strict_model_validation() -> None:
    with pytest.raises(ValidationError):
        tree(depth=11)
    with pytest.raises(ValidationError):
        search("")
    with pytest.raises(ValidationError):
        import_check(["json; unsafe"])
    with pytest.raises(ValidationError):
        pytest_check(timeout_seconds=0)


def test_context_helpers_cover_empty_limits_git_and_sections(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = WorkspaceInventory(entries=(), hashed_bytes=0)
    assert context_module._tree_lines(empty) == (("[EMPTY PROJECT]",), False)
    assert context_module._file_lines(empty) == (("[NO FILES]",), False)
    entries = tuple(
        InventoryEntry(
            path=Path(f"file-{index}.txt"),  # type: ignore[arg-type]
            kind=InventoryEntryKind.FILE,
            size=1,
            modified_ns=0,
            mode=0o600,
            sha256="0" * 64,
        )
        for index in range(2)
    )
    inventory = WorkspaceInventory(entries=entries, hashed_bytes=2)
    monkeypatch.setattr(context_module, "_TREE_LIMIT", 1)
    monkeypatch.setattr(context_module, "_FILE_LIMIT", 1)
    assert context_module._tree_lines(inventory)[1] is True
    assert context_module._file_lines(inventory)[1] is True

    assert context_module._section("text", "SUMMARY") is None
    assert context_module._section("=== SUMMARY ===\nvalue", "SUMMARY") == "value"

    (workspace.root / ".git").mkdir()
    monkeypatch.setattr(context_module.shutil, "which", lambda name: "/git")

    def result(
        status: ProcessStatus,
        stdout: str = "",
        *,
        truncated: bool = False,
    ) -> ProcessResult:
        return ProcessResult(
            status=status,
            return_code=0 if status is ProcessStatus.PASSED else 1,
            duration_ms=1,
            stdout=stdout,
            stderr="",
            stdout_truncated=truncated,
            stderr_truncated=False,
        )

    monkeypatch.setattr(
        context_module,
        "run_process",
        lambda *args, **kwargs: result(ProcessStatus.FAILED),
    )
    assert context_module._git_status(workspace) == "NOT_AVAILABLE"
    monkeypatch.setattr(
        context_module,
        "run_process",
        lambda *args, **kwargs: result(ProcessStatus.PASSED),
    )
    assert context_module._git_status(workspace) == "CLEAN"
    monkeypatch.setattr(
        context_module,
        "run_process",
        lambda *args, **kwargs: result(
            ProcessStatus.PASSED,
            "## main\n",
            truncated=True,
        ),
    )
    assert context_module._git_status(workspace) == "## main\n[TRUNCATED]"


def test_context_inventory_and_log_inspection_failures(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        context_module,
        "capture_inventory",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            InventoryError(
                InventoryErrorCode.INSPECTION_FAILED,
                "injected",
                path=Path("bad.txt"),  # type: ignore[arg-type]
            )
        ),
    )
    with pytest.raises(ExecutionError) as capture:
        context_module._capture(workspace)
    assert capture.value.code is ExecutionErrorCode.WORKSPACE_INVENTORY_FAILED

    monkeypatch.undo()
    logs = workspace.root / "patches/logs"
    real_iterdir = Path.iterdir

    def fail_logs(path: Path):
        if path == logs:
            raise OSError("injected")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_logs)
    with pytest.raises(ExecutionError) as inspection:
        context_module._latest_run_context(workspace)
    assert inspection.value.code is ExecutionErrorCode.OPERATIONAL_RECORD_FAILED

    monkeypatch.undo()
    (logs / "log_invalid.log").mkdir()
    (logs / "log_binary.log").write_bytes(b"\xff")
    (logs / "log_no_sections.log").write_text("plain\n", encoding="utf-8")
    assert context_module._latest_run_context(workspace) == (
        "NOT_AVAILABLE",
        "NOT_AVAILABLE",
    )


def test_named_log_validates_label_and_local_redaction_policy(
    workspace: Workspace,
) -> None:
    clock = current_run_clock(workspace)
    with pytest.raises(ValueError):
        write_named_log(workspace, clock=clock, label="bad label", content="x")
    logging = workspace.config.logging.model_copy(
        update={"redact_known_secrets": False}
    )
    unredacted = replace(
        workspace,
        config=workspace.config.model_copy(update={"logging": logging}),
    )
    path = write_named_log(
        unredacted,
        clock=clock,
        label="TEST",
        content="token=visible",
    )
    assert path.read_text("utf-8") == "token=visible\n"

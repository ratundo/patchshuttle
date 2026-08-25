"""Execution contracts for audit and verify jobs."""

from __future__ import annotations

import stat
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import patchshuttle.audit as audit_module
import patchshuttle.verification as verification_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job, RunStatus, execute_plan
from patchshuttle._process import ProcessResult, ProcessStatus
from patchshuttle.audit import AuditActionResult, execute_audit_locked
from patchshuttle.checks import CheckStatus
from patchshuttle.config import CheckProfileSettings
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.inventory import InventoryError, InventoryErrorCode
from patchshuttle.planner import plan_job
from patchshuttle.policy import PathKind
from patchshuttle.registry import load_registry
from patchshuttle.verification import execute_verification_locked
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


def test_all_audit_actions_are_bounded_logged_and_read_only(
    workspace: Workspace,
) -> None:
    source = workspace.root / "src"
    source.mkdir()
    (source / "alpha.txt").write_text("Alpha\nTODO item\n", encoding="utf-8")
    (source / "other.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "binary.txt").write_bytes(b"\x00\x01")
    (source / ".hidden.txt").write_text("hidden\n", encoding="utf-8")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-016",
        kind="audit",
        actions=[
            {"tree": {"path": ".", "depth": 3, "max_entries": 2}},
            {
                "read": {
                    "path": "src/alpha.txt",
                    "start_line": 2,
                    "max_bytes": 70,
                }
            },
            {
                "search": {
                    "path": "src",
                    "text": "todo",
                    "glob": "*.txt",
                    "case_sensitive": False,
                }
            },
            {"find_files": {"path": "src", "glob": "*.py"}},
            {"file_info": {"path": "src/alpha.txt"}},
            {"hash": {"path": "src/alpha.txt"}},
            {"git_status": {}},
            {"environment": {}},
        ],
    )

    result = execute_plan(plan_job(job, workspace))

    assert result.status is RunStatus.COMPLETED
    assert len(result.audit_results) == 8
    assert result.workspace_comparison is not None
    assert result.workspace_comparison.success is True
    assert "[ENTRY LIMIT REACHED]" in result.audit_results[0].output
    assert "TODO item" in result.audit_results[1].output
    assert "src/alpha.txt:2:TODO item" in result.audit_results[2].output
    assert "binary_files_skipped: 1" in result.audit_results[2].output
    assert "src/other.py" in result.audit_results[3].output
    assert "encoding: utf-8" in result.audit_results[4].output
    assert "algorithm: sha256" in result.audit_results[5].output
    assert result.audit_results[6].status == "NOT_AVAILABLE"
    assert "environment" == result.audit_results[7].name
    assert result.log_path is not None
    log = result.log_path.read_text("utf-8")
    assert "=== AUDIT ===" in log
    assert "output_begin" in log
    assert "action_type: search" in log
    assert "=== ACTIONS ===\nNOT_APPLICABLE" in log
    record = load_registry(workspace).jobs[job.id]
    assert record.latest_result == "COMPLETED"
    assert record.kind == "audit"


def test_search_context_and_read_symbol_are_bounded_and_read_only(
    workspace: Workspace,
) -> None:
    (workspace.root / "symbols.py").write_text(
        "def decorator(value):\n"
        "    return value\n"
        "\n"
        "@decorator\n"
        "class Service:\n"
        "    def method(self):\n"
        "        marker = 'TODO'\n"
        "        return marker\n"
        "\n"
        "async def worker():\n"
        "    return None\n",
        encoding="utf-8",
    )
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-CONTEXT-SYMBOLS",
        kind="audit",
        actions=[
            {
                "search_context": {
                    "path": "symbols.py",
                    "text": "TODO",
                    "before": 1,
                    "after": 1,
                }
            },
            {"read_symbol": {"path": "symbols.py", "symbol": "Service.method"}},
            {"read_symbol": {"path": "symbols.py", "symbol": "Service"}},
            {"read_symbol": {"path": "symbols.py", "symbol": "worker"}},
        ],
    )

    result = execute_plan(plan_job(job, workspace))

    context, method, service, worker = (item.output for item in result.audit_results)
    assert "matches: 1" in context
    assert "context_start_line: 6" in context
    assert "context_end_line: 8" in context
    assert ">     7:         marker = 'TODO'" in context
    assert "symbol: Service.method" in method
    assert "kind: function" in method
    assert "start_line: 6" in method
    assert "end_line: 8" in method
    assert "sha256:" in method
    assert "symbol: Service" in service
    assert "kind: class" in service
    assert "start_line: 4" in service
    assert "end_line: 8" in service
    assert "symbol: worker" in worker
    assert "kind: async_function" in worker
    assert result.workspace_comparison.success is True


def test_audit_read_marks_output_truncation(workspace: Workspace) -> None:
    (workspace.root / "long.txt").write_text("x" * 200 + "\n", encoding="utf-8")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-TRUNCATE",
        kind="audit",
        actions=[{"read": {"path": "long.txt", "max_bytes": 40}}],
    )

    result = execute_plan(plan_job(job, workspace))

    assert result.audit_results[0].output_truncated is True
    assert "[TRUNCATED BY PATCHSHUTTLE]" in result.audit_results[0].output


def test_audit_output_bound_holds_when_limit_is_smaller_than_marker() -> None:
    output, truncated = audit_module._bounded_output("abcdef", 3)

    assert truncated is True
    assert output.encode("utf-8") == b"\n[T"


def test_audit_failure_and_unexpected_change_are_recorded(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-FAIL",
        kind="audit",
        actions=[{"read": {"path": "target.txt"}}],
    )
    plan = plan_job(job, workspace)

    def mutate(*args, **kwargs):
        target.write_text("after\n", encoding="utf-8")
        return "COMPLETED", "observed"

    monkeypatch.setattr(audit_module, "_execute_action", mutate)
    with pytest.raises(ExecutionError) as caught:
        execute_plan(plan)

    assert caught.value.code is ExecutionErrorCode.UNEXPECTED_WORKSPACE_CHANGE
    assert caught.value.path == "target.txt"
    assert caught.value.audit_results[0].name == "read"
    assert caught.value.log_path is not None
    assert "failure_stage: WORKSPACE_COMPARISON" in caught.value.log_path.read_text(
        "utf-8"
    )


def test_audit_action_error_retains_prior_results(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "target.txt"
    target.write_text("value\n", encoding="utf-8")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-PARTIAL",
        kind="audit",
        actions=[
            {"hash": {"path": "target.txt"}},
            {"read": {"path": "target.txt"}},
        ],
    )
    original = audit_module._execute_action
    calls = 0

    def fail_second(plan, name, parameters):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected")
        return original(plan, name, parameters)

    monkeypatch.setattr(audit_module, "_execute_action", fail_second)
    with pytest.raises(ExecutionError) as caught:
        execute_plan(plan_job(job, workspace))

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.item_id == "action_002"
    assert [item.name for item in caught.value.audit_results] == ["hash"]
    assert caught.value.log_path is not None
    assert "failure_stage: AUDIT" in caught.value.log_path.read_text("utf-8")


def test_verify_success_failure_and_workspace_change(
    workspace: Workspace,
) -> None:
    success = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="VERIFY-016",
        kind="verify",
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    result = execute_plan(plan_job(success, workspace), approved=True)
    assert result.status is RunStatus.COMPLETED
    assert [item.status for item in result.initial_checks] == [CheckStatus.PASSED]
    assert result.final_checks == ()
    assert result.workspace_comparison is not None
    assert result.workspace_comparison.success is True

    failed_workspace = _profile_workspace(
        workspace,
        "failure",
        ("{python}", "-c", "import sys; sys.exit(7)"),
    )
    failed = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="VERIFY-FAIL",
        kind="verify",
        checks=[{"profile": {"name": "failure"}}],
    )
    with pytest.raises(ExecutionError) as caught:
        execute_plan(plan_job(failed, failed_workspace), approved=True)
    assert caught.value.code is ExecutionErrorCode.CHECK_FAILED
    assert caught.value.check_results[0].status is CheckStatus.FAILED
    assert caught.value.log_path is not None

    mutating_workspace = _profile_workspace(
        workspace,
        "mutate",
        (
            "{python}",
            "-c",
            "from pathlib import Path; Path('side.txt').write_text('side')",
        ),
    )
    mutating = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="VERIFY-MUTATE",
        kind="verify",
        checks=[{"profile": {"name": "mutate"}}],
    )
    with pytest.raises(ExecutionError) as changed:
        execute_plan(plan_job(mutating, mutating_workspace), approved=True)
    assert changed.value.code is ExecutionErrorCode.UNEXPECTED_WORKSPACE_CHANGE
    assert changed.value.path == "side.txt"
    assert changed.value.workspace_comparison is not None


def test_kind_approval_staleness_and_inventory_failures(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-DIRECT",
        kind="audit",
        actions=[{"tree": {"path": "."}}],
    )
    verify = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="VERIFY-DIRECT",
        kind="verify",
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    audit_plan = plan_job(audit, workspace)
    verify_plan = plan_job(verify, workspace)
    with pytest.raises(ExecutionError) as wrong_audit:
        execute_audit_locked(verify_plan)
    assert wrong_audit.value.code is ExecutionErrorCode.JOB_KIND_UNSUPPORTED
    with pytest.raises(ExecutionError) as wrong_verify:
        execute_verification_locked(audit_plan)
    assert wrong_verify.value.code is ExecutionErrorCode.JOB_KIND_UNSUPPORTED
    with pytest.raises(ExecutionError) as approval:
        execute_plan(verify_plan)
    assert approval.value.code is ExecutionErrorCode.APPROVAL_REQUIRED

    monkeypatch.setattr(
        audit_module,
        "plan_job",
        lambda *args, **kwargs: replace(audit_plan, protected_paths_passed=False),
    )
    with pytest.raises(ExecutionError) as stale:
        execute_audit_locked(audit_plan)
    assert stale.value.code is ExecutionErrorCode.PLAN_STALE

    real_verification_plan_job = verification_module.plan_job
    monkeypatch.setattr(
        verification_module,
        "plan_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("injected")),
    )
    with pytest.raises(ExecutionError) as verify_stale_error:
        verification_module._revalidate_plan(verify_plan)
    assert verify_stale_error.value.code is ExecutionErrorCode.PLAN_STALE
    monkeypatch.setattr(
        verification_module,
        "plan_job",
        lambda *args, **kwargs: replace(verify_plan, protected_paths_passed=False),
    )
    with pytest.raises(ExecutionError) as verify_stale_plan:
        verification_module._revalidate_plan(verify_plan)
    assert verify_stale_plan.value.code is ExecutionErrorCode.PLAN_STALE
    monkeypatch.setattr(
        verification_module,
        "plan_job",
        real_verification_plan_job,
    )
    monkeypatch.setattr(
        verification_module,
        "capture_inventory",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            InventoryError(
                InventoryErrorCode.INSPECTION_FAILED,
                "injected",
            )
        ),
    )
    with pytest.raises(ExecutionError) as inventory:
        execute_verification_locked(verify_plan)
    assert inventory.value.code is ExecutionErrorCode.WORKSPACE_INVENTORY_FAILED


def test_audit_helpers_cover_limits_types_encodings_and_statuses(
    workspace: Workspace,
) -> None:
    assert (
        AuditActionResult(
            "a",
            "read",
            "COMPLETED",
            (),
            "now",
            0,
            "",
        ).success
        is True
    )
    assert AuditActionResult("a", "read", "FAILED", (), "now", 0, "").success is False
    assert audit_module._decode_text(b"plain") == ("utf-8", "plain")
    assert audit_module._decode_text(b"\xef\xbb\xbfplain") == (
        "utf-8-sig",
        "plain",
    )
    assert audit_module._decode_text(b"\xff\xfep\x00") == (
        "utf-16-le",
        "p",
    )
    assert audit_module._decode_text(b"\xfe\xff\x00p") == (
        "utf-16-be",
        "p",
    )
    assert audit_module._decode_text(b"\xff\xfe\x00\x00p\x00\x00\x00") == (
        "utf-32-le",
        "p",
    )
    assert audit_module._decode_text(b"\x00\x00\xfe\xff\x00\x00\x00p") == (
        "utf-32-be",
        "p",
    )
    with pytest.raises(ValueError):
        audit_module._decode_text(b"\x00")
    with pytest.raises(ValueError):
        audit_module._decode_text(b"\x01")

    assert audit_module._newline_style("a\r\nb") == "crlf"
    assert audit_module._newline_style("a\nb") == "lf"
    assert audit_module._newline_style("a\rb") == "mixed_or_cr"
    assert audit_module._newline_style("a\r\nb\nc") == "mixed_or_cr"
    assert audit_module._newline_style("a") == "none"
    assert audit_module._mode_name(stat.S_IFREG) == "file"
    assert audit_module._mode_name(stat.S_IFDIR) == "directory"
    assert audit_module._mode_name(stat.S_IFLNK) == "symlink"
    assert audit_module._mode_name(stat.S_IFIFO) == "other"
    assert audit_module._display(PurePosixPath()) == "."
    assert audit_module._display(PurePosixPath("src")) == "src"
    assert audit_module._is_binary_control("\x7f") is True
    assert audit_module._is_binary_control("\n") is False

    directory = workspace.root / "directory"
    directory.mkdir()
    target = audit_module.Policy(workspace).resolve("directory")
    with pytest.raises(OSError, match="not a regular file"):
        audit_module._read_regular_file(
            SimpleNamespace(workspace=workspace),
            target,
        )


def test_audit_search_find_file_info_and_read_edge_cases(
    workspace: Workspace,
) -> None:
    (workspace.root / "one.txt").write_text("hit\nhit\n", encoding="utf-8")
    (workspace.root / "two.txt").write_bytes(b"binary\x00")
    (workspace.root / "folder").mkdir()
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-EDGES",
        kind="audit",
        actions=[
            {"search": {"path": "one.txt", "text": "hit", "max_results": 1}},
            {"find_files": {"path": ".", "glob": "*.txt", "max_results": 1}},
            {"file_info": {"path": "folder"}},
            {"file_info": {"path": "two.txt"}},
            {"read": {"path": "one.txt", "start_line": 20}},
        ],
    )

    result = execute_plan(plan_job(job, workspace))

    assert "result_limit_reached: true" in result.audit_results[0].output
    assert "result_limit_reached: true" in result.audit_results[1].output
    assert "type: directory" in result.audit_results[2].output
    assert "encoding: binary_or_unsupported" in result.audit_results[3].output
    assert "[NO LINES]" in result.audit_results[4].output


def test_audit_git_and_tool_adapters(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / ".git").mkdir()
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-GIT",
        kind="audit",
        actions=[{"git_status": {}}],
    )
    plan = plan_job(job, workspace)
    monkeypatch.setattr(audit_module.shutil, "which", lambda name: f"/{name}")

    def process(
        status=ProcessStatus.PASSED,
        *,
        stdout="## main\n",
        stderr="",
        truncated=False,
    ):
        return ProcessResult(
            status=status,
            return_code=0 if status is ProcessStatus.PASSED else 1,
            duration_ms=1,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=truncated,
            stderr_truncated=False,
        )

    monkeypatch.setattr(audit_module, "run_process", lambda *a, **k: process())
    assert audit_module._git_status(plan) == ("COMPLETED", "## main")
    monkeypatch.setattr(
        audit_module,
        "run_process",
        lambda *a, **k: process(stdout="", truncated=True),
    )
    status, output = audit_module._git_status(plan)
    assert status == "COMPLETED"
    assert "[CLEAN WORKTREE]" in output
    assert "[TRUNCATED BY PATCHSHUTTLE]" in output
    monkeypatch.setattr(
        audit_module,
        "run_process",
        lambda *a, **k: process(ProcessStatus.FAILED, stderr="failed"),
    )
    with pytest.raises(OSError, match="failed"):
        audit_module._git_status(plan)
    assert audit_module._tool_version("git") == "NOT_AVAILABLE"
    monkeypatch.setattr(
        audit_module,
        "run_process",
        lambda *a, **k: process(stdout="git 1\n"),
    )
    assert audit_module._tool_version("git") == "git 1"
    monkeypatch.setattr(
        audit_module,
        "run_process",
        lambda *a, **k: process(stdout="", stderr=""),
    )
    assert audit_module._tool_version("git") == "AVAILABLE"
    monkeypatch.setattr(
        audit_module,
        "run_process",
        lambda *a, **k: process(ProcessStatus.FAILED),
    )
    assert audit_module._tool_version("git") == "NOT_AVAILABLE"
    monkeypatch.setattr(audit_module.shutil, "which", lambda name: None)
    assert audit_module._tool_version("git") == "NOT_AVAILABLE"
    assert audit_module._package_version("package-that-does-not-exist") == (
        "NOT_AVAILABLE"
    )
    monkeypatch.setattr(
        audit_module,
        "_package_version",
        lambda name: f"{name}-version",
    )
    assert "ruff: ruff-version" in audit_module._environment(plan)


def test_audit_internal_failures_are_mapped(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "target.txt"
    target.write_text("value\n", encoding="utf-8")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-INTERNAL",
        kind="audit",
        actions=[{"read": {"path": "target.txt"}}],
    )
    plan = plan_job(job, workspace)
    real_execute = audit_module._execute_action
    existing = ExecutionError(ExecutionErrorCode.ACTION_FAILED, "injected")
    monkeypatch.setattr(
        audit_module,
        "_execute_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(existing),
    )
    with pytest.raises(ExecutionError) as caught:
        execute_audit_locked(plan)
    assert caught.value is existing
    assert caught.value.item_id == "action_001"
    assert caught.value.path == "target.txt"

    monkeypatch.setattr(
        audit_module,
        "plan_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("injected")),
    )
    with pytest.raises(ExecutionError) as stale:
        audit_module._revalidate_plan(plan)
    assert stale.value.code is ExecutionErrorCode.PLAN_STALE

    monkeypatch.setattr(
        audit_module,
        "capture_inventory",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            InventoryError(
                InventoryErrorCode.INSPECTION_FAILED,
                "injected",
                path=PurePosixPath("target.txt"),
            )
        ),
    )
    with pytest.raises(ExecutionError) as inventory:
        audit_module._capture_inventory(plan)
    assert inventory.value.path == "target.txt"

    with pytest.raises(ExecutionError) as unsupported:
        real_execute(plan, "unsupported", SimpleNamespace())
    assert unsupported.value.code is ExecutionErrorCode.ACTION_UNSUPPORTED


def test_audit_walk_and_regular_read_failures(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "a.txt").write_text("a\n", encoding="utf-8")
    (workspace.root / "b.txt").write_text("b\n", encoding="utf-8")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-WALK",
        kind="audit",
        actions=[{"tree": {"path": "."}}],
    )
    plan = plan_job(job, workspace)
    policy = audit_module.Policy(workspace)
    root = policy.resolve(".", allow_root=True)
    execution = workspace.config.execution.model_copy(
        update={"max_inventory_entries": 1}
    )
    limited_workspace = replace(
        workspace,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    with pytest.raises(OSError, match="entry limit"):
        audit_module._walk(
            replace(plan, workspace=limited_workspace),
            root,
            maximum_depth=1,
            include_hidden=True,
        )

    monkeypatch.setattr(
        audit_module.os,
        "scandir",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("scan")),
    )
    with pytest.raises(OSError, match="could not inspect"):
        audit_module._walk(plan, root, maximum_depth=1, include_hidden=True)

    monkeypatch.undo()

    class BadEntry:
        name = "bad.txt"

        def stat(self, *, follow_symlinks: bool):
            raise OSError("injected")

    class BadScan:
        def __enter__(self):
            return iter((BadEntry(),))

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(audit_module.os, "scandir", lambda path: BadScan())
    with pytest.raises(OSError, match="bad.txt"):
        audit_module._walk(plan, root, maximum_depth=1, include_hidden=True)

    monkeypatch.undo()
    target = policy.resolve("a.txt")
    execution = workspace.config.execution.model_copy(
        update={"max_single_file_bytes": 1}
    )
    limited_workspace = replace(
        workspace,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    with pytest.raises(OSError, match="size limit"):
        audit_module._read_regular_file(
            SimpleNamespace(workspace=limited_workspace),
            target,
        )
    real_read = Path.read_bytes

    def mutate(path: Path) -> bytes:
        raw = real_read(path)
        if path == target.absolute:
            path.write_text("changed\n", encoding="utf-8")
        return raw

    monkeypatch.setattr(Path, "read_bytes", mutate)
    with pytest.raises(OSError, match="changed"):
        audit_module._read_regular_file(SimpleNamespace(workspace=workspace), target)


def test_audit_file_candidate_revalidation_can_drop_a_raced_type(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_path = workspace.root / "target.txt"
    target_path.write_text("value\n", encoding="utf-8")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-CANDIDATE",
        kind="audit",
        actions=[{"search": {"path": "target.txt", "text": "value"}}],
    )
    plan = plan_job(job, workspace)
    root = audit_module.Policy(workspace).resolve("target.txt")

    class RacedPolicy:
        def __init__(self, workspace):
            pass

        def resolve(self, path):
            return audit_module.WorkspacePath(
                relative=path,
                absolute=workspace.root,
                kind=PathKind.DIRECTORY,
            )

    monkeypatch.setattr(audit_module, "Policy", RacedPolicy)
    assert audit_module._audit_files(plan, root, glob=None) == ()


def test_audit_redacted_cwd_variants(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: workspace.root))
    assert audit_module._redacted_cwd(workspace.root) == "~"
    nested = workspace.root / "nested"
    nested.mkdir()
    assert audit_module._redacted_cwd(nested) == "~/nested"
    outside = Path("/definitely/outside/home")
    assert audit_module._redacted_cwd(outside) == outside.as_posix()


def _profile_workspace(
    workspace: Workspace,
    name: str,
    argv: tuple[str, ...],
) -> Workspace:
    profiles = dict(workspace.config.checks.profiles)
    profiles[name] = CheckProfileSettings(
        argv=argv,
        timeout_seconds=30,
        allow_job_args=False,
    )
    checks = workspace.config.checks.model_copy(update={"profiles": profiles})
    return replace(
        workspace,
        config=workspace.config.model_copy(update={"checks": checks}),
    )

"""CLI contracts added for Phase 16-19 operations."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
import yaml
from click.testing import CliRunner

import patchshuttle.backup as backup_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job, execute_plan, plan_job
from patchshuttle.cli import main
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.workspace import init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"


@pytest.fixture
def initialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(workspace_module, "generate_project_id", lambda: PROJECT_ID)
    monkeypatch.setattr(
        backup_module,
        "_run_timestamp",
        lambda: "2026_08_13_231500_000001",
    )
    workspace = init_workspace(tmp_path).workspace
    monkeypatch.chdir(tmp_path)
    return workspace


def _write_job(root: Path, payload: dict, name: str) -> Path:
    path = root / "patches/inbox" / name
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_audit_and_verify_kind_specific_commands(initialized) -> None:
    workspace = initialized
    audit_path = _write_job(
        workspace.root,
        {
            "protocol": 1,
            "project_id": PROJECT_ID,
            "id": "AUDIT-CLI-019",
            "kind": "audit",
            "actions": [{"tree": {"path": "."}}],
        },
        "AUDIT-CLI-019.psh.yaml",
    )
    audit = CliRunner().invoke(main, ["audit", str(audit_path)])
    assert audit.exit_code == 0
    assert "audit_results: 1" in audit.stdout

    verify_path = _write_job(
        workspace.root,
        {
            "protocol": 1,
            "project_id": PROJECT_ID,
            "id": "VERIFY-CLI-019",
            "kind": "verify",
            "checks": [{"import_check": {"modules": ["json"]}}],
        },
        "VERIFY-CLI-019.psh.yaml",
    )
    declined = CliRunner().invoke(main, ["verify", str(verify_path)], input="n\n")
    assert declined.exit_code == 4
    assert "VERIFY_FAILED [USER_DECLINED]" in declined.stderr
    verified = CliRunner().invoke(main, ["verify", str(verify_path), "--yes"])
    assert verified.exit_code == 0
    assert "initial_checks: 1" in verified.stdout

    wrong_audit = CliRunner().invoke(main, ["audit", str(verify_path)])
    assert wrong_audit.exit_code == 5
    assert "AUDIT_FAILED [JOB_KIND_UNSUPPORTED]" in wrong_audit.stderr
    wrong_verify = CliRunner().invoke(main, ["verify", str(audit_path), "--yes"])
    assert wrong_verify.exit_code == 5
    assert "VERIFY_FAILED [JOB_KIND_UNSUPPORTED]" in wrong_verify.stderr


def test_snapshot_and_handoff_cli_error_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initialized,
) -> None:
    workspace = initialized
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    snapshot_workspace = CliRunner().invoke(main, ["snapshot"])
    handoff_workspace = CliRunner().invoke(main, ["handoff"])
    assert snapshot_workspace.exit_code == 3
    assert "SNAPSHOT_FAILED" in snapshot_workspace.stderr
    assert handoff_workspace.exit_code == 3
    assert "HANDOFF_FAILED" in handoff_workspace.stderr

    monkeypatch.chdir(workspace.root)
    failure = ExecutionError(
        ExecutionErrorCode.WORKSPACE_INVENTORY_FAILED,
        "injected",
    )
    monkeypatch.setattr(
        "patchshuttle.cli.create_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(
        "patchshuttle.cli.create_handoff",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )
    snapshot_failure = CliRunner().invoke(main, ["snapshot"])
    handoff_failure = CliRunner().invoke(main, ["handoff"])
    assert snapshot_failure.exit_code == 5
    assert "SNAPSHOT_FAILED [WORKSPACE_INVENTORY_FAILED]" in (snapshot_failure.stderr)
    assert handoff_failure.exit_code == 5
    assert "HANDOFF_FAILED [WORKSPACE_INVENTORY_FAILED]" in handoff_failure.stderr


def test_rollback_cli_success_decline_and_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initialized,
) -> None:
    workspace = initialized
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-CLI-ROLLBACK",
        kind="patch",
        actions=[
            {
                "replace_exact": {
                    "path": "existing.txt",
                    "old": "before",
                    "new": "after",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    execute_plan(plan_job(job, workspace), approved=True)

    declined = CliRunner().invoke(
        main,
        ["rollback", job.id],
        input="n\n",
    )
    assert declined.exit_code == 4
    assert "ROLLBACK_FAILED [USER_DECLINED]" in declined.stderr
    with monkeypatch.context() as scoped:
        scoped.setattr(
            "patchshuttle.cli.click.confirm",
            lambda *args, **kwargs: (_ for _ in ()).throw(click.Abort()),
        )
        aborted = CliRunner().invoke(main, ["rollback", job.id])
    assert aborted.exit_code == 4
    assert "ROLLBACK_FAILED [USER_DECLINED]" in aborted.stderr
    rolled = CliRunner().invoke(main, ["rollback", job.id, "--yes"])
    assert rolled.exit_code == 0
    assert rolled.stdout.startswith("ROLLED_BACK\n")
    assert "restored_files: 1" in rolled.stdout

    missing = CliRunner().invoke(main, ["rollback", "PATCH-MISSING", "--yes"])
    assert missing.exit_code == 3
    assert "ROLLBACK_FAILED [JOB_NOT_FOUND]" in missing.stderr

    outside = tmp_path.parent / f"{tmp_path.name}-not-initialized"
    outside.mkdir()
    monkeypatch.chdir(outside)
    workspace_error = CliRunner().invoke(
        main,
        ["rollback", "PATCH-MISSING", "--yes"],
    )
    assert workspace_error.exit_code == 3
    assert "ROLLBACK_FAILED [WORKSPACE_NOT_INITIALIZED]" in workspace_error.stderr

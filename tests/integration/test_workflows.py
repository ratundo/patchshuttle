"""Integration coverage for complete public API workflows."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from patchshuttle import (
    Job,
    create_handoff,
    create_snapshot,
    execute_plan,
    init_workspace,
    load_job,
    plan_job,
    rollback_job,
)
from patchshuttle.actions import hash, replace_exact, search, tree
from patchshuttle.checks import (
    compileall,
    django_check,
    django_migrations_check,
    django_test,
)
from patchshuttle.checks import pytest as pytest_check
from patchshuttle.errors import ExecutionError, ExecutionErrorCode


def test_existing_project_api_yaml_audit_patch_verify_and_rollback(
    tmp_path: Path,
) -> None:
    target = tmp_path / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    workspace = init_workspace(tmp_path).workspace

    audit = Job(
        protocol=1,
        project_id=workspace.project_id,
        id="INTEGRATION-AUDIT",
        kind="audit",
        actions=(
            tree("."),
            search("VALUE", path="module.py"),
            hash("module.py"),
        ),
    )
    audited = execute_plan(plan_job(audit, workspace))
    assert [result.status for result in audited.audit_results] == [
        "COMPLETED",
        "COMPLETED",
        "COMPLETED",
    ]
    assert audited.workspace_comparison is not None
    assert audited.workspace_comparison.unexpected_changes == ()

    payload = {
        "protocol": 1,
        "project_id": workspace.project_id,
        "id": "INTEGRATION-PATCH",
        "kind": "patch",
        "actions": [
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
        "checks": [{"compileall": {"paths": ["module.py"]}}],
    }
    source = workspace.patches_dir / "inbox/INTEGRATION-PATCH.psh.yaml"
    source.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    loaded = load_job(source)
    constructed = Job(
        protocol=1,
        project_id=workspace.project_id,
        id="INTEGRATION-PATCH",
        kind="patch",
        actions=(replace_exact("module.py", "VALUE = 1", "VALUE = 2"),),
        checks=(compileall(("module.py",)),),
    )
    assert loaded == constructed
    assert plan_job(loaded, workspace) == plan_job(constructed, workspace)

    patched = execute_plan(
        plan_job(loaded, workspace),
        approved=True,
        source_path=source,
    )
    assert patched.status.value == "COMPLETED"
    assert target.read_text("utf-8") == "VALUE = 2\n"

    verify = Job(
        protocol=1,
        project_id=workspace.project_id,
        id="INTEGRATION-VERIFY",
        kind="verify",
        checks=(compileall(("module.py",)),),
    )
    verified = execute_plan(plan_job(verify, workspace), approved=True)
    assert verified.status.value == "COMPLETED"
    assert verified.workspace_comparison is not None
    assert verified.workspace_comparison.unexpected_changes == ()
    assert create_snapshot(workspace).path.is_file()
    assert create_handoff(workspace).path.is_file()

    rolled_back = rollback_job(workspace, loaded.id, approved=True)
    assert rolled_back.restored_files == (Path("module.py"),)
    assert target.read_text("utf-8") == "VALUE = 1\n"


def test_real_failed_pytest_check_rolls_back_a_multifile_patch(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path).workspace
    job = Job(
        protocol=1,
        project_id=workspace.project_id,
        id="INTEGRATION-FAIL",
        kind="patch",
        actions=(
            {
                "create_file": {
                    "path": "value.py",
                    "content": "VALUE = 1\n",
                }
            },
            {
                "create_file": {
                    "path": "test_value.py",
                    "content": "def test_value():\n    assert False\n",
                }
            },
        ),
        checks=(pytest_check(("test_value.py",), args=("-q",)),),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_plan(plan_job(job, workspace), approved=True)

    assert caught.value.code is ExecutionErrorCode.CHECK_FAILED
    assert caught.value.rollback_succeeded is True
    assert not (tmp_path / "value.py").exists()
    assert not (tmp_path / "test_value.py").exists()
    assert caught.value.log_path is not None
    assert caught.value.log_path.is_file()


def test_real_django_verify_sequence(tmp_path: Path) -> None:
    (tmp_path / "sample").mkdir()
    (tmp_path / "manage.py").write_text(
        """\
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sample.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
""",
        encoding="utf-8",
    )
    (tmp_path / "sample/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "sample/settings.py").write_text(
        """\
SECRET_KEY = "integration-only"
INSTALLED_APPS = []
MIDDLEWARE = []
ROOT_URLCONF = "sample.urls"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
""",
        encoding="utf-8",
    )
    (tmp_path / "sample/urls.py").write_text(
        "urlpatterns = []\n",
        encoding="utf-8",
    )
    workspace = init_workspace(tmp_path).workspace
    job = Job(
        protocol=1,
        project_id=workspace.project_id,
        id="INTEGRATION-DJANGO",
        kind="verify",
        checks=(
            django_check(),
            django_migrations_check(),
            django_test(),
        ),
    )

    result = execute_plan(plan_job(job, workspace), approved=True)

    assert result.status.value == "COMPLETED"
    assert [check.status.value for check in result.initial_checks] == [
        "PASSED",
        "PASSED",
        "PASSED",
    ]
    assert result.workspace_comparison is not None
    assert result.workspace_comparison.unexpected_changes == ()

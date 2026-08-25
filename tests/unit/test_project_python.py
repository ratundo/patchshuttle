from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from patchshuttle._ai_log import summarize_ai_log
from patchshuttle.checks.runner import _prepare_check, prepare_checks
from patchshuttle.cli import _render_plan
from patchshuttle.config import (
    CheckProfileSettings,
    ChecksSettings,
    ExecutionSettings,
    PatchShuttleConfig,
    ProjectOrigin,
    ProjectSettings,
    load_config,
    render_default_config,
)
from patchshuttle.errors import PlanningError, PlanningErrorCode
from patchshuttle.logging import _plan_section
from patchshuttle.models import Job
from patchshuttle.planner import PlannedCheck, plan_job
from patchshuttle.project_python import (
    ProjectPythonError,
    resolve_project_python,
)
from patchshuttle.workspace import Workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"


def _workspace(
    root: Path,
    *,
    python_executable: str | None,
    profiles: dict[str, CheckProfileSettings] | None = None,
) -> Workspace:
    config = PatchShuttleConfig(
        project=ProjectSettings(
            project_id=PROJECT_ID,
            origin=ProjectOrigin.EXISTING,
        ),
        execution=ExecutionSettings(python_executable=python_executable),
        checks=ChecksSettings(profiles=profiles or {}),
    )
    return Workspace(root=root, config=config)


def _job(checks: list[dict]) -> Job:
    return Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="VERIFY-PROJECT-PYTHON-001",
        kind="verify",
        actions=[],
        checks=checks,
    )


@pytest.mark.parametrize("absolute", (False, True))
def test_resolve_project_python_accepts_relative_and_absolute_paths(
    tmp_path: Path,
    absolute: bool,
) -> None:
    target = tmp_path / "tools" / "project-python"
    target.parent.mkdir()
    target.write_text("placeholder\n", encoding="utf-8")
    configured = str(target) if absolute else "tools/project-python"

    workspace = _workspace(tmp_path, python_executable=configured)

    assert resolve_project_python(workspace) == target.resolve(strict=True)


def test_omitted_project_python_preserves_current_interpreter(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, python_executable=None)

    assert resolve_project_python(workspace) == Path(sys.executable)


def test_missing_project_python_is_rejected_without_execution(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, python_executable="missing/python")

    with pytest.raises(ProjectPythonError) as caught:
        resolve_project_python(workspace)

    assert caught.value.path == "missing/python"


def test_config_loads_explicit_project_python_and_keeps_default_optional(
    tmp_path: Path,
) -> None:
    default_text = render_default_config(PROJECT_ID, ProjectOrigin.EXISTING)
    default_path = tmp_path / "default.toml"
    default_path.write_text(default_text, encoding="utf-8")
    explicit_path = tmp_path / "explicit.toml"
    explicit_path.write_text(
        default_text.replace(
            "[execution]\n",
            '[execution]\npython_executable = "tools/python"\n',
            1,
        ),
        encoding="utf-8",
    )

    assert load_config(default_path).execution.python_executable is None
    assert load_config(explicit_path).execution.python_executable == "tools/python"


def test_selected_python_drives_all_python_project_checks_and_views(
    tmp_path: Path,
) -> None:
    target = tmp_path / "environment" / "python"
    target.parent.mkdir()
    target.write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "manage.py").write_text("pass\n", encoding="utf-8")
    profiles = {
        "project_profile": CheckProfileSettings(
            argv=("{python}", "-c", "print('local')")
        )
    }
    workspace = _workspace(
        tmp_path,
        python_executable="environment/python",
        profiles=profiles,
    )
    job = _job(
        [
            {"compileall": {"paths": ["src"]}},
            {"pytest": {"paths": ["tests"], "args": ["-q"]}},
            {"unittest": {"discover": "tests", "pattern": "test_*.py"}},
            {"django_check": {"manage_py": "manage.py"}},
            {"django_migrations_check": {"manage_py": "manage.py"}},
            {
                "django_test": {
                    "manage_py": "manage.py",
                    "labels": ["clients.tests"],
                }
            },
            {
                "django_import_check": {
                    "manage_py": "manage.py",
                    "modules": ["clients.models"],
                }
            },
            {"import_check": {"modules": ["json"]}},
            {"profile": {"name": "project_profile"}},
        ]
    )

    plan = plan_job(job, workspace)
    prepared = prepare_checks(plan)
    selected = target.resolve(strict=True)

    assert plan.project_python == selected
    assert all(check.argv[0] == str(selected) for check in prepared)
    assert f"project_python: {selected.as_posix()}\n" in _render_plan(plan)
    assert f"project_python: {selected.as_posix()}\n" in _plan_section(plan)
    payload = summarize_ai_log(
        "=== PLAN ===\n"
        f"project_python: {selected.as_posix()}\n"
        "=== SUMMARY ===\n"
        "result: COMPLETED\n"
        "=== PATCHSHUTTLE_AI_HANDOFF ===\n"
        "protocol: 1\n",
        source="patches/logs/example.log",
    )
    assert payload["plan"]["project_python"] == selected.as_posix()


def test_planning_rejects_a_missing_selected_python(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    workspace = _workspace(
        tmp_path,
        python_executable="missing/project-python",
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(
            _job([{"compileall": {"paths": ["src"]}}]),
            workspace,
        )

    assert caught.value.code is PlanningErrorCode.DEPENDENCY_NOT_AVAILABLE
    assert caught.value.item_id == "check_001"


def test_ruff_and_non_python_profiles_ignore_project_python(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path,
        python_executable="missing/project-python",
        profiles={"direct": CheckProfileSettings(argv=(sys.executable, "-V"))},
    )
    ruff = _prepare_check(
        SimpleNamespace(workspace=workspace),
        PlannedCheck(
            id="check_001",
            name="ruff",
            paths=(PurePosixPath("module.py"),),
        ),
        SimpleNamespace(),
    )
    profile_plan = plan_job(_job([{"profile": {"name": "direct"}}]), workspace)
    direct = prepare_checks(profile_plan)[0]

    assert ruff.argv[0] == sys.executable
    assert direct.argv == (sys.executable, "-V")
    assert profile_plan.project_python is None

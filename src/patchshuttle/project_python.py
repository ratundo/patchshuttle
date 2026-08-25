"""Owner-controlled Python interpreter selection for project checks."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchshuttle.models import Job
    from patchshuttle.workspace import Workspace


_PROJECT_PYTHON_CHECKS = frozenset(
    {
        "compileall",
        "pytest",
        "unittest",
        "django_check",
        "django_migrations_check",
        "django_test",
        "django_import_check",
        "import_check",
    }
)


class ProjectPythonError(ValueError):
    """The effective project interpreter is not an existing regular file."""

    def __init__(self, path: str) -> None:
        super().__init__(
            "effective project Python executable is not an existing regular file"
        )
        self.path = path


def resolve_project_python(workspace: Workspace) -> Path:
    """Resolve the owner-selected project interpreter without executing it."""

    configured = workspace.config.execution.python_executable
    raw_path = configured if configured is not None else sys.executable
    candidate = Path(raw_path)
    if configured is None:
        execution_path = candidate
    else:
        if not candidate.is_absolute():
            candidate = workspace.root / candidate
        execution_path = Path(os.path.abspath(candidate))
    try:
        resolved_target = execution_path.resolve(strict=True)
        metadata = resolved_target.stat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectPythonError(raw_path) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ProjectPythonError(raw_path)
    return execution_path


def project_python_for_job(workspace: Workspace, job: Job) -> Path | None:
    """Return the effective interpreter only when the job will use it."""

    for check in job.checks:
        if check.name in _PROJECT_PYTHON_CHECKS:
            return resolve_project_python(workspace)
        if check.name == "profile":
            profile = workspace.config.checks.profiles.get(check.parameters.name)
            if profile is not None and "{python}" in profile.argv:
                return resolve_project_python(workspace)
    return None


__all__ = [
    "ProjectPythonError",
    "project_python_for_job",
    "resolve_project_python",
]

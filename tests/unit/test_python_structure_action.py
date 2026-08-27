"""Integration contracts for the bounded Python structure audit action."""

from __future__ import annotations

from pathlib import Path

import pytest

import patchshuttle.workspace as workspace_module
from patchshuttle import Job, RunStatus, execute_plan
from patchshuttle._python_discovery import evaluate_python_discovery
from patchshuttle.errors import PlanningError, PlanningErrorCode
from patchshuttle.planner import plan_job
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


def test_python_structure_is_bounded_logged_and_read_only(
    workspace: Workspace,
) -> None:
    source = workspace.root / "src"
    source.mkdir()
    (source / "a.py").write_text(
        "import os\n"
        "\n"
        "@route('PRIVATE_ROUTE')\n"
        "class Service:\n"
        "    def run(self, value):\n"
        "        return value\n",
        encoding="utf-8",
    )
    (source / "b.py").write_text(
        "def other():\n    return None\n",
        encoding="utf-8",
    )
    (source / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-PYTHON-STRUCTURE",
        kind="audit",
        actions=[
            {
                "python_structure": {
                    "path": "src",
                    "max_files": 2,
                    "max_symbols": 1,
                }
            },
            {
                "python_structure": {
                    "path": "src/broken.py",
                    "max_files": 1,
                    "max_symbols": 5,
                }
            },
            {
                "python_structure": {
                    "path": "src",
                    "max_files": 2,
                    "max_symbols": 3,
                    "compact": True,
                }
            },
        ],
    )

    result = execute_plan(plan_job(job, workspace))

    assert result.status is RunStatus.COMPLETED
    assert result.workspace_comparison is not None
    assert result.workspace_comparison.success is True
    first, second, compact = (item.output for item in result.audit_results)
    assert "schema: patchshuttle.python_structure_collection.v1" in first
    assert "schema: patchshuttle.python_structure.v1" in first
    assert "files_considered: 2" in first
    assert "files_parsed: 2" in first
    assert "parse_errors: 0" in first
    assert "imports_reported: 1" in first
    assert "symbols_available: 3" in first
    assert "symbols_reported: 1" in first
    assert "file_limit_reached: true" in first
    assert "symbol_limit_reached: true" in first
    assert '"qualified_name":"Service"' in first
    assert '"qualified_name":"Service.run"' not in first
    assert "import: " in first
    assert "PRIVATE_ROUTE" not in first
    assert "files_parsed: 0" in second
    assert "parse_errors: 1" in second
    assert "parse_error: src/broken.py" in second
    assert "schema: patchshuttle.python_structure_collection.compact.v1" in compact
    assert "schema: patchshuttle.python_structure.compact.v1" in compact
    assert "imports_reported: 1" in compact
    assert '"kind":"class"' in compact
    assert '"qualified_name":"Service"' in compact
    assert '"lines":[3,6]' in compact
    assert "import: " not in compact
    assert '"parameters"' not in compact
    assert '"decorators"' not in compact
    assert '"bases"' not in compact

    evaluation = evaluate_python_discovery(job, audit_results=result.audit_results)
    assert evaluation.python_structure_actions == 3
    assert evaluation.python_audit_duration_ms == sum(
        item.duration_ms for item in result.audit_results
    )
    assert evaluation.reason_codes == ("PYTHON_STRUCTURE_DISCOVERY_USED",)
    assert result.log_path is not None
    log = result.log_path.read_text("utf-8")
    assert "python_structure_actions: 3" in log
    assert "PYTHON_STRUCTURE_DISCOVERY_USED" in log


def test_python_structure_planner_accepts_python_files_and_rejects_other_files(
    workspace: Workspace,
) -> None:
    (workspace.root / "valid.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace.root / "notes.txt").write_text("text\n", encoding="utf-8")
    valid = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-PYTHON-STRUCTURE-FILE",
        kind="audit",
        actions=[{"python_structure": {"path": "valid.py"}}],
    )
    invalid = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-PYTHON-STRUCTURE-NON-PYTHON",
        kind="audit",
        actions=[{"python_structure": {"path": "notes.txt"}}],
    )

    planned = plan_job(valid, workspace)

    assert planned.actions[0].paths[0].as_posix() == "valid.py"
    with pytest.raises(PlanningError) as caught:
        plan_job(invalid, workspace)
    assert caught.value.code is PlanningErrorCode.TARGET_TYPE_INVALID
    assert caught.value.path == "notes.txt"

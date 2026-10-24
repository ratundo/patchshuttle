"""Batch guards, ordered simulation, safety restrictions and transaction behavior."""

import json

import pytest
from pydantic import ValidationError

from patchshuttle import Job, execute_plan, plan_job
from patchshuttle.actions import edit_imports, ensure_import
from patchshuttle.errors import ExecutionError, PlanningError
from patchshuttle.planning.imports import inspect_imports
from patchshuttle.selfdoc import render_capabilities, render_explanation, render_schema
from patchshuttle.workspace import init_workspace


def step(operation, statement):
    return dict(operation=operation, statement=statement)


def batch(source, edits, path="a.py", guard=None):
    return edit_imports(
        path, edits, expected_imports_ast_sha256=guard or inspect_imports(source)[2]
    )


def job(ws, actions, checks=None):
    return Job(
        protocol=1,
        project_id=ws.project_id,
        id="PATCH-IMPORT-BATCH",
        kind="patch",
        actions=actions,
        checks=checks or [{"compileall": {"paths": ["a.py"]}}],
    )


def test_batch_execution_and_failed_check_restore_original(tmp_path):
    ws = init_workspace(tmp_path).workspace
    source = "# примітка\nimport os as system\nVALUE = 1\n"
    target = tmp_path / "a.py"
    raw = b"\xef\xbb\xbf" + source.replace("\n", "\r\n").encode("utf-8")
    target.write_bytes(raw)
    action = batch(
        source,
        [step("remove", "import os as system"), step("ensure", "import sys as system")],
    )
    plan = plan_job(
        job(
            ws,
            [action],
            [{"import_check": {"modules": ["patchshuttle_nonexistent_batch_check"]}}],
        ),
        ws,
    )
    assert target.read_bytes() == raw
    assert b"import sys as system\r\n" in plan.file_changes[0].content
    assert plan.file_changes[0].content.startswith(b"\xef\xbb\xbf")
    with pytest.raises(ExecutionError):
        execute_plan(plan, approved=True)
    assert target.read_bytes() == raw
    successful = job(ws, [action]).model_copy(
        update={"id": "PATCH-IMPORT-BATCH-SUCCESS"}
    )
    result = execute_plan(plan_job(successful, ws), approved=True)
    assert result.workspace_comparison.success
    content = target.read_text(encoding="utf-8-sig")
    assert "import sys as system" in content and "import os" not in content
    assert "примітка" in content


@pytest.mark.parametrize(
    "bad", ["import os as system", "import broken(", "from sys import *"]
)
def test_later_step_failure_does_not_apply_earlier_steps(tmp_path, bad):
    ws = init_workspace(tmp_path).workspace
    target = tmp_path / "a.py"
    source = "import sys as system\n"
    target.write_text(source, encoding="utf-8")
    action = batch(source, [step("ensure", "import math"), step("ensure", bad)])
    with pytest.raises(PlanningError, match=r"batch step 2 \(ensure\) failed"):
        plan_job(job(ws, [action]), ws)
    assert target.read_text(encoding="utf-8") == source


def test_order_noop_and_initial_guard_after_prior_action(tmp_path):
    ws = init_workspace(tmp_path).workspace
    target = tmp_path / "a.py"
    source = "import os\n"
    target.write_text(source, encoding="utf-8")
    noop = batch(source, [step("ensure", "import os"), step("remove", "import sys")])
    assert not plan_job(job(ws, [noop]), ws).file_changes
    stale = batch(source, [step("ensure", "import os")], guard="0" * 64)
    with pytest.raises(PlanningError, match="batch initial simulated file"):
        plan_job(job(ws, [stale]), ws)
    prior = ensure_import(
        "a.py", "import sys", expected_imports_ast_sha256=inspect_imports(source)[2]
    )
    with pytest.raises(PlanningError, match="batch initial simulated file"):
        plan_job(job(ws, [prior, noop]), ws)
    current = "import sys\nimport os\n"
    updated = batch(
        current,
        [step("remove", "import sys")],
        guard=inspect_imports(current)[2].upper(),
    )
    assert not plan_job(job(ws, [prior, updated]), ws).file_changes


@pytest.mark.parametrize(
    "source,edits,message",
    [
        ("import os, sys\n", [step("remove", "import os")], "multi-alias"),
        ("import os; VALUE = 1\n", [step("remove", "import os")], "standalone"),
        ("import os\nimport os\n", [step("ensure", "import os")], "ambiguous"),
        (
            "import os as system\n",
            [
                step("ensure", "import sys as system"),
                step("remove", "import os as system"),
            ],
            "binding",
        ),
    ],
)
def test_batch_keeps_single_import_restrictions(tmp_path, source, edits, message):
    ws = init_workspace(tmp_path).workspace
    (tmp_path / "a.py").write_text(source, encoding="utf-8")
    with pytest.raises(PlanningError, match=message):
        plan_job(job(ws, [batch(source, edits)]), ws)


@pytest.mark.parametrize(
    "edits",
    [
        [],
        [step("ensure", "import os")] * 101,
        [step("replace", "import os")],
        [step("ensure", "")],
        [dict(operation="ensure", statement="import os", unexpected=True)],
    ],
)
def test_batch_schema_is_closed_and_bounded(edits):
    with pytest.raises(ValidationError):
        batch("", edits)


def test_batch_discovery_and_read_only_job_rejection(tmp_path):
    ws = init_workspace(tmp_path).workspace
    action = batch("", [step("ensure", "import os")])
    assert action.name == "edit_imports"
    assert "edit_imports" in render_capabilities()
    assert "1-100" in render_explanation("edit_imports")
    schema = json.loads(render_schema())
    assert (
        schema["$defs"]["EditImportsParameters"]["properties"]["edits"]["maxItems"]
        == 100
    )
    payload = job(ws, [action]).model_dump(mode="json")
    payload["kind"] = "audit"
    payload["checks"] = []
    with pytest.raises(ValidationError):
        Job.model_validate(payload)
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    from patchshuttle._python_discovery import evaluate_python_discovery

    result = evaluate_python_discovery(job(ws, [action]))
    assert result.declared_python_import_actions == 1

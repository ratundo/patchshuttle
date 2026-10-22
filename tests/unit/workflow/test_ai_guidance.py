"""Operational guidance remains advisory and reaches actual output surfaces."""

from types import SimpleNamespace

import pytest

from patchshuttle import Job, create_handoff, execute_plan, plan_job
from patchshuttle._python_discovery import evaluate_python_discovery
from patchshuttle.planning.discovery_log import _python_discovery_fields
from patchshuttle.planning.display import _render_plan
from patchshuttle.planning.guidance import symbol_advisories
from patchshuttle.workspace import init_workspace


@pytest.mark.parametrize(
    "text,symbol,failure,expected",
    [
        (1, 0, None, True),
        (1, 1, None, False),
        (0, 0, None, False),
        (1, 1, "TEXT_TARGETING_FAILURE", True),
        (0, 0, "LINE_TARGETING_FAILURE", False),
    ],
)
def test_advisory_conditions(text, symbol, failure, expected):
    evaluation = SimpleNamespace(
        declared_python_text_actions=text,
        declared_python_symbol_actions=symbol,
        failure_signal=failure,
    )
    assert bool(symbol_advisories(evaluation)) is expected


def test_transfer_and_import_telemetry():
    job = Job(
        protocol=1,
        project_id="PSH-8F41C2A73D905E61",
        id="PATCH-COUNTS",
        kind="patch",
        actions=[
            {
                "move_symbol": {
                    "path": "a.py",
                    "symbol": "run",
                    "destination": "b.py",
                    "expected_ast_sha256": "a" * 64,
                    "expected_destination_sha256": "b" * 64,
                }
            },
            {
                "ensure_import": {
                    "path": "b.py",
                    "statement": "import os",
                    "expected_imports_ast_sha256": "c" * 64,
                }
            },
            {
                "move_file": {
                    "path": "c.py",
                    "destination": "d.py",
                    "expected_sha256": "d" * 64,
                }
            },
        ],
    )
    e = evaluate_python_discovery(job)
    assert e.declared_python_symbol_actions == 1
    assert e.declared_python_import_actions == 1
    assert e.declared_python_file_move_actions == 1
    assert "PYTHON_IMPORT_TARGETING_USED" in e.reason_codes
    assert "PYTHON_FILE_MOVE_USED" in e.reason_codes
    assert "declared_python_import_actions: 1" in _python_discovery_fields(e)


@pytest.mark.parametrize("suffix,has_hint", [("py", True), ("txt", False)])
def test_text_advisory_does_not_block_plan_or_execution(tmp_path, suffix, has_hint):
    ws = init_workspace(tmp_path).workspace
    target = tmp_path / ("value." + suffix)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    job = Job(
        protocol=1,
        project_id=ws.project_id,
        id="PATCH-GUIDANCE",
        checks=[{"import_check": {"modules": ["json"]}}],
        kind="patch",
        actions=[
            {
                "replace_exact": {
                    "path": target.name,
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
    )
    plan = plan_job(job, ws)
    assert ("PYTHON_SYMBOL_ACTION_AVAILABLE" in _render_plan(plan)) is has_hint
    assert (
        "PYTHON_SYMBOL_ACTION_AVAILABLE"
        in _python_discovery_fields(evaluate_python_discovery(job))
    ) is has_hint
    result = execute_plan(plan, approved=True)
    from patchshuttle._ai_log import render_ai_log

    log = result.log_path.read_text()
    assert ("PYTHON_SYMBOL_ACTION_AVAILABLE" in log) is has_hint
    assert (
        "PYTHON_SYMBOL_ACTION_AVAILABLE"
        in render_ai_log(log, source="test", json_output=True)
    ) is has_hint
    assert (
        "PYTHON_SYMBOL_ACTION_AVAILABLE"
        in render_ai_log(log, source="test", json_output=False)
    ) is has_hint
    assert result.workspace_comparison.success
    assert "VALUE = 2" in target.read_text()
    handoff = create_handoff(ws)
    # The generated handoff contains guidance even after a non-Python edit.
    handoff_text = handoff.path.read_text()
    assert "prefer read_symbol + replace_symbol" in handoff_text
    assert "expected_ast_sha256:" in handoff_text
    assert "MCP history when available" in handoff_text

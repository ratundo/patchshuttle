"""Structural guards accept formatting changes but reject semantic drift."""

import sys

import pytest
from pydantic import ValidationError

from patchshuttle import Job, execute_plan
from patchshuttle._python_symbols import find_symbol_source
from patchshuttle.errors import PlanningError, PlanningErrorCode
from patchshuttle.models import ReplaceSymbolParameters
from patchshuttle.planner import plan_job
from patchshuttle.workspace import init_workspace


def digest(text, symbol="run"):
    return find_symbol_source(text, symbol, filename="module.py").ast_sha256


def job(workspace, replacement, guard, **extra):
    return Job(
        protocol=1,
        project_id=workspace.project_id,
        id="PATCH-AST-001",
        kind="patch",
        actions=[
            {
                "replace_symbol": dict(
                    path="module.py",
                    symbol="run",
                    expected_ast_sha256=guard,
                    new_content=replacement,
                    **extra,
                )
            }
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )


def test_structural_digest_ignores_formatting_but_preserves_semantics():
    original = "def run(a=1):\n    return a + 2\n"
    assert digest(original) == digest(
        "def run(\n a = 1,\n):\n # comment\n return (a+2)\n"
    )
    assert digest(original) != digest("def run(a=1):\n    return a + 3\n")
    assert digest(original) != digest("@decorator\ndef run(a=1):\n    return a + 2\n")
    assert digest(original) != digest('def run(a=1):\n    "doc"\n    return a + 2\n')


def test_guard_schema_requires_one_and_normalizes_both():
    with pytest.raises(ValidationError, match="at least one symbol guard"):
        ReplaceSymbolParameters(path="a.py", symbol="run", new_content="")
    value = ReplaceSymbolParameters(
        path="a.py", symbol="run", new_content="", expected_ast_sha256="A" * 64
    )
    assert value.expected_ast_sha256 == "a" * 64
    assert value.expected_sha256 is None


def test_structural_edit_runs_and_audit_reports_hash(tmp_path):
    workspace = init_workspace(tmp_path).workspace
    source = "# untouched\r\ndef run( ):\r\n    return (1)\r\n"
    target = tmp_path / "module.py"
    target.write_bytes(source.encode())
    audit = Job(
        protocol=1,
        project_id=workspace.project_id,
        id="AUDIT-AST",
        kind="audit",
        actions=[{"read_symbol": {"path": "module.py", "symbol": "run"}}],
    )
    result = execute_plan(plan_job(audit, workspace))
    assert f"ast_sha256: {digest(source)}" in result.audit_results[0].output
    assert (
        f"ast_python: {sys.version_info.major}.{sys.version_info.minor}"
        in result.audit_results[0].output
    )
    plan = plan_job(
        job(
            workspace,
            "def run():\n    return 2\n",
            digest("def run():\n    return 1\n"),
        ),
        workspace,
    )
    assert (
        plan.file_changes[0].content == b"# untouched\r\ndef run():\r\n    return 2\r\n"
    )
    execute_plan(plan, approved=True)
    assert "return 2" in target.read_text()


@pytest.mark.parametrize(
    "replacement",
    [
        "def run(:\n",
        "def other():\n    pass\n",
        "class run:\n    pass\n",
        "def run():\n    pass\nx = 1\n",
        "",
        "def run():\n    break\n",
    ],
)
def test_invalid_replacements_are_rejected_before_writes(tmp_path, replacement):
    workspace = init_workspace(tmp_path).workspace
    original = "def run():\n    return 1\n"
    target = tmp_path / "module.py"
    target.write_text(original)
    with pytest.raises(PlanningError) as caught:
        plan_job(job(workspace, replacement, digest(original)), workspace)
    assert caught.value.code is PlanningErrorCode.PYTHON_SOURCE_INVALID
    assert target.read_text() == original


def test_semantic_drift_and_strict_combined_guard(tmp_path):
    workspace = init_workspace(tmp_path).workspace
    target = tmp_path / "module.py"
    target.write_text("def run():\n    return 3\n")
    with pytest.raises(PlanningError) as caught:
        plan_job(
            job(
                workspace,
                "def run():\n    return 2\n",
                digest("def run():\n    return 1\n"),
            ),
            workspace,
        )
    assert caught.value.code is PlanningErrorCode.PYTHON_SYMBOL_GUARD_MISMATCH
    with pytest.raises(PlanningError):
        plan_job(
            job(
                workspace,
                "def run():\n    return 2\n",
                digest(target.read_text()),
                expected_sha256="0" * 64,
            ),
            workspace,
        )


def test_old_job_serialization_keeps_registry_hash_compatible():
    original = dict(
        path="module.py",
        symbol="run",
        expected_sha256="a" * 64,
        new_content="def run():\n    return 2\n",
    )
    assert ReplaceSymbolParameters(**original).model_dump(mode="json") == original


def test_formatter_handles_unicode_paths_under_ascii_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    monkeypatch.setenv("PYTHONUTF8", "0")
    root = tmp_path / "проєкт"
    root.mkdir()
    workspace = init_workspace(root).workspace
    value = Job(
        protocol=1,
        project_id=workspace.project_id,
        id="PATCH-UNICODE",
        kind="patch",
        actions=[
            {
                "create_file": {
                    "path": "модуль.py",
                    "content": "import sys\nimport os\n\nTEXT='Привіт'\n",
                }
            }
        ],
        checks=[{"compileall": {"paths": ["модуль.py"]}}],
    )
    result = execute_plan(plan_job(value, workspace), approved=True)
    assert all(item.success for item in result.formatting_results)
    assert "Привіт" in (root / "модуль.py").read_text(encoding="utf-8")
    assert all(
        "\ufffd" not in item.stdout + item.stderr for item in result.formatting_results
    )


@pytest.mark.parametrize(
    ("original", "symbol", "replacement"),
    [
        (
            "class Service:\n    value = 1\n",
            "Service",
            "class Service:\n    value = 2\n",
        ),
        (
            "class Service:\n    async def run(self):\n        return 1\n",
            "Service.run",
            "    async def run(self):\n        return 2\n",
        ),
    ],
)
def test_class_and_async_method_structural_edits(
    tmp_path, original, symbol, replacement
):
    workspace = init_workspace(tmp_path).workspace
    (tmp_path / "module.py").write_text(original)
    value = Job(
        protocol=1,
        project_id=workspace.project_id,
        id="PATCH-NESTED",
        kind="patch",
        actions=[
            {
                "replace_symbol": {
                    "path": "module.py",
                    "symbol": symbol,
                    "expected_ast_sha256": digest(original, symbol),
                    "new_content": replacement,
                }
            }
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )
    plan = plan_job(value, workspace)
    assert b"2" in plan.file_changes[0].content


def test_replacement_cannot_move_method_outside_its_class(tmp_path):
    workspace = init_workspace(tmp_path).workspace
    original = "class Service:\n    value = 1\n    def run(self):\n        return 1\n"
    (tmp_path / "module.py").write_text(original)
    value = Job(
        protocol=1,
        project_id=workspace.project_id,
        id="PATCH-SCOPE",
        kind="patch",
        actions=[
            {
                "replace_symbol": {
                    "path": "module.py",
                    "symbol": "Service.run",
                    "expected_ast_sha256": digest(original, "Service.run"),
                    "new_content": "def run(self):\n    return 2\n",
                }
            }
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )
    with pytest.raises(PlanningError, match="preserve unique symbol resolution"):
        plan_job(value, workspace)


def test_ast_guard_preserves_existing_exact_noop_rule(tmp_path):
    workspace = init_workspace(tmp_path).workspace
    original = "def run():\n    return 1\n"
    (tmp_path / "module.py").write_text(original)
    assert not plan_job(job(workspace, original, "0" * 64), workspace).file_changes

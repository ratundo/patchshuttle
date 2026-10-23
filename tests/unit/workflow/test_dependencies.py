"""Static dependency candidates, scope limits and non-execution contract."""

import json

import pytest
from pydantic import ValidationError

from patchshuttle import Job, execute_plan, plan_job
from patchshuttle.actions import python_dependencies
from patchshuttle.analysis.dependencies import analyze_dependencies, cycle_groups
from patchshuttle.errors import PlanningError
from patchshuttle.selfdoc import render_capabilities, render_explanation, render_schema
from patchshuttle.workspace import init_workspace


def test_relative_namespace_package_cycles_and_dynamic_hints():
    files = {
        "pkg/__init__.py": "from . import a\n",
        "pkg/a.py": "from .b import value\nfrom . import *\n",
        "pkg/b.py": "import pkg.a\n",
        "ns/x.py": "from . import y\n",
        "ns/y.py": "",
        "other.py": "import external\nfrom .. import outside\nfrom importlib import import_module as load\nload('x')\nthing.import_module(name)\n__import__(name)\nnormal_call()\n",
        "self.py": "import self\n",
        "broken.py": "def broken(\n",
        "dup.py": "",
        "dup/__init__.py": "",
        "__init__.py": "",
        "bad-name.py": "",
    }
    report = analyze_dependencies(files)
    kinds = {f["kind"] for f in report["findings"]}
    assert kinds == {
        "local_import_candidate",
        "external_or_unresolved",
        "relative_import_outside_root",
        "star_import",
        "dynamic_import_hint",
        "parse_error",
        "ambiguous_module",
        "unknown_module_name",
    }
    assert report["cycle_groups"] == [["pkg", "pkg.a", "pkg.b"], ["self"]]
    assert sum(f["kind"] == "dynamic_import_hint" for f in report["findings"]) == 3
    assert any(
        f.get("source") == "ns.x" and f.get("target") == "ns.y"
        for f in report["findings"]
    )
    assert "dup" not in report["modules"]
    assert report == analyze_dependencies(dict(reversed(list(files.items()))))
    limited = analyze_dependencies(files, 1)
    assert limited["findings_limit_reached"]
    assert len(limited["findings"]) == 1
    assert limited["cycle_groups"] == report["cycle_groups"]


def test_nested_imports_external_members_and_parent_packages():
    report = analyze_dependencies(
        {
            "pkg/__init__.py": "from . import leaf\n",
            "pkg/leaf.py": "value = 1\n",
            "pkg/sub/__init__.py": "from ..leaf import value\n",
            "client.py": "def run():\n    import pkg.leaf\nif False:\n    from pkg.leaf import value\nfrom unknown import x\n",
        }
    )
    assert ["pkg"] not in report["cycle_groups"]
    assert any(
        f.get("source") == "pkg.sub" and f.get("target") == "pkg.leaf"
        for f in report["findings"]
    )
    assert any(f.get("target") == "unknown" for f in report["findings"])
    assert not report["findings_limit_reached"]
    assert analyze_dependencies({})["cycle_groups"] == []


def test_iterative_cycle_detection_handles_long_chains_and_multiple_components():
    graph = {str(i): {str(i + 1)} for i in range(1500)}
    graph["1500"] = set()
    assert cycle_groups(graph) == []
    graph["1500"].add("1499")
    graph["isolated"] = {"isolated"}
    assert cycle_groups(graph) == [["1499", "1500"], ["isolated"]]


def audit(ws, action, identifier="AUDIT-DEPS"):
    return Job(
        protocol=1,
        project_id=ws.project_id,
        id=identifier,
        kind="audit",
        actions=[action],
    )


def test_public_audit_respects_import_root_policy_and_never_executes(tmp_path):
    ws = init_workspace(tmp_path).workspace
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.py").write_bytes(
        b"\xef\xbb\xbfimport b\r\nraise RuntimeError('must not run')\r\n"
    )
    (root / "b.py").write_text("import a\n# Привіт\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "secret.py").write_text("import hidden\n", encoding="utf-8")
    action = python_dependencies("src")
    result = execute_plan(plan_job(audit(ws, action), ws))
    report = json.loads(result.audit_results[0].output)
    assert report["modules"] == {"a": "a.py", "b": "b.py"}
    assert report["cycle_groups"] == [["a", "b"]]
    assert report["files_considered"] == 2 and not report["file_limit_reached"]
    assert report["import_root"] == "src"
    assert result.workspace_comparison.success
    limited = execute_plan(
        plan_job(audit(ws, python_dependencies("src", max_files=1), "AUDIT-LIMIT"), ws)
    )
    value = json.loads(limited.audit_results[0].output)
    assert value["file_limit_reached"] and value["files_considered"] == 1
    tiny = execute_plan(
        plan_job(audit(ws, python_dependencies("src", max_bytes=40), "AUDIT-BYTES"), ws)
    )
    assert len(tiny.audit_results[0].output.encode("utf-8")) <= 40
    assert "TRUNCATED" in tiny.audit_results[0].output
    with pytest.raises(PlanningError):
        plan_job(audit(ws, python_dependencies("src/a.py")), ws)
    assert "python_dependencies" in render_capabilities()
    assert "explicit import-root" in render_explanation("python_dependencies")
    assert "PythonDependenciesParameters" in json.loads(render_schema())["$defs"]


@pytest.mark.parametrize(
    "parameters",
    [
        {"max_files": 0},
        {"max_files": 1001},
        {"max_findings": 10001},
        {"max_bytes": 1000001},
        {"max_files": True},
    ],
)
def test_dependency_bounds(parameters):
    with pytest.raises(ValidationError):
        python_dependencies(**parameters)

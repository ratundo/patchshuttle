"""Reference candidates are read-only and explicitly lack binding resolution."""

import json

import pytest

from patchshuttle import Job, execute_plan, plan_job
from patchshuttle.actions import python_references
from patchshuttle.analysis.references import analyze_references
from patchshuttle.selfdoc import render_capabilities, render_explanation
from patchshuttle.workspace import init_workspace

SOURCE = "class Service:\n    def run(self):\n        return 1\nService.run\n"


def test_aliases_relative_imports_and_unresolved_attributes():
    files = {
        "pkg/services.py": SOURCE,
        "pkg/client.py": "from .services import Service as S\nS.run()\nimport pkg.services as mod\nmod.Service.run()\nimport pkg.services\npkg.services.Service.run()\nfrom pkg import services as other\nother.Service.run()\nobj.run()\nfactory().run()\nfrom .services import *\n",
        "pkg/__init__.py": "from .services import Service\nService.run()\n",
        "bad.py": "from .. import outside\n",
        "broken.py": "def broken(\n",
        "__init__.py": "",
    }
    report = analyze_references(files, "pkg.services", "Service.run")
    assert report["target_status"] == "resolved_declaration"
    findings = report["findings"]
    expressions = {
        f.get("expression")
        for f in findings
        if f["kind"] == "syntactic_reference_candidate"
    }
    assert {
        "S.run",
        "mod.Service.run",
        "pkg.services.Service.run",
        "other.Service.run",
        "Service.run",
    } <= expressions
    assert sum(f["kind"] == "unresolved_attribute_candidate" for f in findings) == 2
    assert {
        "star_import_unresolved",
        "relative_import_outside_root",
        "parse_error",
        "unknown_module_name",
    } <= {f["kind"] for f in findings}
    assert report["binding_analysis"] == "not_performed"
    assert not report["findings_limit_reached"]
    assert report == analyze_references(
        dict(reversed(list(files.items()))), "pkg.services", "Service.run"
    )
    limited = analyze_references(files, "pkg.services", "Service.run", 1)
    assert limited["findings_limit_reached"] and len(limited["findings"]) == 1


def test_class_references_and_shadowing_are_only_candidates():
    report = analyze_references(
        {
            "services.py": SOURCE,
            "client.py": "from services import Service as S\nfrom unrelated import Service as S\ndef f(S):\n    S.run()\n    other()\nx = 1\n",
        },
        "services",
        "Service",
    )
    assert any(f.get("expression") == "S" for f in report["findings"])
    assert any(f.get("qualified") == "services.Service.run" for f in report["findings"])
    assert "scope shadowing" in report["limitations"][0]


@pytest.mark.parametrize(
    "files,status",
    [
        ({}, "module_not_found_in_scan"),
        ({"services.py": SOURCE, "services/__init__.py": SOURCE}, "ambiguous_module"),
        ({"services.py": "def broken("}, "invalid_target_source"),
        ({"services.py": "VALUE = 1"}, "declaration_not_unique_or_missing"),
        ({"services.py": SOURCE}, "resolved_declaration"),
    ],
)
def test_target_resolution_is_explicit(files, status):
    assert (
        analyze_references(files, "services", "Service.run")["target_status"] == status
    )


def test_public_reference_audit_preserves_files_and_limits(tmp_path):
    ws = init_workspace(tmp_path).workspace
    (tmp_path / "services.py").write_bytes(SOURCE.encode("utf-8"))
    (tmp_path / "client.py").write_bytes(
        b"from services import Service as S\r\nS.run()\r\nraise RuntimeError('must not run')\r\n"
    )

    def run(action, identifier):
        job = Job(
            protocol=1,
            project_id=ws.project_id,
            id=identifier,
            kind="audit",
            actions=[action],
        )
        return execute_plan(plan_job(job, ws))

    result = run(python_references("services", "Service.run"), "AUDIT-REFS")
    report = json.loads(result.audit_results[0].output)
    assert report["target_status"] == "resolved_declaration"
    assert report["files_considered"] == 2
    assert result.workspace_comparison.success
    assert (tmp_path / "services.py").read_bytes() == SOURCE.encode("utf-8")
    limited = run(
        python_references("services", "Service.run", max_files=1), "AUDIT-REFS-LIMIT"
    )
    assert json.loads(limited.audit_results[0].output)["file_limit_reached"]
    truncated = run(
        python_references("services", "Service.run", max_bytes=40), "AUDIT-REFS-BYTES"
    )
    assert "TRUNCATED" in truncated.audit_results[0].output
    assert "python_references" in render_capabilities()
    assert "No binding/type inference" in render_explanation("python_references")

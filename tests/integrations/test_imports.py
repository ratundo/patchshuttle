"""Structural import guards and preservation through the public execution API."""

import pytest

from patchshuttle import Job, execute_plan
from patchshuttle.actions import ensure_import, read_imports, remove_import
from patchshuttle.errors import ExecutionError, PlanningError
from patchshuttle.planner import plan_job
from patchshuttle.planning.imports import edit_import, inspect_imports
from patchshuttle.workspace import init_workspace


def edit(text, statement="import os", *, remove=False, expected=None):
    return edit_import(
        text, statement, expected or inspect_imports(text)[2], remove=remove
    )


def test_hash_ignores_formatting_comments_but_tracks_aliases():
    assert (
        inspect_imports("from os import (path,) # note\n")[2]
        == inspect_imports("from os import path\n")[2]
    )
    assert (
        inspect_imports("import os\n")[2] != inspect_imports("import os as system\n")[2]
    )
    assert (
        edit("import os\n", expected=inspect_imports("import os")[2].upper())
        == "import os\n"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", "import os\n"),
        ("# header", "# header\nimport os\n"),
        ("# header\nx = 1\n", "# header\nimport os\nx = 1\n"),
        ('"doc"', '"doc"\nimport os\n'),
        (
            '"doc"\nfrom __future__ import annotations\nx = 1\n',
            '"doc"\nfrom __future__ import annotations\nimport os\nx = 1\n',
        ),
        (
            "@decorate\ndef run():\n    pass\n",
            "import os\n@decorate\ndef run():\n    pass\n",
        ),
        ("def run():\n    pass\n", "import os\ndef run():\n    pass\n"),
        ("class A:\n    pass\n", "import os\nclass A:\n    pass\n"),
    ],
)
def test_insert_keeps_headers_docstrings_futures_and_decorators(source, expected):
    assert edit(source) == expected


def test_relative_future_and_dotted_imports():
    assert edit('"doc"\nimport os\n', "from __future__ import annotations").startswith(
        '"doc"\nfrom __future__'
    )
    assert (
        edit("", "from .tools import helper as h") == "from .tools import helper as h\n"
    )
    assert edit("", "import os.path") == "import os.path\n"
    assert edit("import sys, os # stay\n") == "import sys, os # stay\n"
    assert edit("x = 1\n", remove=True) == "x = 1\n"
    assert edit("import os as system\n", "import os as system", remove=True) == ""


def test_remove_preserves_comments_and_unrelated_non_ascii_source():
    source = '# шапка\nfrom os import (\n # пояснення\n path,  # кінець\n) # після\nx = "текст"\n'
    assert (
        edit(source, "from os import path", remove=True)
        == '# шапка\n# пояснення\n# кінець\n# після\nx = "текст"\n'
    )
    assert edit("import os", remove=True) == ""


@pytest.mark.parametrize(
    ("source", "statement", "remove", "message"),
    [
        ("", "x = 1", False, "exactly one"),
        ("", "import os, sys", False, "exactly one"),
        ("", "from os import *", False, "exactly one"),
        ("", "import os; import sys", False, "exactly one"),
        ("import os\nimport os\n", "import os", False, "ambiguous"),
        ("import os, sys\n", "import os", True, "multi-alias"),
        ("import os; x = 1\n", "import os", True, "standalone"),
        ("x = 1; import os\n", "import os", True, "standalone"),
        ('"doc"; x = 1\n', "import os", False, "standalone"),
        ("import sys as os\n", "import os", False, "import binding"),
        ("from tools import *\n", "import os", False, "import binding"),
        ("os = 1\n", "import os", False, "Python binding"),
        ("def os():\n    pass\n", "import os", False, "Python binding"),
        ("async def os():\n    pass\n", "import os", False, "Python binding"),
        ("class os:\n    pass\n", "import os", False, "Python binding"),
        ("def f(os):\n    pass\n", "import os", False, "Python binding"),
        (
            "try:\n    pass\nexcept Exception as os:\n    pass\n",
            "import os",
            False,
            "Python binding",
        ),
        (
            "match value:\n    case os:\n        pass\n",
            "import os",
            False,
            "Python binding",
        ),
        (
            "match value:\n    case [*os]:\n        pass\n",
            "import os",
            False,
            "Python binding",
        ),
        (
            "match value:\n    case {**os}:\n        pass\n",
            "import os",
            False,
            "Python binding",
        ),
    ],
)
def test_rejects_ambiguous_unsafe_requests(source, statement, remove, message):
    with pytest.raises(ValueError, match=message):
        edit(source, statement, remove=remove)


def test_guard_before_noop_and_invalid_python():
    with pytest.raises(ValueError, match="guard"):
        edit("import os\n", expected="0" * 64)
    with pytest.raises(SyntaxError):
        inspect_imports("return 1\n")
    with pytest.raises(SyntaxError):
        edit("", "import ???")
    with pytest.raises(SyntaxError):
        edit("", "from __future__ import unknown_feature")


def job(ws, action, *, audit=False):
    return Job(
        protocol=1,
        project_id=ws.project_id,
        id="AUDIT-IMPORTS" if audit else "PATCH-IMPORTS",
        kind="audit" if audit else "patch",
        actions=[action],
        checks=[] if audit else [{"import_check": {"modules": ["json"]}}],
    )


def test_public_roundtrip_guard_and_encoding(tmp_path):
    ws = init_workspace(tmp_path).workspace
    source = b"# coding: utf-8\r\nVALUE = 1\r\n"
    target = tmp_path / "a.py"
    target.write_bytes(source)
    guard = inspect_imports(source.decode())[2]
    action = ensure_import("a.py", "import os", expected_imports_ast_sha256=guard)
    plan = plan_job(job(ws, action), ws)
    assert target.read_bytes() == source
    assert b"import os\r\n" in plan.file_changes[0].content
    result = execute_plan(plan, approved=True)
    assert result.workspace_comparison.success
    assert "import os\n" in target.read_text()
    audit_plan = plan_job(job(ws, read_imports("a.py"), audit=True), ws)
    audit_result = execute_plan(audit_plan, approved=True)
    assert "imports_ast_sha256:" in audit_result.audit_results[0].output
    assert "import os" in audit_result.audit_results[0].output
    new_guard = inspect_imports(target.read_text())[2]
    removal = remove_import("a.py", "import os", expected_imports_ast_sha256=new_guard)
    assert plan_job(job(ws, removal), ws).files_to_modify
    with pytest.raises(PlanningError, match="guard"):
        plan_job(job(ws, action), ws)
    assert read_imports("a.py", max_bytes=128).parameters.max_bytes == 128


def test_planner_and_audit_fail_closed(tmp_path):
    ws = init_workspace(tmp_path).workspace
    for path, source in [("a.py", "def broken("), ("a.txt", "")]:
        (tmp_path / path).write_text(source)
        action = ensure_import(path, "import os", expected_imports_ast_sha256="0" * 64)
        with pytest.raises(PlanningError):
            plan_job(job(ws, action), ws)
    with pytest.raises(PlanningError):
        plan_job(job(ws, read_imports("a.txt"), audit=True), ws)
    plan = plan_job(job(ws, read_imports("a.py"), audit=True), ws)
    with pytest.raises(ExecutionError):
        execute_plan(plan, approved=True)

"""Audit drafts preserve guards, content, bounds and legacy serialization."""

import json

import pytest
from pydantic import ValidationError

from patchshuttle import Job, execute_plan, plan_job
from patchshuttle.actions import read_imports, read_symbol
from patchshuttle.models import Action
from patchshuttle.planning.templates import append_template
from patchshuttle.selfdoc import render_explanation
from patchshuttle.workspace import init_workspace


def audit(ws, action, identifier="AUDIT-DRAFT"):
    job = Job(
        protocol=1,
        project_id=ws.project_id,
        id=identifier,
        kind="audit",
        actions=[action],
    )
    return execute_plan(plan_job(job, ws)).audit_results[0].output


def fragment(output):
    return json.loads(output.split("```json\n", 1)[1].rsplit("\n```", 1)[0])[0]


def patch(ws, action):
    return Job(
        protocol=1,
        project_id=ws.project_id,
        id="PATCH-DRAFT",
        kind="patch",
        actions=[action],
        checks=[{"compileall": {"paths": ["модуль.py"]}}],
    )


def test_symbol_draft_roundtrip_indentation_unicode_and_stale_guard(tmp_path):
    ws = init_workspace(tmp_path).workspace
    source = (
        'class Service:\n    @staticmethod\n    def run():\n        return "Привіт"\n'
    )
    target = tmp_path / "модуль.py"
    raw = b"\xef\xbb\xbf" + source.replace("\n", "\r\n").encode("utf-8")
    target.write_bytes(raw)
    action = read_symbol(
        "модуль.py", "Service.run", include_template=True, max_bytes=50000
    )
    output = audit(ws, action)
    draft = fragment(output)
    parameters = draft["replace_symbol"]
    assert parameters["new_content"].startswith("    @staticmethod\n    def run")
    assert "\r" not in parameters["new_content"]
    assert parameters["expected_sha256"] and parameters["expected_ast_sha256"]
    assert not plan_job(patch(ws, draft), ws).file_changes
    parameters["new_content"] = parameters["new_content"].replace("Привіт", "Вітаю")
    planned = plan_job(patch(ws, draft), ws)
    assert "Вітаю".encode("utf-8") in planned.file_changes[0].content
    assert target.read_bytes() == raw
    target.write_text(source.replace("Привіт", "Змінено"), encoding="utf-8")
    from patchshuttle.errors import PlanningError

    with pytest.raises(PlanningError, match="guard"):
        plan_job(patch(ws, draft), ws)


def test_import_skeleton_requires_explicit_edits(tmp_path):
    ws = init_workspace(tmp_path).workspace
    target = tmp_path / "модуль.py"
    target.write_text("import os\n", encoding="utf-8")
    output = audit(ws, read_imports("модуль.py", include_template=True))
    draft = fragment(output)
    assert draft["edit_imports"]["edits"] == []
    with pytest.raises(ValidationError):
        Action(draft)
    draft["edit_imports"]["edits"] = [
        dict(operation="remove", statement="import os"),
        dict(operation="ensure", statement="import sys"),
    ]
    plan = plan_job(patch(ws, draft), ws)
    assert plan.file_changes[0].content == b"import sys\n"
    assert target.read_text(encoding="utf-8") == "import os\n"


@pytest.mark.parametrize(
    "reader,extra", [(read_symbol, ["Service.run"]), (read_imports, [])]
)
def test_template_opt_in_and_output_budget(tmp_path, reader, extra):
    ws = init_workspace(tmp_path).workspace
    target = tmp_path / "модуль.py"
    target.write_text(
        "class Service:\n    def run(self):\n        return 1\n", encoding="utf-8"
    )
    plain = reader("модуль.py", *extra)
    explicit = reader("модуль.py", *extra, include_template=False)
    assert plain.model_dump(mode="json") == explicit.model_dump(mode="json")
    assert "include_template" not in json.dumps(plain.model_dump(mode="json"))
    original = audit(ws, plain)
    assert "action_template" not in original
    limit = len(original.encode("utf-8")) + 60
    omitted = audit(
        ws,
        reader("модуль.py", *extra, include_template=True, max_bytes=limit),
        "AUDIT-OMIT",
    )
    assert "OMITTED_MAX_BYTES" in omitted
    assert "```json" not in omitted
    assert len(omitted.encode("utf-8")) <= limit
    tiny = audit(
        ws,
        reader("модуль.py", *extra, include_template=True, max_bytes=8),
        "AUDIT-TINY",
    )
    assert len(tiny.encode("utf-8")) <= 8
    with pytest.raises(ValidationError):
        reader("модуль.py", *extra, include_template="yes")
    assert "include_template" in render_explanation(plain.name)


def test_fragment_is_all_or_nothing_at_exact_byte_boundary():
    action = {"replace_symbol": {"path": "а.py", "new_content": "    # ```\n"}}
    full = append_template("source", action, 10000)
    size = len(full.encode("utf-8"))
    assert append_template("source", action, size) == full
    assert "```json" not in append_template("source", action, size - 1)
    assert fragment(full) == action

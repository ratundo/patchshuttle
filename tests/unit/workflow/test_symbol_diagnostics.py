"""Indentation failures remain read-only and explain raw replacement layout."""

import pytest

from patchshuttle import Job
from patchshuttle._python_symbols import find_symbol_source
from patchshuttle.errors import PlanningError, PlanningErrorCode
from patchshuttle.planner import plan_job
from patchshuttle.planning.symbols import replacement_diagnostics
from patchshuttle.workspace import init_workspace


@pytest.mark.parametrize("prefix", ["    ", "\t"])
@pytest.mark.parametrize("decorated", [False, True])
def test_nested_replacement_reports_indentation_without_rewriting(
    tmp_path, prefix, decorated
):
    workspace = init_workspace(tmp_path).workspace
    decorator = prefix + "@staticmethod\n" if decorated else ""
    original = (
        "class Service:\n"
        + decorator
        + prefix
        + "def run():\n"
        + prefix * 2
        + "return 1\n"
    )
    target = tmp_path / "module.py"
    target.write_bytes(original.replace("\n", "\r\n").encode("utf-8"))
    before = target.read_bytes()
    source = find_symbol_source(original, "Service.run", filename="module.py")
    replacement = (
        "@staticmethod\n" if decorated else ""
    ) + "def run():\n    return 2\n"
    job = Job(
        protocol=1,
        project_id=workspace.project_id,
        id="PATCH-INDENT",
        kind="patch",
        actions=[
            {
                "replace_symbol": {
                    "path": "module.py",
                    "symbol": "Service.run",
                    "expected_ast_sha256": source.ast_sha256,
                    "new_content": replacement,
                }
            }
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )
    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)
    assert caught.value.code is PlanningErrorCode.PYTHON_SOURCE_INVALID
    message = str(caught.value)
    assert f"source_first_code_indent: {prefix!r}" in message
    assert "replacement_first_code_indent: ''" in message
    assert "YAML block indentation" in message
    assert "No automatic reindentation" in message
    assert target.read_bytes() == before
    payload = job.model_dump(mode="json")
    payload["actions"][0]["replace_symbol"]["new_content"] = (
        source.selected.content.replace("return 1", "return 2")
    )
    job = Job.model_validate(payload)
    plan = plan_job(job, workspace)
    assert b"return 2\r\n" in plan.file_changes[0].content
    assert target.read_bytes() == before


def test_diagnostic_prefixes_are_bounded_and_ignore_comment_only_lines():
    details = replacement_diagnostics(
        " " * 200 + "def run():\n", "\n# note\n\tdef run():\n"
    )
    assert "characters: 200" in details[0]
    assert len(details[0]) < 160
    assert "'\\t'" in details[1]
    assert (
        "replacement_first_code_indent: ''"
        in replacement_diagnostics("", "# only comment\n")[1]
    )

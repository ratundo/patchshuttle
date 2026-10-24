"""Documentation contracts and runnable indentation-sensitive examples."""

import re
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from patchshuttle import Job, plan_job
from patchshuttle._python_symbols import find_symbol_source
from patchshuttle.models import AUDIT_ACTION_NAMES, CHANGE_ACTION_NAMES, Action, Check
from patchshuttle.planning.guidance import SYMBOL_WORKFLOW_EXAMPLE
from patchshuttle.selfdoc import CHECKS, render_explanation
from patchshuttle.workspace import init_workspace


def document(name):
    return files("patchshuttle.resources").joinpath(name).read_text(encoding="utf-8")


def listed_operations(text, heading):
    section = text.split("## " + heading + "\n", 1)[1].split("\n## ", 1)[0]
    names = set()
    for line in section.splitlines():
        if line.startswith("- `"):
            head = line.split(":", 1)[0].split(" - ", 1)[0]
            names.update(re.findall(r"`([a-z_]+)`", head))
    return names


@pytest.mark.parametrize("name", ["AI_GUIDE.md", "PATCHSHUTTLE_PROTOCOL.md"])
def test_primary_operation_catalog_matches_installed_models(name):
    text = document(name)
    assert listed_operations(text, "Audit actions") == AUDIT_ACTION_NAMES
    assert listed_operations(text, "Change actions") == CHANGE_ACTION_NAMES
    if name == "AI_GUIDE.md":
        assert listed_operations(text, "Checks") == set(CHECKS)


def test_guide_yaml_examples_validate_against_installed_models():
    text = document("AI_GUIDE.md").replace("{{PROJECT_ID}}", "PSH-0123456789ABCDEF")
    blocks = re.findall(r"```yaml\n(.*?)\n```", text, re.S)
    assert len(blocks) >= 10
    for block in blocks:
        block = re.sub(r"<[^>]+>", "a" * 64, block)
        value = yaml.safe_load(block)
        if isinstance(value, list):
            for action in value:
                Action(action)
        elif "protocol" in value:
            Job.model_validate(value)
        else:
            for action in value.get("actions", []):
                Action(action)
            for check in value.get("checks", []):
                Check(check)


@pytest.mark.parametrize("surface", ["guide", "handoff", "explain", "readme"])
def test_documented_method_yaml_preserves_python_indent_and_plans(tmp_path, surface):
    if surface == "guide":
        blocks = re.findall(r"```yaml\n(.*?)\n```", document("AI_GUIDE.md"), re.S)
        fragment = next(
            block for block in blocks if block.startswith("- replace_symbol:")
        )
    elif surface == "handoff":
        fragment = (
            "- replace_symbol:"
            + SYMBOL_WORKFLOW_EXAMPLE.split("- replace_symbol:", 1)[1].split(
                "Explicit |2", 1
            )[0]
        )
    elif surface == "readme":
        text = (Path(__file__).resolve().parents[3] / "README.md").read_text(
            encoding="utf-8"
        )
        fragment = next(
            block
            for block in re.findall(r"```yaml\n(.*?)\n```", text, re.S)
            if "- replace_symbol:" in block
        )
    else:
        fragment = render_explanation("replace_symbol").split("example:\n", 1)[1]
    parsed = yaml.safe_load(fragment)
    action = parsed["actions"][0] if isinstance(parsed, dict) else parsed[0]
    parameters = action["replace_symbol"]
    assert parameters["new_content"].startswith("    def run(self")
    assert "\n        return" in parameters["new_content"]
    ws = init_workspace(tmp_path).workspace
    path = tmp_path / parameters["path"]
    path.parent.mkdir(parents=True)
    original = "class Service:\n    def run(self):\n        return 1\n"
    path.write_bytes(original.encode("utf-8"))
    symbol = find_symbol_source(original, "Service.run", filename=path.name)
    parameters["expected_sha256"] = symbol.selected.sha256
    parameters["expected_ast_sha256"] = symbol.ast_sha256
    job = Job(
        protocol=1,
        project_id=ws.project_id,
        id="PATCH-DOC-EXAMPLE",
        kind="patch",
        actions=[action],
        checks=[{"compileall": {"paths": [parameters["path"]]}}],
    )
    plan = plan_job(job, ws)
    assert plan.file_changes
    assert path.read_bytes() == original.encode("utf-8")

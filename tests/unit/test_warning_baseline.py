"""Warning-baseline parsing and protected state contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import patchshuttle.workspace as workspace_module
from patchshuttle.errors import ExecutionError
from patchshuttle.warning_baseline import (
    WARNING_BASELINE_RELATIVE_PATH,
    WARNING_BASELINE_SCHEMA,
    analyze_django_warning_output,
    load_warning_baseline,
    normalize_warning_ids,
    update_warning_baseline,
)
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"


@pytest.fixture
def workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    return init_workspace(tmp_path).workspace


def test_django_warning_output_classifies_ids_and_retains_new_hints() -> None:
    stderr = (
        "System check identified some issues:\n\n"
        "WARNINGS:\n"
        "?: (urls.W005) URL namespace 'admin' isn't unique.\n"
        "\tHINT: Rename one namespace.\n"
        "app.Model: (models.W001) New model warning.\n"
        "\tHINT: Fix the model declaration.\n\n"
        "System check identified 2 issues (0 silenced).\n"
    )

    analysis = analyze_django_warning_output(
        "",
        stderr,
        known_ids=frozenset({"urls.W005"}),
        output_truncated=False,
    )

    assert analysis.status == "COMPLETE"
    assert analysis.known_warnings == 1
    assert analysis.new_warnings == 1
    assert analysis.new_warning_details == (
        "app.Model: (models.W001) New model warning.\n"
        "\tHINT: Fix the model declaration.",
    )


def test_unidentified_and_repeated_warning_occurrences_are_counted() -> None:
    output = (
        "WARNINGS:\n"
        "?: (urls.W005) First.\n"
        "?: (urls.W005) Second.\n"
        "?: Warning without an ID.\n"
    )

    analysis = analyze_django_warning_output(
        output,
        "",
        known_ids=frozenset({"urls.W005"}),
        output_truncated=False,
    )

    assert analysis.known_warnings == 2
    assert analysis.new_warnings == 1
    assert analysis.new_warning_details == ("?: Warning without an ID.",)


def test_truncated_django_output_is_not_misclassified() -> None:
    analysis = analyze_django_warning_output(
        "WARNINGS:\n?: (urls.W005) Partial",
        "",
        known_ids=frozenset({"urls.W005"}),
        output_truncated=True,
    )

    assert analysis.status == "INCOMPLETE_TRUNCATED"
    assert analysis.known_warnings is None
    assert analysis.new_warnings is None
    assert analysis.new_warning_details == ()


def test_warning_baseline_missing_roundtrip_and_strict_validation(
    workspace: Workspace,
) -> None:
    path = workspace.root / WARNING_BASELINE_RELATIVE_PATH
    path.unlink()
    assert load_warning_baseline(workspace).django_check_ids == ()

    updated = update_warning_baseline(
        workspace,
        add=("urls.W005", "models.W001"),
    )
    assert updated.django_check_ids == ("models.W001", "urls.W005")
    assert load_warning_baseline(workspace) == updated
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "django_check_ids": ["models.W001", "urls.W005"],
        "project_id": workspace.project_id,
        "schema": WARNING_BASELINE_SCHEMA,
    }

    removed = update_warning_baseline(workspace, remove=("models.W001",))
    assert removed.django_check_ids == ("urls.W005",)
    with pytest.raises(ValueError, match="applabel.W001"):
        normalize_warning_ids(("urls.E001",))

    path.write_text("{}\n", encoding="utf-8", newline="")
    with pytest.raises(ExecutionError, match="baseline is invalid"):
        load_warning_baseline(workspace)

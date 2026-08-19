"""Focused tests for AI-facing planning diagnostics and resolved previews."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.planner as planner_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle.errors import PlanningError, PlanningErrorCode
from patchshuttle.planner import plan_job, render_plan_diff
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    return init_workspace(tmp_path).workspace


def patch_job(actions: list[dict]) -> Job:
    return Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-UX-001",
        kind="patch",
        actions=actions,
        checks=[{"import_check": {"modules": ["json"]}}],
    )


def test_occurrence_mismatch_reports_exact_line_numbers(
    workspace: Workspace,
) -> None:
    target = workspace.root / "template.html"
    target.write_text("first\nneedle\nmiddle\nneedle\n", encoding="utf-8")

    with pytest.raises(PlanningError) as caught:
        plan_job(
            patch_job(
                [
                    {
                        "replace_exact": {
                            "path": "template.html",
                            "old": "needle",
                            "new": "replacement",
                        }
                    }
                ]
            ),
            workspace,
        )

    error = caught.value
    assert error.code is PlanningErrorCode.OCCURRENCE_COUNT_MISMATCH
    assert error.details[0] == "  exact_matches:"
    assert "line: 2" in error.details[1]
    assert "line: 4" in error.details[2]
    assert "diagnostics:\n" in str(error)


def test_occurrence_mismatch_reports_three_ranked_approximate_matches(
    workspace: Workspace,
) -> None:
    target = workspace.root / "settings.txt"
    target.write_text(
        "unrelated\ntarget = 10\ntarget = 30\ntarget = 40\n",
        encoding="utf-8",
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(
            patch_job(
                [
                    {
                        "replace_exact": {
                            "path": "settings.txt",
                            "old": "target = 20",
                            "new": "target = 21",
                        }
                    }
                ]
            ),
            workspace,
        )

    details = caught.value.details
    assert details[0] == "  closest_matches:"
    assert len(details) == 4
    assert "line: 2" in details[1]
    assert all("similarity:" in item for item in details[1:])


def test_occurrence_mismatch_handles_an_empty_source(workspace: Workspace) -> None:
    (workspace.root / "empty.txt").write_bytes(b"")

    with pytest.raises(PlanningError) as caught:
        plan_job(
            patch_job(
                [
                    {
                        "delete_exact": {
                            "path": "empty.txt",
                            "text": "missing",
                        }
                    }
                ]
            ),
            workspace,
        )

    assert caught.value.details == ("  closest_matches: none",)


def test_resolved_diff_covers_create_modify_and_missing_final_newline(
    workspace: Workspace,
) -> None:
    (workspace.root / "existing.txt").write_text("before\n", encoding="utf-8")
    plan = plan_job(
        patch_job(
            [
                {
                    "replace_exact": {
                        "path": "existing.txt",
                        "old": "before",
                        "new": "after",
                    }
                },
                {
                    "create_file": {
                        "path": "created.txt",
                        "content": "created without newline",
                    }
                },
            ]
        ),
        workspace,
    )

    preview = render_plan_diff(plan)

    assert preview.truncated is False
    assert "--- a/existing.txt\n+++ b/existing.txt\n" in preview.text
    assert "-before\n+after\n" in preview.text
    assert "--- /dev/null\n+++ b/created.txt\n" in preview.text
    assert "+created without newline\n\\ No newline at end of file\n" in preview.text


@pytest.mark.parametrize(
    ("maximum", "contains_marker"),
    ((12, False), (80, True)),
)
def test_resolved_diff_is_bounded(
    workspace: Workspace,
    maximum: int,
    contains_marker: bool,
) -> None:
    plan = plan_job(
        patch_job(
            [
                {
                    "create_file": {
                        "path": "large.txt",
                        "content": "x" * 300,
                    }
                }
            ]
        ),
        workspace,
    )

    preview = render_plan_diff(plan, maximum_bytes=maximum)

    assert preview.truncated is True
    assert len(preview.text.encode("utf-8")) <= maximum
    assert ("DIFF PREVIEW TRUNCATED" in preview.text) is contains_marker


def test_resolved_diff_is_empty_for_a_no_change_plan(workspace: Workspace) -> None:
    (workspace.root / "same.txt").write_bytes(b"same\n")
    plan = plan_job(
        patch_job([{"create_file": {"path": "same.txt", "content": "same\n"}}]),
        workspace,
    )

    assert render_plan_diff(plan).text == ""
    with pytest.raises(ValueError, match="cannot be negative"):
        render_plan_diff(plan, maximum_bytes=-1)


@pytest.mark.parametrize(
    ("bom", "codec"),
    (
        (b"\xef\xbb\xbf", "utf-8"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
    ),
)
def test_resolved_diff_decodes_supported_bom_files(
    workspace: Workspace,
    bom: bytes,
    codec: str,
) -> None:
    (workspace.root / "encoded.txt").write_bytes(bom + "old\n".encode(codec))
    plan = plan_job(
        patch_job(
            [
                {
                    "replace_exact": {
                        "path": "encoded.txt",
                        "old": "old",
                        "new": "new",
                    }
                }
            ]
        ),
        workspace,
    )

    assert "-old\n+new\n" in render_plan_diff(plan).text


def test_large_diagnostic_candidate_selection_is_bounded() -> None:
    lines = ["ordinary line"] * 6_100
    lines[5_900] = "very_distinctive_anchor is here"

    anchored = planner_module._diagnostic_candidate_starts(
        lines,
        "very_distinctive_anchor",
        width=1,
    )
    sampled = planner_module._diagnostic_candidate_starts(lines, "!!", width=1)
    sampled_without_anchor = planner_module._diagnostic_candidate_starts(
        lines,
        "missing_anchor",
        width=1,
    )
    repeated = planner_module._diagnostic_candidate_starts(
        ["repeated_anchor"] * 6_100,
        "repeated_anchor",
        width=2,
    )

    assert anchored == (5_900,)
    assert len(sampled) <= 5_000
    assert sampled[-1] == 6_099
    assert sampled_without_anchor[-1] == 6_099
    assert len(repeated) == 5_000


def test_preview_helpers_bound_long_values() -> None:
    assert planner_module._preview("x" * 500).endswith("...")
    assert len(planner_module._preview("x" * 500)) == 180
    assert planner_module._line_number("a\nb\nc", 4) == 3


def test_plan_diff_public_record_is_immutable(workspace: Workspace) -> None:
    plan = plan_job(
        patch_job([{"create_file": {"path": "note.txt", "content": "note\n"}}]),
        workspace,
    )
    preview = render_plan_diff(plan)

    assert preview.text
    assert preview.truncated is False
    assert plan.file_changes[0].before_content is None
    assert plan.file_changes[0].path == PurePosixPath("note.txt")

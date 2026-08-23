"""Safety and execution contracts for canonical guarded line ranges."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.workspace as workspace_module
from patchshuttle import Job, RunStatus, execute_plan
from patchshuttle._line_ranges import (
    LineRangeBoundsError,
    canonical_lines,
    canonical_sha256,
    normalize_newlines,
    select_line_range,
)
from patchshuttle.cli import _render_plan
from patchshuttle.errors import PlanningError, PlanningErrorCode
from patchshuttle.planner import ActionDisposition, NewlineStyle, plan_job
from patchshuttle.runner import TransactionStatus, execute_change_transaction
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


def _patch_job(actions: list[dict], *, job_id: str = "PATCH-RANGE-001") -> Job:
    return Job(
        protocol=1,
        project_id=PROJECT_ID,
        id=job_id,
        kind="patch",
        actions=actions,
        checks=[{"import_check": {"modules": ["json"]}}],
    )


def _audit_job(action: dict, *, job_id: str = "AUDIT-RANGE-001") -> Job:
    return Job(
        protocol=1,
        project_id=PROJECT_ID,
        id=job_id,
        kind="audit",
        actions=[action],
    )


def test_canonical_line_helpers_are_lf_stable_and_do_not_add_phantom_lines() -> None:
    assert normalize_newlines("one\r\ntwo\rthree") == "one\ntwo\nthree"
    assert canonical_lines("") == ()
    assert canonical_lines("one\n") == ("one\n",)
    assert canonical_lines("one\ntwo") == ("one\n", "two")

    selected = select_line_range("one\r\ntwo\r\nthree", start_line=2, end_line=3)

    assert selected.content == "two\nthree"
    assert selected.start_offset == 4
    assert selected.end_offset == 13
    assert selected.total_lines == 3
    assert selected.ends_with_newline is False
    assert selected.sha256 == hashlib.sha256(b"two\nthree").hexdigest()
    assert canonical_sha256("two\r\nthree") == selected.sha256


@pytest.mark.parametrize(
    ("value", "start", "end", "total"),
    (
        ("", 1, 1, 0),
        ("one\n", 2, 2, 1),
        ("one", 1, 2, 1),
        ("one", 0, 1, 1),
        ("one\ntwo", 2, 1, 2),
    ),
)
def test_canonical_line_selection_rejects_every_out_of_bounds_range(
    value: str,
    start: int,
    end: int,
    total: int,
) -> None:
    with pytest.raises(LineRangeBoundsError) as caught:
        select_line_range(value, start_line=start, end_line=end)

    assert caught.value.start_line == start
    assert caught.value.end_line == end
    assert caught.value.total_lines == total


def test_plan_applies_ranges_sequentially_and_preserves_crlf(
    workspace: Workspace,
) -> None:
    target = workspace.root / "notes.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree")
    inserted_hash = canonical_sha256("inserted\n")
    job = _patch_job(
        [
            {
                "replace_range": {
                    "path": "notes.txt",
                    "start_line": 2,
                    "end_line": 2,
                    "expected_content": "two\r\n",
                    "new_content": "TWO\n",
                }
            },
            {
                "insert_at_line": {
                    "path": "notes.txt",
                    "line": 2,
                    "position": "after",
                    "content": "inserted\r\n",
                    "expected_content": "TWO\n",
                    "expected_sha256": canonical_sha256("TWO\n"),
                }
            },
            {
                "delete_range": {
                    "path": "notes.txt",
                    "start_line": 3,
                    "end_line": 3,
                    "expected_sha256": inserted_hash,
                }
            },
        ]
    )

    plan = plan_job(job, workspace)

    assert [item.disposition for item in plan.actions] == [
        ActionDisposition.MODIFY,
        ActionDisposition.MODIFY,
        ActionDisposition.MODIFY,
    ]
    assert plan.actions[0].detail is not None
    assert "lines: 2-2" in plan.actions[0].detail
    assert "guards: expected_content" in plan.actions[0].detail
    assert "position: after" in (plan.actions[1].detail or "")
    assert "guards: expected_content+expected_sha256" in (plan.actions[1].detail or "")
    assert "guard_status: PASS" in (plan.actions[2].detail or "")
    assert plan.files_to_modify == (PurePosixPath("notes.txt"),)
    assert plan.file_changes[0].newline is NewlineStyle.CRLF
    assert plan.file_changes[0].content == b"one\r\nTWO\r\nthree"
    rendered = _render_plan(plan)
    assert "  - action_001 replace_range MODIFY: notes.txt" in rendered
    assert "    lines: 2-2" in rendered
    assert "    guard_status: PASS" in rendered

    result = execute_change_transaction(plan, approved=True)

    assert result.status is TransactionStatus.APPLIED
    assert target.read_bytes() == b"one\r\nTWO\r\nthree"


def test_replace_range_can_be_a_guarded_no_change(workspace: Workspace) -> None:
    target = workspace.root / "notes.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    job = _patch_job(
        [
            {
                "replace_range": {
                    "path": "notes.txt",
                    "start_line": 2,
                    "end_line": 2,
                    "expected_content": "two\n",
                    "new_content": "two\n",
                }
            }
        ]
    )

    plan = plan_job(job, workspace)

    assert plan.actions[0].disposition is ActionDisposition.NO_CHANGE
    assert plan.file_changes == ()


def test_insert_before_and_empty_replacement_are_planned_exactly(
    workspace: Workspace,
) -> None:
    target = workspace.root / "notes.txt"
    target.write_bytes(b"one\ntwo\n")
    job = _patch_job(
        [
            {
                "insert_at_line": {
                    "path": "notes.txt",
                    "line": 1,
                    "position": "before",
                    "content": "zero\n",
                    "expected_content": "one\n",
                }
            },
            {
                "replace_range": {
                    "path": "notes.txt",
                    "start_line": 3,
                    "end_line": 3,
                    "expected_content": "two\n",
                    "new_content": "",
                }
            },
        ]
    )

    plan = plan_job(job, workspace)

    assert plan.file_changes[0].content == b"zero\none\n"
    assert "position: before" in (plan.actions[0].detail or "")


def test_range_change_of_python_file_enters_formatter_scope(
    workspace: Workspace,
) -> None:
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    job = _patch_job(
        [
            {
                "replace_range": {
                    "path": "module.py",
                    "start_line": 1,
                    "end_line": 1,
                    "expected_content": "VALUE = 1\n",
                    "new_content": "VALUE = 2\n",
                }
            }
        ]
    )

    plan = plan_job(job, workspace)

    assert plan.formatting_targets == (PurePosixPath("module.py"),)
    assert {(item.path, item.formatter) for item in plan.formatter_plan} == {
        (PurePosixPath("module.py"), "isort"),
        (PurePosixPath("module.py"), "black"),
    }


@pytest.mark.parametrize(
    ("action", "expected_detail"),
    (
        (
            {
                "replace_range": {
                    "path": "notes.txt",
                    "start_line": 1,
                    "end_line": 1,
                    "expected_content": "wrong\n",
                    "expected_sha256": canonical_sha256("first\n"),
                    "new_content": "new\n",
                }
            },
            "content_match: false",
        ),
        (
            {
                "delete_range": {
                    "path": "notes.txt",
                    "start_line": 2,
                    "end_line": 2,
                    "expected_sha256": "0" * 64,
                }
            },
            "sha256_match: false",
        ),
        (
            {
                "insert_at_line": {
                    "path": "notes.txt",
                    "line": 1,
                    "position": "before",
                    "content": "new\n",
                    "expected_content": "second\n",
                }
            },
            "first_mismatch_line: 1",
        ),
        (
            {
                "replace_range": {
                    "path": "notes.txt",
                    "start_line": 1,
                    "end_line": 2,
                    "expected_content": "first\nwrong\n",
                    "new_content": "new\n",
                }
            },
            "first_mismatch_line: 2",
        ),
        (
            {
                "delete_range": {
                    "path": "notes.txt",
                    "start_line": 1,
                    "end_line": 2,
                    "expected_content": "first\nsecond\nextra\n",
                }
            },
            "first_mismatch_line: 3",
        ),
    ),
)
def test_range_guards_fail_strictly_without_relocation(
    workspace: Workspace,
    action: dict,
    expected_detail: str,
) -> None:
    target = workspace.root / "notes.txt"
    target.write_text("first\nsecond\n", encoding="utf-8")
    before = target.read_bytes()

    with pytest.raises(PlanningError) as caught:
        plan_job(_patch_job([action]), workspace)

    assert caught.value.code is PlanningErrorCode.LINE_RANGE_GUARD_MISMATCH
    assert caught.value.item_id == "action_001"
    assert caught.value.path == "notes.txt"
    assert any(expected_detail in item for item in caught.value.details)
    assert any(item.startswith("  actual_sha256: ") for item in caught.value.details)
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    "action",
    (
        {
            "replace_range": {
                "path": "notes.txt",
                "start_line": 2,
                "end_line": 3,
                "expected_content": "two\nthree\n",
                "new_content": "new\n",
            }
        },
        {
            "insert_at_line": {
                "path": "notes.txt",
                "line": 2,
                "position": "after",
                "content": "new\n",
                "expected_content": "two\n",
            }
        },
    ),
)
def test_range_bounds_are_checked_against_current_physical_lines(
    workspace: Workspace,
    action: dict,
) -> None:
    (workspace.root / "notes.txt").write_text("one\n", encoding="utf-8")

    with pytest.raises(PlanningError) as caught:
        plan_job(_patch_job([action]), workspace)

    assert caught.value.code is PlanningErrorCode.LINE_RANGE_OUT_OF_BOUNDS
    assert "  requested_lines: 2-3" in caught.value.details or (
        "  requested_lines: 2-2" in caught.value.details
    )
    assert "  total_lines: 1" in caught.value.details
    assert "  valid_lines: 1-1" in caught.value.details


def test_empty_file_has_no_addressable_physical_line(workspace: Workspace) -> None:
    (workspace.root / "empty.txt").write_bytes(b"")
    action = {
        "delete_range": {
            "path": "empty.txt",
            "start_line": 1,
            "end_line": 1,
            "expected_sha256": hashlib.sha256(b"").hexdigest(),
        }
    }

    with pytest.raises(PlanningError) as caught:
        plan_job(_patch_job([action]), workspace)

    assert caught.value.code is PlanningErrorCode.LINE_RANGE_OUT_OF_BOUNDS
    assert "  total_lines: 0" in caught.value.details
    assert "  valid_lines: none (empty file)" in caught.value.details


def test_hash_range_audit_emits_reusable_canonical_guard_and_is_read_only(
    workspace: Workspace,
) -> None:
    target = workspace.root / "legacy.py"
    raw = b"# -*- coding: latin-1 -*-\r\nname = 'caf\xe9'\r\nlast = True"
    target.write_bytes(raw)
    expected_content = "name = 'caf\u00e9'\nlast = True"
    job = _audit_job(
        {
            "hash_range": {
                "path": "legacy.py",
                "start_line": 2,
                "end_line": 3,
            }
        }
    )

    plan = plan_job(job, workspace)
    assert plan.actions[0].detail == "lines: 2-3"

    result = execute_plan(plan)

    assert result.status is RunStatus.COMPLETED
    assert target.read_bytes() == raw
    output = result.audit_results[0].output
    assert "path: legacy.py" in output
    assert "start_line: 2" in output
    assert "end_line: 3" in output
    assert "total_lines: 3" in output
    assert "canonical_encoding: utf-8" in output
    assert "canonical_newline: lf" in output
    assert "final_newline_included: false" in output
    assert f"sha256: {canonical_sha256(expected_content)}" in output
    assert f"canonical_size_bytes: {len(expected_content.encode('utf-8'))}" in output


def test_hash_range_audit_rejects_invalid_bounds_during_plan(
    workspace: Workspace,
) -> None:
    (workspace.root / "notes.txt").write_text("one\n", encoding="utf-8")
    job = _audit_job(
        {
            "hash_range": {
                "path": "notes.txt",
                "start_line": 1,
                "end_line": 2,
            }
        }
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.LINE_RANGE_OUT_OF_BOUNDS


def test_utf8_bom_and_final_newline_are_preserved_by_range_change(
    workspace: Workspace,
) -> None:
    target = workspace.root / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfone\ntwo\n")
    job = _patch_job(
        [
            {
                "replace_range": {
                    "path": "bom.txt",
                    "start_line": 2,
                    "end_line": 2,
                    "expected_content": "two\n",
                    "new_content": "TWO\n",
                }
            }
        ]
    )

    plan = plan_job(job, workspace)

    assert plan.file_changes[0].encoding == "utf-8-sig"
    assert plan.file_changes[0].content == b"\xef\xbb\xbfone\nTWO\n"

"""Whole-file moves preserve bytes, modes, deletion state and rollback."""

import hashlib
import json
import stat
from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from patchshuttle import Job, execute_plan
from patchshuttle.actions import move_file
from patchshuttle.errors import ExecutionError, PlanningError, PolicyError
from patchshuttle.operations import rollback_job
from patchshuttle.planner import plan_job, render_plan_diff
from patchshuttle.workspace import init_workspace


def make_job(
    ws, content, *, path="old.txt", destination="pkg/new.txt", extra=(), checks=None
):
    return Job(
        protocol=1,
        project_id=ws.project_id,
        id="PATCH-FILE-MOVE",
        kind="patch",
        actions=[
            move_file(
                path, destination, expected_sha256=hashlib.sha256(content).hexdigest()
            ),
            *extra,
        ],
        checks=checks or [{"import_check": {"modules": ["json"]}}],
    )


def test_move_file_preserves_raw_bytes_modes_and_manual_rollback(tmp_path):
    ws = init_workspace(tmp_path).workspace
    raw = b"\xef\xbb\xbfhello\r\nworld\r\n"
    source = tmp_path / "old.txt"
    source.write_bytes(raw)
    source.chmod(0o640)
    mode = stat.S_IMODE(source.stat().st_mode)
    plan = plan_job(make_job(ws, raw), ws)
    assert plan.files_to_delete == (PurePosixPath("old.txt"),)
    assert plan.files_to_modify == ()
    assert "/dev/null" in render_plan_diff(plan).text
    result = execute_plan(plan, approved=True)
    assert not source.exists()
    dest = tmp_path / "pkg/new.txt"
    assert dest.read_bytes() == raw and stat.S_IMODE(dest.stat().st_mode) == mode
    assert result.deleted_files == (PurePosixPath("old.txt"),)
    assert result.workspace_comparison.success
    history = json.loads(result.history_path.read_text())
    assert "old.txt" in history["observed"]["files"]["deleted"]
    rollback_job(ws, "PATCH-FILE-MOVE", approved=True)
    assert source.read_bytes() == raw
    assert stat.S_IMODE(source.stat().st_mode) == mode
    assert not (tmp_path / "pkg").exists()


def test_empty_source_move_and_rollback(tmp_path):
    ws = init_workspace(tmp_path).workspace
    (tmp_path / "old.txt").write_bytes(b"")
    execute_plan(plan_job(make_job(ws, b""), ws), approved=True)
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "pkg/new.txt").read_bytes() == b""
    rollback_job(ws, "PATCH-FILE-MOVE", approved=True)
    assert (tmp_path / "old.txt").is_file()


def test_move_and_edit_python_formats_only_destination(tmp_path):
    ws = init_workspace(tmp_path).workspace
    raw = b"VALUE = 1\n"
    (tmp_path / "old.py").write_bytes(raw)
    value = make_job(
        ws,
        raw,
        path="old.py",
        destination="pkg/new.py",
        extra=[{"replace_exact": {"path": "pkg/new.py", "old": "1", "new": "2"}}],
        checks=[{"ruff": {}}, {"compileall": {"paths": ["pkg"]}}],
    )
    plan = plan_job(value, ws)
    assert plan.formatting_targets == (PurePosixPath("pkg/new.py"),)
    execute_plan(plan, approved=True)
    assert not (tmp_path / "old.py").exists()
    assert (tmp_path / "pkg/new.py").read_bytes() == b"VALUE = 2\n"


def test_check_failure_restores_moved_file(tmp_path):
    ws = init_workspace(tmp_path).workspace
    raw = b"original\n"
    (tmp_path / "old.txt").write_bytes(raw)
    value = make_job(
        ws, raw, checks=[{"import_check": {"modules": ["absent_module_for_test"]}}]
    )
    with pytest.raises(ExecutionError):
        execute_plan(plan_job(value, ws), approved=True)
    assert (tmp_path / "old.txt").read_bytes() == raw
    assert not (tmp_path / "pkg").exists()


@pytest.mark.parametrize(
    "destination",
    ["old.txt", "OLD.txt", "../escape.txt", "patches/state/a.txt", "exists.txt"],
)
def test_invalid_file_move_is_read_only(tmp_path, destination):
    ws = init_workspace(tmp_path).workspace
    (tmp_path / "old.txt").write_bytes(b"old")
    (tmp_path / "exists.txt").write_bytes(b"existing")
    with pytest.raises((PlanningError, PolicyError)):
        plan_job(make_job(ws, b"old", destination=destination), ws)
    assert (tmp_path / "old.txt").read_bytes() == b"old"
    assert (tmp_path / "exists.txt").read_bytes() == b"existing"


def test_stale_guard_and_reuse_are_rejected(tmp_path):
    ws = init_workspace(tmp_path).workspace
    (tmp_path / "old.txt").write_bytes(b"old")
    with pytest.raises(PlanningError, match="byte hash"):
        plan_job(make_job(ws, b"wrong"), ws)
    for extra in [
        {"create_file": {"path": "old.txt", "content": "x"}},
        {"replace_exact": {"path": "old.txt", "old": "old", "new": "x"}},
        {
            "move_file": {
                "path": "pkg/new.txt",
                "destination": "other.txt",
                "expected_sha256": "0" * 64,
            }
        },
    ]:
        with pytest.raises(PlanningError):
            plan_job(make_job(ws, b"old", extra=[extra]), ws)
    with pytest.raises(PlanningError):
        plan_job(
            make_job(ws, b"old", checks=[{"compileall": {"paths": ["old.txt"]}}]), ws
        )


def test_manual_rollback_refuses_recreated_source(tmp_path):
    ws = init_workspace(tmp_path).workspace
    (tmp_path / "old.txt").write_bytes(b"old")
    execute_plan(plan_job(make_job(ws, b"old"), ws), approved=True)
    (tmp_path / "old.txt").write_bytes(b"external")
    with pytest.raises(ExecutionError):
        rollback_job(ws, "PATCH-FILE-MOVE", approved=True)
    assert (tmp_path / "old.txt").read_bytes() == b"external"
    assert (tmp_path / "pkg/new.txt").read_bytes() == b"old"


def test_move_mode_and_deletion_verification(tmp_path):
    from patchshuttle.actions.create import verify_created_file
    from patchshuttle.actions.modify import verify_removed_file

    ws = init_workspace(tmp_path).workspace
    (tmp_path / "old.txt").write_bytes(b"old")
    plan = plan_job(make_job(ws, b"old"), ws)
    deleted = next(c for c in plan.file_changes if c.deleted)
    with pytest.raises(OSError, match="reappeared"):
        verify_removed_file(ws, deleted)
    execute_plan(plan, approved=True)
    created = next(c for c in plan.file_changes if not c.deleted)
    with pytest.raises(OSError, match="mode"):
        verify_created_file(ws, replace(created, mode=0))


@pytest.mark.parametrize(
    "field,value", [("sha256", "0" * 64), ("size", 1), ("mode", 420)]
)
def test_manual_rollback_rejects_corrupt_absence_fingerprint(tmp_path, field, value):
    ws = init_workspace(tmp_path).workspace
    (tmp_path / "old.txt").write_bytes(b"old")
    result = execute_plan(plan_job(make_job(ws, b"old"), ws), approved=True)
    manifest = result.backup_path / "manifest.json"
    data = json.loads(manifest.read_text())
    data["applied_states"]["old.txt"][field] = value
    manifest.write_text(json.dumps(data))
    with pytest.raises(ExecutionError):
        rollback_job(ws, "PATCH-FILE-MOVE", approved=True)
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "pkg/new.txt").read_bytes() == b"old"


@pytest.mark.parametrize("path", ["pkg", "pkg/new.txt"])
def test_manual_rollback_rejects_absent_created_entry(tmp_path, path):
    ws = init_workspace(tmp_path).workspace
    (tmp_path / "old.txt").write_bytes(b"old")
    result = execute_plan(plan_job(make_job(ws, b"old"), ws), approved=True)
    manifest = result.backup_path / "manifest.json"
    data = json.loads(manifest.read_text())
    data["applied_states"][path] = {
        "kind": "missing",
        "sha256": None,
        "size": 0,
        "mode": 0,
    }
    manifest.write_text(json.dumps(data))
    with pytest.raises(ExecutionError):
        rollback_job(ws, "PATCH-FILE-MOVE", approved=True)
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "pkg/new.txt").read_bytes() == b"old"


def test_capture_detects_recreated_move_source(tmp_path):
    from patchshuttle.backup import _capture_applied_states, prepare_backup

    ws = init_workspace(tmp_path).workspace
    (tmp_path / "old.txt").write_bytes(b"old")
    plan = plan_job(make_job(ws, b"old"), ws)
    backup = prepare_backup(plan)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/new.txt").write_bytes(b"old")
    with pytest.raises(ExecutionError, match="state could not be retained"):
        _capture_applied_states(backup)
    assert (tmp_path / "old.txt").read_bytes() == b"old"

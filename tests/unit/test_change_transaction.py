"""Contract tests for the Phase 9 all-change transaction core."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.actions as actions_module
import patchshuttle.backup as backup_module
import patchshuttle.runner as runner_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.planner import plan_job
from patchshuttle.rollback import RollbackResult
from patchshuttle.runner import TransactionStatus, execute_change_transaction
from patchshuttle.workspace import Workspace, discover_workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
RUN_TIMESTAMP = "2026_08_06_220000_000001"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    monkeypatch.setattr(backup_module, "_run_timestamp", lambda: RUN_TIMESTAMP)
    return init_workspace(tmp_path).workspace


def make_plan(workspace: Workspace, actions: list[dict]):
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-009",
        kind="patch",
        actions=actions,
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )
    return plan_job(job, workspace)


def manifest_path(workspace: Workspace) -> Path:
    return (
        workspace.root / "patches/backups/PATCH-009" / RUN_TIMESTAMP / "manifest.json"
    )


def test_change_transaction_applies_all_text_actions_from_final_plan(
    workspace: Workspace,
) -> None:
    module = workspace.root / "module.py"
    notes = workspace.root / "notes.txt"
    diff_target = workspace.root / "diff.txt"
    module_original = b"VALUE = 1\nREMOVE = True\n"
    notes_original = b"anchor\n"
    diff_original = b"old\n"
    module.write_bytes(module_original)
    notes.write_bytes(notes_original)
    diff_target.write_bytes(diff_original)
    module.chmod(0o640)
    module_mode = stat.S_IMODE(module.stat().st_mode)
    diff = """\
--- a/diff.txt
+++ b/diff.txt
@@ -1 +1 @@
-old
+new
"""
    plan = make_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            },
            {
                "insert_after": {
                    "path": "module.py",
                    "anchor": "VALUE = 2",
                    "content": "\nREADY = True",
                }
            },
            {
                "delete_exact": {
                    "path": "module.py",
                    "text": "REMOVE = True\n",
                }
            },
            {
                "insert_before": {
                    "path": "notes.txt",
                    "anchor": "anchor",
                    "content": "header\n",
                }
            },
            {"apply_diff": {"diff": diff}},
            {
                "create_file": {
                    "path": "src/generated.txt",
                    "content": "generated\n",
                }
            },
        ],
    )
    registry_before = (workspace.root / "patches/state/registry.json").read_bytes()

    result = execute_change_transaction(plan, approved=True)

    assert result.status is TransactionStatus.APPLIED
    assert result.modified_files == (
        PurePosixPath("module.py"),
        PurePosixPath("notes.txt"),
        PurePosixPath("diff.txt"),
    )
    assert result.created_files == (PurePosixPath("src/generated.txt"),)
    assert result.created_directories == (PurePosixPath("src"),)
    assert module.read_bytes() == b"VALUE = 2\nREADY = True\n"
    assert notes.read_bytes() == b"header\nanchor\n"
    assert diff_target.read_bytes() == b"new\n"
    assert (workspace.root / "src/generated.txt").read_bytes() == b"generated\n"
    assert stat.S_IMODE(module.stat().st_mode) == module_mode

    backup_root = manifest_path(workspace).parent
    assert (backup_root / "originals/module.py").read_bytes() == module_original
    assert (backup_root / "originals/notes.txt").read_bytes() == notes_original
    assert (backup_root / "originals/diff.txt").read_bytes() == diff_original
    manifest = json.loads(manifest_path(workspace).read_text("utf-8"))
    assert manifest["status"] == "COMPLETED"
    entries = {entry["path"]: entry for entry in manifest["entries"]}
    assert entries["src"] == {
        "kind": "directory",
        "original_state": "ABSENT",
        "path": "src",
    }
    assert entries["src/generated.txt"] == {
        "kind": "file",
        "original_state": "ABSENT",
        "path": "src/generated.txt",
    }
    assert entries["module.py"] == {
        "backup_path": "originals/module.py",
        "encoding": "utf-8",
        "kind": "file",
        "newline": "lf",
        "original_mode": module_mode,
        "original_sha256": hashlib.sha256(module_original).hexdigest(),
        "original_size": len(module_original),
        "original_state": "PRESENT",
        "path": "module.py",
    }
    assert list((workspace.root / "patches/logs").iterdir()) == []
    assert (workspace.root / "patches/state/registry.json").read_bytes() == (
        registry_before
    )


def test_change_transaction_no_change_does_not_create_backup(
    workspace: Workspace,
) -> None:
    target = workspace.root / "module.py"
    target.write_text("VALUE = 2\n", encoding="utf-8")
    plan = make_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
    )

    result = execute_change_transaction(plan, approved=True)

    assert result.status is TransactionStatus.NO_CHANGE
    assert result.modified_files == ()
    assert result.backup_path is None
    assert list((workspace.root / "patches/backups").iterdir()) == []


def test_second_modify_failure_restores_first_from_prepared_originals(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = workspace.root / "first.txt"
    second = workspace.root / "second.txt"
    first.write_text("old one\n", encoding="utf-8")
    second.write_text("old two\n", encoding="utf-8")
    first.chmod(0o600)
    first_mode = stat.S_IMODE(first.stat().st_mode)
    second.chmod(0o444 if first_mode != 0o444 else 0o666)
    second_mode = stat.S_IMODE(second.stat().st_mode)
    assert second_mode != first_mode
    plan = make_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "first.txt",
                    "old": "old one",
                    "new": "new one",
                }
            },
            {
                "replace_exact": {
                    "path": "second.txt",
                    "old": "old two",
                    "new": "new two",
                }
            },
        ],
    )
    real_replace = actions_module.atomic_replace_file
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        backup_root = manifest_path(workspace).parent
        assert (backup_root / "originals/first.txt").exists()
        assert (backup_root / "originals/second.txt").exists()
        if calls == 2:
            raise OSError("injected replace failure")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(actions_module, "atomic_replace_file", fail_second)

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is True
    assert first.read_text("utf-8") == "old one\n"
    assert second.read_text("utf-8") == "old two\n"
    assert stat.S_IMODE(first.stat().st_mode) == first_mode
    assert stat.S_IMODE(second.stat().st_mode) == second_mode
    manifest = json.loads(manifest_path(workspace).read_text("utf-8"))
    assert manifest["status"] == "ROLLED_BACK"
    second.chmod(first_mode)


@pytest.mark.parametrize(
    ("keep_changes", "auto_rollback"),
    [(True, True), (False, False)],
)
def test_failed_transaction_can_retain_partial_changes(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    keep_changes: bool,
    auto_rollback: bool,
) -> None:
    first = workspace.root / "first.txt"
    second = workspace.root / "second.txt"
    first.write_text("old one\n", encoding="utf-8")
    second.write_text("old two\n", encoding="utf-8")
    if not auto_rollback:
        config_path = workspace.root / "patches/patchshuttle.toml"
        config_path.write_text(
            config_path.read_text("utf-8").replace(
                "auto_rollback = true",
                "auto_rollback = false",
            ),
            encoding="utf-8",
        )
        workspace = discover_workspace(workspace.root)
    plan = make_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "first.txt",
                    "old": "old one",
                    "new": "new one",
                }
            },
            {
                "replace_exact": {
                    "path": "second.txt",
                    "old": "old two",
                    "new": "new two",
                }
            },
        ],
    )
    real_replace = actions_module.atomic_replace_file
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replace failure")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(actions_module, "atomic_replace_file", fail_second)

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(
            plan,
            approved=True,
            keep_changes=keep_changes,
        )

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is None
    assert caught.value.rollback_skipped is True
    assert caught.value.changes_kept is True
    assert first.read_text("utf-8") == "new one\n"
    assert second.read_text("utf-8") == "old two\n"
    manifest = json.loads(manifest_path(workspace).read_text("utf-8"))
    assert manifest["status"] == "CHANGES_KEPT"


def test_keep_changes_without_any_published_change_records_failed_backup(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan(
        workspace,
        [{"create_file": {"path": "new.txt", "content": "new\n"}}],
    )
    monkeypatch.setattr(
        actions_module,
        "atomic_create_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected failure")),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True, keep_changes=True)

    assert caught.value.rollback_skipped is True
    assert caught.value.changes_kept is False
    assert not (workspace.root / "new.txt").exists()
    manifest = json.loads(manifest_path(workspace).read_text("utf-8"))
    assert manifest["status"] == "FAILED"


def test_keep_changes_manifest_failure_preserves_original_failure_context(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan(
        workspace,
        [{"create_file": {"path": "new.txt", "content": "new\n"}}],
    )
    monkeypatch.setattr(
        actions_module,
        "atomic_create_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected failure")),
    )
    monkeypatch.setattr(
        runner_module,
        "update_backup",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ExecutionError(
                ExecutionErrorCode.BACKUP_FAILED,
                "retained failure status could not be recorded",
            )
        ),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True, keep_changes=True)

    assert caught.value.code is ExecutionErrorCode.BACKUP_FAILED
    assert caught.value.cause_code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_skipped is True
    assert caught.value.changes_kept is False


def test_keep_changes_respects_workspace_policy(workspace: Workspace) -> None:
    plan = make_plan(
        workspace,
        [{"create_file": {"path": "new.txt", "content": "new\n"}}],
    )
    execution = workspace.config.execution.model_copy(
        update={"allow_keep_changes": False}
    )
    configured = replace(
        workspace,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    plan = replace(plan, workspace=configured)

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True, keep_changes=True)

    assert caught.value.code is ExecutionErrorCode.KEEP_CHANGES_FORBIDDEN
    assert not (workspace.root / "new.txt").exists()
    assert list((workspace.root / "patches/backups").iterdir()) == []


def test_mixed_create_and_modify_failure_removes_created_target(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = workspace.root / "existing.txt"
    existing.write_text("old\n", encoding="utf-8")
    plan = make_plan(
        workspace,
        [
            {"create_file": {"path": "src/new.txt", "content": "new\n"}},
            {
                "replace_exact": {
                    "path": "existing.txt",
                    "old": "old",
                    "new": "changed",
                }
            },
        ],
    )
    monkeypatch.setattr(
        actions_module,
        "atomic_replace_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is True
    assert existing.read_text("utf-8") == "old\n"
    assert not (workspace.root / "src").exists()


def test_modified_post_state_failure_restores_original(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    plan = make_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
    )
    monkeypatch.setattr(
        actions_module,
        "verify_modified_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("post-state failed")),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.rollback_succeeded is True
    assert target.read_text("utf-8") == "VALUE = 1\n"


def test_race_before_atomic_replace_does_not_overwrite_external_content(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    plan = make_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
    )
    real_replace = actions_module.atomic_replace_file

    def race(*args, **kwargs):
        target.write_text("EXTERNAL = True\n", encoding="utf-8")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(actions_module, "atomic_replace_file", race)

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is True
    assert target.read_text("utf-8") == "EXTERNAL = True\n"


def test_race_during_original_capture_is_reported_as_stale(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    plan = make_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
    )
    real_prepare = runner_module.prepare_backup

    def race_then_prepare(current_plan):
        target.write_text("EXTERNAL = True\n", encoding="utf-8")
        return real_prepare(current_plan)

    monkeypatch.setattr(runner_module, "prepare_backup", race_then_prepare)

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.PLAN_STALE
    assert caught.value.path == "module.py"
    assert target.read_text("utf-8") == "EXTERNAL = True\n"


def test_restore_failure_is_reported_and_original_backup_is_preserved(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "module.py"
    original = b"VALUE = 1\n"
    target.write_bytes(original)
    plan = make_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
    )
    monkeypatch.setattr(
        actions_module,
        "verify_modified_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("post-state failed")),
    )
    monkeypatch.setattr(
        runner_module,
        "rollback_transaction",
        lambda *args, **kwargs: RollbackResult(
            removed_files=(),
            removed_directories=(),
            unresolved=(PurePosixPath("module.py"),),
            restored_files=(),
        ),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    backup = manifest_path(workspace).parent / "originals/module.py"
    assert caught.value.code is ExecutionErrorCode.ROLLBACK_FAILED
    assert caught.value.cause_code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is False
    assert backup.read_bytes() == original


def test_replace_error_tracks_modified_target_and_temp_for_rollback(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "module.py"
    original = b"VALUE = 1\n"
    target.write_bytes(original)
    plan = make_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
    )
    temporary = workspace.root / ".patchshuttle-injected.tmp"

    def fail_after_replacement(*args, **kwargs):
        target.write_bytes(plan.file_changes[0].content)
        temporary.write_bytes(b"temporary\n")
        raise actions_module.FileReplaceError(
            "cleanup failed",
            target_modified=True,
            temporary_path=temporary,
        )

    monkeypatch.setattr(
        actions_module,
        "atomic_replace_file",
        fail_after_replacement,
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is True
    assert target.read_bytes() == original
    assert not temporary.exists()


def test_replace_error_without_modified_target_rolls_back_normally(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    plan = make_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
    )
    monkeypatch.setattr(
        actions_module,
        "atomic_replace_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            actions_module.FileReplaceError(
                "replace failed",
                target_modified=False,
            )
        ),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is True
    assert target.read_text("utf-8") == "VALUE = 1\n"


def test_modified_file_backup_without_mode_fails_before_replacement(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    plan = make_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            }
        ],
    )
    real_prepare = runner_module.prepare_backup

    def prepare_without_mode(current_plan):
        backup = real_prepare(current_plan)
        incomplete = replace(backup.entries[0], original_mode=None)
        return replace(backup, entries=(incomplete,))

    monkeypatch.setattr(runner_module, "prepare_backup", prepare_without_mode)

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is True
    assert target.read_text("utf-8") == "VALUE = 1\n"


def test_defensive_post_state_rejects_unreferenced_modified_change(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = workspace.root / "first.txt"
    second = workspace.root / "second.txt"
    first.write_text("old one\n", encoding="utf-8")
    second.write_text("old two\n", encoding="utf-8")
    plan = make_plan(
        workspace,
        [
            {
                "replace_exact": {
                    "path": "first.txt",
                    "old": "old one",
                    "new": "new one",
                }
            },
            {
                "replace_exact": {
                    "path": "second.txt",
                    "old": "old two",
                    "new": "new two",
                }
            },
        ],
    )
    forged = replace(plan, actions=(plan.actions[0],))
    monkeypatch.setattr(runner_module, "_revalidate_plan", lambda plan: None)

    with pytest.raises(ExecutionError) as caught:
        execute_change_transaction(forged, approved=True)

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is True
    assert first.read_text("utf-8") == "old one\n"
    assert second.read_text("utf-8") == "old two\n"

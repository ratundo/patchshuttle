"""Contract tests for the Phase 8 create-only transaction core."""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path, PurePosixPath

import pytest
from filelock import FileLock

import patchshuttle.actions as actions_module
import patchshuttle.backup as backup_module
import patchshuttle.runner as runner_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle.actions import FilePublishError
from patchshuttle.backup import BackupStatus
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.planner import FileDisposition, plan_job
from patchshuttle.rollback import RollbackResult
from patchshuttle.runner import TransactionStatus, execute_create_transaction
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"
RUN_TIMESTAMP = "2026_08_06_190000_000001"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    monkeypatch.setattr(backup_module, "_run_timestamp", lambda: RUN_TIMESTAMP)
    return init_workspace(tmp_path).workspace


def make_job(
    *,
    kind: str = "patch",
    actions: list[dict] | None = None,
    checks: list[dict] | None = None,
) -> Job:
    return Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-001" if kind == "patch" else "VERIFY-001",
        kind=kind,
        actions=actions or [],
        checks=checks or [],
    )


def create_plan(workspace: Workspace, actions: list[dict]):
    return plan_job(
        make_job(
            actions=actions,
            checks=[{"import_check": {"modules": ["patchshuttle"]}}],
        ),
        workspace,
    )


def snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_create_transaction_writes_declared_targets_and_completed_manifest(
    workspace: Workspace,
) -> None:
    plan = create_plan(
        workspace,
        [
            {"create_directory": {"path": "src/example"}},
            {
                "create_file": {
                    "path": "src/example/one.txt",
                    "content": "one\n",
                }
            },
            {
                "create_file": {
                    "path": "src/example/two.txt",
                    "content": "two\n",
                }
            },
        ],
    )

    result = execute_create_transaction(plan, approved=True)

    expected_backup = workspace.root / "patches/backups/PATCH-001" / RUN_TIMESTAMP
    assert result.status is TransactionStatus.APPLIED
    assert result.plan is plan
    assert result.backup_path == expected_backup
    assert result.created_files == (
        PurePosixPath("src/example/one.txt"),
        PurePosixPath("src/example/two.txt"),
    )
    assert result.created_directories == (
        PurePosixPath("src"),
        PurePosixPath("src/example"),
    )
    assert (workspace.root / "src/example/one.txt").read_bytes() == b"one\n"
    assert (workspace.root / "src/example/two.txt").read_bytes() == b"two\n"
    assert not list((workspace.root / "src/example").glob(".patchshuttle-*.tmp"))

    manifest = json.loads((expected_backup / "manifest.json").read_text("utf-8"))
    assert manifest["manifest_version"] == 1
    assert manifest["project_id"] == PROJECT_ID
    assert manifest["job_id"] == "PATCH-001"
    assert manifest["job_hash"] == plan.job_hash
    assert manifest["run_timestamp"] == RUN_TIMESTAMP
    assert manifest["status"] == "COMPLETED"
    assert manifest["failure_code"] is None
    assert manifest["entries"] == [
        {"kind": "directory", "original_state": "ABSENT", "path": "src"},
        {
            "kind": "directory",
            "original_state": "ABSENT",
            "path": "src/example",
        },
        {
            "kind": "file",
            "original_state": "ABSENT",
            "path": "src/example/one.txt",
        },
        {
            "kind": "file",
            "original_state": "ABSENT",
            "path": "src/example/two.txt",
        },
    ]
    assert manifest["action_order"] == [
        "action_001:create_directory",
        "action_002:create_file",
        "action_003:create_file",
    ]
    assert manifest["formatting_targets"] == []

    with pytest.raises(FrozenInstanceError):
        result.status = TransactionStatus.NO_CHANGE  # type: ignore[misc]


def test_create_transaction_no_change_skips_backup(workspace: Workspace) -> None:
    existing = workspace.root / "existing"
    existing.mkdir()
    (existing / "same.txt").write_text("same\n", encoding="utf-8")
    plan = create_plan(
        workspace,
        [
            {"create_directory": {"path": "existing"}},
            {
                "create_file": {
                    "path": "existing/same.txt",
                    "content": "same\n",
                }
            },
        ],
    )

    result = execute_create_transaction(plan, approved=True)

    assert result.status is TransactionStatus.NO_CHANGE
    assert result.backup_path is None
    assert result.created_files == ()
    assert result.created_directories == ()
    assert list((workspace.root / "patches/backups").iterdir()) == []


def test_create_transaction_requires_approval_before_writing(
    workspace: Workspace,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    before = snapshot(workspace.root)

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=False)

    assert caught.value.code is ExecutionErrorCode.APPROVAL_REQUIRED
    assert caught.value.rollback_succeeded is None
    assert snapshot(workspace.root) == before
    assert not (workspace.root / "created.txt").exists()


def test_create_transaction_rejects_unsupported_kind_and_action(
    workspace: Workspace,
) -> None:
    verify = plan_job(
        make_job(
            kind="verify",
            checks=[{"import_check": {"modules": ["patchshuttle"]}}],
        ),
        workspace,
    )
    target = workspace.root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    edit = plan_job(
        make_job(
            actions=[
                {
                    "replace_exact": {
                        "path": "module.py",
                        "old": "VALUE = 1",
                        "new": "VALUE = 2",
                    }
                }
            ],
            checks=[{"import_check": {"modules": ["patchshuttle"]}}],
        ),
        workspace,
    )

    with pytest.raises(ExecutionError) as kind_error:
        execute_create_transaction(verify, approved=True)
    with pytest.raises(ExecutionError) as action_error:
        execute_create_transaction(edit, approved=True)

    assert kind_error.value.code is ExecutionErrorCode.JOB_KIND_UNSUPPORTED
    assert action_error.value.code is ExecutionErrorCode.ACTION_UNSUPPORTED
    assert action_error.value.item_id == "action_001"
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_create_transaction_rejects_a_stale_plan_before_backup(
    workspace: Workspace,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "raced.txt", "content": "planned\n"}}],
    )
    (workspace.root / "raced.txt").write_text("external\n", encoding="utf-8")

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.PLAN_STALE
    assert caught.value.path == "raced.txt"
    assert (workspace.root / "raced.txt").read_text("utf-8") == "external\n"
    assert list((workspace.root / "patches/backups").iterdir()) == []


def test_create_transaction_fails_fast_when_workspace_is_locked(
    workspace: Workspace,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    lock_path = workspace.root / "patches/state/run.lock"

    with FileLock(lock_path, timeout=0):
        with pytest.raises(ExecutionError) as caught:
            execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.WORKSPACE_LOCKED
    assert not (workspace.root / "created.txt").exists()
    assert list((workspace.root / "patches/backups").iterdir()) == []


def test_action_failure_rolls_back_created_files_and_directories(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(
        workspace,
        [
            {"create_file": {"path": "src/one.txt", "content": "one\n"}},
            {"create_file": {"path": "src/two.txt", "content": "two\n"}},
        ],
    )
    real_create = actions_module.atomic_create_file
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(actions_module, "atomic_create_file", fail_second)

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    backup_path = workspace.root / "patches/backups/PATCH-001" / RUN_TIMESTAMP
    manifest = json.loads((backup_path / "manifest.json").read_text("utf-8"))
    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is True
    assert caught.value.backup_path == backup_path
    assert not (workspace.root / "src").exists()
    assert manifest["status"] == "ROLLED_BACK"
    assert manifest["failure_code"] == "ACTION_FAILED"


def test_rollback_failure_is_reported_without_false_success(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(
        workspace,
        [
            {"create_file": {"path": "one.txt", "content": "one\n"}},
            {"create_file": {"path": "two.txt", "content": "two\n"}},
        ],
    )
    real_create = actions_module.atomic_create_file
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(actions_module, "atomic_create_file", fail_second)
    monkeypatch.setattr(
        runner_module,
        "rollback_created",
        lambda *args, **kwargs: RollbackResult(
            removed_files=(),
            removed_directories=(),
            unresolved=(PurePosixPath("one.txt"),),
        ),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    backup_path = workspace.root / "patches/backups/PATCH-001" / RUN_TIMESTAMP
    manifest = json.loads((backup_path / "manifest.json").read_text("utf-8"))
    assert caught.value.code is ExecutionErrorCode.ROLLBACK_FAILED
    assert caught.value.cause_code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is False
    assert caught.value.path == "one.txt"
    assert (workspace.root / "one.txt").read_bytes() == b"one\n"
    assert manifest["status"] == "ROLLBACK_FAILED"
    assert manifest["failure_code"] == "ACTION_FAILED"


def test_atomic_create_race_does_not_overwrite_external_file(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = workspace.root / "existing"
    parent.mkdir()
    plan = create_plan(
        workspace,
        [
            {
                "create_file": {
                    "path": "existing/raced.txt",
                    "content": "planned\n",
                }
            }
        ],
    )
    target = parent / "raced.txt"
    real_create = actions_module.atomic_create_file

    def race(*args, **kwargs):
        target.write_bytes(b"external\n")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(actions_module, "atomic_create_file", race)

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is True
    assert target.read_bytes() == b"external\n"


def test_backup_preflight_failure_never_changes_project(
    workspace: Workspace,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    backups = workspace.root / "patches/backups"
    backups.rmdir()
    backups.write_text("conflict\n", encoding="utf-8")

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.BACKUP_FAILED
    assert caught.value.rollback_succeeded is None
    assert not (workspace.root / "created.txt").exists()


def test_missing_or_unsafe_lock_file_is_rejected(
    workspace: Workspace,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    lock_path = workspace.root / "patches/state/run.lock"
    lock_path.unlink()

    with pytest.raises(ExecutionError) as missing:
        execute_create_transaction(plan, approved=True)

    assert missing.value.code is ExecutionErrorCode.WORKSPACE_LOCK_FAILED

    lock_path.mkdir()
    with pytest.raises(ExecutionError) as unsafe:
        execute_create_transaction(plan, approved=True)

    assert unsafe.value.code is ExecutionErrorCode.WORKSPACE_LOCK_FAILED
    assert not (workspace.root / "created.txt").exists()


def test_lock_operating_system_failure_has_stable_error(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )

    class BrokenLock:
        def __init__(self, *args, **kwargs):
            pass

        def acquire(self, *args, **kwargs):
            raise OSError("lock unavailable")

    monkeypatch.setattr(runner_module, "FileLock", BrokenLock)

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.WORKSPACE_LOCK_FAILED


def test_modify_file_disposition_is_rejected_even_with_create_action_name(
    workspace: Workspace,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    forged_change = replace(
        plan.file_changes[0],
        disposition=FileDisposition.MODIFY,
        before_sha256="0" * 64,
        before_size=0,
    )
    forged = replace(plan, file_changes=(forged_change,))

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(forged, approved=True)

    assert caught.value.code is ExecutionErrorCode.ACTION_UNSUPPORTED


def test_identical_external_create_still_makes_plan_stale(
    workspace: Workspace,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    (workspace.root / "created.txt").write_bytes(b"content\n")

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.PLAN_STALE
    assert caught.value.path == "created.txt"


def test_changed_parent_fingerprint_makes_plan_stale(
    workspace: Workspace,
) -> None:
    parent = workspace.root / "existing"
    parent.mkdir()
    plan = create_plan(
        workspace,
        [
            {
                "create_file": {
                    "path": "existing/created.txt",
                    "content": "content\n",
                }
            }
        ],
    )
    metadata = parent.stat()
    os.utime(
        parent,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.PLAN_STALE
    assert caught.value.path == "existing"


def test_second_changed_fingerprint_is_identified_after_first_match(
    workspace: Workspace,
) -> None:
    first = workspace.root / "first"
    second = workspace.root / "second"
    first.mkdir()
    second.mkdir()
    plan = create_plan(
        workspace,
        [
            {"create_file": {"path": "first/one.txt", "content": "one\n"}},
            {"create_file": {"path": "second/two.txt", "content": "two\n"}},
        ],
    )
    metadata = second.stat()
    os.utime(
        second,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.PLAN_STALE
    assert caught.value.path == "second"


def test_plan_metadata_mismatch_without_changed_path_is_stale(
    workspace: Workspace,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    forged = replace(plan, job_hash="0" * 64)

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(forged, approved=True)

    assert caught.value.code is ExecutionErrorCode.PLAN_STALE
    assert caught.value.path is None


def test_independent_directories_and_no_change_action_preserve_plan_order(
    workspace: Workspace,
) -> None:
    (workspace.root / "existing").mkdir()
    plan = create_plan(
        workspace,
        [
            {"create_directory": {"path": "existing"}},
            {"create_file": {"path": "alpha/one.txt", "content": "one\n"}},
            {"create_file": {"path": "beta/two.txt", "content": "two\n"}},
        ],
    )

    result = execute_create_transaction(plan, approved=True)

    assert result.created_directories == (
        PurePosixPath("alpha"),
        PurePosixPath("beta"),
    )
    assert result.created_files == (
        PurePosixPath("alpha/one.txt"),
        PurePosixPath("beta/two.txt"),
    )


def test_file_publish_error_tracks_target_and_temp_for_rollback(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    target = workspace.root / "created.txt"
    temporary = workspace.root / ".patchshuttle-injected.tmp"

    def fail_after_publication(*args, **kwargs):
        target.write_bytes(b"content\n")
        temporary.write_bytes(b"content\n")
        raise FilePublishError(
            "cleanup failed",
            target_created=True,
            temporary_path=temporary,
        )

    monkeypatch.setattr(actions_module, "atomic_create_file", fail_after_publication)

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is True
    assert not target.exists()
    assert not temporary.exists()


def test_file_publish_error_without_created_paths_rolls_back_normally(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )

    monkeypatch.setattr(
        actions_module,
        "atomic_create_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            FilePublishError("publish failed", target_created=False)
        ),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is True
    assert not (workspace.root / "created.txt").exists()


@pytest.mark.parametrize("extra_kind", ["directory", "file"])
def test_defensive_post_state_rejects_incomplete_forged_plan(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    extra_kind: str,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "src/created.txt", "content": "content\n"}}],
    )
    if extra_kind == "directory":
        forged = replace(
            plan,
            directories_to_create=(
                *plan.directories_to_create,
                PurePosixPath("orphan"),
            ),
        )
    else:
        extra = replace(
            plan.file_changes[0],
            path=PurePosixPath("src/orphan.txt"),
        )
        forged = replace(plan, file_changes=(*plan.file_changes, extra))
    monkeypatch.setattr(runner_module, "_revalidate_plan", lambda plan: None)

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(forged, approved=True)

    assert caught.value.code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is True
    assert not (workspace.root / "src").exists()


def test_existing_execution_error_keeps_its_location_and_rolls_back(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )

    monkeypatch.setattr(
        actions_module,
        "atomic_create_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ExecutionError(
                ExecutionErrorCode.ACTION_FAILED,
                "specific action failure",
                item_id="custom_item",
                path="custom-path",
            )
        ),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.message == "specific action failure"
    assert caught.value.item_id == "custom_item"
    assert caught.value.path == "custom-path"
    assert caught.value.rollback_succeeded is True


def test_completed_manifest_failure_rolls_back_project_changes(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    real_update = runner_module.update_backup

    def fail_completed(backup, status, **kwargs):
        if status is BackupStatus.COMPLETED:
            raise ExecutionError(
                ExecutionErrorCode.BACKUP_FAILED,
                "completed manifest failed",
            )
        return real_update(backup, status, **kwargs)

    monkeypatch.setattr(runner_module, "update_backup", fail_completed)

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.BACKUP_FAILED
    assert caught.value.rollback_succeeded is True
    assert not (workspace.root / "created.txt").exists()


def test_rolled_back_manifest_failure_preserves_project_rollback_result(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    monkeypatch.setattr(
        actions_module,
        "atomic_create_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )

    def fail_rollback_manifest(*args, **kwargs):
        raise ExecutionError(
            ExecutionErrorCode.BACKUP_FAILED,
            "rollback manifest failed",
        )

    monkeypatch.setattr(runner_module, "update_backup", fail_rollback_manifest)

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.BACKUP_FAILED
    assert caught.value.rollback_succeeded is True
    assert caught.value.cause_code is ExecutionErrorCode.ACTION_FAILED


def test_unexpected_rollback_exception_is_reported(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    monkeypatch.setattr(
        actions_module,
        "atomic_create_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )
    monkeypatch.setattr(
        runner_module,
        "rollback_created",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("rollback crashed")),
    )

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.ROLLBACK_FAILED
    assert caught.value.cause_code is ExecutionErrorCode.ACTION_FAILED
    assert caught.value.rollback_succeeded is False


def test_interrupt_after_first_write_rolls_back_before_propagation(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(
        workspace,
        [
            {"create_file": {"path": "src/one.txt", "content": "one\n"}},
            {"create_file": {"path": "src/two.txt", "content": "two\n"}},
        ],
    )
    real_create = actions_module.atomic_create_file
    calls = 0

    def interrupt_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return real_create(*args, **kwargs)

    monkeypatch.setattr(actions_module, "atomic_create_file", interrupt_second)

    with pytest.raises(KeyboardInterrupt):
        execute_create_transaction(plan, approved=True)

    backup_path = workspace.root / "patches/backups/PATCH-001" / RUN_TIMESTAMP
    manifest = json.loads((backup_path / "manifest.json").read_text("utf-8"))
    assert not (workspace.root / "src").exists()
    assert manifest["status"] == "ROLLED_BACK"
    assert manifest["failure_code"] == "ACTION_FAILED"


def test_interrupt_inside_rollback_is_not_misreported(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    monkeypatch.setattr(
        actions_module,
        "atomic_create_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )
    monkeypatch.setattr(
        runner_module,
        "rollback_created",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        execute_create_transaction(plan, approved=True)


def test_rollback_failure_survives_manifest_update_failure(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = create_plan(
        workspace,
        [{"create_file": {"path": "created.txt", "content": "content\n"}}],
    )
    monkeypatch.setattr(
        actions_module,
        "atomic_create_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )
    monkeypatch.setattr(
        runner_module,
        "rollback_created",
        lambda *args, **kwargs: RollbackResult(
            removed_files=(),
            removed_directories=(),
            unresolved=(PurePosixPath("created.txt"),),
        ),
    )

    def fail_status(*args, **kwargs):
        raise ExecutionError(
            ExecutionErrorCode.BACKUP_FAILED,
            "status write failed",
        )

    monkeypatch.setattr(runner_module, "update_backup", fail_status)

    with pytest.raises(ExecutionError) as caught:
        execute_create_transaction(plan, approved=True)

    assert caught.value.code is ExecutionErrorCode.ROLLBACK_FAILED
    assert caught.value.rollback_succeeded is False


def test_execution_error_string_includes_optional_location() -> None:
    located = ExecutionError(
        ExecutionErrorCode.ACTION_FAILED,
        "failed",
        item_id="action_001",
        path="file.txt",
    )
    unlocated = ExecutionError(ExecutionErrorCode.APPROVAL_REQUIRED, "approve")

    assert str(located) == "[ACTION_FAILED] action_001 file.txt: failed"
    assert str(unlocated) == "[APPROVAL_REQUIRED] approve"

"""Unit tests for atomic project-local registry transitions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import patchshuttle.registry as registry_module
import patchshuttle.workspace as workspace_module
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.models import JobKind
from patchshuttle.registry import (
    RegistryDecision,
    decide_job,
    get_job,
    load_registry,
    update_registry,
)
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


def record_run(
    workspace: Workspace,
    *,
    job_hash: str = "a" * 64,
    result: str = "COMPLETED",
    completed: bool = True,
):
    registry = load_registry(workspace)
    archive = workspace.root / "patches/applied/PATCH-014_run_aaaaaaaa.psh.yaml"
    archive.write_bytes(b"job\n")
    backup = workspace.root / "patches/backups/PATCH-014/run"
    backup.mkdir(parents=True)
    return update_registry(
        workspace,
        registry,
        job_id="PATCH-014",
        job_hash=job_hash,
        kind=JobKind.PATCH,
        occurred_at="2026-08-07T12:00:00+00:00",
        result=result,
        backup_path=backup,
        rollback_state="NOT_REQUIRED",
        archived_job_path=archive,
        completed=completed,
    )


def test_initial_registry_is_valid_and_unknown_job_may_proceed(
    workspace: Workspace,
) -> None:
    registry = load_registry(workspace)

    assert registry.project_id == PROJECT_ID
    assert registry.jobs == {}
    assert (
        decide_job(registry, job_id="PATCH-014", job_hash="a" * 64)
        is RegistryDecision.PROCEED
    )


def test_registry_update_round_trips_all_required_operational_fields(
    workspace: Workspace,
) -> None:
    record = record_run(workspace)
    loaded = load_registry(workspace)

    assert get_job(loaded, "PATCH-014") == record
    assert record.job_hash == "a" * 64
    assert record.kind == "patch"
    assert record.first_run_at == "2026-08-07T12:00:00+00:00"
    assert record.latest_run_at == record.first_run_at
    assert record.latest_result == "COMPLETED"
    assert record.backup_reference == "patches/backups/PATCH-014/run"
    assert record.rollback_state == "NOT_REQUIRED"
    assert record.archived_job_copy.startswith("patches/applied/")
    assert record.completed is True
    assert record.run_count == 1
    assert (
        decide_job(loaded, job_id="PATCH-014", job_hash="a" * 64)
        is RegistryDecision.ALREADY_APPLIED
    )
    assert (
        decide_job(loaded, job_id="PATCH-014", job_hash="b" * 64)
        is RegistryDecision.PATCH_ID_CONFLICT
    )


def test_failed_same_hash_can_retry_and_later_completion_preserves_first_run(
    workspace: Workspace,
) -> None:
    failed = record_run(
        workspace,
        result="ROLLED_BACK",
        completed=False,
    )
    assert failed.completed is False
    assert (
        decide_job(
            load_registry(workspace),
            job_id="PATCH-014",
            job_hash="a" * 64,
        )
        is RegistryDecision.PROCEED
    )

    archive = workspace.root / "patches/applied/PATCH-014_retry_aaaaaaaa.psh.yaml"
    archive.write_bytes(b"job\n")
    completed = update_registry(
        workspace,
        load_registry(workspace),
        job_id="PATCH-014",
        job_hash="a" * 64,
        kind=JobKind.PATCH,
        occurred_at="2026-08-07T12:01:00+00:00",
        result="COMPLETED",
        backup_path=None,
        rollback_state="NOT_REQUIRED",
        archived_job_path=archive,
        completed=True,
    )

    assert completed.first_run_at == failed.first_run_at
    assert completed.latest_run_at == "2026-08-07T12:01:00+00:00"
    assert completed.backup_reference == failed.backup_reference
    assert completed.completed is True
    assert completed.run_count == 2


def test_conflict_result_does_not_replace_established_hash_or_completion(
    workspace: Workspace,
) -> None:
    first = record_run(workspace)
    archive = workspace.root / "patches/failed/PATCH-014_conflict_bbbbbbbb.psh.yaml"
    archive.write_bytes(b"conflict\n")

    conflict = update_registry(
        workspace,
        load_registry(workspace),
        job_id="PATCH-014",
        job_hash="b" * 64,
        kind=JobKind.VERIFY,
        occurred_at="2026-08-07T12:02:00+00:00",
        result="PATCH_ID_CONFLICT",
        backup_path=None,
        rollback_state="NOT_STARTED",
        archived_job_path=archive,
        completed=False,
    )

    assert conflict.job_hash == first.job_hash
    assert conflict.kind == first.kind
    assert conflict.completed is True
    assert conflict.latest_result == "PATCH_ID_CONFLICT"
    assert conflict.run_count == 2


def test_registry_rejects_missing_job_invalid_json_and_project_mismatch(
    workspace: Workspace,
) -> None:
    with pytest.raises(ExecutionError) as missing:
        get_job(load_registry(workspace), "MISSING")
    assert missing.value.code is ExecutionErrorCode.JOB_NOT_FOUND

    path = workspace.root / "patches/state/registry.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ExecutionError) as invalid:
        load_registry(workspace)
    assert invalid.value.code is ExecutionErrorCode.OPERATIONAL_RECORD_FAILED

    path.write_text(
        json.dumps({"project_id": "PSH-0000000000000000", "jobs": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ExecutionError, match="project ID"):
        load_registry(workspace)


def test_registry_rejects_invalid_record_shapes(
    workspace: Workspace,
) -> None:
    path = workspace.root / "patches/state/registry.json"
    invalid_payloads = (
        [],
        {"project_id": PROJECT_ID, "jobs": []},
        {"project_id": PROJECT_ID, "jobs": {1: {}}},
        {
            "project_id": PROJECT_ID,
            "jobs": {"PATCH-014": {"job_id": 1}},
        },
    )
    for payload in invalid_payloads:
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ExecutionError) as caught:
            load_registry(workspace)
        assert caught.value.code is ExecutionErrorCode.OPERATIONAL_RECORD_FAILED

    path.write_text(
        json.dumps({"project_id": PROJECT_ID, "jobs": {}}),
        encoding="utf-8",
    )
    record_run(workspace)
    valid = json.loads(path.read_text("utf-8"))
    invalid_records = []
    not_an_object = json.loads(json.dumps(valid))
    not_an_object["jobs"]["PATCH-014"] = []
    invalid_records.append(not_an_object)
    wrong_identity = json.loads(json.dumps(valid))
    wrong_identity["jobs"]["PATCH-014"]["job_id"] = "PATCH-OTHER"
    invalid_records.append(wrong_identity)
    zero_runs = json.loads(json.dumps(valid))
    zero_runs["jobs"]["PATCH-014"]["run_count"] = 0
    invalid_records.append(zero_runs)
    invalid_optional = json.loads(json.dumps(valid))
    invalid_optional["jobs"]["PATCH-014"]["backup_reference"] = 123
    invalid_records.append(invalid_optional)
    for payload in invalid_records:
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ExecutionError) as caught:
            load_registry(workspace)
        assert caught.value.code is ExecutionErrorCode.OPERATIONAL_RECORD_FAILED

    with pytest.raises(TypeError):
        registry_module._parse_registry({"project_id": PROJECT_ID, "jobs": {1: {}}})


def test_registry_read_and_atomic_replace_failures_are_stable(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = workspace.root / "patches/state/registry.json"
    path.unlink()
    with pytest.raises(ExecutionError, match="could not be read"):
        load_registry(workspace)

    init_workspace(workspace.root)
    registry = load_registry(workspace)
    archive = workspace.root / "patches/failed/PATCH-014_failed_aaaaaaaa.psh.yaml"
    archive.write_bytes(b"job\n")
    monkeypatch.setattr(
        registry_module.os,
        "replace",
        lambda *args: (_ for _ in ()).throw(OSError("injected")),
    )
    with pytest.raises(ExecutionError, match="could not be written"):
        update_registry(
            workspace,
            registry,
            job_id="PATCH-014",
            job_hash="a" * 64,
            kind=JobKind.PATCH,
            occurred_at="2026-08-07T12:00:00+00:00",
            result="ACTION_FAILED",
            backup_path=None,
            rollback_state="NOT_STARTED",
            archived_job_path=archive,
            completed=False,
        )
    assert list(path.parent.glob(".registry-*.tmp")) == []


def test_registry_enforces_its_own_file_bound(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_module, "_MAX_REGISTRY_BYTES", 1)
    with pytest.raises(ExecutionError, match="could not be read"):
        load_registry(workspace)
